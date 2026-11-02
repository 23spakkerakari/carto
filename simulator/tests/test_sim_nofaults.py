"""Fault-free runs, determinism by seed and output directory hygiene (spec 19)."""

from __future__ import annotations

import filecmp
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from carto_simulator.api import GenerationRequest, generate
from carto_simulator.ground_truth import GroundTruth, iter_events, read_ground_truth

START = date(2026, 9, 23)


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, GroundTruth]:
    out_dir = tmp_path_factory.mktemp("nofaults")
    request = GenerationRequest(
        days=10, seed=3, daily_volume=20, start_date=START, faults=False, noise_rate=0.2
    )
    generate(request, out_dir)
    return out_dir, read_ground_truth(out_dir)


def test_no_faults_means_empty_faults_and_a_file_on_day_9(
    clean_run: tuple[Path, GroundTruth],
) -> None:
    out_dir, truth = clean_run
    assert truth.faults == []
    assert (out_dir / "ground_truth/faults.json").read_text(encoding="utf-8") == "[]\n"
    day_9 = START + timedelta(days=8)
    assert list(out_dir.glob(f"shipping/outbound/SHIP_{day_9:%Y%m%d}_*.csv"))
    export_log = (out_dir / f"warehouse/export-job-{day_9:%Y-%m-%d}.log").read_text("utf-8")
    assert "Permission denied" not in export_log
    assert "SFTP upload complete" in export_log
    sql = (out_dir / "warehouse/purchase_orders.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE" not in sql
    assert not (out_dir / "warehouse/purchase_orders.renamed.csv").exists()
    assert not {"L07b", "L11b", "L13b"} & {link.link_id for link in truth.links}
    assert "L15" in {link.link_id for link in truth.links}
    assert "sys_warehouse/src_wms_db/po_number" not in truth.markers.identifier_values
    assert not any(e.is_error for e in iter_events(out_dir))
    assert truth.manifest.faults_enabled is False


def test_warehouse_clock_skew_applies_even_without_faults(
    clean_run: tuple[Path, GroundTruth],
) -> None:
    out_dir, truth = clean_run
    skew = {s.source_id: s.clock_skew_seconds for s in truth.sources}
    assert skew["src_wms_db"] == skew["src_wms_export_log"] == 90
    first_row = next(e for e in iter_events(out_dir) if e.source_id == "src_wms_db")
    csv_lines = (out_dir / "warehouse/purchase_orders.csv").read_text("utf-8").splitlines()
    rendered = csv_lines[1].rsplit(",", 2)[-2]
    assert rendered != first_row.observed_at.strftime("%Y-%m-%d %H:%M:%S")


def _digests(out_dir: Path) -> dict[str, str]:
    manifest = json.loads((out_dir / "ground_truth/manifest.json").read_text(encoding="utf-8"))
    digests: dict[str, str] = manifest["sha256"]
    return digests


def test_same_request_is_byte_identical_and_seed_changes_output(tmp_path: Path) -> None:
    request = GenerationRequest(days=2, seed=11, daily_volume=10, start_date=START)
    first = generate(request, tmp_path / "a")
    second = generate(request, tmp_path / "b")
    assert first.manifest == second.manifest
    assert _digests(tmp_path / "a") == _digests(tmp_path / "b")
    for path in sorted((tmp_path / "a" / "ground_truth").glob("*")):
        assert filecmp.cmp(path, tmp_path / "b" / "ground_truth" / path.name, shallow=False)
    other = generate(
        GenerationRequest(days=2, seed=12, daily_volume=10, start_date=START), tmp_path / "c"
    )
    assert other.manifest.sha256 != first.manifest.sha256
    assert not filecmp.cmp(
        tmp_path / "a/ground_truth/event_txn.ndjson",
        tmp_path / "c/ground_truth/event_txn.ndjson",
        shallow=False,
    )


def test_regeneration_removes_stale_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    request = GenerationRequest(days=1, seed=1, daily_volume=5, start_date=START)
    generate(request, out_dir)  # a previous run makes the directory ours to empty
    (out_dir / "stale").mkdir()
    (out_dir / "stale/old.txt").write_text("old", encoding="utf-8")
    (out_dir / "old.ndjson").write_text("old", encoding="utf-8")
    generate(request, out_dir)
    assert not (out_dir / "stale").exists()
    assert not (out_dir / "old.ndjson").exists()
    assert (out_dir / "ground_truth/manifest.json").exists()
    assert (out_dir / "README.md").exists()


def test_empty_existing_directory_is_used(tmp_path: Path) -> None:
    generate(GenerationRequest(days=1, seed=1, daily_volume=2, start_date=START), tmp_path)
    assert (tmp_path / "ground_truth/manifest.json").exists()


def test_populated_directory_without_a_previous_run_is_refused(tmp_path: Path) -> None:
    out_dir = tmp_path / "checkout"
    (out_dir / ".git").mkdir(parents=True)
    (out_dir / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (out_dir / "src").mkdir()
    (out_dir / "src/main.py").write_text("print(1)\n", encoding="utf-8")
    (out_dir / "notes.txt").write_text("notes", encoding="utf-8")
    request = GenerationRequest(days=1, seed=1, daily_volume=2, start_date=START)
    with pytest.raises(ValueError, match=r"refusing to empty .*no ground_truth/manifest\.json"):
        generate(request, out_dir)
    assert (out_dir / ".git/HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert (out_dir / "src/main.py").exists() and (out_dir / "notes.txt").exists()
    assert sorted(p.name for p in out_dir.iterdir()) == [".git", "notes.txt", "src"]


def test_out_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")
    request = GenerationRequest(days=1, seed=1, daily_volume=1, start_date=START)
    with pytest.raises(ValueError, match="exists and is not a directory"):
        generate(request, target)
    assert target.read_text(encoding="utf-8") == "x"


def test_short_runs_skip_faults_outside_the_window(tmp_path: Path) -> None:
    result = generate(
        GenerationRequest(days=6, seed=5, daily_volume=10, start_date=START), tmp_path
    )
    truth = read_ground_truth(tmp_path)
    assert [f.fault_id for f in truth.faults] == [
        "f1_payments_503_spike",
        "f6_warehouse_clock_skew",
    ]
    assert result.manifest.counts["transactions"] == 4 * 10 + 2 * 5
    assert "po_number" not in (tmp_path / "warehouse/purchase_orders.sql").read_text("utf-8")
