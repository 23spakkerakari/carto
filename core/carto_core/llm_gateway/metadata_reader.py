"""The PostgreSQL :class:`~carto_core.llm_gateway.payload.MetadataReader` (spec 9.9; plan M2
package G "payload builder reads metadata tables only"; ADR 0056).

A field summary is read from ``fields`` (``id``, ``path``, ``shapes_json``), ``systems``
(``name``) and ``templates`` (``template_text``), and an entity's members through
``entity_fields``; the statements name those tables and nothing else, so no code path leads
from the event store to a payload. Every value read back is untrusted text: it goes through
:meth:`carto_core.llm_gateway.payload.FieldRecord.summary`, the same cleaning and bounds the
in-memory reader applies (control characters dropped, token-shaped substrings masked, lengths
clipped), and the builders clean it once more. Requests are bounded (:data:`MAX_FIELD_IDS` ids
per call, the top :data:`MAX_TOP_SHAPES` shapes per field) and errors name the driver's
exception class only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.llm_gateway.payload import FieldRecord
from carto_core.llm_gateway.schemas import FieldSummary

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "MAX_FIELD_IDS",
    "MAX_TOP_SHAPES",
    "MetadataReaderError",
    "PostgresMetadataReader",
    "summary_from_row",
]

MAX_FIELD_IDS: Final = 256
"""Ids per ``field_summaries`` call; a task takes at most 64 fields (``MAX_FIELDS_PER_REQUEST``)."""
MAX_TOP_SHAPES: Final = 8
"""Shapes per summary, most frequent first (``shapes_json`` is already ordered by share)."""


class MetadataReaderError(Exception):
    """The summaries could not be read; the message names the exception type only."""


_BY_IDS = sqlalchemy.text(
    "SELECT f.id AS field_id, f.path, f.system_id, f.shapes_json, s.name AS system_name, "
    "t.template_text FROM fields AS f LEFT JOIN systems AS s ON s.id = f.system_id "
    "AND s.tenant_id = f.tenant_id LEFT JOIN templates AS t ON t.tenant_id = f.tenant_id "
    "AND t.system_id = f.system_id AND t.id = f.template_id WHERE f.tenant_id = :tenant_id "
    "AND f.id = ANY(:ids) ORDER BY f.id"
)
_BY_ENTITY = sqlalchemy.text(
    "SELECT DISTINCT ON (f.id) f.id AS field_id, f.path, f.system_id, f.shapes_json, "
    "s.name AS system_name, t.template_text FROM fields AS f LEFT JOIN systems AS s "
    "ON s.id = f.system_id AND s.tenant_id = f.tenant_id LEFT JOIN templates AS t "
    "ON t.tenant_id = f.tenant_id AND t.system_id = f.system_id AND t.id = f.template_id "
    "JOIN entity_fields AS e ON e.tenant_id = f.tenant_id AND e.field_id = f.id "
    "WHERE f.tenant_id = :tenant_id AND e.entity_id = :entity_id ORDER BY f.id"
)


def _top_shapes(value: object) -> tuple[str, ...]:
    """The shape strings of a ``shapes_json`` column (``[{"shape", "share"}, ...]``), in the
    stored order; a column of any other shape yields none."""
    if isinstance(value, str | bytes):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    if not isinstance(value, list):
        return ()
    shapes: list[str] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("shape"), str):
            shapes.append(item["shape"])
        if len(shapes) == MAX_TOP_SHAPES:
            break
    return tuple(shapes)


def summary_from_row(tenant_id: str, row: Mapping[Any, Any]) -> FieldSummary:
    """One joined row (``field_id``, ``path``, ``system_id``, ``shapes_json``, ``system_name``,
    ``template_text``) as a cleaned, bounded :class:`FieldSummary`; the system id stands in for
    a missing system name and a missing template gives an empty text."""
    system_name = row.get("system_name")
    template_text = row.get("template_text")
    return FieldRecord(
        tenant_id=tenant_id,
        field_id=str(row["field_id"]),
        path=str(row.get("path") or ""),
        system_name=str(system_name) if system_name else str(row.get("system_id") or ""),
        top_shapes=_top_shapes(row.get("shapes_json")),
        template_text=str(template_text) if template_text else "",
    ).summary()


class PostgresMetadataReader:
    """See the module docstring."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def field_summaries(self, tenant_id: str, field_ids: Sequence[str]) -> list[FieldSummary]:
        wanted = list(dict.fromkeys(field_ids))[:MAX_FIELD_IDS]
        if not wanted:
            return []
        return self._read(_BY_IDS, {"tenant_id": tenant_id, "ids": wanted}, tenant_id)

    def entity_members(self, tenant_id: str, entity_id: str) -> list[FieldSummary]:
        return self._read(_BY_ENTITY, {"tenant_id": tenant_id, "entity_id": entity_id}, tenant_id)

    def _read(
        self, statement: sqlalchemy.TextClause, parameters: dict[str, Any], tenant_id: str
    ) -> list[FieldSummary]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement, parameters).mappings().all()
        except SQLAlchemyError as exc:
            msg = f"field summary read failed: {type(exc).__name__}"
            raise MetadataReaderError(msg) from exc
        return [summary_from_row(tenant_id, row) for row in rows]
