from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from ariel.agent_loop import LoopConfig, run_agent_loop
from ariel.config import AppSettings
from ariel.model_adapter import ModelAdapter, ModelAdapterError
from ariel.persistence import SessionRecord, TurnRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
    responses_run_message,
    responses_with_run_calls,
)


def _timeline(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    return resp.json()


@dataclass
class DirectResponseAdapter:
    provider: str = "provider.direct-response"
    model: str = "model.direct-response-v1"

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
        return responses_run_message(
            assistant_text="ok",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_direct_123",
            input_tokens=2,
            output_tokens=2,
        )


@dataclass
class RetryableFailureAdapter:
    provider: str = "provider.retryable-failure"
    model: str = "model.retryable-failure-v1"
    attempts: int = 0

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
        self.attempts += 1
        raise ModelAdapterError(
            safe_reason="temporary provider timeout",
            status_code=502,
            code="E_MODEL_FAILURE",
            message="model provider request failed",
            retryable=True,
        )


@dataclass
class DefectiveAdapter:
    provider: str = "provider.defective"
    model: str = "model.defective-v1"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history, context_bundle
        raise RuntimeError("adapter bug")


class RepeatingRunAdapter:
    provider: str = "provider.repeating-run"
    model: str = "model.repeating-run-v1"

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
        del tools, user_message, history, context_bundle, input_items
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


def test_unexpected_model_adapter_exception_propagates_as_defect(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            session = SessionRecord(
                id="ses_model_defect",
                is_active=True,
                lifecycle_state="active",
                rotated_from_session_id=None,
                rotation_reason=None,
                created_at=now,
                updated_at=now,
            )
            turn = TurnRecord(
                id="trn_model_defect",
                session_id=session.id,
                user_message="trigger adapter defect",
                assistant_message=None,
                status="in_progress",
                kind="agent_turn",
                created_at=now,
                updated_at=now,
            )
            db.add_all([session, turn])

        events: list[dict[str, Any]] = []
        sandbox = FakeSandboxRuntime()
        sandbox.start()
        try:
            with pytest.raises(RuntimeError, match="adapter bug"):
                run_agent_loop(
                    LoopConfig(
                        output_mode="message",
                        finding_mode="",
                        prompt_version="test",
                        budget_seconds=60.0,
                        max_model_calls=3,
                        is_main_agent_loop=True,
                        record_judgments=False,
                        judgment_type=None,
                        retry_on_model_error=True,
                        void_failed_program_approvals=True,
                        protocol_nudge="retry with a run call",
                        program_failure_nudge="fix the program",
                        action_trace_nudge="emit a terminal result",
                        emit_value_nudge="emit a terminal result",
                        no_terminal_output_nudge="emit a terminal result",
                    ),
                    sandbox=sandbox,
                    db=db,
                    session_factory=session_factory,
                    session_id=session.id,
                    turn=turn,
                    settings=AppSettings(),
                    model_adapter=DefectiveAdapter(),
                    responses_input_items=[{"role": "system", "content": "test"}],
                    tools=[],
                    user_message=turn.user_message,
                    history=[],
                    context_bundle={},
                    allowed_capability_ids=frozenset(),
                    scratch={},
                    proposal_index_start=0,
                    approval_ttl_seconds=60,
                    approval_actor_id="test",
                    add_event=lambda event_type, payload: events.append(
                        {"event_type": event_type, "payload": payload}
                    ),
                    now_fn=lambda: now,
                    new_id_fn=lambda prefix: f"{prefix}_model_defect",
                    runtime_provenance=None,
                    google_runtime=None,
                    execute_google_reads_outside_transaction=False,
                    agency_runtime=None,
                    attachment_runtime=None,
                )
        finally:
            sandbox.close()

    assert [event["event_type"] for event in events] == ["evt.model.started"]


def test_model_call_backstop_exhaustion_ends_gracefully(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "1")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    adapter = RetryableFailureAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger model call backstop")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client, session_id)
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
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "0.001")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.1
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = DirectResponseAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger turn budget")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client, session_id)
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
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = RepeatingRunAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="trigger stuck detection")
        assert turn.status == "completed"
        assert turn.assistant_message == "I wasn't able to finish that within the time available."

        timeline = _timeline(client, session_id)
        turn_data = timeline["turns"][0]
        assert turn_data["status"] == "completed"
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.turn.failed" not in event_types
        assert "evt.turn.completed" in event_types
