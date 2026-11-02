"""Composite matching units (spec 9.5): business days and blocking with its cap, EM recovering
planted ``m`` and the prior, ``u`` from random pairs, the threshold reaching the target
precision, the trigger each way (ceiling, coverage floor, correlation, reverse ordering), the
component views the API renders, and the rules repository in memory and against PostgreSQL
(``integration``)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from carto_core.linker.composite import (
    Adjacency,
    CompositeMatch,
    CompositeOutcome,
    InMemoryCompositeRuleRepository,
    PostgresCompositeRuleRepository,
    component_views,
    components_from_json,
    components_json,
    cross_correlation,
    event_coverage,
    exact_field_pairs,
    hourly_series,
    identifier_links,
    key_event_series,
    one_rule_per_system_pair,
    primary_component,
    rolling_day,
    trigger_pairs,
)
from carto_core.linker.composite_blocking import (
    COMPOSITE_FORMS,
    Side,
    SideEvent,
    block_pairs,
    business_days_between,
    within_business_days,
)
from carto_core.linker.composite_em import (
    AgreementMatrix,
    ComponentSpec,
    agreement_matrix,
    choose_threshold,
    components_from,
    discover_components,
    estimate_u,
    fellegi_sunter_em,
    label_vector,
    pair_weights,
    posteriors,
    side_has,
    split_components,
)
from carto_core.linker.model import (
    CompositeComponent,
    CompositeRule,
    Direction,
    LinkRole,
    LinkType,
    PairFeatures,
)
from carto_core.linker.review import ScoredLink
from carto_core.settings import LinkerSettings
from carto_core.store.model import FieldForm, HourlyVolume, IdentifierRow, Window

SETTINGS = LinkerSettings()
FRIDAY = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
MONDAY = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def side_event(
    event_id: str,
    at: datetime,
    *,
    date: str | None = None,
    amount: str | None = None,
    name: frozenset[str] | None = None,
    partial: str | None = None,
    attributes: dict[str, str] | None = None,
    system: str = "sys_a",
) -> SideEvent:
    return SideEvent(
        event_id=event_id,
        template_id="tpl",
        observed_at=at,
        dates={f"{system}/tpl/when": date} if date else {},
        amounts={f"{system}/tpl/total": amount} if amount else {},
        names={f"{system}/tpl/name": name} if name else {},
        partials={(f"{system}/tpl/ref", "digits.0"): partial} if partial else {},
        attributes=attributes or {},
    )


def side(system: str, events: list[SideEvent]) -> Side:
    result = Side(system_id=system)
    for event in events:
        result.add(event)
    return result


# -- business days and blocking ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (FRIDAY, FRIDAY + timedelta(hours=2), 0),
        (FRIDAY, FRIDAY + timedelta(days=1), 0),  # Saturday
        (FRIDAY, FRIDAY + timedelta(days=2), 0),  # Sunday
        (FRIDAY, MONDAY, 1),
        (FRIDAY, MONDAY + timedelta(days=2), 3),  # Wednesday
        (FRIDAY, MONDAY + timedelta(days=3), 4),  # Thursday
        (MONDAY, MONDAY + timedelta(days=4), 4),  # Friday
        (MONDAY, MONDAY + timedelta(days=7), 5),  # next Monday
        (MONDAY, FRIDAY, None),
    ],
)
def test_business_days_between(start: datetime, end: datetime, expected: int | None) -> None:
    assert business_days_between(start, end) == expected


def test_within_business_days_is_inclusive_and_refuses_the_past() -> None:
    assert within_business_days(FRIDAY, MONDAY + timedelta(days=2), 3)
    assert not within_business_days(FRIDAY, MONDAY + timedelta(days=3), 3)
    assert not within_business_days(MONDAY, FRIDAY, 3)
    assert within_business_days(FRIDAY, FRIDAY, 3)


def test_blocking_shares_a_date_or_amount_inside_the_business_day_window() -> None:
    a = side(
        "sys_a",
        [
            side_event("a1", FRIDAY, date="d1", amount="x1"),
            side_event("a2", FRIDAY, date="d2", amount="x2"),
            side_event("a3", MONDAY + timedelta(days=3), date="d1", amount="x3"),
        ],
    )
    b = side(
        "sys_b",
        [
            side_event("b1", MONDAY, date="d1", amount="x9", system="sys_b"),  # date with a1
            side_event("b2", MONDAY, date="d9", amount="x2", system="sys_b"),  # amount with a2
            side_event("b3", MONDAY + timedelta(days=3), date="d1", system="sys_b"),  # 4 days
            side_event("b4", FRIDAY - timedelta(days=1), date="d1", system="sys_b"),  # before
        ],
    )
    blocked = block_pairs(a, b, window_business_days=3, max_pairs=1000)
    assert blocked.capped is False
    # b3 shares d1 with a1 (4 business days: out) and with a3 (same day: in).
    assert blocked.pairs == ((0, 0), (1, 1), (2, 2))


def test_blocking_stops_at_the_cap_and_says_so() -> None:
    a = side("sys_a", [side_event(f"a{n}", FRIDAY, date="d1") for n in range(5)])
    b = side("sys_b", [side_event(f"b{n}", MONDAY, date="d1", system="sys_b") for n in range(5)])
    blocked = block_pairs(a, b, window_business_days=3, max_pairs=7)
    assert blocked.capped is True
    assert len(blocked.pairs) == 7
    assert blocked.pairs[:5] == ((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
    assert len(block_pairs(a, b, window_business_days=3, max_pairs=100).pairs) == 25


def test_composite_forms_are_the_spec_names() -> None:
    assert {"date", "amount", "alnum", "digits.0", "digits.2", "phonetic.0", "phonetic.7"} <= (
        COMPOSITE_FORMS
    )
    assert "raw" not in COMPOSITE_FORMS and "norm" not in COMPOSITE_FORMS
    assert "digits.3" not in COMPOSITE_FORMS and "phonetic.8" not in COMPOSITE_FORMS


# -- components and Fellegi-Sunter -----------------------------------------------------------


def test_components_are_the_best_agreeing_end_pair_per_kind_and_per_attribute_key() -> None:
    a = side(
        "sys_a",
        [
            side_event(
                "a1",
                FRIDAY,
                date="d1",
                amount="x1",
                name=frozenset({"n1", "n2"}),
                partial="p1",
                attributes={"warehouse": "DC-01", "currency": "USD"},
            )
        ],
    )
    b = side(
        "sys_b",
        [
            side_event(
                "b1",
                MONDAY,
                date="d1",
                amount="x1",
                name=frozenset({"n1", "n2"}),
                partial="p1",
                attributes={"warehouse": "DC-01", "currency": "EUR"},
                system="sys_b",
            )
        ],
    )
    specs = discover_components(a, b, [(0, 0)])
    assert [spec.kind for spec in specs] == [
        "amount",
        "attribute",
        "date",
        "partial_identifier",
        "phonetic",
    ]
    by_kind = {spec.kind: spec for spec in specs}
    assert by_kind["phonetic"].a == FieldForm("sys_a/tpl/name", "phonetic.0")
    assert by_kind["attribute"].key == "warehouse"
    assert by_kind["attribute"].a == FieldForm("sys_a/tpl/warehouse", "raw")
    assert by_kind["partial_identifier"].b == FieldForm("sys_b/tpl/ref", "digits.0")
    matrix = agreement_matrix(specs, a, b, [(0, 0)])
    assert matrix.agree.tolist() == [[True] * 5]
    assert matrix.present.tolist() == [[True] * 5]
    assert all(
        side_has(spec, a.events[0], "a") and side_has(spec, b.events[0], "b") for spec in specs
    )


def test_a_missing_field_is_neither_agreement_nor_disagreement() -> None:
    a = side("sys_a", [side_event("a1", FRIDAY, date="d1", amount="x1")])
    b = side("sys_b", [side_event("b1", MONDAY, date="d1", system="sys_b")])
    specs = (
        ComponentSpec(
            "date", FieldForm("sys_a/tpl/when", "date"), FieldForm("sys_b/tpl/when", "date")
        ),
        ComponentSpec(
            "amount", FieldForm("sys_a/tpl/total", "amount"), FieldForm("sys_b/tpl/total", "amount")
        ),
    )
    matrix = agreement_matrix(specs, a, b, [(0, 0)])
    assert matrix.agree.tolist() == [[True, False]]
    assert matrix.present.tolist() == [[True, False]]
    m = np.asarray([0.95, 0.95])
    u = np.asarray([0.2, 0.01])
    assert pair_weights(matrix, m, u)[0] == pytest.approx(np.log2(0.95 / 0.2)), (
        "amount adds nothing"
    )
    assert not side_has(specs[1], b.events[0], "b")


def test_exact_link_components_become_labels_and_label_the_pairs() -> None:
    exact = frozenset({frozenset({"sys_a/tpl/ref", "sys_b/tpl/ref"})})
    a = side(
        "sys_a",
        [
            side_event("a1", FRIDAY, date="d1", partial="p1"),
            side_event("a2", FRIDAY, date="d1", partial="p2"),
            side_event("a3", FRIDAY, date="d1"),
        ],
    )
    b = side(
        "sys_b",
        [
            side_event("b1", MONDAY, date="d1", partial="p1", system="sys_b"),
            side_event("b2", MONDAY, date="d1", system="sys_b"),
        ],
    )
    pairs = [(0, 0), (1, 0), (2, 0), (0, 1)]
    scoring, labels = split_components(discover_components(a, b, pairs), exact)
    assert [spec.kind for spec in scoring] == ["date"]
    assert [spec.kind for spec in labels] == ["partial_identifier"]
    vector = label_vector(labels, a, b, pairs)
    assert vector[0] == 1.0, "ids agree: a known match"
    assert vector[1] == 0.0, "both carry an id and they differ: a known non-match"
    assert np.isnan(vector[2]) and np.isnan(vector[3]), "no id on one side: unknown"
    assert np.all(np.isnan(label_vector((), a, b, pairs)))


def test_labels_identify_a_rule_with_one_informative_component() -> None:
    # 9,000 same-date pairs, 180 of them true matches: without labels a lone name component
    # cannot be separated from the prior; with the exact link labelling 95% of them it can.
    rng = np.random.default_rng(4)
    n, matches = 9_000, 180
    truth = np.zeros(n, dtype=np.bool_)
    truth[:matches] = True
    name = np.where(truth, rng.random(n) < 0.97, rng.random(n) < 0.002)
    agree = np.stack([np.ones(n, dtype=np.bool_), name], axis=1)
    matrix = AgreementMatrix.complete(agree)
    labels = np.where(rng.random(n) < 0.95, truth.astype(np.float64), np.nan)
    u = np.asarray([0.15, 0.004])
    result = fellegi_sunter_em(
        matrix,
        u,
        iterations=10,
        initial_prior=0.02,
        update_u=True,
        labels=labels,
        match_density=0.03,
    )
    assert abs(result.m[1] - 0.97) < 0.03 and result.u[1] < 0.01
    assert abs(result.prior - matches / n) < 0.01
    posterior = posteriors(matrix, result.m, result.u, result.prior)
    choice = choose_threshold(pair_weights(matrix, result.m, u), posterior, 0.9)
    assert choice is not None and choice.matched == int(name[:matches].sum() + name[matches:].sum())
    assert truth[pair_weights(matrix, result.m, u) >= choice.threshold].mean() > 0.9


def test_match_density_moves_the_u_start_toward_the_random_pair_value() -> None:
    # A block where nearly every pair is a match (amounts with cents): the observed agreement
    # rate is m, not u; with the density at one the random-pair u stands and the block scores.
    agree = np.ones((500, 1), dtype=np.bool_)
    matrix = AgreementMatrix.complete(agree)
    u = np.asarray([0.0001])
    dense = fellegi_sunter_em(
        matrix, u, iterations=10, initial_prior=0.5, update_u=True, match_density=1.0
    )
    sparse = fellegi_sunter_em(
        matrix, u, iterations=10, initial_prior=0.5, update_u=True, match_density=0.0
    )
    assert posteriors(matrix, dense.m, dense.u, dense.prior)[0] > 0.99
    assert posteriors(matrix, sparse.m, sparse.u, sparse.prior)[0] < 0.6


def planted_matrix(
    rng: np.random.Generator, m: list[float], u: list[float], prior: float, n: int
) -> tuple[np.ndarray, np.ndarray]:
    is_match = rng.random(n) < prior
    probabilities = np.where(is_match[:, None], np.asarray(m)[None, :], np.asarray(u)[None, :])
    return rng.random((n, len(m))) < probabilities, is_match


def test_em_recovers_planted_m_and_prior_within_five_points() -> None:
    rng = np.random.default_rng(7)
    m = [0.95, 0.8, 0.9, 0.7]
    u = [0.05, 0.2, 0.01, 0.3]
    matrix, _truth = planted_matrix(rng, m, u, prior=0.3, n=20_000)
    result = fellegi_sunter_em(
        AgreementMatrix.complete(matrix), np.asarray(u), iterations=SETTINGS.composite_em_iterations
    )
    assert np.all(np.abs(result.m - np.asarray(m)) < 0.05), result.m.tolist()
    assert abs(result.prior - 0.3) < 0.05
    assert 1 <= result.iterations <= SETTINGS.composite_em_iterations
    assert np.all(np.abs(result.u - np.asarray(u)) < 1e-9), "u is held fixed"


def test_em_with_update_u_recovers_u_from_a_wrong_start() -> None:
    rng = np.random.default_rng(9)
    m = [0.95, 0.85, 0.9]
    u = [0.3, 0.05, 0.1]
    matrix, _truth = planted_matrix(rng, m, u, prior=0.2, n=30_000)
    result = fellegi_sunter_em(
        AgreementMatrix.complete(matrix),
        np.asarray([0.1, 0.01, 0.01]),
        iterations=50,
        initial_prior=0.1,
        update_u=True,
    )
    assert np.all(np.abs(result.u - np.asarray(u)) < 0.05), result.u.tolist()
    assert np.all(np.abs(result.m - np.asarray(m)) < 0.05), result.m.tolist()
    assert abs(result.prior - 0.2) < 0.05


def test_one_to_one_prior_is_the_smaller_side_over_the_pairs_capped_at_a_half() -> None:
    from carto_core.linker.composite_em import one_to_one_prior  # noqa: PLC0415

    assert one_to_one_prior(400, 300, 30_000) == pytest.approx(0.01)
    assert one_to_one_prior(400, 300, 100) == 0.5
    assert one_to_one_prior(400, 300, 0) == 0.5


def test_em_on_nothing_returns_the_starting_point() -> None:
    result = fellegi_sunter_em(
        AgreementMatrix.complete(np.zeros((0, 2), dtype=np.bool_)),
        np.asarray([0.1, 0.2]),
        iterations=5,
    )
    assert result.iterations == 0 and result.converged is False
    assert result.m.tolist() == [0.9, 0.9]


def test_u_is_estimated_from_random_pairs_among_those_carrying_the_component() -> None:
    rng = np.random.default_rng(3)
    dates = [f"d{n}" for n in range(20)]
    amounts = [f"x{n}" for n in range(100)]
    a = side(
        "sys_a",
        [
            side_event(f"a{n}", FRIDAY, date=dates[n % 20], amount=amounts[(n * 7) % 100])
            for n in range(200)
        ],
    )
    b = side(
        "sys_b",
        [
            side_event(
                f"b{n}",
                MONDAY,
                date=dates[(n * 3) % 20],
                amount=amounts[(n * 11) % 100],
                system="sys_b",
            )
            for n in range(200)
        ],
    )
    specs = (
        ComponentSpec(
            "date", FieldForm("sys_a/tpl/when", "date"), FieldForm("sys_b/tpl/when", "date")
        ),
        ComponentSpec(
            "amount", FieldForm("sys_a/tpl/total", "amount"), FieldForm("sys_b/tpl/total", "amount")
        ),
    )
    u = estimate_u(specs, a, b, rng=rng, sample_pairs=5_000)
    assert abs(u[0] - 0.05) < 0.05
    assert abs(u[1] - 0.01) < 0.05
    # Half of A without an amount: the amount rate is taken over the pairs that carry one.
    thinned = side(
        "sys_a",
        [
            side_event(
                f"a{n}",
                FRIDAY,
                date=dates[n % 20],
                amount=amounts[(n * 7) % 100] if n % 2 else None,
            )
            for n in range(200)
        ],
    )
    conditioned = estimate_u(specs, thinned, b, rng=rng, sample_pairs=5_000)
    assert abs(conditioned[1] - 0.01) < 0.05
    # Nothing to sample: the smoothed floor.
    assert estimate_u(specs, side("sys_a", []), b, rng=rng).tolist() == [0.5, 0.5]


def test_weights_and_posteriors_follow_the_log_likelihood_ratios() -> None:
    matrix = AgreementMatrix.complete(np.asarray([[True, True], [True, False], [False, False]]))
    m = np.asarray([0.9, 0.8])
    u = np.asarray([0.1, 0.2])
    weights = pair_weights(matrix, m, u)
    expected_first = np.log2(0.9 / 0.1) + np.log2(0.8 / 0.2)
    expected_second = np.log2(0.9 / 0.1) + np.log2(0.2 / 0.8)
    assert weights[0] == pytest.approx(expected_first)
    assert weights[1] == pytest.approx(expected_second)
    assert weights[2] < 0
    posterior = posteriors(matrix, m, u, prior=0.5)
    assert posterior[0] > posterior[1] > posterior[2]
    assert posterior[0] == pytest.approx(1.0 / (1.0 + 2.0 ** (-expected_first)))


def test_threshold_is_the_lowest_weight_meeting_the_target_precision() -> None:
    weights = np.concatenate([np.full(100, 10.0), np.full(50, 2.0), np.full(50, -5.0)])
    posterior = np.concatenate([np.full(100, 0.99), np.full(50, 0.5), np.full(50, 0.01)])
    strict = choose_threshold(weights, posterior, 0.9)
    assert strict is not None
    assert strict.threshold == 10.0 and strict.matched == 100
    assert strict.estimated_precision == pytest.approx(0.99)
    loose = choose_threshold(weights, posterior, 0.8)
    assert loose is not None
    assert loose.threshold == 2.0 and loose.matched == 150
    assert loose.estimated_precision == pytest.approx((99 + 25) / 150)
    assert choose_threshold(weights, posterior, 0.995) is None
    assert choose_threshold(np.zeros(0), np.zeros(0), 0.5) is None


def test_threshold_reaches_the_target_on_a_planted_mixture() -> None:
    rng = np.random.default_rng(11)
    m = [0.97, 0.9, 0.95]
    u = [0.05, 0.1, 0.02]
    agree, truth = planted_matrix(rng, m, u, prior=0.4, n=20_000)
    matrix = AgreementMatrix.complete(agree)
    result = fellegi_sunter_em(matrix, np.asarray(u), iterations=SETTINGS.composite_em_iterations)
    weights = pair_weights(matrix, result.m, result.u)
    posterior = posteriors(matrix, result.m, result.u, result.prior)
    choice = choose_threshold(weights, posterior, SETTINGS.composite_target_precision)
    assert choice is not None
    selected = weights >= choice.threshold
    precision = truth[selected].mean()
    assert precision >= SETTINGS.composite_target_precision - 0.05
    assert abs(choice.estimated_precision - precision) < 0.05
    assert truth[selected].sum() / truth.sum() > 0.7


# -- the trigger -------------------------------------------------------------------------------


def features(c_ab: float, c_ba: float) -> PairFeatures:
    return PairFeatures(
        n_a=100,
        n_b=100,
        n_match=int(100 * c_ab),
        c_ab=c_ab,
        c_ba=c_ba,
        lag_median=60.0,
        lag_iqr=10.0,
        lag_positive_share=0.95,
        fanout_ab_median=1.0,
        fanout_ab_p99=1.0,
        fanout_ba_median=1.0,
        fanout_ba_p99=1.0,
        concentration=0.1,
        chance_ratio=0.01,
        domain_size=1e6,
    )


def exact(
    a: str,
    b: str,
    score: float,
    c_ab: float = 0.95,
    c_ba: float = 0.95,
    kind: LinkType = LinkType.EXACT,
) -> ScoredLink:
    return ScoredLink(
        a=FieldForm(f"{a}/tpl/id", "raw"),
        b=FieldForm(f"{b}/tpl/ref", "raw"),
        link_type=kind,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=score,
        features=features(c_ab, c_ba),
        affected_volume=90,
    )


def adjacency(a: str, b: str, correlation: float, lag: int = 2) -> Adjacency:
    return Adjacency(a, b, lag, correlation)


def test_trigger_fires_without_an_exact_link_when_volumes_correlate() -> None:
    [trigger] = trigger_pairs([], [adjacency("sys_a", "sys_b", 0.8)], SETTINGS)
    assert trigger.triggered and trigger.reason == "no_exact_link"
    assert trigger.best_exact_score is None and trigger.lag_hours == 2


def test_trigger_does_not_fire_under_a_strong_exact_link() -> None:
    [trigger] = trigger_pairs(
        [exact("sys_a", "sys_b", 0.9)], [adjacency("sys_a", "sys_b", 0.9)], SETTINGS
    )
    assert not trigger.triggered and trigger.reason == "exact_link"
    assert trigger.best_exact_score == 0.9 and trigger.later_end_coverage == 0.95


def test_trigger_fires_when_the_exact_link_scores_at_or_below_the_ceiling() -> None:
    [trigger] = trigger_pairs(
        [exact("sys_a", "sys_b", SETTINGS.composite_exact_ceiling)],
        [adjacency("sys_a", "sys_b", 0.9)],
        SETTINGS,
    )
    assert trigger.triggered and trigger.reason == "no_exact_link"


def test_trigger_fires_when_the_exact_link_covers_too_little_of_the_later_end() -> None:
    # Scenario C: the HRIS carries the candidate id in 60% of rows.
    [trigger] = trigger_pairs(
        [exact("sys_onb", "sys_hris", 0.9, c_ab=0.9, c_ba=0.6)],
        [adjacency("sys_onb", "sys_hris", 0.7)],
        SETTINGS,
    )
    assert trigger.triggered and trigger.reason == "low_coverage"
    assert trigger.later_end_coverage == 0.6
    # The same link seen from the other side reads the coverage of the other end.
    [reverse] = trigger_pairs(
        [exact("sys_hris", "sys_onb", 0.9, c_ab=0.6, c_ba=0.9)],
        [adjacency("sys_onb", "sys_hris", 0.7)],
        SETTINGS,
    )
    assert reverse.triggered and reverse.later_end_coverage == 0.6


def test_coverage_is_measured_against_the_later_ends_events() -> None:
    # Scenario C: every non-null source_ref matches (token containment 1.0) but only 60% of
    # the HRIS rows carry one.
    link = exact("sys_onb", "sys_hris", 0.9, c_ab=0.6, c_ba=1.0)
    events = {("sys_hris", "tpl"): 100}
    assert event_coverage(link, "sys_hris", events) == (link.features.n_match / 100, 100)
    assert event_coverage(link, "sys_hris", None) == (1.0, None)
    assert event_coverage(link, "sys_hris", {}) == (1.0, None)
    [trigger] = trigger_pairs(
        [link], [adjacency("sys_onb", "sys_hris", 0.7)], SETTINGS, template_events=events
    )
    assert trigger.triggered and trigger.reason == "low_coverage"
    assert trigger.later_end_coverage == pytest.approx(0.6) and trigger.later_end_events == 100
    [covered] = trigger_pairs(
        [exact("sys_onb", "sys_hris", 0.9, c_ab=0.95, c_ba=1.0)],
        [adjacency("sys_onb", "sys_hris", 0.7)],
        SETTINGS,
        template_events=events,
    )
    assert not covered.triggered and covered.reason == "exact_link"
    assert covered.later_end_coverage == pytest.approx(0.95)
    # Coverage never exceeds one even when tokens repeat across events.
    assert event_coverage(link, "sys_hris", {("sys_hris", "tpl"): 10})[0] == 1.0


def test_links_between_weak_forms_are_not_exact_links_for_the_trigger() -> None:
    amount_link = ScoredLink(
        a=FieldForm("sys_pay/tpl/amount", "amount"),
        b=FieldForm("sys_wh/tpl/total", "amount"),
        link_type=LinkType.EXACT,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=0.99,
        features=features(0.95, 0.95),
        affected_volume=90,
    )
    assert identifier_links([amount_link]) == []
    assert exact_field_pairs([amount_link], SETTINGS.composite_exact_ceiling) == frozenset()
    [trigger] = trigger_pairs([amount_link], [adjacency("sys_pay", "sys_wh", 0.9)], SETTINGS)
    assert trigger.triggered and trigger.reason == "no_exact_link"
    real = exact("sys_pay", "sys_wh", 0.9)
    assert identifier_links([amount_link, real]) == [real]
    assert exact_field_pairs([real], SETTINGS.composite_exact_ceiling) == frozenset(
        {frozenset({"sys_pay/tpl/id", "sys_wh/tpl/ref"})}
    )
    assert exact_field_pairs([exact("sys_pay", "sys_wh", 0.5)], 0.5) == frozenset()


def test_trigger_needs_the_volume_correlation() -> None:
    [trigger] = trigger_pairs(
        [],
        [adjacency("sys_a", "sys_b", SETTINGS.composite_adjacency_min_correlation - 0.01)],
        SETTINGS,
    )
    assert not trigger.triggered and trigger.reason == "no_adjacency"
    [at_floor] = trigger_pairs(
        [], [adjacency("sys_a", "sys_b", SETTINGS.composite_adjacency_min_correlation)], SETTINGS
    )
    assert at_floor.triggered


def test_both_orderings_of_an_adjacent_pair_are_tried() -> None:
    # A clerk keys payments into purchase orders within hours: the rolling-day peaks read
    # 0.93 one way and 0.95 the other, which says nothing about who leads.
    forward, backward = trigger_pairs(
        [],
        [adjacency("sys_a", "sys_b", 0.93, lag=0), adjacency("sys_b", "sys_a", 0.95, lag=2)],
        SETTINGS,
    )
    assert forward.triggered and backward.triggered
    assert forward.reason == backward.reason == "no_exact_link"


def test_one_rule_stands_per_system_pair() -> None:
    def outcome(a: str, b: str, matched: int, score: float) -> CompositeOutcome:
        trigger = trigger_pairs([], [adjacency(a, b, 0.9)], SETTINGS)[0]
        link = ScoredLink(
            a=FieldForm(f"{a}/tpl/total", "amount"),
            b=FieldForm(f"{b}/tpl/total", "amount"),
            link_type=LinkType.COMPOSITE,
            role=LinkRole.TRANSACTION,
            direction=Direction.A_TO_B,
            score=score,
            features=features(0.9, 0.9),
            affected_volume=matched,
        )
        matches = tuple(CompositeMatch(f"a{n}", f"b{n}", 10.0) for n in range(matched))
        return CompositeOutcome(trigger, 100, 100, matched, False, rule(), link, matches)

    forward = outcome("sys_pay", "sys_wh", 4979, 0.8)
    backward = outcome("sys_wh", "sys_pay", 143, 0.9)
    other = outcome("sys_a", "sys_b", 50, 0.7)
    empty = CompositeOutcome(forward.trigger, 100, 100, 0, False, None, None)
    kept = one_rule_per_system_pair([backward, forward, other, empty])
    assert [o.rule is not None for o in kept] == [False, True, True, False]
    assert kept[0].link is None and kept[0].matches == () and kept[0].trigger is backward.trigger
    assert kept[0].stage == "other_ordering" and kept[1].stage == "rule"
    assert kept[1] is forward and kept[2] is other and kept[3] is empty
    # Equal match counts: the higher score stands.
    tie = one_rule_per_system_pair([outcome("x", "y", 30, 0.6), outcome("y", "x", 30, 0.7)])
    assert [o.link.score if o.link else None for o in tie] == [None, 0.7]


def test_bridges_count_as_exact_links_and_other_systems_do_not() -> None:
    [trigger] = trigger_pairs(
        [exact("sys_a", "sys_b", 0.99, kind=LinkType.BRIDGE), exact("sys_a", "sys_c", 0.99)],
        [adjacency("sys_a", "sys_b", 0.9)],
        SETTINGS,
    )
    assert not trigger.triggered
    [other] = trigger_pairs(
        [exact("sys_a", "sys_c", 0.99)], [adjacency("sys_a", "sys_b", 0.9)], SETTINGS
    )
    assert other.triggered


def test_cross_correlation_finds_the_lag_of_a_shifted_series() -> None:
    rng = np.random.default_rng(5)
    base = rng.random(24 * 7) * 10
    shifted = np.concatenate([np.zeros(5), base[:-5]])
    lag, correlation = cross_correlation(base, shifted, max_lag=72)
    assert lag == 5 and correlation > 0.99
    assert cross_correlation(base, np.ones_like(base), max_lag=72) == (0, 0.0)
    assert cross_correlation(base[:1], shifted[:1], max_lag=72) == (0, 0.0)


def test_key_event_series_counts_each_key_carrying_event_once_per_system() -> None:
    start = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    window = Window(start, start + timedelta(days=1))

    sample_token = "t1." + "A" * 22

    def row(event_id: str, system: str, at: datetime, field: str) -> IdentifierRow:
        return IdentifierRow(
            token=sample_token,
            field_ref=f"{system}/tpl/{field}",
            form="date",
            shape="9999-99-99",
            event_id=event_id,
            system_id=system,
            observed_at=at,
            stored_at=at,
        )

    rows = [
        row("e1", "sys_a", start + timedelta(hours=2, minutes=5), "when"),
        row("e1", "sys_a", start + timedelta(hours=2, minutes=5), "other_date"),
        row("e2", "sys_a", start + timedelta(hours=2, minutes=40), "when"),
        row("e3", "sys_b", start + timedelta(hours=30), "when"),  # outside: dropped
    ]
    series, hours = key_event_series(rows, window)
    assert hours == 24
    assert series["sys_a"][2] == 2.0 and series["sys_a"].sum() == 2.0
    assert "sys_b" not in series


def test_rolling_day_sums_the_trailing_24_hours_and_aligns_day_scale_lags() -> None:
    hours = 24 * 7
    morning = np.zeros(hours)
    spread = np.zeros(hours)
    for day in range(5):  # a morning intake keyed over the next day's business hours
        morning[day * 24 + 8] = 50.0
        spread[(day + 1) * 24 + 9 : (day + 1) * 24 + 17] = 6.0
    assert cross_correlation(morning, spread, 72)[1] < 0.5, "hourly shapes differ"
    lag, correlation = cross_correlation(rolling_day(morning), rolling_day(spread), 72)
    assert correlation >= 0.9 and 0 < lag <= 48
    assert rolling_day(np.ones(30))[23] == 24.0 and rolling_day(np.ones(30))[0] == 1.0
    assert rolling_day(np.zeros(0)).shape == (0,)


def test_primary_component_weighs_the_agreement_weight_by_presence() -> None:
    specs = (
        ComponentSpec(
            "partial_identifier", FieldForm("a/t/id", "digits.0"), FieldForm("b/t/ref", "digits.0")
        ),
        ComponentSpec(
            "phonetic", FieldForm("a/t/name", "phonetic.0"), FieldForm("b/t/name", "phonetic.0")
        ),
    )
    components = components_from(specs, np.asarray([1.0, 0.98]), np.asarray([0.002, 0.004]))
    assert components[0].weight > components[1].weight, "the id fragment is rarer"
    present = np.asarray([[True, True]] * 50 + [[False, True]] * 50)
    matrix = AgreementMatrix(agree=present.copy(), present=present)
    selected = np.arange(100)
    spec, component = primary_component(specs, components, matrix, selected)
    assert spec.kind == "phonetic" and component.kind == "phonetic", "present in every matched pair"
    everyone = AgreementMatrix.complete(np.ones((100, 2), dtype=np.bool_))
    assert primary_component(specs, components, everyone, selected)[0].kind == "partial_identifier"


def test_hourly_series_is_dense_over_the_window() -> None:
    start = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    window = Window(start, start + timedelta(days=2))
    volumes = [
        HourlyVolume("sys_a", start + timedelta(hours=3), 4),
        HourlyVolume("sys_a", start + timedelta(hours=3), 1),
        HourlyVolume("sys_b", start + timedelta(hours=30), 2),
        HourlyVolume("sys_b", start - timedelta(hours=1), 9),  # outside: dropped
    ]
    series, hours = hourly_series(volumes, window)
    assert hours == 48
    assert series["sys_a"][3] == 5.0 and series["sys_a"].sum() == 5.0
    assert series["sys_b"][30] == 2.0 and series["sys_b"].sum() == 2.0


# -- rules: views and repositories -------------------------------------------------------------


def rule() -> CompositeRule:
    return CompositeRule(
        components=(
            CompositeComponent(
                a=FieldForm("sys_a/tpl_a/when", "date"),
                b=FieldForm("sys_b/tpl_b/order_date", "date"),
                kind="date",
                m=0.98,
                u=0.05,
            ),
            CompositeComponent(
                a=FieldForm("sys_a/tpl_a/total", "amount"),
                b=FieldForm("sys_b/tpl_b/total", "amount"),
                kind="amount",
                m=0.97,
                u=0.002,
            ),
        ),
        window_seconds=3 * 86_400,
        threshold=4.5,
        estimated_precision=0.93,
    )


def test_component_views_match_the_api_contract_strongest_first() -> None:
    from carto_schema.api import CompositeComponentView  # noqa: PLC0415

    views = component_views(rule())
    assert [v["a"]["form"] for v in views] == ["amount", "date"], "strongest first"
    assert views[0]["a"] == {
        "field_id": "sys_a/tpl_a/total",
        "system_id": "sys_a",
        "template_id": "tpl_a",
        "path": "total",
        "form": "amount",
    }
    for view in views:
        parsed = CompositeComponentView.model_validate(view)
        assert parsed.weight == pytest.approx(np.log2(parsed.m / parsed.u))


def test_components_json_round_trips() -> None:
    document = components_json(rule())
    assert components_from_json(document) == rule().components
    assert components_from_json("junk") == ()
    assert components_from_json([{"a": 1}, "x"]) == ()


def exercise_rules(repository: object, tenant: str) -> None:
    from carto_core.linker.composite import CompositeRuleRepository  # noqa: PLC0415

    assert isinstance(repository, CompositeRuleRepository)
    assert repository.ping()
    assert repository.get(tenant, "lnk_missing") is None
    stored = repository.upsert(tenant, "lnk_one", rule(), FRIDAY)
    assert stored.id == "lnk_one" and stored.key_link_id == "lnk_one"
    assert stored.rule == rule()
    changed = CompositeRule(
        components=rule().components[:1],
        window_seconds=86_400,
        threshold=1.0,
        estimated_precision=None,
    )
    again = repository.upsert(tenant, "lnk_one", changed, MONDAY)
    assert again.rule == changed
    assert repository.get(tenant, "lnk_one") == again
    repository.upsert(tenant, "lnk_two", rule(), MONDAY)
    assert [row.id for row in repository.list_rules(tenant)] == ["lnk_one", "lnk_two"]
    assert repository.list_rules("another-tenant") == []


def test_in_memory_rule_repository() -> None:
    exercise_rules(InMemoryCompositeRuleRepository(), "mem")


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415

        client = docker.from_env()
    except Exception:
        return False
    try:
        client.ping()
    except Exception:
        return False
    finally:
        client.close()
    return True


integration = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


@pytest.fixture(scope="module")
def composite_postgres_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    from testcontainers.core.config import testcontainers_config  # noqa: PLC0415

    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.migrations import apply_postgres_migrations  # noqa: PLC0415
    from carto_core.settings import PostgresSettings  # noqa: PLC0415

    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
    image = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text("carto_test\n", encoding="utf-8")
    with PostgresContainer(
        image,
        username="carto_test",
        password="carto_test",  # noqa: S106 - throwaway container credential
        dbname="carto_test",
        driver=None,
    ) as container:
        settings = PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database="carto_test",
            user="carto_test",
            password_file=password_file,
            sslmode="disable",
        )
        engine = create_engine_from_settings(settings)
        apply_postgres_migrations(engine)
        yield engine
        engine.dispose()


@pytest.mark.parametrize("marker", [pytest.param(None, marks=integration)])
def test_postgres_rule_repository(composite_postgres_engine: object, marker: None) -> None:
    import sqlalchemy  # noqa: PLC0415
    from sqlalchemy import Engine  # noqa: PLC0415

    assert isinstance(composite_postgres_engine, Engine)
    exercise_rules(PostgresCompositeRuleRepository(composite_postgres_engine), "pg-rules")
    with composite_postgres_engine.connect() as connection:
        kind = connection.execute(
            sqlalchemy.text(
                "SELECT jsonb_typeof(components_json) FROM composite_rules "
                "WHERE tenant_id = 'pg-rules' AND id = 'lnk_two'"
            )
        ).scalar_one()
    assert kind == "array"
