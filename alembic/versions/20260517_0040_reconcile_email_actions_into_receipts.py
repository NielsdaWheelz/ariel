"""reconcile email_actions into provider_write_receipts

Revision ID: 20260517_0040
Revises: 20260517_0039
Create Date: 2026-05-18 00:40:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260517_0040"
down_revision = "20260517_0039"
branch_labels = None
depends_on = None


_EMAIL_MUTATION_CAPABILITIES = (
    "capability_id IN ('cap.email.archive', 'cap.email.trash', "
    "'cap.email.labels.modify', 'cap.email.undo')"
)
_UNDO_FIELDS_NULL = (
    "before_state IS NULL AND after_state IS NULL AND "
    "undo_token_hash IS NULL AND undo_expires_at IS NULL"
)


def upgrade() -> None:
    op.add_column(
        "provider_write_receipts",
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "provider_write_receipts",
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "provider_write_receipts",
        sa.Column("undo_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "provider_write_receipts",
        sa.Column("undo_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_provider_write_receipt_status", "provider_write_receipts", type_="check")
    op.create_check_constraint(
        "ck_provider_write_receipt_status",
        "provider_write_receipts",
        "status IN ('executing', 'succeeded', 'failed', 'ambiguous', 'undone')",
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_undo_fields_email_only",
        "provider_write_receipts",
        f"{_EMAIL_MUTATION_CAPABILITIES} OR ({_UNDO_FIELDS_NULL})",
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_undo_fields_paired",
        "provider_write_receipts",
        "(undo_token_hash IS NULL) = (undo_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_provider_write_receipt_succeeded_mutation_has_undo",
        "provider_write_receipts",
        f"NOT ({_EMAIL_MUTATION_CAPABILITIES}) OR "
        "capability_id = 'cap.email.undo' OR status != 'succeeded' OR "
        "(undo_token_hash IS NOT NULL AND undo_expires_at IS NOT NULL)",
    )
    op.create_index(
        "ix_provider_write_receipts_undo_token_hash",
        "provider_write_receipts",
        ["undo_token_hash"],
        unique=True,
        postgresql_where=sa.text("undo_token_hash IS NOT NULL"),
    )

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


def downgrade() -> None:
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
        sa.ForeignKeyConstraint(
            ["action_attempt_id"],
            ["action_attempts.id"],
            ondelete="RESTRICT",
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

    op.drop_index(
        "ix_provider_write_receipts_undo_token_hash", table_name="provider_write_receipts"
    )
    op.drop_constraint(
        "ck_provider_write_receipt_succeeded_mutation_has_undo",
        "provider_write_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_provider_write_receipt_undo_fields_paired",
        "provider_write_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_provider_write_receipt_undo_fields_email_only",
        "provider_write_receipts",
        type_="check",
    )
    op.drop_constraint("ck_provider_write_receipt_status", "provider_write_receipts", type_="check")
    op.create_check_constraint(
        "ck_provider_write_receipt_status",
        "provider_write_receipts",
        "status IN ('executing', 'succeeded', 'failed', 'ambiguous')",
    )
    op.drop_column("provider_write_receipts", "undo_expires_at")
    op.drop_column("provider_write_receipts", "undo_token_hash")
    op.drop_column("provider_write_receipts", "after_state")
    op.drop_column("provider_write_receipts", "before_state")
