"""The ClickHouse :class:`~carto_core.store.model.EventStore`, the one a service runs against
(ADR 0046; spec 7.2, 9.1 to 9.5, 14.7).

Every query reads ``events`` or ``event_identifiers`` through clickhouse-connect with server-side
parameters only (``{name:Type}``): the SQL text is fixed, table and column names are constants,
and no value is ever interpolated (spec 14.7). Times travel as epoch milliseconds
(``fromUnixTimestamp64Milli(..., 'UTC')``), because the client's datetime binding drops the
milliseconds the window bounds carry.

Semantics follow :mod:`carto_core.store.model` and the reference
:class:`~carto_core.store.memory.InMemoryEventStore`; the contract suite in
``core/tests/test_core_store_contract.py`` runs both. Both tables are ``ReplacingMergeTree`` and
hold a redelivered batch twice until a merge runs (ADR 0034), so:

- an identifier row is one ``GROUP BY (event_id, field_ref, form)`` group with the earliest
  ``stored_at`` of its copies (``min``), and the token of the group is its smallest (during a
  key rotation one field and form may carry two key versions; the engine sees one);
- an event counts once: ``uniqExact(event_id)``, or a ``GROUP BY event_id``;
- the watermark (:meth:`ClickHouseStore.max_stored_at`) is the latest of those per-row minima,
  so a duplicate copy stored later is never "new" again;
- a quantile is the element at index ``floor(level * (n - 1))`` of the sorted values, computed
  explicitly with ``arraySort(groupArray(...))`` and a 1-based index (ClickHouse's own
  ``quantileExactLow`` differs for small arrays), and the lag is ``(ms_b - ms_a) / 1000`` in
  Float64, which is bit for bit what the reference computes.

:meth:`pair_join` is one statement: the per-token aggregates of both ends (two subqueries
joined on the token), a window ``row_number`` over the join-row product for the top-10
concentration, ``n_a`` and ``n_b`` as two scalar subqueries over the same window, and an outer
select that picks the quantiles out of the sorted arrays.
:meth:`cooccurrences` never self-joins the table: it groups each event's deduplicated ends into
a sorted array, expands the ``i < j`` pairs with array functions (at most 64 ends per event, spec
7.1), then aggregates per pair and per ``(pair, token)`` for the functional-dependency counts
of spec 9.4. Its result is bounded by :data:`MAX_COOCCURRENCE_ROWS` with a warning when the
bound is hit, because the caller cannot bound it.

Failures raise :class:`~carto_core.store.model.StoreError` naming the driver error class only,
never a parameter. Nothing here returns a raw value: tokens, shapes, counts and times only.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from clickhouse_connect.driver.exceptions import ClickHouseError

from carto_common.logging import get_logger
from carto_core.store.memory import CONCENTRATION_TOP_N
from carto_core.store.model import (
    CooccurrenceRow,
    EventKeys,
    FieldForm,
    HourlyVolume,
    IdentifierRow,
    PairJoin,
    StoreError,
    TemplateCount,
    Window,
)

__all__ = ["MAX_COOCCURRENCE_ROWS", "ClickHouseStore", "QueryClient"]

MAX_COOCCURRENCE_ROWS: Final = 100_000
"""Most pairs :meth:`ClickHouseStore.cooccurrences` returns; the rest are logged as dropped."""

log = get_logger(component="store")

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class _QueryResult(Protocol):
    @property
    def result_rows(self) -> Sequence[Sequence[Any]]: ...


class _StreamContext(Protocol):
    def __enter__(self) -> Any: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...


class QueryClient(Protocol):
    """The clickhouse-connect calls the store uses (a fake can stand in for tests)."""

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _QueryResult: ...

    def query_rows_stream(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _StreamContext: ...

    def ping(self) -> bool: ...


# -- fixed SQL ---------------------------------------------------------------------------------
#
# Every statement is one string literal (adjacent literals only): the window condition
# ``tenant_id = {tenant_id:String} AND observed_at >= ... AND observed_at < ...`` is written out
# in each, so no SQL text is ever assembled at runtime (spec 14.7).

_MAX_STORED_AT: Final = (
    "SELECT count(), max(stored_at) FROM ("
    "SELECT min(stored_at) AS stored_at FROM event_identifiers "
    "WHERE tenant_id = {tenant_id:String} GROUP BY event_id, field_ref, form)"
)

_IDENTIFIER_ROWS_HEAD: Final = (
    "SELECT min(token), field_ref, form, min(shape), event_id, min(system_id), "
    "min(observed_at), min(stored_at) AS stored_at "
    "FROM event_identifiers WHERE tenant_id = {tenant_id:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC')"
)
_FORMS_FILTER: Final = " AND form IN {forms:Array(String)}"
_IDENTIFIER_ROWS_GROUP: Final = " GROUP BY event_id, field_ref, form"
_STORED_AFTER: Final = " HAVING stored_at > fromUnixTimestamp64Milli({stored_after:Int64}, 'UTC')"

_TEMPLATE_COUNTS: Final = (
    "SELECT system_id, template_id, toDate(observed_at, 'UTC') AS day, "
    "uniqExact(event_id) AS events FROM events WHERE tenant_id = {tenant_id:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') "
    "GROUP BY system_id, template_id, day ORDER BY system_id, template_id, day"
)

# Quantiles use the reference definition explicitly (ClickHouse's own quantileExactLow differs
# for small arrays): the element at index floor(level * (n - 1)) of the sorted values, and
# ClickHouse arrays are 1-based. ``greatest(length, 1)`` only keeps the index valid for an empty
# array, whose values pair_join ignores (n_match is 0).
_PAIR_JOIN: Final = (
    "SELECT n_a, n_b, n_match, "
    "lags[toUInt32(floor(0.25 * (greatest(length(lags), 1) - 1))) + 1] AS lag_q25, "
    "lags[toUInt32(floor(0.5 * (greatest(length(lags), 1) - 1))) + 1] AS lag_median, "
    "lags[toUInt32(floor(0.75 * (greatest(length(lags), 1) - 1))) + 1] AS lag_q75, "
    "lag_positive, "
    "fan_ab[toUInt32(floor(0.5 * (greatest(length(fan_ab), 1) - 1))) + 1] AS fan_ab_median, "
    "fan_ab[toUInt32(floor(0.99 * (greatest(length(fan_ab), 1) - 1))) + 1] AS fan_ab_p99, "
    "fan_ba[toUInt32(floor(0.5 * (greatest(length(fan_ba), 1) - 1))) + 1] AS fan_ba_median, "
    "fan_ba[toUInt32(floor(0.99 * (greatest(length(fan_ba), 1) - 1))) + 1] AS fan_ba_p99, "
    "top_product, total_product "
    "FROM ("
    "SELECT "
    "(SELECT uniqExact(token) FROM event_identifiers WHERE tenant_id = {tenant_id:String} "
    "AND field_ref = {field_a:String} AND form = {form_a:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC')) AS n_a, "
    "(SELECT uniqExact(token) FROM event_identifiers WHERE tenant_id = {tenant_id:String} "
    "AND field_ref = {field_b:String} AND form = {form_b:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC')) AS n_b, "
    "count() AS n_match, "
    "arraySort(groupArray(lag)) AS lags, "
    "countIf(lag > 0) AS lag_positive, "
    "arraySort(groupArray(count_b)) AS fan_ab, "
    "arraySort(groupArray(count_a)) AS fan_ba, "
    "sumIf(product, rank <= {top_n:UInt64}) AS top_product, "
    "sum(product) AS total_product "
    "FROM ("
    "SELECT token, lag, count_a, count_b, product, "
    "row_number() OVER (ORDER BY product DESC) AS rank "
    "FROM ("
    "SELECT a.token AS token, "
    "(toUnixTimestamp64Milli(b.first_at) - toUnixTimestamp64Milli(a.first_at)) / 1000 AS lag, "
    "a.n_events AS count_a, b.n_events AS count_b, a.n_events * b.n_events AS product "
    "FROM ("
    "SELECT token, min(observed_at) AS first_at, uniqExact(event_id) AS n_events "
    "FROM event_identifiers WHERE tenant_id = {tenant_id:String} "
    "AND field_ref = {field_a:String} AND form = {form_a:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') GROUP BY token) AS a "
    "INNER JOIN ("
    "SELECT token, min(observed_at) AS first_at, uniqExact(event_id) AS n_events "
    "FROM event_identifiers WHERE tenant_id = {tenant_id:String} "
    "AND field_ref = {field_b:String} AND form = {form_b:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') GROUP BY token) AS b "
    "ON a.token = b.token)))"
)

_COOCCURRENCES: Final = (
    "WITH ends AS ("
    "SELECT event_id, field_ref, form, min(token) AS token_min, min(system_id) AS system_id_min "
    "FROM event_identifiers WHERE tenant_id = {tenant_id:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') "
    "GROUP BY event_id, field_ref, form), "
    "per_event AS ("
    "SELECT event_id, min(system_id_min) AS system_id, "
    "arraySort(groupArray((field_ref, form, token_min))) AS items "
    "FROM ends GROUP BY event_id HAVING length(items) >= 2), "
    "pairs AS ("
    "SELECT system_id, "
    "tupleElement(tupleElement(pair, 1), 1) AS field_a, "
    "tupleElement(tupleElement(pair, 1), 2) AS form_a, "
    "tupleElement(tupleElement(pair, 1), 3) AS token_a, "
    "tupleElement(tupleElement(pair, 2), 1) AS field_b, "
    "tupleElement(tupleElement(pair, 2), 2) AS form_b, "
    "tupleElement(tupleElement(pair, 2), 3) AS token_b "
    "FROM per_event ARRAY JOIN arrayFlatten(arrayMap(i -> arrayMap(j -> (items[i], items[j]), "
    "range(i + 1, length(items) + 1)), arrayEnumerate(items))) AS pair) "
    "SELECT sa.field_a, sa.form_a, sa.field_b, sa.form_b, sa.system_id, sa.events, "
    "sa.distinct_a, sa.single_b_a, sb.distinct_b, sb.single_a_b "
    "FROM ("
    "SELECT field_a, form_a, field_b, form_b, min(system_id) AS system_id, "
    "sum(events_a) AS events, count() AS distinct_a, countIf(partners_b = 1) AS single_b_a "
    "FROM ("
    "SELECT field_a, form_a, field_b, form_b, token_a, min(system_id) AS system_id, "
    "count() AS events_a, uniqExact(token_b) AS partners_b "
    "FROM pairs GROUP BY field_a, form_a, field_b, form_b, token_a) "
    "GROUP BY field_a, form_a, field_b, form_b) AS sa "
    "INNER JOIN ("
    "SELECT field_a, form_a, field_b, form_b, "
    "count() AS distinct_b, countIf(partners_a = 1) AS single_a_b "
    "FROM ("
    "SELECT field_a, form_a, field_b, form_b, token_b, uniqExact(token_a) AS partners_a "
    "FROM pairs GROUP BY field_a, form_a, field_b, form_b, token_b) "
    "GROUP BY field_a, form_a, field_b, form_b) AS sb "
    "ON sa.field_a = sb.field_a AND sa.form_a = sb.form_a "
    "AND sa.field_b = sb.field_b AND sa.form_b = sb.form_b "
    "WHERE sa.events >= {min_events:UInt64} "
    "ORDER BY sa.field_a, sa.form_a, sa.field_b, sa.form_b "
    "LIMIT {limit:UInt64}"
)

_HOURLY_VOLUMES: Final = (
    "SELECT system_id, toStartOfHour(observed_at) AS hour, uniqExact(event_id) AS events "
    "FROM events WHERE tenant_id = {tenant_id:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') "
    "GROUP BY system_id, hour ORDER BY system_id, hour"
)

# Aggregated columns carry names of their own (``template_id_min``, not ``template_id``): a
# ClickHouse alias that shadows the column it aggregates is inlined into outer expressions and
# turns ``groupArray((..., token))`` into ``groupArray((..., min(token)))``, ILLEGAL_AGGREGATION.
_EVENTS_WITH_KEYS_HEAD: Final = (
    "SELECT e.event_id, e.template_id_min, e.observed_at_min, e.attributes_any, i.triples FROM ("
    "SELECT event_id, min(template_id) AS template_id_min, min(observed_at) AS observed_at_min, "
    "any(attributes) AS attributes_any FROM events WHERE tenant_id = {tenant_id:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') "
    "AND system_id = {system_id:String} GROUP BY event_id) AS e "
    "LEFT JOIN ("
    "SELECT event_id, groupArray((field_ref, form, token_min)) AS triples FROM ("
    "SELECT event_id, field_ref, form, min(token) AS token_min FROM event_identifiers "
    "WHERE tenant_id = {tenant_id:String} "
    "AND observed_at >= fromUnixTimestamp64Milli({start:Int64}, 'UTC') "
    "AND observed_at < fromUnixTimestamp64Milli({end:Int64}, 'UTC') "
    "AND system_id = {system_id:String}"
)
_EVENTS_WITH_KEYS_TAIL: Final = (
    " GROUP BY event_id, field_ref, form) GROUP BY event_id) AS i ON e.event_id = i.event_id"
)


# -- helpers -----------------------------------------------------------------------------------


def _ms(at: datetime) -> int:
    """Epoch milliseconds of an aware datetime (the window bounds, the watermark)."""
    return int((at.astimezone(UTC) - _EPOCH).total_seconds() * 1000)


def _aware(value: Any) -> datetime:
    """A datetime from the client as an aware UTC datetime (the columns carry 'UTC')."""
    if not isinstance(value, datetime):
        msg = "the store returned a non-datetime where a time was expected"
        raise StoreError(msg)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _window_parameters(tenant_id: str, window: Window) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "start": _ms(window.start), "end": _ms(window.end)}


def _quantile(value: Any) -> float | None:
    """A quantile the statement picked out of a sorted array, as a float (``None`` if absent)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN guard


class ClickHouseStore:
    """See the module docstring. One client, sessionless, shared by the worker's threads."""

    __slots__ = ("_client",)

    def __init__(self, client: QueryClient) -> None:
        self._client = client

    def __repr__(self) -> str:
        return "ClickHouseStore()"

    # -- plumbing ---------------------------------------------------------------------------------

    def _query(self, what: str, sql: str, parameters: dict[str, Any]) -> list[Sequence[Any]]:
        try:
            return list(self._client.query(sql, parameters=parameters).result_rows)
        except (ClickHouseError, OSError) as exc:
            msg = f"store {what} failed: {type(exc).__name__}"
            raise StoreError(msg) from exc

    def _stream(self, what: str, sql: str, parameters: dict[str, Any]) -> Iterator[Sequence[Any]]:
        try:
            with self._client.query_rows_stream(sql, parameters=parameters) as stream:
                yield from stream
        except (ClickHouseError, OSError) as exc:
            msg = f"store {what} failed: {type(exc).__name__}"
            raise StoreError(msg) from exc

    # -- EventStore -------------------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except (ClickHouseError, OSError):
            return False

    def max_stored_at(self, tenant_id: str) -> datetime | None:
        rows = self._query("max_stored_at", _MAX_STORED_AT, {"tenant_id": tenant_id})
        if not rows or int(rows[0][0]) == 0:
            return None
        return _aware(rows[0][1])

    def identifier_rows(
        self,
        tenant_id: str,
        window: Window,
        *,
        stored_after: datetime | None = None,
        forms: frozenset[str] | None = None,
    ) -> Iterator[IdentifierRow]:
        sql = _IDENTIFIER_ROWS_HEAD
        parameters = _window_parameters(tenant_id, window)
        if forms is not None:
            sql += _FORMS_FILTER
            parameters["forms"] = sorted(forms)
        sql += _IDENTIFIER_ROWS_GROUP
        if stored_after is not None:
            sql += _STORED_AFTER
            parameters["stored_after"] = _ms(stored_after)
        for row in self._stream("identifier_rows", sql, parameters):
            yield IdentifierRow(
                token=str(row[0]),
                field_ref=str(row[1]),
                form=str(row[2]),
                shape=str(row[3]),
                event_id=str(row[4]),
                system_id=str(row[5]),
                observed_at=_aware(row[6]),
                stored_at=_aware(row[7]),
            )

    def template_counts(self, tenant_id: str, window: Window) -> list[TemplateCount]:
        rows = self._query(
            "template_counts", _TEMPLATE_COUNTS, _window_parameters(tenant_id, window)
        )
        return [
            TemplateCount(
                system_id=str(row[0]), template_id=str(row[1]), day=row[2], events=int(row[3])
            )
            for row in rows
        ]

    def pair_join(self, tenant_id: str, a: FieldForm, b: FieldForm, window: Window) -> PairJoin:
        parameters = _window_parameters(tenant_id, window) | {
            "field_a": a.field_ref,
            "form_a": a.form,
            "field_b": b.field_ref,
            "form_b": b.form,
            "top_n": CONCENTRATION_TOP_N,
        }
        rows = self._query("pair_join", _PAIR_JOIN, parameters)
        if not rows:
            msg = "store pair_join returned no row"
            raise StoreError(msg)
        row = rows[0]
        n_a, n_b, n_match = int(row[0]), int(row[1]), int(row[2])
        if n_match == 0:
            return PairJoin(
                n_a=n_a,
                n_b=n_b,
                n_match=0,
                lag_median_seconds=None,
                lag_iqr_seconds=None,
                lag_positive_share=None,
                fanout_ab_median=None,
                fanout_ab_p99=None,
                fanout_ba_median=None,
                fanout_ba_p99=None,
                concentration=None,
            )
        q25, lag_median, q75 = _quantile(row[3]), _quantile(row[4]), _quantile(row[5])
        lag_positive = int(row[6])
        top_product, total_product = int(row[11]), int(row[12])
        return PairJoin(
            n_a=n_a,
            n_b=n_b,
            n_match=n_match,
            lag_median_seconds=lag_median,
            lag_iqr_seconds=(q75 - q25) if q25 is not None and q75 is not None else None,
            lag_positive_share=lag_positive / n_match,
            fanout_ab_median=_quantile(row[7]),
            fanout_ab_p99=_quantile(row[8]),
            fanout_ba_median=_quantile(row[9]),
            fanout_ba_p99=_quantile(row[10]),
            concentration=top_product / total_product if total_product else None,
        )

    def cooccurrences(
        self, tenant_id: str, window: Window, *, min_events: int = 1
    ) -> list[CooccurrenceRow]:
        parameters = _window_parameters(tenant_id, window) | {
            "min_events": max(1, int(min_events)),
            "limit": MAX_COOCCURRENCE_ROWS,
        }
        rows = self._query("cooccurrences", _COOCCURRENCES, parameters)
        if len(rows) >= MAX_COOCCURRENCE_ROWS:
            log.warning("cooccurrences truncated", tenant_id=tenant_id, limit=MAX_COOCCURRENCE_ROWS)
        return [
            CooccurrenceRow(
                a=FieldForm(str(row[0]), str(row[1])),
                b=FieldForm(str(row[2]), str(row[3])),
                system_id=str(row[4]),
                events=int(row[5]),
                distinct_a=int(row[6]),
                single_b_a=int(row[7]),
                distinct_b=int(row[8]),
                single_a_b=int(row[9]),
            )
            for row in rows
        ]

    def hourly_volumes(self, tenant_id: str, window: Window) -> list[HourlyVolume]:
        rows = self._query("hourly_volumes", _HOURLY_VOLUMES, _window_parameters(tenant_id, window))
        return [
            HourlyVolume(system_id=str(row[0]), hour=_aware(row[1]), events=int(row[2]))
            for row in rows
        ]

    def events_with_keys(
        self,
        tenant_id: str,
        system_id: str,
        window: Window,
        *,
        forms: frozenset[str] | None = None,
    ) -> Iterator[EventKeys]:
        sql = _EVENTS_WITH_KEYS_HEAD
        parameters = _window_parameters(tenant_id, window) | {"system_id": system_id}
        if forms is not None:
            sql += _FORMS_FILTER
            parameters["forms"] = sorted(forms)
        sql += _EVENTS_WITH_KEYS_TAIL
        for row in self._stream("events_with_keys", sql, parameters):
            attributes = row[3] if isinstance(row[3], dict) else {}
            triples = tuple((str(item[0]), str(item[1]), str(item[2])) for item in (row[4] or ()))
            yield EventKeys(
                event_id=str(row[0]),
                system_id=system_id,
                template_id=str(row[1]),
                observed_at=_aware(row[2]),
                attributes={str(key): str(value) for key, value in attributes.items()},
                identifiers=triples,
            )
