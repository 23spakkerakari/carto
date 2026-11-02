"""The PostgreSQL audit log: recorder, reader and chain verifier (spec 14.9; ADR 0051).

:class:`PostgresAuditLog` appends under a transaction-scoped advisory lock keyed by the tenant,
so two API workers cannot both chain from the same head and fork the log; the row's content is
hashed exactly as :func:`carto_core.audit.chain_hash` defines it, with ``at`` normalised to UTC
first so the hash survives the round trip through ``timestamptz`` whatever the session time
zone. The application role has INSERT and SELECT only and a trigger rejects UPDATE and DELETE
(migration 0004), so the chain is the tamper evidence and :func:`verify_audit_log` walks it in
id order and reports the first break (``carto-core audit-verify``).

``before`` and ``after`` are scrubbed on write (:func:`carto_core.audit.scrub`) as the backstop
behind the metadata-only views the handlers pass.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC
from typing import TYPE_CHECKING, Any

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.api.ports import AuditRow, PortPage
from carto_core.audit import GENESIS_HASH, AuditEntry, ChainBreak, chain_hash, scrub, verify_chain
from carto_core.db.postgres import ping_engine

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["AuditStoreError", "PostgresAuditLog", "verify_audit_log"]


class AuditStoreError(Exception):
    """The audit row could not be written or read; the message names the exception type."""


_LOCK = sqlalchemy.text("SELECT pg_advisory_xact_lock(hashtext(:tenant_id))")
_HEAD = sqlalchemy.text(
    "SELECT row_hash FROM audit_log WHERE tenant_id = :tenant_id ORDER BY id DESC LIMIT 1"
)
_INSERT = sqlalchemy.text(
    "INSERT INTO audit_log (tenant_id, at, actor, action, target, before, after, reason, "
    "request_id, ip, user_agent, prev_hash, row_hash, created_at) VALUES (:tenant_id, :at, "
    ":actor, :action, :target, CAST(:before AS jsonb), CAST(:after AS jsonb), :reason, "
    ":request_id, :ip, :user_agent, :prev_hash, :row_hash, :at)"
)
_PAGE = sqlalchemy.text(
    "SELECT id, at, actor, action, target, before, after, reason, request_id, row_hash "
    "FROM audit_log WHERE tenant_id = :tenant_id "
    "AND (CAST(:before_id AS BIGINT) IS NULL OR id < CAST(:before_id AS BIGINT)) "
    "ORDER BY id DESC LIMIT :limit"
)
_WALK = sqlalchemy.text(
    "SELECT id, tenant_id, at, actor, action, target, before, after, reason, request_id, ip, "
    "user_agent, prev_hash, row_hash FROM audit_log WHERE tenant_id = :tenant_id ORDER BY id"
)


def _json_text(value: Mapping[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(dict(value), ensure_ascii=False, default=str)


def _json_value(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return dict(value) if isinstance(value, dict) else None


def normalize(entry: AuditEntry) -> AuditEntry:
    """UTC ``at`` and scrubbed views: the form that is hashed and stored."""
    return replace(
        entry,
        at=entry.at.astimezone(UTC),
        before=scrub(entry.before),
        after=scrub(entry.after),
    )


class PostgresAuditLog:
    """``AuditRecorder`` and ``AuditPort`` over ``audit_log``."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, entry: AuditEntry) -> str:
        clean = normalize(entry)
        try:
            with self._engine.begin() as connection:
                connection.execute(_LOCK, {"tenant_id": clean.tenant_id})
                prev = connection.execute(_HEAD, {"tenant_id": clean.tenant_id}).scalar()
                prev_hash = str(prev) if prev else GENESIS_HASH
                row_hash = chain_hash(prev_hash, clean.content())
                connection.execute(
                    _INSERT,
                    {
                        "tenant_id": clean.tenant_id,
                        "at": clean.at,
                        "actor": clean.actor,
                        "action": clean.action,
                        "target": clean.target,
                        "before": _json_text(clean.before),
                        "after": _json_text(clean.after),
                        "reason": clean.reason,
                        "request_id": clean.request_id,
                        "ip": clean.ip,
                        "user_agent": clean.user_agent,
                        "prev_hash": prev_hash,
                        "row_hash": row_hash,
                    },
                )
        except SQLAlchemyError as exc:
            msg = f"audit append failed: {type(exc).__name__}"
            raise AuditStoreError(msg) from exc
        return row_hash

    def latest_hash(self, tenant_id: str) -> str:
        try:
            with self._engine.connect() as connection:
                prev = connection.execute(_HEAD, {"tenant_id": tenant_id}).scalar()
        except SQLAlchemyError as exc:
            msg = f"audit head lookup failed: {type(exc).__name__}"
            raise AuditStoreError(msg) from exc
        return str(prev) if prev else GENESIS_HASH

    def list(self, tenant_id: str, cursor: str | None, limit: int) -> PortPage[AuditRow]:
        before_id = int(cursor) if cursor is not None and cursor.isdigit() else None
        if cursor is not None and before_id is None:
            return PortPage(())
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    _PAGE, {"tenant_id": tenant_id, "before_id": before_id, "limit": limit + 1}
                ).all()
        except SQLAlchemyError as exc:
            msg = f"audit page failed: {type(exc).__name__}"
            raise AuditStoreError(msg) from exc
        items = tuple(
            AuditRow(
                id=int(row.id),
                at=row.at.astimezone(UTC),
                actor=row.actor,
                action=row.action,
                target=row.target,
                before=_json_value(row.before),
                after=_json_value(row.after),
                reason=row.reason or "",
                request_id=row.request_id,
                row_hash=row.row_hash,
            )
            for row in rows[:limit]
        )
        more = len(rows) > limit
        return PortPage(items, str(items[-1].id) if items and more else None)

    def ping(self) -> bool:
        return ping_engine(self._engine)


def _walk(engine: Engine, tenant_id: str) -> Iterator[tuple[int, str, str, Mapping[str, Any]]]:
    with engine.connect() as connection:
        for row in connection.execute(_WALK, {"tenant_id": tenant_id}).yield_per(500):
            entry = AuditEntry(
                at=row.at.astimezone(UTC),
                actor=row.actor,
                action=row.action,
                target=row.target,
                before=_json_value(row.before),
                after=_json_value(row.after),
                reason=row.reason or "",
                request_id=row.request_id,
                ip=row.ip,
                user_agent=row.user_agent,
                tenant_id=row.tenant_id,
            )
            yield int(row.id), row.prev_hash, row.row_hash, entry.content()


def verify_audit_log(engine: Engine, tenant_id: str) -> ChainBreak | None:
    """Walk the tenant's chain in id order; ``None`` when intact, else the first break."""
    try:
        return verify_chain(_walk(engine, tenant_id))
    except SQLAlchemyError as exc:
        msg = f"audit verify failed: {type(exc).__name__}"
        raise AuditStoreError(msg) from exc
