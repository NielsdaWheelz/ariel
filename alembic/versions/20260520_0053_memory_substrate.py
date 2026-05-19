"""replace the crystallization memory tables with the two-layer substrate

Drops ``memory_facts``, ``memory_profile``, and ``sessions.digest``; creates
the append-only ``memory_log`` and the editable ``memory_notes`` with their
HNSW (embedding) and GIN (search_vector) indexes; installs the
``memory_log_append_only`` trigger; and amends the ``ai_judgments`` and
``background_tasks`` CHECK enums to the substrate task types.

Revision ID: 20260520_0053
Revises: 20260520_0052
Create Date: 2026-05-20 00:53:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision = "20260520_0053"
down_revision = "20260520_0052"
branch_labels = None
depends_on = None


EMBEDDING_DIMENSIONS = 1536

# Current CHECK values (head of the chain before this migration).
_AI_JUDGMENT_TYPE_BEFORE = "judgment_type IN ('memory_recall', 'memory_remember', 'model_output')"
_AI_JUDGMENT_TYPE_AFTER = (
    "judgment_type IN ('memory_recall', 'memory_encode', 'memory_dream', 'model_output')"
)

_BACKGROUND_TASK_TYPE_BEFORE = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_remember', "
    "'memory_sweep', 'execute_action_attempt', 'google_object_hydration_due', "
    "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due', "
    "'user_message', 'research_run')"
)
_BACKGROUND_TASK_TYPE_AFTER = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_encode', "
    "'memory_dream', 'execute_action_attempt', 'google_object_hydration_due', "
    "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due', "
    "'user_message', 'research_run')"
)


def upgrade() -> None:
    # 1. Drop the crystallization tables (memory_facts and memory_profile).
    #    Drop the HNSW index first to be safe, then the table (which drops
    #    remaining indexes automatically).
    op.execute("DROP INDEX IF EXISTS ix_memory_facts_embedding_hnsw")
    op.drop_table("memory_facts")
    op.drop_table("memory_profile")

    # 2. Drop the sessions.digest column.
    op.drop_column("sessions", "digest")

    # 3. Create memory_log (append-only raw event log).
    op.create_table(
        "memory_log",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=32), nullable=True),
        sa.Column("turn_id", sa.String(length=32), nullable=True),
        sa.Column("taint", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ("
            "'user_message', 'agent_round', 'assistant_message', "
            "'tool_observation', 'proactive_trigger', 'note_create', "
            "'note_edit', 'note_delete', 'recall', 'research_finding'"
            ")",
            name="ck_memory_log_kind",
        ),
        sa.CheckConstraint(
            "taint IN ('clean', 'tainted')",
            name="ck_memory_log_taint",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_log_search_vector",
        "memory_log",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_memory_log_session_created",
        "memory_log",
        ["session_id", "created_at"],
    )
    op.execute(
        "CREATE INDEX ix_memory_log_embedding_hnsw "
        "ON memory_log USING hnsw (embedding vector_cosine_ops)"
    )

    # Create memory_notes (editable curated layer).
    op.create_table(
        "memory_notes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("taint", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "taint IN ('clean', 'tainted')",
            name="ck_memory_notes_taint",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_notes_search_vector",
        "memory_notes",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.execute(
        "CREATE INDEX ix_memory_notes_embedding_hnsw "
        "ON memory_notes USING hnsw (embedding vector_cosine_ops)"
    )

    # 4. Install the append-only enforcement trigger on memory_log.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION memory_log_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'memory_log is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER memory_log_append_only_trigger
        BEFORE UPDATE OR DELETE ON memory_log
        FOR EACH ROW EXECUTE FUNCTION memory_log_append_only()
        """
    )

    # 5. Amend the CHECK constraints on ai_judgments and background_tasks.
    #    The cutover retires the memory_remember and memory_sweep types; their
    #    rows cannot satisfy the narrowed CHECKs, so clear them first. The
    #    ai_judgments rows audit the rememberer this cutover deletes; the
    #    background_tasks rows are transient queue entries for it.
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.execute("DELETE FROM ai_judgments WHERE judgment_type = 'memory_remember'")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_AFTER)

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.execute(
        "DELETE FROM background_tasks WHERE task_type IN ('memory_remember', 'memory_sweep')"
    )
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_AFTER
    )


def downgrade() -> None:
    # Reverse the CHECK constraint amendments.
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_BEFORE
    )

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_BEFORE)

    # Drop the append-only trigger and function.
    op.execute("DROP TRIGGER IF EXISTS memory_log_append_only_trigger ON memory_log")
    op.execute("DROP FUNCTION IF EXISTS memory_log_append_only()")

    # Drop the substrate tables.
    op.execute("DROP INDEX IF EXISTS ix_memory_notes_embedding_hnsw")
    op.drop_index("ix_memory_notes_search_vector", table_name="memory_notes")
    op.drop_table("memory_notes")

    op.execute("DROP INDEX IF EXISTS ix_memory_log_embedding_hnsw")
    op.drop_index("ix_memory_log_session_created", table_name="memory_log")
    op.drop_index("ix_memory_log_search_vector", table_name="memory_log")
    op.drop_table("memory_log")

    # Re-add sessions.digest.
    op.add_column("sessions", sa.Column("digest", sa.Text(), nullable=True))

    # Recreate the crystallization tables (verbatim from 0042's upgrade()).
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
