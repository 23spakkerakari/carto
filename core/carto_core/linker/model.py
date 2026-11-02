"""Linker data model (spec 9.2 to 9.6). The vocabularies (:class:`LinkType`, :class:`LinkRole`,
:class:`Direction`, :class:`LinkStatus`, :class:`RejectReason`) come from the API contract
(:mod:`carto_schema.api`) so the engine, the API and the web client share one spelling.

:class:`PairFeatures` names every feature of the spec 9.3 table as the spec does, plus the
bridge consistency of 9.4; :class:`LinkScore` is the logistic score with the hard-filter and
direction and role decisions; :class:`KeyLink` is one ``key_links`` row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from carto_core.store.model import FieldForm, PairJoin
from carto_schema.api import Direction, LinkRole, LinkStatus, LinkType, RejectReason

__all__ = [
    "Candidate",
    "CompositeComponent",
    "CompositeRule",
    "Direction",
    "KeyLink",
    "LinkRole",
    "LinkScore",
    "LinkStatus",
    "LinkType",
    "PairFeatures",
    "RejectReason",
]


@dataclass(frozen=True, slots=True, order=True)
class Candidate:
    """A pair the linker will verify, with how it was found (spec 9.2)."""

    a: FieldForm
    b: FieldForm
    estimated_containment: float
    """LSH estimate (``max(c_ab, c_ba)`` from the sketches) or 1.0 for a bridge pair."""
    source: str = "lsh"
    """``lsh`` (9.2 step 1), ``bridge`` (9.2 step 3) or ``repropose`` (9.6)."""


@dataclass(frozen=True, slots=True)
class PairFeatures:
    """The spec 9.3 table, every field named as in the spec, computed from a :class:`PairJoin`
    plus the shapes (``domain_size``) and, for bridges, the pairing consistency (9.4)."""

    n_a: int
    n_b: int
    n_match: int
    c_ab: float
    c_ba: float
    lag_median: float | None
    lag_iqr: float | None
    lag_positive_share: float | None
    fanout_ab_median: float | None
    fanout_ab_p99: float | None
    fanout_ba_median: float | None
    fanout_ba_p99: float | None
    concentration: float | None
    chance_ratio: float | None
    domain_size: float | None
    pairing_consistency: float | None = None
    """Bridges only: ``max(consistency_ab, consistency_ba)`` of the co-occurrence row."""

    @classmethod
    def from_join(
        cls,
        join: PairJoin,
        *,
        domain_size: float | None,
        pairing_consistency: float | None = None,
    ) -> PairFeatures:
        chance: float | None = None
        if domain_size is not None and domain_size > 0 and join.n_match > 0:
            chance = (join.n_a * join.n_b / domain_size) / join.n_match
        return cls(
            n_a=join.n_a,
            n_b=join.n_b,
            n_match=join.n_match,
            c_ab=join.c_ab,
            c_ba=join.c_ba,
            lag_median=join.lag_median_seconds,
            lag_iqr=join.lag_iqr_seconds,
            lag_positive_share=join.lag_positive_share,
            fanout_ab_median=join.fanout_ab_median,
            fanout_ab_p99=join.fanout_ab_p99,
            fanout_ba_median=join.fanout_ba_median,
            fanout_ba_p99=join.fanout_ba_p99,
            concentration=join.concentration,
            chance_ratio=chance,
            domain_size=domain_size,
            pairing_consistency=pairing_consistency,
        )

    def as_evidence(self) -> dict[str, Any]:
        """The ``key_links.evidence_json`` content: every feature, JSON-safe."""
        return {
            "n_a": self.n_a,
            "n_b": self.n_b,
            "n_match": self.n_match,
            "c_ab": self.c_ab,
            "c_ba": self.c_ba,
            "lag_median_seconds": self.lag_median,
            "lag_iqr_seconds": self.lag_iqr,
            "lag_positive_share": self.lag_positive_share,
            "fanout_ab_median": self.fanout_ab_median,
            "fanout_ab_p99": self.fanout_ab_p99,
            "fanout_ba_median": self.fanout_ba_median,
            "fanout_ba_p99": self.fanout_ba_p99,
            "concentration": self.concentration,
            "chance_ratio": self.chance_ratio,
            "domain_size": self.domain_size,
            "pairing_consistency": self.pairing_consistency,
        }


@dataclass(frozen=True, slots=True)
class LinkScore:
    """The outcome of scoring one candidate (spec 9.3)."""

    score: float
    passes_filters: bool
    filter_reason: str | None
    """``concentration``, ``chance_ratio`` or ``n_match`` when a hard filter discarded it."""
    direction: Direction
    role: LinkRole
    link_type: LinkType


@dataclass(frozen=True, slots=True)
class CompositeComponent:
    """One component of a composite rule (spec 9.5)."""

    a: FieldForm
    b: FieldForm
    kind: str
    """``date``, ``amount``, ``phonetic``, ``attribute`` or ``partial_identifier``."""
    m: float
    u: float

    @property
    def weight(self) -> float:
        """Log-likelihood ratio of agreement, ``log2(m / u)``."""
        import math  # noqa: PLC0415

        return math.log2(self.m / self.u) if self.m > 0 and self.u > 0 else 0.0


@dataclass(frozen=True, slots=True)
class CompositeRule:
    """One ``composite_rules`` row (spec 7.3)."""

    components: tuple[CompositeComponent, ...]
    window_seconds: int
    threshold: float
    """Pair weight at or above which two events match; chosen for the target precision."""
    estimated_precision: float | None = None


@dataclass(frozen=True, slots=True)
class KeyLink:
    """One ``key_links`` row (spec 7.3) with its evidence."""

    id: str
    a: FieldForm
    b: FieldForm
    link_type: LinkType
    role: LinkRole
    direction: Direction
    score: float
    status: LinkStatus
    features: PairFeatures
    affected_volume: int
    version: int = 1
    entity_id: str | None = None
    reject_reason: RejectReason | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    ambiguous_merges: int = 0
    composite: CompositeRule | None = None
    first_proposed_at: datetime | None = None
    updated_at: datetime | None = None
    baseline_match_rate: float | None = None
    """``c_ab`` when the link was accepted; staleness compares against it (9.6)."""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_usable_for_families(self) -> bool:
        """Accepted (or auto-accepted) links of role ``transaction`` form families (spec 9.6
        "connected components over accepted transaction links"), exact ones and bridges alike.
        A bridge of role ``batch`` (one file to many shipments) or ``association`` is shown in
        the map and never merges families: it would glue a batch's entity to the records it
        carries (spec 9.7 handles batches). Composite links never auto-accept and join a family
        only once a reviewer accepts them."""
        return (
            self.status in (LinkStatus.ACCEPTED, LinkStatus.AUTO_ACCEPTED)
            and self.role == LinkRole.TRANSACTION
        )
