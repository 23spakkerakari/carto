"""``carto-eval`` end to end on a small generated scenario (days 2, daily volume 30)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carto_eval import cli
from carto_eval.predictions import LINKS_FILE, Predictions
from carto_simulator.api import GenerationRequest, generate
from carto_simulator.ground_truth import (
    EVENTS_FILE,
    GROUND_TRUTH_DIR,
    MANIFEST_FILE,
    iter_events,
    read_ground_truth,
)
from carto_simulator.ground_truth import LINKS_FILE as TRUTH_LINKS_FILE

SMALL = ["--scenario", "shop", "--days", "2", "--seed", "1", "--daily-volume", "30"]


@pytest.fixture(scope="module")
def small_scenario(tmp_path_factory: pytest.TempPathFactory) -> Path:
    sim_out = tmp_path_factory.mktemp("sim") / "shop"
    generate(GenerationRequest(scenario="shop", days=2, seed=1, daily_volume=30), sim_out)
    return sim_out


def run(sim_out: Path, out: Path, *extra: str) -> int:
    return cli.main(["run", *SMALL, "--sim-out", str(sim_out), "--out", str(out), *extra])


def load_report(out: Path) -> dict[str, object]:
    payload: dict[str, object] = json.loads((out / "shop.json").read_text(encoding="utf-8"))
    return payload


def metric_values(report: dict[str, object]) -> dict[str, float | None]:
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    return {metric_id: result["value"] for metric_id, result in metrics.items()}


def test_run_with_empty_predictions_is_na_or_zero(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "reports"
    assert run(small_scenario, out) == 0
    captured = capsys.readouterr()
    assert "| Transaction pairwise F1 | 0.0000 | >= 0.95 | fail |" in captured.out
    assert captured.err == ""
    assert (out / "shop.md").is_file() and (out / "history.ndjson").is_file()
    report = load_report(out)
    assert report["predictions_present"] is False
    assert report["regressions"] == []
    assert report["counts"] == {
        "events": 4690,
        "transactions": 60,
        "links": 15,
        "entities": 3,
        "batches": 4,
        "faults": 1,
        "alerts": 0,
    }
    values = metric_values(report)
    assert len(values) == 15
    assert all(value in (None, 0.0) for value in values.values()), values
    assert values["false_alerts_per_flow_day"] == 0.0
    assert values["link_precision_exact_bridge"] is None
    assert values["transaction_pairwise_f1"] == 0.0
    markdown = (out / "shop.md").read_text(encoding="utf-8")
    assert markdown.startswith("# carto eval: shop\n")
    assert "Predictions: not present" in markdown
    # a second run appends to the history and reports no regressions
    assert run(small_scenario, out) == 0
    assert len((out / "history.ndjson").read_text(encoding="utf-8").splitlines()) == 2
    assert load_report(out)["regressions"] == []
    assert "Regressions against the previous run of this scenario: none." in capsys.readouterr().out


def test_enforce_targets_with_empty_predictions_returns_1(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(small_scenario, tmp_path / "reports", "--enforce-targets") == 1
    assert "metrics below target: " in capsys.readouterr().err
    assert (tmp_path / "reports" / "shop.json").is_file()


def test_self_check_is_perfect(small_scenario: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["self-check", *SMALL, "--sim-out", str(small_scenario)]) == 0
    captured = capsys.readouterr()
    assert "self-check: perfect" in captured.out
    assert "| Transaction pairwise F1 | 1.0000 | >= 0.95 | pass |" in captured.out
    assert "| Injected fault detection recall | n/a | >= 1.00 | n/a |" in captured.out
    assert captured.err == ""


@pytest.mark.slow
def test_self_check_on_fourteen_days_measures_every_metric(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # the two-day truth holds only the clock skew (expected_alert False); fourteen days hold all
    # six injected faults, so fault recall, time to detect, gap attribution and cause top-1 are
    # measured on real scenario values and must be perfect for the truth against itself
    sim_out = tmp_path / "shop14"
    argv = ["--scenario", "shop", "--days", "14", "--seed", "1", "--daily-volume", "30"]
    assert cli.main(["self-check", *argv, "--sim-out", str(sim_out)]) == 0
    captured = capsys.readouterr()
    assert "generated scenario shop into" in captured.out
    assert "self-check: perfect" in captured.out
    assert captured.err == ""
    assert "n/a" not in captured.out
    assert "| Injected fault detection recall | 1.0000 | >= 1.00 | pass |" in captured.out
    assert "| Time to detect after deadline (p95) | 0.0 s | <= 120 s | pass |" in captured.out
    assert "| Visibility gap correctly attributed | 1.0000 | >= 1.00 | pass |" in captured.out
    assert (
        "| Likely cause top-1 accuracy on injected faults | 1.0000 | >= 0.70 | pass |"
        in captured.out
    )
    truth = read_ground_truth(sim_out)
    assert len(truth.faults) == 6
    assert sum(1 for fault in truth.faults if fault.expected_alert) == 5


def test_run_scores_perfect_predictions_and_detects_the_regression_to_empty(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    predictions = tmp_path / "engine"
    truth = read_ground_truth(small_scenario)
    Predictions.from_truth(truth, iter_events(small_scenario)).write(predictions)
    out = tmp_path / "reports"
    assert run(small_scenario, out, "--predictions", str(predictions), "--enforce-targets") == 0
    report = load_report(out)
    assert report["predictions_present"] is True
    values = metric_values(report)
    assert values["transaction_pairwise_f1"] == 1.0
    assert values["entity_purity"] == 1.0
    assert values["manual_hop_recall"] == 1.0
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    assert {result["status"] for result in metrics.values()} == {"pass", "n/a"}
    capsys.readouterr()
    # a tolerance of 100 points swallows every ratio drop of the empty prediction (1.0 to 0.0 is
    # exactly 100 points); what remains is the loss of measurability, which no tolerance covers
    assert run(small_scenario, out, "--tolerance", "100", "--enforce-regressions") == 1
    capsys.readouterr()
    regressions = load_report(out)["regressions"]
    assert isinstance(regressions, list) and regressions
    assert all(line.endswith("-> n/a (no longer measurable)") for line in regressions)
    # the perfect prediction again, so the next run is compared with it
    assert run(small_scenario, out, "--predictions", str(predictions)) == 0
    assert load_report(out)["regressions"] == []
    capsys.readouterr()
    # the empty prediction after a perfect one is a regression on every defined metric
    assert run(small_scenario, out, "--enforce-regressions") == 1
    captured = capsys.readouterr()
    assert "regression(s)" in captured.err
    regressions = load_report(out)["regressions"]
    assert isinstance(regressions, list)
    assert any(line.startswith("transaction_pairwise_f1: 1.0000 -> 0.0000") for line in regressions)
    assert any(
        line.startswith("link_precision_exact_bridge: 1.0000 -> n/a") for line in regressions
    )
    assert "- transaction_pairwise_f1: 1.0000 -> 0.0000" in captured.out
    # without enforcement the same run passes
    assert run(small_scenario, out) == 0


def test_run_generates_a_missing_scenario_and_refuses_other_parameters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sim_out = tmp_path / "sim"
    out = tmp_path / "reports"
    tiny = ["run", "--scenario", "shop", "--days", "1", "--seed", "3", "--daily-volume", "5"]
    assert cli.main([*tiny, "--sim-out", str(sim_out), "--out", str(out)]) == 0
    assert "generated scenario shop into" in capsys.readouterr().out
    manifest_path = sim_out / GROUND_TRUTH_DIR / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert (manifest["days"], manifest["seed"], manifest["daily_volume"]) == (1, 3, 5)
    # the same request keeps the data
    assert cli.main([*tiny, "--sim-out", str(sim_out), "--out", str(out)]) == 0
    assert "generated scenario" not in capsys.readouterr().out
    # another volume without --regenerate is refused, and nothing next to the data is deleted
    sentinel = sim_out / "engine_output.txt"
    sentinel.write_text("keep", encoding="utf-8")
    tiny[-1] = "6"
    assert cli.main([*tiny, "--sim-out", str(sim_out), "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert "was generated with daily_volume=5" in captured.err and "--regenerate" in captured.err
    assert "generated scenario" not in captured.out
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["daily_volume"] == 5
    assert sentinel.read_text(encoding="utf-8") == "keep"
    # --regenerate replaces the run with the requested parameters
    assert cli.main([*tiny, "--sim-out", str(sim_out), "--out", str(out), "--regenerate"]) == 0
    assert "(--regenerate)" in capsys.readouterr().out
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["daily_volume"] == 6
    assert not sentinel.exists()


def test_run_keeps_and_scores_a_fault_free_ground_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # the 18.4 row "False alerts on fault-free days" invites a --no-faults run; carto-eval cannot
    # request one, so it scores what is there (with a note) instead of replacing it
    sim_out = tmp_path / "sim"
    generate(
        GenerationRequest(scenario="shop", days=1, seed=3, daily_volume=5, faults=False), sim_out
    )
    out = tmp_path / "reports"
    tiny = ["run", "--scenario", "shop", "--days", "1", "--seed", "3", "--daily-volume", "5"]
    assert cli.main([*tiny, "--sim-out", str(sim_out), "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "generated scenario" not in captured.out
    assert "was generated with faults=False; scoring it as it is" in captured.err
    manifest = json.loads((sim_out / GROUND_TRUTH_DIR / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["faults_enabled"] is False
    report = load_report(out)
    assert report["counts"]["faults"] == 0  # type: ignore[index]
    assert metric_values(report)["fault_detection_recall"] is None


def test_run_refuses_an_incomplete_or_unreadable_ground_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sim_out = tmp_path / "sim"
    out = tmp_path / "reports"
    generate(GenerationRequest(scenario="shop", days=1, seed=3, daily_volume=5), sim_out)
    tiny = ["run", "--scenario", "shop", "--days", "1", "--seed", "3", "--daily-volume", "5"]
    argv = [*tiny, "--sim-out", str(sim_out), "--out", str(out)]
    truth_dir = sim_out / GROUND_TRUTH_DIR
    events_path = truth_dir / EVENTS_FILE
    original = events_path.read_bytes()
    lines = original.decode("utf-8").splitlines()

    def refused(detail: str) -> None:
        assert cli.main(argv) == 2
        captured = capsys.readouterr()
        assert "is incomplete or unreadable" in captured.err
        assert detail in captured.err and "--regenerate" in captured.err
        assert not out.exists()

    # truncated at a line boundary: what an interrupted generation leaves behind a valid manifest
    half = len(lines) // 2
    events_path.write_text("\n".join(lines[:half]) + "\n", encoding="utf-8", newline="\n")
    refused(f"holds {half} records but the manifest counts {len(lines)}")
    # truncated in the middle of a line
    events_path.write_text("\n".join(lines[:3]) + "\n" + lines[3][:20], encoding="utf-8")
    refused("EventTruth")
    events_path.write_bytes(original)
    # a missing file
    (truth_dir / TRUTH_LINKS_FILE).rename(truth_dir / "links.bak")
    refused(TRUTH_LINKS_FILE)
    (truth_dir / "links.bak").rename(truth_dir / TRUTH_LINKS_FILE)
    # a corrupt manifest is refused before anything could be replaced
    manifest_path = truth_dir / MANIFEST_FILE
    manifest_path.write_text("{not json", encoding="utf-8")
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "manifest" in captured.err and "unreadable" in captured.err
    assert "--regenerate" in captured.err
    assert manifest_path.read_text(encoding="utf-8") == "{not json"
    # --regenerate recovers
    assert cli.main([*argv, "--regenerate"]) == 0
    capsys.readouterr()
    assert load_report(out)["counts"]["events"] == len(lines)  # type: ignore[index]


def test_out_that_is_a_file_returns_2(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "reports.txt"
    blocker.write_text("x", encoding="utf-8")
    assert run(small_scenario, blocker) == 2
    assert "cannot create" in capsys.readouterr().err
    assert blocker.read_text(encoding="utf-8") == "x"


def test_malformed_predictions_return_2(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    predictions = tmp_path / "engine"
    predictions.mkdir()
    (predictions / LINKS_FILE).write_text("[{", encoding="utf-8")
    assert run(small_scenario, tmp_path / "reports", "--predictions", str(predictions)) == 2
    captured = capsys.readouterr()
    assert "cannot read predictions" in captured.err and LINKS_FILE in captured.err
    assert not (tmp_path / "reports").exists()


def test_missing_predictions_directory_scores_the_empty_prediction(
    small_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "reports"
    assert run(small_scenario, out, "--predictions", str(tmp_path / "nowhere")) == 0
    assert "scoring the empty prediction" in capsys.readouterr().err
    assert load_report(out)["predictions_present"] is False


def test_usage_and_request_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert cli.main(["run"]) == 2
    assert cli.main(["--help"]) == 0
    capsys.readouterr()
    argv = ["run", "--days", "0", "--sim-out", str(tmp_path / "sim"), "--out", str(tmp_path / "r")]
    assert cli.main(argv) == 2
    assert "days must be at least 1" in capsys.readouterr().err
    assert cli.main(["self-check", "--scenario", "nope", "--sim-out", str(tmp_path / "sim")]) == 2
    assert "unknown scenario" in capsys.readouterr().err
    # a populated directory that is not a previous run is never emptied
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "keep.txt").write_text("keep", encoding="utf-8")
    argv = [
        "run",
        "--days",
        "1",
        "--daily-volume",
        "1",
        "--sim-out",
        str(populated),
        "--out",
        str(tmp_path / "r"),
    ]
    assert cli.main(argv) == 2
    assert "refusing to empty" in capsys.readouterr().err
    assert [path.name for path in populated.iterdir()] == ["keep.txt"]
