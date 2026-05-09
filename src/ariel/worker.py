from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import ulid
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .action_runtime import process_action_execution_task, reconcile_expired_approvals_for_session
from .agency_daemon import AgencyDaemonClient, AgencyRuntime
from .config import AppSettings
from .google_connector import (
    DefaultGoogleOAuthClient,
    DefaultGoogleWorkspaceProvider,
    GoogleConnectorRuntime,
)
from .persistence import (
    AgencyEventRecord,
    BackgroundTaskRecord,
    ConnectorSubscriptionRecord,
    EmailThreadWatchRecord,
    JobEventRecord,
    JobRecord,
    NotificationDeliveryRecord,
    NotificationRecord,
    SessionRecord,
    SyncCursorRecord,
)
from .memory import process_memory_extract_turn, process_memory_projection_job
from .proactivity import (
    mark_proactive_turn_delivered,
    process_proactive_action_execution_due,
    process_proactive_deliberation_due,
    process_proactive_feedback_learning_due,
    process_proactive_follow_up_due,
    process_ambient_interpretation_due,
)
from .redaction import safe_failure_reason
from .sync_runtime import (
    emit_email_thread_watch_signal,
    process_provider_event_received,
    process_provider_sync_due,
)


PROACTIVE_RECOVERABLE_TASK_TYPES = (
    "ambient_interpretation_due",
    "proactive_deliberation_due",
    "proactive_follow_up_due",
    "proactive_feedback_learning_due",
    "proactive_action_execution_due",
)


class UnsupportedTaskType(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


def process_due_email_thread_watches(
    *,
    session_factory: sessionmaker[Session],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> int:
    now = now_fn()
    with session_factory() as db:
        with db.begin():
            pending_provider_sync_task_id = db.scalar(
                select(BackgroundTaskRecord.id)
                .where(
                    BackgroundTaskRecord.status.in_(("pending", "running")),
                    BackgroundTaskRecord.run_after <= now,
                    BackgroundTaskRecord.task_type.in_(
                        ("provider_event_received", "provider_sync_due")
                    ),
                )
                .order_by(BackgroundTaskRecord.run_after.asc(), BackgroundTaskRecord.id.asc())
                .limit(1)
            )
            syncing_gmail_cursor_id = db.scalar(
                select(SyncCursorRecord.id)
                .where(
                    SyncCursorRecord.provider == "google",
                    SyncCursorRecord.resource_type == "gmail",
                    SyncCursorRecord.status == "syncing",
                )
                .limit(1)
            )
            if pending_provider_sync_task_id is not None or syncing_gmail_cursor_id is not None:
                return 0

            watches = db.scalars(
                select(EmailThreadWatchRecord)
                .where(
                    EmailThreadWatchRecord.status == "active",
                    EmailThreadWatchRecord.deadline <= now,
                )
                .with_for_update()
                .order_by(EmailThreadWatchRecord.deadline.asc(), EmailThreadWatchRecord.id.asc())
                .limit(100)
            ).all()
            for watch in watches:
                if watch.condition == "no_reply_by_deadline":
                    watch.status = "due"
                    emit_email_thread_watch_signal(
                        db,
                        watch=watch,
                        signal_type="email_thread_watch_due",
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                else:
                    watch.status = "failed"
                watch.updated_at = now
            return len(watches)


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
    recoverable_failed_tasks = db.scalars(
        select(BackgroundTaskRecord)
        .where(
            BackgroundTaskRecord.status == "failed",
            BackgroundTaskRecord.task_type.in_(PROACTIVE_RECOVERABLE_TASK_TYPES),
            BackgroundTaskRecord.attempts < BackgroundTaskRecord.max_attempts,
            BackgroundTaskRecord.run_after <= now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for task in recoverable_failed_tasks:
        task.status = "pending"
        task.claimed_by = None
        task.last_heartbeat = None
        task.updated_at = now
    if stale_tasks or recoverable_failed_tasks:
        db.flush()
    return len(stale_tasks) + len(recoverable_failed_tasks)


def enqueue_due_worker_owned_ambient_task(
    db: Session,
    *,
    settings: AppSettings,
    now: datetime,
) -> BackgroundTaskRecord | None:
    due_task_id = db.scalar(
        select(BackgroundTaskRecord.id)
        .where(
            BackgroundTaskRecord.status == "pending",
            BackgroundTaskRecord.run_after <= now,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if due_task_id is not None:
        return None
    latest = db.scalar(
        select(BackgroundTaskRecord)
        .where(BackgroundTaskRecord.task_type == "ambient_interpretation_due")
        .order_by(
            BackgroundTaskRecord.run_after.desc(),
            BackgroundTaskRecord.created_at.desc(),
            BackgroundTaskRecord.id.desc(),
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if latest is not None and latest.status in {"pending", "running"}:
        return None
    due_after = now - timedelta(seconds=settings.proactive_ambient_interval_seconds)
    if latest is not None and latest.run_after > due_after:
        return None
    return enqueue_background_task(
        db,
        task_type="ambient_interpretation_due",
        payload={"origin": "worker_ambient"},
        now=now,
        max_attempts=settings.proactive_worker_max_attempts,
    )


def _google_runtime(settings: AppSettings) -> GoogleConnectorRuntime:
    return GoogleConnectorRuntime(
        oauth_client=DefaultGoogleOAuthClient(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            timeout_seconds=settings.google_oauth_timeout_seconds,
        ),
        workspace_provider=DefaultGoogleWorkspaceProvider(),
        redirect_uri=settings.google_oauth_redirect_uri,
        oauth_state_ttl_seconds=settings.google_oauth_state_ttl_seconds,
        encryption_secret=settings.connector_encryption_secret,
        encryption_key_version=settings.connector_encryption_key_version,
        encryption_keys=settings.connector_encryption_keys,
    )


def _agency_runtime(settings: AppSettings) -> AgencyRuntime:
    return AgencyRuntime(
        client=AgencyDaemonClient(
            socket_path=settings.agency_socket_path,
            timeout_seconds=settings.agency_timeout_seconds,
        ),
        allowed_repo_roots=tuple(
            root.strip() for root in settings.agency_allowed_repo_roots.split(",") if root.strip()
        ),
        default_base_branch=settings.agency_default_base_branch,
        default_runner=settings.agency_default_runner,
    )


def process_one_task(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings | None = None,
    worker_id: str | None = None,
    model_adapter: Any | None = None,
) -> bool:
    resolved_settings = settings or AppSettings()
    resolved_worker_id = worker_id or f"worker-{ulid.new().str.lower()}"

    with session_factory() as db:
        with db.begin():
            now = _utcnow()
            reap_stale_tasks(
                db,
                now=now,
                heartbeat_timeout_seconds=resolved_settings.worker_heartbeat_timeout_seconds,
            )

    if process_due_email_thread_watches(
        session_factory=session_factory,
        now_fn=_utcnow,
        new_id_fn=_new_id,
    ):
        return True

    with session_factory() as db:
        with db.begin():
            now = _utcnow()
            enqueue_due_worker_owned_ambient_task(db, settings=resolved_settings, now=now)

    if process_memory_projection_job(
        session_factory=session_factory,
        settings=resolved_settings,
        now_fn=_utcnow,
        new_id_fn=_new_id,
    ):
        return True

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
            case "execute_action_attempt":
                action_attempt_id = _payload_text(task_payload, "action_attempt_id")
                if action_attempt_id is None:
                    raise RuntimeError("execute_action_attempt task missing action_attempt_id")
                process_action_execution_task(
                    session_factory=session_factory,
                    action_attempt_id=action_attempt_id,
                    google_runtime=_google_runtime(resolved_settings),
                    agency_runtime=_agency_runtime(resolved_settings),
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
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
            case "provider_event_received":
                process_provider_event_received(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "provider_subscription_renewal_due":
                _process_provider_subscription_renewal_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                )
            case "provider_sync_due":
                process_provider_sync_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "memory_extract_turn":
                process_memory_extract_turn(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "ambient_interpretation_due":
                process_ambient_interpretation_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                    model_adapter=model_adapter,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "proactive_deliberation_due":
                process_proactive_deliberation_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                    model_adapter=model_adapter,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "proactive_follow_up_due":
                process_proactive_follow_up_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "proactive_feedback_learning_due":
                process_proactive_feedback_learning_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                    model_adapter=model_adapter,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "proactive_action_execution_due":
                process_proactive_action_execution_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    google_runtime=_google_runtime(resolved_settings),
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case _:
                raise UnsupportedTaskType(f"unsupported task type: {task_type}")
    except UnsupportedTaskType as exc:
        _mark_task_failed(
            session_factory=session_factory,
            task_id=task_id,
            error=safe_failure_reason(str(exc), fallback="unsupported task type"),
            retry=False,
        )
        return True
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
    engine = create_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        isolation_level="SERIALIZABLE",
    )
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
    retry: bool = True,
) -> None:
    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, task_id)
            if task is None:
                return
            now = _utcnow()
            task.status = (
                "pending" if retry and task.attempts < task.max_attempts else "dead_letter"
            )
            task.error = error
            task.claimed_by = None
            task.last_heartbeat = None
            task.run_after = (
                now + timedelta(seconds=min(300, 2 ** max(task.attempts - 1, 0)))
                if task.status == "pending"
                else now
            )
            task.updated_at = now


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _process_provider_subscription_renewal_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
) -> None:
    subscription_id = _payload_text(task_payload, "subscription_id")
    if subscription_id is None:
        raise RuntimeError("provider_subscription_renewal_due task missing subscription_id")
    with session_factory() as db:
        with db.begin():
            subscription = db.scalar(
                select(ConnectorSubscriptionRecord)
                .where(ConnectorSubscriptionRecord.id == subscription_id)
                .with_for_update()
                .limit(1)
            )
            if subscription is None:
                raise RuntimeError("connector subscription not found")
            now = _utcnow()
            subscription.status = "renewal_due"
            subscription.renew_after = now
            subscription.updated_at = now
            enqueue_background_task(
                db,
                task_type="provider_sync_due",
                payload={
                    "provider": subscription.provider,
                    "resource_type": subscription.resource_type,
                    "resource_id": subscription.resource_id,
                    "subscription_id": subscription.id,
                    "reason": "subscription_renewal_due",
                },
                now=now,
                max_attempts=settings.proactive_worker_max_attempts,
            )


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
                enqueue_background_task(
                    db,
                    task_type="ambient_interpretation_due",
                    payload={"origin": "agency_event", "agency_event_id": agency_event.id},
                    now=now,
                    max_attempts=3,
                )

            agency_event.status = "processed"
            agency_event.processed_at = now


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
            proactive_turn_id = payload.get("proactive_turn_id")
            proactive_case_id = payload.get("case_id")
            job = (
                db.scalar(
                    select(JobRecord).where(JobRecord.id == job_id).with_for_update().limit(1)
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
                        proactive_case_id=(
                            proactive_case_id if isinstance(proactive_case_id, str) else None
                        ),
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
                    select(JobRecord).where(JobRecord.id == job_id).with_for_update().limit(1)
                )
                if isinstance(job_id, str)
                else None
            )
            now = _utcnow()
            if job is not None and created_thread_id is not None:
                job.discord_thread_id = created_thread_id
                job.updated_at = now
            proactive_turn_id = payload.get("proactive_turn_id")
            if error is None and isinstance(proactive_turn_id, str):
                mark_proactive_turn_delivered(
                    db=db,
                    proactive_turn_id=proactive_turn_id,
                    now=now,
                )
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
    proactive_case_id: str | None,
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
    if proactive_case_id is not None:
        buttons.append(
            {
                "type": 2,
                "style": 1,
                "label": "Acknowledge",
                "custom_id": f"ariel:proactive:ack:{proactive_case_id}",
            }
        )
        buttons.append(
            {
                "type": 2,
                "style": 2,
                "label": "Correct",
                "custom_id": f"ariel:proactive:correct:{proactive_case_id}",
            }
        )
        buttons.append(
            {
                "type": 2,
                "style": 2,
                "label": "Stop pattern",
                "custom_id": f"ariel:proactive:stop:{proactive_case_id}",
            }
        )
        buttons.append(
            {
                "type": 2,
                "style": 2,
                "label": "More",
                "custom_id": f"ariel:proactive:more:{proactive_case_id}",
            }
        )
        return [{"type": 1, "components": buttons}]
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
