"""The event store contract (ADR 0046; spec 7.2, 9.1 to 9.5, 18.2): one case list with
hand-computed expected answers, run against the reference in-memory store and against
``ClickHouseStore`` on a real ClickHouse (a container; skipped without Docker). The dataset is
loaded as two batches and the first batch a second time with a later ``stored_at``, so every
query is proven to count a redelivered copy once and to keep the earliest ``stored_at``."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from testcontainers.community.clickhouse import ClickHouseContainer
from testcontainers.core.config import testcontainers_config

from carto_common.ids import derive_ulid
from carto_common.settings import RetentionSettings
from carto_core.db.clickhouse import ClickHouseWriter, create_client
from carto_core.migrations import apply_clickhouse_migrations
from carto_core.settings import ClickHouseSettings
from carto_core.store import EventStore, FieldForm, HourlyVolume, TemplateCount, Window
from carto_core.store.clickhouse import ClickHouseStore
from carto_core.store.memory import InMemoryEventStore
from carto_schema.event import CanonicalEvent

# --- the dataset ---------------------------------------------------------------------------------

TENANT = "default"
OTHER = "other"
D0 = datetime(2026, 10, 1, tzinfo=UTC)
D1 = D0 + timedelta(days=1)
D2 = D0 + timedelta(days=2)
D3 = D0 + timedelta(days=3)
D4 = D0 + timedelta(days=4)
W = Window(D0, D3)
T1 = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
"""When the first batch is stored."""
T2 = T1 + timedelta(hours=1)
"""When the second batch is stored."""
T3 = T1 + timedelta(hours=2)
"""When the first batch is stored again (a redelivery)."""

ORDER_ID = FieldForm("sys_orders/tpl_create/order_id", "raw")
ORDER_DIGITS = FieldForm("sys_orders/tpl_create/order_id", "digits.0")
CART_ID = FieldForm("sys_orders/tpl_create/cart_id", "raw")
ORDER_REF = FieldForm("sys_pay/tpl_pay/order_ref", "raw")
SHIP_ORDER_ID = FieldForm("sys_orders/tpl_ship/order_id", "raw")
NAME_RAW = FieldForm("sys_wms/tpl_recv/name", "raw")
NAME_PHONETIC = FieldForm("sys_wms/tpl_recv/name", "phonetic.0")
NAME_REF = "sys_wms/tpl_recv/name"

Ident = tuple[str, str, str, str]
"""``(field, form, token, shape)``."""


def tok(prefix: str, index: int) -> str:
    """A contract-valid token whose body names the test value, e.g. ``t1.______________T01``."""
    body = f"{prefix}{index:02d}"
    return "t1." + body.rjust(22, "_")


def event(
    tenant: str,
    system: str,
    template: str,
    source: str,
    observed_at: datetime,
    identifiers: Sequence[Ident],
    tag: str,
    attributes: dict[str, str] | None = None,
) -> CanonicalEvent:
    ms = int(observed_at.timestamp() * 1000)
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(ms, system, template, tag),
        "tenant_id": tenant,
        "source_id": source,
        "system_id": system,
        "template_id": template,
        "observed_at": observed_at,
        "ingested_at": observed_at + timedelta(seconds=3),
        "attributes": attributes if attributes is not None else {"status": "CREATED"},
        "identifiers": [
            {"field": field, "form": form, "token": token, "shape": shape, "len": 4}
            for field, form, token, shape in identifiers
        ],
    }
    return CanonicalEvent.model_validate(data)


def _a_base(i: int) -> datetime:
    return D0 + timedelta(hours=12, minutes=i)


def _lag(i: int) -> timedelta:
    if i == 19:
        return timedelta(seconds=-30)
    if i == 20:
        return timedelta(seconds=-90)
    return timedelta(seconds=60 * i)


def first_batch() -> list[CanonicalEvent]:
    out: list[CanonicalEvent] = []

    def order(i: int, offset: int, cart: int) -> None:
        out.append(
            event(
                TENANT,
                "sys_orders",
                "tpl_create",
                "src_orders_db",
                _a_base(i) + timedelta(seconds=offset),
                [
                    ("order_id", "raw", tok("T", i), "9999"),
                    ("order_id", "digits.0", tok("D", i), "9999"),
                    ("cart_id", "raw", tok("C", cart), "A9"),
                ],
                f"order-{i}-{offset}",
            )
        )

    # 30 order tokens; T1 in three events, T2 in two; T29 with carts C29 and C30; T30 with C29.
    for i in range(1, 31):
        order(i, 0, 29 if i == 30 else i)
    order(1, 10, 1)
    order(1, 20, 1)
    order(2, 10, 2)
    order(29, 10, 30)

    def pay(i: int, offset: int, token: str) -> None:
        out.append(
            event(
                TENANT,
                "sys_pay",
                "tpl_pay",
                "src_pay_log",
                _a_base(i) + _lag(i) + timedelta(seconds=offset),
                [("order_ref", "raw", token, "9999")],
                f"pay-{i}-{offset}",
            )
        )

    # 20 matched tokens with known lags (two negative); T1 in four events, T3 in two; 5 unmatched.
    for i in range(1, 21):
        pay(i, 0, tok("T", i))
    for offset in (5, 10, 15):
        pay(1, offset, tok("T", 1))
    pay(3, 5, tok("T", 3))
    out.extend(
        event(
            TENANT,
            "sys_pay",
            "tpl_pay",
            "src_pay_log",
            D0 + timedelta(hours=13, minutes=5 + k),
            [("order_ref", "raw", tok("U", k), "9999")],
            f"pay-u-{k}",
        )
        for k in range(1, 6)
    )
    # Another template of the order system: three shipments a day later.
    out.extend(
        event(
            TENANT,
            "sys_orders",
            "tpl_ship",
            "src_orders_db",
            D1 + timedelta(hours=9, minutes=k),
            [("order_id", "raw", tok("T", k), "9999")],
            f"ship-{k}",
        )
        for k in range(1, 4)
    )
    # The warehouse: the window's first millisecond, five names in two forms, the last
    # millisecond of the window, and the first millisecond after it.
    out.append(
        event(
            TENANT,
            "sys_wms",
            "tpl_recv",
            "src_wms",
            D0,
            [("sku", "raw", tok("S", 1), "A9")],
            "wms-start",
            {"dock": "D0"},
        )
    )
    out.extend(
        event(
            TENANT,
            "sys_wms",
            "tpl_recv",
            "src_wms",
            D2 + timedelta(hours=10, minutes=k),
            [
                ("name", "raw", tok("N", k), "AAAAA"),
                ("name", "phonetic.0", tok("P", k), "AAAAA"),
            ],
            f"wms-name-{k}",
            {"dock": f"D{k}"},
        )
        for k in range(1, 6)
    )
    out.append(
        event(
            TENANT,
            "sys_wms",
            "tpl_recv",
            "src_wms",
            D3 - timedelta(milliseconds=1),
            [("sku", "raw", tok("S", 2), "A9")],
            "wms-end-minus",
            {"dock": "D2"},
        )
    )
    out.append(
        event(
            TENANT,
            "sys_wms",
            "tpl_recv",
            "src_wms",
            D3,
            [("sku", "raw", tok("S", 3), "A9")],
            "wms-end",
            {"dock": "D3"},
        )
    )
    # Another tenant carrying the same order tokens: never counted for the default tenant.
    out.extend(
        event(
            OTHER,
            "sys_orders",
            "tpl_create",
            "src_orders_db",
            D1 + timedelta(hours=11, minutes=k),
            [("order_id", "raw", tok("T", k), "9999")],
            f"other-{k}",
        )
        for k in range(1, 3)
    )
    return out


def second_batch() -> list[CanonicalEvent]:
    return [
        event(
            TENANT,
            "sys_wms",
            "tpl_recv",
            "src_wms",
            D1 + timedelta(hours=15, minutes=k),
            [("sku", "raw", tok("S", 10 + k), "A9")],
            f"wms-late-{k}",
            {"dock": "D9"},
        )
        for k in range(1, 3)
    ]


# --- fixtures -----------------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return False
    try:
        client = docker.from_env()
        try:
            client.ping()
        finally:
            client.close()
    except Exception:
        return False
    return True


CLICKHOUSE_IMAGE = os.environ.get(
    "CARTO_TEST_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:24.8"
)
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"

CLICKHOUSE_PARAM = pytest.param(
    "clickhouse",
    marks=[
        pytest.mark.integration,
        pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
        pytest.mark.filterwarnings("ignore::ResourceWarning"),
        pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
    ],
)


@dataclass
class Loaded:
    store: EventStore
    batch1: list[CanonicalEvent]
    batch2: list[CanonicalEvent]


@pytest.fixture(scope="module")
def memory_loaded() -> Loaded:
    store = InMemoryEventStore()
    batch1, batch2 = first_batch(), second_batch()
    assert store.add_events(batch1, T1) == len(batch1)
    assert store.add_events(batch2, T2) == len(batch2)
    assert store.add_events(batch1, T3) == 0, "a redelivered batch adds nothing"
    return Loaded(store, batch1, batch2)


@pytest.fixture(scope="module")
def clickhouse_loaded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Loaded]:
    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
    password_file = tmp_path_factory.mktemp("secrets") / "clickhouse"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with ClickHouseContainer(
        CLICKHOUSE_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME
    ) as container:
        settings = ClickHouseSettings(
            url=f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8123)}",
            secure=False,
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            timeout_seconds=60,
        )
        client = create_client(settings)
        try:
            apply_clickhouse_migrations(client, RetentionSettings())
            now = {"at": T1}
            writer = ClickHouseWriter(client, clock=lambda: now["at"])
            batch1, batch2 = first_batch(), second_batch()
            writer.write_events(batch1)
            now["at"] = T2
            writer.write_events(batch2)
            now["at"] = T3
            writer.write_events(batch1)
            yield Loaded(ClickHouseStore(client), batch1, batch2)
        finally:
            client.close()


@pytest.fixture(params=["memory", CLICKHOUSE_PARAM])
def loaded(request: pytest.FixtureRequest) -> Loaded:
    if request.param == "memory":
        return request.getfixturevalue("memory_loaded")  # type: ignore[no-any-return]
    return request.getfixturevalue("clickhouse_loaded")  # type: ignore[no-any-return]


@pytest.fixture
def store(loaded: Loaded) -> EventStore:
    return loaded.store


# --- identifier rows and the watermark ------------------------------------------------------------


def test_the_store_satisfies_the_protocol(store: EventStore) -> None:
    assert isinstance(store, EventStore)
    assert store.ping() is True


def test_identifier_rows_count_every_row_once(store: EventStore) -> None:
    rows = list(store.identifier_rows(TENANT, W))
    assert len(rows) == 148
    keys = {(row.event_id, row.field_ref, row.form) for row in rows}
    assert len(keys) == 148, "one row per (event, field, form), the redelivery merged away"
    assert all(row.observed_at.tzinfo is not None and W.contains(row.observed_at) for row in rows)
    assert all(row.stored_at.tzinfo is not None for row in rows)
    by_form: dict[str, int] = {}
    for row in rows:
        by_form[row.form] = by_form.get(row.form, 0) + 1
    assert by_form == {"raw": 109, "digits.0": 34, "phonetic.0": 5}
    one = next(row for row in rows if row.token == tok("P", 2))
    assert (one.field_ref, one.form, one.shape, one.system_id) == (
        NAME_REF,
        "phonetic.0",
        "AAAAA",
        "sys_wms",
    )
    assert one.observed_at == D2 + timedelta(hours=10, minutes=2)
    assert one.stored_at == T1


@pytest.mark.parametrize(
    ("forms", "expected"),
    [
        (frozenset({"phonetic.0"}), 5),
        (frozenset({"digits.0"}), 34),
        (frozenset({"raw"}), 109),
        (frozenset({"raw", "digits.0"}), 143),
        (frozenset({"alnum"}), 0),
    ],
)
def test_identifier_rows_filter_by_form(
    store: EventStore, forms: frozenset[str], expected: int
) -> None:
    rows = list(store.identifier_rows(TENANT, W, forms=forms))
    assert len(rows) == expected
    assert {row.form for row in rows} <= forms


def test_identifier_rows_after_a_watermark_are_the_second_batch_only(store: EventStore) -> None:
    """The first batch stored again at T3 is not new: its rows keep T1 (ADR 0046)."""
    rows = list(store.identifier_rows(TENANT, W, stored_after=T1))
    assert sorted(row.token for row in rows) == [tok("S", 11), tok("S", 12)]
    assert all(row.stored_at == T2 for row in rows)
    assert list(store.identifier_rows(TENANT, W, stored_after=T2)) == []
    assert (
        len(list(store.identifier_rows(TENANT, W, stored_after=T1 - timedelta(seconds=1)))) == 148
    )


def test_max_stored_at_ignores_the_redelivered_copy(store: EventStore) -> None:
    assert store.max_stored_at(TENANT) == T2
    assert store.max_stored_at(OTHER) == T1
    assert store.max_stored_at("nobody") is None


def test_window_bounds_are_half_open_on_observed_at(store: EventStore) -> None:
    after = list(store.identifier_rows(TENANT, Window(D3, D4)))
    assert [row.token for row in after] == [tok("S", 3)]
    first_ms = list(store.identifier_rows(TENANT, Window(D0, D0 + timedelta(milliseconds=1))))
    assert [row.token for row in first_ms] == [tok("S", 1)]
    last_ms = list(store.identifier_rows(TENANT, Window(D3 - timedelta(milliseconds=1), D3)))
    assert [row.token for row in last_ms] == [tok("S", 2)]
    assert list(store.identifier_rows(TENANT, Window(D4, D4 + timedelta(days=1)))) == []


def test_tenants_never_mix(store: EventStore) -> None:
    rows = list(store.identifier_rows(OTHER, W))
    assert sorted(row.token for row in rows) == [tok("T", 1), tok("T", 2)]
    assert all(row.system_id == "sys_orders" for row in rows)
    assert list(store.identifier_rows("nobody", W)) == []


# --- counts -----------------------------------------------------------------------------------


def test_template_counts_are_distinct_events_per_template_and_utc_day(store: EventStore) -> None:
    assert store.template_counts(TENANT, W) == [
        TemplateCount("sys_orders", "tpl_create", date(2026, 10, 1), 34),
        TemplateCount("sys_orders", "tpl_ship", date(2026, 10, 2), 3),
        TemplateCount("sys_pay", "tpl_pay", date(2026, 10, 1), 29),
        TemplateCount("sys_wms", "tpl_recv", date(2026, 10, 1), 1),
        TemplateCount("sys_wms", "tpl_recv", date(2026, 10, 2), 2),
        TemplateCount("sys_wms", "tpl_recv", date(2026, 10, 3), 6),
    ]
    assert store.template_counts(OTHER, W) == [
        TemplateCount("sys_orders", "tpl_create", date(2026, 10, 2), 2)
    ]
    assert store.template_counts(TENANT, Window(D4, D4 + timedelta(days=1))) == []


def test_hourly_volumes_are_distinct_events_per_system_and_utc_hour(store: EventStore) -> None:
    assert store.hourly_volumes(TENANT, W) == [
        HourlyVolume("sys_orders", D0 + timedelta(hours=12), 34),
        HourlyVolume("sys_orders", D1 + timedelta(hours=9), 3),
        HourlyVolume("sys_pay", D0 + timedelta(hours=12), 24),
        HourlyVolume("sys_pay", D0 + timedelta(hours=13), 5),
        HourlyVolume("sys_wms", D0, 1),
        HourlyVolume("sys_wms", D1 + timedelta(hours=15), 2),
        HourlyVolume("sys_wms", D2 + timedelta(hours=10), 5),
        HourlyVolume("sys_wms", D2 + timedelta(hours=23), 1),
    ]


# --- the verification join (spec 9.3) --------------------------------------------------------


def test_pair_join_features_with_asymmetric_fanout_and_negative_lags(store: EventStore) -> None:
    join = store.pair_join(TENANT, ORDER_ID, ORDER_REF, W)
    assert (join.n_a, join.n_b, join.n_match) == (30, 25, 20)
    assert join.c_ab == 20 / 30 and join.c_ba == 20 / 25
    assert join.lag_median_seconds == 480.0
    assert join.lag_iqr_seconds == 600.0
    assert join.lag_positive_share == 0.9
    assert (join.fanout_ab_median, join.fanout_ab_p99) == (1.0, 2.0)
    assert (join.fanout_ba_median, join.fanout_ba_p99) == (1.0, 2.0)
    assert join.concentration == 23 / 33


def test_pair_join_swapped_ends_flip_the_lag_and_the_fanout(store: EventStore) -> None:
    join = store.pair_join(TENANT, ORDER_REF, ORDER_ID, W)
    assert (join.n_a, join.n_b, join.n_match) == (25, 30, 20)
    assert join.lag_median_seconds == -540.0
    assert join.lag_iqr_seconds == 600.0
    assert join.lag_positive_share == 0.1
    assert (join.fanout_ab_median, join.fanout_ab_p99) == (1.0, 2.0)
    assert (join.fanout_ba_median, join.fanout_ba_p99) == (1.0, 2.0)
    assert join.concentration == 23 / 33


def test_pair_join_within_one_system_across_templates(store: EventStore) -> None:
    join = store.pair_join(TENANT, ORDER_ID, SHIP_ORDER_ID, W)
    assert (join.n_a, join.n_b, join.n_match) == (30, 3, 3)
    assert join.lag_median_seconds == 75600.0
    assert join.lag_iqr_seconds == 0.0
    assert join.lag_positive_share == 1.0
    assert (join.fanout_ab_median, join.fanout_ab_p99) == (1.0, 1.0)
    assert (join.fanout_ba_median, join.fanout_ba_p99) == (2.0, 2.0)
    assert join.concentration == 1.0


def test_pair_join_with_no_match_has_no_distributions(store: EventStore) -> None:
    join = store.pair_join(TENANT, ORDER_ID, NAME_RAW, W)
    assert (join.n_a, join.n_b, join.n_match) == (30, 5, 0)
    assert join.c_ab == 0.0
    for name in (
        "lag_median_seconds",
        "lag_iqr_seconds",
        "lag_positive_share",
        "fanout_ab_median",
        "fanout_ab_p99",
        "fanout_ba_median",
        "fanout_ba_p99",
        "concentration",
    ):
        assert getattr(join, name) is None, name


def test_pair_join_respects_the_tenant_and_the_window(store: EventStore) -> None:
    other = store.pair_join(OTHER, ORDER_ID, ORDER_REF, W)
    assert (other.n_a, other.n_b, other.n_match) == (2, 0, 0)
    empty = store.pair_join(TENANT, ORDER_ID, ORDER_REF, Window(D4, D4 + timedelta(days=1)))
    assert (empty.n_a, empty.n_b, empty.n_match) == (0, 0, 0)
    assert empty.c_ab == 0.0 and empty.c_ba == 0.0
    # A window that ends before the negative-lag payments: T19 and T20 keep their order rows.
    narrow = store.pair_join(
        TENANT, ORDER_ID, ORDER_REF, Window(D0, D0 + timedelta(hours=12, minutes=18))
    )
    assert (narrow.n_a, narrow.n_b, narrow.n_match) == (17, 8, 8)


# --- bridges (spec 9.4) ------------------------------------------------------------------------


def test_cooccurrences_pair_every_two_ends_of_an_event_once_with_consistency_counts(
    store: EventStore,
) -> None:
    rows = store.cooccurrences(TENANT, W)
    assert [(row.a, row.b, row.system_id) for row in rows] == [
        (CART_ID, ORDER_DIGITS, "sys_orders"),
        (CART_ID, ORDER_ID, "sys_orders"),
        (ORDER_DIGITS, ORDER_ID, "sys_orders"),
        (NAME_PHONETIC, NAME_RAW, "sys_wms"),
    ]
    counts = [
        (row.events, row.distinct_a, row.single_b_a, row.distinct_b, row.single_a_b) for row in rows
    ]
    assert counts == [
        (34, 30, 29, 30, 29),
        (34, 30, 29, 30, 29),
        (34, 30, 30, 30, 30),
        (5, 5, 5, 5, 5),
    ]
    assert rows[0].consistency_ab == 29 / 30 and rows[2].consistency_ba == 1.0
    assert [row.events for row in store.cooccurrences(TENANT, W, min_events=6)] == [34, 34, 34]
    assert store.cooccurrences(TENANT, W, min_events=35) == []
    assert store.cooccurrences(OTHER, W) == []


# --- composite blocking input (spec 9.5) -----------------------------------------------------


def test_events_with_keys_pass_attributes_and_identifier_triples_through(
    store: EventStore,
) -> None:
    events = {item.event_id: item for item in store.events_with_keys(TENANT, "sys_wms", W)}
    assert len(events) == 9
    assert all(
        item.system_id == "sys_wms" and item.template_id == "tpl_recv" for item in events.values()
    )
    assert all(W.contains(item.observed_at) for item in events.values())
    by_dock = {
        item.attributes["dock"]: item for item in events.values() if "dock" in item.attributes
    }
    assert set(by_dock) == {"D0", "D1", "D2", "D3", "D4", "D5", "D9"}
    name_three = next(
        item for item in events.values() if (NAME_REF, "raw", tok("N", 3)) in item.identifiers
    )
    assert set(name_three.identifiers) == {
        (NAME_REF, "raw", tok("N", 3)),
        (NAME_REF, "phonetic.0", tok("P", 3)),
    }
    assert name_three.attributes == {"dock": "D3"}
    assert name_three.observed_at == D2 + timedelta(hours=10, minutes=3)
    start = by_dock["D0"]
    assert start.identifiers == (("sys_wms/tpl_recv/sku", "raw", tok("S", 1)),)
    phonetic_only = list(
        store.events_with_keys(TENANT, "sys_wms", W, forms=frozenset({"phonetic.0"}))
    )
    assert len(phonetic_only) == 9, "events without a matching form still stream, with no triples"
    triples = sorted(triple for item in phonetic_only for triple in item.identifiers)
    assert triples == [(NAME_REF, "phonetic.0", tok("P", k)) for k in range(1, 6)]
    assert list(store.events_with_keys(TENANT, "sys_nobody", W)) == []
    assert list(store.events_with_keys(OTHER, "sys_wms", W)) == []


def test_events_with_keys_count_a_redelivered_event_once(store: EventStore) -> None:
    orders = list(store.events_with_keys(TENANT, "sys_orders", W))
    assert len(orders) == 37
    assert len({item.event_id for item in orders}) == 37
    assert all(len(item.identifiers) in (1, 3) for item in orders)
    assert all(item.attributes == {"status": "CREATED"} for item in orders)


def test_nothing_returned_is_a_raw_value(loaded: Loaded) -> None:
    """Spec 2.3 invariant 2: tokens, shapes, counts and times only (a smoke check over the rows)."""
    rows = list(loaded.store.identifier_rows(TENANT, W))
    for row in rows:
        assert row.token.startswith("t1.")
        assert row.shape in {"9999", "A9", "AAAAA"}
    serialized: list[Any] = [
        row for item in loaded.store.cooccurrences(TENANT, W) for row in (item.a, item.b)
    ]
    assert all(isinstance(end, FieldForm) for end in serialized)
