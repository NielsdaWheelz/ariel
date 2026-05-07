"""cut proactive runtime over to AI deliberation

Revision ID: 20260501_0020
Revises: 20260501_0019
Create Date: 2026-05-01 04:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0020"
down_revision = "20260501_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_workspace_item_provider", "workspace_items", type_="check")
    op.create_check_constraint(
        "ck_workspace_item_provider",
        "workspace_items",
        "provider IN ('google', 'ariel', 'discord')",
    )
    op.drop_constraint("ck_workspace_item_type", "workspace_items", type_="check")
    op.create_check_constraint(
        "ck_workspace_item_type",
        "workspace_items",
        (
            "item_type IN ('calendar_event', 'email_message', 'drive_file', "
            "'internal_state', 'discord_message')"
        ),
    )

    op.create_table(
        "ai_judgments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("judgment_type", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("provider_response_id", sa.String(length=128), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=False),
        sa.Column("input_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("selected", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("omitted", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("uncertainty", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "judgment_type IN ('memory_curation', 'tool_result_interpretation', "
                "'continuity_compaction', 'feedback_learning', "
                "'ambient_interpretation', 'proactive_deliberation', 'model_output')"
            ),
            name="ck_ai_judgment_type",
        ),
        sa.CheckConstraint("status IN ('succeeded', 'failed')", name="ck_ai_judgment_status"),
        sa.CheckConstraint(
            (
                "parse_status IN ('not_required_no_candidates', 'parsed', 'invalid_json', "
                "'missing_output', 'schema_invalid')"
            ),
            name="ck_ai_judgment_parse_status",
        ),
        sa.CheckConstraint(
            "validation_status IN ('valid', 'invalid', 'not_validated')",
            name="ck_ai_judgment_validation_status",
        ),
        sa.CheckConstraint(
            (
                "failure_code IS NULL OR failure_code IN ("
                "'E_AI_JUDGMENT_REQUIRED', 'E_AI_JUDGMENT_CREDENTIALS', "
                "'E_AI_JUDGMENT_TIMEOUT', 'E_AI_JUDGMENT_INVALID_JSON', "
                "'E_AI_JUDGMENT_SCHEMA', 'E_AI_JUDGMENT_VALIDATION', "
                "'E_AI_JUDGMENT_BUDGET')"
            ),
            name="ck_ai_judgment_failure_code",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_judgments_created_at", "ai_judgments", ["created_at"])
    op.create_index("ix_ai_judgments_judgment_type", "ai_judgments", ["judgment_type"])
    op.create_index("ix_ai_judgments_source_id", "ai_judgments", ["source_id"])
    op.create_index("ix_ai_judgments_source_type", "ai_judgments", ["source_type"])
    op.create_index("ix_ai_judgments_updated_at", "ai_judgments", ["updated_at"])

    op.create_table(
        "proactive_observations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_item_id", sa.String(length=32), nullable=True),
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
            (
                "source_type IN ('workspace_item', 'job', 'approval_request', "
                "'memory_assertion', 'google_connector', 'capture')"
            ),
            name="ck_proactive_observation_source_type",
        ),
        sa.CheckConstraint(
            (
                "trust_boundary IN ('trusted_internal', 'reviewed_memory', 'user', "
                "'provider', 'tainted')"
            ),
            name="ck_proactive_observation_trust_boundary",
        ),
        sa.CheckConstraint(
            "status IN ('new', 'linked', 'ignored')",
            name="ck_proactive_observation_status",
        ),
        sa.ForeignKeyConstraint(["workspace_item_id"], ["workspace_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_proactive_observations_created_at", "proactive_observations", ["created_at"]
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
        "ix_proactive_observations_updated_at", "proactive_observations", ["updated_at"]
    )
    op.create_index(
        "ix_proactive_observations_workspace_item_id",
        "proactive_observations",
        ["workspace_item_id"],
    )

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
            (
                "status IN ('open', 'waiting', 'spoken', 'acted', 'asked', 'ignored', "
                "'acknowledged', 'resolved', 'failed')"
            ),
            name="ck_proactive_case_status",
        ),
        sa.ForeignKeyConstraint(
            ["latest_observation_id"], ["proactive_observations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_key"),
    )
    op.create_index("ix_proactive_cases_created_at", "proactive_cases", ["created_at"])
    op.create_index(
        "ix_proactive_cases_latest_observation_id",
        "proactive_cases",
        ["latest_observation_id"],
    )
    op.create_index("ix_proactive_cases_last_decision_id", "proactive_cases", ["last_decision_id"])
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
    op.create_index("ix_proactive_cases_updated_at", "proactive_cases", ["updated_at"])

    op.create_table(
        "proactive_case_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "event_type IN ('opened', 'updated', 'context_built', 'decided', "
                "'validated', 'turn_created', 'action_planned', 'action_executed', "
                "'waiting', 'acknowledged', 'resolved', 'feedback_recorded', 'failed')"
            ),
            name="ck_proactive_case_event_type",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_proactive_case_events_case_id", "proactive_case_events", ["case_id"])
    op.create_index("ix_proactive_case_events_created_at", "proactive_case_events", ["created_at"])

    op.create_table(
        "proactive_context_snapshots",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("snapshot_key", sa.String(length=220), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("omitted_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_key"),
    )
    op.create_index(
        "ix_proactive_context_snapshots_case_created",
        "proactive_context_snapshots",
        ["case_id", "created_at"],
    )
    op.create_index(
        "ix_proactive_context_snapshots_case_id", "proactive_context_snapshots", ["case_id"]
    )
    op.create_index(
        "ix_proactive_context_snapshots_created_at",
        "proactive_context_snapshots",
        ["created_at"],
    )

    op.create_table(
        "proactive_decisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("context_snapshot_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("provider_response_id", sa.String(length=160), nullable=True),
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
        sa.Column("raw_model_output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "decision_type IN ('ignore', 'remember', 'wait', 'observe_more', "
                "'speak_now', 'ask_user', 'act_now', 'speak_and_act')"
            ),
            name="ck_proactive_decision_type",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'invalid', 'validated', 'executed', 'ignored')",
            name="ck_proactive_decision_status",
        ),
        sa.CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_proactive_decision_urgency",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_proactive_decision_confidence",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["context_snapshot_id"],
            ["proactive_context_snapshots.id"],
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
    op.create_index(
        "ix_proactive_decisions_context_snapshot_id",
        "proactive_decisions",
        ["context_snapshot_id"],
    )
    op.create_index("ix_proactive_decisions_created_at", "proactive_decisions", ["created_at"])

    op.create_foreign_key(
        "fk_proactive_cases_last_decision_id",
        "proactive_cases",
        "proactive_decisions",
        ["last_decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "proactive_policy_validations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("action_plan_hash", sa.String(length=128), nullable=True),
        sa.Column("constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "result IN ('authorized', 'authorized_with_constraints', 'denied', "
                "'needs_user_authority', 'stale_context', 'invalid_decision', "
                "'duplicate', 'dead_letter')"
            ),
            name="ck_proactive_policy_validation_result",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["proactive_decisions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_policy_validations_case_id",
        "proactive_policy_validations",
        ["case_id"],
    )
    op.create_index(
        "ix_proactive_policy_validations_created_at",
        "proactive_policy_validations",
        ["created_at"],
    )
    op.create_index(
        "ix_proactive_policy_validations_decision",
        "proactive_policy_validations",
        ["decision_id", "created_at"],
    )
    op.create_index(
        "ix_proactive_policy_validations_decision_id",
        "proactive_policy_validations",
        ["decision_id"],
    )

    op.create_table(
        "proactive_turns",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivery_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'acknowledged', 'failed', 'cancelled')",
            name="ck_proactive_turn_status",
        ),
        sa.CheckConstraint("origin IN ('proactive')", name="ck_proactive_turn_origin"),
        sa.CheckConstraint("channel IN ('discord')", name="ck_proactive_turn_channel"),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["proactive_decisions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_proactive_turns_case_id", "proactive_turns", ["case_id"])
    op.create_index("ix_proactive_turns_created_at", "proactive_turns", ["created_at"])
    op.create_index("ix_proactive_turns_decision_id", "proactive_turns", ["decision_id"])
    op.create_index(
        "ix_proactive_turns_status_updated", "proactive_turns", ["status", "updated_at"]
    )
    op.create_index("ix_proactive_turns_updated_at", "proactive_turns", ["updated_at"])

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
        sa.Column("policy_validation_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "status IN ('proposed', 'authorized', 'denied', 'executing', "
                "'succeeded', 'failed', 'cancelled')"
            ),
            name="ck_proactive_action_plan_status",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('low', 'medium', 'high', 'blocked')",
            name="ck_proactive_action_plan_risk_tier",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["proactive_cases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["proactive_decisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["policy_validation_id"],
            ["proactive_policy_validations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_key"),
    )
    op.create_index(
        "ix_proactive_action_plans_case_status",
        "proactive_action_plans",
        ["case_id", "status", "updated_at"],
    )
    op.create_index("ix_proactive_action_plans_case_id", "proactive_action_plans", ["case_id"])
    op.create_index(
        "ix_proactive_action_plans_created_at", "proactive_action_plans", ["created_at"]
    )
    op.create_index(
        "ix_proactive_action_plans_decision_id",
        "proactive_action_plans",
        ["decision_id"],
    )
    op.create_index(
        "ix_proactive_action_plans_policy_validation_id",
        "proactive_action_plans",
        ["policy_validation_id"],
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
        "autonomy_scopes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=220), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("source_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("target_system", sa.String(length=128), nullable=False),
        sa.Column(
            "allowed_target_systems", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("allowed_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allowed_payload_shape", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "status IN ('active', 'paused', 'revoked')",
            name="ck_autonomy_scope_status",
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
            "audit_visibility IN ('private', 'operator_visible')",
            name="ck_autonomy_scope_audit_visibility",
        ),
        sa.CheckConstraint("version >= 1", name="ck_autonomy_scope_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_key"),
    )
    op.create_index("ix_autonomy_scopes_created_at", "autonomy_scopes", ["created_at"])
    op.create_index(
        "ix_autonomy_scopes_status_action",
        "autonomy_scopes",
        ["status", "action_type", "target_system"],
    )
    op.create_index("ix_autonomy_scopes_updated_at", "autonomy_scopes", ["updated_at"])

    op.create_table(
        "proactive_feedback",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=32), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            (
                "feedback_type IN ('ack', 'correct', 'stop_pattern', 'more_aggressive', "
                "'useful', 'wrong', 'automatic_next_time')"
            ),
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
            (
                "record_type IN ('instruction', 'example', 'calibration', "
                "'preference', 'source_preference', 'prompt_instruction', "
                "'autonomy_request')"
            ),
            name="ck_proactive_learning_record_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'rejected')",
            name="ck_proactive_learning_record_status",
        ),
        sa.CheckConstraint(
            "parse_status IN ('parsed', 'invalid_json', 'missing_output', 'schema_invalid')",
            name="ck_proactive_learning_parse_status",
        ),
        sa.CheckConstraint(
            "validation_status IN ('valid', 'invalid')",
            name="ck_proactive_learning_validation_status",
        ),
        sa.ForeignKeyConstraint(["feedback_id"], ["proactive_feedback.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proactive_learning_records_created_at",
        "proactive_learning_records",
        ["created_at"],
    )
    op.create_index(
        "ix_proactive_learning_records_feedback_id",
        "proactive_learning_records",
        ["feedback_id"],
    )
    op.create_index(
        "ix_proactive_learning_records_updated_at",
        "proactive_learning_records",
        ["updated_at"],
    )

    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn', "
            "'ambient_interpretation_due', 'proactive_deliberation_due', "
            "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
            "'proactive_action_execution_due', 'execute_action_attempt')"
        ),
    )
    op.drop_constraint("ck_notification_source_type", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_source_type",
        "notifications",
        "source_type IN ('agency_event', 'proactive_turn', 'approval', 'connector_event')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workspace_item_events WHERE workspace_item_id IN ("
        "SELECT id FROM workspace_items WHERE provider = 'discord' "
        "OR item_type = 'discord_message')"
    )
    op.execute(
        "DELETE FROM workspace_items WHERE provider = 'discord' OR item_type = 'discord_message'"
    )
    op.execute(
        "DELETE FROM background_tasks WHERE task_type IN ("
        "'ambient_interpretation_due', 'proactive_deliberation_due', "
        "'proactive_follow_up_due', 'proactive_feedback_learning_due', "
        "'proactive_action_execution_due')"
    )
    op.execute(
        "DELETE FROM notification_deliveries WHERE notification_id IN ("
        "SELECT id FROM notifications WHERE source_type = 'proactive_turn')"
    )
    op.execute("DELETE FROM notifications WHERE source_type = 'proactive_turn'")

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
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn', 'execute_action_attempt')"
        ),
    )
    op.drop_constraint("ck_workspace_item_type", "workspace_items", type_="check")
    op.create_check_constraint(
        "ck_workspace_item_type",
        "workspace_items",
        "item_type IN ('calendar_event', 'email_message', 'drive_file', 'internal_state')",
    )
    op.drop_constraint("ck_workspace_item_provider", "workspace_items", type_="check")
    op.create_check_constraint(
        "ck_workspace_item_provider",
        "workspace_items",
        "provider IN ('google', 'ariel')",
    )

    op.drop_table("proactive_learning_records")
    op.drop_table("proactive_feedback")
    op.drop_table("autonomy_scopes")
    op.drop_table("proactive_action_executions")
    op.drop_table("proactive_action_plans")
    op.drop_table("proactive_turns")
    op.drop_table("proactive_policy_validations")
    op.drop_constraint("fk_proactive_cases_last_decision_id", "proactive_cases", type_="foreignkey")
    op.drop_table("proactive_decisions")
    op.drop_table("proactive_context_snapshots")
    op.drop_table("proactive_case_events")
    op.drop_table("proactive_cases")
    op.drop_table("proactive_observations")
    op.drop_table("ai_judgments")
