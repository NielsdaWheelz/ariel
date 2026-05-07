"""add durable action execution task

Revision ID: 20260501_0019
Revises: 20260501_0018
Create Date: 2026-05-01 00:19:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260501_0019"
down_revision = "20260501_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn', "
            "'execute_action_attempt')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn')"
        ),
    )
