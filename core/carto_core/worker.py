"""``carto-core worker``: lease profile, link and family jobs from PostgreSQL (spec 5.3, 9.1;
plan M2 "How the engine runs"; ADR 0055).

:class:`Worker` is the loop, testable with an injected :class:`~carto_core.jobs.JobQueue`, a
clock and a mapping of :class:`~carto_core.jobs.JobKind` to handler. Each round it makes sure
the recurring jobs exist (``enqueue`` with ``dedupe`` keeps one pending row per kind and
payload, so restarts and two workers never pile them up), then leases the oldest runnable job of
the kinds it has a handler for, runs the handler, and completes it:

- an incremental ``profile`` (payload ``{}``) re-queues itself at ``now +
  profiler.update_interval_seconds`` and, when ``worker.link_after_profile`` is set and the pass
  reported new sketches, queues a ``link`` (payload ``{}``) at once;
- the nightly full passes, ``profile {"full": true}`` at ``profiler.rebuild_hour_utc`` and
  ``link {"full": true}`` at ``worker.full_pass_hour_utc``, are re-queued for the next slot by
  the recurring check, so they run once a day whatever happened in between.

A handler that raises is logged by exception class only (spec 2.3 invariant 7) and the job is
released for a retry after ``worker.backoff_base_seconds * 2 ** attempts``; after
``worker.max_attempts`` it is dropped. The job bodies come from packages B and C
(``carto_core.profiler.run.ProfilePass``, ``carto_core.linker.run.LinkPass``, both with
``run(tenant_id, *, full, now)``) through the :data:`HandlerFactory` seam:
:func:`default_handler_factory` imports each pass lazily and builds it with its
``from_context(context)`` class method (the pass wires its own repositories from the context's
engine and stores); a pass that is missing or has no ``from_context`` yet leaves its kind
without a handler, logged once, so the worker runs while the engine packages are being wired.
"""

from __future__ import annotations

import importlib
import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from carto_common.logging import configure_logging, get_logger
from carto_core.jobs import Job, JobKind, JobQueue, JobQueueError

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from carto_core.metadata import MetadataStore
    from carto_core.settings import CoreSettings, ProfilerSettings, WorkerSettings
    from carto_core.store import EventStore

__all__ = [
    "Handler",
    "HandlerFactory",
    "JobOutcome",
    "Worker",
    "WorkerContext",
    "default_handler_factory",
    "load_pass",
    "next_daily_slot",
    "run_worker",
]

log = get_logger(component="worker")

MAX_BACKOFF_EXPONENT: Final = 20
"""Bounds ``2 ** attempts`` so a misconfigured attempt count cannot overflow a timedelta."""


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """What a handler reports back: whether the pass produced new sketches (which queues a link
    pass) and counts for the log line. Never a value."""

    new_sketches: bool = False
    counts: Mapping[str, int] = field(default_factory=dict)


Handler = Callable[[Job], JobOutcome]
"""One job body. It raises on failure; the worker schedules the retry."""


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """What the engine passes get: settings, the tenant, both stores, the engine and the clock."""

    settings: CoreSettings
    tenant_id: str
    events: EventStore
    metadata: MetadataStore
    engine: Engine
    clock: Callable[[], datetime]


HandlerFactory = Callable[[WorkerContext], Mapping[JobKind, Handler]]
"""Builds the handlers for a context; the seam packages B and C plug into."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _outcome(result: object) -> JobOutcome:
    """Accept a :class:`JobOutcome` or any pass report with a ``new_sketches`` attribute; the
    report's integer fields become the log counts."""
    if isinstance(result, JobOutcome):
        return result
    counts: dict[str, int] = {}
    if hasattr(result, "__dict__"):
        counts = {
            name: value
            for name, value in vars(result).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    return JobOutcome(new_sketches=bool(getattr(result, "new_sketches", False)), counts=counts)


def load_pass(module_name: str, class_name: str) -> Any | None:
    """The pass class when its module and ``from_context`` exist, else ``None`` (logged)."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        log.warning("engine packages not built yet", missing=module_name)
        return None
    pass_type = getattr(module, class_name, None)
    if pass_type is None or not callable(getattr(pass_type, "from_context", None)):
        log.warning("engine pass not wired yet", missing=f"{module_name}.{class_name}.from_context")
        return None
    return pass_type


def _handler(pass_object: Any, context: WorkerContext) -> Handler:
    def run(job: Job) -> JobOutcome:
        full = bool(job.payload.get("full"))
        return _outcome(pass_object.run(context.tenant_id, full=full, now=context.clock()))

    return run


def default_handler_factory(context: WorkerContext) -> Mapping[JobKind, Handler]:
    """``ProfilePass`` and ``LinkPass`` built with ``from_context``; kinds whose pass is not
    available yet get no handler (module docstring)."""
    handlers: dict[JobKind, Handler] = {}
    for kind, module_name, class_name in (
        (JobKind.PROFILE, "carto_core.profiler.run", "ProfilePass"),
        (JobKind.LINK, "carto_core.linker.run", "LinkPass"),
    ):
        pass_type = load_pass(module_name, class_name)
        if pass_type is not None:
            handlers[kind] = _handler(pass_type.from_context(context), context)
    return handlers


def next_daily_slot(now: datetime, hour_utc: int) -> datetime:
    """The next ``hour_utc:00`` UTC strictly after ``now`` (a job due exactly now was queued
    by an earlier round, so a completed nightly job is never re-queued for the same slot)."""
    at = now.astimezone(UTC)
    slot = at.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return slot if slot > at else slot + timedelta(days=1)


class Worker:
    """See the module docstring."""

    def __init__(
        self,
        queue: JobQueue,
        tenant_id: str,
        *,
        handlers: Mapping[JobKind, Handler],
        worker: WorkerSettings,
        profiler: ProfilerSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue = queue
        self._tenant_id = tenant_id
        self._handlers = dict(handlers)
        self._worker = worker
        self._profiler = profiler
        self._clock = clock if clock is not None else _utc_now

    def __repr__(self) -> str:
        return f"Worker(tenant_id={self._tenant_id!r}, kinds={sorted(self._handlers)!r})"

    @property
    def kinds(self) -> frozenset[JobKind]:
        return frozenset(self._handlers)

    def ensure_recurring(self, now: datetime | None = None) -> None:
        """Queue the recurring jobs that are not already waiting (dedupe)."""
        at = now if now is not None else self._clock()
        tenant = self._tenant_id
        self._queue.enqueue(tenant, JobKind.PROFILE, {}, at)
        self._queue.enqueue(
            tenant,
            JobKind.PROFILE,
            {"full": True},
            next_daily_slot(at, self._profiler.rebuild_hour_utc),
        )
        self._queue.enqueue(
            tenant,
            JobKind.LINK,
            {"full": True},
            next_daily_slot(at, self._worker.full_pass_hour_utc),
        )

    def run_once(self, now: datetime | None = None) -> bool:
        """Lease and run one job; False when nothing was runnable."""
        at = now if now is not None else self._clock()
        if not self._handlers:
            return False
        job = self._queue.lease(self.kinds, at, self._worker.lease_seconds)
        if job is None:
            return False
        context = {"job_id": job.id, "kind": job.kind.value, "attempt": job.attempts}
        full = bool(job.payload.get("full"))
        try:
            outcome = self._handlers[job.kind](job)
        except Exception as exc:  # the loop must survive a failing pass; the type says enough
            self._failed(job, type(exc).__name__, context)
            return True
        self._queue.complete(job.id)
        log.info(
            "job completed",
            full=full,
            new_sketches=outcome.new_sketches,
            **outcome.counts,
            **context,
        )
        self._follow_up(job, outcome, full)
        return True

    def _failed(self, job: Job, error: str, context: dict[str, Any]) -> None:
        if job.attempts >= self._worker.max_attempts:
            log.error("job dropped after its last attempt", error=error, **context)
            self._queue.fail(job.id, error, None)
            return
        exponent = min(job.attempts, MAX_BACKOFF_EXPONENT)
        delay = timedelta(seconds=self._worker.backoff_base_seconds * 2**exponent)
        retry_at = self._clock() + delay
        log.warning("job failed", error=error, retry_in_seconds=delay.total_seconds(), **context)
        self._queue.fail(job.id, error, retry_at)

    def _follow_up(self, job: Job, outcome: JobOutcome, full: bool) -> None:
        if job.kind is not JobKind.PROFILE:
            return
        now = self._clock()
        if not full:
            interval = timedelta(seconds=self._profiler.update_interval_seconds)
            self._queue.enqueue(self._tenant_id, JobKind.PROFILE, {}, now + interval)
        if self._worker.link_after_profile and outcome.new_sketches:
            self._queue.enqueue(self._tenant_id, JobKind.LINK, {}, now)

    def run(self, *, once: bool = False, stop: threading.Event | None = None) -> int:
        """The service loop: recurring check, drain the runnable jobs, wait ``poll_seconds``;
        with ``once`` return after the first drain. Returns the process exit code."""
        halt = stop if stop is not None else threading.Event()
        if not self._handlers:
            log.warning("worker has no job handlers; nothing will run")
        while not halt.is_set():
            try:
                self.ensure_recurring()
                while not halt.is_set() and self.run_once():
                    pass
            except JobQueueError as exc:
                log.warning("job queue unavailable", error=type(exc).__name__)
            if once:
                return 0
            halt.wait(self._worker.poll_seconds)
        return 0


def run_worker(settings: CoreSettings, *, once: bool = False) -> int:
    """Connect to both stores, build the handlers and run the loop until signalled."""
    from carto_core.db.clickhouse import create_client  # noqa: PLC0415 - keeps import light
    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.jobs_postgres import PostgresJobQueue  # noqa: PLC0415
    from carto_core.metadata import PostgresMetadataStore  # noqa: PLC0415
    from carto_core.store.clickhouse import ClickHouseStore  # noqa: PLC0415

    configure_logging("worker", settings.log_level, settings.log_json)
    client = create_client(settings.clickhouse)
    try:
        engine = create_engine_from_settings(settings.postgres)
        try:
            context = WorkerContext(
                settings=settings,
                tenant_id=settings.tenant_id,
                events=ClickHouseStore(client),
                metadata=PostgresMetadataStore(engine),
                engine=engine,
                clock=_utc_now,
            )
            worker = Worker(
                PostgresJobQueue(engine),
                settings.tenant_id,
                handlers=default_handler_factory(context),
                worker=settings.worker,
                profiler=settings.profiler,
            )
            stop = threading.Event()
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, lambda *_args: stop.set())
            log.info("worker started", tenant_id=settings.tenant_id, kinds=sorted(worker.kinds))
            return worker.run(once=once, stop=stop)
        finally:
            engine.dispose()
    finally:
        client.close()
