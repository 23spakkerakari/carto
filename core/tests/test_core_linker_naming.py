"""Heuristic naming (spec 9.9 dictionary): tokenization of paths, the majority vote over a
family's fields on the scenario A and C names, field labels from path and shape, and the
length cap. Pure functions over metadata; no token ever reaches them."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from carto_core.linker.naming import (
    LABEL_ORDER,
    MAX_NAME_LEN,
    labels_in,
    noun_for,
    noun_for_path,
    plural,
    suggest_entity_name,
    suggest_field_label,
    tokenize,
)
from carto_core.llm_gateway.schemas import LABEL_DICTIONARY


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("merchantRef", ["merchant", "ref"]),
        ("po_num", ["po", "num"]),
        ("purchase_order_id", ["purchase", "order", "id"]),
        ("items.3.skuCode", ["items", "3", "sku", "code"]),
        ("SHIP_FILE-name", ["ship", "file", "name"]),
        ("HTTPResponseCode", ["http", "response", "code"]),
        ("", []),
    ],
)
def test_tokenize(text: str, tokens: list[str]) -> None:
    assert tokenize(text) == tokens


def test_every_spec_dictionary_word_is_a_label() -> None:
    for word in LABEL_DICTIONARY:
        assert labels_in([word]), word


def test_labels_in_handles_bigrams_and_synonyms() -> None:
    assert labels_in(tokenize("purchase_order_id")) == ["PO"]
    assert labels_in(tokenize("po_num")) == ["PO"]
    assert labels_in(tokenize("emp_no")) == ["Employee"]
    assert labels_in(tokenize("cand_ref")) == ["Candidate"]
    assert labels_in(tokenize("legacy_sku")) == ["SKU"]
    assert labels_in(tokenize("order_ref_po")) == ["Order", "PO"]
    assert labels_in(tokenize("merchant_ref")) == []


def test_scenario_a_order_family_is_named_order() -> None:
    paths = [
        "cart_id",
        "cart_id",
        "order_id",
        "merchant_ref",
        "merchantRef",
        "order_ref",
        "po_num",
        "po_num",
        "shipment_no",
    ]
    counts = [1000, 3000, 9000, 3000, 3000, 1000, 1000, 500, 500]
    assert suggest_entity_name(paths, weights=counts) == "Order"
    # Unweighted it is a three-way tie (Cart, Order, PO) that the dictionary order resolves.
    assert suggest_entity_name(paths) == "Order"


def test_scenario_a_other_families() -> None:
    assert suggest_entity_name(["file_name", "file"]) == "File"
    assert suggest_entity_name(["manifest_id"]) == "Manifest"
    assert suggest_entity_name(["x_ref", "y_ref"], shapes=["AAAA_99999999_9999.AAA"]) == "File"


def test_scenario_c_families() -> None:
    assert suggest_entity_name(["employee_id", "worker_number", "emp_no"]) == "Employee"
    assert suggest_entity_name(["candidate_id", "cand_ref"]) == "Candidate"
    assert suggest_entity_name(["sku", "legacy_sku", "sku_code"]) == "SKU"
    assert suggest_entity_name(["username", "login"]) is None


def test_template_texts_vote_once_each() -> None:
    paths = ["x_id", "y_id"]
    assert suggest_entity_name(paths) is None
    assert (
        suggest_entity_name(paths, template_texts=["order created from cart", "order released"])
        == "Order"
    )


def test_weights_outvote_counts() -> None:
    assert suggest_entity_name(["po_num", "po_num", "order_id"], weights=[1, 1, 10]) == "Order"
    assert suggest_entity_name(["po_num", "po_num", "order_id"], weights=[10, 10, 1]) == "PO"


def test_names_are_title_case_and_capped() -> None:
    for label in LABEL_ORDER:
        assert label[0].isupper()
        assert len(label) <= MAX_NAME_LEN


@given(st.lists(st.text(max_size=40), max_size=10))
def test_suggestion_is_bounded_or_none(paths: list[str]) -> None:
    name = suggest_entity_name(paths)
    assert name is None or (0 < len(name) <= MAX_NAME_LEN and name in LABEL_ORDER)


@pytest.mark.parametrize(
    ("path", "shape", "label"),
    [
        ("po_num", "99-999", "PO number"),
        ("order_ref", "AA-9999999", "Order reference"),
        ("order_id", "9999", "Order number"),
        ("order_id", None, "Order reference"),
        ("merchantRef", "AA-9999", None),
        ("shipment_no", None, "Shipment number"),
        ("claim_id", "999999999999+", "Claim number"),
    ],
)
def test_field_labels(path: str, shape: str | None, label: str | None) -> None:
    assert suggest_field_label(path, shape) == label


def test_nouns_and_plurals() -> None:
    assert noun_for("PO") == "purchase order"
    assert noun_for("SKU") == "SKU"
    assert noun_for("Order") == "order"
    assert noun_for_path("po_num") == "purchase order"
    assert noun_for_path("merchant_ref") is None
    assert plural("purchase order") == "purchase orders"
    assert plural("SKU") == "SKUs"
    assert plural("batch") == "batches"
    assert plural("entry") == "entries"
    assert plural("day") == "days"
