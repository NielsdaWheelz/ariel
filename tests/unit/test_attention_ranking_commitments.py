from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ariel.attention_ranking import build_work_follow_up_feature_packet


NOW = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)


def test_feature_packet_contains_due_facts_without_delivery_judgment() -> None:
    packet = build_work_follow_up_feature_packet(
        owner="user",
        lifecycle_state="active",
        loop_kind="due_date",
        due_at=NOW - timedelta(minutes=1),
        now=NOW,
        confidence=0.9,
        snoozed_until=None,
        source_evidence_state="available",
    )

    assert packet["rail_status"] == "eligible"
    assert packet["overdue_seconds"] == 60
    assert packet["time_until_due_seconds"] == -60
    assert packet["due_at"] == "2026-05-12T11:59:00Z"
    assert "rank_score" not in packet
    assert "priority" not in packet
    assert "urgency" not in packet
    assert "delivery_decision" not in packet
    assert "text" not in packet
    assert "snippet" not in packet
    assert "body" not in packet


def test_waiting_direction_is_a_fact_not_a_delivery_decision() -> None:
    packet = build_work_follow_up_feature_packet(
        owner="counterparty",
        lifecycle_state="waiting_on_counterparty",
        loop_kind="waiting_for_reply",
        due_at=NOW - timedelta(minutes=1),
        now=NOW,
        confidence=0.9,
        snoozed_until=None,
        source_evidence_state="available",
    )

    assert packet["rail_status"] == "eligible"
    assert packet["waiting_direction"] == "counterparty"
    assert "delivery_decision" not in packet


def test_snoozed_commitment_is_a_hard_rail_without_changing_truth() -> None:
    snoozed_until = NOW + timedelta(hours=2)

    packet = build_work_follow_up_feature_packet(
        owner="user",
        lifecycle_state="active",
        loop_kind="due_date",
        due_at=NOW - timedelta(minutes=1),
        now=NOW,
        confidence=0.9,
        snoozed_until=snoozed_until,
        source_evidence_state="available",
    )

    assert packet["rail_status"] == "suppressed"
    assert packet["rail_reason"] == "snoozed"
    assert packet["snoozed_until"] == "2026-05-12T14:00:00Z"


def test_invalid_source_evidence_and_active_notification_are_hard_rails() -> None:
    missing_evidence = build_work_follow_up_feature_packet(
        owner="user",
        lifecycle_state="active",
        loop_kind="due_date",
        due_at=NOW - timedelta(minutes=1),
        now=NOW,
        confidence=0.9,
        snoozed_until=None,
        source_evidence_state="deleted",
    )
    pending_notification = build_work_follow_up_feature_packet(
        owner="user",
        lifecycle_state="active",
        loop_kind="due_date",
        due_at=NOW - timedelta(minutes=1),
        now=NOW,
        confidence=0.9,
        snoozed_until=None,
        source_evidence_state="available",
        pending_notification=True,
    )

    assert missing_evidence["rail_status"] == "suppressed"
    assert missing_evidence["rail_reason"] == "source_evidence_invalid"
    assert pending_notification["rail_status"] == "suppressed"
    assert pending_notification["rail_reason"] == "notification_pending_ack"


def test_feedback_connector_and_sensitivity_remain_features() -> None:
    packet = build_work_follow_up_feature_packet(
        owner="user",
        lifecycle_state="active",
        loop_kind="due_date",
        due_at=NOW - timedelta(minutes=1),
        now=NOW,
        confidence=0.9,
        snoozed_until=None,
        last_feedback="too_noisy",
        connector_status="connected",
        sensitivity="restricted",
        source_evidence_state="available",
    )

    assert packet["rail_status"] == "eligible"
    assert packet["last_feedback"] == "too_noisy"
    assert packet["connector_status"] == "connected"
    assert packet["sensitivity"] == "restricted"
