"""Bridges (spec 9.4): the role comes from the consistency of both directions, a clean
functional dependency with transaction fanout scores at least its consistency, a one-directional
dependency is an association, and bridge features are built from the co-occurrence row rather
than the (empty) token join."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from carto_core.linker.bridges import bridge_outcome
from carto_core.linker.model import Candidate, LinkRole, LinkType, PairFeatures
from carto_core.linker.score import Scorer
from carto_core.linker.verify import (
    bridge_features,
    default_domain_size,
    pair_domain_size,
    verify,
)
from carto_core.profiler.model import FieldProfile, ShapeShare
from carto_core.settings import LinkerSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import CooccurrenceRow, FieldForm, Window

SETTINGS = LinkerSettings()
ORDER = FieldForm("sys_orders/tpl_pay/order_id", "raw")
MERCHANT = FieldForm("sys_orders/tpl_pay/merchant_ref", "raw")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def row(
    events: int = 600,
    distinct_a: int = 600,
    single_b_a: int = 600,
    distinct_b: int = 600,
    single_a_b: int = 600,
) -> CooccurrenceRow:
    return CooccurrenceRow(
        a=ORDER,
        b=MERCHANT,
        system_id="sys_orders",
        events=events,
        distinct_a=distinct_a,
        single_b_a=single_b_a,
        distinct_b=distinct_b,
        single_a_b=single_a_b,
    )


def profile(end: FieldForm, distinct: int, shape: str = "9999") -> FieldProfile:
    return FieldProfile(
        end=end,
        count=distinct,
        distinct=distinct,
        null_rate=0.0,
        shapes=(ShapeShare(shape, 1.0),),
        length_stats=None,
        top_share=0.01,
        quality=0.9,
        is_candidate=True,
        first_seen=None,
        last_seen=None,
    )


def features(**over: Any) -> PairFeatures:
    return bridge_features(
        row(**over), {ORDER: profile(ORDER, 600), MERCHANT: profile(MERCHANT, 600)}
    )


def test_bridge_features_come_from_the_cooccurrence_row() -> None:
    item = features()
    assert item.n_match == 600
    assert item.n_a == item.n_b == 600
    assert item.c_ab == item.c_ba == 1.0
    assert item.pairing_consistency == 1.0
    assert item.lag_median == 0.0 and item.lag_iqr == 0.0
    assert item.chance_ratio is None and item.concentration is None
    assert item.fanout_ab_median is None


def test_bridge_containment_is_the_share_of_the_profiled_values() -> None:
    item = bridge_features(
        row(distinct_a=300), {ORDER: profile(ORDER, 600), MERCHANT: profile(MERCHANT, 600)}
    )
    assert item.c_ab == pytest.approx(0.5)
    assert item.c_ba == 1.0
    assert bridge_features(row(), {}).c_ab == 1.0, "no profile: the share is taken as whole"


def test_clean_functional_dependency_is_a_transaction_bridge_scored_at_its_consistency() -> None:
    score, role = bridge_outcome(0.4, LinkRole.ASSOCIATION, features(), SETTINGS, row())
    assert role == LinkRole.TRANSACTION
    assert score == 1.0


def test_one_directional_dependency_is_an_association() -> None:
    # Every order carries exactly one customer, a customer carries many orders.
    customer_like = row(distinct_a=100, single_b_a=20, distinct_b=600, single_a_b=600)
    item = bridge_features(customer_like, {})
    score, role = bridge_outcome(0.99, LinkRole.TRANSACTION, item, SETTINGS, customer_like)
    assert role == LinkRole.ASSOCIATION
    assert score == pytest.approx(0.99), "the association keeps its logistic score"


def test_both_directions_mostly_single_but_below_the_floor_is_a_transaction_without_boost() -> None:
    split = row(distinct_a=600, single_b_a=400, distinct_b=800, single_a_b=800)
    # consistency_ab 0.67 (a PO splits into shipments sometimes), consistency_ba 1.0 >= 0.98
    score, role = bridge_outcome(
        0.7, LinkRole.ASSOCIATION, bridge_features(split, {}), SETTINGS, split
    )
    assert role == LinkRole.TRANSACTION
    assert score == 1.0, "the larger direction is above the floor"
    weaker = row(distinct_a=600, single_b_a=400, distinct_b=600, single_a_b=540)
    score, role = bridge_outcome(
        0.7, LinkRole.ASSOCIATION, bridge_features(weaker, {}), SETTINGS, weaker
    )
    assert role == LinkRole.TRANSACTION
    assert score == pytest.approx(0.7), "below the floor the logistic score stands (0.7 < 0.9)"


def test_two_multi_valued_fields_never_score_above_their_consistency() -> None:
    noisy = row(distinct_a=600, single_b_a=100, distinct_b=600, single_a_b=120)
    score, role = bridge_outcome(
        0.99, LinkRole.TRANSACTION, bridge_features(noisy, {}), SETTINGS, noisy
    )
    assert role == LinkRole.ASSOCIATION
    assert score == pytest.approx(0.2)


def test_bridge_floor_is_a_setting() -> None:
    strict = LinkerSettings(bridge_consistency_min=1.0)
    nearly = row(single_b_a=594, single_a_b=594)  # 0.99 both ways
    score, role = bridge_outcome(
        0.6, LinkRole.ASSOCIATION, bridge_features(nearly, {}), strict, nearly
    )
    assert role == LinkRole.TRANSACTION
    assert score == pytest.approx(0.6)
    score, _role = bridge_outcome(
        0.6, LinkRole.ASSOCIATION, bridge_features(nearly, {}), SETTINGS, nearly
    )
    assert score == pytest.approx(0.99)


@pytest.mark.parametrize(
    ("consistency", "role", "expected"),
    [
        (None, LinkRole.TRANSACTION, (0.6, LinkRole.TRANSACTION)),
        (0.99, LinkRole.TRANSACTION, (0.99, LinkRole.TRANSACTION)),
        (0.99, LinkRole.ASSOCIATION, (0.6, LinkRole.ASSOCIATION)),
        (0.5, LinkRole.TRANSACTION, (0.5, LinkRole.ASSOCIATION)),
        (0.5, LinkRole.BATCH, (0.5, LinkRole.BATCH)),
    ],
)
def test_without_a_row_the_stored_consistency_decides(
    consistency: float | None, role: LinkRole, expected: tuple[float, LinkRole]
) -> None:
    item = replace(features(), pairing_consistency=consistency)
    score, got_role = bridge_outcome(0.6, role, item, SETTINGS)
    assert score == pytest.approx(expected[0])
    assert got_role == expected[1]


def test_scorer_types_bridges_by_candidate_source_and_override() -> None:
    scorer = Scorer(SETTINGS)
    bridge = Candidate(ORDER, MERCHANT, 1.0, source="bridge")
    outcome = scorer.score(bridge, features(), cooccurrence=row())
    assert outcome.link_type == LinkType.BRIDGE
    assert outcome.role == LinkRole.TRANSACTION
    assert outcome.score == 1.0
    assert outcome.passes_filters
    exact = scorer.score(Candidate(ORDER, MERCHANT, 1.0, source="lsh"), features())
    assert exact.link_type == LinkType.EXACT
    forced = scorer.score(
        Candidate(ORDER, MERCHANT, 0.0, source="repropose"), features(), link_type=LinkType.BRIDGE
    )
    assert forced.link_type == LinkType.BRIDGE


def test_verify_uses_the_row_for_bridges_and_checks_it_belongs_to_the_pair() -> None:
    store = InMemoryEventStore()
    window = Window(NOW, NOW.replace(hour=13))
    profiles = {ORDER: profile(ORDER, 600), MERCHANT: profile(MERCHANT, 600)}
    item = verify(
        store,
        "default",
        Candidate(ORDER, MERCHANT, 1.0, "bridge"),
        window,
        domain_size_fn=lambda _shape: None,
        profiles=profiles,
        cooccurrence=row(),
    )
    assert item.n_match == 600 and item.pairing_consistency == 1.0
    reversed_candidate = Candidate(MERCHANT, ORDER, 1.0, "bridge")
    swapped = verify(
        store,
        "default",
        reversed_candidate,
        window,
        domain_size_fn=lambda _shape: None,
        profiles=profiles,
        cooccurrence=row(distinct_a=300),
    )
    assert swapped.n_a == 600 and swapped.n_b == 300, "the row is turned to the candidate's order"
    other = CooccurrenceRow(
        a=FieldForm("sys_x/t/x", "raw"),
        b=MERCHANT,
        system_id="sys_x",
        events=50,
        distinct_a=50,
        single_b_a=50,
        distinct_b=50,
        single_a_b=50,
    )
    with pytest.raises(ValueError, match="does not belong"):
        verify(
            store,
            "default",
            Candidate(ORDER, MERCHANT, 1.0, "bridge"),
            window,
            domain_size_fn=lambda _shape: None,
            profiles=profiles,
            cooccurrence=other,
        )


@pytest.mark.parametrize(
    ("shape", "size"),
    [
        ("9999", 1e4),
        ("AA-9999999", 26**2 * 1e7),
        ("999999999999+", 1e13),
        ("a-x", 26 * 36),
        ("", None),
    ],
)
def test_default_domain_size_follows_the_spec_rule(shape: str, size: float | None) -> None:
    assert default_domain_size(shape) == (None if size is None else pytest.approx(size))


def test_pair_domain_is_the_smaller_end_or_unknown() -> None:
    assert pair_domain_size("9999", "AA-9999999", default_domain_size) == pytest.approx(1e4)
    assert pair_domain_size(None, "9999", default_domain_size) is None
    assert pair_domain_size("9999", "", default_domain_size) is None
    assert pair_domain_size("9999", "9999", lambda _shape: None) is None


def test_bridge_share_without_profile_or_values() -> None:
    empty = bridge_features(row(distinct_a=0, single_b_a=0), {})
    assert empty.c_ab == 0.0 and empty.c_ba == 1.0


def test_one_to_very_many_bridge_is_a_batch() -> None:
    # Fourteen manifests, seven hundred shipments: every shipment names one manifest.
    manifest_like = row(events=700, distinct_a=14, single_b_a=0, distinct_b=700, single_a_b=700)
    item = bridge_features(manifest_like, {})
    score, role = bridge_outcome(0.99, LinkRole.TRANSACTION, item, SETTINGS, manifest_like)
    assert role == LinkRole.BATCH
    assert score == pytest.approx(0.99)
    few = row(events=60, distinct_a=14, single_b_a=0, distinct_b=60, single_a_b=60)
    _score, role = bridge_outcome(
        0.9, LinkRole.TRANSACTION, bridge_features(few, {}), SETTINGS, few
    )
    assert role == LinkRole.ASSOCIATION, "4 per value is one-to-few, not a batch"
    lenient = LinkerSettings(transaction_fanout_p99_max=4.0)
    _score, role = bridge_outcome(0.9, LinkRole.TRANSACTION, bridge_features(few, {}), lenient, few)
    assert role == LinkRole.BATCH, "the batch threshold is the p99 fan-out setting"
