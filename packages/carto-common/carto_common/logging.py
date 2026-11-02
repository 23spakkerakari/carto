"""structlog configuration and the redaction processor (spec 14.12, 2.3 invariant 7).

Spec 14.12: "A structlog processor drops keys matching secret names, masks high-entropy strings
and anything shaped like a token or identifier, and never logs request or response bodies."
Spec 2.3 invariant 7: "The product's own logs never contain secrets or raw identifier values. A
logging processor enforces this and a test verifies it." Spec 14.3: secret values are never
logged. :func:`redact` is that processor; :func:`configure_logging` installs it directly in front
of the renderer on both output paths, structlog's own chain and the standard library's root
logger (third-party libraries such as uvicorn, httpx and database drivers log through the
latter), so nothing reaches the output stream without passing through it.

Rules, in the order :func:`redact` applies them to every event (key names first, then values):

1. **Secret-named keys** keep the key and replace the value with ``[REDACTED]``, whatever its
   type. The name is lower-cased and split into segments on ``_``, ``-``, ``.``, whitespace,
   camelCase and letter-digit boundaries. The key matches when any segment is one of
   ``password``, ``passwd``, ``passphrase``, ``pass``, ``pwd``, ``secret``, ``token``,
   ``apikey``, ``authorization``, ``auth``, ``cookie``, ``session``, ``bearer``, ``credential``,
   ``signature`` or ``sig`` (singular or plural), or when two adjacent segments
   are ``api key``, ``private key``, ``client secret`` or ``access key``, unless the last
   segment is a qualifier naming a count, a reference or a property rather than the value
   itself (:data:`SECRET_KEY_QUALIFIERS`: ``count``, ``ref``, ``name``, ``type``, ``ttl`` and
   the like). So ``auth_token``, ``x-api-key``, ``clientSecret``, ``secret_value``,
   ``password_hash``, ``session_id``, ``private_key_pem`` and ``cookies`` are redacted, while
   ``token_count`` (a count) and ``secret_ref`` (a reference, never a value) survive and go
   through the value rules like any other string. Whatever follows a secret word is part of the
   secret unless it is a known qualifier: any other reading logs ``secret_value`` in clear.
2. **Body keys** (``body``, ``request_body``, ``response_body``, ``payload``, ``raw``,
   ``raw_value``, ``raw_values``, ``values`` and ``record``, after the same normalisation) keep
   the key and replace the value with ``[DROPPED]``, whatever its type. Bodies are never logged.
3. **Strings** anywhere, the ``event`` message and dictionary keys included, are masked in this
   order: PEM blocks (``-----BEGIN`` through the closing ``-----END ...-----``) -> ``[PEM]``;
   JWTs (three base64url segments joined by dots, the first starting with ``eyJ``) -> ``[JWT]``;
   carto tokens (spec 8.4, ``t<key_version>.<22 base64url chars>``) -> ``[TOKEN]``; AWS access
   key ids (``AKIA`` or ``ASIA`` plus 16 characters) -> ``[AWS_KEY]``; the password in a URL's
   userinfo (``scheme://user:password@host``, the DSNs and SFTP URLs of spec 8.1) ->
   ``user:[REDACTED]@host``; e-mail addresses -> ``[EMAIL]``; high-entropy runs (32 or more
   characters of ``[A-Za-z0-9+/=_-]`` with at least three of the classes upper, lower, digit
   and other, and Shannon entropy of at least 3.5 bits per character) -> ``[HIGH_ENTROPY]``; a
   secret word followed by ``=`` or ``:`` and a value in free text (``password=hunter2``,
   ``Authorization: Basic ...``, ``?token=...&``, ``{"password": "..."}``, ``;PWD=...;``) -> the
   value becomes ``[REDACTED]`` unless an
   earlier rule already masked it (``token=[JWT]`` and ``secret=[HIGH_ENTROPY]`` stay); lower-case
   hex runs of eight or more characters holding both a letter and a digit (UUIDs, content hashes,
   the simulator's ``mk<8 hex>`` leak markers of spec 18.3) -> their shape; and
   identifier-shaped whitespace-delimited tokens -> their shape, digits to ``9`` and letters to
   ``A`` with punctuation kept (the spec 8.3 shape without run collapsing). A token is
   identifier-shaped when it holds a run of four or more digits, or four or more digits in
   total while also containing ``-`` or ``_`` (the spec 1.1 identifiers ``88-210`` and
   ``X9-0442``), and is not an ISO 8601 date, time or timestamp with real month, day, hour,
   minute and second ranges. So ``SO-0004471`` logs as ``AA-9999999``, ``order 4471 failed`` as
   ``order 9999 failed`` and ``88-210`` as ``99-999``, while ``order 471 failed``, ``c-881``,
   ``DC-03``, ``took 0.123s``, ``python 3.12.15``, ``10.0.0.1``, ``1,234`` and
   ``2026-10-06T12:34:56Z`` are left alone. Mask markers and ULIDs (the product's own
   ``event_id``, spec 5.4, which operators grep for) inside a token are kept as they are, so
   ``user:1234:t1.<token>`` logs as ``AAAA:9999:[TOKEN]`` and
   ``event_id=01ARZ3NDEKTSV4RRFFQ69G5FAV`` stays readable.
4. **Numbers** (``int`` and ``float``, not ``bool``) are values until their key says otherwise
   (ADR 0040). A number whose decimal text holds four or more digits, leading zeros not counted,
   logs as its shape (``customer_id=48812213`` -> ``"99999999"``, ``total=207.81`` ->
   ``"999.99"``) unless the last segment of its own key is a metric qualifier
   (:data:`METRIC_KEY_QUALIFIERS`: ``count``, ``ms``, ``seconds``, ``bytes``, ``port``,
   ``events``, ``version`` and the like). The qualifier covers the number directly under the
   key only, never the items of a list. Smaller numbers, ``None`` and ``bool`` pass through.
5. Other types: dates and times are rendered ISO 8601 (which the identifier rule exempts),
   ``bytes`` are decoded and masked as text, enums contribute their value, pydantic models and
   dataclasses are walked like mappings, sets and tuples become lists, and any other object is
   ``str()``-ed and masked, so the JSON renderer never falls back to ``repr`` on something the
   processor has not inspected.
   Two keys that mask to the same shape stay two keys: the second becomes ``<shape>#2``.

The walk is bounded to :data:`MAX_DEPTH` nested containers. Anything deeper, and any top-level
key whose value raises during the walk, is replaced by ``[REDACTION_ERROR]``: the processor
never raises, because a log call that throws would take the service down with it. Every mask is
a fixed string, so the spec 18.3 leak test can grep rendered logs for planted markers.

What the processor cannot do: it recognises shapes, not meaning. Person names, free text and any
PII that is neither an e-mail address nor identifier-shaped is logged as given, so callers must
not log it (spec 18.3 plants markers in exactly those fields and fails the build when one shows
up in a service log). A secret that is shorter than 32 characters or made of fewer than three
character classes is hidden only when its key, URL position or ``key=value`` context names it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import enum
import itertools
import logging
import math
import re
import sys
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import TracebackType
from typing import Any, Final, TextIO, cast

import structlog
from pydantic import BaseModel
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from carto_common.ids import ULID_CHARS

__all__ = [
    "AWS_KEY_MASK",
    "DEFAULT_SERVICE",
    "DROPPED",
    "DROP_KEYS",
    "EMAIL_MASK",
    "HIGH_ENTROPY_MASK",
    "IDENTIFIER_MIN_DIGITS",
    "JWT_MASK",
    "MASKS",
    "MAX_DEPTH",
    "METRIC_KEY_QUALIFIERS",
    "PEM_MASK",
    "QUIET_LIBRARY_LOGGERS",
    "REDACTED",
    "REDACTION_ERROR",
    "SECRET_KEY_PAIRS",
    "SECRET_KEY_QUALIFIERS",
    "SECRET_KEY_WORDS",
    "TOKEN_MASK",
    "WARNINGS_LOGGER",
    "configure_logging",
    "get_logger",
    "identifier_shape",
    "is_drop_key",
    "is_metric_key",
    "is_secret_key",
    "mask_string",
    "redact",
]

QUIET_LIBRARY_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "hpack", "urllib3")
"""Third-party loggers held at WARNING: at INFO and DEBUG they write the full request URL with
its query string (``HTTP Request: GET https://host/path?param=value``) or wire-level detail, which
can carry identifier values the redaction masks are not guaranteed to recognise (spec 14.12, 2.3
invariant 7). Their warnings and errors still flow through :func:`redact`."""

REDACTED: Final = "[REDACTED]"
DROPPED: Final = "[DROPPED]"
REDACTION_ERROR: Final = "[REDACTION_ERROR]"
PEM_MASK: Final = "[PEM]"
JWT_MASK: Final = "[JWT]"
TOKEN_MASK: Final = "[TOKEN]"  # noqa: S105 - a fixed mask marker, not a credential
AWS_KEY_MASK: Final = "[AWS_KEY]"
EMAIL_MASK: Final = "[EMAIL]"
HIGH_ENTROPY_MASK: Final = "[HIGH_ENTROPY]"
MASKS: Final = (
    REDACTED,
    DROPPED,
    REDACTION_ERROR,
    PEM_MASK,
    JWT_MASK,
    TOKEN_MASK,
    AWS_KEY_MASK,
    EMAIL_MASK,
    HIGH_ENTROPY_MASK,
)
"""Every marker the processor emits; the identifier rule never rewrites one of these."""

MAX_DEPTH: Final = 16
"""Containers nested deeper than this are not inspected and become ``[REDACTION_ERROR]``."""

DEFAULT_SERVICE: Final = "unconfigured"
"""Service name :func:`get_logger` binds when :func:`configure_logging` was never called."""

_SECRET_WORDS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "bearer",
    "credential",
    "pwd",
    "passphrase",
    "pass",
    "auth",
    "signature",
    "sig",
)
SECRET_KEY_WORDS: Final = frozenset(_SECRET_WORDS) | frozenset(f"{word}s" for word in _SECRET_WORDS)
"""Key segments that mark a key as holding a secret (spec 8.3 name hints plus session ids)."""

_SECRET_PAIRS: Final = (("api", "key"), ("private", "key"), ("client", "secret"), ("access", "key"))
SECRET_KEY_PAIRS: Final = frozenset(_SECRET_PAIRS) | frozenset(
    (first, f"{second}s") for first, second in _SECRET_PAIRS
)
"""Adjacent segment pairs that mark a key as holding a secret."""

SECRET_KEY_QUALIFIERS: Final = frozenset(
    {
        "count",
        "total",
        "len",
        "length",
        "size",
        "num",
        "number",
        "ref",
        "version",
        "name",
        "type",
        "kind",
        "present",
        "expires",
        "expiry",
        "ttl",
        "scheme",
    }
)
"""Last segments that make a secret-named key describe the secret rather than hold it."""

DROP_KEYS: Final = frozenset(
    {
        "body",
        "request_body",
        "response_body",
        "payload",
        "raw",
        "raw_value",
        "raw_values",
        "values",
        "record",
    }
)
"""Normalised key names whose values are never logged (spec 14.12: no bodies, no raw values)."""

METRIC_KEY_QUALIFIERS: Final = frozenset(
    {
        # counts and tallies of the product's own objects
        "count",
        "counts",
        "attempt",
        "attempts",
        "retries",
        "rows",
        "records",
        "events",
        "batches",
        "samples",
        # units
        "bytes",
        "ms",
        "seconds",
        "second",
        "days",
        # sizes and limits
        "depth",
        "size",
        "len",
        "length",
        "limit",
        # process and network
        "port",
        "status",
        "pid",
        "version",
        "versions",
    }
)
"""Last key segments that make a number a metric the operator needs in clear (rule 4, ADR 0040).

A number under any other key is a value and is masked once it has four or more digits: order
totals, customer numbers and card numbers are numbers too. ``total`` and ``num`` are not here on
purpose (``total=207.81`` is an amount, ``order_num=4471`` an identifier); a count is logged as
``*_count``."""

NUMBER_MIN_DIGITS: Final = 4
"""Digits (leading zeros not counted) from which a number under a non-metric key is masked."""

HIGH_ENTROPY_MIN_LENGTH: Final = 32
HIGH_ENTROPY_MIN_CLASSES: Final = 3
HIGH_ENTROPY_MIN_BITS: Final = 3.5
IDENTIFIER_MIN_DIGITS: Final = 4
HEX_RUN_MIN_LENGTH: Final = 8

_LEVELS: Final = {"critical": 50, "error": 40, "warning": 30, "info": 20, "debug": 10}

_CAMEL_BOUNDARY: Final = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])"
)
_KEY_SEPARATORS: Final = re.compile(r"[._\s-]+")

_PEM_BEGIN: Final = "-----BEGIN"
_PEM_END: Final = "-----END"
_DASHES: Final = "-----"
# The look-behind keeps the scan linear: a run of base64url characters is tried once, not once
# per "eyJ" inside it (an attacker-written log line is untrusted input, spec 2.3 invariant 8).
_JWT: Final = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_CARTO_TOKEN: Final = re.compile(r"t[0-9]{1,4}\.[A-Za-z0-9_-]{22}")
_AWS_KEY: Final = re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")
# scheme://user:password@host -> the password only; the user name helps the operator.
_URL_USERINFO: Final = re.compile(r"(?<=://)([^/\s@:]+):([^/\s@]+)(?=@)")
# password=..., api-key: ..., Authorization: Basic ..., ?token=...&, {"password": "..."},
# ;PWD={...};: the value after the secret word, up to whitespace or a separator (or a whole
# quoted or braced string). The word may be closed by a quote, escaped or not, before the
# separator, so JSON objects and Python reprs match. "token_count=3" and "secret_ref=vault://x"
# do not match because "_" follows the word, not "=" or ":". The short words pwd, pass, auth and
# sig must start a word or a camelCase hump ("dbPass=", not "bypass=" or "oauth="). A value that
# an earlier rule already turned into a mask ("token=[JWT]") keeps the more specific mask. Every
# quantifier is either bounded or stops at a fixed character, so the scan stays linear on
# attacker-written text (spec 2.3 invariant 8).
_SECRET_ASSIGNMENT: Final = re.compile(
    r"(?i)((?:api|private|access)[\s_.-]?key|client[\s_.-]?secret|password|passwd|passphrase"
    r"|secret|token|authorization|cookie|session|bearer|credentials?|signature"
    r"|(?<![A-Za-z])(?:pwd|pass|auth|sig)|(?-i:(?<=[a-z0-9])(?:P(?i:wd|ass)|A(?i:uth)|S(?i:ig))))"
    r"""(\\?["']?[ \t]{0,16}[=:][ \t]{0,16}|[ \t]{1,16}(?=\\?["']))(?!\\?["']?\[[A-Z_]+\])"""
    r"""(?:(?:basic|bearer|digest)[ \t]{1,16})?"""
    r"""(?:\\"[^"\n]*\\"|"[^"\n]*"|'[^'\n]*'|\{[^}\n]*\}|[^\s,;&]+)"""
)
# The look-behind stops a DSN user name (postgres://carto@db.internal) from reading as a mailbox.
_EMAIL: Final = re.compile(r"(?<![\w.%+/-])[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")
_ENTROPY_CANDIDATE: Final = re.compile(r"[A-Za-z0-9+/_-]+=*")
_ENTROPY_OTHER: Final = frozenset("+/=_-")
_HEX_RUN: Final = re.compile(rf"[0-9a-f]{{{HEX_RUN_MIN_LENGTH},}}")
_DIGIT_RUN: Final = re.compile(rf"\d{{{IDENTIFIER_MIN_DIGITS}}}")
_IDENTIFIER_JOINERS: Final = frozenset("-_")
_WHITESPACE: Final = re.compile(r"(\s+)")
_ISO_DATE: Final = r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
_ISO_TIME: Final = (
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,9})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9])?"
)
_ISO_EXEMPT: Final = re.compile(
    r"""[(\[{"']*"""
    rf"(?:{_ISO_DATE}(?:T{_ISO_TIME})?|{_ISO_TIME})"
    r"""[)\]},;:.!?"']*"""
)
# Spans the identifier rule must not rewrite: the masks above and canonical ULIDs (spec 5.4).
_PROTECTED: Final = re.compile(
    "("
    + "|".join(re.escape(mask) for mask in MASKS)
    + rf"|(?<![0-9A-Za-z]){ULID_CHARS}(?![0-9A-Za-z]))"
)


# ---------------------------------------------------------------------------------------------
# Key rules (1 and 2)
# ---------------------------------------------------------------------------------------------


def _key_segments(name: str) -> list[str]:
    """Lower-cased segments of a key split on ``_``, ``-``, ``.``, space, camelCase and digits."""
    spaced = _CAMEL_BOUNDARY.sub("_", name)
    return [segment for segment in _KEY_SEPARATORS.split(spaced.lower()) if segment]


def is_secret_key(name: str) -> bool:
    """True when a segment or adjacent pair names a secret and no qualifier ends the key (rule 1).

    ``secret_value``, ``password_hash`` and ``session_id`` are secrets; ``token_count`` and
    ``secret_ref`` describe one (:data:`SECRET_KEY_QUALIFIERS`) and are not.
    """
    segments = _key_segments(name)
    if not segments or segments[-1] in SECRET_KEY_QUALIFIERS:
        return False
    if any(segment in SECRET_KEY_WORDS for segment in segments):
        return True
    return any(pair in SECRET_KEY_PAIRS for pair in itertools.pairwise(segments))


def is_metric_key(name: str) -> bool:
    """True when the key's last segment names a metric, so its number is logged as is (rule 4)."""
    segments = _key_segments(name)
    return bool(segments) and segments[-1] in METRIC_KEY_QUALIFIERS


def is_drop_key(name: str) -> bool:
    """True when the normalised key names a body or raw value that is never logged (rule 2)."""
    return "_".join(_key_segments(name)) in DROP_KEYS


# ---------------------------------------------------------------------------------------------
# String masks (rule 3)
# ---------------------------------------------------------------------------------------------


def _mask_pem(text: str) -> str:
    """Replace every ``-----BEGIN ... -----END ...-----`` block with ``[PEM]`` in one pass."""
    if _PEM_BEGIN not in text:
        return text
    out: list[str] = []
    pos = 0
    while (start := text.find(_PEM_BEGIN, pos)) >= 0:
        end = text.find(_PEM_END, start + len(_PEM_BEGIN))
        if end < 0:
            break
        close = text.find(_DASHES, end + len(_PEM_END))
        if close < 0:
            break
        out.append(text[pos:start])
        out.append(PEM_MASK)
        pos = close + len(_DASHES)
    out.append(text[pos:])
    return "".join(out)


def _shannon_bits(token: str) -> float:
    """Shannon entropy of the characters of ``token`` in bits per character."""
    length = len(token)
    return -sum((count / length) * math.log2(count / length) for count in Counter(token).values())


def _is_high_entropy(token: str) -> bool:
    if len(token) < HIGH_ENTROPY_MIN_LENGTH:
        return False
    classes = sum(
        (
            any(char.isupper() for char in token),
            any(char.islower() for char in token),
            any(char.isdigit() for char in token),
            any(char in _ENTROPY_OTHER for char in token),
        )
    )
    return classes >= HIGH_ENTROPY_MIN_CLASSES and _shannon_bits(token) >= HIGH_ENTROPY_MIN_BITS


def _entropy_replacement(match: re.Match[str]) -> str:
    token = match.group()
    return HIGH_ENTROPY_MASK if _is_high_entropy(token) else token


def identifier_shape(token: str) -> str:
    """Shape of a string: digits to ``9``, letters to ``A``, everything else kept (spec 8.3)."""
    return "".join("9" if c.isdecimal() else "A" if c.isalpha() else c for c in token)


def _hex_replacement(match: re.Match[str]) -> str:
    """Shape a lower-case hex run that mixes letters and digits; leave words like ``deadbeef``."""
    run = match.group()
    if any(c.isdecimal() for c in run) and any(c.isalpha() for c in run):
        return identifier_shape(run)
    return run


def _looks_like_identifier(text: str) -> bool:
    """Run of four digits, or four digits in total in a ``-`` or ``_`` joined token (rule 3)."""
    if _DIGIT_RUN.search(text) is not None:
        return True
    if not any(char in _IDENTIFIER_JOINERS for char in text):
        return False
    return sum(1 for char in text if char.isdecimal()) >= IDENTIFIER_MIN_DIGITS


def _shape_if_identifier(piece: str) -> str:
    """Shape one whitespace-delimited piece, keeping mask markers and ULIDs inside it intact."""
    parts = _PROTECTED.split(piece)  # even indices are plain text, odd ones protected spans
    plain = "".join(parts[::2])
    if not _looks_like_identifier(plain) or _ISO_EXEMPT.fullmatch(plain) is not None:
        return piece
    return "".join(
        part if index % 2 else identifier_shape(part) for index, part in enumerate(parts)
    )


def mask_string(value: str) -> str:
    """Apply the value masks of rule 3 to one string, in order."""
    value = _mask_pem(value)
    value = _JWT.sub(JWT_MASK, value)
    value = _CARTO_TOKEN.sub(TOKEN_MASK, value)
    value = _AWS_KEY.sub(AWS_KEY_MASK, value)
    value = _URL_USERINFO.sub(rf"\1:{REDACTED}", value)
    value = _EMAIL.sub(EMAIL_MASK, value)
    value = _ENTROPY_CANDIDATE.sub(_entropy_replacement, value)
    value = _SECRET_ASSIGNMENT.sub(rf"\1\2{REDACTED}", value)
    value = _HEX_RUN.sub(_hex_replacement, value)
    return "".join(_shape_if_identifier(piece) for piece in _WHITESPACE.split(value))


# ---------------------------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------------------------


def _key_name(key: object) -> str:
    return key if isinstance(key, str) else str(key)


def _unique_key(taken: Mapping[str, object], masked: str) -> str:
    """``masked``, or ``masked#2``, ``masked#3``... when masking folded distinct keys together."""
    if masked not in taken:
        return masked
    suffix = 2
    while f"{masked}#{suffix}" in taken:
        suffix += 1
    return f"{masked}#{suffix}"


def _mask_number(value: float, *, metric: bool) -> object:
    """``value`` as a plain number, or its shape when it is not a metric and holds
    :data:`NUMBER_MIN_DIGITS` or more digits (rule 4)."""
    value = int(value) if isinstance(value, int) else float(value)  # drops IntEnum and the like
    if metric or (
        isinstance(value, int)
        and -(10 ** (NUMBER_MIN_DIGITS - 1)) < value < 10 ** (NUMBER_MIN_DIGITS - 1)
    ):
        return value
    try:
        text = repr(value)
    except ValueError:  # an int past the interpreter's digit limit is certainly not a metric
        return REDACTION_ERROR
    if sum(char.isdecimal() for char in text.lstrip("-0.")) < NUMBER_MIN_DIGITS:
        return value
    return identifier_shape(text)


def _walk_field(name: str, value: object, depth: int) -> object:
    if is_drop_key(name):
        return DROPPED
    if is_secret_key(name):
        return REDACTED
    return _walk(value, depth, metric=is_metric_key(name))


def _walk_mapping(mapping: Mapping[Any, Any], depth: int) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in mapping.items():
        name = _key_name(key)
        out[_unique_key(out, mask_string(name))] = _walk_field(name, value, depth + 1)
    return out


def _walk(value: object, depth: int, *, metric: bool = False) -> object:
    """Return the loggable form of ``value`` nested inside ``depth`` containers (rules 3 to 5).

    ``metric`` is true when ``value`` sits directly under a key naming a metric (rule 4).
    """
    if depth > MAX_DEPTH:
        return REDACTION_ERROR
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, enum.Enum):
        return _walk(value.value, depth, metric=metric)
    if isinstance(value, int | float):
        return _mask_number(value, metric=metric)
    if isinstance(value, str):
        return mask_string(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return mask_string(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return _mask_number(value.total_seconds(), metric=metric)
    if isinstance(value, BaseModel):
        return _walk_mapping(value.model_dump(), depth)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _walk_mapping(dataclasses.asdict(value), depth)
    if isinstance(value, Mapping):
        return _walk_mapping(value, depth)
    if isinstance(value, Sequence | AbstractSet):
        return [_walk(item, depth + 1) for item in value]
    return mask_string(str(value))


def redact(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor enforcing spec 2.3 invariant 7 on every event (see module docs).

    Returns a new event dict holding only JSON-native values: ``None``, ``bool``, ``int`` and
    ``float`` (small, or under a metric key), masked ``str``, ``list`` and ``dict`` with masked
    ``str`` keys. Never raises: a key whose value cannot be walked is logged as
    ``[REDACTION_ERROR]``.
    """
    del logger, method_name
    redacted: dict[str, Any] = {}
    for key, value in list(event_dict.items()):
        name = REDACTION_ERROR
        try:
            name = _key_name(key)
            redacted[_unique_key(redacted, mask_string(name))] = _walk_field(name, value, 0)
        except Exception:  # a log call must never take the service down (spec 2.3.7)
            redacted[_unique_key(redacted, name)] = REDACTION_ERROR
    return redacted


# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------


class _BindService:
    """Processor stamping ``service`` on every event, in every thread and task."""

    def __init__(self, service: str) -> None:
        self._service = service

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        del logger, method_name
        event_dict["service"] = self._service
        return event_dict


class _RootHandler(logging.StreamHandler[TextIO]):
    """Root handler for standard-library records, resolving ``sys.stdout`` at emit time.

    structlog's ``WriteLogger`` picks up ``sys.stdout`` when a line is written; this handler
    does the same when no explicit stream was given, so both paths always land in one place.
    """

    def __init__(self, stream: TextIO | None) -> None:
        super().__init__(stream if stream is not None else sys.stdout)
        self._fixed_stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        if self._fixed_stream is None:
            self.stream = sys.stdout
        super().emit(record)


_HOOK_MARK: Final = "_carto_redacting_hook"
WARNINGS_LOGGER: Final = "py.warnings"
"""Logger name of Python warnings, as ``logging.captureWarnings`` uses."""

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def _log_exception(event: str, exc_info: _ExcInfo, **fields: object) -> None:
    """Log ``exc_info`` at CRITICAL through the processor chain; never raise from a hook."""
    with contextlib.suppress(Exception):  # never fall back to printing the raw traceback
        get_logger().critical(event, exc_info=exc_info, **fields)


def _show_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    """``warnings.showwarning`` replacement: the warning goes to the root logger, redacted."""
    del file, line  # the source line adds nothing an operator needs; files are never written
    logging.getLogger(WARNINGS_LOGGER).warning(
        "%s:%s: %s: %s", filename, lineno, category.__name__, message
    )


def _is_ours(hook: object) -> bool:
    return bool(getattr(hook, _HOOK_MARK, False))


def _sys_hook(
    previous: Callable[[type[BaseException], BaseException, TracebackType | None], object] | None,
) -> Callable[[type[BaseException], BaseException, TracebackType | None], None]:
    def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        _log_exception("uncaught exception", (kind, value, tb))
        if previous is not None:
            previous(kind, value, tb)

    setattr(hook, _HOOK_MARK, True)
    return hook


def _thread_hook(
    previous: Callable[[threading.ExceptHookArgs], object] | None,
) -> Callable[[threading.ExceptHookArgs], None]:
    def hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            thread = args.thread.name if args.thread is not None else None
            _log_exception(
                "uncaught exception in thread",
                (args.exc_type, args.exc_value, args.exc_traceback),
                thread=thread,
            )
        if previous is not None:
            previous(args)

    setattr(hook, _HOOK_MARK, True)
    return hook


def _unraisable_hook(
    previous: Callable[[Any], object] | None,
) -> Callable[[Any], None]:
    def hook(args: Any) -> None:
        if args.exc_value is not None:
            _log_exception(
                "unraisable exception",
                (args.exc_type, args.exc_value, args.exc_traceback),
                detail=args.err_msg,
                object_type=type(args.object).__name__,
            )
        if previous is not None:
            previous(args)

    setattr(hook, _HOOK_MARK, True)
    return hook


def _install_hooks() -> None:
    """Route warnings and uncaught exceptions through :func:`redact` (spec 2.3 invariant 7).

    The interpreter's defaults print the raw message and traceback to stderr, so they are
    replaced, never called. A hook someone else installed before (a test runner's) is kept and
    called after logging. Installing twice changes nothing.
    """
    warnings.showwarning = _show_warning
    if not _is_ours(sys.excepthook):
        previous = None if sys.excepthook is sys.__excepthook__ else sys.excepthook
        sys.excepthook = _sys_hook(previous)
    if not _is_ours(threading.excepthook):
        previous_thread = (
            None if threading.excepthook is threading.__excepthook__ else threading.excepthook
        )
        threading.excepthook = _thread_hook(previous_thread)
    if not _is_ours(sys.unraisablehook):
        previous_unraisable = (
            None if sys.unraisablehook is sys.__unraisablehook__ else sys.unraisablehook
        )
        sys.unraisablehook = _unraisable_hook(previous_unraisable)


def configure_logging(
    service: str,
    level: str = "INFO",
    json_output: bool = True,
    *,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog and the root ``logging`` logger with :func:`redact` before the renderer.

    The chain is ``merge_contextvars``, the service binder, ``add_log_level``,
    ``TimeStamper(fmt="iso", utc=True)``, ``format_exc_info`` (so traceback text is masked like
    any other string), :func:`redact`, then ``JSONRenderer`` (one JSON object per line) or, when
    ``json_output`` is false, ``ConsoleRenderer`` for local development. Events below ``level``
    (``critical``, ``error``, ``warning``, ``info``, ``debug``; case does not matter) are dropped
    before any processor runs. ``stream`` defaults to ``sys.stdout``, resolved at log time;
    tests pass an ``io.StringIO``.

    The same chain, with the originating logger's name added, is installed as the only handler
    of the root ``logging`` logger, replacing whatever was there: records from third-party
    libraries (uvicorn, httpx, database drivers) and stray ``logging.getLogger()`` calls are
    rendered through :func:`redact` too, never through ``logging.lastResort``. Libraries that
    configure their own handlers must be told not to (uvicorn: ``log_config=None``). The HTTP
    client loggers of :data:`QUIET_LIBRARY_LOGGERS` are held at WARNING because their INFO lines
    carry request URLs and query strings.

    Python warnings are logged as WARNING records of the :data:`WARNINGS_LOGGER` logger, and
    uncaught exceptions (main thread, other threads, and the "exception ignored" path of
    ``sys.unraisablehook``) as CRITICAL events with their traceback, all through :func:`redact`
    instead of the interpreter's raw stderr output (ADR 0040).
    """
    level_name = level.strip().lower()
    if level_name not in _LEVELS:
        msg = f"unknown log level {level!r}; expected one of {', '.join(_LEVELS)}"
        raise ValueError(msg)
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _BindService(service),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, redact, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level_name]),
        context_class=dict,
        logger_factory=structlog.WriteLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )
    handler = _RootHandler(stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[structlog.stdlib.add_logger_name, *shared],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                redact,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(_LEVELS[level_name])
    for name in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, _LEVELS[level_name]))
    _install_hooks()


def get_logger(**initial_values: Any) -> FilteringBoundLogger:
    """Return a structlog logger with ``initial_values`` bound.

    If :func:`configure_logging` has not run yet, the process is configured with the defaults
    and ``service=unconfigured`` first, so a forgotten call can never produce unredacted output.
    """
    if not structlog.is_configured():
        configure_logging(DEFAULT_SERVICE)
    return cast("FilteringBoundLogger", structlog.get_logger(**initial_values))
