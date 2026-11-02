"""Programmatic entry point: ``generate(request, out_dir)``. The CLI and the eval harness call this.

Scenarios register in :mod:`carto_simulator.scenarios`. Generation is deterministic: the same
request produces byte-identical output (spec 19, "Deterministic by seed").

Batch runs have fixed dates, so a product clock would sooner or later find their events older
than retention (ADR 0036). :func:`run_as_of` is the reference time to analyze a run as of: the
start of the day after its last day, in UTC.

ADR 0036 note: a run longer than event retention loses its first days to the range, as a
backfill deeper than retention would.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

from carto_simulator.ground_truth import GROUND_TRUTH_DIR, MANIFEST_FILE, Manifest

GENERATOR_VERSION = "0.1.0"

DEFAULT_SCENARIO = "shop"
DEFAULT_DAYS = 14
DEFAULT_SEED = 1
DEFAULT_DAILY_VOLUME = 800
DEFAULT_START_DATE = date(2026, 9, 23)  # a Wednesday; puts the spec's fault days on weekdays
DEFAULT_NOISE_RATE = 1.0
DEFAULT_PII_DENSITY = 0.3


@dataclass(frozen=True)
class GenerationRequest:
    """Inputs per spec 19: scenario, days, daily volume, seed, faults, noise, PII density."""

    scenario: str = DEFAULT_SCENARIO
    days: int = DEFAULT_DAYS
    seed: int = DEFAULT_SEED
    daily_volume: int = DEFAULT_DAILY_VOLUME
    start_date: date = DEFAULT_START_DATE
    faults: bool = True
    noise_rate: float = DEFAULT_NOISE_RATE
    pii_density: float = DEFAULT_PII_DENSITY

    def __post_init__(self) -> None:
        if self.days < 1:
            msg = "days must be at least 1"
            raise ValueError(msg)
        if self.daily_volume < 1:
            msg = "daily_volume must be at least 1"
            raise ValueError(msg)
        if not 0.0 <= self.noise_rate <= 10.0:
            msg = "noise_rate must be between 0 and 10 (multiplier on the scenario's noise)"
            raise ValueError(msg)
        if not 0.0 <= self.pii_density <= 1.0:
            msg = "pii_density must be between 0 and 1"
            raise ValueError(msg)


@dataclass(frozen=True)
class GenerationResult:
    out_dir: Path
    ground_truth_dir: Path
    manifest: Manifest


class Scenario(Protocol):
    """A scenario writes native files under ``out_dir`` and ``ground_truth/`` next to them."""

    name: str
    description: str

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult: ...


def as_of_for(start_date: date, days: int) -> datetime:
    """Midnight UTC at the start of the day after the run's last day. The range of ADR 0036
    then reaches a day past it (a zone west of UTC ends its last local day up to a day later in
    UTC) and back over a run as long as event retention."""
    return datetime.combine(start_date + timedelta(days=days), time(), UTC)


def run_as_of(out_dir: Path) -> datetime:
    """:func:`as_of_for` the run whose ground truth is under ``out_dir``. Raises ``OSError`` or
    ``ValueError`` when the manifest is missing or unreadable."""
    manifest = Manifest.model_validate_json(
        (out_dir / GROUND_TRUTH_DIR / MANIFEST_FILE).read_bytes()
    )
    return as_of_for(manifest.start_date, manifest.days)


def generate(request: GenerationRequest, out_dir: Path) -> GenerationResult:
    """Generate ``request.scenario`` into ``out_dir`` (created if missing, replaced if present)."""
    from carto_simulator.scenarios import get_scenario  # noqa: PLC0415  (registry import cycle)

    scenario = get_scenario(request.scenario)
    return scenario.generate(request, out_dir)
