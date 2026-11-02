"""The metadata store: templates, fields, systems, sources and the edge inventory in PostgreSQL
(spec 7.3; ADR 0050, 0056).

Who writes what (ADR 0056):

- ``ingest-api`` upserts the distinct templates of every accepted batch
  (:meth:`MetadataStore.upsert_templates`, count incremented) inside the batch ledger's
  ``done`` transaction, so a retried batch never double-counts; the bundle loader upserts
  templates and fields from ``templates.json`` and ``fields.json``.
- The edge owns ``fields.class``, ``policy``, ``pinned``, ``forms`` and the shapes and counts it
  observed (:meth:`MetadataStore.upsert_fields`); it never touches the profiler's statistics.
- The profiler owns ``distinct_estimate``, ``null_rate``, ``shapes_json``, ``quality``,
  ``is_candidate``, ``count`` and ``last_seen`` (:meth:`MetadataStore.update_field_profiles`); a
  field it meets in the event store before any bundle or pin named it is inserted as a
  tokenized identifier, which is the only kind of field that has identifier rows.
- The edge inventory (ADR 0050) replaces core's copy for one ``edge_id`` as a whole
  (:meth:`MetadataStore.replace_inventory`): systems first (``sources`` keeps its foreign key),
  then sources with their configuration (no secrets; the edge stripped them and core treats the
  rest as untrusted text), the payload in ``edge_inventories``, and the field policy pins
  applied to the ``fields`` rows they match. Pins are kept with the inventory, so a later
  ``upsert_fields`` applies them again. A source the edge reported before and no longer lists
  is marked disabled, not deleted.

A pin names a field as ``system/template/path`` or ``system/*/path`` (any template, spec 8.3).
:class:`InMemoryMetadataStore` is the reference and the test double; :class:`PostgresMetadataStore`
uses SQLAlchemy Core ``text`` with ``ON CONFLICT`` on one engine. Every statement is fixed text
with bound parameters (spec 14.7); errors raise :class:`MetadataError` naming the driver error
class only. Everything read back is untrusted (bounded types, no evaluation).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.db.postgres import ping_engine
from carto_core.ingest.health import SourceHealthRecord

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection, Row

    from carto_schema.inventory import EdgeInventory, InventoryFieldPolicy

__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "FieldProfileUpdate",
    "FieldRecord",
    "InMemoryMetadataStore",
    "InventoryResult",
    "MetadataError",
    "MetadataNotFoundError",
    "MetadataStaleError",
    "MetadataStore",
    "PostgresMetadataStore",
    "SourceRecord",
    "SystemRecord",
    "TemplateRecord",
    "pin_matches",
    "updated_at_version",
]

DEFAULT_PAGE: Final = 100
MAX_PAGE: Final = 1000
PIN_SEGMENTS: Final = 3
Shapes = tuple[tuple[str, float], ...]
"""``(shape, share)`` pairs, most frequent first."""


class MetadataError(Exception):
    """The metadata store could not be read or written; the message names the cause only."""


class MetadataNotFoundError(MetadataError):
    """No row with that id for that tenant."""


class MetadataStaleError(MetadataError):
    """``expected_version`` did not match the row's ``updated_at`` (optimistic concurrency)."""


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND: Final = timedelta(milliseconds=1)


def updated_at_version(at: datetime | None) -> int:
    """The optimistic-concurrency version of a row that has ``updated_at`` but no version
    column (``systems``, ``entities``): whole milliseconds since the epoch, UTC, computed with
    integer arithmetic so Python and ``FLOOR(EXTRACT(EPOCH FROM updated_at) * 1000)`` in
    PostgreSQL agree to the millisecond. ``None`` (a row never written) is version 0. Two writes
    inside one millisecond share a version; a version column (a later migration) removes that."""
    if at is None:
        return 0
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return (aware.astimezone(UTC) - _EPOCH) // _MILLISECOND


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    """One ``templates`` row; ``count`` is an increment on upsert."""

    system_id: str
    template_id: str
    template_text: str
    kind: str
    first_seen: datetime
    last_seen: datetime
    count: int
    display_name: str | None = None
    event_type_label: str | None = None


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """One ``fields`` row. The edge-owned columns come first; the rest are read back."""

    field_ref: str
    system_id: str
    template_id: str
    path: str
    field_class: str
    policy: str
    pinned: bool
    forms: tuple[str, ...]
    count: int
    shapes: Shapes
    first_seen: datetime
    last_seen: datetime | None = None
    distinct_estimate: int = 0
    null_rate: float = 0.0
    quality: float | None = None
    is_candidate: bool = False
    label: str | None = None
    suggested_label: str | None = None


@dataclass(frozen=True, slots=True)
class FieldProfileUpdate:
    """The profiler's columns of one field (ADR 0056)."""

    field_ref: str
    count: int
    distinct_estimate: int
    null_rate: float
    shapes: Shapes
    quality: float | None
    is_candidate: bool
    last_seen: datetime | None = None

    @property
    def system_id(self) -> str:
        return self.field_ref.split("/", 2)[0]

    @property
    def template_id(self) -> str:
        parts = self.field_ref.split("/", 2)
        return parts[1] if len(parts) > 1 else ""

    @property
    def path(self) -> str:
        parts = self.field_ref.split("/", 2)
        return parts[2] if len(parts) > 2 else ""


@dataclass(frozen=True, slots=True)
class SystemRecord:
    id: str
    name: str
    description: str = ""
    owner_group: str | None = None
    criticality: str = "medium"
    updated_at: datetime | None = None
    """When the row last changed; :func:`updated_at_version` derives the console's ETag from it."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One ``sources`` row with the inventory columns of ADR 0050 and its latest heartbeat."""

    id: str
    system_id: str
    type: str
    enabled: bool = True
    config: Mapping[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    backfill_days: int | None = None
    schedule: str | None = None
    last_test: str | None = None
    last_test_at: datetime | None = None
    edge_id: str | None = None
    status: str = "unknown"
    health: SourceHealthRecord | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InventoryResult:
    systems: int
    sources: int
    disabled: int
    pins: int
    fields_pinned: int


def pin_matches(pattern: str, field_ref: str) -> bool:
    """``system/template/path`` or ``system/*/path`` against a ``field_ref``."""
    want = pattern.split("/", 2)
    have = field_ref.split("/", 2)
    if len(want) != PIN_SEGMENTS or len(have) != PIN_SEGMENTS:
        return False
    return want[0] == have[0] and want[2] == have[2] and want[1] in ("*", have[1])


def _apply_pins(record: FieldRecord, pins: Iterable[InventoryFieldPolicy]) -> FieldRecord:
    for pin in pins:
        if pin_matches(pin.field, record.field_ref):
            forms = tuple(pin.forms) if pin.forms else record.forms
            record = replace(
                record, field_class=pin.field_class, policy=pin.policy, pinned=True, forms=forms
            )
    return record


def _page(limit: int) -> int:
    return max(1, min(int(limit), MAX_PAGE))


@runtime_checkable
class MetadataStore(Protocol):
    def upsert_templates(
        self,
        tenant_id: str,
        records: Iterable[TemplateRecord],
        *,
        connection: Connection | None = None,
    ) -> int:
        """Insert or update templates, adding ``count``; on ``connection`` when given (the ledger's
        transaction) so the increment and the batch's ``done`` mark commit together."""
        ...

    def upsert_fields(self, tenant_id: str, records: Iterable[FieldRecord]) -> int:
        """The edge-owned columns, with the kept pins applied; never the profiler's."""
        ...

    def update_field_profiles(self, tenant_id: str, profiles: Iterable[FieldProfileUpdate]) -> int:
        """The profiler-owned columns; a field unknown so far is inserted as a tokenized
        identifier."""
        ...

    def replace_inventory(self, inventory: EdgeInventory, received_at: datetime) -> InventoryResult:
        """Systems, then sources, the payload, and the pins over the matching fields."""
        ...

    def list_systems(self, tenant_id: str) -> list[SystemRecord]: ...

    def rename_system(
        self,
        tenant_id: str,
        system_id: str,
        *,
        name: str | None,
        description: str | None,
        expected_version: int,
        now: datetime,
    ) -> SystemRecord:
        """The console's ``PATCH /systems/{id}`` (spec 12): set the name and/or description
        when :func:`updated_at_version` of the stored row equals ``expected_version``, stamping
        ``updated_at = now``. Raises :class:`MetadataNotFoundError` or
        :class:`MetadataStaleError`. The edge's next inventory overwrites the name (ADR 0050)."""
        ...

    def list_sources(self, tenant_id: str) -> list[SourceRecord]: ...

    def get_source(self, tenant_id: str, source_id: str) -> SourceRecord | None: ...

    def list_fields(
        self,
        tenant_id: str,
        *,
        system_id: str | None = None,
        candidates_only: bool = False,
        after: str | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[FieldRecord]:
        """Fields ordered by ``field_ref``; ``after`` is the last ``field_ref`` of the previous
        page."""
        ...

    def get_field(self, tenant_id: str, field_ref: str) -> FieldRecord | None: ...

    def list_templates(
        self, tenant_id: str, system_id: str | None = None
    ) -> list[TemplateRecord]: ...

    def ping(self) -> bool: ...


# --- in memory -----------------------------------------------------------------------------------


class InMemoryMetadataStore:
    """The reference semantics and the test double (module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._templates: dict[tuple[str, str, str], TemplateRecord] = {}
        self._fields: dict[tuple[str, str], FieldRecord] = {}
        self._systems: dict[tuple[str, str], SystemRecord] = {}
        self._sources: dict[tuple[str, str], SourceRecord] = {}
        self._pins: dict[tuple[str, str], list[InventoryFieldPolicy]] = {}
        self._inventories: dict[tuple[str, str], dict[str, Any]] = {}
        self.health: dict[tuple[str, str], SourceHealthRecord] = {}
        """Heartbeats joined into :meth:`list_sources`; tests fill it."""

    @property
    def inventories(self) -> dict[tuple[str, str], dict[str, Any]]:
        with self._lock:
            return dict(self._inventories)

    def _tenant_pins(self, tenant_id: str) -> list[InventoryFieldPolicy]:
        return [
            pin
            for (tenant, _edge), pins in self._pins.items()
            if tenant == tenant_id
            for pin in pins
        ]

    def upsert_templates(
        self,
        tenant_id: str,
        records: Iterable[TemplateRecord],
        *,
        connection: Connection | None = None,
    ) -> int:
        del connection
        written = 0
        with self._lock:
            for record in records:
                key = (tenant_id, record.system_id, record.template_id)
                current = self._templates.get(key)
                if current is None:
                    self._templates[key] = record
                else:
                    self._templates[key] = replace(
                        record,
                        count=current.count + record.count,
                        first_seen=min(current.first_seen, record.first_seen),
                        last_seen=max(current.last_seen, record.last_seen),
                        display_name=current.display_name or record.display_name,
                        event_type_label=current.event_type_label or record.event_type_label,
                    )
                written += 1
        return written

    def upsert_fields(self, tenant_id: str, records: Iterable[FieldRecord]) -> int:
        written = 0
        with self._lock:
            pins = self._tenant_pins(tenant_id)
            for record in records:
                pinned = _apply_pins(record, pins)
                key = (tenant_id, record.field_ref)
                current = self._fields.get(key)
                if current is None:
                    self._fields[key] = pinned
                else:
                    last_seen = max(
                        (at for at in (current.last_seen, pinned.last_seen) if at is not None),
                        default=None,
                    )
                    self._fields[key] = replace(
                        current,
                        field_class=pinned.field_class,
                        policy=pinned.policy,
                        pinned=pinned.pinned,
                        forms=pinned.forms,
                        count=pinned.count,
                        shapes=pinned.shapes,
                        first_seen=min(current.first_seen, pinned.first_seen),
                        last_seen=last_seen,
                    )
                written += 1
        return written

    def update_field_profiles(self, tenant_id: str, profiles: Iterable[FieldProfileUpdate]) -> int:
        written = 0
        with self._lock:
            pins = self._tenant_pins(tenant_id)
            for profile in profiles:
                key = (tenant_id, profile.field_ref)
                current = self._fields.get(key)
                if current is None:
                    seen = profile.last_seen
                    if seen is None:
                        continue
                    current = _apply_pins(
                        FieldRecord(
                            field_ref=profile.field_ref,
                            system_id=profile.system_id,
                            template_id=profile.template_id,
                            path=profile.path,
                            field_class="identifier",
                            policy="tokenize",
                            pinned=False,
                            forms=(),
                            count=0,
                            shapes=(),
                            first_seen=seen,
                        ),
                        pins,
                    )
                self._fields[key] = replace(
                    current,
                    count=profile.count,
                    distinct_estimate=profile.distinct_estimate,
                    null_rate=profile.null_rate,
                    shapes=profile.shapes,
                    quality=profile.quality,
                    is_candidate=profile.is_candidate,
                    last_seen=profile.last_seen or current.last_seen,
                )
                written += 1
        return written

    def replace_inventory(self, inventory: EdgeInventory, received_at: datetime) -> InventoryResult:
        tenant = inventory.tenant_id
        with self._lock:
            for system in inventory.systems:
                self._systems[tenant, system.id] = SystemRecord(
                    id=system.id,
                    name=system.name,
                    owner_group=system.owner_group or None,
                    criticality=system.criticality,
                    updated_at=received_at,
                )
            listed = {source.id for source in inventory.sources}
            disabled = 0
            for key, current in list(self._sources.items()):
                if (
                    key[0] == tenant
                    and current.edge_id == inventory.edge_id
                    and key[1] not in listed
                ):
                    if current.enabled:
                        disabled += 1
                    self._sources[key] = replace(current, enabled=False, updated_at=received_at)
            for source in inventory.sources:
                previous = self._sources.get((tenant, source.id))
                self._sources[tenant, source.id] = SourceRecord(
                    id=source.id,
                    system_id=source.system_id,
                    type=source.type,
                    enabled=source.enabled,
                    config=dict(source.config),
                    secret_ref=source.secret_ref,
                    backfill_days=source.backfill_days,
                    schedule=source.schedule,
                    last_test=source.last_test,
                    last_test_at=source.last_test_at,
                    edge_id=inventory.edge_id,
                    status=previous.status if previous is not None else "unknown",
                    updated_at=received_at,
                )
            self._pins[tenant, inventory.edge_id] = list(inventory.field_policies)
            self._inventories[tenant, inventory.edge_id] = {
                "sources_file_sha256": inventory.sources_file_sha256,
                "sent_at": inventory.sent_at,
                "received_at": received_at,
                "payload": inventory.model_dump(mode="json"),
            }
            pinned = 0
            for key, record in list(self._fields.items()):
                if key[0] != tenant:
                    continue
                updated = _apply_pins(record, inventory.field_policies)
                if updated != record:
                    self._fields[key] = updated
                    pinned += 1
        return InventoryResult(
            systems=len(inventory.systems),
            sources=len(inventory.sources),
            disabled=disabled,
            pins=len(inventory.field_policies),
            fields_pinned=pinned,
        )

    def list_systems(self, tenant_id: str) -> list[SystemRecord]:
        with self._lock:
            return sorted(
                (record for (tenant, _id), record in self._systems.items() if tenant == tenant_id),
                key=lambda record: record.id,
            )

    def rename_system(
        self,
        tenant_id: str,
        system_id: str,
        *,
        name: str | None,
        description: str | None,
        expected_version: int,
        now: datetime,
    ) -> SystemRecord:
        with self._lock:
            current = self._systems.get((tenant_id, system_id))
            if current is None:
                msg = "system not found"
                raise MetadataNotFoundError(msg)
            if updated_at_version(current.updated_at) != expected_version:
                msg = "system changed"
                raise MetadataStaleError(msg)
            updated = replace(
                current,
                name=name if name is not None else current.name,
                description=description if description is not None else current.description,
                updated_at=now,
            )
            self._systems[(tenant_id, system_id)] = updated
            return updated

    def _with_health(self, tenant_id: str, record: SourceRecord) -> SourceRecord:
        return replace(record, health=self.health.get((tenant_id, record.id)))

    def list_sources(self, tenant_id: str) -> list[SourceRecord]:
        with self._lock:
            rows = sorted(
                (record for (tenant, _id), record in self._sources.items() if tenant == tenant_id),
                key=lambda record: record.id,
            )
            return [self._with_health(tenant_id, record) for record in rows]

    def get_source(self, tenant_id: str, source_id: str) -> SourceRecord | None:
        with self._lock:
            record = self._sources.get((tenant_id, source_id))
            return self._with_health(tenant_id, record) if record is not None else None

    def list_fields(
        self,
        tenant_id: str,
        *,
        system_id: str | None = None,
        candidates_only: bool = False,
        after: str | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[FieldRecord]:
        with self._lock:
            rows = sorted(
                (
                    record
                    for (tenant, ref), record in self._fields.items()
                    if tenant == tenant_id
                    and (system_id is None or record.system_id == system_id)
                    and (not candidates_only or record.is_candidate)
                    and (after is None or ref > after)
                ),
                key=lambda record: record.field_ref,
            )
        return rows[: _page(limit)]

    def get_field(self, tenant_id: str, field_ref: str) -> FieldRecord | None:
        with self._lock:
            return self._fields.get((tenant_id, field_ref))

    def list_templates(self, tenant_id: str, system_id: str | None = None) -> list[TemplateRecord]:
        with self._lock:
            return sorted(
                (
                    record
                    for (tenant, system, _id), record in self._templates.items()
                    if tenant == tenant_id and (system_id is None or system == system_id)
                ),
                key=lambda record: (record.system_id, record.template_id),
            )

    def ping(self) -> bool:
        return True


# --- PostgreSQL ----------------------------------------------------------------------------------

_UPSERT_TEMPLATE = sqlalchemy.text(
    "INSERT INTO templates (tenant_id, system_id, id, template_text, kind, display_name, "
    "event_type_label, first_seen, last_seen, count, created_at, updated_at) "
    "VALUES (:tenant_id, :system_id, :id, :template_text, :kind, :display_name, "
    ":event_type_label, :first_seen, :last_seen, :count, :now, :now) "
    "ON CONFLICT (tenant_id, system_id, id) DO UPDATE SET "
    "template_text = EXCLUDED.template_text, kind = EXCLUDED.kind, "
    "display_name = COALESCE(templates.display_name, EXCLUDED.display_name), "
    "event_type_label = COALESCE(templates.event_type_label, EXCLUDED.event_type_label), "
    "first_seen = LEAST(templates.first_seen, EXCLUDED.first_seen), "
    "last_seen = GREATEST(templates.last_seen, EXCLUDED.last_seen), "
    "count = templates.count + EXCLUDED.count, updated_at = EXCLUDED.updated_at"
)
_UPSERT_FIELD = sqlalchemy.text(
    "INSERT INTO fields (tenant_id, id, system_id, template_id, path, class, policy, pinned, "
    "forms, count, shapes_json, first_seen, last_seen, label, suggested_label, created_at, "
    "updated_at) "
    "VALUES (:tenant_id, :id, :system_id, :template_id, :path, :class, :policy, :pinned, "
    "CAST(:forms AS jsonb), :count, CAST(:shapes AS jsonb), :first_seen, :last_seen, :label, "
    ":suggested_label, :now, :now) "
    "ON CONFLICT (tenant_id, id) DO UPDATE SET class = EXCLUDED.class, "
    "policy = EXCLUDED.policy, pinned = EXCLUDED.pinned, forms = EXCLUDED.forms, "
    "count = EXCLUDED.count, shapes_json = EXCLUDED.shapes_json, "
    "first_seen = LEAST(fields.first_seen, EXCLUDED.first_seen), "
    "last_seen = GREATEST(fields.last_seen, EXCLUDED.last_seen), "
    "label = COALESCE(fields.label, EXCLUDED.label), "
    "suggested_label = COALESCE(EXCLUDED.suggested_label, fields.suggested_label), "
    "updated_at = EXCLUDED.updated_at"
)
_UPSERT_PROFILE = sqlalchemy.text(
    "INSERT INTO fields (tenant_id, id, system_id, template_id, path, class, policy, pinned, "
    "forms, count, distinct_estimate, null_rate, shapes_json, quality, is_candidate, "
    "first_seen, last_seen, created_at, updated_at) "
    "VALUES (:tenant_id, :id, :system_id, :template_id, :path, :class, :policy, :pinned, "
    "CAST(:forms AS jsonb), :count, :distinct_estimate, :null_rate, CAST(:shapes AS jsonb), "
    ":quality, :is_candidate, :first_seen, :last_seen, :now, :now) "
    "ON CONFLICT (tenant_id, id) DO UPDATE SET count = EXCLUDED.count, "
    "distinct_estimate = EXCLUDED.distinct_estimate, null_rate = EXCLUDED.null_rate, "
    "shapes_json = EXCLUDED.shapes_json, quality = EXCLUDED.quality, "
    "is_candidate = EXCLUDED.is_candidate, "
    "last_seen = GREATEST(fields.last_seen, EXCLUDED.last_seen), updated_at = EXCLUDED.updated_at"
)
_UPSERT_SYSTEM = sqlalchemy.text(
    "INSERT INTO systems (id, tenant_id, name, owner_group, criticality, created_at, updated_at) "
    "VALUES (:id, :tenant_id, :name, :owner_group, :criticality, :now, :now) "
    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, owner_group = EXCLUDED.owner_group, "
    "criticality = EXCLUDED.criticality, updated_at = EXCLUDED.updated_at "
    "WHERE systems.tenant_id = EXCLUDED.tenant_id"
)
_UPSERT_SOURCE = sqlalchemy.text(
    "INSERT INTO sources (id, tenant_id, system_id, type, config_json, secret_ref, enabled, "
    "backfill_days, schedule, last_test, last_test_at, edge_id, created_at, updated_at) "
    "VALUES (:id, :tenant_id, :system_id, :type, CAST(:config AS jsonb), :secret_ref, :enabled, "
    ":backfill_days, :schedule, :last_test, :last_test_at, :edge_id, :now, :now) "
    "ON CONFLICT (id) DO UPDATE SET system_id = EXCLUDED.system_id, type = EXCLUDED.type, "
    "config_json = EXCLUDED.config_json, secret_ref = EXCLUDED.secret_ref, "
    "enabled = EXCLUDED.enabled, backfill_days = EXCLUDED.backfill_days, "
    "schedule = EXCLUDED.schedule, last_test = EXCLUDED.last_test, "
    "last_test_at = EXCLUDED.last_test_at, edge_id = EXCLUDED.edge_id, "
    "updated_at = EXCLUDED.updated_at WHERE sources.tenant_id = EXCLUDED.tenant_id"
)
_DISABLE_MISSING = sqlalchemy.text(
    "UPDATE sources SET enabled = false, updated_at = :now WHERE tenant_id = :tenant_id "
    "AND edge_id = :edge_id AND enabled AND NOT (id = ANY(:ids))"
)
_UPSERT_INVENTORY = sqlalchemy.text(
    "INSERT INTO edge_inventories (tenant_id, edge_id, sources_file_sha256, sent_at, received_at, "
    "payload, created_at, updated_at) VALUES (:tenant_id, :edge_id, :sha256, :sent_at, "
    ":received_at, CAST(:payload AS jsonb), :received_at, :received_at) "
    "ON CONFLICT (tenant_id, edge_id) DO UPDATE SET "
    "sources_file_sha256 = EXCLUDED.sources_file_sha256, sent_at = EXCLUDED.sent_at, "
    "received_at = EXCLUDED.received_at, payload = EXCLUDED.payload, "
    "updated_at = EXCLUDED.updated_at"
)
_PIN_FIELDS = sqlalchemy.text(
    "UPDATE fields SET class = :class, policy = :policy, pinned = true, "
    "forms = CASE WHEN :has_forms THEN CAST(:forms AS jsonb) ELSE forms END, updated_at = :now "
    "WHERE tenant_id = :tenant_id AND system_id = :system_id AND path = :path "
    "AND (:any_template OR template_id = :template_id)"
)
_PINS = sqlalchemy.text(
    "SELECT payload -> 'field_policies' AS pins FROM edge_inventories WHERE tenant_id = :tenant_id"
)
_SYSTEMS = sqlalchemy.text(
    "SELECT id, name, description, owner_group, criticality, updated_at FROM systems "
    "WHERE tenant_id = :tenant_id ORDER BY id"
)
_SYSTEM = sqlalchemy.text(
    "SELECT id, name, description, owner_group, criticality, updated_at FROM systems "
    "WHERE tenant_id = :tenant_id AND id = :id"
)
_RENAME_SYSTEM = sqlalchemy.text(
    "UPDATE systems SET name = COALESCE(:name, name), description = COALESCE(:description, "
    "description), updated_at = :now WHERE tenant_id = :tenant_id AND id = :id "
    "AND FLOOR(EXTRACT(EPOCH FROM updated_at) * 1000) = :expected_version RETURNING id, name, "
    "description, owner_group, criticality, updated_at"
)
_SOURCES = sqlalchemy.text(
    "SELECT s.id, s.system_id, s.type, s.enabled, s.config_json, s.secret_ref, s.backfill_days, "
    "s.schedule, s.last_test, s.last_test_at, s.edge_id, s.status, s.updated_at, "
    "h.status AS h_status, h.last_success_at AS h_last_success_at, "
    "h.lag_seconds AS h_lag_seconds, h.error_count AS h_error_count, "
    "h.buffer_depth AS h_buffer_depth, h.oldest_buffered_at AS h_oldest_buffered_at, "
    "h.message AS h_message, h.received_at AS h_received_at, h.sent_at AS h_sent_at "
    "FROM sources AS s LEFT JOIN source_health AS h ON h.tenant_id = s.tenant_id "
    "AND h.source_id = s.id WHERE s.tenant_id = :tenant_id ORDER BY s.id"
)
_SOURCE = sqlalchemy.text(
    "SELECT s.id, s.system_id, s.type, s.enabled, s.config_json, s.secret_ref, s.backfill_days, "
    "s.schedule, s.last_test, s.last_test_at, s.edge_id, s.status, s.updated_at, "
    "h.status AS h_status, h.last_success_at AS h_last_success_at, "
    "h.lag_seconds AS h_lag_seconds, h.error_count AS h_error_count, "
    "h.buffer_depth AS h_buffer_depth, h.oldest_buffered_at AS h_oldest_buffered_at, "
    "h.message AS h_message, h.received_at AS h_received_at, h.sent_at AS h_sent_at "
    "FROM sources AS s LEFT JOIN source_health AS h ON h.tenant_id = s.tenant_id "
    "AND h.source_id = s.id WHERE s.tenant_id = :tenant_id AND s.id = :id"
)
_FIELDS = sqlalchemy.text(
    "SELECT id, system_id, template_id, path, class, policy, pinned, forms, count, shapes_json, "
    "first_seen, last_seen, distinct_estimate, null_rate, quality, is_candidate, label, "
    "suggested_label FROM fields WHERE tenant_id = :tenant_id AND (:any_system "
    "OR system_id = :system_id) AND (:all_fields OR is_candidate) AND (:from_start "
    "OR id > :after) ORDER BY id LIMIT :limit"
)
_FIELD = sqlalchemy.text(
    "SELECT id, system_id, template_id, path, class, policy, pinned, forms, count, shapes_json, "
    "first_seen, last_seen, distinct_estimate, null_rate, quality, is_candidate, label, "
    "suggested_label FROM fields WHERE tenant_id = :tenant_id AND id = :id"
)
_TEMPLATES = sqlalchemy.text(
    "SELECT system_id, id, template_text, kind, display_name, event_type_label, first_seen, "
    "last_seen, count FROM templates WHERE tenant_id = :tenant_id "
    "AND (:any_system OR system_id = :system_id) ORDER BY system_id, id"
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _shapes_json(shapes: Shapes) -> str:
    return _json([{"shape": shape, "share": share} for shape, share in shapes])


def _shapes_from(value: object) -> Shapes:
    if not isinstance(value, list):
        return ()
    out: list[tuple[str, float]] = []
    for item in value:
        if isinstance(item, dict) and "shape" in item:
            try:
                out.append((str(item["shape"]), float(item.get("share", 0.0))))
            except (TypeError, ValueError):
                continue
    return tuple(out)


def _strings_from(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _field_from(row: Row[Any]) -> FieldRecord:
    return FieldRecord(
        field_ref=str(row.id),
        system_id=str(row.system_id),
        template_id=str(row.template_id),
        path=str(row.path),
        field_class=str(row._mapping["class"]),
        policy=str(row.policy),
        pinned=bool(row.pinned),
        forms=_strings_from(row.forms),
        count=int(row._mapping["count"]),
        shapes=_shapes_from(row.shapes_json),
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        distinct_estimate=int(row.distinct_estimate),
        null_rate=float(row.null_rate),
        quality=float(row.quality) if row.quality is not None else None,
        is_candidate=bool(row.is_candidate),
        label=row.label,
        suggested_label=row.suggested_label,
    )


def _source_from(row: Row[Any]) -> SourceRecord:
    health = None
    if row.h_status is not None:
        health = SourceHealthRecord(
            tenant_id="",
            source_id=str(row.id),
            status=str(row.h_status),
            last_success_at=row.h_last_success_at,
            lag_seconds=float(row.h_lag_seconds),
            error_count=int(row.h_error_count),
            buffer_depth=int(row.h_buffer_depth),
            oldest_buffered_at=row.h_oldest_buffered_at,
            message=str(row.h_message),
            received_at=row.h_received_at,
            sent_at=row.h_sent_at if row.h_sent_at is not None else row.h_received_at,
        )
    config = row.config_json if isinstance(row.config_json, dict) else {}
    return SourceRecord(
        id=str(row.id),
        system_id=str(row.system_id),
        type=str(row.type),
        enabled=bool(row.enabled),
        config=dict(config),
        secret_ref=row.secret_ref,
        backfill_days=int(row.backfill_days) if row.backfill_days is not None else None,
        schedule=row.schedule,
        last_test=row.last_test,
        last_test_at=row.last_test_at,
        edge_id=row.edge_id,
        status=str(row.status),
        health=health,
        updated_at=row.updated_at,
    )


def _system_from(row: Row[Any]) -> SystemRecord:
    return SystemRecord(
        id=str(row.id),
        name=str(row.name),
        description=str(row.description),
        owner_group=row.owner_group,
        criticality=str(row.criticality),
        updated_at=row.updated_at,
    )


def _template_from(row: Row[Any]) -> TemplateRecord:
    return TemplateRecord(
        system_id=str(row.system_id),
        template_id=str(row.id),
        template_text=str(row.template_text),
        kind=str(row.kind),
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        count=int(row._mapping["count"]),
        display_name=row.display_name,
        event_type_label=row.event_type_label,
    )


def _pins_from(rows: Iterable[Row[Any]]) -> list[InventoryFieldPolicy]:
    from carto_schema.inventory import InventoryFieldPolicy  # noqa: PLC0415 - runtime only here

    pins: list[InventoryFieldPolicy] = []
    for row in rows:
        if not isinstance(row.pins, list):
            continue
        for item in row.pins:
            try:
                pins.append(InventoryFieldPolicy.model_validate(item))
            except ValueError:
                continue  # a payload row is untrusted; a malformed pin is skipped
    return pins


class PostgresMetadataStore:
    """The tables of migrations 0001 and 0004 on one engine (module docstring)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _now(connection: Connection) -> datetime:
        value = connection.execute(sqlalchemy.text("SELECT now()")).scalar_one()
        if not isinstance(value, datetime):
            msg = "the database clock did not return a timestamp"
            raise MetadataError(msg)
        return value

    def upsert_templates(
        self,
        tenant_id: str,
        records: Iterable[TemplateRecord],
        *,
        connection: Connection | None = None,
    ) -> int:
        if connection is not None:
            return self._upsert_templates(connection, tenant_id, records)
        try:
            with self._engine.begin() as owned:
                return self._upsert_templates(owned, tenant_id, records)
        except SQLAlchemyError as exc:
            msg = f"template upsert failed: {type(exc).__name__}"
            raise MetadataError(msg) from exc

    def _upsert_templates(
        self, connection: Connection, tenant_id: str, records: Iterable[TemplateRecord]
    ) -> int:
        now = self._now(connection)
        rows = [
            {
                "tenant_id": tenant_id,
                "system_id": record.system_id,
                "id": record.template_id,
                "template_text": record.template_text,
                "kind": record.kind,
                "display_name": record.display_name,
                "event_type_label": record.event_type_label,
                "first_seen": record.first_seen,
                "last_seen": record.last_seen,
                "count": record.count,
                "now": now,
            }
            for record in records
        ]
        if rows:
            connection.execute(_UPSERT_TEMPLATE, rows)
        return len(rows)

    def _pins(self, connection: Connection, tenant_id: str) -> list[InventoryFieldPolicy]:
        return _pins_from(connection.execute(_PINS, {"tenant_id": tenant_id}).all())

    def upsert_fields(self, tenant_id: str, records: Iterable[FieldRecord]) -> int:
        try:
            with self._engine.begin() as connection:
                now = self._now(connection)
                pins = self._pins(connection, tenant_id)
                rows = []
                for record in records:
                    pinned = _apply_pins(record, pins)
                    rows.append(
                        {
                            "tenant_id": tenant_id,
                            "id": pinned.field_ref,
                            "system_id": pinned.system_id,
                            "template_id": pinned.template_id,
                            "path": pinned.path,
                            "class": pinned.field_class,
                            "policy": pinned.policy,
                            "pinned": pinned.pinned,
                            "forms": _json(list(pinned.forms)),
                            "count": pinned.count,
                            "shapes": _shapes_json(pinned.shapes),
                            "first_seen": pinned.first_seen,
                            "last_seen": pinned.last_seen,
                            "label": pinned.label,
                            "suggested_label": pinned.suggested_label,
                            "now": now,
                        }
                    )
                if rows:
                    connection.execute(_UPSERT_FIELD, rows)
        except SQLAlchemyError as exc:
            msg = f"field upsert failed: {type(exc).__name__}"
            raise MetadataError(msg) from exc
        return len(rows)

    def update_field_profiles(self, tenant_id: str, profiles: Iterable[FieldProfileUpdate]) -> int:
        try:
            with self._engine.begin() as connection:
                now = self._now(connection)
                pins = self._pins(connection, tenant_id)
                rows = []
                for profile in profiles:
                    if profile.last_seen is None:
                        continue
                    default = _apply_pins(
                        FieldRecord(
                            field_ref=profile.field_ref,
                            system_id=profile.system_id,
                            template_id=profile.template_id,
                            path=profile.path,
                            field_class="identifier",
                            policy="tokenize",
                            pinned=False,
                            forms=(),
                            count=0,
                            shapes=(),
                            first_seen=profile.last_seen,
                        ),
                        pins,
                    )
                    rows.append(
                        {
                            "tenant_id": tenant_id,
                            "id": profile.field_ref,
                            "system_id": default.system_id,
                            "template_id": default.template_id,
                            "path": default.path,
                            "class": default.field_class,
                            "policy": default.policy,
                            "pinned": default.pinned,
                            "forms": _json(list(default.forms)),
                            "count": profile.count,
                            "distinct_estimate": profile.distinct_estimate,
                            "null_rate": profile.null_rate,
                            "shapes": _shapes_json(profile.shapes),
                            "quality": profile.quality,
                            "is_candidate": profile.is_candidate,
                            "first_seen": profile.last_seen,
                            "last_seen": profile.last_seen,
                            "now": now,
                        }
                    )
                if rows:
                    connection.execute(_UPSERT_PROFILE, rows)
        except SQLAlchemyError as exc:
            msg = f"field profile update failed: {type(exc).__name__}"
            raise MetadataError(msg) from exc
        return len(rows)

    def replace_inventory(self, inventory: EdgeInventory, received_at: datetime) -> InventoryResult:
        tenant = inventory.tenant_id
        try:
            with self._engine.begin() as connection:
                if inventory.systems:
                    connection.execute(
                        _UPSERT_SYSTEM,
                        [
                            {
                                "id": system.id,
                                "tenant_id": tenant,
                                "name": system.name,
                                "owner_group": system.owner_group or None,
                                "criticality": system.criticality,
                                "now": received_at,
                            }
                            for system in inventory.systems
                        ],
                    )
                disabled = connection.execute(
                    _DISABLE_MISSING,
                    {
                        "tenant_id": tenant,
                        "edge_id": inventory.edge_id,
                        "ids": [source.id for source in inventory.sources],
                        "now": received_at,
                    },
                ).rowcount
                if inventory.sources:
                    connection.execute(
                        _UPSERT_SOURCE,
                        [
                            {
                                "id": source.id,
                                "tenant_id": tenant,
                                "system_id": source.system_id,
                                "type": source.type,
                                "config": _json(source.config),
                                "secret_ref": source.secret_ref,
                                "enabled": source.enabled,
                                "backfill_days": source.backfill_days,
                                "schedule": source.schedule,
                                "last_test": source.last_test,
                                "last_test_at": source.last_test_at,
                                "edge_id": inventory.edge_id,
                                "now": received_at,
                            }
                            for source in inventory.sources
                        ],
                    )
                connection.execute(
                    _UPSERT_INVENTORY,
                    {
                        "tenant_id": tenant,
                        "edge_id": inventory.edge_id,
                        "sha256": inventory.sources_file_sha256,
                        "sent_at": inventory.sent_at,
                        "received_at": received_at,
                        "payload": _json(inventory.model_dump(mode="json")),
                    },
                )
                pinned = 0
                for pin in inventory.field_policies:
                    parts = pin.field.split("/", 2)
                    if len(parts) != PIN_SEGMENTS:
                        continue
                    pinned += connection.execute(
                        _PIN_FIELDS,
                        {
                            "class": pin.field_class,
                            "policy": pin.policy,
                            "has_forms": bool(pin.forms),
                            "forms": _json(list(pin.forms)),
                            "now": received_at,
                            "tenant_id": tenant,
                            "system_id": parts[0],
                            "path": parts[2],
                            "any_template": parts[1] == "*",
                            "template_id": parts[1],
                        },
                    ).rowcount
        except SQLAlchemyError as exc:
            msg = f"inventory replace failed: {type(exc).__name__}"
            raise MetadataError(msg) from exc
        return InventoryResult(
            systems=len(inventory.systems),
            sources=len(inventory.sources),
            disabled=int(disabled),
            pins=len(inventory.field_policies),
            fields_pinned=pinned,
        )

    def _rows(self, statement: sqlalchemy.TextClause, parameters: dict[str, Any], what: str) -> Any:
        try:
            with self._engine.connect() as connection:
                return connection.execute(statement, parameters).all()
        except SQLAlchemyError as exc:
            msg = f"{what} lookup failed: {type(exc).__name__}"
            raise MetadataError(msg) from exc

    def list_systems(self, tenant_id: str) -> list[SystemRecord]:
        return [
            _system_from(row) for row in self._rows(_SYSTEMS, {"tenant_id": tenant_id}, "systems")
        ]

    def rename_system(
        self,
        tenant_id: str,
        system_id: str,
        *,
        name: str | None,
        description: str | None,
        expected_version: int,
        now: datetime,
    ) -> SystemRecord:
        parameters = {
            "tenant_id": tenant_id,
            "id": system_id,
            "name": name,
            "description": description,
            "expected_version": expected_version,
            "now": now,
        }
        try:
            with self._engine.begin() as connection:
                row = connection.execute(_RENAME_SYSTEM, parameters).first()
                if row is None:
                    exists = connection.execute(
                        _SYSTEM, {"tenant_id": tenant_id, "id": system_id}
                    ).first()
                    if exists is None:
                        msg = "system not found"
                        raise MetadataNotFoundError(msg)
                    msg = "system changed"
                    raise MetadataStaleError(msg)
        except SQLAlchemyError as exc:
            msg = f"system rename failed: {type(exc).__name__}"
            raise MetadataError(msg) from exc
        return _system_from(row)

    def list_sources(self, tenant_id: str) -> list[SourceRecord]:
        rows = self._rows(_SOURCES, {"tenant_id": tenant_id}, "sources")
        return [replace(_source_from(row), health=_tenant_health(row, tenant_id)) for row in rows]

    def get_source(self, tenant_id: str, source_id: str) -> SourceRecord | None:
        rows = self._rows(_SOURCE, {"tenant_id": tenant_id, "id": source_id}, "source")
        if not rows:
            return None
        return replace(_source_from(rows[0]), health=_tenant_health(rows[0], tenant_id))

    def list_fields(
        self,
        tenant_id: str,
        *,
        system_id: str | None = None,
        candidates_only: bool = False,
        after: str | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[FieldRecord]:
        rows = self._rows(
            _FIELDS,
            {
                "tenant_id": tenant_id,
                "any_system": system_id is None,
                "system_id": system_id or "",
                "all_fields": not candidates_only,
                "from_start": after is None,
                "after": after or "",
                "limit": _page(limit),
            },
            "fields",
        )
        return [_field_from(row) for row in rows]

    def get_field(self, tenant_id: str, field_ref: str) -> FieldRecord | None:
        rows = self._rows(_FIELD, {"tenant_id": tenant_id, "id": field_ref}, "field")
        return _field_from(rows[0]) if rows else None

    def list_templates(self, tenant_id: str, system_id: str | None = None) -> list[TemplateRecord]:
        rows = self._rows(
            _TEMPLATES,
            {"tenant_id": tenant_id, "any_system": system_id is None, "system_id": system_id or ""},
            "templates",
        )
        return [_template_from(row) for row in rows]

    def ping(self) -> bool:
        return ping_engine(self._engine)


def _tenant_health(row: Row[Any], tenant_id: str) -> SourceHealthRecord | None:
    health = _source_from(row).health
    return replace(health, tenant_id=tenant_id) if health is not None else None
