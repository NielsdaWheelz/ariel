"""add the kind column to turns

Revision ID: 20260520_0052
Revises: 20260520_0051
Create Date: 2026-05-20 00:52:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260520_0052"
down_revision = "20260520_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column("kind", sa.String(32), nullable=False, server_default="agent_turn"),
    )
    op.create_check_constraint("ck_turn_kind", "turns", "kind IN ('agent_turn', 'research')")


def downgrade() -> None:
    op.drop_constraint("ck_turn_kind", "turns", type_="check")
    op.drop_column("turns", "kind")
