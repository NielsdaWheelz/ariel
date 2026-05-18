"""crystallize the memory subsystem to a flat fact store

Drops the 31 ``memory_*`` tables of the assertion / conflict-set / projection
engine and replaces them with a flat ``memory_facts`` store and a singleton
``memory_profile`` document, adds a per-session ``digest`` column, and amends
the ``ai_judgments`` and ``background_tasks`` type CHECK constraints to the two
new memory judgment and task types.

Revision ID: 20260518_0036
Revises: 20260517_0034
Create Date: 2026-05-18 00:36:00
"""

from __future__ import annotations

import ulid
from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260518_0036"
down_revision = "20260517_0034"
branch_labels = None
depends_on = None


EMBEDDING_DIMENSIONS = 1536

# memory_* tables in foreign-key-safe drop order: every table is dropped before
# any table it is referenced by. The 19 leading entries carry inbound memory FKs
# and are ordered dependents-first; the 12 trailing entries have no inbound
# memory FK. memory_assertions self-references and so drops as a unit.
_MEMORY_TABLES_DROP_ORDER = (
    "memory_assertion_evidence",
    "memory_conflict_members",
    "memory_embedding_projections",
    "memory_reviews",
    "memory_salience",
    "memory_procedures",
    "memory_conflict_sets",
    "memory_action_traces",
    "memory_episodes",
    "memory_reasoning_traces",
    "memory_relationships",
    "memory_entity_projections",
    "memory_graph_projections",
    "memory_context_blocks",
    "memory_topic_members",
    "memory_assertions",
    "memory_entities",
    "memory_evidence",
    "memory_topics",
    "memory_deletions",
    "memory_eval_runs",
    "memory_events",
    "memory_export_artifacts",
    "memory_keyword_projections",
    "memory_projection_jobs",
    "memory_retention_policies",
    "memory_scope_bindings",
    "memory_sensitivity_labels",
    "memory_symbol_projections",
    "memory_temporal_projections",
    "memory_versions",
)

_AI_JUDGMENT_TYPE_BEFORE = (
    "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
    "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
    "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
    "'workspace_commitment_extraction', 'reflective_consolidation')"
)
_AI_JUDGMENT_TYPE_AFTER = (
    "judgment_type IN ('memory_recall', 'tool_result_interpretation', "
    "'memory_remember', 'feedback_learning', 'ambient_interpretation', "
    "'proactive_deliberation', 'model_output', "
    "'workspace_commitment_extraction')"
)
_BACKGROUND_TASK_TYPE_BEFORE = (
    "task_type IN ('agency_event_received', 'deliver_discord_notification', "
    "'expire_approvals', 'reap_stale_tasks', "
    "'provider_subscription_renewal_due', 'provider_event_received', "
    "'provider_sync_due', 'memory_extract_turn', "
    "'ambient_interpretation_due', 'proactive_deliberation_due', "
    "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
    "'proactive_action_execution_due', 'execute_action_attempt', "
    "'google_object_hydration_due', 'provider_evidence_extraction_due', "
    "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
    "'provider_write_reconcile_due')"
)
_BACKGROUND_TASK_TYPE_AFTER = (
    "task_type IN ('agency_event_received', 'deliver_discord_notification', "
    "'expire_approvals', 'reap_stale_tasks', "
    "'provider_subscription_renewal_due', 'provider_event_received', "
    "'provider_sync_due', 'memory_remember', 'memory_sweep', "
    "'ambient_interpretation_due', 'proactive_deliberation_due', "
    "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
    "'proactive_action_execution_due', 'execute_action_attempt', "
    "'google_object_hydration_due', 'provider_evidence_extraction_due', "
    "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
    "'provider_write_reconcile_due')"
)


def upgrade() -> None:
    for table_name in _MEMORY_TABLES_DROP_ORDER:
        op.drop_table(table_name)

    op.create_table(
        "memory_facts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'forgotten')",
            name="ck_memory_fact_status",
        ),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_facts_status", "memory_facts", ["status"])
    op.create_index(
        "ix_memory_facts_search_vector",
        "memory_facts",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.execute(
        "CREATE INDEX ix_memory_facts_embedding_hnsw "
        "ON memory_facts USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "memory_profile",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO memory_profile (id, content, updated_at) "
            "VALUES (:id, '', now())"
        ).bindparams(id=f"mpr_{ulid.new().str.lower()}")
    )

    op.add_column("sessions", sa.Column("digest", sa.Text(), nullable=True))

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_AFTER)

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_AFTER
    )


def _downgrade_constraints() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_BEFORE
    )

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_BEFORE)

    op.drop_column("sessions", "digest")

    op.drop_table("memory_profile")

    op.execute("DROP INDEX IF EXISTS ix_memory_facts_embedding_hnsw")
    op.drop_index("ix_memory_facts_search_vector", table_name="memory_facts")
    op.drop_index("ix_memory_facts_status", table_name="memory_facts")
    op.drop_table("memory_facts")


def _downgrade_core_tables() -> None:
    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("source_session_id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_class", sa.String(length=32), nullable=False),
        sa.Column("trust_boundary", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_artifact_id", sa.String(length=32), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("evidence_snippet", sa.Text(), nullable=True),
        sa.Column("redaction_posture", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "content_class IN "
            "('user_message', 'assistant_message', 'tool_output', 'web_content', "
            "'file_content', 'system', 'rotation')",
            name="ck_memory_evidence_content_class",
        ),
        sa.CheckConstraint(
            "trust_boundary IN "
            "('trusted_user', 'system', 'assistant', 'untrusted_tool', "
            "'untrusted_web', 'untrusted_file')",
            name="ck_memory_evidence_trust_boundary",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('available', 'redacted', 'privacy_deleted')",
            name="ck_memory_evidence_lifecycle_state",
        ),
        sa.CheckConstraint(
            "redaction_posture IN ('none', 'redacted', 'privacy_deleted')",
            name="ck_memory_evidence_redaction_posture",
        ),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifacts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_evidence_source_turn_id", "memory_evidence", ["source_turn_id"])
    op.create_index(
        "ix_memory_evidence_source_session_id", "memory_evidence", ["source_session_id"]
    )
    op.create_index(
        "ix_memory_evidence_source_artifact_id", "memory_evidence", ["source_artifact_id"]
    )
    op.create_index("ix_memory_evidence_created_at", "memory_evidence", ["created_at"])
    op.create_index("ix_memory_evidence_updated_at", "memory_evidence", ["updated_at"])

    op.create_table(
        "memory_entities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entity_type IN "
            "('user', 'project', 'repo', 'artifact', 'task', 'commitment', "
            "'decision', 'risk', 'preference', 'procedure', 'person', "
            "'organization', 'domain_concept', 'assertion_subject')",
            name="ck_memory_entity_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_entities_created_at", "memory_entities", ["created_at"])
    op.create_index("ix_memory_entities_updated_at", "memory_entities", ["updated_at"])
    op.create_index(
        "ix_memory_entities_type_key_unique",
        "memory_entities",
        ["entity_type", "entity_key"],
        unique=True,
    )

    op.create_table(
        "memory_assertions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("subject_entity_id", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("object_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assertion_type", sa.String(length=32), nullable=False),
        sa.Column("is_multi_valued", sa.Boolean(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_assertion_id", sa.String(length=32), nullable=True),
        sa.Column("extraction_model", sa.String(length=128), nullable=True),
        sa.Column("extraction_prompt_version", sa.String(length=64), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assertion_type IN "
            "('fact', 'profile', 'preference', 'commitment', 'decision', "
            "'project_state', 'procedure', 'domain_concept', 'negative')",
            name="ck_memory_assertion_type",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN "
            "('candidate', 'active', 'conflicted', 'superseded', 'stale', "
            "'retracted', 'rejected', 'deleted', 'privacy_deleted')",
            name="ck_memory_assertion_lifecycle_state",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_assertion_confidence_range",
        ),
        sa.CheckConstraint(
            "(valid_to IS NULL) OR (valid_from IS NULL) OR (valid_from < valid_to)",
            name="ck_memory_assertion_valid_interval",
        ),
        sa.CheckConstraint(
            "(lifecycle_state != 'superseded') OR (superseded_by_assertion_id IS NOT NULL)",
            name="ck_memory_assertion_superseded_link",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_assertions_subject_entity_id", "memory_assertions", ["subject_entity_id"]
    )
    op.create_index(
        "ix_memory_assertions_invalidated_at", "memory_assertions", ["invalidated_at"]
    )
    op.create_index(
        "ix_memory_assertions_superseded_by_assertion_id",
        "memory_assertions",
        ["superseded_by_assertion_id"],
    )
    op.create_index(
        "ix_memory_assertions_last_verified_at", "memory_assertions", ["last_verified_at"]
    )
    op.create_index("ix_memory_assertions_created_at", "memory_assertions", ["created_at"])
    op.create_index("ix_memory_assertions_updated_at", "memory_assertions", ["updated_at"])
    op.create_index(
        "ix_memory_assertions_subject_predicate_state",
        "memory_assertions",
        ["subject_entity_id", "predicate", "lifecycle_state"],
    )
    op.create_index("ix_memory_assertions_scope_key", "memory_assertions", ["scope_key"])
    op.create_index(
        "ix_memory_assertions_single_active_unique",
        "memory_assertions",
        ["subject_entity_id", "predicate", "scope_key"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'active' AND is_multi_valued IS FALSE"),
    )

    op.create_table(
        "memory_relationships",
        sa.Column("id", sa.String(length=32), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["source_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_relationships_source_entity_id", "memory_relationships", ["source_entity_id"]
    )
    op.create_index(
        "ix_memory_relationships_target_entity_id", "memory_relationships", ["target_entity_id"]
    )
    op.create_index(
        "ix_memory_relationships_evidence_id", "memory_relationships", ["evidence_id"]
    )
    op.create_index("ix_memory_relationships_created_at", "memory_relationships", ["created_at"])
    op.create_index("ix_memory_relationships_updated_at", "memory_relationships", ["updated_at"])

    op.create_table(
        "memory_assertion_evidence",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assertion_id", "evidence_id", name="uq_memory_assertion_evidence_pair"
        ),
    )
    op.create_index(
        "ix_memory_assertion_evidence_assertion_id",
        "memory_assertion_evidence",
        ["assertion_id"],
    )
    op.create_index(
        "ix_memory_assertion_evidence_evidence_id",
        "memory_assertion_evidence",
        ["evidence_id"],
    )
    op.create_index(
        "ix_memory_assertion_evidence_created_at", "memory_assertion_evidence", ["created_at"]
    )

    op.create_table(
        "memory_episodes",
        sa.Column("id", sa.String(length=32), nullable=False),
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
        sa.Column(
            "related_assertion_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_episodes_scope_key", "memory_episodes", ["scope_key"])
    op.create_index(
        "ix_memory_episodes_primary_evidence_id", "memory_episodes", ["primary_evidence_id"]
    )
    op.create_index("ix_memory_episodes_created_at", "memory_episodes", ["created_at"])
    op.create_index("ix_memory_episodes_updated_at", "memory_episodes", ["updated_at"])

    op.create_table(
        "memory_reasoning_traces",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("trace_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("task_summary", sa.Text(), nullable=False),
        sa.Column("trace_summary", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("primary_evidence_id", sa.String(length=32), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("related_entity_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "related_assertion_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
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
        sa.PrimaryKeyConstraint("id"),
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
        sa.ForeignKeyConstraint(
            ["action_attempt_id"], ["action_attempts.id"], ondelete="RESTRICT"
        ),
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
    op.create_index(
        "ix_memory_action_traces_created_at", "memory_action_traces", ["created_at"]
    )
    op.create_index(
        "ix_memory_action_traces_updated_at", "memory_action_traces", ["updated_at"]
    )

    op.create_table(
        "memory_procedures",
        sa.Column("id", sa.String(length=32), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
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

    op.create_table(
        "memory_reviews",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN "
            "('pending', 'approved', 'rejected', 'auto_approved', "
            "'needs_user_review', 'needs_operator_review', 'merged', 'superseded')",
            name="ck_memory_review_decision",
        ),
        sa.ForeignKeyConstraint(
            ["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_reviews_assertion_id", "memory_reviews", ["assertion_id"])
    op.create_index("ix_memory_reviews_created_at", "memory_reviews", ["created_at"])

    op.create_table(
        "memory_conflict_sets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("subject_entity_id", sa.String(length=32), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("conflict_type", sa.String(length=32), nullable=False),
        sa.Column("resolution_assertion_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('open', 'resolved', 'ignored')",
            name="ck_memory_conflict_set_lifecycle_state",
        ),
        sa.CheckConstraint(
            "conflict_type IN ('value_contradiction', 'staleness', 'scope_overlap')",
            name="ck_memory_conflict_set_type",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["resolution_assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_conflict_sets_subject_entity_id",
        "memory_conflict_sets",
        ["subject_entity_id"],
    )
    op.create_index(
        "ix_memory_conflict_sets_resolution_assertion_id",
        "memory_conflict_sets",
        ["resolution_assertion_id"],
    )
    op.create_index(
        "ix_memory_conflict_sets_created_at", "memory_conflict_sets", ["created_at"]
    )
    op.create_index(
        "ix_memory_conflict_sets_updated_at", "memory_conflict_sets", ["updated_at"]
    )
    op.create_index(
        "ix_memory_conflict_sets_open_unique",
        "memory_conflict_sets",
        ["subject_entity_id", "predicate", "scope_key"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'open'"),
    )

    op.create_table(
        "memory_conflict_members",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("conflict_set_id", sa.String(length=32), nullable=False),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conflict_set_id"], ["memory_conflict_sets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conflict_set_id", "assertion_id", name="uq_memory_conflict_member_pair"
        ),
    )
    op.create_index(
        "ix_memory_conflict_members_conflict_set_id",
        "memory_conflict_members",
        ["conflict_set_id"],
    )
    op.create_index(
        "ix_memory_conflict_members_assertion_id",
        "memory_conflict_members",
        ["assertion_id"],
    )
    op.create_index(
        "ix_memory_conflict_members_created_at", "memory_conflict_members", ["created_at"]
    )

    op.create_table(
        "memory_salience",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("user_priority", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "user_priority IN ('none', 'pinned', 'deprioritized')",
            name="ck_memory_salience_user_priority",
        ),
        sa.CheckConstraint("score >= 0.0", name="ck_memory_salience_score_non_negative"),
        sa.ForeignKeyConstraint(
            ["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_salience_assertion_id", "memory_salience", ["assertion_id"], unique=True
    )
    op.create_index("ix_memory_salience_created_at", "memory_salience", ["created_at"])
    op.create_index("ix_memory_salience_updated_at", "memory_salience", ["updated_at"])


def _downgrade_policy_tables() -> None:
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
            "scope_type IN ('user', 'project', 'repo', 'thread', 'proactive_case')",
            name="ck_memory_scope_binding_scope_type",
        ),
        sa.CheckConstraint(
            "memory_mode IN ('normal', 'temporary', 'no_memory')",
            name="ck_memory_scope_binding_memory_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_scope_bindings_created_at", "memory_scope_bindings", ["created_at"]
    )
    op.create_index(
        "ix_memory_scope_bindings_updated_at", "memory_scope_bindings", ["updated_at"]
    )
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
        "ix_memory_sensitivity_labels_canonical_id",
        "memory_sensitivity_labels",
        ["canonical_id"],
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
        "memory_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prior_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("redaction_posture", sa.String(length=32), nullable=False),
        sa.Column(
            "projection_invalidation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_evidence', 'memory_entities', "
            "'memory_relationships', 'memory_assertions', 'memory_episodes', "
            "'memory_reasoning_traces', 'memory_action_traces', 'memory_procedures', "
            "'memory_topics', 'memory_topic_members', 'memory_deletions', "
            "'memory_retention_policies', 'memory_sensitivity_labels', "
            "'memory_temporal_projections', 'memory_symbol_projections', "
            "'memory_export_artifacts', 'memory_eval_runs', "
            "'memory_scope_bindings', 'memory_salience', 'memory_conflict_sets', "
            "'memory_events', 'project_state_snapshots')",
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_versions_canonical_id", "memory_versions", ["canonical_id"])
    op.create_index("ix_memory_versions_created_at", "memory_versions", ["created_at"])
    op.create_index(
        "ix_memory_versions_target_version_unique",
        "memory_versions",
        ["canonical_table", "canonical_id", "version"],
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
            "target_table IN ('memory_evidence', 'memory_assertions', "
            "'memory_relationships', 'memory_episodes', 'memory_reasoning_traces', "
            "'memory_action_traces', 'memory_procedures', 'memory_topics')",
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
        "ix_memory_deletions_target", "memory_deletions", ["target_table", "target_id"]
    )

    op.create_table(
        "memory_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("entry_path", sa.String(length=32), nullable=False),
        sa.Column("subject_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entry_path IN ('turn', 'http', 'capability', 'worker', 'proactive', 'consolidation')",
            name="ck_memory_event_entry_path",
        ),
        sa.CheckConstraint(
            "event_type LIKE 'evt.memory.%'",
            name="ck_memory_event_type_prefix",
        ),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_events_event_type", "memory_events", ["event_type"])
    op.create_index("ix_memory_events_scope_key", "memory_events", ["scope_key"])
    op.create_index("ix_memory_events_created_at", "memory_events", ["created_at"])


def _downgrade_projection_tables() -> None:
    op.create_table(
        "memory_projection_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("projection_kind", sa.String(length=32), nullable=False),
        sa.Column("target_table", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("attempt_token", sa.String(length=32), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "projection_kind IN ('embedding', 'graph', 'hot_index')",
            name="ck_memory_projection_job_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('pending', 'running', 'completed', 'failed', 'dead_letter')",
            name="ck_memory_projection_job_lifecycle_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_projection_job_attempts"),
        sa.CheckConstraint("max_retries >= 0", name="ck_memory_projection_job_max_retries"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_projection_jobs_target_id", "memory_projection_jobs", ["target_id"])
    op.create_index("ix_memory_projection_jobs_run_after", "memory_projection_jobs", ["run_after"])
    op.create_index(
        "ix_memory_projection_jobs_last_heartbeat",
        "memory_projection_jobs",
        ["last_heartbeat"],
    )
    op.create_index(
        "ix_memory_projection_jobs_created_at", "memory_projection_jobs", ["created_at"]
    )
    op.create_index(
        "ix_memory_projection_jobs_updated_at", "memory_projection_jobs", ["updated_at"]
    )

    op.create_table(
        "memory_embedding_projections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("source_memory_version", sa.Integer(), nullable=False),
        sa.Column("embedding_provider", sa.String(length=32), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"embedding_dimensions = {EMBEDDING_DIMENSIONS}",
            name="ck_memory_embedding_projection_dimensions",
        ),
        sa.CheckConstraint(
            "source_memory_version > 0",
            name="ck_memory_embedding_projection_source_memory_version",
        ),
        sa.ForeignKeyConstraint(
            ["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_embedding_projections_assertion_id",
        "memory_embedding_projections",
        ["assertion_id"],
    )
    op.create_index(
        "ix_memory_embedding_projections_created_at",
        "memory_embedding_projections",
        ["created_at"],
    )
    op.create_index(
        "ix_memory_embedding_projections_updated_at",
        "memory_embedding_projections",
        ["updated_at"],
    )
    op.create_index(
        "ix_memory_embedding_projection_unique",
        "memory_embedding_projections",
        ["assertion_id", "projection_version"],
        unique=True,
    )
    op.execute(
        "CREATE INDEX ix_memory_embedding_projections_embedding_hnsw "
        "ON memory_embedding_projections "
        "USING hnsw (embedding vector_cosine_ops)"
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
        sa.Column("source_memory_version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "source_memory_version > 0",
            name="ck_memory_temporal_projection_source_memory_version",
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
        sa.Column("source_memory_version", sa.Integer(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_episodes', "
            "'memory_action_traces', 'memory_procedures', 'project_state_snapshots')",
            name="ck_memory_symbol_projection_canonical_table",
        ),
        sa.CheckConstraint(
            "source_memory_version > 0",
            name="ck_memory_symbol_projection_source_memory_version",
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
        "memory_keyword_projections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("source_memory_version", sa.Integer(), nullable=False),
        sa.Column("search_document", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', search_document)", persisted=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "canonical_table IN ('memory_assertions', 'memory_evidence', "
            "'memory_episodes', 'memory_reasoning_traces', 'memory_action_traces', "
            "'memory_procedures')",
            name="ck_memory_keyword_projection_canonical_table",
        ),
        sa.CheckConstraint(
            "source_memory_version > 0",
            name="ck_memory_keyword_projection_source_memory_version",
        ),
        sa.PrimaryKeyConstraint("id"),
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
    op.create_index(
        "ix_memory_keyword_projection_search_vector",
        "memory_keyword_projections",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "memory_entity_projections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("canonical_table", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("source_memory_version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "source_memory_version > 0",
            name="ck_memory_entity_projection_source_memory_version",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["memory_entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
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
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_entity_id", sa.String(length=32), nullable=False),
        sa.Column("target_entity_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column(
            "source_memory_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_projection_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("relationship_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("distance", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("distance >= 0", name="ck_memory_graph_projection_distance"),
        sa.CheckConstraint("score >= 0.0", name="ck_memory_graph_projection_score"),
        sa.CheckConstraint(
            "jsonb_typeof(source_memory_versions) = 'object'",
            name="ck_memory_graph_projection_source_memory_versions_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_projection_versions) = 'object'",
            name="ck_memory_graph_projection_source_projection_versions_object",
        ),
        sa.ForeignKeyConstraint(
            ["source_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_entity_id"], ["memory_entities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
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


def _downgrade_topic_and_artifact_tables() -> None:
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
            "'repo-conventions', 'architecture-decisions', 'commitments', "
            "'procedures', 'negative-knowledge', 'recent-failures', "
            "'proactive-patterns', 'external-connectors', 'open-risks', "
            "'resolved-conflicts')",
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
    op.create_index(
        "ix_memory_topic_members_created_at", "memory_topic_members", ["created_at"]
    )
    op.create_index(
        "ix_memory_topic_members_unique",
        "memory_topic_members",
        ["topic_id", "canonical_table", "canonical_id"],
        unique=True,
    )

    op.create_table(
        "memory_context_blocks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("block_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topic_id", sa.String(length=32), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column(
            "source_assertion_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_episode_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_trace_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "source_action_trace_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_procedure_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_project_state_snapshot_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_memory_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_projection_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "block_type IN ('hot_index', 'topic', 'pinned_core', 'project_state', "
            "'procedure', 'episodic', 'reasoning')",
            name="ck_memory_context_block_type",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('active', 'stale', 'superseded', 'deleted')",
            name="ck_memory_context_block_lifecycle_state",
        ),
        sa.CheckConstraint(
            "(block_type = 'topic' AND topic_id IS NOT NULL) OR "
            "(block_type != 'topic' AND topic_id IS NULL)",
            name="ck_memory_context_block_topic_binding",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_memory_versions) = 'object'",
            name="ck_memory_context_block_source_memory_versions_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_projection_versions) = 'object'",
            name="ck_memory_context_block_source_projection_versions_object",
        ),
        sa.ForeignKeyConstraint(["topic_id"], ["memory_topics.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_context_blocks_topic_id", "memory_context_blocks", ["topic_id"])
    op.create_index(
        "ix_memory_context_blocks_created_at", "memory_context_blocks", ["created_at"]
    )
    op.create_index(
        "ix_memory_context_blocks_updated_at", "memory_context_blocks", ["updated_at"]
    )
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

    op.create_table(
        "memory_export_artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("artifact_kind", sa.String(length=32), nullable=False),
        sa.Column("export_format", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("redaction_posture", sa.String(length=32), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "source_memory_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_projection_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "artifact_kind IN ('memory_snapshot', 'agents_md')",
            name="ck_memory_export_artifact_kind",
        ),
        sa.CheckConstraint(
            "export_format IN ('json', 'markdown')",
            name="ck_memory_export_artifact_format",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'failed')",
            name="ck_memory_export_artifact_status",
        ),
        sa.CheckConstraint(
            "redaction_posture IN ('none', 'redacted', 'privacy_deleted')",
            name="ck_memory_export_artifact_redaction_posture",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_memory_versions) = 'object'",
            name="ck_memory_export_artifact_source_memory_versions_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_projection_versions) = 'object'",
            name="ck_memory_export_artifact_source_projection_versions_object",
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
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_memory_eval_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_eval_runs_created_at", "memory_eval_runs", ["created_at"])
    op.create_index("ix_memory_eval_runs_updated_at", "memory_eval_runs", ["updated_at"])


def downgrade() -> None:
    _downgrade_constraints()
    # Recreate the 31 tables parent-first so each table's foreign keys reference
    # an already-created table; the reverse of the upgrade drop order.
    _downgrade_core_tables()
    _downgrade_policy_tables()
    _downgrade_projection_tables()
    _downgrade_topic_and_artifact_tables()
