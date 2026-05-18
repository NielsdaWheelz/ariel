"""Leave-by end-to-end acceptance coverage (maps-expansion-cutover PR 5).

Drives the whole Workstream B feature through its real worker-callable surface
— ``process_leave_by_scan_due`` then ``process_leave_by_evaluate_due`` — against
real postgres: a synced, located, timed calendar event becomes a notification
delivered to Discord. The per-layer behaviors (detection filters, origin
resolution, the notify/skip decision, dedupe, fail-closed) are owned by
``test_leave_by_detection_and_loop.py`` and
``test_leave_by_subagent_and_notification.py``; this file asserts only the
joined end-to-end story and the Workstream B acceptance criteria.
"""

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
    process_leave_by_scan_due,
)
from ariel.persistence import (
    AIJudgmentRecord,
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    LeaveByReminderRecord,
    NotificationRecord,
)

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
_GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
_id_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{next(_id_counter):026d}"


@pytest.fixture(autouse=True)
def _maps_key_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The maps wire layer reads the API key from the environment, not from the
    # AppSettings passed to the leave-by functions.
    monkeypatch.setenv("ARIEL_MAPS_API_KEY", "test-maps-key")


def _settings(
    *, maps_api_key: str | None = "test-maps-key", home_address: str | None = "Home Base, Seattle"
) -> AppSettings:
    return cast(Any, AppSettings)(
        _env_file=None,
        maps_api_key=maps_api_key,
        home_address=home_address,
    )


@dataclass(slots=True)
class _FakeHTTPResponse:
    status_code: int
    payload: Any = field(default_factory=dict)

    def json(self) -> Any:
        return self.payload


def _routes_response(*, duration: str = "1320s", static: str = "1080s") -> _FakeHTTPResponse:
    return _FakeHTTPResponse(
        status_code=200,
        payload={
            "routes": [
                {
                    "distanceMeters": 17200,
                    "duration": duration,
                    "staticDuration": static,
                    "description": "I-5 N",
                    "legs": [
                        {"distanceMeters": 17200, "duration": duration, "staticDuration": static}
                    ],
                }
            ]
        },
    )


def _install_maps(
    monkeypatch: pytest.MonkeyPatch, response: _FakeHTTPResponse
) -> list[dict[str, Any]]:
    """Route mocked ``httpx.request`` calls to the Routes API the maps wire layer
    targets, recording every call so the test can assert maps was consulted."""
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        assert "routes.googleapis.com" in url
        calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(capability_registry_module.httpx, "request", fake_request)
    return calls


class _LeaveByModelAdapter:
    """Tools-free model adapter stub returning a fixed leave-by evaluation
    payload, mirroring how the proactive tests mock the model adapter."""

    provider = "provider.test"
    model = "model.test"

    def __init__(self, *, decision: str, message: str, urgency: str = "normal") -> None:
        self._text = json.dumps({"decision": decision, "urgency": urgency, "message": message})
        self.calls = 0

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, user_message, history, context_bundle
        self.calls += 1
        assert tools == []  # the leave-by subagent is tools-free (spec B.6)
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_response_id": "resp_leave_by",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": self._text}]}
            ],
        }


_NOTIFY_MESSAGE = (
    "Leave by 1:35 PM for Dentist - Dr. Okafor (2:00 PM). 22-min drive from your "
    "last meeting; traffic is adding 4 min. Reply 'add a hold' and I'll block "
    "1:35-2:00 on your calendar."
)


def _seed_connector(db: Session, *, ready: bool = True) -> None:
    db.add(
        GoogleConnectorRecord(
            id="con_google",
            provider="google",
            status="connected" if ready else "not_connected",
            account_subject="acct-1",
            account_email="user@example.com",
            granted_scopes=[_GOOGLE_CALENDAR_READ_SCOPE] if ready else [],
            access_token_enc="enc-token" if ready else None,
            refresh_token_enc="enc-refresh",
            access_token_expires_at=NOW + timedelta(hours=1),
            token_obtained_at=NOW,
            encryption_key_version="v1",
            last_error_code=None,
            last_error_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _calendar_event(
    *,
    external_id: str,
    start: datetime,
    location: str,
    summary: str = "Dentist - Dr. Okafor",
    all_day: bool = False,
) -> GoogleProviderObjectRecord:
    def _slot(value: datetime) -> dict[str, Any]:
        if all_day:
            return {"value": value.date().isoformat(), "timezone": None, "all_day": True}
        return {
            "value": value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
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
            "summary": summary,
            "start": _slot(start),
            "end": _slot(start),
            "all_day": all_day,
            "location": location,
        },
        content_digest=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _scan(session_factory: sessionmaker[Session], **settings_kwargs: Any) -> None:
    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(**settings_kwargs),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )


def _evaluate(
    session_factory: sessionmaker[Session],
    *,
    reminder_id: str,
    version: int,
    now: datetime,
    model_adapter: Any | None,
) -> None:
    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": version, "compute_attempt": 0},
        settings=_settings(),
        model_adapter=model_adapter,
        now_fn=lambda: now,
        new_id_fn=_new_id,
    )


def _evaluate_task(db: Session, *, version: int) -> BackgroundTaskRecord:
    """The single leave_by_evaluate_due task enqueued for the given reminder
    version — the loop enqueues exactly one per version."""
    task = db.scalar(
        select(BackgroundTaskRecord).where(
            BackgroundTaskRecord.task_type == "leave_by_evaluate_due",
            BackgroundTaskRecord.payload["version"].as_integer() == version,
        )
    )
    assert task is not None
    return task


def test_located_event_flows_scan_to_loop_to_delivered_notification(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole feature, joined: a synced located timed event becomes a
    `scheduled` reminder, the plan pass reschedules it to `computed` within the
    notify lead, and the rescheduled near-departure pass runs the subagent and
    emits one delivered `leave_by` notification. The reminder traverses
    `scheduled -> computed -> notified` and the maps and model calls both fire.
    """
    maps_calls = _install_maps(monkeypatch, _routes_response(duration="1320s", static="1080s"))
    # Event 2h out: the first evaluate lands in the plan pass.
    event_start = NOW + timedelta(hours=2)
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-dentist",
                    start=event_start,
                    location="Dr. Okafor Dental, Seattle, WA",
                )
            )

    # 1. Detection: the scan opens one scheduled reminder and an evaluate task.
    _scan(session_factory)
    with session_factory() as db:
        reminder = db.scalar(select(LeaveByReminderRecord))
        assert reminder is not None
        reminder_id = reminder.id
        assert reminder.state == "scheduled"
        assert reminder.event_location == "Dr. Okafor Dental, Seattle, WA"
        assert _evaluate_task(db, version=1).payload == {
            "reminder_id": reminder_id,
            "version": 1,
            "scheduled_for": (event_start - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "compute_attempt": 0,
        }

    # 2. The evaluate loop, plan pass: computes a traffic-aware leave_by_at and
    #    reschedules itself once to within the notify lead of departure.
    leave_by_at = event_start - timedelta(seconds=1320) - LEAVE_BY_ARRIVAL_BUFFER
    _evaluate(session_factory, reminder_id=reminder_id, version=1, now=NOW, model_adapter=None)
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "computed"
        assert reminder.version == 2
        assert reminder.last_duration_seconds == 1320
        assert reminder.last_static_duration_seconds == 1080
        assert reminder.leave_by_at == leave_by_at
        assert reminder.next_check_at == leave_by_at - LEAVE_BY_NOTIFY_LEAD
        rescheduled = _evaluate_task(db, version=2)
        assert rescheduled.run_after == leave_by_at - LEAVE_BY_NOTIFY_LEAD
        # No notification yet — the plan pass only reschedules.
        assert db.scalars(select(NotificationRecord)).all() == []

    # 3. The rescheduled task fires near departure, notify pass: the subagent
    #    decides notify, a leave_by notification is written and queued.
    adapter = _LeaveByModelAdapter(decision="notify", urgency="high", message=_NOTIFY_MESSAGE)
    notify_now = leave_by_at - LEAVE_BY_NOTIFY_LEAD
    _evaluate(
        session_factory,
        reminder_id=reminder_id,
        version=2,
        now=notify_now,
        model_adapter=adapter,
    )

    assert adapter.calls == 1
    assert len(maps_calls) == 2  # one Routes call per evaluate pass

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "notified"
        assert reminder.next_check_at is None
        assert reminder.notification_id is not None

        notification = db.get(NotificationRecord, reminder.notification_id)
        assert notification is not None
        assert notification.source_type == "leave_by"
        assert notification.source_id == reminder_id
        assert notification.channel == "discord"
        assert notification.status == "pending"
        assert notification.body == _NOTIFY_MESSAGE
        assert notification.payload["urgency"] == "high"
        # The dedupe key is keyed on the reminder version at notify time.
        assert notification.dedupe_key == f"leave-by:{reminder_id}:{reminder.version - 1}"
        # A leave_by notification carries no proactive linkage.
        assert notification.proactive_case_id is None
        assert notification.proactive_decision_id is None

        # The notification is queued for Discord via the existing delivery task.
        delivery = db.scalar(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "deliver_discord_notification"
            )
        )
        assert delivery is not None
        assert delivery.payload["notification_id"] == notification.id

        # The whole loop wakes exactly twice and ends terminal — no further task.
        live_tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "leave_by_evaluate_due",
                BackgroundTaskRecord.run_after > notify_now,
            )
        ).all()
        assert live_tasks == []

        # The notify/skip judgment is one audited ai_judgments row.
        judgment = db.scalar(
            select(AIJudgmentRecord).where(AIJudgmentRecord.judgment_type == "leave_by_evaluation")
        )
        assert judgment is not None
        assert judgment.status == "succeeded"
        assert judgment.source_id == reminder_id


def test_scan_filters_to_located_timed_events_within_the_horizon(
    session_factory: sessionmaker[Session],
) -> None:
    """The detection filter: among a located timed in-horizon event, an all-day
    event, a location-less event, and an event past the 24h horizon, only the
    first produces a `leave_by_reminders` row."""
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-eligible",
                    start=NOW + timedelta(hours=6),
                    location="SEA Airport, Seattle, WA",
                )
            )
            db.add(
                _calendar_event(
                    external_id="evt-all-day",
                    start=NOW + timedelta(hours=6),
                    location="Conference Center",
                    all_day=True,
                )
            )
            db.add(
                _calendar_event(
                    external_id="evt-no-location",
                    start=NOW + timedelta(hours=6),
                    location="",
                )
            )
            db.add(
                _calendar_event(
                    external_id="evt-far-future",
                    start=NOW + timedelta(hours=30),
                    location="Far Place",
                )
            )

    _scan(session_factory)

    with session_factory() as db:
        reminders = db.scalars(select(LeaveByReminderRecord)).all()
        assert len(reminders) == 1
        assert reminders[0].event_id == "evt-eligible"


def test_scan_is_inert_when_maps_is_unconfigured(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `ARIEL_MAPS_API_KEY` unset the leave-by subsystem is inert by
    configuration: the scan creates no reminder, enqueues no evaluate task, and
    makes no maps call — there is no fallback branch (spec cutover policy)."""

    def fail_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        raise AssertionError("an unconfigured leave-by subsystem must not call maps")

    monkeypatch.setattr(capability_registry_module.httpx, "request", fail_request)
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-located",
                    start=NOW + timedelta(hours=6),
                    location="SEA Airport, Seattle, WA",
                )
            )

    _scan(session_factory, maps_api_key=None)

    with session_factory() as db:
        assert db.scalars(select(LeaveByReminderRecord)).all() == []
        assert (
            db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
                )
            ).all()
            == []
        )


def test_trivial_trip_yields_no_notification(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trivial trip is the subagent's judgment to make: on a `skip` decision
    the reminder ends `skipped`, with no notification and no delivery task."""
    _install_maps(monkeypatch, _routes_response(duration="240s", static="220s"))
    # Event 12 min out -> the evaluate loop lands directly in the notify pass.
    event_start = NOW + timedelta(minutes=12)
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-quick",
                    start=event_start,
                    location="Corner Cafe, Seattle, WA",
                )
            )

    _scan(session_factory)
    with session_factory() as db:
        reminder_id = db.scalars(select(LeaveByReminderRecord)).all()[0].id

    adapter = _LeaveByModelAdapter(
        decision="skip", message="A 4-minute drive is not worth a reminder."
    )
    _evaluate(session_factory, reminder_id=reminder_id, version=1, now=NOW, model_adapter=adapter)

    assert adapter.calls == 1
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "skipped"
        assert reminder.next_check_at is None
        assert reminder.notification_id is None
        assert db.scalars(select(NotificationRecord)).all() == []
        assert (
            db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "deliver_discord_notification"
                )
            ).all()
            == []
        )


def test_unrecoverable_maps_failure_fails_the_reminder_with_no_notification(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecoverable maps failure mid-evaluation is a typed terminal state:
    the reminder ends `failed` with no notification — never a degraded
    reminder (spec cutover policy, Workstream B acceptance criteria)."""
    # 403 -> provider_permission_denied: a non-transient maps failure.
    _install_maps(
        monkeypatch,
        _FakeHTTPResponse(status_code=403, payload={"error": {"status": "PERMISSION_DENIED"}}),
    )
    event_start = NOW + timedelta(minutes=12)
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-target",
                    start=event_start,
                    location="SEA Airport, Seattle, WA",
                )
            )

    _scan(session_factory)
    with session_factory() as db:
        reminder_id = db.scalars(select(LeaveByReminderRecord)).all()[0].id

    _evaluate(session_factory, reminder_id=reminder_id, version=1, now=NOW, model_adapter=None)

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "failed"
        assert reminder.next_check_at is None
        assert reminder.notification_id is None
        assert db.scalars(select(NotificationRecord)).all() == []
