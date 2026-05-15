from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .capability_registry import capability_id_for_run_callable


_MAX_RUN_SOURCE_CHARS = 20000
_MAX_RUN_CALLS = 20


@dataclass(frozen=True, slots=True)
class RunEffects:
    emitted_message: str
    paused: bool
    emitted_values: list[Any]
    function_calls: list[dict[str, Any]]
    errors: list[str]


def run_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "run",
            "description": (
                "Execute one small Ariel run program. The source is JSON with a calls list. "
                "Use agent.emit_message for user-visible output, terminal.run for commands, "
                "and typed host callables such as memory.search, email.search, or agency.run."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20000,
                    }
                },
                "required": ["source"],
            },
            "strict": True,
        }
    ]


def parse_run_function_call(
    function_calls: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    if len(function_calls) != 1:
        return None, "run_protocol_requires_exactly_one_tool_call"
    function_call = function_calls[0]
    if function_call.get("name") != "run":
        return None, "run_protocol_requires_run_tool"
    raw_arguments = function_call.get("arguments")
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
    except ValueError:
        return None, "run_arguments_invalid_json"
    if set(arguments.keys()) != {"source"} or not isinstance(arguments.get("source"), str):
        return None, "run_arguments_schema_invalid"
    source = arguments["source"].strip()
    if not source:
        return None, "run_source_empty"
    if len(source) > _MAX_RUN_SOURCE_CHARS:
        return None, "run_source_too_large"
    return source, None


def unpack_run_source(source: str) -> RunEffects:
    try:
        program = json.loads(source)
    except ValueError:
        return RunEffects("", False, [], [], ["run_source_invalid_json"])
    if not isinstance(program, dict) or set(program.keys()) != {"calls"}:
        return RunEffects("", False, [], [], ["run_source_schema_invalid"])
    raw_calls = program.get("calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return RunEffects("", False, [], [], ["run_source_calls_required"])
    if len(raw_calls) > _MAX_RUN_CALLS:
        return RunEffects("", False, [], [], ["run_source_too_many_calls"])

    emitted_message = ""
    paused = False
    emitted_values: list[Any] = []
    function_calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_call in enumerate(raw_calls, start=1):
        if not isinstance(raw_call, dict) or set(raw_call.keys()) != {"name", "input"}:
            errors.append(f"call_{index}_schema_invalid")
            continue
        name = raw_call.get("name")
        input_payload = raw_call.get("input")
        if not isinstance(name, str) or not name.strip() or not isinstance(input_payload, dict):
            errors.append(f"call_{index}_schema_invalid")
            continue
        name = name.strip()
        if name == "agent.emit_message":
            if emitted_message:
                errors.append("agent_emit_message_must_be_unique")
                continue
            text = input_payload.get("text")
            if (
                set(input_payload.keys()) != {"text"}
                or not isinstance(text, str)
                or not text.strip()
            ):
                errors.append("agent_emit_message_schema_invalid")
                continue
            emitted_message = text.strip()
            continue
        if name == "agent.pause_until_input":
            if input_payload:
                errors.append("agent_pause_until_input_schema_invalid")
                continue
            paused = True
            continue
        if name == "agent.emit_value":
            if set(input_payload.keys()) != {"value"}:
                errors.append("agent_emit_value_schema_invalid")
                continue
            try:
                encoded_value = json.dumps(input_payload["value"], sort_keys=True)
            except TypeError:
                errors.append("agent_emit_value_schema_invalid")
                continue
            if len(emitted_values) >= 10 or len(encoded_value) > 12000:
                errors.append("agent_emit_value_too_large")
                continue
            emitted_values.append(input_payload["value"])
            continue

        if name.startswith("cap."):
            errors.append(f"{name}: capability_ids_are_not_run_callables")
            continue
        capability_id = capability_id_for_run_callable(name)
        if capability_id is None:
            errors.append(f"{name}: unknown_callable")
            continue
        function_calls.append(
            {
                "call_id": f"run_call_{index}",
                "tool_name": name,
                "capability_id": capability_id,
                "input": input_payload,
            }
        )

    if not emitted_message and not paused and not emitted_values and not function_calls:
        errors.append("run_source_no_effect")
    if paused and (emitted_message or emitted_values or function_calls):
        errors.append("agent_pause_until_input_must_be_only_effect")
    if emitted_message and function_calls:
        errors.append("agent_emit_message_must_not_share_run_with_internal_calls")
    if emitted_message and emitted_values:
        errors.append("agent_emit_message_must_not_share_run_with_emit_value")
    if errors:
        return RunEffects("", False, [], [], errors)
    return RunEffects(emitted_message, paused, emitted_values, function_calls, errors)
