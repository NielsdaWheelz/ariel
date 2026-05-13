from __future__ import annotations

from datetime import datetime
from typing import Any


def build_work_follow_up_feature_packet(
    *,
    owner: str,
    lifecycle_state: str,
    loop_kind: str,
    due_at: datetime | None,
    now: datetime,
    confidence: float,
    snoozed_until: datetime | None,
    last_feedback: str | None = None,
    thread_state: str | None = None,
    calendar_context: dict[str, Any] | None = None,
    connector_status: str | None = None,
    sensitivity: str | None = None,
    source_evidence_state: str | None = None,
    pending_notification: bool = False,
    prior_notification_count: int = 0,
) -> dict[str, Any]:
    due_at_rfc3339 = due_at.isoformat().replace("+00:00", "Z") if due_at is not None else None
    overdue_seconds = None
    time_until_due_seconds = None
    if due_at is not None:
        seconds_until_due = int((due_at - now).total_seconds())
        time_until_due_seconds = seconds_until_due
        if seconds_until_due < 0:
            overdue_seconds = abs(seconds_until_due)

    rail_status = "eligible"
    rail_reason = None
    if lifecycle_state in {
        "resolved",
        "superseded",
        "rejected",
        "stale",
        "expired",
        "deleted",
    }:
        rail_status = "suppressed"
        rail_reason = lifecycle_state
    elif source_evidence_state in {
        None,
        "missing",
        "redacted",
        "deleted",
        "superseded",
        "stale",
        "unavailable",
    }:
        rail_status = "suppressed"
        rail_reason = "source_evidence_invalid"
    elif connector_status is not None and connector_status != "connected":
        rail_status = "suppressed"
        rail_reason = "connector_unavailable"
    elif snoozed_until is not None and snoozed_until > now:
        rail_status = "suppressed"
        rail_reason = "snoozed"
    if thread_state in {"resolved", "stale"}:
        rail_status = "suppressed"
        rail_reason = f"thread_{thread_state}"
    elif pending_notification:
        rail_status = "suppressed"
        rail_reason = "notification_pending_ack"

    waiting_direction = None
    if loop_kind == "waiting_for_reply":
        waiting_direction = "counterparty"
    elif loop_kind == "needs_user_reply":
        waiting_direction = "user"

    return {
        "owner": owner,
        "lifecycle_state": lifecycle_state,
        "loop_kind": loop_kind,
        "due_at": due_at_rfc3339,
        "overdue_seconds": overdue_seconds,
        "time_until_due_seconds": time_until_due_seconds,
        "waiting_direction": waiting_direction,
        "thread_state": thread_state,
        "source_evidence_state": source_evidence_state,
        "confidence": confidence,
        "snoozed_until": snoozed_until.isoformat().replace("+00:00", "Z")
        if snoozed_until is not None
        else None,
        "last_feedback": last_feedback,
        "connector_status": connector_status,
        "sensitivity": sensitivity,
        "calendar_context": calendar_context or {},
        "pending_notification": pending_notification,
        "prior_notification_count": prior_notification_count,
        "rail_status": rail_status,
        "rail_reason": rail_reason,
    }
