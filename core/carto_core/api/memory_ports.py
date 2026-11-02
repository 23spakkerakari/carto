"""In-memory implementations of every port in :mod:`carto_core.api.ports`.

They back the unit tests, the authorization matrix test and :func:`render_openapi`, and they
are the reference for the semantics an implementation over the tables must keep: the queue
order, the cursor rule, the decision state machine (spec 9.6) and the versioning that feeds
``ETag``/``If-Match``. :class:`InMemoryAuditLog` is both the recorder and the reader and keeps
the same hash chain as the PostgreSQL table (:mod:`carto_core.audit`).

State machine (spec 9.6, ADR 0053): ``proposed``, ``stale`` and ``auto_accepted`` may be
accepted, rejected or marked as an association; ``accepted`` may be rejected or re-marked;
``rejected`` is final until the linker re-proposes it (score up by more than 0.2), which is
the linker's job, not the API's. Every decision bumps ``version`` and sets ``decided_by`` and
``decided_at``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from carto_core.api.ports import (
    ApiPorts,
    AuditRow,
    DecisionError,
    EntityFieldRecord,
    EntityRecord,
    FieldRecord,
    LinkEndRecord,
    LinkRecord,
    LoginAttempt,
    PortPage,
    RoleBinding,
    SessionRecord,
    SourceRecord,
    SystemRecord,
    UserRecord,
)
from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash, scrub
from carto_core.ingest.health import SourceHealthRecord
from carto_schema.api import Direction, LinkRole, LinkStatus, LinkType, RejectReason, Role

__all__ = [
    "InMemoryAuditLog",
    "InMemoryEntityPort",
    "InMemoryFieldPort",
    "InMemoryLinkPort",
    "InMemoryLoginAttemptStore",
    "InMemoryRoleBindingStore",
    "InMemorySessionStore",
    "InMemorySourcePort",
    "InMemorySystemPort",
    "InMemoryUserStore",
    "binding",
    "example_entity",
    "example_field",
    "example_link",
    "example_source",
    "example_system",
    "memory_ports",
    "seeded_ports",
]

_DECIDABLE = frozenset(
    {LinkStatus.PROPOSED, LinkStatus.STALE, LinkStatus.AUTO_ACCEPTED, LinkStatus.ACCEPTED}
)


def _page[T](ordered: Sequence[T], key: Any, cursor: str | None, limit: int) -> PortPage[T]:
    """Slice ``ordered`` after the item whose ``key`` equals ``cursor``; the next cursor is the
    key of the last item returned when more follow. An unknown cursor yields an empty page."""
    start = 0
    if cursor is not None:
        start = next((i + 1 for i, item in enumerate(ordered) if key(item) == cursor), len(ordered))
    items = tuple(ordered[start : start + limit])
    more = start + len(items) < len(ordered)
    return PortPage(items, key(items[-1]) if items and more else None)


class InMemoryLinkPort:
    def __init__(self, links: Iterable[LinkRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._links: dict[tuple[str, str], LinkRecord] = {}
        self.decisions: list[tuple[str, str, str, int]] = []
        """``(link_id, action, actor, version)`` per decision, like ``key_link_decisions``."""
        self.ready = True
        for link in links:
            self.put("default", link)

    def put(self, tenant_id: str, link: LinkRecord) -> None:
        with self._lock:
            self._links[(tenant_id, link.id)] = link

    def list(
        self, tenant_id: str, status: LinkStatus, cursor: str | None, limit: int
    ) -> PortPage[LinkRecord]:
        with self._lock:
            rows = [
                link
                for (tenant, _id), link in self._links.items()
                if tenant == tenant_id and link.status == status
            ]
        rows.sort(key=lambda link: (-link.rank, link.id))
        return _page(rows, lambda link: link.id, cursor, limit)

    def get(self, tenant_id: str, link_id: str) -> LinkRecord | None:
        with self._lock:
            return self._links.get((tenant_id, link_id))

    def _decide(
        self,
        tenant_id: str,
        link_id: str,
        action: str,
        status: LinkStatus,
        role: LinkRole | None,
        *,
        actor: str,
        expected_version: int,
        reason: RejectReason | None,
        at: datetime,
    ) -> LinkRecord:
        with self._lock:
            current = self._links.get((tenant_id, link_id))
            if current is None:
                raise DecisionError("not_found")
            if current.version != expected_version:
                raise DecisionError("stale_version")
            if current.status not in _DECIDABLE:
                raise DecisionError("invalid_transition")
            if status is LinkStatus.REJECTED and reason is None:
                raise DecisionError("reason_required")
            updated = replace(
                current,
                status=status,
                role=role if role is not None else current.role,
                reject_reason=reason if status is LinkStatus.REJECTED else None,
                decided_by=actor,
                decided_at=at,
                updated_at=at,
                version=current.version + 1,
            )
            self._links[(tenant_id, link_id)] = updated
            self.decisions.append((link_id, action, actor, updated.version))
            return updated

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
        del note
        return self._decide(
            tenant_id,
            link_id,
            "accept",
            LinkStatus.ACCEPTED,
            None,
            actor=actor,
            expected_version=expected_version,
            reason=reason,
            at=at,
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
        del note
        return self._decide(
            tenant_id,
            link_id,
            "reject",
            LinkStatus.REJECTED,
            None,
            actor=actor,
            expected_version=expected_version,
            reason=reason,
            at=at,
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
        del note
        return self._decide(
            tenant_id,
            link_id,
            "association",
            LinkStatus.ACCEPTED,
            LinkRole.ASSOCIATION,
            actor=actor,
            expected_version=expected_version,
            reason=reason,
            at=at,
        )

    def ping(self) -> bool:
        return self.ready


class InMemoryEntityPort:
    def __init__(self, entities: Iterable[EntityRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], EntityRecord] = {}
        self.ready = True
        for entity in entities:
            self.put("default", entity)

    def put(self, tenant_id: str, entity: EntityRecord) -> None:
        with self._lock:
            self._rows[(tenant_id, entity.id)] = entity

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[EntityRecord]:
        with self._lock:
            rows = sorted(
                (row for (tenant, _id), row in self._rows.items() if tenant == tenant_id),
                key=lambda row: row.id,
            )
        return _page(rows, lambda row: row.id, cursor, limit)

    def get(self, tenant_id: str, entity_id: str) -> EntityRecord | None:
        with self._lock:
            return self._rows.get((tenant_id, entity_id))

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
        del actor
        with self._lock:
            current = self._rows.get((tenant_id, entity_id))
            if current is None:
                raise DecisionError("not_found")
            if current.version != expected_version:
                raise DecisionError("stale_version")
            updated = replace(
                current,
                name=name if name is not None else current.name,
                name_confirmed=current.name_confirmed or name is not None,
                description=description if description is not None else current.description,
                version=current.version + 1,
                updated_at=at,
            )
            self._rows[(tenant_id, entity_id)] = updated
            return updated

    def ping(self) -> bool:
        return self.ready


class InMemorySystemPort:
    def __init__(self, systems: Iterable[SystemRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], SystemRecord] = {}
        self.ready = True
        for system in systems:
            self.put("default", system)

    def put(self, tenant_id: str, system: SystemRecord) -> None:
        with self._lock:
            self._rows[(tenant_id, system.id)] = system

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[SystemRecord]:
        with self._lock:
            rows = sorted(
                (row for (tenant, _id), row in self._rows.items() if tenant == tenant_id),
                key=lambda row: row.id,
            )
        return _page(rows, lambda row: row.id, cursor, limit)

    def get(self, tenant_id: str, system_id: str) -> SystemRecord | None:
        with self._lock:
            return self._rows.get((tenant_id, system_id))

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
        del actor
        with self._lock:
            current = self._rows.get((tenant_id, system_id))
            if current is None:
                raise DecisionError("not_found")
            if current.version != expected_version:
                raise DecisionError("stale_version")
            updated = replace(
                current,
                name=name if name is not None else current.name,
                description=description if description is not None else current.description,
                version=current.version + 1,
                updated_at=at,
            )
            self._rows[(tenant_id, system_id)] = updated
            return updated

    def ping(self) -> bool:
        return self.ready


class InMemorySourcePort:
    def __init__(self, sources: Iterable[SourceRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], SourceRecord] = {}
        self.ready = True
        for source in sources:
            self.put("default", source)

    def put(self, tenant_id: str, source: SourceRecord) -> None:
        with self._lock:
            self._rows[(tenant_id, source.id)] = source

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[SourceRecord]:
        with self._lock:
            rows = sorted(
                (row for (tenant, _id), row in self._rows.items() if tenant == tenant_id),
                key=lambda row: row.id,
            )
        return _page(rows, lambda row: row.id, cursor, limit)

    def get(self, tenant_id: str, source_id: str) -> SourceRecord | None:
        with self._lock:
            return self._rows.get((tenant_id, source_id))

    def ping(self) -> bool:
        return self.ready


class InMemoryFieldPort:
    def __init__(self, fields: Iterable[FieldRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], FieldRecord] = {}
        self.ready = True
        for row in fields:
            self.put("default", row)

    def put(self, tenant_id: str, row: FieldRecord) -> None:
        with self._lock:
            self._rows[(tenant_id, row.id)] = row

    def list(
        self, tenant_id: str, system_id: str | None, cursor: str | None, limit: int
    ) -> PortPage[FieldRecord]:
        with self._lock:
            rows = sorted(
                (
                    row
                    for (tenant, _id), row in self._rows.items()
                    if tenant == tenant_id and (system_id is None or row.system_id == system_id)
                ),
                key=lambda row: row.id,
            )
        return _page(rows, lambda row: row.id, cursor, limit)

    def get(self, tenant_id: str, field_id: str) -> FieldRecord | None:
        with self._lock:
            return self._rows.get((tenant_id, field_id))

    def ping(self) -> bool:
        return self.ready


class InMemoryAuditLog:
    """Recorder and reader over one list, hash-chained per tenant like the table."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: list[tuple[int, AuditEntry, str, str]] = []
        """``(id, entry, prev_hash, row_hash)`` in append order."""
        self.ready = True

    def record(self, entry: AuditEntry) -> str:
        clean = replace(
            entry,
            at=entry.at.astimezone(UTC),
            before=scrub(entry.before),
            after=scrub(entry.after),
        )
        with self._lock:
            prev = self._latest(clean.tenant_id)
            row_hash = chain_hash(prev, clean.content())
            self.rows.append((len(self.rows) + 1, clean, prev, row_hash))
        return row_hash

    def _latest(self, tenant_id: str) -> str:
        for _id, entry, _prev, row_hash in reversed(self.rows):
            if entry.tenant_id == tenant_id:
                return row_hash
        return GENESIS_HASH

    def latest_hash(self, tenant_id: str) -> str:
        with self._lock:
            return self._latest(tenant_id)

    def entries(self, tenant_id: str = "default") -> list[AuditEntry]:
        with self._lock:
            return [entry for _id, entry, _prev, _hash in self.rows if entry.tenant_id == tenant_id]

    def chain(self, tenant_id: str = "default") -> list[tuple[int, str, str, Mapping[str, Any]]]:
        """Rows in the shape :func:`carto_core.audit.verify_chain` takes."""
        with self._lock:
            return [
                (row_id, prev, row_hash, entry.content())
                for row_id, entry, prev, row_hash in self.rows
                if entry.tenant_id == tenant_id
            ]

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[AuditRow]:
        with self._lock:
            rows = [
                AuditRow(
                    id=row_id,
                    at=entry.at,
                    actor=entry.actor,
                    action=entry.action,
                    target=entry.target,
                    before=entry.before,
                    after=entry.after,
                    reason=entry.reason,
                    request_id=entry.request_id,
                    row_hash=row_hash,
                )
                for row_id, entry, _prev, row_hash in reversed(self.rows)
                if entry.tenant_id == tenant_id
            ]
        return _page(rows, lambda row: str(row.id), cursor, limit)

    def ping(self) -> bool:
        return self.ready


class InMemorySessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, SessionRecord] = {}
        self.ready = True

    def create(self, session: SessionRecord) -> None:
        with self._lock:
            self._rows[session.id] = session

    def get(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._rows.get(session_id)

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        with self._lock:
            current = self._rows.get(session_id)
            if current is not None:
                self._rows[session_id] = replace(current, last_seen_at=last_seen_at)

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._rows.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def ping(self) -> bool:
        return self.ready


class InMemoryLoginAttemptStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, LoginAttempt] = {}

    def put(self, attempt: LoginAttempt) -> None:
        with self._lock:
            self._rows[attempt.state_hash] = attempt

    def pop(self, state_hash: str) -> LoginAttempt | None:
        with self._lock:
            return self._rows.pop(state_hash, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


class InMemoryUserStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], UserRecord] = {}
        self.ready = True

    def upsert(self, user: UserRecord) -> UserRecord:
        key = (user.tenant_id, user.subject)
        with self._lock:
            current = self._rows.get(key)
            stored = (
                replace(
                    current,
                    display_name=user.display_name,
                    email=user.email,
                    last_login_at=user.last_login_at,
                )
                if current is not None
                else replace(user, reveal_granted=False)
            )
            self._rows[key] = stored
            return stored

    def get(self, tenant_id: str, subject: str) -> UserRecord | None:
        with self._lock:
            return self._rows.get((tenant_id, subject))

    def grant_reveal(self, tenant_id: str, subject: str, *, granted: bool) -> None:
        """Test hook for the separate reveal grant (M6 wires the admin path)."""
        with self._lock:
            current = self._rows.get((tenant_id, subject))
            if current is not None:
                self._rows[(tenant_id, subject)] = replace(current, reveal_granted=granted)

    def ping(self) -> bool:
        return self.ready


class InMemoryRoleBindingStore:
    def __init__(self, bindings: Iterable[RoleBinding] = ()) -> None:
        self._lock = threading.Lock()
        self._rows: list[RoleBinding] = list(bindings)
        self.ready = True

    def list(self, tenant_id: str) -> Sequence[RoleBinding]:
        with self._lock:
            return [row for row in self._rows if row.tenant_id == tenant_id]

    def seed(self, bindings: Sequence[RoleBinding]) -> bool:
        with self._lock:
            tenants = {binding.tenant_id for binding in bindings}
            if any(row.tenant_id in tenants for row in self._rows):
                return False
            self._rows.extend(bindings)
            return True

    def ping(self) -> bool:
        return self.ready


def memory_ports(
    *,
    links: Iterable[LinkRecord] = (),
    entities: Iterable[EntityRecord] = (),
    systems: Iterable[SystemRecord] = (),
    sources: Iterable[SourceRecord] = (),
    fields: Iterable[FieldRecord] = (),
    bindings: Iterable[RoleBinding] = (),
) -> ApiPorts:
    """An :class:`ApiPorts` over fresh in-memory stores seeded for tenant ``default``."""
    audit = InMemoryAuditLog()
    return ApiPorts(
        links=InMemoryLinkPort(links),
        entities=InMemoryEntityPort(entities),
        systems=InMemorySystemPort(systems),
        sources=InMemorySourcePort(sources),
        fields=InMemoryFieldPort(fields),
        audit=audit,
        audit_recorder=audit,
        sessions=InMemorySessionStore(),
        login_attempts=InMemoryLoginAttemptStore(),
        users=InMemoryUserStore(),
        role_bindings=InMemoryRoleBindingStore(bindings),
    )


def binding(group: str, role: Role, tenant_id: str = "default") -> RoleBinding:
    """A binding row for tests and the bootstrap seed."""
    return RoleBinding(
        id=f"{tenant_id}:{group}:{role.value}", tenant_id=tenant_id, group_name=group, role=role
    )


# ---------------------------------------------------------------------------------------------
# Example records: one of each, for the unit tests, the matrix test and the OpenAPI render.
# Nothing in them is a value: paths, shapes, counts and a sentence built from them.
# ---------------------------------------------------------------------------------------------

EXAMPLE_AT = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def example_link(
    link_id: str = "lnk_01", *, score: float = 0.93, affected_volume: int = 12_400
) -> LinkRecord:
    return LinkRecord(
        id=link_id,
        a=LinkEndRecord(
            field_id="shop/t-order-created/order.id",
            system_id="shop",
            template_id="t-order-created",
            path="order.id",
            form="exact",
            label=None,
            top_shape="A-999999",
        ),
        b=LinkEndRecord(
            field_id="wms/t-pick/orderRef",
            system_id="wms",
            template_id="t-pick",
            path="orderRef",
            form="exact",
            label=None,
            top_shape="A-999999",
        ),
        link_type=LinkType.EXACT,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=score,
        status=LinkStatus.PROPOSED,
        affected_volume=affected_volume,
        sentence=(
            "98% of order ids in shop appear in wms within 4 minutes; 12,400 matched over 7 days."
        ),
        evidence={
            "n_a": 12_650,
            "n_b": 12_900,
            "n_match": 12_400,
            "c_ab": 0.98,
            "c_ba": 0.96,
            "lag_median_seconds": 240.0,
            "lag_iqr_seconds": 90.0,
            "lag_positive_share": 0.99,
            "fanout_ab_median": 1.0,
            "fanout_ab_p99": 1.0,
            "fanout_ba_median": 1.0,
            "fanout_ba_p99": 1.0,
            "concentration": 0.001,
            "chance_ratio": 0.0001,
            "domain_size": 1_000_000.0,
            "pairing_consistency": None,
            "ignored_by_the_contract": "dropped",
        },
        version=1,
        first_proposed_at=EXAMPLE_AT,
        updated_at=EXAMPLE_AT,
    )


def example_entity(entity_id: str = "ent_01") -> EntityRecord:
    return EntityRecord(
        id=entity_id,
        name="Order",
        suggested_name="Order",
        name_confirmed=False,
        description="",
        fields=(
            EntityFieldRecord("shop/t-order-created/order.id", "exact", "shop", "order.id"),
            EntityFieldRecord("wms/t-pick/orderRef", "exact", "wms", "orderRef"),
        ),
        link_ids=("lnk_01",),
        system_count=2,
        version=1,
        updated_at=EXAMPLE_AT,
    )


def example_system(system_id: str = "shop") -> SystemRecord:
    return SystemRecord(
        id=system_id,
        name="Shop",
        description="The web shop",
        owner_group=None,
        criticality="high",
        source_count=1,
        version=1,
        updated_at=EXAMPLE_AT,
    )


def example_source(source_id: str = "shop_logs", *, with_health: bool = True) -> SourceRecord:
    health = (
        SourceHealthRecord(
            tenant_id="default",
            source_id=source_id,
            status="ok",
            last_success_at=EXAMPLE_AT,
            lag_seconds=12.0,
            error_count=0,
            buffer_depth=3,
            oldest_buffered_at=None,
            message="",
            received_at=EXAMPLE_AT,
            sent_at=EXAMPLE_AT,
        )
        if with_health
        else None
    )
    return SourceRecord(
        id=source_id,
        system_id="shop",
        type="file",
        enabled=True,
        config={"path": "/var/log/shop/*.log", "format": "json"},
        secret_ref=None,
        backfill_days=7,
        schedule=None,
        last_test="read-only verified",
        updated_at=EXAMPLE_AT,
        health=health,
    )


def example_field(field_id: str = "shop/t-order-created/order.id") -> FieldRecord:
    system_id, template_id, path = field_id.split("/", 2)
    return FieldRecord(
        id=field_id,
        system_id=system_id,
        template_id=template_id,
        path=path,
        field_class="identifier",
        policy="tokenize",
        pinned=False,
        count=12_650,
        distinct_estimate=12_400,
        null_rate=0.0,
        shapes=(("A-999999", 0.97), ("A-99999", 0.03)),
        forms=("exact", "normalized"),
        quality=0.91,
        label=None,
        suggested_label="Order number",
        first_seen=EXAMPLE_AT,
        last_seen=EXAMPLE_AT,
    )


def seeded_ports(bindings: Iterable[RoleBinding] = ()) -> ApiPorts:
    """One of everything (plus a second, lower-ranked link and a source without health)."""
    return memory_ports(
        links=[example_link(), example_link("lnk_02", score=0.6, affected_volume=100)],
        entities=[example_entity()],
        systems=[example_system(), example_system("wms")],
        sources=[example_source(), example_source("wms_db", with_health=False)],
        fields=[example_field(), example_field("wms/t-pick/orderRef")],
        bindings=bindings,
    )
