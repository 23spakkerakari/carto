"""Candidate generation (spec 9.2; ADR 0048).

1. A MinHash LSH Ensemble over the window sketches of every candidate end (profiles that pass
   ``q`` whose form carries an identifier, :func:`is_identifier_form`: ``date``, ``amount`` and
   ``phonetic.k`` forms are composite components of spec 9.5, never exact keys) is queried once
   per end for other ends whose estimated containment is at least ``containment_threshold``. A
   pair is kept when its ends belong to different systems or to different templates of one
   system; an end is never paired with itself or with another form of the same field (those
   overlap by construction).
2. Each LSH pair is ranked by the containment estimated from the bottom-k samples
   (``max(c_ab, c_ba)``; :func:`default_containment` is the exact bottom-k estimator, the
   profiler package offers the same over its sketches) so the per-run cap keeps the most
   promising pairs.
3. Bridge pairs come first: every pair of identifier ends carried together by at least
   ``bridge_min_events`` events (:meth:`EventStore.cooccurrences`), with the co-occurrence row
   kept so verification can read the pairing consistency of 9.4. A bridge end need not pass
   ``q`` or ``min_distinct``: batch keys have few distinct values by nature (ADR 0026) and the
   co-occurrence is the evidence; the role follows the fan-out.
4. The cap ``max_candidates_per_run`` bounds bridges plus LSH pairs; pairs already decided
   (accepted, rejected) are appended after the cap with source ``repropose`` so a pass re-checks
   staleness and re-proposal without taking a slot from an undecided pair (ADR 0048).

Ordering is deterministic: bridges by co-occurring events descending, LSH pairs by estimated
containment descending, ties by :class:`FieldForm` order. The MinHash bytes are opaque sketch
serialisations owned by the profiler; :data:`MinHashDecoder` is how they become ``datasketch``
objects and :func:`default_minhash_decoder` reads the profiler's big-endian ``uint64``
``hashvalues`` encoding.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np
from datasketch import MinHash, MinHashLSHEnsemble

from carto_core.linker.model import Candidate
from carto_core.store.model import FieldForm
from carto_schema.forms import token_domain

if TYPE_CHECKING:
    from carto_core.profiler.model import FieldProfile, WindowSketch
    from carto_core.settings import LinkerSettings, ProfilerSettings
    from carto_core.store.model import CooccurrenceRow, EventStore, Window

__all__ = [
    "LSH_PARTITIONS",
    "CandidateError",
    "CandidateGenerator",
    "CandidateSet",
    "ContainmentFn",
    "MinHashDecoder",
    "default_containment",
    "default_minhash_decoder",
    "is_identifier_form",
    "pair_allowed",
]

ContainmentFn = Callable[["WindowSketch", "WindowSketch"], tuple[float, float]]
"""``(a, b) -> (c_ab, c_ba)`` estimated from two window sketches."""

MinHashDecoder = Callable[[bytes], MinHash]
"""Turns the profiler's serialised MinHash into a ``datasketch`` object."""

LSH_PARTITIONS: Final = 16
"""``num_part`` of the LSH Ensemble: partitions by set size (datasketch's recommended default)."""

_MINHASH_SEED: Final = 1
_MINHASH_SCHEME: Final = "affine32"


class CandidateError(Exception):
    """The candidate index could not be built. The message never carries sketch content."""


def default_containment(
    a: WindowSketch, b: WindowSketch, *, k: int | None = None
) -> tuple[float, float]:
    """The bottom-k containment estimator. ``k`` is the profiler's sample size: a sample with
    fewer hashes than ``k`` is the whole set, and when both are the result is exact. Otherwise,
    among the ``k`` smallest hashes of the union (every one of which is in the sample of the
    set it belongs to), the share of A's that B also holds, and the reverse."""
    sample_a = set(a.bottom_k)
    sample_b = set(b.bottom_k)
    if not sample_a or not sample_b:
        return 0.0, 0.0
    if k is not None and len(sample_a) < k and len(sample_b) < k:
        both_exact = len(sample_a & sample_b)
        return both_exact / len(sample_a), both_exact / len(sample_b)
    k = k if k is not None else max(len(sample_a), len(sample_b))
    union_k = sorted(sample_a | sample_b)[:k]
    in_a = sum(1 for h in union_k if h in sample_a)
    in_b = sum(1 for h in union_k if h in sample_b)
    both = sum(1 for h in union_k if h in sample_a and h in sample_b)
    return (both / in_a if in_a else 0.0, both / in_b if in_b else 0.0)


def default_minhash_decoder(data: bytes) -> MinHash:
    """``hashvalues`` as big-endian ``uint64`` (the profiler's ``serialize_minhash`` encoding;
    the 32-bit scheme's values fit) with datasketch's default seed and scheme."""
    if not data or len(data) % 8:
        msg = "minhash bytes are not a whole number of 64-bit hash values"
        raise CandidateError(msg)
    values = np.frombuffer(data, dtype=">u8").astype(np.uint32)
    return MinHash(
        num_perm=len(values), seed=_MINHASH_SEED, hashvalues=values, scheme=_MINHASH_SCHEME
    )


def is_identifier_form(form: str) -> bool:
    """Whether a form carries an identifier: the forms of the ``id`` token domain of spec 8.4
    (``raw``, ``norm``, ``alnum``, ``digits.k``). ``date``, ``amount`` and ``phonetic.k`` are
    composite components (spec 9.5), never exact keys or bridge ends: two systems' totals
    matching is not a key link, and a bridge through an amount would glue entities together
    through their prices."""
    try:
        return token_domain(form) == "id"
    except ValueError:
        return False


def _template_of(end: FieldForm) -> str:
    return end.field_ref.split("/", 2)[1] if end.field_ref.count("/") >= 1 else ""


def pair_allowed(a: FieldForm, b: FieldForm) -> bool:
    """Spec 9.2 step 2: different systems, or different templates of one system; never the
    same field in any form."""
    if a.field_ref == b.field_ref:
        return False
    if a.system_id != b.system_id:
        return True
    return _template_of(a) != _template_of(b)


def _ordered(a: FieldForm, b: FieldForm) -> tuple[FieldForm, FieldForm]:
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """What one pass verifies, in order, plus the co-occurrence rows of the bridge pairs."""

    candidates: tuple[Candidate, ...]
    cooccurrences: Mapping[tuple[FieldForm, FieldForm], CooccurrenceRow]
    lsh_pairs: int
    """LSH pairs found before the cap."""
    bridge_pairs: int
    truncated: int
    """Pairs the cap dropped; they wait for the next pass."""


class CandidateGenerator:
    """See the module docstring."""

    __slots__ = ("_containment", "_decode", "_profiler", "_settings")

    def __init__(
        self,
        settings: LinkerSettings,
        profiler_settings: ProfilerSettings,
        *,
        containment: ContainmentFn = default_containment,
        decode_minhash: MinHashDecoder = default_minhash_decoder,
    ) -> None:
        self._settings = settings
        self._profiler = profiler_settings
        self._containment: ContainmentFn = (
            functools.partial(default_containment, k=profiler_settings.bottom_k)
            if containment is default_containment
            else containment
        )
        self._decode = decode_minhash

    def generate(
        self,
        tenant_id: str,
        *,
        store: EventStore,
        window: Window,
        profiles: Sequence[FieldProfile],
        sketch_of: Callable[[FieldForm], WindowSketch | None],
        decided: Iterable[tuple[FieldForm, FieldForm]] = (),
    ) -> CandidateSet:
        ends: dict[FieldForm, FieldProfile] = {
            profile.end: profile
            for profile in profiles
            if profile.is_candidate and is_identifier_form(profile.end.form)
        }
        sketches: dict[FieldForm, WindowSketch] = {}
        for end in ends:
            sketch = sketch_of(end)
            if sketch is not None:
                sketches[end] = sketch

        bridges, rows = self._bridge_pairs(tenant_id, store, window)
        taken = {(c.a, c.b) for c in bridges}
        lsh = [c for c in self._lsh_pairs(ends, sketches) if (c.a, c.b) not in taken]
        ordered = [*bridges, *lsh]
        cap = self._settings.max_candidates_per_run
        kept = ordered[:cap]
        taken = {(c.a, c.b) for c in kept}
        for first, second in decided:
            a, b = _ordered(first, second)
            if (a, b) in taken:
                continue
            taken.add((a, b))
            kept.append(Candidate(a=a, b=b, estimated_containment=0.0, source="repropose"))
        return CandidateSet(
            candidates=tuple(kept),
            cooccurrences=rows,
            lsh_pairs=len(lsh),
            bridge_pairs=len(bridges),
            truncated=max(0, len(ordered) - cap),
        )

    def _bridge_pairs(
        self, tenant_id: str, store: EventStore, window: Window
    ) -> tuple[list[Candidate], dict[tuple[FieldForm, FieldForm], CooccurrenceRow]]:
        """Every pair of identifier ends carried together by at least ``bridge_min_events``
        events. A bridge needs neither end to pass ``q`` or ``min_distinct``: a batch key (a
        manifest, a nightly file) has few distinct values by nature (ADR 0026) and the
        co-occurrence itself is the evidence; the role then follows the fan-out (spec 9.3)."""
        rows: dict[tuple[FieldForm, FieldForm], CooccurrenceRow] = {}
        for row in store.cooccurrences(
            tenant_id, window, min_events=self._settings.bridge_min_events
        ):
            if row.a.field_ref == row.b.field_ref:
                continue
            if not (is_identifier_form(row.a.form) and is_identifier_form(row.b.form)):
                continue
            rows[_ordered(row.a, row.b)] = row
        ranked = sorted(rows.items(), key=lambda item: (-item[1].events, item[0]))
        candidates = [
            Candidate(a=key[0], b=key[1], estimated_containment=1.0, source="bridge")
            for key, _row in ranked
        ]
        return candidates, rows

    def _lsh_pairs(
        self, ends: Mapping[FieldForm, FieldProfile], sketches: Mapping[FieldForm, WindowSketch]
    ) -> list[Candidate]:
        if len(sketches) < 2:
            return []
        try:
            index = MinHashLSHEnsemble(
                threshold=self._settings.containment_threshold,
                num_perm=self._profiler.minhash_permutations,
                num_part=LSH_PARTITIONS,
            )
            decoded = {end: self._decode(sketch.minhash) for end, sketch in sketches.items()}
            sizes = {end: max(1, ends[end].distinct) for end in sketches}
            index.index((end, decoded[end], sizes[end]) for end in sorted(sketches))
            pairs: set[tuple[FieldForm, FieldForm]] = set()
            for end in sorted(sketches):
                for other in index.query(decoded[end], sizes[end]):
                    if not isinstance(other, FieldForm) or not pair_allowed(end, other):
                        continue
                    pairs.add(_ordered(end, other))
        except ValueError as exc:
            msg = f"candidate index could not be built: {type(exc).__name__}"
            raise CandidateError(msg) from exc
        candidates: list[Candidate] = []
        for a, b in pairs:
            c_ab, c_ba = self._containment(sketches[a], sketches[b])
            candidates.append(
                Candidate(a=a, b=b, estimated_containment=max(c_ab, c_ba), source="lsh")
            )
        candidates.sort(key=lambda c: (-c.estimated_containment, c.a, c.b))
        return candidates
