"""drop the 18 old proactive tables

P4d of the proactivity crystallization cutover. P4a-c removed the old proactive
API, worker engine, and modules; this migration drops the 18 tables they owned:
the 8 ``proactive_*`` tables, ``autonomy_scopes``, the 5 ``work_*`` tables,
``leave_by_reminders``, ``notifications``, ``notification_deliveries``, and
``email_thread_watches``. Proactivity now rides the shared agent loop and
``background_tasks``; none of these tables has a surviving reader or writer.

``proactive_cases`` and ``proactive_decisions`` form a circular foreign-key
cycle (``proactive_cases.latest_observation_id``,
``proactive_cases.last_decision_id``, ``proactive_decisions.case_id``). The
upgrade drops those three constraints first, then drops every table
dependents-before-targets; the downgrade creates the two tables without their
circular foreign keys, then adds the three constraints once both tables exist.

Revision ID: 20260518_0048
Revises: 20260518_0047
Create Date: 2026-05-18 17:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260518_0048"
down_revision = "20260518_0047"
branch_labels = None
depends_on = None


# Tables dropped dependents-first, after the three circular foreign keys among
# proactive_cases and proactive_decisions are removed.
_DROP_ORDER = (
    "leave_by_reminders",
    "notification_deliveries",
    "notifications",
    "proactive_learning_records",
    "proactive_feedback",
    "proactive_action_executions",
    "proactive_action_plans",
    "proactive_case_events",
    "proactive_cases",
    "proactive_decisions",
    "proactive_observations",
    "work_follow_up_events",
    "work_follow_up_loops",
    "work_commitment_sources",
    "work_commitments",
    "work_threads",
    "email_thread_watches",
    "autonomy_scopes",
)


def upgrade() -> None:
    # Break the circular foreign keys before dropping the cycle's tables.
    op.drop_constraint(
        "proactive_cases_latest_observation_id_fkey", "proactive_cases", type_="foreignkey"
    )
    op.drop_constraint("fk_proactive_cases_last_decision_id", "proactive_cases", type_="foreignkey")
    op.drop_constraint(
        "proactive_decisions_case_id_fkey", "proactive_decisions", type_="foreignkey"
    )

    for table_name in _DROP_ORDER:
        op.drop_table(table_name)


def downgrade() -> None:
    op.create_table(
        "proactive_observations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("discord_message_id", sa.String(length=32), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("observation_type", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trust_boundary", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('discord_message', 'job', 'approval_request', "
            "'memory_assertion', 'google_connector', 'capture')",
            name="ck_proactive_observation_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('new', 'linked', 'ignored')",
            name="ck_proactive_observation_status",
        ),
        sa.CheckConstraint(
            "trust_boundary IN ('trusted_internal', 'reviewed_memory', 'user', "
            "'provider', 'tainted')",
            name="ck_proactive_observation_trust_boundary",
        ),
        sa.ForeignKeyConstraint(
            ["discord_message_id"], ["discord_messages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_proactive_observations_discord_message_id",
        "proactive_observations",
        ["discord_message_id"],
    )
    op.create_index(
        "ix_proactive_observations_observed_at", "proactive_observations", ["observed_at"]
    )
    op.create_index(
        "ix_proactive_observations_source",
        "proactive_observations",
        ["source_type", "source_id"],
    )
    op.create_index(
        "ix_proactive_observations_status_updated",
        "proactive_observations",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_proactive_observations_created_at", "proactive_observations", ["created_at"]
    )
    op.create_index(
        "ix_proactive_observations_updated_at", "proactive_observations", ["updated_at"]
    )

    # proactive_decisions and proactive_cases form an FK cycle: create both
    # without the circular FKs, then add the three constraints below.
    op.create_table(
        "proactive_decisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("urgency", sa.String(length=32), nullable=False),
        sa.Column("user_visible_message", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tool_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("follow_up", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("omitted_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_result", sa.String(length=32), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("action_plan_hash", sa.String(length=128), nullable=True),
        sa.Column("policy_constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("ai_judgment_id", sa.String(length=32), nullable=False),
        sa.Column("memory_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_proactive_decision_confidence",
        ),
        sa.CheckConstraint(
            "decision_type IN ('ignore', 'remember', 'wait', 'observe_more', "
            "'speak_now', 'ask_user', 'act_now', 'speak_and_act')",
            name="ck_proactive_decision_type",
        ),
        sa.CheckConstraint(
            "policy_result IN ('authorized', 'authorized_with_constraints', 'denied', "
            "'needs_user_authority', 'stale_context', 'invalid_decision', "
            "'duplicate', 'dead_letter')",
            name="ck_proactive_decision_policy_result",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'invalid', 'validated', 'executed', 'ignored')",
            name="ck_proactive_decision_status",
        ),
        sa.CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_proactive_decision_urgency",
        ),
        sa.ForeignKeyConstraint(
            ["ai_judgment_id"],
            ["ai_judgments.id"],
            name="fk_proactive_decisions_ai_judgment_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_decisions_case_created",
        "proactive_decisions",
        ["case_id", "created_at"],
    )
    op.create_index("ix_proactive_decisions_case_id", "proactive_decisions", ["case_id"])
    op.create_index("ix_proactive_decisions_created_at", "proactive_decisions", ["created_at"])

    op.create_table(
        "proactive_cases",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_key", sa.String(length=220), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("latest_observation_id", sa.String(length=32), nullable=False),
        sa.Column("last_decision_id", sa.String(length=32), nullable=True),
        sa.Column("next_recheck_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'waiting', 'spoken', 'acted', 'asked', 'ignored', "
            "'acknowledged', 'resolved', 'failed')",
            name="ck_proactive_case_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_key"),
    )
    op.create_index("ix_proactive_cases_last_decision_id", "proactive_cases", ["last_decision_id"])
    op.create_index(
        "ix_proactive_cases_latest_observation_id",
        "proactive_cases",
        ["latest_observation_id"],
    )
    op.create_index(
        "ix_proactive_cases_next_recheck_after",
        "proactive_cases",
        ["next_recheck_after"],
    )
    op.create_index(
        "ix_proactive_cases_recheck",
        "proactive_cases",
        ["status", "next_recheck_after", "id"],
    )
    op.create_index(
        "ix_proactive_cases_status_updated", "proactive_cases", ["status", "updated_at"]
    )
    op.create_index("ix_proactive_cases_created_at", "proactive_cases", ["created_at"])
    op.create_index("ix_proactive_cases_updated_at", "proactive_cases", ["updated_at"])

    # The three circular foreign keys, now that both tables exist.
    op.create_foreign_key(
        "proactive_cases_latest_observation_id_fkey",
        "proactive_cases",
        "proactive_observations",
        ["latest_observation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_proactive_cases_last_decision_id",
        "proactive_cases",
        "proactive_decisions",
        ["last_decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "proactive_decisions_case_id_fkey",
        "proactive_decisions",
        "proactive_cases",
        ["case_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "proactive_case_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('opened', 'updated', 'context_built', 'decided', "
            "'validated', 'turn_created', 'action_planned', 'action_executed', "
            "'waiting', 'acknowledged', 'resolved', 'feedback_recorded', 'failed')",
            name="ck_proactive_case_event_type",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_proactive_case_events_case_id", "proactive_case_events", ["case_id"])
    op.create_index("ix_proactive_case_events_created_at", "proactive_case_events", ["created_at"])

    op.create_table(
        "proactive_action_plans",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("plan_key", sa.String(length=220), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=160), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=128), nullable=False),
        sa.Column("risk_tier", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "risk_tier IN ('low', 'medium', 'high', 'blocked')",
            name="ck_proactive_action_plan_risk_tier",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'authorized', 'denied', 'executing', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_proactive_action_plan_status",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["proactive_decisions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_key"),
    )
    op.create_index("ix_proactive_action_plans_case_id", "proactive_action_plans", ["case_id"])
    op.create_index(
        "ix_proactive_action_plans_case_status",
        "proactive_action_plans",
        ["case_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_proactive_action_plans_decision_id",
        "proactive_action_plans",
        ["decision_id"],
    )
    op.create_index(
        "ix_proactive_action_plans_created_at", "proactive_action_plans", ["created_at"]
    )
    op.create_index(
        "ix_proactive_action_plans_updated_at", "proactive_action_plans", ["updated_at"]
    )

    op.create_table(
        "proactive_action_executions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("action_plan_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=220), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_proactive_action_execution_status",
        ),
        sa.ForeignKeyConstraint(
            ["action_plan_id"], ["proactive_action_plans.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_proactive_action_executions_action_plan_id",
        "proactive_action_executions",
        ["action_plan_id"],
    )
    op.create_index(
        "ix_proactive_action_executions_created_at",
        "proactive_action_executions",
        ["created_at"],
    )
    op.create_index(
        "ix_proactive_action_executions_updated_at",
        "proactive_action_executions",
        ["updated_at"],
    )

    op.create_table(
        "proactive_feedback",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "feedback_type IN ('ack', 'correct', 'stop_pattern', 'more_aggressive', "
            "'useful', 'wrong', 'automatic_next_time')",
            name="ck_proactive_feedback_type",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_proactive_feedback_case_id", "proactive_feedback", ["case_id"])
    op.create_index("ix_proactive_feedback_created_at", "proactive_feedback", ["created_at"])

    op.create_table(
        "proactive_learning_records",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("feedback_id", sa.String(length=32), nullable=True),
        sa.Column("record_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("provider_response_id", sa.String(length=128), nullable=True),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "parse_status IN ('parsed', 'invalid_json', 'missing_output', 'schema_invalid')",
            name="ck_proactive_learning_parse_status",
        ),
        sa.CheckConstraint(
            "record_type IN ('instruction', 'example', 'calibration', 'preference', "
            "'source_preference', 'prompt_instruction', 'autonomy_request')",
            name="ck_proactive_learning_record_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'rejected')",
            name="ck_proactive_learning_record_status",
        ),
        sa.CheckConstraint(
            "validation_status IN ('valid', 'invalid')",
            name="ck_proactive_learning_validation_status",
        ),
        sa.ForeignKeyConstraint(["feedback_id"], ["proactive_feedback.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_learning_records_feedback_id",
        "proactive_learning_records",
        ["feedback_id"],
    )
    op.create_index(
        "ix_proactive_learning_records_created_at",
        "proactive_learning_records",
        ["created_at"],
    )
    op.create_index(
        "ix_proactive_learning_records_updated_at",
        "proactive_learning_records",
        ["updated_at"],
    )

    op.create_table(
        "autonomy_scopes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=220), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("source_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("target_system", sa.String(length=128), nullable=False),
        sa.Column(
            "allowed_target_systems",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("allowed_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "allowed_payload_shape",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("max_impact", sa.String(length=32), nullable=False),
        sa.Column("revocation_rule", sa.Text(), nullable=False),
        sa.Column("notification_rule", sa.String(length=32), nullable=False),
        sa.Column("audit_visibility", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "audit_visibility IN ('private', 'operator_visible')",
            name="ck_autonomy_scope_audit_visibility",
        ),
        sa.CheckConstraint(
            "max_impact IN ('low', 'medium', 'high')",
            name="ck_autonomy_scope_max_impact",
        ),
        sa.CheckConstraint(
            "notification_rule IN ('silent_audit', 'notify_after', 'notify_before')",
            name="ck_autonomy_scope_notification_rule",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'revoked')",
            name="ck_autonomy_scope_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_autonomy_scope_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_key"),
    )
    op.create_index(
        "ix_autonomy_scopes_status_action",
        "autonomy_scopes",
        ["status", "action_type", "target_system"],
    )
    op.create_index("ix_autonomy_scopes_created_at", "autonomy_scopes", ["created_at"])
    op.create_index("ix_autonomy_scopes_updated_at", "autonomy_scopes", ["updated_at"])

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
    op.create_index("ix_work_threads_last_evidence_id", "work_threads", ["last_evidence_id"])
    op.create_index("ix_work_threads_last_inbound_at", "work_threads", ["last_inbound_at"])
    op.create_index("ix_work_threads_last_outbound_at", "work_threads", ["last_outbound_at"])
    op.create_index(
        "ix_work_threads_provider_thread_unique",
        "work_threads",
        ["provider", "provider_account_id", "provider_thread_id"],
        unique=True,
    )
    op.create_index("ix_work_threads_created_at", "work_threads", ["created_at"])
    op.create_index("ix_work_threads_updated_at", "work_threads", ["updated_at"])

    op.create_table(
        "work_commitments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=False),
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
            "owner IN ('user', 'counterparty', 'shared', 'unknown')",
            name="ck_work_commitment_owner",
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_work_commitment_priority",
        ),
        sa.CheckConstraint("provider IN ('google')", name="ck_work_commitment_provider"),
        sa.CheckConstraint(
            "review_state IN ('unreviewed', 'review_required', 'approved', 'edited', 'rejected')",
            name="ck_work_commitment_review_state",
        ),
        sa.CheckConstraint(
            "(lifecycle_state != 'superseded') OR (superseded_by_commitment_id IS NOT NULL)",
            name="ck_work_commitment_superseded_link",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_evidence_id"], ["provider_evidence.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_commitment_id"], ["work_commitments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["work_threads.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_commitments_due_end", "work_commitments", ["due_end"])
    op.create_index("ix_work_commitments_due_start", "work_commitments", ["due_start"])
    op.create_index("ix_work_commitments_lifecycle_state", "work_commitments", ["lifecycle_state"])
    op.create_index(
        "ix_work_commitments_owner_lifecycle_state",
        "work_commitments",
        ["owner", "lifecycle_state", "updated_at"],
    )
    op.create_index(
        "ix_work_commitments_provider_state_due",
        "work_commitments",
        ["provider", "provider_account_id", "lifecycle_state", "due_start", "id"],
    )
    op.create_index(
        "ix_work_commitments_resolution_evidence_id",
        "work_commitments",
        ["resolution_evidence_id"],
    )
    op.create_index(
        "ix_work_commitments_superseded_by_commitment_id",
        "work_commitments",
        ["superseded_by_commitment_id"],
    )
    op.create_index("ix_work_commitments_thread_id", "work_commitments", ["thread_id"])
    op.create_index(
        "ix_work_commitments_thread_state",
        "work_commitments",
        ["thread_id", "lifecycle_state", "updated_at"],
    )
    op.create_index("ix_work_commitments_created_at", "work_commitments", ["created_at"])
    op.create_index("ix_work_commitments_updated_at", "work_commitments", ["updated_at"])
    op.create_index(
        "ix_work_commitments_active_source_unique",
        "work_commitments",
        ["provider", "provider_account_id", "dedupe_digest"],
        unique=True,
        postgresql_where=sa.text(
            "lifecycle_state IN ('active', 'waiting_on_user', "
            "'waiting_on_counterparty', 'scheduled', 'snoozed')"
        ),
    )

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
        "ix_work_commitment_sources_commitment_id",
        "work_commitment_sources",
        ["commitment_id"],
    )
    op.create_index(
        "ix_work_commitment_sources_evidence_id",
        "work_commitment_sources",
        ["evidence_id"],
    )
    op.create_index(
        "ix_work_commitment_sources_created_at",
        "work_commitment_sources",
        ["created_at"],
    )
    op.create_index(
        "ix_work_commitment_sources_unique",
        "work_commitment_sources",
        ["commitment_id", "evidence_id", "source_role"],
        unique=True,
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
        sa.ForeignKeyConstraint(
            ["last_evaluated_evidence_id"],
            ["provider_evidence.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["work_threads.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_follow_up_loops_commitment_id",
        "work_follow_up_loops",
        ["commitment_id"],
    )
    op.create_index(
        "ix_work_follow_up_loops_due",
        "work_follow_up_loops",
        ["state", "next_check_at", "id"],
    )
    op.create_index(
        "ix_work_follow_up_loops_last_evaluated_evidence_id",
        "work_follow_up_loops",
        ["last_evaluated_evidence_id"],
    )
    op.create_index(
        "ix_work_follow_up_loops_next_check_at",
        "work_follow_up_loops",
        ["next_check_at"],
    )
    op.create_index(
        "ix_work_follow_up_loops_next_notification_at",
        "work_follow_up_loops",
        ["next_notification_at"],
    )
    op.create_index(
        "ix_work_follow_up_loops_snoozed_until",
        "work_follow_up_loops",
        ["snoozed_until"],
    )
    op.create_index(
        "ix_work_follow_up_loops_stale_after",
        "work_follow_up_loops",
        ["stale_after"],
    )
    op.create_index("ix_work_follow_up_loops_thread_id", "work_follow_up_loops", ["thread_id"])
    op.create_index("ix_work_follow_up_loops_created_at", "work_follow_up_loops", ["created_at"])
    op.create_index("ix_work_follow_up_loops_updated_at", "work_follow_up_loops", ["updated_at"])

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
        "notifications",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
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
        sa.Column("proactive_case_id", sa.String(length=32), nullable=True),
        sa.Column("proactive_decision_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint("channel IN ('discord')", name="ck_notification_channel"),
        sa.CheckConstraint(
            "(source_type = 'proactive_turn') = "
            "(proactive_case_id IS NOT NULL AND proactive_decision_id IS NOT NULL)",
            name="ck_notification_proactive_shape",
        ),
        sa.CheckConstraint(
            "source_type IN ('agency_event', 'proactive_turn', 'approval', "
            "'connector_event', 'work_follow_up', 'leave_by')",
            name="ck_notification_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'acknowledged')",
            name="ck_notification_status",
        ),
        sa.ForeignKeyConstraint(
            ["proactive_case_id"],
            ["proactive_cases.id"],
            name="fk_notifications_proactive_case_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proactive_decision_id"],
            ["proactive_decisions.id"],
            name="fk_notifications_proactive_decision_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_notifications_proactive_case_id", "notifications", ["proactive_case_id"])
    op.create_index(
        "ix_notifications_proactive_decision_id",
        "notifications",
        ["proactive_decision_id"],
    )
    op.create_index("ix_notifications_source_id", "notifications", ["source_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
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
        "ix_notification_deliveries_notification_id",
        "notification_deliveries",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_deliveries_created_at",
        "notification_deliveries",
        ["created_at"],
    )

    op.create_table(
        "leave_by_reminders",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("calendar_id", sa.String(length=256), nullable=False),
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("event_summary", sa.Text(), nullable=True),
        sa.Column("event_location", sa.Text(), nullable=False),
        sa.Column("event_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_origin", sa.Text(), nullable=True),
        sa.Column("last_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("last_static_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("leave_by_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('scheduled', 'computed', 'notified', 'skipped', 'cancelled', 'failed')",
            name="ck_leave_by_reminder_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_leave_by_reminder_version"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_account_id",
            "calendar_id",
            "event_id",
            name="uq_leave_by_reminder_event",
        ),
    )
    op.create_index("ix_leave_by_reminders_next_check_at", "leave_by_reminders", ["next_check_at"])
    op.create_index(
        "ix_leave_by_reminders_notification_id",
        "leave_by_reminders",
        ["notification_id"],
    )
    op.create_index("ix_leave_by_reminders_created_at", "leave_by_reminders", ["created_at"])
    op.create_index("ix_leave_by_reminders_updated_at", "leave_by_reminders", ["updated_at"])

    op.create_table(
        "email_thread_watches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=128), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=256), nullable=False),
        sa.Column("anchor_message_id", sa.String(length=256), nullable=False),
        sa.Column("condition", sa.String(length=32), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("cancel_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_by_action_attempt_id", sa.String(length=32), nullable=False),
        sa.Column("matched_message_id", sa.String(length=256), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "condition IN ('no_reply_by_deadline', 'any_reply_arrives')",
            name="ck_email_thread_watch_condition",
        ),
        sa.CheckConstraint(
            "(matched_message_id IS NULL AND matched_at IS NULL) OR "
            "(matched_message_id IS NOT NULL AND matched_at IS NOT NULL)",
            name="ck_email_thread_watch_matched_fields_paired",
        ),
        sa.CheckConstraint("provider IN ('google')", name="ck_email_thread_watch_provider"),
        sa.CheckConstraint(
            "status IN ('active', 'due', 'completed', 'canceled', 'failed')",
            name="ck_email_thread_watch_status",
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'due', 'failed') "
            "AND canceled_at IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'canceled' "
            "AND canceled_at IS NOT NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' "
            "AND completed_at IS NOT NULL "
            "AND canceled_at IS NULL "
            "AND matched_message_id IS NOT NULL "
            "AND matched_at IS NOT NULL)",
            name="ck_email_thread_watch_status_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_action_attempt_id"],
            ["action_attempts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_thread_watches_created_by_action_attempt_id",
        "email_thread_watches",
        ["created_by_action_attempt_id"],
    )
    op.create_index("ix_email_thread_watches_deadline", "email_thread_watches", ["deadline"])
    op.create_index(
        "ix_email_thread_watches_due",
        "email_thread_watches",
        ["status", "deadline", "id"],
    )
    op.create_index(
        "ix_email_thread_watches_provider_thread_status",
        "email_thread_watches",
        ["provider", "provider_account_id", "provider_thread_id", "status"],
    )
    op.create_index("ix_email_thread_watches_created_at", "email_thread_watches", ["created_at"])
    op.create_index(
        "ix_email_thread_watches_idempotency_key_unique",
        "email_thread_watches",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_email_thread_watches_cancel_idempotency_key_unique",
        "email_thread_watches",
        ["cancel_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("cancel_idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_email_thread_watches_active_thread",
        "email_thread_watches",
        ["provider", "provider_account_id", "provider_thread_id", "condition", "deadline"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
