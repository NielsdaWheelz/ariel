"""remove undispatched background task types

Revision ID: 20260523_0065
Revises: 20260523_0064
Create Date: 2026-05-23 23:50:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260523_0065"
down_revision = "20260523_0064"
branch_labels = None
depends_on = None


_TASK_TYPE_BEFORE = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_encode', "
    "'memory_dream', 'execute_action_attempt', 'google_object_hydration_due', "
    "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due', "
    "'user_message', 'research_run')"
)
_TASK_TYPE_AFTER = (
    "task_type IN ('agency_event_received', 'expire_approvals', "
    "'provider_event_received', 'provider_sync_due', 'memory_encode', "
    "'memory_dream', 'execute_action_attempt', 'provider_write_reconcile_due', "
    "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due', "
    "'user_message', 'research_run')"
)


def upgrade() -> None:
    removed_rows = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT count(*)
                FROM background_tasks
                WHERE task_type IN (
                    'google_object_hydration_due',
                    'provider_evidence_extraction_due'
                )
                """
            )
        )
        .scalar_one()
    )
    if removed_rows:
        raise RuntimeError("undispatched background task rows must be repaired first")

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint("ck_background_task_type", "background_tasks", _TASK_TYPE_AFTER)


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint("ck_background_task_type", "background_tasks", _TASK_TYPE_BEFORE)
