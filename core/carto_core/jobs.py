"""Worker jobs: PostgreSQL leases, no broker (spec 5.3, 7.3 ``jobs``).

A :class:`Job` row is leased with ``SELECT ... FOR UPDATE SKIP LOCKED`` by ``carto-core worker``
(:mod:`carto_core.worker`); a lease older than the worker's ``lease_seconds`` is retaken, so a
crashed worker never strands a job. Recurring jobs (``profile`` every five minutes, ``link``
after each profile pass and nightly in full) are re-queued by the worker when they complete
(``run_after`` set to the next slot), so the table always holds at most one pending job per
recurring kind and payload.

This module holds the model, the Protocol and :class:`InMemoryJobQueue`, the reference
implementation the worker's tests run against; :class:`carto_core.jobs_postgres.PostgresJobQueue`
is the one a service uses. Semantics both share: a job is runnable when ``run_after <= now`` and
it is not leased or its lease expired; a lease takes the oldest runnable job by ``(run_after,
id)`` and increments ``attempts``; ``enqueue`` with ``dedupe`` adds nothing while an unleased job
of the same tenant, kind and payload waits; ``fail`` releases the lease for a retry at
``retry_at`` or deletes the job when the attempts are exhausted. ``last_error`` holds an error
class name, bounded by :data:`MAX_ERROR_LEN`, never a message that could carry data.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

__all__ = ["MAX_ERROR_LEN", "InMemoryJobQueue", "Job", "JobKind", "JobQueue", "JobQueueError"]

MAX_ERROR_LEN: Final = 256
"""``jobs.last_error`` width (migration 0004): an error class name, never a message with data."""
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class JobQueueError(Exception):
    """The job table could not be read or written."""


class JobKind(StrEnum):
    PROFILE = "profile"
    """Incremental profile pass by store watermark; ``payload['full']`` for the nightly rebuild."""
    LINK = "link"
    """Link pass over the current sketches; ``payload['full']`` for the nightly full pass."""
    FAMILIES = "families"
    """Rebuild entity families after a review decision."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    tenant_id: str
    kind: JobKind
    payload: dict[str, Any] = field(default_factory=dict)
    run_after: datetime | None = None
    leased_until: datetime | None = None
    attempts: int = 0
    last_error: str | None = None


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(
        self,
        tenant_id: str,
        kind: JobKind,
        payload: dict[str, Any],
        run_after: datetime,
        *,
        dedupe: bool = True,
    ) -> Job | None:
        """Add a job; with ``dedupe`` do nothing when an unleased job of the same tenant, kind
        and payload is already waiting (return ``None``)."""
        ...

    def lease(self, kinds: frozenset[JobKind], now: datetime, lease_seconds: int) -> Job | None:
        """Take the oldest runnable job of the kinds (``run_after <= now`` and not leased, or
        its lease expired), mark it leased until ``now + lease_seconds``, increment attempts."""
        ...

    def complete(self, job_id: int) -> None:
        """Delete the job."""
        ...

    def fail(self, job_id: int, error: str, retry_at: datetime | None) -> None:
        """Release the lease; schedule a retry at ``retry_at`` or, when ``None``, delete the job
        (attempts exhausted). ``error`` is a bounded class name, never a message with data."""
        ...

    def pending(self, tenant_id: str) -> list[Job]:
        """Every job of the tenant, for the worker's status output and tests."""
        ...

    def ping(self) -> bool: ...


class InMemoryJobQueue:
    """The reference :class:`JobQueue` for tests and the worker's unit tests: a dict under one
    lock, so two threads leasing at once take different jobs, as ``SKIP LOCKED`` gives two
    workers on PostgreSQL (:class:`carto_core.jobs_postgres.PostgresJobQueue`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[int, Job] = {}
        self._next_id = 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def enqueue(
        self,
        tenant_id: str,
        kind: JobKind,
        payload: dict[str, Any],
        run_after: datetime,
        *,
        dedupe: bool = True,
    ) -> Job | None:
        with self._lock:
            if dedupe and any(
                job.tenant_id == tenant_id
                and job.kind is kind
                and job.payload == payload
                and job.leased_until is None
                for job in self._rows.values()
            ):
                return None
            job = Job(
                id=self._next_id,
                tenant_id=tenant_id,
                kind=kind,
                payload=dict(payload),
                run_after=run_after,
            )
            self._next_id += 1
            self._rows[job.id] = job
            return job

    def lease(self, kinds: frozenset[JobKind], now: datetime, lease_seconds: int) -> Job | None:
        with self._lock:
            runnable = [
                job
                for job in self._rows.values()
                if job.kind in kinds
                and job.run_after is not None
                and job.run_after <= now
                and (job.leased_until is None or job.leased_until <= now)
            ]
            if not runnable:
                return None
            oldest = min(runnable, key=lambda job: (job.run_after, job.id))
            leased = replace(
                oldest,
                leased_until=now + timedelta(seconds=lease_seconds),
                attempts=oldest.attempts + 1,
            )
            self._rows[leased.id] = leased
            return leased

    def complete(self, job_id: int) -> None:
        with self._lock:
            self._rows.pop(job_id, None)

    def fail(self, job_id: int, error: str, retry_at: datetime | None) -> None:
        with self._lock:
            job = self._rows.get(job_id)
            if job is None:
                return
            if retry_at is None:
                del self._rows[job_id]
                return
            self._rows[job_id] = replace(
                job, leased_until=None, run_after=retry_at, last_error=error[:MAX_ERROR_LEN]
            )

    def pending(self, tenant_id: str) -> list[Job]:
        with self._lock:
            jobs = [job for job in self._rows.values() if job.tenant_id == tenant_id]
        return sorted(jobs, key=lambda job: (job.run_after or _EPOCH, job.id))

    def ping(self) -> bool:
        return True
