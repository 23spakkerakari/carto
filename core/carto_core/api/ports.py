"""The data the console API reads and writes, as narrow Protocols (spec 12; plan M2 package E).

The API never touches a table of the engine directly: it asks a port for records and renders
them as the views of :mod:`carto_schema.api`. Every record below carries exactly the columns
its view needs, named as the view names them, so an implementation over ``key_links``,
``entities``, ``fields``, ``systems`` and ``sources`` (packages A and C) is a straight mapping
and the in-memory implementations of :mod:`carto_core.api.memory_ports` back the unit tests
and the authorization matrix test. Every port is scoped by ``tenant_id`` on every call (ADR
0002: one tenant per install, but no query is ever unscoped).

Pagination is cursor-based (spec 12): a port returns a :class:`PortPage` whose ``next_cursor``
is opaque to the API and bounded in length; the API passes it back unchanged. Decisions on
links and patches on entities and systems are optimistic: the caller passes the version it saw
(``If-Match``) and the port raises :class:`DecisionError` with ``kind="stale_version"`` when the
row moved (spec 12 "ETag/If-Match on mutable resources to prevent lost updates").

Nothing in a record is a token or a raw value (spec 2.3 invariant 2): evidence is counts,
shares and lags; fields carry shapes; sources carry configuration without secrets (ADR 0050).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol, runtime_checkable

from carto_core.audit import AuditRecorder
from carto_core.ingest.health import SourceHealthRecord
from carto_schema.api import Direction, LinkRole, LinkStatus, LinkType, RejectReason, Role

__all__ = [
    "MAX_CURSOR_LEN",
    "ApiPorts",
    "AuditPort",
    "AuditRow",
    "DecisionError",
    "DecisionErrorKind",
    "EntityFieldRecord",
    "EntityPort",
    "EntityRecord",
    "FieldPort",
    "FieldRecord",
    "LinkEndRecord",
    "LinkPort",
    "LinkRecord",
    "LoginAttempt",
    "LoginAttemptStore",
    "PortPage",
    "RoleBinding",
    "RoleBindingStore",
    "SessionRecord",
    "SessionStore",
    "SourcePort",
    "SourceRecord",
    "SystemPort",
    "SystemRecord",
    "UserRecord",
    "UserStore",
]

MAX_CURSOR_LEN: Final = 256
"""``Page.next_cursor`` is bounded to this in the contract; a port never returns a longer one."""

DecisionErrorKind = Literal["not_found", "stale_version", "invalid_transition", "reason_required"]


class DecisionError(Exception):
    """A decision or patch the port refused. ``kind`` maps to a status (404, 412, 409, 422)."""

    def __init__(self, kind: DecisionErrorKind) -> None:
        super().__init__(kind)
        self.kind: DecisionErrorKind = kind


@dataclass(frozen=True, slots=True)
class PortPage[T]:
    """One page of records and the opaque cursor of the next page (``None`` at the end)."""

    items: tuple[T, ...]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------------------------
# Links (spec 9.3 to 9.6, ``key_links``)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkEndRecord:
    """One end of a link; the columns of :class:`carto_schema.api.LinkEnd`."""

    field_id: str
    system_id: str
    template_id: str
    path: str
    form: str
    label: str | None = None
    top_shape: str | None = None


@dataclass(frozen=True, slots=True)
class LinkRecord:
    """One ``key_links`` row as the queue shows it; the columns of
    :class:`carto_schema.api.LinkView`. ``evidence`` is the ``evidence_json`` content
    (:meth:`carto_core.linker.model.PairFeatures.as_evidence`, plus ``components`` and the
    window for composite links); the API keeps only the keys the contract names."""

    id: str
    a: LinkEndRecord
    b: LinkEndRecord
    link_type: LinkType
    role: LinkRole
    direction: Direction
    score: float
    status: LinkStatus
    affected_volume: int
    sentence: str
    evidence: Mapping[str, Any]
    version: int
    first_proposed_at: datetime
    updated_at: datetime
    entity_id: str | None = None
    reject_reason: RejectReason | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    ambiguous_merges: int = 0

    @property
    def rank(self) -> float:
        """The queue order key (spec 9.6: ``score x affected_volume``)."""
        return self.score * self.affected_volume


@runtime_checkable
class LinkPort(Protocol):
    """The review queue (``key_links``). Lists are sorted by ``score x affected_volume``
    descending, then ``id`` ascending, so a cursor is stable between pages."""

    def list(
        self, tenant_id: str, status: LinkStatus, cursor: str | None, limit: int
    ) -> PortPage[LinkRecord]: ...

    def get(self, tenant_id: str, link_id: str) -> LinkRecord | None: ...

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
        """``proposed``, ``stale`` or ``auto_accepted`` to ``accepted``; appends a decision row
        (``key_link_decisions``) and bumps ``version``. Raises :class:`DecisionError`."""
        ...

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
        """To ``rejected``; ``reason`` is required (``reason_required`` otherwise)."""
        ...

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
        """Accept with ``role = association`` (spec 9.6 "Mark as association")."""
        ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------------------------
# Entities (spec 9.6 families, ``entities`` and ``entity_fields``)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityFieldRecord:
    field_id: str
    form: str
    system_id: str
    path: str


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """The columns of :class:`carto_schema.api.EntityView` plus ``version`` for the ETag."""

    id: str
    name: str
    suggested_name: str | None
    name_confirmed: bool
    description: str
    fields: tuple[EntityFieldRecord, ...]
    link_ids: tuple[str, ...]
    system_count: int
    version: int
    updated_at: datetime


@runtime_checkable
class EntityPort(Protocol):
    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[EntityRecord]: ...

    def get(self, tenant_id: str, entity_id: str) -> EntityRecord | None: ...

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
        """Rename or describe; a new ``name`` sets ``name_confirmed`` (spec 9.6 naming).
        Raises :class:`DecisionError` (``not_found``, ``stale_version``)."""
        ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------------------------
# Inventory (ADR 0050; ``systems``, ``sources``, ``fields``)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemRecord:
    """The columns of :class:`carto_schema.api.SystemView` plus ``version`` for the ETag."""

    id: str
    name: str
    description: str
    owner_group: str | None
    criticality: str
    source_count: int
    version: int
    updated_at: datetime


@runtime_checkable
class SystemPort(Protocol):
    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[SystemRecord]: ...

    def get(self, tenant_id: str, system_id: str) -> SystemRecord | None: ...

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
    ) -> SystemRecord: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """The columns of :class:`carto_schema.api.SourceView`. ``config`` is the connector
    configuration as the edge reported it, secrets removed (ADR 0050); ``health`` is the latest
    heartbeat (:mod:`carto_core.ingest.health`) or ``None`` before the first one."""

    id: str
    system_id: str
    type: str
    enabled: bool
    config: Mapping[str, Any]
    secret_ref: str | None
    backfill_days: int | None
    schedule: str | None
    last_test: str | None
    updated_at: datetime
    health: SourceHealthRecord | None = None


@runtime_checkable
class SourcePort(Protocol):
    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[SourceRecord]: ...

    def get(self, tenant_id: str, source_id: str) -> SourceRecord | None: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """The columns of :class:`carto_schema.api.FieldView`; ``shapes`` are ``(shape, share)``
    pairs, never values."""

    id: str
    system_id: str
    template_id: str
    path: str
    field_class: str
    policy: str
    pinned: bool
    count: int
    distinct_estimate: int
    null_rate: float
    shapes: tuple[tuple[str, float], ...]
    forms: tuple[str, ...]
    quality: float | None
    label: str | None
    suggested_label: str | None
    first_seen: datetime | None
    last_seen: datetime | None


@runtime_checkable
class FieldPort(Protocol):
    def list(
        self, tenant_id: str, system_id: str | None, cursor: str | None, limit: int
    ) -> PortPage[FieldRecord]: ...

    def get(self, tenant_id: str, field_id: str) -> FieldRecord | None: ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------------------------
# Audit reads (spec 14.9; the writer is :class:`carto_core.audit.AuditRecorder`)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditRow:
    """The columns of :class:`carto_schema.api.AuditEntryView`."""

    id: int
    at: datetime
    actor: str
    action: str
    target: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    reason: str
    request_id: str | None
    row_hash: str


@runtime_checkable
class AuditPort(Protocol):
    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[AuditRow]:
        """Newest first; the cursor is the last ``id`` of the page as text."""
        ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------------------------
# Identity: sessions, pending logins, users, role bindings (spec 14.1, 14.2)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One ``sessions`` row. ``id`` is the SHA-256 hex of the cookie value: the table never
    holds the value itself, so a database read cannot replay a session (spec 14.1)."""

    id: str
    tenant_id: str
    subject: str
    csrf_token: str
    authenticated_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    """The absolute deadline (spec 14.1: 12 hours); the idle deadline is ``last_seen_at`` plus
    the idle timeout and is computed, not stored."""
    roles: tuple[Role, ...]
    display_name: str
    ip: str | None
    user_agent: str | None
    reveal_granted: bool = False


@runtime_checkable
class SessionStore(Protocol):
    def create(self, session: SessionRecord) -> None: ...

    def get(self, session_id: str) -> SessionRecord | None: ...

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        """Slide the idle window."""
        ...

    def delete(self, session_id: str) -> None: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    """What ``GET /auth/login`` keeps server-side until the callback: the PKCE verifier, the
    nonce and the state (spec 14.1, PKCE). Keyed by the SHA-256 of ``state``."""

    state_hash: str
    tenant_id: str
    code_verifier: str
    nonce: str
    created_at: datetime
    expires_at: datetime


@runtime_checkable
class LoginAttemptStore(Protocol):
    def put(self, attempt: LoginAttempt) -> None: ...

    def pop(self, state_hash: str) -> LoginAttempt | None:
        """Return and delete: a state is usable once."""
        ...


@dataclass(frozen=True, slots=True)
class UserRecord:
    """One ``users`` row (spec 14.2); ``reveal_granted`` is the separate reveal grant."""

    tenant_id: str
    subject: str
    display_name: str
    email: str
    last_login_at: datetime
    reveal_granted: bool = False


@runtime_checkable
class UserStore(Protocol):
    def upsert(self, user: UserRecord) -> UserRecord:
        """Insert or update name, email and last login; keep ``reveal_granted`` as stored and
        return the stored row."""
        ...

    def get(self, tenant_id: str, subject: str) -> UserRecord | None: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RoleBinding:
    """One ``role_bindings`` row: an identity-provider group holds a role (spec 14.2). Scopes
    (``scope_json``) are stored but not enforced until M6."""

    id: str
    tenant_id: str
    group_name: str
    role: Role
    scope: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class RoleBindingStore(Protocol):
    def list(self, tenant_id: str) -> Sequence[RoleBinding]: ...

    def seed(self, bindings: Sequence[RoleBinding]) -> bool:
        """Insert ``bindings`` only when the tenant has none yet; True when they were written."""
        ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------------------------
# The bundle the application is built with
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiPorts:
    """Everything :func:`carto_core.api.app.create_app` needs, injected (tests pass the
    in-memory implementations; ``carto-core api`` the PostgreSQL ones)."""

    links: LinkPort
    entities: EntityPort
    systems: SystemPort
    sources: SourcePort
    fields: FieldPort
    audit: AuditPort
    audit_recorder: AuditRecorder
    sessions: SessionStore
    login_attempts: LoginAttemptStore
    users: UserStore
    role_bindings: RoleBindingStore

    def ping_all(self) -> bool:
        """Readiness: every port answers (``readyz``)."""
        return all(
            port.ping()
            for port in (
                self.links,
                self.entities,
                self.systems,
                self.sources,
                self.fields,
                self.audit,
                self.sessions,
                self.users,
                self.role_bindings,
            )
        )
