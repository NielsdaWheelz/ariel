"""cut proactive attention over to provider sync signals

Revision ID: 20260501_0015
Revises: 20260501_0014
Create Date: 2026-05-01 00:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0015"
down_revision = "20260501_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM notifications WHERE source_type = 'attention_item'")
    op.drop_table("attention_item_events")
    op.drop_table("attention_items")
    op.drop_table("proactive_check_runs")
    op.drop_table("proactive_subscriptions")

    op.create_table(
        "connector_subscriptions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("channel_token", sa.Text(), nullable=True),
        sa.Column("provider_subscription_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renew_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_connector_subscription_provider"),
        sa.CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_connector_subscription_resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'renewal_due', 'expired', 'error', 'revoked')",
            name="ck_connector_subscription_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "resource_type", "resource_id", name="uq_subscription_resource"
        ),
    )
    op.create_index(
        "ix_connector_subscriptions_created_at", "connector_subscriptions", ["created_at"]
    )
    op.create_index(
        "ix_connector_subscriptions_expires_at", "connector_subscriptions", ["expires_at"]
    )
    op.create_index(
        "ix_connector_subscriptions_last_error_at",
        "connector_subscriptions",
        ["last_error_at"],
    )
    op.create_index(
        "ix_connector_subscriptions_renew_after", "connector_subscriptions", ["renew_after"]
    )
    op.create_index(
        "ix_connector_subscriptions_renewal",
        "connector_subscriptions",
        ["status", "renew_after", "id"],
    )
    op.create_index(
        "ix_connector_subscriptions_updated_at", "connector_subscriptions", ["updated_at"]
    )

    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column("cursor_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_sync_cursor_provider"),
        sa.CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_sync_cursor_resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'syncing', 'invalid', 'error', 'revoked')",
            name="ck_sync_cursor_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "resource_type", "resource_id", name="uq_sync_cursor_resource"
        ),
    )
    op.create_index("ix_sync_cursors_created_at", "sync_cursors", ["created_at"])
    op.create_index("ix_sync_cursors_last_error_at", "sync_cursors", ["last_error_at"])
    op.create_index(
        "ix_sync_cursors_last_successful_sync_at",
        "sync_cursors",
        ["last_successful_sync_at"],
    )
    op.create_index("ix_sync_cursors_updated_at", "sync_cursors", ["updated_at"])

    op.create_table(
        "provider_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("external_event_id", sa.String(length=160), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body_digest", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("provider IN ('google')", name="ck_provider_event_provider"),
        sa.CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_provider_event_resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'processed', 'failed')",
            name="ck_provider_event_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_provider_events_processed_at", "provider_events", ["processed_at"])
    op.create_index("ix_provider_events_received_at", "provider_events", ["received_at"])
    op.create_index(
        "ix_provider_events_resource",
        "provider_events",
        ["provider", "resource_type", "resource_id"],
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("provider_event_id", sa.String(length=32), nullable=True),
        sa.Column("cursor_before", sa.Text(), nullable=True),
        sa.Column("cursor_after", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("signal_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_sync_run_provider"),
        sa.CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_sync_run_resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_sync_run_status"
        ),
        sa.CheckConstraint("item_count >= 0", name="ck_sync_run_item_count"),
        sa.CheckConstraint("signal_count >= 0", name="ck_sync_run_signal_count"),
        sa.ForeignKeyConstraint(["provider_event_id"], ["provider_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_runs_created_at", "sync_runs", ["created_at"])
    op.create_index("ix_sync_runs_provider_event_id", "sync_runs", ["provider_event_id"])

    op.create_table(
        "workspace_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google', 'ariel')", name="ck_workspace_item_provider"),
        sa.CheckConstraint(
            "item_type IN ('calendar_event', 'email_message', 'drive_file', 'internal_state')",
            name="ck_workspace_item_type",
        ),
        sa.CheckConstraint("status IN ('active', 'deleted')", name="ck_workspace_item_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "item_type", "external_id", name="uq_workspace_item_external"
        ),
    )
    op.create_index("ix_workspace_items_created_at", "workspace_items", ["created_at"])
    op.create_index("ix_workspace_items_deleted_at", "workspace_items", ["deleted_at"])
    op.create_index(
        "ix_workspace_items_provider_type",
        "workspace_items",
        ["provider", "item_type", "updated_at"],
    )
    op.create_index("ix_workspace_items_updated_at", "workspace_items", ["updated_at"])

    op.create_table(
        "workspace_item_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_item_id", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("provider_event_id", sa.String(length=32), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('created', 'updated', 'deleted', 'restored')",
            name="ck_workspace_item_event_type",
        ),
        sa.ForeignKeyConstraint(["provider_event_id"], ["provider_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_item_id"], ["workspace_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_workspace_item_events_created_at", "workspace_item_events", ["created_at"])
    op.create_index(
        "ix_workspace_item_events_provider_event_id",
        "workspace_item_events",
        ["provider_event_id"],
    )
    op.create_index(
        "ix_workspace_item_events_workspace_item_id",
        "workspace_item_events",
        ["workspace_item_id"],
    )

    op.create_table(
        "attention_signals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_item_id", sa.String(length=32), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("urgency", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "source_type IN ('workspace_item', 'job', 'approval_request', "
                "'memory_assertion', 'google_connector', 'capture')"
            ),
            name="ck_attention_signal_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('new', 'reviewed', 'dismissed', 'superseded')",
            name="ck_attention_signal_status",
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_signal_priority",
        ),
        sa.CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_signal_urgency",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_attention_signal_confidence",
        ),
        sa.ForeignKeyConstraint(["workspace_item_id"], ["workspace_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_attention_signals_created_at", "attention_signals", ["created_at"])
    op.create_index(
        "ix_attention_signals_source", "attention_signals", ["source_type", "source_id"]
    )
    op.create_index(
        "ix_attention_signals_status_priority",
        "attention_signals",
        ["status", "priority", "updated_at"],
    )
    op.create_index("ix_attention_signals_updated_at", "attention_signals", ["updated_at"])
    op.create_index(
        "ix_attention_signals_workspace_item_id", "attention_signals", ["workspace_item_id"]
    )

    op.create_table(
        "attention_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_signal_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("urgency", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_follow_up_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('attention_signal')",
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

    op.create_table(
        "proactive_feedback",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("attention_item_id", sa.String(length=32), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "feedback_type IN ('important', 'noise', 'wrong', 'useful')",
            name="ck_proactive_feedback_type",
        ),
        sa.ForeignKeyConstraint(["attention_item_id"], ["attention_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_feedback_attention_item_id", "proactive_feedback", ["attention_item_id"]
    )
    op.create_index("ix_proactive_feedback_created_at", "proactive_feedback", ["created_at"])

    op.create_table(
        "action_proposals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("attention_item_id", sa.String(length=32), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("policy_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="ck_action_proposal_status",
        ),
        sa.ForeignKeyConstraint(["attention_item_id"], ["attention_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_action_proposals_attention_item_id", "action_proposals", ["attention_item_id"]
    )
    op.create_index("ix_action_proposals_created_at", "action_proposals", ["created_at"])
    op.create_index("ix_action_proposals_updated_at", "action_proposals", ["updated_at"])

    op.execute("DELETE FROM background_tasks WHERE task_type = 'proactive_check_due'")
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'workspace_signal_derivation_due', "
            "'attention_review_due', 'attention_item_follow_up_due', "
            "'action_proposal_review_due')"
        ),
    )


def downgrade() -> None:
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

    op.drop_table("action_proposals")
    op.drop_table("proactive_feedback")
    op.drop_table("attention_item_events")
    op.drop_table("attention_items")
    op.drop_table("attention_signals")
    op.drop_table("workspace_item_events")
    op.drop_table("workspace_items")
    op.drop_table("sync_runs")
    op.drop_table("provider_events")
    op.drop_table("sync_cursors")
    op.drop_table("connector_subscriptions")

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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_subscriptions_due",
        "proactive_subscriptions",
        ["status", "next_run_after", "id"],
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
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["proactive_subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_check_runs_subscription_id",
        "proactive_check_runs",
        ["subscription_id"],
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
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_follow_up_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["proactive_subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )

    op.create_table(
        "attention_item_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("attention_item_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attention_item_id"], ["attention_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
