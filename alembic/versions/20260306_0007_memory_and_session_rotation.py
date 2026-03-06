"""add durable memories and session rotation metadata

Revision ID: 20260306_0007
Revises: 20260303_0006
Create Date: 2026-03-06 20:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260306_0007"
down_revision = "20260303_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.execute("UPDATE sessions SET lifecycle_state = 'closed' WHERE is_active IS FALSE")
    op.add_column("sessions", sa.Column("rotated_from_session_id", sa.String(length=32), nullable=True))
    op.add_column("sessions", sa.Column("rotation_reason", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        "fk_sessions_rotated_from_session_id",
        "sessions",
        "sessions",
        ["rotated_from_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_sessions_rotated_from_session_id",
        "sessions",
        ["rotated_from_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_rotated_from_session_id_unique",
        "sessions",
        ["rotated_from_session_id"],
        unique=True,
        postgresql_where=sa.text("rotated_from_session_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_session_rotation_reason",
        "sessions",
        (
            "(rotation_reason IS NULL) OR "
            "(rotation_reason IN ('user_initiated', 'threshold_turn_count', "
            "'threshold_age', 'threshold_context_pressure'))"
        ),
    )
    op.create_check_constraint(
        "ck_session_lifecycle_state",
        "sessions",
        "lifecycle_state IN ('active', 'rotating', 'closed', 'recovery_needed')",
    )
    op.create_check_constraint(
        "ck_session_lifecycle_matches_is_active",
        "sessions",
        (
            "(is_active IS TRUE AND lifecycle_state = 'active') OR "
            "(is_active IS FALSE AND lifecycle_state IN ('rotating', 'closed', 'recovery_needed'))"
        ),
    )
    op.create_check_constraint(
        "ck_session_rotation_fields_paired",
        "sessions",
        (
            "(rotation_reason IS NULL AND rotated_from_session_id IS NULL) OR "
            "(rotation_reason IS NOT NULL AND rotated_from_session_id IS NOT NULL)"
        ),
    )

    op.create_table(
        "session_rotations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("rotated_from_session_id", sa.String(length=32), nullable=False),
        sa.Column("rotated_to_session_id", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "reason IN ('user_initiated', 'threshold_turn_count', "
                "'threshold_age', 'threshold_context_pressure')"
            ),
            name="ck_session_rotation_reason_type",
        ),
        sa.ForeignKeyConstraint(
            ["rotated_from_session_id"],
            ["sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rotated_to_session_id"],
            ["sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rotated_to_session_id", name="uq_session_rotations_rotated_to"),
    )
    op.create_index(
        "ix_session_rotations_rotated_from_session_id",
        "session_rotations",
        ["rotated_from_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_rotations_idempotency_key",
        "session_rotations",
        ["idempotency_key"],
        unique=False,
    )
    op.create_index(
        "ix_session_rotations_idempotency_key_unique",
        "session_rotations",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "turn_idempotency_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_turn_idempotency_keys_session_id",
        "turn_idempotency_keys",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_turn_idempotency_keys_turn_id",
        "turn_idempotency_keys",
        ["turn_id"],
        unique=False,
    )
    op.create_index(
        "ix_turn_idempotency_keys_created_at",
        "turn_idempotency_keys",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_turn_idempotency_keys_updated_at",
        "turn_idempotency_keys",
        ["updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_turn_idempotency_session_key_unique",
        "turn_idempotency_keys",
        ["session_id", "idempotency_key"],
        unique=True,
    )

    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("memory_class", sa.String(length=32), nullable=False),
        sa.Column("memory_key", sa.Text(), nullable=False),
        sa.Column("active_revision_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "memory_class IN "
                "('profile', 'preference', 'project', 'commitment', 'episodic_summary')"
            ),
            name="ck_memory_item_class",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_items_class_key_unique",
        "memory_items",
        ["memory_class", "memory_key"],
        unique=True,
    )
    op.create_index(
        "ix_memory_items_active_revision_id",
        "memory_items",
        ["active_revision_id"],
        unique=False,
    )

    op.create_table(
        "memory_revisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("memory_item_id", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("source_session_id", sa.String(length=32), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('candidate', 'validated', 'superseded', 'retracted')",
            name="ck_memory_revision_lifecycle_state",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_revision_confidence_range",
        ),
        sa.CheckConstraint(
            (
                "(lifecycle_state = 'retracted' AND value IS NULL) OR "
                "(lifecycle_state <> 'retracted' AND value IS NOT NULL)"
            ),
            name="ck_memory_revision_value_presence",
        ),
        sa.ForeignKeyConstraint(
            ["memory_item_id"],
            ["memory_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_revisions_memory_item_id",
        "memory_revisions",
        ["memory_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_revisions_source_turn_id",
        "memory_revisions",
        ["source_turn_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_revisions_source_session_id",
        "memory_revisions",
        ["source_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_revisions_last_verified_at",
        "memory_revisions",
        ["last_verified_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_revisions_created_at",
        "memory_revisions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_revisions_item_created",
        "memory_revisions",
        ["memory_item_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_turn_idempotency_session_key_unique", table_name="turn_idempotency_keys")
    op.drop_index("ix_turn_idempotency_keys_updated_at", table_name="turn_idempotency_keys")
    op.drop_index("ix_turn_idempotency_keys_created_at", table_name="turn_idempotency_keys")
    op.drop_index("ix_turn_idempotency_keys_turn_id", table_name="turn_idempotency_keys")
    op.drop_index("ix_turn_idempotency_keys_session_id", table_name="turn_idempotency_keys")
    op.drop_table("turn_idempotency_keys")

    op.drop_index("ix_memory_revisions_item_created", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_created_at", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_last_verified_at", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_source_session_id", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_source_turn_id", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_memory_item_id", table_name="memory_revisions")
    op.drop_table("memory_revisions")

    op.drop_index("ix_memory_items_active_revision_id", table_name="memory_items")
    op.drop_index("ix_memory_items_class_key_unique", table_name="memory_items")
    op.drop_table("memory_items")

    op.drop_index(
        "ix_session_rotations_idempotency_key_unique",
        table_name="session_rotations",
    )
    op.drop_index("ix_session_rotations_idempotency_key", table_name="session_rotations")
    op.drop_index(
        "ix_session_rotations_rotated_from_session_id",
        table_name="session_rotations",
    )
    op.drop_table("session_rotations")

    op.drop_constraint("ck_session_lifecycle_matches_is_active", "sessions", type_="check")
    op.drop_constraint("ck_session_lifecycle_state", "sessions", type_="check")
    op.drop_constraint("ck_session_rotation_fields_paired", "sessions", type_="check")
    op.drop_constraint("ck_session_rotation_reason", "sessions", type_="check")
    op.drop_index("ix_sessions_rotated_from_session_id_unique", table_name="sessions")
    op.drop_index("ix_sessions_rotated_from_session_id", table_name="sessions")
    op.drop_constraint("fk_sessions_rotated_from_session_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "rotation_reason")
    op.drop_column("sessions", "rotated_from_session_id")
    op.drop_column("sessions", "lifecycle_state")
