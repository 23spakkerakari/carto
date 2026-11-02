"""``carto-eval fit-weights``: logistic weights fitted on simulator truth (spec 9.3; plan M2
decision 4; ADR 0049 item 1).

For each scenario the command runs the driver's analyze and load steps
(:func:`carto_eval.driver.prepare`, :class:`~carto_eval.driver.InMemoryEngine`), one full
profile pass, candidate generation and verification exactly as a link pass would
(:class:`~carto_core.linker.candidates.CandidateGenerator`,
:func:`carto_core.linker.verify.verify`), and keeps the
:func:`~carto_core.linker.score.feature_vector` of every verified candidate. A candidate is a
positive when its ends, mapped to truth fields (:mod:`carto_eval.mapping`), form an exact or
bridge pair of the ground truth, compared by field pair without the forms (the engine
proposes every form pair that shares tokens, and the ``alnum`` variant of a true ``raw`` link
carries the same features); every other verified candidate is a negative, except a pair of two
templates' copies of one source field, which the truth says nothing about and which is left out.
The samples of all scenarios are fitted together with
``sklearn.linear_model.LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000)`` and
written as a :class:`~carto_core.linker.weights.Weights` file (version
``fitted-<date>-<sha8 of the samples>``, ``fitted_on`` naming the scenarios and day counts) that
``LinkerSettings.weights_file`` can point at. The command prints precision and recall at the
proposal threshold on the training samples, per scenario and overall.

scikit-learn lives here and nowhere in core (``test_eval_fit_weights.py`` greps for it).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from carto_core.linker.candidates import CandidateGenerator
from carto_core.linker.profile_source import SketchProfileSource, profiler_seams
from carto_core.linker.score import feature_vector, logistic
from carto_core.linker.verify import verify
from carto_core.linker.weights import FEATURE_NAMES, Weights, load_weights
from carto_core.profiler.repository import MetadataFieldProfileSink
from carto_core.profiler.run import ProfilePass
from carto_core.settings import CoreSettings
from carto_core.store.model import Window
from carto_edge.pipeline.pii import PiiDetector
from carto_eval.driver import InMemoryEngine, PreparedRun, prepare
from carto_eval.mapping import FieldMapping
from carto_simulator import api
from carto_simulator.ground_truth import FieldRef, GroundTruth

__all__ = [
    "DEFAULT_DAYS",
    "POSITIVE_TYPES",
    "FitError",
    "FitResult",
    "LabeledSample",
    "ScenarioSamples",
    "collect_samples",
    "evaluate",
    "field_pair",
    "fit",
    "run_fit",
    "truth_field_pairs",
    "weights_version",
    "write_weights",
]

POSITIVE_TYPES: Final[frozenset[str]] = frozenset({"exact", "bridge"})
DEFAULT_DAYS: Final[dict[str, int]] = {"shop": 14, "people_ops": 28}
"""Per-scenario default day counts (plan M2 package H: scenario C needs 28 days)."""


class FitError(Exception):
    """The fit could not run; the message never carries data."""


@dataclass(frozen=True, slots=True)
class LabeledSample:
    scenario: str
    vector: dict[str, float]
    label: bool
    source: str
    """``lsh`` or ``bridge``: how the candidate was found."""
    a: str
    b: str
    """The engine ends (``field_ref|form``), for the record; never a token."""


@dataclass(frozen=True, slots=True)
class ScenarioSamples:
    scenario: str
    days: int
    samples: tuple[LabeledSample, ...]
    candidates: int
    unmapped: int
    """Candidates with an end no truth field could be mapped to (labelled negative)."""
    same_field: int = 0
    """Candidates pairing two templates' copies of one source field (left out)."""

    @property
    def positives(self) -> int:
        return sum(1 for sample in self.samples if sample.label)


@dataclass(frozen=True, slots=True)
class Metrics:
    precision: float | None
    recall: float | None
    predicted: int
    positives: int
    matched: int


@dataclass(frozen=True, slots=True)
class FitResult:
    weights: Weights
    path: Path
    scenarios: tuple[ScenarioSamples, ...]
    per_scenario: dict[str, Metrics]
    overall: Metrics
    threshold: float

    def lines(self) -> list[str]:
        out = [
            (
                f"fit-weights: {sum(len(s.samples) for s in self.scenarios)} samples "
                f"({sum(s.positives for s in self.scenarios)} positives) from "
                f"{', '.join(f'{s.scenario} ({s.days} days)' for s in self.scenarios)}"
            ),
            f"  version    {self.weights.version}",
            f"  intercept  {self.weights.intercept:.4f}",
        ]
        out.extend(f"  {name:<20} {self.weights.coefficient(name):+.4f}" for name in FEATURE_NAMES)
        out.append(f"  at the proposal threshold {self.threshold:.2f} on the training samples:")
        for scenario, metrics in self.per_scenario.items():
            out.append(f"    {scenario:<12} {_fmt(metrics)}")
        out.append(f"    {'overall':<12} {_fmt(self.overall)}")
        out.append(f"  wrote      {self.path}")
        return out


def _fmt(metrics: Metrics) -> str:
    precision = "n/a" if metrics.precision is None else f"{metrics.precision:.3f}"
    recall = "n/a" if metrics.recall is None else f"{metrics.recall:.3f}"
    return (
        f"precision {precision}, recall {recall} ({metrics.matched} of {metrics.positives} "
        f"positives in {metrics.predicted} proposals)"
    )


# -- samples ------------------------------------------------------------------------------------


def collect_samples(
    prepared: PreparedRun, settings: CoreSettings, *, engine: InMemoryEngine | None = None
) -> ScenarioSamples:
    """Profile, generate and verify candidates over the prepared bundle; label by truth."""
    runner = engine if engine is not None else InMemoryEngine(settings)
    parts = runner.load(prepared.bundle_dir, stored_at=prepared.as_of)
    tenant_id = settings.tenant_id
    as_of = prepared.as_of
    ProfilePass(
        parts.store,
        parts.sketches,
        MetadataFieldProfileSink(parts.metadata),
        parts.watermarks,
        settings.profiler,
    ).run(tenant_id, full=True, now=as_of)
    seams = profiler_seams(settings.profiler)
    source = SketchProfileSource(
        parts.sketches, parts.store, settings.profiler, clock=lambda: as_of
    )
    profiles = list(source.profiles(tenant_id))
    index = {profile.end: profile for profile in profiles}
    window = Window(
        start=as_of - timedelta(days=settings.profiler.window_days),
        end=as_of + timedelta(days=1),
    )
    generator = CandidateGenerator(
        settings.linker,
        settings.profiler,
        containment=seams.containment,
        decode_minhash=seams.decode_minhash,
    )
    candidates = generator.generate(
        tenant_id,
        store=parts.store,
        window=window,
        profiles=profiles,
        sketch_of=lambda end: source.window_sketch(tenant_id, end),
    )
    mapping = FieldMapping.from_bundle(
        prepared.verified, prepared.truth, locator_map=prepared.locator_map
    )
    positives = truth_field_pairs(prepared.truth, POSITIVE_TYPES)
    samples: list[LabeledSample] = []
    unmapped = 0
    same_field = 0
    for candidate in candidates.candidates:
        cooccurrence = candidates.cooccurrences.get((candidate.a, candidate.b))
        features = verify(
            parts.store,
            tenant_id,
            candidate,
            window,
            domain_size_fn=seams.domain_size,
            profiles=index,
            cooccurrence=cooccurrence,
        )
        ref_a = mapping.map_field_ref(candidate.a.field_ref)
        ref_b = mapping.map_field_ref(candidate.b.field_ref)
        label = False
        if ref_a is None or ref_b is None:
            unmapped += 1
        elif ref_a == ref_b:
            same_field += 1
            continue
        else:
            label = field_pair(ref_a, ref_b) in positives
        samples.append(
            LabeledSample(
                scenario=prepared.request.scenario,
                vector=feature_vector(features, settings.linker),
                label=label,
                source=candidate.source,
                a=f"{candidate.a.field_ref}|{candidate.a.form}",
                b=f"{candidate.b.field_ref}|{candidate.b.form}",
            )
        )
    return ScenarioSamples(
        scenario=prepared.request.scenario,
        days=prepared.request.days,
        samples=tuple(samples),
        candidates=len(candidates.candidates),
        unmapped=unmapped,
        same_field=same_field,
    )


def field_pair(a: FieldRef, b: FieldRef) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    first, second = sorted(
        ((a.system_id, a.source_id, a.field), (b.system_id, b.source_id, b.field))
    )
    return first, second


def truth_field_pairs(
    truth: GroundTruth, link_types: Collection[str]
) -> frozenset[tuple[tuple[str, str, str], tuple[str, str, str]]]:
    """The unordered field pairs (forms ignored) of the truth links of ``link_types``."""
    return frozenset(
        field_pair(link.a, link.b) for link in truth.links if link.link_type in link_types
    )


# -- the fit ------------------------------------------------------------------------------------


def weights_version(samples: Sequence[LabeledSample], today: datetime) -> str:
    """``fitted-<YYYYMMDD>-<sha8>`` where the digest covers the sorted sample rows."""
    rows = sorted(
        json.dumps(
            [sample.scenario, sample.label, [sample.vector[name] for name in FEATURE_NAMES]],
            separators=(",", ":"),
        )
        for sample in samples
    )
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:8]
    return f"fitted-{today.strftime('%Y%m%d')}-{digest}"


def fit(scenarios: Sequence[ScenarioSamples], *, today: datetime | None = None) -> Weights:
    """Balanced logistic regression over every sample (module docstring)."""
    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415 - eval-only dependency

    samples = [sample for scenario in scenarios for sample in scenario.samples]
    positives = sum(1 for sample in samples if sample.label)
    if positives == 0 or positives == len(samples):
        msg = (
            f"cannot fit: {positives} positives among {len(samples)} verified candidates; "
            "both classes are needed"
        )
        raise FitError(msg)
    matrix = [[sample.vector[name] for name in FEATURE_NAMES] for sample in samples]
    labels = [1 if sample.label else 0 for sample in samples]
    model = LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000)
    model.fit(matrix, labels)
    coefficients = {
        name: float(value) for name, value in zip(FEATURE_NAMES, model.coef_[0], strict=True)
    }
    now = today if today is not None else datetime.now(UTC)
    return Weights(
        version=weights_version(samples, now),
        fitted_on=", ".join(
            f"{s.scenario} ({s.days} days, {len(s.samples)} pairs)" for s in scenarios
        )[:256],
        intercept=float(model.intercept_[0]),
        coefficients=coefficients,
    )


def evaluate(samples: Sequence[LabeledSample], weights: Weights, threshold: float) -> Metrics:
    """Precision and recall of ``score >= threshold`` against the labels."""
    predicted = matched = positives = 0
    for sample in samples:
        proposed = logistic(sample.vector, weights) >= threshold
        positives += sample.label
        predicted += proposed
        matched += proposed and sample.label
    return Metrics(
        precision=matched / predicted if predicted else None,
        recall=matched / positives if positives else None,
        predicted=predicted,
        positives=positives,
        matched=matched,
    )


def write_weights(out: Path, weights: Weights) -> None:
    """Write the weights document and read it back through :func:`load_weights`."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(weights.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        load_weights(out)  # the file must read back as a valid weights document
    except OSError as exc:
        msg = f"cannot write {out}: {type(exc).__name__}"
        raise FitError(msg) from exc


def run_fit(
    scenarios: Sequence[str],
    *,
    sim_root: Path,
    out: Path,
    days: int | None = None,
    seed: int = api.DEFAULT_SEED,
    daily_volume: int = api.DEFAULT_DAILY_VOLUME,
    settings: CoreSettings | None = None,
    detector: PiiDetector | None = None,
    reuse_bundle: bool = False,
    today: datetime | None = None,
) -> FitResult:
    """Collect, fit, write, evaluate. ``days`` applies to every scenario; None takes each
    scenario's default (:data:`DEFAULT_DAYS`, else the simulator's)."""
    core = settings if settings is not None else CoreSettings(_env_file=None)
    collected: list[ScenarioSamples] = []
    for scenario in scenarios:
        day_count = days if days is not None else DEFAULT_DAYS.get(scenario, api.DEFAULT_DAYS)
        try:
            request = api.GenerationRequest(
                scenario=scenario, days=day_count, seed=seed, daily_volume=daily_volume
            )
        except ValueError as exc:
            raise FitError(str(exc)) from exc
        prepared = prepare(
            request, sim_root / scenario, detector=detector, reuse_bundle=reuse_bundle
        )
        collected.append(collect_samples(prepared, core))
    weights = fit(collected, today=today)
    write_weights(out, weights)
    threshold = core.linker.proposal_threshold
    per_scenario = {item.scenario: evaluate(item.samples, weights, threshold) for item in collected}
    overall = evaluate([s for item in collected for s in item.samples], weights, threshold)
    return FitResult(
        weights=weights,
        path=out,
        scenarios=tuple(collected),
        per_scenario=per_scenario,
        overall=overall,
        threshold=threshold,
    )


def sample_rows(result: FitResult) -> list[dict[str, Any]]:
    """The labelled samples as JSON-safe rows (for a debugging dump; never tokens)."""
    return [
        {
            "scenario": sample.scenario,
            "label": sample.label,
            "source": sample.source,
            "a": sample.a,
            "b": sample.b,
            **sample.vector,
        }
        for scenario in result.scenarios
        for sample in scenario.samples
    ]
