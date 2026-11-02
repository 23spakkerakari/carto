"""``carto-edge analyze``: the offline analyzer (spec 4.1, 8.1.1; ADR 0017, 0022, 0024, 0025).

    carto-edge analyze --config FILE --input DIR --out DIR [--state-dir DIR] [--tenant-id ID]
                       [--locator-map] [--producer TEXT] [--as-of TIME]

Every enabled source of the configuration must be an ``upload`` source; its ``paths`` are
resolved against ``--input`` (the connector's ``base_dir``). The runtime is built in
``analyze`` mode with a local-KMS tenant key created under the state directory on first use
and reused afterwards (ADR 0025; default ``<out>.edge-state`` next to the bundle), so re-runs
over one state directory produce the same tokens, template ids and event ids.

Two passes (ADR 0017): pass 1 feeds every record to ``pipeline.observe_only`` (statistics and
templates, nothing emitted); pass 2 runs ``pipeline.process``, writes the ``raw`` form values to
the reveal vault in chunks of :data:`CHUNK_SIZE` and streams the events into the bundle. The
connectors' async generators are driven with :func:`asyncio.run` over a helper that hands
records over in chunks, so memory stays flat for inputs of any size.

Records whose time lies outside the plausible range (ADR 0036) are checked against the run time:
older than event retention plus one day they are dropped (``too_old``), more than one day ahead
they keep the ingest time. ``--as-of`` names another reference time, for exported data analyzed
long after it was exported and for the simulator's fixed-date scenarios
(``carto-sim as-of``); the manifest notes it. The manifest counts both cases.

Exit codes: 0 the bundle was written, 1 the run failed (keys unusable, an input over the spec
8.1.1 limits, a write failed), 2 a usage or configuration error. The summary names counts and
paths, never a value (spec 2.3 invariant 7). :func:`analyze` is the importable core the tests
call; :func:`run_analyze` is the command handler.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import metadata
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from carto_common.logging import get_logger
from carto_edge.bundle import BundleWriteError, BundleWriter, default_locator_map_path
from carto_edge.config import (
    ConfigError,
    EdgeSettings,
    SourceConfig,
    SourcesFile,
    SourceType,
    load_sources_file,
)
from carto_edge.connectors.base import ConnectorError, InvalidConfigError, ReadConnector
from carto_edge.connectors.registry import build_connector
from carto_edge.keys import KeyManagementError
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord
from carto_edge.pipeline.pii import PiiDetector
from carto_edge.runtime import EdgeRuntime, build_connector_context, build_runtime
from carto_schema.bundle import BundleManifest

__all__ = [
    "CHUNK_SIZE",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_USAGE",
    "PRODUCER_NAME",
    "AnalyzeError",
    "AnalyzeResult",
    "add_analyze_arguments",
    "analyze",
    "default_producer",
    "default_state_dir",
    "drain",
    "emit",
    "format_summary",
    "parse_as_of",
    "run_analyze",
    "upload_sources",
]

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
EXIT_USAGE: Final = 2
CHUNK_SIZE: Final = 1000
PRODUCER_NAME: Final = "carto-edge analyze"
STATE_DIR_SUFFIX: Final = ".edge-state"
_DISTRIBUTION: Final = "carto-edge"
_FALLBACK_VERSION: Final = "0.1.0"
_MAX_MESSAGE: Final = 300

log = get_logger(component="carto_edge.cli.analyze")


class AnalyzeError(Exception):
    """The run cannot proceed; ``code`` is the exit code, the message never carries a value."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    """What a successful run wrote."""

    bundle_dir: Path
    manifest: BundleManifest
    locator_map: Path | None
    state_dir: Path
    dropped: dict[str, int]
    elapsed_seconds: float


def emit(text: str) -> None:
    """Print without failing on characters the console encoding cannot show (ADR 0007)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding))


def describe(exc: BaseException) -> str:
    """Class name plus the first line of the message, bounded."""
    text = str(exc)
    first_line = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first_line[:_MAX_MESSAGE]}"


def default_producer() -> str:
    """``carto-edge analyze <version of the installed distribution>``."""
    try:
        version = metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        version = _FALLBACK_VERSION
    return f"{PRODUCER_NAME} {version}"


def default_state_dir(out_dir: Path) -> Path:
    """``<out>.edge-state`` next to the bundle directory (ADR 0025)."""
    resolved = out_dir.resolve()
    return resolved.parent / f"{resolved.name}{STATE_DIR_SUFFIX}"


def upload_sources(sources: SourcesFile, input_dir: Path) -> list[SourceConfig]:
    """The enabled sources with ``base_dir`` set to ``input_dir``; any enabled source that is
    not an upload source is a usage error (the analyzer reads exported files only)."""
    enabled = [source for source in sources.sources if source.enabled]
    others = [source.id for source in enabled if source.type is not SourceType.UPLOAD]
    if others:
        msg = (
            "the analyzer reads exported files only; these enabled sources are not of type "
            f"upload: {', '.join(others)}"
        )
        raise AnalyzeError(msg, EXIT_USAGE)
    if not enabled:
        msg = "the configuration has no enabled upload source"
        raise AnalyzeError(msg, EXIT_USAGE)
    base = str(input_dir)
    return [
        source.model_copy(update={"config": {**source.config, "base_dir": base}})
        for source in enabled
    ]


async def _drain_async(
    connector: ReadConnector, handle: Callable[[list[RawRecord]], None], chunk_size: int
) -> int:
    chunk: list[RawRecord] = []
    total = 0
    try:
        async for record in connector.read(None):
            if isinstance(record, CursorCheckpoint):  # the analyzer keeps no cursor
                continue
            chunk.append(record)
            if len(chunk) >= chunk_size:
                handle(chunk)
                total += len(chunk)
                chunk = []
        if chunk:
            handle(chunk)
            total += len(chunk)
    finally:
        await connector.close()
    return total


def drain(
    connector: ReadConnector,
    handle: Callable[[list[RawRecord]], None],
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """Read the whole source once, handing records over in chunks (at most ``chunk_size`` at a
    time, so memory stays flat), then close the connector; returns the record count. The
    handler may stop the read early by raising."""
    return asyncio.run(_drain_async(connector, handle, chunk_size))


def _check_paths(input_dir: Path, out_dir: Path, state_dir: Path) -> None:
    if not input_dir.is_dir():
        msg = f"--input {input_dir} is not a directory"
        raise AnalyzeError(msg, EXIT_USAGE)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        msg = f"--out {out_dir} exists and is not empty; choose a new bundle directory"
        raise AnalyzeError(msg, EXIT_USAGE)
    if state_dir == out_dir or state_dir.is_relative_to(out_dir):
        msg = "--state-dir must not be inside the bundle directory (it holds the key and vault)"
        raise AnalyzeError(msg, EXIT_USAGE)


def _settings(state_dir: Path, tenant_id: str | None) -> EdgeSettings:
    try:
        if tenant_id is None:
            return EdgeSettings(state_dir=state_dir)
        return EdgeSettings(state_dir=state_dir, tenant_id=tenant_id)
    except ValidationError as exc:
        msg = f"invalid settings: {describe(exc)}"
        raise AnalyzeError(msg, EXIT_USAGE) from exc


def _connectors(runtime: EdgeRuntime, sources: Sequence[SourceConfig]) -> list[ReadConnector]:
    context = build_connector_context(runtime)
    try:
        return [build_connector(source, context) for source in sources]
    except InvalidConfigError as exc:
        raise AnalyzeError(str(exc), EXIT_USAGE) from exc


def _observe(runtime: EdgeRuntime, chunk: list[RawRecord]) -> None:
    for record in chunk:
        runtime.pipeline.observe_only(record)


def _emit_chunk(
    runtime: EdgeRuntime, writer: BundleWriter, source_id: str, chunk: list[RawRecord]
) -> None:
    results = [runtime.pipeline.process(record) for record in chunk]
    runtime.write_vault_entries(results)
    for result in results:
        writer.add(result, source_id)


def parse_as_of(text: str) -> datetime:
    """``--as-of``: an ISO 8601 time with an offset (``2026-10-08T00:00:00Z``), or a date, read
    as midnight UTC at its start. Raises ``ValueError`` naming the expected forms."""
    value = text.strip()
    try:
        if len(value) == len("2026-10-08"):
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        msg = "--as-of must be an ISO 8601 date or a time with an offset"
        raise ValueError(msg) from None
    if parsed.tzinfo is None:
        msg = "--as-of needs an offset (for example 2026-10-08T00:00:00Z)"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def _notes(runtime: EdgeRuntime, as_of: datetime | None = None) -> list[str]:
    backend = getattr(runtime.detector, "backend", type(runtime.detector).__name__)
    notes = [
        f"PII detector: {backend}.",
        (
            "Two-pass analysis (ADR 0017): every field was classified with the statistics of the "
            "whole input."
        ),
    ]
    if as_of is not None:
        notes.append(
            f"Event times were checked against {as_of.isoformat()} (--as-of), not the run time "
            "(ADR 0036)."
        )
    internal = runtime.pipeline.parser_internal_errors()
    if internal:
        notes.append(f"{internal} records hit a parser internal error and were dropped.")
    return notes


def analyze(
    config: Path,
    input_dir: Path,
    out_dir: Path,
    *,
    state_dir: Path | None = None,
    tenant_id: str | None = None,
    locator_map: bool = False,
    producer: str | None = None,
    detector: PiiDetector | None = None,
    clock: Callable[[], datetime] | None = None,
    chunk_size: int = CHUNK_SIZE,
    as_of: datetime | None = None,
) -> AnalyzeResult:
    """Run both passes and write the bundle; raises :class:`AnalyzeError` with the exit code.

    ``detector`` replaces the configured PII detector (tests use the regex detector for
    speed); the command always runs the configured one. ``as_of`` is the time event times are
    checked against (ADR 0036); by default the run time.
    """
    started = time.perf_counter()
    now = clock if clock is not None else lambda: datetime.now(UTC)
    try:
        sources_file = load_sources_file(config)
    except ConfigError as exc:
        raise AnalyzeError(str(exc), EXIT_USAGE) from exc
    input_dir = input_dir.resolve()
    out_dir = out_dir.resolve()
    state = (state_dir if state_dir is not None else default_state_dir(out_dir)).resolve()
    _check_paths(input_dir, out_dir, state)
    sources = upload_sources(sources_file, input_dir)
    settings = _settings(state, tenant_id)
    try:
        runtime = build_runtime(
            settings,
            sources_file,
            mode="analyze",
            detector=detector,
            init_local_keys=True,
            # By default the pipeline reads the run time itself (the wall clock).
            range_clock=(lambda: as_of) if as_of is not None else clock,
        )
    except KeyManagementError as exc:
        msg = f"keys unusable: {describe(exc)}"
        raise AnalyzeError(msg, EXIT_FAILURE) from exc
    try:
        manifest, locator_path, dropped = _run(
            runtime,
            sources,
            out_dir,
            locator_map=locator_map,
            producer=producer or default_producer(),
            created_at=now(),
            clock=now,
            chunk_size=chunk_size,
            as_of=as_of,
        )
    finally:
        runtime.close()
    elapsed = time.perf_counter() - started
    log.info(
        "analyze.complete",
        bundle_id=manifest.bundle_id,
        events=manifest.counts.events,
        records=manifest.counts.records_read,
        seconds=round(elapsed, 3),
    )
    return AnalyzeResult(
        bundle_dir=out_dir,
        manifest=manifest,
        locator_map=locator_path,
        state_dir=state,
        dropped=dropped,
        elapsed_seconds=elapsed,
    )


def _run(
    runtime: EdgeRuntime,
    sources: Sequence[SourceConfig],
    out_dir: Path,
    *,
    locator_map: bool,
    producer: str,
    created_at: datetime,
    clock: Callable[[], datetime],
    chunk_size: int,
    as_of: datetime | None = None,
) -> tuple[BundleManifest, Path | None, dict[str, int]]:
    locator_path = default_locator_map_path(out_dir) if locator_map else None
    try:
        first = _connectors(runtime, sources)
        for source, connector in zip(sources, first, strict=True):
            records = drain(connector, functools.partial(_observe, runtime), chunk_size)
            log.info("analyze.pass1_source", source_id=source.id, records=records)
        with BundleWriter(
            out_dir,
            tenant_id=runtime.tenant_id,
            producer=producer,
            key_versions=runtime.key_versions,
            policy_version=runtime.policy_version or "0",
            created_at=created_at,
            locator_map=locator_path,
            clock=clock,
        ) as writer:
            runtime.pipeline.reset_parsers()  # pass 2 reads each file from its first line
            runtime.templates.freeze()  # pass 2 must not count a message a second time
            runtime.pipeline.names.freeze()  # nor a field name (ADR 0030)
            second = _connectors(runtime, sources)
            for source, connector in zip(sources, second, strict=True):
                handle = functools.partial(_emit_chunk, runtime, writer, source.id)
                records = drain(connector, handle, chunk_size)
                log.info("analyze.pass2_source", source_id=source.id, records=records)
            manifest = writer.finish(
                runtime.classifier.bundle_fields(),
                runtime.templates.registry(),
                connector_types={source.id: source.type.value for source in sources},
                system_of={source.id: source.system for source in sources},
                notes=_notes(runtime, as_of),
            )
            dropped = writer.dropped_by_reason()
    except ConnectorError as exc:
        msg = f"reading the inputs failed: {describe(exc)}"
        raise AnalyzeError(msg, EXIT_FAILURE) from exc
    except BundleWriteError as exc:
        raise AnalyzeError(str(exc), EXIT_FAILURE) from exc
    except OSError as exc:
        msg = f"I/O failed: {describe(exc)}"
        raise AnalyzeError(msg, EXIT_FAILURE) from exc
    return manifest, locator_path, dropped


def format_summary(result: AnalyzeResult) -> list[str]:
    """The lines the command prints: counts and paths only."""
    manifest = result.manifest
    counts = manifest.counts
    dropped = ", ".join(f"{reason} {count:,}" for reason, count in result.dropped.items())
    lines = [
        f"carto-edge analyze: bundle {manifest.bundle_id} written to {result.bundle_dir}",
        f"  tenant          {manifest.tenant_id}",
        f"  records read    {counts.records_read:,}",
        f"  events          {counts.events:,}",
        f"  dropped         {counts.records_dropped:,}" + (f" ({dropped})" if dropped else ""),
        f"  parse errors    {counts.parse_errors:,}",
        (
            f"  out of range    {counts.events_too_old:,} too old (dropped), "
            f"{counts.events_future:,} in the future (ingest time kept)"
        ),
        f"  identifiers     {counts.identifiers:,} tokens",
        (
            f"  fields          kept {counts.fields_kept:,}, "
            f"tokenized {counts.fields_tokenized:,}, dropped {counts.fields_dropped:,}"
        ),
        f"  elapsed         {result.elapsed_seconds:.1f} s",
    ]
    lines.extend(
        f"  source {summary.source_id}: {summary.records_read:,} records, "
        f"{summary.events_written:,} events, {summary.records_dropped:,} dropped"
        for summary in manifest.sources
    )
    if result.locator_map is not None:
        lines.append(f"  locator map     {result.locator_map} (eval only; never send it)")
    lines.extend(
        [
            f"  state dir       {result.state_dir} (key and reveal vault; keep it, never send it)",
            "Review MANIFEST.md in the bundle before you send it.",
        ]
    )
    return lines


def add_analyze_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True, help="Analyzer sources file (YAML)")
    parser.add_argument(
        "--input", type=Path, required=True, help="Directory the upload paths are relative to"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="Bundle directory to create (must be new/empty)"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Key, reveal vault and templates (default: <out>.edge-state next to the bundle)",
    )
    parser.add_argument("--tenant-id", default=None, help="Tenant key domain (default: default)")
    parser.add_argument(
        "--locator-map",
        action="store_true",
        help="Eval only: write <out>.locator_map.ndjson next to the bundle (ADR 0024)",
    )
    parser.add_argument("--producer", default=None, help="Producer text for the manifest")
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=None,
        help="Check event times against this ISO 8601 time instead of the run time (ADR 0036)",
    )


def run_analyze(args: argparse.Namespace) -> int:
    try:
        result = analyze(
            args.config,
            args.input,
            args.out,
            state_dir=args.state_dir,
            tenant_id=args.tenant_id,
            locator_map=args.locator_map,
            producer=args.producer,
            as_of=args.as_of,
        )
    except AnalyzeError as exc:
        print(f"carto-edge analyze: {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # never a traceback: it could quote a value
        print(f"carto-edge analyze: internal error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILURE
    for line in format_summary(result):
        emit(line)
    return EXIT_OK
