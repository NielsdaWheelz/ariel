from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.persistence import (
    ApprovalRequestRecord,
    AttentionItemEventRecord,
    AttentionItemRecord,
    BackgroundTaskRecord,
    CaptureRecord,
    GoogleConnectorRecord,
    JobRecord,
    MemoryAssertionRecord,
    NotificationRecord,
    ProactiveCheckRunRecord,
    ProactiveSubscriptionRecord,
    to_rfc3339,
)


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _payload_timestamp(payload: dict[str, Any], key: str) -> datetime | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def process_proactive_check_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    subscription_id = _payload_text(task_payload, "subscription_id")
    if subscription_id is None:
        raise RuntimeError("proactive_check_due task missing subscription_id")

    with session_factory() as db:
        with db.begin():
            subscription = db.scalar(
                select(ProactiveSubscriptionRecord)
                .where(ProactiveSubscriptionRecord.id == subscription_id)
                .with_for_update()
                .limit(1)
            )
            if subscription is None:
                raise RuntimeError("proactive subscription not found")
            if subscription.status != "active":
                return

            scheduled_for = _payload_timestamp(task_payload, "scheduled_for")
            if scheduled_for is None:
                scheduled_for = subscription.next_run_after

            check_run = db.scalar(
                select(ProactiveCheckRunRecord)
                .where(
                    ProactiveCheckRunRecord.subscription_id == subscription.id,
                    ProactiveCheckRunRecord.scheduled_for == scheduled_for,
                )
                .with_for_update()
                .limit(1)
            )
            if check_run is not None and check_run.status == "succeeded":
                return

            now = now_fn()
            if check_run is None:
                check_run = ProactiveCheckRunRecord(
                    id=new_id_fn("pcr"),
                    subscription_id=subscription.id,
                    scheduled_for=scheduled_for,
                    status="running",
                    started_at=now,
                    completed_at=None,
                    created_attention_count=0,
                    error=None,
                    result_payload={},
                    created_at=now,
                )
                db.add(check_run)
                db.flush()
            else:
                check_run.status = "running"
                check_run.started_at = now
                check_run.completed_at = None
                check_run.error = None

            created_count = 0
            matched_count = 0

            if subscription.source_type == "open_jobs":
                jobs = db.scalars(
                    select(JobRecord)
                    .where(JobRecord.status.in_(("queued", "running", "waiting_approval")))
                    .order_by(JobRecord.updated_at.desc(), JobRecord.id.asc())
                    .limit(12)
                ).all()
                for job in jobs:
                    priority = "high" if job.status == "waiting_approval" else "normal"
                    title = job.title or job.external_job_id
                    was_created = _upsert_attention_item(
                        db,
                        subscription=subscription,
                        dedupe_key=f"open-job:{job.id}",
                        source_type="job",
                        source_id=job.id,
                        priority=priority,
                        urgency=priority,
                        confidence=1.0,
                        title=f"Job needs attention: {title}",
                        body=job.summary or f"{job.external_job_id} is {job.status}.",
                        reason=f"Job status is {job.status}.",
                        evidence={
                            "job_id": job.id,
                            "source": job.source,
                            "external_job_id": job.external_job_id,
                            "status": job.status,
                        },
                        expires_at=None,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    matched_count += 1
                    created_count += 1 if was_created else 0

            elif subscription.source_type == "pending_approvals":
                approvals = db.scalars(
                    select(ApprovalRequestRecord)
                    .where(
                        ApprovalRequestRecord.status == "pending",
                        ApprovalRequestRecord.expires_at > now,
                    )
                    .order_by(
                        ApprovalRequestRecord.expires_at.asc(), ApprovalRequestRecord.id.asc()
                    )
                    .limit(12)
                ).all()
                for approval in approvals:
                    was_created = _upsert_attention_item(
                        db,
                        subscription=subscription,
                        dedupe_key=f"pending-approval:{approval.id}",
                        source_type="approval_request",
                        source_id=approval.id,
                        priority="high",
                        urgency="high",
                        confidence=1.0,
                        title="Approval is waiting",
                        body=f"Approval {approval.id} is pending.",
                        reason="Approval request is pending and not expired.",
                        evidence={
                            "approval_request_id": approval.id,
                            "action_attempt_id": approval.action_attempt_id,
                            "expires_at": to_rfc3339(approval.expires_at),
                        },
                        expires_at=approval.expires_at,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    matched_count += 1
                    created_count += 1 if was_created else 0

            elif subscription.source_type == "memory_commitments":
                commitments = db.scalars(
                    select(MemoryAssertionRecord)
                    .where(
                        MemoryAssertionRecord.assertion_type == "commitment",
                        MemoryAssertionRecord.lifecycle_state == "active",
                    )
                    .order_by(
                        MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc()
                    )
                    .limit(12)
                ).all()
                for assertion in commitments:
                    value = (
                        assertion.object_value if isinstance(assertion.object_value, dict) else {}
                    )
                    text = (
                        value.get("text")
                        if isinstance(value.get("text"), str)
                        else assertion.predicate
                    )
                    was_created = _upsert_attention_item(
                        db,
                        subscription=subscription,
                        dedupe_key=f"memory-commitment:{assertion.id}",
                        source_type="memory_assertion",
                        source_id=assertion.id,
                        priority="normal",
                        urgency="normal",
                        confidence=assertion.confidence,
                        title="Remembered commitment needs review",
                        body=str(text),
                        reason="Active commitment is part of the proactive review set.",
                        evidence={
                            "assertion_id": assertion.id,
                            "subject_key": assertion.subject_key,
                            "predicate": assertion.predicate,
                            "confidence": assertion.confidence,
                        },
                        expires_at=assertion.valid_to,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    matched_count += 1
                    created_count += 1 if was_created else 0

            elif subscription.source_type == "connector_health":
                connectors = db.scalars(
                    select(GoogleConnectorRecord)
                    .where(GoogleConnectorRecord.status != "connected")
                    .order_by(
                        GoogleConnectorRecord.updated_at.desc(), GoogleConnectorRecord.id.asc()
                    )
                    .limit(12)
                ).all()
                for connector in connectors:
                    was_created = _upsert_attention_item(
                        db,
                        subscription=subscription,
                        dedupe_key=f"connector-health:{connector.id}",
                        source_type="google_connector",
                        source_id=connector.id,
                        priority="high" if connector.status == "error" else "normal",
                        urgency="high" if connector.status == "error" else "normal",
                        confidence=1.0,
                        title="Google connector needs attention",
                        body=f"Google connector is {connector.status}.",
                        reason="Connector is not connected.",
                        evidence={
                            "connector_id": connector.id,
                            "status": connector.status,
                            "last_error_code": connector.last_error_code,
                        },
                        expires_at=None,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    matched_count += 1
                    created_count += 1 if was_created else 0

            elif subscription.source_type == "quick_capture_review":
                captures = db.scalars(
                    select(CaptureRecord)
                    .where(CaptureRecord.terminal_state == "turn_created")
                    .order_by(CaptureRecord.created_at.desc(), CaptureRecord.id.asc())
                    .limit(12)
                ).all()
                for capture in captures:
                    was_created = _upsert_attention_item(
                        db,
                        subscription=subscription,
                        dedupe_key=f"quick-capture:{capture.id}",
                        source_type="capture",
                        source_id=capture.id,
                        priority="low",
                        urgency="low",
                        confidence=1.0,
                        title="Captured item is ready for review",
                        body=capture.normalized_turn_input or "Captured item is ready for review.",
                        reason="Recent quick capture was converted into a turn.",
                        evidence={
                            "capture_id": capture.id,
                            "capture_kind": capture.capture_kind,
                            "turn_id": capture.turn_id,
                        },
                        expires_at=None,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    matched_count += 1
                    created_count += 1 if was_created else 0

            elif subscription.source_type in {"calendar_watch", "email_watch", "drive_watch"}:
                watch_title = _payload_text(subscription.check_payload, "title")
                watch_body = _payload_text(subscription.check_payload, "body")
                source_id = (
                    _payload_text(subscription.check_payload, "source_id") or subscription.id
                )
                if watch_title is not None and watch_body is not None:
                    was_created = _upsert_attention_item(
                        db,
                        subscription=subscription,
                        dedupe_key=f"{subscription.source_type}:{source_id}",
                        source_type=subscription.source_type,
                        source_id=source_id,
                        priority="normal",
                        urgency="normal",
                        confidence=1.0,
                        title=watch_title,
                        body=watch_body,
                        reason="Subscription payload supplied an actionable watch signal.",
                        evidence={"subscription_id": subscription.id, "source_id": source_id},
                        expires_at=None,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    matched_count += 1
                    created_count += 1 if was_created else 0
            else:
                raise RuntimeError(f"unsupported proactive source type: {subscription.source_type}")

            check_run.status = "succeeded"
            check_run.completed_at = now
            check_run.created_attention_count = created_count
            check_run.result_payload = {"matched_count": matched_count}
            subscription.last_checked_at = now
            subscription.next_run_after = now + timedelta(
                seconds=subscription.check_interval_seconds
            )
            subscription.updated_at = now
            db.add(
                BackgroundTaskRecord(
                    id=new_id_fn("tsk"),
                    task_type="proactive_check_due",
                    payload={
                        "subscription_id": subscription.id,
                        "scheduled_for": to_rfc3339(subscription.next_run_after),
                    },
                    status="pending",
                    attempts=0,
                    max_attempts=3,
                    error=None,
                    claimed_by=None,
                    run_after=subscription.next_run_after,
                    last_heartbeat=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def process_attention_item_follow_up_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    attention_item_id = _payload_text(task_payload, "attention_item_id")
    if attention_item_id is None:
        raise RuntimeError("attention_item_follow_up_due task missing attention_item_id")

    with session_factory() as db:
        with db.begin():
            item = db.scalar(
                select(AttentionItemRecord)
                .where(AttentionItemRecord.id == attention_item_id)
                .with_for_update()
                .limit(1)
            )
            if item is None:
                raise RuntimeError("attention item not found")
            if item.status not in {"open", "notified", "snoozed"}:
                return

            now = now_fn()
            if item.next_follow_up_after is not None and item.next_follow_up_after > now:
                return

            scheduled_for = _payload_text(task_payload, "scheduled_for") or to_rfc3339(now)
            notification = db.scalar(
                select(NotificationRecord)
                .where(
                    NotificationRecord.dedupe_key
                    == f"attention-item:{item.id}:follow-up:{scheduled_for}"
                )
                .with_for_update()
                .limit(1)
            )
            if notification is None:
                notification = NotificationRecord(
                    id=new_id_fn("ntf"),
                    dedupe_key=f"attention-item:{item.id}:follow-up:{scheduled_for}",
                    source_type="attention_item",
                    source_id=item.id,
                    channel="discord",
                    status="pending",
                    title=item.title,
                    body=item.body,
                    payload={"attention_item_id": item.id},
                    created_at=now,
                    updated_at=now,
                )
                db.add(notification)
                db.flush()
                db.add(
                    BackgroundTaskRecord(
                        id=new_id_fn("tsk"),
                        task_type="deliver_discord_notification",
                        payload={"notification_id": notification.id},
                        status="pending",
                        attempts=0,
                        max_attempts=5,
                        error=None,
                        claimed_by=None,
                        run_after=now,
                        last_heartbeat=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

            item.status = "notified"
            item.last_notified_at = now
            item.next_follow_up_after = None
            item.updated_at = now
            db.add(
                AttentionItemEventRecord(
                    id=new_id_fn("aie"),
                    attention_item_id=item.id,
                    event_type="notified",
                    payload={"notification_id": notification.id, "kind": "follow_up"},
                    created_at=now,
                )
            )


def _upsert_attention_item(
    db: Session,
    *,
    subscription: ProactiveSubscriptionRecord,
    dedupe_key: str,
    source_type: str,
    source_id: str,
    priority: str,
    urgency: str,
    confidence: float,
    title: str,
    body: str,
    reason: str,
    evidence: dict[str, Any],
    expires_at: datetime | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> bool:
    item = db.scalar(
        select(AttentionItemRecord)
        .where(AttentionItemRecord.dedupe_key == dedupe_key)
        .with_for_update()
        .limit(1)
    )
    created = item is None
    if item is None:
        item = AttentionItemRecord(
            id=new_id_fn("att"),
            subscription_id=subscription.id,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
            status="open",
            priority=priority,
            urgency=urgency,
            confidence=confidence,
            title=title,
            body=body,
            reason=reason,
            evidence=evidence,
            expires_at=expires_at,
            next_follow_up_after=None,
            last_notified_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.flush()
        db.add(
            AttentionItemEventRecord(
                id=new_id_fn("aie"),
                attention_item_id=item.id,
                event_type="detected",
                payload={
                    "dedupe_key": dedupe_key,
                    "source_type": source_type,
                    "source_id": source_id,
                },
                created_at=now,
            )
        )
    elif item.status in {"open", "notified", "acknowledged", "snoozed"}:
        item.priority = priority
        item.urgency = urgency
        item.confidence = confidence
        item.title = title
        item.body = body
        item.reason = reason
        item.evidence = evidence
        item.expires_at = expires_at
        item.updated_at = now
        db.add(
            AttentionItemEventRecord(
                id=new_id_fn("aie"),
                attention_item_id=item.id,
                event_type="updated",
                payload={"dedupe_key": dedupe_key},
                created_at=now,
            )
        )

    if item.status == "open" and priority in {"critical", "high", "normal"}:
        notification = db.scalar(
            select(NotificationRecord)
            .where(NotificationRecord.dedupe_key == f"attention-item:{item.id}:initial")
            .with_for_update()
            .limit(1)
        )
        if notification is None:
            notification = NotificationRecord(
                id=new_id_fn("ntf"),
                dedupe_key=f"attention-item:{item.id}:initial",
                source_type="attention_item",
                source_id=item.id,
                channel="discord",
                status="pending",
                title=item.title,
                body=item.body,
                payload={"attention_item_id": item.id},
                created_at=now,
                updated_at=now,
            )
            db.add(notification)
            db.flush()
            db.add(
                BackgroundTaskRecord(
                    id=new_id_fn("tsk"),
                    task_type="deliver_discord_notification",
                    payload={"notification_id": notification.id},
                    status="pending",
                    attempts=0,
                    max_attempts=5,
                    error=None,
                    claimed_by=None,
                    run_after=now,
                    last_heartbeat=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        item.status = "notified"
        item.last_notified_at = now
        item.updated_at = now
        db.add(
            AttentionItemEventRecord(
                id=new_id_fn("aie"),
                attention_item_id=item.id,
                event_type="notified",
                payload={"notification_id": notification.id, "kind": "initial"},
                created_at=now,
            )
        )

    return created
