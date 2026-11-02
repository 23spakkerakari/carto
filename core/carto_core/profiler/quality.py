"""Identifier quality ``q(field)`` and the field profile (spec 9.1; ADR 0047, 0055).

Spec 9.1: ``q(field)`` in [0, 1] "combines: distinct ratio, top-shape share (consistent format),
length stability, and low value concentration (top 10 values hold under 20% of occurrences).
Fields with ``q < 0.4`` or fewer than 50 distinct values are not used as link candidates."

The four terms, each in [0, 1]:

- **distinct ratio**: ``min(1, distinct / count)``; an identifier repeats little inside a
  window of events (an order id once per event of the order), a status code repeats a lot.
- **top-shape share**: the share of occurrences with the most common shape; a consistent format
  is what an identifier scheme produces.
- **length stability**: ``1 - min(1, stdev / mean)`` of the value lengths (one minus the
  coefficient of variation, floored at 0); fixed-width identifiers score 1, free text scores
  near 0. With no lengths the term is 0.
- **low concentration**: ``1 - min(1, top_share / concentration_max_share)`` where ``top_share``
  is the share of occurrences the ``concentration_top_n`` most frequent tokens hold; it is 1
  when the top ten hold nothing and 0 once they hold the configured 20% or more.

``q`` is the equal-weight mean of the four. Equal weights are the spec's hand-set v1 (it lists
the terms without weights, and the eval harness is how numbers get tuned, ADR 0055); each term
already saturates at its spec threshold, so no term can be bought with another. ``q`` is
monotonic in the distinct ratio and in the top-shape share, which the property tests check.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

from carto_core.profiler.model import FieldProfile, LengthStats, ShapeShare
from carto_core.profiler.sketches import distinct_estimate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from carto_core.profiler.model import WindowSketch
    from carto_core.settings import ProfilerSettings

__all__ = [
    "build_profile",
    "identifier_quality",
    "is_candidate",
    "length_stats",
    "top_shapes",
    "top_token_share",
]


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def length_stats(length_counts: Mapping[int, int]) -> LengthStats | None:
    """Min, max, mean and population standard deviation of a length histogram."""
    total = sum(length_counts.values())
    if total <= 0:
        return None
    mean = sum(length * n for length, n in length_counts.items()) / total
    variance = sum(n * (length - mean) ** 2 for length, n in length_counts.items()) / total
    return LengthStats(
        min=min(length_counts),
        max=max(length_counts),
        mean=mean,
        stdev=math.sqrt(max(0.0, variance)),
    )


def top_shapes(shape_counts: Mapping[str, int], limit: int) -> tuple[ShapeShare, ...]:
    """The ``limit`` shapes with the largest share, descending; ties broken by shape text."""
    total = sum(shape_counts.values())
    if total <= 0:
        return ()
    ordered = sorted(shape_counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ShapeShare(shape=shape, share=n / total) for shape, n in ordered[:limit])


def top_token_share(top_token_counts: Mapping[int, int], count: int, top_n: int) -> float:
    """Share of occurrences the ``top_n`` most frequent tokens hold (0 for an empty field)."""
    if count <= 0 or not top_token_counts:
        return 0.0
    largest = sorted(top_token_counts.values(), reverse=True)[:top_n]
    return _unit(sum(largest) / count)


def identifier_quality(
    count: int,
    distinct: int,
    top_shape_share: float,
    length_stats: LengthStats | None,
    top_share: float,
    settings: ProfilerSettings,
) -> float:
    """``q(field)`` as the module docstring defines it; 0 for an empty field."""
    if count <= 0:
        return 0.0
    distinct_ratio = _unit(distinct / count)
    shape_term = _unit(top_shape_share)
    if length_stats is None or length_stats.mean <= 0:
        length_term = 0.0
    else:
        length_term = _unit(1.0 - length_stats.stdev / length_stats.mean)
    concentration_term = _unit(1.0 - top_share / settings.concentration_max_share)
    return (distinct_ratio + shape_term + length_term + concentration_term) / 4.0


def is_candidate(profile: FieldProfile, settings: ProfilerSettings) -> bool:
    """Spec 9.1: ``q`` at or above the floor and at least ``min_distinct`` distinct values."""
    return profile.quality >= settings.quality_floor and profile.distinct >= settings.min_distinct


def build_profile(
    window: WindowSketch, template_events: int, settings: ProfilerSettings
) -> FieldProfile:
    """The :class:`FieldProfile` of a window sketch. ``template_events`` is the number of
    distinct events of the field's template over the same days; the null rate is the share of
    them not carrying the field, clamped to [0, 1] (0 when the template count is unknown).
    ``distinct`` is exact while the bottom-k sample is not saturated (see
    :func:`~carto_core.profiler.sketches.distinct_estimate`), so an end with exactly
    ``min_distinct`` values passes the floor."""
    distinct = distinct_estimate(window, k=settings.bottom_k)
    shapes = top_shapes(window.shape_counts, settings.top_shapes)
    lengths = length_stats(window.length_counts)
    top_share = top_token_share(window.top_token_counts, window.count, settings.concentration_top_n)
    null_rate = _unit(1.0 - window.count / template_events) if template_events > 0 else 0.0
    quality = identifier_quality(
        window.count, distinct, shapes[0].share if shapes else 0.0, lengths, top_share, settings
    )
    profile = FieldProfile(
        end=window.end,
        count=window.count,
        distinct=distinct,
        null_rate=null_rate,
        shapes=shapes,
        length_stats=lengths,
        top_share=top_share,
        quality=quality,
        is_candidate=False,
        first_seen=window.first_seen,
        last_seen=window.last_seen,
    )
    return replace(profile, is_candidate=is_candidate(profile, settings))
