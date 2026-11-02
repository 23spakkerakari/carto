"""Verification of one candidate pair (spec 9.3, 9.4; ADR 0048).

**Exact pairs.** One :meth:`EventStore.pair_join` per candidate returns every feature of the 9.3
table that comes from the token join. The chance ratio needs ``domain_size``, estimated from
the dominant shape of each end: the smaller of the two domains is used, because a chance match
can only come from values both domains hold. :func:`default_domain_size` is the spec's rule
(``9999`` is ``10^4``, ``AA-9999999`` is ``26^2 x 10^7``); the profiler package offers a finer
one and the pass takes either as an injected :data:`DomainSizeFn`.

**Bridge pairs.** Two different identifiers carried by the same events (``order_id`` and
``merchant_ref``) share no tokens, so the token join says nothing about them; their exact join
is on the event (spec 9.4). :func:`bridge_features` fills the same table from the
:class:`CooccurrenceRow`: ``n_a`` and ``n_b`` are the distinct values of each end seen together,
``n_match`` the events carrying both (so the ``n_match`` hard filter is the ``bridge_min_events``
floor), ``c_ab`` and ``c_ba`` the share of each end's values (from its profile) that appear with
the other, the lag is zero by construction, and ``pairing_consistency`` is the larger of the two
functional-dependency shares; fanout, concentration and chance ratio stay unknown, since the
pairing is by event, not by chance, and :mod:`carto_core.linker.bridges` reads the role from the
consistency of both directions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from carto_core.linker.model import PairFeatures

if TYPE_CHECKING:
    from carto_core.linker.model import Candidate
    from carto_core.profiler.model import FieldProfile
    from carto_core.store.model import CooccurrenceRow, EventStore, FieldForm, Window

__all__ = [
    "MAX_DOMAIN_SIZE",
    "DomainSizeFn",
    "bridge_features",
    "default_domain_size",
    "pair_domain_size",
    "verify",
]

DomainSizeFn = Callable[[str], float | None]
"""``shape -> number of distinct values the shape admits``, or None when unknown."""

MAX_DOMAIN_SIZE: Final = 1e300
"""Domains are capped so a 64-character shape never overflows a float."""

_PER_CHARACTER: Final[dict[str, float]] = {"9": 10.0, "A": 26.0, "a": 26.0}


def default_domain_size(shape: str) -> float | None:
    """Spec 9.3: a digit position admits 10 values, a letter 26, any other character of the
    shape is a constant. ``+`` (a run longer than the shape keeps, spec 8.3) counts as one more
    position of the class before it. Returns None for an empty shape."""
    if not shape:
        return None
    size = 1.0
    previous = 1.0
    for char in shape:
        if char == "+":
            factor = previous
        elif char.isalpha():
            factor = _PER_CHARACTER.get(char, 36.0)
        else:
            factor = _PER_CHARACTER.get(char, 1.0)
        previous = factor
        size = min(MAX_DOMAIN_SIZE, size * factor)
    return size


def pair_domain_size(
    shape_a: str | None, shape_b: str | None, domain_size_fn: DomainSizeFn
) -> float | None:
    """The smaller of the two ends' domains; None when either end has no shape or no domain."""
    if shape_a is None or shape_b is None:
        return None
    size_a = domain_size_fn(shape_a)
    size_b = domain_size_fn(shape_b)
    if size_a is None or size_b is None:
        return None
    return min(size_a, size_b)


def _top_shape(profiles: Mapping[FieldForm, FieldProfile], end: FieldForm) -> str | None:
    profile = profiles.get(end)
    return profile.top_shape if profile is not None else None


def _share(seen_together: int, end: FieldForm, profiles: Mapping[FieldForm, FieldProfile]) -> float:
    profile = profiles.get(end)
    if profile is None or profile.distinct <= 0:
        return 1.0 if seen_together else 0.0
    return min(1.0, seen_together / profile.distinct)


def bridge_features(
    row: CooccurrenceRow, profiles: Mapping[FieldForm, FieldProfile]
) -> PairFeatures:
    """The 9.3 table for a bridge pair, from its co-occurrence row (module docstring)."""
    return PairFeatures(
        n_a=row.distinct_a,
        n_b=row.distinct_b,
        n_match=row.events,
        c_ab=_share(row.distinct_a, row.a, profiles),
        c_ba=_share(row.distinct_b, row.b, profiles),
        lag_median=0.0,
        lag_iqr=0.0,
        lag_positive_share=None,
        fanout_ab_median=None,
        fanout_ab_p99=None,
        fanout_ba_median=None,
        fanout_ba_p99=None,
        concentration=None,
        chance_ratio=None,
        domain_size=None,
        pairing_consistency=max(row.consistency_ab, row.consistency_ba),
    )


def verify(
    store: EventStore,
    tenant_id: str,
    candidate: Candidate,
    window: Window,
    *,
    domain_size_fn: DomainSizeFn,
    profiles: Mapping[FieldForm, FieldProfile],
    cooccurrence: CooccurrenceRow | None = None,
) -> PairFeatures:
    """The spec 9.3 feature table for ``candidate`` over ``window``: from the co-occurrence row
    when one is given (a bridge), else from the token join."""
    if cooccurrence is not None:
        if {cooccurrence.a, cooccurrence.b} != {candidate.a, candidate.b}:
            msg = "co-occurrence row does not belong to the candidate"
            raise ValueError(msg)
        row = cooccurrence
        if row.a != candidate.a:
            from carto_core.store.model import CooccurrenceRow as _Row  # noqa: PLC0415

            row = _Row(
                a=row.b,
                b=row.a,
                system_id=row.system_id,
                events=row.events,
                distinct_a=row.distinct_b,
                single_b_a=row.single_a_b,
                distinct_b=row.distinct_a,
                single_a_b=row.single_b_a,
            )
        return bridge_features(row, profiles)
    join = store.pair_join(tenant_id, candidate.a, candidate.b, window)
    domain = pair_domain_size(
        _top_shape(profiles, candidate.a), _top_shape(profiles, candidate.b), domain_size_fn
    )
    return PairFeatures.from_join(join, domain_size=domain)
