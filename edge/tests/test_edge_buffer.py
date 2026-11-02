"""carto_edge.pipeline.buffer: durable appends, exact size accounting, backpressure, acks,
parking and the round trip of the compressed wire form (spec 8.5, ADR 0014); the versioned
schema, parked rows aged from when they were parked, and the bounded purged ledger (ADR 0073)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import zstandard

from carto_common.ids import new_ulid
from carto_edge.pipeline.buffer import (
    PURGED_LEDGER_ROWS,
    SCHEMA_VERSION,
    BufferFullError,
    BufferSchemaError,
    DiskBuffer,
    compress_batch,
    decompress_batch,
)
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def _batch(events: int = 2, source_id: str = "src_wms_db") -> IngestBatch:
    example = CanonicalEvent.example().model_dump()
    items = [
        CanonicalEvent.model_validate(
            example | {"event_id": f"01J9ZK8X5Q8V3N6M2T4R7W1Y{i:02d}"[:26], "source_id": source_id}
        )
        for i in range(events)
    ]
    return IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id=source_id,
        batch_id=new_ulid(),
        sent_at=NOW,
        events=items,
    )


def test_append_is_durable_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "buffer.sqlite"
    batch = _batch()
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as buffer:
        row_id = buffer.append(batch)
        assert row_id >= 1
        assert buffer.depth() == 1
        assert buffer.pending_events() == 2
        assert buffer.bytes() == len(compress_batch(batch))
        assert buffer.oldest_at() == NOW
    # A fresh handle (as after a crash and restart) sees the committed row.
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as reopened:
        stored = reopened.oldest()
        assert stored is not None
        assert stored.batch_id == batch.batch_id
        assert stored.source_id == "src_wms_db"
        assert decompress_batch(stored.payload) == batch
        assert reopened.bytes() == stored.size_bytes
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_same_batch_id_is_stored_once(tmp_path: Path) -> None:
    batch = _batch()
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        first = buf.append(batch)
        second = buf.append(batch)
        assert first == second
        assert buf.depth() == 1


def test_ack_removes_rows_and_bytes(tmp_path: Path) -> None:
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        ids = [buf.append(_batch()) for _ in range(3)]
        assert buf.depth() == 3
        assert buf.ack(ids[:2]) == 2
        assert buf.depth() == 1
        remaining = buf.iter_pending()
        assert [row.id for row in remaining] == [ids[2]]
        assert buf.bytes() == remaining[0].size_bytes
        assert buf.ack([ids[0]]) == 0  # already gone
        assert buf.ack(ids[2:]) == 1
        assert buf.bytes() == 0
        assert buf.oldest_at() is None


def test_pending_is_oldest_first(tmp_path: Path) -> None:
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        batches = [_batch() for _ in range(4)]
        for batch in batches:
            buf.append(batch)
        pending = buf.iter_pending(limit=3)
        assert [row.batch_id for row in pending] == [b.batch_id for b in batches[:3]]


def test_backpressure_and_full(tmp_path: Path) -> None:
    # Each batch id is a fresh ULID, so the compressed sizes differ by a byte or two: the cap
    # is the exact sum of the three payloads that will be stored.
    batches = [_batch() for _ in range(3)]
    total = sum(len(compress_batch(batch)) for batch in batches)
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=total, backpressure_ratio=0.5) as buf:
        buf.append(batches[0])
        assert not buf.is_above_backpressure()
        buf.append(batches[1])
        assert buf.is_above_backpressure()  # two of three payloads >= 50%
        assert not buf.is_full()
        buf.append(batches[2])
        assert buf.is_full()
        with pytest.raises(BufferFullError):
            buf.append(_batch())
        assert buf.depth() == 3


def test_park_keeps_bytes_but_leaves_queue(tmp_path: Path) -> None:
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        first = buf.append(_batch())
        second = buf.append(_batch())
        assert buf.park(first)
        assert not buf.park(first)
        assert buf.depth() == 1
        assert buf.parked() == 1
        assert buf.oldest() is not None
        assert buf.oldest().id == second  # type: ignore[union-attr]
        assert buf.bytes() > 0
        assert buf.ack([first]) == 1  # an operator can still clear it


def test_purge_parked_removes_old_parked_rows_and_their_bytes(tmp_path: Path) -> None:
    """ADR 0073: a parked row older than the age is deleted and its bytes released; a younger
    parked row and an old pending row stay (pending rows are core's to acknowledge)."""
    old_at = NOW - timedelta(days=8)
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        old_batch, young_batch, pending_batch = _batch(), _batch(), _batch()
        old_id = buf.append_bytes(
            compress_batch(old_batch),
            source_id="src_wms_db",
            batch_id=old_batch.batch_id,
            events=2,
            created_at=old_at,
        )
        young_id = buf.append(young_batch)  # created at NOW
        buf.append_bytes(
            compress_batch(pending_batch),
            source_id="src_wms_db",
            batch_id=pending_batch.batch_id,
            events=2,
            created_at=old_at,
        )
        assert buf.park(old_id, at=old_at) and buf.park(young_id, at=NOW)
        before = buf.bytes()
        old_size = len(compress_batch(old_batch))
        assert buf.purge_parked(NOW - timedelta(days=7)) == (1, old_batch.batch_id)
        assert buf.parked() == 1
        assert buf.depth() == 1, "the old pending row is not touched"
        assert buf.bytes() == before - old_size
        assert buf.purge_parked(NOW - timedelta(days=7)) == (0, None)
        assert buf.purge_parked(NOW + timedelta(seconds=1)) == (1, young_batch.batch_id)
        assert buf.parked() == 0
        assert buf.bytes() == len(compress_batch(pending_batch))
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as again:
        assert again.bytes() == len(compress_batch(pending_batch)), "the delete was committed"


def test_parked_rows_age_from_when_they_were_parked_not_from_append(tmp_path: Path) -> None:
    """ADR 0073: a batch that waited a month in the queue and was parked today gets its full
    parked_max_age_days from today; aging it from its append time would purge it at once."""
    appended_at = NOW - timedelta(days=30)
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        stored = _batch()
        row_id = buf.append_bytes(
            compress_batch(stored),
            source_id="src_wms_db",
            batch_id=stored.batch_id,
            events=2,
            created_at=appended_at,
        )
        assert buf.park(row_id, at=NOW, reason="http_422")
        assert buf.purge_parked(NOW - timedelta(days=7)) == (0, None), "parked today: kept"
        assert buf.parked() == 1
        assert buf.purge_parked(NOW + timedelta(days=7, seconds=1)) == (1, stored.batch_id)
        assert buf.parked() == 0
        # park() without a time stamps the wall clock, never the append time.
        later = _batch()
        later_id = buf.append_bytes(
            compress_batch(later),
            source_id="src_wms_db",
            batch_id=later.batch_id,
            events=2,
            created_at=appended_at,
        )
        assert buf.park(later_id)
        assert buf.purge_parked(datetime.now(UTC) - timedelta(seconds=60)) == (0, None)


def _v1_schema(path: Path, *, batch: IngestBatch, created_at: datetime, state: int) -> None:
    """A buffer file as the code before ADR 0073's ledger wrote it: no user_version, no
    parked_at_ms, no purged table."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS batches ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, "
            "batch_id TEXT NOT NULL UNIQUE, events INTEGER NOT NULL, size INTEGER NOT NULL, "
            "created_at_ms INTEGER NOT NULL, state INTEGER NOT NULL DEFAULT 0, "
            "payload BLOB NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS batches_state_id ON batches (state, id)")
        payload = compress_batch(batch)
        conn.execute(
            "INSERT INTO batches (source_id, batch_id, events, size, created_at_ms, state, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "src_wms_db",
                batch.batch_id,
                2,
                len(payload),
                int(created_at.timestamp() * 1000),
                state,
                payload,
            ),
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_a_buffer_from_before_schema_versioning_is_migrated_on_open(tmp_path: Path) -> None:
    """The schema is versioned with ``PRAGMA user_version`` (ADR 0073). A file at version 0
    that already holds ``batches`` is a version-1 file: it gains the parked columns and the
    purged table, keeps its rows, and a row parked before the migration (no parked_at_ms) is
    aged from its append time, which is all that is known about it."""
    path = tmp_path / "b.sqlite"
    parked_before, pending_before = _batch(), _batch()
    _v1_schema(path, batch=parked_before, created_at=NOW - timedelta(days=30), state=1)
    with sqlite3.connect(path) as conn:
        payload = compress_batch(pending_before)
        conn.execute(
            "INSERT INTO batches (source_id, batch_id, events, size, created_at_ms, state, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("src_wms_db", pending_before.batch_id, 2, len(payload), 1, 0, payload),
        )
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        assert buf.schema_version() == SCHEMA_VERSION
        assert buf.parked() == 1 and buf.depth() == 1
        assert buf.bytes() == len(compress_batch(parked_before)) + len(payload)
        columns = {row[1] for row in buf._conn.execute("PRAGMA table_info(batches)").fetchall()}
        assert {"parked_at_ms", "parked_reason"} <= columns
        assert buf.purge_parked(NOW - timedelta(days=7)) == (1, parked_before.batch_id)
        assert buf.parked() == 0
        assert buf.depth() == 1, "the pending row is untouched"
        (entry,) = buf.purged_ledger()
        assert entry.batch_id == parked_before.batch_id
        assert entry.parked_reason is None, "nothing was recorded before the migration"
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as again:
        assert again.schema_version() == SCHEMA_VERSION, "opening again migrates nothing"
        assert again.depth() == 1


def test_a_buffer_written_by_a_newer_edge_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.sqlite"
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        buf.append(_batch())
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(BufferSchemaError, match="newer"):
        DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8)


def test_purged_batches_are_recorded_in_a_bounded_ledger(tmp_path: Path) -> None:
    """ADR 0073: a purge is a data-loss event. Each purged batch leaves a row in ``purged``
    (batch id, event count, parked reason, when; never a payload), kept to the last
    ``PURGED_LEDGER_ROWS`` rows."""
    path = tmp_path / "b.sqlite"
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        assert buf.purged_ledger() == []
        first, second = _batch(events=3), _batch(events=1)
        first_id, second_id = buf.append(first), buf.append(second)
        assert buf.park(first_id, at=NOW - timedelta(days=9), reason="http_422")
        assert buf.park(second_id, at=NOW - timedelta(days=8), reason="http_400")
        assert buf.purge_parked(NOW - timedelta(days=7), now=NOW) == (2, first.batch_id)
        ledger = buf.purged_ledger()
        assert [(e.batch_id, e.events, e.parked_reason, e.purged_at) for e in ledger] == [
            (second.batch_id, 1, "http_400", NOW),
            (first.batch_id, 3, "http_422", NOW),
        ], "newest first"
        assert buf.purged_ledger(limit=1) == ledger[:1]
        # The ledger is bounded: with 10,000 earlier purges on file, one more drops the oldest.
        buf._conn.executemany(
            "INSERT INTO purged (batch_id, events, parked_reason, purged_at_ms) "
            "VALUES (?, ?, ?, ?)",
            [(f"FILLER{index:020d}", 1, "http_422", 0) for index in range(PURGED_LEDGER_ROWS)],
        )
        third = _batch()
        assert buf.park(buf.append(third), at=NOW - timedelta(days=8), reason="http_413")
        assert buf.purge_parked(NOW - timedelta(days=7), now=NOW) == (1, third.batch_id)
        count = buf._conn.execute("SELECT COUNT(*) FROM purged").fetchone()
        assert count is not None and count[0] == PURGED_LEDGER_ROWS
        assert buf.purged_ledger(limit=1)[0].batch_id == third.batch_id
        kept = {row[0] for row in buf._conn.execute("SELECT batch_id FROM purged").fetchall()}
        assert first.batch_id not in kept and second.batch_id not in kept
        assert f"FILLER{0:020d}" not in kept, "the three oldest rows went"
        assert f"FILLER{1:020d}" in kept and f"FILLER{PURGED_LEDGER_ROWS - 1:020d}" in kept
    with DiskBuffer(path, max_bytes=10_000_000, backpressure_ratio=0.8) as again:
        assert again.purged_ledger(limit=1)[0].batch_id == third.batch_id, "durable"


def test_rejects_bad_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        DiskBuffer(tmp_path / "b.sqlite", max_bytes=0, backpressure_ratio=0.8)
    with pytest.raises(ValueError, match="backpressure_ratio"):
        DiskBuffer(tmp_path / "b.sqlite", max_bytes=10, backpressure_ratio=1.0)


def test_decompress_is_bounded() -> None:
    payload = compress_batch(_batch())
    assert decompress_batch(payload).source_id == "src_wms_db"
    with pytest.raises(zstandard.ZstdError):
        decompress_batch(b"not zstd")


class _FailingCommit:
    """Delegates to a real connection but fails every COMMIT."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._inner.execute(sql, *args)

    def close(self) -> None:
        self._inner.close()


def test_a_failed_ack_keeps_the_byte_count_exact(tmp_path: Path) -> None:
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        row_id = buf.append(_batch())
        before = buf.bytes()
        real = buf._conn
        buf._conn = _FailingCommit(real)  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            buf.ack([row_id])
        buf._conn = real
        assert buf.bytes() == before
        assert buf.depth() == 1


def test_commits_are_synced_to_disk(tmp_path: Path) -> None:
    """synchronous=FULL: a cursor in another file can never be durable before its batch."""
    with DiskBuffer(tmp_path / "b.sqlite", max_bytes=10_000_000, backpressure_ratio=0.8) as buf:
        level = buf._conn.execute("PRAGMA synchronous").fetchone()
        assert level is not None and level[0] == 2  # FULL
