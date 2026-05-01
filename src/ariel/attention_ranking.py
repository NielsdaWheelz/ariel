from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.persistence import (
    AttentionGroupMemberRecord,
    AttentionGroupRecord,
    AttentionItemEventRecord,
    AttentionItemRecord,
    AttentionRankFeatureRecord,
    AttentionRankSnapshotRecord,
    AttentionSignalRecord,
    BackgroundTaskRecord,
    NotificationRecord,
    ProactiveFeedbackRecord,
    ProactiveFeedbackRuleRecord,
    to_rfc3339,
)


RANKING_VERSION = "attention-ranking-v1"


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _add_task(
    db: Session,
    *,
    task_type: str,
    payload: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> None:
    db.add(
        BackgroundTaskRecord(
            id=new_id_fn("tsk"),
            task_type=task_type,
            payload=payload,
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            error=None,
            claimed_by=None,
            run_after=run_after or now,
            last_heartbeat=None,
            created_at=now,
            updated_at=now,
        )
    )


def process_attention_feature_extraction_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    signal_id = _payload_text(task_payload, "attention_signal_id")
    with session_factory() as db:
        with db.begin():
            query = select(AttentionSignalRecord).where(AttentionSignalRecord.status == "new")
            if signal_id is not None:
                query = query.where(AttentionSignalRecord.id == signal_id)
            signals = db.scalars(
                query.order_by(
                    AttentionSignalRecord.updated_at.asc(), AttentionSignalRecord.id.asc()
                )
                .limit(100)
                .with_for_update()
            ).all()
            if not signals:
                return

            now = now_fn()
            for signal in signals:
                features = {
                    "source_type": signal.source_type,
                    "source_id": signal.source_id,
                    "priority": signal.priority,
                    "urgency": signal.urgency,
                    "confidence": signal.confidence,
                    "workspace_item_id": signal.workspace_item_id,
                    "signal_updated_at": to_rfc3339(signal.updated_at),
                    "evidence": signal.evidence,
                    "taint": signal.taint,
                }
                score_components = {
                    "source": signal.source_type,
                    "priority": signal.priority,
                    "urgency": signal.urgency,
                    "confidence": signal.confidence,
                }
                feature = db.scalar(
                    select(AttentionRankFeatureRecord)
                    .where(
                        AttentionRankFeatureRecord.attention_signal_id == signal.id,
                        AttentionRankFeatureRecord.feature_set_version == RANKING_VERSION,
                    )
                    .with_for_update()
                    .limit(1)
                )
                if feature is None:
                    db.add(
                        AttentionRankFeatureRecord(
                            id=new_id_fn("arf"),
                            attention_signal_id=signal.id,
                            feature_set_version=RANKING_VERSION,
                            features=features,
                            score_components=score_components,
                            created_at=now,
                        )
                    )
                else:
                    feature.features = features
                    feature.score_components = score_components
                    feature.created_at = now

            _add_task(
                db,
                task_type="attention_grouping_due",
                payload={},
                now=now,
                new_id_fn=new_id_fn,
            )


def process_attention_grouping_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    signal_id = _payload_text(task_payload, "attention_signal_id")
    with session_factory() as db:
        with db.begin():
            query = select(AttentionSignalRecord).where(AttentionSignalRecord.status == "new")
            if signal_id is not None:
                query = query.where(AttentionSignalRecord.id == signal_id)
            signals = db.scalars(
                query.order_by(
                    AttentionSignalRecord.updated_at.asc(), AttentionSignalRecord.id.asc()
                )
                .limit(100)
                .with_for_update()
            ).all()
            if not signals:
                return

            now = now_fn()
            group_ids: list[str] = []
            for signal in signals:
                group_type = "workspace"
                group_key = f"workspace:{signal.workspace_item_id or signal.source_id}"
                if signal.source_type == "approval_request":
                    group_type = "approval"
                    group_key = f"approval:{signal.source_id}"
                elif signal.source_type == "job":
                    group_type = "job"
                    group_key = f"job:{signal.source_id}"
                elif signal.source_type == "google_connector":
                    group_type = "connector"
                    group_key = f"connector:{signal.source_id}"
                elif signal.source_type == "memory_assertion":
                    group_type = "memory"
                    group_key = f"memory:{signal.source_id}"
                elif signal.source_type == "capture":
                    group_type = "capture"
                    group_key = f"capture:{signal.source_id}"

                group = db.scalar(
                    select(AttentionGroupRecord)
                    .where(AttentionGroupRecord.group_key == group_key)
                    .with_for_update()
                    .limit(1)
                )
                if group is None:
                    group = AttentionGroupRecord(
                        id=new_id_fn("agr"),
                        group_key=group_key,
                        group_type=group_type,
                        status="active",
                        title=signal.title,
                        summary=signal.body,
                        group_metadata={"source_type": signal.source_type},
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(group)
                    db.flush()
                else:
                    group.status = "active"
                    group.title = signal.title
                    group.summary = signal.body
                    group.group_metadata = {"source_type": signal.source_type}
                    group.updated_at = now

                member = db.scalar(
                    select(AttentionGroupMemberRecord)
                    .where(
                        AttentionGroupMemberRecord.group_id == group.id,
                        AttentionGroupMemberRecord.attention_signal_id == signal.id,
                    )
                    .with_for_update()
                    .limit(1)
                )
                if member is None:
                    db.add(
                        AttentionGroupMemberRecord(
                            id=new_id_fn("agm"),
                            group_id=group.id,
                            attention_signal_id=signal.id,
                            grouping_reason=f"grouped by {group_key}",
                            ranking_version=RANKING_VERSION,
                            created_at=now,
                        )
                    )
                if group.id not in group_ids:
                    group_ids.append(group.id)

            for group_id in group_ids:
                _add_task(
                    db,
                    task_type="attention_ranking_due",
                    payload={"attention_group_id": group_id},
                    now=now,
                    new_id_fn=new_id_fn,
                )


def process_attention_ranking_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    group_id = _payload_text(task_payload, "attention_group_id")
    if group_id is None:
        raise RuntimeError("attention_ranking_due task missing attention_group_id")

    with session_factory() as db:
        with db.begin():
            group = db.scalar(
                select(AttentionGroupRecord)
                .where(
                    AttentionGroupRecord.status == "active",
                    AttentionGroupRecord.id == group_id,
                )
                .order_by(AttentionGroupRecord.updated_at.asc(), AttentionGroupRecord.id.asc())
                .with_for_update()
                .limit(1)
            )
            if group is None:
                return

            now = now_fn()
            rules = db.scalars(
                select(ProactiveFeedbackRuleRecord)
                .where(ProactiveFeedbackRuleRecord.status == "active")
                .order_by(
                    ProactiveFeedbackRuleRecord.priority.asc(), ProactiveFeedbackRuleRecord.id.asc()
                )
            ).all()

            members = db.scalars(
                select(AttentionGroupMemberRecord)
                .where(AttentionGroupMemberRecord.group_id == group.id)
                .order_by(
                    AttentionGroupMemberRecord.created_at.asc(),
                    AttentionGroupMemberRecord.id.asc(),
                )
            ).all()
            signal_ids = [member.attention_signal_id for member in members]
            if not signal_ids:
                return
            signals = db.scalars(
                select(AttentionSignalRecord)
                .where(
                    AttentionSignalRecord.id.in_(signal_ids),
                    AttentionSignalRecord.status.in_(("new", "reviewed")),
                )
                .order_by(AttentionSignalRecord.updated_at.desc(), AttentionSignalRecord.id.asc())
            ).all()
            if not signals:
                return

            active_signal_ids = [signal.id for signal in signals]
            features = db.scalars(
                select(AttentionRankFeatureRecord).where(
                    AttentionRankFeatureRecord.attention_signal_id.in_(active_signal_ids),
                    AttentionRankFeatureRecord.feature_set_version == RANKING_VERSION,
                )
            ).all()
            if {feature.attention_signal_id for feature in features} != set(active_signal_ids):
                raise RuntimeError("attention_ranking_due missing rank features")

            primary = signals[0]
            source_types = sorted({signal.source_type for signal in signals})
            source_ids = sorted({signal.source_id for signal in signals})
            score = 0.4
            priority = "normal"
            urgency = "normal"
            delivery_decision = "digest"
            delivery_reason = "digest by default"
            rank_reason = "workspace_or_internal_signal"
            suppression_reason: str | None = None
            next_follow_up_after: datetime | None = None
            expires_at_for_item: datetime | None = None

            if "approval_request" in source_types:
                score = 0.95
                priority = "critical"
                urgency = "critical"
                delivery_decision = "interrupt_now"
                delivery_reason = "approval requires user decision"
                rank_reason = "pending_approval"
                expires_raw = primary.evidence.get("expires_at")
                if isinstance(expires_raw, str):
                    try:
                        expires_at = datetime.fromisoformat(
                            expires_raw.replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except ValueError:
                        expires_at = None
                    if expires_at is not None and expires_at > now:
                        expires_at_for_item = expires_at
                        if expires_at - now > timedelta(minutes=5):
                            next_follow_up_after = expires_at - timedelta(minutes=5)
                        else:
                            next_follow_up_after = expires_at
            elif "job" in source_types:
                status = str(primary.evidence.get("status") or "")
                if status == "waiting_approval":
                    score = 0.85
                    priority = "high"
                    urgency = "high"
                    delivery_decision = "interrupt_now"
                    delivery_reason = "job is waiting on user approval"
                    rank_reason = "job_waiting_on_user"
                else:
                    score = 0.5
                    priority = "normal"
                    urgency = "normal"
                    delivery_decision = "queue"
                    delivery_reason = "job is active but not blocked"
                    rank_reason = f"job_{status or 'active'}"
            elif "google_connector" in source_types:
                status = str(primary.evidence.get("status") or "")
                if status in {"error", "revoked"}:
                    score = 0.9
                    priority = "critical"
                    urgency = "high"
                    delivery_decision = "interrupt_now"
                    delivery_reason = "connector health blocks workspace awareness"
                    rank_reason = f"connector_{status}"
                else:
                    score = 0.6
                    priority = "normal"
                    urgency = "normal"
                    delivery_decision = "queue"
                    delivery_reason = "connector needs repair but is not critical"
                    rank_reason = f"connector_{status or 'not_connected'}"
            elif "memory_assertion" in source_types:
                score = min(0.75, 0.35 + primary.confidence * 0.4)
                priority = "normal"
                urgency = "normal"
                delivery_decision = "queue"
                delivery_reason = "commitment is active memory"
                rank_reason = "active_commitment"
            elif "capture" in source_types:
                text = f"{primary.title} {primary.body}".lower()
                task_like = any(
                    marker in text
                    for marker in ("remind", "follow up", "todo", "to do", "due", "deadline")
                )
                score = 0.65 if task_like else 0.25
                priority = "normal" if task_like else "low"
                urgency = "normal" if task_like else "low"
                delivery_decision = "queue" if task_like else "digest"
                delivery_reason = (
                    "capture looks task-like" if task_like else "capture is reviewable"
                )
                rank_reason = "task_like_capture" if task_like else "recent_capture"

            for rule in rules:
                conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
                if conditions.get("source_type") not in (None, primary.source_type):
                    continue
                if conditions.get("source_id") not in (None, primary.source_id):
                    continue
                effect = rule.effect if isinstance(rule.effect, dict) else {}
                if effect.get("delivery_decision") == "suppress" and priority != "critical":
                    delivery_decision = "suppress"
                    suppression_reason = str(
                        effect.get("suppression_reason") or "suppressed by feedback"
                    )
                    score = min(score, 0.2)
                    rank_reason = f"{rank_reason}+feedback_suppressed"
                score_delta = effect.get("score_delta")
                if isinstance(score_delta, (int, float)):
                    score = max(0.0, min(1.0, score + float(score_delta)))
                    rank_reason = f"{rank_reason}+feedback_adjusted"
                    if effect.get("delivery_decision") == "queue" and delivery_decision == "digest":
                        delivery_decision = "queue"
                        delivery_reason = "feedback marked similar items useful"

            snapshot_id = new_id_fn("ars")
            db.add(
                AttentionRankSnapshotRecord(
                    id=snapshot_id,
                    group_id=group.id,
                    snapshot_key=f"group:{group.id}:snapshot:{snapshot_id}",
                    ranker_version=RANKING_VERSION,
                    source_signal_ids=[signal.id for signal in signals],
                    rank_score=round(score, 4),
                    rank_inputs={
                        "source_types": source_types,
                        "source_ids": source_ids,
                        "signal_count": len(signals),
                        "delivery_decision": delivery_decision,
                        "expires_at": (
                            to_rfc3339(expires_at_for_item)
                            if expires_at_for_item is not None
                            else None
                        ),
                    },
                    rank_reason=rank_reason,
                    delivery_decision=delivery_decision,
                    delivery_reason=delivery_reason,
                    suppression_reason=suppression_reason,
                    next_follow_up_after=next_follow_up_after,
                    priority=priority,
                    urgency=urgency,
                    confidence=max(signal.confidence for signal in signals),
                    title=group.title,
                    body=group.summary,
                    evidence={
                        "attention_signal_ids": [signal.id for signal in signals],
                        "signal_evidence": [signal.evidence for signal in signals],
                    },
                    taint={"signals": [signal.taint for signal in signals]},
                    created_at=now,
                )
            )

            _add_task(
                db,
                task_type="attention_review_due",
                payload={"attention_rank_snapshot_ids": [snapshot_id]},
                now=now,
                new_id_fn=new_id_fn,
            )


def process_attention_review_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    raw_snapshot_ids = task_payload.get("attention_rank_snapshot_ids")
    snapshot_ids = (
        [item for item in raw_snapshot_ids if isinstance(item, str)]
        if isinstance(raw_snapshot_ids, list)
        else []
    )
    if not snapshot_ids:
        raise RuntimeError("attention_review_due task missing attention_rank_snapshot_ids")

    with session_factory() as db:
        with db.begin():
            snapshots = db.scalars(
                select(AttentionRankSnapshotRecord)
                .where(AttentionRankSnapshotRecord.id.in_(snapshot_ids))
                .order_by(
                    AttentionRankSnapshotRecord.created_at.desc(),
                    AttentionRankSnapshotRecord.id.desc(),
                )
                .limit(100)
                .with_for_update()
            ).all()
            if not snapshots:
                raise RuntimeError("attention_review_due rank snapshot not found")
            if {snapshot.id for snapshot in snapshots} != set(snapshot_ids):
                raise RuntimeError("attention_review_due rank snapshot not found")

            now = now_fn()
            seen_group_ids: set[str] = set()
            for snapshot in snapshots:
                if snapshot.group_id in seen_group_ids:
                    continue
                seen_group_ids.add(snapshot.group_id)
                expires_at = None
                expires_raw = snapshot.rank_inputs.get("expires_at")
                if isinstance(expires_raw, str):
                    try:
                        expires_at = datetime.fromisoformat(
                            expires_raw.replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except ValueError:
                        expires_at = None
                item = db.scalar(
                    select(AttentionItemRecord)
                    .where(AttentionItemRecord.dedupe_key == f"attention-group:{snapshot.group_id}")
                    .with_for_update()
                    .limit(1)
                )
                if item is not None and item.rank_snapshot_id == snapshot.id:
                    continue
                if item is None:
                    item = AttentionItemRecord(
                        id=new_id_fn("att"),
                        group_id=snapshot.group_id,
                        rank_snapshot_id=snapshot.id,
                        source_type="attention_group",
                        source_id=snapshot.group_id,
                        source_signal_ids=snapshot.source_signal_ids,
                        dedupe_key=f"attention-group:{snapshot.group_id}",
                        status="open",
                        priority=snapshot.priority,
                        urgency=snapshot.urgency,
                        confidence=snapshot.confidence,
                        title=snapshot.title,
                        body=snapshot.body,
                        reason=snapshot.rank_reason,
                        evidence=snapshot.evidence,
                        taint=snapshot.taint,
                        rank_score=snapshot.rank_score,
                        rank_inputs=snapshot.rank_inputs,
                        rank_reason=snapshot.rank_reason,
                        delivery_decision=snapshot.delivery_decision,
                        delivery_reason=snapshot.delivery_reason,
                        suppression_reason=snapshot.suppression_reason,
                        expires_at=expires_at,
                        next_follow_up_after=snapshot.next_follow_up_after,
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
                            payload={"rank_snapshot_id": snapshot.id},
                            created_at=now,
                        )
                    )
                elif item.status in {"open", "notified", "acknowledged", "snoozed"}:
                    item.group_id = snapshot.group_id
                    item.rank_snapshot_id = snapshot.id
                    item.source_signal_ids = snapshot.source_signal_ids
                    item.priority = snapshot.priority
                    item.urgency = snapshot.urgency
                    item.confidence = snapshot.confidence
                    item.title = snapshot.title
                    item.body = snapshot.body
                    item.reason = snapshot.rank_reason
                    item.evidence = snapshot.evidence
                    item.taint = snapshot.taint
                    item.rank_score = snapshot.rank_score
                    item.rank_inputs = snapshot.rank_inputs
                    item.rank_reason = snapshot.rank_reason
                    item.delivery_decision = snapshot.delivery_decision
                    item.delivery_reason = snapshot.delivery_reason
                    item.suppression_reason = snapshot.suppression_reason
                    item.expires_at = expires_at
                    item.next_follow_up_after = snapshot.next_follow_up_after
                    item.updated_at = now
                    db.add(
                        AttentionItemEventRecord(
                            id=new_id_fn("aie"),
                            attention_item_id=item.id,
                            event_type="updated",
                            payload={"rank_snapshot_id": snapshot.id},
                            created_at=now,
                        )
                    )
                else:
                    continue

                for signal_id in snapshot.source_signal_ids:
                    signal = db.get(AttentionSignalRecord, signal_id)
                    if signal is not None and signal.status == "new":
                        signal.status = "reviewed"
                        signal.updated_at = now

                if item.next_follow_up_after is not None:
                    _add_task(
                        db,
                        task_type="attention_item_follow_up_due",
                        payload={
                            "attention_item_id": item.id,
                            "scheduled_for": to_rfc3339(item.next_follow_up_after),
                        },
                        now=now,
                        run_after=item.next_follow_up_after,
                        new_id_fn=new_id_fn,
                    )
                    db.add(
                        AttentionItemEventRecord(
                            id=new_id_fn("aie"),
                            attention_item_id=item.id,
                            event_type="follow_up_queued",
                            payload={"scheduled_for": to_rfc3339(item.next_follow_up_after)},
                            created_at=now,
                        )
                    )

                if item.status == "open" and item.delivery_decision == "interrupt_now":
                    _add_task(
                        db,
                        task_type="attention_delivery_due",
                        payload={"attention_item_id": item.id},
                        now=now,
                        new_id_fn=new_id_fn,
                    )


def process_attention_delivery_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    attention_item_id = _payload_text(task_payload, "attention_item_id")
    if attention_item_id is None:
        raise RuntimeError("attention_delivery_due task missing attention_item_id")

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
            if item.status != "open" or item.delivery_decision != "interrupt_now":
                return

            now = now_fn()
            notification = db.scalar(
                select(NotificationRecord)
                .where(
                    NotificationRecord.dedupe_key
                    == f"attention-item:{item.id}:rank:{item.rank_snapshot_id}"
                )
                .with_for_update()
                .limit(1)
            )
            if notification is None:
                notification = NotificationRecord(
                    id=new_id_fn("ntf"),
                    dedupe_key=f"attention-item:{item.id}:rank:{item.rank_snapshot_id}",
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
                _add_task(
                    db,
                    task_type="deliver_discord_notification",
                    payload={"notification_id": notification.id},
                    now=now,
                    new_id_fn=new_id_fn,
                    max_attempts=5,
                )

            item.status = "notified"
            item.last_notified_at = now
            item.updated_at = now
            db.add(
                AttentionItemEventRecord(
                    id=new_id_fn("aie"),
                    attention_item_id=item.id,
                    event_type="notified",
                    payload={"notification_id": notification.id, "kind": "ranked_delivery"},
                    created_at=now,
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
            if item.status in {"resolved", "cancelled", "expired", "superseded"}:
                return

            now = now_fn()
            if item.next_follow_up_after is not None and item.next_follow_up_after > now:
                return
            if item.expires_at is not None and item.expires_at <= now:
                item.status = "expired"
                item.next_follow_up_after = None
                item.updated_at = now
                db.add(
                    AttentionItemEventRecord(
                        id=new_id_fn("aie"),
                        attention_item_id=item.id,
                        event_type="expired",
                        payload={},
                        created_at=now,
                    )
                )
                return

            item.status = "open"
            item.next_follow_up_after = None
            item.updated_at = now
            db.add(
                AttentionItemEventRecord(
                    id=new_id_fn("aie"),
                    attention_item_id=item.id,
                    event_type="updated",
                    payload={"kind": "follow_up_due"},
                    created_at=now,
                )
            )
            _add_task(
                db,
                task_type="attention_ranking_due",
                payload={"attention_group_id": item.group_id},
                now=now,
                new_id_fn=new_id_fn,
            )


def process_proactive_feedback_review_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    feedback_id = _payload_text(task_payload, "feedback_id")
    if feedback_id is None:
        raise RuntimeError("proactive_feedback_review_due task missing feedback_id")

    with session_factory() as db:
        with db.begin():
            feedback = db.scalar(
                select(ProactiveFeedbackRecord)
                .where(ProactiveFeedbackRecord.id == feedback_id)
                .with_for_update()
                .limit(1)
            )
            if feedback is None:
                raise RuntimeError("proactive feedback not found")
            item = db.scalar(
                select(AttentionItemRecord)
                .where(AttentionItemRecord.id == feedback.attention_item_id)
                .limit(1)
            )
            if item is None or not item.source_signal_ids:
                return
            signal = db.get(AttentionSignalRecord, item.source_signal_ids[0])
            if signal is None:
                return

            now = now_fn()
            effect: dict[str, Any]
            if feedback.feedback_type in {"noise", "wrong"}:
                rule_type = "suppression"
                effect = {
                    "delivery_decision": "suppress",
                    "suppression_reason": f"user_feedback_{feedback.feedback_type}",
                }
                priority = 10
            else:
                rule_type = "ranking"
                effect = {"score_delta": 0.2, "delivery_decision": "queue"}
                priority = 20

            rule_key = f"feedback:{feedback.feedback_type}:{signal.source_type}:{signal.source_id}"
            rule = db.scalar(
                select(ProactiveFeedbackRuleRecord)
                .where(ProactiveFeedbackRuleRecord.rule_key == rule_key)
                .with_for_update()
                .limit(1)
            )
            if rule is None:
                db.add(
                    ProactiveFeedbackRuleRecord(
                        id=new_id_fn("pfr"),
                        rule_key=rule_key,
                        rule_type=rule_type,
                        status="active",
                        priority=priority,
                        conditions={
                            "source_type": signal.source_type,
                            "source_id": signal.source_id,
                        },
                        effect=effect,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                rule.rule_type = rule_type
                rule.status = "active"
                rule.priority = priority
                rule.conditions = {"source_type": signal.source_type, "source_id": signal.source_id}
                rule.effect = effect
                rule.updated_at = now

            _add_task(
                db,
                task_type="attention_ranking_due",
                payload={"attention_group_id": item.group_id},
                now=now,
                new_id_fn=new_id_fn,
            )
