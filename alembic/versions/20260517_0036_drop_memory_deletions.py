"""drop memory_deletions: fully-derivable subset of memory_versions

Revision ID: 20260517_0036
Revises: 20260517_0035
Create Date: 2026-05-17 23:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260517_0036"
down_revision = "20260517_0035"
branch_labels = None
depends_on = None


_MEMORY_VERSION_CANONICAL_TABLE_BEFORE = (
    "canonical_table IN ('memory_evidence', 'memory_entities', "
    "'memory_relationships', 'memory_assertions', 'memory_episodes', "
    "'memory_reasoning_traces', 'memory_action_traces', 'memory_procedures', "
    "'memory_topics', 'memory_topic_members', 'memory_deletions', "
    "'memory_retention_policies', 'memory_sensitivity_labels', "
    "'memory_temporal_projections', 'memory_symbol_projections', "
    "'memory_export_artifacts', 'memory_eval_runs', "
    "'memory_scope_bindings', 'memory_salience', 'memory_conflict_sets', "
    "'memory_events', 'project_state_snapshots')"
)
_MEMORY_VERSION_CANONICAL_TABLE_AFTER = (
    "canonical_table IN ('memory_evidence', 'memory_entities', "
    "'memory_relationships', 'memory_assertions', 'memory_episodes', "
    "'memory_reasoning_traces', 'memory_action_traces', 'memory_procedures', "
    "'memory_topics', 'memory_topic_members', "
    "'memory_retention_policies', 'memory_sensitivity_labels', "
    "'memory_temporal_projections', 'memory_symbol_projections', "
    "'memory_export_artifacts', 'memory_eval_runs', "
    "'memory_scope_bindings', 'memory_salience', 'memory_conflict_sets', "
    "'memory_events', 'project_state_snapshots')"
)


def upgrade() -> None:
    op.drop_index("ix_memory_deletions_target", table_name="memory_deletions")
    op.drop_index("ix_memory_deletions_created_at", table_name="memory_deletions")
    op.drop_index("ix_memory_deletions_target_id", table_name="memory_deletions")
    op.drop_table("memory_deletions")

    op.drop_constraint("ck_memory_version_canonical_table", "memory_versions", type_="check")
    op.create_check_constraint(
        "ck_memory_version_canonical_table",
        "memory_versions",
        _MEMORY_VERSION_CANONICAL_TABLE_AFTER,
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_version_canonical_table", "memory_versions", type_="check")
    op.create_check_constraint(
        "ck_memory_version_canonical_table",
        "memory_versions",
        _MEMORY_VERSION_CANONICAL_TABLE_BEFORE,
    )

    op.create_table(
        "memory_deletions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("target_table", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=32), nullable=False),
        sa.Column("deletion_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("redaction_posture", sa.String(length=32), nullable=False),
        sa.Column(
            "projection_invalidation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_table IN ('memory_evidence', 'memory_assertions', 'memory_relationships', "
            "'memory_episodes', 'memory_reasoning_traces', 'memory_action_traces', "
            "'memory_procedures', 'memory_topics')",
            name="ck_memory_deletion_target_table",
        ),
        sa.CheckConstraint(
            "deletion_type IN ('delete', 'privacy_delete', 'redact', 'retract')",
            name="ck_memory_deletion_type",
        ),
        sa.CheckConstraint(
            "redaction_posture IN ('none', 'redacted', 'privacy_deleted')",
            name="ck_memory_deletion_redaction_posture",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_deletions_target_id", "memory_deletions", ["target_id"])
    op.create_index("ix_memory_deletions_created_at", "memory_deletions", ["created_at"])
    op.create_index(
        "ix_memory_deletions_target",
        "memory_deletions",
        ["target_table", "target_id"],
    )
