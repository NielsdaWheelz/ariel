"""add action attempt contract hash

Revision ID: 20260302_0003
Revises: 20260302_0002
Create Date: 2026-03-02 21:59:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260302_0003"
down_revision = "20260302_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "action_attempts",
        sa.Column("capability_contract_hash", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE action_attempts SET capability_contract_hash = payload_hash "
        "WHERE capability_contract_hash IS NULL"
    )
    op.alter_column("action_attempts", "capability_contract_hash", nullable=False)


def downgrade() -> None:
    op.drop_column("action_attempts", "capability_contract_hash")
