from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, create_app
from ariel.config import AppSettings
from ariel.worker import claim_next_task, enqueue_background_task, process_one_task, reap_stale_tasks


@dataclass
class DurableWorkflowAdapter:
    provider: str = "provider.discord-primary-durable"
    model: str = "model.discord-primary-durable-v1"

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history, context_bundle
        return {
            "assistant_text": f"assistant::{user_message}",
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
            "provider_response_id": "resp_discord_primary_durable_123",
        }


@dataclass(frozen=True)
class SignedAgencyBody:
    body: bytes
    headers: dict[str, str]


@dataclass
class FrozenClock:
    timestamp: int

    def __call__(self) -> float:
        return float(self.timestamp)


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _count_rows(client: TestClient, table_name: str) -> int:
    with _session_factory(client)() as db:
        with db.begin():
            result = db.execute(text(f"SELECT COUNT(*) AS count FROM {table_name}")).mappings().one()
            return int(result["count"])


def _signed_agency_body(payload: dict[str, Any], *, secret: str, timestamp: int) -> SignedAgencyBody:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return SignedAgencyBody(
        body=body,
        headers={
            "content-type": "application/json",
            "X-Ariel-Agency-Timestamp": str(timestamp),
            "X-Ariel-Agency-Signature": f"sha256={signature}",
        },
    )


def test_background_tasks_claim_retry_and_reap_are_durable_and_worker_safe(
    postgres_url: str,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        with _session_factory(client)() as db:
            with db.begin():
                first = enqueue_background_task(
                    db,
                    task_type="reap_stale_tasks",
                    payload={},
                    now=now,
                )
                enqueue_background_task(
                    db,
                    task_type="reap_stale_tasks",
                    payload={},
                    now=now + timedelta(minutes=20),
                )

            with db.begin():
                claimed = claim_next_task(db, worker_id="worker-a", now=now)
                assert claimed is not None
                assert claimed.id == first.id
                assert claimed.status == "running"
                assert claimed.attempts == 1
                assert claim_next_task(db, worker_id="worker-b", now=now) is None

            with db.begin():
                assert reap_stale_tasks(db, now=now + timedelta(minutes=10), heartbeat_timeout_seconds=60) == 1

            with db.begin():
                retried = claim_next_task(db, worker_id="worker-c", now=now + timedelta(minutes=10))
                assert retried is not None
                assert retried.id == first.id
                assert retried.status == "running"
                assert retried.attempts == 2

        assert _count_rows(client, "background_tasks") == 2


def test_agency_event_ingress_is_signed_idempotent_and_rejects_conflicts(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_000
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload = {
        "source": "agency.local",
        "event_id": "agency-event-001",
        "event_type": "job.completed",
        "external_job_id": "agency-job-001",
        "title": "Discord-primary cutover",
        "summary": "Implementation finished.",
        "payload": {"branch": "main"},
    }

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        first = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)
        assert first.status_code == 202
        assert first.json()["duplicate"] is False

        replay = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True

        changed = _signed_agency_body(
            {**payload, "summary": "Different payload."},
            secret=secret,
            timestamp=timestamp,
        )
        conflict = client.post("/v1/agency/events", content=changed.body, headers=changed.headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "E_AGENCY_EVENT_CONFLICT"

        assert _count_rows(client, "agency_events") == 1
        assert _count_rows(client, "background_tasks") == 1


def test_agency_event_worker_creates_job_event_and_discord_notification_once(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_100
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload = {
        "source": "agency.local",
        "event_id": "agency-event-002",
        "event_type": "job.completed",
        "external_job_id": "agency-job-002",
        "title": "Agency bridge",
        "summary": "Agency bridge finished.",
        "payload": {"artifact": "pr"},
    }

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        response = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)
        assert response.status_code == 202

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-a",
        ) is True
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-a",
        ) is True

        assert _count_rows(client, "jobs") == 1
        assert _count_rows(client, "job_events") == 1
        assert _count_rows(client, "notifications") == 1
        assert _count_rows(client, "notification_deliveries") == 0

        with _session_factory(client)() as db:
            with db.begin():
                job_id = str(db.execute(text("SELECT id FROM jobs")).scalar_one())

        job = client.get(f"/v1/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["job"]["status"] == "succeeded"

        events = client.get(f"/v1/jobs/{job_id}/events")
        assert events.status_code == 200
        assert [event["event_type"] for event in events.json()["events"]] == ["job.completed"]

        notifications = client.get("/v1/notifications")
        assert notifications.status_code == 200
        assert notifications.json()["notifications"][0]["title"] == "Agency completed: Agency bridge"
