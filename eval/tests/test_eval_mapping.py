"""The engine-to-truth field mapping rules (plan M2 decision 6; ADR 0049 item 3) on hand-built
parts: dotted paths, masked segments, map entries, CSV names, case, the acceptance policy, the
same-field and form-collapse rules, the policy families and the report."""

from __future__ import annotations

from collections import Counter
from typing import Any

from carto_eval.mapping import (
    EngineField,
    FieldMapping,
    collapse_forms,
    family_usable,
    member_form,
    policy_families,
    policy_statement,
)
from carto_eval.metrics import link_pair
from carto_simulator.ground_truth import FieldRef

SOURCES = {
    ("sys_orders", "tpl_o1"): "src_orders_log",
    ("sys_orders", "tpl_o2"): "src_orders_log",
    ("sys_webstore", "tpl_w1"): "src_webstore_log",
    ("sys_warehouse", "tpl_wms"): "src_wms_db",
    ("sys_payments", "tpl_p1"): "src_payments_xml",
}
TRUTH = {
    ("sys_orders", "src_orders_log", "order_id"),
    ("sys_orders", "src_orders_log", "cart_id"),
    ("sys_webstore", "src_webstore_log", "cart_id"),
    ("sys_webstore", "src_webstore_log", "basket.items"),
    ("sys_warehouse", "src_wms_db", "order_ref"),
    ("sys_warehouse", "src_wms_db", "Customer Name"),
    ("sys_payments", "src_payments_xml", "merchantRef"),
    ("sys_payments", "src_payments_xml", "stock"),
}
LINK_FIELDS = {
    ("sys_orders", "src_orders_log", "order_id"),
    ("sys_warehouse", "src_wms_db", "order_ref"),
    ("sys_payments", "src_payments_xml", "merchantRef"),
}


def mapping(engine_fields: list[EngineField] | None = None) -> FieldMapping:
    return FieldMapping(
        sources=SOURCES,
        truth_fields=TRUTH,
        truth_link_fields=LINK_FIELDS,
        engine_fields=engine_fields or [],
    )


def end(system: str, template: str, path: str, form: str = "raw") -> dict[str, str]:
    return {
        "field_ref": f"{system}/{template}/{path}",
        "system_id": system,
        "template_id": template,
        "path": path,
        "form": form,
    }


def record(
    link_id: str,
    a: dict[str, str],
    b: dict[str, str],
    *,
    score: float = 0.9,
    link_type: str = "exact",
    role: str = "transaction",
    status: str = "proposed",
) -> dict[str, Any]:
    return {
        "id": link_id,
        "status": status,
        "link_type": link_type,
        "role": role,
        "score": score,
        "a": a,
        "b": b,
        "affected_volume": 100,
    }


def field_of(m: FieldMapping, system: str, template: str, path: str) -> str | None:
    ref, _reason, _known = m.map_path(system, template, path)
    return None if ref is None else ref.field


def test_path_rules() -> None:
    m = mapping()
    ref, reason, known = m.map_path("sys_orders", "tpl_o1", "order_id")
    assert ref == FieldRef(system_id="sys_orders", source_id="src_orders_log", field="order_id")
    assert reason is None and known
    # a dotted path flattens to the leaf the truth uses...
    assert m.map_path("sys_webstore", "tpl_w1", "cart.cart_id")[0] == FieldRef(
        system_id="sys_webstore", source_id="src_webstore_log", field="cart_id"
    )
    # ...unless the truth field itself is dotted
    assert m.map_path("sys_webstore", "tpl_w1", "basket.items")[0] == FieldRef(
        system_id="sys_webstore", source_id="src_webstore_log", field="basket.items"
    )
    # map entries and masked tails name their parent (ADR 0030)
    assert field_of(m, "sys_payments", "tpl_p1", "stock.0.key") == "stock"
    assert field_of(m, "sys_payments", "tpl_p1", "stock.*") == "stock"
    # CSV column names as emitted, case preserved
    assert field_of(m, "sys_warehouse", "tpl_wms", "Customer Name") == "Customer Name"
    assert field_of(m, "sys_payments", "tpl_p1", "merchantRef") == "merchantRef"
    assert field_of(m, "sys_payments", "tpl_p1", "merchantref") == "merchantref"
    # a field the truth does not know keeps its leaf name and is flagged as unknown
    ref, reason, known = m.map_path("sys_orders", "tpl_o1", "msg.param_0")
    assert ref is not None and ref.field == "param_0" and reason is None and not known
    # masked names and unknown templates cannot be mapped
    assert m.map_path("sys_orders", "tpl_o1", "*~1") == (None, "masked name", False)
    assert m.map_path("sys_orders", "tpl_zz", "order_id") == (
        None,
        "template carried by no source",
        False,
    )
    assert m.map_field_ref("sys_orders/tpl_o1/order_id") is not None
    assert m.map_field_ref("sys_orders/tpl_o1/*") is None


def test_map_links_applies_the_policy_and_the_truth_vocabulary() -> None:
    m = mapping()
    o1 = end("sys_orders", "tpl_o1", "order_id")
    o2 = end("sys_orders", "tpl_o2", "order_id")
    wms = end("sys_warehouse", "tpl_wms", "order_ref", "digits.0")
    wms_alnum = end("sys_warehouse", "tpl_wms", "order_ref", "alnum")
    o1_alnum = end("sys_orders", "tpl_o1", "order_id", "alnum")
    pay = end("sys_payments", "tpl_p1", "merchantRef")
    cart = end("sys_orders", "tpl_o1", "cart_id")
    masked = end("sys_orders", "tpl_o1", "*")
    records = [
        record("same", o1, o2),  # two templates' copies of one field: not scored
        record("l5", o1, wms, score=0.97),
        record("l5-alnum", o1_alnum, wms_alnum, score=0.99),  # a form variant of l5
        record("l5-t2", o2, wms, score=0.96),  # the same truth pair from another template
        record("assoc", o1, pay, score=0.8, role="association"),
        record("batch", cart, pay, score=0.7, role="batch"),
        record("bridge", o1, cart, score=0.6, link_type="bridge"),
        record("batch-bridge", cart, wms, score=0.9, link_type="bridge", role="batch"),
        record("assoc-bridge", pay, wms_alnum, score=0.9, link_type="bridge", role="association"),
        record("comp", pay, wms, score=0.5, link_type="composite"),
        record("low", cart, wms, score=0.3),
        record("rej", cart, pay, status="rejected"),
        record("mask", masked, pay),
    ]
    mapped, report = m.map_links(records, threshold=0.5)
    by_id = {item.link_id: item for item in mapped}
    # l5 wins over its alnum variant (more raw ends) and its other-template twin (score)
    assert "l5" in by_id and "l5-alnum" not in by_id and "l5-t2" not in by_id
    assert by_id["l5"].link.form_a == "raw" and by_id["l5"].link.form_b == "digits.0"
    assert by_id["l5"].family_usable
    assert by_id["assoc"].link.link_type == "exact" and not by_id["assoc"].family_usable
    assert by_id["batch"].link.link_type == "batch" and not by_id["batch"].family_usable
    assert by_id["bridge"].link.link_type == "bridge" and by_id["bridge"].family_usable
    assert not by_id["batch-bridge"].family_usable and not by_id["assoc-bridge"].family_usable
    assert by_id["comp"].link.link_type == "composite" and not by_id["comp"].family_usable
    assert not by_id["low"].family_usable
    assert "same" not in by_id and "rej" not in by_id and "mask" not in by_id
    assert report.same_field_pairs == 1
    assert report.form_variants == 1 and report.template_duplicates == 1
    assert report.dropped_links == (("rej", "status rejected"), ("mask", "masked name"))
    assert report.predicted_links == len(mapped) == 8
    assert report.family_links == 2 and report.composite_links == 1
    families = policy_families(mapped)
    assert len(families) == 1
    members = {(f.ref.source_id, f.ref.field, f.form) for f in families[0].fields}
    assert members == {
        ("src_orders_log", "order_id", "raw"),
        ("src_wms_db", "order_ref", "digits.0"),
        ("src_orders_log", "cart_id", "raw"),
    }
    assert link_pair(by_id["l5"].link.a, "raw", by_id["l5"].link.b, "digits.0") == link_pair(
        by_id["l5"].link.b, "digits.0", by_id["l5"].link.a, "raw"
    )


def test_collapse_keeps_order_and_prefers_raw_then_score() -> None:
    m = mapping()
    o1 = end("sys_orders", "tpl_o1", "order_id", "alnum")
    pay = end("sys_payments", "tpl_p1", "merchantRef", "alnum")
    o1_raw = end("sys_orders", "tpl_o1", "order_id")
    pay_raw = end("sys_payments", "tpl_p1", "merchantRef")
    mapped, _ = m.map_links(
        [record("alnum", o1, pay, score=0.99), record("raw", o1_raw, pay_raw, score=0.6)],
        threshold=0.5,
    )
    assert [item.link_id for item in mapped] == ["raw"]
    assert [item.link_id for item in collapse_forms(mapped)] == ["raw"]


def test_report_lists_unreached_and_unlinked_truth_fields_and_pass1_only() -> None:
    fields = [
        EngineField("sys_orders", "tpl_o1", "order_id", "tokenize", ("raw",)),
        EngineField("sys_orders", "tpl_o1", "*~1", "tokenize", ("raw",)),
        EngineField("sys_orders", "tpl_o1", "amount", "tokenize", ("amount",)),
        EngineField("sys_orders", "tpl_o1", "department", "keep", ()),
        EngineField("sys_orders", "tpl_pass1", "order_id", "tokenize", ("raw",)),
        EngineField("sys_warehouse", "tpl_wms", "order_ref", "tokenize", ("raw", "digits")),
    ]
    m = mapping(fields)
    o1 = end("sys_orders", "tpl_o1", "order_id")
    wms = end("sys_warehouse", "tpl_wms", "order_ref", "digits.0")
    _mapped, report = m.map_links([record("l5", o1, wms)], threshold=0.5)
    assert report.engine_fields == 6 and report.tokenized_fields == 5
    assert report.pass1_only_fields == 1
    assert report.mapped_to_truth == 2
    assert report.unmappable == (("sys_orders/tpl_o1/*~1", "masked name"),)
    assert report.not_in_truth == (("sys_orders/tpl_o1/amount", "amount"),)
    # merchantRef is a truth link field no engine field reached; nothing reached is unlinked
    assert report.unreached_truth_fields == ("sys_payments/src_payments_xml/merchantRef",)
    assert report.unlinked_truth_fields == ()
    text = report.to_markdown()
    assert "## Field mapping (ADR 0049)" in text
    assert "- sys_payments/src_payments_xml/merchantRef" in text
    assert "- sys_orders/tpl_o1/*~1: masked name" in text
    assert "1 of them in templates without events (pass 1 only)" in text
    payload = report.to_json()
    assert payload["unreached_truth_fields"] == ["sys_payments/src_payments_xml/merchantRef"]
    assert "form variants" in policy_statement(0.5) and "0.50" in policy_statement(0.5)


def test_map_entities_drops_unmappable_members() -> None:
    m = mapping()
    entities = [
        {
            "entity_id": "ent_1",
            "members": [
                end("sys_orders", "tpl_o1", "order_id"),
                end("sys_orders", "tpl_o1", "*"),
                end("sys_warehouse", "tpl_wms", "order_ref", "digits.0"),
            ],
        },
        {"entity_id": "ent_2", "members": [end("sys_orders", "tpl_o1", "*~2")]},
    ]
    mapped = m.map_entities(entities)
    assert [e.entity_id for e in mapped] == ["ent_1"]
    assert len(mapped[0].fields) == 2


def test_family_usable_rules() -> None:
    assert family_usable("exact", "transaction", 0.5, 0.5)
    assert not family_usable("exact", "transaction", 0.49, 0.5)
    assert not family_usable("exact", "association", 0.9, 0.5)
    assert not family_usable("exact", "batch", 0.9, 0.5)
    assert family_usable("bridge", "transaction", 0.9, 0.5)
    assert not family_usable("bridge", "association", 0.9, 0.5)
    assert not family_usable("bridge", "batch", 0.9, 0.5)
    assert not family_usable("composite", "transaction", 0.9, 0.5)


def test_policy_families_are_keyed_by_field_and_keep_the_forms() -> None:
    """A field linked through raw on one side and digits.0 on another sits in one family (the
    Order family must not split into a raw family and a digits family); a batch-role bridge
    (a file to its shipments) never merges the file's entity into it."""
    m = mapping()
    o1 = end("sys_orders", "tpl_o1", "order_id")
    o1_digits = end("sys_orders", "tpl_o1", "order_id", "digits.0")
    cart = end("sys_orders", "tpl_o1", "cart_id")
    wms = end("sys_warehouse", "tpl_wms", "order_ref", "digits.0")
    pay = end("sys_payments", "tpl_p1", "merchantRef")
    stock = end("sys_payments", "tpl_p1", "stock")
    mapped, _report = m.map_links(
        [
            record("a", cart, o1),
            record("b", o1_digits, wms),
            record("c", pay, stock, link_type="bridge", role="batch"),
            record("d", wms, pay),
        ],
        threshold=0.5,
    )
    families = policy_families(mapped)
    assert len(families) == 1
    members = {(f.ref.field, f.form) for f in families[0].fields}
    # order_id is reached as raw (by cart_id) and digits.0 (by order_ref): one member, raw
    assert members == {
        ("cart_id", "raw"),
        ("order_id", "raw"),
        ("order_ref", "digits.0"),
        ("merchantRef", "raw"),
    }
    assert member_form(Counter({"alnum": 2, "digits.0": 2})) == "alnum"
    assert member_form(Counter({"alnum": 1, "raw": 1})) == "raw"
    assert "template duplicates" in _report.to_markdown()
    assert "transaction, exact or bridge" in policy_statement(0.5)
    assert "joins no family" in policy_statement(0.5)
