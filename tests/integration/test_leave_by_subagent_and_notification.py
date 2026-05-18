from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import ariel.capability_registry as capability_registry_module
from ariel.config import AppSettings
from ariel.leave_by import (
    LEAVE_BY_ARRIVAL_BUFFER,
    LEAVE_BY_NOTIFY_LEAD,
    process_leave_by_evaluate_due,
)
from ariel.persistence import (
    AIJudgmentRecord,
    BackgroundTaskRecord,
    GoogleProviderObjectRecord,
    LeaveByReminderRecord,
    NotificationRecord,
)

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
_id_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{next(_id_counter):026d}"


@pytest.fixture(autouse=True)
def _maps_key_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MAPS_API_KEY", "test-maps-key")


def _settings(*, home_address: str | None = "Home Base, Seattle") -> AppSettings:
    return cast(Any, AppSettings)(
        _env_file=None,
        maps_api_key="test-maps-key",
        home_address=home_address,
    )


@dataclass(slots=True)
class _FakeHTTPResponse:
    status_code: int
    payload: Any = field(default_factory=dict)

    def json(self) -> Any:
        return self.payload


def _routes_response(*, duration: str = "1320s", static: str | None = "1080s") -> _FakeHTTPResponse:
    route: dict[str, Any] = {
        "distanceMeters": 17200,
        "duration": duration,
        "description": "I-5 N",
        "legs": [{"distanceMeters": 17200, "duration": duration}],
    }
    if static is not None:
        route["staticDuration"] = static
        route["legs"][0]["staticDuration"] = static
    return _FakeHTTPResponse(status_code=200, payload={"routes": [route]})


def _install_maps(monkeypatch: pytest.MonkeyPatch, response: _FakeHTTPResponse) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        return response

    monkeypatch.setattr(capability_registry_module.httpx, "request", fake_request)


class _LeaveByModelAdapter:
    """A tools-free model adapter stub returning a fixed leave-by evaluation
    payload, mirroring how the proactive tests mock the model adapter."""

    provider = "provider.test"
    model = "model.test"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.last_context: dict[str, Any] | None = None

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, history
        self.calls += 1
        assert tools == []  # the leave-by subagent is tools-free (spec B.6)
        # The deterministic evaluator's evidence is the second system item.
        self.last_context = json.loads(input_items[1]["content"])
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_response_id": "resp_leave_by",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": self.text}]}
            ],
        }


def _decision_adapter(
    *,
    decision: str = "notify",
    urgency: str = "normal",
    message: str = (
        "Leave by 1:35 PM for Dentist (2:00 PM). 22-min drive; traffic is adding 4 min. "
        "Reply 'add a hold' and I'll block 1:35-2:00 on your calendar."
    ),
) -> _LeaveByModelAdapter:
    return _LeaveByModelAdapter(
        json.dumps({"decision": decision, "urgency": urgency, "message": message})
    )


def _calendar_event(
    *, external_id: str, start: datetime, location: str = "SEA Airport, Seattle, WA"
) -> GoogleProviderObjectRecord:
    slot = {
        "value": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "timezone": "UTC",
        "all_day": False,
    }
    return GoogleProviderObjectRecord(
        id=_new_id("gpo"),
        provider_account_id="acct-1",
        object_type="calendar_event",
        external_id=external_id,
        thread_external_id=None,
        calendar_id="primary",
        ical_uid=None,
        status="active",
        source_timestamp=NOW,
        observed_at=NOW,
        provider_url=None,
        metadata_json={
            "summary": "Dentist",
            "start": slot,
            "end": slot,
            "all_day": False,
            "location": location,
        },
        content_digest=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _scheduled_reminder(
    db: Session, *, event_id: str, event_start: datetime
) -> LeaveByReminderRecord:
    reminder = LeaveByReminderRecord(
        id=_new_id("lbr"),
        provider_account_id="acct-1",
        calendar_id="primary",
        event_id=event_id,
        event_summary="Dentist",
        event_location="SEA Airport, Seattle, WA",
        event_start_at=event_start,
        state="scheduled",
        version=1,
        next_check_at=event_start - timedelta(hours=2),
        resolved_origin=None,
        last_duration_seconds=None,
        last_static_duration_seconds=None,
        leave_by_at=None,
        notification_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(reminder)
    return reminder


def _seed_notify_pass_reminder(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> str:
    """Seed a reminder whose event is close enough that the evaluate loop lands
    in the notify pass (event 12 min out -> past leave_by_at - notify lead)."""
    _install_maps(monkeypatch, _routes_response(duration="600s", static="540s"))
    event_start = NOW + timedelta(minutes=12)
    with session_factory() as db:
        with db.begin():
            db.add(_calendar_event(external_id="evt-target", start=event_start))
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id
    return reminder_id


# --- Notify / skip decision ------------------------------------------------


def test_notify_decision_writes_notification_and_marks_notified(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_id = _seed_notify_pass_reminder(session_factory, monkeypatch)
    adapter = _decision_adapter(decision="notify", urgency="high")

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    assert adapter.calls == 1
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "notified"
        assert reminder.next_check_at is None
        assert reminder.notification_id is not None

        notification = db.get(NotificationRecord, reminder.notification_id)
        assert notification is not None
        assert notification.body.startswith("Leave by 1:35 PM for Dentist")
        assert notification.payload["urgency"] == "high"


def test_skip_decision_marks_reminder_skipped_with_no_notification(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_id = _seed_notify_pass_reminder(session_factory, monkeypatch)
    adapter = _decision_adapter(decision="skip")

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    assert adapter.calls == 1
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "skipped"
        assert reminder.next_check_at is None
        assert reminder.notification_id is None
        # A skip is still an audited judgment.
        judgment = db.scalar(
            select(AIJudgmentRecord).where(AIJudgmentRecord.judgment_type == "leave_by_evaluation")
        )
        assert judgment is not None
        assert judgment.status == "succeeded"
        assert db.scalars(select(NotificationRecord)).all() == []


def test_subagent_context_carries_the_deterministic_travel_evidence(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_id = _seed_notify_pass_reminder(session_factory, monkeypatch)
    adapter = _decision_adapter()

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    # The evaluator gathers the evidence; the model judges on it (ai-first.md).
    context = adapter.last_context
    assert context is not None
    assert context["duration_seconds"] == 600
    assert context["static_duration_seconds"] == 540
    assert context["traffic_delta_seconds"] == 60
    assert context["resolved_origin"] == "Home Base, Seattle"
    assert context["event_location"] == "SEA Airport, Seattle, WA"
    assert context["event_timezone"] == "UTC"
    assert "leave_by_at" in context
    assert "current_time" in context


# --- Notification contract and dedupe -------------------------------------


def test_notification_contract_and_dedupe_key(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_id = _seed_notify_pass_reminder(session_factory, monkeypatch)

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=_decision_adapter(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        notification = db.scalar(select(NotificationRecord))
        assert notification is not None
        assert notification.source_type == "leave_by"
        assert notification.source_id == reminder_id
        assert notification.channel == "discord"
        assert notification.status == "pending"
        assert notification.title == "Leave-by reminder: Dentist"
        # The version at notify time is one below the final bumped version.
        assert notification.dedupe_key == f"leave-by:{reminder_id}:{reminder.version - 1}"
        assert notification.proactive_case_id is None
        assert notification.proactive_decision_id is None

        # The notification is queued for Discord delivery via the existing task.
        delivery_task = db.scalar(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "deliver_discord_notification"
            )
        )
        assert delivery_task is not None
        assert delivery_task.payload["notification_id"] == notification.id


# --- Fail-closed on invalid model output ----------------------------------


@pytest.mark.parametrize(
    "bad_text",
    [
        "this is not json",
        json.dumps({"decision": "notify", "urgency": "normal"}),  # missing message
        json.dumps({"decision": "maybe", "urgency": "normal", "message": "x"}),  # bad enum
        json.dumps({"decision": "notify", "urgency": "loud", "message": "x"}),  # bad urgency
        json.dumps({"decision": "notify", "urgency": "normal", "message": ""}),  # empty message
        json.dumps(
            {"decision": "notify", "urgency": "normal", "message": "x", "extra": 1}
        ),  # extra key
    ],
)
def test_invalid_model_output_fails_closed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    bad_text: str,
) -> None:
    reminder_id = _seed_notify_pass_reminder(session_factory, monkeypatch)

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=_LeaveByModelAdapter(bad_text),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        # Fail closed: no deterministic fallback message (ai-first.md).
        assert reminder.state == "failed"
        assert reminder.next_check_at is None
        assert reminder.notification_id is None
        assert db.scalars(select(NotificationRecord)).all() == []
        # The failure is auditable.
        judgment = db.scalar(
            select(AIJudgmentRecord).where(AIJudgmentRecord.judgment_type == "leave_by_evaluation")
        )
        assert judgment is not None
        assert judgment.status == "failed"
        assert judgment.failure_code is not None


def test_missing_model_credentials_fails_closed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No model adapter and no openai_api_key: the model call cannot be made.
    reminder_id = _seed_notify_pass_reminder(session_factory, monkeypatch)

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "failed"
        assert db.scalars(select(NotificationRecord)).all() == []


# --- End-to-end: scheduled -> computed -> notified -------------------------


def test_loop_cycles_from_scheduled_through_computed_to_notified(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the loop through the plan pass, fire the rescheduled task, and
    confirm it reaches the notify pass and notifies. This is the sequence that
    proves `computed` is non-terminal (PR4)."""
    _install_maps(monkeypatch, _routes_response(duration="1320s", static="1080s"))
    # Event 2h out: the first evaluate lands in the plan pass.
    event_start = NOW + timedelta(hours=2)
    with session_factory() as db:
        with db.begin():
            db.add(_calendar_event(external_id="evt-target", start=event_start))
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    # Plan pass: at NOW the loop reschedules itself, state -> computed.
    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    leave_by_at = event_start - timedelta(seconds=1320) - LEAVE_BY_ARRIVAL_BUFFER
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "computed"
        assert reminder.version == 2
        # The plan pass enqueued the next evaluate task at version 2.
        rescheduled = db.scalar(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
            )
        )
        assert rescheduled is not None
        assert rescheduled.payload["version"] == 2
        assert rescheduled.run_after == leave_by_at - LEAVE_BY_NOTIFY_LEAD

    # Fire the rescheduled task at its run_after: now within the notify lead.
    notify_now = leave_by_at - LEAVE_BY_NOTIFY_LEAD
    adapter = _decision_adapter(decision="notify")
    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 2, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=adapter,
        now_fn=lambda: notify_now,
        new_id_fn=_new_id,
    )

    assert adapter.calls == 1
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        # The rescheduled task reached the notify pass and notified.
        assert reminder.state == "notified"
        assert reminder.notification_id is not None
        notification = db.get(NotificationRecord, reminder.notification_id)
        assert notification is not None
        assert notification.source_type == "leave_by"
