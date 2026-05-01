"""cut proactive attention over to ranked groups

Revision ID: 20260501_0018
Revises: 20260501_0017
Create Date: 2026-05-01 03:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260501_0018"
down_revision = "20260501_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM background_tasks "
        "WHERE task_type = 'deliver_discord_notification' "
        "AND payload->>'notification_id' IN ("
        "SELECT id FROM notifications WHERE source_type = 'attention_item'"
        ")"
    )
    op.execute("DELETE FROM notifications WHERE source_type = 'attention_item'")
    op.drop_table("action_proposals")
    op.drop_table("proactive_feedback")
    op.drop_table("attention_item_events")
    op.drop_table("attention_items")

    op.create_table(
        "attention_groups",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("group_key", sa.String(length=160), nullable=False),
        sa.Column("group_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "group_type IN ('approval', 'job', 'connector', 'memory', 'capture', 'workspace')",
            name="ck_attention_group_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suppressed', 'resolved')",
            name="ck_attention_group_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_key"),
    )
    op.create_index("ix_attention_groups_created_at", "attention_groups", ["created_at"])
    op.create_index(
        "ix_attention_groups_status_updated",
        "attention_groups",
        ["status", "updated_at"],
    )
    op.create_index("ix_attention_groups_updated_at", "attention_groups", ["updated_at"])

    op.create_table(
        "attention_group_members",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("group_id", sa.String(length=32), nullable=False),
        sa.Column("attention_signal_id", sa.String(length=32), nullable=False),
        sa.Column("grouping_reason", sa.Text(), nullable=False),
        sa.Column("ranking_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["attention_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attention_signal_id"], ["attention_signals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "attention_signal_id",
            name="uq_attention_group_member_signal",
        ),
    )
    op.create_index(
        "ix_attention_group_members_attention_signal_id",
        "attention_group_members",
        ["attention_signal_id"],
    )
    op.create_index(
        "ix_attention_group_members_created_at", "attention_group_members", ["created_at"]
    )
    op.create_index("ix_attention_group_members_group_id", "attention_group_members", ["group_id"])

    op.create_table(
        "attention_rank_features",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("attention_signal_id", sa.String(length=32), nullable=False),
        sa.Column("feature_set_version", sa.String(length=64), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score_components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attention_signal_id"], ["attention_signals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attention_signal_id",
            "feature_set_version",
            name="uq_attention_rank_feature_signal_version",
        ),
    )
    op.create_index(
        "ix_attention_rank_features_attention_signal_id",
        "attention_rank_features",
        ["attention_signal_id"],
    )
    op.create_index(
        "ix_attention_rank_features_created_at", "attention_rank_features", ["created_at"]
    )

    op.create_table(
        "attention_rank_snapshots",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("group_id", sa.String(length=32), nullable=False),
        sa.Column("snapshot_key", sa.String(length=160), nullable=False),
        sa.Column("ranker_version", sa.String(length=64), nullable=False),
        sa.Column("source_signal_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rank_score", sa.Float(), nullable=False),
        sa.Column("rank_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rank_reason", sa.Text(), nullable=False),
        sa.Column("delivery_decision", sa.String(length=32), nullable=False),
        sa.Column("delivery_reason", sa.Text(), nullable=False),
        sa.Column("suppression_reason", sa.Text(), nullable=True),
        sa.Column("next_follow_up_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("urgency", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rank_score >= 0.0 AND rank_score <= 1.0",
            name="ck_attention_rank_snapshot_score",
        ),
        sa.CheckConstraint(
            "delivery_decision IN ('interrupt_now', 'queue', 'digest', 'suppress')",
            name="ck_attention_rank_snapshot_delivery_decision",
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_rank_snapshot_priority",
        ),
        sa.CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_rank_snapshot_urgency",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_attention_rank_snapshot_confidence",
        ),
        sa.ForeignKeyConstraint(["group_id"], ["attention_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_key"),
    )
    op.create_index(
        "ix_attention_rank_snapshots_created_at", "attention_rank_snapshots", ["created_at"]
    )
    op.create_index(
        "ix_attention_rank_snapshots_delivery",
        "attention_rank_snapshots",
        ["delivery_decision", "created_at"],
    )
    op.create_index(
        "ix_attention_rank_snapshots_group_created",
        "attention_rank_snapshots",
        ["group_id", "created_at"],
    )
    op.create_index(
        "ix_attention_rank_snapshots_next_follow_up_after",
        "attention_rank_snapshots",
        ["next_follow_up_after"],
    )

    op.create_table(
        "attention_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("group_id", sa.String(length=32), nullable=False),
        sa.Column("rank_snapshot_id", sa.String(length=32), nullable=False),
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
        sa.Column("rank_score", sa.Float(), nullable=False),
        sa.Column("rank_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rank_reason", sa.Text(), nullable=False),
        sa.Column("delivery_decision", sa.String(length=32), nullable=False),
        sa.Column("delivery_reason", sa.Text(), nullable=False),
        sa.Column("suppression_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_follow_up_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('attention_group')",
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
        sa.CheckConstraint(
            "rank_score >= 0.0 AND rank_score <= 1.0",
            name="ck_attention_item_rank_score",
        ),
        sa.CheckConstraint(
            "delivery_decision IN ('interrupt_now', 'queue', 'digest', 'suppress')",
            name="ck_attention_item_delivery_decision",
        ),
        sa.ForeignKeyConstraint(["group_id"], ["attention_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["rank_snapshot_id"], ["attention_rank_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_attention_items_created_at", "attention_items", ["created_at"])
    op.create_index("ix_attention_items_expires_at", "attention_items", ["expires_at"])
    op.create_index(
        "ix_attention_items_delivery",
        "attention_items",
        ["delivery_decision", "status", "updated_at"],
    )
    op.create_index(
        "ix_attention_items_follow_up_due",
        "attention_items",
        ["status", "next_follow_up_after", "id"],
    )
    op.create_index("ix_attention_items_group_id", "attention_items", ["group_id"])
    op.create_index(
        "ix_attention_items_next_follow_up_after", "attention_items", ["next_follow_up_after"]
    )
    op.create_index("ix_attention_items_rank_snapshot_id", "attention_items", ["rank_snapshot_id"])
    op.create_index("ix_attention_items_source", "attention_items", ["source_type", "source_id"])
    op.create_index("ix_attention_items_source_id", "attention_items", ["source_id"])
    op.create_index(
        "ix_attention_items_status_priority",
        "attention_items",
        ["status", "priority", "updated_at"],
    )
    op.create_index(
        "ix_attention_items_status_rank",
        "attention_items",
        ["status", "rank_score", "updated_at"],
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
        "proactive_feedback_rules",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("rule_key", sa.String(length=160), nullable=False),
        sa.Column("rule_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effect", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rule_type IN ('ranking', 'grouping', 'delivery', 'suppression')",
            name="ck_proactive_feedback_rule_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_proactive_feedback_rule_status",
        ),
        sa.CheckConstraint("priority >= 0", name="ck_proactive_feedback_rule_priority"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_key"),
    )
    op.create_index(
        "ix_proactive_feedback_rules_created_at",
        "proactive_feedback_rules",
        ["created_at"],
    )
    op.create_index(
        "ix_proactive_feedback_rules_status_priority",
        "proactive_feedback_rules",
        ["status", "priority", "updated_at"],
    )
    op.create_index(
        "ix_proactive_feedback_rules_updated_at",
        "proactive_feedback_rules",
        ["updated_at"],
    )

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

    op.execute(
        "DELETE FROM background_tasks WHERE task_type IN "
        "('attention_review_due', 'attention_item_follow_up_due', 'attention_model_review_due')"
    )
    op.drop_constraint("ck_background_task_type", "background_tasks", type_="check")
    op.create_check_constraint(
        "ck_background_task_type",
        "background_tasks",
        (
            "task_type IN ('agency_event_received', 'deliver_discord_notification', "
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn', 'workspace_signal_derivation_due', "
            "'attention_feature_extraction_due', 'attention_grouping_due', "
            "'attention_ranking_due', 'attention_review_due', 'attention_delivery_due', "
            "'attention_item_follow_up_due', 'proactive_feedback_review_due', "
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
            "'expire_approvals', 'reap_stale_tasks', "
            "'provider_subscription_renewal_due', 'provider_event_received', "
            "'provider_sync_due', 'memory_extract_turn', 'workspace_signal_derivation_due', "
            "'attention_review_due', 'attention_item_follow_up_due', "
            "'action_proposal_review_due')"
        ),
    )

    op.drop_table("action_proposals")
    op.drop_table("proactive_feedback_rules")
    op.drop_table("proactive_feedback")
    op.drop_table("attention_item_events")
    op.drop_table("attention_items")
    op.drop_table("attention_rank_snapshots")
    op.drop_table("attention_rank_features")
    op.drop_table("attention_group_members")
    op.drop_table("attention_groups")

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
