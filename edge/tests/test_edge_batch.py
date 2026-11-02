"""carto_edge.pipeline.batch: batches of at most max_events events or max_bytes of JSON (spec
8.5), and the ingestor's cursor rule (spec 8.1 "Cursor checkpointing"): a cursor is committed
only after every event of every record up to it is durably in the disk buffer; a batch the full
buffer refuses never advances the cursor, and its source is told to re-read (at-least-once)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_common.ids import is_ulid
from carto_edge.config import (
    CoreLinkSettings,
    EdgeSettings,
    PiiSettings,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.metrics import EdgeMetrics, default_metrics
from carto_edge.pipeline.batch import (
    REASON_BUFFER_ERROR,
    REASON_BUFFER_FULL,
    UNKNOWN_SOURCE_LABEL,
    BatchAccumulator,
    Ingestor,
    buffer_refused,
)
from carto_edge.pipeline.buffer import DiskBuffer, decompress_batch
from carto_edge.pipeline.ingestor import BackpressureState, IngestorLike
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.pipeline import REASON_UNKNOWN_SOURCE
from carto_edge.runtime import EdgeRuntime, build_runtime
from carto_edge.state import CursorStore
from carto_schema.event import CanonicalEvent, EventKind
from carto_schema.ingest import IngestBatch

NOW = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)
BIG = 1024**3


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------------------------
# BatchAccumulator
# ---------------------------------------------------------------------------------------------


def event(i: int, *, source_id: str = "src_wms_db", padding: int = 0) -> CanonicalEvent:
    data: dict[str, Any] = CanonicalEvent.example().model_dump()
    data["event_id"] = f"01J9ZK8X5Q8V3N6M2T4R7W{i:04d}"
    data["source_id"] = source_id
    if padding:
        data["attributes"] = {f"a{n}": "v" * 200 for n in range(padding)}
    return CanonicalEvent.model_validate(data)


def json_size(batch: IngestBatch) -> int:
    return len(batch.model_dump_json().encode("utf-8"))


def test_accumulator_seals_when_the_event_count_would_be_exceeded() -> None:
    clock = Clock()
    acc = BatchAccumulator("default", "src_wms_db", max_events=3, max_bytes=BIG, clock=clock)
    assert acc.pending == 0
    assert acc.add(event(1)) is None
    assert acc.add(event(2)) is None
    assert acc.add(event(3)) is None
    assert acc.pending == 3
    sealed = acc.add(event(4))
    assert sealed is not None
    assert [e.event_id for e in sealed.events] == [event(i).event_id for i in (1, 2, 3)]
    assert sealed.tenant_id == "default"
    assert sealed.source_id == "src_wms_db"
    assert sealed.schema_version == "1"
    assert sealed.sent_at == NOW
    assert is_ulid(sealed.batch_id)
    assert acc.pending == 1
    rest = acc.seal()
    assert rest is not None
    assert [e.event_id for e in rest.events] == [event(4).event_id]
    assert rest.batch_id != sealed.batch_id
    assert acc.pending == 0
    assert acc.seal() is None


def test_accumulator_seals_when_the_size_would_be_exceeded() -> None:
    one = len(event(1, padding=40).model_dump_json().encode("utf-8"))
    acc = BatchAccumulator("default", "src_wms_db", max_events=5000, max_bytes=3 * one + 1024)
    sealed: list[IngestBatch] = []
    for i in range(10):
        batch = acc.add(event(i, padding=40))
        if batch is not None:
            sealed.append(batch)
    tail = acc.seal()
    assert tail is not None
    sealed.append(tail)
    assert sum(len(batch.events) for batch in sealed) == 10
    assert all(len(batch.events) <= 3 for batch in sealed)
    assert all(json_size(batch) <= 3 * one + 1024 for batch in sealed)


def test_a_single_event_is_always_accepted() -> None:
    acc = BatchAccumulator("default", "src_wms_db", max_events=10, max_bytes=1024)
    assert acc.add(event(1, padding=50)) is None  # larger than max_bytes on its own
    assert acc.pending == 1
    sealed = acc.add(event(2))
    assert sealed is not None
    assert len(sealed.events) == 1


def test_age_counts_from_the_first_pending_event() -> None:
    clock = Clock()
    acc = BatchAccumulator("default", "src_wms_db", max_events=10, max_bytes=BIG, clock=clock)
    assert acc.age_seconds(NOW) == 0.0
    acc.add(event(1))
    clock.advance(5)
    acc.add(event(2))
    assert acc.age_seconds(clock()) == pytest.approx(5.0)
    acc.seal()
    assert acc.age_seconds(clock()) == 0.0


def test_accumulator_refuses_an_event_of_another_source() -> None:
    acc = BatchAccumulator("default", "src_wms_db", max_events=10, max_bytes=BIG)
    with pytest.raises(ValueError, match="source"):
        acc.add(event(1, source_id="src_other"))
    with pytest.raises(ValueError, match="max_events"):
        BatchAccumulator("default", "src_wms_db", max_events=5001, max_bytes=BIG)
    with pytest.raises(ValueError, match="max_bytes"):
        BatchAccumulator("default", "src_wms_db", max_events=10, max_bytes=100)


@settings(max_examples=30, deadline=None)
@given(
    paddings=st.lists(st.integers(min_value=0, max_value=30), min_size=1, max_size=40),
    max_events=st.integers(min_value=1, max_value=8),
    max_kib=st.integers(min_value=2, max_value=40),
)
def test_sealed_batches_respect_both_limits(
    paddings: list[int], max_events: int, max_kib: int
) -> None:
    max_bytes = max_kib * 1024
    acc = BatchAccumulator("default", "src_wms_db", max_events=max_events, max_bytes=max_bytes)
    batches: list[IngestBatch] = []
    for i, padding in enumerate(paddings):
        sealed = acc.add(event(i, padding=padding))
        if sealed is not None:
            batches.append(sealed)
    tail = acc.seal()
    if tail is not None:
        batches.append(tail)
    assert [e.event_id for b in batches for e in b.events] == [
        event(i).event_id for i in range(len(paddings))
    ]
    for batch in batches:
        assert len(batch.events) <= max_events
        assert len(batch.events) == 1 or json_size(batch) <= max_bytes


# ---------------------------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------------------------


def sources() -> SourcesFile:
    return SourcesFile(
        systems=[SystemConfig(id="sys_web", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_web",
                type=SourceType.UPLOAD,
                config={"paths": ["web/*.ndjson"]},
            ),
            SourceConfig(id="src_push", system="sys_web", type=SourceType.OTLP),
        ],
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[EdgeRuntime]:
    settings_ = EdgeSettings(state_dir=tmp_path / "state", pii=PiiSettings(enabled=False))
    built = build_runtime(settings_, sources(), init_local_keys=True, detector=RegexDetector())
    yield built
    built.close()


@pytest.fixture
def buffer(tmp_path: Path) -> Iterator[DiskBuffer]:
    with DiskBuffer(tmp_path / "buffer.sqlite", max_bytes=BIG, backpressure_ratio=0.8) as buf:
        yield buf


@pytest.fixture
def cursors(tmp_path: Path) -> Iterator[CursorStore]:
    with CursorStore(tmp_path / "cursors.sqlite") as store:
        yield store


def record(
    i: int, *, source_id: str = "src_web", cursor: bool = True, text: str | None = None
) -> RawRecord:
    payload = {
        "ts": "2026-09-23T04:00:00Z",
        "level": "info",
        "msg": "cart created",
        "cart_id": f"c-{88000 + i}",
    }
    return RawRecord(
        source_id=source_id,
        system_id="sys_web",
        kind=EventKind.LOG,
        locator=f"app.ndjson:line:{i}",
        received_at=NOW,
        text=json.dumps(payload) if text is None else text,
        sequence=i,
        commit_cursor={"file": "app.ndjson", "line": i} if cursor else None,
    )


@dataclass
class Rig:
    ingestor: Ingestor
    buffer: DiskBuffer
    cursors: CursorStore
    metrics: EdgeMetrics
    clock: Clock

    def buffered_events(self) -> list[CanonicalEvent]:
        return [
            item
            for row in self.buffer.iter_pending(1000)
            for item in decompress_batch(row.payload).events
        ]


def rig(
    runtime: EdgeRuntime,
    buffer: DiskBuffer,
    cursors: CursorStore,
    *,
    events: int = 3,
    flush_seconds: float = 2.0,
) -> Rig:
    metrics = default_metrics()
    clock = Clock()
    link = CoreLinkSettings(batch_events=events, batch_flush_seconds=flush_seconds)
    ingestor = Ingestor(runtime, buffer, cursors, link=link, metrics=metrics, clock=clock)
    return Rig(ingestor, buffer, cursors, metrics, clock)


def line(cursor: object) -> object:
    assert isinstance(cursor, dict)
    return cursor["line"]


def test_ingestor_implements_the_protocol(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    assert isinstance(rig(runtime, buffer, cursors).ingestor, IngestorLike)


def test_cursor_advances_only_with_sealed_batches(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=3)
    outcome = r.ingestor.ingest([record(1), record(2)])
    assert (outcome.records, outcome.events, outcome.batches_sealed) == (2, 2, 0)
    assert cursors.get("src_web") is None  # both events sit in a partial batch
    r.ingestor.ingest([record(3)])
    assert cursors.get("src_web") is None
    outcome = r.ingestor.ingest([record(4)])
    assert outcome.batches_sealed == 1
    assert buffer.depth() == 1
    assert line(cursors.get("src_web")) == 3  # record 4 is still pending
    assert r.ingestor.flush() == 1
    assert line(cursors.get("src_web")) == 4
    assert len(r.buffered_events()) == 4
    assert r.metrics.get("carto_edge_batches_sealed_total") == 2
    assert r.metrics.get("carto_edge_buffer_depth") == 2
    assert r.metrics.get("carto_edge_buffer_bytes") == buffer.bytes()


def test_one_call_commits_up_to_the_last_sealed_batch(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=3)
    outcome = r.ingestor.ingest([record(i) for i in range(1, 8)])
    assert outcome.batches_sealed == 2
    assert line(cursors.get("src_web")) == 6
    assert r.ingestor.flush() == 1
    assert line(cursors.get("src_web")) == 7
    ids = [e.event_id for e in r.buffered_events()]
    assert len(ids) == len(set(ids)) == 7


def test_dropped_records_advance_the_cursor_only_behind_durable_events(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=3)
    outcome = r.ingestor.ingest([record(1, text="   "), record(2, text="   ")])
    assert outcome.events == 0
    assert outcome.dropped_total == 2
    assert line(cursors.get("src_web")) == 2  # nothing pending: nothing to wait for
    r.ingestor.ingest([record(3), record(4, text="   ")])
    assert line(cursors.get("src_web")) == 2  # record 3's event is not durable yet
    r.ingestor.flush()
    assert line(cursors.get("src_web")) == 4
    assert r.metrics.get("carto_edge_dropped_total", source_id="src_web", reason="empty") == 3


def test_records_out_of_the_plausible_range_are_counted_by_source_and_reason(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    """ADR 0036: the gateway range is read on the wall clock, pinned to 2026-10-15 in tests."""
    r = rig(runtime, buffer, cursors, events=10)

    def at(i: int, ts: str) -> RawRecord:
        payload = {"ts": ts, "level": "info", "msg": "cart created"}
        return record(i, text=json.dumps(payload))

    outcome = r.ingestor.ingest(
        [
            at(1, "2026-08-01T00:00:00Z"),
            at(2, "2026-08-02T00:00:00Z"),
            at(3, "2027-01-01T00:00:00Z"),
            at(4, "2026-10-15T00:00:00Z"),
        ]
    )
    assert outcome.events == 2
    assert outcome.dropped == {"too_old": 2}
    metric = "carto_edge_events_out_of_range_total"
    assert r.metrics.get(metric, source_id="src_web", reason="too_old") == 2
    assert r.metrics.get(metric, source_id="src_web", reason="future") == 1
    assert r.metrics.get("carto_edge_dropped_total", source_id="src_web", reason="too_old") == 2


def test_unknown_source_is_dropped_and_counted(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors)
    outcome = r.ingestor.ingest([record(1, source_id="src_nope")])
    assert outcome.dropped[REASON_UNKNOWN_SOURCE] == 1
    assert r.ingestor.flush() == 0
    assert buffer.depth() == 0
    assert cursors.get("src_nope") is None
    assert (
        r.metrics.get(
            "carto_edge_dropped_total",
            source_id=UNKNOWN_SOURCE_LABEL,
            reason=REASON_UNKNOWN_SOURCE,
        )
        == 1
    )
    assert r.metrics.get("carto_edge_records_total", source_id=UNKNOWN_SOURCE_LABEL) == 1


def test_metrics_and_vault_entries(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=10)
    outcome = r.ingestor.ingest([record(i) for i in range(1, 6)])
    assert outcome.vault_entries >= 1
    r.ingestor.flush()
    assert r.metrics.get("carto_edge_records_total", source_id="src_web") == 5
    assert r.metrics.get("carto_edge_events_total", source_id="src_web") == 5
    assert r.metrics.get("carto_edge_vault_entries_total") == outcome.vault_entries
    event_ = r.buffered_events()[-1]  # names are in clear from the third record (ADR 0030)
    raw = [
        identifier.token
        for identifier in event_.identifiers
        if identifier.form == "raw" and identifier.field == "cart_id"
    ]
    assert raw
    assert runtime.vault.reveal(raw, now=NOW) == dict.fromkeys(raw, "c-88005")


def test_full_buffer_never_advances_the_cursor_and_asks_for_a_re_read(
    runtime: EdgeRuntime, tmp_path: Path, cursors: CursorStore
) -> None:
    with DiskBuffer(tmp_path / "small.sqlite", max_bytes=1, backpressure_ratio=0.5) as small:
        r = rig(runtime, small, cursors, events=2)
        r.ingestor.ingest([record(1), record(2), record(3)])  # seals [1, 2]; the cap is hit
        assert line(cursors.get("src_web")) == 2
        assert r.ingestor.backpressure() is BackpressureState.FULL
        outcome = r.ingestor.ingest([record(4), record(5), record(6)])
        # [3, 4] is refused at record 5; 5 and 6 come after the gap and are refused too.
        assert outcome.dropped[REASON_BUFFER_FULL] == 4
        assert line(cursors.get("src_web")) == 2
        assert r.ingestor.flush() == 0
        assert line(cursors.get("src_web")) == 2
        assert (
            r.metrics.get(
                "carto_edge_dropped_total", source_id="src_web", reason=REASON_BUFFER_FULL
            )
            == 4
        )
        # Core catches up; the reader restarts from the committed cursor.
        small.ack([row.id for row in small.iter_pending()])
        outcome = r.ingestor.ingest([record(i) for i in range(3, 7)])
        assert outcome.dropped_total == 0
        assert line(cursors.get("src_web")) == 4
        small.ack([row.id for row in small.iter_pending()])
        assert r.ingestor.flush() == 1
        assert line(cursors.get("src_web")) == 6
        assert small.pending_events() == 2


def test_a_loss_in_a_foreign_flush_is_reported_to_the_next_ingest(
    runtime: EdgeRuntime, tmp_path: Path, cursors: CursorStore
) -> None:
    with DiskBuffer(tmp_path / "small.sqlite", max_bytes=1, backpressure_ratio=0.5) as small:
        small.append_bytes(b"x", source_id="src_other", batch_id="b1", events=1, created_at=NOW)
        r = rig(runtime, small, cursors, events=10)
        r.ingestor.ingest([record(1), record(2)])
        assert r.ingestor.flush() == 0  # refused: the buffer is full
        assert cursors.get("src_web") is None
        small.ack([row.id for row in small.iter_pending()])
        outcome = r.ingestor.ingest([record(3)])
        assert outcome.dropped[REASON_BUFFER_FULL] == 1  # the reader must restart
        outcome = r.ingestor.ingest([record(1), record(2), record(3)])
        assert outcome.dropped_total == 0
        assert r.ingestor.flush() == 1
        assert line(cursors.get("src_web")) == 3


def test_push_sources_without_cursors_keep_flowing_after_a_loss(
    runtime: EdgeRuntime, tmp_path: Path
) -> None:
    with DiskBuffer(tmp_path / "small.sqlite", max_bytes=1, backpressure_ratio=0.5) as small:
        metrics = default_metrics()
        ingestor = Ingestor(
            runtime, small, None, link=CoreLinkSettings(batch_events=1), metrics=metrics
        )
        pushes = [record(i, source_id="src_push", cursor=False) for i in range(1, 4)]
        outcome = ingestor.ingest(pushes)
        assert outcome.batches_sealed == 1  # [1] fits, [2] is refused, [3] stays pending
        assert outcome.dropped[REASON_BUFFER_FULL] == 1
        small.ack([row.id for row in small.iter_pending()])
        assert ingestor.flush() == 1
        assert small.pending_events() == 1


def test_durable_ingest_seals_only_the_touched_sources(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=100)
    r.ingestor.ingest([record(1)])  # a pull source with a partial batch stays partial
    outcome = r.ingestor.ingest(
        [record(i, source_id="src_push", cursor=False) for i in range(1, 3)], durable=True
    )
    assert outcome.batches_sealed == 1
    assert buffer.depth() == 1
    assert {e.source_id for e in r.buffered_events()} == {"src_push"}
    assert r.ingestor.pending_events() == 1  # src_web is untouched
    assert cursors.get("src_web") is None


def test_durable_ingest_reports_a_refused_push_batch(runtime: EdgeRuntime, tmp_path: Path) -> None:
    with DiskBuffer(tmp_path / "tiny.sqlite", max_bytes=1, backpressure_ratio=0.5) as tiny:
        ingestor = Ingestor(
            runtime, tiny, None, link=CoreLinkSettings(batch_events=100), metrics=default_metrics()
        )
        first = ingestor.ingest([record(1, source_id="src_push", cursor=False)], durable=True)
        assert first.batches_sealed == 1 and not first.dropped
        second = ingestor.ingest([record(2, source_id="src_push", cursor=False)], durable=True)
        assert second.dropped[REASON_BUFFER_FULL] == 1
        assert ingestor.pending_events() == 0


def test_flush_stale_seals_old_partial_batches(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=100, flush_seconds=2.0)
    r.ingestor.ingest([record(1)])
    r.clock.advance(1.0)
    assert r.ingestor.flush_stale() == 0
    assert cursors.get("src_web") is None
    assert r.ingestor.flush_stale(NOW + timedelta(seconds=2.5)) == 1
    assert line(cursors.get("src_web")) == 1
    assert buffer.depth() == 1


def test_backpressure_follows_the_buffer(runtime: EdgeRuntime, tmp_path: Path) -> None:
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=1000, backpressure_ratio=0.5) as buf:
        metrics = default_metrics()
        ingestor = Ingestor(runtime, buf, None, link=CoreLinkSettings(), metrics=metrics)
        assert ingestor.backpressure() is BackpressureState.OK
        assert metrics.get("carto_edge_backpressure") == 0
        buf.append_bytes(b"x" * 600, source_id="src_web", batch_id="a", events=1, created_at=NOW)
        assert ingestor.backpressure() is BackpressureState.SLOW
        assert metrics.get("carto_edge_backpressure") == 1
        buf.append_bytes(b"x" * 600, source_id="src_web", batch_id="b", events=1, created_at=NOW)
        assert ingestor.backpressure() is BackpressureState.FULL
        assert metrics.get("carto_edge_backpressure") == 2


def test_concurrent_ingest_buffers_every_event_once(
    runtime: EdgeRuntime, buffer: DiskBuffer
) -> None:
    ingestor = Ingestor(
        runtime, buffer, None, link=CoreLinkSettings(batch_events=7), metrics=default_metrics()
    )

    def push(worker: int) -> None:
        for i in range(25):
            n = worker * 1000 + i
            ingestor.ingest([record(n, source_id="src_push", cursor=False)])

    threads = [threading.Thread(target=push, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ingestor.flush()
    ids = [
        item.event_id
        for row in buffer.iter_pending(1000)
        for item in decompress_batch(row.payload).events
    ]
    assert len(ids) == len(set(ids)) == 100


def test_ingest_of_nothing_is_a_no_op(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors)
    outcome = r.ingestor.ingest([])
    assert outcome.records == 0
    assert r.ingestor.flush() == 0
    assert buffer.depth() == 0
    assert repr(r.ingestor).startswith("Ingestor(")


# ---------------------------------------------------------------------------------------------
# Review findings: a refused append never lets a cursor pass the lost events
# ---------------------------------------------------------------------------------------------


class FlakyBuffer(DiskBuffer):
    """A DiskBuffer whose next appends fail like a full disk (SQLITE_FULL)."""

    fail_next = 0

    def append(self, batch: IngestBatch, source_id: str | None = None) -> int:
        if self.fail_next:
            self.fail_next -= 1
            raise sqlite3.OperationalError("database or disk is full")
        return super().append(batch, source_id)


def test_a_failed_append_rolls_the_pulled_source_back(runtime: EdgeRuntime, tmp_path: Path) -> None:
    with (
        FlakyBuffer(tmp_path / "b.sqlite", max_bytes=BIG, backpressure_ratio=0.8) as buf,
        CursorStore(tmp_path / "c.sqlite") as cur,
    ):
        ing = Ingestor(
            runtime, buf, cur, link=CoreLinkSettings(batch_events=3), metrics=default_metrics()
        )
        ing.ingest([record(1), record(2), record(3)])
        buf.fail_next = 1
        outcome = ing.ingest([record(4)])  # seals [1, 2, 3]; the append fails
        assert outcome.dropped[REASON_BUFFER_ERROR] == 4  # the batch and the pending [4]
        assert buffer_refused(outcome) == 4
        ing.flush_stale(datetime(2030, 1, 1, tzinfo=UTC))  # housekeeping a second later
        assert cur.get("src_web") is None  # never past the lost events
        assert buf.depth() == 0
        # The reader restarts from the committed cursor and everything arrives.
        again = ing.ingest([record(i) for i in range(1, 5)])
        assert not buffer_refused(again)
        assert ing.flush() == 1  # [1, 2, 3] sealed while ingesting, [4] here
        assert buf.pending_events() == 4
        assert line(cur.get("src_web")) == 4


def test_a_failed_flush_is_not_committed_by_a_later_unrelated_ingest(
    runtime: EdgeRuntime, tmp_path: Path
) -> None:
    with (
        FlakyBuffer(tmp_path / "b.sqlite", max_bytes=BIG, backpressure_ratio=0.8) as buf,
        CursorStore(tmp_path / "c.sqlite") as cur,
    ):
        ing = Ingestor(
            runtime, buf, cur, link=CoreLinkSettings(batch_events=100), metrics=default_metrics()
        )
        ing.ingest([record(1), record(2)])
        buf.fail_next = 1
        assert ing.flush() == 0  # no exception escapes; the loss is counted
        push = ing.ingest([record(9, source_id="src_push", cursor=False)], durable=True)
        assert push.batches_sealed == 1
        assert cur.get("src_web") is None
        refused = ing.ingest([record(3)])  # the next read of src_web learns it must re-read
        assert refused.dropped[REASON_BUFFER_FULL] == 1
        assert cur.get("src_web") is None


def test_a_refusal_before_the_first_cursor_record_still_forces_a_re_read(
    runtime: EdgeRuntime, tmp_path: Path
) -> None:
    """Splunk, SQL and SFTP put the cursor on the last record of a window, page or listing."""
    with (
        DiskBuffer(tmp_path / "s.sqlite", max_bytes=1, backpressure_ratio=0.5) as small,
        CursorStore(tmp_path / "c.sqlite") as cur,
    ):
        small.append_bytes(b"x", source_id="other", batch_id="pre", events=1, created_at=NOW)
        ing = Ingestor(
            runtime, small, cur, link=CoreLinkSettings(batch_events=3), metrics=default_metrics()
        )
        window = [record(i, cursor=False) for i in range(1, 5)] + [record(5)]
        outcome = ing.ingest(window)
        assert buffer_refused(outcome) == 5
        small.ack([row.id for row in small.iter_pending()])  # the forwarder makes room
        ing.flush_stale(datetime(2030, 1, 1, tzinfo=UTC))
        assert cur.get("src_web") is None
        assert small.pending_events() == 0


# ---------------------------------------------------------------------------------------------
# ADR 0035: cursor-only checkpoints
# ---------------------------------------------------------------------------------------------


def checkpoint(line_no: int, source_id: str = "src_web") -> CursorCheckpoint:
    return CursorCheckpoint(source_id=source_id, cursor={"file": "app.ndjson", "line": line_no})


def test_a_checkpoint_with_nothing_pending_is_committed_at_once(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    """An empty Splunk window or SQL range still moves the cursor (ADR 0035 item 1)."""
    r = rig(runtime, buffer, cursors, events=3)
    outcome = r.ingestor.ingest([checkpoint(10)])
    assert outcome.records == 0  # a checkpoint is not a record and carries no event
    assert outcome.events == 0
    assert line(cursors.get("src_web")) == 10
    assert buffer.depth() == 0


def test_a_checkpoint_waits_for_the_records_before_it(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors, events=3)
    r.ingestor.ingest([record(1, cursor=False), record(2, cursor=False), checkpoint(20)])
    assert cursors.get("src_web") is None  # records 1 and 2 sit in a partial batch
    r.ingestor.ingest([record(3, cursor=False), record(4, cursor=False)])
    # [1, 2, 3] was sealed when 4 arrived: the checkpoint after 2 is durable, 3 carries none.
    assert line(cursors.get("src_web")) == 20
    r.ingestor.ingest([checkpoint(40)])
    assert line(cursors.get("src_web")) == 20  # record 4 is still pending
    r.ingestor.flush()
    assert line(cursors.get("src_web")) == 40


def test_a_checkpoint_after_a_lost_batch_is_refused(
    runtime: EdgeRuntime, tmp_path: Path, cursors: CursorStore
) -> None:
    with DiskBuffer(tmp_path / "small.sqlite", max_bytes=1, backpressure_ratio=0.5) as small:
        small.append_bytes(b"x", source_id="other", batch_id="pre", events=1, created_at=NOW)
        r = rig(runtime, small, cursors, events=2)
        outcome = r.ingestor.ingest(
            [
                record(1, cursor=False),
                record(2, cursor=False),
                record(3, cursor=False),
                checkpoint(30),
            ]
        )
        assert buffer_refused(outcome) == 3
        assert cursors.get("src_web") is None  # never past the lost events
        small.ack([row.id for row in small.iter_pending()])
        r.ingestor.flush()
        assert cursors.get("src_web") is None


def test_a_checkpoint_of_an_unknown_source_is_ignored(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    r = rig(runtime, buffer, cursors)
    outcome = r.ingestor.ingest([checkpoint(5, source_id="src_nope")])
    assert outcome.records == 0
    assert cursors.get("src_nope") is None
