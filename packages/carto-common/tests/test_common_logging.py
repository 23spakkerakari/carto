"""carto_common.logging: every redaction rule, nesting, never-raise and configuration.

Spec 14.12 and 2.3 invariant 7: the product's own logs never contain secrets or raw identifier
values; the processor enforces it and these tests verify it.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import io
import json
import logging
import re
import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
import structlog
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from carto_common.ids import new_ulid, ulid_from_parts
from carto_common.logging import (
    AWS_KEY_MASK,
    DEFAULT_SERVICE,
    DROPPED,
    EMAIL_MASK,
    HIGH_ENTROPY_MASK,
    JWT_MASK,
    MAX_DEPTH,
    METRIC_KEY_QUALIFIERS,
    PEM_MASK,
    QUIET_LIBRARY_LOGGERS,
    REDACTED,
    REDACTION_ERROR,
    TOKEN_MASK,
    configure_logging,
    get_logger,
    identifier_shape,
    is_drop_key,
    is_metric_key,
    is_secret_key,
    mask_string,
    redact,
)

# Markers are assembled at runtime so that no secret-shaped literal sits in the source tree (CI
# secret scanners would flag it) and so that each is exactly what the rule targets.


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(
        json.dumps({"sub": "1234567890", "name": "Jane Roe", "iat": 1516239022}).encode()
    )
    return f"{header}.{claims}.{_b64url(bytes(range(32)))}"


def _carto_token() -> str:
    return "t1." + _b64url(bytes(range(16, 33)))[:22]


def _aws_key() -> str:
    return "AKIA" + "IOSFODNN7EXAMPLE"


def _pem() -> str:
    return "\n".join(
        [
            "-----BEGIN " + "PRIVATE KEY-----",
            _b64url(bytes(range(48))),
            "-----END " + "PRIVATE KEY-----",
        ]
    )


JWT = _jwt()
CARTO_TOKEN = _carto_token()
AWS_KEY = _aws_key()
PEM = _pem()
IDENTIFIER = "X9-0442"
HEX_MARKER = "mk0f1e2d3c"  # the simulator's leak marker shape (spec 18.3): mk plus 8 hex digits
EMAIL = f"jane.roe+{HEX_MARKER}@example.com"  # how the simulator plants it in a contact field
HIGH_ENTROPY = "aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ5-_"
MARKERS = (CARTO_TOKEN, JWT, AWS_KEY, IDENTIFIER, PEM, HEX_MARKER, EMAIL)
PLANTED = "hunter2"
CREDENTIAL_KEY = "password"


def _redact(**fields: Any) -> dict[str, Any]:
    return dict(redact(None, "info", {"event": "probe", **fields}))


@pytest.fixture
def clean_structlog() -> Iterator[None]:
    """Reset structlog, and put the root ``logging`` logger back the way pytest had it."""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    hooks_before = (sys.excepthook, threading.excepthook, sys.unraisablehook, warnings.showwarning)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    sys.excepthook, threading.excepthook, sys.unraisablehook, warnings.showwarning = hooks_before
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
    for handler in handlers_before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level_before)


# ---------------------------------------------------------------------------------------------
# Rule 1: secret-named keys
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "api-key",
        "api.key",
        "apiKey",
        "ApiKey",
        "x-api-key",
        "X-Api-Key",
        "authorization",
        "Authorization",
        "cookie",
        "private_key",
        "privateKey",
        "client_secret",
        "clientSecret",
        "access_key",
        "aws_secret_access_key",
        "session",
        "bearer",
        "auth_token",
        "AUTH_TOKEN",
        "accessToken",
        "db.password",
        "user.auth.token",
        "oauth bearer",
        "secret_value",
        "password_hash",
        "password_plain",
        "token_value",
        "session_id",
        "sessionId",
        "cookie_value",
        "private_key_pem",
        "client_secret_value",
        "api_key_value",
        "client_secret_v2",
        "password2",
        "oauth2_token",
        "s3_access_key",
        "HTTPToken",
        "cookies",
        "tokens",
        "secrets",
        "passwords",
        "credentials",
        "credential",
        "api_keys",
        "pwd",
        "PWD",
        "db_pwd",
        "passphrase",
        "key_passphrase",
        "pass",
        "db_pass",
        "dbPass",
        "auth",
        "AUTH",
        "proxy_auth",
        "signature",
        "request_signature",
        "sig",
        "url_sig",
    ],
)
def test_secret_named_keys_are_redacted(key: str) -> None:
    out = _redact(**{key: PLANTED})
    assert out[key] == REDACTED
    assert is_secret_key(key)
    assert PLANTED not in json.dumps(out)


@pytest.mark.parametrize(
    "key",
    [
        "token_count",
        "secret_ref",
        "session_count",
        "order_id",
        "api_version",
        "bearer_count",
        "token_type",
        "secret_name",
        "api_key_name",
        "cookie_name",
        "token_expiry",
        "token_expires",
        "session_ttl",
        "token_present",
        "private_key_size",
        "key_prefix",
        "tokenCount",
        "auth_scheme",
        "signature_scheme",
        "pass_count",
        "bypass",
        "compass",
        "author",
        "design",
        "signing_key_id",
    ],
)
def test_keys_ending_in_a_qualifier_survive(key: str) -> None:
    out = _redact(**{key: "kept"})
    assert out[key] == "kept"
    assert not is_secret_key(key)


def test_secret_named_key_hides_nested_values_wholesale() -> None:
    out = _redact(connector={"token": {"value": PLANTED, "count": 3}})
    assert out["connector"] == {"token": REDACTED}
    assert _redact(credentials={"user": "svc", "password": PLANTED})["credentials"] == REDACTED


# ---------------------------------------------------------------------------------------------
# Rule 2: bodies are never logged
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "body",
        "request_body",
        "response_body",
        "payload",
        "raw",
        "raw_value",
        "raw_values",
        "values",
        "record",
        "requestBody",
        "Response-Body",
        "RAW",
        "raw.value",
    ],
)
def test_body_keys_are_dropped(key: str) -> None:
    out = _redact(**{key: {"anything": [1, 2, 3]}})
    assert out[key] == DROPPED
    assert is_drop_key(key)
    assert not is_drop_key(key + "_size")


def test_body_keys_are_dropped_at_any_depth() -> None:
    out = _redact(http={"status": 200, "request_body": "SO-0004471", "response_body": b"x"})
    assert out["http"] == {"status": 200, "request_body": DROPPED, "response_body": DROPPED}


# ---------------------------------------------------------------------------------------------
# Rule 3: string masks
# ---------------------------------------------------------------------------------------------


def test_carto_tokens_are_masked() -> None:
    assert mask_string(f"token {CARTO_TOKEN} rejected") == f"token {TOKEN_MASK} rejected"
    assert mask_string(f"tokens={CARTO_TOKEN},{CARTO_TOKEN}") == f"tokens={TOKEN_MASK},{TOKEN_MASK}"
    assert mask_string("t1234." + "a" * 22) == TOKEN_MASK
    assert _redact(identifier=CARTO_TOKEN)["identifier"] == TOKEN_MASK


@pytest.mark.parametrize(
    "value", ["t1.tooshort", "t1." + "a" * 21, "tx." + "a" * 22, "t." + "a" * 22]
)
def test_strings_that_are_not_carto_tokens_survive(value: str) -> None:
    assert mask_string(value) == value


def test_jwts_are_masked() -> None:
    assert mask_string(f"Bearer {JWT}") == f"Bearer {JWT_MASK}"
    assert mask_string(JWT) == JWT_MASK
    assert mask_string(f"auth='{JWT}'") == f"auth='{JWT_MASK}'"
    assert mask_string(f"token={JWT}") == f"token={JWT_MASK}"
    header, claims, _ = JWT.split(".")
    assert mask_string(f"{header}.{claims}.") == JWT_MASK  # unsigned ("alg": "none") form


@pytest.mark.parametrize("value", ["eyJ.only", "xyz.abc.def", "eyJhbGci", "a.b.c"])
def test_strings_that_are_not_jwts_survive(value: str) -> None:
    assert mask_string(value) == value


def test_pem_blocks_are_masked_across_lines() -> None:
    assert mask_string(PEM) == PEM_MASK
    assert mask_string(f"key:\n{PEM}\nend") == f"key:\n{PEM_MASK}\nend"
    cert = PEM.replace("PRIVATE KEY", "CERTIFICATE")
    assert mask_string(cert + "\n" + PEM) == f"{PEM_MASK}\n{PEM_MASK}"
    assert mask_string(PEM.replace("\n", "\r\n")) == PEM_MASK


def test_truncated_pem_body_is_still_hidden() -> None:
    begin, body, _ = PEM.split("\n")
    masked = mask_string(f"{begin}\n{body}")
    assert body not in masked
    assert HIGH_ENTROPY_MASK in masked


def test_strings_that_are_not_pem_survive() -> None:
    assert mask_string("BEGIN and END without dashes") == "BEGIN and END without dashes"
    assert mask_string("--- not a block ---") == "--- not a block ---"


def test_aws_access_key_ids_are_masked() -> None:
    assert mask_string(f"key {AWS_KEY} used") == f"key {AWS_KEY_MASK} used"
    assert mask_string("ASIA" + AWS_KEY[4:]) == AWS_KEY_MASK
    assert mask_string(f"id={AWS_KEY}") == f"id={AWS_KEY_MASK}"


@pytest.mark.parametrize("value", ["AKIA" + "IOSFODNN7EXAMPL", "akia" + "iosfodnn7example", "AKIA"])
def test_strings_that_are_not_aws_keys_survive(value: str) -> None:
    assert mask_string(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"postgres://carto:{PLANTED}@db/carto", f"postgres://carto:{REDACTED}@db/carto"),
        (
            "sftp://svc:Winter-Is-Coming@sftp.example.com/drop",
            f"sftp://svc:{REDACTED}@sftp.example.com/drop",
        ),
        (
            f"https://user:{PLANTED}@api.example.com/v1",
            f"https://user:{REDACTED}@api.example.com/v1",
        ),
        (
            f"dsn postgres://carto:{PLANTED}@db/carto failed",
            f"dsn postgres://carto:{REDACTED}@db/carto failed",
        ),
        (f"postgres://carto:{PLANTED}@db:5432/carto", f"AAAAAAAA://AAAAA:{REDACTED}@AA:9999/AAAAA"),
    ],
)
def test_url_userinfo_passwords_are_masked(value: str, expected: str) -> None:
    assert mask_string(value) == expected
    assert PLANTED not in mask_string(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/v1",
        "postgres://carto@db.internal/carto",
        "sftp://sftp.example.com/",
    ],
)
def test_urls_without_userinfo_survive(value: str) -> None:
    assert mask_string(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"connector failed password={PLANTED}", f"connector failed password={REDACTED}"),
        (f"Password: {PLANTED}", f"Password: {REDACTED}"),
        (f"api_key={PLANTED}, retry", f"api_key={REDACTED}, retry"),
        (f"x-api-key: {PLANTED}", f"x-api-key: {REDACTED}"),
        (f"client secret = {PLANTED}", f"client secret = {REDACTED}"),
        ("Authorization: Basic dXNlcjpwYXNz", f"Authorization: {REDACTED}"),
        (f"Authorization: Bearer {JWT}", f"Authorization: {REDACTED}"),
        (f"GET /cb?token={PLANTED}&next=/home", f"GET /cb?token={REDACTED}&next=/home"),
        (f"password='{PLANTED} two words'; done", f"password={REDACTED}; done"),
        (f'passwd="{PLANTED}"', f"passwd={REDACTED}"),
        (f"session={PLANTED}", f"session={REDACTED}"),
        (f"credentials: {PLANTED}", f"credentials: {REDACTED}"),
        (f"auth_token={PLANTED}", f"auth_token={REDACTED}"),
        (f"token={CARTO_TOKEN}", f"token={TOKEN_MASK}"),  # the earlier, more specific mask stays
        (f"secret={HIGH_ENTROPY}", f"secret={HIGH_ENTROPY_MASK}"),
        # quoted keys: JSON, Python reprs and escaped JSON inside a message
        (f'{{"password": "{PLANTED}"}}', f'{{"password": {REDACTED}}}'),
        (f"{{'password': '{PLANTED}'}}", f"{{'password': {REDACTED}}}"),
        (f'{{"api_key":"{PLANTED}","n":1}}', f'{{"api_key":{REDACTED},"n":1}}'),
        (f'{{\\"token\\": \\"{PLANTED}\\"}}', f'{{\\"token\\": {REDACTED}}}'),
        (f'"secret" : "{PLANTED} with spaces"', f'"secret" : {REDACTED}'),
        # ODBC and JDBC connection strings, braces included
        (
            f"Driver={{ODBC Driver 18}};Server=db;UID=sa;PWD={PLANTED}!;",
            f"Driver={{ODBC Driver 18}};Server=db;UID=sa;PWD={REDACTED};",
        ),
        (f"Server=db;Pwd={{{PLANTED};x}};Db=a", f"Server=db;Pwd={REDACTED};Db=a"),
        (f"jdbc:x://db;user=sa;password={PLANTED}", f"jdbc:x://db;user=sa;password={REDACTED}"),
        # more secret words
        (f"passphrase={PLANTED}", f"passphrase={REDACTED}"),
        (f"pwd: {PLANTED}", f"pwd: {REDACTED}"),
        (f"pass={PLANTED} next", f"pass={REDACTED} next"),
        (f"auth={PLANTED}", f"auth={REDACTED}"),
        (f"Proxy-Auth: {PLANTED}", f"Proxy-Auth: {REDACTED}"),
        (f"GET /blob?sp=r&sig={PLANTED}&se=x", f"GET /blob?sp=r&sig={REDACTED}&se=x"),
        (f"signature: {PLANTED}", f"signature: {REDACTED}"),
        (f"dbPass={PLANTED}", f"dbPass={REDACTED}"),
        # a quoted value right after the word, no separator
        (
            f'login failed for jsmith with secret "{PLANTED}"',
            f"login failed for jsmith with secret {REDACTED}",
        ),
        (f"Password '{PLANTED} x' rejected", f"Password {REDACTED} rejected"),
    ],
)
def test_secret_assignments_in_free_text_are_masked(value: str, expected: str) -> None:
    assert mask_string(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "token_count=3",
        "secret_ref=vault://carto/edge/splunk",
        "3 tokens issued",
        "session timed out",
        "the password rule",
        "api_version=2",
        "bypass=on",
        "compass: north",
        "oauth=enabled",
        "author: Jane",
        "design: flat",
        "the tests pass, all 12",
        "signal=ok",
    ],
)
def test_free_text_without_a_secret_value_survives(value: str) -> None:
    assert mask_string(value) == value


@pytest.mark.parametrize(
    ("unit", "times", "tail"),
    [
        ("password", 20_000, ""),
        ('pass"', 20_000, ""),
        ("Pass ", 20_000, "="),
        ('password="', 1, "a"),
        ("pwd={", 1, "a"),
        ('token\\"', 20_000, ":"),
        ("password: ", 20_000, ""),
    ],
)
def test_secret_assignment_scan_stays_linear(unit: str, times: int, tail: str) -> None:
    """Spec 2.3 invariant 8: an attacker-written log line cannot stall the processor."""
    value = unit * times + (tail * 100_000 if tail == "a" else tail)
    start = perf_counter()
    mask_string(value)
    assert perf_counter() - start < 5.0  # about 0.4 s on the laptop; generous for --cov


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (EMAIL, EMAIL_MASK),
        (f"notify {EMAIL} now", f"notify {EMAIL_MASK} now"),
        ("<jane.roe@example.co.uk>", f"<{EMAIL_MASK}>"),
        ("to=jane_roe%2B1@sub.example.com;", f"to={EMAIL_MASK};"),
        ("'jane.roe@example.com'", f"'{EMAIL_MASK}'"),
    ],
)
def test_email_addresses_are_masked(value: str, expected: str) -> None:
    assert mask_string(value) == expected


@pytest.mark.parametrize(
    "value", ["postgres://carto@db.internal/carto", "user@host", "@handle", "jane@localhost"]
)
def test_strings_that_are_not_email_addresses_survive(value: str) -> None:
    assert mask_string(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (HEX_MARKER, "mk9A9A9A9A"),
        ("12 Mk0f1e2d3c Street", "12 Mk9A9A9A9A Street"),  # the simulator's address line
        ("550e8400-e29b-41d4-a716-446655440000", "999A9999-A99A-99A9-A999-999999999999"),
        ("sha256=3f7a2c1b9d8e4f6a", "sha256=9A9A9A9A9A9A9A9A"),
    ],
)
def test_lowercase_hex_runs_are_shaped(value: str, expected: str) -> None:
    assert mask_string(value) == expected


@pytest.mark.parametrize(
    "value", ["deadbeef", "cafebabe facade", "tpl_4f1c9a", "0xdeadbeef", "beef1", "ABCDEF12"]
)
def test_hex_runs_without_both_letters_and_digits_survive(value: str) -> None:
    assert mask_string(value) == value


def test_high_entropy_strings_are_masked() -> None:
    assert mask_string(HIGH_ENTROPY) == HIGH_ENTROPY_MASK
    assert mask_string(f"secret={HIGH_ENTROPY}") == f"secret={HIGH_ENTROPY_MASK}"
    assert mask_string(f"({HIGH_ENTROPY},") == f"({HIGH_ENTROPY_MASK},"
    padded = _b64url(bytes(range(40))) + "=="
    assert mask_string(padded) == HIGH_ENTROPY_MASK


@pytest.mark.parametrize(
    "value",
    [
        "aBcDeFgHiJkLmNoPqRsTuVwXyZ_-+/1",  # 31 characters: too short
        "a" * 40,  # one character class, zero entropy
        "abcdefghijklmnopqrstuvwxyzabcdefghij",  # one class
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",  # two classes
        "Abc-Abc-Abc-Abc-Abc-Abc-Abc-Abc-Abc-",  # three classes, low entropy
        "this-is-a-long-kebab-case-identifier-name",  # lower plus other only
    ],
)
def test_low_entropy_or_short_strings_survive(value: str) -> None:
    assert mask_string(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SO-0004471", "AA-9999999"),
        ("order 4471 failed", "order 9999 failed"),
        (IDENTIFIER, "A9-9999"),
        ("c-88213", "A-99999"),
        ("SH-5521 for 88-210", "AA-9999 for 99-999"),
        ("po_number=88210;", "AA_AAAAAA=99999;"),  # the whole token is shaped
        ("88-210", "99-999"),  # scenario A po_num: four digits joined by "-", no run of four
        ("ref_12_34", "AAA_99_99"),
        ("run_2026-10-06", "AAA_9999-99-99"),
        ("1500ms", "9999AA"),
        ("été-2026x", "AAA-9999A"),
        ("8821-21-00", "9999-99-99"),  # a date-like layout that is not a date
        ("2026-13-45", "9999-99-99"),
        ("2026-10-06T25:61:61Z", "9999-99-99A99:99:99A"),
        ("MAN-20260923-01", "AAA-99999999-99"),
        ("db:5432/carto", "AA:9999/AAAAA"),
    ],
)
def test_identifier_shaped_tokens_log_as_their_shape(value: str, expected: str) -> None:
    assert mask_string(value) == expected


def test_ulids_are_left_in_clear_so_an_event_can_be_traced() -> None:
    ulid = ulid_from_parts(1_759_747_200_000, bytes(10))  # ends in sixteen zeros
    assert re.search(r"[0-9]{4}", ulid)
    assert mask_string(ulid) == ulid
    assert mask_string(f"event {ulid} deduped") == f"event {ulid} deduped"
    assert mask_string(f"event_id={ulid},") == f"event_id={ulid},"
    assert mask_string(f"({ulid}:SO-0004471)") == f"({ulid}:AA-9999999)"
    assert _redact(event_id=ulid)["event_id"] == ulid
    for _ in range(200):
        fresh = new_ulid()
        assert mask_string(fresh) == fresh
    not_a_ulid = "8" + ulid[1:]  # the first character of a ULID is 0 to 7
    assert mask_string(not_a_ulid) == identifier_shape(not_a_ulid)
    glued = f"x{ulid}"  # not a ULID on its own, so the digit run is shaped
    assert mask_string(glued) == identifier_shape(glued)


def test_mask_markers_inside_a_token_are_not_reshaped() -> None:
    assert mask_string(f"user:1234:{CARTO_TOKEN}") == f"AAAA:9999:{TOKEN_MASK}"
    assert mask_string(f"{AWS_KEY}/4471") == f"{AWS_KEY_MASK}/9999"
    assert mask_string(f"order-4471:{EMAIL}") == f"AAAAA-9999:{EMAIL_MASK}"
    assert mask_string(f"{JWT}#0001") == f"{JWT_MASK}#9999"
    assert mask_string(f"db:5432/{REDACTED}") == f"AA:9999/{REDACTED}"
    assert mask_string(f"{HIGH_ENTROPY}:4471") == f"{HIGH_ENTROPY_MASK}:9999"


@pytest.mark.parametrize(
    "value",
    [
        "order 471 failed",
        "c-881",
        "DC-03",
        "1,234",
        "10.0.0.1",
        "took 0.123s",
        "python 3.12.15",
        "99.95% ok",
        "cron at 02:30",
        "version 1.2.3.4",
        "A1B2C3D4",
        "2026-10-06",
        "2026-10-06T12:34:56Z",
        "2026-10-06T12:34:56.789+00:00",
        "2026-10-06T12:34:56.123456Z",
        "2026-10-06T12:34:56-0500",
        "2026-10-06 12:34:56",
        "at 2026-10-06T12:34:56Z,",
        "(2026-10-06)",
        "12:34:56.789000",
        "retry in 200 ms, 3 of 5",
    ],
)
def test_dates_timestamps_and_short_numbers_survive(value: str) -> None:
    assert mask_string(value) == value


def test_identifier_shape_helper() -> None:
    assert identifier_shape("SO-0004471") == "AA-9999999"
    assert identifier_shape("a1.b2/c3_d4") == "A9.A9/A9_A9"
    assert identifier_shape("") == ""


def test_event_message_is_masked() -> None:
    out = redact(None, "info", {"event": f"order SO-0004471 paid with {CARTO_TOKEN}"})
    assert out["event"] == f"order AA-9999999 paid with {TOKEN_MASK}"


# ---------------------------------------------------------------------------------------------
# Rule 4: other types, nesting, never raise
# ---------------------------------------------------------------------------------------------


def test_small_numbers_booleans_and_none_are_untouched() -> None:
    out = _redact(retries=3, ratio=0.5, took=0.0123, neg=-999, ok=True, nothing=None, amount=12.5)
    assert out["retries"] == 3
    assert out["ratio"] == 0.5
    assert out["took"] == 0.0123  # leading zeros are not digits of a value
    assert out["neg"] == -999
    assert out["ok"] is True
    assert out["nothing"] is None
    assert out["amount"] == 12.5


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("customer_id", 48812213, "99999999"),
        ("card", 4111111111111111, "9999999999999999"),
        ("total", 207.81, "999.99"),  # an order total is a value, not a metric
        ("order", 4471, "9999"),
        ("phone", 15551234567, "99999999999"),
        ("delta", -48812213, "-99999999"),
        ("weight_kg", 1234.5, "9999.9"),
        ("big", 2**70, "9" * len(str(2**70))),
        ("amount", 4471.0, "9999.9"),
        ("tiny", 1.2345e-20, "9.9999A-99"),
    ],
)
def test_numbers_of_four_or_more_digits_are_masked_by_default(
    key: str, value: float, expected: str
) -> None:
    out = _redact(**{key: value})
    assert out[key] == expected
    assert str(value) not in json.dumps(out)


@pytest.mark.parametrize(
    "key",
    [
        "count",
        "total_count",
        "events",
        "records",
        "rows",
        "duration_ms",
        "retry_in_seconds",
        "events_per_second",
        "overlap_days",
        "bytes",
        "queue_depth",
        "batch_size",
        "port",
        "status",
        "pid",
        "attempts",
        "policy_version",
        "quarantine_samples",
        "batches",
        "max_len",
    ],
)
def test_numbers_under_a_metric_key_survive(key: str) -> None:
    assert is_metric_key(key)
    out = _redact(**{key: 48812213}, nested={key: 4471.25})
    assert out[key] == 48812213
    assert out["nested"] == {key: 4471.25}


@pytest.mark.parametrize(
    "key",
    ["customer_id", "order", "total", "amount", "card", "id", "count_of_x", "batch", "level", ""],
)
def test_keys_that_do_not_end_in_a_metric_qualifier_are_not_metric_keys(key: str) -> None:
    assert not is_metric_key(key)


def test_metric_qualifier_does_not_cover_list_items_or_secret_keys() -> None:
    out = _redact(events=[48812213, 3], count={"customer_id": 48812213}, token_count=48812213)
    assert out["events"] == ["99999999", 3]  # a list of numbers is a list of values
    assert out["count"] == {"customer_id": "99999999"}
    assert out["token_count"] == 48812213  # a count of tokens, not a token (rule 1 qualifier)
    assert {"count", "ms", "seconds", "bytes", "port", "version"} <= METRIC_KEY_QUALIFIERS


def test_nested_structures_are_walked() -> None:
    out = _redact(
        batch={
            "orders": [
                {"order_id": "SO-0004471", "api_key": PLANTED, "amount": 12.5},
                ("SH-5521", 7, {"token": PLANTED}),
            ],
            "tags": {"a", "b"},
            "seen_at": "2026-10-06T12:34:56Z",
        }
    )
    batch = out["batch"]
    assert batch["orders"][0] == {"order_id": "AA-9999999", "api_key": REDACTED, "amount": 12.5}
    assert batch["orders"][1] == ["AA-9999", 7, {"token": REDACTED}]
    assert sorted(batch["tags"]) == ["a", "b"]
    assert batch["seen_at"] == "2026-10-06T12:34:56Z"
    assert PLANTED not in json.dumps(out)


def test_walk_stops_at_max_depth_without_leaking() -> None:
    inside: Any = CARTO_TOKEN
    for _ in range(MAX_DEPTH):
        inside = [inside]
    assert TOKEN_MASK in json.dumps(_redact(deep=inside))
    too_deep: Any = [inside]
    rendered = json.dumps(_redact(deep=too_deep))
    assert REDACTION_ERROR in rendered
    assert CARTO_TOKEN not in rendered


def test_self_referencing_containers_do_not_recurse_forever() -> None:
    loop: list[Any] = []
    loop.append(loop)
    assert REDACTION_ERROR in json.dumps(_redact(loop=loop))


def test_processor_never_raises() -> None:
    class Boom:
        def __str__(self) -> str:
            msg = "no string for you"
            raise RuntimeError(msg)

    class BadMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            msg = "iteration failed"
            raise RuntimeError(msg)

        def __len__(self) -> int:
            return 1

    out = _redact(boom=Boom(), mapping=BadMapping(), fine="ok")
    assert out["boom"] == REDACTION_ERROR
    assert out["mapping"] == REDACTION_ERROR
    assert out["fine"] == "ok"
    assert out["event"] == "probe"


class _Kind(enum.Enum):
    FILE = "file arrival SO-0004471"


class _Level(enum.IntEnum):
    HIGH = 4471


class _Source(enum.StrEnum):
    ORDERS = "src_orders_log"


class _Connector(BaseModel):
    name: str
    password: str
    host: str


@dataclasses.dataclass
class _Cursor:
    source: str
    position: str


def test_other_object_types_are_converted_then_masked() -> None:
    out = _redact(
        blob=b"blob SO-0004471",
        when=datetime(2026, 10, 6, 12, 34, 56, 789000, tzinfo=UTC),
        day=date(2026, 10, 6),
        clock=time(12, 34, 56),
        took=timedelta(seconds=1.5),
        path=Path("exports") / "SO-0004471.csv",
        kind=_Kind.FILE,
        level=_Level.HIGH,
        source=_Source.ORDERS,
        connector=_Connector(name="orders", password=PLANTED, host="db-0001.internal"),
        cursor=_Cursor(source="src_orders_log", position="orders.log:line:4471"),
        error=ValueError("order SO-0004471 missing"),
    )
    assert out["blob"] == "blob AA-9999999"
    assert out["when"] == "2026-10-06T12:34:56.789000+00:00"
    assert out["day"] == "2026-10-06"
    assert out["clock"] == "12:34:56"
    assert out["took"] == 1.5
    assert out["path"].replace("\\", "/") == "AAAAAAA/AA-9999999.AAA"
    assert out["kind"] == "file arrival AA-9999999"
    assert out["level"] == "9999"  # an IntEnum is its value, and "level" is not a metric
    assert out["source"] == "src_orders_log"
    assert out["connector"] == {"name": "orders", "password": REDACTED, "host": "AA-9999.AAAAAAAA"}
    assert out["cursor"] == {"source": "src_orders_log", "position": "AAAAAA.AAA:AAAA:9999"}
    assert out["error"] == "order AA-9999999 missing"
    assert PLANTED not in json.dumps(out)


def test_dictionary_keys_are_masked_and_stringified() -> None:
    out = _redact(by_order={"SO-0004471": 1, 4471: 2, CARTO_TOKEN: 3})
    assert out["by_order"] == {"AA-9999999": 1, "9999": 2, TOKEN_MASK: 3}


def test_keys_that_mask_to_the_same_shape_stay_distinct() -> None:
    out = _redact(by_order={"SO-0004471": 1, "SO-0004472": 2, "SO-0004473": 3})
    assert out["by_order"] == {"AA-9999999": 1, "AA-9999999#2": 2, "AA-9999999#3": 3}
    top = redact(None, "info", {"event": "e", "id_4471": 1, "id_4472": 2})
    assert top == {"event": "e", "AA_9999": 1, "AA_9999#2": 2}


def test_output_is_json_native() -> None:
    out = _redact(mixed=[{1, 2}, (3, 4), b"x", Path("p"), _Kind.FILE, {"k": None}])
    assert json.loads(json.dumps(out)) == out


# ---------------------------------------------------------------------------------------------
# Property: planted markers never survive anywhere in an arbitrary event
# ---------------------------------------------------------------------------------------------

_glue = st.sampled_from(["", " ", "=", ": ", "'", '"', "(", ",", "\n", "\t"])
_marker_text = st.tuples(_glue, st.sampled_from(MARKERS), _glue).map("".join)
_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=16),
    _marker_text,
)
# Keys hold no digits so that a masked key still names the metric its original named; numbers
# planted under metric keys and under value keys check both sides of the number rule.
_keys = st.one_of(
    st.text(max_size=12).filter(lambda key: not any(char.isdecimal() for char in key)),
    st.sampled_from(MARKERS),
    st.sampled_from(("count", "duration_ms", "customer_id", "total", "events")),
)


def _nested(depth: int) -> st.SearchStrategy[Any]:
    if depth == 0:
        return _leaf
    inner = _nested(depth - 1)
    return st.one_of(_leaf, st.lists(inner, max_size=4), st.dictionaries(_keys, inner, max_size=4))


def _digits(number: float) -> int:
    return sum(char.isdecimal() for char in str(number).lstrip("-0."))


def _assert_no_marker(value: Any, key: str = "") -> None:
    if isinstance(value, str):
        for marker in MARKERS:
            assert marker not in value
    elif isinstance(value, dict):
        for name, item in value.items():
            _assert_no_marker(name)
            _assert_no_marker(item, name)
    elif isinstance(value, list):
        for item in value:
            _assert_no_marker(item)
    else:
        assert value is None or isinstance(value, bool | int | float)
        if isinstance(value, int | float) and not isinstance(value, bool):
            # numbers of four or more digits are values unless the key names a metric
            assert _digits(value) < 4 or is_metric_key(key.split("#", maxsplit=1)[0])


@given(event=st.dictionaries(_keys, _nested(5), max_size=6), message=_marker_text)
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_planted_markers_never_reach_the_rendered_json(event: dict[str, Any], message: str) -> None:
    out = redact(None, "info", {**event, "event": message})
    rendered = json.dumps(out)
    for marker in MARKERS:
        assert marker not in rendered
        assert json.dumps(marker)[1:-1] not in rendered
    assert REDACTION_ERROR not in rendered
    _assert_no_marker(dict(out))


# ---------------------------------------------------------------------------------------------
# configure_logging and get_logger, end to end
# ---------------------------------------------------------------------------------------------


def test_configure_logging_json_end_to_end(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("edge", level="INFO", stream=stream)
    log = get_logger(component="gateway")
    log.info(
        "login for SO-0004471",
        order_id="SO-0004471",
        count=4471,
        customer_id=48812213,
        card=4111111111111111,
        total=207.81,
        **{CREDENTIAL_KEY: PLANTED},
    )
    line = stream.getvalue()
    assert line.endswith("\n")
    data = json.loads(line)
    assert data["service"] == "edge"
    assert data["level"] == "info"
    assert data["component"] == "gateway"
    assert data["event"] == "login for AA-9999999"
    assert data["order_id"] == "AA-9999999"
    assert data["count"] == 4471
    assert data["customer_id"] == "99999999"
    assert data["card"] == "9999999999999999"
    assert data["total"] == "999.99"
    for planted in ("48812213", "4111111111111111", "207.81"):
        assert planted not in line
    assert data[CREDENTIAL_KEY] == REDACTED
    assert PLANTED not in line
    assert "SO-0004471" not in line
    stamp = datetime.fromisoformat(data["timestamp"])
    assert stamp.utcoffset() == timedelta(0)
    assert abs(datetime.now(tz=UTC) - stamp) < timedelta(minutes=1)


def test_configure_logging_filters_below_level(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("core", level="warning", stream=stream)
    log = get_logger()
    log.debug("hidden")
    log.info("hidden too")
    log.warning("shown")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "shown"
    assert json.loads(lines[0])["level"] == "warning"


def test_configure_logging_console_output_is_redacted_too(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("edge", json_output=False, stream=stream)
    get_logger().info("console SO-0004471", **{CREDENTIAL_KEY: PLANTED})
    text = stream.getvalue()
    assert REDACTED in text
    assert "edge" in text
    assert "AA-9999999" in text
    assert PLANTED not in text
    assert "SO-0004471" not in text


def test_configure_logging_rejects_unknown_levels(clean_structlog: None) -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("edge", level="loud")


def test_http_client_request_lines_never_reach_the_log(clean_structlog: None) -> None:
    """httpx logs ``HTTP Request: GET <url>`` at INFO, query string included (spec 14.12)."""
    stream = io.StringIO()
    configure_logging("edge", level="debug", stream=stream)
    logging.getLogger("httpx").info('HTTP Request: GET https://h/x?member=quokka-77 "200 OK"')
    logging.getLogger("httpcore.http11").debug("send_request_headers.started")
    logging.getLogger("httpx").warning("retrying after a transport error")
    text = stream.getvalue()
    assert "quokka-77" not in text
    assert "send_request_headers" not in text
    assert "retrying after a transport error" in text
    for name in QUIET_LIBRARY_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_contextvars_are_merged_and_masked(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    structlog.contextvars.bind_contextvars(request_id="SO-0004471", service="spoofed")
    get_logger().info("with context")
    data = json.loads(stream.getvalue())
    assert data["request_id"] == "AA-9999999"
    assert data["service"] == "edge"


def test_exception_text_goes_through_the_masks(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    try:
        msg = "order SO-0004471 missing"
        raise ValueError(msg)
    except ValueError:
        get_logger().error("failed", exc_info=True)
    data = json.loads(stream.getvalue())
    assert "ValueError" in data["exception"]
    assert "AA-9999999" in data["exception"]
    assert "SO-0004471" not in stream.getvalue()


def test_capture_logs_sees_redacted_events(clean_structlog: None) -> None:
    configure_logging("edge")
    with structlog.testing.capture_logs(processors=[redact]) as logs:
        get_logger().info("login", **{CREDENTIAL_KEY: PLANTED}, order_id="SO-0004471")
    assert logs == [
        {
            "event": "login",
            CREDENTIAL_KEY: REDACTED,
            "order_id": "AA-9999999",
            "log_level": "info",
        }
    ]


def test_get_logger_configures_defaults_when_nobody_did(
    clean_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not structlog.is_configured()
    get_logger().info("forgot to configure", **{CREDENTIAL_KEY: PLANTED})
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["service"] == DEFAULT_SERVICE
    assert data[CREDENTIAL_KEY] == REDACTED
    assert PLANTED not in out


# ---------------------------------------------------------------------------------------------
# Standard library records (third-party libraries) go through the same chain
# ---------------------------------------------------------------------------------------------


def test_stdlib_records_are_rendered_through_redact(
    clean_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    logging.getLogger("uvicorn.error").warning(
        "connector failed token=%s password=%s order=%s for %s",
        CARTO_TOKEN,
        PLANTED,
        "SO-0004471",
        EMAIL,
    )
    line = stream.getvalue()
    data = json.loads(line)
    assert data["event"] == (  # "order=SO-0004471" is one whitespace token, shaped whole
        f"connector failed token={TOKEN_MASK} password={REDACTED} AAAAA=AA-9999999 for {EMAIL_MASK}"
    )
    assert data["logger"] == "uvicorn.error"
    assert data["service"] == "edge"
    assert data["level"] == "warning"
    assert "timestamp" in data
    assert CARTO_TOKEN not in line
    assert PLANTED not in line
    assert "SO-0004471" not in line
    assert HEX_MARKER not in line
    assert capsys.readouterr().err == ""  # nothing fell through to logging.lastResort


def test_stdlib_exceptions_and_levels_follow_the_configuration(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("core", level="warning", stream=stream)
    library = logging.getLogger("asyncpg.pool")
    library.info("hidden SO-0004471")
    try:
        msg = f"connect failed with {CARTO_TOKEN}"
        raise ConnectionError(msg)
    except ConnectionError:
        library.exception("pool SO-0004471 broken")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["event"] == "pool AA-9999999 broken"
    assert data["logger"] == "asyncpg.pool"
    assert data["level"] == "error"
    assert "ConnectionError" in data["exception"]
    assert TOKEN_MASK in data["exception"]
    assert CARTO_TOKEN not in lines[0]
    assert "SO-0004471" not in lines[0]


def test_stdlib_records_with_quoted_secret_keys_are_redacted(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    logging.getLogger("lib").warning("conn %s", {"password": PLANTED, "pwd": PLANTED})
    logging.getLogger("lib").warning("dsn %s", f"Server=db;UID=sa;PWD={PLANTED};")
    text = stream.getvalue()
    assert text.count(REDACTED) == 3
    assert PLANTED not in text


def test_stdlib_console_output_is_redacted_too(clean_structlog: None) -> None:
    stream = io.StringIO()
    configure_logging("edge", json_output=False, stream=stream)
    logging.getLogger("httpx").warning("password=%s order=%s", PLANTED, "SO-0004471")
    text = stream.getvalue()
    assert REDACTED in text
    assert "AA-9999999" in text
    assert "httpx" in text
    assert PLANTED not in text
    assert "SO-0004471" not in text


def test_configure_logging_replaces_foreign_root_handlers(
    clean_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    root = logging.getLogger()
    leaky = logging.StreamHandler(sys.stderr)
    root.addHandler(leaky)
    configure_logging("edge", stream=io.StringIO())
    assert leaky not in root.handlers
    assert len(root.handlers) == 1
    logging.getLogger("stray").warning("password=%s", PLANTED)
    assert PLANTED not in capsys.readouterr().err


def test_get_logger_default_configuration_covers_stdlib_too(
    clean_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    get_logger()
    logging.getLogger("httpx").warning("token=%s", CARTO_TOKEN)
    out = capsys.readouterr()
    assert out.err == ""
    data = json.loads(out.out)
    assert data["service"] == DEFAULT_SERVICE
    assert data["event"] == f"token={TOKEN_MASK}"


# ---------------------------------------------------------------------------------------------
# Warnings and uncaught exceptions go through the processor too
# ---------------------------------------------------------------------------------------------

LEAKY_MESSAGE = f"could not parse SO-0004471 with password={PLANTED}"


@pytest.fixture
def interpreter_default_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from the interpreter's own hooks, as a service process does (not pytest's)."""
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    monkeypatch.setattr(sys, "unraisablehook", sys.__unraisablehook__)


def _assert_redacted_only(
    stream: io.StringIO, captured: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    out = captured.readouterr()
    assert out.err == ""  # nothing bypasses the processor on its way to stderr
    assert out.out == ""
    text = stream.getvalue()
    assert "SO-0004471" not in text
    assert PLANTED not in text
    data: dict[str, Any] = json.loads(text.splitlines()[-1])
    return data


def test_python_warnings_are_logged_through_redact(
    clean_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(LEAKY_MESSAGE, UserWarning, stacklevel=1)
    data = _assert_redacted_only(stream, capsys)
    assert data["logger"] == "py.warnings"
    assert data["level"] == "warning"
    assert "UserWarning" in data["event"]
    assert f"AA-9999999 with password={REDACTED}" in data["event"]


def test_uncaught_exceptions_are_logged_through_redact(
    clean_structlog: None, interpreter_default_hooks: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    assert sys.excepthook is not sys.__excepthook__
    try:
        raise RuntimeError(LEAKY_MESSAGE)
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    data = _assert_redacted_only(stream, capsys)
    assert data["level"] == "critical"
    assert data["event"] == "uncaught exception"
    assert "RuntimeError" in data["exception"]
    assert "AA-9999999" in data["exception"]


def test_uncaught_thread_exceptions_are_logged_through_redact(
    clean_structlog: None, interpreter_default_hooks: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)

    def work() -> None:
        raise RuntimeError(LEAKY_MESSAGE)

    worker = threading.Thread(target=work, name="SO-0004471-worker")
    worker.start()
    worker.join()
    data = _assert_redacted_only(stream, capsys)
    assert data["event"] == "uncaught exception in thread"
    assert data["thread"] == "AA-9999999-AAAAAA"
    assert "AA-9999999" in data["exception"]


def test_unraisable_exceptions_are_logged_through_redact(
    clean_structlog: None, interpreter_default_hooks: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)

    class Leaky:
        def __repr__(self) -> str:
            return LEAKY_MESSAGE

        def __del__(self) -> None:
            raise RuntimeError(LEAKY_MESSAGE)

    leaky = Leaky()
    del leaky  # CPython runs __del__ here and hands its exception to sys.unraisablehook
    data = _assert_redacted_only(stream, capsys)
    assert data["event"] == "unraisable exception"
    assert "object_type" in data
    assert "AA-9999999" in data["exception"]


def test_exception_hooks_chain_to_a_hook_someone_else_installed(
    clean_structlog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test runner's hook (pytest's) still sees the exception; the interpreter's never does."""
    seen: list[type[BaseException]] = []

    def runner_hook(kind: type[BaseException], value: BaseException, tb: object) -> None:
        del value, tb
        seen.append(kind)

    monkeypatch.setattr(sys, "excepthook", runner_hook)
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    configure_logging("edge", stream=stream)  # idempotent: one log line, one chained call
    try:
        raise ValueError(LEAKY_MESSAGE)
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    assert seen == [ValueError]
    assert len(stream.getvalue().splitlines()) == 1
