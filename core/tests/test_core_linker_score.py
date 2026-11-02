"""Scoring (spec 9.3; ADR 0049): the shipped weights put a clean one-to-one link above 0.9 and
a coincidental overlap below 0.5; the hard filters, direction and role rules follow the spec
thresholds from settings; the score is monotonic in containment and antitonic in concentration
and chance ratio; the direction flips when the ends swap; weights load from a file and refuse
anything that is not a weights document."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from carto_core.linker.model import Candidate, Direction, LinkRole, LinkType, PairFeatures
from carto_core.linker.score import (
    Scorer,
    concentration_excess,
    direction_of,
    feature_vector,
    hard_filter,
    logistic,
    role_of,
)
from carto_core.linker.weights import (
    DEFAULT_WEIGHTS,
    FEATURE_NAMES,
    MAX_WEIGHTS_FILE_BYTES,
    Weights,
    WeightsError,
    load_weights,
)
from carto_core.settings import LinkerSettings
from carto_core.store.model import FieldForm

SETTINGS = LinkerSettings()
A = FieldForm("sys_a/tpl_a/order_id", "raw")
B = FieldForm("sys_b/tpl_b/order_ref", "digits.0")


def features(**over: Any) -> PairFeatures:
    base: dict[str, Any] = {
        "n_a": 556,
        "n_b": 556,
        "n_match": 500,
        "c_ab": 0.9,
        "c_ba": 0.9,
        "lag_median": 7200.0,
        "lag_iqr": 600.0,
        "lag_positive_share": 0.97,
        "fanout_ab_median": 1.0,
        "fanout_ab_p99": 2.0,
        "fanout_ba_median": 1.0,
        "fanout_ba_p99": 1.0,
        "concentration": 0.02,
        "chance_ratio": 0.001,
        "domain_size": 1e7,
        "pairing_consistency": None,
    }
    base.update(over)
    return PairFeatures(**base)


def coincidental() -> PairFeatures:
    return features(
        n_a=500,
        n_b=500,
        n_match=100,
        c_ab=0.2,
        c_ba=0.2,
        lag_median=100.0,
        lag_iqr=80_000.0,
        lag_positive_share=0.5,
        concentration=0.1,
        chance_ratio=0.2,
    )


# -- worked examples ---------------------------------------------------------------------------


def test_clean_one_to_one_link_scores_above_0_9() -> None:
    outcome = Scorer(SETTINGS).score(Candidate(A, B, 0.9), features())
    assert outcome.score > 0.9
    assert outcome.passes_filters and outcome.filter_reason is None
    assert outcome.direction == Direction.A_TO_B
    assert outcome.role == LinkRole.TRANSACTION
    assert outcome.link_type == LinkType.EXACT


def test_coincidental_overlap_scores_below_half_without_a_hard_filter() -> None:
    outcome = Scorer(SETTINGS).score(Candidate(A, B, 0.2), coincidental())
    assert outcome.passes_filters, "the logistic score, not a filter, must reject it"
    assert outcome.score < 0.5


def test_feature_vector_is_bounded_and_complete() -> None:
    vector = feature_vector(features(), SETTINGS)
    assert tuple(vector) == FEATURE_NAMES
    for name, value in vector.items():
        if name != "log_n_match":
            assert 0.0 <= value <= 1.0, name
    assert vector["lag_consistency"] == pytest.approx(1 - 600 / 7200)
    assert vector["lag_direction"] == pytest.approx(0.97)
    assert vector["pairing_consistency"] == 0.0


def test_missing_features_take_neutral_values() -> None:
    vector = feature_vector(
        features(
            lag_median=None,
            lag_iqr=None,
            lag_positive_share=None,
            fanout_ab_median=None,
            fanout_ba_median=None,
            concentration=None,
            chance_ratio=None,
        ),
        SETTINGS,
    )
    assert vector["lag_consistency"] == 0.0
    assert vector["lag_direction"] == 0.5
    assert vector["fanout_penalty"] == 0.0
    assert vector["concentration"] == 0.0
    assert vector["chance_ratio"] == 0.0


def test_fanout_penalty_only_when_both_ends_fan_out() -> None:
    one_to_many = feature_vector(features(fanout_ab_median=40.0), SETTINGS)
    many_to_many = feature_vector(features(fanout_ab_median=40.0, fanout_ba_median=4.0), SETTINGS)
    assert one_to_many["fanout_penalty"] == 0.0
    assert many_to_many["fanout_penalty"] == 1.0


def test_logistic_of_empty_vector_is_the_intercept() -> None:
    assert logistic({}, Weights(version="t", intercept=0.0, coefficients={})) == 0.5
    assert (
        logistic({"c_ab": 1.0}, Weights(version="t", intercept=0.0, coefficients={"c_ab": 50.0}))
        > 0.999
    )
    assert (
        logistic({"c_ab": 1.0}, Weights(version="t", intercept=0.0, coefficients={"c_ab": -50.0}))
        < 0.001
    )


# -- hard filters, direction, role --------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"n_match": 19}, "n_match"),
        ({"concentration": 0.51}, "concentration"),
        ({"chance_ratio": 0.31}, "chance_ratio"),
        ({"concentration": None, "chance_ratio": None}, None),
        ({}, None),
    ],
)
def test_hard_filters(over: dict[str, Any], reason: str | None) -> None:
    assert hard_filter(features(**over), SETTINGS) == reason
    outcome = Scorer(SETTINGS).score(Candidate(A, B, 0.9), features(**over))
    assert outcome.passes_filters == (reason is None)
    assert outcome.filter_reason == reason


def test_hard_filters_follow_settings() -> None:
    strict = LinkerSettings(min_match=600)
    assert hard_filter(features(), strict) == "n_match"


@pytest.mark.parametrize(
    ("share", "direction"),
    [
        (0.8, Direction.A_TO_B),
        (0.95, Direction.A_TO_B),
        (0.2, Direction.B_TO_A),
        (0.0, Direction.B_TO_A),
        (0.5, Direction.UNDIRECTED),
        (0.79, Direction.UNDIRECTED),
        (None, Direction.UNDIRECTED),
    ],
)
def test_direction_rule(share: float | None, direction: Direction) -> None:
    assert direction_of(features(lag_positive_share=share), SETTINGS) == direction


@pytest.mark.parametrize(
    ("over", "role"),
    [
        ({}, LinkRole.TRANSACTION),
        ({"fanout_ab_median": 3.0, "fanout_ab_p99": 20.0}, LinkRole.TRANSACTION),
        ({"fanout_ab_median": 3.5}, LinkRole.ASSOCIATION),
        ({"fanout_ab_p99": 21.0, "fanout_ba_median": 1.0}, LinkRole.BATCH),
        (
            {"fanout_ba_p99": 500.0, "fanout_ba_median": 30.0, "fanout_ab_median": 1.0},
            LinkRole.BATCH,
        ),
        ({"fanout_ab_p99": 21.0, "fanout_ba_median": 2.0}, LinkRole.ASSOCIATION),
        (
            {"fanout_ab_median": 5.0, "fanout_ba_median": 5.0, "fanout_ab_p99": 50.0},
            LinkRole.ASSOCIATION,
        ),
        ({"fanout_ab_median": None}, LinkRole.ASSOCIATION),
    ],
)
def test_role_rule(over: dict[str, Any], role: LinkRole) -> None:
    assert role_of(features(**over), SETTINGS) == role


# -- weights -----------------------------------------------------------------------------------


def test_default_weights_cover_every_feature_with_the_expected_signs() -> None:
    assert set(DEFAULT_WEIGHTS.coefficients) == set(FEATURE_NAMES)
    assert DEFAULT_WEIGHTS.coefficient("c_ab") > 0
    assert DEFAULT_WEIGHTS.coefficient("concentration") < 0
    assert DEFAULT_WEIGHTS.coefficient("chance_ratio") < 0
    assert load_weights(None) is DEFAULT_WEIGHTS


def test_weights_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "weights.json"
    path.write_text(DEFAULT_WEIGHTS.model_dump_json(), encoding="utf-8")
    loaded = load_weights(path)
    assert loaded == DEFAULT_WEIGHTS
    assert Scorer(SETTINGS, loaded).weights.version == "v1-hand-set"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"version": "x", "intercept": 0.0, "coefficients": {"nope": 1.0}}),
        json.dumps({"version": "x", "intercept": 0.0, "coefficients": {}, "extra": 1}),
        json.dumps({"version": "x", "intercept": "NaN", "coefficients": {}}),
    ],
)
def test_weights_file_rejects_bad_documents(tmp_path: Path, content: str) -> None:
    path = tmp_path / "weights.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(WeightsError):
        load_weights(path)


def test_weights_file_missing_or_oversized(tmp_path: Path) -> None:
    with pytest.raises(WeightsError):
        load_weights(tmp_path / "absent.json")
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (MAX_WEIGHTS_FILE_BYTES + 1))
    with pytest.raises(WeightsError):
        load_weights(big)


def test_weights_model_rejects_unknown_features_and_infinities() -> None:
    with pytest.raises(ValueError, match="unknown features"):
        Weights(version="x", intercept=0.0, coefficients={"bogus": 1.0})
    with pytest.raises(ValueError, match="finite"):
        Weights(version="x", intercept=float("inf"), coefficients={})


# -- properties --------------------------------------------------------------------------------

unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@h_settings(max_examples=200)
@given(low=unit, high=unit)
def test_score_is_monotonic_in_containment(low: float, high: float) -> None:
    low, high = sorted((low, high))
    scorer = Scorer(SETTINGS)
    assert scorer.probability(features(c_ab=low)) <= scorer.probability(features(c_ab=high))
    assert scorer.probability(features(c_ba=low)) <= scorer.probability(features(c_ba=high))


@h_settings(max_examples=200)
@given(low=unit, high=unit)
def test_score_is_antitonic_in_concentration_and_chance_ratio(low: float, high: float) -> None:
    low, high = sorted((low, high))
    scorer = Scorer(SETTINGS)
    assert scorer.probability(features(concentration=low)) >= scorer.probability(
        features(concentration=high)
    )
    assert scorer.probability(features(chance_ratio=low)) >= scorer.probability(
        features(chance_ratio=high)
    )


def mirror(item: PairFeatures) -> PairFeatures:
    """The same pair seen from B's side."""
    return replace(
        item,
        n_a=item.n_b,
        n_b=item.n_a,
        c_ab=item.c_ba,
        c_ba=item.c_ab,
        lag_median=None if item.lag_median is None else -item.lag_median,
        lag_positive_share=None
        if item.lag_positive_share is None
        else 1.0 - item.lag_positive_share,
        fanout_ab_median=item.fanout_ba_median,
        fanout_ab_p99=item.fanout_ba_p99,
        fanout_ba_median=item.fanout_ab_median,
        fanout_ba_p99=item.fanout_ab_p99,
    )


FLIPPED = {
    Direction.A_TO_B: Direction.B_TO_A,
    Direction.B_TO_A: Direction.A_TO_B,
    Direction.UNDIRECTED: Direction.UNDIRECTED,
}


@h_settings(max_examples=200)
@given(
    share=unit,
    fan_ab=st.floats(min_value=1.0, max_value=100.0),
    fan_ba=st.floats(min_value=1.0, max_value=100.0),
)
def test_direction_flips_and_role_holds_when_the_ends_swap(
    share: float, fan_ab: float, fan_ba: float
) -> None:
    item = features(
        lag_positive_share=share,
        fanout_ab_median=fan_ab,
        fanout_ab_p99=fan_ab * 2,
        fanout_ba_median=fan_ba,
        fanout_ba_p99=fan_ba * 2,
    )
    swapped = mirror(item)
    assert direction_of(swapped, SETTINGS) == FLIPPED[direction_of(item, SETTINGS)]
    assert role_of(swapped, SETTINGS) == role_of(item, SETTINGS)
    assert hard_filter(swapped, SETTINGS) == hard_filter(item, SETTINGS)


@pytest.mark.parametrize(
    ("concentration", "n_match", "expected"),
    [
        (None, 500, 0.0),
        (0.02, 500, 0.0),
        (0.476, 21, 0.0),
        (1.0, 21, 1.0),
        (0.5, 20, 0.0),
        (0.75, 20, 0.5),
        (0.9, 5, 0.0),
        (0.3, 0, 0.0),
        (0.3, 100, pytest.approx(0.2 / 0.9)),
    ],
)
def test_concentration_excess_over_the_flat_baseline(
    concentration: float | None, n_match: int, expected: float
) -> None:
    assert concentration_excess(concentration, n_match) == expected


def test_sparse_flat_link_is_not_read_as_concentrated() -> None:
    """Twenty-one one-to-one matches put 48% in the top ten by arithmetic alone; the score
    must not treat that as skew (a 60%-filled manual field inside one week)."""
    sparse = features(n_a=50, n_b=150, n_match=21, c_ab=0.42, c_ba=0.14, concentration=10 / 21)
    skewed = features(n_a=50, n_b=150, n_match=21, c_ab=0.42, c_ba=0.14, concentration=0.95)
    assert feature_vector(sparse, SETTINGS)["concentration"] == 0.0
    assert feature_vector(skewed, SETTINGS)["concentration"] > 0.9
    assert Scorer(SETTINGS).probability(sparse) > Scorer(SETTINGS).probability(skewed)
