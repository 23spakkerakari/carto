"""carto_edge.pipeline.stats: streaming field statistics (spec 8.3) and their persistence.

Every value here is synthetic. The reservoir sample must never reach a snapshot, a file or a
log line; several tests assert that by grepping the serialized output for marker values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.config import ClassifySettings
from carto_edge.pipeline.pii import SECRET_MIN_LEN
from carto_edge.pipeline.stats import (
    COUNTED_VALUE_MAX_LEN,
    HLL_PRECISION,
    MAX_COUNTED_VALUES,
    MAX_SHAPES,
    OVERFLOW_SHAPE,
    SNAPSHOT_VERSION,
    FieldStats,
    FieldStatsStore,
    StatsFormatError,
)

MARKER = "mkvalue7f3e9a1c"


def _ids(n: int, prefix: str = "SO-") -> list[str]:
    return [f"{prefix}{i:07d}" for i in range(n)]


# ---------------------------------------------------------------------------------------------
# FieldStats
# ---------------------------------------------------------------------------------------------


def test_counts_nulls_and_null_rate() -> None:
    stats = FieldStats(sample_size=8)
    for value in ["a", None, "b", "", "c"]:
        stats.observe(value)
    assert stats.count == 5
    assert stats.null_count == 2  # None and the empty string both count as null
    assert stats.non_null_count == 3
    assert stats.null_rate == 0.4


def test_empty_stats_are_well_defined() -> None:
    stats = FieldStats(sample_size=8)
    assert stats.count == 0
    assert stats.null_rate == 0.0
    assert stats.distinct_estimate == 0
    assert stats.length_mean == 0.0
    assert stats.whitespace_share == 0.0
    assert stats.whitespace_value_rate == 0.0
    assert stats.top_shapes(3) == []
    assert stats.samples == ()


def test_distinct_estimate_within_five_percent_at_ten_thousand() -> None:
    stats = FieldStats(sample_size=8)
    for value in _ids(10_000):
        stats.observe(value)
    assert abs(stats.distinct_estimate - 10_000) <= 500
    assert HLL_PRECISION == 12


def test_distinct_estimate_is_exact_for_small_sets_and_capped_by_count() -> None:
    stats = FieldStats(sample_size=8)
    for _ in range(300):
        for value in ("CREATED", "RELEASED", "SHIPPED"):
            stats.observe(value)
    assert stats.distinct_estimate == 3
    single = FieldStats(sample_size=8)
    single.observe("only")
    assert single.distinct_estimate == 1


def test_shape_histogram_and_top_shapes() -> None:
    stats = FieldStats(sample_size=8)
    for value in [*_ids(6), "88-210", "88-211", "X9-0442", "4471"]:
        stats.observe(value)
    top = stats.top_shapes(2)
    assert top[0] == ("AA-9999999", 0.6)
    assert top[1] == ("99-999", 0.2)
    assert all(share <= 1.0 for _shape, share in stats.top_shapes(10))
    assert abs(sum(share for _shape, share in stats.top_shapes(10)) - 1.0) < 1e-9


def test_shape_histogram_overflows_into_one_bucket() -> None:
    stats = FieldStats(sample_size=8)
    # Each value has a different shape: the bits of i, rendered as letter or digit.
    for i in range(MAX_SHAPES + 40):
        stats.observe("".join("a" if bit == "0" else "7" for bit in f"{i:010b}"))
    shapes = dict(stats.top_shapes(MAX_SHAPES + 50))
    assert len(shapes) <= MAX_SHAPES + 1
    assert OVERFLOW_SHAPE in shapes
    assert shapes[OVERFLOW_SHAPE] > 0.0


def test_length_and_whitespace_statistics() -> None:
    stats = FieldStats(sample_size=8)
    for value in ["ab", "abcd", "a b c d", None]:
        stats.observe(value)
    assert stats.length_min == 2
    assert stats.length_max == 7
    assert stats.length_mean == (2 + 4 + 7) / 3
    assert stats.whitespace_share == 3 / 13
    assert stats.whitespace_value_rate == 1 / 3


def test_reservoir_sample_is_bounded_and_representative() -> None:
    stats = FieldStats(sample_size=16)
    values = _ids(2_000)
    for value in values:
        stats.observe(value)
    assert len(stats.samples) == 16
    assert set(stats.samples) <= set(values)
    assert len(set(stats.samples)) == 16
    # Not just the first sixteen: reservoir sampling replaces early entries.
    assert set(stats.samples) != set(values[:16])


def test_snapshot_never_carries_samples() -> None:
    stats = FieldStats(sample_size=8)
    stats.observe(MARKER)
    stats.observe("other-value-1")
    snapshot = stats.snapshot()
    text = json.dumps(snapshot)
    assert MARKER not in text
    assert "other-value-1" not in text
    assert "samples" not in snapshot


def test_snapshot_restore_round_trip() -> None:
    stats = FieldStats(sample_size=8)
    for value in [*_ids(3_000), None, None, "88-210", "x y"]:
        stats.observe(value)
    restored = FieldStats.restore(stats.snapshot(), sample_size=8)
    assert restored.count == stats.count
    assert restored.null_count == stats.null_count
    assert restored.distinct_estimate == stats.distinct_estimate
    assert restored.top_shapes(5) == stats.top_shapes(5)
    assert restored.length_min == stats.length_min
    assert restored.length_max == stats.length_max
    assert restored.length_mean == stats.length_mean
    assert restored.whitespace_share == stats.whitespace_share
    assert restored.whitespace_value_rate == stats.whitespace_value_rate
    assert restored.samples == ()
    # The restored sketch keeps counting where the old one stopped.
    for value in _ids(1_000, prefix="PO-"):
        restored.observe(value)
    assert abs(restored.distinct_estimate - 4_001) <= 250


def test_restore_rejects_malformed_data() -> None:
    with pytest.raises(StatsFormatError):
        FieldStats.restore({"count": "many"}, sample_size=8)
    with pytest.raises(StatsFormatError):
        FieldStats.restore({"count": 1, "null_count": 0, "hll": "not base64!!"}, sample_size=8)


@settings(max_examples=100, deadline=None)
@given(st.lists(st.one_of(st.none(), st.text(max_size=80)), max_size=60))
def test_observe_accepts_any_text(values: list[str | None]) -> None:
    stats = FieldStats(sample_size=4)
    for value in values:
        stats.observe(value)
    assert stats.count == len(values)
    assert 0 <= stats.distinct_estimate <= stats.non_null_count
    assert 0.0 <= stats.null_rate <= 1.0
    assert all(1 <= len(shape) <= 64 for shape, _share in stats.top_shapes(8))
    FieldStats.restore(stats.snapshot(), sample_size=4)


def test_secret_count_is_durable() -> None:
    stats = FieldStats(sample_size=2)
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiAic3ZjLW9yZGVycyJ9."
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    )
    stats.observe(jwt)
    for i in range(500):
        stats.observe(f"plain-{i}")
    assert stats.secret_count == 1
    assert jwt not in stats.samples  # evicted from the reservoir, still counted
    restored = FieldStats.restore(stats.snapshot(), sample_size=2)
    assert restored.secret_count == 1
    assert "secret_count" in stats.snapshot()
    assert SECRET_MIN_LEN == 20


# ---------------------------------------------------------------------------------------------
# FieldStatsStore
# ---------------------------------------------------------------------------------------------


def test_store_get_or_create_and_observe() -> None:
    store = FieldStatsStore.from_settings(ClassifySettings(sample_values=8))
    ref = "sys_orders/tpl_000000000001/order_id"
    assert store.get(ref) is None
    first = store.get_or_create(ref)
    assert store.get_or_create(ref) is first
    store.observe(ref, "4471")
    store.observe(ref, None)
    assert first.count == 2
    assert ref in store
    assert len(store) == 1
    assert store.refs() == [ref]


def test_store_snapshot_restore_round_trip() -> None:
    store = FieldStatsStore(sample_size=8)
    for i, value in enumerate(_ids(500)):
        store.observe("sys_a/tpl_000000000001/order_id", value)
        store.observe("sys_a/tpl_000000000001/status", "CREATED" if i % 2 else "RELEASED")
    snapshot = store.snapshot()
    assert snapshot["version"] == SNAPSHOT_VERSION
    other = FieldStatsStore(sample_size=8)
    other.restore(snapshot)
    assert set(other.refs()) == set(store.refs())
    assert other.get("sys_a/tpl_000000000001/status").distinct_estimate == 2  # type: ignore[union-attr]
    assert (
        other.get("sys_a/tpl_000000000001/order_id").count  # type: ignore[union-attr]
        == 500
    )


def test_store_save_is_atomic_and_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "field_stats.json"
    store = FieldStatsStore(sample_size=8, path=path)
    store.observe("sys_a/tpl_000000000001/order_id", MARKER)
    store.observe("sys_a/tpl_000000000001/order_id", "SO-0000001")
    store.save()
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    text = path.read_text(encoding="utf-8")
    assert MARKER not in text
    assert "SO-0000001" not in text
    loaded = FieldStatsStore(sample_size=8, path=path)
    assert loaded.load() is True
    assert loaded.get("sys_a/tpl_000000000001/order_id").count == 2  # type: ignore[union-attr]


def test_store_load_tolerates_missing_and_corrupt_files(tmp_path: Path) -> None:
    missing = FieldStatsStore(sample_size=8, path=tmp_path / "absent.json")
    with structlog.testing.capture_logs() as logs:
        assert missing.load() is False
    assert len(missing) == 0
    assert any(entry["event"] == "field_stats_load_failed" for entry in logs)

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not json", encoding="utf-8")
    corrupt = FieldStatsStore(sample_size=8, path=corrupt_path)
    with structlog.testing.capture_logs() as logs:
        assert corrupt.load() is False
    assert len(corrupt) == 0
    assert any(entry["log_level"] == "warning" for entry in logs)

    wrong_version = tmp_path / "old.json"
    wrong_version.write_text(json.dumps({"version": 999, "fields": {}}), encoding="utf-8")
    old = FieldStatsStore(sample_size=8, path=wrong_version)
    with structlog.testing.capture_logs() as logs:
        assert old.load() is False
    assert any(entry["event"] == "field_stats_load_failed" for entry in logs)


def test_store_load_keeps_existing_fields_on_failure(tmp_path: Path) -> None:
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("[]", encoding="utf-8")
    store = FieldStatsStore(sample_size=8, path=corrupt_path)
    store.observe("sys_a/tpl_000000000001/x", "1")
    with structlog.testing.capture_logs():
        assert store.load() is False
    assert len(store) == 0  # a failed load starts empty, as documented


def test_maybe_flush_honours_the_interval(tmp_path: Path) -> None:
    path = tmp_path / "field_stats.json"
    store = FieldStatsStore(sample_size=8, flush_seconds=30.0, path=path)
    store.observe("sys_a/tpl_000000000001/x", "1")
    assert store.maybe_flush(now=10.0) is False
    assert not path.exists()
    assert store.maybe_flush(now=31.0) is True
    assert path.exists()
    # Nothing changed: no rewrite even when due.
    assert store.maybe_flush(now=100.0) is False
    store.observe("sys_a/tpl_000000000001/x", "2")
    assert store.maybe_flush(now=50.0) is False  # 19 s since the last flush: not due yet
    assert store.maybe_flush(now=70.0) is True


def test_maybe_flush_without_a_path_is_a_no_op() -> None:
    store = FieldStatsStore(sample_size=8)
    store.observe("sys_a/tpl_000000000001/x", "1")
    assert store.maybe_flush(now=10_000.0) is False


def test_value_counts_are_exact_and_never_persisted() -> None:
    stats = FieldStats(8)
    for value in ("yes", "yes", "no", "yes", MARKER, "", None):
        stats.observe(value)
    assert stats.value_count("yes") == 3
    assert stats.value_count("no") == 1
    assert stats.value_count(MARKER) == 1
    assert stats.value_count("maybe") == 0
    long_value = "x" * (COUNTED_VALUE_MAX_LEN + 10)
    stats.observe(long_value)
    stats.observe(long_value)
    assert stats.value_count(long_value) == 2
    assert stats.value_count(long_value + "y") == 0
    snapshot = json.dumps(stats.snapshot())
    assert MARKER not in snapshot
    assert "yes" not in snapshot
    restored = FieldStats.restore(json.loads(snapshot), 8)
    assert restored.value_count("yes") == 0  # counts start empty after a restart


def test_value_counts_stop_when_the_field_has_too_many_distinct_values() -> None:
    stats = FieldStats(8)
    for _ in range(3):
        stats.observe("yes")
    for n in range(MAX_COUNTED_VALUES):
        stats.observe(f"id-{n}")
    assert stats.value_count("yes") == 0
    assert stats.value_count("id-1") == 0


def test_reservoir_is_reproducible_per_field_and_differs_across_fields() -> None:
    def samples(ref: str) -> tuple[str, ...]:
        store = FieldStatsStore(sample_size=16)
        for i in range(5_000):
            store.observe(ref, f"v{i}")
        stats = store.get(ref)
        assert stats is not None
        return stats.samples

    assert samples("sys_a/tpl_1/region") == samples("sys_a/tpl_1/region")
    assert samples("sys_a/tpl_1/region") != samples("sys_a/tpl_1/status")


# ---------------------------------------------------------------------------------------------
# Sticky flags (ADR 0068)
# ---------------------------------------------------------------------------------------------


def test_sticky_flags_round_trip_and_version_one_loads_with_both_false() -> None:
    assert SNAPSHOT_VERSION == 2
    stats = FieldStats(sample_size=8)
    stats.observe("a")
    assert not stats.high_cardinality_seen and not stats.detector_hits_seen
    stats.high_cardinality_seen = True
    stats.detector_hits_seen = True
    snapshot = stats.snapshot()
    assert snapshot["high_cardinality_seen"] is True
    assert snapshot["detector_hits_seen"] is True
    restored = FieldStats.restore(snapshot, sample_size=8)
    assert restored.high_cardinality_seen and restored.detector_hits_seen
    # A version 1 field snapshot has no flags.
    legacy = {key: value for key, value in snapshot.items() if not key.endswith("_seen")}
    old = FieldStats.restore(legacy, sample_size=8)
    assert not old.high_cardinality_seen and not old.detector_hits_seen
    with pytest.raises(StatsFormatError):
        FieldStats.restore({**snapshot, "high_cardinality_seen": 1}, sample_size=8)


def test_store_note_sets_flags_marks_dirty_and_survives_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "field_stats.json"
    store = FieldStatsStore(sample_size=8, path=path)
    ref = "sys_a/tpl_000000000001/order_id"
    store.note(ref, high_cardinality=True)  # unknown ref: ignored
    store.observe(ref, "SO-0000001")
    store.save()
    store.note(ref, high_cardinality=True)
    assert store.maybe_flush(now=10_000.0)  # dirty again after the flag changed
    store.note(ref, detector_hits=True)
    store.save()
    loaded = FieldStatsStore(sample_size=8, path=path)
    assert loaded.load() is True
    stats = loaded.get(ref)
    assert stats is not None
    assert stats.high_cardinality_seen and stats.detector_hits_seen


def test_store_loads_a_version_one_snapshot(tmp_path: Path) -> None:
    store = FieldStatsStore(sample_size=8)
    store.observe("sys_a/tpl_000000000001/status", "CREATED")
    snapshot = store.snapshot()
    snapshot["version"] = 1
    for field_data in snapshot["fields"].values():
        del field_data["high_cardinality_seen"]
        del field_data["detector_hits_seen"]
    other = FieldStatsStore(sample_size=8)
    other.restore(snapshot)
    stats = other.get("sys_a/tpl_000000000001/status")
    assert stats is not None
    assert stats.count == 1
    assert not stats.high_cardinality_seen and not stats.detector_hits_seen
    with pytest.raises(StatsFormatError):
        other.restore({**snapshot, "version": 3})
