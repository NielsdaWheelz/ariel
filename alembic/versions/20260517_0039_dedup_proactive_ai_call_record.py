"""de-duplicate the proactive AI-call record against ai_judgments

Revision ID: 20260517_0039
Revises: 20260517_0038
Create Date: 2026-05-18 00:39:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260517_0039"
down_revision = "20260517_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proactive_decisions",
        sa.Column("ai_judgment_id", sa.String(length=32), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("memory_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        "fk_proactive_decisions_ai_judgment_id",
        "proactive_decisions",
        "ai_judgments",
        ["ai_judgment_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_column("proactive_decisions", "raw_model_output")
    op.drop_column("proactive_decisions", "provider_response_id")
    op.drop_column("proactive_decisions", "model")
    op.drop_column("proactive_decisions", "provider")


def downgrade() -> None:
    op.add_column(
        "proactive_decisions",
        sa.Column("provider", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("model", sa.String(length=128), nullable=False),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("provider_response_id", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "proactive_decisions",
        sa.Column("raw_model_output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )

    op.drop_constraint(
        "fk_proactive_decisions_ai_judgment_id", "proactive_decisions", type_="foreignkey"
    )
    op.drop_column("proactive_decisions", "memory_payload")
    op.drop_column("proactive_decisions", "ai_judgment_id")
