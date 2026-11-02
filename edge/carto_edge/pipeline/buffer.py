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
by :meth:`DiskBuffer.park`, which stamps when and (as a short label, the status) why, so the
queue keeps moving; parked rows still count toward the cap and appear in the metrics until an
operator clears them or the forwarder's housekeeping deletes those parked longer ago than
``core.parked_max_age_days`` with :meth:`DiskBuffer.purge_parked` (ADR 0073), so a persistent
refusal cannot fill the buffer with rows nobody will send. A purge is a data-loss event, so each
purged batch leaves a row in the ``purged`` ledger (batch id, event count, parked reason, when;
never a payload), kept to the last :data:`PURGED_LEDGER_ROWS` rows and read back with
:meth:`DiskBuffer.purged_ledger`.

The schema is versioned with ``PRAGMA user_version`` (:data:`SCHEMA_VERSION`): a file at an
older version is migrated on open, one version per transaction; a file from before versioning
is version 1; a file a newer edge wrote is refused with :class:`BufferSchemaError` rather than
read with the wrong columns.

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

__all__ = [
    "MAX_PARKED_REASON_LEN",
    "PURGED_LEDGER_ROWS",
    "SCHEMA_VERSION",
    "BufferFullError",
    "BufferSchemaError",
    "BufferedBatch",
    "DiskBuffer",
    "PurgedBatch",
    "compress_batch",
    "decompress_batch",
]

ZSTD_LEVEL: Final = 3
STATE_PENDING: Final = 0
STATE_PARKED: Final = 1
MAX_DECOMPRESSED_BYTES: Final = 64 * 1024 * 1024
"""A stored batch is at most 5 MB of JSON before compression (spec 8.5); 64 MB is a safe ceiling
for :func:`decompress_batch` so a corrupt row cannot expand without bound (spec 2.3 invariant 8)."""
SCHEMA_VERSION: Final = 2
"""``PRAGMA user_version`` of a buffer file this code writes (module docstring)."""
PURGED_LEDGER_ROWS: Final = 10_000
"""Rows kept in the ``purged`` ledger: the most recent purges (ADR 0073)."""
MAX_PARKED_REASON_LEN: Final = 32
"""A parked reason is a label such as ``http_422``, cut to this length; never content."""

_MIGRATIONS: Final[dict[int, tuple[str, ...]]] = {
    1: (
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
    ),
    2: (
        # ADR 0073: parked rows are aged from when they were parked (NULL for rows parked
        # before this version: those fall back to created_at_ms), and the purged ledger.
        "ALTER TABLE batches ADD COLUMN parked_at_ms INTEGER",
        "ALTER TABLE batches ADD COLUMN parked_reason TEXT",
        (
            "CREATE TABLE IF NOT EXISTS purged ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "batch_id TEXT NOT NULL, "
            "events INTEGER NOT NULL, "
            "parked_reason TEXT, "
            "purged_at_ms INTEGER NOT NULL)"
        ),
    ),
}
"""The statements that take a file from version ``n - 1`` to ``n``, applied in order."""


class BufferFullError(Exception):
    """The buffer holds ``max_bytes``; the caller must slow down or refuse (spec 8.5)."""


class BufferSchemaError(Exception):
    """The buffer file was written by a newer edge than this one (module docstring)."""


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


@dataclass(frozen=True, slots=True)
class PurgedBatch:
    """One row of the ``purged`` ledger (ADR 0073): what was lost, when and why; no payload."""

    batch_id: str
    events: int
    parked_reason: str | None
    purged_at: datetime


def _ms(at: datetime) -> int:
    return int(at.astimezone(UTC).timestamp() * 1000)


def _from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the file to :data:`SCHEMA_VERSION` (module docstring)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row is not None else 0
    if version == 0:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batches'"
        ).fetchone()
        if present is not None:
            version = 1  # a file from before the schema was versioned
    if version > SCHEMA_VERSION:
        msg = (
            f"buffer schema version {version} is newer than this edge supports "
            f"({SCHEMA_VERSION}); the file was written by a newer edge"
        )
        raise BufferSchemaError(msg)
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN")
        try:
            for statement in _MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target:d}")
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise


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
        try:
            _migrate(self._conn)
        except BaseException:
            self._conn.close()
            raise
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

    def schema_version(self) -> int:
        """The file's ``PRAGMA user_version`` (:data:`SCHEMA_VERSION` once opened)."""
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

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
        created_ms = _ms(created_at)
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

    def park(self, row_id: int, *, at: datetime | None = None, reason: str | None = None) -> bool:
        """Take a row out of the pending queue (core refused it for good); it keeps its bytes.
        ``at`` (default: now) is when it was parked, which :meth:`purge_parked` ages it by;
        ``reason`` is a short label (the status, never content) kept for the purged ledger."""
        parked_ms = _ms(at if at is not None else datetime.now(UTC))
        label = reason[:MAX_PARKED_REASON_LEN] if reason is not None else None
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE batches SET state = ?, parked_at_ms = ?, parked_reason = ? "
                "WHERE id = ? AND state = ?",
                (STATE_PARKED, parked_ms, label, row_id, STATE_PENDING),
            )
            return cursor.rowcount > 0

    def purge_parked(
        self, older_than: datetime, *, now: datetime | None = None
    ) -> tuple[int, str | None]:
        """Delete the rows parked before ``older_than`` (ADR 0073); returns how many and the
        batch id (a ULID, never content) of the one parked longest ago. A row parked before
        the schema recorded the time is aged from its append time. Pending rows are never
        touched. Each deleted row is written to the ``purged`` ledger, stamped ``now``, in the
        same transaction, and the ledger is cut to :data:`PURGED_LEDGER_ROWS`. The bytes are
        released once the delete is committed, as in :meth:`ack`."""
        cutoff_ms = _ms(older_than)
        purged_ms = _ms(now if now is not None else datetime.now(UTC))
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                rows = self._conn.execute(
                    "SELECT id, batch_id, events, size, parked_reason FROM batches "
                    "WHERE state = ? AND COALESCE(parked_at_ms, created_at_ms) < ? "
                    "ORDER BY COALESCE(parked_at_ms, created_at_ms), id",
                    (STATE_PARKED, cutoff_ms),
                ).fetchall()
                if rows:
                    self._conn.executemany(
                        "DELETE FROM batches WHERE id = ?", [(int(row[0]),) for row in rows]
                    )
                    self._conn.executemany(
                        "INSERT INTO purged (batch_id, events, parked_reason, purged_at_ms) "
                        "VALUES (?, ?, ?, ?)",
                        [(row[1], int(row[2]), row[4], purged_ms) for row in rows],
                    )
                    self._conn.execute(
                        "DELETE FROM purged WHERE id NOT IN "
                        "(SELECT id FROM purged ORDER BY id DESC LIMIT ?)",
                        (PURGED_LEDGER_ROWS,),
                    )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            self._bytes -= sum(int(row[3]) for row in rows)
        return len(rows), (str(rows[0][1]) if rows else None)

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
                created_at=_from_ms(row[5]),
                payload=bytes(row[6]),
            )
            for row in rows
        ]

    def purged_ledger(self, limit: int = 100) -> list[PurgedBatch]:
        """The most recent purges, newest first (ADR 0073); at most ``limit`` rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id, events, parked_reason, purged_at_ms FROM purged "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), PURGED_LEDGER_ROWS)),),
            ).fetchall()
        return [
            PurgedBatch(
                batch_id=str(row[0]),
                events=int(row[1]),
                parked_reason=str(row[2]) if row[2] is not None else None,
                purged_at=_from_ms(row[3]),
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
        return _from_ms(row[0])

    def is_above_backpressure(self) -> bool:
        with self._lock:
            return self._bytes >= self._max_bytes * self._backpressure_ratio

    def is_full(self) -> bool:
        with self._lock:
            return self._bytes >= self._max_bytes

    def close(self) -> None:
        with self._lock:
            self._conn.close()
