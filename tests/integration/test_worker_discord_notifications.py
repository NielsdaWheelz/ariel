from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests
from ariel.persistence import JobRecord, NotificationRecord
from ariel.worker import enqueue_background_task, process_one_task


@dataclass
class FakeDiscordResponse:
    status_code: int
    body: dict[str, Any]

    @property
    def text(self) -> str:
        return json.dumps(self.body)

    def json(self) -> dict[str, Any]:
        return self.body


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def session_factory(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    try:
        yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    finally:
        engine.dispose()


def _discord_notification_settings() -> AppSettings:
    return cast(Any, AppSettings)(
        _env_file=None,
        discord_bot_token="discord-token",
        discord_channel_id=333,
        discord_notification_timeout_seconds=1.0,
    )


def _seed_notification_task(
    session_factory: sessionmaker[Session],
    *,
    job_id: str,
    notification_id: str,
    title: str,
    body: str,
    discord_thread_id: str | None,
) -> None:
    now = datetime(2026, 4, 27, 12, 30, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            job = JobRecord(
                id=job_id,
                source="agency.local",
                external_job_id=f"agency-{job_id}",
                title=title,
                status="waiting_approval",
                summary=body,
                latest_payload={},
                discord_thread_id=discord_thread_id,
                created_at=now,
                updated_at=now,
            )
            notification = NotificationRecord(
                id=notification_id,
                dedupe_key=f"agency-event:{notification_id}",
                source_type="agency_event",
                source_id=f"evt_{notification_id}",
                channel="discord",
                status="pending",
                title=f"Agency waiting: {title}",
                body=body,
                payload={"job_id": job.id, "agency_event_id": f"evt_{notification_id}"},
                created_at=now,
                updated_at=now,
            )
            db.add(job)
            db.add(notification)
            enqueue_background_task(
                db,
                task_type="deliver_discord_notification",
                payload={"notification_id": notification.id},
                now=now,
                max_attempts=5,
            )


def test_discord_notification_delivery_creates_thread_and_posts_message_to_it(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_discord_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeDiscordResponse:
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if url == "https://discord.com/api/v10/channels/333/threads":
            return FakeDiscordResponse(status_code=201, body={"id": "thread_123"})
        if url == "https://discord.com/api/v10/channels/thread_123/messages":
            return FakeDiscordResponse(status_code=200, body={"id": "message_123"})
        raise AssertionError(f"unexpected Discord URL: {url}")

    monkeypatch.setattr("ariel.worker.httpx.post", fake_discord_post)
    _seed_notification_task(
        session_factory,
        job_id="job_threaded",
        notification_id="ntf_threaded",
        title="Threaded job",
        body="Thread notification finished.",
        discord_thread_id=None,
    )

    assert process_one_task(
        session_factory=session_factory,
        settings=_discord_notification_settings(),
        worker_id="worker-a",
    ) is True

    assert [call["url"] for call in calls] == [
        "https://discord.com/api/v10/channels/333/threads",
        "https://discord.com/api/v10/channels/thread_123/messages",
    ]
    assert calls[0]["json"]["name"] == "Threaded job"
    assert calls[1]["json"]["content"] == (
        "**Agency waiting: Threaded job**\nThread notification finished."
    )

    with session_factory() as db:
        with db.begin():
            row = db.execute(
                text(
                    "SELECT j.discord_thread_id, n.status, d.status AS delivery_status "
                    "FROM jobs j "
                    "JOIN notifications n ON n.payload->>'job_id' = j.id "
                    "JOIN notification_deliveries d ON d.notification_id = n.id "
                    "WHERE n.id = :notification_id"
                ),
                {"notification_id": "ntf_threaded"},
            ).mappings().one()

    assert row["discord_thread_id"] == "thread_123"
    assert row["status"] == "delivered"
    assert row["delivery_status"] == "succeeded"


def test_discord_notification_delivery_reuses_existing_job_thread(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_discord_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeDiscordResponse:
        del headers, timeout
        calls.append({"url": url, "json": json})
        if url.endswith("/threads"):
            raise AssertionError("existing job thread should be reused")
        if url == "https://discord.com/api/v10/channels/thread_existing/messages":
            return FakeDiscordResponse(status_code=200, body={"id": "message_existing"})
        raise AssertionError(f"unexpected Discord URL: {url}")

    monkeypatch.setattr("ariel.worker.httpx.post", fake_discord_post)
    _seed_notification_task(
        session_factory,
        job_id="job_existing",
        notification_id="ntf_existing",
        title="Existing thread job",
        body="Needs approval.",
        discord_thread_id="thread_existing",
    )

    assert process_one_task(
        session_factory=session_factory,
        settings=_discord_notification_settings(),
        worker_id="worker-a",
    ) is True

    assert [call["url"] for call in calls] == [
        "https://discord.com/api/v10/channels/thread_existing/messages"
    ]
    assert calls[0]["json"]["content"] == (
        "**Agency waiting: Existing thread job**\nNeeds approval."
    )

    with session_factory() as db:
        with db.begin():
            row = db.execute(
                text(
                    "SELECT n.status, d.status AS delivery_status "
                    "FROM notifications n "
                    "JOIN notification_deliveries d ON d.notification_id = n.id "
                    "WHERE n.id = :notification_id"
                ),
                {"notification_id": "ntf_existing"},
            ).mappings().one()

    assert row["status"] == "delivered"
    assert row["delivery_status"] == "succeeded"


def test_discord_notification_delivery_persists_created_thread_when_message_fails(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_discord_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeDiscordResponse:
        del headers, json, timeout
        calls.append(url)
        if url == "https://discord.com/api/v10/channels/333/threads":
            return FakeDiscordResponse(status_code=201, body={"id": "thread_retry"})
        if url == "https://discord.com/api/v10/channels/thread_retry/messages":
            return FakeDiscordResponse(status_code=500, body={"message": "temporary failure"})
        raise AssertionError(f"unexpected Discord URL: {url}")

    monkeypatch.setattr("ariel.worker.httpx.post", fake_discord_post)
    _seed_notification_task(
        session_factory,
        job_id="job_retry",
        notification_id="ntf_retry",
        title="Retry job",
        body="Message post should retry.",
        discord_thread_id=None,
    )

    assert process_one_task(
        session_factory=session_factory,
        settings=_discord_notification_settings(),
        worker_id="worker-a",
    ) is True

    assert calls == [
        "https://discord.com/api/v10/channels/333/threads",
        "https://discord.com/api/v10/channels/thread_retry/messages",
    ]
    with session_factory() as db:
        with db.begin():
            row = db.execute(
                text(
                    "SELECT j.discord_thread_id, n.status, d.status AS delivery_status, "
                    "d.error, t.status AS task_status "
                    "FROM jobs j "
                    "JOIN notifications n ON n.payload->>'job_id' = j.id "
                    "JOIN notification_deliveries d ON d.notification_id = n.id "
                    "JOIN background_tasks t ON t.payload->>'notification_id' = n.id "
                    "WHERE n.id = :notification_id"
                ),
                {"notification_id": "ntf_retry"},
            ).mappings().one()

    assert row["discord_thread_id"] == "thread_retry"
    assert row["status"] == "failed"
    assert row["delivery_status"] == "failed"
    assert row["error"] == "Discord returned HTTP 500"
    assert row["task_status"] == "pending"
