from __future__ import annotations

import json

from ariel.app import _build_responses_input_items, _build_turn_context_bundle
from ariel.prompts import MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS


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

    input_items = _build_responses_input_items(context_bundle=context, user_message="hello")
    static_count = len(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)

    assert [item["content"] for item in input_items[:static_count]] == list(
        MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
    )
    assert input_items[-1] == {"role": "user", "content": "hello"}

    dynamic_tail = json.dumps(input_items[static_count:], sort_keys=True)
    assert "discord context:" in dynamic_tail
    assert "syscall callables your run program may call this turn" in dynamic_tail
    assert "runtime facts:" in dynamic_tail
    assert "turn:trn_1" in dynamic_tail
    assert "memory recall:" in dynamic_tail
    assert "open jobs:" in dynamic_tail
    assert "recent artifacts:" in dynamic_tail
    assert "cap." not in dynamic_tail
