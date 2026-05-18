"""drop dead schema: work_people, connector_subscriptions, leaked scratch table

Revision ID: 20260517_0035
Revises: 20260517_0034
Create Date: 2026-05-17 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260517_0035"
down_revision = "20260517_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_work_commitments_counterparty_person_id", table_name="work_commitments")
    op.drop_index("ix_work_commitments_requester_person_id", table_name="work_commitments")
    op.drop_column("work_commitments", "counterparty_person_id")
    op.drop_column("work_commitments", "requester_person_id")

    op.drop_index("ix_work_people_updated_at", table_name="work_people")
    op.drop_index("ix_work_people_created_at", table_name="work_people")
    op.drop_index("ix_work_people_email_unique", table_name="work_people")
    op.drop_table("work_people")

    op.drop_index("ix_connector_subscriptions_updated_at", table_name="connector_subscriptions")
    op.drop_index("ix_connector_subscriptions_renewal", table_name="connector_subscriptions")
    op.drop_index("ix_connector_subscriptions_renew_after", table_name="connector_subscriptions")
    op.drop_index("ix_connector_subscriptions_last_error_at", table_name="connector_subscriptions")
    op.drop_index("ix_connector_subscriptions_expires_at", table_name="connector_subscriptions")
    op.drop_index("ix_connector_subscriptions_created_at", table_name="connector_subscriptions")
    op.drop_table("connector_subscriptions")

    op.drop_table("ai_judgment_type_cutover_20260514_0027")

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        "task_type IN ('agency_event_received', 'deliver_discord_notification', "
        "'expire_approvals', 'reap_stale_tasks', 'provider_event_received', "
        "'provider_sync_due', 'memory_extract_turn', "
        "'ambient_interpretation_due', 'proactive_deliberation_due', "
        "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
        "'proactive_action_execution_due', 'execute_action_attempt', "
        "'google_object_hydration_due', 'provider_evidence_extraction_due', "
        "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
        "'provider_write_reconcile_due')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        "task_type IN ('agency_event_received', 'deliver_discord_notification', "
        "'expire_approvals', 'reap_stale_tasks', "
        "'provider_subscription_renewal_due', 'provider_event_received', "
        "'provider_sync_due', 'memory_extract_turn', "
        "'ambient_interpretation_due', 'proactive_deliberation_due', "
        "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
        "'proactive_action_execution_due', 'execute_action_attempt', "
        "'google_object_hydration_due', 'provider_evidence_extraction_due', "
        "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
        "'provider_write_reconcile_due')",
    )

    op.create_table(
        "ai_judgment_type_cutover_20260514_0027",
        sa.Column("ai_judgment_id", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("ai_judgment_id"),
    )

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
        "ix_connector_subscriptions_last_error_at", "connector_subscriptions", ["last_error_at"]
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
        "work_people",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("email_address", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_work_person_provider"),
        sa.CheckConstraint(
            "relation IN ('user', 'counterparty', 'unknown')",
            name="ck_work_person_relation",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_people_email_unique",
        "work_people",
        ["provider", "provider_account_id", "email_address"],
        unique=True,
    )
    op.create_index("ix_work_people_created_at", "work_people", ["created_at"])
    op.create_index("ix_work_people_updated_at", "work_people", ["updated_at"])

    op.add_column(
        "work_commitments",
        sa.Column("requester_person_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "work_commitments",
        sa.Column("counterparty_person_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_work_commitments_requester_person_id",
        "work_commitments",
        "work_people",
        ["requester_person_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_work_commitments_counterparty_person_id",
        "work_commitments",
        "work_people",
        ["counterparty_person_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_work_commitments_requester_person_id", "work_commitments", ["requester_person_id"]
    )
    op.create_index(
        "ix_work_commitments_counterparty_person_id", "work_commitments", ["counterparty_person_id"]
    )
