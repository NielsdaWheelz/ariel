from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import ulid
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .action_runtime import (
    process_action_execution_task,
    process_provider_write_reconcile_due,
    reconcile_expired_approvals_for_session,
)
from .app import (
    Runtime,
    WakeContext,
    _get_or_create_active_session,
    _wake,
    build_agency_runtime,
    build_google_runtime,
    build_runtime,
)
from .config import AppSettings
from .google_connector import GOOGLE_CONNECTOR_ID
from .persistence import (
    AgencyEventRecord,
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    JobEventRecord,
    JobRecord,
    ProviderWatchChannelRecord,
    SessionRecord,
    SyncCursorRecord,
    enqueue_background_task,
)
from .memory import enqueue_due_memory_sweep, run_rememberer
from .redaction import safe_failure_reason
from .sync_runtime import (
    process_provider_event_received,
    process_provider_sync_due,
)


class UnsupportedTaskType(RuntimeError):
    pass


# A failing task retries up to this many times. A recurring task that exhausts
# its retries is re-armed to its next occurrence rather than dropped; a one-shot
# is deleted.
MAX_TASK_ATTEMPTS = 5


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


def select_next_task(db: Session, *, now: datetime) -> BackgroundTaskRecord | None:
    # The single-threaded worker takes the earliest due row. There is no claim
    # protocol: "a row exists and is due" is the only pending state.
    return db.scalar(
        select(BackgroundTaskRecord)
        .where(BackgroundTaskRecord.run_after <= now)
        .order_by(
            BackgroundTaskRecord.run_after.asc(),
            BackgroundTaskRecord.created_at.asc(),
            BackgroundTaskRecord.id.asc(),
        )
        .limit(1)
    )


_PROVIDER_WATCH_RENEW_INTERVAL_SECONDS = 6 * 3600


def seed_provider_maintenance_tasks(
    db: Session,
    *,
    settings: AppSettings,
    now: datetime,
) -> None:
    # Ensure exactly one recurring task of each provider-maintenance type
    # exists. Once a row is seeded the worker's recurrence path re-enqueues
    # it; this seeder only fills a gap when no row of the type is present.
    plans = (
        ("provider_watch_renew_due", _PROVIDER_WATCH_RENEW_INTERVAL_SECONDS),
        ("provider_reconcile_sync_due", settings.provider_reconcile_sync_interval_seconds),
    )
    for task_type, recurrence_seconds in plans:
        existing_id = db.scalar(
            select(BackgroundTaskRecord.id)
            .where(BackgroundTaskRecord.task_type == task_type)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if existing_id is not None:
            continue
        enqueue_background_task(
            db,
            task_type=task_type,
            payload={"origin": "worker_provider_maintenance"},
            now=now,
            recurrence_seconds=recurrence_seconds,
        )


_PROVIDER_WATCH_RENEW_LEAD_SECONDS = 24 * 3600


def process_provider_watch_renew_due(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    # Re-arm any push channel approaching expiry. register_provider_watches
    # is idempotent and absorbs per-watch failures, so renewing all eligible
    # watches once a near-expiry row exists is the whole handler.
    #
    # A token-refresh failure here is a connector error the user must see:
    # access_token_for_background_sync records the connector error state on
    # this same transaction, and we enqueue a single agent_wake before the
    # block commits, so the wake and the connector error commit together.
    now = now_fn()
    runtime = build_google_runtime(settings)
    with session_factory() as db:
        with db.begin():
            renew_horizon = now + timedelta(seconds=_PROVIDER_WATCH_RENEW_LEAD_SECONDS)
            near_expiry_id = db.scalar(
                select(ProviderWatchChannelRecord.id)
                .where(
                    ProviderWatchChannelRecord.status == "active",
                    ProviderWatchChannelRecord.expires_at <= renew_horizon,
                )
                .limit(1)
            )
            if near_expiry_id is None:
                return
            connector = db.scalar(
                select(GoogleConnectorRecord)
                .where(GoogleConnectorRecord.id == GOOGLE_CONNECTOR_ID)
                .limit(1)
            )
            if connector is None or connector.status != "connected":
                return
            try:
                access_token = runtime.access_token_for_background_sync(
                    db=db,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
            except RuntimeError as exc:
                error_code = safe_failure_reason(str(exc), fallback="token_refresh_failed")
                enqueue_background_task(
                    db,
                    task_type="agent_wake",
                    payload={
                        "note": (
                            f"The Google connector reported an error {error_code}; "
                            "the user may need to reconnect."
                        )
                    },
                    now=now,
                )
                return
            runtime.register_provider_watches(
                db=db,
                access_token=access_token,
                granted_scopes=list(connector.granted_scopes),
                account_subject=connector.account_subject or connector.id,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )


def process_provider_reconcile_sync_due(
    *,
    session_factory: sessionmaker[Session],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    # The poll baseline: enqueue a provider_sync_due for every cursor of a
    # connected connector, independent of whether push is delivering.
    now = now_fn()
    with session_factory() as db:
        with db.begin():
            connector = db.scalar(
                select(GoogleConnectorRecord)
                .where(GoogleConnectorRecord.id == GOOGLE_CONNECTOR_ID)
                .limit(1)
            )
            if connector is None or connector.status != "connected":
                return
            cursors = db.scalars(
                select(SyncCursorRecord)
                .where(SyncCursorRecord.provider == "google")
                .order_by(SyncCursorRecord.id.asc())
            ).all()
            for cursor in cursors:
                enqueue_background_task(
                    db,
                    task_type="provider_sync_due",
                    payload={
                        "provider": "google",
                        "resource_type": cursor.resource_type,
                        "resource_id": cursor.resource_id,
                    },
                    now=now,
                )


def process_one_task(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings | None = None,
    model_adapter: Any | None = None,
    runtime: Runtime | None = None,
) -> bool:
    resolved_settings = settings or AppSettings()

    with session_factory() as db:
        with db.begin():
            now = _utcnow()
            enqueue_due_memory_sweep(db, settings=resolved_settings, now=now)
            seed_provider_maintenance_tasks(db, settings=resolved_settings, now=now)

    with session_factory() as db:
        with db.begin():
            task = select_next_task(db, now=_utcnow())
            if task is None:
                return False
            task_id = task.id
            task_type = task.task_type
            task_shape_error: str | None = None
            if isinstance(task.payload, dict):
                task_payload = dict(task.payload)
            else:
                task_payload = {}
                task_shape_error = f"{task_type} task payload invalid"
            if task_type == "provider_write_reconcile_due":
                if task.provider_write_receipt_id is None:
                    task_shape_error = "provider_write_reconcile_due task shape invalid"
                else:
                    expected_idempotency_key = (
                        f"provider_write_reconcile:{task.provider_write_receipt_id}"
                    )
                    if task.idempotency_key != expected_idempotency_key:
                        task_shape_error = "provider_write_reconcile_due task idempotency mismatch"
                    else:
                        task_payload = {
                            "provider_write_receipt_id": task.provider_write_receipt_id,
                            "idempotency_key": expected_idempotency_key,
                        }

    try:
        if task_shape_error is not None:
            raise RuntimeError(task_shape_error)
        match task_type:
            case "agency_event_received":
                _process_agency_event_received(
                    session_factory=session_factory,
                    task_payload=task_payload,
                )
            case "agent_wake":
                note = _payload_text(task_payload, "note")
                if note is None:
                    raise RuntimeError("agent_wake task missing note")
                if runtime is None:
                    raise RuntimeError("agent_wake task requires a configured runtime")
                with session_factory() as db:
                    session = _get_or_create_active_session(db)
                    _wake(
                        runtime=runtime,
                        db=db,
                        request_session_id=session.id,
                        wake_context=WakeContext(
                            trigger_kind="scheduled_task",
                            prompt_text=note,
                            discord_context=None,
                            attachment_sources=None,
                            ingress_provenance=None,
                        ),
                        google_runtime=build_google_runtime(runtime.settings),
                    )
                    db.commit()
            case "execute_action_attempt":
                action_attempt_id = _payload_text(task_payload, "action_attempt_id")
                if action_attempt_id is None:
                    raise RuntimeError("execute_action_attempt task missing action_attempt_id")
                process_action_execution_task(
                    session_factory=session_factory,
                    action_attempt_id=action_attempt_id,
                    google_runtime=build_google_runtime(resolved_settings),
                    agency_runtime=build_agency_runtime(resolved_settings),
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                    settings=resolved_settings,
                )
            case "provider_write_reconcile_due":
                shape_error = _payload_text(task_payload, "shape_error")
                if shape_error is not None:
                    raise RuntimeError(shape_error)
                process_provider_write_reconcile_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    agency_runtime=build_agency_runtime(resolved_settings),
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "expire_approvals":
                _expire_approvals(session_factory=session_factory, task_payload=task_payload)
            case "provider_event_received":
                process_provider_event_received(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    now_fn=_utcnow,
                )
            case "provider_sync_due":
                process_provider_sync_due(
                    session_factory=session_factory,
                    task_payload=task_payload,
                    settings=resolved_settings,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "memory_remember":
                turn_id = _payload_text(task_payload, "turn_id")
                if turn_id is None:
                    raise RuntimeError("memory_remember task missing turn_id")
                run_rememberer(
                    session_factory=session_factory,
                    settings=resolved_settings,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                    trigger="turn",
                    turn_id=turn_id,
                )
            case "memory_sweep":
                run_rememberer(
                    session_factory=session_factory,
                    settings=resolved_settings,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                    trigger="sweep",
                )
            case "provider_watch_renew_due":
                process_provider_watch_renew_due(
                    session_factory=session_factory,
                    settings=resolved_settings,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case "provider_reconcile_sync_due":
                process_provider_reconcile_sync_due(
                    session_factory=session_factory,
                    now_fn=_utcnow,
                    new_id_fn=_new_id,
                )
            case _:
                raise UnsupportedTaskType(f"unsupported task type: {task_type}")
    except UnsupportedTaskType:
        _mark_task_failed(session_factory=session_factory, task_id=task_id)
        return True
    except Exception:
        _mark_task_failed(session_factory=session_factory, task_id=task_id)
        return True

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, task_id)
            if task is not None:
                # A recurring task is re-armed in place to its next occurrence;
                # a one-shot is deleted. A row is deleted only on success.
                if task.recurrence_seconds is not None:
                    now = _utcnow()
                    task.run_after = now + timedelta(seconds=task.recurrence_seconds)
                    task.attempts = 0
                    task.updated_at = now
                else:
                    db.delete(task)
    return True


def run_worker(*, runtime: Runtime) -> None:
    while True:
        processed = process_one_task(
            session_factory=runtime.session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )
        if not processed:
            time.sleep(runtime.settings.worker_poll_seconds)


def main() -> None:
    runtime, engine = build_runtime()
    try:
        runtime.sandbox.start()
        run_worker(runtime=runtime)
    finally:
        runtime.sandbox.close()
        engine.dispose()


def _mark_task_failed(
    *,
    session_factory: sessionmaker[Session],
    task_id: str,
) -> None:
    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, task_id)
            if task is None:
                return
            now = _utcnow()
            task.attempts += 1
            task.updated_at = now
            if task.attempts >= MAX_TASK_ATTEMPTS:
                # A recurring maintenance task is never permanently lost: it is
                # re-armed to its next occurrence. A one-shot gives up.
                if task.recurrence_seconds is not None:
                    task.run_after = now + timedelta(seconds=task.recurrence_seconds)
                    task.attempts = 0
                else:
                    db.delete(task)
                return
            task.run_after = now + timedelta(seconds=min(300, 2 ** (task.attempts - 1)))


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
                # A job reaching a settled state wakes the agent so it can
                # review the job and decide whether to inform the user.
                job_name = job.title or job.external_job_id
                enqueue_background_task(
                    db,
                    task_type="agent_wake",
                    payload={
                        "note": (
                            f"The coding job '{job_name}' is now {status}. "
                            "Review it and decide whether to inform the user."
                        )
                    },
                    now=now,
                )

            agency_event.status = "processed"
            agency_event.processed_at = now


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
