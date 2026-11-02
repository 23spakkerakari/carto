"""Persistence of key links, their decisions and entity families (spec 7.3 ``key_links``,
``key_link_decisions``, ``entities``, ``entity_fields``; spec 9.6; ADR 0051).

Two Protocols, each with an in-memory implementation (the unit-test double and the reference
semantics) and a PostgreSQL one over SQLAlchemy Core text against migration 0004's columns:

- :class:`LinkRepository`: one row per ``(tenant_id, field_a, form_a, field_b, form_b)``.
  :meth:`~LinkRepository.upsert` writes the linker's view of a pair and bumps ``version`` on an
  existing row; :meth:`~LinkRepository.decide` is the only way a status changes after that: it
  updates ``key_links`` and inserts the ``key_link_decisions`` row in one transaction and
  refuses a stale ``expected_version`` (:class:`StaleVersionError`), which is what the API's
  ``If-Match`` relies on. Every write returns the stored row with its new ``version`` and
  ``updated_at``.
- :class:`EntityRepository`: entities with their member ends; the family builder replaces the
  whole set per tenant in one transaction and the API renames one entity.

``evidence_json`` holds :meth:`PairFeatures.as_evidence` plus an ``extra`` object (the sentence
and the shapes the review item shows). Nothing stored here is a token or a raw value; errors
raise :class:`RepositoryError` naming the driver error class only (spec 14.7).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.db.postgres import ping_engine
from carto_core.linker.model import (
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
    RejectReason,
)
from carto_core.store.model import FieldForm

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "Decision",
    "DecisionWrite",
    "Entity",
    "EntityRepository",
    "InMemoryEntityRepository",
    "InMemoryLinkRepository",
    "LinkRepository",
    "NotFoundError",
    "PostgresEntityRepository",
    "PostgresLinkRepository",
    "RepositoryError",
    "StaleVersionError",
    "features_from_evidence",
]


class RepositoryError(Exception):
    """A read or write failed. The message names the cause, never a row."""


class StaleVersionError(RepositoryError):
    """``expected_version`` did not match the stored row (optimistic concurrency)."""


class NotFoundError(RepositoryError):
    """No row with that id for that tenant."""


@dataclass(frozen=True, slots=True)
class Decision:
    """One ``key_link_decisions`` row: the state a link entered and who put it there."""

    tenant_id: str
    key_link_id: str
    version: int
    status: LinkStatus
    reject_reason: RejectReason | None
    decided_by: str
    decided_at: datetime
    note: str | None
    score_at_decision: float
    id: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionWrite:
    """What :meth:`LinkRepository.decide` applies to a link in one transaction."""

    status: LinkStatus
    decided_by: str
    decided_at: datetime
    score_at_decision: float
    role: LinkRole | None = None
    """A new role (mark as association); None keeps the stored one."""
    reject_reason: RejectReason | None = None
    note: str | None = None
    baseline_match_rate: float | None = None
    """Set on accept (ADR 0053); None keeps the stored value."""


@dataclass(frozen=True, slots=True)
class Entity:
    """One ``entities`` row with its ``entity_fields`` members."""

    id: str
    members: tuple[FieldForm, ...]
    name: str | None = None
    """The confirmed name (spec 9.6 "the reviewer confirms"); None until then."""
    suggested_name: str | None = None
    name_confirmed: bool = False
    description: str | None = None
    updated_at: datetime | None = None

    @property
    def display_name(self) -> str | None:
        return self.name or self.suggested_name


@runtime_checkable
class LinkRepository(Protocol):
    def get(self, tenant_id: str, link_id: str) -> KeyLink | None: ...

    def find_by_ends(self, tenant_id: str, a: FieldForm, b: FieldForm) -> KeyLink | None:
        """The row for the pair in the stored order ``(a, b)``."""
        ...

    def list_links(
        self, tenant_id: str, *, statuses: Collection[LinkStatus] | None = None
    ) -> list[KeyLink]:
        """Every link of the tenant (or those in ``statuses``), ordered by id."""
        ...

    def upsert(self, tenant_id: str, link: KeyLink, now: datetime) -> KeyLink:
        """Insert the link or overwrite the row with the same ends; ``version`` becomes 1 on
        insert and the stored version plus one on update. Returns the stored row."""
        ...

    def decide(
        self, tenant_id: str, link_id: str, *, expected_version: int, write: DecisionWrite
    ) -> KeyLink:
        """Apply ``write`` to the link and insert its decision row atomically. Raises
        :class:`NotFoundError` or :class:`StaleVersionError`."""
        ...

    def decisions(self, tenant_id: str, link_id: str) -> list[Decision]:
        """The decision rows of a link, oldest first."""
        ...

    def assign_entities(
        self, tenant_id: str, assignments: Mapping[str, str | None], now: datetime
    ) -> int:
        """Set ``entity_id`` per link id (None clears it); returns the rows changed. The
        version is left alone: the family is derived, not reviewed."""
        ...

    def ping(self) -> bool: ...


@runtime_checkable
class EntityRepository(Protocol):
    def list_entities(self, tenant_id: str) -> list[Entity]: ...

    def get(self, tenant_id: str, entity_id: str) -> Entity | None: ...

    def replace_all(self, tenant_id: str, entities: Sequence[Entity], now: datetime) -> None:
        """Make ``entities`` the tenant's whole set (rows, members) in one transaction."""
        ...

    def set_name(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        name: str,
        description: str | None,
        now: datetime,
    ) -> Entity:
        """Confirm a name (spec 9.6). Raises :class:`NotFoundError`."""
        ...

    def ping(self) -> bool: ...


# -- evidence encoding -------------------------------------------------------------------------


def _evidence_document(link: KeyLink) -> dict[str, Any]:
    return link.features.as_evidence() | {"extra": dict(link.extra)}


def features_from_evidence(evidence: Mapping[str, Any]) -> PairFeatures:
    """Rebuild :class:`PairFeatures` from a stored ``evidence_json`` document."""

    def _f(key: str) -> float | None:
        value = evidence.get(key)
        return None if value is None else float(value)

    return PairFeatures(
        n_a=int(evidence.get("n_a", 0)),
        n_b=int(evidence.get("n_b", 0)),
        n_match=int(evidence.get("n_match", 0)),
        c_ab=float(evidence.get("c_ab", 0.0)),
        c_ba=float(evidence.get("c_ba", 0.0)),
        lag_median=_f("lag_median_seconds"),
        lag_iqr=_f("lag_iqr_seconds"),
        lag_positive_share=_f("lag_positive_share"),
        fanout_ab_median=_f("fanout_ab_median"),
        fanout_ab_p99=_f("fanout_ab_p99"),
        fanout_ba_median=_f("fanout_ba_median"),
        fanout_ba_p99=_f("fanout_ba_p99"),
        concentration=_f("concentration"),
        chance_ratio=_f("chance_ratio"),
        domain_size=_f("domain_size"),
        pairing_consistency=_f("pairing_consistency"),
    )


# -- in-memory ---------------------------------------------------------------------------------


class InMemoryLinkRepository:
    """Reference semantics and test double. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._links: dict[tuple[str, str], KeyLink] = {}
        self._decisions: list[Decision] = []

    def get(self, tenant_id: str, link_id: str) -> KeyLink | None:
        with self._lock:
            return self._links.get((tenant_id, link_id))

    def find_by_ends(self, tenant_id: str, a: FieldForm, b: FieldForm) -> KeyLink | None:
        with self._lock:
            for (tenant, _id), link in self._links.items():
                if tenant == tenant_id and link.a == a and link.b == b:
                    return link
        return None

    def list_links(
        self, tenant_id: str, *, statuses: Collection[LinkStatus] | None = None
    ) -> list[KeyLink]:
        with self._lock:
            rows = [
                link
                for (tenant, _id), link in self._links.items()
                if tenant == tenant_id and (statuses is None or link.status in statuses)
            ]
        return sorted(rows, key=lambda link: link.id)

    def upsert(self, tenant_id: str, link: KeyLink, now: datetime) -> KeyLink:
        with self._lock:
            existing = self._links.get((tenant_id, link.id))
            if existing is None:
                for (tenant, _id), other in self._links.items():
                    if tenant == tenant_id and other.a == link.a and other.b == link.b:
                        existing = other
                        break
            if existing is None:
                stored = replace(link, version=1, updated_at=now)
            else:
                stored = replace(
                    link,
                    id=existing.id,
                    version=existing.version + 1,
                    updated_at=now,
                    first_proposed_at=existing.first_proposed_at or link.first_proposed_at,
                )
            self._links[(tenant_id, stored.id)] = stored
            return stored

    def decide(
        self, tenant_id: str, link_id: str, *, expected_version: int, write: DecisionWrite
    ) -> KeyLink:
        with self._lock:
            link = self._links.get((tenant_id, link_id))
            if link is None:
                msg = "link not found"
                raise NotFoundError(msg)
            if link.version != expected_version:
                msg = "link version changed"
                raise StaleVersionError(msg)
            stored = replace(
                link,
                status=write.status,
                role=write.role if write.role is not None else link.role,
                reject_reason=write.reject_reason,
                decided_by=write.decided_by,
                decided_at=write.decided_at,
                version=link.version + 1,
                updated_at=write.decided_at,
                baseline_match_rate=(
                    write.baseline_match_rate
                    if write.baseline_match_rate is not None
                    else link.baseline_match_rate
                ),
            )
            self._links[(tenant_id, link_id)] = stored
            self._decisions.append(
                Decision(
                    tenant_id=tenant_id,
                    key_link_id=link_id,
                    version=stored.version,
                    status=write.status,
                    reject_reason=write.reject_reason,
                    decided_by=write.decided_by,
                    decided_at=write.decided_at,
                    note=write.note,
                    score_at_decision=write.score_at_decision,
                    id=len(self._decisions) + 1,
                )
            )
            return stored

    def decisions(self, tenant_id: str, link_id: str) -> list[Decision]:
        with self._lock:
            return [
                d for d in self._decisions if d.tenant_id == tenant_id and d.key_link_id == link_id
            ]

    def assign_entities(
        self, tenant_id: str, assignments: Mapping[str, str | None], now: datetime
    ) -> int:
        changed = 0
        with self._lock:
            for link_id, entity_id in assignments.items():
                link = self._links.get((tenant_id, link_id))
                if link is None or link.entity_id == entity_id:
                    continue
                self._links[(tenant_id, link_id)] = replace(
                    link, entity_id=entity_id, updated_at=now
                )
                changed += 1
        return changed

    def ping(self) -> bool:
        return True


class InMemoryEntityRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entities: dict[tuple[str, str], Entity] = {}

    def list_entities(self, tenant_id: str) -> list[Entity]:
        with self._lock:
            rows = [e for (tenant, _id), e in self._entities.items() if tenant == tenant_id]
        return sorted(rows, key=lambda entity: entity.id)

    def get(self, tenant_id: str, entity_id: str) -> Entity | None:
        with self._lock:
            return self._entities.get((tenant_id, entity_id))

    def replace_all(self, tenant_id: str, entities: Sequence[Entity], now: datetime) -> None:
        with self._lock:
            for key in [k for k in self._entities if k[0] == tenant_id]:
                del self._entities[key]
            for entity in entities:
                self._entities[(tenant_id, entity.id)] = replace(entity, updated_at=now)

    def set_name(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        name: str,
        description: str | None,
        now: datetime,
    ) -> Entity:
        with self._lock:
            entity = self._entities.get((tenant_id, entity_id))
            if entity is None:
                msg = "entity not found"
                raise NotFoundError(msg)
            stored = replace(
                entity, name=name, name_confirmed=True, description=description, updated_at=now
            )
            self._entities[(tenant_id, entity_id)] = stored
            return stored

    def ping(self) -> bool:
        return True


# -- PostgreSQL --------------------------------------------------------------------------------

_SELECT_LINK = sqlalchemy.text(
    "SELECT id, field_a, form_a, field_b, form_b, link_type, role, direction, score, "
    "evidence_json, status, entity_id, reject_reason, decided_by, decided_at, version, "
    "affected_volume, ambiguous_merges, baseline_match_rate, first_proposed_at, note, updated_at "
    "FROM key_links WHERE tenant_id = :tenant_id AND id = :id"
)
_SELECT_BY_ENDS = sqlalchemy.text(
    "SELECT id, field_a, form_a, field_b, form_b, link_type, role, direction, score, "
    "evidence_json, status, entity_id, reject_reason, decided_by, decided_at, version, "
    "affected_volume, ambiguous_merges, baseline_match_rate, first_proposed_at, note, updated_at "
    "FROM key_links WHERE tenant_id = :tenant_id AND field_a = :field_a AND form_a = :form_a "
    "AND field_b = :field_b AND form_b = :form_b"
)
_SELECT_LINKS = sqlalchemy.text(
    "SELECT id, field_a, form_a, field_b, form_b, link_type, role, direction, score, "
    "evidence_json, status, entity_id, reject_reason, decided_by, decided_at, version, "
    "affected_volume, ambiguous_merges, baseline_match_rate, first_proposed_at, note, updated_at "
    "FROM key_links WHERE tenant_id = :tenant_id ORDER BY id"
)
_UPSERT_LINK = sqlalchemy.text(
    "INSERT INTO key_links (tenant_id, id, field_a, form_a, field_b, form_b, link_type, role, "
    "direction, score, evidence_json, status, entity_id, reject_reason, decided_by, decided_at, "
    "version, affected_volume, ambiguous_merges, baseline_match_rate, first_proposed_at, note, "
    "created_at, updated_at) VALUES (:tenant_id, :id, :field_a, :form_a, :field_b, :form_b, "
    ":link_type, :role, :direction, :score, CAST(:evidence_json AS JSONB), :status, :entity_id, "
    ":reject_reason, :decided_by, :decided_at, 1, :affected_volume, :ambiguous_merges, "
    ":baseline_match_rate, :first_proposed_at, :note, :now, :now) ON CONFLICT (tenant_id, "
    "field_a, form_a, field_b, form_b) DO UPDATE SET link_type = EXCLUDED.link_type, "
    "role = EXCLUDED.role, direction = EXCLUDED.direction, score = EXCLUDED.score, "
    "evidence_json = EXCLUDED.evidence_json, status = EXCLUDED.status, "
    "entity_id = EXCLUDED.entity_id, reject_reason = EXCLUDED.reject_reason, "
    "decided_by = EXCLUDED.decided_by, decided_at = EXCLUDED.decided_at, "
    "version = key_links.version + 1, affected_volume = EXCLUDED.affected_volume, "
    "ambiguous_merges = EXCLUDED.ambiguous_merges, "
    "baseline_match_rate = EXCLUDED.baseline_match_rate, "
    "first_proposed_at = COALESCE(key_links.first_proposed_at, EXCLUDED.first_proposed_at), "
    "note = EXCLUDED.note, updated_at = EXCLUDED.updated_at RETURNING id, field_a, form_a, "
    "field_b, form_b, link_type, role, direction, score, evidence_json, status, entity_id, "
    "reject_reason, decided_by, decided_at, version, affected_volume, ambiguous_merges, "
    "baseline_match_rate, first_proposed_at, note, updated_at"
)
_DECIDE_LINK = sqlalchemy.text(
    "UPDATE key_links SET status = :status, role = COALESCE(:role, role), "
    "reject_reason = :reject_reason, decided_by = :decided_by, decided_at = :decided_at, "
    "note = :note, version = version + 1, baseline_match_rate = COALESCE(:baseline_match_rate, "
    "baseline_match_rate), updated_at = :decided_at WHERE tenant_id = :tenant_id AND id = :id "
    "AND version = :expected_version RETURNING id, field_a, form_a, field_b, form_b, link_type, "
    "role, direction, score, evidence_json, status, entity_id, reject_reason, decided_by, "
    "decided_at, version, affected_volume, ambiguous_merges, baseline_match_rate, "
    "first_proposed_at, note, updated_at"
)
_INSERT_DECISION = sqlalchemy.text(
    "INSERT INTO key_link_decisions (tenant_id, key_link_id, version, status, reject_reason, "
    "decided_by, decided_at, note, score_at_decision, created_at, updated_at) "
    "VALUES (:tenant_id, :key_link_id, :version, :status, :reject_reason, :decided_by, "
    ":decided_at, :note, :score_at_decision, :decided_at, :decided_at)"
)
_SELECT_DECISIONS = sqlalchemy.text(
    "SELECT id, version, status, reject_reason, decided_by, decided_at, note, score_at_decision "
    "FROM key_link_decisions WHERE tenant_id = :tenant_id AND key_link_id = :key_link_id "
    "ORDER BY id"
)
_ASSIGN_ENTITY = sqlalchemy.text(
    "UPDATE key_links SET entity_id = :entity_id, updated_at = :now "
    "WHERE tenant_id = :tenant_id AND id = :id AND entity_id IS DISTINCT FROM :entity_id"
)
_SELECT_ENTITIES = sqlalchemy.text(
    "SELECT id, name, suggested_name, name_confirmed, description, updated_at FROM entities "
    "WHERE tenant_id = :tenant_id ORDER BY id"
)
_SELECT_ENTITY = sqlalchemy.text(
    "SELECT id, name, suggested_name, name_confirmed, description, updated_at FROM entities "
    "WHERE tenant_id = :tenant_id AND id = :id"
)
_SELECT_MEMBERS = sqlalchemy.text(
    "SELECT entity_id, field_id, form FROM entity_fields WHERE tenant_id = :tenant_id "
    "ORDER BY entity_id, field_id, form"
)
_DELETE_MEMBERS = sqlalchemy.text("DELETE FROM entity_fields WHERE tenant_id = :tenant_id")
_DELETE_ENTITIES_EXCEPT = sqlalchemy.text(
    "DELETE FROM entities WHERE tenant_id = :tenant_id AND NOT (id = ANY(:keep))"
)
_UPSERT_ENTITY = sqlalchemy.text(
    "INSERT INTO entities (tenant_id, id, name, suggested_name, name_confirmed, description, "
    "created_at, updated_at) VALUES (:tenant_id, :id, :name, :suggested_name, :name_confirmed, "
    ":description, :now, :now) "
    "ON CONFLICT (tenant_id, id) DO UPDATE SET name = EXCLUDED.name, "
    "suggested_name = EXCLUDED.suggested_name, name_confirmed = EXCLUDED.name_confirmed, "
    "description = EXCLUDED.description, updated_at = EXCLUDED.updated_at"
)
_INSERT_MEMBER = sqlalchemy.text(
    "INSERT INTO entity_fields (tenant_id, entity_id, field_id, form, created_at, updated_at) "
    "VALUES (:tenant_id, :entity_id, :field_id, :form, :now, :now)"
)
_SET_ENTITY_NAME = sqlalchemy.text(
    "UPDATE entities SET name = :name, name_confirmed = TRUE, description = :description, "
    "updated_at = :now WHERE tenant_id = :tenant_id AND id = :id"
)


def _link_parameters(tenant_id: str, link: KeyLink, now: datetime) -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "id": link.id,
        "field_a": link.a.field_ref,
        "form_a": link.a.form,
        "field_b": link.b.field_ref,
        "form_b": link.b.form,
        "link_type": link.link_type.value,
        "role": link.role.value,
        "direction": link.direction.value,
        "score": link.score,
        "evidence_json": json.dumps(_evidence_document(link), sort_keys=True, default=str),
        "status": link.status.value,
        "entity_id": link.entity_id,
        "reject_reason": link.reject_reason.value if link.reject_reason else None,
        "decided_by": link.decided_by,
        "decided_at": link.decided_at,
        "affected_volume": link.affected_volume,
        "ambiguous_merges": link.ambiguous_merges,
        "baseline_match_rate": link.baseline_match_rate,
        "first_proposed_at": link.first_proposed_at,
        "note": link.extra.get("note"),
        "now": now,
    }


def _link_from_row(row: Any) -> KeyLink:
    evidence = row.evidence_json
    if isinstance(evidence, str | bytes):
        evidence = json.loads(evidence)
    document: Mapping[str, Any] = evidence if isinstance(evidence, Mapping) else {}
    extra = document.get("extra")
    extra_dict = dict(extra) if isinstance(extra, Mapping) else {}
    if row.note is not None:
        extra_dict["note"] = row.note
    return KeyLink(
        id=row.id,
        a=FieldForm(row.field_a, row.form_a),
        b=FieldForm(row.field_b, row.form_b),
        link_type=LinkType(row.link_type),
        role=LinkRole(row.role),
        direction=Direction(row.direction),
        score=float(row.score),
        status=LinkStatus(row.status),
        features=features_from_evidence(document),
        affected_volume=int(row.affected_volume or 0),
        version=int(row.version),
        entity_id=row.entity_id,
        reject_reason=RejectReason(row.reject_reason) if row.reject_reason else None,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        ambiguous_merges=int(row.ambiguous_merges or 0),
        first_proposed_at=row.first_proposed_at,
        updated_at=row.updated_at,
        baseline_match_rate=(
            None if row.baseline_match_rate is None else float(row.baseline_match_rate)
        ),
        extra=extra_dict,
    )


def _wrap(what: str, exc: SQLAlchemyError) -> RepositoryError:
    return RepositoryError(f"{what} failed: {type(exc).__name__}")


class PostgresLinkRepository:
    """``key_links`` and ``key_link_decisions`` (migration 0004)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, tenant_id: str, link_id: str) -> KeyLink | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(_SELECT_LINK, {"tenant_id": tenant_id, "id": link_id})
                first = row.first()
        except SQLAlchemyError as exc:
            raise _wrap("link read", exc) from exc
        return None if first is None else _link_from_row(first)

    def find_by_ends(self, tenant_id: str, a: FieldForm, b: FieldForm) -> KeyLink | None:
        parameters = {
            "tenant_id": tenant_id,
            "field_a": a.field_ref,
            "form_a": a.form,
            "field_b": b.field_ref,
            "form_b": b.form,
        }
        try:
            with self._engine.connect() as connection:
                first = connection.execute(_SELECT_BY_ENDS, parameters).first()
        except SQLAlchemyError as exc:
            raise _wrap("link read", exc) from exc
        return None if first is None else _link_from_row(first)

    def list_links(
        self, tenant_id: str, *, statuses: Collection[LinkStatus] | None = None
    ) -> list[KeyLink]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_LINKS, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _wrap("link list", exc) from exc
        links = [_link_from_row(row) for row in rows]
        if statuses is None:
            return links
        wanted = set(statuses)
        return [link for link in links if link.status in wanted]

    def upsert(self, tenant_id: str, link: KeyLink, now: datetime) -> KeyLink:
        try:
            with self._engine.begin() as connection:
                row = connection.execute(_UPSERT_LINK, _link_parameters(tenant_id, link, now))
                first = row.first()
        except SQLAlchemyError as exc:
            raise _wrap("link upsert", exc) from exc
        if first is None:
            msg = "link upsert returned no row"
            raise RepositoryError(msg)
        return _link_from_row(first)

    def decide(
        self, tenant_id: str, link_id: str, *, expected_version: int, write: DecisionWrite
    ) -> KeyLink:
        parameters = {
            "tenant_id": tenant_id,
            "id": link_id,
            "expected_version": expected_version,
            "status": write.status.value,
            "role": write.role.value if write.role is not None else None,
            "reject_reason": write.reject_reason.value if write.reject_reason else None,
            "decided_by": write.decided_by,
            "decided_at": write.decided_at,
            "note": write.note,
            "baseline_match_rate": write.baseline_match_rate,
        }
        try:
            with self._engine.begin() as connection:
                updated = connection.execute(_DECIDE_LINK, parameters).first()
                if updated is None:
                    exists = connection.execute(
                        _SELECT_LINK, {"tenant_id": tenant_id, "id": link_id}
                    ).first()
                    if exists is None:
                        msg = "link not found"
                        raise NotFoundError(msg)
                    msg = "link version changed"
                    raise StaleVersionError(msg)
                connection.execute(
                    _INSERT_DECISION,
                    {
                        "tenant_id": tenant_id,
                        "key_link_id": link_id,
                        "version": int(updated.version),
                        "status": write.status.value,
                        "reject_reason": parameters["reject_reason"],
                        "decided_by": write.decided_by,
                        "decided_at": write.decided_at,
                        "note": write.note,
                        "score_at_decision": write.score_at_decision,
                    },
                )
        except SQLAlchemyError as exc:
            raise _wrap("link decision", exc) from exc
        return _link_from_row(updated)

    def decisions(self, tenant_id: str, link_id: str) -> list[Decision]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    _SELECT_DECISIONS, {"tenant_id": tenant_id, "key_link_id": link_id}
                ).all()
        except SQLAlchemyError as exc:
            raise _wrap("decision list", exc) from exc
        return [
            Decision(
                tenant_id=tenant_id,
                key_link_id=link_id,
                version=int(row.version),
                status=LinkStatus(row.status),
                reject_reason=RejectReason(row.reject_reason) if row.reject_reason else None,
                decided_by=row.decided_by,
                decided_at=row.decided_at,
                note=row.note,
                score_at_decision=float(row.score_at_decision),
                id=int(row.id),
            )
            for row in rows
        ]

    def assign_entities(
        self, tenant_id: str, assignments: Mapping[str, str | None], now: datetime
    ) -> int:
        changed = 0
        try:
            with self._engine.begin() as connection:
                for link_id, entity_id in assignments.items():
                    result = connection.execute(
                        _ASSIGN_ENTITY,
                        {"tenant_id": tenant_id, "id": link_id, "entity_id": entity_id, "now": now},
                    )
                    changed += int(result.rowcount)
        except SQLAlchemyError as exc:
            raise _wrap("entity assignment", exc) from exc
        return changed

    def ping(self) -> bool:
        return ping_engine(self._engine)


def _entity_from_row(row: Any, members: Iterable[FieldForm]) -> Entity:
    return Entity(
        id=row.id,
        members=tuple(members),
        name=row.name,
        suggested_name=row.suggested_name,
        name_confirmed=bool(row.name_confirmed),
        description=row.description,
        updated_at=row.updated_at,
    )


class PostgresEntityRepository:
    """``entities`` and ``entity_fields`` (migration 0004)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _members(self, connection: Connection, tenant_id: str) -> dict[str, list[FieldForm]]:
        members: dict[str, list[FieldForm]] = {}
        for row in connection.execute(_SELECT_MEMBERS, {"tenant_id": tenant_id}):
            members.setdefault(row.entity_id, []).append(FieldForm(row.field_id, row.form))
        return members

    def list_entities(self, tenant_id: str) -> list[Entity]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_ENTITIES, {"tenant_id": tenant_id}).all()
                members = self._members(connection, tenant_id)
        except SQLAlchemyError as exc:
            raise _wrap("entity list", exc) from exc
        return [_entity_from_row(row, members.get(row.id, [])) for row in rows]

    def get(self, tenant_id: str, entity_id: str) -> Entity | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    _SELECT_ENTITY, {"tenant_id": tenant_id, "id": entity_id}
                ).first()
                if row is None:
                    return None
                members = self._members(connection, tenant_id).get(entity_id, [])
        except SQLAlchemyError as exc:
            raise _wrap("entity read", exc) from exc
        return _entity_from_row(row, members)

    def replace_all(self, tenant_id: str, entities: Sequence[Entity], now: datetime) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(_DELETE_MEMBERS, {"tenant_id": tenant_id})
                connection.execute(
                    _DELETE_ENTITIES_EXCEPT,
                    {"tenant_id": tenant_id, "keep": [entity.id for entity in entities]},
                )
                for entity in entities:
                    connection.execute(
                        _UPSERT_ENTITY,
                        {
                            "tenant_id": tenant_id,
                            "id": entity.id,
                            "name": entity.name,
                            "suggested_name": entity.suggested_name,
                            "name_confirmed": entity.name_confirmed,
                            "description": entity.description,
                            "now": now,
                        },
                    )
                    for member in entity.members:
                        connection.execute(
                            _INSERT_MEMBER,
                            {
                                "tenant_id": tenant_id,
                                "entity_id": entity.id,
                                "field_id": member.field_ref,
                                "form": member.form,
                                "now": now,
                            },
                        )
        except SQLAlchemyError as exc:
            raise _wrap("entity replace", exc) from exc

    def set_name(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        name: str,
        description: str | None,
        now: datetime,
    ) -> Entity:
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    _SET_ENTITY_NAME,
                    {
                        "tenant_id": tenant_id,
                        "id": entity_id,
                        "name": name,
                        "description": description,
                        "now": now,
                    },
                )
                if int(result.rowcount) == 0:
                    msg = "entity not found"
                    raise NotFoundError(msg)
        except SQLAlchemyError as exc:
            raise _wrap("entity rename", exc) from exc
        entity = self.get(tenant_id, entity_id)
        if entity is None:
            msg = "entity not found"
            raise NotFoundError(msg)
        return entity

    def ping(self) -> bool:
        return ping_engine(self._engine)
