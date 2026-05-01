from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.persistence import (
    ApprovalRequestRecord,
    AttentionItemEventRecord,
    AttentionItemRecord,
    AttentionSignalRecord,
    BackgroundTaskRecord,
    CaptureRecord,
    GoogleConnectorRecord,
    JobRecord,
    MemoryAssertionRecord,
    NotificationRecord,
    to_rfc3339,
)


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def process_workspace_signal_derivation_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    del task_payload
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            changed = 0

            jobs = db.scalars(
                select(JobRecord)
                .where(JobRecord.status.in_(("queued", "running", "waiting_approval")))
                .order_by(JobRecord.updated_at.desc(), JobRecord.id.asc())
                .limit(24)
            ).all()
            for job in jobs:
                priority = "high" if job.status == "waiting_approval" else "normal"
                title = job.title or job.external_job_id
                changed += upsert_attention_signal(
                    db,
                    dedupe_key=f"job:{job.id}",
                    source_type="job",
                    source_id=job.id,
                    workspace_item_id=None,
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
                    taint={"provenance_status": "trusted_internal"},
                    now=now,
                    new_id_fn=new_id_fn,
                )

            approvals = db.scalars(
                select(ApprovalRequestRecord)
                .where(
                    ApprovalRequestRecord.status == "pending",
                    ApprovalRequestRecord.expires_at > now,
                )
                .order_by(ApprovalRequestRecord.expires_at.asc(), ApprovalRequestRecord.id.asc())
                .limit(24)
            ).all()
            for approval in approvals:
                changed += upsert_attention_signal(
                    db,
                    dedupe_key=f"approval:{approval.id}",
                    source_type="approval_request",
                    source_id=approval.id,
                    workspace_item_id=None,
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
                    taint={"provenance_status": "trusted_internal"},
                    now=now,
                    new_id_fn=new_id_fn,
                )

            commitments = db.scalars(
                select(MemoryAssertionRecord)
                .where(
                    MemoryAssertionRecord.assertion_type == "commitment",
                    MemoryAssertionRecord.lifecycle_state == "active",
                )
                .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
                .limit(24)
            ).all()
            for assertion in commitments:
                value = assertion.object_value if isinstance(assertion.object_value, dict) else {}
                text = (
                    value.get("text") if isinstance(value.get("text"), str) else assertion.predicate
                )
                changed += upsert_attention_signal(
                    db,
                    dedupe_key=f"memory-commitment:{assertion.id}",
                    source_type="memory_assertion",
                    source_id=assertion.id,
                    workspace_item_id=None,
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
                    taint={"provenance_status": "reviewed_memory"},
                    now=now,
                    new_id_fn=new_id_fn,
                )

            connectors = db.scalars(
                select(GoogleConnectorRecord)
                .where(GoogleConnectorRecord.status != "connected")
                .order_by(GoogleConnectorRecord.updated_at.desc(), GoogleConnectorRecord.id.asc())
                .limit(24)
            ).all()
            for connector in connectors:
                priority = "high" if connector.status == "error" else "normal"
                changed += upsert_attention_signal(
                    db,
                    dedupe_key=f"google-connector:{connector.id}",
                    source_type="google_connector",
                    source_id=connector.id,
                    workspace_item_id=None,
                    priority=priority,
                    urgency=priority,
                    confidence=1.0,
                    title="Google connector needs attention",
                    body=f"Google connector is {connector.status}.",
                    reason="Connector is not connected.",
                    evidence={
                        "connector_id": connector.id,
                        "status": connector.status,
                        "last_error_code": connector.last_error_code,
                    },
                    taint={"provenance_status": "trusted_internal"},
                    now=now,
                    new_id_fn=new_id_fn,
                )

            captures = db.scalars(
                select(CaptureRecord)
                .where(CaptureRecord.terminal_state == "turn_created")
                .order_by(CaptureRecord.created_at.desc(), CaptureRecord.id.asc())
                .limit(24)
            ).all()
            for capture in captures:
                changed += upsert_attention_signal(
                    db,
                    dedupe_key=f"capture:{capture.id}",
                    source_type="capture",
                    source_id=capture.id,
                    workspace_item_id=None,
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
                    taint={"provenance_status": "trusted_internal"},
                    now=now,
                    new_id_fn=new_id_fn,
                )

            if changed:
                db.add(
                    BackgroundTaskRecord(
                        id=new_id_fn("tsk"),
                        task_type="attention_review_due",
                        payload={},
                        status="pending",
                        attempts=0,
                        max_attempts=3,
                        error=None,
                        claimed_by=None,
                        run_after=now,
                        last_heartbeat=None,
                        created_at=now,
                        updated_at=now,
                    )
                )


def process_attention_review_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    del task_payload
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            signals = db.scalars(
                select(AttentionSignalRecord)
                .where(AttentionSignalRecord.status == "new")
                .order_by(
                    AttentionSignalRecord.updated_at.asc(),
                    AttentionSignalRecord.id.asc(),
                )
                .limit(50)
                .with_for_update()
            ).all()
            for signal in signals:
                _upsert_attention_item(db, signal=signal, now=now, new_id_fn=new_id_fn)
                signal.status = "reviewed"
                signal.updated_at = now


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


def upsert_attention_signal(
    db: Session,
    *,
    dedupe_key: str,
    source_type: str,
    source_id: str,
    workspace_item_id: str | None,
    priority: str,
    urgency: str,
    confidence: float,
    title: str,
    body: str,
    reason: str,
    evidence: dict[str, Any],
    taint: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> int:
    signal = db.scalar(
        select(AttentionSignalRecord)
        .where(AttentionSignalRecord.dedupe_key == dedupe_key)
        .with_for_update()
        .limit(1)
    )
    if signal is None:
        db.add(
            AttentionSignalRecord(
                id=new_id_fn("sig"),
                workspace_item_id=workspace_item_id,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
                status="new",
                priority=priority,
                urgency=urgency,
                confidence=confidence,
                title=title,
                body=body,
                reason=reason,
                evidence=evidence,
                taint=taint,
                created_at=now,
                updated_at=now,
            )
        )
        return 1

    if signal.status in {"new", "reviewed"}:
        signal.workspace_item_id = workspace_item_id
        signal.source_type = source_type
        signal.source_id = source_id
        signal.status = "new"
        signal.priority = priority
        signal.urgency = urgency
        signal.confidence = confidence
        signal.title = title
        signal.body = body
        signal.reason = reason
        signal.evidence = evidence
        signal.taint = taint
        signal.updated_at = now
        return 1
    return 0


def _upsert_attention_item(
    db: Session,
    *,
    signal: AttentionSignalRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    item = db.scalar(
        select(AttentionItemRecord)
        .where(AttentionItemRecord.dedupe_key == f"attention-signal:{signal.id}")
        .with_for_update()
        .limit(1)
    )
    evidence = {
        "attention_signal_ids": [signal.id],
        "signal_evidence": signal.evidence,
        "taint": signal.taint,
    }
    if item is None:
        item = AttentionItemRecord(
            id=new_id_fn("att"),
            source_type="attention_signal",
            source_id=signal.id,
            source_signal_ids=[signal.id],
            dedupe_key=f"attention-signal:{signal.id}",
            status="open",
            priority=signal.priority,
            urgency=signal.urgency,
            confidence=signal.confidence,
            title=signal.title,
            body=signal.body,
            reason=signal.reason,
            evidence=evidence,
            taint=signal.taint,
            expires_at=None,
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
                payload={"attention_signal_ids": [signal.id]},
                created_at=now,
            )
        )
    elif item.status in {"open", "notified", "acknowledged", "snoozed"}:
        item.source_signal_ids = [signal.id]
        item.priority = signal.priority
        item.urgency = signal.urgency
        item.confidence = signal.confidence
        item.title = signal.title
        item.body = signal.body
        item.reason = signal.reason
        item.evidence = evidence
        item.taint = signal.taint
        item.updated_at = now
        db.add(
            AttentionItemEventRecord(
                id=new_id_fn("aie"),
                attention_item_id=item.id,
                event_type="updated",
                payload={"attention_signal_ids": [signal.id]},
                created_at=now,
            )
        )

    if item.status == "open" and item.priority in {"critical", "high", "normal"}:
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
