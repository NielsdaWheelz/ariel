"""allow shared_content capture kind

Revision ID: 20260313_0009
Revises: 20260313_0008
Create Date: 2026-03-13 18:20:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260313_0009"
down_revision = "20260313_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_capture_kind", "captures", type_="check")
    op.create_check_constraint(
        "ck_capture_kind",
        "captures",
        "capture_kind IN ('text', 'url', 'shared_content', 'unknown')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_capture_kind", "captures", type_="check")
    # Downgrading to the prior enum set requires coercing shared-content rows.
    op.execute("UPDATE captures SET capture_kind = 'unknown' WHERE capture_kind = 'shared_content'")
    op.create_check_constraint(
        "ck_capture_kind",
        "captures",
        "capture_kind IN ('text', 'url', 'unknown')",
    )
