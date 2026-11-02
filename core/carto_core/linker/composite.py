"""Composite links (spec 9.5; plan M2 package D): the temporal-adjacency trigger, the matcher
and the ``composite_rules`` repository.

:class:`CompositeMatcher.run` takes the scored links of a pass and the window and, per ordered
system pair ``(A earlier, B later)``:

1. **Trigger.** The pair is a candidate when no exact or bridge link between the two systems
   on identifier forms scores above ``composite_exact_ceiling`` (a link between two ``date``,
   ``amount`` or ``phonetic`` ends is composite evidence, not an exact link), or the best one
   reaches less than ``composite_coverage_floor`` of the later end's *events* (its ``n_match``
   over the events of that end's template; scenario C: the HRIS carries the candidate id in 60%
   of its rows, so token containment would read 100%), *and* the two systems' volumes of
   key-carrying events (:func:`key_event_series`: events with a ``date`` or ``amount`` token,
   each once; log noise lines carry none) summed over a trailing day (:func:`rolling_day`)
   cross-correlate at or above ``composite_adjacency_min_correlation`` at some lag from zero to
   ``composite_window_business_days x 24`` hours (:func:`cross_correlation`: Pearson, B shifted
   back by the lag). Both orderings of an adjacent pair are tried; when both yield a rule, one
   stands per system pair (:func:`one_rule_per_system_pair`).
2. **Blocking** (:mod:`carto_core.linker.composite_blocking`) and **scoring**
   (:mod:`carto_core.linker.composite_em`): components; ``u`` from random pairs; ``m``, the
   prior and the within-block agreement rates of non-matches from EM over
   ``composite_em_iterations`` rounds started at ``m = 0.9``, the sampled ``u`` and the
   one-to-one prior; pair weights from ``m`` and the sampled ``u`` (what the rule stores and
   M3 re-applies); the threshold on those weights at which the posterior of the fitted
   mixture reaches ``composite_target_precision``.
3. **Output.** One :class:`CompositeRule` per system pair with at least :data:`MIN_COMPONENTS`
   scoring components and at least ``min_match`` matched pairs (the spec 9.3 hard filter
   applies to composite links too), persisted through a
   :class:`CompositeRuleRepository` under the id of the link, and one
   :class:`~carto_core.linker.review.ScoredLink` between the ends of the strongest component
   (``link_type`` composite, role transaction, direction A to B, score = estimated precision x
   share of A events matched). The pass hands the links to
   :meth:`~carto_core.linker.review.ReviewService.propose`, which never auto-accepts a composite
   link (spec 9.5). The matched pairs are returned in memory for M3's assembly; M2 persists
   their counts only.

Nothing stored or logged here is a token or a value: field refs, forms, kinds, weights, counts,
lags and system ids only (spec 2.3 invariant 2).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import numpy as np
import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_common.logging import get_logger
from carto_core.db.postgres import ping_engine
from carto_core.linker.composite_blocking import (
    AMOUNT_FORM,
    COMPOSITE_FORMS,
    DATE_FORM,
    PHONETIC_FORMS,
    SECONDS_PER_DAY,
    Side,
    block_pairs,
    load_side,
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
    one_to_one_prior,
    pair_weights,
    posteriors,
    side_has,
    split_components,
)
from carto_core.linker.model import (
    CompositeComponent,
    CompositeRule,
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
)
from carto_core.linker.repository import RepositoryError
from carto_core.linker.review import ScoredLink, link_id_for
from carto_core.linker.sentence import evidence_sentence
from carto_core.store.memory import quantile_exact_low
from carto_core.store.model import FieldForm, Window

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from sqlalchemy import Engine

    from carto_core.settings import LinkerSettings
    from carto_core.store.model import EventStore, HourlyVolume, IdentifierRow

__all__ = [
    "MAX_COMPONENTS",
    "MIN_COMPONENTS",
    "OUTCOME_STAGES",
    "WEAK_FORMS",
    "Adjacency",
    "CompositeMatch",
    "CompositeMatcher",
    "CompositeOutcome",
    "CompositeReport",
    "CompositeRuleRepository",
    "InMemoryCompositeRuleRepository",
    "PostgresCompositeRuleRepository",
    "StoredCompositeRule",
    "SystemPairTrigger",
    "adjacencies",
    "component_views",
    "cross_correlation",
    "event_coverage",
    "exact_field_pairs",
    "hourly_series",
    "identifier_links",
    "key_event_series",
    "one_rule_per_system_pair",
    "primary_component",
    "rolling_day",
    "trigger_pairs",
]

log = get_logger(component="linker.composite")

MAX_COMPONENTS: Final = 32
"""The most components a rule stores: ``EvidenceView.components`` (carto_schema.api) holds at
most this many, strongest first."""
MIN_COMPONENTS: Final = 2
"""A composite rule combines weak field pairs (spec 9.5 "components"); one pair alone is the
exact linker's business (an amount equal to an amount is not a composite match), and a
one-component mixture cannot be told apart from its prior. Definitional, not a tuning knob."""
HOURS_PER_DAY: Final = 24
SECONDS_PER_HOUR: Final = 3_600
MIN_SERIES_POINTS: Final = 2
"""A Pearson correlation needs at least two aligned points (and some variance) to exist."""


# -- temporal adjacency (trigger) ----------------------------------------------------------------


def hourly_series(
    volumes: Sequence[HourlyVolume], window: Window
) -> tuple[dict[str, NDArray[np.float64]], int]:
    """Dense per-system hourly counts over the window (zeros where a system saw nothing) and
    the number of hours in the series."""
    start = window.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hours = max(1, math.ceil((window.end - start) / timedelta(hours=1)))
    series: dict[str, NDArray[np.float64]] = {}
    for volume in volumes:
        index = int((volume.hour.astimezone(UTC) - start) / timedelta(hours=1))
        if index < 0 or index >= hours:
            continue
        if volume.system_id not in series:
            series[volume.system_id] = np.zeros(hours, dtype=np.float64)
        series[volume.system_id][index] += float(volume.events)
    return series, hours


def key_event_series(
    rows: Iterable[IdentifierRow], window: Window
) -> tuple[dict[str, NDArray[np.float64]], int]:
    """Dense per-system hourly counts of the events that carry a composite key (a ``date`` or
    ``amount`` token), each event once, and the number of hours. Log noise lines without such a
    key cannot be blocked, so they do not shape the adjacency signal either."""
    start = window.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hours = max(1, math.ceil((window.end - start) / timedelta(hours=1)))
    series: dict[str, NDArray[np.float64]] = {}
    seen: set[str] = set()
    for row in rows:
        if row.event_id in seen:
            continue
        seen.add(row.event_id)
        index = int((row.observed_at.astimezone(UTC) - start) / timedelta(hours=1))
        if index < 0 or index >= hours:
            continue
        if row.system_id not in series:
            series[row.system_id] = np.zeros(hours, dtype=np.float64)
        series[row.system_id][index] += 1.0
    return series, hours


def rolling_day(series: NDArray[np.float64]) -> NDArray[np.float64]:
    """The hourly series summed over the trailing 24 hours. A clerk keys a morning's intake
    over the next business day or two, so the hourly shapes of the two systems differ while
    their daily volumes move together; the trailing-day sum is what the correlation compares."""
    if series.shape[0] == 0:
        return series
    summed: NDArray[np.float64] = np.convolve(series, np.ones(HOURS_PER_DAY), mode="full")
    return summed[: series.shape[0]]


def cross_correlation(
    a: NDArray[np.float64], b: NDArray[np.float64], max_lag: int
) -> tuple[int, float]:
    """``(lag, correlation)`` of the lag in ``0..max_lag`` hours at which B's series, shifted
    back by the lag, best correlates (Pearson) with A's. A lag leaving fewer than
    :data:`MIN_SERIES_POINTS` aligned hours or a constant series correlates at zero."""
    best_lag, best = 0, 0.0
    n = min(a.shape[0], b.shape[0])
    for lag in range(max_lag + 1):
        if n - lag < MIN_SERIES_POINTS:
            break
        x = a[: n - lag]
        y = b[lag:n]
        if np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
            continue
        value = float(np.corrcoef(x, y)[0, 1])
        if math.isnan(value):
            continue
        if value > best:
            best_lag, best = lag, value
    return best_lag, best


@dataclass(frozen=True, slots=True)
class Adjacency:
    """The peak hourly cross-correlation of an ordered system pair."""

    system_a: str
    system_b: str
    lag_hours: int
    correlation: float


def adjacencies(
    series: Mapping[str, NDArray[np.float64]], *, max_lag_hours: int
) -> list[Adjacency]:
    """Every ordered pair of systems in ``series`` (one aligned hourly series per system, as
    :func:`key_event_series` then :func:`rolling_day` produce) and its peak correlation."""
    systems = sorted(series)
    result: list[Adjacency] = []
    for system_a in systems:
        for system_b in systems:
            if system_a == system_b:
                continue
            lag, correlation = cross_correlation(series[system_a], series[system_b], max_lag_hours)
            result.append(Adjacency(system_a, system_b, lag, correlation))
    return result


@dataclass(frozen=True, slots=True)
class SystemPairTrigger:
    """Why an ordered system pair is (or is not) a composite candidate."""

    system_a: str
    system_b: str
    best_exact_score: float | None
    """The best exact or bridge link between the two systems in this pass; None when none."""
    later_end_coverage: float | None
    """The share of the later system's events that link reaches: its ``n_match`` over the
    events of the later end's template in the window (one token per event in a row-shaped
    source; capped at one), or its token containment when the template count is unknown.
    Token containment alone would read 100% for a reference that is present in 60% of rows."""
    lag_hours: int
    correlation: float
    triggered: bool
    reason: str
    """``no_exact_link``, ``low_coverage``, ``exact_link`` or ``no_adjacency``."""
    later_end_events: int | None = None
    """Events of the later end's template in the window, when known."""


def _system_of(end: FieldForm) -> str:
    return end.system_id


WEAK_FORMS: Final[frozenset[str]] = frozenset({DATE_FORM, AMOUNT_FORM, *PHONETIC_FORMS})
"""Forms that never identify a record on their own: a link between two of them is composite
evidence, not an exact link, whatever the candidate step called it."""


def identifier_links(links: Sequence[ScoredLink]) -> list[ScoredLink]:
    """The exact and bridge links between two systems whose ends are identifier forms."""
    return [
        link
        for link in links
        if link.link_type in (LinkType.EXACT, LinkType.BRIDGE)
        and link.a.form not in WEAK_FORMS
        and link.b.form not in WEAK_FORMS
        and _system_of(link.a) != _system_of(link.b)
    ]


def exact_field_pairs(links: Sequence[ScoredLink], ceiling: float) -> frozenset[frozenset[str]]:
    """The unordered field pairs (any forms) of the identifier links scoring above ``ceiling``;
    :func:`~carto_core.linker.composite_em.split_components` turns them into labels."""
    return frozenset(
        frozenset({link.a.field_ref, link.b.field_ref})
        for link in identifier_links(links)
        if link.score > ceiling
    )


def _best_links(links: Sequence[ScoredLink]) -> dict[frozenset[str], ScoredLink]:
    best: dict[frozenset[str], ScoredLink] = {}
    for link in identifier_links(links):
        systems = frozenset({_system_of(link.a), _system_of(link.b)})
        current = best.get(systems)
        if current is None or link.score > current.score:
            best[systems] = link
    return best


def _template_of(end: FieldForm) -> str:
    return end.field_ref.split("/", 2)[1] if end.field_ref.count("/") >= 2 else ""


def event_coverage(
    link: ScoredLink, later_system: str, template_events: Mapping[tuple[str, str], int] | None
) -> tuple[float, int | None]:
    """``(coverage, events)`` of the later end (see :class:`SystemPairTrigger`)."""
    later_end = link.b if _system_of(link.b) == later_system else link.a
    containment = link.features.c_ba if later_end is link.b else link.features.c_ab
    if template_events is None:
        return containment, None
    events = template_events.get((later_system, _template_of(later_end)))
    if not events:
        return containment, events
    return min(1.0, link.features.n_match / events), events


def trigger_pairs(
    links: Sequence[ScoredLink],
    adjacency: Sequence[Adjacency],
    settings: LinkerSettings,
    *,
    template_events: Mapping[tuple[str, str], int] | None = None,
) -> list[SystemPairTrigger]:
    """Spec 9.5 trigger per ordered system pair (module docstring step 1). ``template_events``
    maps ``(system_id, template_id)`` to its events in the window (from
    :meth:`~carto_core.store.model.EventStore.template_counts`) for the coverage test."""
    best = _best_links(links)
    result: list[SystemPairTrigger] = []
    for item in adjacency:
        link = best.get(frozenset({item.system_a, item.system_b}))
        score = link.score if link is not None else None
        coverage: float | None = None
        events: int | None = None
        if link is not None:
            coverage, events = event_coverage(link, item.system_b, template_events)
        if link is None or score is None or score <= settings.composite_exact_ceiling:
            weak, reason = True, "no_exact_link"
        elif coverage is not None and coverage < settings.composite_coverage_floor:
            weak, reason = True, "low_coverage"
        else:
            weak, reason = False, "exact_link"
        if weak and item.correlation < settings.composite_adjacency_min_correlation:
            weak, reason = False, "no_adjacency"
        result.append(
            SystemPairTrigger(
                system_a=item.system_a,
                system_b=item.system_b,
                best_exact_score=score,
                later_end_coverage=coverage,
                lag_hours=item.lag_hours,
                correlation=item.correlation,
                triggered=weak,
                reason=reason,
                later_end_events=events,
            )
        )
    return result


# -- rules repository ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCompositeRule:
    """One ``composite_rules`` row (spec 7.3), keyed by the id of its link."""

    id: str
    key_link_id: str
    rule: CompositeRule
    updated_at: datetime | None = None


@runtime_checkable
class CompositeRuleRepository(Protocol):
    def get(self, tenant_id: str, link_id: str) -> StoredCompositeRule | None: ...

    def list_rules(self, tenant_id: str) -> list[StoredCompositeRule]:
        """Every rule of the tenant, ordered by id."""
        ...

    def upsert(
        self, tenant_id: str, link_id: str, rule: CompositeRule, now: datetime
    ) -> StoredCompositeRule:
        """Insert or overwrite the rule of a link; returns the stored row."""
        ...

    def ping(self) -> bool: ...


def components_json(rule: CompositeRule) -> list[dict[str, Any]]:
    return [
        {
            "a": {"field_ref": component.a.field_ref, "form": component.a.form},
            "b": {"field_ref": component.b.field_ref, "form": component.b.form},
            "kind": component.kind,
            "m": component.m,
            "u": component.u,
        }
        for component in rule.components
    ]


def components_from_json(document: object) -> tuple[CompositeComponent, ...]:
    if not isinstance(document, list):
        return ()
    components: list[CompositeComponent] = []
    for item in document:
        if not isinstance(item, Mapping):
            continue
        a = item.get("a")
        b = item.get("b")
        if not isinstance(a, Mapping) or not isinstance(b, Mapping):
            continue
        components.append(
            CompositeComponent(
                a=FieldForm(str(a.get("field_ref", "")), str(a.get("form", ""))),
                b=FieldForm(str(b.get("field_ref", "")), str(b.get("form", ""))),
                kind=str(item.get("kind", "")),
                m=float(item.get("m", 0.0)),
                u=float(item.get("u", 0.0)),
            )
        )
    return tuple(components)


class InMemoryCompositeRuleRepository:
    """Reference semantics and test double. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: dict[tuple[str, str], StoredCompositeRule] = {}

    def get(self, tenant_id: str, link_id: str) -> StoredCompositeRule | None:
        with self._lock:
            return self._rules.get((tenant_id, link_id))

    def list_rules(self, tenant_id: str) -> list[StoredCompositeRule]:
        with self._lock:
            rows = [rule for (tenant, _id), rule in self._rules.items() if tenant == tenant_id]
        return sorted(rows, key=lambda row: row.id)

    def upsert(
        self, tenant_id: str, link_id: str, rule: CompositeRule, now: datetime
    ) -> StoredCompositeRule:
        stored = StoredCompositeRule(id=link_id, key_link_id=link_id, rule=rule, updated_at=now)
        with self._lock:
            self._rules[(tenant_id, link_id)] = stored
        return stored

    def ping(self) -> bool:
        return True


_SELECT_RULE = sqlalchemy.text(
    "SELECT id, key_link_id, components_json, window_seconds, threshold, estimated_precision, "
    "updated_at FROM composite_rules WHERE tenant_id = :tenant_id AND id = :id"
)
_SELECT_RULES = sqlalchemy.text(
    "SELECT id, key_link_id, components_json, window_seconds, threshold, estimated_precision, "
    "updated_at FROM composite_rules WHERE tenant_id = :tenant_id ORDER BY id"
)
_UPSERT_RULE = sqlalchemy.text(
    "INSERT INTO composite_rules (tenant_id, id, key_link_id, components_json, window_seconds, "
    "threshold, estimated_precision, created_at, updated_at) VALUES (:tenant_id, :id, "
    ":key_link_id, CAST(:components_json AS JSONB), :window_seconds, :threshold, "
    ":estimated_precision, :now, :now) ON CONFLICT (tenant_id, id) DO UPDATE "
    "SET key_link_id = EXCLUDED.key_link_id, components_json = EXCLUDED.components_json, "
    "window_seconds = EXCLUDED.window_seconds, threshold = EXCLUDED.threshold, "
    "estimated_precision = EXCLUDED.estimated_precision, updated_at = EXCLUDED.updated_at "
    "RETURNING id, key_link_id, components_json, window_seconds, threshold, estimated_precision, "
    "updated_at"
)


def _rule_from_row(row: Any) -> StoredCompositeRule:
    document = row.components_json
    if isinstance(document, str | bytes):
        document = json.loads(document)
    return StoredCompositeRule(
        id=row.id,
        key_link_id=row.key_link_id,
        rule=CompositeRule(
            components=components_from_json(document),
            window_seconds=int(row.window_seconds),
            threshold=float(row.threshold),
            estimated_precision=(
                None if row.estimated_precision is None else float(row.estimated_precision)
            ),
        ),
        updated_at=row.updated_at,
    )


class PostgresCompositeRuleRepository:
    """``composite_rules`` (migration 0004)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, tenant_id: str, link_id: str) -> StoredCompositeRule | None:
        try:
            with self._engine.connect() as connection:
                first = connection.execute(
                    _SELECT_RULE, {"tenant_id": tenant_id, "id": link_id}
                ).first()
        except SQLAlchemyError as exc:
            raise RepositoryError(f"rule read failed: {type(exc).__name__}") from exc
        return None if first is None else _rule_from_row(first)

    def list_rules(self, tenant_id: str) -> list[StoredCompositeRule]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_RULES, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise RepositoryError(f"rule list failed: {type(exc).__name__}") from exc
        return [_rule_from_row(row) for row in rows]

    def upsert(
        self, tenant_id: str, link_id: str, rule: CompositeRule, now: datetime
    ) -> StoredCompositeRule:
        parameters = {
            "tenant_id": tenant_id,
            "id": link_id,
            "key_link_id": link_id,
            "components_json": json.dumps(components_json(rule), sort_keys=True),
            "window_seconds": rule.window_seconds,
            "threshold": rule.threshold,
            "estimated_precision": rule.estimated_precision,
            "now": now,
        }
        try:
            with self._engine.begin() as connection:
                first = connection.execute(_UPSERT_RULE, parameters).first()
        except SQLAlchemyError as exc:
            raise RepositoryError(f"rule upsert failed: {type(exc).__name__}") from exc
        if first is None:
            msg = "rule upsert returned no row"
            raise RepositoryError(msg)
        return _rule_from_row(first)

    def ping(self) -> bool:
        return ping_engine(self._engine)


# -- the matcher ---------------------------------------------------------------------------------

STAGE_NO_PAIRS: Final = "no_blocked_pairs"
STAGE_FEW_COMPONENTS: Final = "few_components"
STAGE_NO_THRESHOLD: Final = "no_threshold"
STAGE_BELOW_MIN_MATCH: Final = "below_min_match"
STAGE_OTHER_ORDERING: Final = "other_ordering"
STAGE_RULE: Final = "rule"
OUTCOME_STAGES: Final[tuple[str, ...]] = (
    STAGE_NO_PAIRS,
    STAGE_FEW_COMPONENTS,
    STAGE_NO_THRESHOLD,
    STAGE_BELOW_MIN_MATCH,
    STAGE_OTHER_ORDERING,
    STAGE_RULE,
)
"""Where a triggered ordered pair stopped: no blocked pairs, fewer than :data:`MIN_COMPONENTS`
scoring components, no threshold reaching ``composite_target_precision``, fewer than
``min_match`` matched pairs, the other ordering of the system pair kept the rule
(:func:`one_rule_per_system_pair`), or a rule."""


@dataclass(frozen=True, slots=True)
class CompositeMatch:
    """One matched event pair, kept in memory for M3 (never persisted in M2)."""

    event_id_a: str
    event_id_b: str
    weight: float


@dataclass(frozen=True, slots=True)
class CompositeOutcome:
    """What one triggered system pair produced."""

    trigger: SystemPairTrigger
    events_a: int
    events_b: int
    blocked_pairs: int
    capped: bool
    rule: CompositeRule | None
    link: ScoredLink | None
    matches: tuple[CompositeMatch, ...] = ()
    stage: str = STAGE_RULE
    """Where the pair stopped (:data:`OUTCOME_STAGES`); counts and parameters only below."""
    components: int = 0
    """Scoring components found over the blocked pairs (label components excluded)."""
    labelled_pairs: int = 0
    """Blocked pairs an exact link above the ceiling labels as a match or a non-match."""
    em_prior: float | None = None
    em_iterations: int = 0
    em_converged: bool = False
    threshold: float | None = None
    estimated_precision: float | None = None


@dataclass(frozen=True, slots=True)
class CompositeReport:
    """Counts of one composite run plus the outcomes, for the pass and the eval report."""

    tenant_id: str
    window: Window
    pairs_considered: int
    pairs_triggered: int
    rules: int
    matched_pairs: int
    capped: int
    outcomes: tuple[CompositeOutcome, ...] = ()
    triggers: tuple[SystemPairTrigger, ...] = ()
    """Every ordered system pair considered, triggered or not, with the reason."""

    @property
    def links(self) -> tuple[ScoredLink, ...]:
        return tuple(o.link for o in self.outcomes if o.link is not None)


def _end_view(end: FieldForm) -> dict[str, Any]:
    system_id, _sep, rest = end.field_ref.partition("/")
    template_id, _sep, path = rest.partition("/")
    return {
        "field_id": end.field_ref,
        "system_id": system_id,
        "template_id": template_id,
        "path": path or end.field_ref,
        "form": end.form,
    }


def component_views(rule: CompositeRule) -> list[dict[str, Any]]:
    """``EvidenceView.components`` (carto_schema.api) as JSON-safe dicts, strongest first, with
    exactly the contract's keys so the API can pass them through; the kinds travel beside them
    in the link's ``extra`` and in the ``composite_rules`` row."""
    ordered = sorted(rule.components, key=lambda c: -c.weight)[:MAX_COMPONENTS]
    return [
        {
            "a": _end_view(component.a),
            "b": _end_view(component.b),
            "weight": component.weight,
            "m": component.m,
            "u": component.u,
        }
        for component in ordered
    ]


def primary_component(
    specs: Sequence[ComponentSpec],
    components: Sequence[CompositeComponent],
    matrix: AgreementMatrix,
    selected: NDArray[np.int64],
) -> tuple[ComponentSpec, CompositeComponent]:
    """The pair the link is named after: the component that contributes the most evidence
    across the matched pairs, its agreement weight times the share of matched pairs that carry
    it. A rare id fragment present in half the rows loses to a name present in all of them;
    ties go to the first in :class:`ComponentSpec` order."""
    best: tuple[float, int] | None = None
    for index, component in enumerate(components):
        share = float(matrix.present[selected, index].mean()) if selected.shape[0] else 0.0
        score = component.weight * share
        if best is None or score > best[0]:
            best = (score, index)
    if best is None:  # specs is never empty here (checked by the caller)
        msg = "no component to name the link after"
        raise ValueError(msg)
    return specs[best[1]], components[best[1]]


def one_rule_per_system_pair(outcomes: Sequence[CompositeOutcome]) -> list[CompositeOutcome]:
    """Both orderings of an adjacent system pair are tried, because the peak correlation of
    two rolling-day series cannot tell which system leads when both exceed the floor (a clerk
    keys payments into purchase orders within hours: 0.93 one way, 0.95 the other). The wrong
    ordering blocks few pairs and usually dies at the component or match floors; when both
    orderings produce a rule, the one with more matched pairs (then the higher score) stands
    and the other is reported without a rule, so the queue sees one link per system pair."""
    best: dict[frozenset[str], int] = {}
    for index, outcome in enumerate(outcomes):
        if outcome.rule is None or outcome.link is None:
            continue
        key = frozenset({outcome.trigger.system_a, outcome.trigger.system_b})
        current = best.get(key)
        if current is None:
            best[key] = index
            continue
        rival = outcomes[current]
        if rival.link is None:  # only indexes of outcomes with a link are kept
            msg = "a kept outcome has no link"
            raise RuntimeError(msg)
        if (len(outcome.matches), outcome.link.score) > (len(rival.matches), rival.link.score):
            best[key] = index
    kept = set(best.values())
    result: list[CompositeOutcome] = []
    for index, outcome in enumerate(outcomes):
        if outcome.rule is not None and index not in kept:
            log.info(
                "composite reverse ordering dropped",
                system_a=outcome.trigger.system_a,
                system_b=outcome.trigger.system_b,
                matched=len(outcome.matches),
            )
            result.append(
                replace(outcome, rule=None, link=None, matches=(), stage=STAGE_OTHER_ORDERING)
            )
        else:
            result.append(outcome)
    return result


def _seed(tenant_id: str, system_a: str, system_b: str) -> int:
    digest = hashlib.sha256("\n".join((tenant_id, system_a, system_b)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class CompositeMatcher:
    """See the module docstring. :meth:`hook` is the ``composite_hook`` of
    :class:`~carto_core.linker.run.LinkPass`; :meth:`run` is the same step with its report."""

    def __init__(
        self,
        *,
        store: EventStore,
        settings: LinkerSettings,
        rules: CompositeRuleRepository,
        clock: Callable[[], datetime] | None = None,
        system_names: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._rules = rules
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._system_names = dict(system_names) if system_names else {}
        self._last_report: CompositeReport | None = None

    @property
    def last_report(self) -> CompositeReport | None:
        return self._last_report

    def hook(
        self,
        *,
        tenant_id: str,
        window: Window,
        links: Sequence[ScoredLink],
        profiles: Mapping[FieldForm, Any] | None = None,
    ) -> tuple[ScoredLink, ...]:
        """The pass's ``composite_hook``: returns the composite links for it to propose.
        ``profiles`` (the pass's field profiles, when it passes them) is accepted for forward
        compatibility; coverage is read from the store's template counts, since a profile
        counts the rows that carry a field and the question is the rows that do not."""
        del profiles
        return self.run(tenant_id, window, links).links

    def run(self, tenant_id: str, window: Window, links: Sequence[ScoredLink]) -> CompositeReport:
        settings = self._settings
        if not settings.composite_enabled:
            report = CompositeReport(tenant_id, window, 0, 0, 0, 0, 0)
            self._last_report = report
            return report
        max_lag = settings.composite_window_business_days * HOURS_PER_DAY
        rows = self._store.identifier_rows(
            tenant_id, window, forms=frozenset({DATE_FORM, AMOUNT_FORM})
        )
        series, _hours = key_event_series(rows, window)
        smoothed = {system: rolling_day(values) for system, values in series.items()}
        adjacency = adjacencies(smoothed, max_lag_hours=max_lag)
        template_events: dict[tuple[str, str], int] = {}
        for count in self._store.template_counts(tenant_id, window):
            key = (count.system_id, count.template_id)
            template_events[key] = template_events.get(key, 0) + count.events
        triggers = trigger_pairs(links, adjacency, settings, template_events=template_events)
        exact_pairs = exact_field_pairs(links, settings.composite_exact_ceiling)
        now = self._clock()
        outcomes: list[CompositeOutcome] = []
        sides: dict[str, Side] = {}
        for trigger in triggers:
            if not trigger.triggered:
                continue
            side_a = self._side(sides, tenant_id, trigger.system_a, window)
            side_b = self._side(sides, tenant_id, trigger.system_b, window)
            outcomes.append(self._match(tenant_id, window, trigger, side_a, side_b, exact_pairs))
        outcomes = one_rule_per_system_pair(outcomes)
        for outcome in outcomes:
            log.info(
                "composite pair outcome",
                tenant_id=tenant_id,
                system_a=outcome.trigger.system_a,
                system_b=outcome.trigger.system_b,
                stage=outcome.stage,
                events_a=outcome.events_a,
                events_b=outcome.events_b,
                blocked_pairs=outcome.blocked_pairs,
                capped=outcome.capped,
                components=outcome.components,
                labelled_pairs=outcome.labelled_pairs,
                em_prior=outcome.em_prior,
                em_iterations=outcome.em_iterations,
                em_converged=outcome.em_converged,
                threshold=outcome.threshold,
                estimated_precision=outcome.estimated_precision,
                matched=len(outcome.matches),
            )
            if outcome.rule is not None and outcome.link is not None:
                link_id = link_id_for(tenant_id, outcome.link.a, outcome.link.b)
                self._rules.upsert(tenant_id, link_id, outcome.rule, now)
        report = CompositeReport(
            tenant_id=tenant_id,
            window=window,
            pairs_considered=len(triggers),
            pairs_triggered=sum(1 for t in triggers if t.triggered),
            rules=sum(1 for o in outcomes if o.rule is not None),
            matched_pairs=sum(len(o.matches) for o in outcomes),
            capped=sum(1 for o in outcomes if o.capped),
            outcomes=tuple(outcomes),
            triggers=tuple(triggers),
        )
        log.info(
            "composite pass done",
            tenant_id=tenant_id,
            pairs_considered=report.pairs_considered,
            pairs_triggered=report.pairs_triggered,
            rules=report.rules,
            matched_pairs=report.matched_pairs,
            capped=report.capped,
        )
        self._last_report = report
        return report

    def _side(self, cache: dict[str, Side], tenant_id: str, system_id: str, window: Window) -> Side:
        side = cache.get(system_id)
        if side is None:
            side = load_side(
                system_id,
                self._store.events_with_keys(tenant_id, system_id, window, forms=COMPOSITE_FORMS),
            )
            cache[system_id] = side
        return side

    def _match(
        self,
        tenant_id: str,
        window: Window,
        trigger: SystemPairTrigger,
        side_a: Side,
        side_b: Side,
        exact_pairs: Collection[frozenset[str]] = frozenset(),
    ) -> CompositeOutcome:
        settings = self._settings
        blocked = block_pairs(
            side_a,
            side_b,
            window_business_days=settings.composite_window_business_days,
            max_pairs=settings.composite_max_pairs,
        )
        if blocked.capped:
            log.warning(
                "composite blocking capped",
                tenant_id=tenant_id,
                system_a=trigger.system_a,
                system_b=trigger.system_b,
                pairs=len(blocked.pairs),
            )
        empty = CompositeOutcome(
            trigger,
            len(side_a),
            len(side_b),
            len(blocked.pairs),
            blocked.capped,
            None,
            None,
            stage=STAGE_NO_PAIRS,
        )
        if not blocked.pairs:
            return empty
        specs, label_specs = split_components(
            discover_components(side_a, side_b, blocked.pairs), exact_pairs
        )
        empty = replace(empty, stage=STAGE_FEW_COMPONENTS, components=len(specs))
        if len(specs) < MIN_COMPONENTS:
            log.info(
                "composite needs at least two components",
                tenant_id=tenant_id,
                system_a=trigger.system_a,
                system_b=trigger.system_b,
                components=len(specs),
            )
            return empty
        rng = np.random.default_rng(_seed(tenant_id, trigger.system_a, trigger.system_b))
        u = estimate_u(specs, side_a, side_b, rng=rng)
        matrix = agreement_matrix(specs, side_a, side_b, blocked.pairs)
        labels = label_vector(label_specs, side_a, side_b, blocked.pairs)
        em = fellegi_sunter_em(
            matrix,
            u,
            iterations=settings.composite_em_iterations,
            initial_prior=one_to_one_prior(len(side_a), len(side_b), len(blocked.pairs)),
            update_u=True,
            labels=labels,
            match_density=min(len(side_a), len(side_b)) / len(blocked.pairs),
        )
        weights = pair_weights(matrix, em.m, u)
        posterior = posteriors(matrix, em.m, em.u, em.prior)
        choice = choose_threshold(weights, posterior, settings.composite_target_precision)
        empty = replace(
            empty,
            stage=STAGE_NO_THRESHOLD,
            labelled_pairs=int(np.count_nonzero(~np.isnan(labels))),
            em_prior=em.prior,
            em_iterations=em.iterations,
            em_converged=em.converged,
        )
        if choice is None:
            return empty
        empty = replace(
            empty,
            stage=STAGE_BELOW_MIN_MATCH,
            threshold=choice.threshold,
            estimated_precision=choice.estimated_precision,
        )
        selected = np.flatnonzero(weights >= choice.threshold)
        matches = tuple(
            CompositeMatch(
                event_id_a=side_a.events[blocked.pairs[row][0]].event_id,
                event_id_b=side_b.events[blocked.pairs[row][1]].event_id,
                weight=float(weights[row]),
            )
            for row in selected.tolist()
        )
        if len(matches) < settings.min_match:
            log.info(
                "composite rule below the match floor",
                tenant_id=tenant_id,
                system_a=trigger.system_a,
                system_b=trigger.system_b,
                matched=len(matches),
            )
            return empty
        scored = components_from(specs, em.m, u)
        components = tuple(sorted(scored, key=lambda c: -c.weight)[:MAX_COMPONENTS])
        rule = CompositeRule(
            components=components,
            window_seconds=settings.composite_window_business_days * SECONDS_PER_DAY,
            threshold=choice.threshold,
            estimated_precision=choice.estimated_precision,
        )
        primary = primary_component(specs, scored, matrix, selected)
        link = self._scored_link(
            tenant_id, window, trigger, rule, primary, side_a, side_b, blocked.pairs, selected
        )
        return replace(empty, rule=rule, link=link, matches=matches, stage=STAGE_RULE)

    def _scored_link(
        self,
        tenant_id: str,
        window: Window,
        trigger: SystemPairTrigger,
        rule: CompositeRule,
        primary: tuple[ComponentSpec, CompositeComponent],
        side_a: Side,
        side_b: Side,
        pairs: Sequence[tuple[int, int]],
        selected: NDArray[np.int64],
    ) -> ScoredLink:
        """The link between the primary pair's ends. ``n_a`` counts the A events that carry the
        primary field and whose whole matching window lies inside the data window (an event
        near the end has not had its chance yet); ``n_b`` the B events carrying the primary
        field; ``c_ab`` and ``c_ba`` are the matched shares of those."""
        spec, strongest = primary
        days = self._settings.composite_window_business_days
        last = window.end - timedelta(microseconds=1)
        eligible_a = {
            i
            for i, event in enumerate(side_a.events)
            if side_has(spec, event, "a")
            and not within_business_days(event.observed_at, last, days)
        }
        carrying_b = sum(1 for event in side_b.events if side_has(spec, event, "b"))
        matched_a: set[int] = set()
        matched_b: set[int] = set()
        lags: list[float] = []
        for row in selected.tolist():
            i, j = pairs[row]
            matched_a.add(i)
            matched_b.add(j)
            lags.append(
                (side_b.events[j].observed_at - side_a.events[i].observed_at) / timedelta(seconds=1)
            )
        n_a, n_b = len(eligible_a), carrying_b
        q25 = quantile_exact_low(lags, 0.25)
        q75 = quantile_exact_low(lags, 0.75)
        features = PairFeatures(
            n_a=n_a,
            n_b=n_b,
            n_match=len(lags),
            c_ab=min(1.0, len(matched_a & eligible_a) / n_a) if n_a else 0.0,
            c_ba=min(1.0, len(matched_b) / n_b) if n_b else 0.0,
            lag_median=quantile_exact_low(lags, 0.5),
            lag_iqr=(q75 - q25) if q25 is not None and q75 is not None else None,
            lag_positive_share=(sum(1 for lag in lags if lag > 0) / len(lags)) if lags else None,
            fanout_ab_median=None,
            fanout_ab_p99=None,
            fanout_ba_median=None,
            fanout_ba_p99=None,
            concentration=None,
            chance_ratio=None,
            domain_size=None,
        )
        precision = rule.estimated_precision if rule.estimated_precision is not None else 0.0
        score = min(1.0, max(0.0, precision * features.c_ab))
        extra: dict[str, Any] = {
            "components": component_views(rule),
            "component_kinds": [component.kind for component in rule.components],
            "window_seconds": rule.window_seconds,
            "threshold": rule.threshold,
            "estimated_precision": rule.estimated_precision,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "adjacency_lag_hours": trigger.lag_hours,
            "adjacency_correlation": trigger.correlation,
            "trigger_reason": trigger.reason,
        }
        preview = KeyLink(
            id=link_id_for(tenant_id, strongest.a, strongest.b),
            a=strongest.a,
            b=strongest.b,
            link_type=LinkType.COMPOSITE,
            role=LinkRole.TRANSACTION,
            direction=Direction.A_TO_B,
            score=score,
            status=LinkStatus.PROPOSED,
            features=features,
            affected_volume=features.n_match,
            extra=extra,
        )
        extra["sentence"] = evidence_sentence(preview, system_names=self._system_names)
        return ScoredLink(
            a=strongest.a,
            b=strongest.b,
            link_type=LinkType.COMPOSITE,
            role=LinkRole.TRANSACTION,
            direction=Direction.A_TO_B,
            score=score,
            features=features,
            affected_volume=features.n_match,
            extra=extra,
        )
