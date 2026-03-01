"""initial ariel schema

Revision ID: 20260301_0001
Revises:
Create Date: 2026-03-01 06:23:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260301_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_single_active_session",
        "sessions",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "turns",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column("assistant_message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('in_progress', 'completed', 'failed')", name="ck_turn_status"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_turns_created_at", "turns", ["created_at"], unique=False)
    op.create_index("ix_turns_session_id", "turns", ["session_id"], unique=False)

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_event_sequence_positive"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_created_at", "events", ["created_at"], unique=False)
    op.create_index("ix_events_session_id", "events", ["session_id"], unique=False)
    op.create_index("ix_events_turn_id", "events", ["turn_id"], unique=False)
    op.create_index("ix_turn_sequence_unique", "events", ["turn_id", "sequence"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_turn_sequence_unique", table_name="events")
    op.drop_index("ix_events_turn_id", table_name="events")
    op.drop_index("ix_events_session_id", table_name="events")
    op.drop_index("ix_events_created_at", table_name="events")
    op.drop_table("events")

    op.drop_index("ix_turns_session_id", table_name="turns")
    op.drop_index("ix_turns_created_at", table_name="turns")
    op.drop_table("turns")

    op.drop_index("ix_single_active_session", table_name="sessions")
    op.drop_table("sessions")
