"""narrow capture ingress schema to durable records

Revision ID: 20260522_0063
Revises: 20260522_0062
Create Date: 2026-05-22 03:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260522_0063"
down_revision = "20260522_0062"
branch_labels = None
depends_on = None


_OLD_CAPTURE_LINKAGE_CHECK = (
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
)


def upgrade() -> None:
    invalid_count = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT count(*)
            FROM captures
            WHERE capture_kind NOT IN ('text', 'url', 'shared_content')
               OR terminal_state <> 'turn_created'
               OR turn_id IS NULL
               OR effective_session_id IS NULL
               OR normalized_turn_input IS NULL
               OR status_code <> 200
            """
            )
        )
        .scalar_one()
    )
    if invalid_count:
        raise RuntimeError("capture rows must be repaired before capture schema narrowing")

    op.drop_constraint("ck_capture_terminal_linkage", "captures", type_="check")
    op.drop_constraint("ck_capture_terminal_state", "captures", type_="check")
    op.drop_constraint("ck_capture_kind", "captures", type_="check")

    op.alter_column("captures", "normalized_turn_input", existing_type=sa.Text(), nullable=False)
    op.alter_column(
        "captures",
        "effective_session_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.alter_column("captures", "turn_id", existing_type=sa.String(length=32), nullable=False)

    op.drop_column("captures", "original_payload")
    op.drop_column("captures", "terminal_state")
    op.drop_column("captures", "ingest_error_code")
    op.drop_column("captures", "ingest_error_message")
    op.drop_column("captures", "ingest_error_details")
    op.drop_column("captures", "ingest_error_retryable")
    op.drop_column("captures", "status_code")
    op.drop_column("captures", "response_payload")

    op.create_check_constraint(
        "ck_capture_kind",
        "captures",
        "capture_kind IN ('text', 'url', 'shared_content')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_capture_kind", "captures", type_="check")

    op.add_column(
        "captures",
        sa.Column(
            "original_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "captures",
        sa.Column(
            "terminal_state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'turn_created'"),
        ),
    )
    op.add_column("captures", sa.Column("ingest_error_code", sa.String(length=64), nullable=True))
    op.add_column("captures", sa.Column("ingest_error_message", sa.Text(), nullable=True))
    op.add_column(
        "captures",
        sa.Column(
            "ingest_error_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("captures", sa.Column("ingest_error_retryable", sa.Boolean(), nullable=True))
    op.add_column(
        "captures",
        sa.Column("status_code", sa.Integer(), nullable=False, server_default=sa.text("200")),
    )
    op.add_column(
        "captures",
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.alter_column("captures", "original_payload", server_default=None)
    op.alter_column("captures", "terminal_state", server_default=None)
    op.alter_column("captures", "status_code", server_default=None)
    op.alter_column("captures", "response_payload", server_default=None)

    op.alter_column("captures", "normalized_turn_input", existing_type=sa.Text(), nullable=True)
    op.alter_column(
        "captures",
        "effective_session_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.alter_column("captures", "turn_id", existing_type=sa.String(length=32), nullable=True)

    op.create_check_constraint(
        "ck_capture_kind",
        "captures",
        "capture_kind IN ('text', 'url', 'shared_content', 'unknown')",
    )
    op.create_check_constraint(
        "ck_capture_terminal_state",
        "captures",
        "terminal_state IN ('turn_created', 'ingest_failed')",
    )
    op.create_check_constraint(
        "ck_capture_terminal_linkage",
        "captures",
        _OLD_CAPTURE_LINKAGE_CHECK,
    )
