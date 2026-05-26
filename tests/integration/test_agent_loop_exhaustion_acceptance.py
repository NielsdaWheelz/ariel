from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
    responses_run_message,
    responses_with_run_calls,
    run_response,
)


def _timeline(client: TestClient) -> dict[str, Any]:
    resp = client.get("/v1/events")
    assert resp.status_code == 200
    return resp.json()


class DirectResponseAdapter(FakeModelAdapter):
    provider = "provider.direct-response"
    model = "model.direct-response-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        return responses_run_message(
            assistant_text="ok",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_direct_123",
            input_tokens=2,
            output_tokens=2,
        )


class RetryableFailureAdapter(FakeModelAdapter):
    provider = "provider.retryable-failure"
    model = "model.retryable-failure-v1"

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.attempts += 1
        exc = RuntimeError("temporary provider timeout")
        # The agent loop's retry path keys on a truthy ``retryable`` attribute
        # on the raised exception. Set it so the main loop retries this fake.
        exc.retryable = True  # type: ignore[attr-defined]
        raise exc


class RepeatingRunAdapter(FakeModelAdapter):
    provider = "provider.repeating-run"
    model = "model.repeating-run-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        return responses_with_run_calls(
            calls=[{"name": "agent.emit_value", "input": {"value": {"x": 1}}}],
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_repeating_run_123",
            input_tokens=1,
            output_tokens=1,
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def test_model_call_backstop_exhaustion_ends_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "1")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_SOFT", "1")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_HARD", "1")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_SOFT", "300.0")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_HARD", "300.0")
    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="trigger model call backstop")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in timeline["turns"])
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types
        assert "evt.assistant.emitted" in event_types


def test_turn_budget_exhaustion_ends_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_SOFT", "0.001")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_HARD", "0.001")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_SOFT", "100")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_HARD", "100")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.1
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = DirectResponseAdapter()
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="trigger turn budget")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        assert not any(saved_turn["status"] == "in_progress" for saved_turn in timeline["turns"])
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types


def test_stuck_detection_ends_turn_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_SOFT", "300.0")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_HARD", "300.0")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_SOFT", "100")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_HARD", "100")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = RepeatingRunAdapter()
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="trigger stuck detection")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types


class WrapUpNudgeAdapter(FakeModelAdapter):
    """Two non-terminal rounds, then a terminal message after the wrap-up nudge
    arrives in the message stream."""

    provider = "provider.wrap-up"
    model = "model.wrap-up-v1"

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count = 0
        self.saw_wrap_up_nudge = False

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.main_call_count += 1
        for msg in request.messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if (
                        isinstance(part, SystemPromptPart)
                        and "past your soft turn budget" in part.content
                    ):
                        self.saw_wrap_up_nudge = True
        if self.saw_wrap_up_nudge:
            return run_response(
                source="agent.emit_message(text='wrapping up with what i have')\n",
                provider=self.provider,
                model=self.model,
                provider_response_id=f"resp_wrap_call_{self.main_call_count}",
            )
        return run_response(
            source=f"agent.emit_value(value={{'x': {self.main_call_count}}})\n",
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_emit_value_call_{self.main_call_count}",
        )


def test_soft_budget_injects_wrap_up_nudge_and_completes_with_message(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_SOFT", "300.0")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_HARD", "300.0")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_SOFT", "1")
    monkeypatch.setenv("ARIEL_TURN_MAX_MODEL_CALLS_HARD", "10")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = WrapUpNudgeAdapter()
    with _build_client(postgres_url, adapter) as client:
        turn = post_message_and_drain(client, message="trigger soft nudge")
        assert turn.status == "completed"
        assert turn.assistant_message == "wrapping up with what i have"

        timeline = _timeline(client)
        turn_data = timeline["turns"][0]
        nudge_events = [
            e for e in turn_data["events"] if e["event_type"] == "evt.agent.wrap_up_nudged"
        ]
        assert len(nudge_events) == 1
        nudge_payload = nudge_events[0]["payload"]
        assert nudge_payload["soft_max_model_calls"] == 1
        assert nudge_payload["model_call_count"] >= 2
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.assistant.emitted" in event_types
        assert adapter.saw_wrap_up_nudge is True
