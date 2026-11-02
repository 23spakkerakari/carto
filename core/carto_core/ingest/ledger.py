"""Batch ledger: ingest idempotency by batch id (ADR 0015 as amended by ADR 0034, spec 8.5).

``ingest-api`` and the bundle loader keep ``ingest_batches (tenant_id, batch_id)`` in PostgreSQL.
A delivery claims its batch before writing it:

- :meth:`BatchLedger.claim` inserts a ``pending`` row. A new row, or a ``pending`` row whose
  claim is older than the claim timeout (its writer is presumed dead), is the caller's to
  write: :attr:`ClaimState.CLAIMED`. A ``done`` row is :attr:`ClaimState.DONE`, a duplicate to
  acknowledge without writing. A fresh ``pending`` row is :attr:`ClaimState.BUSY`: another
  delivery is writing it, so the caller answers 503 and the edge retries later.
- :meth:`BatchLedger.complete` marks the batch ``done`` after the write, whoever holds the claim
  by then: the rows are in ClickHouse either way.
- :meth:`BatchLedger.release` deletes the caller's own ``pending`` row after a failed write, so
  the retry is taken at once instead of waiting out the claim timeout.
- :meth:`BatchLedger.prune` deletes rows received before a cutoff (events retention plus one
  day); :class:`LedgerPruner` runs it in the background of ``ingest-api``.

A write that succeeds but is never marked ``done`` (the ledger failed, the process died) is
written again by the next claim. ``events`` and ``event_identifiers`` are
``ReplacingMergeTree`` tables keyed by the event, so the second copy merges away (ADR 0034).

Every statement is parameterized (spec 14.7). Errors raise :class:`LedgerError` naming the
driver error class only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_common.logging import get_logger
from carto_core.db.postgres import ping_engine

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "PRUNE_BATCH_ROWS",
    "BatchLedger",
    "Claim",
    "ClaimState",
    "InMemoryBatchLedger",
    "LedgerEntry",
    "LedgerError",
    "LedgerPruner",
    "LedgerRow",
    "PostgresBatchLedger",
]

PRUNE_BATCH_ROWS: Final = 10_000
"""Rows one pruning statement deletes, so a large backlog never holds one long lock."""

log = get_logger(component="ledger")


class LedgerError(Exception):
    """The ledger could not be read or written; the caller answers 503 so the edge retries."""


class ClaimState(StrEnum):
    """What :meth:`BatchLedger.claim` found."""

    CLAIMED = "claimed"
    """The caller holds the claim and writes the batch (a new row, or a stale claim taken over)."""
    DONE = "done"
    """The batch was written before: acknowledge it as a duplicate."""
    BUSY = "busy"
    """Another delivery holds a fresh claim: answer 503 so the edge retries later."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One batch delivery: who sent it, how many events, when core received it."""

    tenant_id: str
    batch_id: str
    source_id: str
    event_count: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class Claim:
    """The outcome of a claim. ``claimed_at`` identifies the caller's own claim for
    :meth:`BatchLedger.release`; ``took_over`` is set when a stale claim was taken."""

    state: ClaimState
    claimed_at: datetime
    took_over: bool = False


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One row of the in-memory ledger, as ``ingest_batches`` holds it."""

    entry: LedgerEntry
    state: str
    claimed_at: datetime


@runtime_checkable
class BatchLedger(Protocol):
    """What the ingest path and the bundle loader need from the ledger."""

    def claim(self, entry: LedgerEntry, stale_before: datetime) -> Claim:
        """Claim the batch at ``entry.received_at``; a ``pending`` claim older than
        ``stale_before`` is taken over."""
        ...

    def complete(self, entry: LedgerEntry, done_at: datetime) -> None:
        """Mark the batch ``done`` (inserting the row when it is missing)."""
        ...

    def release(self, tenant_id: str, batch_id: str, claimed_at: datetime) -> None:
        """Delete the caller's own ``pending`` claim; another claim or a ``done`` row stays."""
        ...

    def prune(self, received_before: datetime) -> int:
        """Delete the rows received before the cutoff; return how many."""
        ...

    def ping(self) -> bool: ...


class InMemoryBatchLedger:
    """A ledger in a dict, for tests and the offline smoke path."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], LedgerRow] = {}
        self._lock = threading.Lock()

    @property
    def rows(self) -> tuple[LedgerRow, ...]:
        with self._lock:
            return tuple(self._rows.values())

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        """The entries of the ``done`` rows."""
        return tuple(row.entry for row in self.rows if row.state == "done")

    def state_of(self, tenant_id: str, batch_id: str) -> str | None:
        with self._lock:
            row = self._rows.get((tenant_id, batch_id))
        return row.state if row is not None else None

    def claim(self, entry: LedgerEntry, stale_before: datetime) -> Claim:
        key = (entry.tenant_id, entry.batch_id)
        now = entry.received_at
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                self._rows[key] = LedgerRow(entry, "pending", now)
                return Claim(ClaimState.CLAIMED, now)
            if row.state == "done":
                return Claim(ClaimState.DONE, row.claimed_at)
            if row.claimed_at < stale_before:
                self._rows[key] = replace(row, claimed_at=now)
                return Claim(ClaimState.CLAIMED, now, took_over=True)
            return Claim(ClaimState.BUSY, row.claimed_at)

    def complete(self, entry: LedgerEntry, done_at: datetime) -> None:
        key = (entry.tenant_id, entry.batch_id)
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                self._rows[key] = LedgerRow(entry, "done", done_at)
            else:
                self._rows[key] = replace(row, state="done")

    def release(self, tenant_id: str, batch_id: str, claimed_at: datetime) -> None:
        key = (tenant_id, batch_id)
        with self._lock:
            row = self._rows.get(key)
            if row is not None and row.state == "pending" and row.claimed_at == claimed_at:
                del self._rows[key]

    def prune(self, received_before: datetime) -> int:
        with self._lock:
            old = [
                key for key, row in self._rows.items() if row.entry.received_at < received_before
            ]
            for key in old:
                del self._rows[key]
        return len(old)

    def ping(self) -> bool:
        return True


_CLAIM = sqlalchemy.text(
    "INSERT INTO ingest_batches "
    "(tenant_id, batch_id, source_id, received_at, event_count, state, claimed_at, "
    "created_at, updated_at) "
    "VALUES (:tenant_id, :batch_id, :source_id, :received_at, :event_count, 'pending', "
    ":received_at, :received_at, :received_at) "
    "ON CONFLICT (tenant_id, batch_id) DO UPDATE "
    "SET claimed_at = EXCLUDED.claimed_at, updated_at = EXCLUDED.updated_at "
    "WHERE ingest_batches.state = 'pending' AND ingest_batches.claimed_at < :stale_before "
    "RETURNING (xmax = 0) AS inserted"
)
"""Insert a claim, or take over a stale one. Concurrent takeovers serialize on the row lock and
the ``WHERE`` is checked again after it, so only one of them returns a row."""
_STATE = sqlalchemy.text(
    "SELECT state, claimed_at FROM ingest_batches "
    "WHERE tenant_id = :tenant_id AND batch_id = :batch_id"
)
_COMPLETE = sqlalchemy.text(
    "INSERT INTO ingest_batches "
    "(tenant_id, batch_id, source_id, received_at, event_count, state, claimed_at, "
    "created_at, updated_at) "
    "VALUES (:tenant_id, :batch_id, :source_id, :received_at, :event_count, 'done', "
    ":done_at, :done_at, :done_at) "
    "ON CONFLICT (tenant_id, batch_id) DO UPDATE "
    "SET state = 'done', updated_at = EXCLUDED.updated_at"
)
_RELEASE = sqlalchemy.text(
    "DELETE FROM ingest_batches "
    "WHERE tenant_id = :tenant_id AND batch_id = :batch_id "
    "AND state = 'pending' AND claimed_at = :claimed_at"
)
_PRUNE = sqlalchemy.text(
    "DELETE FROM ingest_batches WHERE ctid IN ("
    "SELECT ctid FROM ingest_batches WHERE received_at < :before LIMIT :limit)"
)


def _entry_parameters(entry: LedgerEntry) -> dict[str, object]:
    return {
        "tenant_id": entry.tenant_id,
        "batch_id": entry.batch_id,
        "source_id": entry.source_id,
        "received_at": entry.received_at,
        "event_count": entry.event_count,
    }


class PostgresBatchLedger:
    """The ``ingest_batches`` table (migrations 0001 and 0003)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(self, entry: LedgerEntry, stale_before: datetime) -> Claim:
        parameters = _entry_parameters(entry) | {"stale_before": stale_before}
        try:
            with self._engine.begin() as connection:
                inserted = connection.execute(_CLAIM, parameters).scalar_one_or_none()
                if inserted is not None:
                    return Claim(ClaimState.CLAIMED, entry.received_at, took_over=not inserted)
                row = connection.execute(
                    _STATE, {"tenant_id": entry.tenant_id, "batch_id": entry.batch_id}
                ).first()
        except SQLAlchemyError as exc:
            msg = f"ledger claim failed: {type(exc).__name__}"
            raise LedgerError(msg) from exc
        if row is not None and row.state == "done":
            return Claim(ClaimState.DONE, row.claimed_at)
        # A fresh claim, or a row released or pruned between the two statements: retry later.
        return Claim(ClaimState.BUSY, row.claimed_at if row is not None else entry.received_at)

    def complete(self, entry: LedgerEntry, done_at: datetime) -> None:
        self._run(_COMPLETE, _entry_parameters(entry) | {"done_at": done_at}, "complete")

    def release(self, tenant_id: str, batch_id: str, claimed_at: datetime) -> None:
        parameters = {"tenant_id": tenant_id, "batch_id": batch_id, "claimed_at": claimed_at}
        self._run(_RELEASE, parameters, "release")

    def prune(self, received_before: datetime) -> int:
        deleted = 0
        while True:
            count = self._run(
                _PRUNE, {"before": received_before, "limit": PRUNE_BATCH_ROWS}, "prune"
            )
            deleted += count
            if count < PRUNE_BATCH_ROWS:
                return deleted

    def ping(self) -> bool:
        return ping_engine(self._engine)

    def _run(
        self, statement: sqlalchemy.TextClause, parameters: dict[str, object], what: str
    ) -> int:
        try:
            with self._engine.begin() as connection:
                return int(connection.execute(statement, parameters).rowcount)
        except SQLAlchemyError as exc:
            msg = f"ledger {what} failed: {type(exc).__name__}"
            raise LedgerError(msg) from exc


class LedgerPruner:
    """Prunes the ledger every ``interval_seconds`` on a daemon thread (ADR 0034 item 4).

    Rows received before ``now - events retention - 1 day`` go: their events are past the
    ClickHouse TTL, so a late duplicate would be deleted on arrival anyway. A failed run is
    logged and tried again at the next interval.
    """

    def __init__(
        self,
        ledger: BatchLedger,
        retention_days: int,
        clock: Callable[[], datetime],
        *,
        interval_seconds: float = 3600.0,
    ) -> None:
        self._ledger = ledger
        self._keep = timedelta(days=retention_days + 1)
        self._clock = clock
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        """One pruning pass; returns the rows deleted, 0 when the ledger failed."""
        try:
            deleted = self._ledger.prune(self._clock() - self._keep)
        except LedgerError as exc:
            log.warning("ledger pruning failed", error=type(exc).__name__)
            return 0
        if deleted:
            log.info("ledger pruned", rows=deleted)
        return deleted

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ledger-pruner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self._interval)
