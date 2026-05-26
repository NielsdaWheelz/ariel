from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from ariel.prompts import MAIN_AGENT_PROMPT_VERSION, MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
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


def _timeline(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    return resp.json()


class PromptContextAdapter(FakeModelAdapter):
    provider = "provider.prompt-context"
    model = "model.prompt-context-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        user_message = last_user_message(request.messages)

        if user_message.strip().lower() == "book me travel":
            assistant_text = "i need your destination and travel dates before i can plan this trip."
        else:
            assistant_text = f"direct::{user_message}"

        return responses_run_message(
            assistant_text=assistant_text,
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_prompt_context_123",
            input_tokens=9,
            output_tokens=11,
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def test_model_led_direct_and_clarification_messages_are_emitted(postgres_url: str) -> None:
    adapter = PromptContextAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        clear_turn = post_message_and_drain(
            client, session_id, message="summarize this in one line"
        )
        assert clear_turn.assistant_message is not None
        assert clear_turn.assistant_message.startswith("direct::")

        ambiguous_turn = post_message_and_drain(client, session_id, message="book me travel")
        assert ambiguous_turn.assistant_message is not None
        assert "destination and travel dates" in ambiguous_turn.assistant_message

        timeline = _timeline(client, session_id)
        event_types_by_turn = [
            [event["event_type"] for event in turn["events"]] for turn in timeline["turns"]
        ]
        assert all("evt.assistant.emitted" in event_types for event_types in event_types_by_turn)
        assert all("evt.turn.completed" in event_types for event_types in event_types_by_turn)


def test_turn_context_section_order_and_audit_metadata(
    postgres_url: str,
) -> None:
    """Context bundle section_order and audit metadata follow the recall-based schema.

    Every turn receives the same four sections and a zero recent_window. The
    main agent's evt.model.started carries the pre-call snapshot.
    """
    adapter = PromptContextAdapter()

    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]

        post_message_and_drain(client, session_id, message="first turn")
        post_message_and_drain(client, session_id, message="second turn")

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert len(turns) == 2

        # Retriever evt.model.started events happen before the turn context is built.
        # The main agent's evt.model.started is the last one; it carries the real snapshot.
        for turn_data in turns:
            model_started_events = [
                e for e in turn_data["events"] if e["event_type"] == "evt.model.started"
            ]
            main_agent_started = model_started_events[-1]
            context_meta = main_agent_started["payload"]["context"]
            assert context_meta["schema_version"] == "1.0"
            assert context_meta["prompt_version"] == MAIN_AGENT_PROMPT_VERSION
            assert context_meta["section_order"] == [
                "policy_system_instructions",
                "recall_v1",
                "recent_events",
                "open_commitments_and_jobs",
                "relevant_artifacts_and_observations",
            ]
            assert context_meta["policy_instruction_count"] == len(
                MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
            )
            # Bounded session-turn window is gone; recent_window is always zero.
            assert context_meta["recent_window"] == {
                "max_recent_turns": 0,
                "included_turn_count": 0,
                "omitted_turn_count": 0,
                "included_turn_ids": [],
            }
