"""``carto-sim`` command line and the request contract (spec 19 inputs)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from carto_simulator import api, cli
from carto_simulator.api import GenerationRequest
from carto_simulator.scenarios import get_scenario, list_scenarios


def test_generate_writes_files_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "generate", "--scenario", "shop", "--days", "2", "--seed", "4", "--daily-volume", "8",
        "--start-date", "2026-09-23", "--out", str(tmp_path / "out"), "--noise-rate", "0.1",
        "--pii-density", "0.5",
    ]  # fmt: skip
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "transactions: 16" in out and "elapsed:" in out
    assert (tmp_path / "out/ground_truth/manifest.json").exists()
    assert (tmp_path / "out/webstore/app-2026-09-23.ndjson").exists()
    assert (tmp_path / "out/warehouse/purchase_orders.sql").exists()


def test_no_faults_flag(tmp_path: Path) -> None:
    argv = ["generate", "--days", "1", "--daily-volume", "3", "--out", str(tmp_path), "--no-faults"]
    assert cli.main(argv) == 0
    assert (tmp_path / "ground_truth/faults.json").read_text(encoding="utf-8") == "[]\n"


def test_unknown_scenario_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["generate", "--scenario", "nope", "--out", str(tmp_path)]) == 2
    assert "unknown scenario 'nope'" in capsys.readouterr().err
    assert not (tmp_path / "ground_truth").exists()


def test_invalid_request_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["generate", "--days", "0", "--out", str(tmp_path)]) == 2
    assert "days must be at least 1" in capsys.readouterr().err


def test_unusable_out_path_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")
    argv = ["generate", "--days", "1", "--daily-volume", "1", "--out", str(target)]
    assert cli.main(argv) == 2
    assert "exists and is not a directory" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "x"
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "keep.txt").write_text("keep", encoding="utf-8")
    argv = ["generate", "--days", "1", "--daily-volume", "1", "--out", str(populated)]
    assert cli.main(argv) == 2
    assert "refusing to empty" in capsys.readouterr().err
    assert [p.name for p in populated.iterdir()] == ["keep.txt"]


def test_list_prints_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.split() == ["people_ops", "shop"]
    assert list_scenarios() == ["people_ops", "shop"]


def test_registry() -> None:
    scenario = get_scenario("shop")
    assert scenario.name == "shop" and scenario.description
    with pytest.raises(KeyError, match="known scenarios: people_ops, shop"):
        get_scenario("payer")


def test_defaults_match_the_cli() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["generate", "--out", "x"])
    request = GenerationRequest()
    assert (args.scenario, args.days, args.seed, args.daily_volume) == (
        request.scenario, request.days, request.seed, request.daily_volume,
    )  # fmt: skip
    assert args.start_date == request.start_date == date(2026, 9, 23)
    assert args.noise_rate == request.noise_rate == 1.0
    assert args.pii_density == request.pii_density == 0.3
    assert request.faults and request.days == 14 and request.daily_volume == 800


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: GenerationRequest(days=0), "days must be at least 1"),
        (lambda: GenerationRequest(daily_volume=0), "daily_volume must be at least 1"),
        (lambda: GenerationRequest(noise_rate=10.5), "noise_rate must be between 0 and 10"),
        (lambda: GenerationRequest(noise_rate=-0.1), "noise_rate must be between 0 and 10"),
        (lambda: GenerationRequest(pii_density=1.5), "pii_density must be between 0 and 1"),
    ],
)
def test_request_validation(factory: Callable[[], GenerationRequest], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_scenario_refuses_a_request_for_another_scenario(tmp_path: Path) -> None:
    request = GenerationRequest(scenario="payer", days=1, daily_volume=1)
    with pytest.raises(ValueError, match="not 'shop'"):
        get_scenario("shop").generate(request, tmp_path)


def test_as_of_is_the_start_of_the_day_after_the_run() -> None:
    """ADR 0036: the time to analyze a fixed-date batch run as of."""
    assert api.as_of_for(date(2026, 9, 23), 14) == datetime(2026, 10, 7, tzinfo=UTC)


def test_as_of_reads_the_runs_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "shop"
    api.generate(GenerationRequest(days=2, daily_volume=5, seed=3), out)
    assert api.run_as_of(out) == datetime(2026, 9, 25, tzinfo=UTC)
    assert cli.main(["as-of", "--out", str(out)]) == 0
    assert capsys.readouterr().out.strip() == "2026-09-25T00:00:00Z"


def test_as_of_without_a_run_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["as-of", "--out", str(tmp_path)]) == 2
    assert "no readable run manifest" in capsys.readouterr().err
