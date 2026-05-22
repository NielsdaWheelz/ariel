"""add subscriber heartbeat schema rules

Revision ID: 20260521_0058
Revises: 20260521_0057
Create Date: 2026-05-21 00:58:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260521_0058"
down_revision = "20260521_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_heartbeat",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.alter_column("subscriber_heartbeat", "created_at", server_default=None)
    op.create_check_constraint(
        "ck_subscriber_heartbeat_in_flight_count_nonnegative",
        "subscriber_heartbeat",
        "in_flight_count >= 0",
    )
    op.create_check_constraint(
        "ck_subscriber_heartbeat_errors_in_window_nonnegative",
        "subscriber_heartbeat",
        "errors_in_window >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_subscriber_heartbeat_errors_in_window_nonnegative",
        "subscriber_heartbeat",
        type_="check",
    )
    op.drop_constraint(
        "ck_subscriber_heartbeat_in_flight_count_nonnegative",
        "subscriber_heartbeat",
        type_="check",
    )
    op.drop_column("subscriber_heartbeat", "created_at")
