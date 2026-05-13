"""add google workspace reasoning work graph

Revision ID: 20260512_0024
Revises: 20260508_0023
Create Date: 2026-05-12 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260512_0024"
down_revision = "20260508_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
        "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
        "'ambient_interpretation', 'proactive_deliberation', 'model_output', "
        "'workspace_commitment_extraction')",
    )

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
    op.add_column(
        "background_tasks",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("work_follow_up_loop_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("work_follow_up_loop_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("work_follow_up_scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("provider_write_receipt_id", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_background_task_work_follow_up_shape",
        "background_tasks",
        "(task_type = 'work_follow_up_evaluate_due' "
        "AND work_follow_up_loop_id IS NOT NULL "
        "AND work_follow_up_loop_version IS NOT NULL "
        "AND work_follow_up_loop_version > 0 "
        "AND work_follow_up_scheduled_for IS NOT NULL) OR "
        "(task_type != 'work_follow_up_evaluate_due' "
        "AND work_follow_up_loop_id IS NULL "
        "AND work_follow_up_loop_version IS NULL "
        "AND work_follow_up_scheduled_for IS NULL)",
    )
    op.create_check_constraint(
        "ck_background_task_provider_write_reconcile_shape",
        "background_tasks",
        "(task_type = 'provider_write_reconcile_due' "
        "AND provider_write_receipt_id IS NOT NULL) OR "
        "(task_type != 'provider_write_reconcile_due' "
        "AND provider_write_receipt_id IS NULL)",
    )
    op.create_index(
        "ix_background_tasks_idempotency_key_unique",
        "background_tasks",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_background_tasks_work_follow_up_loop_id",
        "background_tasks",
        ["work_follow_up_loop_id"],
    )
    op.create_index(
        "ix_background_tasks_work_follow_up_scheduled_for",
        "background_tasks",
        ["work_follow_up_scheduled_for"],
    )
    op.create_index(
        "ix_background_tasks_provider_write_receipt_id",
        "background_tasks",
        ["provider_write_receipt_id"],
    )
    op.create_index(
        "ix_background_tasks_work_follow_up_unique",
        "background_tasks",
        [
            "work_follow_up_loop_id",
            "work_follow_up_loop_version",
            "work_follow_up_scheduled_for",
        ],
        unique=True,
        postgresql_where=sa.text("task_type = 'work_follow_up_evaluate_due'"),
    )
    op.create_index(
        "ix_background_tasks_provider_write_reconcile_unique",
        "background_tasks",
        ["provider_write_receipt_id"],
        unique=True,
        postgresql_where=sa.text("task_type = 'provider_write_reconcile_due'"),
    )
    op.create_table(
        "action_private_payloads",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("payload_kind", sa.String(length=64), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_enc", sa.Text(), nullable=False),
        sa.Column("encryption_key_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "payload_kind IN ('google_provider_write_input')",
            name="ck_action_private_payload_kind",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name="ck_action_private_payload_digest",
        ),
        sa.ForeignKeyConstraint(["action_attempt_id"], ["action_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_action_private_payloads_action_attempt_id",
        "action_private_payloads",
        ["action_attempt_id"],
        unique=True,
    )
    op.create_index(
        "ix_action_private_payloads_created_at",
        "action_private_payloads",
        ["created_at"],
    )

    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_source_type",
        "notifications",
        "source_type IN ('agency_event', 'proactive_turn', 'approval', "
        "'connector_event', 'work_follow_up')",
    )

    op.create_table(
        "google_provider_objects",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("thread_external_id", sa.String(length=256), nullable=True),
        sa.Column("calendar_id", sa.String(length=256), nullable=True),
        sa.Column("ical_uid", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_url", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "object_type IN ('gmail_message', 'gmail_thread', 'calendar_event', "
            "'calendar_availability')",
            name="ck_google_provider_object_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deleted', 'stale', 'unavailable')",
            name="ck_google_provider_object_status",
        ),
        sa.CheckConstraint(
            "(object_type != 'calendar_event') OR (calendar_id IS NOT NULL)",
            name="ck_google_provider_object_calendar_identity",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_google_provider_object_identity_unique",
        "google_provider_objects",
        ["provider_account_id", "object_type", "external_id"],
        unique=True,
        postgresql_where=sa.text("object_type != 'calendar_event'"),
    )
    op.create_index(
        "ix_google_provider_objects_calendar_event_identity_unique",
        "google_provider_objects",
        ["provider_account_id", "object_type", "calendar_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("object_type = 'calendar_event'"),
    )
    op.create_index(
        "ix_google_provider_objects_thread",
        "google_provider_objects",
        ["provider_account_id", "thread_external_id"],
    )
    op.create_index(
        "ix_google_provider_objects_thread_external_id",
        "google_provider_objects",
        ["thread_external_id"],
    )
    op.create_index(
        "ix_google_provider_objects_calendar_id",
        "google_provider_objects",
        ["calendar_id"],
    )
    op.create_index(
        "ix_google_provider_objects_content_digest",
        "google_provider_objects",
        ["content_digest"],
    )
    op.create_index("ix_google_provider_objects_ical_uid", "google_provider_objects", ["ical_uid"])
    op.create_index(
        "ix_google_provider_objects_source_timestamp",
        "google_provider_objects",
        ["source_timestamp"],
    )
    op.create_index(
        "ix_google_provider_objects_created_at", "google_provider_objects", ["created_at"]
    )
    op.create_index(
        "ix_google_provider_objects_updated_at", "google_provider_objects", ["updated_at"]
    )

    op.create_table(
        "provider_evidence",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider_object_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("thread_external_id", sa.String(length=256), nullable=True),
        sa.Column("calendar_id", sa.String(length=256), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", sa.String(length=32), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("retention_policy", sa.String(length=32), nullable=False),
        sa.Column("extraction_status", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_provider_evidence_provider"),
        sa.CheckConstraint(
            "source_kind IN ('gmail_message', 'gmail_thread', 'calendar_event', "
            "'calendar_availability')",
            name="ck_provider_evidence_source_kind",
        ),
        sa.CheckConstraint(
            "taint IN ('provider_untrusted', 'provider_metadata', 'internal')",
            name="ck_provider_evidence_taint",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('normal', 'private', 'restricted')",
            name="ck_provider_evidence_sensitivity",
        ),
        sa.CheckConstraint(
            "retention_policy IN ('provider_source', 'short_lived', 'user_pinned')",
            name="ck_provider_evidence_retention_policy",
        ),
        sa.CheckConstraint(
            "extraction_status IN ('pending', 'extracted', 'not_actionable', 'failed')",
            name="ck_provider_evidence_extraction_status",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('available', 'superseded', 'redacted', 'deleted', "
            "'stale', 'unavailable')",
            name="ck_provider_evidence_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["provider_object_id"],
            ["google_provider_objects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_evidence_identity_digest_unique",
        "provider_evidence",
        ["provider_object_id", "content_digest"],
        unique=True,
    )
    op.create_index(
        "ix_provider_evidence_source",
        "provider_evidence",
        ["provider", "provider_account_id", "source_kind", "external_id"],
    )
    op.create_index(
        "ix_provider_evidence_provider_object_id", "provider_evidence", ["provider_object_id"]
    )
    op.create_index(
        "ix_provider_evidence_thread_external_id", "provider_evidence", ["thread_external_id"]
    )
    op.create_index("ix_provider_evidence_calendar_id", "provider_evidence", ["calendar_id"])
    op.create_index(
        "ix_provider_evidence_source_timestamp", "provider_evidence", ["source_timestamp"]
    )
    op.create_index("ix_provider_evidence_created_at", "provider_evidence", ["created_at"])
    op.create_index("ix_provider_evidence_updated_at", "provider_evidence", ["updated_at"])

    op.create_table(
        "provider_evidence_blocks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("block_kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("source_offsets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("block_index >= 0", name="ck_provider_evidence_block_index"),
        sa.CheckConstraint(
            "block_kind IN ('body', 'html_body', 'quote', 'forwarded', 'signature', "
            "'calendar_description', 'availability')",
            name="ck_provider_evidence_block_kind",
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["provider_evidence.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_evidence_blocks_unique",
        "provider_evidence_blocks",
        ["evidence_id", "block_index"],
        unique=True,
    )
    op.create_index(
        "ix_provider_evidence_blocks_evidence_id", "provider_evidence_blocks", ["evidence_id"]
    )
    op.create_index(
        "ix_provider_evidence_blocks_created_at", "provider_evidence_blocks", ["created_at"]
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

    op.create_table(
        "work_threads",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=256), nullable=False),
        sa.Column("normalized_subject", sa.Text(), nullable=False),
        sa.Column("participant_emails", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evidence_id", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_work_thread_provider"),
        sa.CheckConstraint(
            "state IN ('active', 'waiting_on_user', 'waiting_on_counterparty', "
            "'resolved', 'stale')",
            name="ck_work_thread_state",
        ),
        sa.ForeignKeyConstraint(
            ["last_evidence_id"], ["provider_evidence.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_threads_provider_thread_unique",
        "work_threads",
        ["provider", "provider_account_id", "provider_thread_id"],
        unique=True,
    )
    op.create_index("ix_work_threads_last_inbound_at", "work_threads", ["last_inbound_at"])
    op.create_index("ix_work_threads_last_outbound_at", "work_threads", ["last_outbound_at"])
    op.create_index("ix_work_threads_last_evidence_id", "work_threads", ["last_evidence_id"])
    op.create_index("ix_work_threads_created_at", "work_threads", ["created_at"])
    op.create_index("ix_work_threads_updated_at", "work_threads", ["updated_at"])

    op.create_table(
        "work_commitments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=False),
        sa.Column("requester_person_id", sa.String(length=32), nullable=True),
        sa.Column("counterparty_person_id", sa.String(length=32), nullable=True),
        sa.Column("thread_id", sa.String(length=32), nullable=True),
        sa.Column("dedupe_digest", sa.String(length=64), nullable=False),
        sa.Column("action_text", sa.Text(), nullable=False),
        sa.Column("action_category", sa.String(length=64), nullable=False),
        sa.Column("due_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("review_state", sa.String(length=32), nullable=False),
        sa.Column("resolution_evidence_id", sa.String(length=32), nullable=True),
        sa.Column("superseded_by_commitment_id", sa.String(length=32), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_work_commitment_provider"),
        sa.CheckConstraint(
            "owner IN ('user', 'counterparty', 'shared', 'unknown')",
            name="ck_work_commitment_owner",
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_work_commitment_priority",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_work_commitment_confidence",
        ),
        sa.CheckConstraint(
            "(due_end IS NULL) OR (due_start IS NULL) OR (due_start < due_end)",
            name="ck_work_commitment_due_interval",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('candidate', 'needs_review', 'active', "
            "'waiting_on_user', 'waiting_on_counterparty', 'scheduled', 'snoozed', "
            "'resolved', 'superseded', 'dismissed', 'rejected', 'stale', 'expired', "
            "'deleted')",
            name="ck_work_commitment_lifecycle_state",
        ),
        sa.CheckConstraint(
            "review_state IN ('unreviewed', 'review_required', 'approved', 'edited', 'rejected')",
            name="ck_work_commitment_review_state",
        ),
        sa.CheckConstraint(
            "(lifecycle_state != 'superseded') OR (superseded_by_commitment_id IS NOT NULL)",
            name="ck_work_commitment_superseded_link",
        ),
        sa.ForeignKeyConstraint(["requester_person_id"], ["work_people.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["counterparty_person_id"], ["work_people.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["work_threads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["resolution_evidence_id"], ["provider_evidence.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_commitment_id"],
            ["work_commitments.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_commitments_provider_state_due",
        "work_commitments",
        ["provider", "provider_account_id", "lifecycle_state", "due_start", "id"],
    )
    op.create_index(
        "ix_work_commitments_thread_state",
        "work_commitments",
        ["thread_id", "lifecycle_state", "updated_at"],
    )
    op.create_index(
        "ix_work_commitments_lifecycle_state",
        "work_commitments",
        ["lifecycle_state"],
    )
    op.create_index(
        "ix_work_commitments_owner_lifecycle_state",
        "work_commitments",
        ["owner", "lifecycle_state", "updated_at"],
    )
    op.create_index("ix_work_commitments_due_start", "work_commitments", ["due_start"])
    op.create_index("ix_work_commitments_due_end", "work_commitments", ["due_end"])
    op.create_index("ix_work_commitments_thread_id", "work_commitments", ["thread_id"])
    op.create_index(
        "ix_work_commitments_active_source_unique",
        "work_commitments",
        [
            "provider",
            "provider_account_id",
            "dedupe_digest",
        ],
        unique=True,
        postgresql_where=sa.text(
            "lifecycle_state IN ('active', 'waiting_on_user', "
            "'waiting_on_counterparty', 'scheduled', 'snoozed')"
        ),
    )
    op.create_index(
        "ix_work_commitments_requester_person_id", "work_commitments", ["requester_person_id"]
    )
    op.create_index(
        "ix_work_commitments_counterparty_person_id", "work_commitments", ["counterparty_person_id"]
    )
    op.create_index(
        "ix_work_commitments_resolution_evidence_id", "work_commitments", ["resolution_evidence_id"]
    )
    op.create_index(
        "ix_work_commitments_superseded_by_commitment_id",
        "work_commitments",
        ["superseded_by_commitment_id"],
    )
    op.create_index("ix_work_commitments_created_at", "work_commitments", ["created_at"])
    op.create_index("ix_work_commitments_updated_at", "work_commitments", ["updated_at"])

    op.create_table(
        "work_commitment_sources",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("commitment_id", sa.String(length=32), nullable=False),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("block_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_role IN ('created', 'updated', 'resolved', 'superseded')",
            name="ck_work_commitment_source_role",
        ),
        sa.ForeignKeyConstraint(["commitment_id"], ["work_commitments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_id"], ["provider_evidence.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_commitment_sources_unique",
        "work_commitment_sources",
        ["commitment_id", "evidence_id", "source_role"],
        unique=True,
    )
    op.create_index(
        "ix_work_commitment_sources_commitment_id", "work_commitment_sources", ["commitment_id"]
    )
    op.create_index(
        "ix_work_commitment_sources_evidence_id", "work_commitment_sources", ["evidence_id"]
    )
    op.create_index(
        "ix_work_commitment_sources_created_at", "work_commitment_sources", ["created_at"]
    )

    op.create_table(
        "work_follow_up_loops",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("commitment_id", sa.String(length=32), nullable=True),
        sa.Column("thread_id", sa.String(length=32), nullable=True),
        sa.Column("loop_kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_evidence_id", sa.String(length=32), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_feedback", sa.String(length=32), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(commitment_id IS NOT NULL AND thread_id IS NULL) OR "
            "(commitment_id IS NULL AND thread_id IS NOT NULL)",
            name="ck_work_follow_up_loop_owner",
        ),
        sa.CheckConstraint(
            "loop_kind IN ('due_date', 'waiting_for_reply', 'needs_user_reply')",
            name="ck_work_follow_up_loop_kind",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'waiting', 'snoozed', 'notified', 'resolved', "
            "'stale', 'suppressed', 'deleted')",
            name="ck_work_follow_up_loop_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_work_follow_up_loop_version"),
        sa.ForeignKeyConstraint(["commitment_id"], ["work_commitments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["thread_id"], ["work_threads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["last_evaluated_evidence_id"],
            ["provider_evidence.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_follow_up_loops_due",
        "work_follow_up_loops",
        ["state", "next_check_at", "id"],
    )
    op.create_index(
        "ix_work_follow_up_loops_next_check_at",
        "work_follow_up_loops",
        ["next_check_at"],
    )
    op.create_index(
        "ix_work_follow_up_loops_commitment_id", "work_follow_up_loops", ["commitment_id"]
    )
    op.create_index("ix_work_follow_up_loops_thread_id", "work_follow_up_loops", ["thread_id"])
    op.create_index(
        "ix_work_follow_up_loops_next_notification_at",
        "work_follow_up_loops",
        ["next_notification_at"],
    )
    op.create_index("ix_work_follow_up_loops_stale_after", "work_follow_up_loops", ["stale_after"])
    op.create_index(
        "ix_work_follow_up_loops_snoozed_until", "work_follow_up_loops", ["snoozed_until"]
    )
    op.create_index(
        "ix_work_follow_up_loops_last_evaluated_evidence_id",
        "work_follow_up_loops",
        ["last_evaluated_evidence_id"],
    )
    op.create_index("ix_work_follow_up_loops_created_at", "work_follow_up_loops", ["created_at"])
    op.create_index("ix_work_follow_up_loops_updated_at", "work_follow_up_loops", ["updated_at"])
    op.create_foreign_key(
        "fk_background_tasks_work_follow_up_loop_id",
        "background_tasks",
        "work_follow_up_loops",
        ["work_follow_up_loop_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "work_follow_up_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("loop_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("loop_version > 0", name="ck_work_follow_up_event_loop_version"),
        sa.CheckConstraint(
            "event_type IN ('evaluated', 'scheduled', 'notified', 'suppressed', "
            "'snoozed', 'dismissed', 'resolved', 'stale_noop', 'failed')",
            name="ck_work_follow_up_event_type",
        ),
        sa.ForeignKeyConstraint(["loop_id"], ["work_follow_up_loops.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_follow_up_events_loop_id", "work_follow_up_events", ["loop_id"])
    op.create_index("ix_work_follow_up_events_created_at", "work_follow_up_events", ["created_at"])

    op.create_table(
        "provider_write_receipts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_object_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ambiguity_reason", sa.Text(), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_etag", sa.String(length=256), nullable=True),
        sa.Column("provider_history_id", sa.String(length=256), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google')", name="ck_provider_write_receipt_provider"),
        sa.CheckConstraint(
            "capability_id IN ('cap.email.draft', 'cap.email.send', "
            "'cap.email.archive', 'cap.email.trash', 'cap.email.labels.modify', "
            "'cap.email.undo', 'cap.calendar.create_event', 'cap.calendar.update_event', "
            "'cap.calendar.respond_to_event', 'cap.drive.share')",
            name="ck_provider_write_receipt_capability",
        ),
        sa.CheckConstraint(
            "status IN ('executing', 'succeeded', 'failed', 'ambiguous')",
            name="ck_provider_write_receipt_status",
        ),
        sa.CheckConstraint(
            "(status = 'ambiguous' AND ambiguity_reason IS NOT NULL) OR "
            "(status != 'ambiguous' AND ambiguity_reason IS NULL)",
            name="ck_provider_write_receipt_ambiguity_reason",
        ),
        sa.ForeignKeyConstraint(["action_attempt_id"], ["action_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_write_receipts_idempotency_unique",
        "provider_write_receipts",
        ["provider", "provider_account_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_provider_write_receipts_attempt_idempotency_unique",
        "provider_write_receipts",
        ["action_attempt_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_provider_write_receipts_action_attempt_id",
        "provider_write_receipts",
        ["action_attempt_id"],
    )
    op.create_index(
        "ix_provider_write_receipts_created_at", "provider_write_receipts", ["created_at"]
    )
    op.create_index(
        "ix_provider_write_receipts_updated_at", "provider_write_receipts", ["updated_at"]
    )
    op.create_index(
        "ix_provider_write_receipts_provider_timestamp",
        "provider_write_receipts",
        ["provider_timestamp"],
    )
    op.create_foreign_key(
        "fk_background_tasks_provider_write_receipt_id",
        "background_tasks",
        "provider_write_receipts",
        ["provider_write_receipt_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_background_tasks_provider_write_receipt_id",
        "background_tasks",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_background_tasks_provider_write_reconcile_unique",
        table_name="background_tasks",
    )
    op.drop_index(
        "ix_background_tasks_provider_write_receipt_id",
        table_name="background_tasks",
    )
    op.drop_constraint(
        "ck_background_task_provider_write_reconcile_shape",
        "background_tasks",
        type_="check",
    )
    op.drop_column("background_tasks", "provider_write_receipt_id")

    op.drop_index(
        "ix_action_private_payloads_created_at",
        table_name="action_private_payloads",
    )
    op.drop_index(
        "ix_action_private_payloads_action_attempt_id",
        table_name="action_private_payloads",
    )
    op.drop_table("action_private_payloads")

    op.drop_index(
        "ix_provider_write_receipts_provider_timestamp",
        table_name="provider_write_receipts",
    )
    op.drop_index("ix_provider_write_receipts_updated_at", table_name="provider_write_receipts")
    op.drop_index("ix_provider_write_receipts_created_at", table_name="provider_write_receipts")
    op.drop_index(
        "ix_provider_write_receipts_action_attempt_id", table_name="provider_write_receipts"
    )
    op.drop_index(
        "ix_provider_write_receipts_attempt_idempotency_unique",
        table_name="provider_write_receipts",
    )
    op.drop_index(
        "ix_provider_write_receipts_idempotency_unique", table_name="provider_write_receipts"
    )
    op.drop_table("provider_write_receipts")

    op.drop_index("ix_work_follow_up_events_created_at", table_name="work_follow_up_events")
    op.drop_index("ix_work_follow_up_events_loop_id", table_name="work_follow_up_events")
    op.drop_table("work_follow_up_events")

    op.execute("DELETE FROM background_tasks WHERE task_type = 'work_follow_up_evaluate_due'")
    op.drop_constraint(
        "fk_background_tasks_work_follow_up_loop_id",
        "background_tasks",
        type_="foreignkey",
    )
    op.drop_index("ix_background_tasks_work_follow_up_unique", table_name="background_tasks")
    op.drop_index(
        "ix_background_tasks_work_follow_up_scheduled_for",
        table_name="background_tasks",
    )
    op.drop_index(
        "ix_background_tasks_work_follow_up_loop_id",
        table_name="background_tasks",
    )
    op.drop_constraint(
        "ck_background_task_work_follow_up_shape",
        "background_tasks",
        type_="check",
    )
    op.drop_column("background_tasks", "work_follow_up_scheduled_for")
    op.drop_column("background_tasks", "work_follow_up_loop_version")
    op.drop_column("background_tasks", "work_follow_up_loop_id")

    op.drop_index("ix_work_follow_up_loops_updated_at", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_created_at", table_name="work_follow_up_loops")
    op.drop_index(
        "ix_work_follow_up_loops_last_evaluated_evidence_id",
        table_name="work_follow_up_loops",
    )
    op.drop_index("ix_work_follow_up_loops_snoozed_until", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_stale_after", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_next_notification_at", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_thread_id", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_commitment_id", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_next_check_at", table_name="work_follow_up_loops")
    op.drop_index("ix_work_follow_up_loops_due", table_name="work_follow_up_loops")
    op.drop_table("work_follow_up_loops")

    op.drop_index("ix_work_commitment_sources_created_at", table_name="work_commitment_sources")
    op.drop_index("ix_work_commitment_sources_evidence_id", table_name="work_commitment_sources")
    op.drop_index("ix_work_commitment_sources_commitment_id", table_name="work_commitment_sources")
    op.drop_index("ix_work_commitment_sources_unique", table_name="work_commitment_sources")
    op.drop_table("work_commitment_sources")

    op.drop_index("ix_work_commitments_updated_at", table_name="work_commitments")
    op.drop_index("ix_work_commitments_created_at", table_name="work_commitments")
    op.drop_index(
        "ix_work_commitments_superseded_by_commitment_id",
        table_name="work_commitments",
    )
    op.drop_index("ix_work_commitments_resolution_evidence_id", table_name="work_commitments")
    op.drop_index("ix_work_commitments_counterparty_person_id", table_name="work_commitments")
    op.drop_index("ix_work_commitments_requester_person_id", table_name="work_commitments")
    op.drop_index("ix_work_commitments_active_source_unique", table_name="work_commitments")
    op.drop_index("ix_work_commitments_thread_id", table_name="work_commitments")
    op.drop_index("ix_work_commitments_due_end", table_name="work_commitments")
    op.drop_index("ix_work_commitments_due_start", table_name="work_commitments")
    op.drop_index("ix_work_commitments_owner_lifecycle_state", table_name="work_commitments")
    op.drop_index("ix_work_commitments_lifecycle_state", table_name="work_commitments")
    op.drop_index("ix_work_commitments_thread_state", table_name="work_commitments")
    op.drop_index("ix_work_commitments_provider_state_due", table_name="work_commitments")
    op.drop_table("work_commitments")

    op.drop_index("ix_work_threads_updated_at", table_name="work_threads")
    op.drop_index("ix_work_threads_created_at", table_name="work_threads")
    op.drop_index("ix_work_threads_last_evidence_id", table_name="work_threads")
    op.drop_index("ix_work_threads_last_outbound_at", table_name="work_threads")
    op.drop_index("ix_work_threads_last_inbound_at", table_name="work_threads")
    op.drop_index("ix_work_threads_provider_thread_unique", table_name="work_threads")
    op.drop_table("work_threads")

    op.drop_index("ix_work_people_updated_at", table_name="work_people")
    op.drop_index("ix_work_people_created_at", table_name="work_people")
    op.drop_index("ix_work_people_email_unique", table_name="work_people")
    op.drop_table("work_people")

    op.drop_index("ix_provider_evidence_blocks_created_at", table_name="provider_evidence_blocks")
    op.drop_index("ix_provider_evidence_blocks_evidence_id", table_name="provider_evidence_blocks")
    op.drop_index("ix_provider_evidence_blocks_unique", table_name="provider_evidence_blocks")
    op.drop_table("provider_evidence_blocks")

    op.drop_index("ix_provider_evidence_updated_at", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_created_at", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_source_timestamp", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_calendar_id", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_thread_external_id", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_provider_object_id", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_source", table_name="provider_evidence")
    op.drop_index("ix_provider_evidence_identity_digest_unique", table_name="provider_evidence")
    op.drop_table("provider_evidence")

    op.drop_index("ix_google_provider_objects_updated_at", table_name="google_provider_objects")
    op.drop_index("ix_google_provider_objects_created_at", table_name="google_provider_objects")
    op.drop_index(
        "ix_google_provider_objects_source_timestamp",
        table_name="google_provider_objects",
    )
    op.drop_index("ix_google_provider_objects_ical_uid", table_name="google_provider_objects")
    op.drop_index("ix_google_provider_objects_content_digest", table_name="google_provider_objects")
    op.drop_index("ix_google_provider_objects_calendar_id", table_name="google_provider_objects")
    op.drop_index(
        "ix_google_provider_objects_calendar_event_identity_unique",
        table_name="google_provider_objects",
    )
    op.drop_index(
        "ix_google_provider_objects_thread_external_id",
        table_name="google_provider_objects",
    )
    op.drop_index("ix_google_provider_objects_thread", table_name="google_provider_objects")
    op.drop_index("ix_google_provider_object_identity_unique", table_name="google_provider_objects")
    op.drop_table("google_provider_objects")

    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.execute("DELETE FROM notifications WHERE source_type = 'work_follow_up'")
    op.create_check_constraint(
        "ck_notification_source_type",
        "notifications",
        "source_type IN ('agency_event', 'proactive_turn', 'approval', 'connector_event')",
    )

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.execute(
        "DELETE FROM background_tasks WHERE task_type IN ("
        "'google_object_hydration_due', 'provider_evidence_extraction_due', "
        "'workspace_commitment_extraction_due', 'work_follow_up_evaluate_due', "
        "'provider_write_reconcile_due')"
    )
    op.drop_index("ix_background_tasks_idempotency_key_unique", table_name="background_tasks")
    op.drop_column("background_tasks", "idempotency_key")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        "task_type IN ('agency_event_received', 'deliver_discord_notification', "
        "'expire_approvals', 'reap_stale_tasks', "
        "'provider_subscription_renewal_due', 'provider_event_received', "
        "'provider_sync_due', 'memory_extract_turn', "
        "'ambient_interpretation_due', 'proactive_deliberation_due', "
        "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
        "'proactive_action_execution_due', 'execute_action_attempt')",
    )

    op.drop_constraint("ck_ai_judgment_type", "ai_judgments", type_="check")
    op.execute("DELETE FROM ai_judgments WHERE judgment_type = 'workspace_commitment_extraction'")
    op.create_check_constraint(
        "ck_ai_judgment_type",
        "ai_judgments",
        "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
        "'memory_extraction', 'continuity_compaction', 'feedback_learning', "
        "'ambient_interpretation', 'proactive_deliberation', 'model_output')",
    )
