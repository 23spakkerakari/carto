"""Candidate generation (spec 9.2; ADR 0048): the bottom-k containment estimator, the MinHash
decoder, the pairing rules (different systems or different templates, never the same field),
LSH over window sketches, bridge pairs first with their co-occurrence rows, the per-run cap,
decided pairs appended after the cap, and deterministic ordering."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from datasketch import MinHash

from carto_common.ids import derive_ulid
from carto_core.linker.candidates import (
    CandidateError,
    CandidateGenerator,
    default_containment,
    default_minhash_decoder,
    is_identifier_form,
    pair_allowed,
)
from carto_core.profiler.model import FieldProfile, ShapeShare, WindowSketch
from carto_core.settings import LinkerSettings, ProfilerSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import FieldForm, Window
from carto_schema.event import CanonicalEvent

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
WINDOW = Window(NOW - timedelta(days=7), NOW + timedelta(days=1))
BASE_MS = 1_790_000_000_000
PROFILER = ProfilerSettings()


def token(value: str) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return f"t1.{body[:22]}"


def token_hash(value: str) -> int:
    return int.from_bytes(hashlib.sha256(token(value).encode()).digest()[:8], "big")


def sketch(end: FieldForm, values: Iterable[str]) -> WindowSketch:
    values = list(values)
    minhash = MinHash(num_perm=PROFILER.minhash_permutations)
    for value in values:
        minhash.update(token(value).encode())
    bottom = tuple(sorted(token_hash(value) for value in values)[: PROFILER.bottom_k])
    return WindowSketch(
        end=end,
        days=(date(2026, 10, 17),),
        count=len(values),
        shape_counts={},
        length_counts={},
        minhash=np.asarray(minhash.hashvalues, dtype=np.uint64).astype(">u8").tobytes(),
        hll=b"",
        bottom_k=bottom,
        top_token_counts={},
        first_seen=None,
        last_seen=None,
    )


def profile(end: FieldForm, distinct: int, *, candidate: bool = True) -> FieldProfile:
    return FieldProfile(
        end=end,
        count=distinct,
        distinct=distinct,
        null_rate=0.0,
        shapes=(ShapeShare("9999", 1.0),),
        length_stats=None,
        top_share=0.01,
        quality=0.9 if candidate else 0.1,
        is_candidate=candidate,
        first_seen=None,
        last_seen=None,
    )


def event(
    seed: str,
    system_id: str,
    template_id: str,
    observed_at: datetime,
    identifiers: Sequence[tuple[str, str, str, str]],
) -> CanonicalEvent:
    """``identifiers`` are ``(field, form, value, shape)``; the token is the value's, so the
    same value in two forms (``raw`` and ``digits.0``) links as it does at the edge."""
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(BASE_MS, seed),
        "system_id": system_id,
        "source_id": f"src_{system_id.removeprefix('sys_')}",
        "template_id": template_id,
        "observed_at": observed_at,
        "ingested_at": observed_at,
        "attributes": {},
        "identifiers": [
            {"field": field, "form": form, "token": token(value), "shape": shape, "len": len(value)}
            for field, form, value, shape in identifiers
        ],
    }
    return CanonicalEvent.model_validate(data)


A = FieldForm("sys_a/tpl_a/order_id", "raw")
A_ALNUM = FieldForm("sys_a/tpl_a/order_id", "alnum")
A_OTHER_TEMPLATE = FieldForm("sys_a/tpl_a2/order_id_copy", "raw")
A_SAME_TEMPLATE = FieldForm("sys_a/tpl_a/merchant_ref", "raw")
B = FieldForm("sys_b/tpl_b/order_ref", "digits.0")
C = FieldForm("sys_c/tpl_c/noise", "raw")

ORDERS = [f"{n:04d}" for n in range(1, 501)]


class Source:
    def __init__(self, sketches: Mapping[FieldForm, WindowSketch]) -> None:
        self.sketches = dict(sketches)

    def __call__(self, end: FieldForm) -> WindowSketch | None:
        return self.sketches.get(end)


def generator(**over: object) -> CandidateGenerator:
    return CandidateGenerator(LinkerSettings(**over), PROFILER)  # type: ignore[arg-type]


# -- estimators ---------------------------------------------------------------------------------


def test_default_containment_is_exact_on_small_sets() -> None:
    a = sketch(A, ORDERS[:100])
    b = sketch(B, ORDERS[50:150])
    k = PROFILER.bottom_k
    assert default_containment(a, b, k=k) == (0.5, 0.5)
    subset = sketch(B, ORDERS[:50])
    assert default_containment(a, subset, k=k) == (0.5, 1.0)
    assert default_containment(a, sketch(C, []), k=k) == (0.0, 0.0)
    estimate = default_containment(a, b)
    assert 0.3 < estimate[0] < 0.7, "without k the estimator is still in range"


def test_default_containment_stays_close_on_sampled_sets() -> None:
    big_a = [f"a{n}" for n in range(20_000)]
    big_b = big_a[5_000:] + [f"b{n}" for n in range(5_000)]  # 75% of A in B, 75% of B in A
    c_ab, c_ba = default_containment(sketch(A, big_a), sketch(B, big_b))
    assert c_ab == pytest.approx(0.75, abs=0.05)
    assert c_ba == pytest.approx(0.75, abs=0.05)


def test_minhash_decoder_round_trip_and_rejects_odd_lengths() -> None:
    original = MinHash(num_perm=64)
    for value in ORDERS:
        original.update(value.encode())
    encoded = np.asarray(original.hashvalues, dtype=np.uint64).astype(">u8").tobytes()
    decoded = default_minhash_decoder(encoded)
    assert decoded.jaccard(original) == 1.0
    with pytest.raises(CandidateError):
        default_minhash_decoder(b"abcdefg")
    with pytest.raises(CandidateError):
        default_minhash_decoder(b"")


# -- pairing rules -------------------------------------------------------------------------------


def test_pair_allowed() -> None:
    assert pair_allowed(A, B)
    assert pair_allowed(A, A_OTHER_TEMPLATE)
    assert not pair_allowed(A, A)
    assert not pair_allowed(A, A_ALNUM)
    assert not pair_allowed(A, A_SAME_TEMPLATE)


# -- generation ----------------------------------------------------------------------------------


def test_lsh_pairs_overlapping_ends_across_systems_and_templates() -> None:
    sketches = {
        A: sketch(A, ORDERS),
        A_ALNUM: sketch(A_ALNUM, ORDERS),
        A_OTHER_TEMPLATE: sketch(A_OTHER_TEMPLATE, ORDERS[:300]),
        A_SAME_TEMPLATE: sketch(A_SAME_TEMPLATE, ORDERS),
        B: sketch(B, ORDERS[:450] + [f"x{n}" for n in range(50)]),
        C: sketch(C, [f"c{n}" for n in range(500)]),
    }
    profiles = [profile(end, 500) for end in sketches]
    result = generator().generate(
        "default",
        store=InMemoryEventStore(),
        window=WINDOW,
        profiles=profiles,
        sketch_of=Source(sketches),
    )
    pairs = {(c.a, c.b): c for c in result.candidates}
    assert (A, B) in pairs
    assert pairs[(A, B)].estimated_containment == pytest.approx(0.9, abs=0.02)
    assert pairs[(A, B)].source == "lsh"
    assert (A_ALNUM, B) in pairs
    assert (A, A_OTHER_TEMPLATE) in pairs, "different templates of one system pair up"
    assert (A, A_ALNUM) not in pairs, "two forms of one field never pair"
    assert (A, A_SAME_TEMPLATE) not in pairs, "same template without co-occurrence is not a pair"
    assert not any(C in key for key in pairs), "a disjoint end has no partner"
    assert result.bridge_pairs == 0 and result.truncated == 0
    assert result.lsh_pairs == len(result.candidates)
    containments = [c.estimated_containment for c in result.candidates]
    assert containments == sorted(containments, reverse=True)


def test_bridges_come_first_with_their_rows() -> None:
    store = InMemoryEventStore()
    store.add_events(
        event(
            f"o{n}",
            "sys_a",
            "tpl_a",
            NOW - timedelta(hours=n % 48),
            [("order_id", "raw", ORDERS[n], "9999"), ("merchant_ref", "raw", f"m{n}", "A9")],
        )
        for n in range(40)
    )
    sketches = {
        A: sketch(A, ORDERS),
        A_SAME_TEMPLATE: sketch(A_SAME_TEMPLATE, [f"m{n}" for n in range(40)]),
        B: sketch(B, ORDERS),
    }
    profiles = [profile(end, 500) for end in sketches]
    result = generator().generate(
        "default", store=store, window=WINDOW, profiles=profiles, sketch_of=Source(sketches)
    )
    first = result.candidates[0]
    assert first.source == "bridge"
    assert (first.a, first.b) == (A_SAME_TEMPLATE, A) or (first.a, first.b) == (A, A_SAME_TEMPLATE)
    assert first.estimated_containment == 1.0
    assert (first.a, first.b) in result.cooccurrences
    assert result.cooccurrences[(first.a, first.b)].events == 40
    assert result.bridge_pairs == 1
    assert {(c.a, c.b) for c in result.candidates[1:]} == {(A, B)}


def test_bridge_needs_the_minimum_events_and_candidate_ends() -> None:
    store = InMemoryEventStore()
    store.add_events(
        event(
            f"o{n}",
            "sys_a",
            "tpl_a",
            NOW,
            [("order_id", "raw", ORDERS[n], "9999"), ("merchant_ref", "raw", f"m{n}", "A9")],
        )
        for n in range(10)
    )
    sketches = {A: sketch(A, ORDERS), A_SAME_TEMPLATE: sketch(A_SAME_TEMPLATE, ["m1"])}
    few = generator().generate(
        "default",
        store=store,
        window=WINDOW,
        profiles=[profile(end, 500) for end in sketches],
        sketch_of=Source(sketches),
    )
    assert few.bridge_pairs == 0
    enough = generator(bridge_min_events=5).generate(
        "default",
        store=store,
        window=WINDOW,
        profiles=[profile(A, 500), profile(A_SAME_TEMPLATE, 500, candidate=False)],
        sketch_of=Source(sketches),
    )
    assert enough.bridge_pairs == 1, "an end below q still bridges (batch keys, ADR 0026)"
    bridged = generator(bridge_min_events=5).generate(
        "default",
        store=store,
        window=WINDOW,
        profiles=[profile(end, 500) for end in sketches],
        sketch_of=Source(sketches),
    )
    assert bridged.bridge_pairs == 1


def test_cap_truncates_but_decided_pairs_are_appended() -> None:
    sketches = {
        A: sketch(A, ORDERS),
        A_OTHER_TEMPLATE: sketch(A_OTHER_TEMPLATE, ORDERS[:400]),
        B: sketch(B, ORDERS[:450]),
    }
    profiles = [profile(end, 500) for end in sketches]
    decided = [(C, A), (B, A)]
    result = generator(max_candidates_per_run=1).generate(
        "default",
        store=InMemoryEventStore(),
        window=WINDOW,
        profiles=profiles,
        sketch_of=Source(sketches),
        decided=decided,
    )
    assert result.truncated == 2, "three LSH pairs, one slot"
    assert len(result.candidates) == 3
    assert result.candidates[0].source == "lsh"
    assert result.candidates[0].estimated_containment >= 0.89
    reproposed = result.candidates[1:]
    assert all(c.source == "repropose" for c in reproposed)
    assert [(c.a, c.b) for c in reproposed] == [(A, C), (A, B)], "FieldForm order, given order"


def test_non_candidate_or_unsketched_ends_are_skipped_and_order_is_stable() -> None:
    sketches = {A: sketch(A, ORDERS), B: sketch(B, ORDERS), C: sketch(C, ORDERS)}
    profiles = [profile(A, 500), profile(B, 500), profile(C, 500, candidate=False)]
    first = generator().generate(
        "default",
        store=InMemoryEventStore(),
        window=WINDOW,
        profiles=profiles,
        sketch_of=Source(sketches),
    )
    assert {(c.a, c.b) for c in first.candidates} == {(A, B)}
    second = generator().generate(
        "default",
        store=InMemoryEventStore(),
        window=WINDOW,
        profiles=profiles,
        sketch_of=Source({A: sketches[A], B: sketches[B]}),
    )
    assert second.candidates == first.candidates
    none = generator().generate(
        "default",
        store=InMemoryEventStore(),
        window=WINDOW,
        profiles=profiles,
        sketch_of=Source({}),
    )
    assert none.candidates == ()


def test_mismatched_permutations_raise_candidate_error() -> None:
    sketches = {A: sketch(A, ORDERS), B: sketch(B, ORDERS)}
    wrong = CandidateGenerator(LinkerSettings(), ProfilerSettings(minhash_permutations=64))
    with pytest.raises(CandidateError):
        wrong.generate(
            "default",
            store=InMemoryEventStore(),
            window=WINDOW,
            profiles=[profile(A, 500), profile(B, 500)],
            sketch_of=Source(sketches),
        )


# -- identifier forms only (spec 9.5: amounts, dates and phonetic codes are composite components)


@pytest.mark.parametrize(
    ("form", "identifier"),
    [
        ("raw", True),
        ("norm", True),
        ("alnum", True),
        ("digits.0", True),
        ("digits.2", True),
        ("date", False),
        ("amount", False),
        ("phonetic.0", False),
        ("phonetic.7", False),
        ("bogus", False),
    ],
)
def test_is_identifier_form(form: str, identifier: bool) -> None:
    assert is_identifier_form(form) == identifier


def test_amount_date_and_phonetic_ends_are_never_exact_candidates() -> None:
    amount_a = FieldForm("sys_a/tpl_a/total", "amount")
    amount_b = FieldForm("sys_b/tpl_b/amount", "amount")
    date_a = FieldForm("sys_a/tpl_a/created", "date")
    date_b = FieldForm("sys_b/tpl_b/created", "date")
    name_a = FieldForm("sys_a/tpl_a/name", "phonetic.0")
    name_b = FieldForm("sys_b/tpl_b/name", "phonetic.0")
    sketches = {
        A: sketch(A, ORDERS),
        B: sketch(B, ORDERS),
        amount_a: sketch(amount_a, ORDERS),
        amount_b: sketch(amount_b, ORDERS),
        date_a: sketch(date_a, ORDERS),
        date_b: sketch(date_b, ORDERS),
        name_a: sketch(name_a, ORDERS),
        name_b: sketch(name_b, ORDERS),
    }
    result = generator().generate(
        "default",
        store=InMemoryEventStore(),
        window=WINDOW,
        profiles=[profile(end, 500) for end in sketches],
        sketch_of=Source(sketches),
    )
    assert {(c.a, c.b) for c in result.candidates} == {(A, B)}


def test_bridges_skip_non_identifier_ends_but_accept_low_cardinality_ones() -> None:
    store = InMemoryEventStore()
    manifest = FieldForm("sys_a/tpl_a/manifest_id", "raw")
    total = FieldForm("sys_a/tpl_a/total", "amount")
    store.add_events(
        event(
            f"s{n}",
            "sys_a",
            "tpl_a",
            NOW - timedelta(hours=n % 48),
            [
                ("order_id", "raw", ORDERS[n], "9999"),
                ("manifest_id", "raw", f"M{n % 7}", "A9"),
                ("total", "amount", f"{n}.00", "99.99"),
            ],
        )
        for n in range(60)
    )
    sketches = {A: sketch(A, ORDERS)}
    profiles = [
        profile(A, 500),
        profile(manifest, 7, candidate=False),
        profile(total, 60, candidate=True),
    ]
    result = generator().generate(
        "default", store=store, window=WINDOW, profiles=profiles, sketch_of=Source(sketches)
    )
    pairs = {(c.a, c.b) for c in result.candidates}
    assert (manifest, A) in pairs or (A, manifest) in pairs, "a batch key below q still bridges"
    assert not any(total in pair for pair in pairs), "an amount never bridges an identifier"
    assert result.bridge_pairs == 1
    assert result.cooccurrences[next(iter(pairs))].events == 60
