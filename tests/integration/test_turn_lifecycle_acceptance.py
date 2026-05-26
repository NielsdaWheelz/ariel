from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from ariel.app import create_app
from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_memory_subsystem_call,
    last_user_message,
    post_message_and_drain,
    responses_run_message,
)


def _parse_utc_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC
    return parsed


def _timeline(client: TestClient) -> dict[str, Any]:
    resp = client.get("/v1/events")
    assert resp.status_code == 200
    return resp.json()


class DeterministicModelAdapter(FakeModelAdapter):
    provider = "provider.test"
    model = "model.test-v1"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        if self.fail:
            raise RuntimeError("simulated provider failure")
        user_message = last_user_message(request.messages)
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
        turn = post_message_and_drain(client, message="hello from phone")
        assert turn.assistant_message == "assistant::hello from phone"
        assert turn.status == "completed"


def test_single_session_and_ordered_turn_event_chain(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        for message in ("first message", "second message"):
            post_message_and_drain(client, message=message)

        timeline = _timeline(client)
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
    class IdentifiedAdapter(DeterministicModelAdapter):
        provider = "provider.alpha"
        model = "alpha-mini"

    adapter = IdentifiedAdapter()
    with _build_client(postgres_url, adapter) as client:
        post_message_and_drain(client, message="inspect model metadata")

        timeline = _timeline(client)
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


def test_model_failure_is_auditable_and_user_message_falls_back(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter(fail=True)
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="this should fail")
        # User-message wakes fall back to a polite reply on model failure so the
        # user sees something instead of silence; the model failure is still
        # auditable via evt.model.failed.
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to complete that request. Please try again."

        timeline = _timeline(client)
        turns = timeline["turns"]
        assert len(turns) == 1
        turn_data = turns[0]
        assert turn_data["status"] == "completed"
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert event_types == [
            "evt.turn.started",
            "evt.model.started",  # retriever
            "evt.model.completed",  # retriever
            "evt.model.started",  # main agent
            "evt.model.failed",  # main agent
            "evt.assistant.emitted",
            "evt.turn.completed",
        ]
        model_failed = next(
            event for event in turn_data["events"] if event["event_type"] == "evt.model.failed"
        )
        assert model_failed["payload"]["failure_reason"] == "simulated provider failure"
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in turns)


def test_ids_timestamps_and_error_envelope_follow_constitution(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="validate ids")
        assert turn.id.startswith("trn_")
        _parse_utc_rfc3339(turn.created_at.isoformat())
        _parse_utc_rfc3339(turn.updated_at.isoformat())

        timeline = _timeline(client)
        for saved_turn in timeline["turns"]:
            _parse_utc_rfc3339(saved_turn["created_at"])
            _parse_utc_rfc3339(saved_turn["updated_at"])
            for event in saved_turn["events"]:
                _parse_utc_rfc3339(event["created_at"])


def test_whitespace_only_message_is_rejected_with_standard_error(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as client:
        invalid = client.post(
            "/v1/messages",
            json={"message": "   "},
        )
        assert invalid.status_code == 422
        body = invalid.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "E_VALIDATION"
        assert body["error"]["retryable"] is False

        timeline = _timeline(client)
        assert timeline["turns"] == []


class NonSecretFailureAdapter(FakeModelAdapter):
    provider = "provider.non-secret"
    model = "model.non-secret-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        raise RuntimeError("token limit exceeded for this request")


def test_model_failure_reason_preserves_non_secret_detail(postgres_url: str) -> None:
    adapter = NonSecretFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="trigger non-secret failure")
        assert turn.status == "completed"

        timeline = _timeline(client)
        events = timeline["turns"][0]["events"]
        model_failed = next(event for event in events if event["event_type"] == "evt.model.failed")
        assert model_failed["payload"]["failure_reason"] == "token limit exceeded for this request"


def test_restart_preserves_history_and_appends_turns(postgres_url: str) -> None:
    adapter = DeterministicModelAdapter()
    with _build_client(postgres_url, adapter) as first_client:
        post_message_and_drain(first_client, message="before restart")

        timeline_before = _timeline(first_client)
        assert [turn["user_message"] for turn in timeline_before["turns"]] == ["before restart"]

    restarted_app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(restarted_app) as second_client:
        timeline_after_restart = _timeline(second_client)
        assert [turn["user_message"] for turn in timeline_after_restart["turns"]] == [
            "before restart"
        ]

        post_message_and_drain(second_client, message="after restart")

        final_timeline = _timeline(second_client)
        assert [turn["user_message"] for turn in final_timeline["turns"]] == [
            "before restart",
            "after restart",
        ]
