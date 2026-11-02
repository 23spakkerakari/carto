"""The in-memory event store on its own (ADR 0046): the exact-low quantile definition both
implementations share, loading rules (aware ``stored_at``, repeated events ignored), and the
empty answers of every query."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carto_core.store import EventStore, FieldForm, Window
from carto_core.store.memory import CONCENTRATION_TOP_N, InMemoryEventStore, quantile_exact_low
from carto_schema.event import CanonicalEvent

NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
WINDOW = Window(NOW - timedelta(days=7), NOW)
END = FieldForm("sys_warehouse/tpl_4f1c9a/po_num", "raw")


def test_quantile_exact_low_is_the_element_at_floor_of_level_times_n_minus_one() -> None:
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert quantile_exact_low(values, 0.0) == 1.0
    assert quantile_exact_low(values, 0.5) == 3.0
    assert quantile_exact_low(values, 0.25) == 2.0
    assert quantile_exact_low(values, 0.75) == 4.0
    assert quantile_exact_low(values, 0.99) == 4.0, "floor(0.99 * 4) = 3"
    assert quantile_exact_low(values, 1.0) == 5.0
    assert quantile_exact_low([7.0], 0.99) == 7.0
    assert quantile_exact_low([1.0, 2.0], 0.5) == 1.0, "the lower of the two, never a mean"
    assert quantile_exact_low([], 0.5) is None
    assert CONCENTRATION_TOP_N == 10


def test_window_rejects_naive_or_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="before"):
        Window(NOW, NOW)
    with pytest.raises(ValueError, match="aware"):
        Window(datetime(2026, 10, 1), datetime(2026, 10, 2))
    assert WINDOW.contains(WINDOW.start) and not WINDOW.contains(WINDOW.end)
    assert FieldForm("sys_a/tpl/x", "raw").system_id == "sys_a"
    assert FieldForm("a/t/x", "raw") < FieldForm("a/t/y", "digits.0") < FieldForm("b/t/a", "raw")


def test_add_events_ignores_repeats_and_needs_an_aware_time() -> None:
    store = InMemoryEventStore()
    event = CanonicalEvent.example()
    assert store.add_events([event], NOW) == 1
    assert store.add_events([event], NOW + timedelta(hours=1)) == 0
    assert len(store) == 1
    with pytest.raises(ValueError, match="aware"):
        store.add_events([event], datetime(2026, 10, 6))
    assert store.max_stored_at("default") == NOW, "the first copy's time is kept"
    other = CanonicalEvent.model_validate(event.model_dump() | {"tenant_id": "other"})
    assert store.add_events([other], NOW) == 1, "the same event id in another tenant is new"
    assert len(store) == 2


def test_add_events_without_a_time_uses_the_wall_clock() -> None:
    store = InMemoryEventStore()
    before = datetime.now(UTC)
    store.add_events([CanonicalEvent.example()])
    stored = store.max_stored_at("default")
    assert stored is not None and before <= stored <= datetime.now(UTC)


def test_every_query_answers_empty_on_an_empty_store() -> None:
    store: EventStore = InMemoryEventStore()
    assert isinstance(store, EventStore)
    assert store.ping() is True
    assert store.max_stored_at("default") is None
    assert list(store.identifier_rows("default", WINDOW)) == []
    assert store.template_counts("default", WINDOW) == []
    assert store.cooccurrences("default", WINDOW) == []
    assert store.hourly_volumes("default", WINDOW) == []
    assert list(store.events_with_keys("default", "sys_warehouse", WINDOW)) == []
    join = store.pair_join("default", END, FieldForm("sys_pay/tpl_1/ref", "raw"), WINDOW)
    assert (join.n_a, join.n_b, join.n_match) == (0, 0, 0)
    assert join.c_ab == 0.0 and join.c_ba == 0.0
    assert join.lag_median_seconds is None and join.concentration is None


def test_the_example_event_profile() -> None:
    """The spec 7.1 example: four identifier rows, one event, one cooccurrence per pair of ends."""
    store = InMemoryEventStore()
    store.add_events([CanonicalEvent.example()], NOW)
    rows = list(store.identifier_rows("default", WINDOW))
    assert len(rows) == 4
    assert {row.form for row in rows} == {"raw", "alnum", "digits.0"}
    assert store.max_stored_at("default") == NOW
    assert store.template_counts("default", WINDOW)[0].events == 1
    pairs = store.cooccurrences("default", WINDOW)
    assert len(pairs) == 6, "four ends make six unordered pairs"
    assert all(pair.a < pair.b and pair.events == 1 for pair in pairs)
    assert all(pair.consistency_ab == 1.0 and pair.consistency_ba == 1.0 for pair in pairs)
    (keys,) = store.events_with_keys("default", "sys_warehouse", WINDOW)
    assert keys.attributes == {"status": "CREATED", "warehouse_code": "DC-03"}
    assert len(keys.identifiers) == 4
    same = store.pair_join("default", END, END, WINDOW)
    assert (same.n_a, same.n_b, same.n_match) == (1, 1, 1)
    assert same.lag_median_seconds == 0.0 and same.concentration == 1.0
    assert same.fanout_ab_median == 1.0 and same.lag_positive_share == 0.0


def test_an_event_without_identifiers_has_no_watermark_but_counts() -> None:
    store = InMemoryEventStore()
    bare = CanonicalEvent.model_validate(
        CanonicalEvent.example().model_dump() | {"identifiers": []}
    )
    store.add_events([bare], NOW)
    assert store.max_stored_at("default") is None
    assert store.template_counts("default", WINDOW)[0].events == 1
    assert store.hourly_volumes("default", WINDOW)[0].events == 1
    (keys,) = store.events_with_keys("default", "sys_warehouse", WINDOW)
    assert keys.identifiers == ()
