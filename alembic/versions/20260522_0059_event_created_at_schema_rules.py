"""add event created_at schema rules

Revision ID: 20260522_0059
Revises: 20260521_0058
Create Date: 2026-05-22 00:59:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260522_0059"
down_revision = "20260521_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("provider_events", "agency_events"):
        op.add_column(
            table_name,
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.execute(f"UPDATE {table_name} SET created_at = received_at")
        op.alter_column(table_name, "created_at", nullable=False)


def downgrade() -> None:
    op.drop_column("agency_events", "created_at")
    op.drop_column("provider_events", "created_at")
