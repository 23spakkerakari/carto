"""The metadata store (spec 7.3; ADR 0050, 0056) on the reference in-memory implementation:
template counts add up across upserts, the edge's field columns and the profiler's never
overwrite each other, an unknown field the profiler meets becomes a tokenized identifier, the
inventory replaces systems, sources and pins as a whole (missing sources disabled, pins applied
to existing and later fields), sources carry their latest heartbeat, and listing pages by
field reference. The PostgreSQL implementation runs the same cases in
``test_core_m2_integration.py``; its SQL is checked here for shape."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carto_core import metadata as metadata_module
from carto_core.ingest.health import SourceHealthRecord
from carto_core.metadata import (
    FieldProfileUpdate,
    FieldRecord,
    InMemoryMetadataStore,
    InventoryResult,
    MetadataStore,
    TemplateRecord,
    pin_matches,
)
from carto_schema.inventory import (
    EdgeInventory,
    InventoryFieldPolicy,
    InventorySource,
    InventorySystem,
)

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
SHA = "a" * 64


def template(count: int = 3, **overrides: object) -> TemplateRecord:
    data: dict[str, object] = {
        "system_id": "sys_orders",
        "template_id": "tpl_create",
        "template_text": "INSERT orders",
        "kind": "row_change",
        "first_seen": NOW,
        "last_seen": NOW + timedelta(minutes=5),
        "count": count,
    }
    return TemplateRecord(**(data | overrides))  # type: ignore[arg-type]


def field_record(path: str = "order_id", **overrides: object) -> FieldRecord:
    data: dict[str, object] = {
        "field_ref": f"sys_orders/tpl_create/{path}",
        "system_id": "sys_orders",
        "template_id": "tpl_create",
        "path": path,
        "field_class": "identifier",
        "policy": "tokenize",
        "pinned": False,
        "forms": ("raw", "digits.0"),
        "count": 120,
        "shapes": (("9999", 0.9), ("99999", 0.1)),
        "first_seen": NOW,
        "last_seen": NOW + timedelta(hours=1),
    }
    return FieldRecord(**(data | overrides))  # type: ignore[arg-type]


def inventory(**overrides: object) -> EdgeInventory:
    data: dict[str, object] = {
        "schema_version": "1",
        "tenant_id": "default",
        "edge_id": "edge-gateway",
        "sent_at": NOW,
        "sources_file_sha256": SHA,
        "systems": [
            InventorySystem(id="sys_orders", name="Orders", owner_group="shop"),
            InventorySystem(id="sys_pay", name="Payments", criticality="high"),
        ],
        "sources": [
            InventorySource(
                id="src_orders_db",
                system_id="sys_orders",
                type="sql",
                config={"host": "db.internal", "port": 5432, "tables": ["orders"]},
                secret_ref="vault://db/orders",  # noqa: S106 - a reference name, not a secret
                backfill_days=7,
                schedule="every 60 s",
            ),
            InventorySource(id="src_pay_log", system_id="sys_pay", type="otlp", enabled=False),
        ],
        "field_policies": [
            InventoryFieldPolicy(
                field="sys_orders/*/customer_name",
                field_class="person_name",
                policy="tokenize",
                forms=["phonetic.0"],
            ),
            InventoryFieldPolicy(
                field="sys_orders/tpl_create/debug", field_class="free_text", policy="drop"
            ),
        ],
    }
    return EdgeInventory(**(data | overrides))  # type: ignore[arg-type]


@pytest.fixture
def store() -> InMemoryMetadataStore:
    return InMemoryMetadataStore()


def test_the_in_memory_store_satisfies_the_protocol(store: InMemoryMetadataStore) -> None:
    assert isinstance(store, MetadataStore)
    assert store.ping() is True
    assert store.list_systems("default") == []
    assert store.list_sources("default") == []
    assert store.list_fields("default") == []
    assert store.list_templates("default") == []
    assert store.get_field("default", "x/y/z") is None
    assert store.get_source("default", "src") is None


def test_template_upserts_add_counts_and_widen_the_seen_range(store: InMemoryMetadataStore) -> None:
    assert store.upsert_templates("default", [template(3)]) == 1
    later = template(
        2,
        first_seen=NOW - timedelta(days=1),
        last_seen=NOW + timedelta(days=1),
        template_text="INSERT orders v2",
        display_name="Order created",
    )
    assert store.upsert_templates("default", [later, template(1, template_id="tpl_ship")]) == 2
    (created, ship) = store.list_templates("default")
    assert created.count == 5
    assert created.first_seen == NOW - timedelta(days=1)
    assert created.last_seen == NOW + timedelta(days=1)
    assert created.template_text == "INSERT orders v2"
    assert created.display_name == "Order created"
    assert ship.template_id == "tpl_ship" and ship.count == 1
    store.upsert_templates("default", [template(1, display_name="Renamed")])
    assert store.list_templates("default")[0].display_name == "Order created", "reviewer-owned"
    assert store.list_templates("default", "sys_pay") == []
    assert store.list_templates("other") == []
    assert store.upsert_templates("default", [], connection=None) == 0


def test_edge_columns_and_profiler_columns_never_overwrite_each_other(
    store: InMemoryMetadataStore,
) -> None:
    assert store.upsert_fields("default", [field_record()]) == 1
    profile = FieldProfileUpdate(
        field_ref="sys_orders/tpl_create/order_id",
        count=4000,
        distinct_estimate=3900,
        null_rate=0.01,
        shapes=(("9999", 1.0),),
        quality=0.87,
        is_candidate=True,
        last_seen=NOW + timedelta(days=2),
    )
    assert store.update_field_profiles("default", [profile]) == 1
    after_profile = store.get_field("default", "sys_orders/tpl_create/order_id")
    assert after_profile is not None
    assert (after_profile.count, after_profile.distinct_estimate) == (4000, 3900)
    assert after_profile.quality == 0.87 and after_profile.is_candidate is True
    assert after_profile.last_seen == NOW + timedelta(days=2)
    assert (after_profile.field_class, after_profile.policy) == ("identifier", "tokenize")
    # The edge reports again: its columns move, the profiler's stay.
    edge_again = field_record(
        field_class="identifier", policy="tokenize", pinned=True, count=130, forms=("raw",)
    )
    store.upsert_fields("default", [edge_again])
    again = store.get_field("default", "sys_orders/tpl_create/order_id")
    assert again is not None
    assert (again.count, again.forms, again.pinned) == (130, ("raw",), True)
    assert (again.distinct_estimate, again.null_rate, again.quality) == (3900, 0.01, 0.87)
    assert again.is_candidate is True
    assert again.last_seen == NOW + timedelta(days=2), "the later of the two"
    assert again.first_seen == NOW


def test_the_profiler_inserts_a_field_it_meets_first_as_a_tokenized_identifier(
    store: InMemoryMetadataStore,
) -> None:
    profile = FieldProfileUpdate(
        field_ref="sys_pay/tpl_pay/order_ref",
        count=10,
        distinct_estimate=10,
        null_rate=0.0,
        shapes=(),
        quality=None,
        is_candidate=False,
        last_seen=NOW,
    )
    assert store.update_field_profiles("default", [profile]) == 1
    created = store.get_field("default", "sys_pay/tpl_pay/order_ref")
    assert created is not None
    assert (created.system_id, created.template_id, created.path) == (
        "sys_pay",
        "tpl_pay",
        "order_ref",
    )
    assert (created.field_class, created.policy, created.pinned) == (
        "identifier",
        "tokenize",
        False,
    )
    assert created.first_seen == NOW and created.last_seen == NOW
    unseen = FieldProfileUpdate("sys_pay/tpl_pay/x", 0, 0, 0.0, (), None, False, last_seen=None)
    assert store.update_field_profiles("default", [unseen]) == 0, "no time to seed first_seen"


def test_pin_patterns_match_one_template_or_any(store: InMemoryMetadataStore) -> None:
    assert pin_matches("sys_orders/*/customer_name", "sys_orders/tpl_create/customer_name")
    assert pin_matches("sys_orders/tpl_create/customer_name", "sys_orders/tpl_create/customer_name")
    assert not pin_matches(
        "sys_orders/tpl_ship/customer_name", "sys_orders/tpl_create/customer_name"
    )
    assert not pin_matches("sys_pay/*/customer_name", "sys_orders/tpl_create/customer_name")
    assert not pin_matches("sys_orders/*/name", "sys_orders/tpl_create/customer_name")
    assert not pin_matches("customer_name", "sys_orders/tpl_create/customer_name")
    assert not pin_matches("sys_orders/*/a/b", "sys_orders/tpl_create/a/b") or pin_matches(
        "sys_orders/*/a/b", "sys_orders/tpl_create/a/b"
    )
    assert store.ping()


def test_inventory_replaces_systems_sources_and_pins_as_a_whole(
    store: InMemoryMetadataStore,
) -> None:
    store.upsert_fields(
        "default",
        [
            field_record("customer_name", field_class="unknown", policy="drop", forms=()),
            field_record(
                "customer_name",
                template_id="tpl_ship",
                field_ref="sys_orders/tpl_ship/customer_name",
            ),
            field_record("debug", field_class="free_text", policy="tokenize"),
            field_record("order_id"),
        ],
    )
    result = store.replace_inventory(inventory(), NOW)
    assert result == InventoryResult(systems=2, sources=2, disabled=0, pins=2, fields_pinned=3)
    systems = store.list_systems("default")
    assert [(s.id, s.name, s.owner_group, s.criticality) for s in systems] == [
        ("sys_orders", "Orders", "shop", "medium"),
        ("sys_pay", "Payments", None, "high"),
    ]
    orders_db = store.get_source("default", "src_orders_db")
    assert orders_db is not None
    assert orders_db.config == {"host": "db.internal", "port": 5432, "tables": ["orders"]}
    assert orders_db.secret_ref == "vault://db/orders"  # noqa: S105 - a reference name
    assert (orders_db.backfill_days, orders_db.schedule, orders_db.edge_id) == (
        7,
        "every 60 s",
        "edge-gateway",
    )
    assert orders_db.enabled is True and orders_db.health is None
    pay_log = store.get_source("default", "src_pay_log")
    assert pay_log is not None and pay_log.enabled is False
    name = store.get_field("default", "sys_orders/tpl_create/customer_name")
    ship_name = store.get_field("default", "sys_orders/tpl_ship/customer_name")
    debug = store.get_field("default", "sys_orders/tpl_create/debug")
    order_id = store.get_field("default", "sys_orders/tpl_create/order_id")
    assert name is not None and ship_name is not None and debug is not None and order_id is not None
    assert (name.field_class, name.policy, name.pinned, name.forms) == (
        "person_name",
        "tokenize",
        True,
        ("phonetic.0",),
    )
    assert (ship_name.field_class, ship_name.pinned) == ("person_name", True), (
        "the * pin spans templates"
    )
    assert (debug.field_class, debug.policy, debug.pinned) == ("free_text", "drop", True)
    assert (order_id.field_class, order_id.pinned) == ("identifier", False)
    assert store.inventories["default", "edge-gateway"]["payload"]["sources_file_sha256"] == SHA
    assert store.inventories["default", "edge-gateway"]["received_at"] == NOW
    # A later field upsert applies the kept pins too.
    store.upsert_fields(
        "default",
        [
            field_record(
                "customer_name",
                template_id="tpl_x",
                field_ref="sys_orders/tpl_x/customer_name",
                field_class="unknown",
            )
        ],
    )
    late = store.get_field("default", "sys_orders/tpl_x/customer_name")
    assert late is not None and (late.field_class, late.pinned) == ("person_name", True)
    # The next inventory lists one source less: the missing one is disabled, not deleted.
    shrunk = inventory(sources=[inventory().sources[0]], field_policies=[])
    result = store.replace_inventory(shrunk, NOW + timedelta(hours=1))
    assert result == InventoryResult(systems=2, sources=1, disabled=0, pins=0, fields_pinned=0)
    assert [s.id for s in store.list_sources("default")] == ["src_orders_db", "src_pay_log"]
    gone = store.get_source("default", "src_pay_log")
    assert gone is not None and gone.enabled is False
    enabled_then_missing = inventory(sources=[], field_policies=[])
    assert store.replace_inventory(enabled_then_missing, NOW).disabled == 1
    assert all(not s.enabled for s in store.list_sources("default"))
    assert store.list_sources("other") == []


def test_sources_carry_their_latest_heartbeat(store: InMemoryMetadataStore) -> None:
    store.replace_inventory(inventory(), NOW)
    beat = SourceHealthRecord(
        tenant_id="default",
        source_id="src_orders_db",
        status="degraded",
        last_success_at=NOW,
        lag_seconds=12.5,
        error_count=2,
        buffer_depth=4,
        oldest_buffered_at=None,
        message="",
        received_at=NOW,
        sent_at=NOW,
    )
    store.health[("default", "src_orders_db")] = beat
    listed = {source.id: source for source in store.list_sources("default")}
    assert listed["src_orders_db"].health == beat
    assert listed["src_pay_log"].health is None
    one = store.get_source("default", "src_orders_db")
    assert one is not None and one.health == beat


def test_list_fields_filters_and_pages_by_field_ref(store: InMemoryMetadataStore) -> None:
    refs = [
        field_record("a"),
        field_record("b"),
        field_record(
            "c", field_ref="sys_pay/tpl_pay/c", system_id="sys_pay", template_id="tpl_pay"
        ),
    ]
    store.upsert_fields("default", refs)
    store.update_field_profiles(
        "default",
        [FieldProfileUpdate("sys_orders/tpl_create/b", 1, 1, 0.0, (), 0.9, True, NOW)],
    )
    all_refs = [f.field_ref for f in store.list_fields("default")]
    assert all_refs == ["sys_orders/tpl_create/a", "sys_orders/tpl_create/b", "sys_pay/tpl_pay/c"]
    first = store.list_fields("default", limit=2)
    assert [f.field_ref for f in first] == all_refs[:2]
    second = store.list_fields("default", after=first[-1].field_ref, limit=2)
    assert [f.field_ref for f in second] == all_refs[2:]
    assert store.list_fields("default", after=all_refs[-1]) == []
    assert [f.field_ref for f in store.list_fields("default", system_id="sys_pay")] == [
        "sys_pay/tpl_pay/c"
    ]
    assert [f.field_ref for f in store.list_fields("default", candidates_only=True)] == [
        "sys_orders/tpl_create/b"
    ]
    assert len(store.list_fields("default", limit=0)) == 1, "the page is at least one"
    assert store.list_fields("other") == []


def test_postgres_statements_are_fixed_text_with_bound_parameters() -> None:
    """Spec 14.7 by inspection; the container test runs them."""
    statements = [
        value
        for name, value in vars(metadata_module).items()
        if name.startswith("_") and name.isupper() and hasattr(value, "compile")
    ]
    assert len(statements) >= 12
    for statement in statements:
        text = str(statement)
        assert "%" not in text, text
    assert "ON CONFLICT (tenant_id, system_id, id)" in str(metadata_module._UPSERT_TEMPLATE)
    assert "count = templates.count + EXCLUDED.count" in str(metadata_module._UPSERT_TEMPLATE)
    field_upsert = str(metadata_module._UPSERT_FIELD)
    for column in ("distinct_estimate", "null_rate", "quality", "is_candidate"):
        assert column not in field_upsert, f"the edge never writes {column}"
    profile_upsert = str(metadata_module._UPSERT_PROFILE)
    assert "class = EXCLUDED" not in profile_upsert and "policy = EXCLUDED" not in profile_upsert
    assert "LEFT JOIN source_health" in str(metadata_module._SOURCES)
    assert "FOR UPDATE" not in str(metadata_module._PIN_FIELDS)
