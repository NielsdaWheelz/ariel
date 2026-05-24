"""replace database cascading foreign keys with restrict

Revision ID: 20260522_0062
Revises: 20260522_0061
Create Date: 2026-05-22 02:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260522_0062"
down_revision = "20260522_0061"
branch_labels = None
depends_on = None


_FK_REPLACEMENTS = (
    (
        "fk_sessions_rotated_from_session_id",
        "sessions",
        "sessions",
        ["rotated_from_session_id"],
        ["id"],
        "SET NULL",
    ),
    (
        "captures_effective_session_id_fkey",
        "captures",
        "sessions",
        ["effective_session_id"],
        ["id"],
        "SET NULL",
    ),
    ("captures_turn_id_fkey", "captures", "turns", ["turn_id"], ["id"], "SET NULL"),
    ("events_turn_id_fkey", "events", "turns", ["turn_id"], ["id"], "CASCADE"),
    (
        "approval_requests_action_attempt_id_fkey",
        "approval_requests",
        "action_attempts",
        ["action_attempt_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "approval_requests_turn_id_fkey",
        "approval_requests",
        "turns",
        ["turn_id"],
        ["id"],
        "CASCADE",
    ),
    ("artifacts_turn_id_fkey", "artifacts", "turns", ["turn_id"], ["id"], "CASCADE"),
    (
        "artifacts_action_attempt_id_fkey",
        "artifacts",
        "action_attempts",
        ["action_attempt_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "attachment_sources_turn_id_fkey",
        "attachment_sources",
        "turns",
        ["turn_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "attachment_extractions_source_id_fkey",
        "attachment_extractions",
        "attachment_sources",
        ["source_id"],
        ["id"],
        "CASCADE",
    ),
    (
        "google_connector_events_connector_id_fkey",
        "google_connector_events",
        "google_connectors",
        ["connector_id"],
        ["id"],
        "CASCADE",
    ),
    ("fk_jobs_session_id", "jobs", "sessions", ["session_id"], ["id"], "SET NULL"),
    ("fk_jobs_turn_id", "jobs", "turns", ["turn_id"], ["id"], "SET NULL"),
    (
        "fk_jobs_action_attempt_id",
        "jobs",
        "action_attempts",
        ["action_attempt_id"],
        ["id"],
        "SET NULL",
    ),
    ("job_events_job_id_fkey", "job_events", "jobs", ["job_id"], ["id"], "CASCADE"),
)


def _replace_foreign_keys(*, ondelete: str) -> None:
    for (
        name,
        source_table,
        referent_table,
        local_cols,
        remote_cols,
        _old_ondelete,
    ) in _FK_REPLACEMENTS:
        op.drop_constraint(name, source_table, type_="foreignkey")
        op.create_foreign_key(
            name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
            ondelete=ondelete,
        )


def upgrade() -> None:
    _replace_foreign_keys(ondelete="RESTRICT")


def downgrade() -> None:
    for (
        name,
        source_table,
        referent_table,
        local_cols,
        remote_cols,
        old_ondelete,
    ) in _FK_REPLACEMENTS:
        op.drop_constraint(name, source_table, type_="foreignkey")
        op.create_foreign_key(
            name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
            ondelete=old_ondelete,
        )
