from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal

PolicyDecision = Literal["allow_inline", "requires_approval", "deny"]


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    impact_level: str
    policy_decision: PolicyDecision
    validate_input: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str | None]]
    execute: Callable[[dict[str, Any]], dict[str, Any]]


def _validate_exact_text_input(
    raw_input: dict[str, Any],
    *,
    field_name: str,
    max_length: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {field_name}:
        return None, "schema_invalid"
    value = raw_input.get(field_name)
    if not isinstance(value, str):
        return None, "schema_invalid"
    normalized = value.strip()
    if not normalized:
        return None, "schema_invalid"
    if len(normalized) > max_length:
        return None, "schema_invalid"
    return {field_name: normalized}, None


def _validate_read_echo_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="text", max_length=4000)


def _validate_read_private_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="text", max_length=4000)


def _validate_write_note_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="note", max_length=500)


def _execute_read_echo(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"text": input_payload["text"]}


def _execute_read_private(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"text": input_payload["text"], "classification": "private"}


def _execute_write_note(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": "recorded", "note": input_payload["note"]}


_CAPABILITY_REGISTRY: dict[str, CapabilityDefinition] = {
    "cap.framework.read_echo": CapabilityDefinition(
        capability_id="cap.framework.read_echo",
        impact_level="read",
        policy_decision="allow_inline",
        validate_input=_validate_read_echo_input,
        execute=_execute_read_echo,
    ),
    "cap.framework.read_private": CapabilityDefinition(
        capability_id="cap.framework.read_private",
        impact_level="read",
        policy_decision="deny",
        validate_input=_validate_read_private_input,
        execute=_execute_read_private,
    ),
    "cap.framework.write_note": CapabilityDefinition(
        capability_id="cap.framework.write_note",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        validate_input=_validate_write_note_input,
        execute=_execute_write_note,
    ),
}


def get_capability(capability_id: str) -> CapabilityDefinition | None:
    return _CAPABILITY_REGISTRY.get(capability_id)


def canonical_action_payload(*, capability_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"capability_id": capability_id, "input": input_payload}


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
