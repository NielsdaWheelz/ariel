"""reshape background_tasks to the simplified single-worker queue

Revision ID: 20260518_0047
Revises: 20260518_0046
Create Date: 2026-05-18 16:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260518_0047"
down_revision = "20260518_0046"
branch_labels = None
depends_on = None


# The surviving task types after the proactivity cutover deletes the proactive,
# leave-by, work-follow-up, notification, and stale-task-reaper machinery.
_TASK_TYPE_AFTER = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_remember', "
    "'memory_sweep', 'execute_action_attempt', 'google_object_hydration_due', "
    "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due')"
)
_TASK_TYPE_BEFORE = (
    "task_type IN ('agency_event_received', 'deliver_discord_notification', "
    "'expire_approvals', 'reap_stale_tasks', 'provider_event_received', "
    "'provider_sync_due', 'memory_remember', 'memory_sweep', "
    "'ambient_interpretation_due', 'proactive_deliberation_due', "
    "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
    "'proactive_action_execution_due', 'execute_action_attempt', "
    "'google_object_hydration_due', 'provider_evidence_extraction_due', "
    "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
    "'provider_write_reconcile_due', 'leave_by_scan_due', "
    "'leave_by_evaluate_due', 'agent_wake', 'provider_watch_renew_due', "
    "'provider_reconcile_sync_due')"
)

_WORK_FOLLOW_UP_SHAPE = (
    "(task_type = 'work_follow_up_evaluate_due' "
    "AND work_follow_up_loop_id IS NOT NULL "
    "AND work_follow_up_loop_version IS NOT NULL "
    "AND work_follow_up_loop_version > 0 "
    "AND work_follow_up_scheduled_for IS NOT NULL) OR "
    "(task_type != 'work_follow_up_evaluate_due' "
    "AND work_follow_up_loop_id IS NULL "
    "AND work_follow_up_loop_version IS NULL "
    "AND work_follow_up_scheduled_for IS NULL)"
)

_STATUS_VALUES = "status IN ('pending', 'running', 'completed', 'failed', 'dead_letter')"


def upgrade() -> None:
    # The single-threaded worker selects the earliest due row directly; the
    # claim protocol, heartbeat, dead-letter, and stale-task reaper are gone.
    # The plain ix_background_tasks_run_after index serves the earliest-due
    # query and is kept.
    op.drop_index("ix_background_tasks_claimable", table_name="background_tasks")
    op.drop_index("ix_background_tasks_work_follow_up_unique", table_name="background_tasks")
    op.drop_index("ix_background_tasks_work_follow_up_loop_id", table_name="background_tasks")
    op.drop_index("ix_background_tasks_work_follow_up_scheduled_for", table_name="background_tasks")
    op.drop_index("ix_background_tasks_last_heartbeat", table_name="background_tasks")

    op.drop_constraint("ck_background_task_status", "background_tasks", type_="check")
    op.drop_constraint("ck_background_task_work_follow_up_shape", "background_tasks", type_="check")
    op.drop_constraint(
        "ck_background_task_max_attempts_positive", "background_tasks", type_="check"
    )

    op.drop_constraint(
        "fk_background_tasks_work_follow_up_loop_id", "background_tasks", type_="foreignkey"
    )

    op.drop_column("background_tasks", "status")
    op.drop_column("background_tasks", "claimed_by")
    op.drop_column("background_tasks", "last_heartbeat")
    op.drop_column("background_tasks", "max_attempts")
    op.drop_column("background_tasks", "error")
    op.drop_column("background_tasks", "work_follow_up_loop_id")
    op.drop_column("background_tasks", "work_follow_up_loop_version")
    op.drop_column("background_tasks", "work_follow_up_scheduled_for")

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint("ck_background_task_type", "background_tasks", _TASK_TYPE_AFTER)


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint("ck_background_task_type", "background_tasks", _TASK_TYPE_BEFORE)

    op.add_column(
        "background_tasks",
        sa.Column("work_follow_up_scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("work_follow_up_loop_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("work_follow_up_loop_id", sa.String(length=32), nullable=True),
    )
    op.add_column("background_tasks", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "background_tasks",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.alter_column("background_tasks", "max_attempts", server_default=None)
    op.add_column(
        "background_tasks",
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("background_tasks", sa.Column("claimed_by", sa.String(length=128), nullable=True))
    op.add_column(
        "background_tasks",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.alter_column("background_tasks", "status", server_default=None)

    op.create_foreign_key(
        "fk_background_tasks_work_follow_up_loop_id",
        "background_tasks",
        "work_follow_up_loops",
        ["work_follow_up_loop_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_check_constraint(
        "ck_background_task_max_attempts_positive", "background_tasks", "max_attempts > 0"
    )
    op.create_check_constraint(
        "ck_background_task_work_follow_up_shape", "background_tasks", _WORK_FOLLOW_UP_SHAPE
    )
    op.create_check_constraint("ck_background_task_status", "background_tasks", _STATUS_VALUES)

    op.create_index("ix_background_tasks_last_heartbeat", "background_tasks", ["last_heartbeat"])
    op.create_index(
        "ix_background_tasks_work_follow_up_scheduled_for",
        "background_tasks",
        ["work_follow_up_scheduled_for"],
    )
    op.create_index(
        "ix_background_tasks_work_follow_up_loop_id",
        "background_tasks",
        ["work_follow_up_loop_id"],
    )
    op.create_index(
        "ix_background_tasks_work_follow_up_unique",
        "background_tasks",
        ["work_follow_up_loop_id", "work_follow_up_loop_version", "work_follow_up_scheduled_for"],
        unique=True,
        postgresql_where=sa.text("task_type = 'work_follow_up_evaluate_due'"),
    )
    op.create_index(
        "ix_background_tasks_claimable",
        "background_tasks",
        ["status", "run_after", "created_at"],
    )
