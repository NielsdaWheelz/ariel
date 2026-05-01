"""apply memory SOTA schema cutover

Revision ID: 20260501_0016
Revises: 20260501_0015
Create Date: 2026-05-01 01:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0016"
down_revision = "20260501_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("memory_evidence_source_turn_id_fkey", "memory_evidence", type_="foreignkey")
    op.create_foreign_key(
        "memory_evidence_source_turn_id_fkey",
        "memory_evidence",
        "turns",
        ["source_turn_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "memory_evidence",
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="available",
        ),
    )
    op.add_column("memory_evidence", sa.Column("source_uri", sa.Text(), nullable=True))
    op.add_column(
        "memory_evidence", sa.Column("source_artifact_id", sa.String(length=32), nullable=True)
    )
    op.add_column("memory_evidence", sa.Column("evidence_snippet", sa.Text(), nullable=True))
    op.add_column(
        "memory_evidence",
        sa.Column(
            "redaction_posture",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "memory_evidence",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_foreign_key(
        "memory_evidence_source_artifact_id_fkey",
        "memory_evidence",
        "artifacts",
        ["source_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_memory_evidence_source_artifact_id", "memory_evidence", ["source_artifact_id"]
    )
    op.create_index("ix_memory_evidence_updated_at", "memory_evidence", ["updated_at"])
    op.create_check_constraint(
        "ck_memory_evidence_lifecycle_state",
        "memory_evidence",
        "lifecycle_state IN ('available', 'redacted', 'privacy_deleted')",
    )
    op.create_check_constraint(
        "ck_memory_evidence_redaction_posture",
        "memory_evidence",
        "redaction_posture IN ('none', 'redacted', 'privacy_deleted')",
    )
    op.alter_column("memory_evidence", "lifecycle_state", server_default=None)
    op.alter_column("memory_evidence", "redaction_posture", server_default=None)
    op.alter_column("memory_evidence", "updated_at", server_default=None)

    op.drop_constraint("ck_memory_entity_type", "memory_entities", type_="check")
    op.create_check_constraint(
        "ck_memory_entity_type",
        "memory_entities",
        (
            "entity_type IN ('user', 'project', 'repo', 'artifact', 'task', "
            "'commitment', 'decision', 'risk', 'preference', 'procedure', "
            "'person', 'organization', 'domain_concept', 'assertion_subject')"
        ),
    )

    op.create_table(
        "memory_relationships",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_entity_id", sa.String(length=32), nullable=False),
        sa.Column("target_entity_id", sa.String(length=32), nullable=False),
        sa.Column("relationship_type", sa.String(length=64), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('candidate', 'active', 'superseded', 'retracted', "
            "'rejected', 'deleted')",
            name="ck_memory_relationship_lifecycle_state",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_relationship_confidence_range",
        ),
        sa.CheckConstraint(
            "(valid_to IS NULL) OR (valid_from IS NULL) OR (valid_from < valid_to)",
            name="ck_memory_relationship_valid_interval",
        ),
        sa.ForeignKeyConstraint(["source_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_memory_relationships_source_entity_id",
        "memory_relationships",
        ["source_entity_id"],
    )
    op.create_index(
        "ix_memory_relationships_target_entity_id",
        "memory_relationships",
        ["target_entity_id"],
    )
    op.create_index("ix_memory_relationships_evidence_id", "memory_relationships", ["evidence_id"])
    op.create_index("ix_memory_relationships_created_at", "memory_relationships", ["created_at"])
    op.create_index("ix_memory_relationships_updated_at", "memory_relationships", ["updated_at"])

    op.drop_constraint("ck_memory_assertion_type", "memory_assertions", type_="check")
    op.drop_constraint(
        "memory_assertions_superseded_by_assertion_id_fkey",
        "memory_assertions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "memory_assertions_superseded_by_assertion_id_fkey",
        "memory_assertions",
        "memory_assertions",
        ["superseded_by_assertion_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "memory_assertions",
        sa.Column(
            "is_multi_valued",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "memory_assertions", sa.Column("extraction_model", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "memory_assertions",
        sa.Column("extraction_prompt_version", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_memory_assertion_type",
        "memory_assertions",
        (
            "assertion_type IN ('fact', 'profile', 'preference', 'commitment', "
            "'decision', 'project_state', 'procedure', 'domain_concept')"
        ),
    )
    op.create_check_constraint(
        "ck_memory_assertion_superseded_link",
        "memory_assertions",
        "(lifecycle_state != 'superseded') OR (superseded_by_assertion_id IS NOT NULL)",
    )
    op.create_index(
        "ix_memory_assertions_single_active_unique",
        "memory_assertions",
        ["subject_entity_id", "predicate", "scope_key"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'active' AND is_multi_valued IS FALSE"),
    )
    op.alter_column("memory_assertions", "is_multi_valued", server_default=None)

    op.create_table(
        "memory_episodes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("episode_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("primary_evidence_id", sa.String(length=32), nullable=False),
        sa.Column("related_entity_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("related_assertion_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "episode_type IN ('source_snippet', 'task_event', 'action_outcome', "
            "'decision_history', 'project_update')",
            name="ck_memory_episode_type",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'superseded', 'retracted', 'deleted')",
            name="ck_memory_episode_lifecycle_state",
        ),
        sa.CheckConstraint(
            "(valid_to IS NULL) OR (valid_from IS NULL) OR (valid_from < valid_to)",
            name="ck_memory_episode_valid_interval",
        ),
        sa.ForeignKeyConstraint(
            ["primary_evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_memory_episodes_scope_key", "memory_episodes", ["scope_key"])
    op.create_index(
        "ix_memory_episodes_primary_evidence_id", "memory_episodes", ["primary_evidence_id"]
    )
    op.create_index("ix_memory_episodes_created_at", "memory_episodes", ["created_at"])
    op.create_index("ix_memory_episodes_updated_at", "memory_episodes", ["updated_at"])

    op.create_table(
        "memory_reasoning_traces",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("trace_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("task_summary", sa.Text(), nullable=False),
        sa.Column("trace_summary", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("primary_evidence_id", sa.String(length=32), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("related_entity_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("related_assertion_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trace_type IN ('action_path', 'failure', 'user_correction', "
            "'successful_pattern', 'diagnostic')",
            name="ck_memory_reasoning_trace_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'corrected', 'unknown')",
            name="ck_memory_reasoning_trace_outcome",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'superseded', 'retracted', 'deleted')",
            name="ck_memory_reasoning_trace_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["primary_evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_memory_reasoning_traces_scope_key", "memory_reasoning_traces", ["scope_key"]
    )
    op.create_index(
        "ix_memory_reasoning_traces_primary_evidence_id",
        "memory_reasoning_traces",
        ["primary_evidence_id"],
    )
    op.create_index(
        "ix_memory_reasoning_traces_source_turn_id",
        "memory_reasoning_traces",
        ["source_turn_id"],
    )
    op.create_index(
        "ix_memory_reasoning_traces_created_at", "memory_reasoning_traces", ["created_at"]
    )
    op.create_index(
        "ix_memory_reasoning_traces_updated_at", "memory_reasoning_traces", ["updated_at"]
    )

    op.create_table(
        "memory_procedures",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("procedure_key", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("review_state", sa.String(length=32), nullable=False),
        sa.Column("source_assertion_id", sa.String(length=32), nullable=True),
        sa.Column("primary_evidence_id", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('candidate', 'active', 'superseded', 'retracted', "
            "'rejected', 'deleted')",
            name="ck_memory_procedure_lifecycle_state",
        ),
        sa.CheckConstraint(
            "review_state IN ('pending', 'approved', 'rejected', 'auto_approved', "
            "'needs_user_review', 'needs_operator_review')",
            name="ck_memory_procedure_review_state",
        ),
        sa.CheckConstraint(
            "(valid_to IS NULL) OR (valid_from IS NULL) OR (valid_from < valid_to)",
            name="ck_memory_procedure_valid_interval",
        ),
        sa.ForeignKeyConstraint(
            ["source_assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["primary_evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_memory_procedures_source_assertion_id",
        "memory_procedures",
        ["source_assertion_id"],
    )
    op.create_index(
        "ix_memory_procedures_primary_evidence_id",
        "memory_procedures",
        ["primary_evidence_id"],
    )
    op.create_index("ix_memory_procedures_created_at", "memory_procedures", ["created_at"])
    op.create_index("ix_memory_procedures_updated_at", "memory_procedures", ["updated_at"])
    op.create_index(
        "ix_memory_procedures_key_scope_unique",
        "memory_procedures",
        ["procedure_key", "scope_key"],
        unique=True,
    )

    op.drop_constraint(
        "memory_conflict_sets_resolution_assertion_id_fkey",
        "memory_conflict_sets",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "memory_conflict_sets_resolution_assertion_id_fkey",
        "memory_conflict_sets",
        "memory_assertions",
        ["resolution_assertion_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "memory_versions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prior_state", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("new_state", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("redaction_posture", sa.String(length=32), nullable=False),
        sa.Column(
            "projection_invalidation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_evidence', 'memory_entities', "
            "'memory_relationships', 'memory_assertions', 'memory_episodes', "
            "'memory_reasoning_traces', 'memory_procedures', "
            "'project_state_snapshots')",
            name="ck_memory_version_canonical_table",
        ),
        sa.CheckConstraint("version > 0", name="ck_memory_version_positive"),
        sa.CheckConstraint(
            "change_type IN ('created', 'updated', 'reviewed', 'superseded', "
            "'retracted', 'deleted', 'redacted', 'privacy_deleted', "
            "'projection_invalidated', 'imported', 'exported')",
            name="ck_memory_version_change_type",
        ),
        sa.CheckConstraint(
            "redaction_posture IN ('none', 'redacted', 'privacy_deleted')",
            name="ck_memory_version_redaction_posture",
        ),
    )
    op.create_index("ix_memory_versions_canonical_id", "memory_versions", ["canonical_id"])
    op.create_index("ix_memory_versions_created_at", "memory_versions", ["created_at"])
    op.create_index(
        "ix_memory_versions_target_version_unique",
        "memory_versions",
        ["canonical_table", "canonical_id", "version"],
        unique=True,
    )

    op.drop_constraint("ck_memory_projection_job_kind", "memory_projection_jobs", type_="check")
    op.create_check_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        (
            "projection_kind IN ('embedding', 'keyword', 'entity', 'graph', "
            "'context_block', 'project_state')"
        ),
    )

    op.create_table(
        "memory_keyword_projections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("weighted_terms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_evidence', "
            "'memory_episodes', 'memory_reasoning_traces', 'memory_procedures')",
            name="ck_memory_keyword_projection_canonical_table",
        ),
    )
    op.create_index(
        "ix_memory_keyword_projections_canonical_id",
        "memory_keyword_projections",
        ["canonical_id"],
    )
    op.create_index(
        "ix_memory_keyword_projections_created_at",
        "memory_keyword_projections",
        ["created_at"],
    )
    op.create_index(
        "ix_memory_keyword_projections_updated_at",
        "memory_keyword_projections",
        ["updated_at"],
    )
    op.create_index(
        "ix_memory_keyword_projection_unique",
        "memory_keyword_projections",
        ["canonical_table", "canonical_id", "projection_version"],
        unique=True,
    )

    op.create_table(
        "memory_entity_projections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("mention_text", sa.Text(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_evidence', "
            "'memory_relationships', 'memory_episodes', 'memory_reasoning_traces', "
            "'memory_procedures', 'project_state_snapshots')",
            name="ck_memory_entity_projection_canonical_table",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["memory_entities.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_memory_entity_projections_canonical_id",
        "memory_entity_projections",
        ["canonical_id"],
    )
    op.create_index(
        "ix_memory_entity_projections_entity_id", "memory_entity_projections", ["entity_id"]
    )
    op.create_index(
        "ix_memory_entity_projections_created_at", "memory_entity_projections", ["created_at"]
    )
    op.create_index(
        "ix_memory_entity_projections_updated_at", "memory_entity_projections", ["updated_at"]
    )
    op.create_index(
        "ix_memory_entity_projection_unique",
        "memory_entity_projections",
        ["canonical_table", "canonical_id", "entity_id", "projection_version"],
        unique=True,
    )

    op.create_table(
        "memory_graph_projections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_entity_id", sa.String(length=32), nullable=False),
        sa.Column("target_entity_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("relationship_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("distance", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("distance >= 0", name="ck_memory_graph_projection_distance"),
        sa.CheckConstraint("score >= 0.0", name="ck_memory_graph_projection_score"),
        sa.ForeignKeyConstraint(["source_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_memory_graph_projections_source_entity_id",
        "memory_graph_projections",
        ["source_entity_id"],
    )
    op.create_index(
        "ix_memory_graph_projections_target_entity_id",
        "memory_graph_projections",
        ["target_entity_id"],
    )
    op.create_index(
        "ix_memory_graph_projections_created_at", "memory_graph_projections", ["created_at"]
    )
    op.create_index(
        "ix_memory_graph_projections_updated_at", "memory_graph_projections", ["updated_at"]
    )
    op.create_index(
        "ix_memory_graph_projection_unique",
        "memory_graph_projections",
        ["source_entity_id", "target_entity_id", "projection_version"],
        unique=True,
    )

    op.drop_constraint("ck_memory_context_block_type", "memory_context_blocks", type_="check")
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_episode_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_trace_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_procedure_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "memory_context_blocks",
        sa.Column(
            "source_project_state_snapshot_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_memory_context_block_type",
        "memory_context_blocks",
        "block_type IN ('pinned_core', 'project_state', 'procedure', 'episodic', 'reasoning')",
    )
    op.alter_column("memory_context_blocks", "source_episode_ids", server_default=None)
    op.alter_column("memory_context_blocks", "source_trace_ids", server_default=None)
    op.alter_column("memory_context_blocks", "source_procedure_ids", server_default=None)
    op.alter_column(
        "memory_context_blocks", "source_project_state_snapshot_ids", server_default=None
    )

    op.add_column(
        "project_state_snapshots",
        sa.Column(
            "source_episode_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "project_state_snapshots",
        sa.Column(
            "source_evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "project_state_snapshots",
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "project_state_snapshots",
        sa.Column(
            "projection_version",
            sa.String(length=32),
            nullable=False,
            server_default="semantic-v2",
        ),
    )
    op.create_check_constraint(
        "ck_project_state_snapshot_lifecycle_state",
        "project_state_snapshots",
        "lifecycle_state IN ('active', 'superseded', 'retracted', 'deleted')",
    )
    op.alter_column("project_state_snapshots", "source_episode_ids", server_default=None)
    op.alter_column("project_state_snapshots", "source_evidence_ids", server_default=None)
    op.alter_column("project_state_snapshots", "lifecycle_state", server_default=None)
    op.alter_column("project_state_snapshots", "projection_version", server_default=None)

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn', "
            "'workspace_signal_derivation_due', "
            "'attention_review_due', 'attention_item_follow_up_due', "
            "'action_proposal_review_due')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'workspace_signal_derivation_due', "
            "'attention_review_due', 'attention_item_follow_up_due', "
            "'action_proposal_review_due')"
        ),
    )

    op.drop_constraint(
        "ck_project_state_snapshot_lifecycle_state",
        "project_state_snapshots",
        type_="check",
    )
    op.drop_column("project_state_snapshots", "projection_version")
    op.drop_column("project_state_snapshots", "lifecycle_state")
    op.drop_column("project_state_snapshots", "source_evidence_ids")
    op.drop_column("project_state_snapshots", "source_episode_ids")

    op.drop_constraint("ck_memory_context_block_type", "memory_context_blocks", type_="check")
    op.create_check_constraint(
        "ck_memory_context_block_type",
        "memory_context_blocks",
        "block_type IN ('pinned_core', 'project_state', 'procedure')",
    )
    op.drop_column("memory_context_blocks", "source_project_state_snapshot_ids")
    op.drop_column("memory_context_blocks", "source_procedure_ids")
    op.drop_column("memory_context_blocks", "source_trace_ids")
    op.drop_column("memory_context_blocks", "source_episode_ids")

    op.drop_index("ix_memory_graph_projection_unique", table_name="memory_graph_projections")
    op.drop_index("ix_memory_graph_projections_updated_at", table_name="memory_graph_projections")
    op.drop_index("ix_memory_graph_projections_created_at", table_name="memory_graph_projections")
    op.drop_index(
        "ix_memory_graph_projections_target_entity_id", table_name="memory_graph_projections"
    )
    op.drop_index(
        "ix_memory_graph_projections_source_entity_id", table_name="memory_graph_projections"
    )
    op.drop_table("memory_graph_projections")

    op.drop_index("ix_memory_entity_projection_unique", table_name="memory_entity_projections")
    op.drop_index("ix_memory_entity_projections_updated_at", table_name="memory_entity_projections")
    op.drop_index("ix_memory_entity_projections_created_at", table_name="memory_entity_projections")
    op.drop_index("ix_memory_entity_projections_entity_id", table_name="memory_entity_projections")
    op.drop_index(
        "ix_memory_entity_projections_canonical_id", table_name="memory_entity_projections"
    )
    op.drop_table("memory_entity_projections")

    op.drop_index("ix_memory_keyword_projection_unique", table_name="memory_keyword_projections")
    op.drop_index(
        "ix_memory_keyword_projections_updated_at", table_name="memory_keyword_projections"
    )
    op.drop_index(
        "ix_memory_keyword_projections_created_at", table_name="memory_keyword_projections"
    )
    op.drop_index(
        "ix_memory_keyword_projections_canonical_id", table_name="memory_keyword_projections"
    )
    op.drop_table("memory_keyword_projections")

    op.drop_constraint("ck_memory_projection_job_kind", "memory_projection_jobs", type_="check")
    op.create_check_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        "projection_kind IN ('embedding', 'context_block', 'graph_cache', 'project_state')",
    )

    op.drop_index("ix_memory_versions_target_version_unique", table_name="memory_versions")
    op.drop_index("ix_memory_versions_created_at", table_name="memory_versions")
    op.drop_index("ix_memory_versions_canonical_id", table_name="memory_versions")
    op.drop_table("memory_versions")

    op.drop_constraint(
        "memory_conflict_sets_resolution_assertion_id_fkey",
        "memory_conflict_sets",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "memory_conflict_sets_resolution_assertion_id_fkey",
        "memory_conflict_sets",
        "memory_assertions",
        ["resolution_assertion_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_index("ix_memory_procedures_key_scope_unique", table_name="memory_procedures")
    op.drop_index("ix_memory_procedures_updated_at", table_name="memory_procedures")
    op.drop_index("ix_memory_procedures_created_at", table_name="memory_procedures")
    op.drop_index("ix_memory_procedures_primary_evidence_id", table_name="memory_procedures")
    op.drop_index("ix_memory_procedures_source_assertion_id", table_name="memory_procedures")
    op.drop_table("memory_procedures")

    op.drop_index("ix_memory_reasoning_traces_updated_at", table_name="memory_reasoning_traces")
    op.drop_index("ix_memory_reasoning_traces_created_at", table_name="memory_reasoning_traces")
    op.drop_index("ix_memory_reasoning_traces_source_turn_id", table_name="memory_reasoning_traces")
    op.drop_index(
        "ix_memory_reasoning_traces_primary_evidence_id",
        table_name="memory_reasoning_traces",
    )
    op.drop_index("ix_memory_reasoning_traces_scope_key", table_name="memory_reasoning_traces")
    op.drop_table("memory_reasoning_traces")

    op.drop_index("ix_memory_episodes_updated_at", table_name="memory_episodes")
    op.drop_index("ix_memory_episodes_created_at", table_name="memory_episodes")
    op.drop_index("ix_memory_episodes_primary_evidence_id", table_name="memory_episodes")
    op.drop_index("ix_memory_episodes_scope_key", table_name="memory_episodes")
    op.drop_table("memory_episodes")

    op.drop_index("ix_memory_assertions_single_active_unique", table_name="memory_assertions")
    op.drop_constraint("ck_memory_assertion_superseded_link", "memory_assertions", type_="check")
    op.drop_constraint("ck_memory_assertion_type", "memory_assertions", type_="check")
    op.create_check_constraint(
        "ck_memory_assertion_type",
        "memory_assertions",
        "assertion_type IN ('fact', 'preference', 'commitment', 'decision', "
        "'project_state', 'procedure')",
    )
    op.drop_column("memory_assertions", "extraction_prompt_version")
    op.drop_column("memory_assertions", "extraction_model")
    op.drop_column("memory_assertions", "is_multi_valued")
    op.drop_constraint(
        "memory_assertions_superseded_by_assertion_id_fkey",
        "memory_assertions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "memory_assertions_superseded_by_assertion_id_fkey",
        "memory_assertions",
        "memory_assertions",
        ["superseded_by_assertion_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_index("ix_memory_relationships_updated_at", table_name="memory_relationships")
    op.drop_index("ix_memory_relationships_created_at", table_name="memory_relationships")
    op.drop_index("ix_memory_relationships_evidence_id", table_name="memory_relationships")
    op.drop_index("ix_memory_relationships_target_entity_id", table_name="memory_relationships")
    op.drop_index("ix_memory_relationships_source_entity_id", table_name="memory_relationships")
    op.drop_table("memory_relationships")

    op.drop_constraint("ck_memory_entity_type", "memory_entities", type_="check")
    op.create_check_constraint(
        "ck_memory_entity_type",
        "memory_entities",
        "entity_type IN ('user', 'project', 'repo', 'artifact', 'task', "
        "'commitment', 'preference', 'procedure', 'assertion_subject')",
    )

    op.drop_constraint("ck_memory_evidence_redaction_posture", "memory_evidence", type_="check")
    op.drop_constraint("ck_memory_evidence_lifecycle_state", "memory_evidence", type_="check")
    op.drop_index("ix_memory_evidence_updated_at", table_name="memory_evidence")
    op.drop_index("ix_memory_evidence_source_artifact_id", table_name="memory_evidence")
    op.drop_constraint(
        "memory_evidence_source_artifact_id_fkey", "memory_evidence", type_="foreignkey"
    )
    op.drop_column("memory_evidence", "updated_at")
    op.drop_column("memory_evidence", "redaction_posture")
    op.drop_column("memory_evidence", "evidence_snippet")
    op.drop_column("memory_evidence", "source_artifact_id")
    op.drop_column("memory_evidence", "source_uri")
    op.drop_column("memory_evidence", "lifecycle_state")
    op.drop_constraint("memory_evidence_source_turn_id_fkey", "memory_evidence", type_="foreignkey")
    op.create_foreign_key(
        "memory_evidence_source_turn_id_fkey",
        "memory_evidence",
        "turns",
        ["source_turn_id"],
        ["id"],
        ondelete="SET NULL",
    )
