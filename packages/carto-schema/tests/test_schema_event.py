"""CanonicalEvent: the spec 7.1 example, round trips, limits and the timestamp rule."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from carto_schema.event import (
    MAX_ATTRIBUTE_KEY_LEN,
    MAX_ATTRIBUTE_VALUE_LEN,
    MAX_ATTRIBUTES,
    MAX_DROPPED_FIELDS,
    MAX_IDENTIFIERS_PER_EVENT,
    MAX_TEMPLATE_TEXT_LEN,
    SCHEMA_VERSION,
    Actor,
    ActorKind,
    CanonicalEvent,
    EventKind,
    Identifier,
    ObservedAtQuality,
    Redaction,
    Severity,
)
from carto_schema.forms import shape

# The spec 7.1 example verbatim, with the four tokens the spec abbreviates spelled out in full.
SPEC_EXAMPLE_JSON = """
{
  "schema_version": "1",
  "event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0A",
  "tenant_id": "default",
  "source_id": "src_wms_db",
  "system_id": "sys_warehouse",
  "kind": "row_change",
  "observed_at": "2026-10-06T21:12:03.412Z",
  "ingested_at": "2026-10-06T21:12:09.020Z",
  "observed_at_quality": "source",
  "template_id": "tpl_4f1c9a",
  "template_text": "INSERT purchase_orders",
  "severity": null,
  "attributes": {
    "status": "CREATED",
    "warehouse_code": "DC-03"
  },
  "identifiers": [
    {
      "field": "po_num",
      "form": "raw",
      "token": "t1.q8Jm0h3cR2VfZp4Lx9sT1w",
      "shape": "99-999",
      "len": 6
    },
    {
      "field": "po_num",
      "form": "alnum",
      "token": "t1.Gk2Wq7nXf4Lr9bT0sYv3Ez",
      "shape": "99999",
      "len": 5
    },
    {
      "field": "order_ref",
      "form": "raw",
      "token": "t1.Yd7Rm2Kp8Vx1Qs6Nc4Tw0H",
      "shape": "AA-9999999",
      "len": 10
    },
    {
      "field": "order_ref",
      "form": "digits.0",
      "token": "t1.Pz1Lk5Xr8Mw2Bd6Qn9Ct3F",
      "shape": "9999",
      "len": 4
    }
  ],
  "actor": { "token": "t1.Hh3Vq6Zt1Nm4Rk8Pw2Ls7D", "kind": "human" },
  "dropped_fields": ["customer_name", "ship_to_address"],
  "redaction": { "policy_version": "3", "entities_masked": 0 }
}
"""


def make_token(seed: int, version: int = 1) -> str:
    """A syntactically valid synthetic token whose body is the seed zero-padded to 22 digits."""
    return f"t{version}.{seed:022d}"


def example_dict() -> dict[str, Any]:
    """The example event as JSON-shaped data, safe to mutate."""
    return CanonicalEvent.example().model_dump(mode="json")


def identifier_dict(index: int, form: str = "raw") -> dict[str, Any]:
    return {
        "field": f"field_{index}",
        "form": form,
        "token": make_token(index),
        "shape": "9999",
        "len": 4,
    }


def errors_of(info: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, tuple[Any, ...]]]:
    return [(error["type"], tuple(error["loc"])) for error in info.value.errors()]


# -- the example ---------------------------------------------------------------------------------


def test_example_validates_and_matches_the_spec() -> None:
    event = CanonicalEvent.example()
    assert event == CanonicalEvent.model_validate_json(SPEC_EXAMPLE_JSON)
    assert event.model_dump(mode="json") == json.loads(SPEC_EXAMPLE_JSON)
    assert event.schema_version == SCHEMA_VERSION == "1"
    assert event.kind is EventKind.ROW_CHANGE
    assert event.observed_at_quality is ObservedAtQuality.SOURCE
    assert event.severity is None
    assert event.actor == Actor.model_validate(
        {"token": "t1.Hh3Vq6Zt1Nm4Rk8Pw2Ls7D", "kind": "human"}
    )
    assert event.actor.kind is ActorKind.HUMAN
    assert event.redaction == Redaction(policy_version="3", entities_masked=0)
    assert [(i.field, i.form) for i in event.identifiers] == [
        ("po_num", "raw"),
        ("po_num", "alnum"),
        ("order_ref", "raw"),
        ("order_ref", "digits.0"),
    ]
    assert all(len(identifier.token) == 25 for identifier in event.identifiers)


def test_example_returns_equal_but_distinct_instances() -> None:
    first, second = CanonicalEvent.example(), CanonicalEvent.example()
    assert first == second
    assert first is not second


# -- round trips ----------------------------------------------------------------------------------


def test_round_trip_model_to_json_to_model() -> None:
    event = CanonicalEvent.example()
    assert CanonicalEvent.model_validate_json(event.model_dump_json()) == event
    assert CanonicalEvent.model_validate(event.model_dump(mode="json")) == event
    assert CanonicalEvent.model_validate(event.model_dump()) == event


def test_round_trip_json_to_model_to_json_is_byte_identical() -> None:
    compact_spec = json.dumps(json.loads(SPEC_EXAMPLE_JSON), separators=(",", ":"))
    dumped = CanonicalEvent.model_validate_json(compact_spec).model_dump_json()
    assert dumped == compact_spec
    assert dumped.encode("utf-8") == compact_spec.encode("utf-8")
    assert CanonicalEvent.example().model_dump_json() == compact_spec


def test_json_key_order_follows_the_spec() -> None:
    assert list(json.loads(CanonicalEvent.example().model_dump_json())) == [
        "schema_version",
        "event_id",
        "tenant_id",
        "source_id",
        "system_id",
        "kind",
        "observed_at",
        "ingested_at",
        "observed_at_quality",
        "template_id",
        "template_text",
        "severity",
        "attributes",
        "identifiers",
        "actor",
        "dropped_fields",
        "redaction",
    ]


def test_optional_fields_default_and_serialize_as_null_or_empty() -> None:
    data = example_dict()
    for key in ("severity", "actor", "dropped_fields"):
        del data[key]
    event = CanonicalEvent.model_validate(data)
    assert event.severity is None
    assert event.actor is None
    assert event.dropped_fields == []
    dumped = json.loads(event.model_dump_json())
    assert dumped["severity"] is None
    assert dumped["actor"] is None
    assert dumped["dropped_fields"] == []
    assert CanonicalEvent.model_validate_json(event.model_dump_json()) == event


# -- enums and constants --------------------------------------------------------------------------


def test_enum_values_match_the_spec() -> None:
    assert [k.value for k in EventKind] == [
        "log",
        "row_change",
        "file_arrived",
        "file_removed",
        "http_access",
        "webhook",
    ]
    assert [q.value for q in ObservedAtQuality] == ["source", "ingest", "inferred"]
    assert [a.value for a in ActorKind] == ["human", "service", "unknown"]
    assert [s.value for s in Severity] == ["trace", "debug", "info", "warn", "error", "fatal"]


def test_limits_match_the_spec() -> None:
    assert MAX_IDENTIFIERS_PER_EVENT == 64
    assert MAX_ATTRIBUTE_VALUE_LEN == 256
    assert MAX_ATTRIBUTES == 256
    assert MAX_TEMPLATE_TEXT_LEN == 4096
    assert MAX_DROPPED_FIELDS == 1024
    assert MAX_ATTRIBUTE_KEY_LEN == 128


@pytest.mark.parametrize("severity", [s.value for s in Severity])
def test_every_severity_is_accepted(severity: str) -> None:
    event = CanonicalEvent.model_validate({**example_dict(), "severity": severity})
    assert event.severity == severity
    assert json.loads(event.model_dump_json())["severity"] == severity


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("severity", "critical"),
        ("kind", "insert"),
        ("observed_at_quality", "guessed"),
        ("schema_version", "2"),
        ("schema_version", 1),
    ],
)
def test_enum_and_version_rejections(key: str, value: object) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), key: value})
    assert errors_of(info)[0][1] == (key,)


# -- limits ---------------------------------------------------------------------------------------


def test_identifier_count_limit() -> None:
    data = example_dict()
    data["identifiers"] = [identifier_dict(i) for i in range(MAX_IDENTIFIERS_PER_EVENT)]
    assert len(CanonicalEvent.model_validate(data).identifiers) == MAX_IDENTIFIERS_PER_EVENT
    data["identifiers"].append(identifier_dict(MAX_IDENTIFIERS_PER_EVENT))
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert ("too_long", ("identifiers",)) in errors_of(info)


def test_duplicate_field_and_form_is_rejected() -> None:
    data = example_dict()
    data["identifiers"] = [identifier_dict(1), {**identifier_dict(2), "field": "field_1"}]
    with pytest.raises(ValidationError, match="duplicate identifier") as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("value_error", ("identifiers",))]


def test_same_field_and_form_under_two_key_versions_is_accepted() -> None:
    # Spec 8.4 rotation: the edge dual-tokenizes, one token per live key version (ADR 0023).
    data = example_dict()
    second = {**identifier_dict(1), "token": "t2." + make_token(2).partition(".")[2]}
    data["identifiers"] = [identifier_dict(1), second]
    assert len(CanonicalEvent.model_validate(data).identifiers) == 2


def test_same_field_form_and_key_version_is_rejected() -> None:
    data = example_dict()
    data["identifiers"] = [identifier_dict(1), {**identifier_dict(2), "field": "field_1"}]
    with pytest.raises(ValidationError, match="key version") as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("value_error", ("identifiers",))]


def test_same_field_with_different_forms_is_accepted() -> None:
    data = example_dict()
    data["identifiers"] = [
        {**identifier_dict(1), "form": "raw"},
        {**identifier_dict(2), "field": "field_1", "form": "norm"},
        {**identifier_dict(3), "field": "field_1", "form": "digits.0"},
    ]
    assert len(CanonicalEvent.model_validate(data).identifiers) == 3


def test_attribute_value_length_limit() -> None:
    data = example_dict()
    data["attributes"] = {"status": "x" * MAX_ATTRIBUTE_VALUE_LEN}
    CanonicalEvent.model_validate(data)
    data["attributes"] = {"status": "x" * (MAX_ATTRIBUTE_VALUE_LEN + 1)}
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert ("string_too_long", ("attributes", "status")) in errors_of(info)


def _names(count: int) -> list[str]:
    """``count`` distinct field names without digit runs (``a.a``, ``a.b``, ...)."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    return [
        f"{letters[i // 676 % 26]}{letters[i // 26 % 26]}.{letters[i % 26]}" for i in range(count)
    ]


def test_attribute_count_limit() -> None:
    data = example_dict()
    data["attributes"] = dict.fromkeys(_names(MAX_ATTRIBUTES), "v")
    CanonicalEvent.model_validate(data)
    data["attributes"]["one_more"] = "v"
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert ("too_long", ("attributes",)) in errors_of(info)


@pytest.mark.parametrize("key", ["", ("k" * 63 + ".") * 2 + "kk"])
def test_attribute_key_length_limits(key: str) -> None:
    data = example_dict()
    longest = ("k" * 63 + ".") * 2  # two segments of 63 and a dot each: 128 characters
    assert len(longest) == MAX_ATTRIBUTE_KEY_LEN
    data["attributes"] = {longest: "v"}
    CanonicalEvent.model_validate(data)
    data["attributes"] = {key: "v"}
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    # The key is not part of the location: a rejected key may be a value (spec 2.3 #2).
    assert errors_of(info) == [("value_error", ("attributes",))]


def test_attribute_values_must_be_strings() -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), "attributes": {"count": 3}})
    assert ("string_type", ("attributes", "count")) in errors_of(info)


def test_template_text_length_limit() -> None:
    data = example_dict()
    data["template_text"] = "x" * MAX_TEMPLATE_TEXT_LEN
    CanonicalEvent.model_validate(data)
    data["template_text"] = "x" * (MAX_TEMPLATE_TEXT_LEN + 1)
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert ("string_too_long", ("template_text",)) in errors_of(info)


def test_dropped_fields_limits() -> None:
    data = example_dict()
    data["dropped_fields"] = _names(MAX_DROPPED_FIELDS)
    CanonicalEvent.model_validate(data)
    data["dropped_fields"].append("one_more")
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert ("too_long", ("dropped_fields",)) in errors_of(info)
    data["dropped_fields"] = [""]
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert ("string_too_short", ("dropped_fields", 0)) in errors_of(info)


def test_unknown_key_is_rejected_at_every_level() -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), "raw_message": "never"})
    assert errors_of(info) == [("extra_forbidden", ("raw_message",))]

    data = example_dict()
    data["identifiers"][0]["value"] = "88-210"
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("extra_forbidden", ("identifiers", 0, "value"))]

    data = example_dict()
    data["actor"]["name"] = "clerk"
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("extra_forbidden", ("actor", "name"))]

    data = example_dict()
    data["redaction"]["policy"] = "x"
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("extra_forbidden", ("redaction", "policy"))]


@pytest.mark.parametrize(
    "token",
    [
        "t0.q8Jm0h3cR2VfZp4Lx9sT1w",
        "t1.q8Jm0h3cR2VfZp4Lx9sT1",
        "t1.q8Jm0h3cR2VfZp4Lx9sT1wX",
        "t1.q8Jm0h3cR2VfZp4Lx9sT1=",
        "q8Jm0h3cR2VfZp4Lx9sT1w",
        "",
    ],
)
def test_bad_token_is_rejected_in_identifier_and_actor(token: str) -> None:
    data = example_dict()
    data["identifiers"][0]["token"] = token
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("string_pattern_mismatch", ("identifiers", 0, "token"))]

    data = example_dict()
    data["actor"]["token"] = token
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("string_pattern_mismatch", ("actor", "token"))]


@pytest.mark.parametrize(
    "event_id",
    [
        "01j9zk8x5q8v3n6m2t4r7w1y0a",
        "01J9ZK8X5Q8V3N6M2T4R7W1Y0",
        "01J9ZK8X5Q8V3N6M2T4R7W1Y0AA",
        "01J9ZK8X5Q8V3N6M2T4R7W1YIA",
        "01J9ZK8X5Q8V3N6M2T4R7W1YLA",
        "01J9ZK8X5Q8V3N6M2T4R7W1YOA",
        "01J9ZK8X5Q8V3N6M2T4R7W1YUA",
        "81J9ZK8X5Q8V3N6M2T4R7W1Y0A",
        "",
        "01J9ZK8X5Q8V3N6M2T4R7W1Y0A\n",
    ],
)
def test_bad_event_id_is_rejected(event_id: str) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), "event_id": event_id})
    assert errors_of(info) == [("string_pattern_mismatch", ("event_id",))]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tenant_id", "Default"),
        ("tenant_id", "-tenant"),
        ("tenant_id", "a" * 65),
        ("tenant_id", ""),
        ("source_id", "1src"),
        ("source_id", "src-wms"),
        ("source_id", "Src_wms"),
        ("source_id", "s" * 65),
        ("system_id", "_sys"),
        ("system_id", "sys warehouse"),
        ("template_id", ""),
        ("template_id", "t" * 65),
        ("template_id", "tpl/4f1c9a"),
        ("template_id", "tpl 4f1c9a"),
    ],
)
def test_bad_ids_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), key: value})
    assert errors_of(info) == [("string_pattern_mismatch", (key,))]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tenant_id", "0"),
        ("tenant_id", "acme-corp_eu"),
        ("tenant_id", "t" * 64),
        ("source_id", "s"),
        ("source_id", "src_ship_sftp"),
        ("template_id", "tpl:4f1c9a.v2-b"),
        ("template_id", "T" * 64),
    ],
)
def test_edge_ids_are_accepted(key: str, value: str) -> None:
    assert getattr(CanonicalEvent.model_validate({**example_dict(), key: value}), key) == value


def test_identifier_field_limits() -> None:
    longest = ".".join(["f" * 64] * 3 + ["f" * 61])  # 256 characters in 64-character segments
    ok = Identifier(field=longest, form="raw", token=make_token(1), shape="A9" * 32, len=1)
    assert ok.len == 1
    assert len(ok.shape) == 64
    for bad in (
        {"field": ""},
        {"field": longest + "f"},
        {"shape": ""},
        {"shape": "A9" * 33},
        {"shape": "SO-0004471"},
        {"shape": "9" * 13},
        {"shape": "a9"},
        {"len": 0},
        {"len": -1},
        {"len": True},
        {"len": "4"},
        {"len": 4.0},
        {"form": "digits.3"},
    ):
        with pytest.raises(ValidationError):
            Identifier.model_validate({**identifier_dict(1), **bad})


# -- names that carry values (spec 2.3 invariant 2, ADR 0027) ----------------------------------

VALUE_NAMES = [
    "customers.jane.doe@example.com.SO-0004471",  # an email address and an order number
    "Alice Smith 4111111111111111",  # a card number in an attribute key
    "ssn 078-05-1120",  # a social security number in a dropped field name
    "orders.SO-0004471.status",  # a JSON map keyed by order number
    "stock.SKU-123.qty",  # three digits are a value, not a name
    "a" * 65,  # a segment longer than any field name: a value
    "notes.x" + "y" * 64,
    "user.jdoe@corp",  # @ after the first character
]
EDGE_NAMES = [
    "status",
    "payload.order.id",
    "balances.0.key",  # a map entry the name gate numbered (ADR 0030)
    "balances.19.value.amount",
    "items.3.sku",  # an array index
    "*",  # the mask for a value-like segment
    "stock.*.qty",
    "@id",  # an XML attribute
    "order.@currency",
    "param_0",
    "msg.param_12",
    "a" * 64,
    "utf8_name",
    "ipv4.addr",
]


@pytest.mark.parametrize("name", VALUE_NAMES)
def test_names_that_carry_values_are_rejected_everywhere(name: str) -> None:
    """Core's backstop for the edge's name masking: a field name, attribute key or dropped
    field that looks like a value is refused, and the error does not repeat it."""
    for data in (
        {**example_dict(), "attributes": {name: "v"}},
        {**example_dict(), "dropped_fields": [name]},
        {**example_dict(), "identifiers": [{**identifier_dict(1), "field": name}]},
    ):
        with pytest.raises(ValidationError) as info:
            CanonicalEvent.model_validate(data)
        for piece in name.replace("@", ".").split("."):
            if len(piece) >= 4:
                assert piece not in str(info.value)
                assert piece not in json.dumps(
                    info.value.errors(include_input=False, include_context=False, include_url=False)
                )


@pytest.mark.parametrize("name", EDGE_NAMES)
def test_names_the_edge_emits_are_accepted(name: str) -> None:
    data = {
        **example_dict(),
        "attributes": {name: "v"},
        "dropped_fields": [name],
        "identifiers": [{**identifier_dict(1), "field": name}],
    }
    event = CanonicalEvent.model_validate(data)
    assert event.dropped_fields == [name]


@pytest.mark.parametrize(
    "text", ["refund for order 0004471 issued", "card 4111111111111111", "id=123456"]
)
def test_template_text_with_a_long_digit_run_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), "template_text": text})
    assert ("value_error", ("template_text",)) in errors_of(info)
    assert text not in str(info.value)


@pytest.mark.parametrize(
    "text", ["GET /api/v1/orders/<*> HTTP/1.1 200", "retry 3 of 10000", "keys:a,b,c", ""]
)
def test_template_text_with_short_numbers_is_accepted(text: str) -> None:
    event = CanonicalEvent.model_validate({**example_dict(), "template_text": text})
    assert event.template_text == text


def test_shape_must_be_a_shape() -> None:
    # A raw value in the shape slot would carry clear text to core (spec 2.3 invariant 2).
    data = example_dict()
    data["identifiers"][0]["shape"] = "SO-0004471"
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("value_error", ("identifiers", 0, "shape"))]
    assert "SO-0004471" not in str(info.value)
    assert "SO-0004471" not in repr(info.value)
    data["identifiers"][0]["shape"] = shape("SO-0004471")
    assert CanonicalEvent.model_validate(data).identifiers[0].shape == "AA-9999999"


@pytest.mark.parametrize("value", [True, False, "4", 4.0, 4.5])
def test_len_and_entities_masked_are_strict_integers(value: object) -> None:
    # The exported schema says ``type: integer``; lax coercion would accept what it rejects.
    data = example_dict()
    data["identifiers"][0]["len"] = value
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("int_type", ("identifiers", 0, "len"))]
    data = example_dict()
    data["redaction"]["entities_masked"] = value
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert errors_of(info) == [("int_type", ("redaction", "entities_masked"))]


def test_strict_integers_apply_in_json_mode_too() -> None:
    text = CanonicalEvent.example().model_dump_json()
    assert text.count('"len":6') == 1
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate_json(text.replace('"len":6', '"len":true'))
    assert errors_of(info) == [("int_type", ("identifiers", 0, "len"))]
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate_json(text.replace('"len":6', '"len":"6"'))
    assert errors_of(info) == [("int_type", ("identifiers", 0, "len"))]


def test_validation_errors_do_not_echo_the_rejected_input() -> None:
    # Spec 2.3 invariant 7: a rejected value may be clear text; str()/repr() must not carry it.
    marker = "PHI-MARKER-4471"
    data = example_dict()
    data["attributes"]["status"] = marker + "x" * MAX_ATTRIBUTE_VALUE_LEN
    data["identifiers"][0]["token"] = marker
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert {error[0] for error in errors_of(info)} == {"string_too_long", "string_pattern_mismatch"}
    assert marker not in str(info.value)
    assert marker not in repr(info.value)
    assert marker not in json.dumps(info.value.errors(include_input=False), default=str)
    assert marker not in info.value.json(include_input=False)
    # errors() and json() keep the input by default: whatever logs or returns them must opt out.
    assert marker in info.value.json()


def test_redaction_limits() -> None:
    for bad in ({"policy_version": ""}, {"policy_version": "v" * 33}, {"entities_masked": -1}):
        with pytest.raises(ValidationError):
            Redaction.model_validate({"policy_version": "3", "entities_masked": 0, **bad})


def test_models_are_frozen() -> None:
    event = CanonicalEvent.example()
    with pytest.raises(ValidationError):
        event.template_text = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        event.identifiers[0].len = 9  # type: ignore[misc]


# -- timestamps -----------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["observed_at", "ingested_at"])
def test_naive_timestamp_is_rejected(key: str) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), key: "2026-10-06T21:12:03.412"})
    assert errors_of(info) == [("timezone_aware", (key,))]

    naive = CanonicalEvent.example().model_dump() | {key: datetime(2026, 10, 6, 21, 12, 3)}
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(naive)
    assert errors_of(info) == [("timezone_aware", (key,))]


def test_microseconds_are_truncated_to_milliseconds_and_serialized_with_z() -> None:
    data = example_dict() | {
        "observed_at": datetime(2026, 10, 6, 21, 12, 3, 412999, tzinfo=UTC),
        "ingested_at": "2026-10-06T21:12:09.020999Z",
    }
    event = CanonicalEvent.model_validate(data)
    assert event.observed_at == datetime(2026, 10, 6, 21, 12, 3, 412000, tzinfo=UTC)
    assert event.ingested_at.microsecond == 20000
    dumped = json.loads(event.model_dump_json())
    assert dumped["observed_at"] == "2026-10-06T21:12:03.412Z"
    assert dumped["ingested_at"] == "2026-10-06T21:12:09.020Z"


def test_timestamps_are_converted_to_utc() -> None:
    new_york = datetime(2026, 10, 6, 17, 12, 3, 412000, tzinfo=ZoneInfo("America/New_York"))
    plus_0530 = datetime(2026, 10, 7, 2, 42, 9, 20000, tzinfo=timezone(timedelta(hours=5.5)))
    event = CanonicalEvent.model_validate(
        example_dict() | {"observed_at": new_york, "ingested_at": plus_0530}
    )
    assert event.observed_at.tzinfo is UTC
    assert event.observed_at == datetime(2026, 10, 6, 21, 12, 3, 412000, tzinfo=UTC)
    assert event.ingested_at == datetime(2026, 10, 6, 21, 12, 9, 20000, tzinfo=UTC)
    assert event == CanonicalEvent.example()
    dumped = json.loads(event.model_dump_json())
    assert dumped["observed_at"] == "2026-10-06T21:12:03.412Z"
    assert dumped["ingested_at"] == "2026-10-06T21:12:09.020Z"

    offset_text = CanonicalEvent.model_validate(
        example_dict() | {"observed_at": "2026-10-06T17:12:03.412-04:00"}
    )
    assert offset_text.observed_at == event.observed_at


def test_whole_seconds_serialize_with_three_fraction_digits() -> None:
    event = CanonicalEvent.model_validate(example_dict() | {"observed_at": "2026-10-06T21:12:03Z"})
    assert json.loads(event.model_dump_json())["observed_at"] == "2026-10-06T21:12:03.000Z"


def test_python_mode_dump_keeps_datetimes() -> None:
    dumped = CanonicalEvent.example().model_dump()
    assert isinstance(dumped["observed_at"], datetime)
    assert dumped["observed_at"].tzinfo is UTC


def test_timestamp_out_of_range_after_utc_conversion_is_a_validation_error() -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(example_dict() | {"observed_at": "0001-01-01T00:00:00+05:00"})
    assert errors_of(info) == [("value_error", ("observed_at",))]


@pytest.mark.parametrize("value", ["", "yesterday", "2026-13-01T00:00:00Z", 1700000000000, None])
def test_unparseable_timestamps_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**example_dict(), "observed_at": value})
    assert errors_of(info)[0][1] == ("observed_at",)
