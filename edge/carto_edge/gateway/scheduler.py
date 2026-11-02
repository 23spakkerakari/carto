"""The poll scheduler of edge-gateway: one asyncio task per enabled pull source (spec 8.1, 8.5;
plan M1 wave 2, D1).

Pull sources are the connector types that read on the edge's clock: ``upload``, ``splunk``,
``sql`` and ``sftp``. ``otlp`` and ``webhook`` sources are pushed to the gateway's routes and
have no task here. A connector is built once per source with
:func:`carto_edge.connectors.registry.build_connector`; a source whose config the connector
refuses is logged by id and skipped, so one bad entry does not stop the others.

Before its first read, each task runs the connector's ``test()`` (spec 8.1.4 and 2.3 invariant
1: connectivity plus read-only verification) and polls only when the result can enable the
source (:attr:`~carto_edge.connectors.base.TestResult.can_enable`: the test passed and the
credential is not write-capable; ``not_verifiable`` with a passing test may poll). A refused
test is logged with the source id, the ``read_only`` status and the names of the failed checks
(never their detail text, which may quote what the credential sees), counted in
``carto_edge_connector_errors_total`` and the source's health, and retried with the same capped
backoff as a failed read, so a corrected grant lets the source start without a restart. The
last result per source is kept for the gateway (:meth:`PollScheduler.test_results`).

Then each task loops:

1. Wait while :meth:`~carto_edge.pipeline.ingestor.IngestorLike.backpressure` is not ``OK``
   (spec 8.5: "when the buffer is above 80%, pull connectors slow down").
2. Read from the committed cursor (:class:`~carto_edge.state.CursorStore`), handing records and
   the connector's cursor checkpoints (ADR 0035) to the ingestor in chunks of
   :data:`CHUNK_RECORDS` on a worker thread (the pipeline is CPU work and must not block the
   event loop). Between chunks the buffer is checked again: above the
   backpressure ratio the read stops early, and a chunk the ingestor refused with
   ``buffer_full`` aborts the read without a flush, because the ingestor wants the source
   re-read from its committed cursor (see :mod:`carto_edge.pipeline.batch`).
3. Flush the ingestor, so every event read is durably buffered and the cursor committed behind
   it (spec 8.1 "a cursor is committed only after the batch is durably in the disk buffer").
4. Record the read in :class:`~carto_edge.pipeline.forward.SourceHealth` with
   ``lag_seconds`` = now minus the last record's ``received_at`` (0 when nothing was read) and
   set ``carto_edge_connector_lag_seconds``.
5. Sleep the source's ``poll_seconds`` (its connector config) or the gateway default.

A failed read (a :class:`~carto_edge.connectors.base.ConnectorError` or any other exception) is
logged by exception type and source id only, never the message (a driver message may quote a
value), counted in ``carto_edge_connector_errors_total`` and in the source's health, and the
task backs off with jittered exponential delays (:func:`carto_edge.net.retry.backoff_delay`)
capped at ten minutes. At most ``gateway.poll_concurrency`` reads run at once. Every wait returns
as soon as the stop event is set; a read in progress stops at the next chunk and flushes what
it read.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

from carto_common.logging import get_logger
from carto_edge.config import SourceConfig
from carto_edge.connectors.base import (
    ConnectorContext,
    ConnectorError,
    ReadConnector,
    ReadOnlyStatus,
    ReadOnlyViolationError,
    TestCheck,
    TestResult,
)
from carto_edge.connectors.registry import build_connector
from carto_edge.metrics import EdgeMetrics
from carto_edge.net.retry import backoff_delay
from carto_edge.pipeline.batch import PULL_SOURCE_TYPES, buffer_refused
from carto_edge.pipeline.forward import MAX_BACKOFF_ATTEMPT, SourceHealth
from carto_edge.pipeline.ingestor import BackpressureState, IngestorLike
from carto_edge.pipeline.model import CursorCheckpoint, ReadItem
from carto_edge.runtime import EdgeRuntime
from carto_edge.state import CursorStore

__all__ = [
    "BACKPRESSURE_WAIT_SECONDS",
    "CHUNK_RECORDS",
    "ERROR_BACKOFF_BASE_SECONDS",
    "ERROR_BACKOFF_CAP_SECONDS",
    "PULL_SOURCE_TYPES",
    "PollReport",
    "PollScheduler",
]

CHUNK_RECORDS: Final = 500
BACKPRESSURE_WAIT_SECONDS: Final = 1.0
ERROR_BACKOFF_BASE_SECONDS: Final = 5.0
ERROR_BACKOFF_CAP_SECONDS: Final = 600.0

Clock = Callable[[], datetime]
ConnectorBuilder = Callable[[SourceConfig, ConnectorContext], ReadConnector]

log = get_logger(component="carto_edge.scheduler")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PollReport:
    """One read of one source. ``aborted`` means the read ended before the connector was
    exhausted (backpressure, a refused chunk, or shutdown)."""

    source_id: str
    records: int
    lag_seconds: float
    aborted: bool = False


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; returns early, and True, once ``stop`` is set."""
    if stop.is_set():
        return True
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    return stop.is_set()


class PollScheduler:
    """Drives every enabled pull connector of the sources file (module docstring)."""

    def __init__(
        self,
        runtime: EdgeRuntime,
        ingestor: IngestorLike,
        cursors: CursorStore,
        context: ConnectorContext,
        health: SourceHealth,
        *,
        metrics: EdgeMetrics,
        default_poll_seconds: float,
        clock: Clock | None = None,
        connector_factory: ConnectorBuilder = build_connector,
        chunk_records: int = CHUNK_RECORDS,
        backpressure_wait_seconds: float = BACKPRESSURE_WAIT_SECONDS,
        error_backoff_base_seconds: float = ERROR_BACKOFF_BASE_SECONDS,
        error_backoff_cap_seconds: float = ERROR_BACKOFF_CAP_SECONDS,
    ) -> None:
        if default_poll_seconds <= 0:
            msg = "default_poll_seconds must be positive"
            raise ValueError(msg)
        if chunk_records < 1:
            msg = "chunk_records must be at least 1"
            raise ValueError(msg)
        if not 0 < error_backoff_base_seconds <= error_backoff_cap_seconds:
            msg = "the error backoff base must be positive and at most the cap"
            raise ValueError(msg)
        self._runtime = runtime
        self._ingestor = ingestor
        self._cursors = cursors
        self._health = health
        self._metrics = metrics
        self._default_poll = default_poll_seconds
        self._clock = clock if clock is not None else _utc_now
        self._chunk = chunk_records
        self._pressure_wait = backpressure_wait_seconds
        self._error_base = error_backoff_base_seconds
        self._error_cap = error_backoff_cap_seconds
        self._concurrency = runtime.settings.gateway.poll_concurrency
        self._sources: dict[str, SourceConfig] = {}
        self._connectors: dict[str, ReadConnector] = {}
        self._test_results: dict[str, TestResult] = {}
        for source in runtime.sources.sources:
            if not source.enabled or source.type not in PULL_SOURCE_TYPES:
                continue
            try:
                connector = connector_factory(source, context)
            except ConnectorError as exc:
                log.warning(
                    "scheduler.source_skipped", source_id=source.id, error=type(exc).__name__
                )
                # The heartbeat must say "failing", not "ok with no data" (spec 8.1 health).
                self._health.record_error(source.id, self._clock())
                continue
            self._sources[source.id] = source
            self._connectors[source.id] = connector

    def __repr__(self) -> str:
        return f"PollScheduler(sources={sorted(self._connectors)!r})"

    @property
    def connectors(self) -> Mapping[str, ReadConnector]:
        """The pull connectors by source id (read-only view)."""
        return MappingProxyType(self._connectors)

    def poll_seconds(self, source: SourceConfig) -> float:
        """``poll_seconds`` from the source's connector config when it is a positive number,
        else the gateway default."""
        value = source.config.get("poll_seconds")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return self._default_poll
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            return self._default_poll
        return seconds

    def test_results(self) -> dict[str, TestResult]:
        """The last ``test()`` result of every source tested so far."""
        return dict(self._test_results)

    # -- the read-only gate ---------------------------------------------------------------------

    async def check(self, source_id: str) -> TestResult:
        """Run the connector's ``test()`` and keep the result. An exception from the test is a
        failed result whose only check names the exception type."""
        connector = self._connectors[source_id]
        try:
            result = await connector.test()
        except Exception as exc:  # a test that cannot run cannot enable the source
            name = type(exc).__name__
            result = TestResult(
                ok=False,
                read_only=ReadOnlyStatus.NOT_VERIFIABLE,
                checks=(TestCheck(f"test_raised:{name}", ok=False),),
                problems=(f"the connector test raised {name}",),
            )
        self._test_results[source_id] = result
        if result.can_enable:
            log.info(
                "scheduler.source_enabled", source_id=source_id, read_only=result.read_only.value
            )
        return result

    def _refused(self, source_id: str, result: TestResult, attempt: int, delay: float) -> None:
        self._metrics.inc("carto_edge_connector_errors_total", source_id=source_id)
        self._health.record_error(source_id, self._clock())
        log.warning(
            "scheduler.source_not_enabled",
            source_id=source_id,
            ok=result.ok,
            read_only=result.read_only.value,
            failed_checks=[check.name for check in result.checks if not check.ok],
            attempt=attempt,
            retry_in_seconds=round(delay, 3),
        )

    async def _wait_until_enabled(
        self, source_id: str, stop: asyncio.Event, gate: asyncio.Semaphore
    ) -> bool:
        """Test until the source may be read; False when ``stop`` was set first."""
        attempt = 0
        while not stop.is_set():
            async with gate:
                result = await self.check(source_id)
            if result.can_enable:
                return True
            attempt = min(attempt + 1, MAX_BACKOFF_ATTEMPT)
            delay = backoff_delay(attempt, base=self._error_base, cap=self._error_cap)
            self._refused(source_id, result, attempt, delay)
            if await _wait(stop, delay):
                return False
        return False

    # -- one read -------------------------------------------------------------------------------

    async def poll_once(self, source_id: str) -> PollReport:
        """Read ``source_id`` once from its committed cursor (steps 2 to 4 of the module
        docstring), testing it first unless its last test enabled it. Raises
        :class:`~carto_edge.connectors.base.ReadOnlyViolationError` for a write-capable
        credential and :class:`~carto_edge.connectors.base.ConnectorError` for another refused
        test; exceptions from the connector or the ingestor propagate."""
        last = self._test_results.get(source_id)
        if last is None or not last.can_enable:
            result = await self.check(source_id)
            if not result.can_enable:
                self._refused(source_id, result, 1, 0.0)
                if result.read_only is ReadOnlyStatus.WRITE_CAPABLE:
                    msg = f"source {source_id!r} has a write-capable credential; it is not read"
                    raise ReadOnlyViolationError(msg)
                msg = f"source {source_id!r} did not pass its connector test; it is not read"
                raise ConnectorError(msg)
        return await self._poll(source_id, None)

    async def _poll(self, source_id: str, stop: asyncio.Event | None) -> PollReport:
        source = self._sources[source_id]
        connector = self._connectors[source_id]
        cursor = await asyncio.to_thread(self._cursors.get, source_id)
        records = 0
        last_received: datetime | None = None
        aborted = False
        flush = True
        chunk: list[ReadItem] = []
        iterator = connector.read(cursor)
        try:
            async for item in iterator:
                chunk.append(item)
                if not isinstance(item, CursorCheckpoint):  # checkpoints carry no record
                    records += 1
                    last_received = item.received_at
                if len(chunk) < self._chunk:
                    continue
                outcome = await asyncio.to_thread(self._ingestor.ingest, chunk)
                chunk = []
                if buffer_refused(outcome):
                    aborted, flush = True, False
                    log.warning(
                        "scheduler.read_aborted", source_id=source.id, reason="buffer_refused"
                    )
                    break
                if (stop is not None and stop.is_set()) or (
                    self._ingestor.backpressure() is not BackpressureState.OK
                ):
                    aborted = True
                    break
        finally:
            await _close_iterator(iterator)
        if chunk:
            outcome = await asyncio.to_thread(self._ingestor.ingest, chunk)
            if buffer_refused(outcome):
                aborted, flush = True, False
        if flush:
            await asyncio.to_thread(self._ingestor.flush)
        now = self._clock()
        lag = 0.0
        if last_received is not None:
            lag = max(0.0, (now - last_received).total_seconds())
        self._health.record_read(source.id, records, lag, now)
        self._metrics.set("carto_edge_connector_lag_seconds", lag, source_id=source.id)
        return PollReport(source.id, records, lag, aborted)

    # -- the loop ---------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """One task per pull source until ``stop`` is set."""
        if not self._connectors:
            await stop.wait()
            return
        gate = asyncio.Semaphore(self._concurrency)
        tasks = [
            asyncio.create_task(self._loop(source_id, stop, gate), name=f"poll:{source_id}")
            for source_id in self._connectors
        ]
        log.info("scheduler.started", sources=len(tasks), concurrency=self._concurrency)
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("scheduler.stopped")

    async def _paused(self, stop: asyncio.Event) -> bool:
        """Wait while the buffer pushes back (spec 8.5); True when ``stop`` was set meanwhile."""
        while self._ingestor.backpressure() is not BackpressureState.OK:
            if await _wait(stop, self._pressure_wait):
                return True
        return stop.is_set()

    async def _loop(self, source_id: str, stop: asyncio.Event, gate: asyncio.Semaphore) -> None:
        source = self._sources[source_id]
        interval = self.poll_seconds(source)
        if not await self._wait_until_enabled(source_id, stop, gate):
            return
        attempt = 0
        while not stop.is_set():
            try:
                if await self._paused(stop):
                    return
                async with gate:
                    if stop.is_set():
                        return
                    await self._poll(source_id, stop)
            except Exception as exc:  # a connector or driver failure must not end the task
                attempt = min(attempt + 1, MAX_BACKOFF_ATTEMPT)
                delay = backoff_delay(attempt, base=self._error_base, cap=self._error_cap)
                self._metrics.inc("carto_edge_connector_errors_total", source_id=source_id)
                self._health.record_error(source_id, self._clock())
                log.warning(
                    "scheduler.read_failed",
                    source_id=source_id,
                    error=type(exc).__name__,
                    attempt=attempt,
                    retry_in_seconds=round(delay, 3),
                )
                if await _wait(stop, delay):
                    return
                continue
            attempt = 0
            if await _wait(stop, interval):
                return

    async def close(self) -> None:
        """Close every connector; a failing close is logged by type and does not stop the rest."""
        for source_id, connector in self._connectors.items():
            try:
                await connector.close()
            except Exception as exc:  # closing is best effort at shutdown
                log.warning("scheduler.close_failed", source_id=source_id, error=type(exc).__name__)


async def _close_iterator(iterator: AsyncIterator[ReadItem]) -> None:
    """Close an async generator left before its end so its ``finally`` blocks run now."""
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()
