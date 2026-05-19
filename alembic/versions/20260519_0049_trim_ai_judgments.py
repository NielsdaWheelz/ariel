"""trim ai_judgments to the three surviving judgment types

The ai_judgments cutover narrows the table to the judgment types the agent
loop still writes — ``memory_recall``, ``memory_remember``, and
``model_output``. The retired proactivity, feedback-learning, ambient, and
workspace-extraction judgment types are gone, so this migration purges their
rows and drops the six columns that only those records used: ``selected``,
``omitted``, ``rationale``, ``uncertainty``, ``confidence``, and
``updated_at`` (ai_judgments is append-only, so a mutation timestamp had no
surviving reader). The ``ck_ai_judgment_type`` and
``ck_ai_judgment_parse_status`` CHECK constraints are narrowed to match.

Revision ID: 20260519_0049
Revises: 20260518_0048
Create Date: 2026-05-19 00:49:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260519_0049"
down_revision = "20260518_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Purge rows of retired judgment types so the narrowed CHECK is satisfiable.
    op.execute(
        "DELETE FROM ai_judgments WHERE judgment_type NOT IN "
        "('memory_recall', 'memory_remember', 'model_output')"
    )

    op.drop_index("ix_ai_judgments_updated_at", table_name="ai_judgments")

    op.drop_column("ai_judgments", "updated_at")
    op.drop_column("ai_judgments", "selected")
    op.drop_column("ai_judgments", "omitted")
    op.drop_column("ai_judgments", "rationale")
    op.drop_column("ai_judgments", "uncertainty")
    op.drop_column("ai_judgments", "confidence")

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_recall', 'memory_remember', 'model_output')",
    )

    op.drop_constraint("ck_ai_judgment_parse_status", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_parse_status",
        "ai_judgments",
        "parse_status IN ('parsed', 'invalid_json', 'missing_output', 'schema_invalid')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_judgment_parse_status", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_parse_status",
        "ai_judgments",
        "parse_status IN ('not_required_no_candidates', 'parsed', 'invalid_json', "
        "'missing_output', 'schema_invalid')",
    )

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_recall', 'tool_result_interpretation', "
        "'memory_remember', 'feedback_learning', 'ambient_interpretation', "
        "'proactive_deliberation', 'model_output', "
        "'workspace_commitment_extraction', 'leave_by_evaluation')",
    )

    op.add_column("ai_judgments", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("ai_judgments", sa.Column("uncertainty", sa.Text(), nullable=True))
    op.add_column("ai_judgments", sa.Column("rationale", sa.Text(), nullable=True))
    op.add_column(
        "ai_judgments",
        sa.Column(
            "omitted",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.alter_column("ai_judgments", "omitted", server_default=None)
    op.add_column(
        "ai_judgments",
        sa.Column(
            "selected",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.alter_column("ai_judgments", "selected", server_default=None)
    op.add_column(
        "ai_judgments",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.alter_column("ai_judgments", "updated_at", server_default=None)

    op.create_index("ix_ai_judgments_updated_at", "ai_judgments", ["updated_at"])
