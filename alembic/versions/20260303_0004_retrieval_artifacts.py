"""add retrieval provenance artifacts

Revision ID: 20260303_0004
Revises: 20260302_0003
Create Date: 2026-03-03 16:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260303_0004"
down_revision = "20260302_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "artifact_type IN ('retrieval_provenance')",
            name="ck_artifact_type",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_attempt_id"], ["action_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_session_id", "artifacts", ["session_id"], unique=False)
    op.create_index("ix_artifacts_turn_id", "artifacts", ["turn_id"], unique=False)
    op.create_index(
        "ix_artifacts_action_attempt_id", "artifacts", ["action_attempt_id"], unique=False
    )
    op.create_index("ix_artifacts_retrieved_at", "artifacts", ["retrieved_at"], unique=False)
    op.create_index("ix_artifacts_created_at", "artifacts", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_artifacts_created_at", table_name="artifacts")
    op.drop_index("ix_artifacts_retrieved_at", table_name="artifacts")
    op.drop_index("ix_artifacts_action_attempt_id", table_name="artifacts")
    op.drop_index("ix_artifacts_turn_id", table_name="artifacts")
    op.drop_index("ix_artifacts_session_id", table_name="artifacts")
    op.drop_table("artifacts")
