"""The console API's data ports over the engine's stores (spec 12; plan M2 packages A, C and E).

:class:`carto_core.api.ports` names what the routes need; packages A (:mod:`carto_core.metadata`)
and C (:mod:`carto_core.linker.repository`, :mod:`carto_core.linker.review`) own the tables. The
adapters here are the straight mappings between the two, sharing one SQLAlchemy engine with the
identity stores of :mod:`carto_core.api.stores_postgres`:

- :class:`LinkPortAdapter`: the review queue. Lists come from ``LinkRepository.list_links``
  sorted by ``score x affected_volume`` descending then ``id`` (spec 9.6); the cursor is the id
  of the last item, as the in-memory reference keeps it. Decisions go through
  :class:`~carto_core.linker.review.ReviewService`, the one place a status changes, so the state
  machine, the ``key_link_decisions`` row and the ``If-Match`` check under the repository's own
  guard are the linker's, not a second copy. The review service's own audit recorder is a no-op
  here on purpose: the route that called the port writes the audit row for every human decision
  (:func:`carto_core.api.context.record_audit`, with the request id, the socket address and the
  user agent the port never sees), and one decision must produce one row, not two. The
  linker-side transitions (re-proposal, staleness, auto-accept) run in the worker with the real
  recorder (:meth:`carto_core.linker.run.LinkPass.from_context`).
- :class:`EntityPortAdapter`: families as the repository stores them; ``link_ids`` come from the
  links assigned to the entity.
- :class:`SystemPortAdapter`, :class:`SourcePortAdapter`, :class:`FieldPortAdapter`: the inventory
  (ADR 0050) from the metadata store.

``systems`` and ``entities`` have no version column, so their ETag version is
:func:`carto_core.metadata.updated_at_version` of ``updated_at`` (whole milliseconds since the
epoch), derived the same way in list, get and patch. A link's version is the real column.

A review item is built from the stored row only: the ends from the ``field_ref`` and the
``fields`` table (label, top shape), the sentence and shapes from the evidence's ``extra``
(:class:`~carto_core.linker.run.LinkPass` writes them), the features from
:meth:`PairFeatures.as_evidence` plus the composite components when present (spec 9.5). Nothing
in a record is a token or a value (spec 2.3 invariant 2).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from carto_common.logging import get_logger
from carto_core.api.ports import (
    MAX_CURSOR_LEN,
    DecisionError,
    DecisionErrorKind,
    EntityFieldRecord,
    EntityPort,
    EntityRecord,
    FieldPort,
    FieldRecord,
    LinkEndRecord,
    LinkPort,
    LinkRecord,
    PortPage,
    SourcePort,
    SourceRecord,
    SystemPort,
    SystemRecord,
)
from carto_core.linker.repository import (
    EntityRepository,
    LinkRepository,
    NotFoundError,
    PostgresEntityRepository,
    PostgresLinkRepository,
)
from carto_core.linker.review import DecisionError as ReviewDecisionError
from carto_core.linker.review import DecisionKind, RequestContext, ReviewService
from carto_core.linker.sentence import evidence_sentence
from carto_core.metadata import (
    MAX_PAGE,
    MetadataNotFoundError,
    MetadataStaleError,
    MetadataStore,
    PostgresMetadataStore,
    updated_at_version,
)
from carto_schema.api import MAX_SENTENCE_LEN, LinkStatus, RejectReason

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from carto_core.audit import AuditEntry
    from carto_core.linker.model import KeyLink
    from carto_core.linker.repository import Entity
    from carto_core.metadata import FieldRecord as MetadataField
    from carto_core.settings import CoreSettings, LinkerSettings
    from carto_core.store.model import FieldForm

__all__ = [
    "MAX_SHAPES",
    "DataPorts",
    "EntityPortAdapter",
    "FieldPortAdapter",
    "LinkPortAdapter",
    "SourcePortAdapter",
    "SystemPortAdapter",
    "assemble_data_ports",
    "build_data_ports",
]

log = get_logger(component="api")

MAX_SHAPES: Final = 8
"""``FieldView.shapes`` holds at most this many ``(shape, share)`` pairs."""
MAX_FORMS: Final = 16
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_DECISION_KINDS: Final[dict[DecisionKind, DecisionErrorKind]] = {
    DecisionKind.NOT_FOUND: "not_found",
    DecisionKind.STALE_VERSION: "stale_version",
    DecisionKind.INVALID_TRANSITION: "invalid_transition",
    DecisionKind.REASON_REQUIRED: "reason_required",
}


@dataclass(frozen=True, slots=True)
class DataPorts:
    """The engine-side ports the identity stores are combined with
    (:func:`carto_core.api.server.build_ports`)."""

    links: LinkPort
    entities: EntityPort
    systems: SystemPort
    sources: SourcePort
    fields: FieldPort


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _page[T](
    ordered: Sequence[T], key: Callable[[T], str], cursor: str | None, limit: int
) -> PortPage[T]:
    """The in-memory reference's cursor rule: slice after the item whose key is ``cursor``; an
    unknown cursor yields an empty page; the next cursor is the key of the last item returned
    when more follow."""
    start = 0
    if cursor is not None:
        start = next((i + 1 for i, item in enumerate(ordered) if key(item) == cursor), len(ordered))
    items = tuple(ordered[start : start + limit])
    more = start + len(items) < len(ordered)
    return PortPage(items, key(items[-1]) if items and more else None)


def _split_ref(field_ref: str) -> tuple[str, str, str]:
    """``system_id/template_id/path``; a malformed ref keeps the whole string as the path so a
    bad row renders rather than hides the queue."""
    parts = field_ref.split("/", 2)
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    return (parts[0] if parts and parts[0] else "unknown", "unknown", field_ref)


class _RouteAudits:
    """The audit recorder of the API-side review service: a no-op, because the calling route
    records the decision with the request's own context (module docstring)."""

    __slots__ = ()

    def record(self, entry: AuditEntry) -> str:
        del entry
        return ""

    def latest_hash(self, tenant_id: str) -> str:
        del tenant_id
        return ""


class _AtClock:
    """A clock the review service reads: the ``at`` the route passed for the current decision
    (per thread, since routes run in a thread pool), else the adapter's clock."""

    __slots__ = ("_fallback", "_local")

    def __init__(self, fallback: Callable[[], datetime]) -> None:
        self._fallback = fallback
        self._local = threading.local()

    def __call__(self) -> datetime:
        at = getattr(self._local, "at", None)
        return at if isinstance(at, datetime) else self._fallback()

    def set(self, at: datetime | None) -> None:
        self._local.at = at


class _Lookup:
    """Per-call cache over the metadata store: a queue page names each field once or twice."""

    __slots__ = ("_fields", "_metadata", "_systems", "_tenant")

    def __init__(self, metadata: MetadataStore, tenant_id: str) -> None:
        self._metadata = metadata
        self._tenant = tenant_id
        self._fields: dict[str, MetadataField | None] = {}
        self._systems: dict[str, str] | None = None

    def field(self, field_ref: str) -> MetadataField | None:
        if field_ref not in self._fields:
            self._fields[field_ref] = self._metadata.get_field(self._tenant, field_ref)
        return self._fields[field_ref]

    def system_names(self) -> Mapping[str, str]:
        if self._systems is None:
            self._systems = {
                system.id: system.name for system in self._metadata.list_systems(self._tenant)
            }
        return self._systems


# ---------------------------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------------------------


class LinkPortAdapter:
    """``LinkPort`` over ``LinkRepository`` and ``ReviewService`` (module docstring)."""

    __slots__ = ("_at", "_clock", "_links", "_metadata", "_review")

    def __init__(
        self,
        links: LinkRepository,
        metadata: MetadataStore,
        settings: LinkerSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._links = links
        self._metadata = metadata
        self._clock = clock if clock is not None else _utc_now
        self._at = _AtClock(self._clock)
        self._review = ReviewService(links, _RouteAudits(), settings, clock=self._at)

    @property
    def review(self) -> ReviewService:
        return self._review

    def list(
        self, tenant_id: str, status: LinkStatus, cursor: str | None, limit: int
    ) -> PortPage[LinkRecord]:
        links = self._links.list_links(tenant_id, statuses=(status,))
        links.sort(key=lambda link: (-(link.score * link.affected_volume), link.id))
        page = _page(links, lambda link: link.id, cursor, limit)
        lookup = _Lookup(self._metadata, tenant_id)
        return PortPage(tuple(self._record(link, lookup) for link in page.items), page.next_cursor)

    def get(self, tenant_id: str, link_id: str) -> LinkRecord | None:
        link = self._links.get(tenant_id, link_id)
        if link is None:
            return None
        return self._record(link, _Lookup(self._metadata, tenant_id))

    def accept(
        self,
        tenant_id: str,
        link_id: str,
        *,
        actor: str,
        expected_version: int,
        reason: RejectReason | None,
        note: str,
        at: datetime,
    ) -> LinkRecord:
        del reason
        return self._decide(
            tenant_id,
            at,
            lambda: self._review.accept(
                tenant_id,
                link_id,
                actor=actor,
                expected_version=expected_version,
                note=note or None,
                context=RequestContext(),
            ),
        )

    def reject(
        self,
        tenant_id: str,
        link_id: str,
        *,
        actor: str,
        expected_version: int,
        reason: RejectReason | None,
        note: str,
        at: datetime,
    ) -> LinkRecord:
        return self._decide(
            tenant_id,
            at,
            lambda: self._review.reject(
                tenant_id,
                link_id,
                actor=actor,
                expected_version=expected_version,
                reason=reason,
                note=note or None,
                context=RequestContext(),
            ),
        )

    def mark_association(
        self,
        tenant_id: str,
        link_id: str,
        *,
        actor: str,
        expected_version: int,
        reason: RejectReason | None,
        note: str,
        at: datetime,
    ) -> LinkRecord:
        del reason
        return self._decide(
            tenant_id,
            at,
            lambda: self._review.mark_association(
                tenant_id,
                link_id,
                actor=actor,
                expected_version=expected_version,
                note=note or None,
                context=RequestContext(),
            ),
        )

    def ping(self) -> bool:
        return self._links.ping()

    # -----------------------------------------------------------------------------------------

    def _decide(self, tenant_id: str, at: datetime, action: Callable[[], KeyLink]) -> LinkRecord:
        self._at.set(at)
        try:
            stored = action()
        except ReviewDecisionError as exc:
            raise DecisionError(_DECISION_KINDS[exc.kind]) from None
        finally:
            self._at.set(None)
        return self._record(stored, _Lookup(self._metadata, tenant_id))

    def _record(self, link: KeyLink, lookup: _Lookup) -> LinkRecord:
        extra = link.extra
        evidence: dict[str, Any] = link.features.as_evidence()
        components = extra.get("components")
        if isinstance(components, list):  # composite links (spec 9.5): CompositeComponentView rows
            evidence["components"] = components
        for key in ("window_start", "window_end"):
            if isinstance(extra.get(key), str):
                evidence[key] = extra[key]
        sentence = extra.get("sentence")
        if not isinstance(sentence, str) or not sentence:
            sentence = evidence_sentence(link, system_names=lookup.system_names())
        first = link.first_proposed_at or link.updated_at or self._clock()
        return LinkRecord(
            id=link.id,
            a=_end(link.a, extra.get("shape_a"), lookup),
            b=_end(link.b, extra.get("shape_b"), lookup),
            link_type=link.link_type,
            role=link.role,
            direction=link.direction,
            score=link.score,
            status=link.status,
            affected_volume=link.affected_volume,
            sentence=sentence[:MAX_SENTENCE_LEN],
            evidence=evidence,
            version=link.version,
            first_proposed_at=first,
            updated_at=link.updated_at or first,
            entity_id=link.entity_id,
            reject_reason=link.reject_reason,
            decided_by=link.decided_by,
            decided_at=link.decided_at,
            ambiguous_merges=link.ambiguous_merges,
        )


def _end(end: FieldForm, shape: object, lookup: _Lookup) -> LinkEndRecord:
    system_id, template_id, path = _split_ref(end.field_ref)
    field = lookup.field(end.field_ref)
    top_shape = shape if isinstance(shape, str) and shape else None
    if top_shape is None and field is not None and field.shapes:
        top_shape = field.shapes[0][0]
    return LinkEndRecord(
        field_id=end.field_ref,
        system_id=system_id,
        template_id=template_id,
        path=path,
        form=end.form,
        label=field.label if field is not None else None,
        top_shape=top_shape,
    )


# ---------------------------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------------------------


class EntityPortAdapter:
    """``EntityPort`` over ``EntityRepository``; ``link_ids`` from the links assigned to the
    entity. The version is :func:`updated_at_version` of the row (module docstring).

    The repository offers one write, ``set_name``, which confirms the name it is given; a patch
    that carries only a description therefore confirms the current display name as well. The
    route checks ``If-Match`` and the adapter checks the version again before writing, but the
    write itself is not guarded by the version (``set_name`` takes none).
    """

    __slots__ = ("_entities", "_links")

    def __init__(self, entities: EntityRepository, links: LinkRepository) -> None:
        self._entities = entities
        self._links = links

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[EntityRecord]:
        entities = self._entities.list_entities(tenant_id)
        entities.sort(key=lambda entity: entity.id)
        page = _page(entities, lambda entity: entity.id, cursor, limit)
        assigned = self._assigned(tenant_id)
        return PortPage(
            tuple(_entity_record(entity, assigned.get(entity.id, ())) for entity in page.items),
            page.next_cursor,
        )

    def get(self, tenant_id: str, entity_id: str) -> EntityRecord | None:
        entity = self._entities.get(tenant_id, entity_id)
        if entity is None:
            return None
        return _entity_record(entity, self._assigned(tenant_id).get(entity_id, ()))

    def patch(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        name: str | None,
        description: str | None,
        expected_version: int,
        actor: str,
        at: datetime,
    ) -> EntityRecord:
        del actor  # the route audits the actor; the row keeps no reviewer column (spec 9.6)
        current = self._entities.get(tenant_id, entity_id)
        if current is None:
            raise DecisionError("not_found")
        if updated_at_version(current.updated_at) != expected_version:
            raise DecisionError("stale_version")
        try:
            stored = self._entities.set_name(
                tenant_id,
                entity_id,
                name=name if name is not None else (current.display_name or ""),
                description=description if description is not None else current.description,
                now=at,
            )
        except NotFoundError:
            raise DecisionError("not_found") from None
        return _entity_record(stored, self._assigned(tenant_id).get(entity_id, ()))

    def ping(self) -> bool:
        return self._entities.ping()

    def _assigned(self, tenant_id: str) -> dict[str, tuple[str, ...]]:
        by_entity: dict[str, list[str]] = {}
        for link in self._links.list_links(tenant_id):
            if link.entity_id is not None:
                by_entity.setdefault(link.entity_id, []).append(link.id)
        return {entity_id: tuple(sorted(ids)) for entity_id, ids in by_entity.items()}


def _entity_record(entity: Entity, link_ids: tuple[str, ...]) -> EntityRecord:
    fields = []
    for member in entity.members:
        system_id, _template_id, path = _split_ref(member.field_ref)
        fields.append(EntityFieldRecord(member.field_ref, member.form, system_id, path))
    updated_at = entity.updated_at or _EPOCH
    return EntityRecord(
        id=entity.id,
        name=entity.display_name or "",
        suggested_name=entity.suggested_name,
        name_confirmed=entity.name_confirmed,
        description=entity.description or "",
        fields=tuple(fields),
        link_ids=link_ids,
        system_count=len({row.system_id for row in fields}),
        version=updated_at_version(entity.updated_at),
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------------------------


class SystemPortAdapter:
    """``SystemPort`` over ``MetadataStore.list_systems`` and ``rename_system``; the version is
    :func:`updated_at_version` of the row, checked by the store inside the update."""

    __slots__ = ("_metadata",)

    def __init__(self, metadata: MetadataStore) -> None:
        self._metadata = metadata

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[SystemRecord]:
        counts = self._source_counts(tenant_id)
        rows = sorted(
            (
                _system_record(system, counts.get(system.id, 0))
                for system in self._metadata.list_systems(tenant_id)
            ),
            key=lambda row: row.id,
        )
        return _page(rows, lambda row: row.id, cursor, limit)

    def get(self, tenant_id: str, system_id: str) -> SystemRecord | None:
        for system in self._metadata.list_systems(tenant_id):
            if system.id == system_id:
                return _system_record(system, self._source_counts(tenant_id).get(system_id, 0))
        return None

    def patch(
        self,
        tenant_id: str,
        system_id: str,
        *,
        name: str | None,
        description: str | None,
        expected_version: int,
        actor: str,
        at: datetime,
    ) -> SystemRecord:
        del actor  # the route audits the actor
        try:
            stored = self._metadata.rename_system(
                tenant_id,
                system_id,
                name=name,
                description=description,
                expected_version=expected_version,
                now=at,
            )
        except MetadataNotFoundError:
            raise DecisionError("not_found") from None
        except MetadataStaleError:
            raise DecisionError("stale_version") from None
        return _system_record(stored, self._source_counts(tenant_id).get(system_id, 0))

    def ping(self) -> bool:
        return self._metadata.ping()

    def _source_counts(self, tenant_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in self._metadata.list_sources(tenant_id):
            counts[source.system_id] = counts.get(source.system_id, 0) + 1
        return counts


def _system_record(system: Any, source_count: int) -> SystemRecord:
    return SystemRecord(
        id=system.id,
        name=system.name,
        description=system.description,
        owner_group=system.owner_group,
        criticality=system.criticality,
        source_count=source_count,
        version=updated_at_version(system.updated_at),
        updated_at=system.updated_at or _EPOCH,
    )


class SourcePortAdapter:
    """``SourcePort`` over ``MetadataStore.list_sources`` and ``get_source`` (configuration
    without secrets and the latest heartbeat, ADR 0050)."""

    __slots__ = ("_metadata",)

    def __init__(self, metadata: MetadataStore) -> None:
        self._metadata = metadata

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[SourceRecord]:
        rows = sorted(
            (_source_record(source) for source in self._metadata.list_sources(tenant_id)),
            key=lambda row: row.id,
        )
        return _page(rows, lambda row: row.id, cursor, limit)

    def get(self, tenant_id: str, source_id: str) -> SourceRecord | None:
        source = self._metadata.get_source(tenant_id, source_id)
        return _source_record(source) if source is not None else None

    def ping(self) -> bool:
        return self._metadata.ping()


def _source_record(source: Any) -> SourceRecord:
    health = source.health
    updated_at = source.updated_at
    if updated_at is None:
        updated_at = health.received_at if health is not None else (source.last_test_at or _EPOCH)
    return SourceRecord(
        id=source.id,
        system_id=source.system_id,
        type=source.type,
        enabled=source.enabled,
        config=dict(source.config),
        secret_ref=source.secret_ref,
        backfill_days=source.backfill_days,
        schedule=source.schedule,
        last_test=source.last_test,
        updated_at=updated_at,
        health=health,
    )


class FieldPortAdapter:
    """``FieldPort`` over ``MetadataStore.list_fields`` (keyset pages by ``field_ref``) and
    ``get_field``. The cursor is the last ``field_ref`` of the page; a ``field_ref`` longer than
    :data:`~carto_core.api.ports.MAX_CURSOR_LEN` cannot be a cursor (a path near its 256-character
    limit inside a long system and template id), so the page is cut after the last item whose
    ref fits, and when none fits the listing ends there with a warning rather than skipping or
    repeating a row."""

    __slots__ = ("_metadata",)

    def __init__(self, metadata: MetadataStore) -> None:
        self._metadata = metadata

    def list(
        self, tenant_id: str, system_id: str | None, cursor: str | None, limit: int
    ) -> PortPage[FieldRecord]:
        wanted = max(1, min(limit, MAX_PAGE - 1))
        rows = self._metadata.list_fields(
            tenant_id, system_id=system_id, after=cursor, limit=wanted + 1
        )
        more = len(rows) > wanted
        items = rows[:wanted]
        if not more:
            return PortPage(tuple(_field_record(row) for row in items))
        while items and len(items[-1].field_ref) > MAX_CURSOR_LEN:
            items = items[:-1]
        if not items:
            log.warning(
                "field listing cut short: field_ref longer than a cursor", tenant_id=tenant_id
            )
            return PortPage(tuple(_field_record(row) for row in rows[:wanted]))
        return PortPage(tuple(_field_record(row) for row in items), items[-1].field_ref)

    def get(self, tenant_id: str, field_id: str) -> FieldRecord | None:
        row = self._metadata.get_field(tenant_id, field_id)
        return _field_record(row) if row is not None else None

    def ping(self) -> bool:
        return self._metadata.ping()


def _field_record(row: MetadataField) -> FieldRecord:
    return FieldRecord(
        id=row.field_ref,
        system_id=row.system_id,
        template_id=row.template_id,
        path=row.path,
        field_class=row.field_class,
        policy=row.policy,
        pinned=row.pinned,
        count=row.count,
        distinct_estimate=row.distinct_estimate,
        null_rate=row.null_rate,
        shapes=tuple(row.shapes[:MAX_SHAPES]),
        forms=tuple(row.forms[:MAX_FORMS]),
        quality=row.quality,
        label=row.label,
        suggested_label=row.suggested_label,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


# ---------------------------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------------------------


def assemble_data_ports(
    *,
    links: LinkRepository,
    entities: EntityRepository,
    metadata: MetadataStore,
    settings: LinkerSettings,
    clock: Callable[[], datetime] | None = None,
) -> DataPorts:
    """The adapters over any implementations of the three stores (tests pass the in-memory
    ones; :func:`build_data_ports` the PostgreSQL ones)."""
    return DataPorts(
        links=LinkPortAdapter(links, metadata, settings, clock=clock),
        entities=EntityPortAdapter(entities, links),
        systems=SystemPortAdapter(metadata),
        sources=SourcePortAdapter(metadata),
        fields=FieldPortAdapter(metadata),
    )


def build_data_ports(
    engine: Engine, settings: CoreSettings, *, clock: Callable[[], datetime] | None = None
) -> DataPorts:
    """The production ports of ``carto-core api``: every store on the one PostgreSQL engine the
    identity stores use (migration 0004)."""
    return assemble_data_ports(
        links=PostgresLinkRepository(engine),
        entities=PostgresEntityRepository(engine),
        metadata=PostgresMetadataStore(engine),
        settings=settings.linker,
        clock=clock,
    )
