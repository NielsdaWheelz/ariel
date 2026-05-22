"""standardize subscriber heartbeat identity

Revision ID: 20260522_0060
Revises: 20260522_0059
Create Date: 2026-05-22 01:00:00
"""

from __future__ import annotations

import ulid
from alembic import op
import sqlalchemy as sa


revision = "20260522_0060"
down_revision = "20260522_0059"
branch_labels = None
depends_on = None


def _new_id() -> str:
    return f"shb_{ulid.new().str.lower()}"


def upgrade() -> None:
    op.add_column("subscriber_heartbeat", sa.Column("id", sa.String(length=32), nullable=True))

    bind = op.get_bind()
    subscriber_names = list(
        bind.execute(
            sa.text("SELECT subscriber_name FROM subscriber_heartbeat ORDER BY subscriber_name")
        ).scalars()
    )
    for subscriber_name in subscriber_names:
        bind.execute(
            sa.text(
                "UPDATE subscriber_heartbeat SET id = :id WHERE subscriber_name = :subscriber_name"
            ),
            {"id": _new_id(), "subscriber_name": subscriber_name},
        )

    op.alter_column("subscriber_heartbeat", "id", nullable=False)
    op.create_unique_constraint(
        "uq_subscriber_heartbeat_subscriber_name",
        "subscriber_heartbeat",
        ["subscriber_name"],
    )
    op.drop_constraint("subscriber_heartbeat_pkey", "subscriber_heartbeat", type_="primary")
    op.create_primary_key("pk_subscriber_heartbeat", "subscriber_heartbeat", ["id"])


def downgrade() -> None:
    op.drop_constraint("pk_subscriber_heartbeat", "subscriber_heartbeat", type_="primary")
    op.create_primary_key("subscriber_heartbeat_pkey", "subscriber_heartbeat", ["subscriber_name"])
    op.drop_constraint(
        "uq_subscriber_heartbeat_subscriber_name",
        "subscriber_heartbeat",
        type_="unique",
    )
    op.drop_column("subscriber_heartbeat", "id")
