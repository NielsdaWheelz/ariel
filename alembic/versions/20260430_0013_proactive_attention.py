"""add proactive subscriptions and attention items

Revision ID: 20260430_0013
Revises: 20260430_0012
Create Date: 2026-04-30 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260430_0013"
down_revision = "20260430_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proactive_subscriptions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("next_run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notification_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "source_type IN ('open_jobs', 'pending_approvals', 'memory_commitments', "
                "'connector_health', 'quick_capture_review', 'calendar_watch', "
                "'email_watch', 'drive_watch')"
            ),
            name="ck_proactive_subscription_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'cancelled')",
            name="ck_proactive_subscription_status",
        ),
        sa.CheckConstraint(
            "check_interval_seconds >= 60",
            name="ck_proactive_subscription_interval_seconds",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_subscriptions_created_at", "proactive_subscriptions", ["created_at"]
    )
    op.create_index(
        "ix_proactive_subscriptions_due",
        "proactive_subscriptions",
        ["status", "next_run_after", "id"],
    )
    op.create_index(
        "ix_proactive_subscriptions_last_checked_at",
        "proactive_subscriptions",
        ["last_checked_at"],
    )
    op.create_index(
        "ix_proactive_subscriptions_next_run_after",
        "proactive_subscriptions",
        ["next_run_after"],
    )
    op.create_index(
        "ix_proactive_subscriptions_updated_at", "proactive_subscriptions", ["updated_at"]
    )

    op.create_table(
        "proactive_check_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("subscription_id", sa.String(length=32), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_attention_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_proactive_check_run_status",
        ),
        sa.CheckConstraint(
            "created_attention_count >= 0",
            name="ck_proactive_check_run_attention_count",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["proactive_subscriptions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "scheduled_for",
            name="uq_proactive_check_run_subscription_window",
        ),
    )
    op.create_index("ix_proactive_check_runs_created_at", "proactive_check_runs", ["created_at"])
    op.create_index(
        "ix_proactive_check_runs_scheduled_for", "proactive_check_runs", ["scheduled_for"]
    )
    op.create_index(
        "ix_proactive_check_runs_subscription_id", "proactive_check_runs", ["subscription_id"]
    )

    op.create_table(
        "attention_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("subscription_id", sa.String(length=32), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("urgency", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_follow_up_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "source_type IN ('job', 'approval_request', 'memory_assertion', "
                "'google_connector', 'capture', 'calendar_watch', 'email_watch', "
                "'drive_watch', 'manual_signal')"
            ),
            name="ck_attention_item_source_type",
        ),
        sa.CheckConstraint(
            (
                "status IN ('open', 'notified', 'acknowledged', 'snoozed', 'resolved', "
                "'expired', 'cancelled', 'superseded')"
            ),
            name="ck_attention_item_status",
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_item_priority",
        ),
        sa.CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_item_urgency",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_attention_item_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["proactive_subscriptions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_attention_items_created_at", "attention_items", ["created_at"])
    op.create_index("ix_attention_items_expires_at", "attention_items", ["expires_at"])
    op.create_index(
        "ix_attention_items_follow_up_due",
        "attention_items",
        ["status", "next_follow_up_after", "id"],
    )
    op.create_index(
        "ix_attention_items_next_follow_up_after",
        "attention_items",
        ["next_follow_up_after"],
    )
    op.create_index("ix_attention_items_source", "attention_items", ["source_type", "source_id"])
    op.create_index("ix_attention_items_source_id", "attention_items", ["source_id"])
    op.create_index(
        "ix_attention_items_status_priority",
        "attention_items",
        ["status", "priority", "updated_at"],
    )
    op.create_index("ix_attention_items_subscription_id", "attention_items", ["subscription_id"])
    op.create_index("ix_attention_items_updated_at", "attention_items", ["updated_at"])

    op.create_table(
        "attention_item_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("attention_item_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "event_type IN ('detected', 'updated', 'notified', 'acknowledged', "
                "'snoozed', 'resolved', 'cancelled', 'expired', 'follow_up_queued', "
                "'refreshed')"
            ),
            name="ck_attention_item_event_type",
        ),
        sa.ForeignKeyConstraint(["attention_item_id"], ["attention_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attention_item_events_attention_item_id",
        "attention_item_events",
        ["attention_item_id"],
    )
    op.create_index("ix_attention_item_events_created_at", "attention_item_events", ["created_at"])

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', 'proactive_check_due', "
            "'attention_item_follow_up_due')"
        ),
    )
    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_source_type",
        "notifications",
        "source_type IN ('agency_event', 'attention_item', 'approval', 'connector_event')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_source_type",
        "notifications",
        "source_type IN ('agency_event')",
    )
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks')"
        ),
    )

    op.drop_index("ix_attention_item_events_created_at", table_name="attention_item_events")
    op.drop_index(
        "ix_attention_item_events_attention_item_id",
        table_name="attention_item_events",
    )
    op.drop_table("attention_item_events")
    op.drop_index("ix_attention_items_updated_at", table_name="attention_items")
    op.drop_index("ix_attention_items_subscription_id", table_name="attention_items")
    op.drop_index("ix_attention_items_status_priority", table_name="attention_items")
    op.drop_index("ix_attention_items_source_id", table_name="attention_items")
    op.drop_index("ix_attention_items_source", table_name="attention_items")
    op.drop_index("ix_attention_items_next_follow_up_after", table_name="attention_items")
    op.drop_index("ix_attention_items_follow_up_due", table_name="attention_items")
    op.drop_index("ix_attention_items_expires_at", table_name="attention_items")
    op.drop_index("ix_attention_items_created_at", table_name="attention_items")
    op.drop_table("attention_items")
    op.drop_index("ix_proactive_check_runs_subscription_id", table_name="proactive_check_runs")
    op.drop_index("ix_proactive_check_runs_scheduled_for", table_name="proactive_check_runs")
    op.drop_index("ix_proactive_check_runs_created_at", table_name="proactive_check_runs")
    op.drop_table("proactive_check_runs")
    op.drop_index("ix_proactive_subscriptions_updated_at", table_name="proactive_subscriptions")
    op.drop_index(
        "ix_proactive_subscriptions_next_run_after",
        table_name="proactive_subscriptions",
    )
    op.drop_index(
        "ix_proactive_subscriptions_last_checked_at",
        table_name="proactive_subscriptions",
    )
    op.drop_index("ix_proactive_subscriptions_due", table_name="proactive_subscriptions")
    op.drop_index("ix_proactive_subscriptions_created_at", table_name="proactive_subscriptions")
    op.drop_table("proactive_subscriptions")
