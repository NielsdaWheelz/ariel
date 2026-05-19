"""add provider_watch_channels and the provider maintenance task types

Revision ID: 20260518_0046
Revises: 20260518_0045
Create Date: 2026-05-18 14:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260518_0046"
down_revision = "20260518_0045"
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
    "'leave_by_evaluate_due', 'agent_wake')"
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
    "'leave_by_evaluate_due', 'agent_wake', 'provider_watch_renew_due', "
    "'provider_reconcile_sync_due')"
)


def upgrade() -> None:
    op.create_table(
        "provider_watch_channels",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=True),
        sa.Column("channel_token", sa.String(length=255), nullable=True),
        sa.Column("provider_resource_id", sa.String(length=255), nullable=True),
        sa.Column("cursor_seed", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('google')",
            name="ck_provider_watch_channels_provider",
        ),
        sa.CheckConstraint(
            "resource_type IN ('gmail', 'calendar')",
            name="ck_provider_watch_channels_resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'failed')",
            name="ck_provider_watch_channels_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "resource_type",
            "resource_id",
            name="uq_provider_watch_channel_resource",
        ),
    )
    op.create_index(
        "ix_provider_watch_channels_expires_at",
        "provider_watch_channels",
        ["expires_at"],
    )
    op.create_index(
        "ix_provider_watch_channels_created_at",
        "provider_watch_channels",
        ["created_at"],
    )
    op.create_index(
        "ix_provider_watch_channels_updated_at",
        "provider_watch_channels",
        ["updated_at"],
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

    op.drop_index("ix_provider_watch_channels_updated_at", table_name="provider_watch_channels")
    op.drop_index("ix_provider_watch_channels_created_at", table_name="provider_watch_channels")
    op.drop_index("ix_provider_watch_channels_expires_at", table_name="provider_watch_channels")
    op.drop_table("provider_watch_channels")
