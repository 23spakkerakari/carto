"""trace: transactions, the assembly index, batch keys, nodes, hops, terminal nodes, entity
statistics; entities gain a version and a max span (spec 7.3, 9.7, 9.8; ADR 0060, 0061, 0064,
0066; plan M3-demo package A-lite)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02 00:00:00 UTC

Every table carries ``tenant_id`` (spec 5.5, 22.4; first in every primary key except the
``txn_merges`` history, keyed by its sequence), ``created_at`` and ``updated_at``.
``transactions.id`` is a ULID (``carto_common.ids``; spec 7.3 writes ``txn_id``, the column is
``id`` like every other table's key); ``merged_into`` names the survivor of a merge and stays
set, so the old id answers 303 (M3 plan decision 2). ``txn_merges.token_end`` names the
``field_ref/form`` whose token caused a merge for the review screen (ADR 0061), never the
token. ``entity_stats`` holds the per-entity quiet span of spec 9.8 (ADR 0061).
``txn_token_index`` is the union-find index of spec 9.7 (one row per live token);
``txn_token_fanout`` counts distinct transactions per token and day for hub detection;
``batch_keys`` holds the tokens that never merge (batch-role link ends and detected hubs).
``nodes`` is one row per template (ADR 0066) with a real ``version`` for its ETag; ``hops``
is the rollup the API reads (ADR 0061); ``terminal_nodes`` and ``entity_stats`` are per
entity (spec 9.8). ``entities.version`` is bumped by reviewer patches only and
``entities.max_span_days`` is null until a reviewer sets one (ADR 0064).

Grants follow 0004's pattern and are applied only when the role exists: ``carto_worker``
writes every new table; ``carto_app`` reads them and may update the three columns of ``nodes``
the rename route sets; ``carto_ingest`` gets nothing here. Every statement is a fixed string.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER_ROLE = "carto_worker"
APP_ROLE = "carto_app"
NEW_TABLES = (
    "transactions",
    "txn_token_index",
    "txn_token_fanout",
    "txn_merges",
    "batch_keys",
    "nodes",
    "hops",
    "terminal_nodes",
    "entity_stats",
)
NEW_SEQUENCES = ("txn_merges_id_seq",)
TXN_STATUSES = ("in_progress", "completed", "errored", "stalled")


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
    tables = ", ".join(NEW_TABLES)
    sequences = ", ".join(NEW_SEQUENCES)
    return [
        _guarded(
            WORKER_ROLE,
            [
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO {WORKER_ROLE};",
                f"GRANT USAGE ON SEQUENCE {sequences} TO {WORKER_ROLE};",
            ],
        ),
        _guarded(
            APP_ROLE,
            [
                f"GRANT SELECT ON {tables} TO {APP_ROLE};",
                f"GRANT UPDATE (display_name, version, updated_at) ON nodes TO {APP_ROLE};",
            ],
        ),
    ]


def _revoke_statements() -> list[str]:
    tables = ", ".join(NEW_TABLES)
    sequences = ", ".join(NEW_SEQUENCES)
    return [
        _guarded(
            WORKER_ROLE,
            [
                f"REVOKE ALL ON {tables} FROM {WORKER_ROLE};",
                f"REVOKE ALL ON SEQUENCE {sequences} FROM {WORKER_ROLE};",
            ],
        ),
        _guarded(APP_ROLE, [f"REVOKE ALL ON {tables} FROM {APP_ROLE};"]),
    ]


def upgrade() -> None:
    op.add_column(
        "entities", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("entities", sa.Column("max_span_days", sa.Integer(), nullable=True))

    op.create_table(
        "transactions",
        _tenant(),
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        _tstz("first_seen", nullable=False),
        _tstz("last_seen", nullable=False),
        sa.Column("current_node", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="in_progress"),
        sa.Column("event_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("merged_into", sa.String(26), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_transactions"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in TXN_STATUSES) + ")",
            name="ck_transactions_status",
        ),
    )
    op.create_index("ix_transactions_tenant_entity", "transactions", ["tenant_id", "entity_id"])
    op.create_index("ix_transactions_tenant_merged", "transactions", ["tenant_id", "merged_into"])
    op.create_index(
        "ix_transactions_tenant_status_seen", "transactions", ["tenant_id", "status", "last_seen"]
    )

    op.create_table(
        "txn_token_index",
        _tenant(),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("txn_id", sa.String(26), nullable=False),
        _tstz("last_seen", nullable=False),
        _tstz("expires_at", nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "token", name="pk_txn_token_index"),
    )
    op.create_index("ix_txn_token_index_tenant_txn", "txn_token_index", ["tenant_id", "txn_id"])
    op.create_index(
        "ix_txn_token_index_tenant_expires", "txn_token_index", ["tenant_id", "expires_at"]
    )

    op.create_table(
        "txn_token_fanout",
        _tenant(),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("txn_count", sa.Integer(), nullable=False, server_default="0"),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "token", "day", name="pk_txn_token_fanout"),
    )

    op.create_table(
        "txn_merges",
        sa.Column("id", sa.BigInteger()),
        _tenant(),
        sa.Column("from_txn", sa.String(26), nullable=False),
        sa.Column("to_txn", sa.String(26), nullable=False),
        _tstz("merged_at", nullable=False),
        sa.Column("token_end", sa.String(512), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_txn_merges"),
    )
    op.create_index("ix_txn_merges_tenant_from", "txn_merges", ["tenant_id", "from_txn"])

    op.create_table(
        "batch_keys",
        _tenant(),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("field_ref", sa.String(512), nullable=False),
        sa.Column("fanout", sa.Integer(), nullable=False, server_default="0"),
        _tstz("first_seen", nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "token", name="pk_batch_keys"),
    )

    op.create_table(
        "nodes",
        _tenant(),
        sa.Column("id", sa.String(256), nullable=False),
        sa.Column("system_id", sa.String(64), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("event_type_label", sa.String(128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _tstz("first_seen", nullable=True),
        _tstz("last_seen", nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_nodes"),
    )
    op.create_index("ix_nodes_tenant_system", "nodes", ["tenant_id", "system_id"])

    op.create_table(
        "hops",
        _tenant(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("from_node", sa.String(256), nullable=False),
        sa.Column("to_node", sa.String(256), nullable=False),
        sa.Column("support", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("p_next", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_p50", sa.Float(), nullable=True),
        sa.Column("latency_p95", sa.Float(), nullable=True),
        sa.Column("latency_p99", sa.Float(), nullable=True),
        sa.Column("error_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("human_actor_share", sa.Float(), nullable=True),
        sa.Column(
            "link_types",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_manual_score", sa.Float(), nullable=True),
        sa.Column("is_manual_confirmed", sa.Boolean(), nullable=True),
        _tstz("window_start", nullable=False),
        _tstz("window_end", nullable=False),
        _tstz("computed_at", nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_hops"),
    )
    op.create_index("ix_hops_tenant_entity", "hops", ["tenant_id", "entity_id"])
    op.create_index("ix_hops_tenant_from", "hops", ["tenant_id", "from_node"])

    op.create_table(
        "terminal_nodes",
        _tenant(),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(256), nullable=False),
        sa.Column("share", sa.Float(), nullable=False),
        _tstz("computed_at", nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "entity_id", "node_id", name="pk_terminal_nodes"),
    )

    op.create_table(
        "entity_stats",
        _tenant(),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("p99_gap_seconds", sa.Float(), nullable=True),
        sa.Column("quiet_span_seconds", sa.Float(), nullable=False),
        sa.Column("settled_count", sa.BigInteger(), nullable=False, server_default="0"),
        _tstz("computed_at", nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "entity_id", name="pk_entity_stats"),
    )

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    for statement in _revoke_statements():
        op.execute(statement)
    op.drop_table("entity_stats")
    op.drop_table("terminal_nodes")
    op.drop_index("ix_hops_tenant_from", table_name="hops")
    op.drop_index("ix_hops_tenant_entity", table_name="hops")
    op.drop_table("hops")
    op.drop_index("ix_nodes_tenant_system", table_name="nodes")
    op.drop_table("nodes")
    op.drop_table("batch_keys")
    op.drop_index("ix_txn_merges_tenant_from", table_name="txn_merges")
    op.drop_table("txn_merges")
    op.drop_table("txn_token_fanout")
    op.drop_index("ix_txn_token_index_tenant_expires", table_name="txn_token_index")
    op.drop_index("ix_txn_token_index_tenant_txn", table_name="txn_token_index")
    op.drop_table("txn_token_index")
    op.drop_index("ix_transactions_tenant_status_seen", table_name="transactions")
    op.drop_index("ix_transactions_tenant_merged", table_name="transactions")
    op.drop_index("ix_transactions_tenant_entity", table_name="transactions")
    op.drop_table("transactions")
    op.drop_column("entities", "max_span_days")
    op.drop_column("entities", "version")
