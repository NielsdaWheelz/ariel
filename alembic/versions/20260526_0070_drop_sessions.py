"""drop sessions, session_rotations, and all session_id columns

The sessions construct served no purpose at the agent level — the agent never
reasoned about it, ``memory.recall`` already cross-cut sessions, and
auto-rotation was invented operational complexity. After this migration, turns
and events are globally scoped; idempotency keys are unique by key alone.

Revision ID: 20260526_0070
Revises: 20260525_0069
Create Date: 2026-05-26 06:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260526_0070"
down_revision = "20260525_0069"
branch_labels = None
depends_on = None


# (table, column, FK constraint name, single-column index name or None).
_SESSION_FK_COLUMNS = (
    ("turns", "session_id", "turns_session_id_fkey", "ix_turns_session_id"),
    (
        "turn_idempotency_keys",
        "session_id",
        "turn_idempotency_keys_session_id_fkey",
        "ix_turn_idempotency_keys_session_id",
    ),
    ("events", "session_id", "events_session_id_fkey", "ix_events_session_id"),
    (
        "action_attempts",
        "session_id",
        "action_attempts_session_id_fkey",
        "ix_action_attempts_session_id",
    ),
    (
        "approval_requests",
        "session_id",
        "approval_requests_session_id_fkey",
        "ix_approval_requests_session_id",
    ),
    ("artifacts", "session_id", "artifacts_session_id_fkey", "ix_artifacts_session_id"),
    (
        "captures",
        "effective_session_id",
        "captures_effective_session_id_fkey",
        "ix_captures_effective_session_id",
    ),
    ("memory_log", "session_id", "memory_log_session_id_fkey", None),
    ("jobs", "session_id", "fk_jobs_session_id", "ix_jobs_session_id"),
)


def upgrade() -> None:
    # Idempotency now scopes by key alone (Discord message_ids are globally
    # unique snowflakes). Maintenance expires rows by ``created_at``.
    op.drop_index("ix_turn_idempotency_session_key_unique", table_name="turn_idempotency_keys")
    op.create_index(
        "ix_turn_idempotency_key_unique",
        "turn_idempotency_keys",
        ["idempotency_key"],
        unique=True,
    )

    # memory_log composite index on (session_id, created_at) → recency only.
    op.drop_index("ix_memory_log_session_created", table_name="memory_log")
    op.create_index("ix_memory_log_created_at", "memory_log", ["created_at"])

    # attachment_sources had a 3-column UNIQUE (session_id, turn_id, attachment_ref).
    # The session_id is gone; (turn_id, attachment_ref) is still the natural
    # idempotency rail for attachments within a turn.
    op.drop_index("ix_attachment_sources_session_turn_ref", table_name="attachment_sources")
    op.create_index(
        "ix_attachment_sources_turn_ref",
        "attachment_sources",
        ["turn_id", "attachment_ref"],
        unique=True,
    )

    for table_name, column_name, fk_constraint, index_name in _SESSION_FK_COLUMNS:
        op.drop_constraint(fk_constraint, table_name, type_="foreignkey")
        if index_name is not None:
            op.drop_index(index_name, table_name=table_name)
        op.drop_column(table_name, column_name)

    # attachment_sources.session_id is also session-scoped; drop it through the
    # same surgery as the others. Its FK and index were dropped above through
    # the partial _SESSION_FK_COLUMNS handling. But attachment_sources isn't
    # actually in that tuple — handle it inline here.
    op.drop_constraint(
        "attachment_sources_session_id_fkey", "attachment_sources", type_="foreignkey"
    )
    op.drop_index("ix_attachment_sources_session_id", table_name="attachment_sources")
    op.drop_column("attachment_sources", "session_id")

    op.drop_table("session_rotations")
    op.drop_table("sessions")


def downgrade() -> None:
    # Not supported. Sessions were a deterministic-code judgment about where the
    # conversation breaks; the cutover deleted the judgment, not just the data.
    raise NotImplementedError("session abolition is forward-only")
