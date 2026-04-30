"""add agency capability job metadata

Revision ID: 20260427_0011
Revises: 20260427_0010
Create Date: 2026-04-27 18:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260427_0011"
down_revision = "20260427_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("session_id", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("turn_id", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("action_attempt_id", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("agency_repo_root", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("agency_repo_id", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("agency_task_id", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("agency_invocation_id", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("agency_worktree_id", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("agency_worktree_path", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("agency_branch", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("agency_runner", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("agency_request_id", sa.String(length=128), nullable=True))
    op.add_column(
        "jobs", sa.Column("agency_last_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("jobs", sa.Column("agency_pr_number", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("agency_pr_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("discord_thread_id", sa.String(length=128), nullable=True))
    op.create_foreign_key(
        "fk_jobs_session_id", "jobs", "sessions", ["session_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_jobs_turn_id", "jobs", "turns", ["turn_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_jobs_action_attempt_id",
        "jobs",
        "action_attempts",
        ["action_attempt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_jobs_session_id", "jobs", ["session_id"])
    op.create_index("ix_jobs_turn_id", "jobs", ["turn_id"])
    op.create_index("ix_jobs_action_attempt_id", "jobs", ["action_attempt_id"])
    op.create_index("ix_jobs_agency_repo_id", "jobs", ["agency_repo_id"])
    op.create_index("ix_jobs_agency_task_id", "jobs", ["agency_task_id"])
    op.create_index("ix_jobs_agency_invocation_id", "jobs", ["agency_invocation_id"])
    op.create_index("ix_jobs_agency_worktree_id", "jobs", ["agency_worktree_id"])
    op.alter_column(
        "job_events",
        "agency_event_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "job_events",
        "agency_event_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.drop_index("ix_jobs_agency_worktree_id", table_name="jobs")
    op.drop_index("ix_jobs_agency_invocation_id", table_name="jobs")
    op.drop_index("ix_jobs_agency_task_id", table_name="jobs")
    op.drop_index("ix_jobs_agency_repo_id", table_name="jobs")
    op.drop_index("ix_jobs_action_attempt_id", table_name="jobs")
    op.drop_index("ix_jobs_turn_id", table_name="jobs")
    op.drop_index("ix_jobs_session_id", table_name="jobs")
    op.drop_constraint("fk_jobs_action_attempt_id", "jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_turn_id", "jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_session_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "discord_thread_id")
    op.drop_column("jobs", "agency_pr_url")
    op.drop_column("jobs", "agency_pr_number")
    op.drop_column("jobs", "agency_last_synced_at")
    op.drop_column("jobs", "agency_request_id")
    op.drop_column("jobs", "agency_runner")
    op.drop_column("jobs", "agency_branch")
    op.drop_column("jobs", "agency_worktree_path")
    op.drop_column("jobs", "agency_worktree_id")
    op.drop_column("jobs", "agency_invocation_id")
    op.drop_column("jobs", "agency_task_id")
    op.drop_column("jobs", "agency_repo_id")
    op.drop_column("jobs", "agency_repo_root")
    op.drop_column("jobs", "action_attempt_id")
    op.drop_column("jobs", "turn_id")
    op.drop_column("jobs", "session_id")
