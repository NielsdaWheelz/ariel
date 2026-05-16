"""apply memory completion cutover schema

Revision ID: 20260515_0030
Revises: 20260514_0029
Create Date: 2026-05-15 09:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260515_0030"
down_revision = "20260514_0029"
branch_labels = None
depends_on = None


_MEMORY_ASSERTION_TYPE_BEFORE = (
    "assertion_type IN "
    "('fact', 'profile', 'preference', 'commitment', 'decision', "
    "'project_state', 'procedure', 'domain_concept')"
)
_MEMORY_ASSERTION_TYPE_AFTER = (
    "assertion_type IN "
    "('fact', 'profile', 'preference', 'commitment', 'decision', "
    "'project_state', 'procedure', 'domain_concept', 'negative')"
)
_MEMORY_VERSION_CANONICAL_TABLE_BEFORE = (
    "canonical_table IN ('memory_evidence', 'memory_entities', "
    "'memory_relationships', 'memory_assertions', 'memory_episodes', "
    "'memory_reasoning_traces', 'memory_action_traces', 'memory_procedures', "
    "'memory_topics', 'memory_topic_members', 'memory_deletions', "
    "'memory_retention_policies', 'memory_sensitivity_labels', "
    "'memory_temporal_projections', 'memory_symbol_projections', "
    "'memory_export_artifacts', 'memory_eval_runs', "
    "'project_state_snapshots')"
)
_MEMORY_VERSION_CANONICAL_TABLE_AFTER = (
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
_MEMORY_SCOPE_BINDING_SCOPE_TYPE_BEFORE = (
    "scope_type IN ('user', 'project', 'repo', 'session', 'thread', 'proactive_case')"
)
_MEMORY_SCOPE_BINDING_SCOPE_TYPE_AFTER = (
    "scope_type IN ('user', 'project', 'repo', 'thread', 'proactive_case')"
)
_MEMORY_KEYWORD_PROJECTION_CANONICAL_TABLE_BEFORE = (
    "canonical_table IN ('memory_assertions', 'memory_evidence', "
    "'memory_episodes', 'memory_reasoning_traces', 'memory_procedures')"
)
_MEMORY_KEYWORD_PROJECTION_CANONICAL_TABLE_AFTER = (
    "canonical_table IN ('memory_assertions', 'memory_evidence', "
    "'memory_episodes', 'memory_reasoning_traces', 'memory_action_traces', "
    "'memory_procedures')"
)
_MEMORY_PROJECTION_JOB_KIND_BEFORE = (
    "projection_kind IN ('embedding', 'keyword', 'entity', 'graph', "
    "'context_block', 'project_state', 'hot_index', 'topic_block', "
    "'action_trace', 'temporal', 'symbol', 'export')"
)
_MEMORY_PROJECTION_JOB_KIND_AFTER = (
    "projection_kind IN ('embedding', 'graph', 'context_block', "
    "'project_state', 'hot_index', 'topic_block')"
)


def _upgrade_memory_events() -> None:
    op.create_table(
        "memory_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
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
    )
    op.create_index("ix_memory_events_event_type", "memory_events", ["event_type"])
    op.create_index("ix_memory_events_scope_key", "memory_events", ["scope_key"])
    op.create_index("ix_memory_events_source_turn_id", "memory_events", ["source_turn_id"])
    op.create_index("ix_memory_events_created_at", "memory_events", ["created_at"])


def _upgrade_memory_conflict_sets() -> None:
    op.add_column(
        "memory_conflict_sets",
        sa.Column(
            "conflict_type",
            sa.String(length=32),
            nullable=False,
            server_default="value_contradiction",
        ),
    )
    op.create_check_constraint(
        "ck_memory_conflict_set_type",
        "memory_conflict_sets",
        "conflict_type IN ('value_contradiction', 'staleness', 'scope_overlap')",
    )
    op.alter_column("memory_conflict_sets", "conflict_type", server_default=None)


def _upgrade_memory_scope_bindings() -> None:
    op.drop_constraint(
        "ck_memory_scope_binding_scope_type",
        "memory_scope_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_scope_binding_scope_type",
        "memory_scope_bindings",
        _MEMORY_SCOPE_BINDING_SCOPE_TYPE_AFTER,
    )


def _upgrade_memory_keyword_projections() -> None:
    op.add_column(
        "memory_keyword_projections",
        sa.Column("search_document", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("memory_keyword_projections", "search_document", server_default=None)
    op.add_column(
        "memory_keyword_projections",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', search_document)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_memory_keyword_projections_search_vector",
        "memory_keyword_projections",
        ["search_vector"],
        postgresql_using="gin",
    )
    # weighted_terms is superseded by the search_vector tsvector column; lexical
    # retrieval is Postgres full-text only, so the JSONB term map is dropped.
    op.drop_column("memory_keyword_projections", "weighted_terms")
    op.drop_constraint(
        "ck_memory_keyword_projection_canonical_table",
        "memory_keyword_projections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_keyword_projection_canonical_table",
        "memory_keyword_projections",
        _MEMORY_KEYWORD_PROJECTION_CANONICAL_TABLE_AFTER,
    )


def _upgrade_memory_projection_jobs() -> None:
    op.drop_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        _MEMORY_PROJECTION_JOB_KIND_AFTER,
    )


def upgrade() -> None:
    _upgrade_memory_events()
    _upgrade_memory_conflict_sets()

    op.drop_constraint("ck_memory_assertion_type", "memory_assertions", type_="check")
    op.create_check_constraint(
        "ck_memory_assertion_type",
        "memory_assertions",
        _MEMORY_ASSERTION_TYPE_AFTER,
    )

    op.drop_constraint("ck_memory_version_canonical_table", "memory_versions", type_="check")
    op.create_check_constraint(
        "ck_memory_version_canonical_table",
        "memory_versions",
        _MEMORY_VERSION_CANONICAL_TABLE_AFTER,
    )

    _upgrade_memory_scope_bindings()
    _upgrade_memory_keyword_projections()
    _upgrade_memory_projection_jobs()


def _downgrade_memory_keyword_projections() -> None:
    op.drop_constraint(
        "ck_memory_keyword_projection_canonical_table",
        "memory_keyword_projections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_keyword_projection_canonical_table",
        "memory_keyword_projections",
        _MEMORY_KEYWORD_PROJECTION_CANONICAL_TABLE_BEFORE,
    )
    op.add_column(
        "memory_keyword_projections",
        sa.Column(
            "weighted_terms",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.alter_column("memory_keyword_projections", "weighted_terms", server_default=None)
    op.drop_index(
        "ix_memory_keyword_projections_search_vector",
        table_name="memory_keyword_projections",
    )
    op.drop_column("memory_keyword_projections", "search_vector")
    op.drop_column("memory_keyword_projections", "search_document")


def _downgrade_memory_scope_bindings() -> None:
    op.drop_constraint(
        "ck_memory_scope_binding_scope_type",
        "memory_scope_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_scope_binding_scope_type",
        "memory_scope_bindings",
        _MEMORY_SCOPE_BINDING_SCOPE_TYPE_BEFORE,
    )


def _downgrade_memory_conflict_sets() -> None:
    op.drop_constraint(
        "ck_memory_conflict_set_type",
        "memory_conflict_sets",
        type_="check",
    )
    op.drop_column("memory_conflict_sets", "conflict_type")


def _downgrade_memory_events() -> None:
    op.drop_index("ix_memory_events_created_at", table_name="memory_events")
    op.drop_index("ix_memory_events_source_turn_id", table_name="memory_events")
    op.drop_index("ix_memory_events_scope_key", table_name="memory_events")
    op.drop_index("ix_memory_events_event_type", table_name="memory_events")
    op.drop_table("memory_events")


def _downgrade_memory_projection_jobs() -> None:
    op.drop_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_projection_job_kind",
        "memory_projection_jobs",
        _MEMORY_PROJECTION_JOB_KIND_BEFORE,
    )


def downgrade() -> None:
    _downgrade_memory_projection_jobs()
    _downgrade_memory_keyword_projections()
    _downgrade_memory_scope_bindings()

    op.drop_constraint("ck_memory_version_canonical_table", "memory_versions", type_="check")
    op.create_check_constraint(
        "ck_memory_version_canonical_table",
        "memory_versions",
        _MEMORY_VERSION_CANONICAL_TABLE_BEFORE,
    )

    op.drop_constraint("ck_memory_assertion_type", "memory_assertions", type_="check")
    op.create_check_constraint(
        "ck_memory_assertion_type",
        "memory_assertions",
        _MEMORY_ASSERTION_TYPE_BEFORE,
    )

    _downgrade_memory_conflict_sets()
    _downgrade_memory_events()
