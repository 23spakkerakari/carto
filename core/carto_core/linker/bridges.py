"""Bridge scoring (spec 9.3 roles, 9.4).

Two identifier fields carried by the same events are a bridge candidate. What makes a bridge
trustworthy is **pairing consistency**: the share of A values that always appear with the same
B value (a functional dependency), and the reverse. From the two shares and the row's counts
the rule reads the role and the score:

- **Role.** The median fanout of a direction is exactly one when more than half of its values
  pair with exactly one partner, so both consistencies at or above
  :data:`MEDIAN_ONE_CONSISTENCY` (one half) mean "transaction-link fanout" in spec 9.4's words
  (near one-to-one both ways). Otherwise one side fans out: when its mean partners per value
  (events over its distinct values) reach ``transaction_fanout_p99_max`` the bridge is a
  ``batch`` (one manifest to many shipments, one file to many claims; spec 9.3, 9.7), else an
  ``association`` (a customer id next to an order id groups records, it does not identify one).
- **Score.** A transaction bridge whose larger consistency is at or above
  ``bridge_consistency_min`` scores ``max(logistic, consistency)``: a clean functional
  dependency written by the same event is a link whatever the lag features say. Any other
  bridge scores ``min(logistic, consistency)``, so two multi-valued fields that merely share
  events never reach the proposal threshold on the strength of trivially containing each other;
  a batch or association bridge with a clean dependency in its one-to-one direction keeps its
  full score and is shown as what it is.

Without a co-occurrence row (a stored bridge whose pairs no longer co-occur) only
``features.pairing_consistency`` is known: the 9.3 role stands, the floor still decides between
the boost and the association role. Bridges make families transitive (9.4): ``cart_id ~
order_id`` plus ``order_id ~ merchant_ref`` plus ``merchant_ref ~ payments.ref`` puts all four
ends in one family.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from carto_core.linker.model import LinkRole

if TYPE_CHECKING:
    from carto_core.linker.model import PairFeatures
    from carto_core.settings import LinkerSettings
    from carto_core.store.model import CooccurrenceRow

__all__ = ["MEDIAN_ONE_CONSISTENCY", "bridge_outcome", "bridge_role"]

MEDIAN_ONE_CONSISTENCY: Final = 0.5
"""More than half of the values pairing with exactly one partner is the same statement as
"the median fanout is one"; a fact about medians, not a tunable threshold."""


def _mean_partners(events: int, distinct: int) -> float:
    return events / distinct if distinct > 0 else 0.0


def bridge_role(cooccurrence: CooccurrenceRow, settings: LinkerSettings) -> LinkRole:
    """The spec 9.3 role of a bridge from its co-occurrence row (module docstring)."""
    if (
        cooccurrence.consistency_ab >= MEDIAN_ONE_CONSISTENCY
        and cooccurrence.consistency_ba >= MEDIAN_ONE_CONSISTENCY
    ):
        return LinkRole.TRANSACTION
    fan_out = max(
        _mean_partners(cooccurrence.events, cooccurrence.distinct_a),
        _mean_partners(cooccurrence.events, cooccurrence.distinct_b),
    )
    if fan_out >= settings.transaction_fanout_p99_max:
        return LinkRole.BATCH
    return LinkRole.ASSOCIATION


def bridge_outcome(
    probability: float,
    role: LinkRole,
    features: PairFeatures,
    settings: LinkerSettings,
    cooccurrence: CooccurrenceRow | None = None,
) -> tuple[float, LinkRole]:
    """``(score, role)`` for a bridge given its logistic ``probability`` and 9.3 ``role``."""
    if cooccurrence is not None:
        high = max(cooccurrence.consistency_ab, cooccurrence.consistency_ba)
        bridge_kind = bridge_role(cooccurrence, settings)
        if high >= settings.bridge_consistency_min:
            if bridge_kind == LinkRole.TRANSACTION:
                return max(probability, min(1.0, high)), bridge_kind
            return probability, bridge_kind
        return min(probability, high), bridge_kind
    consistency = features.pairing_consistency
    if consistency is None:
        return probability, role
    if consistency >= settings.bridge_consistency_min:
        if role == LinkRole.TRANSACTION:
            return max(probability, min(1.0, consistency)), role
        return probability, role
    if role == LinkRole.TRANSACTION:
        return min(probability, consistency), LinkRole.ASSOCIATION
    return min(probability, consistency), role
