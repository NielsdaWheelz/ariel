"""make turn-linked ledgers restrictive

Revision ID: 20260514_0029
Revises: 20260514_0028
Create Date: 2026-05-14 14:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260514_0029"
down_revision = "20260514_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "turn_idempotency_keys_session_id_fkey",
        "turn_idempotency_keys",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "turn_idempotency_keys_session_id_fkey",
        "turn_idempotency_keys",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("action_attempts_turn_id_fkey", "action_attempts", type_="foreignkey")
    op.create_foreign_key(
        "action_attempts_turn_id_fkey",
        "action_attempts",
        "turns",
        ["turn_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "turn_idempotency_keys_turn_id_fkey",
        "turn_idempotency_keys",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "turn_idempotency_keys_turn_id_fkey",
        "turn_idempotency_keys",
        "turns",
        ["turn_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "turn_idempotency_keys_turn_id_fkey",
        "turn_idempotency_keys",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "turn_idempotency_keys_turn_id_fkey",
        "turn_idempotency_keys",
        "turns",
        ["turn_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "turn_idempotency_keys_session_id_fkey",
        "turn_idempotency_keys",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "turn_idempotency_keys_session_id_fkey",
        "turn_idempotency_keys",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("action_attempts_turn_id_fkey", "action_attempts", type_="foreignkey")
    op.create_foreign_key(
        "action_attempts_turn_id_fkey",
        "action_attempts",
        "turns",
        ["turn_id"],
        ["id"],
        ondelete="CASCADE",
    )
