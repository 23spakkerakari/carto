"""``carto-edge bench --path gateway``: the full gateway ingest path (spec 17; ADR 0028).

The ``pipeline`` benchmark (:mod:`carto_edge.cli.bench`) measures parsing, classification,
tokenization and the reveal vault in analyzer mode. The gateway does more for every record, and
this benchmark runs all of it in one process, the way ``edge-gateway`` does:

- a ``gateway``-mode runtime: field statistics updated on every record, the spec's quarantine
  rule, templates persisted under the state directory;
- the real :class:`~carto_edge.pipeline.batch.Ingestor`, fed in chunks of
  :data:`~carto_edge.gateway.scheduler.CHUNK_RECORDS` records as the poll scheduler feeds it,
  with the reveal vault, batch sealing, the SQLite :class:`~carto_edge.pipeline.buffer.DiskBuffer`
  (``synchronous=FULL``) and cursor commits;
- the real :class:`~carto_edge.pipeline.forward.Forwarder` on its own thread, posting every
  stored zstd batch to a stub core that answers 202 without reading it. Network and TLS to core
  are the only parts of the edge's work left out.

Before the measurement, one pass over every record warms the edge up (fields leave quarantine,
the PII model loads, templates form) and the buffer is emptied. Then records are ingested for
``--seconds``; afterwards the partial batches are sealed and the forwarder is given up to
:data:`DRAIN_TIMEOUT_SECONDS` to empty the buffer. The sustained figure is the events the stub
core acknowledged over the ingest time plus that drain time, so a forwarder that cannot keep up
lowers it. Reading the input files is outside the measurement.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import httpx
from pydantic import ValidationError

from carto_common.logging import get_logger
from carto_edge.cli.analyze import EXIT_FAILURE, EXIT_USAGE, AnalyzeError, describe, upload_sources
from carto_edge.cli.bench import TARGET_EVENTS_PER_SECOND, load_records, percentiles
from carto_edge.config import ConfigError, EdgeSettings, load_sources_file
from carto_edge.connectors.base import ConnectorError
from carto_edge.gateway.scheduler import CHUNK_RECORDS
from carto_edge.keys import KeyManagementError
from carto_edge.metrics import default_metrics
from carto_edge.pipeline.batch import Ingestor
from carto_edge.pipeline.buffer import DiskBuffer
from carto_edge.pipeline.forward import Forwarder
from carto_edge.pipeline.model import RawRecord
from carto_edge.pipeline.pii import PiiDetector
from carto_edge.runtime import EdgeRuntime, build_runtime
from carto_edge.state import CursorStore

__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "STUB_CORE_URL",
    "GatewayBenchResult",
    "bench_gateway",
    "format_gateway_bench",
]

DRAIN_TIMEOUT_SECONDS: Final = 600.0
STUB_CORE_URL: Final = "https://core.bench.invalid"
"""Never resolved: the stub transport answers every request in process."""
_POLL_SECONDS: Final = 0.02

log = get_logger(component="carto_edge.cli.bench_gateway")


@dataclass(frozen=True, slots=True)
class GatewayBenchResult:
    sources: int
    records_loaded: int
    warmup_seconds: float
    ingest_seconds: float
    drain_seconds: float
    processed: int
    events: int
    forwarded: int
    batches: int
    dropped: dict[str, int]
    p50_us: float
    p95_us: float
    p99_us: float
    drained: bool

    @property
    def ingest_events_per_second(self) -> float:
        """Events into the buffer per second of ingest (what the readers see)."""
        return self.events / self.ingest_seconds if self.ingest_seconds > 0 else 0.0

    @property
    def sustained_events_per_second(self) -> float:
        """Events acknowledged by core per second of ingest plus drain (spec 17)."""
        total = self.ingest_seconds + self.drain_seconds
        return self.forwarded / total if total > 0 else 0.0

    @property
    def target_met(self) -> bool:
        return self.drained and self.sustained_events_per_second >= TARGET_EVENTS_PER_SECOND


def _accept(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(202)


def _chunks(records: Sequence[RawRecord]) -> list[Sequence[RawRecord]]:
    return [records[i : i + CHUNK_RECORDS] for i in range(0, len(records), CHUNK_RECORDS)]


def _measure(
    runtime: EdgeRuntime, settings: EdgeSettings, records: list[RawRecord], seconds: float
) -> GatewayBenchResult:
    chunks = _chunks(records)
    buffer = DiskBuffer(
        settings.buffer_db_file, settings.buffer.max_bytes, settings.buffer.backpressure_ratio
    )
    cursors = CursorStore(settings.cursors_db_file)
    client = httpx.Client(base_url=STUB_CORE_URL, transport=httpx.MockTransport(_accept))
    stop = threading.Event()
    thread: threading.Thread | None = None
    try:
        metrics = default_metrics()
        ingestor = Ingestor(runtime, buffer, cursors, link=settings.core, metrics=metrics)
        forwarder = Forwarder(buffer, client, metrics=metrics)

        started = time.perf_counter()
        for chunk in chunks:
            ingestor.ingest(chunk)
        ingestor.flush()
        forwarder.drain()
        warmup_seconds = time.perf_counter() - started
        sent_before = metrics.get("carto_edge_batches_sent_total")

        thread = threading.Thread(
            target=forwarder.run, args=(stop,), name="bench-forwarder", daemon=True
        )
        thread.start()
        latencies: list[float] = []
        dropped: Counter[str] = Counter()
        processed = 0
        events = 0
        index = 0
        start = time.perf_counter()
        deadline = start + seconds
        while True:
            chunk = chunks[index]
            index = index + 1 if index + 1 < len(chunks) else 0
            before = time.perf_counter()
            outcome = ingestor.ingest(chunk)
            after = time.perf_counter()
            if outcome.records:
                latencies.append((after - before) / outcome.records)
            processed += outcome.records
            events += outcome.events
            dropped.update(outcome.dropped)
            if after >= deadline:
                break
        ingestor.flush()
        ingest_end = time.perf_counter()
        drain_deadline = ingest_end + DRAIN_TIMEOUT_SECONDS
        while buffer.depth() > 0 and time.perf_counter() < drain_deadline:
            time.sleep(_POLL_SECONDS)
        end = time.perf_counter()
        stop.set()
        thread.join(timeout=30)
        pending = buffer.pending_events()
        p50, p95, p99 = percentiles(latencies)
        return GatewayBenchResult(
            sources=len({record.source_id for record in records}),
            records_loaded=len(records),
            warmup_seconds=warmup_seconds,
            ingest_seconds=ingest_end - start,
            drain_seconds=end - ingest_end,
            processed=processed,
            events=events,
            forwarded=max(0, events - pending),
            batches=int(metrics.get("carto_edge_batches_sent_total") - sent_before),
            dropped=dict(dropped.most_common()),
            p50_us=p50,
            p95_us=p95,
            p99_us=p99,
            drained=pending == 0 and buffer.parked() == 0,
        )
    finally:
        stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=30)
        client.close()
        cursors.close()
        buffer.close()


def bench_gateway(
    config: Path,
    input_dir: Path,
    *,
    seconds: float,
    max_records: int,
    detector: PiiDetector | None = None,
    as_of: datetime | None = None,
) -> GatewayBenchResult:
    """Run the gateway benchmark; raises :class:`~carto_edge.cli.analyze.AnalyzeError` with the
    exit code. ``detector`` replaces the configured PII detector (tests only);
    ``as_of`` is the time event times are checked against (ADR 0036, ``--as-of``), by default
    the wall clock."""
    if seconds <= 0 or max_records < 1:
        msg = "--seconds must be positive and --max-records at least 1"
        raise AnalyzeError(msg, EXIT_USAGE)
    try:
        sources_file = load_sources_file(config)
    except ConfigError as exc:
        raise AnalyzeError(str(exc), EXIT_USAGE) from exc
    if not input_dir.is_dir():
        msg = f"--input {input_dir} is not a directory"
        raise AnalyzeError(msg, EXIT_USAGE)
    sources = upload_sources(sources_file, input_dir.resolve())
    with tempfile.TemporaryDirectory(prefix="carto-edge-bench-gw-") as tmp:
        try:
            settings = EdgeSettings(state_dir=Path(tmp) / "state")
        except ValidationError as exc:
            msg = f"invalid settings: {describe(exc)}"
            raise AnalyzeError(msg, EXIT_USAGE) from exc
        try:
            runtime = build_runtime(
                settings,
                sources_file,
                mode="gateway",
                detector=detector,
                init_local_keys=True,
                range_clock=(lambda: as_of) if as_of is not None else None,
            )
        except KeyManagementError as exc:
            msg = f"keys unusable: {describe(exc)}"
            raise AnalyzeError(msg, EXIT_FAILURE) from exc
        try:
            records = load_records(runtime, sources, max_records)
            if not records:
                msg = "the inputs hold no record to benchmark"
                raise AnalyzeError(msg, EXIT_FAILURE)
            result = _measure(runtime, settings, records, seconds)
        except ConnectorError as exc:
            msg = f"reading the inputs failed: {describe(exc)}"
            raise AnalyzeError(msg, EXIT_FAILURE) from exc
        finally:
            runtime.close()  # before the directory goes: Windows keeps open SQLite files
    log.info(
        "bench.gateway_complete",
        records=result.processed,
        events=result.events,
        forwarded=result.forwarded,
        sustained_events_per_second=round(result.sustained_events_per_second, 1),
    )
    return result


def format_gateway_bench(result: GatewayBenchResult) -> list[str]:
    dropped = ", ".join(f"{reason} {count:,}" for reason, count in result.dropped.items())
    verdict = "met" if result.target_met else "NOT met"
    drain = "" if result.drained else " (buffer NOT emptied: forwarder fell behind)"
    return [
        (
            f"carto-edge bench (gateway path): {result.records_loaded:,} records from "
            f"{result.sources} sources (warm-up pass {result.warmup_seconds:.1f} s, outside the "
            "measurement)"
        ),
        (
            f"  ingest          {result.ingest_seconds:.1f} s, "
            f"then drain {result.drain_seconds:.1f} s{drain}"
        ),
        (
            f"  records         {result.processed:,} ingested, {result.events:,} events, "
            f"{result.ingest_events_per_second:,.0f} events/s into the buffer"
        ),
        (
            f"  forwarded       {result.forwarded:,} events in {result.batches:,} batches, "
            f"{result.sustained_events_per_second:,.0f} events/s sustained "
            "(acknowledged by a stub core; no network or TLS)"
        ),
        (
            f"  latency         p50 {result.p50_us:,.0f} us, p95 {result.p95_us:,.0f} us, "
            f"p99 {result.p99_us:,.0f} us per record (ingest call, chunk average)"
        ),
        f"  dropped         {sum(result.dropped.values()):,}{f' ({dropped})' if dropped else ''}",
        f"  spec 17 target  {TARGET_EVENTS_PER_SECOND:,} events/s sustained: {verdict}",
    ]
