"""Day sketches, merges and estimates (spec 9.1, 18.1; ADR 0047): merging day sketches equals
sketching the union, bit for bit; containment estimates stay within the sampling tolerance of
exact containment; serialization round-trips without pickle; no token string ever appears in a
sketch's bytes or ``repr``."""

from __future__ import annotations

import base64
import hashlib
import math
import pickle
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from carto_core.profiler.model import DaySketch
from carto_core.profiler.sketches import (
    HLL_PRECISION,
    SKETCH_VERSION,
    DaySketchBuilder,
    containment_estimate,
    deserialize_hll,
    deserialize_minhash,
    distinct_estimate,
    jaccard_estimate,
    merge_day,
    merge_window,
    pack_bottom_k,
    sketch_compatible,
    token_hash,
    unpack_bottom_k,
)
from carto_core.settings import ProfilerSettings
from carto_core.store.model import FieldForm, IdentifierRow

END = FieldForm("sys_a/tpl_1/order_ref", "raw")
OTHER = FieldForm("sys_b/tpl_9/ref", "raw")
DAY = date(2026, 10, 17)
NOON = datetime(2026, 10, 17, 12, tzinfo=UTC)
SMALL = ProfilerSettings(minhash_permutations=16, bottom_k=64, concentration_top_n=3)
SHAPES = ("AA-9999999", "9999", "AAAAAAAA", "99-999")


def tok(i: int) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(str(i).encode()).digest()).decode()[:22]
    return f"t1.{body}"


def row(
    i: int,
    *,
    end: FieldForm = END,
    shape: str | None = None,
    at: datetime | None = None,
    event: int | None = None,
) -> IdentifierRow:
    return IdentifierRow(
        token=tok(i),
        field_ref=end.field_ref,
        form=end.form,
        shape=shape if shape is not None else SHAPES[i % len(SHAPES)],
        event_id=f"ev{event if event is not None else i}",
        system_id=end.system_id,
        observed_at=at if at is not None else NOON + timedelta(seconds=i % 3600),
        stored_at=NOON,
    )


def sketch(
    tokens: list[int], *, day: date = DAY, prof: ProfilerSettings = SMALL, end: FieldForm = END
) -> DaySketch:
    builder = DaySketchBuilder(end, day, prof)
    builder.add_all(row(i, end=end) for i in tokens)
    return builder.build()


def same_content(a: DaySketch, b: DaySketch) -> None:
    assert a.count == b.count
    assert dict(a.shape_counts) == dict(b.shape_counts)
    assert dict(a.length_counts) == dict(b.length_counts)
    assert a.minhash == b.minhash
    assert a.hll == b.hll
    assert a.bottom_k == b.bottom_k
    assert a.first_seen == b.first_seen
    assert a.last_seen == b.last_seen


# -- token hash and serialization -------------------------------------------------------------


def test_token_hash_is_first_8_bytes_of_sha256_big_endian() -> None:
    token = tok(1)
    expected = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
    assert token_hash(token) == expected
    assert 0 <= token_hash(token) < 2**64
    assert token_hash(tok(1)) != token_hash(tok(2))


def test_minhash_round_trip_and_merge_after_deserialize() -> None:
    one = sketch([1, 2, 3])
    two = sketch([3, 4])
    m1 = deserialize_minhash(one.minhash)
    m2 = deserialize_minhash(two.minhash)
    assert len(one.minhash) == SMALL.minhash_permutations * 8
    m1.merge(m2)
    merged = merge_day(one, two, settings=SMALL)
    assert deserialize_minhash(merged.minhash).hashvalues.tolist() == m1.hashvalues.tolist()


def test_hll_round_trip_preserves_estimate() -> None:
    one = sketch(list(range(500)))
    hll = deserialize_hll(one.hll)
    assert hll.p == HLL_PRECISION
    assert len(one.hll) == 1 << HLL_PRECISION
    assert abs(distinct_estimate(one) - 500) <= 50


def test_bottom_k_pack_round_trip_is_big_endian_uint64() -> None:
    values = (1, 2**40, 2**64 - 1)
    packed = pack_bottom_k(values)
    assert packed == b"".join(v.to_bytes(8, "big") for v in values)
    assert unpack_bottom_k(packed) == values
    assert unpack_bottom_k(b"") == ()


@pytest.mark.parametrize(
    ("func", "data"),
    [
        (deserialize_minhash, b""),
        (deserialize_minhash, b"\x00" * 7),
        (deserialize_hll, b""),
        (deserialize_hll, b"\x00" * 100),
        (unpack_bottom_k, b"\x00" * 3),
    ],
)
def test_bad_bytes_are_refused_without_data(func: object, data: bytes) -> None:
    with pytest.raises(ValueError, match="bytes"):
        func(data)  # type: ignore[operator]


def test_sketch_bytes_and_repr_never_hold_a_token() -> None:
    tokens = list(range(50))
    one = sketch(tokens)
    blob = one.minhash + one.hll + pack_bottom_k(one.bottom_k)
    text = repr(one) + repr(merge_window([one], settings=SMALL))
    for i in tokens:
        token = tok(i)
        assert token.encode() not in blob
        assert token[3:].encode() not in blob
        assert token not in text
        assert token[3:] not in text
    assert b"pickle" not in blob
    with pytest.raises(pickle.UnpicklingError):
        pickle.loads(one.minhash)  # noqa: S301 - proves the bytes are not a pickle


# -- building ---------------------------------------------------------------------------------


def test_builder_counts_shapes_lengths_and_times() -> None:
    builder = DaySketchBuilder(END, DAY, SMALL)
    builder.add(row(1, shape="AA-9999999", at=NOON))
    builder.add(row(2, shape="9999", at=NOON - timedelta(hours=1)))
    builder.add(row(2, shape="9999", at=NOON + timedelta(hours=1), event=99))
    built = builder.build()
    assert built.end == END
    assert built.day == DAY
    assert built.count == 3
    assert dict(built.shape_counts) == {"AA-9999999": 1, "9999": 2}
    assert dict(built.length_counts) == {10: 1, 4: 2}
    assert built.first_seen == NOON - timedelta(hours=1)
    assert built.last_seen == NOON + timedelta(hours=1)
    assert built.sketch_version == SKETCH_VERSION
    assert built.bottom_k == tuple(sorted({token_hash(tok(1)), token_hash(tok(2))}))
    assert dict(built.top_token_counts) == {token_hash(tok(1)): 1, token_hash(tok(2)): 2}


def test_builder_refuses_rows_of_another_end_without_naming_it() -> None:
    builder = DaySketchBuilder(END, DAY, SMALL)
    with pytest.raises(ValueError, match="another field or form") as info:
        builder.add(row(1, end=OTHER))
    assert OTHER.field_ref not in str(info.value)
    assert tok(1) not in str(info.value)


def test_bottom_k_is_bounded_and_holds_the_smallest_hashes() -> None:
    tokens = list(range(1000))
    built = sketch(tokens)
    hashes = sorted({token_hash(tok(i)) for i in tokens})
    assert len(built.bottom_k) == SMALL.bottom_k
    assert built.bottom_k == tuple(hashes[: SMALL.bottom_k])
    assert list(built.bottom_k) == sorted(built.bottom_k)


def test_top_token_counts_keep_twice_top_n_heaviest() -> None:
    tokens = [i for i in range(20) for _ in range(i + 1)]  # token i appears i + 1 times
    built = sketch(tokens)
    assert len(built.top_token_counts) == 2 * SMALL.concentration_top_n
    assert set(built.top_token_counts.values()) == set(range(15, 21))
    assert built.count == len(tokens)


def test_empty_builder_gives_an_empty_sketch() -> None:
    built = DaySketchBuilder(END, DAY, SMALL).build()
    assert built.count == 0
    assert built.bottom_k == ()
    assert built.first_seen is None
    assert distinct_estimate(built) == 0
    assert containment_estimate(built, built) == (0.0, 0.0)
    assert sketch_compatible(built, SMALL)


# -- merging ----------------------------------------------------------------------------------

token_lists = st.lists(st.integers(min_value=0, max_value=400), min_size=0, max_size=300)


@given(first=token_lists, second=token_lists)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_merging_day_sketches_equals_sketching_the_union(
    first: list[int], second: list[int]
) -> None:
    merged = merge_day(sketch(first), sketch(second), settings=SMALL)
    whole = sketch(first + second)
    same_content(merged, whole)
    assert merged.end == whole.end
    assert merged.day == whole.day
    # Top counts are trimmed per day, so they are exact only while the distinct count fits.
    if len(set(first + second)) <= 2 * SMALL.concentration_top_n:
        assert dict(merged.top_token_counts) == dict(whole.top_token_counts)
    # Incremental: three merges in sequence give the same bytes as one.
    third = merge_day(
        merge_day(None, sketch(first), settings=SMALL), sketch(second), settings=SMALL
    )
    same_content(third, whole)


@given(parts=st.lists(token_lists, min_size=1, max_size=4))
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_window_merge_equals_sketching_all_days_at_once(parts: list[list[int]]) -> None:
    days = [sketch(part, day=DAY + timedelta(days=i)) for i, part in enumerate(parts)]
    window = merge_window(days, settings=SMALL)
    whole = sketch([i for part in parts for i in part])
    assert window.end == END
    assert window.days == tuple(DAY + timedelta(days=i) for i in range(len(parts)))
    assert window.count == whole.count
    assert dict(window.shape_counts) == dict(whole.shape_counts)
    assert dict(window.length_counts) == dict(whole.length_counts)
    assert window.minhash == whole.minhash
    assert window.hll == whole.hll
    assert window.bottom_k == whole.bottom_k
    assert window.first_seen == whole.first_seen
    assert window.last_seen == whole.last_seen


def test_merge_day_with_nothing_stored_returns_the_new_sketch() -> None:
    new = sketch([1, 2])
    assert merge_day(None, new, settings=SMALL) is new


def test_merge_day_refuses_other_ends_or_days() -> None:
    with pytest.raises(ValueError, match="different ends or days"):
        merge_day(sketch([1]), sketch([2], end=OTHER), settings=SMALL)
    with pytest.raises(ValueError, match="different ends or days"):
        merge_day(sketch([1]), sketch([2], day=DAY + timedelta(days=1)), settings=SMALL)


def test_merge_window_refuses_mixed_ends_and_nothing() -> None:
    with pytest.raises(ValueError, match="different ends"):
        merge_window([sketch([1]), sketch([2], end=OTHER)], settings=SMALL)
    with pytest.raises(ValueError, match="at least one"):
        merge_window([], settings=SMALL)


def test_merge_refuses_different_permutation_counts() -> None:
    other = ProfilerSettings(minhash_permutations=32, bottom_k=64, concentration_top_n=3)
    with pytest.raises(ValueError, match="MinHash"):
        merge_day(sketch([1]), sketch([2], prof=other), settings=SMALL)


def test_sketch_compatible_checks_version_and_sizes() -> None:
    good = sketch([1])
    assert sketch_compatible(good, SMALL)
    assert not sketch_compatible(good, ProfilerSettings(minhash_permutations=32))
    stale = DaySketch(
        end=good.end,
        day=good.day,
        count=good.count,
        shape_counts=good.shape_counts,
        length_counts=good.length_counts,
        minhash=good.minhash,
        hll=good.hll,
        bottom_k=good.bottom_k,
        sketch_version=SKETCH_VERSION + 1,
    )
    assert not sketch_compatible(stale, SMALL)


# -- estimates --------------------------------------------------------------------------------


def _sample_size(a: DaySketch, b: DaySketch, k: int) -> int:
    """How many elements of the union's k smallest belong to A (the estimate's sample)."""
    set_a, set_b = set(a.bottom_k), set(b.bottom_k)
    union_k = sorted(set_a | set_b)[:k]
    return sum(1 for v in union_k if v in set_a)


@given(
    size_a=st.integers(min_value=200, max_value=5000),
    size_b=st.integers(min_value=200, max_value=5000),
    overlap=st.floats(min_value=0.0, max_value=1.0),
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_containment_estimate_within_sampling_tolerance(
    size_a: int, size_b: int, overlap: float
) -> None:
    shared = round(overlap * min(size_a, size_b))
    a_tokens = list(range(size_a))
    b_tokens = list(range(shared)) + list(range(size_a, size_a + size_b - shared))
    prof = ProfilerSettings(minhash_permutations=16, bottom_k=2048)
    a = sketch(a_tokens, prof=prof)
    b = sketch(b_tokens, prof=prof)
    exact_ab = shared / size_a
    exact_ba = shared / size_b
    est_ab, est_ba = containment_estimate(a, b, k=prof.bottom_k)
    if size_a + size_b - shared <= prof.bottom_k:
        assert est_ab == pytest.approx(exact_ab)
        assert est_ba == pytest.approx(exact_ba)
        return
    n_a = _sample_size(a, b, prof.bottom_k)
    n_b = _sample_size(b, a, prof.bottom_k)
    for est, exact, n, size in ((est_ab, exact_ab, n_a, size_a), (est_ba, exact_ba, n_b, size_b)):
        correction = (size - n) / (size - 1)
        tolerance = 5 * math.sqrt(exact * (1 - exact) / n * correction) + 1e-9
        assert abs(est - exact) <= tolerance


def test_containment_is_exact_for_small_sets_and_directional() -> None:
    a = sketch(list(range(40)))
    b = sketch(list(range(20, 60)))
    k = SMALL.bottom_k
    assert containment_estimate(a, b, k=k) == (0.5, 0.5)
    c = sketch(list(range(10)))
    assert containment_estimate(c, a, k=k) == (1.0, 0.25)
    assert containment_estimate(a, c, k=k) == (0.25, 1.0)
    # Without the capacity the estimate is still a proportion of a uniform sample of A.
    est_ab, est_ba = containment_estimate(a, b)
    assert 0.0 <= est_ab <= 1.0 and 0.0 <= est_ba <= 1.0
    assert containment_estimate(a, b, k=0) == (0.0, 0.0)


def test_jaccard_estimate_tracks_overlap() -> None:
    prof = ProfilerSettings(minhash_permutations=256, bottom_k=64)
    a = sketch(list(range(1000)), prof=prof)
    b = sketch(list(range(500, 1500)), prof=prof)
    assert jaccard_estimate(a, a) == 1.0
    assert abs(jaccard_estimate(a, b) - 1 / 3) < 0.12
    assert jaccard_estimate(a, sketch(list(range(5000, 5100)), prof=prof)) < 0.1


def test_distinct_estimate_is_exact_below_the_sample_capacity() -> None:
    # SMALL.bottom_k is 64: 50 distinct tokens (seen several times each) leave the sample
    # unsaturated, so the count is the sample size, not a HyperLogLog reading.
    fifty = sketch([i % 50 for i in range(500)])
    assert len(fifty.bottom_k) == 50
    assert distinct_estimate(fifty, k=SMALL.bottom_k) == 50
    assert distinct_estimate(fifty, k=SMALL.bottom_k) >= ProfilerSettings().min_distinct
    # Exactly k distinct saturates the sample: the HyperLogLog answers from here on.
    full = sketch(list(range(SMALL.bottom_k)))
    assert len(full.bottom_k) == SMALL.bottom_k
    hll_reading = round(float(deserialize_hll(full.hll).count()))
    assert distinct_estimate(full, k=SMALL.bottom_k) == hll_reading
    assert distinct_estimate(sketch(list(range(1000))), k=SMALL.bottom_k) == round(
        float(deserialize_hll(sketch(list(range(1000))).hll).count())
    )
    # Without k the HyperLogLog is used, as before.
    assert distinct_estimate(fifty) == round(float(deserialize_hll(fifty.hll).count()))
    assert distinct_estimate(DaySketchBuilder(END, DAY, SMALL).build(), k=SMALL.bottom_k) == 0


def test_distinct_estimate_tracks_distinct_tokens() -> None:
    repeated = [i % 300 for i in range(3000)]
    built = sketch(repeated)
    assert built.count == 3000
    assert abs(distinct_estimate(built) - 300) <= 30
    both = merge_window([built, sketch(list(range(300, 600)))], settings=SMALL)
    assert abs(distinct_estimate(both) - 600) <= 60
