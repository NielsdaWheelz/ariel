from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import ulid
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .action_runtime import reconcile_expired_approvals_for_session
from .config import AppSettings
from .persistence import (
    AgencyEventRecord,
    BackgroundTaskRecord,
    JobEventRecord,
    JobRecord,
    NotificationDeliveryRecord,
    NotificationRecord,
    SessionRecord,
)
from .redaction import safe_failure_reason


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


def enqueue_background_task(
    db: Session,
    *,
    task_type: str,
    payload: dict[str, Any],
    now: datetime,
    max_attempts: int = 3,
) -> BackgroundTaskRecord:
    task = BackgroundTaskRecord(
        id=_new_id("tsk"),
        task_type=task_type,
        payload=payload,
        status="pending",
        attempts=0,
        max_attempts=max_attempts,
        error=None,
        claimed_by=None,
        run_after=now,
        last_heartbeat=None,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.flush()
    return task


def claim_next_task(db: Session, *, worker_id: str, now: datetime) -> BackgroundTaskRecord | None:
    task = db.scalar(
        select(BackgroundTaskRecord)
        .where(
            BackgroundTaskRecord.status == "pending",
            BackgroundTaskRecord.run_after <= now,
        )
        .order_by(
            BackgroundTaskRecord.run_after.asc(),
            BackgroundTaskRecord.created_at.asc(),
            BackgroundTaskRecord.id.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if task is None:
        return None
    task.status = "running"
    task.attempts += 1
    task.claimed_by = worker_id
    task.last_heartbeat = now
    task.updated_at = now
    db.flush()
    return task


def reap_stale_tasks(db: Session, *, now: datetime, heartbeat_timeout_seconds: int) -> int:
    stale_before = now - timedelta(seconds=heartbeat_timeout_seconds)
    stale_tasks = db.scalars(
        select(BackgroundTaskRecord)
        .where(
            BackgroundTaskRecord.status == "running",
            BackgroundTaskRecord.last_heartbeat < stale_before,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for task in stale_tasks:
        task.status = "pending" if task.attempts < task.max_attempts else "dead_letter"
        task.error = "heartbeat timeout"
        task.claimed_by = None
        task.last_heartbeat = None
        task.run_after = now
        task.updated_at = now
    if stale_tasks:
        db.flush()
    return len(stale_tasks)


def process_one_task(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings | None = None,
    worker_id: str | None = None,
) -> bool:
    resolved_settings = settings or AppSettings()
    resolved_worker_id = worker_id or f"worker-{ulid.new().str.lower()}"

    with session_factory() as db:
        with db.begin():
            reap_stale_tasks(
                db,
                now=_utcnow(),
                heartbeat_timeout_seconds=resolved_settings.worker_heartbeat_timeout_seconds,
            )

    with session_factory() as db:
        with db.begin():
            task = claim_next_task(db, worker_id=resolved_worker_id, now=_utcnow())
            if task is None:
                return False
            task_id = task.id
            task_type = task.task_type
            task_payload = dict(task.payload)

    try:
        match task_type:
            case "agency_event_received":
                _process_agency_event_received(
                    session_factory=session_factory,
                    task_payload=task_payload,
                )
            case "deliver_discord_notification":
                _deliver_discord_notification(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                )
            case "expire_approvals":
                _expire_approvals(session_factory=session_factory, task_payload=task_payload)
            case "reap_stale_tasks":
                with session_factory() as db:
                    with db.begin():
                        reap_stale_tasks(
                            db,
                            now=_utcnow(),
                            heartbeat_timeout_seconds=(
                                resolved_settings.worker_heartbeat_timeout_seconds
                            ),
                        )
            case _:
                raise RuntimeError(f"unsupported task type: {task_type}")
    except Exception as exc:
        _mark_task_failed(
            session_factory=session_factory,
            task_id=task_id,
            error=safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}"),
        )
        return True

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, task_id)
            if task is not None:
                now = _utcnow()
                task.status = "completed"
                task.error = None
                task.claimed_by = None
                task.last_heartbeat = None
                task.updated_at = now
    return True


def run_worker(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings | None = None,
) -> None:
    resolved_settings = settings or AppSettings()
    while True:
        processed = process_one_task(
            session_factory=session_factory,
            settings=resolved_settings,
        )
        if not processed:
            time.sleep(resolved_settings.worker_poll_seconds)


def main() -> None:
    settings = AppSettings()
    engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
    try:
        run_worker(
            session_factory=sessionmaker(bind=engine, future=True, expire_on_commit=False),
            settings=settings,
        )
    finally:
        engine.dispose()


def _mark_task_failed(
    *,
    session_factory: sessionmaker[Session],
    task_id: str,
    error: str,
) -> None:
    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, task_id)
            if task is None:
                return
            now = _utcnow()
            task.status = "dead_letter" if task.attempts >= task.max_attempts else "pending"
            task.error = error
            task.claimed_by = None
            task.last_heartbeat = None
            task.run_after = now + timedelta(seconds=min(300, 2**max(task.attempts - 1, 0)))
            task.updated_at = now


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _job_status_for_event(event_type: str) -> str:
    match event_type:
        case "job.queued":
            return "queued"
        case "job.started" | "job.progress":
            return "running"
        case "job.waiting":
            return "waiting_approval"
        case "job.completed":
            return "succeeded"
        case "job.failed":
            return "failed"
        case "job.cancelled":
            return "cancelled"
        case "job.timed_out":
            return "timed_out"
        case _:
            raise RuntimeError(f"unsupported agency job event type: {event_type}")


def _process_agency_event_received(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
) -> None:
    agency_event_id = _payload_text(task_payload, "agency_event_id")
    if agency_event_id is None:
        raise RuntimeError("agency_event_received task missing agency_event_id")

    with session_factory() as db:
        with db.begin():
            agency_event = db.scalar(
                select(AgencyEventRecord)
                .where(AgencyEventRecord.id == agency_event_id)
                .with_for_update()
                .limit(1)
            )
            if agency_event is None:
                raise RuntimeError("agency event not found")
            if agency_event.processed_at is not None:
                return

            now = _utcnow()
            if agency_event.event_type == "heartbeat":
                agency_event.status = "processed"
                agency_event.processed_at = now
                return

            if agency_event.external_job_id is None:
                agency_event.status = "failed"
                agency_event.error = "job event missing external_job_id"
                agency_event.processed_at = now
                raise RuntimeError("job event missing external_job_id")

            status = _job_status_for_event(agency_event.event_type)
            payload = dict(agency_event.payload)
            job = db.scalar(
                select(JobRecord)
                .where(
                    JobRecord.source == agency_event.source,
                    JobRecord.external_job_id == agency_event.external_job_id,
                )
                .with_for_update()
                .limit(1)
            )
            if job is None:
                job = JobRecord(
                    id=_new_id("job"),
                    source=agency_event.source,
                    external_job_id=agency_event.external_job_id,
                    title=_payload_text(payload, "title"),
                    status=status,
                    summary=_payload_text(payload, "summary"),
                    latest_payload=payload,
                    created_at=now,
                    updated_at=now,
                )
                db.add(job)
                db.flush()
            else:
                job.status = status
                job.title = _payload_text(payload, "title") or job.title
                job.summary = _payload_text(payload, "summary") or job.summary
                job.latest_payload = payload
                job.updated_at = now

            db.add(
                JobEventRecord(
                    id=_new_id("jev"),
                    job_id=job.id,
                    agency_event_id=agency_event.id,
                    event_type=agency_event.event_type,
                    payload=payload,
                    created_at=now,
                )
            )

            if agency_event.event_type in {
                "job.waiting",
                "job.completed",
                "job.failed",
                "job.cancelled",
                "job.timed_out",
            }:
                notification = db.scalar(
                    select(NotificationRecord)
                    .where(NotificationRecord.dedupe_key == f"agency-event:{agency_event.id}")
                    .with_for_update()
                    .limit(1)
                )
                if notification is None:
                    notification = NotificationRecord(
                        id=_new_id("ntf"),
                        dedupe_key=f"agency-event:{agency_event.id}",
                        source_type="agency_event",
                        source_id=agency_event.id,
                        channel="discord",
                        status="pending",
                        title=_notification_title(agency_event.event_type, job),
                        body=_notification_body(agency_event.event_type, job),
                        payload={"job_id": job.id, "agency_event_id": agency_event.id},
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(notification)
                    db.flush()
                    enqueue_background_task(
                        db,
                        task_type="deliver_discord_notification",
                        payload={"notification_id": notification.id},
                        now=now,
                        max_attempts=5,
                    )

            agency_event.status = "processed"
            agency_event.processed_at = now


def _notification_title(event_type: str, job: JobRecord) -> str:
    title = job.title or job.external_job_id
    match event_type:
        case "job.waiting":
            return f"Agency waiting: {title}"
        case "job.completed":
            return f"Agency completed: {title}"
        case "job.failed":
            return f"Agency failed: {title}"
        case "job.cancelled":
            return f"Agency cancelled: {title}"
        case "job.timed_out":
            return f"Agency timed out: {title}"
        case _:
            return f"Agency update: {title}"


def _notification_body(event_type: str, job: JobRecord) -> str:
    if job.summary is not None and job.summary.strip():
        return job.summary.strip()
    return f"{job.external_job_id} is {job.status} after {event_type}."


def _deliver_discord_notification(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
) -> None:
    notification_id = _payload_text(task_payload, "notification_id")
    if notification_id is None:
        raise RuntimeError("deliver_discord_notification task missing notification_id")

    with session_factory() as db:
        with db.begin():
            notification = db.scalar(
                select(NotificationRecord)
                .where(NotificationRecord.id == notification_id)
                .with_for_update()
                .limit(1)
            )
            if notification is None:
                raise RuntimeError("notification not found")
            if notification.status in {"delivered", "acknowledged"}:
                return
            content = f"**{notification.title}**\n{notification.body}"
            payload = notification.payload if isinstance(notification.payload, dict) else {}
            job_id = payload.get("job_id")
            job = (
                db.scalar(
                    select(JobRecord)
                    .where(JobRecord.id == job_id)
                    .with_for_update()
                    .limit(1)
                )
                if isinstance(job_id, str)
                else None
            )
            discord_thread_id = (
                job.discord_thread_id if job is not None and job.discord_thread_id else None
            )

    if settings.discord_bot_token is None or settings.discord_channel_id is None:
        raise RuntimeError("Discord notification delivery is not configured")

    error: str | None = None
    response_payload: dict[str, Any] | None = None
    created_thread_id: str | None = None
    try:
        target_channel_id = discord_thread_id
        thread_payload: dict[str, Any] | None = None
        if target_channel_id is None and job is not None:
            thread_response = httpx.post(
                f"https://discord.com/api/v10/channels/{settings.discord_channel_id}/threads",
                headers={
                    "authorization": f"Bot {settings.discord_bot_token}",
                    "content-type": "application/json",
                },
                json={
                    "name": _discord_thread_name(job),
                    "type": 11,
                    "auto_archive_duration": 1440,
                },
                timeout=settings.discord_notification_timeout_seconds,
            )
            thread_payload = {
                "status_code": thread_response.status_code,
                "body": thread_response.text[:1000],
            }
            if thread_response.status_code < 200 or thread_response.status_code >= 300:
                error = f"Discord returned HTTP {thread_response.status_code} while creating thread"
            else:
                created_thread_payload = thread_response.json()
                created_thread_id = (
                    created_thread_payload.get("id")
                    if isinstance(created_thread_payload, dict)
                    else None
                )
                if isinstance(created_thread_id, str) and created_thread_id:
                    target_channel_id = created_thread_id
                else:
                    error = "Discord returned invalid thread response"

        message_payload: dict[str, Any] | None = None
        if error is None:
            if target_channel_id is None:
                target_channel_id = str(settings.discord_channel_id)
            response = httpx.post(
                f"https://discord.com/api/v10/channels/{target_channel_id}/messages",
                headers={
                    "authorization": f"Bot {settings.discord_bot_token}",
                    "content-type": "application/json",
                },
                json={
                    "content": content,
                    "allowed_mentions": {"parse": []},
                    "components": _discord_notification_components(
                        notification_id=notification_id,
                        job_id=job.id if job is not None else None,
                    ),
                },
                timeout=settings.discord_notification_timeout_seconds,
            )
            message_payload = {"status_code": response.status_code, "body": response.text[:1000]}
            if response.status_code < 200 or response.status_code >= 300:
                error = f"Discord returned HTTP {response.status_code}"

        response_payload = {
            "thread": thread_payload,
            "message": message_payload,
        }
    except ValueError:
        error = "Discord returned invalid thread JSON"
    except httpx.HTTPError as exc:
        error = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")

    with session_factory() as db:
        with db.begin():
            notification = db.scalar(
                select(NotificationRecord)
                .where(NotificationRecord.id == notification_id)
                .with_for_update()
                .limit(1)
            )
            if notification is None:
                raise RuntimeError("notification not found after delivery attempt")
            payload = notification.payload if isinstance(notification.payload, dict) else {}
            job_id = payload.get("job_id")
            job = (
                db.scalar(
                    select(JobRecord)
                    .where(JobRecord.id == job_id)
                    .with_for_update()
                    .limit(1)
                )
                if isinstance(job_id, str)
                else None
            )
            now = _utcnow()
            if job is not None and created_thread_id is not None:
                job.discord_thread_id = created_thread_id
                job.updated_at = now
            db.add(
                NotificationDeliveryRecord(
                    id=_new_id("ndl"),
                    notification_id=notification.id,
                    channel="discord",
                    status="failed" if error is not None else "succeeded",
                    error=error,
                    response_payload=response_payload,
                    created_at=now,
                )
            )
            notification.status = "failed" if error is not None else "delivered"
            notification.delivered_at = now if error is None else notification.delivered_at
            notification.updated_at = now

    if error is not None:
        raise RuntimeError(error)


def _discord_thread_name(job: JobRecord) -> str:
    title = job.title or job.external_job_id
    normalized = " ".join(title.split())
    if len(normalized) <= 80:
        return normalized
    return normalized[:80].rstrip()


def _discord_notification_components(
    *,
    notification_id: str,
    job_id: str | None,
) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    if job_id is not None:
        buttons.append(
            {
                "type": 2,
                "style": 2,
                "label": "Refresh job",
                "custom_id": f"ariel:job:refresh:{job_id}",
            }
        )
    buttons.append(
        {
            "type": 2,
            "style": 1,
            "label": "Acknowledge",
            "custom_id": f"ariel:notification:ack:{notification_id}",
        }
    )
    return [{"type": 1, "components": buttons}]


def _expire_approvals(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
) -> None:
    session_id = _payload_text(task_payload, "session_id")
    with session_factory() as db:
        with db.begin():
            if session_id is not None:
                reconcile_expired_approvals_for_session(
                    db=db,
                    session_id=session_id,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
                return
            session_ids = db.scalars(select(SessionRecord.id)).all()
            for existing_session_id in session_ids:
                reconcile_expired_approvals_for_session(
                    db=db,
                    session_id=existing_session_id,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )


if __name__ == "__main__":
    main()
