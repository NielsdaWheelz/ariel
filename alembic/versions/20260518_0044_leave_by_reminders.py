"""add leave_by_reminders and widen leave-by check enums

Revision ID: 20260518_0044
Revises: 20260518_0043
Create Date: 2026-05-18 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260518_0044"
down_revision = "20260518_0043"
branch_labels = None
depends_on = None


_BACKGROUND_TASK_TYPE_BEFORE = (
    "task_type IN ('agency_event_received', 'deliver_discord_notification', "
    "'expire_approvals', 'reap_stale_tasks', 'provider_event_received', "
    "'provider_sync_due', 'memory_remember', 'memory_sweep', "
    "'ambient_interpretation_due', 'proactive_deliberation_due', "
    "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
    "'proactive_action_execution_due', 'execute_action_attempt', "
    "'google_object_hydration_due', 'provider_evidence_extraction_due', "
    "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
    "'provider_write_reconcile_due')"
)
_BACKGROUND_TASK_TYPE_AFTER = (
    "task_type IN ('agency_event_received', 'deliver_discord_notification', "
    "'expire_approvals', 'reap_stale_tasks', 'provider_event_received', "
    "'provider_sync_due', 'memory_remember', 'memory_sweep', "
    "'ambient_interpretation_due', 'proactive_deliberation_due', "
    "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
    "'proactive_action_execution_due', 'execute_action_attempt', "
    "'google_object_hydration_due', 'provider_evidence_extraction_due', "
    "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
    "'provider_write_reconcile_due', 'leave_by_scan_due', "
    "'leave_by_evaluate_due')"
)

_NOTIFICATION_SOURCE_TYPE_BEFORE = (
    "source_type IN ('agency_event', 'proactive_turn', 'approval', "
    "'connector_event', 'work_follow_up')"
)
_NOTIFICATION_SOURCE_TYPE_AFTER = (
    "source_type IN ('agency_event', 'proactive_turn', 'approval', "
    "'connector_event', 'work_follow_up', 'leave_by')"
)

_AI_JUDGMENT_TYPE_BEFORE = (
    "judgment_type IN ('memory_recall', 'tool_result_interpretation', "
    "'memory_remember', 'feedback_learning', 'ambient_interpretation', "
    "'proactive_deliberation', 'model_output', "
    "'workspace_commitment_extraction')"
)
_AI_JUDGMENT_TYPE_AFTER = (
    "judgment_type IN ('memory_recall', 'tool_result_interpretation', "
    "'memory_remember', 'feedback_learning', 'ambient_interpretation', "
    "'proactive_deliberation', 'model_output', "
    "'workspace_commitment_extraction', 'leave_by_evaluation')"
)


def upgrade() -> None:
    op.create_table(
        "leave_by_reminders",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("calendar_id", sa.String(length=256), nullable=False),
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("event_summary", sa.Text(), nullable=True),
        sa.Column("event_location", sa.Text(), nullable=False),
        sa.Column("event_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_origin", sa.Text(), nullable=True),
        sa.Column("last_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("last_static_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("leave_by_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('scheduled', 'computed', 'notified', 'skipped', 'cancelled', 'failed')",
            name="ck_leave_by_reminder_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_leave_by_reminder_version"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_account_id",
            "calendar_id",
            "event_id",
            name="uq_leave_by_reminder_event",
        ),
    )
    op.create_index(
        "ix_leave_by_reminders_next_check_at",
        "leave_by_reminders",
        ["next_check_at"],
    )
    op.create_index(
        "ix_leave_by_reminders_notification_id",
        "leave_by_reminders",
        ["notification_id"],
    )
    op.create_index("ix_leave_by_reminders_created_at", "leave_by_reminders", ["created_at"])
    op.create_index("ix_leave_by_reminders_updated_at", "leave_by_reminders", ["updated_at"])

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_AFTER
    )

    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_source_type", "notifications", _NOTIFICATION_SOURCE_TYPE_AFTER
    )

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_AFTER)


def downgrade() -> None:
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint("ck_ai_judgment_type", "ai_judgments", _AI_JUDGMENT_TYPE_BEFORE)

    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_source_type", "notifications", _NOTIFICATION_SOURCE_TYPE_BEFORE
    )

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_BEFORE
    )

    op.drop_index("ix_leave_by_reminders_updated_at", table_name="leave_by_reminders")
    op.drop_index("ix_leave_by_reminders_created_at", table_name="leave_by_reminders")
    op.drop_index("ix_leave_by_reminders_notification_id", table_name="leave_by_reminders")
    op.drop_index("ix_leave_by_reminders_next_check_at", table_name="leave_by_reminders")
    op.drop_table("leave_by_reminders")
