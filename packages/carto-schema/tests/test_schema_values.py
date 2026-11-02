"""Value shapes (ADR 0069, spec 2.3 invariants 2 and 5): ``template_text`` and every attribute
value refuse an email address, a UUID, a long hex run, a long digit run, a JWT, an AWS access
key id, a PEM header, a carto token, a URL with a password and a secret-named key with a value;
low-cardinality words, placeholders and short numbers pass; the messages name the shape and
never the text; ``mask_value_shapes`` writes the placeholder over each shape and keeps the key
of a secret assignment; every scan stays linear on hostile text."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from pydantic import ValidationError

from carto_schema.event import (
    MAX_ATTRIBUTE_VALUE_LEN,
    MAX_TEMPLATE_TEXT_LEN,
    PLACEHOLDER,
    VALUE_SHAPES,
    CanonicalEvent,
    mask_value_shapes,
    value_shape,
)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
TOKEN = "t1.q8Jm0h3cR2VfZp4Lx9sT1w"  # noqa: S105 - a synthetic token shape
SHAPED: dict[str, str] = {
    "a PEM header": "-----BEGIN RSA PRIVATE KEY-----",
    "a JWT": JWT,
    "a carto token": TOKEN,
    "an AWS access key id": "AKIAIOSFODNN7EXAMPLE",
    "a URL with a password": "postgres://carto:hunter2@db.internal/orders",
    "a secret-named key with a value": "password=hunter2",
    "an email address": "jane.doe@example.com",
    "a UUID": "123e4567-e89b-12d3-a456-426614174000",
    "a run of 16 or more hex characters": "0123456789abcdef",
    "a run of 6 or more digits": "order 000447 shipped",
}
"""One sample per shape, keyed by the words the error message uses."""
SECRET_PARTS = ("hunter2", "jane", "example.com", "123e4567", "0123456789abcdef", "000447")
"""Substrings of the samples that must never appear in an error."""
LONG_QUOTED_VALUE = "h" * 600
MORE_SHAPED: list[tuple[str, str, str]] = [
    ("a secret-named key with a value", f'password="{LONG_QUOTED_VALUE}"', 'password="<*>"'),
    ("a secret-named key with a value", f"token: '{LONG_QUOTED_VALUE}' x", "token: '<*>' x"),
    ("a URL with a password", "redis://:hunter2@cache:6379", "redis://:<*>@cache:6379"),
    ("a URL with a password", "dsn redis://:hunter2@cache/0 ok", "dsn redis://:<*>@cache/0 ok"),
    ("a secret-named key with a value", 'password="aaaa', "password=<*>"),
    ("a secret-named key with a value", "token='x", "token=<*>"),
    ("a secret-named key with a value", "secret={abc", "secret=<*>"),
    ("a secret-named key with a value", 'login with secret "hunter2"', 'login with secret "<*>"'),
]
"""Variants of a shape with the masked text: a quoted value longer than the 512-character bound
of the quoted alternative (read bare up to whitespace, quotes kept), a URL whose userinfo has a
password but no user name, an unterminated quoted or braced value (read bare, so it is still a
value) and a quoted value right after the word with no separator."""
HOSTILE_UNITS = ["password={ ", 'password="a', "token='"]
"""Unterminated openers; repeated to 256 KiB each scan must stay linear (review of 2026-11-02:
an unbounded quoted alternative made the braced opener quadratic, 15 s at this size)."""
HOSTILE_BYTES = 256 * 1024

CONSTANTS = [
    "approved",
    "EUR",
    "GET /orders",
    PLACEHOLDER,
    "HTTP/1.1 200",
    "DC-03",
    "INSERT purchase_orders",
    "GET /api/v1/orders/<*> HTTP/1.1 200",
    "retry 3 of 10000",
    "2026-10-06T21:12:03.412Z",
    "password=<*>",
    'pwd=""',
    "Authorization: Bearer <*>",
    '"token": "<*>"',
    "token: [REDACTED]",
    "token_count=3",
    "secret_ref=vault://x",
    "tokens=512",
    "bypass=1",
    "oauth=1",
    "auth failed",
    "postgres://carto@db.internal/orders",
    "redis://:<*>@cache:6379",
    "user@<*>",
    "deadbeef",
    "12345",
    "",
]
"""Template constants and low-cardinality values the edge legitimately sends in clear."""


def _example() -> dict[str, Any]:
    return CanonicalEvent.example().model_dump(mode="json")


def _errors(info: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, tuple[Any, ...]]]:
    return [(error["type"], tuple(error["loc"])) for error in info.value.errors()]


def _assert_no_echo(info: pytest.ExceptionInfo[ValidationError]) -> None:
    rendered = str(info.value) + repr(info.value)
    rendered += json.dumps(info.value.errors(include_input=False), default=str)
    for part in (*SECRET_PARTS, JWT, TOKEN, "AKIAIOSFODNN7EXAMPLE"):
        assert part not in rendered


# -- value_shape and mask_value_shapes -----------------------------------------------------------


def test_the_samples_cover_every_shape_in_order() -> None:
    assert list(SHAPED) == [name for name, _ in VALUE_SHAPES]


@pytest.mark.parametrize(("name", "text"), list(SHAPED.items()))
def test_value_shape_names_the_shape(name: str, text: str) -> None:
    assert value_shape(text) == name
    assert value_shape(f"seen {text} here") == name


@pytest.mark.parametrize(("name", "text", "masked"), MORE_SHAPED)
def test_shape_variants_are_named_and_masked(name: str, text: str, masked: str) -> None:
    assert value_shape(text) == name
    assert mask_value_shapes(text) == masked
    assert value_shape(masked) is None
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**_example(), "template_text": text})
    assert name in str(info.value)
    assert "hunter2" not in str(info.value) and LONG_QUOTED_VALUE not in str(info.value)


@pytest.mark.parametrize("text", CONSTANTS)
def test_constants_carry_no_value_shape_and_mask_to_themselves(text: str) -> None:
    assert value_shape(text) is None
    assert mask_value_shapes(text) == text


@pytest.mark.parametrize("text", list(SHAPED.values()))
def test_mask_value_shapes_writes_the_placeholder_over_the_shape(text: str) -> None:
    masked = mask_value_shapes(f"seen {text} here")
    assert PLACEHOLDER in masked
    assert value_shape(masked) is None
    for part in (*SECRET_PARTS, JWT, TOKEN, "IOSFODNN7EXAMPLE"):
        assert part not in masked


def test_mask_value_shapes_keeps_keys_user_names_and_quotes() -> None:
    assert mask_value_shapes("password=hunter2 user=bob") == "password=<*> user=bob"
    assert mask_value_shapes('{"password": "hunter 2", "x": 1}') == '{"password": "<*>", "x": 1}'
    assert mask_value_shapes("pass='x y' next") == "pass='<*>' next"
    assert mask_value_shapes(f"Authorization: Bearer {JWT}") == "Authorization: Bearer <*>"
    assert mask_value_shapes("Authorization: Basic dXNlcjpwYXNz") == "Authorization: Basic <*>"
    assert mask_value_shapes("dbPass=x api-key: k") == "dbPass=<*> api-key: <*>"
    assert (
        mask_value_shapes("postgres://carto:pw@db.internal") == "postgres://carto:<*>@db.internal"
    )
    assert mask_value_shapes("n=1234567 h=0123456789abcdef0123") == "n=<*> h=<*>", "whole runs"
    assert mask_value_shapes("Order <*> shipped to <*>") == "Order <*> shipped to <*>"
    pem = "-----BEGIN KEY-----\nabc\n-----END KEY----- tail"
    assert mask_value_shapes(pem) == "<*> tail"
    assert mask_value_shapes("-----BEGIN KEY----- unterminated") == "<*>"
    assert mask_value_shapes("mail jane@example.com id 123e4567-e89b-12d3-a456-426614174000") == (
        "mail <*> id <*>"
    )


@pytest.mark.parametrize(
    "text",
    [
        "eyJ" * (MAX_TEMPLATE_TEXT_LEN // 3),
        "eyJ." * (MAX_TEMPLATE_TEXT_LEN // 4),
        "-----BEGIN " * (MAX_TEMPLATE_TEXT_LEN // 11),
        "password= " * (MAX_TEMPLATE_TEXT_LEN // 10),
        'password="' * (MAX_TEMPLATE_TEXT_LEN // 10),
        "a@" * (MAX_TEMPLATE_TEXT_LEN // 2),
        "a.b" * (MAX_TEMPLATE_TEXT_LEN // 3),
        "://u:" * (MAX_TEMPLATE_TEXT_LEN // 5),
        "://:" * (MAX_TEMPLATE_TEXT_LEN // 4),
        'password="' + "a" * (MAX_TEMPLATE_TEXT_LEN - 10),
        "password='" + "a" * (MAX_TEMPLATE_TEXT_LEN - 10),
        'password="a' * (MAX_TEMPLATE_TEXT_LEN // 11),
        "pwd={" + "a" * (MAX_TEMPLATE_TEXT_LEN - 5),
        "0123456789abcde-" * (MAX_TEMPLATE_TEXT_LEN // 16),
    ],
)
def test_scans_stay_linear_on_hostile_text(text: str) -> None:
    started = time.perf_counter()
    for _ in range(5):
        value_shape(text)
        mask_value_shapes(text)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("unit", HOSTILE_UNITS)
def test_an_unterminated_opener_costs_linear_time_at_256_kib(unit: str) -> None:
    """Spec 2.3 invariant 8: 256 KiB of a repeated unterminated opener is masked in well under
    a second (the quadratic form took 15 s)."""
    text = (unit * (HOSTILE_BYTES // len(unit) + 1))[:HOSTILE_BYTES]
    started = time.perf_counter()
    value_shape(text)
    assert time.perf_counter() - started < 1.0
    started = time.perf_counter()
    masked = mask_value_shapes(text)
    assert time.perf_counter() - started < 1.0
    assert PLACEHOLDER in masked


# -- the contract --------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "text"), list(SHAPED.items()))
def test_an_attribute_value_with_a_value_shape_is_refused_without_echo(
    name: str, text: str
) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**_example(), "attributes": {"status": text}})
    assert ("value_error", ("attributes", "status")) in _errors(info)
    assert name in str(info.value)
    _assert_no_echo(info)


@pytest.mark.parametrize(("name", "text"), list(SHAPED.items()))
def test_template_text_with_a_value_shape_is_refused_without_echo(name: str, text: str) -> None:
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate({**_example(), "template_text": f"seen {text} here"})
    assert ("value_error", ("template_text",)) in _errors(info)
    assert name in str(info.value)
    _assert_no_echo(info)


@pytest.mark.parametrize("text", CONSTANTS)
def test_constants_are_accepted_in_attributes_and_template_text(text: str) -> None:
    event = CanonicalEvent.model_validate(
        {**_example(), "template_text": text, "attributes": {"status": text}}
    )
    assert event.template_text == text
    assert event.attributes == {"status": text}


def test_the_length_cap_still_applies_before_the_shape_rule() -> None:
    data = {**_example(), "attributes": {"status": "x" * (MAX_ATTRIBUTE_VALUE_LEN + 1)}}
    with pytest.raises(ValidationError) as info:
        CanonicalEvent.model_validate(data)
    assert _errors(info) == [("string_too_long", ("attributes", "status"))]
