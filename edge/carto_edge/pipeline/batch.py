"""Batching and the ingest stage of edge-gateway (spec 5.4 step 7, 8.1 cursor checkpointing,
8.5; plan M1 wave 2, D1).

:class:`BatchAccumulator` collects the canonical events of one source into
:class:`~carto_schema.ingest.IngestBatch` envelopes of at most ``max_events`` events and
``max_bytes`` of JSON (spec 8.5: 5,000 events or 5 MB). The size is tracked as the sum of each
event's UTF-8 JSON length plus one separator byte, plus :data:`ENVELOPE_BYTES` for the envelope
fields, so a sealed batch's JSON never exceeds the cap; only a single event larger than the cap
on its own travels alone in an oversized batch (an event is never split or dropped for size).
When adding an event would exceed either limit, the pending batch is sealed and returned and the
event starts the next one.

:class:`Ingestor` is the :class:`~carto_edge.pipeline.ingestor.IngestorLike` the receivers and
the poll scheduler hand records to. Per call: every record goes through
``runtime.pipeline.process`` (outside the ingestor lock; the pipeline has its own), the reveal
vault entries of all results are written in one ``runtime.write_vault_entries`` call (before any
of their events can reach the buffer, so a forwarded token always has its vault row), and then,
under the ingestor lock, events join their source's accumulator, sealed batches are appended to
the :class:`~carto_edge.pipeline.buffer.DiskBuffer`, and only after a successful append is the
source's cursor committed to the :class:`~carto_edge.state.CursorStore`.

The cursor rule (spec 8.1, "a cursor is committed only after the batch is durably in the disk
buffer"): per source the ingestor keeps a *candidate*, the latest ``commit_cursor`` among the
records whose events are pending or durable (records are placed in order, so every record before
the candidate has been placed). When ``add`` seals a batch, the candidate as it stood before the
current record (whose event now starts the next batch) is committed after the append; when a
flush seals the partial batch, the current candidate is committed. A source with nothing pending
(say every record of a call was dropped by the pipeline) commits its candidate at the end of the
call. So a record whose event sits in a partial batch never commits its cursor. A
:class:`~carto_edge.pipeline.model.CursorCheckpoint` (ADR 0035) takes the place of such a record:
it skips the pipeline and only becomes the candidate, so it is committed under the same rule.

A batch the buffer refuses is lost from memory: a full buffer
(:class:`~carto_edge.pipeline.buffer.BufferFullError`, reason ``buffer_full``) or any other
failure of the append (a full disk, an I/O error, a locked database: ``sqlite3.Error`` or
``OSError``, reason ``buffer_error``). Its events are counted as dropped and the cursor stays
where it was. For a pulled source (:data:`PULL_SOURCE_TYPES`; decided by the source type, not by
whether a record with a cursor was seen yet, because Splunk, SQL and SFTP put the cursor on the
last record of a window, page or listing) the ingestor then also discards the source's pending
events, rolls the candidate back to the committed cursor and refuses every further record of
that source until one ingest call has reported the refusal: a caller that sees a refusal
(:func:`buffer_refused`) in an :class:`~carto_edge.pipeline.ingestor.IngestOutcome` must stop its
read and restart from the committed cursor (the poll scheduler does), so no later cursor can be
committed past the lost events (at-least-once; core dedupes by ``event_id`` and ``batch_id``).
Pushed sources (OTLP and webhooks) lose only the refused batch, and the gateway answers 503 so
the sender keeps it and retries.

Records whose source the sources file does not know are dropped by the pipeline with reason
``unknown_source`` and never reach an accumulator; their metrics carry the label
:data:`UNKNOWN_SOURCE_LABEL`, so a pushed source id cannot mint label values. Metrics and log
lines carry source ids, batch ids, counts and reasons only (spec 2.3 invariant 7).
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_edge.config import MIB, CoreLinkSettings, SourceType
from carto_edge.connectors.base import Cursor
from carto_edge.metrics import EdgeMetrics
from carto_edge.pipeline.buffer import BufferFullError, DiskBuffer
from carto_edge.pipeline.ingestor import BackpressureState, IngestOutcome
from carto_edge.pipeline.model import CursorCheckpoint, PipelineResult, RawRecord, ReadItem
from carto_edge.runtime import EdgeRuntime
from carto_edge.state import CursorError, CursorStore
from carto_schema.event import SCHEMA_VERSION, CanonicalEvent
from carto_schema.ingest import MAX_EVENTS_PER_BATCH, IngestBatch

__all__ = [
    "BUFFER_REFUSALS",
    "DEFAULT_MAX_BYTES",
    "ENVELOPE_BYTES",
    "PULL_SOURCE_TYPES",
    "REASON_BUFFER_ERROR",
    "REASON_BUFFER_FULL",
    "UNKNOWN_SOURCE_LABEL",
    "BatchAccumulator",
    "Ingestor",
    "buffer_refused",
]

ENVELOPE_BYTES: Final = 512
"""Allowance for the envelope around the events: ``schema_version``, ``tenant_id`` and
``source_id`` (at most 64 characters each), ``batch_id``, ``sent_at`` and the brackets."""
DEFAULT_MAX_BYTES: Final = 5 * MIB
REASON_BUFFER_FULL: Final = "buffer_full"
REASON_BUFFER_ERROR: Final = "buffer_error"
BUFFER_REFUSALS: Final = frozenset({REASON_BUFFER_FULL, REASON_BUFFER_ERROR})
PULL_SOURCE_TYPES: Final = frozenset(
    {SourceType.UPLOAD, SourceType.SPLUNK, SourceType.SQL, SourceType.SFTP}
)
"""Sources the edge reads with a cursor; a refused batch makes them re-read."""


def buffer_refused(outcome: IngestOutcome) -> int:
    """Events of ``outcome`` the disk buffer refused (full, or failed to write)."""
    return sum(outcome.dropped.get(reason, 0) for reason in BUFFER_REFUSALS)


UNKNOWN_SOURCE_LABEL: Final = "unknown"
_BACKPRESSURE_GAUGE: Final = {
    BackpressureState.OK: 0,
    BackpressureState.SLOW: 1,
    BackpressureState.FULL: 2,
}

Clock = Callable[[], datetime]
Stored = Literal["stored", "refused", "reread"]
"""What :meth:`Ingestor._store` did: appended; refused (counted); refused and the source must
be re-read from its committed cursor."""

log = get_logger(component="carto_edge.batch")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event_bytes(event: CanonicalEvent) -> int:
    return len(event.model_dump_json().encode("utf-8"))


class BatchAccumulator:
    """The pending batch of one source. Not thread-safe on its own; the ingestor locks."""

    __slots__ = (
        "_bytes",
        "_clock",
        "_events",
        "_max_bytes",
        "_max_events",
        "_source_id",
        "_started",
        "_tenant_id",
    )

    def __init__(
        self,
        tenant_id: str,
        source_id: str,
        *,
        max_events: int = MAX_EVENTS_PER_BATCH,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Clock | None = None,
    ) -> None:
        if not 1 <= max_events <= MAX_EVENTS_PER_BATCH:
            msg = f"max_events must be between 1 and {MAX_EVENTS_PER_BATCH}"
            raise ValueError(msg)
        if max_bytes <= ENVELOPE_BYTES:
            msg = f"max_bytes must exceed the {ENVELOPE_BYTES}-byte envelope allowance"
            raise ValueError(msg)
        self._tenant_id = tenant_id
        self._source_id = source_id
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._clock = clock if clock is not None else _utc_now
        self._events: list[CanonicalEvent] = []
        self._bytes = ENVELOPE_BYTES
        self._started: datetime | None = None

    def __repr__(self) -> str:
        return f"BatchAccumulator(source_id={self._source_id!r}, pending={len(self._events)})"

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def pending(self) -> int:
        """Events waiting in the partial batch."""
        return len(self._events)

    @property
    def pending_bytes(self) -> int:
        """The tracked JSON size of the partial batch, envelope allowance included."""
        return self._bytes

    def age_seconds(self, now: datetime) -> float:
        """Seconds since the first pending event was added; 0 when nothing is pending."""
        if self._started is None:
            return 0.0
        return max(0.0, (now - self._started).total_seconds())

    def add(self, event: CanonicalEvent) -> IngestBatch | None:
        """Add ``event``; returns the sealed batch when the event did not fit in it."""
        if event.source_id != self._source_id:
            msg = "the event belongs to another source than this accumulator"
            raise ValueError(msg)
        size = _event_bytes(event) + 1
        sealed: IngestBatch | None = None
        if self._events and (
            len(self._events) + 1 > self._max_events or self._bytes + size > self._max_bytes
        ):
            sealed = self.seal()
        if not self._events:
            self._started = self._clock()
        self._events.append(event)
        self._bytes += size
        return sealed

    def seal(self) -> IngestBatch | None:
        """Return the pending events as a batch and start empty; ``None`` when nothing waits."""
        if not self._events:
            return None
        events = self._events
        self._events = []
        self._bytes = ENVELOPE_BYTES
        self._started = None
        return IngestBatch(
            schema_version=SCHEMA_VERSION,
            tenant_id=self._tenant_id,
            source_id=self._source_id,
            batch_id=new_ulid(),
            sent_at=self._clock(),
            events=events,
        )


@dataclass(slots=True)
class _SourceState:
    """Cursor bookkeeping of one source (see the module docstring)."""

    accumulator: BatchAccumulator
    candidate: Cursor | None = None
    committed: Cursor | None = None
    uses_cursor: bool = False
    lost: bool = False


@dataclass(slots=True)
class _Tally:
    """Per-source counts of one call, flushed into the metrics once."""

    records: Counter[str] = field(default_factory=Counter)
    events: Counter[str] = field(default_factory=Counter)
    dropped: Counter[tuple[str, str]] = field(default_factory=Counter)
    out_of_range: Counter[tuple[str, str]] = field(default_factory=Counter)


class Ingestor:
    """Pipeline, vault, per-source accumulators, disk buffer and cursor commits (plan M1 D1)."""

    __slots__ = (
        "_buffer",
        "_clock",
        "_cursors",
        "_flush_seconds",
        "_link",
        "_lock",
        "_metrics",
        "_runtime",
        "_states",
    )

    def __init__(
        self,
        runtime: EdgeRuntime,
        buffer: DiskBuffer,
        cursors: CursorStore | None,
        *,
        link: CoreLinkSettings,
        metrics: EdgeMetrics,
        clock: Clock | None = None,
    ) -> None:
        self._runtime = runtime
        self._buffer = buffer
        self._cursors = cursors
        self._link = link
        self._metrics = metrics
        if runtime.metrics is None:  # the runtime's housekeeping counts into the same registry
            runtime.metrics = metrics
        self._clock = clock if clock is not None else _utc_now
        self._flush_seconds = link.batch_flush_seconds
        self._lock = threading.Lock()
        self._states: dict[str, _SourceState] = {}

    def __repr__(self) -> str:
        return f"Ingestor(sources={len(self._states)}, batch_events={self._link.batch_events})"

    # -- IngestorLike -------------------------------------------------------------------------

    def ingest(self, records: Iterable[ReadItem], *, durable: bool = False) -> IngestOutcome:
        """Process ``records`` in order; full batches are durably buffered before this returns.

        ``durable`` (push receivers): also seal and append the partial batches of every source
        these records belong to, so the caller's acknowledgement covers only what is on disk; a
        refused batch shows up as ``buffer_full`` in the outcome. Checkpoints among the records
        are placed in order without going through the pipeline (ADR 0035)."""
        outcome = IngestOutcome()
        items: list[tuple[ReadItem, PipelineResult | None]] = [
            (
                item,
                None
                if isinstance(item, CursorCheckpoint)
                else self._runtime.pipeline.process(item),
            )
            for item in records
        ]
        if not items:
            return outcome
        processed = [
            (item, result)
            for item, result in items
            if isinstance(item, RawRecord) and result is not None
        ]
        outcome.vault_entries = self._runtime.write_vault_entries(
            result for _record, result in processed
        )
        tally = _Tally()
        with self._lock:
            reported: set[str] = set()
            for item, result in items:
                if isinstance(item, CursorCheckpoint):
                    self._place_checkpoint(item)
                elif result is not None:
                    self._place(item, result, outcome, tally, reported)
            if durable:
                self._seal_touched(processed, outcome, tally, reported)
            for source_id in reported:
                self._states[source_id].lost = False
            self._commit_idle()
            if outcome.batches_sealed or buffer_refused(outcome):
                self._refresh_buffer_gauges()
        self._publish(tally)
        if outcome.vault_entries:
            self._metrics.inc("carto_edge_vault_entries_total", outcome.vault_entries)
        return outcome

    def flush(self) -> int:
        """Seal every partial batch into the buffer and commit the cursors behind them."""
        return self._seal_where(lambda _state: True)

    def flush_stale(self, now: datetime | None = None) -> int:
        """Seal the partial batches whose first event waited ``batch_flush_seconds`` or more."""
        moment = now if now is not None else self._clock()
        return self._seal_where(
            lambda state: state.accumulator.age_seconds(moment) >= self._flush_seconds
        )

    def backpressure(self) -> BackpressureState:
        """FULL at the buffer cap, SLOW above the backpressure ratio, OK otherwise (spec 8.5)."""
        if self._buffer.is_full():
            state = BackpressureState.FULL
        elif self._buffer.is_above_backpressure():
            state = BackpressureState.SLOW
        else:
            state = BackpressureState.OK
        self._metrics.set("carto_edge_backpressure", _BACKPRESSURE_GAUGE[state])
        return state

    def pending_events(self) -> int:
        """Events waiting in partial batches across all sources."""
        with self._lock:
            return sum(state.accumulator.pending for state in self._states.values())

    # -- internals (called with the lock held) -----------------------------------------------

    def _state(self, source_id: str) -> _SourceState:
        state = self._states.get(source_id)
        if state is None:
            state = _SourceState(
                BatchAccumulator(
                    self._runtime.tenant_id,
                    source_id,
                    max_events=self._link.batch_events,
                    max_bytes=self._link.batch_bytes,
                    clock=self._clock,
                )
            )
            source = self._runtime.pipeline.source(source_id)
            state.uses_cursor = source is not None and source.type in PULL_SOURCE_TYPES
            self._states[source_id] = state
        return state

    def _place(
        self,
        record: RawRecord,
        result: PipelineResult,
        outcome: IngestOutcome,
        tally: _Tally,
        reported: set[str],
    ) -> None:
        source_id = record.source_id
        known = self._runtime.pipeline.source(source_id) is not None
        label = source_id if known else UNKNOWN_SOURCE_LABEL
        outcome.records += 1
        tally.records[label] += 1
        if result.out_of_range is not None:
            tally.out_of_range[label, result.out_of_range] += 1
        if result.event is not None:
            outcome.events += 1
            tally.events[label] += 1
        if not known:
            reason = result.dropped_reason or "unknown"
            outcome.dropped[reason] += 1
            tally.dropped[label, reason] += 1
            return
        state = self._state(source_id)
        if state.lost:
            # Records after a lost batch: refused until the reader restarts from the cursor.
            outcome.dropped[REASON_BUFFER_FULL] += 1
            tally.dropped[source_id, REASON_BUFFER_FULL] += 1
            reported.add(source_id)
            return
        if result.event is None:
            reason = result.dropped_reason or "unknown"
            outcome.dropped[reason] += 1
            tally.dropped[source_id, reason] += 1
        else:
            before = state.candidate
            sealed = state.accumulator.add(result.event)
            if sealed is not None:
                stored = self._store(state, sealed, before, outcome, tally)
                if stored == "reread":
                    # The batch was refused and this event went with the discarded tail.
                    reported.add(source_id)
                    return
        if record.commit_cursor is not None:
            state.candidate = record.commit_cursor
            state.uses_cursor = True

    def _place_checkpoint(self, checkpoint: CursorCheckpoint) -> None:
        """A cursor with no record: it becomes the candidate unless the source lost a batch
        (then the reader must restart from the committed cursor) or is unknown."""
        if self._runtime.pipeline.source(checkpoint.source_id) is None:
            return
        state = self._state(checkpoint.source_id)
        if state.lost:
            return
        state.candidate = checkpoint.cursor
        state.uses_cursor = True

    def _store(
        self,
        state: _SourceState,
        batch: IngestBatch,
        commit: Cursor | None,
        outcome: IngestOutcome | None,
        tally: _Tally,
    ) -> Stored:
        """Append ``batch``, then commit ``commit``. When the buffer refuses it (full, or the
        write failed), count the loss and, for a pulled source, discard what is pending and refuse
        the source until reported, so its cursor can never move past the lost events."""
        source_id = state.accumulator.source_id
        try:
            self._buffer.append(batch, source_id)
        except (BufferFullError, sqlite3.Error, OSError) as exc:
            reason = REASON_BUFFER_FULL if isinstance(exc, BufferFullError) else REASON_BUFFER_ERROR
            lost = len(batch.events)
            if state.uses_cursor:
                discarded = state.accumulator.seal()
                lost += len(discarded.events) if discarded is not None else 0
                state.candidate = state.committed
                state.lost = True
            if outcome is not None:
                outcome.dropped[reason] += lost
            tally.dropped[source_id, reason] += lost
            log.warning(
                "batch.refused",
                reason=reason,
                source_id=source_id,
                batch_id=batch.batch_id,
                events=lost,
                rereads=state.uses_cursor,
                error=type(exc).__name__,
            )
            return "reread" if state.uses_cursor else "refused"
        if outcome is not None:
            outcome.batches_sealed += 1
        self._metrics.inc("carto_edge_batches_sealed_total")
        if commit is not None:
            self._commit(state, commit)
        return "stored"

    def _commit(self, state: _SourceState, cursor: Cursor) -> None:
        if cursor == state.committed:
            return
        source_id = state.accumulator.source_id
        if self._cursors is not None:
            try:
                self._cursors.set(source_id, cursor)
            except CursorError as exc:
                # The connector made a cursor the store refuses; the old one stays (re-reads,
                # never skips). The message names the limit, never the cursor.
                log.warning("cursor.refused", source_id=source_id, error=str(exc))
                return
        state.committed = cursor

    def _seal_touched(
        self,
        processed: list[tuple[RawRecord, PipelineResult]],
        outcome: IngestOutcome,
        tally: _Tally,
        reported: set[str],
    ) -> None:
        """Seal the pending batch of every source in ``processed`` (lock held)."""
        for source_id in dict.fromkeys(record.source_id for record, _result in processed):
            state = self._states.get(source_id)
            if state is None or state.accumulator.pending == 0:
                continue
            batch = state.accumulator.seal()
            if batch is None:
                continue
            if self._store(state, batch, state.candidate, outcome, tally) == "reread":
                reported.add(source_id)

    def _commit_idle(self) -> None:
        """Sources with nothing pending: every placed record is durable or dropped."""
        for state in self._states.values():
            if (
                not state.lost
                and state.accumulator.pending == 0
                and state.candidate is not None
                and state.candidate != state.committed
            ):
                self._commit(state, state.candidate)

    def _seal_where(self, wanted: Callable[[_SourceState], bool]) -> int:
        sealed = 0
        tally = _Tally()
        with self._lock:
            for state in self._states.values():
                if state.accumulator.pending == 0 or not wanted(state):
                    continue
                batch = state.accumulator.seal()
                if batch is None:
                    continue
                if self._store(state, batch, state.candidate, None, tally) == "stored":
                    sealed += 1
            self._commit_idle()
            if sealed or tally.dropped:
                self._refresh_buffer_gauges()
        self._publish(tally)
        return sealed

    def _refresh_buffer_gauges(self) -> None:
        self._metrics.set("carto_edge_buffer_bytes", self._buffer.bytes())
        self._metrics.set("carto_edge_buffer_depth", self._buffer.depth())

    def _publish(self, tally: _Tally) -> None:
        for label, count in tally.records.items():
            self._metrics.inc("carto_edge_records_total", count, source_id=label)
        for label, count in tally.events.items():
            self._metrics.inc("carto_edge_events_total", count, source_id=label)
        for (label, reason), count in tally.dropped.items():
            self._metrics.inc("carto_edge_dropped_total", count, source_id=label, reason=reason)
        for (label, reason), count in tally.out_of_range.items():
            self._metrics.inc(
                "carto_edge_events_out_of_range_total", count, source_id=label, reason=reason
            )
