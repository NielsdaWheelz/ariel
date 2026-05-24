"""make async message idempotency durable

Revision ID: 20260524_0067
Revises: 20260524_0066
Create Date: 2026-05-24 16:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260524_0067"
down_revision = "20260524_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "turn_idempotency_keys",
        "turn_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.add_column(
        "turn_idempotency_keys",
        sa.Column("background_task_id", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_turn_idempotency_background_task_unique",
        "turn_idempotency_keys",
        ["background_task_id"],
        unique=True,
        postgresql_where=sa.text("background_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_turn_idempotency_background_task_unique",
        table_name="turn_idempotency_keys",
    )
    op.drop_column("turn_idempotency_keys", "background_task_id")
    op.execute("DELETE FROM turn_idempotency_keys WHERE turn_id IS NULL")
    op.alter_column(
        "turn_idempotency_keys",
        "turn_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
