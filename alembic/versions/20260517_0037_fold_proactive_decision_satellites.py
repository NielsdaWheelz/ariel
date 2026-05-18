"""fold proactive_context_snapshots and proactive_policy_validations into proactive_decisions

Revision ID: 20260517_0037
Revises: 20260517_0036
Create Date: 2026-05-18 00:36:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260517_0037"
down_revision = "20260517_0036"
branch_labels = None
depends_on = None


_POLICY_RESULT_VALUES = (
    "'authorized', 'authorized_with_constraints', 'denied', "
    "'needs_user_authority', 'stale_context', 'invalid_decision', "
    "'duplicate', 'dead_letter'"
)


def upgrade() -> None:
    op.add_column(
        "proactive_decisions",
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("model_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("omitted_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("context_taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("policy_result", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("policy_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("action_plan_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("policy_constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("denial_reason", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_proactive_decision_policy_result",
        "proactive_decisions",
        f"policy_result IN ({_POLICY_RESULT_VALUES})",
    )

    op.drop_index("ix_proactive_decisions_context_snapshot_id", table_name="proactive_decisions")
    op.drop_column("proactive_decisions", "context_snapshot_id")

    op.drop_index(
        "ix_proactive_action_plans_policy_validation_id", table_name="proactive_action_plans"
    )
    op.drop_column("proactive_action_plans", "policy_validation_id")

    op.drop_index(
        "ix_proactive_policy_validations_decision", table_name="proactive_policy_validations"
    )
    op.drop_index(
        "ix_proactive_policy_validations_decision_id",
        table_name="proactive_policy_validations",
    )
    op.drop_index(
        "ix_proactive_policy_validations_created_at",
        table_name="proactive_policy_validations",
    )
    op.drop_index(
        "ix_proactive_policy_validations_case_id", table_name="proactive_policy_validations"
    )
    op.drop_table("proactive_policy_validations")

    op.drop_index(
        "ix_proactive_context_snapshots_created_at",
        table_name="proactive_context_snapshots",
    )
    op.drop_index(
        "ix_proactive_context_snapshots_case_id", table_name="proactive_context_snapshots"
    )
    op.drop_index(
        "ix_proactive_context_snapshots_case_created",
        table_name="proactive_context_snapshots",
    )
    op.drop_table("proactive_context_snapshots")


def downgrade() -> None:
    op.create_table(
        "proactive_context_snapshots",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("snapshot_key", sa.String(length=220), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("omitted_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_key"),
    )
    op.create_index(
        "ix_proactive_context_snapshots_case_created",
        "proactive_context_snapshots",
        ["case_id", "created_at"],
    )
    op.create_index(
        "ix_proactive_context_snapshots_case_id", "proactive_context_snapshots", ["case_id"]
    )
    op.create_index(
        "ix_proactive_context_snapshots_created_at",
        "proactive_context_snapshots",
        ["created_at"],
    )

    op.create_table(
        "proactive_policy_validations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("action_plan_hash", sa.String(length=128), nullable=True),
        sa.Column("constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"result IN ({_POLICY_RESULT_VALUES})",
            name="ck_proactive_policy_validation_result",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["proactive_decisions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_policy_validations_case_id",
        "proactive_policy_validations",
        ["case_id"],
    )
    op.create_index(
        "ix_proactive_policy_validations_created_at",
        "proactive_policy_validations",
        ["created_at"],
    )
    op.create_index(
        "ix_proactive_policy_validations_decision",
        "proactive_policy_validations",
        ["decision_id", "created_at"],
    )
    op.create_index(
        "ix_proactive_policy_validations_decision_id",
        "proactive_policy_validations",
        ["decision_id"],
    )

    op.add_column(
        "proactive_action_plans",
        sa.Column("policy_validation_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_proactive_action_plans_policy_validation_id",
        "proactive_action_plans",
        "proactive_policy_validations",
        ["policy_validation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_proactive_action_plans_policy_validation_id",
        "proactive_action_plans",
        ["policy_validation_id"],
    )

    op.add_column(
        "proactive_decisions",
        sa.Column("context_snapshot_id", sa.String(length=32), nullable=False),
    )
    op.create_foreign_key(
        "fk_proactive_decisions_context_snapshot_id",
        "proactive_decisions",
        "proactive_context_snapshots",
        ["context_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_proactive_decisions_context_snapshot_id",
        "proactive_decisions",
        ["context_snapshot_id"],
    )

    op.drop_constraint("ck_proactive_decision_policy_result", "proactive_decisions", type_="check")
    op.drop_column("proactive_decisions", "denial_reason")
    op.drop_column("proactive_decisions", "policy_constraints")
    op.drop_column("proactive_decisions", "action_plan_hash")
    op.drop_column("proactive_decisions", "policy_version")
    op.drop_column("proactive_decisions", "policy_result")
    op.drop_column("proactive_decisions", "context_taint")
    op.drop_column("proactive_decisions", "omitted_context")
    op.drop_column("proactive_decisions", "model_input")
    op.drop_column("proactive_decisions", "context")
