"""Payload builders read metadata only (spec 9.9; plan M2 package G): the payload carries no
token, no identifier-class attribute and nothing planted as an event value; texts are cleaned
and clipped to the schema bounds; the bounds and the oracle behave."""

from __future__ import annotations

import pytest

from carto_core.llm_gateway.payload import (
    AttributeRecord,
    FieldRecord,
    InMemoryMetadataReader,
    MetadataReader,
    PayloadError,
    build_label_field_payload,
    build_name_entity_payload,
    clean_text,
    forbidden_content_check,
)
from carto_core.llm_gateway.schemas import (
    MAX_FIELDS_PER_REQUEST,
    MAX_TEMPLATE_TEXT_LEN,
    AttributeSummary,
    LabelFieldRequest,
    NameEntityRequest,
    TaskKind,
)

TENANT = "default"
TOKENS = ("t1.Zm9vYmFyYmF6cXV4MTIzND", "t12.AbCdEfGhIjKlMnOpQrStUv")
EVENT_VALUES = ("ORD-2026-000123", "Jane Q. Public", "jane@example.com", "4111111111111111")
IDENTIFIER_ATTRIBUTES = ("cust-88123", "PO-55-991")


def _reader() -> InMemoryMetadataReader:
    reader = InMemoryMetadataReader()
    reader.add(
        FieldRecord(
            tenant_id=TENANT,
            field_id="src_orders/orders/order_id",
            path="orders.order_id",
            system_name="Order system",
            top_shapes=("AAA-9999-999999", "9999999"),
            template_text="Created order <*> for customer <*>",
            attributes=(
                AttributeRecord("status", ("new", "paid", "shipped")),
                AttributeRecord("customer_ref", IDENTIFIER_ATTRIBUTES, identifier=True),
                AttributeRecord("token_like", (TOKENS[1],)),
            ),
            tokens=TOKENS,
            event_values=EVENT_VALUES,
            entity_id="ent_order",
        )
    )
    reader.add(
        FieldRecord(
            tenant_id=TENANT,
            field_id="src_wms/tpl_31/param_1",
            path="tpl_31.param_1",
            system_name="Warehouse",
            top_shapes=("99-999",),
            template_text="PO <*> received\x00 from \u200bweb\tshop " + "x" * 600,
            tokens=TOKENS,
            event_values=EVENT_VALUES,
            entity_id="ent_order",
        )
    )
    reader.add(
        FieldRecord(
            tenant_id="other-tenant",
            field_id="src_orders/orders/order_id",
            path="leak.other_tenant",
            system_name="Other",
            event_values=("OTHER-TENANT-VALUE",),
        )
    )
    return reader


def test_the_in_memory_reader_is_a_metadata_reader() -> None:
    assert isinstance(_reader(), MetadataReader)


def test_the_label_payload_carries_metadata_only() -> None:
    request = build_label_field_payload(
        _reader(), TENANT, ["src_orders/orders/order_id", "src_wms/tpl_31/param_1"]
    )
    assert isinstance(request, LabelFieldRequest)
    assert request.task is TaskKind.LABEL_FIELD
    payload = request.model_dump_json()
    forbidden = [
        *TOKENS,
        *EVENT_VALUES,
        *IDENTIFIER_ATTRIBUTES,
        "customer_ref",
        "OTHER-TENANT",
        "leak",
    ]
    assert forbidden_content_check(payload, forbidden=forbidden) == ()
    first, second = request.fields
    assert first.path == "orders.order_id"
    assert first.system_name == "Order system"
    assert first.top_shapes == ["AAA-9999-999999", "9999999"]
    assert [a.key for a in first.attributes] == ["status"], "identifier class and token dropped"
    assert first.attributes[0].values == ["new", "paid", "shipped"]
    assert second.attributes == []


def test_texts_are_cleaned_and_clipped() -> None:
    request = build_label_field_payload(_reader(), TENANT, ["src_wms/tpl_31/param_1"])
    (field,) = request.fields
    assert field.template_text.startswith("PO <*> received from web shop x")
    assert "\x00" not in field.template_text
    assert "\u200b" not in field.template_text
    assert len(field.template_text) == MAX_TEMPLATE_TEXT_LEN


def test_clean_text_rules() -> None:
    assert clean_text("  a\r\n b\t\tc  ", 10) == "a b c"
    assert clean_text(f"ref {TOKENS[0]} seen", 40) == "ref <*> seen", "tokens become placeholders"
    assert clean_text("e\u0301", 10) == "\u00e9", "NFC"
    assert clean_text("x\u202ey\u0007z", 10) == "xyz", "format and control characters dropped"
    assert clean_text("abcdef", 3) == "abc"


def test_order_is_kept_and_duplicates_and_unknown_ids_are_dropped() -> None:
    ids = [
        "src_wms/tpl_31/param_1",
        "missing",
        "src_orders/orders/order_id",
        "src_wms/tpl_31/param_1",
    ]
    request = build_label_field_payload(_reader(), TENANT, ids)
    assert [f.field_id for f in request.fields] == [
        "src_wms/tpl_31/param_1",
        "src_orders/orders/order_id",
    ]


def test_bounds_of_the_label_payload() -> None:
    reader = _reader()
    with pytest.raises(PayloadError, match="at least one"):
        build_label_field_payload(reader, TENANT, [])
    with pytest.raises(PayloadError, match="at most"):
        build_label_field_payload(
            reader, TENANT, [f"f{i}" for i in range(MAX_FIELDS_PER_REQUEST + 1)]
        )
    with pytest.raises(PayloadError, match="none of the field ids exist"):
        build_label_field_payload(reader, TENANT, ["missing"])
    with pytest.raises(PayloadError, match="none of the field ids exist"):
        build_label_field_payload(reader, "other-tenant", ["src_wms/tpl_31/param_1"])


def test_caller_attributes_are_merged_and_bounded() -> None:
    extra = {
        "src_wms/tpl_31/param_1": [
            # eight values with one duplicate: the builder dedupes to seven
            AttributeSummary(key="channel", values=[f"c{i}" for i in range(7)] + ["c6"]),
            # a token-shaped value is dropped, the clean one kept
            AttributeSummary(key="leaky", values=[TOKENS[0], "web"]),
            AttributeSummary(key="empty", values=[]),
        ]
    }
    request = build_label_field_payload(
        _reader(), TENANT, ["src_wms/tpl_31/param_1"], attributes=extra
    )
    (field,) = request.fields
    assert [a.key for a in field.attributes] == ["channel", "leaky"]
    assert field.attributes[0].values == [f"c{i}" for i in range(7)]
    assert field.attributes[1].values == ["web"]
    assert forbidden_content_check(request.model_dump_json(), forbidden=TOKENS) == ()


def test_the_entity_payload_lists_members_sorted_and_clean() -> None:
    request = build_name_entity_payload(_reader(), TENANT, "ent_order")
    assert isinstance(request, NameEntityRequest)
    assert request.task is TaskKind.NAME_ENTITY
    assert request.entity_id == "ent_order"
    assert [f.field_id for f in request.fields] == [
        "src_orders/orders/order_id",
        "src_wms/tpl_31/param_1",
    ]
    forbidden = [*TOKENS, *EVENT_VALUES, *IDENTIFIER_ATTRIBUTES]
    assert forbidden_content_check(request.model_dump_json(), forbidden=forbidden) == ()
    with pytest.raises(PayloadError, match="no member fields"):
        build_name_entity_payload(_reader(), TENANT, "ent_missing")
    with pytest.raises(PayloadError, match="no member fields"):
        build_name_entity_payload(_reader(), "other-tenant", "ent_order")


def test_the_entity_payload_caps_the_member_count() -> None:
    reader = InMemoryMetadataReader()
    for i in range(MAX_FIELDS_PER_REQUEST + 5):
        reader.add(FieldRecord(TENANT, f"f{i:03}", f"p{i}", "S", entity_id="e"))
    request = build_name_entity_payload(reader, TENANT, "e")
    assert len(request.fields) == MAX_FIELDS_PER_REQUEST


def test_the_oracle_finds_planted_strings_and_tokens() -> None:
    payload = '{"template_text":"order t1.Zm9vYmFyYmF6cXV4MTIzND done","x":"ORD-1"}'
    findings = forbidden_content_check(payload, forbidden=["ORD-1", "absent", ""])
    assert findings == ("ORD-1", "token:t1.Zm9vYmFyYmF6cXV4MTIzND")
