"""widen ck_ai_judgment_type for reflective_consolidation

Revision ID: 20260516_0032
Revises: 20260516_0031
Create Date: 2026-05-16 10:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260516_0032"
down_revision = "20260516_0031"
branch_labels = None
depends_on = None


_AI_JUDGMENT_TYPE_BEFORE = (
    "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
    "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
    "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
    "'workspace_commitment_extraction')"
)
_AI_JUDGMENT_TYPE_AFTER = (
    "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
    "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
    "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
    "'workspace_commitment_extraction', 'reflective_consolidation')"
)


def upgrade() -> None:
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_AFTER)


def downgrade() -> None:
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_BEFORE)
