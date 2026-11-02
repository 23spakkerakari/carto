"""Spec 18.4 metrics on tiny hand-built inputs; every expected number is derived in a comment."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from carto_eval import metrics
from carto_eval.metrics import FalseAlertResult, FaultResult
from carto_eval.predictions import (
    PredictedAlert,
    PredictedBatch,
    PredictedEntity,
    PredictedLink,
    PredictedManualHop,
)
from carto_simulator.ground_truth import (
    BatchTruth,
    EntityField,
    EntityTruth,
    FaultKind,
    FaultTruth,
    FieldRef,
    LinkTruth,
    LinkType,
    ManualHopTruth,
)

T0 = datetime(2026, 9, 28, 14, 20, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)


def ref(field: str, source: str = "src_a", system: str = "sys_a") -> FieldRef:
    return FieldRef(system_id=system, source_id=source, field=field)


def member(field: str, form: str = "raw", source: str = "src_a") -> EntityField:
    return EntityField(ref=ref(field, source), form=form)


def entity(entity_id: str, *fields: EntityField) -> EntityTruth:
    return EntityTruth(entity_id=entity_id, name=entity_id, fields=list(fields))


def family(entity_id: str, *fields: EntityField) -> PredictedEntity:
    return PredictedEntity(entity_id=entity_id, fields=list(fields))


def truth_link(
    link_id: str,
    a: FieldRef,
    b: FieldRef,
    link_type: LinkType,
    *,
    form_a: str = "raw",
    form_b: str = "raw",
) -> LinkTruth:
    return LinkTruth(
        link_id=link_id,
        a=a,
        form_a=form_a,
        b=b,
        form_b=form_b,
        link_type=link_type,
        role="transaction",
        direction="a_to_b",
        entity_id="ent_x",
    )


def predicted_link(
    a: FieldRef, b: FieldRef, link_type: LinkType, *, form_a: str = "raw", form_b: str = "raw"
) -> PredictedLink:
    return PredictedLink(
        a=a,
        form_a=form_a,
        b=b,
        form_b=form_b,
        link_type=link_type,
        role="transaction",
        score=0.9,
    )


def alert(
    alert_id: str,
    kind: str = "error_rate",
    opened: datetime = T0,
    resolved: datetime | None = None,
    affected: Sequence[str] = ("t1",),
    *,
    gap: bool = False,
    causes: Sequence[str] = (),
) -> PredictedAlert:
    return PredictedAlert(
        alert_id=alert_id,
        expectation_kind=kind,
        target="node",
        opened_at=opened,
        resolved_at=resolved,
        affected_txn_ids=list(affected),
        is_visibility_gap=gap,
        likely_causes=list(causes),
    )


def fault(
    fault_id: str,
    kind: FaultKind = FaultKind.ERROR_SPIKE,
    start: datetime = T0,
    end: datetime | None = T0 + 40 * MINUTE,
    *,
    expected: bool = True,
    alert_kind: str | None = "error_rate",
    cause: str | None = "error_spike",
    affected: Sequence[str] = ("t1", "t2"),
) -> FaultTruth:
    return FaultTruth(
        fault_id=fault_id,
        kind=kind,
        start=start,
        end=end,
        system_id="sys_a",
        affected_txn_ids=list(affected),
        expected_alert=expected,
        expected_alert_kind=alert_kind,
        expected_cause_kind=cause,
    )


def hop(
    from_node: str, to_node: str, score: float, confirmed: bool | None = None
) -> PredictedManualHop:
    return PredictedManualHop(
        from_node=from_node, to_node=to_node, score=score, confirmed=confirmed
    )


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def test_ratio_is_none_when_undefined() -> None:
    assert metrics.ratio(0, 0) is None
    assert metrics.ratio(1, 4) == 0.25
    assert metrics.ratio(0, 3) == 0.0


def test_p95_nearest_rank() -> None:
    # ten values 10..100: rank ceil of 9.5 is 10, the largest value
    assert metrics.p95(range(10, 110, 10)) == 100
    # three values: rank ceil of 2.85 is 3, so the largest again, whatever the input order
    assert metrics.p95([30, 10, 20]) == 30
    # twenty values 1..20: rank ceil of 19 is 19
    assert metrics.p95(range(1, 21)) == 19
    assert metrics.p95([7]) == 7
    assert metrics.p95([]) is None


def test_pairs_is_n_choose_2() -> None:
    assert [metrics.pairs(n) for n in range(5)] == [0, 0, 1, 3, 6]


def test_link_pair_is_unordered() -> None:
    a, b = ref("x"), ref("y", "src_b")
    assert metrics.link_pair(a, "raw", b, "digits.0") == metrics.link_pair(b, "digits.0", a, "raw")
    assert metrics.link_pair(a, "raw", b, "raw") != metrics.link_pair(a, "raw", b, "digits.0")


# ---------------------------------------------------------------------------------------------
# Transaction pairwise F1
# ---------------------------------------------------------------------------------------------


def test_pairwise_f1_from_contingency_counts() -> None:
    # true clusters: T1 holds a, b, c (three pairs) and T2 holds d, e (one pair): four true pairs.
    # predicted: P1 holds a, b (one pair), P2 holds c, d (one pair), e is not placed: two pairs.
    # only (a, b) is together on both sides: precision 1/2, recall 1/4, f1 is 2 over (2 plus 4).
    truth = {"a": "T1", "b": "T1", "c": "T1", "d": "T2", "e": "T2"}
    predicted = {"a": "P1", "b": "P1", "c": "P2", "d": "P2"}
    result = metrics.transaction_pairwise_f1(truth, predicted)
    assert (result.keys, result.true_pairs, result.predicted_pairs, result.matched_pairs) == (
        5,
        4,
        2,
        1,
    )
    assert result.precision == 0.5
    assert result.recall == 0.25
    assert result.f1 == pytest.approx(1 / 3)


def test_pairwise_singleton_rule() -> None:
    # a and b are truly together (one true pair); a is placed, b is unassigned or missing, so it
    # is its own cluster: no predicted pair at all, precision undefined, recall and f1 zero.
    truth = {"a": "T1", "b": "T1"}
    for predicted in ({"a": "P1", "b": None}, {"a": "P1"}):
        result = metrics.transaction_pairwise_f1(truth, predicted)
        assert (result.true_pairs, result.predicted_pairs, result.matched_pairs) == (1, 0, 0)
        assert result.precision is None
        assert result.recall == 0.0
        assert result.f1 == 0.0


def test_pairwise_ignores_keys_without_a_true_transaction() -> None:
    # n is noise; placing it with a and b must neither help nor hurt: one true pair, one predicted
    # pair among the scored keys, matched.
    truth = {"a": "T1", "b": "T1", "n": None}
    predicted = {"a": "P1", "b": "P1", "n": "P1"}
    result = metrics.transaction_pairwise_f1(truth, predicted)
    assert result.keys == 2
    assert (result.true_pairs, result.predicted_pairs, result.matched_pairs) == (1, 1, 1)
    assert result.f1 == 1.0


def test_pairwise_over_merge() -> None:
    # two true clusters of two (two true pairs) merged into one predicted cluster of four
    # (six predicted pairs): precision 2/6, recall 1, f1 is 4 over (6 plus 2).
    truth = {"a": "T1", "b": "T1", "c": "T2", "d": "T2"}
    predicted = dict.fromkeys(truth, "P1")
    result = metrics.transaction_pairwise_f1(truth, predicted)
    assert result.precision == pytest.approx(1 / 3)
    assert result.recall == 1.0
    assert result.f1 == 0.5


def test_pairwise_empty_prediction_scores_zero_and_empty_truth_is_undefined() -> None:
    truth = {"a": "T1", "b": "T1"}
    assert metrics.transaction_pairwise_f1(truth, {}).f1 == 0.0
    assert metrics.transaction_pairwise_f1({}, {"a": "P1"}).f1 is None
    # true singletons only: no true pair, so recall is undefined and a predicted pair is a
    # false one: precision 0, f1 0
    result = metrics.transaction_pairwise_f1({"a": "T1", "b": "T2"}, {"a": "P1", "b": "P1"})
    assert result.recall is None
    assert result.precision == 0.0
    assert result.f1 == 0.0


# ---------------------------------------------------------------------------------------------
# Entity purity
# ---------------------------------------------------------------------------------------------


def test_purity_with_a_mixed_family_and_an_unknown_member() -> None:
    # truth: E1 holds m1, m2, m3; E2 holds m4, m5.
    # F1 holds m1, m2, m4: the largest single-entity share is 2 of 3.
    # F2 holds m5 and m9 (in no true entity): 1 of 2.
    # size-weighted: (2 plus 1) over (3 plus 2), 0.6
    truth = [
        entity("E1", member("m1"), member("m2"), member("m3")),
        entity("E2", member("m4"), member("m5")),
    ]
    predicted = [
        family("F1", member("m1"), member("m2"), member("m4")),
        family("F2", member("m5"), member("m9")),
    ]
    result = metrics.entity_purity(truth, predicted)
    assert (result.families, result.members, result.pure_members) == (2, 5, 3)
    assert result.purity == pytest.approx(0.6)


def test_purity_is_one_for_the_truth_itself_and_undefined_without_families() -> None:
    truth = [entity("E1", member("m1"), member("m2")), entity("E2", member("m3", "digits.0"))]
    predicted = [family(e.entity_id, *e.fields) for e in truth]
    assert metrics.entity_purity(truth, predicted).purity == 1.0
    assert metrics.entity_purity(truth, []).purity is None
    # a family without members is ignored, so it does not make the metric defined
    assert metrics.entity_purity(truth, [family("F0")]).purity is None


def test_purity_counts_a_repeated_member_once_whatever_its_forms() -> None:
    # F lists m1 twice and m4 once: distinct members m1, m4, one of them in E1: 1 of 2.
    truth = [entity("E1", member("m1")), entity("E2", member("m4"))]
    predicted = [family("F", member("m1"), member("m1"), member("m4"))]
    assert metrics.entity_purity(truth, predicted).purity == 0.5
    # m1 in form digits.0 is the same field as m1 raw (spec 9.6 families are over fields):
    # one member, in E1, so the family is pure
    predicted = [family("F", member("m1"), member("m1", "digits.0"))]
    assert metrics.entity_purity(truth, predicted).purity == 1.0


def test_purity_member_in_two_true_entities_counts_for_both() -> None:
    truth = [entity("E1", member("m1"), member("m2")), entity("E2", member("m2"), member("m3"))]
    # F holds m2 and m3: both belong to E2, purity 1
    assert metrics.entity_purity(truth, [family("F", member("m2"), member("m3"))]).purity == 1.0


# ---------------------------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------------------------


def test_bridges_are_compared_by_field_pair_whatever_the_forms() -> None:
    """A bridge is a property of two fields in one record (spec 9.4): the truth may name it in
    the form that links across systems, the engine stores one link per field pair."""
    a = FieldRef(system_id="sys_w", source_id="src_w", field="order_ref")
    b = FieldRef(system_id="sys_w", source_id="src_w", field="po_num")
    c = FieldRef(system_id="sys_o", source_id="src_o", field="order_id")
    truth = [
        LinkTruth(
            link_id="L13",
            a=a,
            form_a="digits.0",
            b=b,
            form_b="raw",
            link_type="bridge",
            role="transaction",
            direction="undirected",
            entity_id="E1",
        ),
        LinkTruth(
            link_id="L5",
            a=c,
            form_a="raw",
            b=a,
            form_b="digits.0",
            link_type="exact",
            role="transaction",
            direction="a_to_b",
            entity_id="E1",
        ),
    ]
    predicted = [
        PredictedLink(
            a=b, form_a="raw", b=a, form_b="raw", link_type="bridge", role="transaction", score=1.0
        ),
        PredictedLink(
            a=a, form_a="raw", b=c, form_b="raw", link_type="exact", role="transaction", score=0.9
        ),
    ]
    result = metrics.link_precision_recall(truth, predicted, frozenset({"exact", "bridge"}))
    # the bridge matches in other forms; the exact link in another form does not
    assert (result.matched, result.predicted, result.expected) == (1, 2, 2)
    assert result.precision == 0.5 and result.recall == 0.5
    # an exact prediction over a truth bridge's field pair counts, in any forms
    predicted = [
        PredictedLink(
            a=a, form_a="raw", b=b, form_b="raw", link_type="exact", role="transaction", score=1.0
        )
    ]
    result = metrics.link_precision_recall(truth[:1], predicted, frozenset({"exact", "bridge"}))
    assert result.precision == 1.0 and result.recall == 1.0


def test_link_precision_recall_ignores_direction_and_respects_the_type_filter() -> None:
    a, b, c = ref("a"), ref("b", "src_b"), ref("c", "src_c")
    d, e = ref("d", "src_d"), ref("e", "src_e")
    truth = [
        truth_link("L1", a, b, "exact"),
        truth_link("L2", b, c, "bridge"),
        truth_link("L3", d, e, "composite"),
    ]
    predicted = [
        predicted_link(b, a, "exact"),  # L1, reversed
        predicted_link(a, c, "exact"),  # wrong
        predicted_link(e, d, "composite"),  # L3, reversed
    ]
    # exact and bridge: expected {ab, bc}, predicted {ab, ac}: one match of two each side
    result = metrics.link_precision_recall(truth, predicted, frozenset({"exact", "bridge"}))
    assert (result.matched, result.predicted, result.expected) == (1, 2, 2)
    assert result.precision == 0.5
    assert result.recall == 0.5
    # composite: expected {de}, predicted {de}
    result = metrics.link_precision_recall(truth, predicted, frozenset({"composite"}))
    assert result.precision == 1.0
    assert result.recall == 1.0


def test_link_type_filter_applies_to_both_sides() -> None:
    a, b = ref("a"), ref("b", "src_b")
    truth = [truth_link("L1", a, b, "exact")]
    predicted = [predicted_link(a, b, "composite")]
    result = metrics.link_precision_recall(truth, predicted, frozenset({"exact", "bridge"}))
    assert result.precision is None  # nothing predicted of that type
    assert result.recall == 0.0
    result = metrics.link_precision_recall(truth, predicted, frozenset({"composite"}))
    assert result.precision == 0.0
    assert result.recall is None  # nothing expected of that type
    # an exact prediction of a true bridge counts: the table does not separate the two
    result = metrics.link_precision_recall(
        [truth_link("L2", a, b, "bridge")],
        [predicted_link(a, b, "exact")],
        frozenset({"exact", "bridge"}),
    )
    assert result.precision == 1.0


def test_link_form_is_part_of_the_identity() -> None:
    order_id, order_ref = ref("order_id", "src_orders_log"), ref("order_ref", "src_wms_db")
    truth = [truth_link("L5", order_id, order_ref, "exact", form_b="digits.0")]
    wrong_form = [predicted_link(order_id, order_ref, "exact")]
    right_form = [predicted_link(order_id, order_ref, "exact", form_b="digits.0")]
    types = frozenset({"exact", "bridge"})
    assert metrics.link_precision_recall(truth, wrong_form, types).matched == 0
    assert metrics.link_precision_recall(truth, right_form, types).matched == 1


# ---------------------------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------------------------


def test_batch_key_precision_recall_over_key_values() -> None:
    field = ref("file_name", "src_ship_sftp", "sys_shipping")
    truth = [
        BatchTruth(batch_id="b1", key_field=field, key_value="SHIP_1.csv", txn_ids=["t1"]),
        BatchTruth(batch_id="b2", key_field=field, key_value="SHIP_2.csv", txn_ids=["t2"]),
        BatchTruth(batch_id="b3", key_field=field, key_value="MAN-1", txn_ids=["t1"]),
    ]
    predicted = [
        PredictedBatch(key_value="SHIP_1.csv", txn_ids=["t1", "t9"]),
        PredictedBatch(key_value="MAN-1"),
        PredictedBatch(key_value="other"),
    ]
    # expected {SHIP_1, SHIP_2, MAN-1}, predicted {SHIP_1, MAN-1, other}: two of three each side
    result = metrics.batch_key_precision_recall(truth, predicted)
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(2 / 3)
    assert metrics.batch_key_precision_recall(truth, []).precision is None
    assert metrics.batch_key_precision_recall(truth, []).recall == 0.0


# ---------------------------------------------------------------------------------------------
# Fault detection
# ---------------------------------------------------------------------------------------------


def test_fault_detected_with_time_to_detect() -> None:
    # the alert opens 60 s after the fault start, carries the right kind and shares t2
    result = metrics.fault_detection(
        [fault("f1")], [alert("a1", opened=T0 + 60 * SECOND, affected=["t2"])]
    )
    (fault_result,) = result.faults
    assert fault_result.detected
    assert fault_result.matched_alert_id == "a1"
    assert fault_result.time_to_detect_seconds == 60.0
    assert (result.expected, result.detected, result.recall) == (1, 1, 1.0)
    assert result.time_to_detect_p95_seconds == 60.0
    assert result.false_alert_ids == ()


@pytest.mark.parametrize(
    ("opened", "detected"),
    [
        (T0 - 2 * MINUTE, True),  # exactly at the start of the grace
        (T0 - 2 * MINUTE - SECOND, False),  # one second too early
        (T0 + 40 * MINUTE + 2 * MINUTE, True),  # exactly at the end of the grace after the end
        (T0 + 42 * MINUTE + SECOND, False),  # one second too late
    ],
)
def test_fault_detection_grace(opened: datetime, detected: bool) -> None:
    result = metrics.fault_detection([fault("f1")], [alert("a1", opened=opened)])
    assert result.faults[0].detected is detected
    if detected and opened < T0:
        # an alert inside the grace before the start has time to detect zero, never negative
        assert result.faults[0].time_to_detect_seconds == 0.0


def test_fault_not_detected_by_the_wrong_kind_or_disjoint_transactions() -> None:
    wrong_kind = alert("a_sched", kind="schedule")
    disjoint = alert("a_other", affected=["t9"])
    result = metrics.fault_detection([fault("f1")], [wrong_kind, disjoint])
    assert not result.faults[0].detected
    assert result.recall == 0.0
    assert result.time_to_detect_p95_seconds is None
    # both alerts matched nothing: false alerts, in opening order then id
    assert result.false_alert_ids == ("a_other", "a_sched")


def test_fault_without_kind_or_affected_ids_matches_loosely() -> None:
    loose = fault("f_loose", alert_kind=None, affected=())
    result = metrics.fault_detection([loose], [alert("a1", kind="schedule", affected=["t9"])])
    assert result.faults[0].matched_alert_id == "a1"


def test_fault_with_expected_alert_false_is_excluded_and_its_alert_is_false() -> None:
    skew = fault(
        "f6",
        FaultKind.CLOCK_SKEW,
        end=None,
        expected=False,
        alert_kind=None,
        cause=None,
        affected=(),
    )
    skew_alert = alert("a_skew", kind="freshness", opened=T0 + 60 * MINUTE)
    spike_alert = alert("a_spike", opened=T0 + MINUTE)
    result = metrics.fault_detection([skew, fault("f1")], [skew_alert, spike_alert])
    by_id = {r.fault_id: r for r in result.faults}
    # the skew fault did get an alert (the earliest matching one is a_spike, kind unconstrained)
    assert by_id["f6"].detected
    assert not by_id["f6"].expected_alert
    assert by_id["f1"].matched_alert_id == "a_spike"
    # recall counts expected faults only; a_skew matched no expected fault
    assert (result.expected, result.detected, result.recall) == (1, 1, 1.0)
    assert result.false_alert_ids == ("a_skew",)
    # alone, the skew fault leaves recall undefined
    alone = metrics.fault_detection([skew], [skew_alert])
    assert alone.recall is None
    assert alone.false_alert_ids == ("a_skew",)


def test_open_ended_fault_window_is_one_day_plus_grace() -> None:
    open_ended = fault("f_open", end=None)
    inside = alert("a1", opened=T0 + timedelta(days=1, minutes=2))
    outside = alert("a2", opened=T0 + timedelta(days=1, minutes=2, seconds=1))
    assert metrics.fault_detection([open_ended], [inside]).faults[0].detected
    assert not metrics.fault_detection([open_ended], [outside]).faults[0].detected


def test_earliest_matching_alert_wins_ties_by_id() -> None:
    later = alert("a_later", opened=T0 + 5 * MINUTE)
    early_b = alert("b_early", opened=T0 + MINUTE)
    early_a = alert("a_early", opened=T0 + MINUTE)
    result = metrics.fault_detection([fault("f1")], [later, early_b, early_a])
    assert result.faults[0].matched_alert_id == "a_early"
    assert result.faults[0].time_to_detect_seconds == 60.0
    assert result.false_alert_ids == ("b_early", "a_later")


def test_one_alert_may_detect_two_faults_and_p95_spans_detected_faults() -> None:
    first = fault("f1", affected=("t1",))
    second = fault("f2", start=T0 + 10 * MINUTE, end=T0 + 50 * MINUTE, affected=("t1",))
    shared = alert("a1", opened=T0 + 11 * MINUTE)
    result = metrics.fault_detection([first, second], [shared])
    # f1 sees the alert 660 s after its start, f2 60 s; p95 of two values is the larger
    assert [r.time_to_detect_seconds for r in result.faults] == [660.0, 60.0]
    assert result.time_to_detect_p95_seconds == 660.0
    assert result.recall == 1.0
    assert result.false_alert_ids == ()


# ---------------------------------------------------------------------------------------------
# Visibility gap attribution
# ---------------------------------------------------------------------------------------------


GAP = fault(
    "f3",
    FaultKind.VISIBILITY_GAP,
    end=T0 + 2 * timedelta(hours=1),
    alert_kind="freshness",
    cause="visibility_gap",
)
GAP_ALERT = alert("a_gap", kind="freshness", opened=T0 + 30 * SECOND, gap=True)


def attribution(*alerts: PredictedAlert, faults: Sequence[FaultTruth] = (GAP,)) -> float | None:
    detection = metrics.fault_detection(faults, alerts)
    return metrics.visibility_gap_attribution(faults, alerts, detection)


def test_visibility_gap_attributed_when_flagged_and_no_stall_blames_it() -> None:
    assert attribution(GAP_ALERT) == 1.0
    # a stall after the gap ended, or one about other transactions, does not count against it
    late_stall = alert("a_late", kind="hop_deadline", opened=T0 + timedelta(hours=3))
    other_stall = alert("a_other", kind="hop_deadline", opened=T0 + MINUTE, affected=["t9"])
    resolved_before = alert(
        "a_before", kind="hop_deadline", opened=T0 - timedelta(hours=1), resolved=T0 - 30 * MINUTE
    )
    assert attribution(GAP_ALERT, late_stall, other_stall, resolved_before) == 1.0


def test_visibility_gap_reported_as_stall_is_not_attributed() -> None:
    stall = alert("a_stall", kind="hop_deadline", opened=T0 + timedelta(hours=1), affected=["t2"])
    assert attribution(GAP_ALERT, stall) == 0.0
    # a stall opened before the gap and still open during it blames it too
    lingering = alert("a_linger", kind="hop_deadline", opened=T0 - timedelta(hours=1))
    assert attribution(GAP_ALERT, lingering) == 0.0
    # the matched alert itself must say it is a visibility gap
    unflagged = alert("a_unflagged", kind="freshness", opened=T0 + 30 * SECOND)
    assert attribution(unflagged) == 0.0


def test_visibility_gap_undetected_or_absent() -> None:
    assert attribution() == 0.0
    assert attribution(GAP_ALERT, faults=[fault("f1")]) is None
    # two gaps, one attributed: one half
    second_gap = fault(
        "f3b",
        FaultKind.VISIBILITY_GAP,
        start=T0 + timedelta(days=1),
        end=T0 + timedelta(days=1, hours=2),
        alert_kind="freshness",
        cause="visibility_gap",
        affected=("t7",),
    )
    assert attribution(GAP_ALERT, faults=[GAP, second_gap]) == 0.5


# ---------------------------------------------------------------------------------------------
# False alerts on fault-free days
# ---------------------------------------------------------------------------------------------


RUN_START = date(2026, 9, 27)
"""A four-day run, Sep 27 to Sep 30: T0 (Sep 28, 14:20) falls on day 2."""
SKEW = fault(
    "f6",
    FaultKind.CLOCK_SKEW,
    start=datetime(2026, 9, 27, tzinfo=UTC),
    end=datetime(2026, 10, 1, tzinfo=UTC),
    expected=False,
    alert_kind=None,
    cause=None,
    affected=(),
)
"""Spans every day of the run and must not alert, like the warehouse clock skew."""


def false_alerts(
    faults: Sequence[FaultTruth],
    alerts: Sequence[PredictedAlert],
    *,
    start_date: date = RUN_START,
    days: int = 4,
    n_entities: int = 2,
) -> FalseAlertResult:
    detection = metrics.fault_detection(faults, alerts)
    return metrics.false_alerts_per_flow_day(
        faults, alerts, detection, start_date=start_date, days=days, n_entities=n_entities
    )


def test_run_window_is_the_calendar_days_from_the_start_date() -> None:
    assert metrics.run_window(RUN_START, 3) == (
        date(2026, 9, 27),
        date(2026, 9, 28),
        date(2026, 9, 29),
    )
    assert metrics.run_window(RUN_START, 0) == ()


def test_faulty_days_follow_expected_faults_only() -> None:
    window = metrics.run_window(RUN_START, 4)
    # f1 runs 14:20 to 15:00 on day 2; the skew spans the run but must not alert
    assert metrics.faulty_days([fault("f1"), SKEW], window) == {date(2026, 9, 28)}
    assert metrics.faulty_days([SKEW], window) == frozenset()
    # an open-ended fault covers one day from its start: days 2 and 3
    assert metrics.faulty_days([fault("f_open", end=None)], window) == {
        date(2026, 9, 28),
        date(2026, 9, 29),
    }
    # a day is [00:00, 24:00) and a fault window is closed: a fault ending exactly at midnight
    # touches the day that starts then; one starting at midnight leaves the day before alone
    midnight = datetime(2026, 9, 29, tzinfo=UTC)
    ends_at_midnight = fault("f_end", start=midnight - timedelta(hours=1), end=midnight)
    starts_at_midnight = fault("f_start", start=midnight, end=midnight + timedelta(hours=1))
    assert metrics.faulty_days([ends_at_midnight], window) == {
        date(2026, 9, 28),
        date(2026, 9, 29),
    }
    assert metrics.faulty_days([starts_at_midnight], window) == {date(2026, 9, 29)}
    # a fault outside the window touches no run day
    before = fault("f_before", start=T0 - 10 * DAY, end=T0 - 9 * DAY)
    assert metrics.faulty_days([before], window) == frozenset()


def test_false_alerts_are_rated_on_fault_free_days_only() -> None:
    # four days; f1 makes day 2 faulty and the skew never makes one faulty: three fault-free
    # days. a_hit detects f1. a_day2 (wrong kind), a_day4 and b_day4 (after the window) match no
    # expected fault: three false alerts, although every one of them falls inside the skew's
    # window. Two of them open on fault-free days, over two flows times three days: 2 over 6
    faults = [fault("f1"), SKEW]
    alerts = [
        alert("a_hit", opened=T0 + MINUTE),
        alert("a_day2", kind="volume", opened=T0 + 2 * MINUTE),
        alert("a_day4", opened=T0 + 2 * DAY),
        alert("b_day4", opened=T0 + 2 * DAY + MINUTE),
    ]
    assert metrics.fault_detection(faults, alerts).false_alert_ids == (
        "a_day2",
        "a_day4",
        "b_day4",
    )
    result = false_alerts(faults, alerts)
    assert result == FalseAlertResult(
        false_alerts_total=3,
        false_alerts_on_fault_free_days=2,
        fault_free_days=3,
        run_days=4,
        n_entities=2,
    )
    assert result.per_flow_day == pytest.approx(1 / 3)


def test_false_alerts_without_faults_count_every_day() -> None:
    alerts = [alert("a_day2", opened=T0), alert("a_day4", opened=T0 + 2 * DAY)]
    # no fault, so every day is fault-free and both alerts are false: 2 over 2 times 4
    result = false_alerts([], alerts)
    assert result == FalseAlertResult(
        false_alerts_total=2,
        false_alerts_on_fault_free_days=2,
        fault_free_days=4,
        run_days=4,
        n_entities=2,
    )
    assert result.per_flow_day == 0.25


def test_false_alerts_undefined_without_fault_free_days_or_entities() -> None:
    # a two-day run (Sep 28 and 29) under an open-ended fault that covers both days
    open_ended = fault("f_open", end=None)
    noise = alert("a_noise", kind="volume", opened=T0 + DAY)
    result = false_alerts([open_ended], [noise], start_date=date(2026, 9, 28), days=2)
    assert result == FalseAlertResult(
        false_alerts_total=1,
        false_alerts_on_fault_free_days=0,
        fault_free_days=0,
        run_days=2,
        n_entities=2,
    )
    assert result.per_flow_day is None
    # fault-free days but no flow to rate against
    result = false_alerts([], [noise], n_entities=0)
    assert (result.fault_free_days, result.false_alerts_on_fault_free_days) == (4, 1)
    assert result.per_flow_day is None


def test_false_alert_outside_the_run_counts_in_the_total_only() -> None:
    early = alert("a_early", opened=T0 - 10 * DAY)
    late = alert("a_late", opened=T0 + 10 * DAY)
    # 23:30 on the last day two hours behind UTC is 01:30 UTC the day after the run
    offset = alert(
        "a_offset", opened=datetime(2026, 9, 30, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    )
    result = false_alerts([], [early, late, offset], n_entities=1)
    assert (result.false_alerts_total, result.false_alerts_on_fault_free_days) == (3, 0)
    assert result.per_flow_day == 0.0


# ---------------------------------------------------------------------------------------------
# Manual hops, likely cause
# ---------------------------------------------------------------------------------------------


MANUAL = ManualHopTruth(
    hop_id="hop_payments_to_warehouse",
    from_node="payments:authorized",
    to_node="warehouse:po_created",
    entity_id="ent_order",
    manual=True,
    share_manual=0.3,
)
AUTOMATED = ManualHopTruth(
    hop_id="hop_webstore_to_orders",
    from_node="webstore:checkout_completed",
    to_node="orders:order_created",
    entity_id="ent_order",
    manual=False,
    share_manual=0.0,
)


def test_manual_hops_at_the_threshold() -> None:
    predicted = [
        hop("payments:authorized", "warehouse:po_created", 0.7),  # exactly at the threshold
        hop("webstore:checkout_completed", "orders:order_created", 0.69),  # just below
    ]
    result = metrics.manual_hop_precision_recall([MANUAL, AUTOMATED], predicted)
    assert (result.matched, result.predicted, result.expected) == (1, 1, 1)
    assert result.precision == 1.0
    assert result.recall == 1.0


@pytest.mark.parametrize(
    ("score", "confirmed", "manual"),
    [
        (0.9, False, False),  # a dismissal vetoes a score above the threshold
        (0.2, True, True),  # a confirmation promotes a score below it
        (0.7, None, True),  # no decision: the threshold decides
        (0.69, None, False),
    ],
)
def test_predicted_manual_follows_the_reviewer_then_the_threshold(
    score: float, confirmed: bool | None, manual: bool
) -> None:
    predicted = hop("payments:authorized", "warehouse:po_created", score, confirmed)
    assert metrics.predicted_manual(predicted) is manual
    result = metrics.manual_hop_precision_recall([MANUAL, AUTOMATED], [predicted])
    assert (result.matched, result.predicted) == ((1, 1) if manual else (0, 0))


def test_manual_hops_reviewer_decision_wins_over_the_score() -> None:
    confirmed_low = hop("webstore:checkout_completed", "orders:order_created", 0.1, confirmed=True)
    dismissed_high = hop("payments:authorized", "warehouse:po_created", 0.9, confirmed=False)
    result = metrics.manual_hop_precision_recall(
        [MANUAL, AUTOMATED], [confirmed_low, dismissed_high]
    )
    # only the confirmed hop is predicted manual, and it is the automated one: precision 0; the
    # dismissed hop is the truly manual one and is missed: recall 0
    assert (result.matched, result.predicted, result.expected) == (0, 1, 1)
    assert result.precision == 0.0
    assert result.recall == 0.0


def test_manual_hops_missed_or_nothing_predicted() -> None:
    low = hop("payments:authorized", "warehouse:po_created", 0.2)
    result = metrics.manual_hop_precision_recall([MANUAL, AUTOMATED], [low])
    assert result.precision is None
    assert result.recall == 0.0
    wrong = hop("webstore:checkout_completed", "orders:order_created", 0.8)
    result = metrics.manual_hop_precision_recall([MANUAL, AUTOMATED], [low, wrong])
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert metrics.manual_hop_precision_recall([AUTOMATED], [wrong]).recall is None


def fault_result(
    fault_id: str,
    matched: str | None,
    *,
    cause: str | None = "error_spike",
    expected: bool = True,
) -> FaultResult:
    return FaultResult(
        fault_id=fault_id,
        kind="error_spike",
        expected_alert=expected,
        expected_cause_kind=cause,
        matched_alert_id=matched,
        time_to_detect_seconds=None if matched is None else 1.0,
    )


def test_likely_cause_top1() -> None:
    alerts = [
        alert("a_right", causes=["error_spike", "visibility_gap"]),
        alert("a_wrong", causes=["visibility_gap", "error_spike"]),
        alert("a_none"),
        alert("a_skew", causes=["error_spike"]),
    ]
    results = [
        fault_result("f_right", "a_right"),  # first cause matches
        fault_result("f_wrong", "a_wrong"),  # right cause ranked second
        fault_result("f_none", "a_none"),  # no causes at all
        fault_result("f_no_cause", "a_right", cause=None),  # no expected cause: excluded
        fault_result("f_missed", None),  # undetected: excluded
        fault_result("f_skew", "a_skew", expected=False),  # must not alert: excluded
    ]
    result = metrics.likely_cause_top1(results, alerts)
    # three faults considered, one correct
    assert (result.considered, result.correct) == (3, 1)
    assert result.accuracy == pytest.approx(1 / 3)
    assert metrics.likely_cause_top1([fault_result("f_missed", None)], alerts).accuracy is None
