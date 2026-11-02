"""``carto-edge bench``: edge pipeline throughput (spec 17: 2,000 events/s sustained; M1 plan).

    carto-edge bench --input DIR --config FILE [--seconds 30] [--max-records 50000]
                     [--path pipeline|gateway]

``--path gateway`` runs the full gateway ingest path instead (:mod:`carto_edge.cli.bench_gateway`,
ADR 0028); what follows describes the default ``pipeline`` path.

Builds an ``analyze``-mode runtime with a throwaway local key in a temporary state directory,
loads up to ``--max-records`` raw records, an equal share per source, from the configuration's
upload sources (paths relative to ``--input``) into memory, runs pass 1 (``observe_only``) over
them once and decides every field (which loads the PII model) so every field is classified as
the analyzer would, then cycles ``process`` over the list until
``--seconds`` have passed, writing the reveal vault every :data:`VAULT_CHUNK` results as the
gateway and the analyzer do. Reading files is outside the measurement.

Reported: events/s and records/s over the wall time of the loop (pipeline plus vault writes),
p50/p95/p99 latency of one ``process`` call in microseconds, drops by reason, and whether the
spec 17 target was met. The PII detector is the configured one (Presidio unless disabled),
because that is what an edge runs. The figure is informative on shared CI runners; the
reference-node figure is a founder measurement. Exit 0 whenever the run completed.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from carto_common.logging import get_logger
from carto_edge.cli.analyze import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    AnalyzeError,
    describe,
    emit,
    parse_as_of,
    upload_sources,
)
from carto_edge.config import ConfigError, EdgeSettings, SourceConfig, load_sources_file
from carto_edge.connectors.base import ConnectorError, InvalidConfigError, ReadConnector
from carto_edge.connectors.registry import build_connector
from carto_edge.keys import KeyManagementError
from carto_edge.pipeline.model import CursorCheckpoint, PipelineResult, RawRecord
from carto_edge.pipeline.pii import PiiDetector
from carto_edge.runtime import EdgeRuntime, build_connector_context, build_runtime

__all__ = [
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_SECONDS",
    "TARGET_EVENTS_PER_SECOND",
    "VAULT_CHUNK",
    "BenchResult",
    "add_bench_arguments",
    "bench",
    "format_bench",
    "load_records",
    "percentiles",
    "run_bench",
]

TARGET_EVENTS_PER_SECOND: Final = 2_000
DEFAULT_SECONDS: Final = 30.0
DEFAULT_MAX_RECORDS: Final = 50_000
VAULT_CHUNK: Final = 1000
_MICROS: Final = 1_000_000

log = get_logger(component="carto_edge.cli.bench")


@dataclass(frozen=True, slots=True)
class BenchResult:
    sources: int
    records_loaded: int
    observe_seconds: float
    classify_seconds: float
    seconds: float
    processed: int
    events: int
    dropped: dict[str, int]
    p50_us: float
    p95_us: float
    p99_us: float

    @property
    def events_per_second(self) -> float:
        return self.events / self.seconds if self.seconds > 0 else 0.0

    @property
    def records_per_second(self) -> float:
        return self.processed / self.seconds if self.seconds > 0 else 0.0

    @property
    def target_met(self) -> bool:
        return self.events_per_second >= TARGET_EVENTS_PER_SECOND


async def _collect(connector: ReadConnector, limit: int) -> list[RawRecord]:
    records: list[RawRecord] = []
    stream = connector.read(None)
    try:
        async for record in stream:
            if isinstance(record, CursorCheckpoint):  # no cursor in a benchmark
                continue
            records.append(record)
            if len(records) >= limit:
                break
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
        await connector.close()
    return records


def load_records(
    runtime: EdgeRuntime, sources: Sequence[SourceConfig], limit: int
) -> list[RawRecord]:
    """Up to ``limit`` records, an equal share from every source (each in its own order), so
    the mix covers every format the configuration reads, not just the first files."""
    context = build_connector_context(runtime)
    records: list[RawRecord] = []
    share = -(-limit // len(sources)) if sources else 0
    for source in sources:
        remaining = min(share, limit - len(records))
        if remaining <= 0:
            break
        try:
            connector = build_connector(source, context)
        except InvalidConfigError as exc:
            raise AnalyzeError(str(exc), EXIT_USAGE) from exc
        records.extend(asyncio.run(_collect(connector, remaining)))
    return records


def percentiles(latencies: list[float]) -> tuple[float, float, float]:
    if not latencies:
        return 0.0, 0.0, 0.0
    if len(latencies) == 1:
        only = latencies[0] * _MICROS
        return only, only, only
    cuts = statistics.quantiles(latencies, n=100, method="inclusive")
    return cuts[49] * _MICROS, cuts[94] * _MICROS, cuts[98] * _MICROS


def _measure(
    runtime: EdgeRuntime, records: list[RawRecord], seconds: float, sources: int
) -> BenchResult:
    started = time.perf_counter()
    for record in records:
        runtime.pipeline.observe_only(record)
    observe_seconds = time.perf_counter() - started
    runtime.pipeline.reset_parsers()  # as the analyzer does between its passes
    started = time.perf_counter()
    # Warm-up outside the measurement: decide every field once (the PII model loads and the
    # samples are checked here, as at the end of the analyzer's pass 1), so the loop measures
    # the steady state an edge runs in.
    runtime.classifier.bundle_fields()
    classify_seconds = time.perf_counter() - started
    process = runtime.pipeline.process
    latencies: list[float] = []
    pending: list[PipelineResult] = []
    dropped: Counter[str] = Counter()
    events = 0
    index = 0
    total = len(records)
    start = time.perf_counter()
    deadline = start + seconds
    while True:
        record = records[index]
        index = index + 1 if index + 1 < total else 0
        before = time.perf_counter()
        result = process(record)
        after = time.perf_counter()
        latencies.append(after - before)
        if result.event is None:
            dropped[result.dropped_reason or "unknown"] += 1
        else:
            events += 1
        pending.append(result)
        if len(pending) >= VAULT_CHUNK:
            runtime.write_vault_entries(pending)
            pending.clear()
        if after >= deadline:
            break
    if pending:
        runtime.write_vault_entries(pending)
    elapsed = time.perf_counter() - start
    p50, p95, p99 = percentiles(latencies)
    return BenchResult(
        sources=sources,
        records_loaded=total,
        observe_seconds=observe_seconds,
        classify_seconds=classify_seconds,
        seconds=elapsed,
        processed=len(latencies),
        events=events,
        dropped=dict(dropped.most_common()),
        p50_us=p50,
        p95_us=p95,
        p99_us=p99,
    )


def bench(
    config: Path,
    input_dir: Path,
    *,
    seconds: float = DEFAULT_SECONDS,
    max_records: int = DEFAULT_MAX_RECORDS,
    detector: PiiDetector | None = None,
    as_of: datetime | None = None,
) -> BenchResult:
    """Run the benchmark; raises :class:`~carto_edge.cli.analyze.AnalyzeError` with the exit
    code. ``detector`` replaces the configured PII detector (tests only);
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
    with tempfile.TemporaryDirectory(prefix="carto-edge-bench-") as tmp:
        try:
            settings = EdgeSettings(state_dir=Path(tmp) / "state")
        except ValidationError as exc:
            msg = f"invalid settings: {describe(exc)}"
            raise AnalyzeError(msg, EXIT_USAGE) from exc
        try:
            runtime = build_runtime(
                settings,
                sources_file,
                mode="analyze",
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
            result = _measure(runtime, records, seconds, len(sources))
        except ConnectorError as exc:
            msg = f"reading the inputs failed: {describe(exc)}"
            raise AnalyzeError(msg, EXIT_FAILURE) from exc
        finally:
            runtime.close()  # before the directory goes: Windows keeps open SQLite files
    log.info(
        "bench.complete",
        records=result.processed,
        events=result.events,
        seconds=round(result.seconds, 3),
        events_per_second=round(result.events_per_second, 1),
    )
    return result


def format_bench(result: BenchResult) -> list[str]:
    dropped = ", ".join(f"{reason} {count:,}" for reason, count in result.dropped.items())
    verdict = "met" if result.target_met else "NOT met"
    return [
        (
            f"carto-edge bench: {result.records_loaded:,} records from {result.sources} sources "
            f"(pass 1 observe {result.observe_seconds:.1f} s, "
            f"classify {result.classify_seconds:.1f} s, both outside the measurement)"
        ),
        f"  duration        {result.seconds:.1f} s",
        (
            f"  records         {result.processed:,} processed, "
            f"{result.records_per_second:,.0f} records/s"
        ),
        (
            f"  events          {result.events:,} emitted, "
            f"{result.events_per_second:,.0f} events/s (pipeline and reveal vault writes)"
        ),
        (
            f"  latency         p50 {result.p50_us:,.0f} us, p95 {result.p95_us:,.0f} us, "
            f"p99 {result.p99_us:,.0f} us per record (process only)"
        ),
        f"  dropped         {sum(result.dropped.values()):,}{f' ({dropped})' if dropped else ''}",
        f"  spec 17 target  {TARGET_EVENTS_PER_SECOND:,} events/s sustained: {verdict}",
    ]


def add_bench_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input", type=Path, required=True, help="Directory the upload paths are relative to"
    )
    parser.add_argument("--config", type=Path, required=True, help="Analyzer sources file (YAML)")
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help=f"Measurement time (default {DEFAULT_SECONDS:.0f})",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=DEFAULT_MAX_RECORDS,
        help=f"Records loaded into memory (default {DEFAULT_MAX_RECORDS})",
    )
    parser.add_argument(
        "--path",
        choices=("pipeline", "gateway"),
        default="pipeline",
        help="pipeline: parse to vault in analyzer mode (default); gateway: the full ingest path "
        "with buffer and forwarder (ADR 0028)",
    )
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=None,
        help="Check event times against this ISO 8601 time instead of now (ADR 0036)",
    )


def run_bench(args: argparse.Namespace) -> int:
    from carto_edge.cli.bench_gateway import bench_gateway, format_gateway_bench  # noqa: PLC0415

    try:
        if args.path == "gateway":
            lines = format_gateway_bench(
                bench_gateway(
                    args.config,
                    args.input,
                    seconds=args.seconds,
                    max_records=args.max_records,
                    as_of=args.as_of,
                )
            )
        else:
            lines = format_bench(
                bench(
                    args.config,
                    args.input,
                    seconds=args.seconds,
                    max_records=args.max_records,
                    as_of=args.as_of,
                )
            )
    except AnalyzeError as exc:
        print(f"carto-edge bench: {exc}", file=sys.stderr)
        return exc.code
    for line in lines:
        emit(line)
    return EXIT_OK
