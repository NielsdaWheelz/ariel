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
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_memory_subsystem_call,
    last_user_message,
    responses_run_message,
)
from ariel.config import AppSettings
from ariel.google_connector import GOOGLE_CONNECTOR_ID
from ariel.persistence import (
    AgencyEventRecord,
    GoogleConnectorRecord,
    ProviderWatchChannelRecord,
    enqueue_background_task,
)
from ariel.worker import process_one_task
from tests.fake_sandbox import FakeSandboxRuntime


class DurableWorkflowAdapter(FakeModelAdapter):
    provider = "provider.discord-primary-durable"
    model = "model.discord-primary-durable-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        user_message = last_user_message(request.messages)
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )

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


class _DbDiag:
    def __init__(self, constraint_name: str | None = None) -> None:
        self.constraint_name = constraint_name


class _DbOrig(Exception):
    def __init__(self, *, sqlstate: str, constraint_name: str | None = None) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        self.diag = _DbDiag(constraint_name)


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
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


def _stored_agency_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": payload["source"],
        "event_id": payload["event_id"],
        "event_type": payload["event_type"],
        "external_job_id": payload["external_job_id"],
        "title": payload["title"],
        "summary": payload["summary"],
        "payload": payload["payload"],
    }


def _install_agency_insert_race_winner(
    *,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: Any,
    payload: dict[str, Any],
    stored_payload: dict[str, Any],
) -> list[bool]:
    original_flush = Session.flush
    inserted_race_winner = [False]

    def flush_with_duplicate_race(
        self: Session,
        objects: Any | None = None,
    ) -> None:
        is_agency_event_flush = any(isinstance(row, AgencyEventRecord) for row in self.new)
        if not inserted_race_winner[0] and is_agency_event_flush:
            inserted_race_winner[0] = True
            now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
            event_id = "age_race_winner"
            with session_factory() as seed_db:
                with seed_db.begin():
                    seed_db.add(
                        AgencyEventRecord(
                            id=event_id,
                            source=cast(str, payload["source"]),
                            external_event_id=cast(str, payload["event_id"]),
                            event_type=cast(str, payload["event_type"]),
                            external_job_id=cast(str, payload["external_job_id"]),
                            payload=stored_payload,
                            status="accepted",
                            error=None,
                            created_at=now,
                            received_at=now,
                            processed_at=None,
                        )
                    )
                    enqueue_background_task(
                        seed_db,
                        task_type="agency_event_received",
                        payload={"agency_event_id": event_id},
                        now=now,
                    )
            raise IntegrityError(
                "INSERT INTO agency_events",
                {},
                _DbOrig(
                    sqlstate="23505",
                    constraint_name="uq_agency_event_source_external_id",
                ),
            )
        return original_flush(self, objects)

    monkeypatch.setattr(Session, "flush", flush_with_duplicate_race)
    return inserted_race_winner


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
        "title": "Discord primary workflow",
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

        for _ in range(8):
            assert process_one_task(
                session_factory=_session_factory(client),
                settings=cast(Any, client.app).state.runtime.settings,
            )
            with _session_factory(client)() as db:
                status = db.execute(
                    text(
                        "SELECT status FROM agency_events "
                        "WHERE external_event_id = 'agency-event-001'"
                    )
                ).scalar_one()
            if status == "processed":
                break

        with _session_factory(client)() as db:
            row = (
                db.execute(
                    text(
                        "SELECT j.status AS job_status, j.title, j.summary, "
                        "e.status AS event_status, je.event_type AS job_event_type "
                        "FROM agency_events e "
                        "JOIN jobs j ON j.source = e.source "
                        "AND j.external_job_id = e.external_job_id "
                        "JOIN job_events je ON je.agency_event_id = e.id "
                        "WHERE e.external_event_id = 'agency-event-001'"
                    )
                )
                .mappings()
                .one()
            )
            wake_count = db.execute(
                text("SELECT COUNT(*) FROM background_tasks WHERE task_type = 'agent_wake'")
            ).scalar_one()

        assert row["event_status"] == "processed"
        assert row["job_status"] == "succeeded"
        assert row["title"] == "Discord primary workflow"
        assert row["summary"] == "Implementation finished."
        assert row["job_event_type"] == "job.completed"
        assert wake_count == 1


def test_agency_event_ingress_rejects_job_event_without_external_job_id(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_000
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload = {
        "source": "agency.local",
        "event_id": "agency-event-missing-job-id",
        "event_type": "job.completed",
        "summary": "Missing job id.",
        "payload": {"branch": "main"},
    }

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        response = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "E_AGENCY_EVENT_INVALID"
        assert _count_rows(client, "agency_events") == 0
        assert _count_rows(client, "background_tasks") == 0


def test_agency_event_ingress_retries_duplicate_insert_race(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_000
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload: dict[str, Any] = {
        "source": "agency.local",
        "event_id": "agency-event-race",
        "event_type": "job.completed",
        "external_job_id": "agency-job-race",
        "title": "Duplicate race workflow",
        "summary": "Concurrent insert won first.",
        "payload": {"branch": "main"},
    }

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        session_factory = _session_factory(client)
        inserted_race_winner = _install_agency_insert_race_winner(
            monkeypatch=monkeypatch,
            session_factory=session_factory,
            payload=payload,
            stored_payload=_stored_agency_event_payload(payload),
        )

        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        response = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)

        assert response.status_code == 202
        assert response.json()["duplicate"] is True
        assert inserted_race_winner == [True]
        assert _count_rows(client, "agency_events") == 1
        assert _count_rows(client, "background_tasks") == 1


def test_agency_event_ingress_retries_conflicting_insert_race(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_000
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload: dict[str, Any] = {
        "source": "agency.local",
        "event_id": "agency-event-conflict-race",
        "event_type": "job.completed",
        "external_job_id": "agency-job-conflict-race",
        "title": "Conflict race workflow",
        "summary": "Losing insert has this summary.",
        "payload": {"branch": "main"},
    }
    winning_payload = {**payload, "summary": "Concurrent winner used this summary."}

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        session_factory = _session_factory(client)
        inserted_race_winner = _install_agency_insert_race_winner(
            monkeypatch=monkeypatch,
            session_factory=session_factory,
            payload=winning_payload,
            stored_payload=_stored_agency_event_payload(winning_payload),
        )

        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        response = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "E_AGENCY_EVENT_CONFLICT"
        assert inserted_race_winner == [True]
        assert _count_rows(client, "agency_events") == 1
        assert _count_rows(client, "background_tasks") == 1


def test_agency_event_ingress_retries_serialization_failure(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-agency-secret"
    timestamp = 1_775_000_000
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", secret)
    monkeypatch.setattr(time, "time", FrozenClock(timestamp))
    payload: dict[str, Any] = {
        "source": "agency.local",
        "event_id": "agency-event-serialization-retry",
        "event_type": "job.completed",
        "external_job_id": "agency-job-serialization-retry",
        "title": "Serialization retry workflow",
        "summary": "Retried after serialization failure.",
        "payload": {"branch": "main"},
    }

    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        original_scalar = Session.scalar
        raised_serialization_failure = [False]

        def scalar_with_serialization_failure(
            self: Session,
            statement: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if not raised_serialization_failure[0] and "agency_events" in str(statement):
                raised_serialization_failure[0] = True
                raise OperationalError(
                    "SELECT agency_events",
                    {},
                    _DbOrig(sqlstate="40001"),
                )
            return original_scalar(self, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "scalar", scalar_with_serialization_failure)

        signed = _signed_agency_body(payload, secret=secret, timestamp=timestamp)
        response = client.post("/v1/agency/events", content=signed.body, headers=signed.headers)

        assert response.status_code == 202
        assert response.json()["duplicate"] is False
        assert raised_serialization_failure == [True]
        assert _count_rows(client, "agency_events") == 1
        assert _count_rows(client, "background_tasks") == 1


def _seed_calendar_watch_channel(
    client: TestClient,
    *,
    channel_id: str,
    channel_token: str,
    resource_id: str = "primary",
    provider_resource_id: str | None = None,
    status: str = "active",
    expires_at: datetime | None = None,
) -> None:
    now = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    with _session_factory(client)() as db:
        with db.begin():
            db.add(
                ProviderWatchChannelRecord(
                    id=f"wch-{channel_id}",
                    provider="google",
                    resource_type="calendar",
                    resource_id=resource_id,
                    channel_id=channel_id,
                    channel_token=channel_token,
                    provider_resource_id=provider_resource_id or f"res-{channel_id}",
                    cursor_seed=None,
                    status=status,
                    expires_at=expires_at or datetime.now(UTC) + timedelta(days=7),
                    created_at=now,
                    updated_at=now,
                )
            )


def test_google_provider_event_ingress_is_token_bound_deduped_and_conflict_safe(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_calendar_watch_channel(client, channel_id="channel-1", channel_token="channel-token")
        headers = {
            "X-Goog-Channel-Token": "channel-token",
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
        )
        with _session_factory(client)() as db:
            with db.begin():
                provider_event_id = db.execute(
                    text("SELECT id FROM provider_events ORDER BY created_at DESC LIMIT 1")
                ).scalar_one()
                sync_tasks = (
                    db.execute(
                        text(
                            "SELECT task_type, payload FROM background_tasks "
                            "WHERE task_type NOT IN ('memory_dream', "
                            "'provider_watch_renew_due', 'provider_reconcile_sync_due', "
                            "'expire_approvals') "
                            "ORDER BY created_at DESC"
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(sync_tasks) == 1
                assert sync_tasks[0]["task_type"] == "provider_sync_due"
                assert sync_tasks[0]["payload"] == {
                    "provider_event_id": provider_event_id,
                    "provider": "google",
                    "resource_type": "calendar",
                    "resource_id": "primary",
                }


def test_google_provider_event_rejects_unknown_channel_id(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "provider-token",
                "X-Goog-Channel-ID": "unknown-channel",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "exists",
                "content-type": "application/json",
            },
            content=b"{}",
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_PROVIDER_EVENT_CHANNEL_INVALID"


def test_google_provider_event_rejects_wrong_channel_token(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_calendar_watch_channel(
            client, channel_id="channel-1", channel_token="real-channel-token"
        )
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "provider-token",
                "X-Goog-Channel-ID": "channel-1",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "exists",
                "content-type": "application/json",
            },
            content=b"{}",
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_PROVIDER_EVENT_CHANNEL_INVALID"


def test_google_provider_event_rejects_cross_channel_token_replay(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_calendar_watch_channel(client, channel_id="channel-a", channel_token="token-a")
        _seed_calendar_watch_channel(
            client, channel_id="channel-b", channel_token="token-b", resource_id="secondary"
        )
        # Posting channel-b's token against channel-a's id must fail.
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "token-b",
                "X-Goog-Channel-ID": "channel-a",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "exists",
                "content-type": "application/json",
            },
            content=b"{}",
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    ("channel_status", "expires_at"),
    [
        ("failed", None),
        ("active", datetime(2026, 5, 18, 12, 0, tzinfo=UTC)),
    ],
)
def test_google_provider_event_rejects_inactive_or_expired_channel(
    postgres_url: str,
    channel_status: str,
    expires_at: datetime | None,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_calendar_watch_channel(
            client,
            channel_id="channel-1",
            channel_token="channel-token",
            status=channel_status,
            expires_at=expires_at,
        )
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "channel-token",
                "X-Goog-Channel-ID": "channel-1",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "exists",
                "content-type": "application/json",
            },
            content=b"{}",
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_PROVIDER_EVENT_CHANNEL_INVALID"


def test_google_provider_event_rejects_provider_resource_id_mismatch(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        _seed_calendar_watch_channel(
            client,
            channel_id="channel-1",
            channel_token="channel-token",
            provider_resource_id="provider-resource-1",
        )
        response = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "channel-token",
                "X-Goog-Channel-ID": "channel-1",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-ID": "provider-resource-2",
                "X-Goog-Resource-State": "exists",
                "content-type": "application/json",
            },
            content=b"{}",
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_PROVIDER_EVENT_CHANNEL_INVALID"


def test_google_provider_event_rejects_gmail_resource_type(
    postgres_url: str,
) -> None:
    with _build_client(postgres_url, DurableWorkflowAdapter()) as client:
        response = client.post(
            "/v1/providers/google/events?resource_type=gmail&resource_id=primary",
            headers={
                "X-Goog-Channel-Token": "provider-token",
                "X-Goog-Channel-ID": "channel-1",
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-State": "exists",
                "content-type": "application/json",
            },
            content=b"{}",
        )
        assert response.status_code == 422


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
        now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        with _session_factory(client)() as db:
            with db.begin():
                db.add(
                    GoogleConnectorRecord(
                        id=GOOGLE_CONNECTOR_ID,
                        provider="google",
                        status="connected",
                        account_subject="sub_durable_sync",
                        account_email="durable-sync@example.com",
                        granted_scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                        access_token_enc=None,
                        refresh_token_enc=None,
                        access_token_expires_at=None,
                        token_obtained_at=None,
                        encryption_key_version="v1",
                        last_error_code=None,
                        last_error_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                enqueue_background_task(
                    db,
                    task_type="provider_sync_due",
                    payload={
                        "provider": "google",
                        "resource_type": "calendar",
                        "resource_id": "primary",
                    },
                    now=now,
                )

        settings = cast(Any, AppSettings)(_env_file=None)
        assert process_one_task(
            session_factory=_session_factory(client),
            settings=settings,
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
                            "ORDER BY created_at ASC"
                        )
                    )
                    .mappings()
                    .all()
                )
                # The calendar sync found new data, so it wakes the agent.
                assert any(task["task_type"] == "agent_wake" for task in pending_tasks)
