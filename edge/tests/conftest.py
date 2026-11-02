"""Options for the leak test over a full simulator run (``make leak SCENARIO=...``, spec 18.3),
and the pinned reference time of the plausible ``observed_at`` range (ADR 0036)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

import carto_edge.pipeline.pipeline as pipeline_module

PINNED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
"""The edge tests' wall clock for the range check: their records and the simulator's default
runs carry fixed dates from September and October 2026, which a real clock would drop as too
old from November on. Tests of the range itself pass the pipeline an explicit clock."""


@pytest.fixture(autouse=True, scope="session")
def _pinned_range_clock() -> Iterator[None]:
    """Session-wide, so module-scoped fixtures (an analyzer run reused by several tests) see
    the same clock as the tests that compare against them."""
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(pipeline_module, "wall_clock", lambda: PINNED_NOW)
        yield


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("carto-leak", "carto leak test (spec 18.3)")
    group.addoption("--scenario", default="shop", help="Simulator scenario to scan (default shop)")
    group.addoption(
        "--sim-out", default="sim-out", help="Directory of simulator runs and bundles (sim-out)"
    )
