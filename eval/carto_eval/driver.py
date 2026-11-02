"""The end-to-end eval driver (spec 18.4; plan M2 package I; ADR 0006, 0049).

:func:`run` takes one simulator scenario through the whole M2 path and scores it:

1. **Simulate** when ``<sim_out>/ground_truth`` is missing (:func:`ensure_ground_truth`; an
   existing ground truth generated with other parameters is refused, as ``carto-eval run``
   does).
2. **Analyze** with the offline analyzer in process (:func:`carto_edge.cli.analyze.analyze`)
   using ``simulator/analyze.<scenario>.yaml``, the eval-only locator map (ADR 0006) and the
   run's reference time (:func:`carto_simulator.api.run_as_of`), into ``<sim_out>.carto`` with
   the state directory next to it. A previous bundle is replaced unless ``reuse_bundle`` is set
   and it still verifies; the state directory is kept, so re-runs tokenize identically.
3. **Discover** through an :class:`EngineRunner`: :class:`InMemoryEngine` loads the bundle into
   :class:`~carto_core.store.memory.InMemoryEventStore` with the in-memory repositories and
   runs :func:`carto_core.discover.discover`; :class:`StoresEngine` runs
   :func:`carto_core.discover.run_discover` against the ClickHouse and PostgreSQL named by the
   ``CARTO_`` settings. Either writes ``engine_links.json``, ``engine_entities.json`` and
   ``discover_summary.json`` under ``<out>/<scenario>.engine``; the driver reads those files and
   nothing else from the engine.
4. **Map** engine fields to truth fields (:mod:`carto_eval.mapping`) and apply the acceptance
   policy of ADR 0049 item 2; write the prediction directory ``<out>/<scenario>.predictions``
   with the reference writer.
5. **Score** with :func:`carto_eval.scoring.score`, build the report with the existing
   builders, append the field-mapping section, the policy statement and the engine summary to
   the Markdown, write ``<out>/<scenario>.mapping.json`` with the mapping report and the
   discover summary, and append the history line.

Ground truth is read only here and in :mod:`carto_eval.mapping`; the engine sees the bundle.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash
from carto_core.bundle import (
    LoadResult,
    VerifiedBundle,
    iter_events,
    record_bundle_metadata,
    verify_bundle,
)
from carto_core.discover import (
    ENGINE_ENTITIES_FILE,
    ENGINE_LINKS_FILE,
    SUMMARY_FILE,
    EngineParts,
    discover,
    run_discover,
    summary_lines,
)
from carto_core.linker.composite import InMemoryCompositeRuleRepository
from carto_core.linker.repository import InMemoryEntityRepository, InMemoryLinkRepository
from carto_core.metadata import InMemoryMetadataStore
from carto_core.profiler.repository import InMemorySketchRepository, InMemoryWatermarkStore
from carto_core.settings import CoreSettings
from carto_core.store.memory import InMemoryEventStore
from carto_edge.bundle import default_locator_map_path
from carto_edge.cli.analyze import AnalyzeResult, analyze, default_state_dir
from carto_edge.pipeline.pii import PiiDetector
from carto_eval.mapping import (
    FieldMapping,
    MappedLink,
    MappingReport,
    policy_families,
    policy_statement,
)
from carto_eval.predictions import Predictions
from carto_eval.report import (
    HISTORY_FILE,
    EvalReport,
    append_history,
    build_report,
    find_regressions,
    last_record,
    read_history,
)
from carto_eval.scoring import (
    Scorecard,
    TruthEvents,
    check_complete,
    ensure_ground_truth,
    load_truth_events,
    report_counts,
    score,
)
from carto_eval.targets import DEFAULT_TOLERANCE_POINTS
from carto_simulator import api
from carto_simulator.ground_truth import GroundTruth, read_ground_truth

__all__ = [
    "BUNDLE_SUFFIX",
    "ENGINE_DIR_SUFFIX",
    "PREDICTIONS_DIR_SUFFIX",
    "DriverError",
    "EngineRunner",
    "EvalRun",
    "InMemoryAuditLog",
    "InMemoryEngine",
    "PreparedRun",
    "StoresEngine",
    "analyzer_config",
    "engine_section",
    "prepare",
    "run",
]

BUNDLE_SUFFIX: Final = ".carto"
ENGINE_DIR_SUFFIX: Final = ".engine"
PREDICTIONS_DIR_SUFFIX: Final = ".predictions"
MAPPING_FILE_SUFFIX: Final = ".mapping.json"
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_MAX_ENGINE_FILE_BYTES: Final = 256 * 1024 * 1024


class DriverError(Exception):
    """A step of the driver could not run; the message names the step, never a value."""


def analyzer_config(scenario: str) -> Path:
    """``simulator/analyze.<scenario>.yaml`` of the workspace."""
    return _REPO_ROOT / "simulator" / f"analyze.{scenario}.yaml"


class EngineRunner(Protocol):
    """Runs discovery over a bundle and writes the three engine files into ``out_dir``."""

    def discover(self, bundle_dir: Path, *, out_dir: Path, as_of: datetime) -> list[str]:
        """Returns the human-readable summary lines."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """Everything before the engine: the truth, the bundle and the reference time."""

    request: api.GenerationRequest
    sim_out: Path
    truth: GroundTruth
    events: TruthEvents
    bundle_dir: Path
    locator_map: Path
    verified: VerifiedBundle
    as_of: datetime
    generated: str | None
    """Why the scenario was (re)generated, or None when the existing ground truth was kept."""
    analyzed: AnalyzeResult | None
    """None when a previous bundle was reused."""


@dataclass(frozen=True, slots=True)
class EvalRun:
    """What :func:`run` produced."""

    prepared: PreparedRun
    report: EvalReport
    scorecard: Scorecard
    mapping: MappingReport
    discover_summary: Mapping[str, Any]
    engine_lines: tuple[str, ...]
    predictions: Predictions
    engine_dir: Path
    predictions_dir: Path
    json_path: Path
    markdown_path: Path
    mapping_path: Path
    markdown: str


# -- engines ------------------------------------------------------------------------------------


class InMemoryAuditLog:
    """The :class:`~carto_core.audit.AuditRecorder` of an in-memory run: one hash chain per
    tenant, kept for the tests to inspect; nothing is persisted."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._heads: dict[str, str] = {}

    def record(self, entry: AuditEntry) -> str:
        head = chain_hash(self._heads.get(entry.tenant_id, GENESIS_HASH), entry.content())
        self._heads[entry.tenant_id] = head
        self.entries.append(entry)
        return head

    def latest_hash(self, tenant_id: str) -> str:
        return self._heads.get(tenant_id, GENESIS_HASH)


class InMemoryEngine:
    """The engine over the in-memory store and repositories (ADR 0046: the reference
    semantics; no Docker needed). ``parts`` holds the collaborators of the last ``load`` so
    ``fit-weights`` can run the profiler and the candidate step on them."""

    def __init__(self, settings: CoreSettings | None = None) -> None:
        self.settings = settings if settings is not None else CoreSettings(_env_file=None)
        self.parts: EngineParts | None = None
        self.loaded: LoadResult | None = None
        self.verified: VerifiedBundle | None = None

    def load(self, bundle_dir: Path, *, stored_at: datetime) -> EngineParts:
        """Verify the bundle, add its events to a fresh in-memory store (``stored_at`` is the
        load time the watermark sees) and record its templates and fields."""
        verified = verify_bundle(bundle_dir)
        store = InMemoryEventStore()
        metadata = InMemoryMetadataStore()
        written = store.add_events(iter_events(verified), stored_at=stored_at)
        record_bundle_metadata(verified, metadata, templates=True)
        self.parts = EngineParts(
            store=store,
            metadata=metadata,
            sketches=InMemorySketchRepository(),
            watermarks=InMemoryWatermarkStore(),
            links=InMemoryLinkRepository(),
            entities=InMemoryEntityRepository(),
            composite_rules=InMemoryCompositeRuleRepository(),
            audit=InMemoryAuditLog(),
            settings=self.settings,
        )
        self.loaded = LoadResult(events_written=written, chunks=1, duplicates=0)
        self.verified = verified
        return self.parts

    def discover(self, bundle_dir: Path, *, out_dir: Path, as_of: datetime) -> list[str]:
        parts = self.load(bundle_dir, stored_at=as_of)
        if self.verified is None:  # set by load
            msg = "the bundle was not verified"
            raise RuntimeError(msg)
        result = discover(
            parts,
            tenant_id=self.settings.tenant_id,
            out_dir=out_dir,
            as_of=as_of,
            bundle=self.loaded,
            bundle_id=self.verified.manifest.bundle_id,
        )
        return summary_lines(result)


class StoresEngine:
    """The engine over ClickHouse and PostgreSQL (``CARTO_`` settings), through
    :func:`carto_core.discover.run_discover`, which verifies and loads the bundle itself."""

    def __init__(self, settings: CoreSettings | None = None) -> None:
        self.settings = settings if settings is not None else CoreSettings()

    def discover(self, bundle_dir: Path, *, out_dir: Path, as_of: datetime) -> list[str]:
        return run_discover(
            self.settings, out_dir=out_dir, bundle=bundle_dir, as_of=as_of.isoformat()
        )


# -- preparation --------------------------------------------------------------------------------


def _bundle_paths(sim_out: Path) -> tuple[Path, Path, Path]:
    resolved = sim_out.resolve()
    bundle_dir = resolved.parent / f"{resolved.name}{BUNDLE_SUFFIX}"
    return bundle_dir, default_locator_map_path(bundle_dir), default_state_dir(bundle_dir)


def _reusable(bundle_dir: Path, locator_map: Path) -> VerifiedBundle | None:
    if not bundle_dir.is_dir() or not locator_map.is_file():
        return None
    try:
        return verify_bundle(bundle_dir)
    except Exception:  # any defect means re-analyze
        return None


def _remove_previous(bundle_dir: Path, locator_map: Path) -> None:
    try:
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        locator_map.unlink(missing_ok=True)
    except OSError as exc:
        msg = f"cannot replace the previous bundle {bundle_dir}: {type(exc).__name__}"
        raise DriverError(msg) from exc


def prepare(
    request: api.GenerationRequest,
    sim_out: Path,
    *,
    config: Path | None = None,
    detector: PiiDetector | None = None,
    regenerate: bool = False,
    reuse_bundle: bool = False,
    as_of: datetime | None = None,
    tenant_id: str | None = None,
) -> PreparedRun:
    """Steps 1 and 2 of the module docstring. ``detector`` replaces the configured PII detector
    (tests pass the regex one); ``as_of`` defaults to the run's own reference time."""
    try:
        generated = ensure_ground_truth(request, sim_out, regenerate=regenerate)
    except (KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) else exc
        raise DriverError(str(message)) from exc
    try:
        truth = read_ground_truth(sim_out)
        events = load_truth_events(sim_out)
        check_complete(truth, events)
    except (OSError, ValueError, TypeError) as exc:
        msg = f"ground truth under {sim_out} is incomplete or unreadable ({type(exc).__name__})"
        raise DriverError(msg) from exc
    reference = (
        as_of
        if as_of is not None
        else api.as_of_for(truth.manifest.start_date, truth.manifest.days)
    )
    bundle_dir, locator_map, state_dir = _bundle_paths(sim_out)
    verified = _reusable(bundle_dir, locator_map) if reuse_bundle and not generated else None
    analyzed: AnalyzeResult | None = None
    if verified is None:
        _remove_previous(bundle_dir, locator_map)
        config_path = config if config is not None else analyzer_config(request.scenario)
        if not config_path.is_file():
            msg = f"no analyzer configuration for scenario {request.scenario} at {config_path}"
            raise DriverError(msg)
        try:
            analyzed = analyze(
                config_path,
                sim_out,
                bundle_dir,
                state_dir=state_dir,
                tenant_id=tenant_id,
                locator_map=True,
                detector=detector,
                as_of=reference,
            )
        except Exception as exc:  # AnalyzeError carries a code; the message never has a value
            msg = f"analyzer failed: {type(exc).__name__}: {exc}"
            raise DriverError(msg) from exc
        verified = verify_bundle(bundle_dir)
    return PreparedRun(
        request=request,
        sim_out=sim_out,
        truth=truth,
        events=events,
        bundle_dir=bundle_dir,
        locator_map=locator_map,
        verified=verified,
        as_of=reference,
        generated=generated,
        analyzed=analyzed,
    )


# -- the run ------------------------------------------------------------------------------------


def _read_engine_file(path: Path) -> Any:
    try:
        if path.stat().st_size > _MAX_ENGINE_FILE_BYTES:
            msg = f"{path} is larger than {_MAX_ENGINE_FILE_BYTES} bytes"
            raise DriverError(msg)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"cannot read {path}: {type(exc).__name__}"
        raise DriverError(msg) from exc


def _threshold(summary: Mapping[str, Any], fallback: float) -> float:
    settings = summary.get("settings")
    linker = settings.get("linker") if isinstance(settings, Mapping) else None
    value = linker.get("proposal_threshold") if isinstance(linker, Mapping) else None
    return float(value) if isinstance(value, int | float) else fallback


def engine_section(summary: Mapping[str, Any], lines: Sequence[str]) -> str:
    """The "Engine run" section of the Markdown report: the summary lines and the window."""
    out = ["## Engine run", ""]
    out.extend(f"- {line.strip()}" for line in lines)
    window = summary.get("window")
    if isinstance(window, Mapping):
        out.append(f"- window {window.get('start')} to {window.get('end')}")
    composite = summary.get("composite")
    if isinstance(composite, Mapping):
        out.append(
            f"- composite: {composite.get('pairs_triggered')} of "
            f"{composite.get('pairs_considered')} system pairs triggered, "
            f"{composite.get('rules')} rules, {composite.get('matched_pairs')} matched pairs"
        )
    out.append("")
    return "\n".join(out) + "\n"


def run(
    scenario: str,
    days: int,
    seed: int,
    daily_volume: int,
    sim_out: Path,
    out_dir: Path,
    *,
    engine: EngineRunner,
    as_of: datetime | None = None,
    config: Path | None = None,
    detector: PiiDetector | None = None,
    regenerate: bool = False,
    reuse_bundle: bool = False,
    tolerance_points: float = DEFAULT_TOLERANCE_POINTS,
    clock: Callable[[], datetime] | None = None,
    proposal_threshold: float | None = None,
) -> EvalRun:
    """The whole path (module docstring). Raises :class:`DriverError`, :class:`MappingError`,
    :class:`carto_core.discover.DiscoverError` or :class:`carto_core.bundle.BundleError`."""
    try:
        request = api.GenerationRequest(
            scenario=scenario, days=days, seed=seed, daily_volume=daily_volume
        )
    except ValueError as exc:
        raise DriverError(str(exc)) from exc
    prepared = prepare(
        request,
        sim_out,
        config=config,
        detector=detector,
        regenerate=regenerate,
        reuse_bundle=reuse_bundle,
        as_of=as_of,
    )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"cannot create {out_dir}: {type(exc).__name__}"
        raise DriverError(msg) from exc
    engine_dir = out_dir / f"{scenario}{ENGINE_DIR_SUFFIX}"
    lines = engine.discover(prepared.bundle_dir, out_dir=engine_dir, as_of=prepared.as_of)
    engine_links = _read_engine_file(engine_dir / ENGINE_LINKS_FILE)
    engine_entities = _read_engine_file(engine_dir / ENGINE_ENTITIES_FILE)
    summary = _read_engine_file(engine_dir / SUMMARY_FILE)
    if not isinstance(engine_links, list) or not isinstance(engine_entities, list):
        msg = f"{engine_dir}: the engine files are not JSON arrays"
        raise DriverError(msg)
    if not isinstance(summary, Mapping):
        msg = f"{engine_dir / SUMMARY_FILE}: not a JSON object"
        raise DriverError(msg)
    threshold = (
        proposal_threshold
        if proposal_threshold is not None
        else _threshold(summary, CoreSettings(_env_file=None).linker.proposal_threshold)
    )
    mapping = FieldMapping.from_bundle(
        prepared.verified, prepared.truth, locator_map=prepared.locator_map
    )
    mapped, mapping_report = mapping.map_links(engine_links, threshold=threshold)
    predictions = _predictions(mapped)
    predictions_dir = out_dir / f"{scenario}{PREDICTIONS_DIR_SUFFIX}"
    now = clock if clock is not None else (lambda: datetime.now(UTC))
    scorecard = score(prepared.truth, prepared.events.membership, predictions)
    report = build_report(
        scenario=scenario,
        seed=seed,
        days=days,
        generated_at=now(),
        predictions_present=True,
        counts=report_counts(prepared.truth, prepared.events, predictions),
        values=scorecard.values(),
    )
    history_path = out_dir / HISTORY_FILE
    try:
        records = read_history(history_path)
    except (OSError, ValueError) as exc:
        msg = f"cannot read history: {exc}"
        raise DriverError(msg) from exc
    previous = last_record(records, scenario, seed, days)
    report = report.with_regressions(
        find_regressions(previous, report, tolerance_points=tolerance_points)
    )
    markdown = (
        report.to_markdown()
        + "\n"
        + policy_statement(threshold)
        + "\n\n"
        + mapping_report.to_markdown()
        + "\n"
        + engine_section(summary, lines)
    )
    json_path = out_dir / f"{scenario}.json"
    markdown_path = out_dir / f"{scenario}.md"
    mapping_path = out_dir / f"{scenario}{MAPPING_FILE_SUFFIX}"
    try:
        predictions.write(predictions_dir)
        report.write_json(json_path)
        markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
        mapping_path.write_text(
            json.dumps(
                {
                    "scenario": scenario,
                    "policy": policy_statement(threshold),
                    "proposal_threshold": threshold,
                    "as_of": prepared.as_of.isoformat(),
                    "bundle_id": prepared.verified.manifest.bundle_id,
                    "mapping": mapping_report.to_json(),
                    "discover": dict(summary),
                    "engine_lines": list(lines),
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        append_history(history_path, report.history_record())
    except OSError as exc:
        msg = f"cannot write to {out_dir}: {type(exc).__name__}"
        raise DriverError(msg) from exc
    return EvalRun(
        prepared=prepared,
        report=report,
        scorecard=scorecard,
        mapping=mapping_report,
        discover_summary=summary,
        engine_lines=tuple(lines),
        predictions=predictions,
        engine_dir=engine_dir,
        predictions_dir=predictions_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        mapping_path=mapping_path,
        markdown=markdown,
    )


def _predictions(mapped: Sequence[MappedLink]) -> Predictions:
    return Predictions(
        links=[item.link for item in mapped],
        entities=policy_families(mapped),
        present=True,
    )
