"""ClickHouse client factory and the event writer (spec 5.3 ``ingest-api``, 7.2, 14.4).

The writer turns :class:`~carto_schema.event.CanonicalEvent` objects into one ``events`` row and
one ``event_identifiers`` row per identifier form (spec 7.2 "exploded identifier forms, the join
workhorse"), with ``field_ref = system_id/template_id/field`` (spec 7.2, ADR 0016). Rows are
built by pure functions so the mapping is unit-tested without a server; inserts are
column-oriented, which is what clickhouse-connect sends most cheaply. ``template_text`` is not a
column of ``events`` (spec 7.2) and is not written here; templates live in PostgreSQL (spec 7.3)
from M2 on. Both rows carry ``stored_at``, core's wall clock at the insert (migrations 0007 and
0008, ADR 0046): the profiler's incremental watermark, which the edge's ``ingested_at`` cannot
be because a backed-up disk buffer delivers hours late. The writer takes a ``clock`` so tests
and the bundle loader control it; it is UTC at millisecond precision like every timestamp.

Both tables are partitioned by the day of ``observed_at``, and ClickHouse refuses an insert
that touches more than 100 partitions. The writer therefore inserts the events of at most
:data:`MAX_PARTITION_DAYS` days at a time (ADR 0036), each group's events before its
identifiers. A failure part way leaves the earlier groups written; the batch is not marked done,
its retry writes them again, and the ``ReplacingMergeTree`` tables merge the copies (ADR 0034).

A failed insert raises :class:`WriterError` naming the error class and code only, never the
rows. ``permanent`` is set when retrying cannot help, because ClickHouse refused the data
itself (:data:`PERMANENT_ERROR_CODES`, or the client could not serialize a value): the caller
answers 422 and the edge parks the batch. Anything else (the server unreachable, overloaded or
not migrated yet) is transient: 503, and the edge retries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError, DataError

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

    from carto_core.settings import ClickHouseSettings
    from carto_schema.event import CanonicalEvent

__all__ = [
    "EVENTS_TABLE",
    "EVENT_COLUMNS",
    "EVENT_IDENTIFIERS_TABLE",
    "IDENTIFIER_COLUMNS",
    "MAX_PARTITION_DAYS",
    "PERMANENT_ERROR_CODES",
    "ClickHouseWriter",
    "EventRow",
    "EventWriter",
    "IdentifierRow",
    "InsertClient",
    "WriteResult",
    "WriterError",
    "columns_of",
    "create_client",
    "event_row",
    "field_ref",
    "identifier_rows",
    "is_permanent",
    "partition_day_groups",
    "stored_at_now",
]

MAX_PARTITION_DAYS: Final = 90
"""Most partition days one insert touches; ClickHouse's ``max_partitions_per_insert_block``
default is 100 (ADR 0036)."""

PERMANENT_ERROR_CODES: Final[frozenset[int]] = frozenset(
    {
        6,  # CANNOT_PARSE_TEXT
        9,  # SIZES_OF_COLUMNS_DOESNT_MATCH
        25,  # CANNOT_PARSE_ESCAPE_SEQUENCE
        26,  # CANNOT_PARSE_QUOTED_STRING
        27,  # CANNOT_PARSE_INPUT_ASSERTION_FAILED
        38,  # CANNOT_PARSE_DATE
        41,  # CANNOT_PARSE_DATETIME
        43,  # ILLEGAL_TYPE_OF_ARGUMENT
        53,  # TYPE_MISMATCH
        69,  # ARGUMENT_OUT_OF_BOUND
        70,  # CANNOT_CONVERT_TYPE
        72,  # CANNOT_PARSE_NUMBER
        117,  # INCORRECT_DATA
        131,  # TOO_LARGE_STRING_SIZE
        321,  # VALUE_IS_OUT_OF_RANGE_OF_DATA_TYPE
        349,  # CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN
        565,  # TOO_MANY_PARTITIONS
    }
)
"""ClickHouse server error codes (``src/Common/ErrorCodes.cpp``) that refuse the data of an
insert, so the same batch fails the same way every time. Code 252 (``TOO_MANY_PARTS``) is not
here: ClickHouse 24.8 raises it both for "too many partitions for a single insert", which the
day groups prevent, and for "merges are slower than inserts", which passes."""

EVENTS_TABLE: Final = "events"
EVENT_IDENTIFIERS_TABLE: Final = "event_identifiers"

EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "event_id",
    "source_id",
    "system_id",
    "kind",
    "observed_at",
    "ingested_at",
    "observed_at_quality",
    "template_id",
    "severity",
    "attributes",
    "actor_token",
    "actor_kind",
    "stored_at",
)
"""``events`` columns in DDL order (spec 7.2, ``0004_events_replacing.sql`` plus ``stored_at``
from ``0007_events_stored_at.sql``)."""

IDENTIFIER_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "token",
    "field_ref",
    "form",
    "shape",
    "event_id",
    "system_id",
    "observed_at",
    "stored_at",
)
"""``event_identifiers`` columns in DDL order (spec 7.2, ``0006_event_identifiers_replacing.sql``
plus ``stored_at`` from ``0008_event_identifiers_stored_at.sql``)."""

type EventRow = tuple[
    str,
    str,
    str,
    str,
    str,
    datetime,
    datetime,
    str,
    str,
    str | None,
    dict[str, str],
    str | None,
    str | None,
    datetime,
]
"""One ``events`` row, in :data:`EVENT_COLUMNS` order."""

type IdentifierRow = tuple[str, str, str, str, str, str, str, datetime, datetime]
"""One ``event_identifiers`` row, in :data:`IDENTIFIER_COLUMNS` order."""


class WriterError(Exception):
    """ClickHouse did not take the write. The message names the cause, never the rows;
    ``permanent`` is set when the same rows would fail again."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class WriteResult:
    """How many rows one call wrote."""

    events: int
    identifiers: int


@runtime_checkable
class EventWriter(Protocol):
    """What ``ingest-api`` and the bundle loader write through."""

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult: ...

    def ping(self) -> bool: ...


class InsertClient(Protocol):
    """The two clickhouse-connect calls the writer uses (a fake stands in for tests)."""

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        *,
        column_oriented: bool = False,
    ) -> Any: ...

    def ping(self) -> bool: ...


def field_ref(system_id: str, template_id: str, field: str) -> str:
    """``system_id/template_id/field`` (spec 7.2 comment on ``field_ref``, ADR 0016)."""
    return f"{system_id}/{template_id}/{field}"


def stored_at_now(clock: Callable[[], datetime] | None = None) -> datetime:
    """The ``stored_at`` of rows written now: ``clock()`` (the wall clock by default) in UTC,
    truncated to milliseconds like every contract timestamp."""
    at = clock() if clock is not None else datetime.now(UTC)
    if at.tzinfo is None:
        msg = "the writer clock must return an aware datetime"
        raise ValueError(msg)
    at = at.astimezone(UTC)
    return at.replace(microsecond=at.microsecond // 1000 * 1000)


def event_row(event: CanonicalEvent, stored_at: datetime) -> EventRow:
    """The ``events`` row of one canonical event (spec 7.1 to 7.2 mapping) stored at
    ``stored_at`` (:func:`stored_at_now`)."""
    actor = event.actor
    return (
        event.tenant_id,
        event.event_id,
        event.source_id,
        event.system_id,
        event.kind.value,
        event.observed_at,
        event.ingested_at,
        event.observed_at_quality.value,
        event.template_id,
        event.severity.value if event.severity is not None else None,
        dict(event.attributes),
        actor.token if actor is not None else None,
        actor.kind.value if actor is not None else None,
        stored_at,
    )


def identifier_rows(event: CanonicalEvent, stored_at: datetime) -> list[IdentifierRow]:
    """One ``event_identifiers`` row per identifier form of the event (spec 7.2)."""
    return [
        (
            event.tenant_id,
            identifier.token,
            field_ref(event.system_id, event.template_id, identifier.field),
            identifier.form,
            identifier.shape,
            event.event_id,
            event.system_id,
            event.observed_at,
            stored_at,
        )
        for identifier in event.identifiers
    ]


def is_permanent(exc: BaseException) -> bool:
    """True when ClickHouse refused the data itself (see :data:`PERMANENT_ERROR_CODES`)."""
    if isinstance(exc, DataError):
        return True
    code = getattr(exc, "code", None)
    return isinstance(code, int) and code in PERMANENT_ERROR_CODES


def _partition_day(event: CanonicalEvent) -> date:
    """``toYYYYMMDD(observed_at)`` of the DDL: the UTC day."""
    return event.observed_at.astimezone(UTC).date()


def partition_day_groups(
    events: Sequence[CanonicalEvent], max_days: int = MAX_PARTITION_DAYS
) -> Iterator[list[CanonicalEvent]]:
    """The events in groups spanning at most ``max_days`` partition days, oldest days first;
    within a day the batch order is kept."""
    by_day: dict[date, list[CanonicalEvent]] = {}
    for event in events:
        by_day.setdefault(_partition_day(event), []).append(event)
    days = sorted(by_day)
    for start in range(0, len(days), max_days):
        group: list[CanonicalEvent] = []
        for day in days[start : start + max_days]:
            group.extend(by_day[day])
        yield group


def columns_of(rows: Sequence[Sequence[Any]], width: int) -> list[list[Any]]:
    """Transpose rows into ``width`` column lists (clickhouse-connect ``column_oriented``)."""
    columns: list[list[Any]] = [[] for _ in range(width)]
    for row in rows:
        for index, value in enumerate(row):
            columns[index].append(value)
    return columns


class ClickHouseWriter:
    """Writes canonical events to ``events`` and ``event_identifiers`` (spec 5.4 step 8)."""

    __slots__ = ("_client", "_clock")

    def __init__(
        self, client: InsertClient, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._client = client
        self._clock = clock

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        """Insert every event and its identifier forms, at most :data:`MAX_PARTITION_DAYS`
        partition days per insert, all with one ``stored_at`` read from the clock when the call
        starts. Raises :class:`WriterError` on failure."""
        stored_at = stored_at_now(self._clock)
        events_written = identifiers_written = 0
        for group in partition_day_groups(events):
            result = self._write_group(group, stored_at)
            events_written += result.events
            identifiers_written += result.identifiers
        return WriteResult(events=events_written, identifiers=identifiers_written)

    def _write_group(self, events: Sequence[CanonicalEvent], stored_at: datetime) -> WriteResult:
        rows = [event_row(event, stored_at) for event in events]
        id_rows: list[IdentifierRow] = []
        for event in events:
            id_rows.extend(identifier_rows(event, stored_at))
        try:
            self._client.insert(
                EVENTS_TABLE,
                columns_of(rows, len(EVENT_COLUMNS)),
                column_names=list(EVENT_COLUMNS),
                column_oriented=True,
            )
            if id_rows:
                self._client.insert(
                    EVENT_IDENTIFIERS_TABLE,
                    columns_of(id_rows, len(IDENTIFIER_COLUMNS)),
                    column_names=list(IDENTIFIER_COLUMNS),
                    column_oriented=True,
                )
        except (ClickHouseError, OSError) as exc:
            code = getattr(exc, "code", None)
            suffix = f" (code {code})" if isinstance(code, int) else ""
            msg = f"ClickHouse insert failed: {type(exc).__name__}{suffix}"
            raise WriterError(msg, permanent=is_permanent(exc)) from exc
        return WriteResult(events=len(rows), identifiers=len(id_rows))

    def ping(self) -> bool:
        """True when the server answers; never raises (readiness probes call this)."""
        try:
            return bool(self._client.ping())
        except (ClickHouseError, OSError):
            return False


def create_client(settings: ClickHouseSettings) -> Client:
    """Connect with the configured user; the password comes from ``password_file`` (spec 14.4).

    The client is sessionless because ``ingest-api`` shares it across concurrent requests.
    Raises :class:`WriterError` when the server cannot be reached or refuses the login; the
    message never includes the password.
    """
    timeout = max(1, int(settings.timeout_seconds))
    try:
        return clickhouse_connect.get_client(
            host=settings.host,
            port=settings.port,
            username=settings.user,
            password=settings.read_password(),
            database=settings.database,
            secure=settings.secure,
            ca_cert=str(settings.ca_file) if settings.ca_file is not None else None,
            connect_timeout=timeout,
            send_receive_timeout=timeout,
            # One client serves every threadpool worker. With a session id each concurrent
            # request would wait for, or fail with, SESSION_IS_LOCKED; nothing needs a session.
            autogenerate_session_id=False,
        )
    except (ClickHouseError, OSError) as exc:
        msg = (
            f"cannot connect to ClickHouse at {settings.url} as {settings.user}: "
            f"{type(exc).__name__}"
        )
        raise WriterError(msg) from exc
