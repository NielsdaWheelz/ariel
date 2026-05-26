"""index events for recent-events window

Revision ID: 20260525_0069
Revises: 20260524_0068
Create Date: 2026-05-25 22:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260525_0069"
down_revision = "20260524_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_events_event_type_created_at",
        "events",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_events_event_type_created_at", table_name="events")
