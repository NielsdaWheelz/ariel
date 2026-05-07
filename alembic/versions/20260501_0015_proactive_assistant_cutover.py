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
        sa.Column("observation_count", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("observation_count >= 0", name="ck_sync_run_observation_count"),
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
        sa.CheckConstraint(
            "provider IN ('google', 'ariel', 'discord')",
            name="ck_workspace_item_provider",
        ),
        sa.CheckConstraint(
            (
                "item_type IN ('calendar_event', 'email_message', 'drive_file', "
                "'internal_state', 'discord_message')"
            ),
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

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks')"
        ),
    )

    op.drop_table("workspace_item_events")
    op.drop_table("workspace_items")
    op.drop_table("sync_runs")
    op.drop_table("provider_events")
    op.drop_table("sync_cursors")
    op.drop_table("connector_subscriptions")
