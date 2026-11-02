"""``carto-eval fit-weights`` (plan M2 decision 4; ADR 0049 item 1): the samples of a small shop
run are labelled from truth after mapping (field pair, forms ignored; same-field pairs left
out), a fit over mixed samples writes a weights document that ``load_weights`` accepts and the
scorer runs with, the version names the data, a one-class sample set is refused, and core never
imports scikit-learn. ``bench-linker`` runs at a toy size."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carto_core.linker.score import Scorer
from carto_core.linker.weights import FEATURE_NAMES, Weights, load_weights
from carto_core.settings import CoreSettings, LinkerSettings
from carto_edge.pipeline.pii import RegexDetector
from carto_eval import bench_linker, cli, fit_weights
from carto_eval.driver import prepare
from carto_eval.fit_weights import LabeledSample, ScenarioSamples
from carto_simulator.api import GenerationRequest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core" / "carto_core"
TODAY = datetime(2026, 10, 18, tzinfo=UTC)


def sample(label: bool, **features: float) -> LabeledSample:
    vector = dict.fromkeys(FEATURE_NAMES, 0.0) | features
    return LabeledSample("x", vector, label, "lsh", "sys/tpl/a|raw", "sys/tpl/b|raw")


def mixed() -> ScenarioSamples:
    """Clean links against coincidental overlaps, the way the shipped weights describe them."""
    positives = [
        sample(True, c_ab=0.9 + i / 100, c_ba=0.85, log_n_match=6.0, lag_consistency=0.9)
        for i in range(6)
    ]
    negatives = [
        sample(False, c_ab=0.2 + i / 100, c_ba=0.15, log_n_match=4.0, chance_ratio=0.3)
        for i in range(6)
    ]
    return ScenarioSamples("x", 3, tuple(positives + negatives), 12, 0)


@pytest.fixture(scope="module")
def collected(tmp_path_factory: pytest.TempPathFactory) -> fit_weights.ScenarioSamples:
    root = tmp_path_factory.mktemp("fit")
    prepared = prepare(
        GenerationRequest(scenario="shop", days=3, daily_volume=200),
        root / "shop",
        detector=RegexDetector(),
    )
    return fit_weights.collect_samples(prepared, CoreSettings(_env_file=None))


def test_samples_are_labelled_from_truth(collected: fit_weights.ScenarioSamples) -> None:
    assert collected.scenario == "shop" and collected.days == 3
    assert collected.candidates >= len(collected.samples) + collected.same_field
    assert collected.positives > 0
    assert all(set(item.vector) == set(FEATURE_NAMES) for item in collected.samples)
    assert all("t1." not in item.a and "t1." not in item.b for item in collected.samples)
    assert {item.source for item in collected.samples} <= {"lsh", "bridge"}
    # the shipped weights propose the labelled positives at the proposal threshold
    from carto_core.linker.weights import DEFAULT_WEIGHTS  # noqa: PLC0415

    metrics = fit_weights.evaluate(collected.samples, DEFAULT_WEIGHTS, 0.5)
    assert metrics.positives == collected.positives
    assert metrics.recall is not None and metrics.recall > 0.5


def test_fit_writes_a_valid_weights_file(tmp_path: Path) -> None:
    weights = fit_weights.fit([mixed()], today=TODAY)
    out = tmp_path / "weights" / "fitted.json"
    fit_weights.write_weights(out, weights)
    loaded = load_weights(out)
    assert loaded == weights
    assert weights.version.startswith("fitted-20261018-") and len(weights.version) == 24
    assert weights.fitted_on == "x (3 days, 12 pairs)"
    assert set(weights.coefficients) == set(FEATURE_NAMES)
    assert weights.coefficients["c_ab"] > 0 > weights.coefficients["chance_ratio"]
    document = json.loads(out.read_text(encoding="utf-8"))
    assert Weights.model_validate(document) == weights
    # the scorer accepts the fitted set as it accepts the shipped one
    assert Scorer(LinkerSettings(), weights).weights.version == weights.version
    metrics = fit_weights.evaluate(mixed().samples, weights, 0.5)
    assert metrics.precision == 1.0 and metrics.recall == 1.0


def test_fit_needs_both_classes() -> None:
    one = ScenarioSamples("x", 1, tuple(sample(True, c_ab=0.5) for _ in range(3)), 3, 0)
    with pytest.raises(fit_weights.FitError, match="both classes"):
        fit_weights.fit([one], today=TODAY)


def test_version_is_a_function_of_the_samples() -> None:
    first = fit_weights.weights_version([sample(True, c_ab=0.5)], TODAY)
    assert first == fit_weights.weights_version([sample(True, c_ab=0.5)], TODAY)
    assert first != fit_weights.weights_version([sample(True, c_ab=0.4)], TODAY)
    assert first.startswith("fitted-20261018-")


def test_core_never_imports_sklearn() -> None:
    """ADR 0049 item 1: scikit-learn is an eval-only dependency."""
    pattern = re.compile(r"^\s*(from|import)\s+sklearn\b", re.MULTILINE)
    offenders = [
        path for path in CORE.rglob("*.py") if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    assert pattern.search((ROOT / "eval" / "carto_eval" / "fit_weights.py").read_text("utf-8"))
    pyproject = (ROOT / "core" / "pyproject.toml").read_text(encoding="utf-8")
    assert "scikit-learn" not in pyproject and "sklearn" not in pyproject


def test_cli_fit_weights_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = str(tmp_path / "w.json")
    code = cli.main(
        [
            "fit-weights",
            "--scenario",
            "shop",
            "--days",
            "0",
            "--sim-out",
            str(tmp_path),
            "--out",
            out,
        ]
    )
    assert code == 2
    assert "days must be at least 1" in capsys.readouterr().err


def test_bench_linker_runs_at_toy_size(capsys: pytest.CaptureFixture[str]) -> None:
    result = bench_linker.run_bench(
        systems=3, fields_per_system=2, tokens_per_field=60, verify_cap=4
    )
    assert result.fields == 6 and result.planted_pairs == 4
    assert result.verified <= 4 and result.candidate_seconds >= 0.0
    assert any("bench-linker: 3 systems x 2 fields" in line for line in result.lines())
    args = [
        "bench-linker",
        "--systems",
        "2",
        "--fields",
        "2",
        "--tokens",
        "60",
        "--verify-cap",
        "2",
    ]
    assert cli.main(args) == 0
    assert "measured, not gating" in capsys.readouterr().out
    assert cli.main(["bench-linker", "--systems", "0"]) == 2
