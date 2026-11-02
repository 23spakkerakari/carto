"""``carto-core worker`` (spec 5.3; plan M2 "How the engine runs"): the recurring jobs exist
once, a profile pass re-queues itself and queues a link pass when it produced new sketches, a
failing handler backs off exponentially by exception class only and is dropped after the last
attempt, kinds without a handler are never leased, ``--once`` exits 0, and the handler factory
runs with no handlers while the engine packages are missing."""

from __future__ import annotations

import importlib
import io
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from carto_common.logging import configure_logging
from carto_core import worker as worker_module
from carto_core.jobs import InMemoryJobQueue, Job, JobKind
from carto_core.settings import CoreSettings, ProfilerSettings, WorkerSettings
from carto_core.worker import (
    JobOutcome,
    Worker,
    WorkerContext,
    default_handler_factory,
    next_daily_slot,
    run_worker,
)

NOW = datetime(2026, 10, 18, 8, 30, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _worker(
    queue: InMemoryJobQueue,
    clock: Clock,
    handlers: dict[JobKind, Any],
    **overrides: Any,
) -> Worker:
    return Worker(
        queue,
        "default",
        handlers=handlers,
        worker=WorkerSettings(**overrides),
        profiler=ProfilerSettings(),
        clock=clock,
    )


def _by_kind(queue: InMemoryJobQueue) -> dict[tuple[JobKind, bool], Job]:
    return {(job.kind, bool(job.payload.get("full"))): job for job in queue.pending("default")}


def test_next_daily_slot_is_strictly_after_now() -> None:
    assert next_daily_slot(NOW, 9) == NOW.replace(hour=9, minute=0)
    assert next_daily_slot(NOW, 2) == (NOW + timedelta(days=1)).replace(hour=2, minute=0)
    at_slot = NOW.replace(hour=2, minute=0)
    assert next_daily_slot(at_slot, 2) == at_slot + timedelta(days=1)
    assert next_daily_slot(at_slot - timedelta(seconds=1), 2) == at_slot


def test_recurring_jobs_exist_once_however_often_they_are_ensured() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    worker = _worker(queue, clock, {JobKind.PROFILE: lambda _job: JobOutcome()})
    worker.ensure_recurring()
    worker.ensure_recurring()
    clock.at += timedelta(minutes=1)
    worker.ensure_recurring()
    jobs = _by_kind(queue)
    assert set(jobs) == {(JobKind.PROFILE, False), (JobKind.PROFILE, True), (JobKind.LINK, True)}
    assert jobs[JobKind.PROFILE, False].run_after == NOW
    assert jobs[JobKind.PROFILE, True].run_after == (NOW + timedelta(days=1)).replace(
        hour=2, minute=0
    )
    assert jobs[JobKind.LINK, True].run_after == (NOW + timedelta(days=1)).replace(hour=3, minute=0)
    assert len(queue) == 3


def test_a_profile_pass_requeues_itself_and_queues_a_link_when_it_produced_sketches() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    seen: list[Job] = []

    def profile(job: Job) -> JobOutcome:
        seen.append(job)
        return JobOutcome(new_sketches=True, counts={"rows": 12})

    worker = _worker(
        queue, clock, {JobKind.PROFILE: profile, JobKind.LINK: lambda _j: JobOutcome()}
    )
    worker.ensure_recurring()
    assert worker.run_once() is True
    assert [job.kind for job in seen] == [JobKind.PROFILE]
    jobs = _by_kind(queue)
    assert jobs[JobKind.PROFILE, False].run_after == NOW + timedelta(seconds=300)
    assert jobs[JobKind.LINK, False].run_after == NOW
    assert worker.run_once() is True, "the link pass runs at once"
    assert (JobKind.LINK, False) not in _by_kind(queue)
    assert worker.run_once() is False, "the next profile pass is five minutes away"
    clock.at += timedelta(seconds=300)
    assert worker.run_once() is True


def test_no_link_pass_without_new_sketches_or_when_disabled() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    worker = _worker(queue, clock, {JobKind.PROFILE: lambda _job: JobOutcome(new_sketches=False)})
    worker.ensure_recurring()
    worker.run_once()
    assert (JobKind.LINK, False) not in _by_kind(queue)
    queue, clock = InMemoryJobQueue(), Clock()
    worker = _worker(
        queue,
        clock,
        {JobKind.PROFILE: lambda _job: JobOutcome(new_sketches=True)},
        link_after_profile=False,
    )
    worker.ensure_recurring()
    worker.run_once()
    assert (JobKind.LINK, False) not in _by_kind(queue)


def test_the_nightly_full_pass_does_not_requeue_itself_every_five_minutes() -> None:
    slot = NOW.replace(hour=2, minute=0)
    queue, clock = InMemoryJobQueue(), Clock(slot - timedelta(minutes=1))
    full_runs: list[bool] = []

    def profile(job: Job) -> JobOutcome:
        full_runs.append(bool(job.payload.get("full")))
        return JobOutcome(new_sketches=True)

    worker = _worker(queue, clock, {JobKind.PROFILE: profile})
    worker.ensure_recurring()
    assert _by_kind(queue)[JobKind.PROFILE, True].run_after == slot, "today's slot, still ahead"
    clock.at = slot
    worker.ensure_recurring()
    while worker.run_once():
        pass
    assert full_runs == [False, True]
    jobs = _by_kind(queue)
    assert jobs[JobKind.PROFILE, False].run_after == slot + timedelta(seconds=300)
    assert (JobKind.PROFILE, True) not in jobs
    worker.ensure_recurring()
    assert _by_kind(queue)[JobKind.PROFILE, True].run_after == slot + timedelta(days=1)
    assert len([job for job in queue.pending("default") if job.payload.get("full")]) == 2


def test_a_failing_handler_backs_off_by_class_only_and_is_dropped_after_the_last_attempt() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    buffer = io.StringIO()
    configure_logging("worker-test", level="debug", stream=buffer)

    def explode(_job: Job) -> JobOutcome:
        raise ValueError("token t1.SECRETVALUE must not be logged")

    worker = _worker(queue, clock, {JobKind.LINK: explode}, max_attempts=3, backoff_base_seconds=10)
    queue.enqueue("default", JobKind.LINK, {}, NOW)
    assert worker.run_once() is True
    (job,) = queue.pending("default")
    assert job.attempts == 1 and job.last_error == "ValueError"
    assert job.run_after == NOW + timedelta(seconds=20), "base * 2 ** attempts"
    assert job.leased_until is None
    assert worker.run_once() is False, "not before the retry time"
    clock.at = job.run_after
    assert worker.run_once() is True
    (job,) = queue.pending("default")
    assert job.attempts == 2 and job.run_after == clock.at + timedelta(seconds=40)
    clock.at = job.run_after
    assert worker.run_once() is True
    assert queue.pending("default") == [], "dropped after max_attempts"
    logs = buffer.getvalue()
    assert "ValueError" in logs
    assert "SECRETVALUE" not in logs and "must not be logged" not in logs


def test_kinds_without_a_handler_are_never_leased() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    worker = _worker(queue, clock, {JobKind.PROFILE: lambda _job: JobOutcome()})
    queue.enqueue("default", JobKind.LINK, {}, NOW - timedelta(hours=1))
    queue.enqueue("default", JobKind.FAMILIES, {}, NOW - timedelta(hours=1))
    assert worker.kinds == frozenset({JobKind.PROFILE})
    assert worker.run_once() is False
    assert all(job.leased_until is None for job in queue.pending("default"))
    idle = _worker(queue, clock, {})
    assert idle.run_once() is False


def test_run_once_flag_drains_and_exits_zero() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    runs: list[JobKind] = []

    def record(job: Job) -> JobOutcome:
        runs.append(job.kind)
        return JobOutcome(new_sketches=job.kind is JobKind.PROFILE)

    worker = _worker(
        queue,
        clock,
        {JobKind.PROFILE: record, JobKind.LINK: record},
    )
    assert worker.run(once=True) == 0
    assert runs == [JobKind.PROFILE, JobKind.LINK]
    assert set(_by_kind(queue)) == {
        (JobKind.PROFILE, False),
        (JobKind.PROFILE, True),
        (JobKind.LINK, True),
    }
    assert _worker(InMemoryJobQueue(), clock, {}).run(once=True) == 0


def test_the_service_loop_stops_when_signalled() -> None:
    queue, clock = InMemoryJobQueue(), Clock()
    worker = _worker(queue, clock, {JobKind.PROFILE: lambda _job: JobOutcome()}, poll_seconds=0.5)
    stop = threading.Event()
    codes: list[int] = []
    thread = threading.Thread(target=lambda: codes.append(worker.run(stop=stop)), daemon=True)
    thread.start()
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and codes == [0]


def _context() -> WorkerContext:
    return WorkerContext(
        settings=CoreSettings(),
        tenant_id="default",
        events=object(),  # type: ignore[arg-type]
        metadata=object(),  # type: ignore[arg-type]
        engine=object(),  # type: ignore[arg-type]
        clock=Clock(),
    )


def test_the_default_factory_runs_without_the_engine_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packages B and C land in parallel: a missing module or a pass without ``from_context``
    leaves its kind without a handler instead of failing the worker."""
    monkeypatch.setattr(worker_module, "load_pass", lambda _module, _name: None)
    assert dict(default_handler_factory(_context())) == {}
    monkeypatch.undo()
    assert worker_module.load_pass("carto_core.no_such_package.run", "Pass") is None

    class Unwired:
        pass

    fake = type("module", (), {"ProfilePass": Unwired})
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake)
    assert worker_module.load_pass("carto_core.profiler.run", "ProfilePass") is None
    assert worker_module.load_pass("carto_core.profiler.run", "Missing") is None


def test_the_default_factory_builds_passes_from_the_context_and_calls_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, bool, datetime]] = []

    class Report:
        def __init__(self, new_sketches: bool) -> None:
            self.new_sketches = new_sketches
            self.rows_read = 7
            self.full = False

    class FakePass:
        def __init__(self, name: str) -> None:
            self.name = name

        @classmethod
        def from_context(cls, context: WorkerContext) -> FakePass:
            return cls(context.tenant_id)

        def run(self, tenant_id: str, *, full: bool, now: datetime) -> Report:
            calls.append((self.name, tenant_id, full, now))
            return Report(new_sketches=not full)

    monkeypatch.setattr(worker_module, "load_pass", lambda _module, _name: FakePass)
    handlers = default_handler_factory(_context())
    assert set(handlers) == {JobKind.PROFILE, JobKind.LINK}
    outcome = handlers[JobKind.PROFILE](Job(1, "default", JobKind.PROFILE, {}))
    assert outcome == JobOutcome(new_sketches=True, counts={"rows_read": 7})
    nightly = handlers[JobKind.LINK](Job(2, "default", JobKind.LINK, {"full": True}))
    assert nightly.new_sketches is False
    assert [(name, tenant, full) for name, tenant, full, _ in calls] == [
        ("default", "default", False),
        ("default", "default", True),
    ]
    assert all(now == NOW for _, _, _, now in calls)


def test_run_worker_once_wires_the_stores_and_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class Closable:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

        def dispose(self) -> None:
            self.closed += 1

    client, engine = Closable(), Closable()
    queue = InMemoryJobQueue()
    seen: dict[str, Any] = {}
    monkeypatch.setattr("carto_core.db.clickhouse.create_client", lambda _s: client)
    monkeypatch.setattr("carto_core.db.postgres.create_engine_from_settings", lambda _s: engine)
    monkeypatch.setattr("carto_core.jobs_postgres.PostgresJobQueue", lambda _engine: queue)

    def factory(context: WorkerContext) -> dict[JobKind, Any]:
        seen["context"] = context
        return {JobKind.PROFILE: lambda _job: JobOutcome(new_sketches=True)}

    monkeypatch.setattr(worker_module, "default_handler_factory", factory)
    assert run_worker(CoreSettings(), once=True) == 0
    assert client.closed == 1 and engine.closed == 1
    context = seen["context"]
    assert context.tenant_id == "default" and context.engine is engine
    assert {job.kind for job in queue.pending("default")} == {JobKind.PROFILE, JobKind.LINK}
