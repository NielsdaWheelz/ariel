"""add durable jobs, agency events, notifications, and tasks

Revision ID: 20260427_0010
Revises: 20260313_0009
Create Date: 2026-04-27 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260427_0010"
down_revision = "20260313_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_tasks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "task_type IN ('agency_event_received', 'deliver_discord_notification', "
                "'expire_approvals', 'reap_stale_tasks')"
            ),
            name="ck_background_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'dead_letter')",
            name="ck_background_task_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_background_task_attempts_nonnegative"),
        sa.CheckConstraint("max_attempts > 0", name="ck_background_task_max_attempts_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_background_tasks_created_at", "background_tasks", ["created_at"])
    op.create_index(
        "ix_background_tasks_claimable",
        "background_tasks",
        ["status", "run_after", "created_at"],
    )
    op.create_index("ix_background_tasks_last_heartbeat", "background_tasks", ["last_heartbeat"])
    op.create_index("ix_background_tasks_run_after", "background_tasks", ["run_after"])
    op.create_index("ix_background_tasks_updated_at", "background_tasks", ["updated_at"])

    op.create_table(
        "agency_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("external_job_id", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            (
                "event_type IN ('heartbeat', 'job.queued', 'job.started', 'job.progress', "
                "'job.waiting', 'job.completed', 'job.failed', 'job.cancelled', 'job.timed_out')"
            ),
            name="ck_agency_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'processed', 'failed')",
            name="ck_agency_event_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "external_event_id", name="uq_agency_event_source_external_id"
        ),
    )
    op.create_index("ix_agency_events_external_job_id", "agency_events", ["external_job_id"])
    op.create_index("ix_agency_events_processed_at", "agency_events", ["processed_at"])
    op.create_index("ix_agency_events_received_at", "agency_events", ["received_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_job_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("latest_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "status IN ('queued', 'running', 'waiting_approval', 'succeeded', "
                "'failed', 'cancelled', 'timed_out')"
            ),
            name="ck_job_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_job_id", name="uq_job_source_external_id"),
    )
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index("ix_jobs_updated_at", "jobs", ["updated_at"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("agency_event_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agency_event_id"], ["agency_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agency_event_id"),
    )
    op.create_index("ix_job_events_agency_event_id", "job_events", ["agency_event_id"])
    op.create_index("ix_job_events_created_at", "job_events", ["created_at"])
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source_type IN ('agency_event')", name="ck_notification_source_type"),
        sa.CheckConstraint("channel IN ('discord')", name="ck_notification_channel"),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'acknowledged')",
            name="ck_notification_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index("ix_notifications_source_id", "notifications", ["source_id"])
    op.create_index("ix_notifications_updated_at", "notifications", ["updated_at"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("notification_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel IN ('discord')", name="ck_notification_delivery_channel"),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_notification_delivery_status",
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_deliveries_created_at", "notification_deliveries", ["created_at"]
    )
    op.create_index(
        "ix_notification_deliveries_notification_id",
        "notification_deliveries",
        ["notification_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_deliveries_notification_id", table_name="notification_deliveries"
    )
    op.drop_index("ix_notification_deliveries_created_at", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notifications_updated_at", table_name="notifications")
    op.drop_index("ix_notifications_source_id", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_index("ix_job_events_created_at", table_name="job_events")
    op.drop_index("ix_job_events_agency_event_id", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_updated_at", table_name="jobs")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_agency_events_received_at", table_name="agency_events")
    op.drop_index("ix_agency_events_processed_at", table_name="agency_events")
    op.drop_index("ix_agency_events_external_job_id", table_name="agency_events")
    op.drop_table("agency_events")
    op.drop_index("ix_background_tasks_updated_at", table_name="background_tasks")
    op.drop_index("ix_background_tasks_run_after", table_name="background_tasks")
    op.drop_index("ix_background_tasks_last_heartbeat", table_name="background_tasks")
    op.drop_index("ix_background_tasks_claimable", table_name="background_tasks")
    op.drop_index("ix_background_tasks_created_at", table_name="background_tasks")
    op.drop_table("background_tasks")
