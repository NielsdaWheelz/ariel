"""The recent-events block surfaced into every wake's initial context.

The agent reads its own recent history through this block: the most recent
externally-relevant events from the durable log, chronologically ordered
oldest-first, capped by token budget. Selection is content-agnostic: a
structural whitelist of event_types representing world/conversation state
changes (excluding loop trace), plus a per-event byte cap that recursively
compacts oversized payloads while preserving canonical IDs the agent uses
to re-fetch via existing capabilities.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import AppSettings
from .persistence import EventRecord


# event_types that represent state changes in the world or the conversation.
# Loop-internal events (model timing, action.proposed, policy_decided, started
# markers, intra-turn emit_value, ai_judgment internals, connector lifecycle
# noise) are excluded — the next-turn agent has no use for them. New event_types
# representing real state changes get added here.
EXTERNAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "evt.turn.started",
        "evt.turn.completed",
        "evt.turn.failed",
        "evt.assistant.emitted",
        "evt.action.execution.succeeded",
        "evt.action.execution.failed",
        "evt.action.approval.requested",
        "evt.action.approval.approved",
        "evt.action.approval.denied",
        "evt.action.approval.expired",
        "evt.action.call_denied",
        "evt.run.validation_failed",
        "evt.research.finding_emitted",
        "evt.research.failed",
        "evt.research.partial",
        "evt.connector.google.disconnected",
        "evt.connector.google.reconnect.succeeded",
        "evt.model.failed",
        "evt.model.protocol_failed",
        "evt.provider_write.receipt_reconciled",
        "evt.memory.recalled",
    }
)


_BLOCK_HEADER = (
    "recent_external_events (last K events you have access to, chronological, "
    "oldest first; loop-trace events are filtered out at the system level). "
    "each line is one event row as JSON: {id, created_at, turn_id, event_type, payload}. "
    "use canonical IDs in payloads to re-fetch full content via existing capabilities "
    "(email.read, calendar.list, drive.read, etc.)."
)

_ID_KEYS = frozenset({"id", "provider_object_ids", "provider_event_ref"})
_LONG_STRING_LIMIT = 200
_LONG_STRING_PREVIEW = 80
_LARGE_LIST_LIMIT = 50


def _walk_compact(value: Any) -> Any:
    """Recursively compact a value: preserve scalars and canonical IDs verbatim,
    summarize long strings and lists. Used when a payload exceeds the per-event
    byte cap; idempotent on small values."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        if len(value) <= _LONG_STRING_LIMIT:
            return value
        return {
            "_truncated_str": True,
            "_byte_size": len(value.encode("utf-8")),
            "_preview": value[:_LONG_STRING_PREVIEW],
        }
    if isinstance(value, list):
        if len(value) > _LARGE_LIST_LIMIT:
            return {
                "_kind": "list",
                "_size": len(value),
                "_sampled": [_walk_compact(v) for v in value[:_LARGE_LIST_LIMIT]],
            }
        return [_walk_compact(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if key in _ID_KEYS or key.endswith("_id") or key.endswith("_ids"):
                out[key] = sub
            else:
                out[key] = _walk_compact(sub)
        return out
    return value


def _compact_event_payload(payload: dict[str, Any], *, cap: int) -> dict[str, Any]:
    """Return ``payload`` unchanged when its serialized size is at or under ``cap``
    bytes. Above cap, return a structurally compacted view that preserves
    canonical IDs at any nesting depth and summarizes long content."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= cap:
        return payload
    walked = _walk_compact(payload)
    if not isinstance(walked, dict):
        return {"_truncated": True, "_root_kind": type(walked).__name__}
    return {**walked, "_truncated": True}


def build_recent_events_block(
    *,
    db: Session,
    settings: AppSettings,
) -> str | None:
    """Render the recent-events system block. ``None`` when the log is empty.

    Selection is purely recency-bounded: the most recent K externally-relevant
    events globally, ordered chronologically. There is no session filter.
    """
    rows = (
        db.execute(
            select(EventRecord)
            .where(EventRecord.event_type.in_(tuple(EXTERNAL_EVENT_TYPES)))
            .order_by(EventRecord.created_at.desc(), EventRecord.sequence.desc())
            .limit(settings.recent_events_max_rows)
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    rows = list(reversed(rows))  # chronological, oldest first
    budget_bytes = settings.recent_events_token_budget * 4  # ~4 bytes/token (English/JSON)
    per_event_cap = settings.recent_event_payload_byte_cap

    lines: list[str] = []
    total_bytes = 0
    for row in rows:
        raw_payload = row.payload if isinstance(row.payload, dict) else {}
        payload_view = _compact_event_payload(raw_payload, cap=per_event_cap)
        line = json.dumps(
            {
                "id": row.id,
                "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
                "turn_id": row.turn_id,
                "event_type": row.event_type,
                "payload": payload_view,
            },
            separators=(",", ":"),
        )
        # If a single event still exceeds the cap after walk-compaction, fall back
        # to bare metadata: the agent learns the event existed and can re-fetch
        # via the standard tool surface using turn_id / event_type semantics.
        if len(line.encode("utf-8")) > per_event_cap:
            line = json.dumps(
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
                    "turn_id": row.turn_id,
                    "event_type": row.event_type,
                    "payload": {"_oversize": True},
                },
                separators=(",", ":"),
            )
        lines.append(line)
        total_bytes += len(line.encode("utf-8")) + 1  # +1 newline

    # Evict oldest events until under budget; keep at least the most recent line
    # so the agent at minimum sees its current turn's own evt.turn.started.
    while total_bytes > budget_bytes and len(lines) > 1:
        dropped = lines.pop(0)
        total_bytes -= len(dropped.encode("utf-8")) + 1

    return _BLOCK_HEADER + "\n" + "\n".join(lines)
