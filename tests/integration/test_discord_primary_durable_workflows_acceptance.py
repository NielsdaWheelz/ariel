from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_run_message
from ariel.config import AppSettings
from ariel.persistence import enqueue_background_task
from ariel.worker import (
    claim_next_task,
    process_one_task,
    reap_stale_tasks,
)
from tests.fake_sandbox import FakeSandboxRuntime


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
        del input_items, tools, history, context_bundle
        return responses_run_message(
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


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
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


def test_background_tasks_claim_retry_and_reap_are_durable_and_worker_safe(
    postgres_url: str,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    adapter = DurableWorkflowAdapter()
    with _build_client(postgres_url, adapter) as client:
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

    adapter = DurableWorkflowAdapter()
    with _build_client(postgres_url, adapter) as client:
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
                        "AND task_type NOT IN ('memory_sweep', "
                        "'provider_watch_renew_due', 'provider_reconcile_sync_due') "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                ).scalar_one()
                assert task_type == "provider_sync_due"


def test_google_calendar_sync_persists_provider_evidence_without_ambient_case(
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
                        "description": "Please prepare design review notes by Friday.",
                        "status": "confirmed",
                        "updated": "2026-04-30T12:00:00Z",
                        "start": {"dateTime": "2026-05-01T17:00:00Z", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-05-01T17:30:00Z", "timeZone": "UTC"},
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

        discord_messages = client.get("/v1/discord-messages")
        assert discord_messages.status_code == 200
        assert discord_messages.json()["discord_messages"] == []

        with _session_factory(client)() as db:
            with db.begin():
                evidence = (
                    db.execute(
                        text(
                            "SELECT id, source_kind, external_id, calendar_id, source_uri, "
                            "extraction_status, lifecycle_state "
                            "FROM provider_evidence "
                            "WHERE source_kind = 'calendar_event' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert evidence["external_id"] == "event-1"
                assert evidence["calendar_id"] == "primary"
                assert evidence["source_uri"] == "https://calendar.google.com/event?eid=event-1"
                assert evidence["extraction_status"] == "pending"
                assert evidence["lifecycle_state"] == "available"

                pending_tasks = (
                    db.execute(
                        text(
                            "SELECT task_type, payload FROM background_tasks "
                            "WHERE status = 'pending' "
                            "ORDER BY created_at ASC"
                        )
                    )
                    .mappings()
                    .all()
                )
                # The calendar sync found new data, so it wakes the agent;
                # there is no commitment-extraction or ambient pipeline task.
                assert any(task["task_type"] == "agent_wake" for task in pending_tasks)
                assert all(
                    task["task_type"]
                    not in {"workspace_commitment_extraction_due", "ambient_interpretation_due"}
                    for task in pending_tasks
                )
