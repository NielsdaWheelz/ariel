from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import ariel.capability_registry as capability_registry_module
from ariel.config import AppSettings
from ariel.leave_by import (
    LEAVE_BY_ARRIVAL_BUFFER,
    LEAVE_BY_MAX_COMPUTE_ATTEMPTS,
    LEAVE_BY_NOTIFY_LEAD,
    process_leave_by_evaluate_due,
    process_leave_by_scan_due,
)
from ariel.persistence import (
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    LeaveByReminderRecord,
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
    *, maps_api_key: str | None = "test-maps-key", home_address: str | None = None
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


def _install_maps(
    monkeypatch: pytest.MonkeyPatch, response: _FakeHTTPResponse
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(capability_registry_module.httpx, "request", fake_request)
    return calls


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
    start: datetime | None,
    end: datetime | None = None,
    location: str | None,
    summary: str = "Dentist",
    status: str = "active",
    all_day: bool = False,
    calendar_id: str = "primary",
    provider_account_id: str = "acct-1",
) -> GoogleProviderObjectRecord:
    def _slot(value: datetime | None) -> dict[str, Any]:
        if value is None:
            return {"value": None, "timezone": None, "all_day": all_day}
        if all_day:
            return {"value": value.date().isoformat(), "timezone": None, "all_day": True}
        return {
            "value": value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "timezone": "UTC",
            "all_day": False,
        }

    return GoogleProviderObjectRecord(
        id=_new_id("gpo"),
        provider_account_id=provider_account_id,
        object_type="calendar_event",
        external_id=external_id,
        thread_external_id=None,
        calendar_id=calendar_id,
        ical_uid=None,
        status=status,
        source_timestamp=NOW,
        observed_at=NOW,
        provider_url=None,
        metadata_json={
            "summary": summary,
            "start": _slot(start),
            "end": _slot(end if end is not None else start),
            "all_day": all_day,
            "location": location,
        },
        content_digest=None,
        created_at=NOW,
        updated_at=NOW,
    )


# --- Detection -------------------------------------------------------------


def test_scan_creates_one_reminder_for_a_located_timed_event_in_horizon(
    session_factory: sessionmaker[Session],
) -> None:
    event_start = NOW + timedelta(hours=6)
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-located",
                    start=event_start,
                    location="SEA Airport, Seattle, WA",
                )
            )

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminders = db.scalars(select(LeaveByReminderRecord)).all()
        assert len(reminders) == 1
        reminder = reminders[0]
        assert reminder.state == "scheduled"
        assert reminder.version == 1
        assert reminder.event_id == "evt-located"
        assert reminder.event_location == "SEA Airport, Seattle, WA"
        assert reminder.event_start_at == event_start
        # The loop wakes 2h before the event.
        assert reminder.next_check_at == event_start - timedelta(hours=2)

        task = db.scalar(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
            )
        )
        assert task is not None
        assert task.run_after == event_start - timedelta(hours=2)
        assert task.payload["reminder_id"] == reminder.id
        assert task.payload["version"] == 1


def test_scan_skips_all_day_locationless_and_out_of_horizon_events(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-all-day",
                    start=NOW + timedelta(hours=4),
                    location="Somewhere",
                    all_day=True,
                )
            )
            db.add(
                _calendar_event(
                    external_id="evt-no-location",
                    start=NOW + timedelta(hours=4),
                    location=None,
                )
            )
            db.add(
                _calendar_event(
                    external_id="evt-far-future",
                    start=NOW + timedelta(hours=30),
                    location="Far Place",
                )
            )

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

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


def test_scan_is_idempotent_and_does_not_duplicate_reminders(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-located",
                    start=NOW + timedelta(hours=6),
                    location="SEA Airport",
                )
            )

    for _ in range(3):
        process_leave_by_scan_due(
            session_factory=session_factory,
            settings=_settings(),
            now_fn=lambda: NOW,
            new_id_fn=_new_id,
        )

    with session_factory() as db:
        assert len(db.scalars(select(LeaveByReminderRecord)).all()) == 1
        assert (
            len(
                db.scalars(
                    select(BackgroundTaskRecord).where(
                        BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
                    )
                ).all()
            )
            == 1
        )


def test_scan_is_inert_without_maps_api_key(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-located",
                    start=NOW + timedelta(hours=6),
                    location="SEA Airport",
                )
            )

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(maps_api_key=None),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

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


def test_scan_is_inert_when_calendar_connector_not_ready(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_connector(db, ready=False)
            db.add(
                _calendar_event(
                    external_id="evt-located",
                    start=NOW + timedelta(hours=6),
                    location="SEA Airport",
                )
            )

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        assert db.scalars(select(LeaveByReminderRecord)).all() == []


def test_scan_reconciles_a_moved_event_by_bumping_version(
    session_factory: sessionmaker[Session],
) -> None:
    original_start = NOW + timedelta(hours=6)
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-moved", start=original_start, location="SEA Airport"
                )
            )

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    moved_start = NOW + timedelta(hours=9)
    with session_factory() as db:
        with db.begin():
            event = db.scalar(
                select(GoogleProviderObjectRecord).where(
                    GoogleProviderObjectRecord.external_id == "evt-moved"
                )
            )
            assert event is not None
            metadata = dict(event.metadata_json)
            metadata["start"] = {
                "value": moved_start.isoformat().replace("+00:00", "Z"),
                "timezone": "UTC",
                "all_day": False,
            }
            event.metadata_json = metadata

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminders = db.scalars(select(LeaveByReminderRecord)).all()
        assert len(reminders) == 1
        reminder = reminders[0]
        assert reminder.version == 2
        assert reminder.event_start_at == moved_start
        assert reminder.next_check_at == moved_start - timedelta(hours=2)
        # A v2 evaluate task is enqueued alongside the now-stale v1 task.
        versions = {
            task.payload["version"]
            for task in db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
                )
            ).all()
        }
        assert versions == {1, 2}


def test_scan_cancels_a_reminder_whose_event_was_cancelled(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_connector(db)
            db.add(
                _calendar_event(
                    external_id="evt-gone",
                    start=NOW + timedelta(hours=6),
                    location="SEA Airport",
                )
            )

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        with db.begin():
            event = db.scalar(
                select(GoogleProviderObjectRecord).where(
                    GoogleProviderObjectRecord.external_id == "evt-gone"
                )
            )
            assert event is not None
            event.status = "deleted"

    process_leave_by_scan_due(
        session_factory=session_factory,
        settings=_settings(),
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.scalar(select(LeaveByReminderRecord))
        assert reminder is not None
        assert reminder.state == "cancelled"
        assert reminder.next_check_at is None
        assert reminder.version == 2


# --- Origin resolution -----------------------------------------------------


def _scheduled_reminder(
    db: Session, *, event_id: str, event_start: datetime, location: str = "SEA Airport"
) -> LeaveByReminderRecord:
    reminder = LeaveByReminderRecord(
        id=_new_id("lbr"),
        provider_account_id="acct-1",
        calendar_id="primary",
        event_id=event_id,
        event_summary="Dentist",
        event_location=location,
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


def test_origin_resolves_to_preceding_event_location(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_maps(monkeypatch, _routes_response())
    event_start = NOW + timedelta(minutes=90)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            db.add(
                _calendar_event(
                    external_id="evt-prior",
                    start=NOW - timedelta(hours=1),
                    end=NOW + timedelta(minutes=5),
                    location="Downtown Office, Seattle",
                    summary="Standup",
                )
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        # The preceding located event wins over ARIEL_HOME_ADDRESS.
        assert reminder.resolved_origin == "Downtown Office, Seattle"
    routes_body = calls[0]["json"]
    assert routes_body["origin"] == {"address": "Downtown Office, Seattle"}
    assert routes_body["destination"] == {"address": "SEA Airport"}


def test_origin_falls_back_to_home_address_when_no_preceding_event(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_maps(monkeypatch, _routes_response())
    event_start = NOW + timedelta(minutes=90)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.resolved_origin == "Home Base, Seattle"
        assert reminder.state == "computed"


def test_origin_unresolvable_skips_the_reminder(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No maps call must occur when the origin cannot be resolved.
    def fail_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        raise AssertionError("maps must not be called when the origin is unresolvable")

    monkeypatch.setattr(capability_registry_module.httpx, "request", fail_request)
    event_start = NOW + timedelta(minutes=90)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address=None),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "skipped"
        assert reminder.next_check_at is None
        assert reminder.version == 2


def test_origin_ignores_preceding_event_outside_max_gap(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_maps(monkeypatch, _routes_response())
    event_start = NOW + timedelta(minutes=90)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            # Preceding event ends >3h before the target start: outside the gap.
            db.add(
                _calendar_event(
                    external_id="evt-stale-prior",
                    start=NOW - timedelta(hours=6),
                    end=NOW - timedelta(hours=5),
                    location="Old Office",
                    summary="Old meeting",
                )
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.resolved_origin == "Home Base, Seattle"


# --- Plan / notify phase split --------------------------------------------


def test_plan_pass_reschedules_to_within_notify_lead(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_maps(monkeypatch, _routes_response(duration="1320s", static="1080s"))
    # Event is 2h out: leave_by_at is well over the notify lead away -> plan pass.
    event_start = NOW + timedelta(hours=2)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "computed"
        assert reminder.version == 2
        assert reminder.last_duration_seconds == 1320
        assert reminder.last_static_duration_seconds == 1080
        leave_by_at = event_start - timedelta(seconds=1320) - LEAVE_BY_ARRIVAL_BUFFER
        assert reminder.leave_by_at == leave_by_at
        assert reminder.next_check_at == leave_by_at - LEAVE_BY_NOTIFY_LEAD

        # The loop reschedules itself to wake near departure at version 2.
        task = db.scalar(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
            )
        )
        assert task is not None
        assert task.payload["version"] == 2
        assert task.run_after == leave_by_at - LEAVE_BY_NOTIFY_LEAD


# --- Stale-task discard ----------------------------------------------------


def test_stale_version_task_no_ops(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        raise AssertionError("a stale evaluate task must not call maps")

    monkeypatch.setattr(capability_registry_module.httpx, "request", fail_request)
    event_start = NOW + timedelta(hours=2)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            seeded = _scheduled_reminder(db, event_id="evt-target", event_start=event_start)
            seeded.version = 3  # the row has advanced past the task's version
            reminder_id = seeded.id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        # Untouched: still scheduled at version 3, no compute persisted.
        assert reminder.state == "scheduled"
        assert reminder.version == 3
        assert reminder.resolved_origin is None


def test_terminal_state_task_no_ops(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(method: str, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        raise AssertionError("a terminal reminder must not call maps")

    monkeypatch.setattr(capability_registry_module.httpx, "request", fail_request)
    event_start = NOW + timedelta(hours=2)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            seeded = _scheduled_reminder(db, event_id="evt-target", event_start=event_start)
            seeded.state = "cancelled"
            reminder_id = seeded.id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "cancelled"
        assert reminder.version == 1


# --- cancelled / failed escapes -------------------------------------------


def test_evaluate_cancels_when_event_gone(
    session_factory: sessionmaker[Session],
) -> None:
    event_start = NOW + timedelta(hours=2)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            # No backing calendar_event row exists for this reminder.
            reminder_id = _scheduled_reminder(
                db, event_id="evt-missing", event_start=event_start
            ).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "cancelled"
        assert reminder.next_check_at is None
        assert reminder.version == 2


def test_evaluate_cancels_when_event_already_started(
    session_factory: sessionmaker[Session],
) -> None:
    started_at = NOW - timedelta(minutes=5)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-started", start=started_at, location="SEA Airport")
            )
            seeded = _scheduled_reminder(db, event_id="evt-started", event_start=started_at)
            reminder_id = seeded.id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "cancelled"


def test_persistent_maps_failure_marks_reminder_failed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 403 -> provider_permission_denied: a non-transient maps failure.
    _install_maps(
        monkeypatch,
        _FakeHTTPResponse(status_code=403, payload={"error": {"status": "PERMISSION_DENIED"}}),
    )
    event_start = NOW + timedelta(hours=2)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "failed"
        assert reminder.next_check_at is None
        assert reminder.version == 2
        assert (
            db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
                )
            ).all()
            == []
        )


def test_transient_maps_failure_retries_then_fails_when_budget_exhausted(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 503 -> provider_upstream_failure on every attempt: a transient failure.
    _install_maps(
        monkeypatch,
        _FakeHTTPResponse(status_code=503, payload={"error": {"status": "UNAVAILABLE"}}),
    )
    event_start = NOW + timedelta(hours=2)
    reminder_id: str
    with session_factory() as db:
        with db.begin():
            db.add(
                _calendar_event(external_id="evt-target", start=event_start, location="SEA Airport")
            )
            reminder_id = _scheduled_reminder(db, event_id="evt-target", event_start=event_start).id

    # First attempt: under budget -> a near retry is enqueued, reminder stays scheduled.
    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={"reminder_id": reminder_id, "version": 1, "compute_attempt": 0},
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "scheduled"
        assert reminder.version == 1
        retry_task = db.scalar(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "leave_by_evaluate_due"
            )
        )
        assert retry_task is not None
        assert retry_task.payload["compute_attempt"] == 1
        assert retry_task.run_after == NOW + timedelta(minutes=2)

    # Final attempt at the budget edge -> the reminder fails closed.
    process_leave_by_evaluate_due(
        session_factory=session_factory,
        task_payload={
            "reminder_id": reminder_id,
            "version": 1,
            "compute_attempt": LEAVE_BY_MAX_COMPUTE_ATTEMPTS - 1,
        },
        settings=_settings(home_address="Home Base, Seattle"),
        model_adapter=None,
        now_fn=lambda: NOW,
        new_id_fn=_new_id,
    )
    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, reminder_id)
        assert reminder is not None
        assert reminder.state == "failed"
        assert reminder.next_check_at is None
