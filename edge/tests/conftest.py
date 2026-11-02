"""Options for the leak test over a full simulator run (``make leak SCENARIO=...``, spec 18.3),
and the pinned reference time of the plausible ``observed_at`` range (ADR 0036)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import carto_edge.pipeline.pipeline as pipeline_module

PINNED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
"""The edge tests' wall clock for the range check: their records and the simulator's default
runs carry fixed dates from September and October 2026, which a real clock would drop as too
old from November on. Tests of the range itself pass the pipeline an explicit clock."""


@pytest.fixture(autouse=True)
def _pinned_range_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "wall_clock", lambda: PINNED_NOW)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("carto-leak", "carto leak test (spec 18.3)")
    group.addoption("--scenario", default="shop", help="Simulator scenario to scan (default shop)")
    group.addoption(
        "--sim-out", default="sim-out", help="Directory of simulator runs and bundles (sim-out)"
    )
