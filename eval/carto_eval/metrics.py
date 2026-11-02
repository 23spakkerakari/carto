"""Metric functions of the eval harness, one per row of the spec 18.4 table.

Every function is pure: it takes ground truth (:mod:`carto_simulator.ground_truth`) and
predictions (:mod:`carto_eval.predictions`) and returns a small frozen dataclass. A metric with
nothing to score (no predicted links, no injected faults) is ``None``, never 0 or 1, so the report
says "n/a" instead of claiming a pass or a fail. The docstring of each function names the table
row it implements.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from carto_eval.predictions import (
    PredictedAlert,
    PredictedBatch,
    PredictedEntity,
    PredictedLink,
    PredictedManualHop,
)
from carto_simulator.ground_truth import (
    BatchTruth,
    EntityTruth,
    FaultKind,
    FaultTruth,
    FieldRef,
    LinkTruth,
    ManualHopTruth,
)

DEFAULT_GRACE = timedelta(minutes=2)
DEFAULT_MANUAL_THRESHOLD = 0.7
OPEN_ENDED_FAULT_SPAN = timedelta(days=1)

FieldKey = tuple[str, str, str, str]
"""A field and a form as a plain tuple: system, source, field, form."""
FieldOnlyKey = tuple[str, str, str]
"""A field as a plain tuple: system, source, field (family members, spec 9.6)."""
LinkPair = tuple[FieldKey, FieldKey]
"""The two ends of a link in sorted order, so direction does not matter."""
FieldPair = tuple[FieldOnlyKey, FieldOnlyKey]
"""The two fields of a link in sorted order, forms dropped (bridges)."""
HopPair = tuple[str, str]


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def ratio(numerator: float, denominator: float) -> float | None:
    """``numerator / denominator``, or None when the denominator is zero (undefined)."""
    if denominator == 0:
        return None
    return numerator / denominator


def p95(values: Iterable[float]) -> float | None:
    """Nearest-rank 95th percentile: the value at rank ``ceil(0.95 n)`` of the sorted values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def pairs(n: int) -> int:
    """``n choose 2``: the number of unordered pairs inside a cluster of ``n`` members."""
    return n * (n - 1) // 2


def field_key(ref: FieldRef, form: str) -> FieldKey:
    """The hashable identity of a (field, form) end."""
    return (ref.system_id, ref.source_id, ref.field, form)


def field_only_key(ref: FieldRef) -> FieldOnlyKey:
    """The hashable identity of a field, whatever form a link used (entity purity)."""
    return (ref.system_id, ref.source_id, ref.field)


def link_pair(a: FieldRef, form_a: str, b: FieldRef, form_b: str) -> LinkPair:
    """The unordered pair of (field, form) ends of a link."""
    first, second = sorted((field_key(a, form_a), field_key(b, form_b)))
    return (first, second)


def field_pair(a: FieldRef, b: FieldRef) -> FieldPair:
    """The unordered pair of fields of a link, whatever forms it used (bridges)."""
    first, second = sorted((field_only_key(a), field_only_key(b)))
    return (first, second)


def _fields_of(pair: LinkPair) -> FieldPair:
    (sa, oa, fa, _form_a), (sb, ob, fb, _form_b) = pair
    first, second = sorted(((sa, oa, fa), (sb, ob, fb)))
    return (first, second)


@dataclass(frozen=True, slots=True)
class PrecisionRecall:
    """Set overlap between what the truth holds and what was predicted. ``matched_expected``
    is set when the two sides are keyed differently (bridges by field pair, other links by
    field and form) and the number of expected items reached can differ from the number of
    predictions that hit; it defaults to ``matched``."""

    matched: int
    predicted: int
    expected: int
    matched_expected: int | None = None

    @property
    def precision(self) -> float | None:
        """Matched over predicted; None when nothing was predicted."""
        return ratio(self.matched, self.predicted)

    @property
    def recall(self) -> float | None:
        """Expected items reached over expected; None when the truth holds nothing of the kind."""
        reached = self.matched if self.matched_expected is None else self.matched_expected
        return ratio(reached, self.expected)


def _overlap(expected: AbstractSet[object], predicted: AbstractSet[object]) -> PrecisionRecall:
    return PrecisionRecall(
        matched=len(expected & predicted), predicted=len(predicted), expected=len(expected)
    )


# ---------------------------------------------------------------------------------------------
# Links and entities
# ---------------------------------------------------------------------------------------------


def link_precision_recall(
    truth_links: Iterable[LinkTruth],
    predicted_links: Iterable[PredictedLink],
    link_types: frozenset[str],
) -> PrecisionRecall:
    """18.4 "Exact and bridge link precision / recall" and "Composite link precision / recall".

    A link is the unordered pair of its (field, form) ends, so a predicted link in the other
    direction still matches, and a composite link is compared on its primary pair only (the
    components are evidence, spec 9.5). A bridge is compared by its field pair without the
    forms: it is a property of two fields carried by one record (spec 9.4), every form of the
    field co-occurs the same way, and the engine stores one link per field pair, so a truth
    bridge named with ``digits.0`` and a predicted bridge in ``raw`` are the same link. A
    bridge on either side also matches a non-bridge link over the same field pair, and a
    non-bridge matches a non-bridge on field and form. ``link_types`` filters both sides: a
    truth bridge scored with ``{"exact", "bridge"}`` matches a predicted exact link with the
    same ends, because the table does not separate the two. The role is not compared.
    """
    expected_forms: set[LinkPair] = set()
    expected_fields: set[FieldPair] = set()
    for link in truth_links:
        if link.link_type not in link_types:
            continue
        if link.link_type == "bridge":
            expected_fields.add(field_pair(link.a, link.b))
        else:
            expected_forms.add(link_pair(link.a, link.form_a, link.b, link.form_b))
    predicted_forms: set[LinkPair] = set()
    predicted_fields: set[FieldPair] = set()
    for prediction in predicted_links:
        if prediction.link_type not in link_types:
            continue
        if prediction.link_type == "bridge":
            predicted_fields.add(field_pair(prediction.a, prediction.b))
        else:
            predicted_forms.add(
                link_pair(prediction.a, prediction.form_a, prediction.b, prediction.form_b)
            )
    predicted_form_fields = {_fields_of(pair) for pair in predicted_forms}
    expected_form_fields = {_fields_of(pair) for pair in expected_forms}
    reached = sum(
        1
        for pair in expected_forms
        if pair in predicted_forms or _fields_of(pair) in predicted_fields
    ) + sum(
        1
        for fields in expected_fields
        if fields in predicted_fields or fields in predicted_form_fields
    )
    hits = sum(
        1
        for pair in predicted_forms
        if pair in expected_forms or _fields_of(pair) in expected_fields
    ) + sum(
        1
        for fields in predicted_fields
        if fields in expected_fields or fields in expected_form_fields
    )
    return PrecisionRecall(
        matched=hits,
        predicted=len(predicted_forms) + len(predicted_fields),
        expected=len(expected_forms) + len(expected_fields),
        matched_expected=reached,
    )


@dataclass(frozen=True, slots=True)
class PurityResult:
    """Size-weighted purity: ``pure_members`` over ``members`` across predicted families."""

    families: int
    members: int
    pure_members: int

    @property
    def purity(self) -> float | None:
        """None when no predicted family has a member."""
        return ratio(self.pure_members, self.members)


def entity_purity(
    truth_entities: Iterable[EntityTruth], predicted_entities: Iterable[PredictedEntity]
) -> PurityResult:
    """18.4 "Entity family purity".

    The purity of one predicted family is the largest share of its distinct field members that
    belong to a single true entity; a member of no true entity counts in the size and in no
    share. Members are fields (system, source, field): spec 9.6 builds families over fields and
    the form is how a link matched, not another member, so ``order_ref`` reached as ``raw`` by
    one link and as ``digits.0`` by another is one member (changed 2026-10-18 with the M2
    engine; the truth's forms are the ones its links use). The metric is the size-weighted mean
    over predicted families: the sum of the largest counts over the sum of the sizes. Families
    without members are ignored; no family at all gives None.
    """
    owners: dict[FieldOnlyKey, set[str]] = {}
    for entity in truth_entities:
        for member in entity.fields:
            owners.setdefault(field_only_key(member.ref), set()).add(entity.entity_id)
    families = members = pure_members = 0
    for family in predicted_entities:
        distinct = {field_only_key(member.ref) for member in family.fields}
        if not distinct:
            continue
        families += 1
        members += len(distinct)
        per_entity: Counter[str] = Counter()
        for key in distinct:
            per_entity.update(owners.get(key, ()))
        pure_members += max(per_entity.values(), default=0)
    return PurityResult(families=families, members=members, pure_members=pure_members)


# ---------------------------------------------------------------------------------------------
# Transactions and batches
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PairwiseResult:
    """Pair counts over the keys with a true transaction."""

    keys: int
    true_pairs: int
    predicted_pairs: int
    matched_pairs: int

    @property
    def precision(self) -> float | None:
        """Matched over predicted pairs; None when nothing was predicted together."""
        return ratio(self.matched_pairs, self.predicted_pairs)

    @property
    def recall(self) -> float | None:
        """Matched over true pairs; None when no two keys share a true transaction."""
        return ratio(self.matched_pairs, self.true_pairs)

    @property
    def f1(self) -> float | None:
        """``2 tp / (2 tp + fp + fn)``, which is ``2 tp / (predicted + true)``."""
        return ratio(2 * self.matched_pairs, self.predicted_pairs + self.true_pairs)


def transaction_pairwise_f1(
    truth: Mapping[str, str | None], predicted: Mapping[str, str | None]
) -> PairwiseResult:
    """18.4 "Transaction pairwise F1".

    Over the keys whose true transaction is set, two keys form a pair when they share a
    transaction. Precision is the share of pairs predicted together that are truly together,
    recall the share of true pairs predicted together, F1 their harmonic mean. A key the engine
    did not place (absent from ``predicted`` or mapped to null) is its own singleton cluster and
    contributes no predicted pair. Keys without a true transaction (noise) are ignored on both
    sides. Counts come from the contingency table of (true, predicted) clusters: a cell of size
    n holds n choose 2 pairs, so no pair is ever enumerated.
    """
    rows: Counter[str] = Counter()
    columns: Counter[str] = Counter()
    cells: Counter[tuple[str, str]] = Counter()
    keys = 0
    for key, true_txn in truth.items():
        if true_txn is None:
            continue
        keys += 1
        rows[true_txn] += 1
        predicted_txn = predicted.get(key)
        if predicted_txn is None:
            continue
        columns[predicted_txn] += 1
        cells[(true_txn, predicted_txn)] += 1
    return PairwiseResult(
        keys=keys,
        true_pairs=sum(pairs(n) for n in rows.values()),
        predicted_pairs=sum(pairs(n) for n in columns.values()),
        matched_pairs=sum(pairs(n) for n in cells.values()),
    )


def batch_key_precision_recall(
    truth_batches: Iterable[BatchTruth], predicted_batches: Iterable[PredictedBatch]
) -> PrecisionRecall:
    """18.4 "Batch key detection (precision / recall)".

    Compared on the set of batch key values (file names, manifest ids), not on the transactions
    linked to each batch.
    """
    expected = {batch.key_value for batch in truth_batches}
    predicted = {batch.key_value for batch in predicted_batches}
    return _overlap(expected, predicted)


# ---------------------------------------------------------------------------------------------
# Faults and alerts
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FaultResult:
    """How one injected fault fared."""

    fault_id: str
    kind: str
    expected_alert: bool
    expected_cause_kind: str | None
    matched_alert_id: str | None
    time_to_detect_seconds: float | None

    @property
    def detected(self) -> bool:
        """An alert matched the fault (for a fault that must not alert: a false alert)."""
        return self.matched_alert_id is not None


@dataclass(frozen=True, slots=True)
class FaultDetectionResult:
    """Per-fault results plus the alerts that matched no expected fault."""

    faults: tuple[FaultResult, ...]
    false_alert_ids: tuple[str, ...]

    @property
    def expected(self) -> int:
        """Faults that expect an alert."""
        return sum(1 for fault in self.faults if fault.expected_alert)

    @property
    def detected(self) -> int:
        """Expected faults with a matching alert."""
        return sum(1 for fault in self.faults if fault.expected_alert and fault.detected)

    @property
    def recall(self) -> float | None:
        """18.4 "Injected fault detection recall": detected over expected; None without faults."""
        return ratio(self.detected, self.expected)

    @property
    def time_to_detect_p95_seconds(self) -> float | None:
        """18.4 "Time to detect after deadline": nearest-rank p95 over detected expected faults."""
        return p95(
            fault.time_to_detect_seconds
            for fault in self.faults
            if fault.expected_alert and fault.time_to_detect_seconds is not None
        )


def fault_detection(
    truth_faults: Iterable[FaultTruth],
    predicted_alerts: Iterable[PredictedAlert],
    grace: timedelta = DEFAULT_GRACE,
) -> FaultDetectionResult:
    """18.4 "Injected fault detection recall" and "Time to detect after deadline".

    A fault is detected by an alert that carries the fault's ``expected_alert_kind`` when the
    truth sets one, opened inside ``[start - grace, end + grace]`` (a fault without an end is
    given one day) and shares at least one affected transaction when the fault lists any. The
    earliest such alert is the match, ties broken by alert id; one alert may match several
    faults. ``time_to_detect`` is ``opened_at - start`` in seconds, floored at zero for an
    alert that opened inside the grace before the start. Recall counts faults with
    ``expected_alert`` True only. An alert that matches no expected fault is a false alert; that
    includes alerts raised for a fault that must not alert, such as the warehouse clock skew.
    """
    alerts = sorted(predicted_alerts, key=lambda alert: (alert.opened_at, alert.alert_id))
    results: list[FaultResult] = []
    matched_ids: set[str] = set()
    for fault in truth_faults:
        match = _first_match(fault, alerts, grace)
        seconds = None
        if match is not None:
            seconds = max(0.0, (match.opened_at - fault.start).total_seconds())
            if fault.expected_alert:
                matched_ids.add(match.alert_id)
        results.append(
            FaultResult(
                fault_id=fault.fault_id,
                kind=fault.kind.value,
                expected_alert=fault.expected_alert,
                expected_cause_kind=fault.expected_cause_kind,
                matched_alert_id=None if match is None else match.alert_id,
                time_to_detect_seconds=seconds,
            )
        )
    false_alert_ids = tuple(alert.alert_id for alert in alerts if alert.alert_id not in matched_ids)
    return FaultDetectionResult(faults=tuple(results), false_alert_ids=false_alert_ids)


def fault_window(fault: FaultTruth) -> tuple[datetime, datetime]:
    """``[start, end]`` of a fault; one without an end is given :data:`OPEN_ENDED_FAULT_SPAN`."""
    return fault.start, fault.end or fault.start + OPEN_ENDED_FAULT_SPAN


def _first_match(
    fault: FaultTruth, alerts: Iterable[PredictedAlert], grace: timedelta
) -> PredictedAlert | None:
    start, end = fault_window(fault)
    window_start = start - grace
    window_end = end + grace
    affected = set(fault.affected_txn_ids)
    for alert in alerts:
        if fault.expected_alert_kind is not None and (
            alert.expectation_kind != fault.expected_alert_kind
        ):
            continue
        if not window_start <= alert.opened_at <= window_end:
            continue
        if affected and affected.isdisjoint(alert.affected_txn_ids):
            continue
        return alert
    return None


def _overlaps(alert: PredictedAlert, start: datetime, end: datetime | None) -> bool:
    """Whether the alert's open interval meets ``[start, end]`` (both ends may be open)."""
    if end is not None and alert.opened_at > end:
        return False
    return alert.resolved_at is None or alert.resolved_at >= start


def visibility_gap_attribution(
    truth_faults: Iterable[FaultTruth],
    predicted_alerts: Iterable[PredictedAlert],
    detection: FaultDetectionResult,
) -> float | None:
    """18.4 "Visibility gap correctly attributed (not reported as stall)".

    The share of visibility-gap faults whose matched alert says ``is_visibility_gap`` and
    during which no alert with ``is_visibility_gap`` False overlaps the gap window while sharing
    one of its affected transactions: the stall the detector must not raise (spec 10.2). An
    undetected gap is not attributed. None when no visibility gap was injected.
    """
    alerts = list(predicted_alerts)
    by_id = {alert.alert_id: alert for alert in alerts}
    results = {result.fault_id: result for result in detection.faults}
    gaps = [fault for fault in truth_faults if fault.kind is FaultKind.VISIBILITY_GAP]
    correct = 0
    for gap in gaps:
        result = results.get(gap.fault_id)
        if result is None or result.matched_alert_id is None:
            continue
        if not by_id[result.matched_alert_id].is_visibility_gap:
            continue
        affected = set(gap.affected_txn_ids)
        blamed = any(
            not alert.is_visibility_gap
            and _overlaps(alert, gap.start, gap.end)
            and not affected.isdisjoint(alert.affected_txn_ids)
            for alert in alerts
        )
        if not blamed:
            correct += 1
    return ratio(correct, len(gaps))


def run_window(start_date: date, days: int) -> tuple[date, ...]:
    """The UTC calendar days of a run: ``days`` days from ``start_date`` (the manifest's window)."""
    return tuple(start_date + timedelta(days=offset) for offset in range(days))


def faulty_days(truth_faults: Iterable[FaultTruth], days: Iterable[date]) -> frozenset[date]:
    """The days among ``days`` that the window of a fault with ``expected_alert`` True touches.

    A calendar day is ``[00:00, 24:00)`` UTC and a fault window is closed (:func:`fault_window`),
    so a fault that ends exactly at midnight touches the day starting then. A fault that must
    not alert, such as the warehouse clock skew that spans the whole run, never makes a day
    faulty.
    """
    windows = [fault_window(fault) for fault in truth_faults if fault.expected_alert]
    faulty: set[date] = set()
    for day in days:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        if any(start < day_end and end >= day_start for start, end in windows):
            faulty.add(day)
    return frozenset(faulty)


@dataclass(frozen=True, slots=True)
class FalseAlertResult:
    """False alerts, and the fault-free days and flows they are rated against."""

    false_alerts_total: int
    false_alerts_on_fault_free_days: int
    fault_free_days: int
    run_days: int
    n_entities: int

    @property
    def per_flow_day(self) -> float | None:
        """False alerts on fault-free days over flows times fault-free days; None without either."""
        return ratio(self.false_alerts_on_fault_free_days, self.n_entities * self.fault_free_days)


def false_alerts_per_flow_day(
    truth_faults: Iterable[FaultTruth],
    predicted_alerts: Iterable[PredictedAlert],
    detection: FaultDetectionResult,
    *,
    start_date: date,
    days: int,
    n_entities: int,
) -> FalseAlertResult:
    """18.4 "False alerts on fault-free days" (at most 1 per flow per day).

    A false alert is one that matched no expected fault in ``detection``
    (:func:`fault_detection`; an alert raised for a fault that must not alert, such as the
    warehouse clock skew, is false). The run is ``days`` UTC calendar days from ``start_date``,
    the manifest's window; a day is fault-free unless the window of a fault with
    ``expected_alert`` True touches it (:func:`faulty_days`). The rate is the false alerts whose
    ``opened_at`` falls on a fault-free day over flows (entities) times fault-free days; None
    when the run has no fault-free day or the truth no entity. A false alert on a faulty day, or
    outside the run, counts in ``false_alerts_total`` only.
    """
    window = run_window(start_date, days)
    fault_free = frozenset(window) - faulty_days(truth_faults, window)
    false_ids = frozenset(detection.false_alert_ids)
    on_fault_free_days = sum(
        1
        for alert in predicted_alerts
        if alert.alert_id in false_ids and alert.opened_at.astimezone(UTC).date() in fault_free
    )
    return FalseAlertResult(
        false_alerts_total=len(false_ids),
        false_alerts_on_fault_free_days=on_fault_free_days,
        fault_free_days=len(fault_free),
        run_days=len(window),
        n_entities=n_entities,
    )


@dataclass(frozen=True, slots=True)
class CauseResult:
    """Top-1 cause accuracy over detected faults that have an expected cause kind."""

    considered: int
    correct: int

    @property
    def accuracy(self) -> float | None:
        """Correct over considered; None when no detected fault has an expected cause."""
        return ratio(self.correct, self.considered)


def likely_cause_top1(
    fault_results: Iterable[FaultResult], alerts: Iterable[PredictedAlert]
) -> CauseResult:
    """18.4 "Likely cause top-1 accuracy on injected faults".

    Over detected expected faults with an ``expected_cause_kind`` (spec 10.4), the share whose
    matched alert ranks that kind first in ``likely_causes``.
    """
    by_id = {alert.alert_id: alert for alert in alerts}
    considered = correct = 0
    for result in fault_results:
        if not result.expected_alert or result.expected_cause_kind is None:
            continue
        if result.matched_alert_id is None:
            continue
        considered += 1
        causes = by_id[result.matched_alert_id].likely_causes
        if causes and causes[0] == result.expected_cause_kind:
            correct += 1
    return CauseResult(considered=considered, correct=correct)


# ---------------------------------------------------------------------------------------------
# Manual hops
# ---------------------------------------------------------------------------------------------


def predicted_manual(hop: PredictedManualHop, threshold: float = DEFAULT_MANUAL_THRESHOLD) -> bool:
    """Spec 11.1: the reviewer's decision when there is one, else the "Likely manual" threshold.

    ``confirmed`` True is manual and False is not, whatever the score; with no decision
    (``confirmed`` None) the hop is manual when its score reaches ``threshold``.
    """
    if hop.confirmed is not None:
        return hop.confirmed
    return hop.score >= threshold


def manual_hop_precision_recall(
    truth_hops: Iterable[ManualHopTruth],
    predicted_hops: Iterable[PredictedManualHop],
    threshold: float = DEFAULT_MANUAL_THRESHOLD,
) -> PrecisionRecall:
    """18.4 "Manual hop precision / recall".

    A true hop is manual when ``manual`` is True. A predicted hop counts as manual when a reviewer
    confirmed it, or when no reviewer has decided (``confirmed`` None) and its score reaches
    ``threshold`` (0.7, the "Likely manual" badge of spec 11.1). The reviewer's decision wins: a
    dismissal (``confirmed`` False) never counts, whatever the score (:func:`predicted_manual`).
    Compared on ``(from_node, to_node)``.
    """
    expected: set[HopPair] = {(hop.from_node, hop.to_node) for hop in truth_hops if hop.manual}
    predicted: set[HopPair] = {
        (hop.from_node, hop.to_node) for hop in predicted_hops if predicted_manual(hop, threshold)
    }
    return _overlap(expected, predicted)
