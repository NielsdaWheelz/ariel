"""add memory_export_artifacts.artifact_kind and widen export_format

Revision ID: 20260516_0033
Revises: 20260516_0032
Create Date: 2026-05-16 11:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260516_0033"
down_revision = "20260516_0032"
branch_labels = None
depends_on = None


_FORMAT_BEFORE = "export_format IN ('json')"
_FORMAT_AFTER = "export_format IN ('json', 'markdown')"
_KIND_CHECK = "artifact_kind IN ('memory_snapshot', 'agents_md')"


def upgrade() -> None:
    op.add_column(
        "memory_export_artifacts",
        sa.Column(
            "artifact_kind",
            sa.String(length=32),
            nullable=False,
            server_default="memory_snapshot",
        ),
    )
    op.alter_column("memory_export_artifacts", "artifact_kind", server_default=None)
    op.create_check_constraint(
        "ck_memory_export_artifact_kind", "memory_export_artifacts", _KIND_CHECK
    )
    op.drop_constraint("ck_memory_export_artifact_format", "memory_export_artifacts", type_="check")
    op.create_check_constraint(
        "ck_memory_export_artifact_format", "memory_export_artifacts", _FORMAT_AFTER
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_export_artifact_format", "memory_export_artifacts", type_="check")
    op.create_check_constraint(
        "ck_memory_export_artifact_format", "memory_export_artifacts", _FORMAT_BEFORE
    )
    op.drop_constraint("ck_memory_export_artifact_kind", "memory_export_artifacts", type_="check")
    op.drop_column("memory_export_artifacts", "artifact_kind")
