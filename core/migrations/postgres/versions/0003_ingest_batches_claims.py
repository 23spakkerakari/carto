"""ingest_batches claims a batch before it is written (ADR 0034)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 00:00:00 UTC

``state`` is ``pending`` while a delivery writes the batch and ``done`` once it is written;
``claimed_at`` says when the current claim was taken, so a claim older than the timeout can be
taken over (``carto_core.ingest.ledger``). Rows written before this revision recorded finished
writes, so they become ``done`` with their ``received_at`` as the claim time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_batches",
        sa.Column("state", sa.String(8), nullable=False, server_default="done"),
    )
    op.add_column(
        "ingest_batches",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE ingest_batches SET claimed_at = received_at")
    op.alter_column("ingest_batches", "claimed_at", nullable=False)
    op.alter_column("ingest_batches", "state", server_default=None)
    op.create_check_constraint(
        "ck_ingest_batches_state", "ingest_batches", "state IN ('pending', 'done')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_ingest_batches_state", "ingest_batches", type_="check")
    op.drop_column("ingest_batches", "claimed_at")
    op.drop_column("ingest_batches", "state")
