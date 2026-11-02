"""grants: the ledger role may DELETE its ledger, the worker loses the identity tables,
llm_calls is append-only (spec 2.3 invariant 9, 9.9, 14.4; ADR 0071; plan
security-fixes-2026-11 package G)

Revision ID: 0006
Revises: 0004
Create Date: 2026-11-02 00:00:00 UTC

Migration 0004 gave ``carto_ingest`` ``SELECT, INSERT, UPDATE`` on ``ingest_batches``, but the
ledger's ``release`` (after a failed ClickHouse write) and ``prune`` (hourly) are ``DELETE FROM
ingest_batches`` (``carto_core.ingest.ledger``, ADR 0043 section 10), and both callers log the
refusal and carry on: in the pilot stack every failed write left a claim nobody could release
and the pruner never deleted a row. The grant follows the code: ``DELETE`` on ``ingest_batches``
alone, still nothing on ``audit_log`` and no ``DELETE`` on the metadata tables.

``carto_worker`` had every metadata table of 0004, the identity tables among them. The worker
(profiler, linker, namer) never reads ``users``, ``groups``, ``role_bindings`` or ``sessions``;
only the console api does. They are revoked from it. It keeps ``settings`` whole: it reads the
engine thresholds (ADR 0055) and the profiler upserts its watermark there. The plan also names
``login_attempts``; no such table exists (the api keeps login attempts in its session store),
so a migration that creates one grants it to ``carto_app`` alone.

``llm_calls`` is the audit of LLM calls (spec 9.9: "every call is audited"). Nothing updates or
deletes a row (the gateway's sink only inserts), so it becomes append-only like ``audit_log``:
``SELECT, INSERT`` for both console roles and a ``BEFORE UPDATE OR DELETE`` plus ``BEFORE
TRUNCATE`` trigger pair that refuses the owner too. 0004's trigger function names
``audit_log`` in its message, so this file carries its own.

Grants and revokes are applied only when the role exists, as in 0004, and every statement is a
fixed string: no value is interpolated (spec 14.7). Downgrade restores exactly 0004's grants.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0006"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLES: Final = ("carto_app", "carto_worker")
WORKER_ROLE: Final = "carto_worker"
INGEST_ROLE: Final = "carto_ingest"
IDENTITY_TABLES: Final = ("users", "groups", "role_bindings", "sessions")
"""The identity tables of 0004 (spec 14.1, 14.2); only the console api touches them."""

LLM_TRIGGER_FUNCTION: Final = (
    "CREATE OR REPLACE FUNCTION llm_calls_append_only() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION 'llm_calls is append-only (spec 9.9)' "
    "USING ERRCODE = 'insufficient_privilege'; "
    "RETURN NULL; END $$;"
)
LLM_TRIGGER_ROWS: Final = (
    "CREATE TRIGGER llm_calls_no_update_delete BEFORE UPDATE OR DELETE ON llm_calls "
    "FOR EACH ROW EXECUTE FUNCTION llm_calls_append_only();"
)
LLM_TRIGGER_TRUNCATE: Final = (
    "CREATE TRIGGER llm_calls_no_truncate BEFORE TRUNCATE ON llm_calls "
    "FOR EACH STATEMENT EXECUTE FUNCTION llm_calls_append_only();"
)
LLM_TRIGGER_DROPS: Final = (
    "DROP TRIGGER IF EXISTS llm_calls_no_truncate ON llm_calls;",
    "DROP TRIGGER IF EXISTS llm_calls_no_update_delete ON llm_calls;",
    "DROP FUNCTION IF EXISTS llm_calls_append_only();",
)


def _guarded(role: str, statements: Sequence[str]) -> str:
    """A ``DO`` block that runs ``statements`` only when ``role`` exists (fixed strings); the
    same shape as 0004's, repeated because Alembic scripts do not import one another."""
    body = " ".join(statements)
    guard = f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608  # nosec B608 - role is one of the fixed role names
    return guard + body + " END IF; END $$;"


def _grant_statements() -> list[str]:
    identity = ", ".join(IDENTITY_TABLES)
    out: list[str] = []
    for role in APP_ROLES:
        statements = [
            f"REVOKE ALL ON llm_calls FROM {role};",
            f"GRANT SELECT, INSERT ON llm_calls TO {role};",
        ]
        if role == WORKER_ROLE:
            statements.append(f"REVOKE ALL ON {identity} FROM {role};")
        out.append(_guarded(role, statements))
    out.append(_guarded(INGEST_ROLE, [f"GRANT DELETE ON ingest_batches TO {INGEST_ROLE};"]))
    return out


def _revoke_statements() -> list[str]:
    """Back to 0004: full metadata grants on ``llm_calls`` and, for the worker, the identity
    tables; no ``DELETE`` on the ledger for ``carto_ingest``."""
    identity = ", ".join(IDENTITY_TABLES)
    out: list[str] = []
    for role in APP_ROLES:
        statements = [
            f"REVOKE ALL ON llm_calls FROM {role};",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON llm_calls TO {role};",
        ]
        if role == WORKER_ROLE:
            statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {identity} TO {role};")
        out.append(_guarded(role, statements))
    out.append(_guarded(INGEST_ROLE, [f"REVOKE DELETE ON ingest_batches FROM {INGEST_ROLE};"]))
    return out


def upgrade() -> None:
    op.execute(LLM_TRIGGER_FUNCTION)
    op.execute(LLM_TRIGGER_ROWS)
    op.execute(LLM_TRIGGER_TRUNCATE)
    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    for statement in _revoke_statements():
        op.execute(statement)
    for statement in LLM_TRIGGER_DROPS:
        op.execute(statement)
