"""add the user_message task type to background_tasks

Revision ID: 20260520_0050
Revises: 20260519_0049
Create Date: 2026-05-20 00:50:00
"""

from __future__ import annotations

from alembic import op


revision = "20260520_0050"
down_revision = "20260519_0049"
branch_labels = None
depends_on = None


_BEFORE = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_remember', "
    "'memory_sweep', 'execute_action_attempt', 'google_object_hydration_due', "
    "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due')"
)
_AFTER = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_remember', "
    "'memory_sweep', 'execute_action_attempt', 'google_object_hydration_due', "
    "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due', "
    "'user_message')"
)


def upgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint("ck_background_task_type", "background_tasks", _AFTER)


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint("ck_background_task_type", "background_tasks", _BEFORE)
