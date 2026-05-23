from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ariel.app import create_app
from ariel.model_adapter import ModelAdapter, ModelAdapterError
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
    responses_run_message,
)
from tests.fake_sandbox import FakeSandboxRuntime


def _parse_utc_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC
    return parsed


def _timeline(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    return resp.json()


@dataclass
class DeterministicModelAdapter:
    provider: str = "provider.test"
    model: str = "model.test-v1"
    fail: bool = False

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, history, context_bundle
        if self.fail:
            raise ModelAdapterError(
                safe_reason="simulated provider failure",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=False,
            )
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_test_123",
            input_tokens=11,
            output_tokens=7,
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def test_user_can_send_message_and_receive_model_backed_response(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        turn = post_message_and_drain(client, session_id, message="hello from phone")
        assert turn.assistant_message == "assistant::hello from phone"
        assert turn.status == "completed"


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


def test_single_active_session_and_ordered_turn_event_chain(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        for message in ("first message", "second message"):
            post_message_and_drain(client, session_id, message=message)

        active_again = client.get("/v1/sessions/active")
        assert active_again.status_code == 200
        assert active_again.json()["session"]["id"] == session_id

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert [turn["user_message"] for turn in turns] == ["first message", "second message"]

        # Pre-turn retriever fires before the main agent on every wake, producing
        # two retriever model events followed by the main agent model events.
        expected_types = [
            "evt.turn.started",
            "evt.model.started",  # retriever
            "evt.model.completed",  # retriever
            "evt.model.started",  # main agent
            "evt.model.completed",  # main agent
            "evt.assistant.emitted",
            "evt.turn.completed",
        ]
        for turn in turns:
            assert [event["event_type"] for event in turn["events"]] == expected_types
            assert [event["sequence"] for event in turn["events"]] == list(
                range(1, len(expected_types) + 1)
            )

        first_turn_ts = _parse_utc_rfc3339(turns[0]["created_at"])
        second_turn_ts = _parse_utc_rfc3339(turns[1]["created_at"])
        assert first_turn_ts <= second_turn_ts


def test_model_timeline_includes_identity_duration_and_usage(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(provider="provider.alpha", model="alpha-mini")
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        post_message_and_drain(client, session_id, message="inspect model metadata")

        timeline = _timeline(client, session_id)
        events = timeline["turns"][0]["events"]
        # The pre-turn retriever fires first (its model.completed has 0 tokens from
        # empty_recall_response). The main agent's model.completed is the last one;
        # it carries the real token counts reported by DeterministicModelAdapter.
        model_completed_events = [
            event for event in events if event["event_type"] == "evt.model.completed"
        ]
        # Main agent's event is the last evt.model.completed in the sequence.
        model_completed = model_completed_events[-1]
        payload = model_completed["payload"]
        assert payload["provider"] == "provider.alpha"
        assert payload["model"] == "alpha-mini"
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0
        assert payload["usage"]["input_tokens"] == 11
        assert payload["usage"]["output_tokens"] == 7
        assert payload["usage"]["total_tokens"] == 18


def test_model_failure_is_auditable_and_turn_terminates_failed(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(fail=True)
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="this should fail")
        assert turn.status == "failed"

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert len(turns) == 1
        turn_data = turns[0]
        assert turn_data["status"] == "failed"
        event_types = [event["event_type"] for event in turn_data["events"]]
        # Retriever runs first (succeeds, emitting its model events); then the
        # main agent call fails with the typed model adapter error.
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",  # retriever
            "evt.model.completed",  # retriever
            "evt.model.started",  # main agent
            "evt.model.failed",  # main agent
            "evt.turn.failed",
        ]
        model_failed = next(
            event for event in turn_data["events"] if event["event_type"] == "evt.model.failed"
        )
        assert model_failed["payload"]["failure_reason"] == "simulated provider failure"
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

        turn = post_message_and_drain(client, session["id"], message="validate ids")
        assert turn.id.startswith("trn_")
        _parse_utc_rfc3339(turn.created_at.isoformat())
        _parse_utc_rfc3339(turn.updated_at.isoformat())

        timeline = _timeline(client, session["id"])
        for saved_turn in timeline["turns"]:
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

        timeline = _timeline(client, session_id)
        assert timeline["turns"] == []


@dataclass
class NonSecretFailureAdapter:
    provider: str = "provider.non-secret"
    model: str = "model.non-secret-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle
        raise ModelAdapterError(
            safe_reason="token limit exceeded for this request",
            status_code=502,
            code="E_MODEL_FAILURE",
            message="model provider request failed",
            retryable=False,
        )


def test_default_runtime_model_requires_server_secret_credentials(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MODEL_NAME", "gpt-5.5")
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "")

    app = create_test_app(
        database_url=postgres_url,
        model_adapter=None,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="credential check")
        assert turn.status == "failed"

        timeline = _timeline(client, session_id)
        events = timeline["turns"][0]["events"]
        event_types = [event["event_type"] for event in events]
        # The real adapter raises ModelAdapterError(safe_reason="model credentials
        # are not configured") for both the retriever call and the main agent call.
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",  # retriever (no API key)
            "evt.model.failed",  # retriever fails
            "evt.memory.recall_failed",  # typed recall failure is non-fatal
            "evt.model.started",  # main agent (no API key)
            "evt.model.failed",  # main agent fails
            "evt.turn.failed",
        ]
        # The main agent's model.failed is the last evt.model.failed event.
        model_failed_events = [e for e in events if e["event_type"] == "evt.model.failed"]
        failure_payload = model_failed_events[-1]["payload"]
        assert "credential" in failure_payload["failure_reason"].lower()
        assert "sk-" not in failure_payload["failure_reason"]


def test_model_failure_reason_preserves_non_secret_detail(postgres_url: str) -> None:
    adapter = NonSecretFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger non-secret failure")
        assert turn.status == "failed"

        timeline = _timeline(client, session_id)
        events = timeline["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert model_failed["payload"]["failure_reason"] == "token limit exceeded for this request"


def test_restart_preserves_history_and_appends_to_same_active_session(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as first_client:
        first_session = first_client.get("/v1/sessions/active")
        assert first_session.status_code == 200
        session_id = first_session.json()["session"]["id"]
        post_message_and_drain(first_client, session_id, message="before restart")

        timeline_before = _timeline(first_client, session_id)
        assert [turn["user_message"] for turn in timeline_before["turns"]] == ["before restart"]

    restarted_app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(restarted_app) as second_client:
        active_after_restart = second_client.get("/v1/sessions/active")
        assert active_after_restart.status_code == 200
        assert active_after_restart.json()["session"]["id"] == session_id

        timeline_after_restart = _timeline(second_client, session_id)
        assert [turn["user_message"] for turn in timeline_after_restart["turns"]] == [
            "before restart"
        ]

        post_message_and_drain(second_client, session_id, message="after restart")

        final_timeline = _timeline(second_client, session_id)
        assert [turn["user_message"] for turn in final_timeline["turns"]] == [
            "before restart",
            "after restart",
        ]
