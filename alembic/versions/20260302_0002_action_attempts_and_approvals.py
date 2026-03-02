"""add action attempts and approval requests

Revision ID: 20260302_0002
Revises: 20260301_0001
Create Date: 2026-03-02 20:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260302_0002"
down_revision = "20260301_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_attempts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("proposal_index", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=32), nullable=False),
        sa.Column("impact_level", sa.String(length=32), nullable=False),
        sa.Column("proposed_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_decision", sa.String(length=32), nullable=False),
        sa.Column("policy_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("execution_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("execution_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "proposal_index > 0",
            name="ck_action_attempt_proposal_index_positive",
        ),
        sa.CheckConstraint(
            (
                "impact_level IN ('read', 'write_reversible', "
                "'write_irreversible', 'external_send')"
            ),
            name="ck_action_attempt_impact_level",
        ),
        sa.CheckConstraint(
            (
                "status IN ('proposed', 'rejected', 'awaiting_approval', 'approved', "
                "'denied', 'expired', 'executing', 'succeeded', 'failed')"
            ),
            name="ck_action_attempt_status",
        ),
        sa.CheckConstraint(
            "policy_decision IN ('allow_inline', 'requires_approval', 'deny')",
            name="ck_action_attempt_policy_decision",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_attempts_session_id", "action_attempts", ["session_id"], unique=False)
    op.create_index("ix_action_attempts_turn_id", "action_attempts", ["turn_id"], unique=False)
    op.create_index("ix_action_attempts_created_at", "action_attempts", ["created_at"], unique=False)
    op.create_index(
        "ix_turn_proposal_index_unique",
        "action_attempts",
        ["turn_id", "proposal_index"],
        unique=True,
    )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_request_status",
        ),
        sa.ForeignKeyConstraint(["action_attempt_id"], ["action_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_requests_action_attempt_id",
        "approval_requests",
        ["action_attempt_id"],
        unique=True,
    )
    op.create_index("ix_approval_requests_session_id", "approval_requests", ["session_id"], unique=False)
    op.create_index("ix_approval_requests_turn_id", "approval_requests", ["turn_id"], unique=False)
    op.create_index("ix_approval_requests_expires_at", "approval_requests", ["expires_at"], unique=False)
    op.create_index("ix_approval_requests_created_at", "approval_requests", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_approval_requests_created_at", table_name="approval_requests")
    op.drop_index("ix_approval_requests_expires_at", table_name="approval_requests")
    op.drop_index("ix_approval_requests_turn_id", table_name="approval_requests")
    op.drop_index("ix_approval_requests_session_id", table_name="approval_requests")
    op.drop_index("ix_approval_requests_action_attempt_id", table_name="approval_requests")
    op.drop_table("approval_requests")

    op.drop_index("ix_turn_proposal_index_unique", table_name="action_attempts")
    op.drop_index("ix_action_attempts_created_at", table_name="action_attempts")
    op.drop_index("ix_action_attempts_turn_id", table_name="action_attempts")
    op.drop_index("ix_action_attempts_session_id", table_name="action_attempts")
    op.drop_table("action_attempts")
