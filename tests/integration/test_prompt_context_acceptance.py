from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from ariel.app import ModelAdapter
from ariel.prompts import MAIN_AGENT_PROMPT_VERSION, MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_migrated_app
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
    responses_run_message,
)


def _timeline(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    return resp.json()


@dataclass
class PromptContextAdapter:
    provider: str = "provider.prompt-context"
    model: str = "model.prompt-context-v1"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)

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
        del tools, history
        self.context_bundles.append(context_bundle)

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


@dataclass
class MutatingContextAdapter:
    provider: str = "provider.mutating"
    model: str = "model.mutating-v1"

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
        del tools, user_message, history
        section_order = context_bundle.get("section_order")
        if isinstance(section_order, list):
            section_order.append("mutated")
        recent_window = context_bundle.get("recent_window")
        if isinstance(recent_window, dict):
            recent_window["included_turn_count"] = 999
            recent_window["included_turn_ids"] = ["mutated"]

        return responses_run_message(
            assistant_text="mutating-adapter-response",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_mutating_123",
            input_tokens=3,
            output_tokens=3,
        )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_migrated_app(
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

        assert len(adapter.context_bundles) == 2
        for context_bundle in adapter.context_bundles:
            assert context_bundle["prompt_version"] == MAIN_AGENT_PROMPT_VERSION
            assert context_bundle["section_order"] == [
                "policy_system_instructions",
                "recall_v1",
                "open_commitments_and_jobs",
                "relevant_artifacts_and_observations",
            ]
            # recall_v1 is always present (populated by the retriever).
            assert "recall_v1" in context_bundle
            assert context_bundle["policy_system_instructions"] == list(
                MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
            )

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        assert len(turns) == 2

        # The retriever's evt.model.started carries the empty sentinel (section_order=[]).
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


def test_context_audit_is_stable_even_if_adapter_mutates_context_bundle(
    postgres_url: str,
) -> None:
    adapter = MutatingContextAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        post_message_and_drain(client, session_id, message="seed history")
        post_message_and_drain(client, session_id, message="mutate context")

        timeline = _timeline(client, session_id)
        turns = timeline["turns"]
        # The retriever fires first; its model.started carries the empty sentinel
        # context meta. The main agent's model.started is the last evt.model.started
        # in the turn; it carries the real pre-call snapshot, stable even though
        # MutatingContextAdapter appends "mutated" to section_order after the fact.
        model_started_events = [
            event for event in turns[1]["events"] if event["event_type"] == "evt.model.started"
        ]
        main_agent_started = model_started_events[-1]
        context_meta = main_agent_started["payload"]["context"]
        assert context_meta["schema_version"] == "1.0"
        assert context_meta["prompt_version"] == MAIN_AGENT_PROMPT_VERSION
        # The audit snapshot preserves the recall-based context sections.
        assert context_meta["section_order"] == [
            "policy_system_instructions",
            "recall_v1",
            "open_commitments_and_jobs",
            "relevant_artifacts_and_observations",
        ]
        # "mutated" must NOT appear: the audit snapshot was taken before the adapter ran.
        assert "mutated" not in context_meta["section_order"]
        # recent_window is always zero: live turn context is reconstructed by
        # recall_v1, not a bounded transcript window.
        assert context_meta["recent_window"] == {
            "max_recent_turns": 0,
            "included_turn_count": 0,
            "omitted_turn_count": 0,
            "included_turn_ids": [],
        }
