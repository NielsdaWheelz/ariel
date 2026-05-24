"""add source background task identity to turns

Revision ID: 20260524_0066
Revises: 20260523_0065
Create Date: 2026-05-24 13:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260524_0066"
down_revision = "20260523_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column("source_background_task_id", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_turns_source_background_task_unique",
        "turns",
        ["source_background_task_id"],
        unique=True,
        postgresql_where=sa.text("source_background_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_turns_source_background_task_unique", table_name="turns")
    op.drop_column("turns", "source_background_task_id")
