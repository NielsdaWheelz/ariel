from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariel.capability_registry import CapabilityDefinition
from ariel.persistence import EventRecord
from ariel.redaction import redact_json_value, safe_failure_reason


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: Literal["succeeded", "failed"]
    output: dict[str, Any] | None
    error: str | None


_UNSAFE_INPUT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("sql_dangerous", "drop table"),
    ("shell_dangerous", "rm -rf"),
)

_UNSAFE_OUTPUT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("script_tag", "<script"),
    ("javascript_uri", "javascript:"),
)

_EGRESS_SENTINEL_KEY = "__egress__"


def _iter_nested_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for nested_value in value.values():
            strings.extend(_iter_nested_strings(nested_value))
        return strings
    if isinstance(value, list):
        strings = []
        for nested_value in value:
            strings.extend(_iter_nested_strings(nested_value))
        return strings
    return []


def _detect_pattern(value: Any, patterns: tuple[tuple[str, str], ...]) -> str | None:
    for candidate in _iter_nested_strings(value):
        lowered = candidate.lower()
        for label, needle in patterns:
            if needle in lowered:
                return label
    return None


def _pre_execution_guardrail_error(
    *,
    capability: CapabilityDefinition,
    normalized_input: dict[str, Any],
) -> str | None:
    if capability.impact_level == "read":
        return None
    violation = _detect_pattern(normalized_input, _UNSAFE_INPUT_PATTERNS)
    if violation is None:
        return None
    return f"guardrail_pre_input_blocked:{violation}"


def _post_execution_guardrail_error(output_payload: Any) -> str | None:
    violation = _detect_pattern(output_payload, _UNSAFE_OUTPUT_PATTERNS)
    if violation is None:
        return None
    return f"guardrail_post_output_blocked:{violation}"


def _normalize_destination(destination: str) -> str | None:
    candidate = destination.strip().lower()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme:
        if parsed.hostname is None:
            return None
        return parsed.hostname.lower()

    # Supports host-only destinations while rejecting malformed URL-like values.
    if "://" in candidate:
        return None
    host = candidate.split("/", maxsplit=1)[0]
    if not host:
        return None
    if ":" in host:
        host = host.split(":", maxsplit=1)[0]
    return host or None


def _normalize_declared_egress_requests(
    raw_declarations: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    if raw_declarations is None:
        return [], "egress_preflight_missing_intent"
    if not isinstance(raw_declarations, list):
        return [], "egress_preflight_contract_invalid"
    if len(raw_declarations) == 0:
        return [], "egress_preflight_undeclared_intent"

    egress_requests: list[dict[str, Any]] = []
    for entry in raw_declarations:
        if not isinstance(entry, dict):
            return [], "egress_preflight_contract_invalid"
        destination_raw = entry.get("destination")
        if not isinstance(destination_raw, str) or not destination_raw.strip():
            return [], "egress_preflight_contract_invalid"
        if "payload" not in entry:
            return [], "egress_preflight_contract_invalid"
        payload_raw = entry.get("payload")
        if not isinstance(payload_raw, dict):
            return [], "egress_preflight_contract_invalid"
        egress_requests.append(
            {
                "destination": destination_raw.strip(),
                "payload": payload_raw,
            }
        )
    return egress_requests, None


def _egress_policy_error(
    *,
    capability: CapabilityDefinition,
    egress_requests: list[dict[str, Any]],
) -> str | None:
    if not egress_requests:
        return None
    allowed_destinations = {
        normalized
        for destination in capability.allowed_egress_destinations
        if (normalized := _normalize_destination(destination)) is not None
    }
    for request in egress_requests:
        requested_destination_raw = request["destination"]
        requested_destination = _normalize_destination(requested_destination_raw)
        if requested_destination is None:
            return "egress_destination_invalid"
        if requested_destination not in allowed_destinations:
            return f"egress_destination_denied:{requested_destination}"
    return None


def _preflight_egress_requests(
    *,
    capability: CapabilityDefinition,
    normalized_input: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    requires_preflight = capability.declare_egress_intent is not None or bool(
        capability.allowed_egress_destinations
    )
    if not requires_preflight:
        return [], None

    declare_egress_intent = capability.declare_egress_intent
    if declare_egress_intent is None:
        return [], "egress_preflight_missing_intent"
    if not callable(declare_egress_intent):
        return [], "egress_preflight_contract_invalid"

    try:
        raw_declarations = declare_egress_intent(normalized_input)
    except Exception:  # noqa: BLE001
        return [], "egress_preflight_contract_invalid"

    egress_requests, declaration_error = _normalize_declared_egress_requests(raw_declarations)
    if declaration_error is not None:
        return [], declaration_error

    egress_error = _egress_policy_error(capability=capability, egress_requests=egress_requests)
    if egress_error is not None:
        return [], egress_error
    return egress_requests, None


def _dispatch_egress_request(*, destination: str, payload: dict[str, Any]) -> str | None:
    # Centralized outbound dispatch boundary for external side effects.
    del destination, payload
    return None


def _dispatch_egress_requests(*, egress_requests: list[dict[str, Any]]) -> str | None:
    for request in egress_requests:
        destination = request.get("destination")
        payload = request.get("payload")
        if not isinstance(destination, str) or not isinstance(payload, dict):
            return "egress_dispatch_contract_invalid"
        try:
            dispatch_error = _dispatch_egress_request(destination=destination, payload=payload)
        except Exception as exc:  # noqa: BLE001
            safe_reason = safe_failure_reason(
                str(exc),
                fallback=f"unexpected {exc.__class__.__name__}",
            )
            return f"egress_dispatch_failed:{safe_reason}"
        if dispatch_error is not None:
            return f"egress_dispatch_failed:{dispatch_error}"
    return None


def build_assistant_action_appendix(
    *,
    inline_results: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    blocked_reasons: list[str],
) -> str:
    lines: list[str] = []
    for result in inline_results:
        capability_id = result["capability_id"]
        output = json.dumps(result["output"], sort_keys=True)
        lines.append(f"action result ({capability_id}): {output}")
    for pending in pending_approvals:
        lines.append(
            "approval required "
            f"({pending['capability_id']}): approval_ref={pending['approval_ref']} "
            f"expires_at={pending['expires_at']}"
        )
    if blocked_reasons:
        lines.append("blocked function calls: " + "; ".join(blocked_reasons))
    return "\n".join(lines)


def append_turn_event(
    *,
    db: Session,
    session_id: str,
    turn_id: str,
    sequence: int,
    event_type: str,
    payload_data: dict[str, Any],
    new_id_fn: Callable[[str], str],
    now_fn: Callable[[], datetime],
) -> EventRecord:
    event = EventRecord(
        id=new_id_fn("evn"),
        session_id=session_id,
        turn_id=turn_id,
        sequence=sequence,
        event_type=event_type,
        payload=jsonable_encoder(payload_data),
        created_at=now_fn(),
    )
    db.add(event)
    return event


def next_turn_event_sequence(*, db: Session, turn_id: str) -> int:
    max_sequence = db.scalar(
        select(func.max(EventRecord.sequence)).where(EventRecord.turn_id == turn_id)
    )
    if isinstance(max_sequence, int):
        return max_sequence + 1
    return 1


def execute_capability(
    *,
    capability: CapabilityDefinition,
    normalized_input: dict[str, Any],
) -> ExecutionResult:
    pre_guardrail_error = _pre_execution_guardrail_error(
        capability=capability,
        normalized_input=normalized_input,
    )
    if pre_guardrail_error is not None:
        return ExecutionResult(status="failed", output=None, error=pre_guardrail_error)

    egress_requests, egress_preflight_error = _preflight_egress_requests(
        capability=capability,
        normalized_input=normalized_input,
    )
    if egress_preflight_error is not None:
        return ExecutionResult(status="failed", output=None, error=egress_preflight_error)

    try:
        raw_output = capability.execute(normalized_input)
        if isinstance(raw_output, dict) and _EGRESS_SENTINEL_KEY in raw_output:
            return ExecutionResult(
                status="failed",
                output=None,
                error="egress_preflight_undeclared_intent",
            )

        post_guardrail_error = _post_execution_guardrail_error(raw_output)
        if post_guardrail_error is not None:
            return ExecutionResult(status="failed", output=None, error=post_guardrail_error)

        encoded_output = jsonable_encoder(raw_output)
        post_guardrail_error = _post_execution_guardrail_error(encoded_output)
        if post_guardrail_error is not None:
            return ExecutionResult(status="failed", output=None, error=post_guardrail_error)

        if capability.impact_level == "external_send":
            dispatch_error = _dispatch_egress_requests(egress_requests=egress_requests)
            if dispatch_error is not None:
                return ExecutionResult(status="failed", output=None, error=dispatch_error)

        redacted_output = redact_json_value(encoded_output)
        output_payload = (
            redacted_output if isinstance(redacted_output, dict) else {"value": redacted_output}
        )
        return ExecutionResult(status="succeeded", output=output_payload, error=None)
    except Exception as exc:  # noqa: BLE001
        error_reason = safe_failure_reason(
            str(exc),
            fallback=f"unexpected {exc.__class__.__name__}",
        )
        return ExecutionResult(status="failed", output=None, error=error_reason)
