"""Profiler data model (spec 9.1; plan M2 decision 2).

A :class:`DaySketch` is what the profiler keeps per ``(field_ref, form, UTC day)``: the counts,
the shape and length histograms, a MinHash signature, a HyperLogLog and the bottom-k sample. A
window is the merge of the last ``window_days`` day sketches (:func:`merge_window`), so the
nightly rebuild and the five-minute update are one code path. MinHash and HyperLogLog merge
natively; the bottom-k sample is the ``k`` smallest 64-bit hashes of the tokens and merges by
taking the smallest again, which also gives an exact containment estimate for two samples
without a database round trip.

The sketch bytes are opaque to everything but the profiler (``datasketch`` serializations); the
rest of the engine reads :class:`FieldProfile`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

from carto_core.store.model import FieldForm

__all__ = [
    "DaySketch",
    "FieldProfile",
    "LengthStats",
    "ShapeShare",
    "WindowSketch",
]


@dataclass(frozen=True, slots=True)
class ShapeShare:
    shape: str
    share: float


@dataclass(frozen=True, slots=True)
class LengthStats:
    """Token value lengths (``len`` of the identifier; the shape's length equals it)."""

    min: int
    max: int
    mean: float
    stdev: float


@dataclass(frozen=True, slots=True)
class DaySketch:
    """One ``field_sketches`` row: ``(field_id, form, day)`` with its serialized sketches."""

    end: FieldForm
    day: date
    count: int
    """Identifier rows (events carrying the field) that day."""
    shape_counts: Mapping[str, int]
    length_counts: Mapping[int, int]
    minhash: bytes
    hll: bytes
    bottom_k: tuple[int, ...]
    """The smallest 64-bit token hashes, ascending, at most ``bottom_k`` of them."""
    top_token_counts: Mapping[int, int] = field(default_factory=dict)
    """Occurrence counts of the most frequent token hashes (for the concentration term of q)."""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sketch_version: int = 1


@dataclass(frozen=True, slots=True)
class WindowSketch:
    """The merge of a field's day sketches over the window."""

    end: FieldForm
    days: tuple[date, ...]
    count: int
    shape_counts: Mapping[str, int]
    length_counts: Mapping[int, int]
    minhash: bytes
    hll: bytes
    bottom_k: tuple[int, ...]
    top_token_counts: Mapping[int, int]
    first_seen: datetime | None
    last_seen: datetime | None


@dataclass(frozen=True, slots=True)
class FieldProfile:
    """What the linker and the API read per ``(field_ref, form)`` (spec 7.3 ``fields``)."""

    end: FieldForm
    count: int
    distinct: int
    """HyperLogLog estimate over the window."""
    null_rate: float
    """``1 - events carrying the field / events of its template`` over the window."""
    shapes: tuple[ShapeShare, ...]
    """Top shapes by share, descending (spec 9.1 "shape distribution")."""
    length_stats: LengthStats | None
    top_share: float
    """Share of occurrences the ``concentration_top_n`` most frequent tokens hold."""
    quality: float
    """``q(field)`` in [0, 1] (spec 9.1)."""
    is_candidate: bool
    """``quality >= quality_floor`` and ``distinct >= min_distinct``."""
    first_seen: datetime | None
    last_seen: datetime | None

    @property
    def top_shape(self) -> str | None:
        return self.shapes[0].shape if self.shapes else None
