"""Day sketches and their merges (spec 9.1; ADR 0047).

The profiler keeps one :class:`~carto_core.profiler.model.DaySketch` per ``(field_ref, form, UTC
day)`` and merges the days of the window into a
:class:`~carto_core.profiler.model.WindowSketch`. Everything here is a pure function of the
identifier rows it is fed, so the incremental pass (merge new rows into a stored day) and the
nightly rebuild (sketch every day from scratch) give the same bytes; the property tests check
that merging equals sketching the union.

Per day a sketch holds:

- ``count``: identifier rows, that is events carrying the field that day;
- ``shape_counts`` and ``length_counts``: histograms. The length is ``len(shape)`` because
  :func:`carto_schema.forms.shape` maps a value character by character; it is exact unless the
  shape collapsed a run of more than :data:`~carto_schema.forms.SHAPE_MAX_RUN` characters or was
  cut at :data:`~carto_schema.forms.SHAPE_MAX_LEN`, in which case it is a lower bound (the
  ``event_identifiers`` table carries no length column);
- a ``datasketch`` MinHash (``minhash_permutations``, default 128) over the token bytes, for the
  LSH index of spec 9.2, serialized as its ``hashvalues`` array, big-endian ``uint64``;
- a HyperLogLog (``p`` = :data:`HLL_PRECISION`) for ``distinct``, serialized as its register
  array. It is only consulted once the bottom-k sample is saturated: a sample holding fewer
  than ``k`` hashes is the whole token set, so :func:`distinct_estimate` returns its length,
  exactly. Small ends matter: the ``min_distinct`` floor of spec 9.1 sits at 50, and a
  HyperLogLog reading 49 for a field with exactly 50 values would drop a real key;
- the bottom-k sample: the ``bottom_k`` (default 2,048) smallest :func:`token_hash` values,
  ascending. The hash is the first 8 bytes of SHA-256 over the token, so every process agrees
  without a shared seed. Two samples give the containment estimate of
  :func:`containment_estimate` without a store round trip (ADR 0048);
- ``top_token_counts``: occurrence counts of the most frequent token hashes, trimmed to
  ``2 * concentration_top_n`` entries. Trimming after every merge is an approximation (a heavy
  hitter that is never in a day's top list is lost), bounded on purpose: the concentration term
  of ``q`` only needs the top ten, and a field whose top ten changes between days is not
  concentrated.

No serialization here uses pickle (spec 2.3 invariant 8); the stored bytes are plain arrays.
Nothing stored is a token in clear: sketches hold hashes and MinHash values only, and no error
message or ``repr`` carries a token.
"""

from __future__ import annotations

import hashlib
import heapq
import warnings
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Final

import numpy as np
from datasketch import HyperLogLog, MinHash

from carto_core.profiler.model import DaySketch, WindowSketch
from carto_core.store.model import FieldForm

if TYPE_CHECKING:
    from carto_core.settings import ProfilerSettings
    from carto_core.store.model import IdentifierRow

__all__ = [
    "HLL_PRECISION",
    "MINHASH_SCHEME",
    "MINHASH_SEED",
    "SKETCH_VERSION",
    "DaySketchBuilder",
    "containment_estimate",
    "deserialize_hll",
    "deserialize_minhash",
    "distinct_estimate",
    "jaccard_estimate",
    "merge_day",
    "merge_window",
    "pack_bottom_k",
    "sketch_compatible",
    "token_hash",
    "unpack_bottom_k",
]

SKETCH_VERSION: Final = 1
"""Bumped when the sketch bytes change meaning; a stored row with another version is rebuilt."""

HLL_PRECISION: Final = 12
"""HyperLogLog ``p``: 4,096 registers, about 1.6% relative error. Not a spec 9.x threshold, so a
constant rather than a setting; changing it needs a sketch version bump."""

MINHASH_SEED: Final = 1
"""The ``datasketch`` permutation seed; every sketch must share it to merge."""

MINHASH_SCHEME: Final = "affine32"
"""The ``datasketch`` 2.x hashing scheme the permutations use; required to rebuild a MinHash
from stored hash values."""

_U64_BE: Final = np.dtype(">u8")


def token_hash(token: str) -> int:
    """The first 8 bytes of SHA-256 over the token as a big-endian unsigned integer."""
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")


# -- serialization ----------------------------------------------------------------------------


def serialize_minhash(minhash: MinHash) -> bytes:
    """The hash values as big-endian ``uint64`` (the 32-bit scheme's values fit)."""
    return np.asarray(minhash.hashvalues, dtype=np.uint64).astype(_U64_BE).tobytes()


def deserialize_minhash(data: bytes) -> MinHash:
    """Rebuild a MinHash from :func:`serialize_minhash` output (the permutations come from
    :data:`MINHASH_SEED`, so the result merges with any sketch of the same length)."""
    if not data or len(data) % _U64_BE.itemsize:
        msg = "minhash bytes are not a whole number of 64-bit values"
        raise ValueError(msg)
    values = np.frombuffer(data, dtype=_U64_BE).astype(np.uint32)
    return MinHash(
        num_perm=len(values), seed=MINHASH_SEED, hashvalues=values, scheme=MINHASH_SCHEME
    )


def serialize_hll(hll: HyperLogLog) -> bytes:
    """The register array, one byte per register (``2 ** p`` bytes)."""
    return np.asarray(hll.reg, dtype=np.int8).tobytes()


def deserialize_hll(data: bytes) -> HyperLogLog:
    """Rebuild a HyperLogLog from :func:`serialize_hll` output; ``p`` follows from the length."""
    size = len(data)
    if size == 0 or size & (size - 1):
        msg = "hll bytes are not a power-of-two register array"
        raise ValueError(msg)
    registers = np.frombuffer(data, dtype=np.int8).copy()
    return HyperLogLog(p=size.bit_length() - 1, reg=registers)


def pack_bottom_k(values: Iterable[int]) -> bytes:
    """Ascending ``uint64`` values packed big-endian (the ``bottom_k`` BYTEA column)."""
    return np.fromiter(values, dtype=np.uint64).astype(_U64_BE).tobytes()


def unpack_bottom_k(data: bytes) -> tuple[int, ...]:
    if len(data) % _U64_BE.itemsize:
        msg = "bottom-k bytes are not a whole number of 64-bit values"
        raise ValueError(msg)
    return tuple(int(value) for value in np.frombuffer(data, dtype=_U64_BE))


def _new_minhash(num_perm: int) -> MinHash:
    return MinHash(num_perm=num_perm, seed=MINHASH_SEED, scheme=MINHASH_SCHEME)


def _new_hll() -> HyperLogLog:
    return HyperLogLog(p=HLL_PRECISION)


# -- building ---------------------------------------------------------------------------------


def _trim_top(counts: Mapping[int, int], keep: int) -> dict[int, int]:
    """The ``keep`` largest counts; ties broken by hash so the result is deterministic."""
    if len(counts) <= keep:
        return dict(counts)
    top = heapq.nsmallest(keep, counts.items(), key=lambda item: (-item[1], item[0]))
    return dict(top)


def _smallest_k(values: Iterable[int], k: int) -> tuple[int, ...]:
    return tuple(heapq.nsmallest(k, set(values)))


def _min_time(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_time(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _add_counts(a: Mapping[int, int], b: Mapping[int, int]) -> Counter[int]:
    total: Counter[int] = Counter(a)
    total.update(b)
    return total


class DaySketchBuilder:
    """Consumes :class:`~carto_core.store.model.IdentifierRow` objects of one ``(end, day)`` and
    builds the :class:`DaySketch`. Memory is bounded by the rows fed per builder (the pass feeds
    one read chunk at a time) plus the sketch sizes; the bottom-k heap never grows past ``k``.
    The caller guarantees the rows belong to ``end`` and ``day``; the builder checks the end and
    refuses a row of another, with a message that names neither."""

    __slots__ = (
        "_bottom_heap",
        "_bottom_set",
        "_count",
        "_day",
        "_end",
        "_first_seen",
        "_hll",
        "_k",
        "_last_seen",
        "_lengths",
        "_minhash",
        "_shapes",
        "_tokens",
        "_top_keep",
    )

    def __init__(self, end: FieldForm, day: date, settings: ProfilerSettings) -> None:
        self._end = end
        self._day = day
        self._k = settings.bottom_k
        self._top_keep = 2 * settings.concentration_top_n
        self._count = 0
        self._shapes: Counter[str] = Counter()
        self._lengths: Counter[int] = Counter()
        self._minhash = _new_minhash(settings.minhash_permutations)
        self._hll = _new_hll()
        self._bottom_heap: list[int] = []  # max-heap of negated hashes
        self._bottom_set: set[int] = set()
        self._tokens: Counter[int] = Counter()
        self._first_seen: datetime | None = None
        self._last_seen: datetime | None = None

    @property
    def end(self) -> FieldForm:
        return self._end

    @property
    def day(self) -> date:
        return self._day

    @property
    def count(self) -> int:
        return self._count

    def add(self, row: IdentifierRow) -> None:
        if row.field_ref != self._end.field_ref or row.form != self._end.form:
            msg = "identifier row belongs to another field or form"
            raise ValueError(msg)
        self._count += 1
        self._shapes[row.shape] += 1
        self._lengths[len(row.shape)] += 1
        encoded = row.token.encode("utf-8")
        self._minhash.update(encoded)
        self._hll.update(encoded)
        digest = token_hash(row.token)
        self._tokens[digest] += 1
        self._offer_bottom(digest)
        self._first_seen = _min_time(self._first_seen, row.observed_at)
        self._last_seen = _max_time(self._last_seen, row.observed_at)

    def add_all(self, rows: Iterable[IdentifierRow]) -> None:
        for row in rows:
            self.add(row)

    def _offer_bottom(self, digest: int) -> None:
        if digest in self._bottom_set:
            return
        if len(self._bottom_heap) < self._k:
            heapq.heappush(self._bottom_heap, -digest)
            self._bottom_set.add(digest)
            return
        largest = -self._bottom_heap[0]
        if digest < largest:
            heapq.heapreplace(self._bottom_heap, -digest)
            self._bottom_set.discard(largest)
            self._bottom_set.add(digest)

    def build(self) -> DaySketch:
        return DaySketch(
            end=self._end,
            day=self._day,
            count=self._count,
            shape_counts=dict(self._shapes),
            length_counts=dict(self._lengths),
            minhash=serialize_minhash(self._minhash),
            hll=serialize_hll(self._hll),
            bottom_k=tuple(sorted(self._bottom_set)),
            top_token_counts=_trim_top(self._tokens, self._top_keep),
            first_seen=self._first_seen,
            last_seen=self._last_seen,
            sketch_version=SKETCH_VERSION,
        )


# -- merging ----------------------------------------------------------------------------------


def sketch_compatible(sketch: DaySketch, settings: ProfilerSettings) -> bool:
    """Whether a stored sketch was built with the current version, permutation count and
    HyperLogLog size, so it can be merged with new ones (ADR 0047: otherwise it is rebuilt)."""
    return (
        sketch.sketch_version == SKETCH_VERSION
        and len(sketch.minhash) == settings.minhash_permutations * _U64_BE.itemsize
        and len(sketch.hll) == 1 << HLL_PRECISION
    )


def _merged_minhash(parts: Iterable[bytes]) -> bytes:
    merged: MinHash | None = None
    for data in parts:
        part = deserialize_minhash(data)
        if merged is None:
            merged = part
            continue
        if len(merged) != len(part):
            msg = "cannot merge MinHash signatures of different lengths"
            raise ValueError(msg)
        merged.merge(part)
    if merged is None:
        msg = "nothing to merge"
        raise ValueError(msg)
    return serialize_minhash(merged)


def _merged_hll(parts: Iterable[bytes]) -> bytes:
    merged: HyperLogLog | None = None
    for data in parts:
        part = deserialize_hll(data)
        if merged is None:
            merged = part
            continue
        if merged.p != part.p:
            msg = "cannot merge HyperLogLogs of different precision"
            raise ValueError(msg)
        merged.merge(part)
    if merged is None:
        msg = "nothing to merge"
        raise ValueError(msg)
    return serialize_hll(merged)


def _add_str_counts(a: Mapping[str, int], b: Mapping[str, int]) -> dict[str, int]:
    total: Counter[str] = Counter(a)
    total.update(b)
    return dict(total)


def merge_day(
    existing: DaySketch | None, new: DaySketch, *, settings: ProfilerSettings
) -> DaySketch:
    """Fold ``new`` (a sketch of rows not yet in ``existing``) into the stored day sketch. With
    no stored sketch, ``new`` is the result. ``settings`` gives the sample capacity
    (``bottom_k``) and the top-count trim (``2 * concentration_top_n``): a sketch does not record
    them, and a sample smaller than the capacity is the whole token set, so the merge needs the
    capacity to know how many of the union to keep. The result carries :data:`SKETCH_VERSION`.
    """
    if existing is None:
        return new
    if existing.end != new.end or existing.day != new.day:
        msg = "cannot merge day sketches of different ends or days"
        raise ValueError(msg)
    k = settings.bottom_k
    keep = 2 * settings.concentration_top_n
    return DaySketch(
        end=new.end,
        day=new.day,
        count=existing.count + new.count,
        shape_counts=_add_str_counts(existing.shape_counts, new.shape_counts),
        length_counts=dict(_add_counts(existing.length_counts, new.length_counts)),
        minhash=_merged_minhash((existing.minhash, new.minhash)),
        hll=_merged_hll((existing.hll, new.hll)),
        bottom_k=_smallest_k([*existing.bottom_k, *new.bottom_k], k),
        top_token_counts=_trim_top(
            _add_counts(existing.top_token_counts, new.top_token_counts), keep
        ),
        first_seen=_min_time(existing.first_seen, new.first_seen),
        last_seen=_max_time(existing.last_seen, new.last_seen),
        sketch_version=SKETCH_VERSION,
    )


def merge_window(sketches: Iterable[DaySketch], *, settings: ProfilerSettings) -> WindowSketch:
    """Merge the day sketches of one end into the window sketch; the same merges as
    :func:`merge_day` across days. Raises :class:`ValueError` on mixed ends or no sketches."""
    ordered = sorted(sketches, key=lambda sketch: sketch.day)
    if not ordered:
        msg = "a window needs at least one day sketch"
        raise ValueError(msg)
    end = ordered[0].end
    if any(sketch.end != end for sketch in ordered):
        msg = "cannot merge day sketches of different ends into one window"
        raise ValueError(msg)
    shapes: dict[str, int] = {}
    lengths: Counter[int] = Counter()
    tokens: Counter[int] = Counter()
    bottom: list[int] = []
    first: datetime | None = None
    last: datetime | None = None
    k = settings.bottom_k
    keep = 2 * settings.concentration_top_n
    for sketch in ordered:
        shapes = _add_str_counts(shapes, sketch.shape_counts)
        lengths.update(sketch.length_counts)
        tokens.update(sketch.top_token_counts)
        bottom.extend(sketch.bottom_k)
        first = _min_time(first, sketch.first_seen)
        last = _max_time(last, sketch.last_seen)
    return WindowSketch(
        end=end,
        days=tuple(sketch.day for sketch in ordered),
        count=sum(sketch.count for sketch in ordered),
        shape_counts=shapes,
        length_counts=dict(lengths),
        minhash=_merged_minhash(sketch.minhash for sketch in ordered),
        hll=_merged_hll(sketch.hll for sketch in ordered),
        bottom_k=_smallest_k(bottom, k),
        top_token_counts=_trim_top(tokens, keep),
        first_seen=first,
        last_seen=last,
    )


# -- estimates --------------------------------------------------------------------------------


def distinct_estimate(sketch: DaySketch | WindowSketch, *, k: int | None = None) -> int:
    """Distinct tokens of the sketch: exactly ``len(bottom_k)`` while the sample is not
    saturated (fewer than ``k`` hashes, ``k`` being ``ProfilerSettings.bottom_k``), the rounded
    HyperLogLog estimate once it is; 0 for an empty sketch. Without ``k`` the HyperLogLog is
    used whenever the sample is non-empty."""
    if sketch.count == 0:
        return 0
    if k is not None and len(sketch.bottom_k) < k:
        return len(sketch.bottom_k)
    hll = deserialize_hll(sketch.hll)
    with warnings.catch_warnings():
        # datasketch warns when the estimate sits near its small-range correction threshold;
        # the estimate is still the best available and q tolerates its error.
        warnings.simplefilter("ignore")
        return round(float(hll.count()))


def jaccard_estimate(a: DaySketch | WindowSketch, b: DaySketch | WindowSketch) -> float:
    """MinHash Jaccard estimate of the two token sets."""
    return float(deserialize_minhash(a.minhash).jaccard(deserialize_minhash(b.minhash)))


def containment_estimate(
    a: DaySketch | WindowSketch, b: DaySketch | WindowSketch, *, k: int | None = None
) -> tuple[float, float]:
    """``(containment of A in B, containment of B in A)`` from the bottom-k samples.

    With ``S_A`` and ``S_B`` the samples and ``k`` the sample capacity
    (``ProfilerSettings.bottom_k``), the ``k`` smallest hashes of their union (``U_k``) are
    exactly the ``k`` smallest of the union of the full sets, and an element of ``U_k`` is in
    ``A`` exactly when it is in ``S_A``. So ``|U_k and S_A and S_B| / |U_k and S_A|`` is an
    unbiased estimate of ``|A and B| / |A|``, exact whenever the union of the full sets has at
    most ``k`` elements. Without ``k`` the larger sample size stands in, which keeps the
    estimate unbiased but not exact for two ends smaller than the capacity. Empty ends give 0.
    """
    set_a = set(a.bottom_k)
    set_b = set(b.bottom_k)
    if k is None:
        k = max(len(set_a), len(set_b))
    if k <= 0 or not (set_a or set_b):
        return (0.0, 0.0)
    union_k = heapq.nsmallest(k, set_a | set_b)
    in_a = sum(1 for value in union_k if value in set_a)
    in_b = sum(1 for value in union_k if value in set_b)
    both = sum(1 for value in union_k if value in set_a and value in set_b)
    return (both / in_a if in_a else 0.0, both / in_b if in_b else 0.0)
