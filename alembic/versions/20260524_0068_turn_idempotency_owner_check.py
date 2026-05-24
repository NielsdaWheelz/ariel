"""require message idempotency owner

Revision ID: 20260524_0068
Revises: 20260524_0067
Create Date: 2026-05-24 18:10:00
"""

from __future__ import annotations

from alembic import op


revision = "20260524_0068"
down_revision = "20260524_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM turn_idempotency_keys WHERE turn_id IS NULL AND background_task_id IS NULL"
    )
    op.create_check_constraint(
        "ck_turn_idempotency_has_owner",
        "turn_idempotency_keys",
        "turn_id IS NOT NULL OR background_task_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_turn_idempotency_has_owner",
        "turn_idempotency_keys",
        type_="check",
    )
