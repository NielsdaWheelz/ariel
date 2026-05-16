"""add memory_assertions.invalidated_at transaction-time column

Revision ID: 20260516_0031
Revises: 20260515_0030
Create Date: 2026-05-16 09:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260516_0031"
down_revision = "20260515_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_assertions",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_memory_assertions_invalidated_at",
        "memory_assertions",
        ["invalidated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_assertions_invalidated_at", table_name="memory_assertions")
    op.drop_column("memory_assertions", "invalidated_at")
