"""The inventory path end to end on the core side (ADR 0050, 0056): the templates a batch
yields for the metadata store, and an inventory's pins reaching fields that a bundle records
afterwards through the same store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from carto_common.ids import new_ulid
from carto_core.ingest.app import batch_templates
from carto_core.metadata import FieldRecord, InMemoryMetadataStore
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch
from carto_schema.inventory import EdgeInventory

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _event(index: int, template_id: str, text: str, minutes: int) -> CanonicalEvent:
    suffix = "ABCDEFGHJKMNPQRSTVWXYZ"[index]
    data = CanonicalEvent.example().model_dump() | {
        "event_id": f"01J9ZK8X5Q8V3N6M2T4R7W1Y0{suffix}",
        "template_id": template_id,
        "template_text": text,
        "observed_at": NOW + timedelta(minutes=minutes),
    }
    return CanonicalEvent.model_validate(data)


def test_batch_templates_group_by_system_and_template_with_the_seen_range() -> None:
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=NOW,
        events=[
            _event(0, "tpl_create", "INSERT purchase_orders", 5),
            _event(1, "tpl_ship", "UPDATE purchase_orders shipped", 2),
            _event(2, "tpl_create", "INSERT purchase_orders", 1),
            _event(3, "tpl_create", "INSERT purchase_orders", 9),
        ],
    )
    create, ship = batch_templates(batch)
    assert (create.system_id, create.template_id, create.kind) == (
        "sys_warehouse",
        "tpl_create",
        "row_change",
    )
    assert create.template_text == "INSERT purchase_orders"
    assert create.count == 3
    assert create.first_seen == NOW + timedelta(minutes=1)
    assert create.last_seen == NOW + timedelta(minutes=9)
    assert (ship.template_id, ship.count) == ("tpl_ship", 1)
    assert ship.first_seen == ship.last_seen == NOW + timedelta(minutes=2)
    assert create.display_name is None and create.event_type_label is None


def test_inventory_pins_reach_fields_recorded_later_by_a_bundle() -> None:
    store = InMemoryMetadataStore()
    inventory = EdgeInventory.model_validate(
        {
            "schema_version": "1",
            "tenant_id": "default",
            "edge_id": "edge-gateway",
            "sent_at": NOW,
            "sources_file_sha256": "c" * 64,
            "systems": [{"id": "sys_warehouse", "name": "Warehouse"}],
            "sources": [{"id": "src_wms_db", "system_id": "sys_warehouse", "type": "sql"}],
            "field_policies": [
                {
                    "field": "sys_warehouse/*/customer_name",
                    "field_class": "person_name",
                    "policy": "tokenize",
                    "forms": ["phonetic.0"],
                },
                {
                    "field": "sys_warehouse/tpl_4f1c9a/note",
                    "field_class": "free_text",
                    "policy": "drop",
                },
            ],
        }
    )
    store.replace_inventory(inventory, NOW)
    recorded = store.upsert_fields(
        "default",
        [
            FieldRecord(
                field_ref="sys_warehouse/tpl_4f1c9a/customer_name",
                system_id="sys_warehouse",
                template_id="tpl_4f1c9a",
                path="customer_name",
                field_class="unknown",
                policy="drop",
                pinned=False,
                forms=(),
                count=10,
                shapes=(),
                first_seen=NOW,
            ),
            FieldRecord(
                field_ref="sys_warehouse/tpl_other/note",
                system_id="sys_warehouse",
                template_id="tpl_other",
                path="note",
                field_class="free_text",
                policy="tokenize",
                pinned=False,
                forms=("raw",),
                count=10,
                shapes=(),
                first_seen=NOW,
            ),
        ],
    )
    assert recorded == 2
    name = store.get_field("default", "sys_warehouse/tpl_4f1c9a/customer_name")
    note = store.get_field("default", "sys_warehouse/tpl_other/note")
    assert name is not None and note is not None
    assert (name.field_class, name.policy, name.pinned, name.forms) == (
        "person_name",
        "tokenize",
        True,
        ("phonetic.0",),
    )
    assert (note.policy, note.pinned) == ("tokenize", False), "the pin names another template"
    assert [s.id for s in store.list_systems("default")] == ["sys_warehouse"]
