"""The PostgreSQL :class:`~carto_core.jobs.JobQueue`: the ``jobs`` table of migration 0004
(spec 5.3 "Workers coordinate through PostgreSQL job leases", 7.3 ``jobs``).

A lease is one transaction: ``SELECT ... FOR UPDATE SKIP LOCKED`` ordered by ``(run_after, id)``
picks the oldest runnable row no other worker holds, and the ``UPDATE`` in the same transaction
sets ``leased_until`` and increments ``attempts``, so two workers never run one job at once and a
lease that outlives a crashed worker is retaken once it expires. ``enqueue`` with ``dedupe``
serializes on a transaction-scoped advisory lock keyed by tenant and kind, so two processes
adding the same recurring job at once still leave one row. Payloads are JSON objects compared
as ``jsonb``, which makes the comparison independent of key order. Every statement is fixed
text with bound parameters (spec 14.7); errors raise :class:`~carto_core.jobs.JobQueueError`
naming the driver error class only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.db.postgres import ping_engine
from carto_core.jobs import MAX_ERROR_LEN, Job, JobKind, JobQueueError

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Row

__all__ = ["PostgresJobQueue"]

_DEDUPE_LOCK = sqlalchemy.text("SELECT pg_advisory_xact_lock(hashtext(:key))")
_EXISTS = sqlalchemy.text(
    "SELECT 1 FROM jobs WHERE tenant_id = :tenant_id AND kind = :kind "
    "AND payload = CAST(:payload AS jsonb) AND leased_until IS NULL LIMIT 1"
)
_INSERT = sqlalchemy.text(
    "INSERT INTO jobs (tenant_id, kind, payload, run_after, created_at, updated_at) "
    "VALUES (:tenant_id, :kind, CAST(:payload AS jsonb), :run_after, :run_after, :run_after) "
    "RETURNING id, tenant_id, kind, payload, run_after, leased_until, attempts, last_error"
)
_PICK = sqlalchemy.text(
    "SELECT id, tenant_id, kind, payload, run_after, leased_until, attempts, last_error "
    "FROM jobs WHERE kind = ANY(:kinds) AND run_after <= :now "
    "AND (leased_until IS NULL OR leased_until <= :now) "
    "ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED"
)
_LEASE = sqlalchemy.text(
    "UPDATE jobs SET leased_until = :until, attempts = attempts + 1, updated_at = :now "
    "WHERE id = :id "
    "RETURNING id, tenant_id, kind, payload, run_after, leased_until, attempts, last_error"
)
_DELETE = sqlalchemy.text("DELETE FROM jobs WHERE id = :id")
_RETRY = sqlalchemy.text(
    "UPDATE jobs SET leased_until = NULL, run_after = :retry_at, last_error = :error, "
    "updated_at = :retry_at WHERE id = :id"
)
_PENDING = sqlalchemy.text(
    "SELECT id, tenant_id, kind, payload, run_after, leased_until, attempts, last_error "
    "FROM jobs WHERE tenant_id = :tenant_id ORDER BY run_after, id"
)


def _job(row: Row[Any]) -> Job:
    """A :class:`Job` from a row; the row is untrusted, so the kind and payload are checked."""
    try:
        kind = JobKind(str(row.kind))
    except ValueError as exc:
        msg = "jobs row carries an unknown kind"
        raise JobQueueError(msg) from exc
    payload = row.payload if isinstance(row.payload, dict) else {}
    return Job(
        id=int(row.id),
        tenant_id=str(row.tenant_id),
        kind=kind,
        payload=dict(payload),
        run_after=row.run_after,
        leased_until=row.leased_until,
        attempts=int(row.attempts),
        last_error=row.last_error,
    )


class PostgresJobQueue:
    """See the module docstring."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enqueue(
        self,
        tenant_id: str,
        kind: JobKind,
        payload: dict[str, Any],
        run_after: datetime,
        *,
        dedupe: bool = True,
    ) -> Job | None:
        parameters = {
            "tenant_id": tenant_id,
            "kind": kind.value,
            "payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "run_after": run_after,
        }
        try:
            with self._engine.begin() as connection:
                if dedupe:
                    connection.execute(_DEDUPE_LOCK, {"key": f"{tenant_id}/{kind.value}"})
                    if connection.execute(_EXISTS, parameters).first() is not None:
                        return None
                row = connection.execute(_INSERT, parameters).one()
        except SQLAlchemyError as exc:
            msg = f"job enqueue failed: {type(exc).__name__}"
            raise JobQueueError(msg) from exc
        return _job(row)

    def lease(self, kinds: frozenset[JobKind], now: datetime, lease_seconds: int) -> Job | None:
        if not kinds:
            return None
        try:
            with self._engine.begin() as connection:
                picked = connection.execute(
                    _PICK, {"kinds": sorted(kind.value for kind in kinds), "now": now}
                ).first()
                if picked is None:
                    return None
                row = connection.execute(
                    _LEASE,
                    {
                        "id": int(picked.id),
                        "until": now + timedelta(seconds=lease_seconds),
                        "now": now,
                    },
                ).one()
        except SQLAlchemyError as exc:
            msg = f"job lease failed: {type(exc).__name__}"
            raise JobQueueError(msg) from exc
        return _job(row)

    def complete(self, job_id: int) -> None:
        self._run(_DELETE, {"id": job_id}, "complete")

    def fail(self, job_id: int, error: str, retry_at: datetime | None) -> None:
        if retry_at is None:
            self._run(_DELETE, {"id": job_id}, "fail")
            return
        self._run(
            _RETRY, {"id": job_id, "retry_at": retry_at, "error": error[:MAX_ERROR_LEN]}, "fail"
        )

    def pending(self, tenant_id: str) -> list[Job]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_PENDING, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            msg = f"job listing failed: {type(exc).__name__}"
            raise JobQueueError(msg) from exc
        return [_job(row) for row in rows]

    def ping(self) -> bool:
        return ping_engine(self._engine)

    def _run(self, statement: sqlalchemy.TextClause, parameters: dict[str, Any], what: str) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(statement, parameters)
        except SQLAlchemyError as exc:
            msg = f"job {what} failed: {type(exc).__name__}"
            raise JobQueueError(msg) from exc
