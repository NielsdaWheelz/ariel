"""add the agent_wake task type and the recurrence_seconds column

Revision ID: 20260518_0045
Revises: 20260518_0044
Create Date: 2026-05-18 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260518_0045"
down_revision = "20260518_0044"
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
    "'provider_write_reconcile_due', 'leave_by_scan_due', "
    "'leave_by_evaluate_due')"
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
    "'leave_by_evaluate_due', 'agent_wake')"
)


def upgrade() -> None:
    op.add_column(
        "background_tasks",
        sa.Column("recurrence_seconds", sa.Integer(), nullable=True),
    )

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_AFTER
    )


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type", "background_tasks", _BACKGROUND_TASK_TYPE_BEFORE
    )

    op.drop_column("background_tasks", "recurrence_seconds")
