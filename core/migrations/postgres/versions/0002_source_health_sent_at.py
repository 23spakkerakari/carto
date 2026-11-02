"""source_health.sent_at orders heartbeat upserts

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12 00:00:00 UTC

Two heartbeats of one source can be in flight together and commit out of order. The upsert
now replaces a row only when the stored ``sent_at`` is not newer than the incoming one
(``carto_core.ingest.health``). Rows written before this revision have no ``sent_at`` and are
replaced by the next heartbeat.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_health",
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source_health", "sent_at")
