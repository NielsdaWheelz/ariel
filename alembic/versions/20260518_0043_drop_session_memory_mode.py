"""drop sessions.memory_mode

Revision ID: 20260518_0043
Revises: 20260518_0042
Create Date: 2026-05-18 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260518_0043"
down_revision = "20260518_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_session_memory_mode", "sessions", type_="check")
    op.drop_column("sessions", "memory_mode")


def downgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "memory_mode",
            sa.String(length=32),
            nullable=False,
            server_default="normal",
        ),
    )
    op.create_check_constraint(
        "ck_session_memory_mode",
        "sessions",
        "memory_mode IN ('normal', 'temporary', 'no_memory')",
    )
    op.alter_column("sessions", "memory_mode", server_default=None)
