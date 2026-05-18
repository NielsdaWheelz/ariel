from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.capability_registry import get_capability
from ariel.config import AppSettings
from ariel.executor import execute_capability
from ariel.google_connector import GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_CONNECTOR_ID
from ariel.persistence import (
    AIJudgmentRecord,
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    LeaveByReminderRecord,
    NotificationRecord,
    to_rfc3339,
)
from ariel.redaction import safe_failure_reason

# Scan and loop horizons (spec B.4). The scan only looks one day out; the loop
# wakes ~2h before the event, then once more ~20m before computed departure.
LEAVE_BY_SCAN_HORIZON = timedelta(hours=24)
LEAVE_BY_INITIAL_LOOKAHEAD = timedelta(hours=2)
LEAVE_BY_NOTIFY_LEAD = timedelta(minutes=20)
LEAVE_BY_ARRIVAL_BUFFER = timedelta(minutes=5)
LEAVE_BY_MAX_ORIGIN_GAP = timedelta(hours=3)
# Bounded retry budget for transient maps failures before the loop fails closed.
LEAVE_BY_MAX_COMPUTE_ATTEMPTS = 3

# ``computed`` is not terminal: the plan pass sets it and reschedules, and the
# rescheduled task must reach the notify pass (spec PR4 — without this the loop
# never cycles).
_LEAVE_BY_TERMINAL_STATES = {"notified", "skipped", "cancelled", "failed"}
_LEAVE_BY_TRANSIENT_MAPS_FAILURES = {
    "provider_timeout",
    "provider_rate_limited",
    "provider_upstream_failure",
    "provider_network_failure",
}

LEAVE_BY_EVALUATION_PROMPT_VERSION = "leave-by-evaluation-v1"
_LEAVE_BY_DECISIONS = ("notify", "skip")
_LEAVE_BY_URGENCIES = ("normal", "high")
_LEAVE_BY_EVALUATION_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "urgency", "message"],
    "properties": {
        "decision": {"type": "string", "enum": list(_LEAVE_BY_DECISIONS)},
        "urgency": {"type": "string", "enum": list(_LEAVE_BY_URGENCIES)},
        "message": {"type": "string"},
    },
}


def _event_start(metadata: dict[str, Any]) -> datetime | None:
    """The timed start of a calendar event, or None for all-day or undated
    events. ``metadata_json["start"]`` is ``{value, timezone, all_day}``; a timed
    event carries an RFC3339 ``value`` and ``all_day`` False."""
    start = metadata.get("start")
    if not isinstance(start, dict) or start.get("all_day") is True:
        return None
    value = start.get("value")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _event_location(metadata: dict[str, Any]) -> str | None:
    location = metadata.get("location")
    if not isinstance(location, str):
        return None
    return location.strip() or None


def _calendar_connector_ready(db: Session) -> bool:
    connector = db.get(GoogleConnectorRecord, GOOGLE_CONNECTOR_ID)
    if connector is None or connector.status != "connected":
        return False
    if connector.access_token_enc is None:
        return False
    granted = connector.granted_scopes if isinstance(connector.granted_scopes, list) else []
    return GOOGLE_CALENDAR_READ_SCOPE in granted


def _add_evaluate_task(
    db: Session,
    *,
    reminder_id: str,
    version: int,
    run_after: datetime,
    compute_attempt: int,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    scheduled_for = to_rfc3339(run_after)
    idempotency_key = f"leave-by-evaluate:{reminder_id}:{version}:{scheduled_for}"
    if (
        db.scalar(
            select(BackgroundTaskRecord.id)
            .where(BackgroundTaskRecord.idempotency_key == idempotency_key)
            .limit(1)
        )
        is not None
    ):
        return
    db.add(
        BackgroundTaskRecord(
            id=new_id_fn("tsk"),
            task_type="leave_by_evaluate_due",
            idempotency_key=idempotency_key,
            payload={
                "reminder_id": reminder_id,
                "version": version,
                "scheduled_for": scheduled_for,
                "compute_attempt": compute_attempt,
            },
            status="pending",
            attempts=0,
            max_attempts=3,
            error=None,
            claimed_by=None,
            run_after=run_after,
            last_heartbeat=None,
            created_at=now,
            updated_at=now,
        )
    )


def process_leave_by_scan_due(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    """Worker-owned recurring scan: detect upcoming located calendar events and
    open one ``leave_by_reminders`` row each, and reconcile rows whose backing
    event moved or was cancelled. Inert unless maps is configured and the Google
    calendar connector is ready (spec B.2 component 1)."""
    if settings.maps_api_key is None:
        return
    with session_factory() as db:
        with db.begin():
            if not _calendar_connector_ready(db):
                return
            now = now_fn()
            horizon_end = now + LEAVE_BY_SCAN_HORIZON
            events = db.scalars(
                select(GoogleProviderObjectRecord)
                .where(
                    GoogleProviderObjectRecord.object_type == "calendar_event",
                    GoogleProviderObjectRecord.status == "active",
                )
                .with_for_update()
            ).all()
            reminders = db.scalars(
                select(LeaveByReminderRecord)
                .where(LeaveByReminderRecord.state.in_(("scheduled", "computed")))
                .with_for_update()
            ).all()
            reminder_by_event = {
                (r.provider_account_id, r.calendar_id, r.event_id): r for r in reminders
            }

            for event in events:
                if event.calendar_id is None:
                    continue
                metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
                event_start = _event_start(metadata)
                location = _event_location(metadata)
                key = (event.provider_account_id, event.calendar_id, event.external_id)
                reminder = reminder_by_event.get(key)

                if event_start is None or location is None:
                    # The event lost its time or location, or is not a leave-by
                    # candidate; cancel any open reminder for it.
                    if reminder is not None:
                        reminder.version += 1
                        reminder.state = "cancelled"
                        reminder.next_check_at = None
                        reminder.updated_at = now
                    continue
                if event_start <= now or event_start > horizon_end:
                    if reminder is not None and event_start <= now:
                        reminder.version += 1
                        reminder.state = "cancelled"
                        reminder.next_check_at = None
                        reminder.updated_at = now
                    continue

                next_check_at = max(now, event_start - LEAVE_BY_INITIAL_LOOKAHEAD)
                if reminder is None:
                    reminder = LeaveByReminderRecord(
                        id=new_id_fn("lbr"),
                        provider_account_id=event.provider_account_id,
                        calendar_id=event.calendar_id,
                        event_id=event.external_id,
                        event_summary=metadata.get("summary")
                        if isinstance(metadata.get("summary"), str)
                        else None,
                        event_location=location,
                        event_start_at=event_start,
                        state="scheduled",
                        version=1,
                        next_check_at=next_check_at,
                        resolved_origin=None,
                        last_duration_seconds=None,
                        last_static_duration_seconds=None,
                        leave_by_at=None,
                        notification_id=None,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(reminder)
                    db.flush()
                    _add_evaluate_task(
                        db,
                        reminder_id=reminder.id,
                        version=reminder.version,
                        run_after=next_check_at,
                        compute_attempt=0,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                elif reminder.event_start_at != event_start or reminder.event_location != location:
                    # The backing event moved: bump the version so the in-flight
                    # evaluate task no-ops, then reschedule from scratch.
                    reminder.version += 1
                    reminder.state = "scheduled"
                    reminder.event_summary = (
                        metadata.get("summary")
                        if isinstance(metadata.get("summary"), str)
                        else None
                    )
                    reminder.event_location = location
                    reminder.event_start_at = event_start
                    reminder.next_check_at = next_check_at
                    reminder.updated_at = now
                    _add_evaluate_task(
                        db,
                        reminder_id=reminder.id,
                        version=reminder.version,
                        run_after=next_check_at,
                        compute_attempt=0,
                        now=now,
                        new_id_fn=new_id_fn,
                    )

            # An open reminder whose backing event row is gone is cancelled.
            live_event_keys = {
                (event.provider_account_id, event.calendar_id, event.external_id)
                for event in events
                if event.calendar_id is not None
            }
            for reminder in reminders:
                if reminder.state not in {"scheduled", "computed"}:
                    continue
                key = (reminder.provider_account_id, reminder.calendar_id, reminder.event_id)
                if key not in live_event_keys:
                    reminder.version += 1
                    reminder.state = "cancelled"
                    reminder.next_check_at = None
                    reminder.updated_at = now


def process_leave_by_evaluate_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    model_adapter: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    """The leave-by evaluate loop (spec B.4). Re-verifies the event, resolves the
    trip origin (B.5), computes traffic-aware travel via ``cap.maps.directions``,
    and either reschedules near departure (plan pass) or runs the leave-by
    subagent and notifies (notify pass, B.6)."""
    reminder_id = task_payload.get("reminder_id")
    version_raw = task_payload.get("version")
    if not isinstance(reminder_id, str) or not isinstance(version_raw, int):
        raise RuntimeError("leave_by_evaluate_due task payload invalid")
    compute_attempt_raw = task_payload.get("compute_attempt")
    compute_attempt = compute_attempt_raw if isinstance(compute_attempt_raw, int) else 0

    # Step 1-3: guard, re-verify the event, resolve the origin. This is read-only
    # plus a small terminal mutation; it commits before the external maps call.
    origin: str | None = None
    destination: str | None = None
    event_start_at: datetime | None = None
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            reminder = db.scalar(
                select(LeaveByReminderRecord)
                .where(LeaveByReminderRecord.id == reminder_id)
                .with_for_update()
                .limit(1)
            )
            if reminder is None:
                return
            if reminder.version != version_raw or reminder.state in _LEAVE_BY_TERMINAL_STATES:
                return

            event = db.scalar(
                select(GoogleProviderObjectRecord)
                .where(
                    GoogleProviderObjectRecord.provider_account_id == reminder.provider_account_id,
                    GoogleProviderObjectRecord.object_type == "calendar_event",
                    GoogleProviderObjectRecord.calendar_id == reminder.calendar_id,
                    GoogleProviderObjectRecord.external_id == reminder.event_id,
                )
                .limit(1)
            )
            metadata = (
                event.metadata_json
                if event is not None and isinstance(event.metadata_json, dict)
                else {}
            )
            verified_start = _event_start(metadata) if event is not None else None
            if (
                event is None
                or event.status != "active"
                or verified_start is None
                or verified_start <= now
            ):
                reminder.version += 1
                reminder.state = "cancelled"
                reminder.next_check_at = None
                reminder.updated_at = now
                return

            resolved_origin = _resolve_origin(
                db,
                provider_account_id=reminder.provider_account_id,
                event_id=reminder.event_id,
                event_start_at=verified_start,
                settings=settings,
            )
            if resolved_origin is None:
                reminder.version += 1
                reminder.state = "skipped"
                reminder.next_check_at = None
                reminder.updated_at = now
                return

            origin = resolved_origin
            destination = reminder.event_location
            event_start_at = verified_start

    # Step 4: compute travel via cap.maps.directions, outside any transaction.
    capability = get_capability("cap.maps.directions")
    if capability is None:
        raise RuntimeError("cap.maps.directions capability missing")
    normalized_input, input_error = capability.validate_input(
        {"origin": origin, "destination": destination, "travel_mode": "driving"}
    )
    if normalized_input is None or input_error is not None:
        raise RuntimeError(f"leave-by directions input invalid: {input_error}")
    result = execute_capability(capability=capability, normalized_input=normalized_input)

    maps_failure: str | None = None
    duration_seconds: int | None = None
    static_duration_seconds: int | None = None
    if result.status == "failed" or result.output is None:
        maps_failure = result.error or "maps_unavailable"
    else:
        routes = result.output.get("routes")
        if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
            maps_failure = "maps_location_not_found"
        else:
            duration_value = routes[0].get("duration_seconds")
            static_value = routes[0].get("static_duration_seconds")
            if not isinstance(duration_value, int):
                maps_failure = "maps_location_not_found"
            else:
                duration_seconds = duration_value
                static_duration_seconds = static_value if isinstance(static_value, int) else None

    # Step 5-6: persist the computation and branch on phase, in a fresh
    # transaction with the stale guard re-checked. The notify pass captures the
    # subagent context and exits the transaction — the model call must not run
    # inside a DB transaction (operation-types.md).
    subagent_context: dict[str, Any] | None = None
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            reminder = db.scalar(
                select(LeaveByReminderRecord)
                .where(LeaveByReminderRecord.id == reminder_id)
                .with_for_update()
                .limit(1)
            )
            if reminder is None:
                return
            if reminder.version != version_raw or reminder.state in _LEAVE_BY_TERMINAL_STATES:
                return

            if maps_failure is not None:
                transient = maps_failure in _LEAVE_BY_TRANSIENT_MAPS_FAILURES
                if transient and compute_attempt + 1 < LEAVE_BY_MAX_COMPUTE_ATTEMPTS:
                    # Reschedule a near retry within the bounded budget. The
                    # version is unchanged; the new run_after gives a distinct
                    # task idempotency key.
                    retry_at = now + timedelta(minutes=2)
                    reminder.next_check_at = retry_at
                    reminder.updated_at = now
                    _add_evaluate_task(
                        db,
                        reminder_id=reminder.id,
                        version=reminder.version,
                        run_after=retry_at,
                        compute_attempt=compute_attempt + 1,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    return
                reminder.version += 1
                reminder.state = "failed"
                reminder.next_check_at = None
                reminder.updated_at = now
                return

            assert duration_seconds is not None and origin is not None
            assert event_start_at is not None
            leave_by_at = (
                event_start_at - timedelta(seconds=duration_seconds) - LEAVE_BY_ARRIVAL_BUFFER
            )
            reminder.resolved_origin = origin
            reminder.last_duration_seconds = duration_seconds
            reminder.last_static_duration_seconds = static_duration_seconds
            reminder.leave_by_at = leave_by_at

            if now < leave_by_at - LEAVE_BY_NOTIFY_LEAD:
                # Plan pass: reschedule the loop to wake near departure.
                next_check_at = leave_by_at - LEAVE_BY_NOTIFY_LEAD
                reminder.version += 1
                reminder.state = "computed"
                reminder.next_check_at = next_check_at
                reminder.updated_at = now
                _add_evaluate_task(
                    db,
                    reminder_id=reminder.id,
                    version=reminder.version,
                    run_after=next_check_at,
                    compute_attempt=0,
                    now=now,
                    new_id_fn=new_id_fn,
                )
                return

            # Notify pass: gather the evidence the subagent judges on, then exit
            # the transaction before the model call.
            subagent_context = {
                "event_summary": reminder.event_summary,
                "event_location": reminder.event_location,
                "event_start_at": to_rfc3339(event_start_at),
                "resolved_origin": origin,
                "duration_seconds": duration_seconds,
                "static_duration_seconds": static_duration_seconds,
                "traffic_delta_seconds": (
                    duration_seconds - static_duration_seconds
                    if static_duration_seconds is not None
                    else None
                ),
                "leave_by_at": to_rfc3339(leave_by_at),
                "current_time": to_rfc3339(now),
            }

    if subagent_context is None:
        return

    # The leave-by subagent (B.6): a tools-free model call deciding notify vs
    # skip and authoring the message. Invalid model output fails closed.
    decision = _run_leave_by_evaluation(
        session_factory=session_factory,
        reminder_id=reminder_id,
        context=subagent_context,
        settings=settings,
        model_adapter=model_adapter,
        now_fn=now_fn,
        new_id_fn=new_id_fn,
    )

    with session_factory() as db:
        with db.begin():
            now = now_fn()
            reminder = db.scalar(
                select(LeaveByReminderRecord)
                .where(LeaveByReminderRecord.id == reminder_id)
                .with_for_update()
                .limit(1)
            )
            if reminder is None:
                return
            if reminder.version != version_raw or reminder.state in _LEAVE_BY_TERMINAL_STATES:
                return

            if decision is None:
                # Invalid or missing model output: fail closed, no notification
                # and no deterministic fallback message (ai-first.md).
                reminder.version += 1
                reminder.state = "failed"
                reminder.next_check_at = None
                reminder.updated_at = now
                return
            if decision.action == "skip":
                reminder.version += 1
                reminder.state = "skipped"
                reminder.next_check_at = None
                reminder.updated_at = now
                return

            assert reminder.leave_by_at is not None  # set in the compute transaction
            notification = NotificationRecord(
                id=new_id_fn("ntf"),
                dedupe_key=f"leave-by:{reminder.id}:{reminder.version}",
                source_type="leave_by",
                source_id=reminder.id,
                channel="discord",
                status="pending",
                title=f"Leave by {to_rfc3339(reminder.leave_by_at)}",
                body=decision.message,
                payload={
                    "reminder_id": reminder.id,
                    "version": reminder.version,
                    "urgency": decision.urgency,
                    "ai_judgment_id": decision.judgment_id,
                },
                created_at=now,
                updated_at=now,
                delivered_at=None,
                acked_at=None,
            )
            db.add(notification)
            db.flush()
            db.add(
                BackgroundTaskRecord(
                    id=new_id_fn("tsk"),
                    task_type="deliver_discord_notification",
                    idempotency_key=None,
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
            reminder.version += 1
            reminder.state = "notified"
            reminder.notification_id = notification.id
            reminder.next_check_at = None
            reminder.updated_at = now


def _resolve_origin(
    db: Session,
    *,
    provider_account_id: str,
    event_id: str,
    event_start_at: datetime,
    settings: AppSettings,
) -> str | None:
    """Origin resolution (spec B.5): the located preceding event within the
    max-origin gap, else ``ARIEL_HOME_ADDRESS``, else None (the trip is
    skipped)."""
    preceding = db.scalars(
        select(GoogleProviderObjectRecord).where(
            GoogleProviderObjectRecord.provider_account_id == provider_account_id,
            GoogleProviderObjectRecord.object_type == "calendar_event",
            GoogleProviderObjectRecord.status == "active",
            GoogleProviderObjectRecord.external_id != event_id,
        )
    ).all()
    best_end: datetime | None = None
    best_location: str | None = None
    for event in preceding:
        metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        location = _event_location(metadata)
        if location is None:
            continue
        end = metadata.get("end")
        if not isinstance(end, dict) or end.get("all_day") is True:
            continue
        end_value = end.get("value")
        if not isinstance(end_value, str) or not end_value.strip():
            continue
        try:
            end_at = datetime.fromisoformat(end_value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if end_at.tzinfo is None or end_at > event_start_at:
            continue
        if event_start_at - end_at > LEAVE_BY_MAX_ORIGIN_GAP:
            continue
        if best_end is None or end_at > best_end:
            best_end = end_at
            best_location = location
    if best_location is not None:
        return best_location
    return settings.home_address


@dataclass(frozen=True, slots=True)
class _LeaveByDecision:
    """The leave-by subagent's validated decision: notify-or-skip, urgency, the
    user-facing message, and the audit record id."""

    action: str
    urgency: str
    message: str
    judgment_id: str


def _run_leave_by_evaluation(
    *,
    session_factory: sessionmaker[Session],
    reminder_id: str,
    context: dict[str, Any],
    settings: AppSettings,
    model_adapter: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> _LeaveByDecision | None:
    """The leave-by subagent (spec B.6): a tools-free model call deciding notify
    vs skip and authoring the message. The deterministic evaluator already
    gathered the travel evidence; the model owns only the judgment and the
    wording (ai-first.md). Writes one ``ai_judgments`` row. Invalid or missing
    model output fails closed — returns None, no deterministic fallback."""
    model_input = [
        {
            "role": "system",
            "content": (
                "You decide whether Ariel should send a leave-by reminder for an "
                "upcoming calendar event. The travel evidence below is already "
                "computed; do not invent numbers. A trivial or already-past trip "
                "is a skip. When you notify, write the user-facing notification "
                "body: state the leave-by time, the drive time, and the traffic "
                "delta, and offer to add an approval-gated 'Leave for X' calendar "
                "hold the user can accept by replying. Return strict JSON only."
            ),
        },
        {
            "role": "system",
            "content": json.dumps(context, sort_keys=True, separators=(",", ":")),
        },
    ]
    input_refs = {"reminder_id": reminder_id, "context": context}

    def record_judgment(
        *,
        status: str,
        output: dict[str, Any],
        parse_status: str,
        validation_status: str,
        failure_code: str | None,
        failure_reason: str | None,
        response: dict[str, Any] | None,
    ) -> str:
        response_payload = response or {}
        judgment_id = new_id_fn("ajg")
        now = now_fn()
        model_raw = response_payload.get("model")
        model = (
            model_raw if isinstance(model_raw, str) and model_raw.strip() else settings.model_name
        )
        response_id = response_payload.get("provider_response_id")
        provider_response_id = (
            response_id if isinstance(response_id, str) and response_id.strip() else None
        )
        with session_factory() as db:
            with db.begin():
                db.add(
                    AIJudgmentRecord(
                        id=judgment_id,
                        judgment_type="leave_by_evaluation",
                        source_type="leave_by",
                        source_id=reminder_id,
                        status=status,
                        model=model,
                        prompt_version=LEAVE_BY_EVALUATION_PROMPT_VERSION,
                        provider_response_id=provider_response_id,
                        input_summary="leave-by notify-or-skip evaluation",
                        input_refs=input_refs,
                        selected=[],
                        omitted=[],
                        output=output,
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status=parse_status,
                        validation_status=validation_status,
                        failure_code=failure_code,
                        failure_reason=failure_reason,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return judgment_id

    try:
        response = _call_leave_by_model(
            model_input=model_input,
            settings=settings,
            model_adapter=model_adapter,
        )
    except (RuntimeError, httpx.HTTPError, ValueError) as exc:
        record_judgment(
            status="failed",
            output={},
            parse_status="missing_output",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_REQUIRED",
            failure_reason=safe_failure_reason(
                str(exc), fallback=f"unexpected {exc.__class__.__name__}"
            ),
            response=None,
        )
        return None

    try:
        decision_payload = _parse_leave_by_model_json(response)
    except (json.JSONDecodeError, RuntimeError) as exc:
        record_judgment(
            status="failed",
            output={"response_output": response.get("output")},
            parse_status="invalid_json"
            if isinstance(exc, json.JSONDecodeError)
            else "missing_output",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_INVALID_JSON"
            if isinstance(exc, json.JSONDecodeError)
            else "E_AI_JUDGMENT_SCHEMA",
            failure_reason=safe_failure_reason(
                str(exc), fallback="leave-by evaluation output missing"
            ),
            response=response,
        )
        return None

    decision = decision_payload.get("decision")
    urgency = decision_payload.get("urgency")
    message = decision_payload.get("message")
    if (
        set(decision_payload) != {"decision", "urgency", "message"}
        or decision not in _LEAVE_BY_DECISIONS
        or urgency not in _LEAVE_BY_URGENCIES
        or not isinstance(message, str)
        or not message.strip()
    ):
        record_judgment(
            status="failed",
            output=decision_payload,
            parse_status="schema_invalid",
            validation_status="invalid",
            failure_code="E_AI_JUDGMENT_SCHEMA",
            failure_reason="leave-by evaluation response failed schema validation",
            response=response,
        )
        return None

    judgment_id = record_judgment(
        status="succeeded",
        output=decision_payload,
        parse_status="parsed",
        validation_status="valid",
        failure_code=None,
        failure_reason=None,
        response=response,
    )
    return _LeaveByDecision(
        action=decision, urgency=urgency, message=message, judgment_id=judgment_id
    )


def _call_leave_by_model(
    *,
    model_input: list[dict[str, Any]],
    settings: AppSettings,
    model_adapter: Any | None,
) -> dict[str, Any]:
    """Issue the tools-free leave-by evaluation model call, via the injected
    adapter in tests or the OpenAI Responses API in production."""
    if model_adapter is not None:
        adapter_response: object = model_adapter.create_response(
            input_items=model_input,
            tools=[],
            user_message="",
            history=[],
            context_bundle={
                "origin": "leave_by_evaluation",
                "model_input": model_input,
                "response_json_schema": _LEAVE_BY_EVALUATION_JSON_SCHEMA,
            },
        )
        if not isinstance(adapter_response, dict):
            raise RuntimeError("model adapter returned a non-object response")
        return {str(key): value for key, value in adapter_response.items()}
    if settings.openai_api_key is None:
        raise RuntimeError("model credentials are not configured")
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={
            "authorization": f"Bearer {settings.openai_api_key}",
            "content-type": "application/json",
        },
        json={
            "model": settings.model_name,
            "input": model_input,
            "store": False,
            "reasoning": {"effort": settings.model_reasoning_effort},
            "text": {
                "verbosity": settings.model_verbosity,
                "format": {
                    "type": "json_schema",
                    "name": "leave_by_evaluation",
                    "strict": True,
                    "schema": _LEAVE_BY_EVALUATION_JSON_SCHEMA,
                },
            },
        },
        timeout=settings.model_timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"model provider returned HTTP {response.status_code}")
    payload = response.json()
    return {
        "output": payload.get("output"),
        "provider": "openai",
        "model": settings.model_name,
        "provider_response_id": payload.get("id"),
    }


def _parse_leave_by_model_json(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, list):
        raise RuntimeError("model response missing output")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if isinstance(text, str):
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
    raise RuntimeError("model response missing JSON decision")
