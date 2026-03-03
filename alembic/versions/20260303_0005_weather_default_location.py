"""add canonical weather default location state

Revision ID: 20260303_0005
Revises: 20260303_0004
Create Date: 2026-03-03 17:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260303_0005"
down_revision = "20260303_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weather_default_locations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("default_location", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('bootstrap', 'user')",
            name="ck_weather_default_location_source",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_weather_default_locations_created_at",
        "weather_default_locations",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_weather_default_locations_updated_at",
        "weather_default_locations",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_weather_default_locations_updated_at", table_name="weather_default_locations")
    op.drop_index("ix_weather_default_locations_created_at", table_name="weather_default_locations")
    op.drop_table("weather_default_locations")
