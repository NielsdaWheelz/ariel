from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from ariel.app import ModelAdapter, ModelAdapterError, create_app
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
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history, context_bundle
        if self.fail:
            raise RuntimeError("simulated provider failure")
        return {
            "assistant_text": f"assistant::{user_message}",
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "provider_response_id": "resp_test_123",
        }


@dataclass
class ContextWindowDecisionAdapter:
    provider: str = "provider.context-window"
    model: str = "model.context-window-v1"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history
        self.context_bundles.append(context_bundle)

        normalized = user_message.strip().lower()
        if normalized == "book me travel":
            assistant_text = (
                "i need your destination and travel dates before i can plan this trip."
            )
        elif normalized.startswith("project codename is "):
            declared_codename = normalized.replace("project codename is ", "", 1).strip()
            assistant_text = f"noted. project codename set to {declared_codename}."
        elif normalized == "what is the project codename?":
            codename = self._find_recent_codename(context_bundle)
            if codename is None:
                assistant_text = (
                    "i'm not sure because that detail is outside my recent context window. "
                    "please remind me of the codename."
                )
            else:
                assistant_text = f"your project codename is {codename}."
        else:
            assistant_text = f"direct::{user_message}"

        return {
            "assistant_text": assistant_text,
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 9, "completion_tokens": 11, "total_tokens": 20},
            "provider_response_id": "resp_context_window_123",
        }

    def _find_recent_codename(self, context_bundle: dict[str, Any]) -> str | None:
        recent_turns = context_bundle.get("recent_active_session_turns")
        if not isinstance(recent_turns, list):
            return None
        for turn in reversed(recent_turns):
            if not isinstance(turn, dict):
                continue
            prior_user_message = turn.get("user_message")
            if not isinstance(prior_user_message, str):
                continue
            normalized = prior_user_message.strip().lower()
            if normalized.startswith("project codename is "):
                return normalized.replace("project codename is ", "", 1).strip()
        return None


@dataclass
class MutatingContextAdapter:
    provider: str = "provider.mutating"
    model: str = "model.mutating-v1"

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, session_id, turn_id, history
        section_order = context_bundle.get("section_order")
        if isinstance(section_order, list):
            section_order.append("mutated")
        recent_window = context_bundle.get("recent_window")
        if isinstance(recent_window, dict):
            recent_window["included_turn_count"] = 999
            recent_window["included_turn_ids"] = ["mutated"]

        return {
            "assistant_text": "mutating-adapter-response",
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
            "provider_response_id": "resp_mutating_123",
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


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
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


def test_pr01_model_led_direct_and_clarification_messages_are_emitted(postgres_url: str) -> None:
    adapter = ContextWindowDecisionAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        clear_turn = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "summarize this in one line"},
        )
        assert clear_turn.status_code == 200
        assert clear_turn.json()["assistant"]["message"].startswith("direct::")

        ambiguous_turn = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "book me travel"},
        )
        assert ambiguous_turn.status_code == 200
        assert "destination and travel dates" in ambiguous_turn.json()["assistant"]["message"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        event_types_by_turn = [
            [event["event_type"] for event in turn["events"]] for turn in timeline.json()["turns"]
        ]
        assert all("evt.assistant.emitted" in event_types for event_types in event_types_by_turn)
        assert all("evt.turn.completed" in event_types for event_types in event_types_by_turn)


def test_pr01_turn_context_is_bounded_ordered_and_auditable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECENT_TURNS", "1")
    adapter = ContextWindowDecisionAdapter()

    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        turn_1 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "project codename is aurora"},
        )
        assert turn_1.status_code == 200

        turn_2 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the project codename?"},
        )
        assert turn_2.status_code == 200
        assert "aurora" in turn_2.json()["assistant"]["message"]

        turn_3 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "let's move on"},
        )
        assert turn_3.status_code == 200

        turn_4 = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "what is the project codename?"},
        )
        assert turn_4.status_code == 200
        assert "outside my recent context window" in turn_4.json()["assistant"]["message"]

        assert len(adapter.context_bundles) == 4
        for context_bundle in adapter.context_bundles:
            assert context_bundle["section_order"] == [
                "policy_system_instructions",
                "recent_active_session_turns",
            ]

        second_turn_context = adapter.context_bundles[1]
        assert [turn["user_message"] for turn in second_turn_context["recent_active_session_turns"]] == [
            "project codename is aurora"
        ]

        fourth_turn_context = adapter.context_bundles[3]
        assert [turn["user_message"] for turn in fourth_turn_context["recent_active_session_turns"]] == [
            "let's move on"
        ]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert len(turns) == 4

        model_started_second_turn = next(
            event for event in turns[1]["events"] if event["event_type"] == "evt.model.started"
        )
        second_context_meta = model_started_second_turn["payload"]["context"]
        assert second_context_meta["schema_version"] == "1.0"
        assert second_context_meta["section_order"] == [
            "policy_system_instructions",
            "recent_active_session_turns",
        ]
        assert second_context_meta["policy_instruction_count"] >= 1
        assert second_context_meta["recent_window"] == {
            "max_recent_turns": 1,
            "included_turn_count": 1,
            "omitted_turn_count": 0,
            "included_turn_ids": [turns[0]["id"]],
        }

        model_started_fourth_turn = next(
            event for event in turns[3]["events"] if event["event_type"] == "evt.model.started"
        )
        fourth_context_meta = model_started_fourth_turn["payload"]["context"]
        assert fourth_context_meta["schema_version"] == "1.0"
        assert fourth_context_meta["recent_window"]["max_recent_turns"] == 1
        assert fourth_context_meta["recent_window"]["included_turn_count"] == 1
        assert fourth_context_meta["recent_window"]["omitted_turn_count"] == 2
        assert fourth_context_meta["recent_window"]["included_turn_ids"] == [turns[2]["id"]]


def test_pr01_context_audit_is_stable_even_if_adapter_mutates_context_bundle(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECENT_TURNS", "1")
    adapter = MutatingContextAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        first = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "seed history"},
        )
        assert first.status_code == 200

        second = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "mutate context"},
        )
        assert second.status_code == 200

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        model_started_second_turn = next(
            event for event in turns[1]["events"] if event["event_type"] == "evt.model.started"
        )
        context_meta = model_started_second_turn["payload"]["context"]
        assert context_meta["schema_version"] == "1.0"
        assert context_meta["section_order"] == [
            "policy_system_instructions",
            "recent_active_session_turns",
        ]
        assert context_meta["recent_window"] == {
            "max_recent_turns": 1,
            "included_turn_count": 1,
            "omitted_turn_count": 0,
            "included_turn_ids": [turns[0]["id"]],
        }


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
        assert "formatEventDetails" in html
        assert "provider=" in html
        assert "model=" in html
        assert "duration_ms=" in html
        assert "failure_reason=" in html
        assert "tokens(" in html

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        turns = timeline.json()["turns"]
        assert [turn["user_message"] for turn in turns] == ["msg-a", "msg-b"]
        assert turns[0]["events"][0]["event_type"] == "evt.turn.started"
        assert turns[1]["events"][0]["event_type"] == "evt.turn.started"


@dataclass
class SecretLeakingFailureAdapter:
    provider: str = "provider.leaky"
    model: str = "model.leaky-v1"
    secret_value: str = "sk-live-very-secret"

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, session_id, turn_id, history, context_bundle
        raise RuntimeError(f"provider rejected credential {self.secret_value}")


@dataclass
class NonSecretFailureAdapter:
    provider: str = "provider.non-secret"
    model: str = "model.non-secret-v1"

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, session_id, turn_id, history, context_bundle
        raise RuntimeError("token limit exceeded for this request")


def test_default_runtime_model_requires_server_secret_credentials(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("ARIEL_MODEL_NAME", "gpt-4o-mini")
    # Force empty key so this assertion is stable even if local .env files exist.
    monkeypatch.setenv("ARIEL_MODEL_API_KEY", "")

    app = create_app(
        database_url=postgres_url,
        model_adapter=None,
        reset_database=True,
    )
    with TestClient(app) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "credential check"},
        )
        assert send.status_code == 503
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_CREDENTIALS"
        assert body["error"]["retryable"] is False
        assert "credential" in body["error"]["message"].lower()
        assert "sk-" not in str(body)

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        event_types = [event["event_type"] for event in events]
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",
            "evt.model.failed",
            "evt.turn.failed",
        ]
        failure_payload = next(
            event["payload"] for event in events if event["event_type"] == "evt.model.failed"
        )
        assert "credential" in failure_payload["failure_reason"].lower()
        assert "sk-" not in failure_payload["failure_reason"]


def test_model_failure_reason_is_redacted_for_secret_like_exceptions(postgres_url: str) -> None:
    adapter = SecretLeakingFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger redaction"},
        )
        assert send.status_code == 502
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_FAILURE"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert adapter.secret_value not in model_failed["payload"]["failure_reason"]
        assert "RuntimeError" in model_failed["payload"]["failure_reason"]


def test_model_failure_reason_preserves_non_secret_detail(postgres_url: str) -> None:
    adapter = NonSecretFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger non-secret failure"},
        )
        assert send.status_code == 502
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_MODEL_FAILURE"

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        events = timeline.json()["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert model_failed["payload"]["failure_reason"] == "token limit exceeded for this request"


def test_restart_preserves_history_and_appends_to_same_active_session(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as first_client:
        first_session = first_client.get("/v1/sessions/active")
        assert first_session.status_code == 200
        session_id = first_session.json()["session"]["id"]
        first_send = first_client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "before restart"},
        )
        assert first_send.status_code == 200

        timeline_before = first_client.get(f"/v1/sessions/{session_id}/events")
        assert timeline_before.status_code == 200
        assert [turn["user_message"] for turn in timeline_before.json()["turns"]] == ["before restart"]

    restarted_app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=False,
    )
    with TestClient(restarted_app) as second_client:
        active_after_restart = second_client.get("/v1/sessions/active")
        assert active_after_restart.status_code == 200
        assert active_after_restart.json()["session"]["id"] == session_id

        timeline_after_restart = second_client.get(f"/v1/sessions/{session_id}/events")
        assert timeline_after_restart.status_code == 200
        assert [turn["user_message"] for turn in timeline_after_restart.json()["turns"]] == [
            "before restart"
        ]

        second_send = second_client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "after restart"},
        )
        assert second_send.status_code == 200

        final_timeline = second_client.get(f"/v1/sessions/{session_id}/events")
        assert final_timeline.status_code == 200
        assert [turn["user_message"] for turn in final_timeline.json()["turns"]] == [
            "before restart",
            "after restart",
        ]


@dataclass
class LongResponseAdapter:
    provider: str = "provider.long-response"
    model: str = "model.long-response-v1"
    response_token_count: int = 16

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, session_id, turn_id, history, context_bundle
        assistant_text = " ".join(["long"] * self.response_token_count)
        return {
            "assistant_text": assistant_text,
            "provider": self.provider,
            "model": self.model,
            "usage": {"prompt_tokens": 5, "completion_tokens": self.response_token_count, "total_tokens": 5},
            "provider_response_id": "resp_long_123",
        }


@dataclass
class UsageDrivenResponseAdapter:
    provider: str = "provider.usage-driven"
    model: str = "model.usage-driven-v1"
    reported_completion_tokens: int = 12

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, session_id, turn_id, history, context_bundle
        return {
            "assistant_text": "ok",
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": self.reported_completion_tokens,
                "total_tokens": self.reported_completion_tokens + 2,
            },
            "provider_response_id": "resp_usage_123",
        }


@dataclass
class RetryableFailureAdapter:
    provider: str = "provider.retryable-failure"
    model: str = "model.retryable-failure-v1"
    attempts: int = 0

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del user_message, session_id, turn_id, history, context_bundle
        self.attempts += 1
        raise ModelAdapterError(
            safe_reason="temporary provider timeout",
            status_code=502,
            code="E_MODEL_FAILURE",
            message="model provider request failed",
            retryable=True,
        )


def test_pr02_context_budget_exhaustion_returns_bounded_failure_with_audit_details(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "1")

    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "hello from bounded context"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        assert "context budget" in body["error"]["message"].lower()

        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "context_tokens"
        assert limit_details["unit"] == "tokens"
        assert limit_details["limit"] == 1
        assert limit_details["measured"] > 1
        assert body["error"]["details"]["session_id"] == session_id
        assert body["error"]["details"]["applied_limits"]["max_recent_turns"] == 12
        assert body["error"]["details"]["applied_limits"]["max_context_tokens"] == 1

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turns = timeline.json()["turns"]
        assert len(turns) == 1
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in turns)

        turn = turns[0]
        assert turn["status"] == "failed"
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.turn.limit_reached",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]
        limit_event = next(
            event for event in turn["events"] if event["event_type"] == "evt.turn.limit_reached"
        )
        assert limit_event["payload"]["code"] == "E_TURN_LIMIT_REACHED"
        assert limit_event["payload"]["limit"]["budget"] == "context_tokens"
        assistant_emitted = next(
            event for event in turn["events"] if event["event_type"] == "evt.assistant.emitted"
        )
        assert "context budget" in assistant_emitted["payload"]["message"].lower()


def test_pr02_response_budget_exhaustion_is_emitted_before_terminal_failed(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "3")

    adapter = LongResponseAdapter(response_token_count=8)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger response budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        assert "response budget" in body["error"]["message"].lower()

        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "response_tokens"
        assert limit_details["unit"] == "tokens"
        assert limit_details["limit"] == 3
        assert limit_details["measured"] > 3
        assert body["error"]["details"]["applied_limits"]["max_response_tokens"] == 3

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["status"] == "failed"
        assert not any(
            saved_turn["status"] == "in_progress" for saved_turn in timeline.json()["turns"]
        )
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",
            "evt.model.completed",
            "evt.turn.limit_reached",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]
        model_completed = next(
            event for event in turn["events"] if event["event_type"] == "evt.model.completed"
        )
        assert model_completed["payload"]["provider"] == adapter.provider
        assert model_completed["payload"]["model"] == adapter.model


def test_pr02_response_budget_uses_reported_completion_tokens_when_present(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "5")

    adapter = UsageDrivenResponseAdapter(reported_completion_tokens=9)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger usage-driven response budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "response_tokens"
        assert limit_details["measured"] == 9
        assert limit_details["limit"] == 5

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",
            "evt.model.completed",
            "evt.turn.limit_reached",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]


def test_pr02_model_attempt_budget_exhaustion_uses_limit_error_not_model_error(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_MODEL_ATTEMPTS", "2")
    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger model attempt budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        assert "attempt limit" in body["error"]["message"].lower()

        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "model_attempts"
        assert limit_details["unit"] == "attempts"
        assert limit_details["limit"] == 2
        assert limit_details["measured"] == 2
        assert body["error"]["details"]["applied_limits"]["max_model_attempts"] == 2

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["status"] == "failed"
        events = turn["events"]
        assert len([event for event in events if event["event_type"] == "evt.model.started"]) == 2
        assert len([event for event in events if event["event_type"] == "evt.model.failed"]) == 2
        event_types = [event["event_type"] for event in events]
        assert event_types[-3:] == [
            "evt.turn.limit_reached",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in timeline.json()["turns"])


def test_pr02_wall_time_budget_takes_precedence_if_multiple_limits_exhaust(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_MODEL_ATTEMPTS", "1")
    monkeypatch.setenv("ARIEL_MAX_TURN_WALL_TIME_MS", "20")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.03
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger competing limits"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "turn_wall_time_ms"
        assert limit_details["measured"] > limit_details["limit"]

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        assert turn["status"] == "failed"
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",
            "evt.model.failed",
            "evt.turn.limit_reached",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]


def test_pr02_wall_time_budget_exhaustion_is_bounded_and_auditable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_TURN_WALL_TIME_MS", "20")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.03
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        send = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "trigger wall-time budget"},
        )
        assert send.status_code == 429
        body = send.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_TURN_LIMIT_REACHED"
        assert "time budget" in body["error"]["message"].lower()

        limit_details = body["error"]["details"]["limit"]
        assert limit_details["budget"] == "turn_wall_time_ms"
        assert limit_details["unit"] == "ms"
        assert limit_details["limit"] == 20
        assert limit_details["measured"] > 20
        assert body["error"]["details"]["applied_limits"]["max_turn_wall_time_ms"] == 20

        timeline = client.get(f"/v1/sessions/{session_id}/events")
        assert timeline.status_code == 200
        turn = timeline.json()["turns"][0]
        event_types = [event["event_type"] for event in turn["events"]]
        assert event_types[-3:] == [
            "evt.turn.limit_reached",
            "evt.assistant.emitted",
            "evt.turn.failed",
        ]
        assert turn["status"] == "failed"
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in timeline.json()["turns"])
