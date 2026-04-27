from __future__ import annotations

import copy
import json
from typing import Any

from ariel.capability_registry import response_tool_name_for_capability_id
from ariel.executor import build_assistant_action_appendix


def _assistant_text_from_function_outputs(
    *,
    input_items: list[dict[str, Any]],
    fallback_text: str,
) -> str:
    for item in reversed(input_items):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if item.get("role") == "system" and isinstance(content, str):
            prefix = "audited tool summary:\n"
            if content.startswith(prefix):
                return content.removeprefix(prefix)

    inline_results: list[dict[str, Any]] = []
    pending_approvals: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []

    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        raw_output = item.get("output")
        try:
            output = json.loads(raw_output) if isinstance(raw_output, str) else {}
        except ValueError:
            output = {}
        capability_id = output.get("capability_id")
        if not isinstance(capability_id, str):
            capability_id = "unknown"
        status = output.get("status")
        if status == "succeeded":
            inline_results.append(
                {
                    "capability_id": capability_id,
                    "output": output.get("output"),
                }
            )
        elif status == "approval_required":
            pending_approvals.append(
                {
                    "capability_id": capability_id,
                    "approval_ref": output.get("approval_ref", ""),
                    "expires_at": output.get("expires_at", ""),
                }
            )
        elif status == "blocked":
            blocked_reasons.append(f"{capability_id}: {output.get('reason', 'blocked')}")
        elif status == "failed":
            blocked_reasons.append(f"{capability_id}: {output.get('error', 'failed')}")

    appendix = build_assistant_action_appendix(
        inline_results=inline_results,
        pending_approvals=pending_approvals,
        blocked_reasons=blocked_reasons,
    )
    return appendix or fallback_text


def responses_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            }
        ],
    }


def responses_with_function_calls(
    *,
    input_items: list[dict[str, Any]],
    assistant_text: str,
    proposals: list[dict[str, Any]],
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
    if any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in input_items):
        return responses_message(
            assistant_text=_assistant_text_from_function_outputs(
                input_items=input_items,
                fallback_text=assistant_text,
            ),
            provider=provider,
            model=model,
            provider_response_id=provider_response_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    output: list[dict[str, Any]] = []
    if assistant_text:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            }
        )
    for index, proposal in enumerate(copy.deepcopy(proposals), start=1):
        capability_id = proposal.get("capability_id")
        tool_name = (
            response_tool_name_for_capability_id(capability_id)
            if isinstance(capability_id, str)
            else "invalid_capability"
        )
        raw_input = proposal.get("input")
        arguments = raw_input if isinstance(raw_input, dict) else {}
        function_call = {
            "type": "function_call",
            "id": f"fc_test_{index}",
            "call_id": f"call_test_{index}",
            "name": tool_name,
            "arguments": json.dumps(arguments, sort_keys=True),
            "status": "completed",
        }
        if "influenced_by_untrusted_content" in proposal:
            function_call["influenced_by_untrusted_content"] = proposal[
                "influenced_by_untrusted_content"
            ]
        output.append(function_call)
    return {
        "provider": provider,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "provider_response_id": provider_response_id,
        "output": output,
    }
