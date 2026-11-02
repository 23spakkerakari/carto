"""carto-edge analyze end to end on a tiny simulator run (spec 4.1, 8.1.1; ADR 0017, 0024, 0025):
the bundle verifies, its counts match the events and the simulator, the locator map sits next to
it with one line per event, the state directory keeps the key and the reveal vault (never the
bundle), a second run over the same state directory reproduces event ids and tokens, event times
are checked against the run time or ``--as-of`` (ADR 0036), and usage errors exit 2 without
touching anything."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
import zstandard

from carto_core.bundle import iter_events, verify_bundle
from carto_edge.bundle import BUNDLE_FILES, default_locator_map_path
from carto_edge.cli.analyze import (
    AnalyzeError,
    AnalyzeResult,
    analyze,
    default_producer,
    default_state_dir,
    parse_as_of,
)
from carto_edge.cli.main import main
from carto_edge.config import EdgeSettings, PiiSettings, load_sources_file
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.runtime import build_runtime
from carto_schema.bundle import EVENTS_FILE
from carto_schema.event import CanonicalEvent, ObservedAtQuality
from carto_simulator.api import GenerationRequest, generate, run_as_of

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "simulator" / "analyze.shop.yaml"


@pytest.fixture(scope="module")
def sim_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("sim") / "shop"
    generate(GenerationRequest(days=1, daily_volume=5, seed=3), out)
    return out


@pytest.fixture(scope="module")
def first_run(sim_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> AnalyzeResult:
    out = tmp_path_factory.mktemp("bundles") / "shop.carto"
    return analyze(CONFIG, sim_dir, out, locator_map=True, detector=RegexDetector())


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """``main`` configures process-wide logging; put it back for the tests that follow."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configured, config = structlog.is_configured(), structlog.get_config()
    yield
    if configured:
        structlog.configure(**config)
    else:
        structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)


def events_of(bundle: Path) -> list[CanonicalEvent]:
    return list(iter_events(verify_bundle(bundle)))


def events_text(bundle: Path) -> str:
    data = (bundle / EVENTS_FILE).read_bytes()
    with zstandard.ZstdDecompressor().stream_reader(data) as reader:
        return reader.read().decode("utf-8")


def test_the_command_writes_a_bundle_that_verifies(
    sim_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,
) -> None:
    monkeypatch.setenv("CARTO_PII__ENABLED", "false")  # the regex detector keeps this fast
    out = tmp_path / "shop.carto"
    args = ["analyze", "--config", str(CONFIG), "--input", str(sim_dir), "--out", str(out)]
    assert main([*args, "--locator-map"]) == 0
    printed = capsys.readouterr().out
    verified = verify_bundle(out)
    assert verified.manifest.producer == default_producer()
    assert verified.manifest.counts.events > 0
    assert str(out.resolve()) in printed
    assert f"events          {verified.manifest.counts.events:,}" in printed
    assert str(default_locator_map_path(out)) in printed
    assert default_locator_map_path(out).is_file()
    assert (default_state_dir(out) / "keys" / "rotation.json").is_file()
    assert default_state_dir(out) == tmp_path.resolve() / "shop.carto.edge-state"


def test_manifest_counts_match_the_events_and_the_simulator(
    first_run: AnalyzeResult, sim_dir: Path
) -> None:
    manifest = first_run.manifest
    events = events_of(first_run.bundle_dir)
    assert len(events) == manifest.counts.events > 0
    assert sum(s.events_written for s in manifest.sources) == manifest.counts.events
    assert sum(s.records_read for s in manifest.sources) == manifest.counts.records_read
    assert manifest.counts.identifiers == sum(len(e.identifiers) for e in events)
    configured = {s.id for s in load_sources_file(CONFIG).sources}
    assert {s.source_id for s in manifest.sources} == configured
    assert {e.source_id for e in events} <= configured
    truth = json.loads((sim_dir / "ground_truth" / "manifest.json").read_text(encoding="utf-8"))
    for summary in manifest.sources:  # every record the simulator wrote was read
        assert summary.records_read == truth["counts"][summary.source_id], summary.source_id
    assert first_run.dropped == {} or sum(first_run.dropped.values()) == (
        manifest.counts.records_dropped
    )


def test_locator_map_has_one_line_per_event_next_to_the_bundle(first_run: AnalyzeResult) -> None:
    locator = first_run.locator_map
    assert locator is not None
    assert locator == default_locator_map_path(first_run.bundle_dir)
    assert locator.parent == first_run.bundle_dir.parent
    lines = [json.loads(line) for line in locator.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == first_run.manifest.counts.events
    sources = tuple(f"{s.source_id}:" for s in first_run.manifest.sources)
    assert all(line["key"].startswith(sources) for line in lines)
    assert {line["event_id"] for line in lines} == {
        e.event_id for e in events_of(first_run.bundle_dir)
    }
    assert {entry.name for entry in first_run.bundle_dir.iterdir()} == BUNDLE_FILES


def test_state_dir_keeps_the_key_and_the_vault_which_reveals_a_token(
    first_run: AnalyzeResult,
) -> None:
    state = first_run.state_dir
    assert (state / "keys" / "rotation.json").is_file()
    assert (state / "vault.sqlite").is_file()
    event = next(e for e in events_of(first_run.bundle_dir) if e.identifiers)
    token = next(i.token for i in event.identifiers if i.form == "raw")
    settings = EdgeSettings(state_dir=state, pii=PiiSettings(enabled=False))
    runtime = build_runtime(
        settings, load_sources_file(CONFIG), mode="analyze", detector=RegexDetector()
    )
    try:
        revealed = runtime.vault.reveal([token], now=event.observed_at)
    finally:
        runtime.close()
    assert set(revealed) == {token}
    value = revealed[token]
    assert value
    assert value not in events_text(first_run.bundle_dir)


def test_a_second_run_over_the_same_state_reproduces_ids_and_tokens(
    first_run: AnalyzeResult, sim_dir: Path, tmp_path: Path
) -> None:
    second = analyze(
        CONFIG,
        sim_dir,
        tmp_path / "again.carto",
        state_dir=first_run.state_dir,
        detector=RegexDetector(),
    )
    assert second.locator_map is None
    assert not default_locator_map_path(second.bundle_dir).exists()

    def tokens(bundle: Path) -> dict[str, list[tuple[str, str, str]]]:
        return {
            e.event_id: sorted((i.field, i.form, i.token) for i in e.identifiers)
            for e in events_of(bundle)
        }

    first_tokens, second_tokens = tokens(first_run.bundle_dir), tokens(second.bundle_dir)
    assert first_tokens.keys() == second_tokens.keys()
    assert first_tokens == second_tokens
    assert second.manifest.bundle_id != first_run.manifest.bundle_id


def test_rerun_into_a_non_empty_out_exits_2(
    first_run: AnalyzeResult, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (first_run.bundle_dir / EVENTS_FILE).read_bytes()
    args = ["--config", str(CONFIG), "--input", str(first_run.bundle_dir.parent)]
    assert main(["analyze", *args, "--out", str(first_run.bundle_dir)]) == 2
    assert "not empty" in capsys.readouterr().err
    assert (first_run.bundle_dir / EVENTS_FILE).read_bytes() == before
    verify_bundle(first_run.bundle_dir)


def test_missing_input_exits_2(
    tmp_path: Path, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--config", str(CONFIG), "--input", str(tmp_path / "nope")]
    assert main(["analyze", *args, "--out", str(tmp_path / "b.carto")]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert not (tmp_path / "b.carto").exists()
    assert not default_state_dir(tmp_path / "b.carto").exists()


def write_config(path: Path, source_type: str, config: str) -> Path:
    path.write_text(
        "systems:\n  - {id: sys_a, name: A}\n"
        f"sources:\n  - {{id: src_a, system: sys_a, type: {source_type}, config: {config}}}\n",
        encoding="utf-8",
    )
    return path


def test_a_non_upload_source_exits_2(
    tmp_path: Path, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path / "a.yaml", "sql", "{query: 'select 1'}")
    args = ["--config", str(config), "--input", str(tmp_path)]
    assert main(["analyze", *args, "--out", str(tmp_path / "b.carto")]) == 2
    assert "src_a" in capsys.readouterr().err
    assert not (tmp_path / "b.carto").exists()


def test_an_invalid_config_exits_2(
    tmp_path: Path, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "a.yaml"
    config.write_text("systems: [\n", encoding="utf-8")
    args = ["--config", str(config), "--input", str(tmp_path)]
    assert main(["analyze", *args, "--out", str(tmp_path / "b.carto")]) == 2
    assert "not valid YAML" in capsys.readouterr().err
    missing = ["--config", str(tmp_path / "none.yaml"), "--input", str(tmp_path)]
    assert main(["analyze", *missing, "--out", str(tmp_path / "b.carto")]) == 2


def test_state_dir_inside_the_bundle_and_bad_tenant_are_usage_errors(
    sim_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "b.carto"
    with pytest.raises(AnalyzeError, match="state-dir") as inside:
        analyze(CONFIG, sim_dir, out, state_dir=out / "state")
    assert inside.value.code == 2
    with pytest.raises(AnalyzeError, match="invalid settings") as tenant:
        analyze(CONFIG, sim_dir, out, tenant_id="Not A Tenant!")
    assert tenant.value.code == 2
    assert not out.exists()


def test_an_input_over_the_limits_fails_with_1_and_leaves_no_bundle(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "app.log").write_text("x" * 200 + "\n", encoding="utf-8")
    config = write_config(
        tmp_path / "a.yaml", "upload", "{kind: log, paths: ['app.log'], max_file_bytes: 10}"
    )
    out = tmp_path / "b.carto"
    with pytest.raises(AnalyzeError, match="reading the inputs failed") as failed:
        analyze(config, tmp_path / "in", out, locator_map=True, detector=RegexDetector())
    assert failed.value.code == 1
    assert not out.exists()
    assert not default_locator_map_path(out).exists()


# --- the plausible range and --as-of (ADR 0036) ------------------------------------------------


def test_a_clean_run_reports_no_event_out_of_range(first_run: AnalyzeResult) -> None:
    counts = first_run.manifest.counts
    assert (counts.events_too_old, counts.events_future) == (0, 0)
    assert not any("--as-of" in note for note in first_run.manifest.notes)


def test_as_of_long_after_the_run_drops_every_record_as_too_old(
    sim_dir: Path, tmp_path: Path
) -> None:
    late = datetime(2027, 6, 1, tzinfo=UTC)
    result = analyze(CONFIG, sim_dir, tmp_path / "late.carto", detector=RegexDetector(), as_of=late)
    counts = result.manifest.counts
    assert counts.events == 0
    assert counts.events_too_old == counts.records_read == counts.records_dropped > 0
    assert result.dropped == {"too_old": counts.records_read}
    manifest_md = (tmp_path / "late.carto" / "MANIFEST.md").read_text(encoding="utf-8")
    assert "Dropped as older than retention (ADR 0036)" in manifest_md
    assert any(late.isoformat() in note and "--as-of" in note for note in result.manifest.notes)


def test_as_of_before_the_run_keeps_its_events_with_the_ingest_time(
    sim_dir: Path, tmp_path: Path
) -> None:
    early = datetime(2026, 9, 1, tzinfo=UTC)
    result = analyze(
        CONFIG, sim_dir, tmp_path / "early.carto", detector=RegexDetector(), as_of=early
    )
    counts = result.manifest.counts
    assert counts.events_future == counts.events > 0
    events = events_of(tmp_path / "early.carto")
    assert all(event.observed_at_quality is ObservedAtQuality.INGEST for event in events)


def test_the_simulators_as_of_keeps_every_event_of_the_run(
    sim_dir: Path, tmp_path: Path, first_run: AnalyzeResult
) -> None:
    result = analyze(
        CONFIG,
        sim_dir,
        tmp_path / "asof.carto",
        detector=RegexDetector(),
        as_of=run_as_of(sim_dir),
    )
    assert result.manifest.counts.events == first_run.manifest.counts.events
    assert result.manifest.counts.events_too_old == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-08", datetime(2026, 10, 8, tzinfo=UTC)),
        ("2026-10-08T00:00:00Z", datetime(2026, 10, 8, tzinfo=UTC)),
        ("2026-10-08T02:00:00+02:00", datetime(2026, 10, 8, tzinfo=UTC)),
    ],
)
def test_as_of_accepts_a_date_or_a_time_with_an_offset(text: str, expected: datetime) -> None:
    assert parse_as_of(text) == expected


@pytest.mark.parametrize("text", ["2026-10-08T00:00:00", "yesterday", "2026-13-01"])
def test_as_of_refuses_a_naive_time_or_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="--as-of"):
        parse_as_of(text)


def test_the_command_takes_as_of(
    sim_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,
) -> None:
    monkeypatch.setenv("CARTO_PII__ENABLED", "false")
    out = tmp_path / "cli.carto"
    args = ["analyze", "--config", str(CONFIG), "--input", str(sim_dir), "--out", str(out)]
    assert main([*args, "--as-of", "2027-06-01"]) == 0
    printed = capsys.readouterr().out
    records = verify_bundle(out).manifest.counts.records_read
    assert f"out of range    {records:,} too old (dropped), 0 in the future" in printed
