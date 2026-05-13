"""add memory north-star schema extensions

Revision ID: 20260508_0023
Revises: 20260501_0022
Create Date: 2026-05-08 17:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260508_0023"
down_revision = "20260501_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
        "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
        "'ambient_interpretation', 'proactive_deliberation', 'model_output')",
    )

    op.add_column(
        "sessions",
        sa.Column(
            "memory_mode",
            sa.String(length=32),
            nullable=False,
            server_default="normal",
        ),
    )
    op.create_check_constraint(
        "ck_session_memory_mode",
        "sessions",
        "memory_mode IN ('normal', 'temporary', 'no_memory')",
    )
    op.alter_column("sessions", "memory_mode", server_default=None)

    op.drop_constraint("ck_memory_assertion_lifecycle_state", "memory_assertions", type_="check")
    op.create_check_constraint(
        "ck_memory_assertion_lifecycle_state",
        "memory_assertions",
        "lifecycle_state IN ('candidate', 'active', 'conflicted', 'superseded', "
        "'stale', 'retracted', 'rejected', 'deleted', 'privacy_deleted')",
    )
    op.drop_constraint("ck_memory_review_decision", "memory_reviews", type_="check")
    op.create_check_constraint(
        "ck_memory_review_decision",
        "memory_reviews",
        "decision IN ('pending', 'approved', 'rejected', 'auto_approved', "
        "'needs_user_review', 'needs_operator_review', 'merged', 'superseded')",
    )
    op.drop_constraint(
        "ck_memory_conflict_set_lifecycle_state", "memory_conflict_sets", type_="check"
    )
    op.create_check_constraint(
        "ck_memory_conflict_set_lifecycle_state",
        "memory_conflict_sets",
        "lifecycle_state IN ('open', 'resolved', 'ignored')",
    )
    op.drop_constraint("ck_memory_projection_job_kind", "memory_projection_jobs", type_="check")
    op.create_check_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        "projection_kind IN ('embedding', 'keyword', 'entity', 'graph', 'context_block', "
        "'project_state', 'hot_index', 'topic_block', 'action_trace', 'temporal', "
        "'symbol', 'export')",
    )
    op.drop_constraint("ck_memory_version_canonical_table", "memory_versions", type_="check")
    op.create_check_constraint(
        "ck_memory_version_canonical_table",
        "memory_versions",
        "canonical_table IN ('memory_evidence', 'memory_entities', 'memory_relationships', "
        "'memory_assertions', 'memory_episodes', 'memory_reasoning_traces', "
        "'memory_action_traces', 'memory_procedures', 'memory_topics', "
        "'memory_topic_members', 'memory_deletions', 'memory_retention_policies', "
        "'memory_sensitivity_labels', 'memory_temporal_projections', "
        "'memory_symbol_projections', 'memory_export_artifacts', 'memory_eval_runs', "
        "'project_state_snapshots')",
    )

    op.create_table(
        "memory_action_traces",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("trace_type", sa.String(length=32), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=True),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("primary_evidence_id", sa.String(length=32), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("result_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trace_type IN ('proposal', 'policy_decision', 'approval_decision', "
            "'execution', 'outcome', 'undo')",
            name="ck_memory_action_trace_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'denied', 'undone', 'unknown')",
            name="ck_memory_action_trace_outcome",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'stale', 'superseded', 'retracted', "
            "'deleted', 'privacy_deleted')",
            name="ck_memory_action_trace_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(["action_attempt_id"], ["action_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["primary_evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_action_traces_scope_key", "memory_action_traces", ["scope_key"])
    op.create_index(
        "ix_memory_action_traces_action_attempt_id",
        "memory_action_traces",
        ["action_attempt_id"],
    )
    op.create_index(
        "ix_memory_action_traces_source_turn_id", "memory_action_traces", ["source_turn_id"]
    )
    op.create_index(
        "ix_memory_action_traces_primary_evidence_id",
        "memory_action_traces",
        ["primary_evidence_id"],
    )
    op.create_index("ix_memory_action_traces_created_at", "memory_action_traces", ["created_at"])
    op.create_index("ix_memory_action_traces_updated_at", "memory_action_traces", ["updated_at"])

    op.create_table(
        "memory_scope_bindings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("memory_mode", sa.String(length=32), nullable=False),
        sa.Column("extraction_enabled", sa.Boolean(), nullable=False),
        sa.Column("recall_enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('user', 'project', 'repo', 'session', 'thread', 'proactive_case')",
            name="ck_memory_scope_binding_scope_type",
        ),
        sa.CheckConstraint(
            "memory_mode IN ('normal', 'temporary', 'no_memory')",
            name="ck_memory_scope_binding_memory_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_scope_bindings_created_at", "memory_scope_bindings", ["created_at"])
    op.create_index("ix_memory_scope_bindings_updated_at", "memory_scope_bindings", ["updated_at"])
    op.create_index(
        "ix_memory_scope_bindings_scope_actor_unique",
        "memory_scope_bindings",
        ["scope_type", "scope_key", "actor_id"],
        unique=True,
    )

    op.create_table(
        "memory_retention_policies",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("policy_kind", sa.String(length=32), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "policy_kind IN ('never_remember', 'delete_after', 'review_after')",
            name="ck_memory_retention_policy_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'deleted')",
            name="ck_memory_retention_policy_lifecycle_state",
        ),
        sa.CheckConstraint(
            "(retention_days IS NULL) OR (retention_days > 0)",
            name="ck_memory_retention_policy_days_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_retention_policies_created_at", "memory_retention_policies", ["created_at"]
    )
    op.create_index(
        "ix_memory_retention_policies_updated_at", "memory_retention_policies", ["updated_at"]
    )
    op.create_index(
        "ix_memory_retention_policies_unique",
        "memory_retention_policies",
        ["scope_key", "policy_kind", "pattern"],
        unique=True,
    )

    op.create_table(
        "memory_sensitivity_labels",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_evidence', 'memory_assertions', "
            "'memory_episodes', 'memory_reasoning_traces', 'memory_action_traces', "
            "'memory_procedures', 'project_state_snapshots')",
            name="ck_memory_sensitivity_label_canonical_table",
        ),
        sa.CheckConstraint(
            "label IN ('personal', 'secret', 'regulated', 'source_confidential', 'public')",
            name="ck_memory_sensitivity_label",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'deleted')",
            name="ck_memory_sensitivity_label_lifecycle_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_sensitivity_labels_canonical_id", "memory_sensitivity_labels", ["canonical_id"]
    )
    op.create_index(
        "ix_memory_sensitivity_labels_created_at", "memory_sensitivity_labels", ["created_at"]
    )
    op.create_index(
        "ix_memory_sensitivity_labels_updated_at", "memory_sensitivity_labels", ["updated_at"]
    )
    op.create_index(
        "ix_memory_sensitivity_labels_unique",
        "memory_sensitivity_labels",
        ["canonical_table", "canonical_id", "label"],
        unique=True,
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

    op.create_table(
        "memory_temporal_projections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("temporal_kind", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_episodes', "
            "'memory_action_traces', 'memory_procedures', 'project_state_snapshots')",
            name="ck_memory_temporal_projection_canonical_table",
        ),
        sa.CheckConstraint(
            "temporal_kind IN ('validity', 'occurrence', 'review', 'retention')",
            name="ck_memory_temporal_projection_kind",
        ),
        sa.CheckConstraint(
            "(valid_to IS NULL) OR (valid_from IS NULL) OR (valid_from < valid_to)",
            name="ck_memory_temporal_projection_valid_interval",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_temporal_projections_canonical_id",
        "memory_temporal_projections",
        ["canonical_id"],
    )
    op.create_index(
        "ix_memory_temporal_projections_created_at",
        "memory_temporal_projections",
        ["created_at"],
    )
    op.create_index(
        "ix_memory_temporal_projections_updated_at",
        "memory_temporal_projections",
        ["updated_at"],
    )
    op.create_index(
        "ix_memory_temporal_projections_unique",
        "memory_temporal_projections",
        ["canonical_table", "canonical_id", "temporal_kind", "projection_version"],
        unique=True,
    )

    op.create_table(
        "memory_symbol_projections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_episodes', "
            "'memory_action_traces', 'memory_procedures', 'project_state_snapshots')",
            name="ck_memory_symbol_projection_canonical_table",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_symbol_projections_canonical_id",
        "memory_symbol_projections",
        ["canonical_id"],
    )
    op.create_index(
        "ix_memory_symbol_projections_created_at", "memory_symbol_projections", ["created_at"]
    )
    op.create_index(
        "ix_memory_symbol_projections_updated_at", "memory_symbol_projections", ["updated_at"]
    )
    op.create_index(
        "ix_memory_symbol_projections_unique",
        "memory_symbol_projections",
        ["canonical_table", "canonical_id", "repo_key", "symbol", "path", "projection_version"],
        unique=True,
    )

    op.create_table(
        "memory_topics",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("topic_key", sa.Text(), nullable=False),
        sa.Column("family", sa.String(length=64), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "family IN ('user-profile', 'user-preferences', 'active-projects', "
            "'repo-conventions', 'architecture-decisions', 'commitments', 'procedures', "
            "'negative-knowledge', 'recent-failures', 'proactive-patterns', "
            "'external-connectors', 'open-risks', 'resolved-conflicts')",
            name="ck_memory_topic_family",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'stale', 'superseded', 'deleted')",
            name="ck_memory_topic_lifecycle_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_topics_created_at", "memory_topics", ["created_at"])
    op.create_index("ix_memory_topics_updated_at", "memory_topics", ["updated_at"])
    op.create_index(
        "ix_memory_topics_scope_key_unique",
        "memory_topics",
        ["scope_key", "topic_key"],
        unique=True,
    )

    op.create_table(
        "memory_topic_members",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("topic_id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("membership_kind", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_episodes', "
            "'memory_reasoning_traces', 'memory_action_traces', 'memory_procedures', "
            "'project_state_snapshots')",
            name="ck_memory_topic_member_canonical_table",
        ),
        sa.CheckConstraint(
            "membership_kind IN ('source', 'pointer', 'summary')",
            name="ck_memory_topic_member_kind",
        ),
        sa.CheckConstraint("rank >= 0", name="ck_memory_topic_member_rank_nonnegative"),
        sa.ForeignKeyConstraint(["topic_id"], ["memory_topics.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_topic_members_topic_id", "memory_topic_members", ["topic_id"])
    op.create_index(
        "ix_memory_topic_members_canonical_id", "memory_topic_members", ["canonical_id"]
    )
    op.create_index("ix_memory_topic_members_created_at", "memory_topic_members", ["created_at"])
    op.create_index(
        "ix_memory_topic_members_unique",
        "memory_topic_members",
        ["topic_id", "canonical_table", "canonical_id"],
        unique=True,
    )

    op.create_table(
        "memory_export_artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("export_format", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("redaction_posture", sa.String(length=32), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("export_format IN ('json')", name="ck_memory_export_artifact_format"),
        sa.CheckConstraint(
            "status IN ('created', 'failed')", name="ck_memory_export_artifact_status"
        ),
        sa.CheckConstraint(
            "redaction_posture IN ('none', 'redacted', 'privacy_deleted')",
            name="ck_memory_export_artifact_redaction_posture",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_export_artifacts_scope_key", "memory_export_artifacts", ["scope_key"]
    )
    op.create_index(
        "ix_memory_export_artifacts_created_at", "memory_export_artifacts", ["created_at"]
    )
    op.create_index(
        "ix_memory_export_artifacts_updated_at", "memory_export_artifacts", ["updated_at"]
    )

    op.create_table(
        "memory_eval_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("eval_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('completed', 'failed')", name="ck_memory_eval_run_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_eval_runs_created_at", "memory_eval_runs", ["created_at"])
    op.create_index("ix_memory_eval_runs_updated_at", "memory_eval_runs", ["updated_at"])

    op.drop_constraint("ck_memory_context_block_type", "memory_context_blocks", type_="check")
    op.add_column(
        "memory_context_blocks", sa.Column("topic_id", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_action_trace_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_memory_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "memory_context_blocks_topic_id_fkey",
        "memory_context_blocks",
        "memory_topics",
        ["topic_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index("ix_memory_context_blocks_unique", table_name="memory_context_blocks")
    op.create_index(
        "ix_memory_context_blocks_unique",
        "memory_context_blocks",
        ["block_type", "scope_key", "projection_version"],
        unique=True,
        postgresql_where=sa.text("block_type != 'topic'"),
    )
    op.create_index(
        "ix_memory_context_topic_blocks_unique",
        "memory_context_blocks",
        ["block_type", "scope_key", "topic_id", "projection_version"],
        unique=True,
        postgresql_where=sa.text("block_type = 'topic'"),
    )
    op.create_index("ix_memory_context_blocks_topic_id", "memory_context_blocks", ["topic_id"])
    op.create_check_constraint(
        "ck_memory_context_block_type",
        "memory_context_blocks",
        "block_type IN ('hot_index', 'topic', 'pinned_core', 'project_state', "
        "'procedure', 'episodic', 'reasoning')",
    )
    op.create_check_constraint(
        "ck_memory_context_block_lifecycle_state",
        "memory_context_blocks",
        "lifecycle_state IN ('active', 'stale', 'superseded', 'deleted')",
    )
    op.create_check_constraint(
        "ck_memory_context_block_topic_binding",
        "memory_context_blocks",
        "(block_type = 'topic' AND topic_id IS NOT NULL) OR "
        "(block_type != 'topic' AND topic_id IS NULL)",
    )
    op.alter_column("memory_context_blocks", "lifecycle_state", server_default=None)
    op.alter_column("memory_context_blocks", "source_action_trace_ids", server_default=None)
    op.alter_column("memory_context_blocks", "source_memory_versions", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_memory_context_block_topic_binding", "memory_context_blocks", type_="check"
    )
    op.drop_constraint(
        "ck_memory_context_block_lifecycle_state", "memory_context_blocks", type_="check"
    )
    op.drop_constraint("ck_memory_context_block_type", "memory_context_blocks", type_="check")
    op.execute("DELETE FROM memory_context_blocks WHERE block_type IN ('hot_index', 'topic')")
    op.create_check_constraint(
        "ck_memory_context_block_type",
        "memory_context_blocks",
        "block_type IN ('pinned_core', 'project_state', 'procedure', 'episodic', 'reasoning')",
    )
    op.drop_index("ix_memory_context_topic_blocks_unique", table_name="memory_context_blocks")
    op.drop_index("ix_memory_context_blocks_unique", table_name="memory_context_blocks")
    op.create_index(
        "ix_memory_context_blocks_unique",
        "memory_context_blocks",
        ["block_type", "scope_key", "projection_version"],
        unique=True,
    )
    op.drop_index("ix_memory_context_blocks_topic_id", table_name="memory_context_blocks")
    op.drop_constraint(
        "memory_context_blocks_topic_id_fkey", "memory_context_blocks", type_="foreignkey"
    )
    op.drop_column("memory_context_blocks", "source_memory_versions")
    op.drop_column("memory_context_blocks", "source_action_trace_ids")
    op.drop_column("memory_context_blocks", "lifecycle_state")
    op.drop_column("memory_context_blocks", "topic_id")

    op.drop_index("ix_memory_eval_runs_updated_at", table_name="memory_eval_runs")
    op.drop_index("ix_memory_eval_runs_created_at", table_name="memory_eval_runs")
    op.drop_table("memory_eval_runs")
    op.drop_index("ix_memory_export_artifacts_updated_at", table_name="memory_export_artifacts")
    op.drop_index("ix_memory_export_artifacts_created_at", table_name="memory_export_artifacts")
    op.drop_index("ix_memory_export_artifacts_scope_key", table_name="memory_export_artifacts")
    op.drop_table("memory_export_artifacts")
    op.drop_index("ix_memory_topic_members_unique", table_name="memory_topic_members")
    op.drop_index("ix_memory_topic_members_created_at", table_name="memory_topic_members")
    op.drop_index("ix_memory_topic_members_canonical_id", table_name="memory_topic_members")
    op.drop_index("ix_memory_topic_members_topic_id", table_name="memory_topic_members")
    op.drop_table("memory_topic_members")
    op.drop_index("ix_memory_topics_scope_key_unique", table_name="memory_topics")
    op.drop_index("ix_memory_topics_updated_at", table_name="memory_topics")
    op.drop_index("ix_memory_topics_created_at", table_name="memory_topics")
    op.drop_table("memory_topics")
    op.drop_index("ix_memory_deletions_target", table_name="memory_deletions")
    op.drop_index("ix_memory_deletions_created_at", table_name="memory_deletions")
    op.drop_index("ix_memory_deletions_target_id", table_name="memory_deletions")
    op.drop_table("memory_deletions")
    op.drop_index("ix_memory_symbol_projections_unique", table_name="memory_symbol_projections")
    op.drop_index("ix_memory_symbol_projections_updated_at", table_name="memory_symbol_projections")
    op.drop_index("ix_memory_symbol_projections_created_at", table_name="memory_symbol_projections")
    op.drop_index(
        "ix_memory_symbol_projections_canonical_id", table_name="memory_symbol_projections"
    )
    op.drop_table("memory_symbol_projections")
    op.drop_index("ix_memory_temporal_projections_unique", table_name="memory_temporal_projections")
    op.drop_index(
        "ix_memory_temporal_projections_updated_at", table_name="memory_temporal_projections"
    )
    op.drop_index(
        "ix_memory_temporal_projections_created_at", table_name="memory_temporal_projections"
    )
    op.drop_index(
        "ix_memory_temporal_projections_canonical_id", table_name="memory_temporal_projections"
    )
    op.drop_table("memory_temporal_projections")
    op.drop_index("ix_memory_sensitivity_labels_unique", table_name="memory_sensitivity_labels")
    op.drop_index("ix_memory_sensitivity_labels_updated_at", table_name="memory_sensitivity_labels")
    op.drop_index("ix_memory_sensitivity_labels_created_at", table_name="memory_sensitivity_labels")
    op.drop_index(
        "ix_memory_sensitivity_labels_canonical_id", table_name="memory_sensitivity_labels"
    )
    op.drop_table("memory_sensitivity_labels")
    op.drop_index("ix_memory_retention_policies_unique", table_name="memory_retention_policies")
    op.drop_index("ix_memory_retention_policies_updated_at", table_name="memory_retention_policies")
    op.drop_index("ix_memory_retention_policies_created_at", table_name="memory_retention_policies")
    op.drop_table("memory_retention_policies")
    op.drop_index("ix_memory_scope_bindings_scope_actor_unique", table_name="memory_scope_bindings")
    op.drop_index("ix_memory_scope_bindings_updated_at", table_name="memory_scope_bindings")
    op.drop_index("ix_memory_scope_bindings_created_at", table_name="memory_scope_bindings")
    op.drop_table("memory_scope_bindings")
    op.drop_index("ix_memory_action_traces_updated_at", table_name="memory_action_traces")
    op.drop_index("ix_memory_action_traces_created_at", table_name="memory_action_traces")
    op.drop_index("ix_memory_action_traces_primary_evidence_id", table_name="memory_action_traces")
    op.drop_index("ix_memory_action_traces_source_turn_id", table_name="memory_action_traces")
    op.drop_index("ix_memory_action_traces_action_attempt_id", table_name="memory_action_traces")
    op.drop_index("ix_memory_action_traces_scope_key", table_name="memory_action_traces")
    op.drop_table("memory_action_traces")

    op.drop_constraint("ck_memory_version_canonical_table", "memory_versions", type_="check")
    op.execute(
        "DELETE FROM memory_versions WHERE canonical_table IN ("
        "'memory_action_traces', 'memory_topics', 'memory_topic_members', "
        "'memory_deletions', 'memory_retention_policies', 'memory_sensitivity_labels', "
        "'memory_temporal_projections', 'memory_symbol_projections', "
        "'memory_export_artifacts', 'memory_eval_runs')"
    )
    op.create_check_constraint(
        "ck_memory_version_canonical_table",
        "memory_versions",
        "canonical_table IN ('memory_evidence', 'memory_entities', 'memory_relationships', "
        "'memory_assertions', 'memory_episodes', 'memory_reasoning_traces', "
        "'memory_procedures', 'project_state_snapshots')",
    )
    op.drop_constraint("ck_memory_projection_job_kind", "memory_projection_jobs", type_="check")
    op.execute(
        "DELETE FROM memory_projection_jobs WHERE projection_kind IN ("
        "'hot_index', 'topic_block', 'action_trace', 'temporal', 'symbol', 'export')"
    )
    op.create_check_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        "projection_kind IN ('embedding', 'keyword', 'entity', 'graph', "
        "'context_block', 'project_state')",
    )
    op.drop_constraint(
        "ck_memory_conflict_set_lifecycle_state", "memory_conflict_sets", type_="check"
    )
    op.execute(
        "UPDATE memory_conflict_sets SET lifecycle_state = 'resolved' "
        "WHERE lifecycle_state = 'ignored'"
    )
    op.create_check_constraint(
        "ck_memory_conflict_set_lifecycle_state",
        "memory_conflict_sets",
        "lifecycle_state IN ('open', 'resolved')",
    )
    op.drop_constraint("ck_memory_review_decision", "memory_reviews", type_="check")
    op.execute("DELETE FROM memory_reviews WHERE decision IN ('merged', 'superseded')")
    op.create_check_constraint(
        "ck_memory_review_decision",
        "memory_reviews",
        "decision IN ('pending', 'approved', 'rejected', 'auto_approved', "
        "'needs_user_review', 'needs_operator_review')",
    )
    op.drop_constraint("ck_memory_assertion_lifecycle_state", "memory_assertions", type_="check")
    op.execute(
        "UPDATE memory_assertions SET lifecycle_state = 'active' WHERE lifecycle_state = 'stale'"
    )
    op.execute(
        "UPDATE memory_assertions SET lifecycle_state = 'deleted' "
        "WHERE lifecycle_state = 'privacy_deleted'"
    )
    op.create_check_constraint(
        "ck_memory_assertion_lifecycle_state",
        "memory_assertions",
        "lifecycle_state IN ('candidate', 'active', 'conflicted', 'superseded', "
        "'retracted', 'rejected', 'deleted')",
    )
    op.drop_constraint("ck_session_memory_mode", "sessions", type_="check")
    op.drop_column("sessions", "memory_mode")
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.execute("DELETE FROM ai_judgments WHERE judgment_type = 'memory_extraction'")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
        "'continuity_compaction', 'feedback_learning', 'ambient_interpretation', "
        "'proactive_deliberation', 'model_output')",
    )
