"""Payload builders: metadata in, request models out (spec 9.9; plan M2 package G).

Spec 9.9: each task has a fixed payload schema built only from metadata tables, and the model
never receives tokens, raw values, reveal output, actor data, connector configuration or
secrets. The builders here read through :class:`MetadataReader`, which can only answer with
:class:`~.schemas.FieldSummary` objects (field path, system name, top shapes, template text,
low-cardinality attribute values), so there is no code path from ``events`` to a payload. On
top of the reader's contract the builders clean every text (control and format characters
removed, whitespace collapsed, lengths clipped to the schema bounds), replace any token-shaped
substring in a text with ``<*>`` and drop any attribute value that is one, as defence in depth
against a reader that returns one by mistake or a log line that quotes one.

The PostgreSQL reader is the orchestrator's (packages A/B/C own the tables). It is expected to
read ``fields.id``, ``fields.path``, ``fields.system_id`` joined to ``systems.name``,
``fields.shapes_json`` (the profiler's top shapes), ``fields.template_id`` joined to
``templates.template_text``, and ``entity_fields.entity_id, entity_fields.field_id`` for
:meth:`MetadataReader.entity_members`. Event attributes live in ClickHouse and are not available
to this package: in M2 the ``attributes`` of a summary stay empty unless the caller supplies
:class:`~.schemas.AttributeSummary` lists built from the profiler's shape data through the
``attributes`` argument of the builders.

:func:`forbidden_content_check` is the test oracle: given the payload JSON and the strings that
must not appear (planted tokens, event values, identifier attributes), it returns every
finding, and a token-shaped substring is always a finding.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

from pydantic import ValidationError

from carto_core.llm_gateway.schemas import (
    MAX_ATTRIBUTE_VALUES,
    MAX_FIELDS_PER_REQUEST,
    MAX_TEMPLATE_TEXT_LEN,
    AttributeSummary,
    FieldSummary,
    LabelFieldRequest,
    NameEntityRequest,
)

__all__ = [
    "TOKEN_SEARCH",
    "AttributeRecord",
    "FieldRecord",
    "InMemoryMetadataReader",
    "MetadataReader",
    "PayloadError",
    "build_label_field_payload",
    "build_name_entity_payload",
    "clean_text",
    "forbidden_content_check",
]

TOKEN_SEARCH: Final = re.compile(r"t[1-9][0-9]{0,3}\.[A-Za-z0-9_-]{22}")
"""A carto token anywhere inside a string (spec 8.4), the unanchored form of ``TOKEN_PATTERN``."""

_MAX_PATH_LEN: Final = 256
_MAX_SYSTEM_NAME_LEN: Final = 128
_MAX_SHAPE_LEN: Final = 64
_MAX_SHAPES: Final = 8
_MAX_ATTRIBUTE_KEY_LEN: Final = 128
_MAX_ATTRIBUTE_VALUE_LEN: Final = 64
_MAX_ATTRIBUTES: Final = 16
_MAX_FIELD_ID_LEN: Final = 512
_WHITESPACE: Final = re.compile(r"\s+")


class PayloadError(ValueError):
    """The payload could not be built; the message names the reason, never the content."""


@runtime_checkable
class MetadataReader(Protocol):
    """Metadata-only access to fields (module docstring); nothing here can return a value."""

    def field_summaries(self, tenant_id: str, field_ids: Sequence[str]) -> list[FieldSummary]:
        """The summaries of the fields that exist, in any order; unknown ids are left out."""
        ...

    def entity_members(self, tenant_id: str, entity_id: str) -> list[FieldSummary]:
        """The summaries of the identifier fields grouped under ``entity_id``."""
        ...


@dataclass(frozen=True, slots=True)
class AttributeRecord:
    """An attribute as the in-memory reader stores it; ``identifier`` marks the class the
    payload must never carry (spec 9.9, plan M2 package G)."""

    key: str
    values: tuple[str, ...]
    identifier: bool = False


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """What the in-memory reader knows about a field, including what must stay behind:
    ``tokens`` and ``event_values`` exist so tests can plant them and prove they never leave."""

    tenant_id: str
    field_id: str
    path: str
    system_name: str
    top_shapes: tuple[str, ...] = ()
    template_text: str = ""
    attributes: tuple[AttributeRecord, ...] = ()
    tokens: tuple[str, ...] = ()
    event_values: tuple[str, ...] = ()
    entity_id: str | None = None

    def summary(self) -> FieldSummary:
        """Metadata only: no token, no event value, no identifier-class attribute."""
        attributes = [
            AttributeSummary(
                key=clean_text(attribute.key, _MAX_ATTRIBUTE_KEY_LEN),
                values=_clean_values(attribute.values),
            )
            for attribute in self.attributes
            if not attribute.identifier
        ]
        return FieldSummary(
            field_id=self.field_id[:_MAX_FIELD_ID_LEN],
            path=clean_text(self.path, _MAX_PATH_LEN),
            system_name=clean_text(self.system_name, _MAX_SYSTEM_NAME_LEN),
            top_shapes=_clean_values(self.top_shapes)[:_MAX_SHAPES],
            template_text=clean_text(self.template_text, MAX_TEMPLATE_TEXT_LEN),
            attributes=[a for a in attributes if a.key and a.values][:_MAX_ATTRIBUTES],
        )


@dataclass(slots=True)
class InMemoryMetadataReader:
    """A reader over :class:`FieldRecord` objects, for tests and the offline path."""

    records: list[FieldRecord] = field(default_factory=list)

    def add(self, record: FieldRecord) -> None:
        self.records.append(record)

    def field_summaries(self, tenant_id: str, field_ids: Sequence[str]) -> list[FieldSummary]:
        wanted = set(field_ids)
        return [
            record.summary()
            for record in self.records
            if record.tenant_id == tenant_id and record.field_id in wanted
        ]

    def entity_members(self, tenant_id: str, entity_id: str) -> list[FieldSummary]:
        return [
            record.summary()
            for record in self.records
            if record.tenant_id == tenant_id and record.entity_id == entity_id
        ]


def clean_text(text: str, limit: int) -> str:
    """Normalise to NFC, drop control and format characters, replace any token-shaped
    substring with the template placeholder ``<*>``, collapse whitespace, clip."""
    normalised = TOKEN_SEARCH.sub("<*>", unicodedata.normalize("NFC", text))
    kept = "".join(
        " " if char in "\t\n\r" else char
        for char in normalised
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn"} or char in "\t\n\r"
    )
    return _WHITESPACE.sub(" ", kept).strip()[:limit]


def _clean_values(values: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        if TOKEN_SEARCH.search(value):
            continue
        text = clean_text(value, _MAX_ATTRIBUTE_VALUE_LEN)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) == MAX_ATTRIBUTE_VALUES:
            break
    return cleaned


def _bounded(summary: FieldSummary, extra: Sequence[AttributeSummary]) -> FieldSummary:
    """Re-clean and re-clip a summary; the reader already met the schema, this is the belt."""
    attributes: list[AttributeSummary] = []
    for attribute in [*summary.attributes, *extra]:
        key = clean_text(attribute.key, _MAX_ATTRIBUTE_KEY_LEN)
        values = _clean_values(attribute.values)
        if key and values:
            attributes.append(AttributeSummary(key=key, values=values))
        if len(attributes) == _MAX_ATTRIBUTES:
            break
    shapes = _clean_values(summary.top_shapes)[:_MAX_SHAPES]
    return FieldSummary(
        field_id=summary.field_id[:_MAX_FIELD_ID_LEN],
        path=clean_text(summary.path, _MAX_PATH_LEN),
        system_name=clean_text(summary.system_name, _MAX_SYSTEM_NAME_LEN),
        top_shapes=[shape[:_MAX_SHAPE_LEN] for shape in shapes],
        template_text=clean_text(summary.template_text, MAX_TEMPLATE_TEXT_LEN),
        attributes=attributes,
    )


def _ordered_unique(ids: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for field_id in ids:
        seen.setdefault(field_id, None)
    return list(seen)


def _summaries(
    found: Sequence[FieldSummary],
    order: Sequence[str],
    attributes: Mapping[str, Sequence[AttributeSummary]] | None,
) -> list[FieldSummary]:
    by_id = {summary.field_id: summary for summary in found}
    extra = attributes or {}
    return [
        _bounded(by_id[field_id], extra.get(field_id, ()))
        for field_id in order
        if field_id in by_id
    ]


def build_label_field_payload(
    reader: MetadataReader,
    tenant_id: str,
    field_ids: Sequence[str],
    *,
    attributes: Mapping[str, Sequence[AttributeSummary]] | None = None,
) -> LabelFieldRequest:
    """The ``label_field`` request for ``field_ids`` (order kept, duplicates and unknown ids
    dropped). ``attributes`` adds caller-supplied low-cardinality values per field id."""
    wanted = _ordered_unique(field_ids)
    if not wanted:
        msg = "label_field payload needs at least one field id"
        raise PayloadError(msg)
    if len(wanted) > MAX_FIELDS_PER_REQUEST:
        msg = f"label_field payload takes at most {MAX_FIELDS_PER_REQUEST} fields"
        raise PayloadError(msg)
    summaries = _summaries(reader.field_summaries(tenant_id, wanted), wanted, attributes)
    if not summaries:
        msg = "label_field payload: none of the field ids exist"
        raise PayloadError(msg)
    try:
        return LabelFieldRequest(fields=summaries)
    except ValidationError as exc:
        msg = f"label_field payload rejected by the schema ({exc.error_count()} errors)"
        raise PayloadError(msg) from None


def build_name_entity_payload(
    reader: MetadataReader,
    tenant_id: str,
    entity_id: str,
    *,
    attributes: Mapping[str, Sequence[AttributeSummary]] | None = None,
) -> NameEntityRequest:
    """The ``name_entity`` request for ``entity_id`` from its member fields (ordered by id)."""
    members = reader.entity_members(tenant_id, entity_id)
    if not members:
        msg = "name_entity payload: the entity has no member fields"
        raise PayloadError(msg)
    order = _ordered_unique(sorted(member.field_id for member in members))
    if len(order) > MAX_FIELDS_PER_REQUEST:
        order = order[:MAX_FIELDS_PER_REQUEST]
    summaries = _summaries(members, order, attributes)
    try:
        return NameEntityRequest(entity_id=entity_id, fields=summaries)
    except ValidationError as exc:
        msg = f"name_entity payload rejected by the schema ({exc.error_count()} errors)"
        raise PayloadError(msg) from None


def forbidden_content_check(payload_json: str, *, forbidden: Iterable[str]) -> tuple[str, ...]:
    """Every forbidden string present in ``payload_json`` and every token-shaped substring, as
    a tuple that is empty when the payload is clean. The test oracle of plan M2 package G."""
    findings = [needle for needle in forbidden if needle and needle in payload_json]
    findings.extend(f"token:{match}" for match in TOKEN_SEARCH.findall(payload_json))
    return tuple(findings)
