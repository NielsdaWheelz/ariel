"""add durable capture ingress records

Revision ID: 20260313_0008
Revises: 20260306_0007
Create Date: 2026-03-13 12:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260313_0008"
down_revision = "20260306_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "captures",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("capture_kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("original_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized_turn_input", sa.Text(), nullable=True),
        sa.Column("effective_session_id", sa.String(length=32), nullable=True),
        sa.Column("turn_id", sa.String(length=32), nullable=True),
        sa.Column("terminal_state", sa.String(length=32), nullable=False),
        sa.Column("ingest_error_code", sa.String(length=64), nullable=True),
        sa.Column("ingest_error_message", sa.Text(), nullable=True),
        sa.Column("ingest_error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ingest_error_retryable", sa.Boolean(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "capture_kind IN ('text', 'url', 'unknown')",
            name="ck_capture_kind",
        ),
        sa.CheckConstraint(
            "terminal_state IN ('turn_created', 'ingest_failed')",
            name="ck_capture_terminal_state",
        ),
        sa.CheckConstraint(
            (
                "(terminal_state = 'turn_created' "
                "AND turn_id IS NOT NULL "
                "AND effective_session_id IS NOT NULL "
                "AND normalized_turn_input IS NOT NULL "
                "AND ingest_error_code IS NULL "
                "AND ingest_error_message IS NULL "
                "AND ingest_error_details IS NULL "
                "AND ingest_error_retryable IS NULL) "
                "OR "
                "(terminal_state = 'ingest_failed' "
                "AND turn_id IS NULL "
                "AND effective_session_id IS NULL "
                "AND normalized_turn_input IS NULL "
                "AND ingest_error_code IS NOT NULL "
                "AND ingest_error_message IS NOT NULL "
                "AND ingest_error_details IS NOT NULL "
                "AND ingest_error_retryable IS NOT NULL)"
            ),
            name="ck_capture_terminal_linkage",
        ),
        sa.ForeignKeyConstraint(["effective_session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_captures_effective_session_id",
        "captures",
        ["effective_session_id"],
        unique=False,
    )
    op.create_index("ix_captures_turn_id", "captures", ["turn_id"], unique=False)
    op.create_index("ix_captures_created_at", "captures", ["created_at"], unique=False)
    op.create_index("ix_captures_updated_at", "captures", ["updated_at"], unique=False)
    op.create_index(
        "ix_captures_idempotency_key_unique",
        "captures",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_captures_idempotency_key_unique", table_name="captures")
    op.drop_index("ix_captures_updated_at", table_name="captures")
    op.drop_index("ix_captures_created_at", table_name="captures")
    op.drop_index("ix_captures_turn_id", table_name="captures")
    op.drop_index("ix_captures_effective_session_id", table_name="captures")
    op.drop_table("captures")
