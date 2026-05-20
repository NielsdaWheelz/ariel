"""add subscriber_heartbeat table

Revision ID: 20260520_0055
Revises: 20260520_0054
Create Date: 2026-05-20 00:55:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260520_0055"
down_revision = "20260520_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriber_heartbeat",
        sa.Column("subscriber_name", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_flight_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("errors_in_window", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("subscriber_name"),
    )


def downgrade() -> None:
    op.drop_table("subscriber_heartbeat")
