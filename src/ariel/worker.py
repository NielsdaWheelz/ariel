from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import ulid
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .action_runtime import (
    RuntimeProvenance,
    process_action_execution_task,
    process_provider_write_reconcile_due,
    reconcile_expired_approvals_for_session,
)
from .app import (
    Runtime,
    TurnExecutionOutcome,
    WakeContext,
    _get_or_create_active_session,
    _wake,
    build_agency_runtime,
    build_google_runtime,
    build_runtime,
)
from .capability_registry import REMEMBERER_CAPABILITY_IDS, capability_action_label
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
from .memory import enqueue_due_memory_dream, run_rememberer
from .redaction import safe_failure_reason
from .research_runtime import ResearchFinding, render_finding, run_research
from .sandbox_runtime import RunSandbox, SandboxRuntime
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


def _deliver_to_discord(*, outcome: TurnExecutionOutcome, settings: AppSettings) -> None:
    if settings.discord_bot_token is None or settings.discord_channel_id is None:
        return
    if outcome.status_code != 200:
        return
    assistant = outcome.response_payload.get("assistant")
    if not isinstance(assistant, dict) or assistant.get("silent") is True:
        return
    message = assistant.get("message")
    if not isinstance(message, str) or not message.strip():
        return

    # Collect pending approvals from the turn's surface_action_lifecycle.
    turn = outcome.response_payload.get("turn")
    lifecycle = turn.get("surface_action_lifecycle") if isinstance(turn, dict) else None
    pending_approvals: list[dict[str, str]] = []
    if isinstance(lifecycle, list):
        for item in lifecycle:
            if not isinstance(item, dict):
                continue
            approval = item.get("approval")
            if not isinstance(approval, dict) or approval.get("status") != "pending":
                continue
            ref = approval.get("reference")
            if not isinstance(ref, str) or not ref:
                continue
            proposal = item.get("proposal")
            action_label = "Action"
            if isinstance(proposal, dict):
                capability_id_raw = proposal.get("capability_id")
                if isinstance(capability_id_raw, str):
                    action_label = capability_action_label(capability_id_raw)
            entry: dict[str, str] = {"ref": ref, "action_label": action_label}
            expires_at = approval.get("expires_at")
            if isinstance(expires_at, str):
                entry["expires_at"] = expires_at
            pending_approvals.append(entry)

    # Build message content: base message, then approval-pending lines.
    content = message.strip()
    if pending_approvals:
        approval_lines: list[str] = []
        for entry in pending_approvals:
            suffix = f" expires_at={entry['expires_at']}" if "expires_at" in entry else ""
            approval_lines.append(
                f"Approval pending ({entry['action_label']}): {entry['ref']}{suffix}. "
                "Use the buttons below."
            )
        content = "\n".join([content, "", *approval_lines])

    if len(content) > 1900:
        # Discord's hard limit is 2000 characters; truncate with a marker.
        content = content[:1888].rstrip() + "\n[truncated]"

    body: dict[str, Any] = {"content": content}
    if pending_approvals:
        body["components"] = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": "Approve",
                        "custom_id": f"ariel:approval:approve:{entry['ref']}",
                    },
                    {
                        "type": 2,
                        "style": 4,
                        "label": "Deny",
                        "custom_id": f"ariel:approval:deny:{entry['ref']}",
                    },
                ],
            }
            for entry in pending_approvals
        ]

    try:
        httpx.post(
            f"https://discord.com/api/v10/channels/{settings.discord_channel_id}/messages",
            headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            json=body,
            timeout=settings.discord_notification_timeout_seconds,
        )
    except httpx.HTTPError:
        return


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


# Gmail's watch expires after 7 days; Google recommends daily renewal. With
# the 6-hour sweep, a 6-day lead means every watch under 6 days remaining is
# renewed each sweep — effective daily cadence with retry headroom.
_PROVIDER_WATCH_RENEW_LEAD_SECONDS = 6 * 24 * 3600


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


def _require_sandbox(runtime: Runtime) -> RunSandbox:
    sandbox = runtime.sandbox
    if sandbox is None:
        raise RuntimeError(
            "worker requires runtime.sandbox; worker.main() must call "
            "build_runtime(sandbox=SandboxRuntime())"
        )
    return sandbox


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
            enqueue_due_memory_dream(db, settings=resolved_settings, now=now)
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
                if runtime is None:
                    raise RuntimeError("agent_wake task requires a configured runtime")
                research_session_id, wake_context = _agent_wake_context(task_payload)
                with session_factory() as db:
                    # A research-completion wake targets the session that
                    # dispatched the run; a plain note wake uses the active one.
                    request_session_id = research_session_id or _get_or_create_active_session(db).id
                    outcome = _wake(
                        runtime=runtime,
                        db=db,
                        request_session_id=request_session_id,
                        wake_context=wake_context,
                        google_runtime=build_google_runtime(runtime.settings),
                    )
                    db.commit()
                _deliver_to_discord(outcome=outcome, settings=runtime.settings)
            case "research_run":
                if runtime is None:
                    raise RuntimeError("research_run task requires a configured runtime")
                _process_research_run(runtime=runtime, task_payload=task_payload)
            case "user_message":
                if runtime is None:
                    raise RuntimeError("user_message task requires a configured runtime")
                session_id = _payload_text(task_payload, "session_id")
                message = _payload_text(task_payload, "message")
                if session_id is None or message is None:
                    raise RuntimeError("user_message task payload invalid")
                discord_context = task_payload.get("discord_context")
                attachment_sources = task_payload.get("attachment_sources")
                with session_factory() as db:
                    outcome = _wake(
                        runtime=runtime,
                        db=db,
                        request_session_id=session_id,
                        wake_context=WakeContext(
                            trigger_kind="user_message",
                            prompt_text=message,
                            discord_context=discord_context
                            if isinstance(discord_context, dict)
                            else None,
                            attachment_sources=attachment_sources
                            if isinstance(attachment_sources, list)
                            else None,
                            ingress_provenance=None,
                        ),
                        google_runtime=build_google_runtime(runtime.settings),
                    )
                    db.commit()
                _deliver_to_discord(outcome=outcome, settings=runtime.settings)
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
            case "memory_encode":
                if runtime is None:
                    raise RuntimeError("memory_encode task requires a configured runtime")
                note = _payload_text(task_payload, "note")
                if not note:
                    raise RuntimeError("memory_encode task missing note")
                session_id = _payload_text(task_payload, "session_id")
                with runtime.session_factory() as db:
                    run_rememberer(
                        trigger="encode",
                        sandbox=_require_sandbox(runtime),
                        db=db,
                        session_factory=runtime.session_factory,
                        session_id=session_id,
                        settings=runtime.settings,
                        model_adapter=runtime.model_adapter,
                        google_runtime=build_google_runtime(runtime.settings),
                        agency_runtime=None,
                        attachment_runtime=None,
                        note=note,
                        allowed_capability_ids=REMEMBERER_CAPABILITY_IDS,
                        approval_ttl_seconds=int(runtime.settings.approval_ttl_seconds),
                        approval_actor_id=str(runtime.settings.approval_actor_id),
                        add_event=lambda *_args, **_kwargs: None,
                        now_fn=_utcnow,
                        new_id_fn=_new_id,
                    )
            case "memory_dream":
                if runtime is None:
                    raise RuntimeError("memory_dream task requires a configured runtime")
                with runtime.session_factory() as db:
                    run_rememberer(
                        trigger="dream",
                        sandbox=_require_sandbox(runtime),
                        db=db,
                        session_factory=runtime.session_factory,
                        session_id=None,
                        settings=runtime.settings,
                        model_adapter=runtime.model_adapter,
                        google_runtime=build_google_runtime(runtime.settings),
                        agency_runtime=None,
                        attachment_runtime=None,
                        note=None,
                        allowed_capability_ids=REMEMBERER_CAPABILITY_IDS,
                        approval_ttl_seconds=int(runtime.settings.approval_ttl_seconds),
                        approval_actor_id=str(runtime.settings.approval_actor_id),
                        add_event=lambda *_args, **_kwargs: None,
                        now_fn=_utcnow,
                        new_id_fn=_new_id,
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
    sandbox = SandboxRuntime()
    runtime, engine = build_runtime(sandbox=sandbox)
    try:
        sandbox.start()
        run_worker(runtime=runtime)
    finally:
        sandbox.close()
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


def _parse_research_finding(raw: dict[str, Any]) -> ResearchFinding:
    """Reconstruct a ``ResearchFinding`` from a completion ``agent_wake`` payload.

    Raises ``RuntimeError`` on any bad shape so the worker's task-failure path
    marks the row failed — mirroring the validate-inside-the-arm style of
    ``case "user_message":``."""
    question = raw.get("question")
    mode = raw.get("mode")
    status = raw.get("status")
    summary = raw.get("summary")
    claims = raw.get("claims")
    gaps = raw.get("gaps")
    sources = raw.get("sources")
    if (
        not isinstance(question, str)
        or not isinstance(mode, str)
        or not isinstance(status, str)
        or not isinstance(summary, str)
        or not isinstance(claims, list)
        or not isinstance(gaps, list)
        or not isinstance(sources, list)
    ):
        raise RuntimeError("agent_wake research_finding payload invalid")
    return ResearchFinding(
        question=question,
        mode=mode,
        status=status,
        summary=summary,
        claims=claims,
        gaps=gaps,
        sources=sources,
    )


def _agent_wake_context(task_payload: dict[str, Any]) -> tuple[str | None, WakeContext]:
    """Build the ``WakeContext`` for an ``agent_wake`` task.

    Two shapes reach this arm. A research-completion wake carries a
    ``research_finding`` object and the ``session_id`` that dispatched the run:
    the finding is rendered into the prompt as a clearly-attributed block and the
    wake is carried with tainted ``ingress_provenance`` — the finding's text is
    model-authored over untrusted content, so a prompt-injected finding cannot
    authorize an unapproved action. A plain wake carries a ``note`` and keeps the
    untainted ``scheduled_task`` path unchanged. Returns the target session id
    (the carried session for a research completion, ``None`` for a plain note —
    the caller resolves the active session) and the context."""
    raw_finding = task_payload.get("research_finding")
    if isinstance(raw_finding, dict):
        finding = _parse_research_finding(raw_finding)
        session_id = _payload_text(task_payload, "session_id")
        if session_id is None:
            raise RuntimeError("agent_wake research_finding task missing session_id")
        return session_id, WakeContext(
            trigger_kind="research_completion",
            prompt_text=render_finding(finding),
            discord_context=None,
            attachment_sources=None,
            ingress_provenance=RuntimeProvenance(
                status="tainted",
                evidence=(
                    {
                        "kind": "research_finding_in_context",
                        "research_mode": finding.mode,
                        "research_status": finding.status,
                    },
                ),
            ),
        )
    note = _payload_text(task_payload, "note")
    if note is None:
        raise RuntimeError("agent_wake task missing note")
    return None, WakeContext(
        trigger_kind="scheduled_task",
        prompt_text=note,
        discord_context=None,
        attachment_sources=None,
        ingress_provenance=None,
    )


def _process_research_run(*, runtime: Runtime, task_payload: dict[str, Any]) -> None:
    """Run one ``research_run`` task: drive ``run_research`` in the worker, then
    enqueue a completion ``agent_wake`` carrying the finding back to the session
    that dispatched the run.

    ``question``, ``mode``, and ``session_id`` are validated inside the arm —
    a bad shape raises so the task-failure path marks the row failed, mirroring
    ``case "user_message":``. ``run_research`` records the run as a
    ``kind="research"`` ``TurnRecord`` and never raises; its typed
    ``ResearchFinding`` becomes the completion wake's payload."""
    question = _payload_text(task_payload, "question")
    mode = _payload_text(task_payload, "mode")
    session_id = _payload_text(task_payload, "session_id")
    if question is None or mode is None or session_id is None:
        raise RuntimeError("research_run task payload invalid")

    with runtime.session_factory() as db:
        finding = run_research(
            sandbox=_require_sandbox(runtime),
            db=db,
            session_factory=runtime.session_factory,
            settings=runtime.settings,
            model_adapter=runtime.model_adapter,
            google_runtime=build_google_runtime(runtime.settings),
            session_id=session_id,
            question=question,
            mode=mode,
        )

    with runtime.session_factory() as db:
        with db.begin():
            enqueue_background_task(
                db,
                task_type="agent_wake",
                payload={
                    "research_finding": {
                        "question": finding.question,
                        "mode": finding.mode,
                        "status": finding.status,
                        "summary": finding.summary,
                        "claims": finding.claims,
                        "gaps": finding.gaps,
                        "sources": finding.sources,
                    },
                    "session_id": session_id,
                },
                now=_utcnow(),
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
