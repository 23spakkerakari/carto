"""The core audit log: append-only, hash-chained (spec 14.9; plan M2 decision 9).

Each row stores ``prev_hash`` and ``row_hash = SHA-256(prev_hash || canonical_json(row))``,
where the canonical JSON is the row's content (:class:`AuditEntry` without the hashes) with
sorted keys, no whitespace and UTF-8. :func:`chain_hash` is the one definition both the writer
and ``carto-core audit verify`` use. The first row chains from :data:`GENESIS_HASH`.

``before`` and ``after`` never hold secrets or raw values: callers pass view dictionaries built
from metadata (a link's status and score, an entity's name), and :func:`scrub` drops any key
that names a secret or a token as a backstop. The application role has INSERT and SELECT only
on ``audit_log`` and a trigger rejects UPDATE and DELETE (migration 0004).

A human decision and its audit row commit together (spec 2.3 invariant 6; ADR 0070): the
store that owns the state change takes a :data:`TransactionHook` and calls it with its open
connection and the stored row, and the hook appends through
:meth:`TransactionalAuditRecorder.append_on` on that connection, so a failed append rolls the
decision back with it. Rows of actions nobody is signed in for (a refused login) carry
:data:`ANONYMOUS_ACTOR`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy import Connection

__all__ = [
    "ANONYMOUS_ACTOR",
    "GENESIS_HASH",
    "SECRET_KEY_MARKERS",
    "AuditEntry",
    "AuditRecorder",
    "ChainBreak",
    "TransactionHook",
    "TransactionalAuditRecorder",
    "canonical_json",
    "chain_hash",
    "scrub",
    "verify_chain",
]

GENESIS_HASH: Final = "0" * 64
ANONYMOUS_ACTOR: Final = "anonymous"
"""The ``actor`` of a row written for a request with no signed-in principal (spec 14.9 wants
an actor on every row; a refused login has none)."""
SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "secret",
    "password",
    "token",
    "key",
    "credential",
    "authorization",
    "cookie",
)
"""A ``before``/``after`` key containing one of these (case-insensitive) is dropped."""
MAX_FIELD_LEN: Final = 4096


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One audit row's content. ``id`` and the hashes are assigned by the recorder."""

    at: datetime
    actor: str
    action: str
    target: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    reason: str
    request_id: str | None
    ip: str | None
    user_agent: str | None
    tenant_id: str = "default"

    def content(self) -> dict[str, Any]:
        """The fields that are hashed, in a JSON-safe form."""
        return {
            "tenant_id": self.tenant_id,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "before": None if self.before is None else dict(self.before),
            "after": None if self.after is None else dict(self.after),
            "reason": self.reason,
            "request_id": self.request_id,
            "ip": self.ip,
            "user_agent": self.user_agent,
        }


def scrub(values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop keys that name secrets or tokens and bound every string (backstop, spec 14.9)."""
    if values is None:
        return None
    clean: dict[str, Any] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(marker in lowered for marker in SECRET_KEY_MARKERS):
            continue
        clean[key] = (
            value[:MAX_FIELD_LEN]
            if isinstance(value, str) and len(value) > MAX_FIELD_LEN
            else value
        )
    return clean


def canonical_json(content: Mapping[str, Any]) -> bytes:
    return json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def chain_hash(prev_hash: str, content: Mapping[str, Any]) -> str:
    """``SHA-256(prev_hash || canonical_json(content))`` as lower-case hex."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(canonical_json(content))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ChainBreak:
    row_id: int
    expected: str
    found: str


def verify_chain(rows: Iterable[tuple[int, str, str, Mapping[str, Any]]]) -> ChainBreak | None:
    """Walk ``(id, prev_hash, row_hash, content)`` rows in id order; return the first break."""
    previous = GENESIS_HASH
    for row_id, prev_hash, row_hash, content in rows:
        if prev_hash != previous:
            return ChainBreak(row_id, previous, prev_hash)
        expected = chain_hash(prev_hash, content)
        if expected != row_hash:
            return ChainBreak(row_id, expected, row_hash)
        previous = row_hash
    return None


@runtime_checkable
class AuditRecorder(Protocol):
    """What mutating handlers and the linker call. The PostgreSQL implementation serializes
    appends (one chain per tenant) and the in-memory one backs the unit tests."""

    def record(self, entry: AuditEntry) -> str:
        """Append; return the new ``row_hash``."""
        ...

    def latest_hash(self, tenant_id: str) -> str:
        """The chain head (``GENESIS_HASH`` when empty): the daily anchor (spec 14.9)."""
        ...


@runtime_checkable
class TransactionalAuditRecorder(AuditRecorder, Protocol):
    """A recorder that can also append inside a transaction another store opened, so the
    state change and its row commit or roll back together (ADR 0070)."""

    def append_on(self, connection: Connection | None, entry: AuditEntry) -> str:
        """Append on ``connection`` (the caller's open transaction, which the caller commits)
        and return the new ``row_hash``; ``None`` appends in a transaction of the recorder's
        own, like :meth:`AuditRecorder.record`."""
        ...


type TransactionHook[T] = Callable[[Connection | None, T], None]
"""What a store that changes state runs inside its transaction, after the write and before the
commit, with the connection and the stored row (``None`` for the in-memory stores, which have
no connection). An exception from the hook rolls the write back."""
