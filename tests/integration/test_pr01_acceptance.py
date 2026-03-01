from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from ariel.app import create_app
from ariel.db import run_migrations


def _parse_utc_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC
    return parsed


@dataclass
class DeterministicModelAdapter:
    provider: str = "provider.test"
    model: str = "model.test-v1"
    fail: bool = False

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return {
            "assistant_text": f"assistant::{user_message}",
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "provider_response_id": "resp_test_123",
        }


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def fresh_postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: DeterministicModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def test_user_can_send_message_and_receive_model_backed_response(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        active = client.get("/v1/sessions/active")
        assert active.status_code == 200
        session_id = active.json()["session"]["id"]

        response = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "hello from phone"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["assistant"]["message"] == "assistant::hello from phone"
        assert body["turn"]["status"] == "completed"


def test_create_session_endpoint_reuses_single_active_session(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        first = client.post("/v1/sessions")
        second = client.post("/v1/sessions")
        active = client.get("/v1/sessions/active")

        assert first.status_code == 200
        assert second.status_code == 200
        assert active.status_code == 200

        first_id = first.json()["session"]["id"]
        assert second.json()["session"]["id"] == first_id
        assert active.json()["session"]["id"] == first_id


def test_schema_not_ready_returns_503_until_migrated(fresh_postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()

    app_without_migration = create_app(
        database_url=fresh_postgres_url,
        model_adapter=adapter,
        reset_database=False,
    )
    with TestClient(app_without_migration) as client:
        health = client.get("/v1/health")
        assert health.status_code == 503
        health_body = health.json()
        assert health_body["ok"] is False
        assert health_body["error"]["code"] == "E_SCHEMA_NOT_READY"
        assert "missing_tables" in health_body["error"]["details"]

        active = client.get("/v1/sessions/active")
        assert active.status_code == 503
        active_body = active.json()
        assert active_body["ok"] is False
        assert active_body["error"]["code"] == "E_SCHEMA_NOT_READY"

    run_migrations(fresh_postgres_url)
    app_with_migration = create_app(
        database_url=fresh_postgres_url,
        model_adapter=adapter,
        reset_database=False,
    )
    with TestClient(app_with_migration) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/sessions/active").status_code == 200


def test_single_active_session_and_ordered_turn_event_chain(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("first message", "second message"):
            send = client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": message},
            )
            assert send.status_code == 200

        active_again = client.get("/v1/sessions/active")
        assert active_again.status_code == 200
        assert active_again.json()["session"]["id"] == session_id

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert [turn["user_message"] for turn in turns] == ["first message", "second message"]

        expected_types = [
            "evt.turn.started",
            "evt.model.started",
            "evt.model.completed",
            "evt.assistant.emitted",
            "evt.turn.completed",
        ]
        for turn in turns:
            assert [event["event_type"] for event in turn["events"]] == expected_types
            assert [event["sequence"] for event in turn["events"]] == [1, 2, 3, 4, 5]

        first_turn_ts = _parse_utc_rfc3339(turns[0]["created_at"])
        second_turn_ts = _parse_utc_rfc3339(turns[1]["created_at"])
        assert first_turn_ts <= second_turn_ts


def test_model_timeline_includes_identity_duration_and_usage(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(provider="provider.alpha", model="alpha-mini")
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "inspect model metadata"},
        )
        assert send.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        model_completed = next(event for event in events if event["event_type"] == "evt.model.completed")
        payload = model_completed["payload"]
        assert payload["provider"] == "provider.alpha"
        assert payload["model"] == "alpha-mini"
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0
        assert payload["usage"]["prompt_tokens"] == 11
        assert payload["usage"]["completion_tokens"] == 7
        assert payload["usage"]["total_tokens"] == 18


def test_model_failure_is_auditable_and_turn_terminates_failed(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(fail=True)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "this should fail"},
        )
        assert send.status_code == 502
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_FAILURE"
        assert body["error"]["retryable"] is True

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert len(turns) == 1
        turn = turns[0]
        assert turn["status"] == "failed"
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",
            "evt.model.failed",
            "evt.turn.failed",
        ]
        model_failed = next(event for event in turn["events"] if event["event_type"] == "evt.model.failed")
        assert "failure_reason" in model_failed["payload"]
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in turns)


def test_ids_timestamps_and_error_envelope_follow_constitution(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        active = client.get("/v1/sessions/active")
        assert active.status_code == 200
        session = active.json()["session"]
        assert session["id"].startswith("ses_")
        _parse_utc_rfc3339(session["created_at"])
        _parse_utc_rfc3339(session["updated_at"])

        send = client.post(
            f"/v1/sessions/{session['id']}/message",
            json={"message": "validate ids"},
        )
        assert send.status_code == 200
        turn = send.json()["turn"]
        assert turn["id"].startswith("trn_")
        _parse_utc_rfc3339(turn["created_at"])
        _parse_utc_rfc3339(turn["updated_at"])

        timeline = client.get(f"/v1/sessions/{session['id']}/events")
        for saved_turn in timeline.json()["turns"]:
            _parse_utc_rfc3339(saved_turn["created_at"])
            _parse_utc_rfc3339(saved_turn["updated_at"])
            for event in saved_turn["events"]:
                _parse_utc_rfc3339(event["created_at"])

        missing = client.post(
            "/v1/sessions/ses_01JZZZZZZZZZZZZZZZZZZZZZZZ/message",
            json={"message": "missing"},
        )
        assert missing.status_code == 404
        error = missing.json()
        assert error["ok"] is False
        assert error["error"]["code"] == "E_SESSION_NOT_FOUND"
        assert isinstance(error["error"]["details"], dict)
        assert error["error"]["retryable"] is False


def test_whitespace_only_message_is_rejected_with_standard_error(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        invalid = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "   "},
        )
        assert invalid.status_code == 422
        body = invalid.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_VALIDATION"
        assert body["error"]["retryable"] is False

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        assert timeline.json()["turns"] == []


def test_phone_surface_renders_timeline_from_stored_event_chain(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("msg-a", "msg-b"):
            sent = client.post(
                f"/v1/sessions/{session_id}/message",
                json={"message": message},
            )
            assert sent.status_code == 200

        surface = client.get("/")
        assert surface.status_code == 200
        html = surface.text
        assert "viewport" in html
        assert "/v1/sessions/active" in html
        assert "/v1/sessions/${sessionId}/events" in html

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        turns = timeline.json()["turns"]
        assert [turn["user_message"] for turn in turns] == ["msg-a", "msg-b"]
        assert turns[0]["events"][0]["event_type"] == "evt.turn.started"
        assert turns[1]["events"][0]["event_type"] == "evt.turn.started"
