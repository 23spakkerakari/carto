"""Helpers shared by the record parsers (spec 8.2, 2.3 invariant 8).

:func:`flatten` turns a nested mapping (a JSON object, an OTLP body, a webhook payload, a DB
row) into the flat ``path -> string`` mapping of :class:`~carto_edge.pipeline.model.ParsedRecord`:
nested keys become dotted paths (``payload.order.id``), arrays are indexed up to
:data:`MAX_ARRAY_ITEMS` elements, nesting stops at :data:`MAX_DEPTH` and the number of fields at
:data:`MAX_FIELDS` (spec 8.2 item 1). Every limit that cuts something off leaves a note so the
pipeline can count it; nothing raises. :func:`stringify` fixes how scalars are rendered so the
same value gives the same string whichever parser produced it (tokens are HMACs of these strings,
spec 8.4). :func:`key_signature` builds the ``keys:<sorted top-level keys>`` template of
structured records without a message (ADR 0016).

Field names leave the edge in clear (attribute keys, identifier and dropped field names, the
``keys:`` template), so a name must never carry a value (spec 2.3 invariant 2). JSON maps keyed
by order id, a CSV data row read as a header, or a logfmt line with bare words would put values
in names. :func:`safe_path` masks every path segment that looks like a value, with ``*``: an
``@`` after the first character (``@attr`` is the XML attribute convention), a run of three or
more digits, or more than :data:`MAX_SEGMENT_LEN` characters. The parser applies it to every
field path and to the signature keys.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final
from uuid import UUID

__all__ = [
    "MAX_ARRAY_ITEMS",
    "MAX_DEPTH",
    "MAX_FIELDS",
    "MAX_KEY_LEN",
    "MAX_SIGNATURE_KEYS",
    "NOTE_ARRAY_LIMIT",
    "NOTE_DEPTH_LIMIT",
    "NOTE_FIELD_LIMIT",
    "Flattened",
    "Segment",
    "SegmentPath",
    "clean_text",
    "flatten",
    "key_signature",
    "safe_path",
    "safe_segment",
    "stringify",
    "utf8_len",
]

MAX_DEPTH: Final = 12
"""Spec 8.2 item 1: nesting levels kept; deeper values are dropped with a note."""
MAX_ARRAY_ITEMS: Final = 20
"""Spec 8.2 item 1: array elements kept per array."""
MAX_FIELDS: Final = 1024
"""Fields kept per record; the canonical event allows 256 attributes and 64 identifiers."""
MAX_KEY_LEN: Final = 256
"""Longest path kept (``MAX_FIELD_NAME_LEN`` of the canonical event)."""
MAX_SIGNATURE_KEYS: Final = 32
"""Keys listed in a ``keys:`` template (ADR 0016)."""
MAX_SIGNATURE_KEY_LEN: Final = 64

MAX_SEGMENT_LEN: Final = 64
"""A path segment longer than this is a value, not a name."""
MASKED_SEGMENT: Final = "*"
_DIGIT_RUN: Final = re.compile(r"[0-9]{3}")
NOTE_DEPTH_LIMIT: Final = "depth_limit"
NOTE_ARRAY_LIMIT: Final = "array_limit"
NOTE_FIELD_LIMIT: Final = "field_limit"
NOTE_KEY_LIMIT: Final = "key_limit"
_SURROGATE: Final = re.compile("[\\ud800-\\udfff]")
REPLACEMENT_CHARACTER: Final = chr(0xFFFD)


Segment = tuple[str, bool]
"""One step of a field path: the key (or array index as text) and whether it is an index."""
SegmentPath = tuple[Segment, ...]


@dataclass(frozen=True, slots=True)
class Flattened:
    """A flattened record: ``fields`` by path, the top-level keys in input order, limit notes.

    ``structure`` gives the segments of every path with more than one: the dotted path is only
    a label, since a key may itself contain a dot (``{"m": {"x.y": 1}}`` and ``{"m.x": {"y":
    1}}`` both read ``m.x.y``) and a key may be all digits (``{"stock": {"42": 1}}`` is not an
    array). The name gate works on the segments (ADR 0030 amendment); a path absent from
    ``structure`` is one segment, not an index."""

    fields: dict[str, str]
    top_keys: tuple[str, ...]
    notes: tuple[str, ...]
    structure: dict[str, SegmentPath] = field(default_factory=dict)


def utf8_len(text: str) -> int:
    """Number of UTF-8 bytes of ``text`` (``parse.max_record_bytes`` counts bytes, spec 8.2)."""
    return len(text.encode("utf-8", errors="surrogatepass"))


def clean_text(text: str) -> str:
    """``text`` with every lone surrogate replaced by U+FFFD.

    ``json.loads`` accepts an escaped lone surrogate and a ``str`` can hold one, but it has no
    UTF-8 encoding: hashing, persisting or forwarding it would raise. The parsers normalize at
    their decode boundary with this function (keys, string values and text lines), so nothing
    downstream sees one. ASCII text is returned as it is."""
    if text.isascii():
        return text
    return _SURROGATE.sub(REPLACEMENT_CHARACTER, text)


def stringify(value: Any) -> str | None:
    """Render a scalar as the string the pipeline works on; ``None`` for null.

    Strings stay as they are; booleans become ``true``/``false`` (checked before integers,
    since ``bool`` is an ``int``); numbers use ``str``; ``datetime`` and ``date`` use ISO
    8601; ``bytes`` are hex; ``Decimal``, ``UUID`` and enums use their natural text. Anything
    else falls back to ``str``, bounded by the caller's size limit.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, Enum):
        return stringify(value.value)
    return clean_text(str(value))


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def flatten(
    value: Mapping[Any, Any],
    *,
    max_depth: int = MAX_DEPTH,
    max_items: int = MAX_ARRAY_ITEMS,
    max_fields: int = MAX_FIELDS,
) -> Flattened:
    """Flatten a nested mapping to dotted paths (spec 8.2 item 1).

    Iterative (no recursion, so adversarial nesting cannot exhaust the stack): a work stack of
    ``(prefix, segments, depth, value)``. Mappings contribute ``prefix.key``; sequences contribute
    ``prefix.<index>`` for the first ``max_items`` elements; scalars are rendered with
    :func:`stringify` and nulls are skipped. Values nested deeper than ``max_depth`` levels,
    elements past ``max_items`` and fields past ``max_fields`` are dropped and noted. Keys are
    rendered with ``str`` (JSON keys are strings already; DB rows may use other types) and lose
    their lone surrogates like string values (:func:`clean_text`); paths
    longer than :data:`MAX_KEY_LEN` are dropped and noted, and a scalar under an empty key
    (``{"": 1}``) is dropped since a field needs a name. Field order follows a
    depth-first walk in input order, so ``top_keys`` and the ``fields`` order are stable.
    ``structure`` holds the segments of each path with more than one (see :class:`Flattened`).
    """
    fields: dict[str, str] = {}
    notes: set[str] = set()
    top_keys = tuple(clean_text(str(key)) for key in value)
    if len(value) <= max_fields and not any(
        isinstance(child, Mapping | list | tuple) for child in value.values()
    ):
        # Fast path: a flat record of scalars (the common NDJSON and row shape).
        for key, child in value.items():
            text = stringify(child)
            path = clean_text(str(key))
            if text is None or not path:
                continue
            if len(path) > MAX_KEY_LEN:
                notes.add(NOTE_KEY_LIMIT)
                continue
            fields[path] = text
        return Flattened(fields=fields, top_keys=top_keys, notes=tuple(sorted(notes)))
    structure: dict[str, SegmentPath] = {}
    stack: list[tuple[str, SegmentPath, int, Any]] = [("", (), 0, value)]
    while stack:
        prefix, segments, depth, current = stack.pop()
        if isinstance(current, Mapping):
            if depth >= max_depth:
                notes.add(NOTE_DEPTH_LIMIT)
                continue
            children: list[tuple[str, SegmentPath, int, Any]] = []
            for key, child in current.items():
                name = clean_text(str(key))
                children.append((_join(prefix, name), (*segments, (name, False)), depth + 1, child))
            stack.extend(reversed(children))
        elif isinstance(current, list | tuple):
            if depth >= max_depth:
                notes.add(NOTE_DEPTH_LIMIT)
                continue
            if len(current) > max_items:
                notes.add(NOTE_ARRAY_LIMIT)
            items: list[tuple[str, SegmentPath, int, Any]] = []
            for index, child in enumerate(current[:max_items]):
                text_index = str(index)
                items.append(
                    (_join(prefix, text_index), (*segments, (text_index, True)), depth + 1, child)
                )
            stack.extend(reversed(items))
        else:
            text = stringify(current)
            if text is None or not prefix:
                continue
            if len(prefix) > MAX_KEY_LEN:
                notes.add(NOTE_KEY_LIMIT)
                continue
            if len(fields) >= max_fields and prefix not in fields:
                notes.add(NOTE_FIELD_LIMIT)
                continue
            fields[prefix] = text
            if len(segments) > 1:
                structure[prefix] = segments
            else:
                structure.pop(prefix, None)
    return Flattened(
        fields=fields, top_keys=top_keys, notes=tuple(sorted(notes)), structure=structure
    )


def _signature_key(key: str) -> str:
    """One key as it appears in a ``keys:`` template: whitespace, commas and control
    characters become ``_`` so the template stays one line of comma-separated names."""
    cleaned = "".join("_" if (c.isspace() or c == "," or not c.isprintable()) else c for c in key)
    return cleaned[:MAX_SIGNATURE_KEY_LEN]


def key_signature(keys: Iterable[str]) -> str:
    """``keys:<sorted top-level keys>`` (ADR 0016), at most :data:`MAX_SIGNATURE_KEYS` keys."""
    unique = sorted({_signature_key(key) for key in keys})
    return "keys:" + ",".join(unique[:MAX_SIGNATURE_KEYS])


def safe_segment(segment: str) -> str:
    """``*`` when a name segment looks like a value (see the module docstring)."""
    if (
        len(segment) > MAX_SEGMENT_LEN
        or "@" in segment[1:]
        or _DIGIT_RUN.search(segment) is not None
    ):
        return MASKED_SEGMENT
    return segment


@functools.lru_cache(maxsize=65_536)
def safe_path(path: str) -> str:
    """``path`` with every value-like segment masked (``stock.SKU-12345`` -> ``stock.*``)."""
    if "." not in path:
        return safe_segment(path)
    return ".".join(safe_segment(segment) for segment in path.split("."))
