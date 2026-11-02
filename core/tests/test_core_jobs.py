"""The job queue contract (spec 5.3, 7.3 ``jobs``) on the in-memory implementation: dedupe on
(tenant, kind, payload) for unleased rows, leases take the oldest runnable job once (two workers
never share one), expired leases are retaken, ``fail`` releases for a retry or deletes, and the
error column never carries more than a bounded class name. The PostgreSQL implementation runs
the same cases in ``test_core_m2_integration.py``; its SQL is checked here for shape."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from carto_core import jobs_postgres
from carto_core.jobs import MAX_ERROR_LEN, InMemoryJobQueue, Job, JobKind, JobQueue

NOW = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
KINDS = frozenset({JobKind.PROFILE, JobKind.LINK})


def test_in_memory_queue_satisfies_the_protocol() -> None:
    queue = InMemoryJobQueue()
    assert isinstance(queue, JobQueue)
    assert queue.ping() is True
    assert queue.pending("default") == []


def test_enqueue_dedupes_unleased_jobs_of_the_same_tenant_kind_and_payload() -> None:
    queue = InMemoryJobQueue()
    first = queue.enqueue("default", JobKind.PROFILE, {}, NOW)
    assert isinstance(first, Job) and first.id == 1 and first.attempts == 0
    assert queue.enqueue("default", JobKind.PROFILE, {}, NOW + timedelta(hours=1)) is None
    assert queue.enqueue("default", JobKind.PROFILE, {"full": True}, NOW) is not None
    assert queue.enqueue("default", JobKind.LINK, {}, NOW) is not None
    assert queue.enqueue("other", JobKind.PROFILE, {}, NOW) is not None
    forced = queue.enqueue("default", JobKind.PROFILE, {}, NOW, dedupe=False)
    assert forced is not None and forced.id == 5
    assert len(queue.pending("default")) == 4
    assert len(queue.pending("other")) == 1


def test_lease_takes_the_oldest_runnable_job_and_marks_it() -> None:
    queue = InMemoryJobQueue()
    later = queue.enqueue("default", JobKind.LINK, {}, NOW + timedelta(minutes=5))
    older = queue.enqueue("default", JobKind.PROFILE, {}, NOW - timedelta(minutes=5))
    future = queue.enqueue("default", JobKind.PROFILE, {"full": True}, NOW + timedelta(days=1))
    assert later and older and future
    leased = queue.lease(KINDS, NOW, 600)
    assert leased is not None
    assert leased.id == older.id
    assert leased.attempts == 1
    assert leased.leased_until == NOW + timedelta(seconds=600)
    assert queue.lease(KINDS, NOW, 600) is None, "the next job is not runnable yet"
    assert queue.lease(frozenset({JobKind.FAMILIES}), NOW, 600) is None
    second = queue.lease(KINDS, NOW + timedelta(minutes=5), 600)
    assert second is not None and second.id == later.id
    assert queue.lease(KINDS, NOW + timedelta(minutes=5), 600) is None, "both are leased"


def test_an_expired_lease_is_retaken_and_attempts_keep_counting() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", JobKind.PROFILE, {}, NOW)
    first = queue.lease(KINDS, NOW, 60)
    assert first is not None and first.attempts == 1
    assert queue.lease(KINDS, NOW + timedelta(seconds=59), 60) is None
    again = queue.lease(KINDS, NOW + timedelta(seconds=60), 60)
    assert again is not None and again.id == first.id and again.attempts == 2


def test_dedupe_ignores_a_leased_job_so_the_next_run_can_be_queued() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", JobKind.PROFILE, {}, NOW)
    leased = queue.lease(KINDS, NOW, 600)
    assert leased is not None
    following = queue.enqueue("default", JobKind.PROFILE, {}, NOW + timedelta(minutes=5))
    assert following is not None and following.id != leased.id
    assert queue.enqueue("default", JobKind.PROFILE, {}, NOW + timedelta(minutes=9)) is None


def test_complete_deletes_and_fail_releases_or_deletes() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", JobKind.PROFILE, {}, NOW)
    queue.enqueue("default", JobKind.LINK, {}, NOW)
    profile = queue.lease(frozenset({JobKind.PROFILE}), NOW, 600)
    link = queue.lease(frozenset({JobKind.LINK}), NOW, 600)
    assert profile is not None and link is not None
    queue.complete(profile.id)
    assert [job.kind for job in queue.pending("default")] == [JobKind.LINK]
    retry_at = NOW + timedelta(minutes=1)
    queue.fail(link.id, "ValueError", retry_at)
    (released,) = queue.pending("default")
    assert released.leased_until is None
    assert released.run_after == retry_at
    assert released.last_error == "ValueError"
    assert released.attempts == 1
    assert queue.lease(KINDS, NOW, 600) is None
    retaken = queue.lease(KINDS, retry_at, 600)
    assert retaken is not None and retaken.attempts == 2
    queue.fail(retaken.id, "ValueError", None)
    assert queue.pending("default") == []
    queue.complete(999)
    queue.fail(999, "ValueError", None)


def test_the_error_column_is_bounded() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", JobKind.PROFILE, {}, NOW)
    job = queue.lease(KINDS, NOW, 600)
    assert job is not None
    queue.fail(job.id, "X" * 1000, NOW)
    (row,) = queue.pending("default")
    assert row.last_error is not None and len(row.last_error) == MAX_ERROR_LEN


def test_two_workers_leasing_at_once_never_share_a_job() -> None:
    queue = InMemoryJobQueue()
    for index in range(40):
        queue.enqueue("default", JobKind.PROFILE, {"n": index}, NOW)

    def take(_: int) -> list[int]:
        taken: list[int] = []
        while (job := queue.lease(KINDS, NOW, 600)) is not None:
            taken.append(job.id)
        return taken

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(take, range(4)))
    ids = [job_id for taken in results for job_id in taken]
    assert len(ids) == 40
    assert len(set(ids)) == 40


def test_pending_is_ordered_by_run_after_then_id() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", JobKind.LINK, {}, NOW + timedelta(minutes=1))
    queue.enqueue("default", JobKind.PROFILE, {}, NOW)
    queue.enqueue("default", JobKind.PROFILE, {"full": True}, NOW)
    assert [(job.kind, job.id) for job in queue.pending("default")] == [
        (JobKind.PROFILE, 2),
        (JobKind.PROFILE, 3),
        (JobKind.LINK, 1),
    ]


def test_postgres_statements_lease_with_skip_locked_and_bind_every_value() -> None:
    """Spec 5.3 and 14.7 by inspection; the container test runs them."""
    pick = str(jobs_postgres._PICK)
    assert "FOR UPDATE SKIP LOCKED" in pick
    assert "ORDER BY run_after, id LIMIT 1" in pick
    assert "kind = ANY(:kinds)" in pick and ":now" in pick
    assert "attempts = attempts + 1" in str(jobs_postgres._LEASE)
    exists = str(jobs_postgres._EXISTS)
    assert "leased_until IS NULL" in exists and "CAST(:payload AS jsonb)" in exists
    assert "pg_advisory_xact_lock" in str(jobs_postgres._DEDUPE_LOCK)
    for statement in (
        jobs_postgres._INSERT,
        jobs_postgres._PICK,
        jobs_postgres._LEASE,
        jobs_postgres._DELETE,
        jobs_postgres._RETRY,
        jobs_postgres._PENDING,
    ):
        assert "'" not in str(statement), str(statement)
