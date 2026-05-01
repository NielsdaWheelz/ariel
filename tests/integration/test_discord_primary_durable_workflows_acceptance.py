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
from tests.integration.responses_helpers import responses_message
from ariel.config import AppSettings
from ariel.persistence import JobRecord
from ariel.worker import (
    claim_next_task,
    enqueue_background_task,
    process_one_task,
    reap_stale_tasks,
)


@dataclass
class DurableWorkflowAdapter:
    provider: str = "provider.discord-primary-durable"
    model: str = "model.discord-primary-durable-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history, context_bundle
        return responses_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_discord_primary_durable_123",
            input_tokens=8,
            output_tokens=5,
        )


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
            result = (
                db.execute(text(f"SELECT COUNT(*) AS count FROM {table_name}")).mappings().one()
            )
            return int(result["count"])


def _signed_agency_body(
    payload: dict[str, Any], *, secret: str, timestamp: int
) -> SignedAgencyBody:
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


def _seed_job(client: TestClient, *, job_id: str, status: str, now: datetime) -> None:
    with _session_factory(client)() as db:
        with db.begin():
            db.add(
                JobRecord(
                    id=job_id,
                    source="agency.local",
                    external_job_id=f"external-{job_id}",
                    title="Proactive cutover",
                    status=status,
                    summary="Proactive worker should notice this job.",
                    latest_payload={"status": status},
                    created_at=now - timedelta(minutes=5),
                    updated_at=now,
                )
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
                assert (
                    reap_stale_tasks(
                        db, now=now + timedelta(minutes=10), heartbeat_timeout_seconds=60
                    )
                    == 1
                )

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
        assert (
            process_one_task(
                session_factory=_session_factory(client),
                settings=settings,
                worker_id="worker-a",
            )
            is True
        )
        assert (
            process_one_task(
                session_factory=_session_factory(client),
                settings=settings,
                worker_id="worker-a",
            )
            is True
        )

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
        assert (
            notifications.json()["notifications"][0]["title"] == "Agency completed: Agency bridge"
        )


def test_google_provider_event_ingress_is_token_bound_deduped_and_conflict_safe(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN", "provider-token")
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        headers = {
            "X-Goog-Channel-Token": "provider-token",
            "X-Goog-Channel-ID": "channel-1",
            "X-Goog-Message-Number": "42",
            "X-Goog-Resource-State": "exists",
            "content-type": "application/json",
        }
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers=headers,
            content=b'{"changed":["events"]}',
        )
        assert response.status_code == 202
        assert response.json()["duplicate"] is False

        duplicate = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers=headers,
            content=b'{"changed":["events"]}',
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["duplicate"] is True

        conflict = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers=headers,
            content=b'{"changed":["different"]}',
        )
        assert conflict.status_code == 409

        listed = client.get("/v1/provider-events")
        assert listed.status_code == 200
        assert len(listed.json()["events"]) == 1

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-provider",
        )
        with _session_factory(client)() as db:
            with db.begin():
                task_type = db.execute(
                    text(
                        "SELECT task_type FROM background_tasks "
                        "WHERE status = 'pending' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert task_type == "provider_sync_due"


def test_google_calendar_sync_creates_workspace_state_and_attention_signal(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGoogleWorkspaceProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            return {
                "nextSyncToken": "sync-token-2",
                "items": [
                    {
                        "id": "event-1",
                        "summary": "Design review",
                        "status": "confirmed",
                        "updated": "2026-04-30T12:00:00Z",
                        "htmlLink": "https://calendar.google.com/event?eid=event-1",
                    }
                ],
            }

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeGoogleWorkspaceProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeGoogleWorkspaceProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        with _session_factory(client)() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="provider_sync_due",
                    payload={
                        "provider": "google",
                        "resource_type": "calendar",
                        "resource_id": "primary",
                    },
                    now=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
                )

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-sync",
        )

        sync_runs = client.get("/v1/sync-runs")
        assert sync_runs.status_code == 200
        assert sync_runs.json()["sync_runs"][0]["status"] == "succeeded"
        assert sync_runs.json()["sync_runs"][0]["item_count"] == 1
        assert sync_runs.json()["sync_runs"][0]["signal_count"] == 1

        workspace_items = client.get("/v1/workspace-items")
        assert workspace_items.status_code == 200
        workspace_item = workspace_items.json()["workspace_items"][0]
        assert workspace_item["item_type"] == "calendar_event"
        assert workspace_item["external_id"] == "event-1"
        assert workspace_item["metadata"]["resource_id"] == "primary"

        signals = client.get("/v1/attention-signals", params={"status": "new"})
        assert signals.status_code == 200
        signal = signals.json()["attention_signals"][0]
        assert signal["source_type"] == "workspace_item"
        assert signal["workspace_item_id"] == workspace_item["id"]


def test_proactive_open_job_check_creates_attention_notification_and_acknowledges_both(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.app._utcnow", lambda: now)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_job(client, job_id="job_proactive_ack", status="waiting_approval", now=now)

        derive = client.post("/v1/attention-signals/derive")
        assert derive.status_code == 200

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
        )
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
        )

        signals = client.get("/v1/attention-signals", params={"status": "reviewed"})
        assert signals.status_code == 200
        signal = signals.json()["attention_signals"][0]
        assert signal["source_type"] == "job"
        assert signal["evidence"]["job_id"] == "job_proactive_ack"

        attention = client.get("/v1/attention-items", params={"status": "notified"})
        assert attention.status_code == 200
        attention_item = attention.json()["attention_items"][0]
        assert "subscription_id" not in attention_item
        assert attention_item["source_type"] == "attention_signal"
        assert attention_item["source_signal_ids"] == [signal["id"]]
        assert attention_item["priority"] == "high"
        assert attention_item["evidence"]["signal_evidence"]["job_id"] == "job_proactive_ack"

        notifications = client.get("/v1/notifications")
        assert notifications.status_code == 200
        notification = notifications.json()["notifications"][0]
        assert notification["source_type"] == "attention_item"
        assert notification["payload"]["attention_item_id"] == attention_item["id"]

        acked = client.post(f"/v1/notifications/{notification['id']}/ack")
        assert acked.status_code == 200

        item_after_ack = client.get(f"/v1/attention-items/{attention_item['id']}")
        assert item_after_ack.status_code == 200
        assert item_after_ack.json()["attention_item"]["status"] == "acknowledged"

        events = client.get(f"/v1/attention-items/{attention_item['id']}/events")
        assert events.status_code == 200
        assert [event["event_type"] for event in events.json()["events"]] == [
            "detected",
            "notified",
            "acknowledged",
        ]


def test_attention_item_snooze_schedules_durable_follow_up(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 30, 13, 0, tzinfo=UTC)
    snooze_until = now + timedelta(days=1)
    monkeypatch.setattr("ariel.app._utcnow", lambda: now)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_job(client, job_id="job_proactive_snooze", status="waiting_approval", now=now)
        derive = client.post("/v1/attention-signals/derive")
        assert derive.status_code == 200

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
        )
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
        )

        attention_item_id = client.get("/v1/attention-items", params={"status": "notified"}).json()[
            "attention_items"
        ][0]["id"]
        snoozed = client.post(
            f"/v1/attention-items/{attention_item_id}/snooze",
            json={"snooze_until": snooze_until.isoformat()},
        )
        assert snoozed.status_code == 200
        assert snoozed.json()["attention_item"]["status"] == "snoozed"

        with _session_factory(client)() as db:
            with db.begin():
                db.execute(
                    text(
                        "UPDATE background_tasks "
                        "SET status = 'completed' "
                        "WHERE status = 'pending' "
                        "AND task_type = 'deliver_discord_notification'"
                    )
                )

        monkeypatch.setattr("ariel.worker._utcnow", lambda: snooze_until + timedelta(seconds=1))
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
            worker_id="worker-proactive",
        )

        item_after_follow_up = client.get(f"/v1/attention-items/{attention_item_id}")
        assert item_after_follow_up.status_code == 200
        assert item_after_follow_up.json()["attention_item"]["status"] == "notified"
        assert item_after_follow_up.json()["attention_item"]["next_follow_up_after"] is None

        notifications = client.get("/v1/notifications")
        assert notifications.status_code == 200
        assert [
            notification["status"] for notification in notifications.json()["notifications"]
        ] == [
            "pending",
            "acknowledged",
        ]
