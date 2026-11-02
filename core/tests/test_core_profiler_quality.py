"""``q(field)``, the field profile and the domain size (spec 9.1, 9.3, 18.1; ADR 0047, 0048,
0055): ``q`` is monotonic in the distinct ratio and the top-shape share, every term saturates at
its spec threshold, the floors decide candidacy, and ``domain_size`` follows the shape alphabet
of :func:`carto_schema.forms.shape`."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from carto_core.profiler.model import FieldProfile, LengthStats, WindowSketch
from carto_core.profiler.quality import (
    build_profile,
    identifier_quality,
    is_candidate,
    length_stats,
    top_shapes,
    top_token_share,
)
from carto_core.profiler.shapes import domain_size
from carto_core.profiler.sketches import DaySketchBuilder, merge_window
from carto_core.settings import ProfilerSettings
from carto_core.store.model import FieldForm, IdentifierRow

END = FieldForm("sys_a/tpl_1/order_ref", "raw")
DEFAULT = ProfilerSettings()
STABLE = LengthStats(min=10, max=10, mean=10.0, stdev=0.0)
unit = st.floats(min_value=0.0, max_value=1.0)
counts = st.integers(min_value=1, max_value=100_000)


# -- q(field) ---------------------------------------------------------------------------------


def test_quality_is_one_for_a_perfect_identifier_and_zero_for_an_empty_field() -> None:
    assert identifier_quality(1000, 1000, 1.0, STABLE, 0.0, DEFAULT) == 1.0
    assert identifier_quality(0, 0, 1.0, STABLE, 0.0, DEFAULT) == 0.0


def test_each_term_saturates_at_its_threshold() -> None:
    # Distinct ratio is capped at 1: a HyperLogLog estimate above the count adds nothing.
    assert identifier_quality(100, 150, 1.0, STABLE, 0.0, DEFAULT) == 1.0
    # Concentration at or above concentration_max_share zeroes that term (spec 9.1: under 20%).
    at_share = identifier_quality(100, 100, 1.0, STABLE, DEFAULT.concentration_max_share, DEFAULT)
    assert at_share == pytest.approx(0.75)
    assert identifier_quality(100, 100, 1.0, STABLE, 0.9, DEFAULT) == pytest.approx(0.75)
    assert identifier_quality(100, 100, 1.0, STABLE, 0.1, DEFAULT) == pytest.approx(0.875)
    # Length stability: a coefficient of variation of one or more zeroes the term.
    wild = LengthStats(min=1, max=50, mean=5.0, stdev=10.0)
    assert identifier_quality(100, 100, 1.0, wild, 0.0, DEFAULT) == pytest.approx(0.75)
    half = LengthStats(min=1, max=50, mean=10.0, stdev=5.0)
    assert identifier_quality(100, 100, 1.0, half, 0.0, DEFAULT) == pytest.approx(0.875)
    assert identifier_quality(100, 100, 1.0, None, 0.0, DEFAULT) == pytest.approx(0.75)


@given(count=counts, d1=counts, d2=counts, shape=unit, share=unit)
def test_quality_is_monotonic_in_the_distinct_ratio(
    count: int, d1: int, d2: int, shape: float, share: float
) -> None:
    low, high = sorted((d1, d2))
    assert identifier_quality(count, low, shape, STABLE, share, DEFAULT) <= identifier_quality(
        count, high, shape, STABLE, share, DEFAULT
    )


@given(count=counts, distinct=counts, s1=unit, s2=unit, share=unit)
def test_quality_is_monotonic_in_the_top_shape_share(
    count: int, distinct: int, s1: float, s2: float, share: float
) -> None:
    low, high = sorted((s1, s2))
    assert identifier_quality(count, distinct, low, STABLE, share, DEFAULT) <= identifier_quality(
        count, distinct, high, STABLE, share, DEFAULT
    )


@given(count=counts, distinct=counts, shape=unit, share=unit)
def test_quality_stays_in_the_unit_interval(
    count: int, distinct: int, shape: float, share: float
) -> None:
    assert 0.0 <= identifier_quality(count, distinct, shape, STABLE, share, DEFAULT) <= 1.0


# -- candidacy --------------------------------------------------------------------------------


def _profile(quality: float, distinct: int) -> FieldProfile:
    return FieldProfile(
        end=END,
        count=distinct,
        distinct=distinct,
        null_rate=0.0,
        shapes=(),
        length_stats=None,
        top_share=0.0,
        quality=quality,
        is_candidate=False,
        first_seen=None,
        last_seen=None,
    )


def test_is_candidate_applies_both_floors() -> None:
    assert is_candidate(_profile(DEFAULT.quality_floor, DEFAULT.min_distinct), DEFAULT)
    assert not is_candidate(_profile(DEFAULT.quality_floor - 0.01, DEFAULT.min_distinct), DEFAULT)
    assert not is_candidate(_profile(1.0, DEFAULT.min_distinct - 1), DEFAULT)
    loose = ProfilerSettings(quality_floor=0.1, min_distinct=2)
    assert is_candidate(_profile(0.1, 2), loose)


# -- helpers ----------------------------------------------------------------------------------


def test_length_stats_is_a_population_statistic() -> None:
    assert length_stats({}) is None
    stats = length_stats({4: 2, 6: 2})
    assert stats == LengthStats(min=4, max=6, mean=5.0, stdev=1.0)
    assert length_stats({7: 10}) == LengthStats(min=7, max=7, mean=7.0, stdev=0.0)


def test_top_shapes_orders_by_share_then_text_and_limits() -> None:
    shares = top_shapes({"9999": 5, "AA-9999999": 3, "AAAA": 3, "A": 1}, limit=3)
    assert [s.shape for s in shares] == ["9999", "AA-9999999", "AAAA"]
    assert [round(s.share, 4) for s in shares] == [round(5 / 12, 4), 0.25, 0.25]
    assert top_shapes({}, limit=3) == ()


def test_top_token_share_takes_the_top_n_and_caps_at_one() -> None:
    counts = {1: 50, 2: 30, 3: 20, 4: 10}
    assert top_token_share(counts, 200, 2) == pytest.approx(0.4)
    assert top_token_share(counts, 50, 10) == 1.0
    assert top_token_share({}, 100, 10) == 0.0
    assert top_token_share(counts, 0, 10) == 0.0


# -- build_profile ----------------------------------------------------------------------------


def _row(i: int, shape: str) -> IdentifierRow:
    return IdentifierRow(
        token=f"t1.{i:022d}",
        field_ref=END.field_ref,
        form=END.form,
        shape=shape,
        event_id=f"ev{i}",
        system_id=END.system_id,
        observed_at=datetime(2026, 10, 17, 12, 0, i % 60, tzinfo=UTC),
        stored_at=datetime(2026, 10, 17, 13, tzinfo=UTC),
    )


def _window(rows: list[IdentifierRow], prof: ProfilerSettings) -> WindowSketch:
    builder = DaySketchBuilder(END, date(2026, 10, 17), prof)
    builder.add_all(rows)
    return merge_window([builder.build()], settings=prof)


def test_build_profile_fills_every_column() -> None:
    prof = ProfilerSettings(minhash_permutations=16, bottom_k=64, min_distinct=10, top_shapes=2)
    rows = [_row(i, "AA-9999999") for i in range(90)] + [_row(i, "9999") for i in range(90, 100)]
    profile = build_profile(_window(rows, prof), template_events=125, settings=prof)
    assert profile.end == END
    assert profile.count == 100
    assert abs(profile.distinct - 100) <= 10
    assert profile.null_rate == pytest.approx(0.2)
    assert [s.shape for s in profile.shapes] == ["AA-9999999", "9999"]
    assert profile.top_shape == "AA-9999999"
    assert profile.shapes[0].share == pytest.approx(0.9)
    assert profile.length_stats is not None
    assert (profile.length_stats.min, profile.length_stats.max) == (4, 10)
    assert profile.top_share == pytest.approx(prof.concentration_top_n / 100)
    assert 0.0 <= profile.quality <= 1.0
    assert profile.is_candidate == is_candidate(profile, prof)
    assert profile.is_candidate
    assert profile.first_seen == datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
    assert profile.last_seen == datetime(2026, 10, 17, 12, 0, 59, tzinfo=UTC)


def test_build_profile_clamps_the_null_rate_and_tolerates_no_template_count() -> None:
    prof = ProfilerSettings(minhash_permutations=16, bottom_k=64)
    window = _window([_row(i, "9999") for i in range(20)], prof)
    assert build_profile(window, template_events=0, settings=prof).null_rate == 0.0
    assert build_profile(window, template_events=10, settings=prof).null_rate == 0.0
    assert build_profile(window, template_events=40, settings=prof).null_rate == 0.5


def test_build_profile_counts_exactly_min_distinct_values_and_passes_the_floor() -> None:
    # Scenario C's legacy source: exactly 50 distinct values in the window. The bottom-k sample
    # (default 2,048) holds them all, so distinct is exact and the field is a candidate.
    prof = ProfilerSettings(minhash_permutations=16)
    rows = [_row(i % prof.min_distinct, "AAAA-9999") for i in range(400)]
    profile = build_profile(_window(rows, prof), template_events=400, settings=prof)
    assert profile.distinct == prof.min_distinct == 50
    assert profile.is_candidate
    # One short of the floor is still exact and still excluded.
    rows = [_row(i % (prof.min_distinct - 1), "AAAA-9999") for i in range(400)]
    profile = build_profile(_window(rows, prof), template_events=400, settings=prof)
    assert profile.distinct == 49
    assert not profile.is_candidate


def test_build_profile_of_a_repeating_status_field_is_not_a_candidate() -> None:
    prof = ProfilerSettings(minhash_permutations=16, bottom_k=64)
    rows = [_row(i % 3, "AAAAAAA") for i in range(300)]
    profile = build_profile(_window(rows, prof), template_events=300, settings=prof)
    assert profile.distinct <= 3
    assert profile.top_share == 1.0
    assert not profile.is_candidate
    assert profile.quality < DEFAULT.quality_floor + 0.2


# -- domain_size ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("9999", 10.0**4),
        ("AA-9999999", 26.0**2 * 10.0**7),
        ("A", 26.0),
        ("-", 1.0),
        ("99-999", 10.0**5),
        ("A9A9", 26.0**2 * 10.0**2),
        ("9" * 12 + "+", 10.0**13),
    ],
)
def test_domain_size_follows_the_shape_alphabet(shape: str, expected: float) -> None:
    assert domain_size(shape) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "SO-0004471", "abc", "x9", "+", "-+"])
def test_domain_size_is_none_for_non_shapes(value: str) -> None:
    assert domain_size(value) is None
