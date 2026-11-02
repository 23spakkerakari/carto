"""map discovery: templates, fields, sketches, key links, entities, jobs, audit, identity,
inventory (spec 7.3, 14.9; ADR 0050, 0051, 0056; plan M2 package A)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 00:00:00 UTC

Every table carries ``tenant_id``, ``created_at`` and ``updated_at`` (spec 7.3) except
``audit_log``, which is append-only and has no ``updated_at``: a trigger raises on ``UPDATE``,
``DELETE`` and ``TRUNCATE`` (spec 14.9). ``fields.id`` is the ``field_ref``
``system_id/template_id/path``; the edge owns its class and policy columns and the profiler the
statistics (ADR 0056). ``key_links`` carries the review state machine of spec 9.6 with a
``version`` that ``key_link_decisions`` records per decision. ``sessions.id`` is the SHA-256 of
the cookie value, never the value (spec 14.1). ``llm_calls.id`` is a ULID the gateway sets
(package G), not a sequence; its audit columns default server-side because the gateway's sink
inserts only its own columns. ``sources`` gains the inventory columns of ADR
0050; the FK to ``systems`` stays, so the inventory upsert writes systems first.

Grants (spec 2.3 invariant 9, 14.4 per-service users) are applied only when the role exists:
``carto_app`` and ``carto_worker`` read and write the metadata tables but may only ``INSERT``
and ``SELECT`` on ``audit_log``; ``carto_ingest`` writes templates, fields, systems, sources,
the inventories, source health and the batch ledger and nothing else. Every statement here is a
fixed string: no value is interpolated.

The columns the models hold as optional are nullable: ``entities.name`` and ``description`` (an
entity carries only a suggested name until a reviewer confirms one), ``key_links.note`` and
``first_proposed_at``, ``key_link_decisions.note`` and ``field_sketches.first_seen``. This
revision was edited in place for that after M2's first CI run (2026-10-19): it had been applied
only to the CI containers, never to a database that is kept.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINK_STATUSES = ("proposed", "accepted", "rejected", "auto_accepted", "stale")
ROLES = ("admin", "integration_engineer", "business_viewer", "auditor")

APP_ROLES = ("carto_app", "carto_worker")
APP_TABLES = (
    "systems",
    "sources",
    "source_health",
    "templates",
    "fields",
    "field_sketches",
    "key_links",
    "key_link_decisions",
    "composite_rules",
    "entities",
    "entity_fields",
    "jobs",
    "users",
    "groups",
    "role_bindings",
    "sessions",
    "settings",
    "llm_calls",
    "edge_inventories",
)
APP_SEQUENCES = (
    "key_link_decisions_id_seq",
    "jobs_id_seq",
    "audit_log_id_seq",
    "role_bindings_id_seq",
)
INGEST_ROLE = "carto_ingest"
INGEST_TABLES = (
    "templates",
    "fields",
    "systems",
    "sources",
    "edge_inventories",
    "source_health",
    "ingest_batches",
)


def _audit_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _tenant() -> sa.Column[Any]:
    return sa.Column("tenant_id", sa.String(64), nullable=False)


def _tstz(name: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _guarded(role: str, statements: Sequence[str]) -> str:
    """A ``DO`` block that runs ``statements`` only when ``role`` exists (fixed strings)."""
    body = " ".join(statements)
    guard = f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608  # nosec B608 - role is one of the fixed role names
    return guard + body + " END IF; END $$;"


def _grant_statements() -> list[str]:
    app_tables = ", ".join(APP_TABLES)
    sequences = ", ".join(APP_SEQUENCES)
    out = [
        _guarded(
            role,
            [
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {app_tables} TO {role};",
                f"GRANT SELECT ON ingest_batches TO {role};",
                f"GRANT SELECT, INSERT ON audit_log TO {role};",
                f"GRANT USAGE ON SEQUENCE {sequences} TO {role};",
            ],
        )
        for role in APP_ROLES
    ]
    ingest_tables = ", ".join(INGEST_TABLES)
    out.append(
        _guarded(
            INGEST_ROLE,
            [f"GRANT SELECT, INSERT, UPDATE ON {ingest_tables} TO {INGEST_ROLE};"],
        )
    )
    return out


def _revoke_statements() -> list[str]:
    app_tables = ", ".join(APP_TABLES)
    sequences = ", ".join(APP_SEQUENCES)
    out = [
        _guarded(
            role,
            [
                f"REVOKE ALL ON {app_tables} FROM {role};",
                f"REVOKE ALL ON ingest_batches FROM {role};",
                f"REVOKE ALL ON audit_log FROM {role};",
                f"REVOKE ALL ON SEQUENCE {sequences} FROM {role};",
            ],
        )
        for role in APP_ROLES
    ]
    ingest_tables = ", ".join(INGEST_TABLES)
    out.append(
        _guarded(INGEST_ROLE, [f"REVOKE ALL ON {ingest_tables} FROM {INGEST_ROLE};"]),
    )
    return out


AUDIT_TRIGGER_FUNCTION = (
    "CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION 'audit_log is append-only (spec 14.9)' "
    "USING ERRCODE = 'insufficient_privilege'; "
    "RETURN NULL; END $$;"
)
AUDIT_TRIGGER_ROWS = (
    "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
    "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();"
)
AUDIT_TRIGGER_TRUNCATE = (
    "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
    "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();"
)


def upgrade() -> None:
    op.create_table(
        "templates",
        _tenant(),
        sa.Column("system_id", sa.String(64), nullable=False),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("event_type_label", sa.String(128), nullable=True),
        _tstz("first_seen", nullable=False),
        _tstz("last_seen", nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "system_id", "id", name="pk_templates"),
    )
    op.create_index("ix_templates_tenant_id", "templates", ["tenant_id"])

    op.create_table(
        "fields",
        _tenant(),
        sa.Column("id", sa.String(512), nullable=False),
        sa.Column("system_id", sa.String(64), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("path", sa.String(256), nullable=False),
        sa.Column("class", sa.String(32), nullable=False),
        sa.Column("policy", sa.String(16), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "forms", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("distinct_estimate", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("null_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "shapes_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("quality", sa.Float(), nullable=True),
        sa.Column("is_candidate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column("suggested_label", sa.String(128), nullable=True),
        _tstz("first_seen", nullable=False),
        _tstz("last_seen", nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_fields"),
    )
    op.create_index("ix_fields_tenant_id", "fields", ["tenant_id"])
    op.create_index("ix_fields_tenant_system", "fields", ["tenant_id", "system_id"])
    op.create_index(
        "ix_fields_tenant_template", "fields", ["tenant_id", "system_id", "template_id"]
    )

    op.create_table(
        "field_sketches",
        _tenant(),
        sa.Column("field_id", sa.String(512), nullable=False),
        sa.Column("form", sa.String(32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "shape_counts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "length_counts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("minhash", postgresql.BYTEA(), nullable=False),
        sa.Column("hll", postgresql.BYTEA(), nullable=False),
        sa.Column("bottom_k", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "top_token_counts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _tstz("first_seen", nullable=True),
        _tstz("last_seen", nullable=True),
        sa.Column("sketch_version", sa.Integer(), nullable=False, server_default="1"),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "field_id", "form", "day", name="pk_field_sketches"),
    )
    op.create_index("ix_field_sketches_tenant_id", "field_sketches", ["tenant_id"])
    op.create_index("ix_field_sketches_tenant_day", "field_sketches", ["tenant_id", "day"])

    op.create_table(
        "key_links",
        _tenant(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("field_a", sa.String(512), nullable=False),
        sa.Column("form_a", sa.String(32), nullable=False),
        sa.Column("field_b", sa.String(512), nullable=False),
        sa.Column("form_b", sa.String(32), nullable=False),
        sa.Column("link_type", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("reject_reason", sa.String(32), nullable=True),
        sa.Column("decided_by", sa.String(256), nullable=True),
        _tstz("decided_at", nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("affected_volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ambiguous_merges", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("baseline_match_rate", sa.Float(), nullable=True),
        _tstz("first_proposed_at", nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_key_links"),
        sa.UniqueConstraint(
            "tenant_id", "field_a", "form_a", "field_b", "form_b", name="uq_key_links_ends"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'rejected', 'auto_accepted', 'stale')",
            name="ck_key_links_status",
        ),
    )
    op.create_index("ix_key_links_tenant_id", "key_links", ["tenant_id"])
    op.create_index("ix_key_links_tenant_status", "key_links", ["tenant_id", "status"])
    op.create_index("ix_key_links_tenant_field_a", "key_links", ["tenant_id", "field_a"])
    op.create_index("ix_key_links_tenant_field_b", "key_links", ["tenant_id", "field_b"])

    op.create_table(
        "key_link_decisions",
        sa.Column("id", sa.BigInteger()),
        _tenant(),
        sa.Column("key_link_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reject_reason", sa.String(32), nullable=True),
        sa.Column("decided_by", sa.String(256), nullable=False),
        _tstz("decided_at", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("score_at_decision", sa.Float(), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_key_link_decisions"),
    )
    op.create_index("ix_key_link_decisions_tenant_id", "key_link_decisions", ["tenant_id"])
    op.create_index(
        "ix_key_link_decisions_link", "key_link_decisions", ["tenant_id", "key_link_id", "version"]
    )

    op.create_table(
        "composite_rules",
        _tenant(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("key_link_id", sa.String(64), nullable=False),
        sa.Column(
            "components_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("estimated_precision", sa.Float(), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_composite_rules"),
    )
    op.create_index("ix_composite_rules_tenant_id", "composite_rules", ["tenant_id"])
    op.create_index("ix_composite_rules_link", "composite_rules", ["tenant_id", "key_link_id"])

    op.create_table(
        "entities",
        _tenant(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("suggested_name", sa.String(128), nullable=True),
        sa.Column("name_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_entities"),
    )
    op.create_index("ix_entities_tenant_id", "entities", ["tenant_id"])

    op.create_table(
        "entity_fields",
        _tenant(),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("field_id", sa.String(512), nullable=False),
        sa.Column("form", sa.String(32), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint(
            "tenant_id", "entity_id", "field_id", "form", name="pk_entity_fields"
        ),
    )
    op.create_index("ix_entity_fields_tenant_id", "entity_fields", ["tenant_id"])
    op.create_index("ix_entity_fields_field", "entity_fields", ["tenant_id", "field_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger()),
        _tenant(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        _tstz("run_after", nullable=False),
        _tstz("leased_until", nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(256), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_tenant_kind_run_after", "jobs", ["tenant_id", "kind", "run_after"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger()),
        _tenant(),
        _tstz("at", nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("prev_hash", sa.CHAR(64), nullable=False),
        sa.Column("row_hash", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_tenant_at", "audit_log", ["tenant_id", "at"])
    op.execute(AUDIT_TRIGGER_FUNCTION)
    op.execute(AUDIT_TRIGGER_ROWS)
    op.execute(AUDIT_TRIGGER_TRUNCATE)

    op.create_table(
        "users",
        _tenant(),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(256), nullable=True),
        _tstz("last_login_at", nullable=True),
        sa.Column("reveal_granted", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "subject", name="pk_users"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "groups",
        _tenant(),
        sa.Column("name", sa.String(256), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "name", name="pk_groups"),
    )
    op.create_index("ix_groups_tenant_id", "groups", ["tenant_id"])

    op.create_table(
        "role_bindings",
        sa.Column("id", sa.BigInteger()),
        _tenant(),
        sa.Column("group_name", sa.String(256), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("scope_json", postgresql.JSONB(), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_role_bindings"),
        sa.UniqueConstraint("tenant_id", "group_name", "role", name="uq_role_bindings"),
        sa.CheckConstraint(
            "role IN ('admin', 'integration_engineer', 'business_viewer', 'auditor')",
            name="ck_role_bindings_role",
        ),
    )
    op.create_index("ix_role_bindings_tenant_id", "role_bindings", ["tenant_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), nullable=False, comment="SHA-256 hex of the cookie value."),
        _tenant(),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        _tstz("authenticated_at", nullable=False),
        _tstz("last_seen_at", nullable=False),
        _tstz("expires_at", nullable=False),
        sa.Column(
            "roles_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index("ix_sessions_tenant_id", "sessions", ["tenant_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "settings",
        _tenant(),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "key", name="pk_settings"),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(26), nullable=False, comment="ULID set by the gateway."),
        _tenant(),
        _tstz("at", nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.CHAR(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_class", sa.String(64), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
    )
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])
    op.create_index("ix_llm_calls_tenant_at", "llm_calls", ["tenant_id", "at"])

    op.create_table(
        "edge_inventories",
        _tenant(),
        sa.Column("edge_id", sa.String(128), nullable=False),
        sa.Column("sources_file_sha256", sa.CHAR(64), nullable=False),
        _tstz("sent_at", nullable=False),
        _tstz("received_at", nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "edge_id", name="pk_edge_inventories"),
    )

    op.add_column(
        "sources", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("sources", sa.Column("backfill_days", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("schedule", sa.String(128), nullable=True))
    op.add_column("sources", sa.Column("last_test", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("edge_id", sa.String(128), nullable=True))

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    for statement in _revoke_statements():
        op.execute(statement)
    for column in ("edge_id", "last_test_at", "last_test", "schedule", "backfill_days", "enabled"):
        op.drop_column("sources", column)
    op.drop_table("edge_inventories")
    op.drop_index("ix_llm_calls_tenant_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_tenant_id", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("settings")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_tenant_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_role_bindings_tenant_id", table_name="role_bindings")
    op.drop_table("role_bindings")
    op.drop_index("ix_groups_tenant_id", table_name="groups")
    op.drop_table("groups")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;")
    op.drop_index("ix_audit_log_tenant_at", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only();")
    op.drop_index("ix_jobs_tenant_kind_run_after", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_entity_fields_field", table_name="entity_fields")
    op.drop_index("ix_entity_fields_tenant_id", table_name="entity_fields")
    op.drop_table("entity_fields")
    op.drop_index("ix_entities_tenant_id", table_name="entities")
    op.drop_table("entities")
    op.drop_index("ix_composite_rules_link", table_name="composite_rules")
    op.drop_index("ix_composite_rules_tenant_id", table_name="composite_rules")
    op.drop_table("composite_rules")
    op.drop_index("ix_key_link_decisions_link", table_name="key_link_decisions")
    op.drop_index("ix_key_link_decisions_tenant_id", table_name="key_link_decisions")
    op.drop_table("key_link_decisions")
    op.drop_index("ix_key_links_tenant_field_b", table_name="key_links")
    op.drop_index("ix_key_links_tenant_field_a", table_name="key_links")
    op.drop_index("ix_key_links_tenant_status", table_name="key_links")
    op.drop_index("ix_key_links_tenant_id", table_name="key_links")
    op.drop_table("key_links")
    op.drop_index("ix_field_sketches_tenant_day", table_name="field_sketches")
    op.drop_index("ix_field_sketches_tenant_id", table_name="field_sketches")
    op.drop_table("field_sketches")
    op.drop_index("ix_fields_tenant_template", table_name="fields")
    op.drop_index("ix_fields_tenant_system", table_name="fields")
    op.drop_index("ix_fields_tenant_id", table_name="fields")
    op.drop_table("fields")
    op.drop_index("ix_templates_tenant_id", table_name="templates")
    op.drop_table("templates")
