from __future__ import annotations

import json

from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

from ariel.app import _build_initial_messages, _build_turn_context_bundle
from ariel.prompts import MAIN_AGENT_PROMPT_VERSION, MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS


def test_turn_context_bundle_uses_versioned_main_prompt() -> None:
    context = _build_turn_context_bundle(
        discord_context=None,
        recall_v1={"summary": "none", "items": [], "status": "complete"},
        open_commitments_and_jobs={"open_jobs": []},
        relevant_artifacts_and_observations={"artifacts": []},
    )

    assert context["prompt_version"] == MAIN_AGENT_PROMPT_VERSION
    assert context["section_order"] == [
        "policy_system_instructions",
        "recall_v1",
        "open_commitments_and_jobs",
        "relevant_artifacts_and_observations",
    ]
    assert context["policy_system_instructions"] == list(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)


def test_responses_input_items_keep_static_prompt_before_dynamic_context() -> None:
    context = _build_turn_context_bundle(
        discord_context={"message_id": 123, "channel_id": 456},
        recall_v1={"summary": "remember the launch", "items": [], "status": "complete"},
        open_commitments_and_jobs={
            "open_jobs": [{"id": "job_1", "status": "open", "title": "ship"}]
        },
        relevant_artifacts_and_observations={"artifacts": [{"title": "notes", "source": "drive"}]},
    )
    context["current_turn"] = {"turn_id": "trn_1", "user_instruction_ref": "turn:trn_1"}
    context["eligible_internal_callables"] = ["memory.search", "agent.emit_message"]
    context["tool_surface_facts"] = {"runtime_bindings": {"agency": False}}

    messages = _build_initial_messages(context_bundle=context, user_message="hello")

    # _build_initial_messages emits exactly one ModelRequest carrying the full
    # system-prompt prefix and the user turn.
    assert len(messages) == 1
    request = messages[0]
    assert isinstance(request, ModelRequest)
    parts = list(request.parts)

    static_count = len(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)
    system_parts = [p for p in parts if isinstance(p, SystemPromptPart)]
    assert [part.content for part in system_parts[:static_count]] == list(
        MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
    )
    user_parts = [p for p in parts if isinstance(p, UserPromptPart)]
    assert len(user_parts) == 1
    assert user_parts[0].content == "hello"

    dynamic_tail = json.dumps([p.content for p in system_parts[static_count:]], sort_keys=True)
    assert "discord context:" in dynamic_tail
    assert "syscall callables your run program may call this turn" in dynamic_tail
    assert "runtime facts:" in dynamic_tail
    assert "turn:trn_1" in dynamic_tail
    assert "memory recall:" in dynamic_tail
    assert "open jobs:" in dynamic_tail
    assert "recent artifacts:" in dynamic_tail
    assert "cap." not in dynamic_tail
