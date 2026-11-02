"""The eval driver end to end in memory (plan M2 package I; ADR 0049): shop for 3 days at 200
orders a day and people_ops for 3 days, through the real analyzer (regex PII detector for
speed), :class:`InMemoryEngine`, the field mapping, the acceptance policy and the scorer. The
numbers themselves are reported, not asserted: a 3-day window is below the profiler's floors
for several fields. What is asserted is the contract: every truth link field of shop is reached
by an engine field, the link metrics are computed, the prediction files validate, the history
grows, bundles are reused or replaced as asked, and the CLI path works."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from carto_core.discover import ENGINE_ENTITIES_FILE, ENGINE_LINKS_FILE, SUMMARY_FILE
from carto_edge.pipeline.pii import RegexDetector
from carto_eval import cli, driver
from carto_eval.predictions import Predictions
from carto_eval.report import read_history
from carto_eval.targets import (
    ENTITY_PURITY,
    LINK_PRECISION_COMPOSITE,
    LINK_PRECISION_EXACT_BRIDGE,
    LINK_RECALL_COMPOSITE,
    LINK_RECALL_EXACT_BRIDGE,
)

CLOCK = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def shop_run(tmp_path_factory: pytest.TempPathFactory) -> driver.EvalRun:
    root = tmp_path_factory.mktemp("shop")
    return driver.run(
        "shop",
        3,
        1,
        200,
        root / "shop",
        root / "reports",
        engine=driver.InMemoryEngine(),
        detector=RegexDetector(),
        clock=lambda: CLOCK,
    )


@pytest.fixture(scope="module")
def people_ops_run(tmp_path_factory: pytest.TempPathFactory) -> driver.EvalRun:
    root = tmp_path_factory.mktemp("people_ops")
    return driver.run(
        "people_ops",
        3,
        1,
        800,
        root / "people_ops",
        root / "reports",
        engine=driver.InMemoryEngine(),
        detector=RegexDetector(),
        clock=lambda: CLOCK,
    )


def test_shop_reaches_every_truth_link_field_and_scores_links(shop_run: driver.EvalRun) -> None:
    run = shop_run
    assert run.mapping.unreached_truth_fields == ()
    assert run.mapping.locator_mismatches == 0
    assert run.mapping.predicted_links > 0 and run.mapping.family_links > 0
    values = run.scorecard.values()
    assert values[LINK_PRECISION_EXACT_BRIDGE] is not None
    assert values[LINK_RECALL_EXACT_BRIDGE] is not None
    assert values[ENTITY_PURITY] is not None
    # the metrics are measured against the engine, so the numbers travel in the report files
    report = json.loads(run.json_path.read_text(encoding="utf-8"))
    assert report["predictions_present"] is True
    assert report["metrics"][LINK_RECALL_EXACT_BRIDGE]["value"] == values[LINK_RECALL_EXACT_BRIDGE]
    assert report["generated_at"] == "2026-10-18T12:00:00Z"
    # the engine found the shop's exact links (L01, L04, L05, L07) and its bridges
    predicted = {
        (link.a.field, link.b.field)
        for link in run.predictions.links
        if link.link_type in ("exact", "bridge")
    }
    predicted_sets = {frozenset(pair) for pair in predicted}
    for a, b in (
        ("cart_id", "cart_id"),
        ("merchant_ref", "merchantRef"),
        ("order_id", "order_ref"),
        ("po_num", "po_num"),
        ("cart_id", "order_id"),
    ):
        assert frozenset((a, b)) in predicted_sets, (a, b)
    assert values[LINK_RECALL_EXACT_BRIDGE] >= 0.75


def test_outputs_validate_and_the_markdown_carries_the_sections(shop_run: driver.EvalRun) -> None:
    run = shop_run
    for name in (ENGINE_LINKS_FILE, ENGINE_ENTITIES_FILE, SUMMARY_FILE):
        assert (run.engine_dir / name).is_file()
    loaded = Predictions.load(run.predictions_dir)
    assert loaded.present and loaded.links == run.predictions.links
    assert loaded.entities == run.predictions.entities
    text = run.markdown_path.read_text(encoding="utf-8")
    assert "## Field mapping (ADR 0049)" in text
    assert "Acceptance policy (ADR 0049 item 2)" in text
    assert "## Engine run" in text
    assert "weights v1-hand-set" in text
    payload = json.loads(run.mapping_path.read_text(encoding="utf-8"))
    assert payload["proposal_threshold"] == 0.5
    assert payload["discover"]["weights_version"] == "v1-hand-set"
    assert payload["discover"]["settings"]["linker"]["proposal_threshold"] == 0.5
    assert payload["mapping"]["form_variants"] >= 0
    summary = json.loads((run.engine_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["as_of"] == run.prepared.as_of.isoformat()
    assert summary["link"]["full"] is True and summary["profile"]["full"] is True
    history = read_history(run.json_path.parent / "history.ndjson")
    assert len(history) == 1 and history[0].predictions_present


def test_no_token_reaches_the_engine_files(shop_run: driver.EvalRun) -> None:
    """Spec 2.3 invariant 2: the engine files carry references, counts and scores only."""
    tokens: set[str] = set()
    from carto_core.bundle import iter_events  # noqa: PLC0415

    for event in iter_events(shop_run.prepared.verified):
        tokens.update(identifier.token for identifier in event.identifiers)
        if len(tokens) > 2000:
            break
    text = "".join(
        (shop_run.engine_dir / name).read_text(encoding="utf-8")
        for name in (ENGINE_LINKS_FILE, ENGINE_ENTITIES_FILE, SUMMARY_FILE)
    )
    assert not any(token in text for token in tokens)
    assert "t1." not in text


def test_people_ops_runs_and_lists_what_the_window_misses(
    people_ops_run: driver.EvalRun,
) -> None:
    run = people_ops_run
    values = run.scorecard.values()
    assert values[LINK_RECALL_EXACT_BRIDGE] is not None
    assert values[LINK_RECALL_COMPOSITE] is not None
    # three days hold too few HRIS and payroll rows for the 50-distinct floor; the report says
    # which truth link fields no predicted link reached instead of scoring silently
    assert run.mapping.unlinked_truth_fields or values[LINK_RECALL_EXACT_BRIDGE] == 1.0
    assert values[LINK_PRECISION_COMPOSITE] is None or 0.0 <= values[LINK_PRECISION_COMPOSITE] <= 1
    text = run.markdown
    assert "Truth link fields" in text


def test_bundle_reuse_and_replacement(shop_run: driver.EvalRun) -> None:
    sim_out = shop_run.prepared.sim_out
    out = sim_out.parent / "reports2"
    reused = driver.run(
        "shop",
        3,
        1,
        200,
        sim_out,
        out,
        engine=driver.InMemoryEngine(),
        detector=RegexDetector(),
        reuse_bundle=True,
        clock=lambda: CLOCK,
    )
    assert reused.prepared.analyzed is None
    first_bundle = shop_run.prepared.verified.manifest.bundle_id
    assert reused.prepared.verified.manifest.bundle_id == first_bundle
    assert reused.scorecard.values() == shop_run.scorecard.values()
    replaced = driver.run(
        "shop",
        3,
        1,
        200,
        sim_out,
        out,
        engine=driver.InMemoryEngine(),
        detector=RegexDetector(),
        clock=lambda: CLOCK,
    )
    assert replaced.prepared.analyzed is not None
    # the state directory is kept, so the re-analysis tokenizes identically
    assert replaced.scorecard.values() == shop_run.scorecard.values()
    history = read_history(out / "history.ndjson")
    assert len(history) == 2 and replaced.report.regressions == []


def test_parameter_mismatch_and_missing_config_are_driver_errors(
    shop_run: driver.EvalRun, tmp_path: Path
) -> None:
    sim_out = shop_run.prepared.sim_out
    with pytest.raises(driver.DriverError, match="generated with"):
        driver.run("shop", 4, 1, 200, sim_out, tmp_path, engine=driver.InMemoryEngine())
    with pytest.raises(driver.DriverError, match="no analyzer configuration"):
        driver.run(
            "shop",
            3,
            1,
            200,
            sim_out,
            tmp_path,
            engine=driver.InMemoryEngine(),
            config=tmp_path / "missing.yaml",
            detector=RegexDetector(),
        )
    with pytest.raises(driver.DriverError, match="days must be at least 1"):
        driver.run("shop", 0, 1, 200, sim_out, tmp_path, engine=driver.InMemoryEngine())


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """The analyzer and the engine log through structlog; the CLI may configure it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configured, config = structlog.is_configured(), structlog.get_config()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    if configured:
        structlog.configure(**config)
    else:
        structlog.reset_defaults()


@pytest.mark.usefixtures("restore_logging")
def test_cli_engine_memory(
    shop_run: driver.EvalRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sim_out = shop_run.prepared.sim_out
    code = cli.main(
        [
            "run",
            "--engine",
            "memory",
            "--reuse-bundle",
            "--scenario",
            "shop",
            "--days",
            "3",
            "--daily-volume",
            "200",
            "--sim-out",
            str(sim_out),
            "--out",
            str(tmp_path / "reports"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "# carto eval: shop" in out and "## Field mapping (ADR 0049)" in out
    assert (tmp_path / "reports" / "shop.mapping.json").is_file()
    # --enforce-targets fails the run (the engine does not reach the targets on 3 days)
    code = cli.main(
        [
            "run",
            "--engine",
            "memory",
            "--reuse-bundle",
            "--enforce-targets",
            "--scenario",
            "shop",
            "--days",
            "3",
            "--daily-volume",
            "200",
            "--sim-out",
            str(sim_out),
            "--out",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 1
    # a parameter mismatch is a usage error
    code = cli.main(
        [
            "run",
            "--engine",
            "memory",
            "--scenario",
            "shop",
            "--days",
            "5",
            "--sim-out",
            str(sim_out),
            "--out",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 2
    assert "generated with" in capsys.readouterr().err
