"""add email decluttering action and thread-watch state

Revision ID: 20260501_0022
Revises: 20260501_0021
Create Date: 2026-05-01 06:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0022"
down_revision = "20260501_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_actions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approval_id", sa.String(length=32), nullable=True),
        sa.Column("provider_message_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_thread_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("intended_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("undo_token_hash", sa.String(length=64), nullable=True),
        sa.Column("undo_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_attempts", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_email_action_provider"),
        sa.CheckConstraint(
            (
                "capability_id IN ('cap.email.archive', 'cap.email.trash', "
                "'cap.email.labels.modify', 'cap.email.undo')"
            ),
            name="ck_email_action_capability",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'executing', 'succeeded', 'failed', 'undone')",
            name="ck_email_action_status",
        ),
        sa.CheckConstraint(
            "execution_attempts >= 0",
            name="ck_email_action_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["action_attempt_id"],
            ["action_attempts.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provider_message_ids) = 'array'",
            name="ck_email_action_provider_message_ids_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provider_thread_ids) = 'array'",
            name="ck_email_action_provider_thread_ids_array",
        ),
        sa.CheckConstraint(
            (
                "(status = 'failed' AND failure_code IS NOT NULL) OR "
                "(status != 'failed' AND failure_code IS NULL)"
            ),
            name="ck_email_action_failure_code_status",
        ),
        sa.CheckConstraint(
            (
                "(undo_token_hash IS NULL AND undo_expires_at IS NULL) OR "
                "(undo_token_hash IS NOT NULL AND undo_expires_at IS NOT NULL)"
            ),
            name="ck_email_action_undo_fields_paired",
        ),
        sa.CheckConstraint(
            (
                "capability_id = 'cap.email.undo' OR status != 'succeeded' OR "
                "(undo_token_hash IS NOT NULL AND undo_expires_at IS NOT NULL)"
            ),
            name="ck_email_action_succeeded_mutation_has_undo",
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["approval_requests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_actions_idempotency_key_unique",
        "email_actions",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_email_actions_action_attempt_id", "email_actions", ["action_attempt_id"])
    op.create_index("ix_email_actions_approval_id", "email_actions", ["approval_id"])
    op.create_index("ix_email_actions_created_at", "email_actions", ["created_at"])
    op.create_index("ix_email_actions_undo_expires_at", "email_actions", ["undo_expires_at"])
    op.create_index(
        "ix_email_actions_provider_account_status",
        "email_actions",
        ["provider", "provider_account_id", "status", "id"],
    )
    op.create_index(
        "ix_email_actions_undo_token_hash",
        "email_actions",
        ["undo_token_hash"],
        unique=True,
        postgresql_where=sa.text("undo_token_hash IS NOT NULL"),
    )
    op.create_index(
        "ix_email_actions_provider_message_ids",
        "email_actions",
        ["provider_message_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_email_actions_provider_thread_ids",
        "email_actions",
        ["provider_thread_ids"],
        postgresql_using="gin",
    )

    op.create_table(
        "email_thread_watches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=256), nullable=False),
        sa.Column("anchor_message_id", sa.String(length=256), nullable=False),
        sa.Column("condition", sa.String(length=32), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("cancel_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_by_action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("matched_message_id", sa.String(length=256), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_email_thread_watch_provider"),
        sa.CheckConstraint(
            "condition IN ('no_reply_by_deadline', 'any_reply_arrives')",
            name="ck_email_thread_watch_condition",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'due', 'completed', 'canceled', 'failed')",
            name="ck_email_thread_watch_status",
        ),
        sa.CheckConstraint(
            (
                "(matched_message_id IS NULL AND matched_at IS NULL) OR "
                "(matched_message_id IS NOT NULL AND matched_at IS NOT NULL)"
            ),
            name="ck_email_thread_watch_matched_fields_paired",
        ),
        sa.CheckConstraint(
            (
                "(status IN ('active', 'due', 'failed') "
                "AND canceled_at IS NULL "
                "AND completed_at IS NULL) OR "
                "(status = 'canceled' "
                "AND canceled_at IS NOT NULL "
                "AND completed_at IS NULL) OR "
                "(status = 'completed' "
                "AND completed_at IS NOT NULL "
                "AND canceled_at IS NULL "
                "AND matched_message_id IS NOT NULL "
                "AND matched_at IS NOT NULL)"
            ),
            name="ck_email_thread_watch_status_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_action_attempt_id"],
            ["action_attempts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_thread_watches_idempotency_key_unique",
        "email_thread_watches",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_email_thread_watches_cancel_idempotency_key_unique",
        "email_thread_watches",
        ["cancel_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("cancel_idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_email_thread_watches_created_by_action_attempt_id",
        "email_thread_watches",
        ["created_by_action_attempt_id"],
    )
    op.create_index("ix_email_thread_watches_created_at", "email_thread_watches", ["created_at"])
    op.create_index("ix_email_thread_watches_deadline", "email_thread_watches", ["deadline"])
    op.create_index(
        "ix_email_thread_watches_active_thread",
        "email_thread_watches",
        ["provider", "provider_account_id", "provider_thread_id", "condition", "deadline"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_email_thread_watches_provider_thread_status",
        "email_thread_watches",
        ["provider", "provider_account_id", "provider_thread_id", "status"],
    )
    op.create_index(
        "ix_email_thread_watches_due",
        "email_thread_watches",
        ["status", "deadline", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_thread_watches_due", table_name="email_thread_watches")
    op.drop_index(
        "ix_email_thread_watches_provider_thread_status",
        table_name="email_thread_watches",
    )
    op.drop_index("ix_email_thread_watches_active_thread", table_name="email_thread_watches")
    op.drop_index("ix_email_thread_watches_deadline", table_name="email_thread_watches")
    op.drop_index("ix_email_thread_watches_created_at", table_name="email_thread_watches")
    op.drop_index(
        "ix_email_thread_watches_cancel_idempotency_key_unique",
        table_name="email_thread_watches",
    )
    op.drop_index(
        "ix_email_thread_watches_idempotency_key_unique",
        table_name="email_thread_watches",
    )
    op.drop_index(
        "ix_email_thread_watches_created_by_action_attempt_id",
        table_name="email_thread_watches",
    )
    op.drop_table("email_thread_watches")

    op.drop_index("ix_email_actions_idempotency_key_unique", table_name="email_actions")
    op.drop_index("ix_email_actions_undo_token_hash", table_name="email_actions")
    op.drop_index("ix_email_actions_provider_thread_ids", table_name="email_actions")
    op.drop_index("ix_email_actions_provider_message_ids", table_name="email_actions")
    op.drop_index("ix_email_actions_provider_account_status", table_name="email_actions")
    op.drop_index("ix_email_actions_undo_expires_at", table_name="email_actions")
    op.drop_index("ix_email_actions_created_at", table_name="email_actions")
    op.drop_index("ix_email_actions_approval_id", table_name="email_actions")
    op.drop_index("ix_email_actions_action_attempt_id", table_name="email_actions")
    op.drop_table("email_actions")
