"""semantic memory hard cutover

Revision ID: 20260430_0012
Revises: 20260427_0011
Create Date: 2026-04-30 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260430_0012"
down_revision = "20260427_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_memory_revisions_item_created", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_created_at", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_last_verified_at", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_source_session_id", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_source_turn_id", table_name="memory_revisions")
    op.drop_index("ix_memory_revisions_memory_item_id", table_name="memory_revisions")
    op.drop_table("memory_revisions")
    op.drop_index("ix_memory_items_active_revision_id", table_name="memory_items")
    op.drop_index("ix_memory_items_class_key_unique", table_name="memory_items")
    op.drop_table("memory_items")

    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("source_session_id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_class", sa.String(length=32), nullable=False),
        sa.Column("trust_boundary", sa.String(length=32), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_session_id"], ["sessions.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_memory_evidence_source_turn_id", "memory_evidence", ["source_turn_id"])
    op.create_index(
        "ix_memory_evidence_source_session_id", "memory_evidence", ["source_session_id"]
    )
    op.create_index("ix_memory_evidence_created_at", "memory_evidence", ["created_at"])

    op.create_table(
        "memory_entities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entity_type IN "
            "('user', 'project', 'repo', 'artifact', 'task', 'commitment', "
            "'preference', 'procedure', 'assertion_subject')",
            name="ck_memory_entity_type",
        ),
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
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subject_entity_id", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("object_value", postgresql.JSONB(), nullable=False),
        sa.Column("assertion_type", sa.String(length=32), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_assertion_id", sa.String(length=32), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assertion_type IN "
            "('fact', 'preference', 'commitment', 'decision', 'project_state', 'procedure')",
            name="ck_memory_assertion_type",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN "
            "('candidate', 'active', 'conflicted', 'superseded', 'retracted', "
            "'rejected', 'deleted')",
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
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["memory_entities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_assertion_id"],
            ["memory_assertions.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_memory_assertions_subject_entity_id", "memory_assertions", ["subject_entity_id"]
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

    op.create_table(
        "memory_assertion_evidence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_id"], ["memory_evidence.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "assertion_id", "evidence_id", name="uq_memory_assertion_evidence_pair"
        ),
    )
    op.create_index(
        "ix_memory_assertion_evidence_assertion_id", "memory_assertion_evidence", ["assertion_id"]
    )
    op.create_index(
        "ix_memory_assertion_evidence_evidence_id", "memory_assertion_evidence", ["evidence_id"]
    )
    op.create_index(
        "ix_memory_assertion_evidence_created_at", "memory_assertion_evidence", ["created_at"]
    )

    op.create_table(
        "memory_reviews",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN "
            "('pending', 'approved', 'rejected', 'auto_approved', "
            "'needs_user_review', 'needs_operator_review')",
            name="ck_memory_review_decision",
        ),
        sa.ForeignKeyConstraint(["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_memory_reviews_assertion_id", "memory_reviews", ["assertion_id"])
    op.create_index("ix_memory_reviews_created_at", "memory_reviews", ["created_at"])

    op.create_table(
        "memory_conflict_sets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subject_entity_id", sa.String(length=32), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("resolution_assertion_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('open', 'resolved')",
            name="ck_memory_conflict_set_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["memory_entities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_assertion_id"],
            ["memory_assertions.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_memory_conflict_sets_subject_entity_id", "memory_conflict_sets", ["subject_entity_id"]
    )
    op.create_index(
        "ix_memory_conflict_sets_resolution_assertion_id",
        "memory_conflict_sets",
        ["resolution_assertion_id"],
    )
    op.create_index("ix_memory_conflict_sets_created_at", "memory_conflict_sets", ["created_at"])
    op.create_index("ix_memory_conflict_sets_updated_at", "memory_conflict_sets", ["updated_at"])
    op.create_index(
        "ix_memory_conflict_sets_open_unique",
        "memory_conflict_sets",
        ["subject_entity_id", "predicate", "scope_key"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'open'"),
    )

    op.create_table(
        "memory_conflict_members",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("conflict_set_id", sa.String(length=32), nullable=False),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conflict_set_id"],
            ["memory_conflict_sets.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "conflict_set_id", "assertion_id", name="uq_memory_conflict_member_pair"
        ),
    )
    op.create_index(
        "ix_memory_conflict_members_conflict_set_id", "memory_conflict_members", ["conflict_set_id"]
    )
    op.create_index(
        "ix_memory_conflict_members_assertion_id", "memory_conflict_members", ["assertion_id"]
    )
    op.create_index(
        "ix_memory_conflict_members_created_at", "memory_conflict_members", ["created_at"]
    )

    op.create_table(
        "memory_salience",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("user_priority", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "user_priority IN ('none', 'pinned', 'deprioritized')",
            name="ck_memory_salience_user_priority",
        ),
        sa.CheckConstraint("score >= 0.0", name="ck_memory_salience_score_non_negative"),
        sa.ForeignKeyConstraint(["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_memory_salience_assertion_id", "memory_salience", ["assertion_id"], unique=True
    )
    op.create_index("ix_memory_salience_created_at", "memory_salience", ["created_at"])
    op.create_index("ix_memory_salience_updated_at", "memory_salience", ["updated_at"])

    op.create_table(
        "memory_projection_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("projection_kind", sa.String(length=32), nullable=False),
        sa.Column("target_table", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "projection_kind IN ('embedding', 'context_block', 'graph_cache', 'project_state')",
            name="ck_memory_projection_job_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('pending', 'running', 'completed', 'failed', 'dead_letter')",
            name="ck_memory_projection_job_lifecycle_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_projection_job_attempts"),
        sa.CheckConstraint("max_retries >= 0", name="ck_memory_projection_job_max_retries"),
    )
    op.create_index("ix_memory_projection_jobs_target_id", "memory_projection_jobs", ["target_id"])
    op.create_index("ix_memory_projection_jobs_run_after", "memory_projection_jobs", ["run_after"])
    op.create_index(
        "ix_memory_projection_jobs_created_at", "memory_projection_jobs", ["created_at"]
    )
    op.create_index(
        "ix_memory_projection_jobs_updated_at", "memory_projection_jobs", ["updated_at"]
    )

    op.create_table(
        "memory_embedding_projections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("assertion_id", sa.String(length=32), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("embedding", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assertion_id"], ["memory_assertions.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_memory_embedding_projections_assertion_id",
        "memory_embedding_projections",
        ["assertion_id"],
    )
    op.create_index(
        "ix_memory_embedding_projections_created_at", "memory_embedding_projections", ["created_at"]
    )
    op.create_index(
        "ix_memory_embedding_projections_updated_at", "memory_embedding_projections", ["updated_at"]
    )
    op.create_index(
        "ix_memory_embedding_projection_unique",
        "memory_embedding_projections",
        ["assertion_id", "projection_version"],
        unique=True,
    )

    op.create_table(
        "memory_context_blocks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("block_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_assertion_ids", postgresql.JSONB(), nullable=False),
        sa.Column("projection_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "block_type IN ('pinned_core', 'project_state', 'procedure')",
            name="ck_memory_context_block_type",
        ),
    )
    op.create_index("ix_memory_context_blocks_created_at", "memory_context_blocks", ["created_at"])
    op.create_index("ix_memory_context_blocks_updated_at", "memory_context_blocks", ["updated_at"])
    op.create_index(
        "ix_memory_context_blocks_unique",
        "memory_context_blocks",
        ["block_type", "scope_key", "projection_version"],
        unique=True,
    )

    op.create_table(
        "project_state_snapshots",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_key", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("source_assertion_ids", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_state_snapshots_project_key", "project_state_snapshots", ["project_key"]
    )
    op.create_index(
        "ix_project_state_snapshots_created_at", "project_state_snapshots", ["created_at"]
    )
    op.create_index(
        "ix_project_state_snapshots_updated_at", "project_state_snapshots", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_state_snapshots_updated_at", table_name="project_state_snapshots")
    op.drop_index("ix_project_state_snapshots_created_at", table_name="project_state_snapshots")
    op.drop_index("ix_project_state_snapshots_project_key", table_name="project_state_snapshots")
    op.drop_table("project_state_snapshots")
    op.drop_index("ix_memory_context_blocks_unique", table_name="memory_context_blocks")
    op.drop_index("ix_memory_context_blocks_updated_at", table_name="memory_context_blocks")
    op.drop_index("ix_memory_context_blocks_created_at", table_name="memory_context_blocks")
    op.drop_table("memory_context_blocks")
    op.drop_index(
        "ix_memory_embedding_projection_unique", table_name="memory_embedding_projections"
    )
    op.drop_index(
        "ix_memory_embedding_projections_updated_at", table_name="memory_embedding_projections"
    )
    op.drop_index(
        "ix_memory_embedding_projections_created_at", table_name="memory_embedding_projections"
    )
    op.drop_index(
        "ix_memory_embedding_projections_assertion_id", table_name="memory_embedding_projections"
    )
    op.drop_table("memory_embedding_projections")
    op.drop_index("ix_memory_projection_jobs_updated_at", table_name="memory_projection_jobs")
    op.drop_index("ix_memory_projection_jobs_created_at", table_name="memory_projection_jobs")
    op.drop_index("ix_memory_projection_jobs_run_after", table_name="memory_projection_jobs")
    op.drop_index("ix_memory_projection_jobs_target_id", table_name="memory_projection_jobs")
    op.drop_table("memory_projection_jobs")
    op.drop_index("ix_memory_salience_updated_at", table_name="memory_salience")
    op.drop_index("ix_memory_salience_created_at", table_name="memory_salience")
    op.drop_index("ix_memory_salience_assertion_id", table_name="memory_salience")
    op.drop_table("memory_salience")
    op.drop_index("ix_memory_conflict_members_created_at", table_name="memory_conflict_members")
    op.drop_index("ix_memory_conflict_members_assertion_id", table_name="memory_conflict_members")
    op.drop_index(
        "ix_memory_conflict_members_conflict_set_id", table_name="memory_conflict_members"
    )
    op.drop_table("memory_conflict_members")
    op.drop_index("ix_memory_conflict_sets_open_unique", table_name="memory_conflict_sets")
    op.drop_index("ix_memory_conflict_sets_updated_at", table_name="memory_conflict_sets")
    op.drop_index("ix_memory_conflict_sets_created_at", table_name="memory_conflict_sets")
    op.drop_index(
        "ix_memory_conflict_sets_resolution_assertion_id", table_name="memory_conflict_sets"
    )
    op.drop_index("ix_memory_conflict_sets_subject_entity_id", table_name="memory_conflict_sets")
    op.drop_table("memory_conflict_sets")
    op.drop_index("ix_memory_reviews_created_at", table_name="memory_reviews")
    op.drop_index("ix_memory_reviews_assertion_id", table_name="memory_reviews")
    op.drop_table("memory_reviews")
    op.drop_index("ix_memory_assertion_evidence_created_at", table_name="memory_assertion_evidence")
    op.drop_index(
        "ix_memory_assertion_evidence_evidence_id", table_name="memory_assertion_evidence"
    )
    op.drop_index(
        "ix_memory_assertion_evidence_assertion_id", table_name="memory_assertion_evidence"
    )
    op.drop_table("memory_assertion_evidence")
    op.drop_index("ix_memory_assertions_scope_key", table_name="memory_assertions")
    op.drop_index("ix_memory_assertions_subject_predicate_state", table_name="memory_assertions")
    op.drop_index("ix_memory_assertions_updated_at", table_name="memory_assertions")
    op.drop_index("ix_memory_assertions_created_at", table_name="memory_assertions")
    op.drop_index("ix_memory_assertions_last_verified_at", table_name="memory_assertions")
    op.drop_index("ix_memory_assertions_superseded_by_assertion_id", table_name="memory_assertions")
    op.drop_index("ix_memory_assertions_subject_entity_id", table_name="memory_assertions")
    op.drop_table("memory_assertions")
    op.drop_index("ix_memory_entities_type_key_unique", table_name="memory_entities")
    op.drop_index("ix_memory_entities_updated_at", table_name="memory_entities")
    op.drop_index("ix_memory_entities_created_at", table_name="memory_entities")
    op.drop_table("memory_entities")
    op.drop_index("ix_memory_evidence_created_at", table_name="memory_evidence")
    op.drop_index("ix_memory_evidence_source_session_id", table_name="memory_evidence")
    op.drop_index("ix_memory_evidence_source_turn_id", table_name="memory_evidence")
    op.drop_table("memory_evidence")

    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("memory_class", sa.String(length=32), nullable=False),
        sa.Column("memory_key", sa.Text(), nullable=False),
        sa.Column("active_revision_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "memory_class IN "
            "('profile', 'preference', 'project', 'commitment', 'episodic_summary')",
            name="ck_memory_item_class",
        ),
    )
    op.create_index(
        "ix_memory_items_class_key_unique",
        "memory_items",
        ["memory_class", "memory_key"],
        unique=True,
    )
    op.create_index("ix_memory_items_active_revision_id", "memory_items", ["active_revision_id"])
    op.create_index("ix_memory_items_created_at", "memory_items", ["created_at"])
    op.create_index("ix_memory_items_updated_at", "memory_items", ["updated_at"])

    op.create_table(
        "memory_revisions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("memory_item_id", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_turn_id", sa.String(length=32), nullable=True),
        sa.Column("source_session_id", sa.String(length=32), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('candidate', 'validated', 'superseded', 'retracted')",
            name="ck_memory_revision_lifecycle_state",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_revision_confidence_range",
        ),
        sa.CheckConstraint(
            "(lifecycle_state = 'retracted' AND value IS NULL) OR "
            "(lifecycle_state <> 'retracted' AND value IS NOT NULL)",
            name="ck_memory_revision_value_presence",
        ),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_session_id"], ["sessions.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_memory_revisions_memory_item_id", "memory_revisions", ["memory_item_id"])
    op.create_index("ix_memory_revisions_source_turn_id", "memory_revisions", ["source_turn_id"])
    op.create_index(
        "ix_memory_revisions_source_session_id", "memory_revisions", ["source_session_id"]
    )
    op.create_index(
        "ix_memory_revisions_last_verified_at", "memory_revisions", ["last_verified_at"]
    )
    op.create_index("ix_memory_revisions_created_at", "memory_revisions", ["created_at"])
    op.create_index(
        "ix_memory_revisions_item_created",
        "memory_revisions",
        ["memory_item_id", "created_at"],
    )
