"""The batch ledger's claim protocol (ADR 0034): a claim is new, taken over when stale, busy
while fresh, or a duplicate once done; a release removes only the caller's own claim; pruning
deletes rows past retention and the pruner survives a failing ledger. The PostgreSQL ledger
runs the same cases in ``test_core_integration.py`` (Docker only)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from carto_core.ingest.ledger import (
    BatchLedger,
    ClaimState,
    InMemoryBatchLedger,
    LedgerEntry,
    LedgerError,
    LedgerPruner,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TIMEOUT = timedelta(seconds=120)


def _entry(at: datetime, batch_id: str = "01K7A0000000000000000000B1") -> LedgerEntry:
    return LedgerEntry("default", batch_id, "src_wms_db", 3, at)


def test_the_in_memory_ledger_is_a_batch_ledger() -> None:
    assert isinstance(InMemoryBatchLedger(), BatchLedger)


def test_a_new_batch_is_claimed_pending() -> None:
    ledger = InMemoryBatchLedger()
    claim = ledger.claim(_entry(T0), T0 - TIMEOUT)
    assert claim.state is ClaimState.CLAIMED
    assert claim.claimed_at == T0
    assert not claim.took_over
    assert ledger.state_of("default", _entry(T0).batch_id) == "pending"
    assert ledger.entries == (), "only done rows are entries"


def test_a_fresh_claim_is_busy_and_a_stale_one_is_taken_over() -> None:
    ledger = InMemoryBatchLedger()
    ledger.claim(_entry(T0), T0 - TIMEOUT)
    soon = T0 + timedelta(seconds=119)
    assert ledger.claim(_entry(soon), soon - TIMEOUT).state is ClaimState.BUSY
    later = T0 + timedelta(seconds=121)
    taken = ledger.claim(_entry(later), later - TIMEOUT)
    assert taken.state is ClaimState.CLAIMED
    assert taken.took_over
    assert taken.claimed_at == later
    (row,) = ledger.rows
    assert row.claimed_at == later
    assert row.entry.received_at == T0, "the first receipt is kept"


def test_a_done_batch_is_a_duplicate_however_old() -> None:
    ledger = InMemoryBatchLedger()
    ledger.claim(_entry(T0), T0 - TIMEOUT)
    ledger.complete(_entry(T0), T0 + timedelta(seconds=1))
    much_later = T0 + timedelta(days=3)
    assert ledger.claim(_entry(much_later), much_later - TIMEOUT).state is ClaimState.DONE
    assert ledger.entries == (_entry(T0),)


def test_complete_marks_done_even_when_the_claim_was_released_or_taken() -> None:
    """The rows are in ClickHouse whoever holds the claim, so the batch is done."""
    ledger = InMemoryBatchLedger()
    ledger.complete(_entry(T0), T0)
    assert ledger.state_of("default", _entry(T0).batch_id) == "done"


def test_release_removes_only_the_callers_own_pending_claim() -> None:
    ledger = InMemoryBatchLedger()
    batch_id = _entry(T0).batch_id
    first = ledger.claim(_entry(T0), T0 - TIMEOUT)
    later = T0 + timedelta(minutes=5)
    ledger.claim(_entry(later), later - TIMEOUT)  # takes the stale claim over
    ledger.release("default", batch_id, first.claimed_at)
    assert ledger.state_of("default", batch_id) == "pending", "the newer claim stays"
    ledger.release("default", batch_id, later)
    assert ledger.state_of("default", batch_id) is None
    ledger.complete(_entry(T0), T0)
    ledger.release("default", batch_id, T0)
    assert ledger.state_of("default", batch_id) == "done", "a done row is never released"


def test_prune_deletes_rows_received_before_the_cutoff() -> None:
    ledger = InMemoryBatchLedger()
    old, new = _entry(T0 - timedelta(days=40), "OLD"), _entry(T0, "NEW")
    ledger.complete(old, old.received_at)
    ledger.claim(new, T0 - TIMEOUT)
    assert ledger.prune(T0 - timedelta(days=31)) == 1
    assert [row.entry.batch_id for row in ledger.rows] == ["NEW"]


def test_the_pruner_keeps_retention_plus_one_day() -> None:
    ledger = InMemoryBatchLedger()
    keep = _entry(T0 - timedelta(days=30, hours=23), "KEEP")
    drop = _entry(T0 - timedelta(days=31, seconds=1), "DROP")
    for entry in (keep, drop):
        ledger.complete(entry, entry.received_at)
    assert LedgerPruner(ledger, 30, lambda: T0).run_once() == 1
    assert [row.entry.batch_id for row in ledger.rows] == ["KEEP"]


class FailingPrune(InMemoryBatchLedger):
    def __init__(self) -> None:
        super().__init__()
        self.calls = threading.Semaphore(0)

    def prune(self, received_before: datetime) -> int:
        self.calls.release()
        raise LedgerError("ledger prune failed: OperationalError")


def test_a_failed_pruning_run_is_logged_and_retried_on_the_next_interval() -> None:
    ledger = FailingPrune()
    pruner = LedgerPruner(ledger, 30, lambda: T0, interval_seconds=0.01)
    assert pruner.run_once() == 0
    pruner.start()
    try:
        for _ in range(3):
            assert ledger.calls.acquire(timeout=5), "the loop keeps running after failures"
    finally:
        pruner.stop()


@pytest.mark.parametrize("starts", [1, 2])
def test_the_pruner_starts_once_and_stops(starts: int) -> None:
    pruner = LedgerPruner(InMemoryBatchLedger(), 30, lambda: T0, interval_seconds=3600)
    for _ in range(starts):
        pruner.start()
    pruner.stop(timeout=5)
    assert pruner._thread is not None and not pruner._thread.is_alive()
