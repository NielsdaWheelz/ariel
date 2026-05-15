"""remove selected tool strategy judgment type

Revision ID: 20260514_0027
Revises: 20260513_0026
Create Date: 2026-05-14 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260514_0027"
down_revision = "20260513_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_judgment_type_cutover_20260514_0027",
        sa.Column("ai_judgment_id", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("ai_judgment_id"),
    )
    op.execute(
        "INSERT INTO ai_judgment_type_cutover_20260514_0027 (ai_judgment_id) "
        "SELECT id FROM ai_judgments WHERE judgment_type = 'tool_strategy'"
    )
    op.execute(
        "UPDATE ai_judgments SET judgment_type = 'model_output' "
        "WHERE judgment_type = 'tool_strategy'"
    )
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        (
            "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
            "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
            "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
            "'workspace_commitment_extraction')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        (
            "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
            "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
            "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
            "'workspace_commitment_extraction', 'tool_strategy')"
        ),
    )
    op.execute(
        "UPDATE ai_judgments SET judgment_type = 'tool_strategy' "
        "WHERE id IN (SELECT ai_judgment_id FROM ai_judgment_type_cutover_20260514_0027)"
    )
    op.drop_table("ai_judgment_type_cutover_20260514_0027")
