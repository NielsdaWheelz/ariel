"""finish ai-first sota cutover constraints

Revision ID: 20260501_0021
Revises: 20260501_0020
Create Date: 2026-05-01 05:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260501_0021"
down_revision = "20260501_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE proactive_observations
        SET source_type = 'workspace_item'
        WHERE source_type NOT IN (
            'workspace_item', 'job', 'approval_request', 'memory_assertion',
            'google_connector', 'capture'
        )
        """
    )

    op.execute("ALTER TABLE ai_judgments DROP CONSTRAINT IF EXISTS ck_ai_judgment_type")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        (
            "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
            "'continuity_compaction', 'feedback_learning', 'ambient_interpretation', "
            "'proactive_deliberation', 'model_output')"
        ),
    )
    op.execute("ALTER TABLE ai_judgments DROP CONSTRAINT IF EXISTS ck_ai_judgment_failure_code")
    op.create_check_constraint(
        "ck_ai_judgment_failure_code",
        "ai_judgments",
        (
            "failure_code IS NULL OR failure_code IN ("
            "'E_AI_JUDGMENT_REQUIRED', 'E_AI_JUDGMENT_CREDENTIALS', "
            "'E_AI_JUDGMENT_TIMEOUT', 'E_AI_JUDGMENT_INVALID_JSON', "
            "'E_AI_JUDGMENT_SCHEMA', 'E_AI_JUDGMENT_VALIDATION', "
            "'E_AI_JUDGMENT_BUDGET')"
        ),
    )
    op.execute(
        "ALTER TABLE proactive_observations "
        "DROP CONSTRAINT IF EXISTS ck_proactive_observation_source_type"
    )
    op.create_check_constraint(
        "ck_proactive_observation_source_type",
        "proactive_observations",
        (
            "source_type IN ('workspace_item', 'job', 'approval_request', "
            "'memory_assertion', 'google_connector', 'capture')"
        ),
    )


def downgrade() -> None:
    op.execute("DELETE FROM ai_judgments WHERE judgment_type = 'model_output'")

    op.execute("ALTER TABLE ai_judgments DROP CONSTRAINT IF EXISTS ck_ai_judgment_type")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        (
            "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
            "'continuity_compaction', 'feedback_learning', 'ambient_interpretation', "
            "'proactive_deliberation')"
        ),
    )
    op.execute("ALTER TABLE ai_judgments DROP CONSTRAINT IF EXISTS ck_ai_judgment_failure_code")
    op.create_check_constraint(
        "ck_ai_judgment_failure_code",
        "ai_judgments",
        (
            "failure_code IS NULL OR failure_code IN ("
            "'E_AI_JUDGMENT_REQUIRED', 'E_AI_JUDGMENT_CREDENTIALS', "
            "'E_AI_JUDGMENT_TIMEOUT', 'E_AI_JUDGMENT_INVALID_JSON', "
            "'E_AI_JUDGMENT_SCHEMA', 'E_AI_JUDGMENT_VALIDATION', "
            "'E_AI_JUDGMENT_BUDGET')"
        ),
    )
    op.execute(
        "ALTER TABLE proactive_observations "
        "DROP CONSTRAINT IF EXISTS ck_proactive_observation_source_type"
    )
    op.create_check_constraint(
        "ck_proactive_observation_source_type",
        "proactive_observations",
        (
            "source_type IN ('workspace_item', 'job', 'approval_request', "
            "'memory_assertion', 'google_connector', 'capture')"
        ),
    )
