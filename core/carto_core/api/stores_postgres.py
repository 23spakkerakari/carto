"""PostgreSQL implementations of the identity ports (spec 14.1, 14.2; migration 0004 of
package A: ``sessions``, ``users``, ``groups``, ``role_bindings``).

``sessions`` holds one row per live session keyed by the SHA-256 of the cookie value, with the
roles and the reveal grant in ``roles_json`` (``{"roles": [...], "reveal": bool}``). Pending
logins share the table so a login survives a restart between ``/auth/login`` and the callback
without a table of their own: a pending row has an empty ``subject``, the PKCE verifier in
``csrf_token``, ``{"pending": true, "nonce": ...}`` in ``roles_json`` and the ten-minute
deadline in ``expires_at``; the session resolver refuses rows with an empty subject, and
:meth:`PostgresSessionStore.pop` deletes the row as it reads it so a state is usable once.
Expired rows of both kinds are swept opportunistically on every create.

SQLAlchemy Core text with bound parameters only (spec 14.7). Errors surface as
:class:`StoreError` with the exception type, never a statement or a value.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from carto_core.api.ports import LoginAttempt, RoleBinding, SessionRecord, UserRecord
from carto_core.db.postgres import ping_engine
from carto_schema.api import Role

__all__ = [
    "PostgresRoleBindingStore",
    "PostgresSessionStore",
    "PostgresUserStore",
    "StoreError",
]


class StoreError(Exception):
    """A store statement failed; the message names the exception type only."""


def _fail(what: str, exc: SQLAlchemyError) -> StoreError:
    return StoreError(f"{what} failed: {type(exc).__name__}")


def _json(value: object) -> Any:
    """A JSON column as psycopg returns it (already decoded) or as text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


_INSERT_SESSION = sqlalchemy.text(
    "INSERT INTO sessions (id, tenant_id, subject, csrf_token, authenticated_at, last_seen_at, "
    "expires_at, roles_json, display_name, ip, user_agent, created_at, updated_at) VALUES (:id, "
    ":tenant_id, :subject, :csrf_token, :authenticated_at, :last_seen_at, :expires_at, "
    "CAST(:roles_json AS jsonb), :display_name, :ip, :user_agent, :now, :now)"
)
_SELECT_SESSION = sqlalchemy.text(
    "SELECT id, tenant_id, subject, csrf_token, authenticated_at, last_seen_at, expires_at, "
    "roles_json, display_name, ip, user_agent FROM sessions WHERE id = :id"
)
_TOUCH_SESSION = sqlalchemy.text(
    "UPDATE sessions SET last_seen_at = :last_seen_at, updated_at = :last_seen_at WHERE id = :id"
)
_DELETE_SESSION = sqlalchemy.text("DELETE FROM sessions WHERE id = :id")
_DELETE_SESSION_RETURNING = sqlalchemy.text(
    "DELETE FROM sessions WHERE id = :id RETURNING id, tenant_id, subject, csrf_token, "
    "authenticated_at, last_seen_at, expires_at, roles_json, display_name, ip, user_agent"
)
_SWEEP_SESSIONS = sqlalchemy.text("DELETE FROM sessions WHERE expires_at < :now")


class PostgresSessionStore:
    """``SessionStore`` and ``LoginAttemptStore`` over the ``sessions`` table."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, session: SessionRecord) -> None:
        roles_json = json.dumps(
            {"roles": [role.value for role in session.roles], "reveal": session.reveal_granted}
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(_SWEEP_SESSIONS, {"now": session.last_seen_at})
                connection.execute(
                    _INSERT_SESSION,
                    {
                        "id": session.id,
                        "tenant_id": session.tenant_id,
                        "subject": session.subject,
                        "csrf_token": session.csrf_token,
                        "authenticated_at": session.authenticated_at,
                        "last_seen_at": session.last_seen_at,
                        "expires_at": session.expires_at,
                        "roles_json": roles_json,
                        "display_name": session.display_name,
                        "ip": session.ip,
                        "user_agent": session.user_agent,
                        "now": session.last_seen_at,
                    },
                )
        except SQLAlchemyError as exc:
            raise _fail("session insert", exc) from exc

    def get(self, session_id: str) -> SessionRecord | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(_SELECT_SESSION, {"id": session_id}).first()
        except SQLAlchemyError as exc:
            raise _fail("session lookup", exc) from exc
        if row is None or not row.subject:
            return None
        return _session_of(row)

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(_TOUCH_SESSION, {"id": session_id, "last_seen_at": last_seen_at})
        except SQLAlchemyError as exc:
            raise _fail("session touch", exc) from exc

    def delete(self, session_id: str) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(_DELETE_SESSION, {"id": session_id})
        except SQLAlchemyError as exc:
            raise _fail("session delete", exc) from exc

    def put(self, attempt: LoginAttempt) -> None:
        roles_json = json.dumps({"pending": True, "nonce": attempt.nonce})
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    _INSERT_SESSION,
                    {
                        "id": attempt.state_hash,
                        "tenant_id": attempt.tenant_id,
                        "subject": "",
                        "csrf_token": attempt.code_verifier,
                        "authenticated_at": attempt.created_at,
                        "last_seen_at": attempt.created_at,
                        "expires_at": attempt.expires_at,
                        "roles_json": roles_json,
                        "display_name": "",
                        "ip": None,
                        "user_agent": None,
                        "now": attempt.created_at,
                    },
                )
        except SQLAlchemyError as exc:
            raise _fail("login attempt insert", exc) from exc

    def pop(self, state_hash: str) -> LoginAttempt | None:
        try:
            with self._engine.begin() as connection:
                row = connection.execute(_DELETE_SESSION_RETURNING, {"id": state_hash}).first()
        except SQLAlchemyError as exc:
            raise _fail("login attempt pop", exc) from exc
        if row is None or row.subject:
            return None
        data = _json(row.roles_json)
        if not isinstance(data, dict) or not data.get("pending"):
            return None
        nonce = data.get("nonce")
        if not isinstance(nonce, str):
            return None
        return LoginAttempt(
            state_hash=row.id,
            tenant_id=row.tenant_id,
            code_verifier=row.csrf_token,
            nonce=nonce,
            created_at=row.authenticated_at,
            expires_at=row.expires_at,
        )

    def ping(self) -> bool:
        return ping_engine(self._engine)


def _session_of(row: Any) -> SessionRecord:
    data = _json(row.roles_json)
    roles: list[Role] = []
    reveal = False
    if isinstance(data, dict):
        raw_roles = data.get("roles")
        if isinstance(raw_roles, list):
            roles = [Role(value) for value in raw_roles if value in Role.__members__.values()]
        reveal = data.get("reveal") is True
    return SessionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        subject=row.subject,
        csrf_token=row.csrf_token,
        authenticated_at=row.authenticated_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        roles=tuple(roles),
        display_name=row.display_name or "",
        ip=row.ip,
        user_agent=row.user_agent,
        reveal_granted=reveal,
    )


_UPSERT_USER = sqlalchemy.text(
    "INSERT INTO users (tenant_id, subject, display_name, email, last_login_at, reveal_granted, "
    "created_at, updated_at) VALUES (:tenant_id, :subject, :display_name, :email, "
    ":last_login_at, false, :last_login_at, :last_login_at) ON CONFLICT (tenant_id, subject) "
    "DO UPDATE SET display_name = EXCLUDED.display_name, email = EXCLUDED.email, "
    "last_login_at = EXCLUDED.last_login_at, updated_at = EXCLUDED.last_login_at "
    "RETURNING tenant_id, subject, display_name, email, last_login_at, reveal_granted"
)
_SELECT_USER = sqlalchemy.text(
    "SELECT tenant_id, subject, display_name, email, last_login_at, reveal_granted FROM users "
    "WHERE tenant_id = :tenant_id AND subject = :subject"
)


def _user_of(row: Any) -> UserRecord:
    return UserRecord(
        tenant_id=row.tenant_id,
        subject=row.subject,
        display_name=row.display_name or "",
        email=row.email or "",
        last_login_at=row.last_login_at,
        reveal_granted=bool(row.reveal_granted),
    )


class PostgresUserStore:
    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(self, user: UserRecord) -> UserRecord:
        try:
            with self._engine.begin() as connection:
                row = connection.execute(
                    _UPSERT_USER,
                    {
                        "tenant_id": user.tenant_id,
                        "subject": user.subject,
                        "display_name": user.display_name,
                        "email": user.email,
                        "last_login_at": user.last_login_at,
                    },
                ).first()
        except SQLAlchemyError as exc:
            raise _fail("user upsert", exc) from exc
        if row is None:  # pragma: no cover - RETURNING always yields the row
            return user
        return _user_of(row)

    def get(self, tenant_id: str, subject: str) -> UserRecord | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    _SELECT_USER, {"tenant_id": tenant_id, "subject": subject}
                ).first()
        except SQLAlchemyError as exc:
            raise _fail("user lookup", exc) from exc
        return _user_of(row) if row is not None else None

    def ping(self) -> bool:
        return ping_engine(self._engine)


_SELECT_BINDINGS = sqlalchemy.text(
    "SELECT id, tenant_id, group_name, role, scope_json FROM role_bindings "
    "WHERE tenant_id = :tenant_id ORDER BY id"
)
_COUNT_BINDINGS = sqlalchemy.text("SELECT count(*) FROM role_bindings WHERE tenant_id = :tenant_id")
_INSERT_GROUP = sqlalchemy.text(
    "INSERT INTO groups (tenant_id, name, created_at, updated_at) VALUES (:tenant_id, :name, "
    ":now, :now) ON CONFLICT DO NOTHING"
)
_INSERT_BINDING = sqlalchemy.text(
    "INSERT INTO role_bindings (tenant_id, group_name, role, scope_json, created_at, "
    "updated_at) VALUES (:tenant_id, :group_name, :role, CAST(:scope_json AS jsonb), "
    ":now, :now)"
)
"""``role_bindings.id`` is a sequence (migration 0004): the row takes the next value and
:meth:`PostgresRoleBindingStore.list` reports it as a string. The seed's own ids (the in-memory
store's) are not stored."""


class PostgresRoleBindingStore:
    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list(self, tenant_id: str) -> Sequence[RoleBinding]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_BINDINGS, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _fail("role binding list", exc) from exc
        bindings: list[RoleBinding] = []
        for row in rows:
            if row.role not in Role.__members__.values():
                continue
            scope = _json(row.scope_json)
            bindings.append(
                RoleBinding(
                    id=str(row.id),
                    tenant_id=row.tenant_id,
                    group_name=row.group_name,
                    role=Role(row.role),
                    scope=scope if isinstance(scope, dict) else {},
                )
            )
        return bindings

    def seed(self, bindings: Sequence[RoleBinding]) -> bool:
        if not bindings:
            return False
        now = datetime.now(UTC)
        tenants = sorted({binding.tenant_id for binding in bindings})
        try:
            with self._engine.begin() as connection:
                for tenant_id in tenants:
                    count = connection.execute(_COUNT_BINDINGS, {"tenant_id": tenant_id}).scalar()
                    if count:
                        return False
                for binding in bindings:
                    connection.execute(
                        _INSERT_GROUP,
                        {"tenant_id": binding.tenant_id, "name": binding.group_name, "now": now},
                    )
                    connection.execute(
                        _INSERT_BINDING,
                        {
                            "tenant_id": binding.tenant_id,
                            "group_name": binding.group_name,
                            "role": binding.role.value,
                            "scope_json": json.dumps(dict(binding.scope)),
                            "now": now,
                        },
                    )
        except SQLAlchemyError as exc:
            raise _fail("role binding seed", exc) from exc
        return True

    def ping(self) -> bool:
        return ping_engine(self._engine)
