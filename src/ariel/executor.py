from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ariel.capability_registry import CapabilityDefinition
from ariel.persistence import EventRecord


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: Literal["succeeded", "failed"]
    output: dict[str, Any] | None
    error: str | None


_SECRET_LIKE_PATTERN = re.compile(
    (
        r"(sk-[A-Za-z0-9_\-]{8,}"
        r"|api[_-]?key"
        r"|secret(?:[_-]?(?:key|value))?"
        r"|authorization"
        r"|bearer\s+[A-Za-z0-9\-_.]+"
        r"|token\s*[:=]\s*[A-Za-z0-9\-_.]+)"
    ),
    re.IGNORECASE,
)


def safe_failure_reason(raw_message: str, *, fallback: str) -> str:
    candidate = raw_message.strip()
    if not candidate:
        return fallback
    if _SECRET_LIKE_PATTERN.search(candidate):
        return fallback
    return candidate[:500]


def redact_text(value: str) -> str:
    return _SECRET_LIKE_PATTERN.sub("[REDACTED]", value)


def redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_json_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    return value


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
            f"({pending['capability_id']}): approval_id={pending['approval_id']} "
            f"expires_at={pending['expires_at']}"
        )
    if blocked_reasons:
        lines.append("blocked action proposals: " + "; ".join(blocked_reasons))
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
    max_sequence = db.scalar(select(func.max(EventRecord.sequence)).where(EventRecord.turn_id == turn_id))
    if isinstance(max_sequence, int):
        return max_sequence + 1
    return 1


def execute_capability(
    *,
    capability: CapabilityDefinition,
    normalized_input: dict[str, Any],
) -> ExecutionResult:
    try:
        raw_output = capability.execute(normalized_input)
        encoded_output = jsonable_encoder(raw_output)
        redacted_output = redact_json_value(encoded_output)
        output_payload = redacted_output if isinstance(redacted_output, dict) else {"value": redacted_output}
        return ExecutionResult(status="succeeded", output=output_payload, error=None)
    except Exception as exc:  # noqa: BLE001
        error_reason = safe_failure_reason(
            str(exc),
            fallback=f"unexpected {exc.__class__.__name__}",
        )
        return ExecutionResult(status="failed", output=None, error=error_reason)
