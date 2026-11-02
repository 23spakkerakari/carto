"""carto_edge.gateway.scheduler: one task per enabled pull source (spec 8.1, 8.5): the read
resumes from the committed cursor, records go to the ingestor in chunks, the read ends with a
flush so the cursor commits only behind durable batches, the loop pauses while the buffer pushes
back, connector failures count in health and back off, and every wait yields to the stop
event."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog

from carto_edge.config import (
    CoreLinkSettings,
    EdgeSettings,
    GatewaySettings,
    ParseConfig,
    PiiSettings,
    RecordFormat,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorError,
    Cursor,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ReadOnlyViolationError,
    ResolvedHost,
)
from carto_edge.connectors.base import (
    TestCheck as ConnectorTestCheck,
)
from carto_edge.connectors.base import (
    TestResult as ConnectorTestResult,
)
from carto_edge.connectors.registry import build_connector
from carto_edge.gateway.scheduler import PULL_SOURCE_TYPES, PollScheduler
from carto_edge.metrics import EdgeMetrics, default_metrics
from carto_edge.pipeline.batch import REASON_BUFFER_FULL, Ingestor
from carto_edge.pipeline.buffer import DiskBuffer, decompress_batch
from carto_edge.pipeline.forward import SourceHealth
from carto_edge.pipeline.ingestor import BackpressureState, IngestOutcome
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.runtime import EdgeRuntime, build_runtime
from carto_edge.state import CursorStore
from carto_schema.event import EventKind
from carto_schema.ingest import SourceStatus

NOW = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)


class Secrets:
    def resolve(self, secret_ref: str) -> str:
        raise AssertionError("no secret is resolved in these tests")


class Network:
    def resolve(self, host: str, port: int) -> ResolvedHost:
        raise AssertionError("no connection is opened in these tests")


CONTEXT = ConnectorContext(secrets=Secrets(), network=Network())


def web_line(i: int) -> str:
    return json.dumps(
        {"ts": "2026-09-23T04:00:00Z", "level": "info", "msg": "cart created", "n": i}
    )


def sources(log_file: Path) -> SourcesFile:
    return SourcesFile(
        systems=[SystemConfig(id="sys_web", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_web",
                type=SourceType.UPLOAD,
                config={"kind": "log", "paths": [str(log_file)]},
                parse=ParseConfig(format=RecordFormat.NDJSON, timestamp_field="ts"),
            ),
            SourceConfig(id="src_push", system="sys_web", type=SourceType.OTLP),
            SourceConfig(
                id="src_hook",
                system="sys_web",
                type=SourceType.WEBHOOK,
                config={"event_type_field": "type"},
            ),
            SourceConfig(
                id="src_broken", system="sys_web", type=SourceType.UPLOAD, config={"kind": "log"}
            ),
            SourceConfig(
                id="src_off",
                system="sys_web",
                type=SourceType.UPLOAD,
                enabled=False,
                config={"paths": ["x.log"]},
            ),
        ],
    )


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "web" / "app.ndjson"
    path.parent.mkdir()
    path.write_text("".join(web_line(i) + "\n" for i in range(1, 4)), encoding="utf-8")
    return path


@pytest.fixture
def runtime(tmp_path: Path, log_file: Path) -> Iterator[EdgeRuntime]:
    settings = EdgeSettings(
        state_dir=tmp_path / "state",
        pii=PiiSettings(enabled=False),
        gateway=GatewaySettings(poll_concurrency=2),
    )
    built = build_runtime(
        settings, sources(log_file), init_local_keys=True, detector=RegexDetector()
    )
    yield built
    built.close()


@pytest.fixture
def buffer(tmp_path: Path) -> Iterator[DiskBuffer]:
    with DiskBuffer(tmp_path / "buffer.sqlite", max_bytes=10**9, backpressure_ratio=0.8) as buf:
        yield buf


@pytest.fixture
def cursors(tmp_path: Path) -> Iterator[CursorStore]:
    with CursorStore(tmp_path / "cursors.sqlite") as store:
        yield store


class SpyIngestor:
    """A real ingestor that records the locators handed to it."""

    def __init__(self, inner: Ingestor) -> None:
        self.inner = inner
        self.locators: list[str] = []
        self.flushes = 0

    def ingest(self, records: Iterable[ReadItem], *, durable: bool = False) -> IngestOutcome:
        batch = list(records)
        self.locators.extend(record.locator for record in batch if isinstance(record, RawRecord))
        return self.inner.ingest(batch)

    def flush(self) -> int:
        self.flushes += 1
        return self.inner.flush()

    def backpressure(self) -> BackpressureState:
        return self.inner.backpressure()


class FakeIngestor:
    def __init__(
        self,
        pressure: list[BackpressureState] | None = None,
        outcomes: list[IngestOutcome] | None = None,
    ) -> None:
        self.pressure = pressure or []
        self.outcomes = outcomes or []
        self.chunks: list[int] = []
        self.flushes = 0
        self.pressure_calls = 0

    def ingest(self, records: Iterable[ReadItem], *, durable: bool = False) -> IngestOutcome:
        batch = list(records)
        self.chunks.append(len(batch))
        if self.outcomes:
            return self.outcomes.pop(0)
        return IngestOutcome(records=len(batch), events=len(batch))

    def flush(self) -> int:
        self.flushes += 1
        return 0

    def backpressure(self) -> BackpressureState:
        self.pressure_calls += 1
        if self.pressure:
            return self.pressure.pop(0)
        return BackpressureState.OK


READ_ONLY = ConnectorTestResult(ok=True, read_only=ReadOnlyStatus.VERIFIED)
WRITE_CAPABLE = ConnectorTestResult(
    ok=False,
    read_only=ReadOnlyStatus.WRITE_CAPABLE,
    checks=(
        ConnectorTestCheck("connect", ok=True, detail="connected"),
        ConnectorTestCheck("grants", ok=False, detail="INSERT on warehouse.c-MARKER-77123"),
    ),
    problems=("the login can write to c-MARKER-77123",),
)


class FakeConnector:
    type: ClassVar[str] = "upload"

    def __init__(
        self,
        source: SourceConfig,
        *,
        records: int = 0,
        fail: bool = False,
        age: float = 30.0,
        tests: list[ConnectorTestResult | Exception] | None = None,
    ) -> None:
        self.source = source
        self.records = records
        self.fail = fail
        self.age = age
        self.tests = list(tests) if tests else [READ_ONLY]
        self.test_calls = 0
        self.reads: list[Cursor | None] = []
        self.generator_closed = False
        self.closed = False

    def validate_config(self, cfg: Mapping[str, Any]) -> ConnectorConfig:
        return ConnectorConfig()

    async def test(self) -> ConnectorTestResult:
        self.test_calls += 1
        answer = self.tests.pop(0) if len(self.tests) > 1 else self.tests[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def read(self, cursor: Cursor | None) -> AsyncIterator[RawRecord]:
        self.reads.append(cursor)
        if self.fail:
            raise ConnectorError("source unreachable")
        try:
            for i in range(1, self.records + 1):
                yield RawRecord(
                    source_id=self.source.id,
                    system_id=self.source.system,
                    kind=EventKind.LOG,
                    locator=f"fake:line:{i}",
                    received_at=NOW - timedelta(seconds=self.age),
                    text=web_line(i),
                    commit_cursor={"line": i},
                )
        finally:
            self.generator_closed = True

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        async for record in self.read(None):
            yield record

    async def close(self) -> None:
        self.closed = True


def factory_for(
    made: dict[str, FakeConnector], **kwargs: Any
) -> Callable[[SourceConfig, ConnectorContext], ReadConnector]:
    def factory(source: SourceConfig, context: ConnectorContext) -> ReadConnector:
        if source.id != "src_web":  # as the real factory refuses src_broken
            raise InvalidConfigError("not built in these tests")
        connector = FakeConnector(source, **kwargs)
        made[source.id] = connector
        return connector

    return factory


def scheduler(
    runtime: EdgeRuntime,
    ingestor: Any,
    cursors: CursorStore,
    *,
    health: SourceHealth | None = None,
    metrics: EdgeMetrics | None = None,
    **kwargs: Any,
) -> PollScheduler:
    return PollScheduler(
        runtime,
        ingestor,
        cursors,
        CONTEXT,
        health if health is not None else SourceHealth(),
        metrics=metrics if metrics is not None else default_metrics(),
        default_poll_seconds=kwargs.pop("default_poll_seconds", 60.0),
        clock=lambda: NOW,
        **kwargs,
    )


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def test_pull_types() -> None:
    assert {kind.value for kind in PULL_SOURCE_TYPES} == {"upload", "splunk", "sql", "sftp"}


def test_connectors_are_built_for_enabled_pull_sources_only(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    with structlog.testing.capture_logs() as logs:
        poll = scheduler(runtime, FakeIngestor(), cursors)
    assert set(poll.connectors) == {"src_web"}
    skipped = [entry for entry in logs if entry.get("source_id") == "src_broken"]
    assert skipped
    assert skipped[0]["log_level"] == "warning"


def test_poll_interval_from_the_source_config_or_the_default(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    poll = scheduler(runtime, FakeIngestor(), cursors, default_poll_seconds=45.0)

    def source(config: dict[str, Any]) -> SourceConfig:
        return SourceConfig(id="src_x", system="sys_web", type=SourceType.SFTP, config=config)

    assert poll.poll_seconds(source({"poll_seconds": 15})) == 15.0
    assert poll.poll_seconds(source({})) == 45.0
    assert poll.poll_seconds(source({"poll_seconds": "soon"})) == 45.0
    assert poll.poll_seconds(source({"poll_seconds": 0})) == 45.0
    assert poll.poll_seconds(source({"poll_seconds": True})) == 45.0


async def test_two_polls_over_a_growing_file_hand_over_only_new_lines(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore, log_file: Path
) -> None:
    metrics = default_metrics()
    inner = Ingestor(
        runtime, buffer, cursors, link=CoreLinkSettings(batch_events=2), metrics=metrics
    )
    spy = SpyIngestor(inner)
    health = SourceHealth()
    poll = scheduler(runtime, spy, cursors, health=health, metrics=metrics)
    try:
        first = await poll.poll_once("src_web")
        assert first.records == 3
        assert not first.aborted
        assert spy.locators == [f"app.ndjson:line:{n}" for n in (1, 2, 3)]
        assert spy.flushes == 1
        assert cursors.get("src_web") == {"file": str(log_file), "line": 3}
        assert buffer.pending_events() == 3

        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(web_line(4) + "\n" + web_line(5) + "\n")
        second = await poll.poll_once("src_web")
        assert second.records == 2
        assert spy.locators[3:] == ["app.ndjson:line:4", "app.ndjson:line:5"]
        assert cursors.get("src_web") == {"file": str(log_file), "line": 5}

        third = await poll.poll_once("src_web")
        assert third.records == 0
        assert len(spy.locators) == 5

        ids = [
            event.event_id
            for row in buffer.iter_pending(100)
            for event in decompress_batch(row.payload).events
        ]
        assert len(ids) == len(set(ids)) == 5
        assert health.snapshot()["src_web"].records == 5
        assert health.status("src_web") is SourceStatus.OK
    finally:
        await poll.close()


async def test_records_go_in_chunks_then_one_flush_and_lag_is_reported(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    ingestor = FakeIngestor()
    metrics = default_metrics()
    health = SourceHealth()
    poll = scheduler(
        runtime,
        ingestor,
        cursors,
        health=health,
        metrics=metrics,
        connector_factory=factory_for(made, records=1200, age=30.0),
    )
    cursors.set("src_web", {"line": 7})
    report = await poll.poll_once("src_web")
    assert report.records == 1200
    assert made["src_web"].reads == [{"line": 7}]
    assert ingestor.chunks == [500, 500, 200]
    assert ingestor.flushes == 1
    assert report.lag_seconds == 30.0
    assert metrics.get("carto_edge_connector_lag_seconds", source_id="src_web") == 30.0
    state = health.snapshot()["src_web"]
    assert state.lag_seconds == 30.0
    assert state.last_success_at == NOW


async def test_a_full_buffer_aborts_the_read_without_a_flush(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    refused = IngestOutcome(records=500)
    refused.dropped[REASON_BUFFER_FULL] = 500
    ingestor = FakeIngestor(outcomes=[refused])
    poll = scheduler(runtime, ingestor, cursors, connector_factory=factory_for(made, records=1200))
    report = await poll.poll_once("src_web")
    assert report.aborted
    assert ingestor.chunks == [500]
    assert ingestor.flushes == 0
    assert made["src_web"].generator_closed


async def test_backpressure_mid_read_stops_early_and_flushes(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    ingestor = FakeIngestor(pressure=[BackpressureState.SLOW])
    poll = scheduler(runtime, ingestor, cursors, connector_factory=factory_for(made, records=1200))
    report = await poll.poll_once("src_web")
    assert report.aborted
    assert ingestor.chunks == [500]
    assert ingestor.flushes == 1  # what was read is made durable before pausing
    assert made["src_web"].generator_closed


async def test_run_waits_while_the_buffer_pushes_back(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    ingestor = FakeIngestor(
        pressure=[BackpressureState.FULL, BackpressureState.SLOW, BackpressureState.SLOW]
    )
    poll = scheduler(
        runtime,
        ingestor,
        cursors,
        connector_factory=factory_for(made, records=3),
        backpressure_wait_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(poll.run(stop))
    await until(lambda: ingestor.flushes >= 1)
    assert ingestor.pressure_calls >= 4  # three refusals, then OK, before the read
    assert ingestor.chunks == [3]
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    await poll.close()
    assert made["src_web"].closed


async def test_connector_errors_count_in_health_and_back_off(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    metrics = default_metrics()
    health = SourceHealth()
    poll = scheduler(
        runtime,
        FakeIngestor(),
        cursors,
        health=health,
        metrics=metrics,
        connector_factory=factory_for(made, fail=True),
        error_backoff_base_seconds=0.01,
        error_backoff_cap_seconds=0.02,
    )
    stop = asyncio.Event()
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(poll.run(stop))
        await until(
            lambda: metrics.get("carto_edge_connector_errors_total", source_id="src_web") >= 3
        )
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    assert health.status("src_web") is SourceStatus.FAILING
    failures = [entry for entry in logs if entry.get("error") == "ConnectorError"]
    assert failures
    assert all(entry["source_id"] == "src_web" for entry in failures)
    assert "source unreachable" not in repr(logs)
    await poll.close()


async def test_run_stops_promptly_during_the_poll_wait(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    metrics = default_metrics()
    ingestor = Ingestor(runtime, buffer, cursors, link=CoreLinkSettings(), metrics=metrics)
    health = SourceHealth()
    poll = scheduler(
        runtime, ingestor, cursors, health=health, metrics=metrics, default_poll_seconds=3600
    )
    stop = asyncio.Event()
    task = asyncio.create_task(poll.run(stop))
    await until(lambda: "src_web" in health.snapshot())
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    await poll.close()
    assert buffer.pending_events() == 3
    assert cursors.get("src_web") is not None


async def test_run_without_pull_sources_waits_for_stop(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    poll = scheduler(
        runtime, FakeIngestor(), cursors, connector_factory=lambda _source, _context: _refuse()
    )
    assert poll.connectors == {}
    stop = asyncio.Event()
    task = asyncio.create_task(poll.run(stop))
    await asyncio.sleep(0.02)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


def _refuse() -> ReadConnector:
    raise ConnectorError("refused at build time")


def test_build_connector_is_the_default_factory(runtime: EdgeRuntime, cursors: CursorStore) -> None:
    poll = scheduler(runtime, FakeIngestor(), cursors)
    connector = poll.connectors["src_web"]
    assert type(connector) is type(build_connector(runtime.sources.sources[0], CONTEXT))


# ---------------------------------------------------------------------------------------------
# The read-only gate (spec 8.1.4, 2.3 invariant 1)
# ---------------------------------------------------------------------------------------------


async def test_a_write_capable_source_is_never_read(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    metrics = default_metrics()
    health = SourceHealth()
    poll = scheduler(
        runtime,
        FakeIngestor(),
        cursors,
        health=health,
        metrics=metrics,
        connector_factory=factory_for(made, records=3, tests=[WRITE_CAPABLE]),
        error_backoff_base_seconds=0.01,
        error_backoff_cap_seconds=0.02,
    )
    stop = asyncio.Event()
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(poll.run(stop))
        await until(lambda: made["src_web"].test_calls >= 3)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    connector = made["src_web"]
    assert connector.reads == []
    assert poll.test_results()["src_web"].read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert metrics.get("carto_edge_connector_errors_total", source_id="src_web") >= 2
    assert health.status("src_web") is not SourceStatus.OK
    refusals = [e for e in logs if e["event"] == "scheduler.source_not_enabled"]
    assert refusals
    assert refusals[0]["source_id"] == "src_web"
    assert refusals[0]["read_only"] == "write_capable"
    assert refusals[0]["failed_checks"] == ["grants"]
    assert "MARKER" not in repr(logs)
    with pytest.raises(ReadOnlyViolationError):
        await poll.poll_once("src_web")
    assert connector.reads == []
    await poll.close()


async def test_a_corrected_grant_starts_polling_without_a_restart(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    ingestor = FakeIngestor()
    poll = scheduler(
        runtime,
        ingestor,
        cursors,
        connector_factory=factory_for(made, records=3, tests=[WRITE_CAPABLE, READ_ONLY]),
        error_backoff_base_seconds=0.01,
        error_backoff_cap_seconds=0.02,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(poll.run(stop))
    await until(lambda: ingestor.flushes >= 1)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert made["src_web"].test_calls == 2
    assert made["src_web"].reads == [None]
    assert poll.test_results()["src_web"].can_enable
    await poll.close()


@pytest.mark.parametrize(
    "answer",
    [
        ConnectorTestResult(ok=True, read_only=ReadOnlyStatus.NOT_VERIFIABLE),
        READ_ONLY,
    ],
    ids=["not_verifiable", "verified"],
)
async def test_a_passing_test_lets_poll_once_read(
    runtime: EdgeRuntime, cursors: CursorStore, answer: ConnectorTestResult
) -> None:
    made: dict[str, FakeConnector] = {}
    poll = scheduler(
        runtime, FakeIngestor(), cursors, connector_factory=factory_for(made, tests=[answer])
    )
    report = await poll.poll_once("src_web")
    assert report.records == 0
    assert made["src_web"].reads == [None]
    await poll.poll_once("src_web")
    assert made["src_web"].test_calls == 1  # tested once, not before every read


async def test_a_failing_or_raising_test_keeps_the_source_off(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    made: dict[str, FakeConnector] = {}
    unreachable = ConnectorTestResult(
        ok=False,
        read_only=ReadOnlyStatus.NOT_VERIFIABLE,
        checks=(ConnectorTestCheck("connect", ok=False, detail="refused"),),
    )
    poll = scheduler(
        runtime,
        FakeIngestor(),
        cursors,
        connector_factory=factory_for(
            made, tests=[unreachable, ConnectorError("boom c-MARKER-77123")]
        ),
    )
    with pytest.raises(ConnectorError):
        await poll.poll_once("src_web")
    with structlog.testing.capture_logs() as logs, pytest.raises(ConnectorError):
        await poll.poll_once("src_web")
    assert made["src_web"].reads == []
    result = poll.test_results()["src_web"]
    assert not result.ok
    assert [check.name for check in result.checks] == ["test_raised:ConnectorError"]
    assert "MARKER" not in repr(logs)


def test_a_source_skipped_at_build_time_reports_an_error_in_health(
    runtime: EdgeRuntime, cursors: CursorStore
) -> None:
    """Spec 8.1: the heartbeat must not say "ok, no data" for a source that is never read."""
    health = SourceHealth()
    poll = scheduler(
        runtime,
        FakeIngestor(),
        cursors,
        health=health,
        connector_factory=lambda _source, _context: _refuse(),
    )
    assert not poll.connectors
    skipped = [
        source.id
        for source in runtime.sources.sources
        if source.enabled and source.type in PULL_SOURCE_TYPES
    ]
    assert skipped
    for source_id in skipped:
        assert health.status(source_id) is not SourceStatus.OK


class CheckpointConnector(FakeConnector):
    """Yields its records without cursors, then one cursor-only checkpoint (ADR 0035)."""

    async def read(self, cursor: Cursor | None) -> AsyncIterator[ReadItem]:  # type: ignore[override]
        self.reads.append(cursor)
        for i in range(1, self.records + 1):
            yield RawRecord(
                source_id=self.source.id,
                system_id=self.source.system,
                kind=EventKind.LOG,
                locator=f"fake:line:{i}",
                received_at=NOW - timedelta(seconds=self.age),
                text=web_line(i),
            )
        yield CursorCheckpoint(self.source.id, {"window_end": "2026-10-10T09:00:00+00:00"})


async def test_a_checkpoint_is_committed_after_the_flush_and_not_counted(
    runtime: EdgeRuntime, buffer: DiskBuffer, cursors: CursorStore
) -> None:
    def factory(source: SourceConfig, context: ConnectorContext) -> ReadConnector:
        if source.id != "src_web":
            raise InvalidConfigError("not built in these tests")
        return CheckpointConnector(source, records=2)

    inner = Ingestor(
        runtime, buffer, cursors, link=CoreLinkSettings(batch_events=10), metrics=default_metrics()
    )
    spy = SpyIngestor(inner)
    poll = scheduler(runtime, spy, cursors, connector_factory=factory)
    try:
        report = await poll.poll_once("src_web")
        assert report.records == 2  # the checkpoint is not a record
        assert spy.locators == ["fake:line:1", "fake:line:2"]
        assert cursors.get("src_web") == {"window_end": "2026-10-10T09:00:00+00:00"}
        assert buffer.pending_events() == 2
    finally:
        await poll.close()
