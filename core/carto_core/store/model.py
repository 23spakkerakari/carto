"""The event store as the engine sees it (plan M2 decision 1; spec 7.2, 9.1 to 9.8).

:class:`EventStore` is the handful of typed queries the profiler, the linker, the composite
matcher, the assembler and the graph rollup need over ``events``, ``event_identifiers`` and
``txn_events`` (the membership table of spec 7.2, M3). ClickHouse is the only deployment
implementation (:mod:`carto_core.store.clickhouse`, founder decision 3); the in-memory
:class:`~carto_core.store.memory.InMemoryEventStore` is the reference for the semantics below and
the double the engine's unit tests run against. The contract test module
(``core/tests/test_core_store_contract.py``) runs the same cases against both.

Semantics every implementation must share:

- **Deduplication.** Both tables are ``ReplacingMergeTree`` (ADR 0034) and a batch delivered
  twice exists twice until a merge runs. Every query counts an event once by ``event_id`` and an
  identifier row once by ``(event_id, field_ref, form)``; a row's ``stored_at`` is the earliest
  copy's, so a redelivered duplicate is never "new" again for a watermark.
- **Windows** are half-open on ``observed_at``: ``start <= observed_at < end``.
- **Quantiles** are "exact low": the element at index ``floor(level * (n - 1))`` of the sorted
  values, which the ClickHouse store computes explicitly over the sorted array, so the two
  implementations agree bit for bit.
- **Ordering** of streamed rows is unspecified; callers never rely on it.
- **Membership** (``txn_events``, ADR 0060): every ``(txn_id, event_id)`` assignment is a row
  with a ``version``; a moved event keeps its old row under the merged-away transaction. A
  reader that starts from an event takes the assignment with the highest version
  (:meth:`EventStore.events_with_token`, :meth:`EventStore.ordered_transactions`,
  :meth:`EventStore.transaction_summaries`); a reader that starts from a transaction id
  (:meth:`EventStore.transaction_events`) is given a surviving id by the caller.
- Nothing here returns raw values: tokens, shapes, counts and times only (spec 2.3 invariant 2).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "AssemblyRow",
    "CooccurrenceRow",
    "EventKeys",
    "EventStore",
    "FieldForm",
    "HourlyVolume",
    "IdentifierRow",
    "OrderedTransaction",
    "PairJoin",
    "StoreError",
    "TemplateCount",
    "TokenHit",
    "TransactionEventRow",
    "TransactionSummary",
    "TxnEventWrite",
    "Window",
    "event_order_key",
]


class StoreError(Exception):
    """The store could not answer. The message names the cause, never a row."""


@dataclass(frozen=True, slots=True, order=True)
class FieldForm:
    """One end of a candidate pair: a ``field_ref`` (``system_id/template_id/path``) and a form."""

    field_ref: str
    form: str

    @property
    def system_id(self) -> str:
        return self.field_ref.partition("/")[0]


@dataclass(frozen=True, slots=True)
class Window:
    """``start <= observed_at < end``, both UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            msg = "window start must be before its end"
            raise ValueError(msg)
        if self.start.tzinfo is None or self.end.tzinfo is None:
            msg = "window bounds must be timezone-aware"
            raise ValueError(msg)

    def contains(self, at: datetime) -> bool:
        return self.start <= at < self.end


@dataclass(frozen=True, slots=True)
class IdentifierRow:
    """One ``event_identifiers`` row (spec 7.2) with the time core stored it (migration 0007)."""

    token: str
    field_ref: str
    form: str
    shape: str
    event_id: str
    system_id: str
    observed_at: datetime
    stored_at: datetime


@dataclass(frozen=True, slots=True)
class TemplateCount:
    """Distinct events of one template on one UTC day; the profiler's null-rate denominator."""

    system_id: str
    template_id: str
    day: date
    events: int


@dataclass(frozen=True, slots=True)
class PairJoin:
    """The spec 9.3 features that come from the exact token join of two ends over a window.

    Per shared token ``t``: ``first_a(t)`` and ``first_b(t)`` are the earliest ``observed_at`` of
    an A and a B row carrying it; ``count_a(t)`` and ``count_b(t)`` are the distinct events of
    each end carrying it. Then:

    - ``n_a``, ``n_b``: distinct tokens of each end in the window; ``n_match``: tokens in both.
    - ``lag`` per token is ``first_b - first_a`` in seconds; ``lag_median_seconds`` and
      ``lag_iqr_seconds`` (``q75 - q25``) are exact-low quantiles over the matched tokens;
      ``lag_positive_share`` is the share of matched tokens with ``lag > 0``.
    - ``fanout_ab`` is the distribution of ``count_b(t)`` over matched tokens (B events per
      matched A token); ``fanout_ba`` is ``count_a(t)``; median and p99 are exact-low quantiles.
    - ``concentration`` is the share of join rows (``sum count_a(t) * count_b(t)``) that the ten
      tokens with the most join rows hold.

    Every distribution value is ``None`` when ``n_match`` is 0.
    """

    n_a: int
    n_b: int
    n_match: int
    lag_median_seconds: float | None
    lag_iqr_seconds: float | None
    lag_positive_share: float | None
    fanout_ab_median: float | None
    fanout_ab_p99: float | None
    fanout_ba_median: float | None
    fanout_ba_p99: float | None
    concentration: float | None

    @property
    def c_ab(self) -> float:
        """Containment of A in B: ``n_match / n_a`` (0 when A is empty)."""
        return self.n_match / self.n_a if self.n_a else 0.0

    @property
    def c_ba(self) -> float:
        return self.n_match / self.n_b if self.n_b else 0.0


@dataclass(frozen=True, slots=True)
class CooccurrenceRow:
    """Two identifier ends carried by the same events (spec 9.4 bridge candidates).

    ``events`` is the number of distinct events carrying both ends. ``distinct_a`` is the number
    of distinct A tokens among those events and ``single_b_a`` how many of them pair with exactly
    one B token across the window (the functional-dependency numerator of spec 9.4);
    ``distinct_b`` and ``single_a_b`` are the reverse. ``a < b`` in :class:`FieldForm` order, so
    each unordered pair appears once; ends of the same field and form are never paired.
    """

    a: FieldForm
    b: FieldForm
    system_id: str
    events: int
    distinct_a: int
    single_b_a: int
    distinct_b: int
    single_a_b: int

    @property
    def consistency_ab(self) -> float:
        return self.single_b_a / self.distinct_a if self.distinct_a else 0.0

    @property
    def consistency_ba(self) -> float:
        return self.single_a_b / self.distinct_b if self.distinct_b else 0.0


@dataclass(frozen=True, slots=True)
class HourlyVolume:
    """Distinct events of one system in one UTC hour (``hour`` is the hour's start)."""

    system_id: str
    hour: datetime
    events: int


@dataclass(frozen=True, slots=True)
class EventKeys:
    """One event with what composite matching blocks and scores on (spec 9.5): its
    low-cardinality attributes and its identifier tokens in the requested forms."""

    event_id: str
    system_id: str
    template_id: str
    observed_at: datetime
    attributes: Mapping[str, str]
    identifiers: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    """``(field_ref, form, token)`` triples, deduplicated."""


@dataclass(frozen=True, slots=True)
class AssemblyRow:
    """One event with the identifier tokens of the requested ends (spec 9.7 step 1): what the
    assembler keys on. ``keys`` are ``(field_ref, form, token)`` triples, deduplicated; an
    event that carries none of the requested ends is not returned."""

    event_id: str
    system_id: str
    template_id: str
    observed_at: datetime
    ingested_at: datetime
    stored_at: datetime
    severity: str | None
    actor_kind: str | None
    keys: tuple[tuple[str, str, str], ...]

    @property
    def node_id(self) -> str:
        """The flow-graph node of the event (ADR 0066): ``system_id/template_id``."""
        return f"{self.system_id}/{self.template_id}"


@dataclass(frozen=True, slots=True)
class TxnEventWrite:
    """One ``txn_events`` row to append (spec 7.2)."""

    tenant_id: str
    txn_id: str
    event_id: str
    node_id: str
    observed_at: datetime
    version: int
    stored_at: datetime


@dataclass(frozen=True, slots=True)
class TransactionEventRow:
    """One event of a transaction as the membership table and ``events`` describe it."""

    event_id: str
    node_id: str
    observed_at: datetime
    ingested_at: datetime
    severity: str | None
    actor_kind: str | None
    version: int

    @property
    def system_id(self) -> str:
        return self.node_id.partition("/")[0]

    @property
    def template_id(self) -> str:
        return self.node_id.partition("/")[2]


def event_order_key(row: TransactionEventRow) -> tuple[datetime, datetime, str]:
    """Spec 9.7 item 6: ``observed_at``, ties by ``ingested_at`` then ``event_id``."""
    return (row.observed_at, row.ingested_at, row.event_id)


@dataclass(frozen=True, slots=True)
class OrderedTransaction:
    """A surviving transaction's events in spec 9.7 order."""

    txn_id: str
    events: tuple[TransactionEventRow, ...]


@dataclass(frozen=True, slots=True)
class TokenHit:
    """An identifier row that matched a searched token, with the event's latest transaction
    (``None`` when the event is in no transaction yet)."""

    token: str
    field_ref: str
    form: str
    event_id: str
    system_id: str
    observed_at: datetime
    txn_id: str | None


@dataclass(frozen=True, slots=True)
class TransactionSummary:
    """What the assembler needs to set a transaction's row after a write: the distinct
    events whose latest assignment is this transaction, their first and last times, and the
    node and severity of the last event in spec 9.7 order."""

    txn_id: str
    event_count: int
    first_seen: datetime
    last_seen: datetime
    last_node: str
    last_severity: str | None


@runtime_checkable
class EventStore(Protocol):
    """The queries of the engine. Implementations raise :class:`StoreError` on failure."""

    def ping(self) -> bool:
        """True when the store answers; never raises (readiness probes call this)."""
        ...

    def max_stored_at(self, tenant_id: str) -> datetime | None:
        """The latest ``stored_at`` of ``event_identifiers``, the profiler's watermark source."""
        ...

    def identifier_rows(
        self,
        tenant_id: str,
        window: Window,
        *,
        stored_after: datetime | None = None,
        forms: frozenset[str] | None = None,
    ) -> Iterator[IdentifierRow]:
        """Stream identifier rows observed in ``window``; with ``stored_after`` only rows whose
        (earliest) ``stored_at`` is strictly later; with ``forms`` only those forms."""
        ...

    def template_counts(self, tenant_id: str, window: Window) -> list[TemplateCount]:
        """Distinct events per ``(system_id, template_id, day)`` in the window."""
        ...

    def pair_join(self, tenant_id: str, a: FieldForm, b: FieldForm, window: Window) -> PairJoin:
        """The exact token join of two ends over the window (see :class:`PairJoin`)."""
        ...

    def cooccurrences(
        self, tenant_id: str, window: Window, *, min_events: int = 1
    ) -> list[CooccurrenceRow]:
        """Every pair of identifier ends carried together by at least ``min_events`` events."""
        ...

    def hourly_volumes(self, tenant_id: str, window: Window) -> list[HourlyVolume]:
        """Distinct events per system per UTC hour in the window (temporal adjacency, 9.5)."""
        ...

    def events_with_keys(
        self,
        tenant_id: str,
        system_id: str,
        window: Window,
        *,
        forms: frozenset[str] | None = None,
    ) -> Iterator[EventKeys]:
        """Stream the events of one system in the window with their attributes and the
        identifier tokens of the requested forms (all forms when ``None``)."""
        ...

    # -- membership (spec 9.7, 9.8; ADR 0060) ---------------------------------------------------

    def assembly_rows(
        self,
        tenant_id: str,
        window: Window,
        *,
        ends: Collection[FieldForm],
        stored_after: datetime | None = None,
        limit: int,
    ) -> list[AssemblyRow]:
        """The events observed in ``window`` that carry at least one identifier in ``ends``,
        with those identifiers only; with ``stored_after`` only events whose (earliest)
        ``stored_at`` is strictly later. Ordered by ``(stored_at, event_id)`` and capped at
        ``limit`` rows, so a caller pages by ``stored_after``."""
        ...

    def append_txn_events(self, rows: Sequence[TxnEventWrite]) -> int:
        """Append membership rows; return how many were written."""
        ...

    def transaction_events(
        self, tenant_id: str, txn_id: str, *, window: Window | None = None
    ) -> list[TransactionEventRow]:
        """The events assigned to ``txn_id`` (latest row per event among the rows of that
        transaction), in spec 9.7 order; ``window`` bounds the ``events`` lookup."""
        ...

    def ordered_transactions(self, tenant_id: str, window: Window) -> Iterator[OrderedTransaction]:
        """Every event observed in ``window`` under its latest assignment, grouped by
        transaction, each transaction's events in spec 9.7 order; transaction order is
        unspecified."""
        ...

    def events_with_token(
        self, tenant_id: str, tokens: Collection[str], window: Window
    ) -> list[TokenHit]:
        """Identifier rows in ``window`` carrying any of ``tokens`` (one per event, field and
        form), each with the event's latest transaction or ``None``."""
        ...

    def transaction_summaries(
        self, tenant_id: str, txn_ids: Collection[str], window: Window
    ) -> dict[str, TransactionSummary]:
        """Per requested id that still owns events under the latest assignment: its summary.
        ``window`` bounds the ``events`` lookup (the assembler's read window)."""
        ...
