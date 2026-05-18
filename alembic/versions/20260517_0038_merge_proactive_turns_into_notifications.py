"""merge proactive_turns into notifications

Revision ID: 20260517_0038
Revises: 20260517_0037
Create Date: 2026-05-18 00:37:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260517_0038"
down_revision = "20260517_0037"
branch_labels = None
depends_on = None


_PROACTIVE_SHAPE_CHECK = (
    "(source_type = 'proactive_turn') = "
    "(proactive_case_id IS NOT NULL AND proactive_decision_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("proactive_case_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column("proactive_decision_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_notifications_proactive_case_id",
        "notifications",
        "proactive_cases",
        ["proactive_case_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_notifications_proactive_decision_id",
        "notifications",
        "proactive_decisions",
        ["proactive_decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_notifications_proactive_case_id", "notifications", ["proactive_case_id"])
    op.create_index(
        "ix_notifications_proactive_decision_id", "notifications", ["proactive_decision_id"]
    )
    op.create_check_constraint(
        "ck_notification_proactive_shape",
        "notifications",
        _PROACTIVE_SHAPE_CHECK,
    )
    op.alter_column(
        "notifications",
        "dedupe_key",
        existing_type=sa.String(length=160),
        type_=sa.String(length=220),
    )

    op.drop_index("ix_proactive_turns_updated_at", table_name="proactive_turns")
    op.drop_index("ix_proactive_turns_status_updated", table_name="proactive_turns")
    op.drop_index("ix_proactive_turns_decision_id", table_name="proactive_turns")
    op.drop_index("ix_proactive_turns_created_at", table_name="proactive_turns")
    op.drop_index("ix_proactive_turns_case_id", table_name="proactive_turns")
    op.drop_table("proactive_turns")


def downgrade() -> None:
    op.create_table(
        "proactive_turns",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivery_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'acknowledged', 'failed', 'cancelled')",
            name="ck_proactive_turn_status",
        ),
        sa.CheckConstraint("origin IN ('proactive')", name="ck_proactive_turn_origin"),
        sa.CheckConstraint("channel IN ('discord')", name="ck_proactive_turn_channel"),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["proactive_decisions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_proactive_turns_case_id", "proactive_turns", ["case_id"])
    op.create_index("ix_proactive_turns_created_at", "proactive_turns", ["created_at"])
    op.create_index("ix_proactive_turns_decision_id", "proactive_turns", ["decision_id"])
    op.create_index(
        "ix_proactive_turns_status_updated", "proactive_turns", ["status", "updated_at"]
    )
    op.create_index("ix_proactive_turns_updated_at", "proactive_turns", ["updated_at"])

    op.alter_column(
        "notifications",
        "dedupe_key",
        existing_type=sa.String(length=220),
        type_=sa.String(length=160),
    )
    op.drop_constraint("ck_notification_proactive_shape", "notifications", type_="check")
    op.drop_index("ix_notifications_proactive_decision_id", table_name="notifications")
    op.drop_index("ix_notifications_proactive_case_id", table_name="notifications")
    op.drop_constraint(
        "fk_notifications_proactive_decision_id", "notifications", type_="foreignkey"
    )
    op.drop_constraint("fk_notifications_proactive_case_id", "notifications", type_="foreignkey")
    op.drop_column("notifications", "proactive_decision_id")
    op.drop_column("notifications", "proactive_case_id")
