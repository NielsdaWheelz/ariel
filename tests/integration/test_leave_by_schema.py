from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from ariel.db import run_migrations
from ariel.persistence import LeaveByReminderRecord, NotificationRecord

_LEAVE_BY_REVISION = "20260518_0044"
_PRIOR_REVISION = "20260518_0043"


def _alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_leave_by_reminders_migration_runs_up_and_down(unmigrated_postgres_url: str) -> None:
    config = _alembic_config(unmigrated_postgres_url)
    run_migrations(unmigrated_postgres_url)

    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        assert inspect(engine).has_table("leave_by_reminders")

        command.downgrade(config, _PRIOR_REVISION)
        assert not inspect(engine).has_table("leave_by_reminders")

        command.upgrade(config, _LEAVE_BY_REVISION)
        assert inspect(engine).has_table("leave_by_reminders")
    finally:
        engine.dispose()


def test_leave_by_reminder_record_round_trips(session_factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    event_start = now + timedelta(hours=3)
    with session_factory() as db:
        with db.begin():
            db.add(
                NotificationRecord(
                    id="ntf_leave_by",
                    dedupe_key="leave-by:lbr_dentist:1",
                    source_type="leave_by",
                    source_id="lbr_dentist",
                    channel="discord",
                    status="pending",
                    title="Leave by 1:35 PM",
                    body="Leave by 1:35 PM for Dentist.",
                    payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                LeaveByReminderRecord(
                    id="lbr_dentist",
                    provider_account_id="google:user@example.com",
                    calendar_id="primary",
                    event_id="evt_dentist",
                    event_summary="Dentist — Dr. Okafor",
                    event_location="123 Main St",
                    event_start_at=event_start,
                    state="notified",
                    version=2,
                    next_check_at=None,
                    resolved_origin="456 Office Rd",
                    last_duration_seconds=1320,
                    last_static_duration_seconds=900,
                    leave_by_at=event_start - timedelta(minutes=27),
                    notification_id="ntf_leave_by",
                    created_at=now,
                    updated_at=now,
                )
            )

    with session_factory() as db:
        reminder = db.get(LeaveByReminderRecord, "lbr_dentist")
        assert reminder is not None
        assert reminder.provider_account_id == "google:user@example.com"
        assert reminder.calendar_id == "primary"
        assert reminder.event_id == "evt_dentist"
        assert reminder.event_summary == "Dentist — Dr. Okafor"
        assert reminder.event_location == "123 Main St"
        assert reminder.event_start_at == event_start
        assert reminder.state == "notified"
        assert reminder.version == 2
        assert reminder.next_check_at is None
        assert reminder.resolved_origin == "456 Office Rd"
        assert reminder.last_duration_seconds == 1320
        assert reminder.last_static_duration_seconds == 900
        assert reminder.leave_by_at == event_start - timedelta(minutes=27)
        assert reminder.notification_id == "ntf_leave_by"
