"""The disk buffer between the pipeline and the forwarder (spec 8.5, ADR 0014).

Batches wait here, zstd-compressed, until ``ingest-api`` acknowledges them. SQLite in WAL mode,
one connection behind a lock: every :meth:`DiskBuffer.append` is its own committed transaction,
so a cursor committed after ``append`` returns points at data that survives a crash (spec 8.1
"a cursor is committed only after the batch is durably in the disk buffer"). ``PRAGMA
synchronous=FULL``: every commit is synced to disk before ``append`` returns. With ``NORMAL`` a
commit survives an application crash but not a power cut, and because the cursors live in a
separate file the cursor could reach the disk before the batch it covers, which would skip
data, a loss core's deduplication cannot repair. One sync per batch of up to 5,000 events is
cheap.

Each row stores the compressed bytes and their size, so the size cap (``buffer.max_bytes``,
default 20 GB) and the backpressure threshold (``buffer.backpressure_ratio``, default 80%) are
exact (ADR 0014). :meth:`DiskBuffer.is_above_backpressure` is what pull connectors poll to slow
down and push receivers map to 429; :meth:`DiskBuffer.is_full` is the hard stop (503, and
:class:`BufferFullError` on ``append``). A batch that core refuses for good (a 4xx) is parked
by :meth:`DiskBuffer.park` so the queue keeps moving; parked rows still count toward the cap
and appear in the metrics until an operator clears them or the forwarder's housekeeping
deletes those older than ``core.parked_max_age_days`` with :meth:`DiskBuffer.purge_parked`
(ADR 0073), so a persistent refusal cannot fill the buffer with rows nobody will send.

Nothing here logs; payloads are opaque bytes to this module.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import zstandard

from carto_schema.ingest import IngestBatch

__all__ = ["BufferFullError", "BufferedBatch", "DiskBuffer", "compress_batch", "decompress_batch"]

ZSTD_LEVEL: Final = 3
STATE_PENDING: Final = 0
STATE_PARKED: Final = 1
MAX_DECOMPRESSED_BYTES: Final = 64 * 1024 * 1024
"""A stored batch is at most 5 MB of JSON before compression (spec 8.5); 64 MB is a safe ceiling
for :func:`decompress_batch` so a corrupt row cannot expand without bound (spec 2.3 invariant 8)."""

_SCHEMA: Final = (
    (
        "CREATE TABLE IF NOT EXISTS batches ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_id TEXT NOT NULL, "
        "batch_id TEXT NOT NULL UNIQUE, "
        "events INTEGER NOT NULL, "
        "size INTEGER NOT NULL, "
        "created_at_ms INTEGER NOT NULL, "
        "state INTEGER NOT NULL DEFAULT 0, "
        "payload BLOB NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS batches_state_id ON batches (state, id)",
)


class BufferFullError(Exception):
    """The buffer holds ``max_bytes``; the caller must slow down or refuse (spec 8.5)."""


@dataclass(frozen=True, slots=True)
class BufferedBatch:
    """One stored batch; ``payload`` is the zstd-compressed JSON of an :class:`IngestBatch`."""

    id: int
    source_id: str
    batch_id: str
    events: int
    size_bytes: int
    created_at: datetime
    payload: bytes


def compress_batch(batch: IngestBatch) -> bytes:
    """The wire form of a batch: zstd level 3 over its JSON (spec 8.5)."""
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(
        batch.model_dump_json().encode("utf-8")
    )


def decompress_batch(payload: bytes) -> IngestBatch:
    """Inverse of :func:`compress_batch`, bounded by :data:`MAX_DECOMPRESSED_BYTES`."""
    data = zstandard.ZstdDecompressor().decompress(payload, max_output_size=MAX_DECOMPRESSED_BYTES)
    return IngestBatch.model_validate_json(data)


class DiskBuffer:
    """Append-only queue of compressed batches with exact size accounting (spec 8.5)."""

    __slots__ = ("_backpressure_ratio", "_bytes", "_conn", "_lock", "_max_bytes", "_path")

    def __init__(self, path: Path, max_bytes: int, backpressure_ratio: float) -> None:
        if max_bytes <= 0:
            msg = "max_bytes must be positive"
            raise ValueError(msg)
        if not 0.0 < backpressure_ratio < 1.0:
            msg = "backpressure_ratio must be between 0 and 1"
            raise ValueError(msg)
        self._path = path
        self._max_bytes = max_bytes
        self._backpressure_ratio = backpressure_ratio
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        row = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM batches").fetchone()
        self._bytes = int(row[0]) if row is not None else 0

    def __repr__(self) -> str:
        return f"DiskBuffer(path={str(self._path)!r}, bytes={self._bytes}, max={self._max_bytes})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    # -- writes --------------------------------------------------------------------------------

    def append(self, batch: IngestBatch, source_id: str | None = None) -> int:
        """Store ``batch`` durably and return its row id. Raises :class:`BufferFullError` when
        the cap is reached; a batch id already stored is returned without a second row."""
        payload = compress_batch(batch)
        return self.append_bytes(
            payload,
            source_id=source_id if source_id is not None else batch.source_id,
            batch_id=batch.batch_id,
            events=len(batch.events),
            created_at=batch.sent_at,
        )

    def append_bytes(
        self, payload: bytes, *, source_id: str, batch_id: str, events: int, created_at: datetime
    ) -> int:
        size = len(payload)
        created_ms = int(created_at.astimezone(UTC).timestamp() * 1000)
        with self._lock:
            if self._bytes >= self._max_bytes:
                msg = f"disk buffer holds {self._bytes} bytes; the cap is {self._max_bytes}"
                raise BufferFullError(msg)
            existing = self._conn.execute(
                "SELECT id FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            cursor = self._conn.execute(
                "INSERT INTO batches (source_id, batch_id, events, size, created_at_ms, state, "
                "payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_id, batch_id, events, size, created_ms, STATE_PENDING, payload),
            )
            self._bytes += size
            return int(cursor.lastrowid or 0)

    def ack(self, ids: Iterable[int]) -> int:
        """Delete acknowledged rows; returns how many were removed."""
        wanted = [int(row_id) for row_id in ids]
        if not wanted:
            return 0
        removed = 0
        freed = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for row_id in wanted:
                    row = self._conn.execute(
                        "SELECT size FROM batches WHERE id = ?", (row_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    self._conn.execute("DELETE FROM batches WHERE id = ?", (row_id,))
                    freed += int(row[0])
                    removed += 1
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            self._bytes -= freed  # only once the delete is committed: the cap stays exact
        return removed

    def park(self, row_id: int) -> bool:
        """Take a row out of the pending queue (core refused it for good); it keeps its bytes."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE batches SET state = ? WHERE id = ? AND state = ?",
                (STATE_PARKED, row_id, STATE_PENDING),
            )
            return cursor.rowcount > 0

    def purge_parked(self, older_than: datetime) -> tuple[int, str | None]:
        """Delete the parked rows created before ``older_than`` (ADR 0073); returns how many
        and the batch id (a ULID, never content) of the oldest one removed. Pending rows are
        never touched. The bytes are released once the delete is committed, as in :meth:`ack`."""
        cutoff_ms = int(older_than.astimezone(UTC).timestamp() * 1000)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                rows = self._conn.execute(
                    "SELECT batch_id, size FROM batches WHERE state = ? AND created_at_ms < ? "
                    "ORDER BY created_at_ms, id",
                    (STATE_PARKED, cutoff_ms),
                ).fetchall()
                if rows:
                    self._conn.execute(
                        "DELETE FROM batches WHERE state = ? AND created_at_ms < ?",
                        (STATE_PARKED, cutoff_ms),
                    )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            self._bytes -= sum(int(row[1]) for row in rows)
        return len(rows), (str(rows[0][0]) if rows else None)

    # -- reads ---------------------------------------------------------------------------------

    def _rows(self, limit: int) -> list[BufferedBatch]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source_id, batch_id, events, size, created_at_ms, payload "
                "FROM batches WHERE state = ? ORDER BY id LIMIT ?",
                (STATE_PENDING, max(1, limit)),
            ).fetchall()
        return [
            BufferedBatch(
                id=int(row[0]),
                source_id=str(row[1]),
                batch_id=str(row[2]),
                events=int(row[3]),
                size_bytes=int(row[4]),
                created_at=datetime.fromtimestamp(int(row[5]) / 1000, tz=UTC),
                payload=bytes(row[6]),
            )
            for row in rows
        ]

    def oldest(self) -> BufferedBatch | None:
        rows = self._rows(1)
        return rows[0] if rows else None

    def iter_pending(self, limit: int = 100) -> list[BufferedBatch]:
        """Up to ``limit`` pending batches, oldest first."""
        return self._rows(limit)

    def depth(self) -> int:
        """Pending batches (spec 8.5 "buffer depth")."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM batches WHERE state = ?", (STATE_PENDING,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def pending_events(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(events), 0) FROM batches WHERE state = ?", (STATE_PENDING,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def parked(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM batches WHERE state = ?", (STATE_PARKED,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def bytes(self) -> int:
        """Bytes stored, pending and parked (what counts toward the cap)."""
        with self._lock:
            return self._bytes

    def oldest_at(self) -> datetime | None:
        """When the oldest pending batch was created (spec 8.5 "oldest event age")."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(created_at_ms) FROM batches WHERE state = ?", (STATE_PENDING,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)

    def is_above_backpressure(self) -> bool:
        with self._lock:
            return self._bytes >= self._max_bytes * self._backpressure_ratio

    def is_full(self) -> bool:
        with self._lock:
            return self._bytes >= self._max_bytes

    def close(self) -> None:
        with self._lock:
            self._conn.close()
