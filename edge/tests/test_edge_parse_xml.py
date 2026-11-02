"""XML parsing with defusedxml (spec 8.2 item 2, 2.3 invariant 8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.parse.common import (
    MAX_ARRAY_ITEMS,
    NOTE_ARRAY_LIMIT,
    NOTE_DEPTH_LIMIT,
    TEMPLATE_MASK,
)
from carto_edge.pipeline.parse.xml import looks_like_xml, parse_xml

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)

PAYMENT = (
    "<paymentMessage><timestamp>2026-09-23T09:17:01.292-04:00</timestamp>"
    "<merchantRef>X9-0442</merchantRef><amount>129.99</amount><currency>USD</currency>"
    "<cardholderName>Jane Smith</cardholderName><status>AUTHORIZED</status>"
    "<httpStatus>200</httpStatus><processor>cardnet</processor></paymentMessage>"
)
HEARTBEAT = (
    "<heartbeat><timestamp>2026-09-23T00:23:06.570-04:00</timestamp><status>OK</status></heartbeat>"
)


def test_parse_xml_payment_message_children_become_fields() -> None:
    parsed = parse_xml(PAYMENT)
    assert parsed is not None
    assert parsed.root == "paymentMessage"
    assert parsed.fields == {
        "timestamp": "2026-09-23T09:17:01.292-04:00",
        "merchantRef": "X9-0442",
        "amount": "129.99",
        "currency": "USD",
        "cardholderName": "Jane Smith",
        "status": "AUTHORIZED",
        "httpStatus": "200",
        "processor": "cardnet",
    }


def test_parse_xml_heartbeat_has_its_own_root() -> None:
    parsed = parse_xml(HEARTBEAT)
    assert parsed is not None
    assert parsed.root == "heartbeat"
    assert parsed.fields == {"timestamp": "2026-09-23T00:23:06.570-04:00", "status": "OK"}


def test_parse_xml_root_name_is_a_template_word_only_when_it_is_not_value_like() -> None:
    """The root element name travels as the template of XML records (ADR 0016), so it is
    masked like any template word when it looks like a value (ADR 0039 amendment of
    2026-11-02): a digit, an ``@`` or more than 64 characters make it ``<*>``. ``root`` stays
    the raw local name for the parser; trust across records is the parser's gate."""
    payment = parse_xml(PAYMENT)
    assert payment is not None
    assert payment.template == "paymentMessage"
    order = parse_xml("<ORDER4471><status>OK</status></ORDER4471>")
    assert order is not None
    assert order.root == "ORDER4471"
    assert order.template == TEMPLATE_MASK
    assert order.fields == {"status": "OK"}
    short = parse_xml("<a1/>")
    assert short is not None
    assert short.template == TEMPLATE_MASK
    long_name = parse_xml(f"<{'x' * 65}/>")
    assert long_name is not None
    assert long_name.template == TEMPLATE_MASK
    word = parse_xml("<customer_alice/>")
    assert word is not None
    assert word.template == "customer_alice"  # a word by shape; the parser's gate decides
    namespaced = parse_xml('<p:order7 xmlns:p="urn:p"/>')
    assert namespaced is not None
    assert namespaced.template == TEMPLATE_MASK


def test_parse_xml_attributes_repeats_nesting_and_text() -> None:
    doc = (
        '<?xml version="1.0"?><order id="7" kind="web"><item sku="A">first</item>'
        '<item sku="B">second</item><ship><addr>x</addr></ship><note>  n  </note>'
        "<empty/></order>"
    )
    parsed = parse_xml(doc)
    assert parsed is not None
    assert parsed.root == "order"
    assert parsed.fields == {
        "@id": "7",
        "@kind": "web",
        "item.0.@sku": "A",
        "item.0.#text": "first",
        "item.1.@sku": "B",
        "item.1.#text": "second",
        "ship.addr": "x",
        "note": "n",
    }


def test_parse_xml_namespaces_are_stripped_to_local_names() -> None:
    parsed = parse_xml('<a xmlns="urn:x" xmlns:p="urn:p"><p:b>1</p:b></a>')
    assert parsed is not None
    assert parsed.root == "a"
    assert parsed.fields == {"b": "1"}


def test_parse_xml_unescapes_entities_in_text() -> None:
    parsed = parse_xml("<a><b>&lt;x&gt; &amp; &quot;y&quot;</b></a>")
    assert parsed is not None
    assert parsed.fields == {"b": '<x> & "y"'}


def test_parse_xml_rejects_dtd_entities_and_external_references() -> None:
    assert parse_xml('<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>') is None
    bomb = (
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;">'
        '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]><lolz>&lol3;</lolz>'
    )
    assert parse_xml(bomb) is None
    xxe = '<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><a>&e;</a>'
    assert parse_xml(xxe) is None
    assert parse_xml('<!DOCTYPE a SYSTEM "http://127.0.0.1/evil.dtd"><a/>') is None


def test_parse_xml_rejects_malformed_and_non_xml() -> None:
    assert parse_xml("<a><b></a>") is None
    assert parse_xml("") is None
    assert parse_xml("not xml") is None
    assert parse_xml("<a>") is None
    assert parse_xml("<a/><b/>") is None


def test_parse_xml_depth_limit_is_applied_and_noted() -> None:
    opening = "".join(f"<l{i}>" for i in range(20))
    closing = "".join(f"</l{i}>" for i in reversed(range(20)))
    parsed = parse_xml(opening + "x" + closing)
    assert parsed is not None
    assert NOTE_DEPTH_LIMIT in parsed.notes
    assert all(path.count(".") < 12 for path in parsed.fields)


def test_parse_xml_survives_very_deep_nesting() -> None:
    result = parse_xml("<a>" * 50_000 + "</a>" * 50_000)
    assert result is None or NOTE_DEPTH_LIMIT in result.notes


def test_parse_xml_repeated_children_are_capped_and_noted() -> None:
    doc = "<r>" + "".join(f"<i>{n}</i>" for n in range(MAX_ARRAY_ITEMS + 3)) + "</r>"
    parsed = parse_xml(doc)
    assert parsed is not None
    assert len(parsed.fields) == MAX_ARRAY_ITEMS
    assert NOTE_ARRAY_LIMIT in parsed.notes


def test_looks_like_xml_is_a_cheap_prefix_check() -> None:
    assert looks_like_xml("<a/>")
    assert looks_like_xml('  <?xml version="1.0"?><a/>')
    assert not looks_like_xml("ts=1")
    assert not looks_like_xml("")
    assert not looks_like_xml("<")


@needs_sim
def test_parse_xml_every_payment_line_from_the_simulator() -> None:
    files = sorted((SIM_DIR / "payments").glob("*.xml"))
    assert files
    roots: set[str] = set()
    for line in files[0].read_text(encoding="utf-8").splitlines()[:500]:
        parsed = parse_xml(line)
        assert parsed is not None, line[:40]
        roots.add(parsed.root)
        assert "timestamp" in parsed.fields
    assert roots == {"heartbeat", "paymentMessage"}


@settings(max_examples=200, deadline=2000)
@given(st.text(max_size=300))
def test_parse_xml_property_never_raises(text: str) -> None:
    result = parse_xml(text)
    assert result is None or isinstance(result.fields, dict)


@settings(max_examples=100, deadline=2000)
@given(st.text(alphabet="<>/&;!\"' ab=", max_size=200))
def test_parse_xml_property_markup_soup_never_raises(text: str) -> None:
    parse_xml(text)
