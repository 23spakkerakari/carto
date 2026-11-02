"""Verification scoring: the logistic score, the hard filters, direction and role (spec 9.3;
ADR 0049, 0055).

:func:`feature_vector` turns :class:`PairFeatures` into the named features of
:data:`~carto_core.linker.weights.FEATURE_NAMES`, each bounded so a weight reads as "logits per
unit":

- ``c_ab``, ``c_ba``: containment both ways;
- ``log_n_match``: ``log1p(n_match)``;
- ``lag_consistency``: ``1 - min(1, lag_iqr / max(|lag_median|, LAG_FLOOR_SECONDS))``, so a
  spread small against the typical lag (or against one minute) counts as consistent;
- ``lag_direction``: ``max(share, 1 - share)`` of ``lag_positive_share``, folded because a link
  that always runs B to A is as real as one that runs A to B;
- ``fanout_penalty``: how far the *smaller* of the two median fanouts sits above one, scaled by
  ``transaction_fanout_median_max``; one-to-many links keep one end at one and are not
  penalised (they become associations by role), many-to-many overlaps are;
- ``concentration``: the top-ten share's excess over the flat baseline ``10 / n_match``
  (:func:`concentration_excess`), and ``chance_ratio`` (clipped at one) from the spec table;
- ``pairing_consistency``: the bridge consistency of 9.4, zero for exact pairs.

A missing feature (no matches, unknown domain) contributes its neutral value. Every threshold
comes from :class:`~carto_core.settings.LinkerSettings` (ADR 0055).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from carto_core.linker.model import (
    Candidate,
    Direction,
    LinkRole,
    LinkScore,
    LinkType,
    PairFeatures,
)
from carto_core.linker.weights import DEFAULT_WEIGHTS, Weights
from carto_core.store.memory import CONCENTRATION_TOP_N

if TYPE_CHECKING:
    from carto_core.settings import LinkerSettings
    from carto_core.store.model import CooccurrenceRow

__all__ = [
    "LAG_FLOOR_SECONDS",
    "Scorer",
    "concentration_excess",
    "direction_of",
    "feature_vector",
    "hard_filter",
    "logistic",
    "role_of",
]

LAG_FLOOR_SECONDS: Final = 60.0
"""The lag spread is judged against the median lag or one minute, whichever is larger, so a
link whose events land within seconds is not called inconsistent by a few seconds of jitter."""


def _or(value: float | None, default: float) -> float:
    return default if value is None else value


def concentration_excess(concentration: float | None, n_match: int) -> float:
    """The concentration feature: how far the top-ten share sits above the flat baseline.

    Spec 9.3's ``concentration`` is the share of matches the ten most matched tokens hold. With
    ``n`` matched tokens of fan-out one that share is ``10 / n`` even when every token matches
    exactly once, so a sparse link (21 matches: 0.48) would read as nearly as concentrated as
    a genuinely skewed one. The feature therefore measures the excess over the flat baseline,
    rescaled to ``[0, 1]``; the hard filter keeps the spec's literal definition."""
    if concentration is None or n_match <= 0:
        return 0.0
    baseline = min(1.0, CONCENTRATION_TOP_N / n_match)
    if baseline >= 1.0:
        return 0.0
    return min(1.0, max(0.0, (concentration - baseline) / (1.0 - baseline)))


def feature_vector(features: PairFeatures, settings: LinkerSettings) -> dict[str, float]:
    """The named, bounded features of the module docstring."""
    lag_iqr = features.lag_iqr
    if lag_iqr is None:
        lag_consistency = 0.0
    else:
        scale = max(abs(_or(features.lag_median, 0.0)), LAG_FLOOR_SECONDS)
        lag_consistency = 1.0 - min(1.0, max(0.0, lag_iqr) / scale)
    share = _or(features.lag_positive_share, 0.5)
    medians = [m for m in (features.fanout_ab_median, features.fanout_ba_median) if m is not None]
    smaller_median = min(medians) if medians else 1.0
    fanout_penalty = min(
        1.0, max(0.0, (smaller_median - 1.0) / settings.transaction_fanout_median_max)
    )
    return {
        "c_ab": min(1.0, max(0.0, features.c_ab)),
        "c_ba": min(1.0, max(0.0, features.c_ba)),
        "log_n_match": math.log1p(max(0, features.n_match)),
        "lag_consistency": lag_consistency,
        "lag_direction": max(share, 1.0 - share),
        "fanout_penalty": fanout_penalty,
        "concentration": concentration_excess(features.concentration, features.n_match),
        "chance_ratio": min(1.0, max(0.0, _or(features.chance_ratio, 0.0))),
        "pairing_consistency": min(1.0, max(0.0, _or(features.pairing_consistency, 0.0))),
    }


def logistic(vector: dict[str, float], weights: Weights) -> float:
    logit = weights.intercept + sum(
        weights.coefficient(name) * value for name, value in vector.items()
    )
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-logit))
    expo = math.exp(logit)
    return expo / (1.0 + expo)


def hard_filter(features: PairFeatures, settings: LinkerSettings) -> str | None:
    """The spec 9.3 hard filters; the name of the first one that discards the pair, else None."""
    if features.n_match < settings.min_match:
        return "n_match"
    if features.concentration is not None and features.concentration > settings.max_concentration:
        return "concentration"
    if features.chance_ratio is not None and features.chance_ratio > settings.max_chance_ratio:
        return "chance_ratio"
    return None


def direction_of(features: PairFeatures, settings: LinkerSettings) -> Direction:
    share = features.lag_positive_share
    if share is None:
        return Direction.UNDIRECTED
    forward = settings.direction_share
    backward = 1.0 - settings.direction_share
    if share >= forward or math.isclose(share, forward):
        return Direction.A_TO_B
    if share <= backward or math.isclose(share, backward):
        return Direction.B_TO_A
    return Direction.UNDIRECTED


def _transaction_like(median: float | None, p99: float | None, settings: LinkerSettings) -> bool:
    return (
        median is not None
        and p99 is not None
        and median <= settings.transaction_fanout_median_max
        and p99 <= settings.transaction_fanout_p99_max
    )


def role_of(features: PairFeatures, settings: LinkerSettings) -> LinkRole:
    """Spec 9.3 link roles: transaction when near one-to-one or one-to-few both ways; batch
    when one end fans out past the p99 limit while the other end stays near one; association
    otherwise (and whenever the fanouts are unknown)."""
    ab = _transaction_like(features.fanout_ab_median, features.fanout_ab_p99, settings)
    ba = _transaction_like(features.fanout_ba_median, features.fanout_ba_p99, settings)
    if ab and ba:
        return LinkRole.TRANSACTION
    if None in (
        features.fanout_ab_median,
        features.fanout_ab_p99,
        features.fanout_ba_median,
        features.fanout_ba_p99,
    ):
        return LinkRole.ASSOCIATION
    ab_p99 = features.fanout_ab_p99 or 0.0
    ba_p99 = features.fanout_ba_p99 or 0.0
    ab_median = features.fanout_ab_median or 0.0
    ba_median = features.fanout_ba_median or 0.0
    p99_max = settings.transaction_fanout_p99_max
    other_end_max = settings.batch_other_end_median_max
    if ab_p99 > p99_max and ba_median <= other_end_max:
        return LinkRole.BATCH
    if ba_p99 > p99_max and ab_median <= other_end_max:
        return LinkRole.BATCH
    return LinkRole.ASSOCIATION


class Scorer:
    """Scores verified candidates with one set of weights and settings."""

    __slots__ = ("_settings", "_weights")

    def __init__(self, settings: LinkerSettings, weights: Weights | None = None) -> None:
        self._settings = settings
        self._weights = weights if weights is not None else DEFAULT_WEIGHTS

    @property
    def weights(self) -> Weights:
        return self._weights

    def probability(self, features: PairFeatures) -> float:
        """The logistic score alone (no filters, no bridge rule)."""
        return logistic(feature_vector(features, self._settings), self._weights)

    def score(
        self,
        candidate: Candidate,
        features: PairFeatures,
        *,
        link_type: LinkType | None = None,
        cooccurrence: CooccurrenceRow | None = None,
    ) -> LinkScore:
        """The spec 9.3 outcome. ``link_type`` defaults from how the candidate was found: a
        co-occurrence pair is a bridge, anything else exact; a re-proposed pair passes its stored
        type. Bridges get the 9.4 rule from :mod:`carto_core.linker.bridges`, which reads the
        consistency of both directions from ``cooccurrence`` when the row is known."""
        from carto_core.linker.bridges import bridge_outcome  # noqa: PLC0415

        kind = link_type
        if kind is None:
            kind = LinkType.BRIDGE if candidate.source == "bridge" else LinkType.EXACT
        probability = self.probability(features)
        role = role_of(features, self._settings)
        if kind == LinkType.BRIDGE:
            probability, role = bridge_outcome(
                probability, role, features, self._settings, cooccurrence
            )
        reason = hard_filter(features, self._settings)
        return LinkScore(
            score=probability,
            passes_filters=reason is None,
            filter_reason=reason,
            direction=direction_of(features, self._settings),
            role=role,
            link_type=kind,
        )
