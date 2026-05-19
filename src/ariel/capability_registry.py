from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from ipaddress import ip_address
from math import asin, cos, radians, sin, sqrt
import hashlib
import json
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx
from .config import AppSettings
from web_search_tool.brave import BraveSearchProvider
from web_search_tool.types import (
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchResultItem,
    WebSearchResultType,
)

PolicyDecision = Literal["allow_inline", "requires_approval", "deny"]

_GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
_GOOGLE_CALENDAR_FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
_GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GOOGLE_GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
_GOOGLE_GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_GOOGLE_DRIVE_METADATA_READ_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
_GOOGLE_DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_GOOGLE_DRIVE_SHARE_SCOPE = "https://www.googleapis.com/auth/drive"
_GOOGLE_ALLOWED_EGRESS_DESTINATIONS = (
    "www.googleapis.com",
    "gmail.googleapis.com",
    "oauth2.googleapis.com",
)
_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS = ("gmail.googleapis.com",)
AGENCY_CAPABILITY_IDS = {
    "cap.agency.run",
    "cap.agency.status",
    "cap.agency.artifacts",
    "cap.agency.request_pr",
}
DISCORD_CAPABILITY_IDS: set[str] = set()
ATTACHMENT_CAPABILITY_IDS = {"cap.attachment.read"}
EMAIL_MUTATION_CAPABILITY_IDS = {
    "cap.email.archive",
    "cap.email.trash",
    "cap.email.labels.modify",
    "cap.email.undo",
}
MEMORY_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "cap.memory.recall",
        "cap.memory.remember",
        "cap.memory.search",
        "cap.memory.read",
        "cap.memory.note.create",
        "cap.memory.note.edit",
        "cap.memory.note.delete",
    }
)
PROACTIVE_CAPABILITY_IDS = {"cap.proactive.schedule"}
MAPS_CAPABILITY_IDS = {"cap.maps.directions", "cap.maps.search_places"}
RESEARCH_CAPABILITY_IDS = {"cap.research.investigate"}
RESEARCH_WEB_CAPABILITY_IDS: frozenset[str] = frozenset(
    {"cap.search.web", "cap.search.news", "cap.web.extract"}
)
RESEARCH_PERSONAL_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "cap.email.search",
        "cap.email.read",
        "cap.drive.search",
        "cap.drive.read",
        "cap.calendar.list",
    }
)
RESEARCH_MEMORIES_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "cap.memory.search",
        "cap.memory.read",
    }
)
REMEMBERER_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "cap.memory.search",
        "cap.memory.read",
        "cap.memory.note.create",
        "cap.memory.note.edit",
        "cap.memory.note.delete",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    version: str
    impact_level: str
    policy_decision: PolicyDecision
    contract_metadata: dict[str, Any]
    allowed_egress_destinations: tuple[str, ...]
    validate_input: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str | None]]
    execute: Callable[[dict[str, Any]], dict[str, Any]] | None
    declare_egress_intent: Callable[[dict[str, Any]], list[dict[str, Any]] | None] | None = None


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


def _validate_search_web_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


def _validate_attachment_read_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {"attachment_ref", "intent"}:
        return None, "schema_invalid"
    attachment_ref = raw_input.get("attachment_ref")
    intent = raw_input.get("intent")
    if not isinstance(attachment_ref, str) or not isinstance(intent, str):
        return None, "schema_invalid"
    normalized_ref = attachment_ref.strip()
    normalized_intent = intent.strip().lower()
    if (
        not normalized_ref
        or len(normalized_ref) > 256
        or "://" in normalized_ref
        or "/" in normalized_ref
        or "\\" in normalized_ref
    ):
        return None, "schema_invalid"
    if normalized_intent not in {"summarize", "ocr", "transcribe", "extract_text", "answer"}:
        return None, "schema_invalid"
    return {"attachment_ref": normalized_ref, "intent": normalized_intent}, None


def _validate_search_news_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


def _validate_web_extract_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="url", max_length=2048)


def _normalize_rfc3339_like(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_calendar_list_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {"window_start", "window_end"}:
        return None, "schema_invalid"
    window_start = _normalize_rfc3339_like(raw_input.get("window_start"))
    window_end = _normalize_rfc3339_like(raw_input.get("window_end"))
    if window_start is None or window_end is None:
        return None, "schema_invalid"
    window_start_dt = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    window_end_dt = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
    if window_end_dt <= window_start_dt:
        return None, "schema_invalid"
    return {
        "window_start": window_start,
        "window_end": window_end,
    }, None


def _validate_calendar_propose_slots_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {
        "window_start",
        "window_end",
        "duration_minutes",
        "attendees",
        "timezone",
        "source_evidence_ids",
        "quoted_content_caveat",
        "participants",
        "proposed_windows",
        "timezone_evidence",
        "constraints",
    }:
        return None, "schema_invalid"

    window_start = _normalize_rfc3339_like(raw_input.get("window_start"))
    window_end = _normalize_rfc3339_like(raw_input.get("window_end"))
    if window_start is None or window_end is None:
        return None, "schema_invalid"
    window_start_dt = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    window_end_dt = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
    if window_end_dt <= window_start_dt:
        return None, "schema_invalid"

    duration_raw = raw_input.get("duration_minutes", 30)
    if not isinstance(duration_raw, int):
        return None, "schema_invalid"
    if duration_raw < 5 or duration_raw > 480:
        return None, "schema_invalid"

    attendees_raw = raw_input.get("attendees", [])
    if not isinstance(attendees_raw, list):
        return None, "schema_invalid"
    attendees: list[str] = []
    for attendee_raw in attendees_raw:
        if not isinstance(attendee_raw, str):
            return None, "schema_invalid"
        attendee = attendee_raw.strip().lower()
        if not attendee or len(attendee) > 320:
            return None, "schema_invalid"
        attendees.append(attendee)

    timezone = _normalize_optional_text(raw_input.get("timezone"), max_length=64)
    if timezone is None:
        return None, "schema_invalid"

    source_evidence_ids_raw = raw_input.get("source_evidence_ids")
    if not isinstance(source_evidence_ids_raw, list) or len(source_evidence_ids_raw) > 20:
        return None, "schema_invalid"
    source_evidence_ids: list[str] = []
    for evidence_id_raw in source_evidence_ids_raw:
        evidence_id = _normalize_optional_text(evidence_id_raw, max_length=64)
        if evidence_id is None:
            return None, "schema_invalid"
        source_evidence_ids.append(evidence_id)

    quoted_content_caveat = raw_input.get("quoted_content_caveat")
    if not isinstance(quoted_content_caveat, bool):
        return None, "schema_invalid"

    participants_raw = raw_input.get("participants")
    if not isinstance(participants_raw, list) or len(participants_raw) > 20:
        return None, "schema_invalid"
    participants: list[str] = []
    for participant_raw in participants_raw:
        participant = _normalize_optional_text(participant_raw, max_length=320)
        if participant is None:
            return None, "schema_invalid"
        participants.append(participant)

    proposed_windows_raw = raw_input.get("proposed_windows")
    if not isinstance(proposed_windows_raw, list) or len(proposed_windows_raw) > 20:
        return None, "schema_invalid"
    proposed_windows: list[dict[str, str]] = []
    for proposed_window_raw in proposed_windows_raw:
        if not isinstance(proposed_window_raw, dict):
            return None, "schema_invalid"
        proposed_start = _normalize_rfc3339_like(proposed_window_raw.get("start"))
        proposed_end = _normalize_rfc3339_like(proposed_window_raw.get("end"))
        if proposed_start is None or proposed_end is None:
            return None, "schema_invalid"
        proposed_start_dt = datetime.fromisoformat(proposed_start.replace("Z", "+00:00"))
        proposed_end_dt = datetime.fromisoformat(proposed_end.replace("Z", "+00:00"))
        if proposed_end_dt <= proposed_start_dt:
            return None, "schema_invalid"
        proposed_windows.append({"start": proposed_start, "end": proposed_end})

    timezone_evidence = raw_input.get("timezone_evidence")
    if not isinstance(timezone_evidence, dict) or set(timezone_evidence.keys()) != {
        "source",
        "rationale",
        "confidence",
    }:
        return None, "schema_invalid"
    timezone_evidence_source = _normalize_optional_text(
        timezone_evidence.get("source"), max_length=128
    )
    if timezone_evidence.get("source") is not None and timezone_evidence_source is None:
        return None, "schema_invalid"
    timezone_evidence_rationale = _normalize_optional_text(
        timezone_evidence.get("rationale"), max_length=500
    )
    if timezone_evidence.get("rationale") is not None and timezone_evidence_rationale is None:
        return None, "schema_invalid"
    timezone_confidence_raw = timezone_evidence.get("confidence")
    if timezone_confidence_raw is None:
        timezone_confidence = None
    elif isinstance(timezone_confidence_raw, int | float) and not isinstance(
        timezone_confidence_raw, bool
    ):
        timezone_confidence = float(timezone_confidence_raw)
        if timezone_confidence < 0 or timezone_confidence > 1:
            return None, "schema_invalid"
    else:
        return None, "schema_invalid"

    constraints = raw_input.get("constraints")
    if not isinstance(constraints, dict) or set(constraints.keys()) != {
        "hard",
        "soft",
        "attendee_notes",
    }:
        return None, "schema_invalid"
    hard_constraints = _normalize_optional_string_list(
        constraints.get("hard"), max_items=20, max_length=500
    )
    soft_constraints = _normalize_optional_string_list(
        constraints.get("soft"), max_items=20, max_length=500
    )
    attendee_notes = _normalize_optional_string_list(
        constraints.get("attendee_notes"), max_items=20, max_length=500
    )
    if hard_constraints is None or soft_constraints is None or attendee_notes is None:
        return None, "schema_invalid"

    return {
        "window_start": window_start,
        "window_end": window_end,
        "duration_minutes": duration_raw,
        "attendees": attendees,
        "timezone": timezone,
        "source_evidence_ids": source_evidence_ids,
        "quoted_content_caveat": quoted_content_caveat,
        "participants": participants,
        "proposed_windows": proposed_windows,
        "timezone_evidence": {
            "source": timezone_evidence_source,
            "rationale": timezone_evidence_rationale,
            "confidence": timezone_confidence,
        },
        "constraints": {
            "hard": hard_constraints,
            "soft": soft_constraints,
            "attendee_notes": attendee_notes,
        },
    }, None


def _validate_email_search_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


def _validate_email_read_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"message_id", "thread_id", "mode"}):
        return None, "schema_invalid"

    mode_raw = raw_input.get("mode")
    if mode_raw is None:
        mode = "message" if raw_input.get("message_id") is not None else "thread"
    elif isinstance(mode_raw, str):
        mode = mode_raw.strip().lower()
    else:
        return None, "schema_invalid"
    if mode not in {"message", "thread", "thread_context"}:
        return None, "schema_invalid"

    raw_message_id = raw_input.get("message_id")
    raw_thread_id = raw_input.get("thread_id")
    message_id = _normalize_optional_text(raw_message_id, max_length=256)
    thread_id = _normalize_optional_text(raw_thread_id, max_length=256)
    if raw_message_id is None:
        message_id = None
    elif message_id is None:
        return None, "schema_invalid"
    if raw_thread_id is None:
        thread_id = None
    elif thread_id is None:
        return None, "schema_invalid"
    if message_id is None and thread_id is None:
        return None, "schema_invalid"
    if mode == "message" and message_id is None:
        return None, "schema_invalid"
    if mode in {"thread", "thread_context"} and thread_id is None and message_id is None:
        return None, "schema_invalid"

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "mode": mode,
    }, None


def _validate_drive_search_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


def _validate_drive_read_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="file_id", max_length=256)


_MAPS_ALLOWED_TRAVEL_MODES = {"driving", "walking", "bicycling", "transit"}
_MAPS_MAX_WAYPOINTS = 10


def _validate_maps_directions_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {"origin", "destination", "travel_mode", "waypoints", "optimize_order"}
    ):
        return None, "schema_invalid"

    origin_raw = raw_input.get("origin")
    destination_raw = raw_input.get("destination")
    travel_mode_raw = raw_input.get("travel_mode", "driving")

    if origin_raw is None:
        origin: str | None = None
    elif isinstance(origin_raw, str):
        normalized_origin = origin_raw.strip()
        if len(normalized_origin) > 320:
            return None, "schema_invalid"
        origin = normalized_origin or None
    else:
        return None, "schema_invalid"

    if destination_raw is None:
        destination: str | None = None
    elif isinstance(destination_raw, str):
        normalized_destination = destination_raw.strip()
        if len(normalized_destination) > 320:
            return None, "schema_invalid"
        destination = normalized_destination or None
    else:
        return None, "schema_invalid"

    if not isinstance(travel_mode_raw, str):
        return None, "schema_invalid"
    travel_mode = travel_mode_raw.strip().lower() or "driving"
    if travel_mode not in _MAPS_ALLOWED_TRAVEL_MODES:
        return None, "schema_invalid"

    waypoints_raw = raw_input.get("waypoints", [])
    if not isinstance(waypoints_raw, list) or len(waypoints_raw) > _MAPS_MAX_WAYPOINTS:
        return None, "schema_invalid"
    waypoints: list[str] = []
    for waypoint_raw in waypoints_raw:
        if not isinstance(waypoint_raw, str):
            return None, "schema_invalid"
        waypoint = waypoint_raw.strip()
        if not waypoint or len(waypoint) > 320:
            return None, "schema_invalid"
        waypoints.append(waypoint)

    optimize_order = raw_input.get("optimize_order", False)
    if not isinstance(optimize_order, bool):
        return None, "schema_invalid"

    return {
        "origin": origin,
        "destination": destination,
        "travel_mode": travel_mode,
        "waypoints": tuple(waypoints),
        "optimize_order": optimize_order,
    }, None


def _validate_maps_search_places_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"query", "location_context", "radius_meters"}):
        return None, "schema_invalid"

    query_raw = raw_input.get("query")
    if not isinstance(query_raw, str):
        return None, "schema_invalid"
    query = query_raw.strip()
    if not query or len(query) > 200:
        return None, "schema_invalid"

    location_context_raw = raw_input.get("location_context")
    if location_context_raw is None:
        location_context: str | None = None
    elif isinstance(location_context_raw, str):
        normalized_location_context = location_context_raw.strip()
        if len(normalized_location_context) > 320:
            return None, "schema_invalid"
        location_context = normalized_location_context or None
    else:
        return None, "schema_invalid"

    radius_meters_raw = raw_input.get("radius_meters", 2000)
    if not isinstance(radius_meters_raw, int):
        return None, "schema_invalid"
    if radius_meters_raw < 100 or radius_meters_raw > 50000:
        return None, "schema_invalid"

    return {
        "query": query,
        "location_context": location_context,
        "radius_meters": radius_meters_raw,
    }, None


def _normalize_email_recipient(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 320:
        return None
    local, sep, domain = normalized.partition("@")
    if not sep or not local or not domain or "." not in domain:
        return None
    if any(ch.isspace() for ch in normalized):
        return None
    return normalized


def _normalize_email_recipients(raw_value: Any) -> list[str] | None:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        return None
    recipients: list[str] = []
    seen: set[str] = set()
    for raw_entry in raw_value:
        recipient = _normalize_email_recipient(raw_entry)
        if recipient is None:
            return None
        if recipient in seen:
            continue
        seen.add(recipient)
        recipients.append(recipient)
    return recipients


_DRIVE_ALLOWED_SHARE_ROLES = {"reader", "commenter", "writer"}


def _normalize_provider_write_authority(
    raw_input: dict[str, Any],
) -> tuple[dict[str, str] | None, str | None]:
    idempotency_key = _normalize_optional_text(raw_input.get("idempotency_key"), max_length=128)
    if idempotency_key is None:
        return None, "schema_invalid"
    authority: dict[str, str] = {"idempotency_key": idempotency_key}
    for key in ("source_evidence_id", "user_instruction_ref"):
        value = _normalize_optional_text(raw_input.get(key), max_length=256)
        if raw_input.get(key) is not None and value is None:
            return None, "schema_invalid"
        if value is not None:
            authority[key] = value
    if sum(1 for key in ("source_evidence_id", "user_instruction_ref") if key in authority) != 1:
        return None, "schema_invalid"
    return authority, None


def _validate_drive_share_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "file_id",
            "grantee_email",
            "role",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error

    file_id_raw = raw_input.get("file_id")
    grantee_raw = raw_input.get("grantee_email")
    role_raw = raw_input.get("role", "reader")
    if not isinstance(file_id_raw, str) or not isinstance(grantee_raw, str):
        return None, "schema_invalid"
    if not isinstance(role_raw, str):
        return None, "schema_invalid"

    file_id = file_id_raw.strip()
    if not file_id or len(file_id) > 256:
        return None, "schema_invalid"
    grantee_email = _normalize_email_recipient(grantee_raw)
    if grantee_email is None:
        return None, "schema_invalid"
    role = role_raw.strip().lower()
    if role not in _DRIVE_ALLOWED_SHARE_ROLES:
        return None, "schema_invalid"

    return {
        "file_id": file_id,
        "grantee_email": grantee_email,
        "role": role,
        **authority,
    }, None


def _validate_calendar_create_event_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "calendar_id",
            "title",
            "start_time",
            "end_time",
            "description",
            "location",
            "attendees",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error

    title_raw = raw_input.get("title")
    if not isinstance(title_raw, str):
        return None, "schema_invalid"
    title = title_raw.strip()
    if not title or len(title) > 200:
        return None, "schema_invalid"

    start_time = _normalize_rfc3339_like(raw_input.get("start_time"))
    end_time = _normalize_rfc3339_like(raw_input.get("end_time"))
    if start_time is None or end_time is None:
        return None, "schema_invalid"
    start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    if end_dt <= start_dt:
        return None, "schema_invalid"

    description_raw = raw_input.get("description")
    description: str | None
    if description_raw is None:
        description = None
    elif isinstance(description_raw, str):
        normalized_description = description_raw.strip()
        if len(normalized_description) > 4000:
            return None, "schema_invalid"
        description = normalized_description or None
    else:
        return None, "schema_invalid"

    location_raw = raw_input.get("location")
    location: str | None
    if location_raw is None:
        location = None
    elif isinstance(location_raw, str):
        normalized_location = location_raw.strip()
        if len(normalized_location) > 500:
            return None, "schema_invalid"
        location = normalized_location or None
    else:
        return None, "schema_invalid"

    attendees = _normalize_email_recipients(raw_input.get("attendees"))
    if attendees is None:
        return None, "schema_invalid"

    calendar_id_raw = raw_input.get("calendar_id")
    normalized: dict[str, Any] = {
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "description": description,
        "location": location,
        "attendees": attendees,
        **authority,
    }
    if isinstance(calendar_id_raw, str) and calendar_id_raw.strip():
        normalized["calendar_id"] = calendar_id_raw.strip()
    elif calendar_id_raw is not None:
        return None, "schema_invalid"
    return normalized, None


def _validate_calendar_update_event_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "calendar_id",
            "event_id",
            "title",
            "start_time",
            "end_time",
            "description",
            "location",
            "attendees",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error
    event_id_raw = raw_input.get("event_id")
    if not isinstance(event_id_raw, str) or not event_id_raw.strip():
        return None, "schema_invalid"
    normalized: dict[str, Any] = {"event_id": event_id_raw.strip(), **authority}
    calendar_id_raw = raw_input.get("calendar_id")
    if isinstance(calendar_id_raw, str) and calendar_id_raw.strip():
        normalized["calendar_id"] = calendar_id_raw.strip()
    elif calendar_id_raw is not None:
        return None, "schema_invalid"

    title_raw = raw_input.get("title")
    if isinstance(title_raw, str):
        title = title_raw.strip()
        if not title or len(title) > 200:
            return None, "schema_invalid"
        normalized["title"] = title
    elif title_raw is not None:
        return None, "schema_invalid"

    start_raw = raw_input.get("start_time")
    end_raw = raw_input.get("end_time")
    if start_raw is not None or end_raw is not None:
        start_time = _normalize_rfc3339_like(start_raw)
        end_time = _normalize_rfc3339_like(end_raw)
        if start_time is None or end_time is None:
            return None, "schema_invalid"
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        if end_dt <= start_dt:
            return None, "schema_invalid"
        normalized["start_time"] = start_time
        normalized["end_time"] = end_time

    description_raw = raw_input.get("description")
    if isinstance(description_raw, str):
        if len(description_raw) > 4000:
            return None, "schema_invalid"
        normalized["description"] = description_raw.strip()
    elif description_raw is not None:
        return None, "schema_invalid"

    location_raw = raw_input.get("location")
    if isinstance(location_raw, str):
        if len(location_raw) > 500:
            return None, "schema_invalid"
        normalized["location"] = location_raw.strip()
    elif location_raw is not None:
        return None, "schema_invalid"

    if "attendees" in raw_input:
        attendees_raw = raw_input.get("attendees")
        if attendees_raw is not None:
            attendees = _normalize_email_recipients(attendees_raw)
            if attendees is None:
                return None, "schema_invalid"
            normalized["attendees"] = attendees

    if set(normalized.keys()).issubset(
        {
            "event_id",
            "calendar_id",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    return normalized, None


def _validate_calendar_respond_to_event_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "calendar_id",
            "event_id",
            "attendee_email",
            "response_status",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error
    event_id_raw = raw_input.get("event_id")
    attendee_email_raw = raw_input.get("attendee_email")
    response_status = raw_input.get("response_status")
    if not isinstance(event_id_raw, str) or not event_id_raw.strip():
        return None, "schema_invalid"
    recipients = _normalize_email_recipients([attendee_email_raw])
    if recipients is None or len(recipients) != 1:
        return None, "schema_invalid"
    if response_status not in {"accepted", "declined", "tentative", "needsAction"}:
        return None, "schema_invalid"
    normalized: dict[str, Any] = {
        "event_id": event_id_raw.strip(),
        "attendee_email": recipients[0],
        "response_status": response_status,
        **authority,
    }
    calendar_id_raw = raw_input.get("calendar_id")
    if isinstance(calendar_id_raw, str) and calendar_id_raw.strip():
        normalized["calendar_id"] = calendar_id_raw.strip()
    elif calendar_id_raw is not None:
        return None, "schema_invalid"
    return normalized, None


def _validate_email_composition_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "to",
            "cc",
            "bcc",
            "subject",
            "body",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    if "to" not in raw_input or "subject" not in raw_input or "body" not in raw_input:
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error

    to_recipients = _normalize_email_recipients(raw_input.get("to"))
    cc_recipients = _normalize_email_recipients(raw_input.get("cc", []))
    bcc_recipients = _normalize_email_recipients(raw_input.get("bcc", []))
    if to_recipients is None or cc_recipients is None or bcc_recipients is None:
        return None, "schema_invalid"
    if not to_recipients and not cc_recipients and not bcc_recipients:
        return None, "schema_invalid"

    subject_raw = raw_input.get("subject")
    body_raw = raw_input.get("body")
    if not isinstance(subject_raw, str) or not isinstance(body_raw, str):
        return None, "schema_invalid"
    subject = subject_raw.strip()
    body = body_raw.strip()
    if not subject or not body:
        return None, "schema_invalid"
    if len(subject) > 998 or len(body) > 20000:
        return None, "schema_invalid"

    return {
        "to": to_recipients,
        "cc": cc_recipients,
        "bcc": bcc_recipients,
        "subject": subject,
        "body": body,
        **authority,
    }, None


def _validate_email_draft_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_email_composition_input(raw_input)


def _validate_email_send_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_email_composition_input(raw_input)


def _normalize_email_message_ids(raw_value: Any) -> list[str] | None:
    if not isinstance(raw_value, list) or not raw_value or len(raw_value) > 1000:
        return None
    message_ids: list[str] = []
    seen: set[str] = set()
    for raw_message_id in raw_value:
        message_id = _normalize_optional_text(raw_message_id, max_length=256)
        if message_id is None:
            return None
        if message_id in seen:
            continue
        seen.add(message_id)
        message_ids.append(message_id)
    if not message_ids:
        return None
    return message_ids


def _normalize_email_label_names(raw_value: Any) -> list[str] | None:
    if not isinstance(raw_value, list) or len(raw_value) > 100:
        return None
    label_names: list[str] = []
    seen: set[str] = set()
    for raw_label_name in raw_value:
        label_name = _normalize_optional_text(raw_label_name, max_length=225)
        if label_name is None:
            return None
        if label_name in seen:
            continue
        seen.add(label_name)
        label_names.append(label_name)
    return label_names


def _validate_email_message_batch_mutation_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "message_ids",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error
    message_ids = _normalize_email_message_ids(raw_input.get("message_ids"))
    if message_ids is None:
        return None, "schema_invalid"
    return {"message_ids": message_ids, **authority}, None


def _validate_email_labels_modify_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "message_ids",
            "add_labels",
            "remove_labels",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error
    message_ids = _normalize_email_message_ids(raw_input.get("message_ids"))
    add_labels = _normalize_email_label_names(raw_input.get("add_labels"))
    remove_labels = _normalize_email_label_names(raw_input.get("remove_labels"))
    if message_ids is None or add_labels is None or remove_labels is None:
        return None, "schema_invalid"
    if not add_labels and not remove_labels:
        return None, "schema_invalid"
    if set(add_labels).intersection(remove_labels):
        return None, "schema_invalid"
    return {
        "message_ids": message_ids,
        "add_labels": add_labels,
        "remove_labels": remove_labels,
        **authority,
    }, None


def _validate_email_undo_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "undo_token",
            "idempotency_key",
            "source_evidence_id",
            "user_instruction_ref",
        }
    ):
        return None, "schema_invalid"
    authority, authority_error = _normalize_provider_write_authority(raw_input)
    if authority is None:
        return None, authority_error
    undo_token = _normalize_optional_text(raw_input.get("undo_token"), max_length=512)
    if undo_token is None:
        return None, "schema_invalid"
    return {"undo_token": undo_token, **authority}, None


_WEATHER_ALLOWED_TIMEFRAMES = {"now", "today", "tomorrow", "next_24h"}


def _validate_weather_forecast_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"location", "timeframe"}):
        return None, "schema_invalid"

    location_raw = raw_input.get("location")
    if location_raw is None:
        location: str | None = None
    elif isinstance(location_raw, str):
        normalized_location = location_raw.strip()
        if len(normalized_location) > 200:
            return None, "schema_invalid"
        location = normalized_location or None
    else:
        return None, "schema_invalid"

    timeframe_raw = raw_input.get("timeframe", "today")
    if not isinstance(timeframe_raw, str):
        return None, "schema_invalid"
    timeframe = timeframe_raw.strip().lower() or "today"
    if timeframe not in _WEATHER_ALLOWED_TIMEFRAMES:
        return None, "schema_invalid"

    return {"location": location, "timeframe": timeframe}, None


def _normalize_bounded_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < minimum or value > maximum:
        return None
    return value


def _normalize_optional_rfc3339_input(value: Any) -> str | None:
    if value is None:
        return None
    return _normalize_rfc3339_like(value)


def _validate_memory_recall_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=12_000)


def _validate_memory_remember_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="note", max_length=12_000)


_MEMORY_LOG_KINDS = frozenset(
    {
        "user_message",
        "agent_round",
        "assistant_message",
        "tool_observation",
        "proactive_trigger",
        "note_create",
        "note_edit",
        "note_delete",
        "recall",
        "research_finding",
    }
)


def _validate_memory_search_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"query", "limit", "since", "kinds"}):
        return None, "schema_invalid"
    query_raw = raw_input.get("query")
    if not isinstance(query_raw, str):
        return None, "schema_invalid"
    query = query_raw.strip()
    if not query or len(query) > 12_000:
        return None, "schema_invalid"
    limit_raw = raw_input.get("limit")
    if limit_raw is None:
        limit = None
    elif isinstance(limit_raw, int) and not isinstance(limit_raw, bool):
        if limit_raw < 1 or limit_raw > 100:
            return None, "schema_invalid"
        limit = limit_raw
    else:
        return None, "schema_invalid"
    since_raw = raw_input.get("since")
    if since_raw is None:
        since = None
    elif isinstance(since_raw, str):
        since = since_raw.strip() or None
    else:
        return None, "schema_invalid"
    kinds_raw = raw_input.get("kinds")
    if kinds_raw is None:
        kinds = None
    elif isinstance(kinds_raw, list):
        kinds: list[str] | None = []
        for k in kinds_raw:
            if not isinstance(k, str) or k not in _MEMORY_LOG_KINDS:
                return None, "schema_invalid"
            kinds.append(k)
    else:
        return None, "schema_invalid"
    return {"query": query, "limit": limit, "since": since, "kinds": kinds}, None


def _validate_memory_read_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="id", max_length=32)


def _validate_memory_note_create_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="content", max_length=12_000)


def _validate_memory_note_edit_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {"id", "content"}:
        return None, "schema_invalid"
    id_raw = raw_input.get("id")
    content_raw = raw_input.get("content")
    if not isinstance(id_raw, str) or not isinstance(content_raw, str):
        return None, "schema_invalid"
    note_id = id_raw.strip()
    content = content_raw.strip()
    if not note_id or len(note_id) > 32:
        return None, "schema_invalid"
    if not content or len(content) > 12_000:
        return None, "schema_invalid"
    return {"id": note_id, "content": content}, None


def _validate_memory_note_delete_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="id", max_length=32)


def _validate_proactive_schedule_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {"when", "note"}:
        return None, "schema_invalid"
    note = raw_input.get("note")
    if not isinstance(note, str):
        return None, "schema_invalid"
    normalized_note = note.strip()
    if not normalized_note or len(normalized_note) > 12_000:
        return None, "schema_invalid"
    when = _normalize_rfc3339_like(raw_input.get("when"))
    if when is None:
        return None, "schema_invalid"
    return {"when": when, "note": normalized_note}, None


_RESEARCH_INVESTIGATE_MAX_QUESTION_LENGTH = 4000
_RESEARCH_INVESTIGATE_ALLOWED_MODES = {"web", "personal", "memories"}


def _validate_research_investigate_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {"question", "mode"}:
        return None, "schema_invalid"
    question = raw_input.get("question")
    if not isinstance(question, str):
        return None, "schema_invalid"
    normalized_question = question.strip()
    if (
        not normalized_question
        or len(normalized_question) > _RESEARCH_INVESTIGATE_MAX_QUESTION_LENGTH
    ):
        return None, "schema_invalid"
    mode = raw_input.get("mode")
    if not isinstance(mode, str) or mode not in _RESEARCH_INVESTIGATE_ALLOWED_MODES:
        return None, "schema_invalid"
    return {"question": normalized_question, "mode": mode}, None


def _normalize_optional_text(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        return None
    return normalized


def _normalize_optional_string_list(
    value: Any, *, max_items: int, max_length: int
) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        return None
    items: list[str] = []
    for item_raw in value:
        item = _normalize_optional_text(item_raw, max_length=max_length)
        if item is None:
            return None
        items.append(item)
    return items


def _normalize_optional_env(value: Any) -> dict[str, str] | None:
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) > 20:
        return None
    env: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict) or set(item.keys()) != {"name", "value"}:
            return None
        key = _normalize_optional_text(item.get("name"), max_length=80)
        env_value = _normalize_optional_text(item.get("value"), max_length=2000)
        if key is None or env_value is None:
            return None
        env[key] = env_value
    return env


def _validate_agency_run_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "repo_root",
            "name",
            "prompt",
            "base_branch",
            "runner",
            "runner_args",
            "env",
            "no_include_untracked",
        }
    ):
        return None, "schema_invalid"
    repo_root = _normalize_optional_text(raw_input.get("repo_root"), max_length=4096)
    name = _normalize_optional_text(raw_input.get("name"), max_length=120)
    prompt = _normalize_optional_text(raw_input.get("prompt"), max_length=262144)
    if repo_root is None or name is None or prompt is None:
        return None, "schema_invalid"
    base_branch = _normalize_optional_text(raw_input.get("base_branch"), max_length=200)
    runner = _normalize_optional_text(raw_input.get("runner"), max_length=80)
    runner_args = _normalize_optional_string_list(
        raw_input.get("runner_args"), max_items=20, max_length=500
    )
    env = _normalize_optional_env(raw_input.get("env"))
    if runner_args is None or env is None:
        return None, "schema_invalid"
    no_include_untracked_raw = raw_input.get("no_include_untracked")
    if no_include_untracked_raw is None:
        no_include_untracked = False
    elif isinstance(no_include_untracked_raw, bool):
        no_include_untracked = no_include_untracked_raw
    else:
        return None, "schema_invalid"
    return {
        "repo_root": repo_root,
        "name": name,
        "prompt": prompt,
        "base_branch": base_branch,
        "runner": runner,
        "runner_args": runner_args,
        "env": env,
        "no_include_untracked": no_include_untracked,
    }, None


def _validate_agency_job_lookup_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"job_id", "repo_id", "task_id"}):
        return None, "schema_invalid"
    job_id = _normalize_optional_text(raw_input.get("job_id"), max_length=32)
    repo_id = _normalize_optional_text(raw_input.get("repo_id"), max_length=128)
    task_id = _normalize_optional_text(raw_input.get("task_id"), max_length=128)
    if job_id is None and (repo_id is None or task_id is None):
        return None, "schema_invalid"
    return {"job_id": job_id, "repo_id": repo_id, "task_id": task_id}, None


def _validate_agency_request_pr_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {
            "job_id",
            "repo_id",
            "task_id",
            "invocation_id",
            "worktree_id",
            "allow_dirty",
            "force_with_lease",
        }
    ):
        return None, "schema_invalid"
    lookup, lookup_error = _validate_agency_job_lookup_input(
        {
            "job_id": raw_input.get("job_id"),
            "repo_id": raw_input.get("repo_id"),
            "task_id": raw_input.get("task_id"),
        }
    )
    if lookup_error is not None or lookup is None:
        return None, "schema_invalid"
    invocation_id = _normalize_optional_text(raw_input.get("invocation_id"), max_length=128)
    worktree_id = _normalize_optional_text(raw_input.get("worktree_id"), max_length=128)
    allow_dirty_raw = raw_input.get("allow_dirty")
    force_with_lease_raw = raw_input.get("force_with_lease")
    if allow_dirty_raw is not None and not isinstance(allow_dirty_raw, bool):
        return None, "schema_invalid"
    if force_with_lease_raw is not None and not isinstance(force_with_lease_raw, bool):
        return None, "schema_invalid"
    return {
        **lookup,
        "invocation_id": invocation_id,
        "worktree_id": worktree_id,
        "allow_dirty": bool(allow_dirty_raw) if allow_dirty_raw is not None else False,
        "force_with_lease": bool(force_with_lease_raw)
        if force_with_lease_raw is not None
        else False,
    }, None


def _search_brave_base_url() -> str:
    default_base_url = "https://api.search.brave.com/res/v1"
    normalized = AppSettings().search_brave_base_url.strip().rstrip("/")
    if not normalized:
        return default_base_url
    parsed = urlparse(normalized)
    if parsed.scheme:
        return normalized
    if "://" in normalized:
        return default_base_url
    return f"https://{normalized.lstrip('/')}"


def _search_web_endpoint() -> str:
    return f"{_search_brave_base_url()}/web/search"


def _search_web_timeout_seconds() -> float:
    return AppSettings().search_web_timeout_seconds


def _search_web_api_key() -> str | None:
    return AppSettings().search_web_api_key


def _endpoint_host(endpoint: str) -> str | None:
    parsed = urlparse(endpoint)
    if parsed.hostname:
        return parsed.hostname.lower()
    if "://" in endpoint:
        return None
    host = endpoint.split("/", maxsplit=1)[0].strip().lower()
    if ":" in host:
        host = host.split(":", maxsplit=1)[0]
    return host or None


def _normalize_optional_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        return normalized
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_search_result_item(raw_item: WebSearchResultItem) -> dict[str, Any] | None:
    title = raw_item.title.strip()
    source = raw_item.url.strip()
    snippet = raw_item.snippet.strip()
    if not title or not source or not snippet:
        return None
    return {
        "title": title,
        "source": source,
        "snippet": snippet,
        "published_at": _normalize_optional_timestamp(raw_item.published_at),
    }


def _normalize_web_search_response(response: WebSearchResponse, *, query: str) -> dict[str, Any]:
    normalized_results: list[dict[str, Any]] = []
    for raw_item in response.results:
        normalized_item = _normalize_search_result_item(raw_item)
        if normalized_item is None:
            continue
        normalized_results.append(normalized_item)
        if len(normalized_results) >= 5:
            break

    return {
        "query": query,
        "retrieved_at": _normalize_optional_timestamp(response.retrieved_at)
        or datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "results": normalized_results,
    }


def _raise_web_search_runtime_error(exc: BaseException, *, prefix: str) -> None:
    if not isinstance(exc, WebSearchError):
        raise RuntimeError(f"{prefix} provider failure") from exc
    if exc.code == WebSearchErrorCode.TIMEOUT:
        raise RuntimeError(f"{prefix} provider timeout") from exc
    if exc.code == WebSearchErrorCode.RATE_LIMITED:
        raise RuntimeError(f"{prefix} provider rate limited") from exc
    if exc.code == WebSearchErrorCode.PROVIDER_DOWN:
        raise RuntimeError(f"{prefix} provider upstream failure") from exc
    if exc.code in {WebSearchErrorCode.INVALID_KEY, WebSearchErrorCode.INVALID_REQUEST}:
        raise RuntimeError(f"{prefix} provider request rejected") from exc
    if exc.code == WebSearchErrorCode.BAD_RESPONSE:
        raise RuntimeError(f"{prefix} provider returned invalid payload") from exc
    raise RuntimeError(f"{prefix} provider failure") from exc


async def _run_brave_search(
    *,
    api_key: str,
    query: str,
    result_type: WebSearchResultType,
    timeout_seconds: float,
) -> WebSearchResponse:
    async with httpx.AsyncClient() as client:
        provider = BraveSearchProvider(
            client,
            api_key=api_key,
            base_url=_search_brave_base_url(),
            timeout_seconds=timeout_seconds,
        )
        return await provider.search(
            WebSearchRequest(query=query, result_type=result_type, limit=5)
        )


def _execute_search_web(input_payload: dict[str, Any]) -> dict[str, Any]:
    api_key = _search_web_api_key()
    if api_key is None:
        raise RuntimeError("search credentials are not configured")
    endpoint_parsed = urlparse(_search_web_endpoint())
    if endpoint_parsed.hostname is None or endpoint_parsed.scheme.lower() not in {"http", "https"}:
        raise RuntimeError("search endpoint invalid")
    try:
        response = asyncio.run(
            _run_brave_search(
                api_key=api_key,
                query=input_payload["query"],
                result_type=WebSearchResultType.WEB,
                timeout_seconds=_search_web_timeout_seconds(),
            )
        )
    except WebSearchError as exc:
        _raise_web_search_runtime_error(exc, prefix="search")

    return _normalize_web_search_response(response, query=input_payload["query"])


def _declare_search_web_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": _search_web_endpoint(),
            "payload": {"query": input_payload["query"]},
        }
    ]


def _search_web_allowed_destinations() -> tuple[str, ...]:
    endpoint = _search_web_endpoint()
    host = _endpoint_host(endpoint)
    if host is not None:
        return (host,)
    return ("api.search.brave.com",)


def _search_news_endpoint() -> str:
    return f"{_search_brave_base_url()}/news/search"


def _search_news_timeout_seconds() -> float:
    return AppSettings().search_news_timeout_seconds


def _search_news_api_key() -> str | None:
    settings = AppSettings()
    return settings.search_news_api_key or settings.search_web_api_key


def _execute_search_news(input_payload: dict[str, Any]) -> dict[str, Any]:
    api_key = _search_news_api_key()
    if api_key is None:
        raise RuntimeError("news search credentials are not configured")
    endpoint_parsed = urlparse(_search_news_endpoint())
    if endpoint_parsed.hostname is None or endpoint_parsed.scheme.lower() not in {"http", "https"}:
        raise RuntimeError("news search endpoint invalid")
    try:
        response = asyncio.run(
            _run_brave_search(
                api_key=api_key,
                query=input_payload["query"],
                result_type=WebSearchResultType.NEWS,
                timeout_seconds=_search_news_timeout_seconds(),
            )
        )
    except WebSearchError as exc:
        _raise_web_search_runtime_error(exc, prefix="news")

    return _normalize_web_search_response(response, query=input_payload["query"])


def _declare_search_news_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": _search_news_endpoint(),
            "payload": {"query": input_payload["query"]},
        }
    ]


def _search_news_allowed_destinations() -> tuple[str, ...]:
    endpoint = _search_news_endpoint()
    host = _endpoint_host(endpoint)
    if host is not None:
        return (host,)
    return ("api.search.brave.com",)


_WEB_EXTRACT_TRACKING_QUERY_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
    }
)
_WEB_EXTRACT_BLOCKED_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".localhost",
    ".home",
    ".lan",
)
_WEB_EXTRACT_MAX_BLOCKS = 8
_WEB_EXTRACT_MAX_BLOCK_CHARS = 1200
_WEB_EXTRACT_MAX_TOTAL_CHARS = 4000


def _web_extract_provider_endpoint() -> str:
    default_endpoint = "https://api.search.brave.com/res/v1/web/extract"
    configured_endpoint = AppSettings().web_extract_provider_endpoint
    if configured_endpoint is None:
        return default_endpoint
    normalized = configured_endpoint.strip()
    if not normalized:
        return default_endpoint
    parsed = urlparse(normalized)
    if parsed.scheme:
        return normalized
    if "://" in normalized:
        return default_endpoint
    return f"https://{normalized.lstrip('/')}"


def _web_extract_timeout_seconds() -> float:
    return AppSettings().web_extract_timeout_seconds


def _web_extract_max_retries() -> int:
    return AppSettings().web_extract_max_retries


def _web_extract_api_key() -> str | None:
    settings = AppSettings()
    return settings.web_extract_api_key or settings.search_web_api_key


def _is_unsafe_web_extract_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if not normalized:
        return True
    if normalized == "localhost":
        return True
    try:
        parsed_ip = ip_address(normalized)
    except ValueError:
        # single-label hosts are treated as local-only/non-public and blocked.
        if "." not in normalized:
            return True
        return normalized.endswith(_WEB_EXTRACT_BLOCKED_HOST_SUFFIXES)
    return (
        parsed_ip.is_private
        or parsed_ip.is_loopback
        or parsed_ip.is_link_local
        or parsed_ip.is_multicast
        or parsed_ip.is_reserved
        or parsed_ip.is_unspecified
    )


def _format_url_netloc(*, host: str, port: int | None) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    if port is None:
        return rendered_host
    return f"{rendered_host}:{port}"


def _normalize_web_extract_url(raw_url: str) -> tuple[str | None, str | None]:
    candidate = raw_url.strip()
    if not candidate:
        return None, "url_invalid"
    if any(character.isspace() for character in candidate):
        return None, "url_invalid"
    parsed = urlparse(candidate)
    if not parsed.scheme:
        return None, "url_invalid"
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None, "url_scheme_unsupported"
    if parsed.hostname is None:
        return None, "url_invalid"
    if parsed.username is not None or parsed.password is not None:
        return None, "url_invalid"
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        return None, "url_invalid"
    try:
        parsed_port = parsed.port
    except ValueError:
        return None, "url_invalid"
    if _is_unsafe_web_extract_host(host):
        return None, "url_destination_unsafe"

    path = parsed.path or "/"
    netloc = _format_url_netloc(host=host, port=parsed_port)
    normalized = urlunparse(
        (
            scheme,
            netloc,
            path,
            "",
            parsed.query,
            "",
        )
    )
    return normalized, None


def _canonicalize_web_extract_source_identity(raw_url: str) -> str | None:
    parsed = urlparse(raw_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        return None

    host = parsed.hostname.lower().rstrip(".")
    if not host:
        return None

    try:
        parsed_port = parsed.port
    except ValueError:
        return None

    default_port = 443 if scheme == "https" else 80
    if parsed_port is None or parsed_port == default_port:
        netloc = _format_url_netloc(host=host, port=None)
    else:
        netloc = _format_url_netloc(host=host, port=parsed_port)

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"

    filtered_query_pairs: list[tuple[str, str]] = []
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return None
    for key, value in query_pairs:
        normalized_key = key.strip()
        if not normalized_key:
            continue
        if normalized_key.lower() in _WEB_EXTRACT_TRACKING_QUERY_KEYS:
            continue
        filtered_query_pairs.append((normalized_key, value))
    filtered_query_pairs.sort()
    try:
        canonical_query = urlencode(filtered_query_pairs, doseq=True)
    except ValueError:
        return None
    return urlunparse((scheme, netloc, path, "", canonical_query, ""))


def _web_extract_content_blocks(payload: dict[str, Any]) -> list[str]:
    raw_blocks = payload.get("content_blocks")
    parsed_blocks: list[str] = []
    if isinstance(raw_blocks, list):
        for raw_block in raw_blocks:
            if isinstance(raw_block, str):
                parsed_blocks.append(raw_block)
                continue
            if isinstance(raw_block, dict):
                text = raw_block.get("text")
                if isinstance(text, str):
                    parsed_blocks.append(text)
        if parsed_blocks:
            return parsed_blocks

    content_raw = payload.get("content")
    if isinstance(content_raw, str) and content_raw.strip():
        normalized_content = content_raw.replace("\r\n", "\n")
        paragraph_blocks = [
            chunk.strip() for chunk in normalized_content.split("\n\n") if chunk.strip()
        ]
        if paragraph_blocks:
            return paragraph_blocks

    excerpt_raw = payload.get("excerpt")
    if isinstance(excerpt_raw, str) and excerpt_raw.strip():
        return [excerpt_raw.strip()]

    return []


def _normalize_web_extract_blocks(
    raw_blocks: list[str],
) -> tuple[list[dict[str, Any]], bool, int]:
    normalized_blocks: list[dict[str, Any]] = []
    total_chars = 0
    truncated = False

    for raw_block in raw_blocks:
        compact_block = " ".join(raw_block.split())
        if not compact_block:
            continue
        if len(normalized_blocks) >= _WEB_EXTRACT_MAX_BLOCKS:
            truncated = True
            break
        if total_chars >= _WEB_EXTRACT_MAX_TOTAL_CHARS:
            truncated = True
            break

        bounded_block = compact_block
        if len(bounded_block) > _WEB_EXTRACT_MAX_BLOCK_CHARS:
            bounded_block = bounded_block[:_WEB_EXTRACT_MAX_BLOCK_CHARS].rstrip()
            truncated = True

        remaining_chars = _WEB_EXTRACT_MAX_TOTAL_CHARS - total_chars
        if len(bounded_block) > remaining_chars:
            bounded_block = bounded_block[:remaining_chars].rstrip()
            truncated = True
        if not bounded_block:
            truncated = True
            break

        normalized_blocks.append(
            {
                "index": len(normalized_blocks) + 1,
                "text": bounded_block,
            }
        )
        total_chars += len(bounded_block)

    return normalized_blocks, truncated, total_chars


def _web_extract_snippet(content_blocks: list[dict[str, Any]]) -> str:
    candidate_parts: list[str] = []
    for block in content_blocks[:2]:
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            candidate_parts.append(text.strip())
    if not candidate_parts:
        return "extracted evidence available"
    snippet = " ".join(candidate_parts).strip()
    if len(snippet) <= 500:
        return snippet
    return snippet[:500].rstrip() + "..."


def _execute_web_extract(input_payload: dict[str, Any]) -> dict[str, Any]:
    raw_url = input_payload.get("url")
    if not isinstance(raw_url, str):
        raise RuntimeError("url_invalid")

    normalized_url, url_error = _normalize_web_extract_url(raw_url)
    if url_error is not None or normalized_url is None:
        raise RuntimeError(url_error or "url_invalid")

    endpoint = _web_extract_provider_endpoint()
    endpoint_host = _endpoint_host(endpoint)
    endpoint_parsed = urlparse(endpoint)
    if endpoint_host is None or endpoint_parsed.scheme.lower() not in {"http", "https"}:
        raise RuntimeError("provider_unreachable")

    headers: dict[str, str] = {
        "accept": "application/json",
        "content-type": "application/json",
    }
    api_key = _web_extract_api_key()
    if api_key is not None:
        headers["x-subscription-token"] = api_key

    response: Any = None
    max_retries = _web_extract_max_retries()
    base_timeout_seconds = _web_extract_timeout_seconds()
    attempt_count = 0
    for attempt_index in range(max_retries + 1):
        attempt_count = attempt_index + 1
        # bounded linear backoff: 1.0x, 1.5x, 2.0x, ... up to retry ceiling.
        timeout_seconds = base_timeout_seconds * (1.0 + (attempt_index * 0.5))
        try:
            response = httpx.post(
                endpoint,
                json={"url": normalized_url},
                headers=headers,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            if attempt_index < max_retries:
                continue
            raise RuntimeError("provider_timeout") from exc
        except httpx.HTTPError as exc:
            if attempt_index < max_retries:
                continue
            raise RuntimeError("provider_network_failure") from exc

        if response.status_code == 429:
            if attempt_index < max_retries:
                continue
            raise RuntimeError("provider_rate_limited")
        if response.status_code in {401, 403, 451}:
            raise RuntimeError("access_restricted")
        if response.status_code == 415:
            raise RuntimeError("unsupported_format")
        if response.status_code >= 500:
            if attempt_index < max_retries:
                continue
            raise RuntimeError("provider_upstream_failure")
        if response.status_code >= 400:
            raise RuntimeError("provider_request_rejected")
        break

    if response is None:
        raise RuntimeError("provider_upstream_failure")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("provider_invalid_payload") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("provider_invalid_payload")

    document_payload_raw = payload.get("document")
    document_payload = document_payload_raw if isinstance(document_payload_raw, dict) else payload
    final_url_raw = (
        document_payload.get("final_url")
        or document_payload.get("canonical_url")
        or document_payload.get("url")
    )
    resolved_url = normalized_url
    if isinstance(final_url_raw, str) and final_url_raw.strip():
        normalized_final_url, final_url_error = _normalize_web_extract_url(final_url_raw)
        if final_url_error is not None or normalized_final_url is None:
            if final_url_error == "url_destination_unsafe":
                raise RuntimeError("url_destination_unsafe")
            raise RuntimeError("provider_invalid_payload")
        resolved_url = normalized_final_url

    canonical_url = _canonicalize_web_extract_source_identity(resolved_url)
    if canonical_url is None:
        raise RuntimeError("provider_invalid_payload")

    canonical_host = urlparse(canonical_url).hostname
    if canonical_host is None or _is_unsafe_web_extract_host(canonical_host):
        raise RuntimeError("url_destination_unsafe")

    retrieved_at = _normalize_optional_timestamp(
        document_payload.get("retrieved_at")
    ) or datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    published_at = _normalize_optional_timestamp(document_payload.get("published_at"))

    title_raw = document_payload.get("title")
    title = (
        title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else "Extracted page"
    )

    raw_blocks = _web_extract_content_blocks(document_payload)
    if not raw_blocks and document_payload is not payload:
        raw_blocks = _web_extract_content_blocks(payload)
    normalized_blocks, content_truncated, content_chars = _normalize_web_extract_blocks(raw_blocks)
    if not normalized_blocks:
        raise RuntimeError("unsupported_format")

    status_raw = document_payload.get("status")
    provider_marked_partial = (
        (isinstance(status_raw, str) and status_raw.strip().lower() == "partial")
        or document_payload.get("partial") is True
        or document_payload.get("truncated") is True
    )
    is_partial = content_truncated or provider_marked_partial
    reason_code = "content_truncated" if is_partial else None
    recovery = (
        "content was truncated. narrow scope to a specific section or shorter page and retry."
        if is_partial
        else None
    )

    language_raw = document_payload.get("language")
    language = (
        language_raw.strip().lower()
        if isinstance(language_raw, str) and language_raw.strip()
        else None
    )

    return {
        "url": normalized_url,
        "canonical_url": canonical_url,
        "retrieved_at": retrieved_at,
        "extract_outcome": {
            "status": "partial" if is_partial else "ok",
            "reason_code": reason_code,
            "recovery": recovery,
        },
        "document": {
            "title": title,
            "canonical_source": canonical_url,
            "resolved_url": resolved_url,
            "retrieved_at": retrieved_at,
            "published_at": published_at,
            "language": language,
            "truncated": is_partial,
            "truncation_reason": reason_code,
            "content_chars": content_chars,
            "content_blocks": normalized_blocks,
        },
        "provider": {
            "endpoint": endpoint,
            "attempt_count": attempt_count,
            "retry_count": attempt_count - 1,
        },
        "results": [
            {
                "title": title,
                "source": canonical_url,
                "snippet": _web_extract_snippet(normalized_blocks),
                "published_at": published_at,
            }
        ],
    }


def _declare_web_extract_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": _web_extract_provider_endpoint(),
            "payload": {"url": input_payload["url"]},
        }
    ]


def _web_extract_allowed_destinations() -> tuple[str, ...]:
    endpoint = _web_extract_provider_endpoint()
    host = _endpoint_host(endpoint)
    if host is not None:
        return (host,)
    return ("api.search.brave.com",)


_MAPS_ROUTES_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
_MAPS_PLACES_SEARCH_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
_MAPS_GEOCODE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"
_MAPS_ROUTES_HOST = "routes.googleapis.com"
_MAPS_PLACES_HOST = "places.googleapis.com"
_MAPS_GEOCODE_HOST = "maps.googleapis.com"
_MAPS_ROUTES_FIELD_MASK = (
    "routes.distanceMeters,routes.duration,routes.staticDuration,routes.description,"
    "routes.legs.distanceMeters,routes.legs.duration,routes.legs.staticDuration,"
    "routes.optimizedIntermediateWaypointIndex"
)
_MAPS_PLACES_FIELD_MASK = (
    "places.displayName,places.formattedAddress,places.location,"
    "places.googleMapsUri,places.rating,places.userRatingCount,"
    "places.regularOpeningHours.openNow,places.businessStatus"
)
_MAPS_TRAVEL_MODE_TO_ROUTES = {
    "driving": "DRIVE",
    "walking": "WALK",
    "bicycling": "BICYCLE",
    "transit": "TRANSIT",
}
_MAPS_MAX_ATTEMPTS = 3
_MAPS_PLACE_RESULT_LIMIT = 5
_MAPS_MAX_ALTERNATIVE_ROUTES = 3
_MAPS_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_EARTH_RADIUS_METERS = 6_371_000.0


def _maps_api_key() -> str:
    api_key = AppSettings().maps_api_key
    if api_key is None:
        raise RuntimeError("provider_credentials_missing")
    return api_key


def _maps_timeout_seconds() -> float:
    return AppSettings().maps_timeout_seconds


def _haversine_meters(
    *, origin_lat: float, origin_lng: float, target_lat: float, target_lng: float
) -> float:
    delta_lat = radians(target_lat - origin_lat)
    delta_lng = radians(target_lng - origin_lng)
    inner = (
        sin(delta_lat / 2) ** 2
        + cos(radians(origin_lat)) * cos(radians(target_lat)) * sin(delta_lng / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * asin(sqrt(inner))


def _maps_request_with_retry(
    *,
    method: str,
    endpoint: str,
    headers: dict[str, str],
    json_payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    base_timeout_seconds = _maps_timeout_seconds()
    for attempt_index in range(_MAPS_MAX_ATTEMPTS):
        is_final_attempt = attempt_index == _MAPS_MAX_ATTEMPTS - 1
        # Bounded linear backoff: each retry grants the provider more time (1.0x, 1.5x, 2.0x).
        timeout_seconds = base_timeout_seconds * (1.0 + attempt_index * 0.5)
        try:
            response = httpx.request(
                method,
                endpoint,
                headers=headers,
                json=json_payload,
                params=params,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            if is_final_attempt:
                raise RuntimeError("provider_timeout") from exc
            continue
        except httpx.HTTPError as exc:
            if is_final_attempt:
                raise RuntimeError("provider_network_failure") from exc
            continue
        if response.status_code in _MAPS_TRANSIENT_STATUS_CODES and not is_final_attempt:
            continue
        return response
    raise AssertionError("maps retry loop exited without returning a response")


def _raise_for_maps_status(response: httpx.Response) -> None:
    status_code = response.status_code
    if status_code < 400:
        return
    if status_code == 429:
        raise RuntimeError("provider_rate_limited")
    if status_code >= 500:
        raise RuntimeError("provider_upstream_failure")
    if status_code in {401, 403}:
        raise RuntimeError("provider_permission_denied")
    raise RuntimeError("provider_request_rejected")


def _maps_response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("provider_invalid_payload") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("provider_invalid_payload")
    return payload


def _normalize_int_like(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized.endswith("s"):
        normalized = normalized[:-1]
    try:
        return int(float(normalized))
    except ValueError:
        return None


def _maps_route_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    routes_raw = payload.get("routes")
    if not isinstance(routes_raw, list):
        return []
    candidates: list[dict[str, Any]] = []
    for raw_route in routes_raw:
        if isinstance(raw_route, dict):
            candidates.append(raw_route)
        if len(candidates) >= _MAPS_MAX_ALTERNATIVE_ROUTES:
            break
    return candidates


def _maps_places_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    places_raw = payload.get("places")
    if not isinstance(places_raw, list):
        return []
    candidates: list[dict[str, Any]] = []
    for raw_place in places_raw:
        if isinstance(raw_place, dict):
            candidates.append(raw_place)
        if len(candidates) >= _MAPS_PLACE_RESULT_LIMIT:
            break
    return candidates


def _maps_directions_source_url(
    *, origin: str, destination: str, travel_mode: str, waypoints: tuple[str, ...]
) -> str:
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(origin, safe='')}"
        f"&destination={quote(destination, safe='')}"
        f"&travelmode={quote(travel_mode, safe='')}"
    )
    if waypoints:
        url += "&waypoints=" + quote("|".join(waypoints), safe="")
    return url


def _maps_ordered_stops(
    *, route_payload: dict[str, Any], origin: str, destination: str, waypoints: tuple[str, ...]
) -> list[str]:
    # optimizedIntermediateWaypointIndex is present only when optimizeWaypointOrder
    # was requested; it permutes waypoints into Google's chosen visiting order.
    order_raw = route_payload.get("optimizedIntermediateWaypointIndex")
    if (
        isinstance(order_raw, list)
        and len(order_raw) == len(waypoints)
        and all(isinstance(index, int) and not isinstance(index, bool) for index in order_raw)
        and sorted(order_raw) == list(range(len(waypoints)))
    ):
        ordered_waypoints = [waypoints[index] for index in order_raw]
    else:
        ordered_waypoints = list(waypoints)
    return [origin, *ordered_waypoints, destination]


def _build_maps_route_result(
    *,
    route_payload: dict[str, Any],
    origin: str,
    destination: str,
    travel_mode: str,
    waypoints: tuple[str, ...],
) -> dict[str, Any]:
    description_raw = route_payload.get("description")
    description = (
        description_raw.strip()
        if isinstance(description_raw, str) and description_raw.strip()
        else None
    )
    legs_raw = route_payload.get("legs")
    legs: list[dict[str, Any]] = []
    if isinstance(legs_raw, list):
        for leg_raw in legs_raw:
            if not isinstance(leg_raw, dict):
                continue
            legs.append(
                {
                    "distance_meters": _normalize_int_like(leg_raw.get("distanceMeters")),
                    "duration_seconds": _normalize_int_like(leg_raw.get("duration")),
                    "static_duration_seconds": _normalize_int_like(leg_raw.get("staticDuration")),
                }
            )
    return {
        "distance_meters": _normalize_int_like(route_payload.get("distanceMeters")),
        "duration_seconds": _normalize_int_like(route_payload.get("duration")),
        "static_duration_seconds": _normalize_int_like(route_payload.get("staticDuration")),
        "description": description,
        "stops": _maps_ordered_stops(
            route_payload=route_payload,
            origin=origin,
            destination=destination,
            waypoints=waypoints,
        ),
        "legs": legs,
        "source": _maps_directions_source_url(
            origin=origin,
            destination=destination,
            travel_mode=travel_mode,
            waypoints=waypoints,
        ),
    }


def _build_maps_place_result(
    *,
    place_payload: dict[str, Any],
    center_lat: float,
    center_lng: float,
    radius_meters: int,
) -> dict[str, Any] | None:
    display_name = place_payload.get("displayName")
    title_raw = display_name.get("text") if isinstance(display_name, dict) else None
    if not isinstance(title_raw, str) or not title_raw.strip():
        return None
    title = title_raw.strip()

    location = place_payload.get("location")
    if not isinstance(location, dict):
        return None
    place_lat = location.get("latitude")
    place_lng = location.get("longitude")
    if not isinstance(place_lat, (int, float)) or isinstance(place_lat, bool):
        return None
    if not isinstance(place_lng, (int, float)) or isinstance(place_lng, bool):
        return None
    distance_meters = round(
        _haversine_meters(
            origin_lat=center_lat,
            origin_lng=center_lng,
            target_lat=float(place_lat),
            target_lng=float(place_lng),
        )
    )
    if distance_meters > radius_meters:
        return None

    maps_uri_raw = place_payload.get("googleMapsUri")
    source = (
        maps_uri_raw.strip()
        if isinstance(maps_uri_raw, str) and maps_uri_raw.strip()
        else f"https://www.google.com/maps/search/?api=1&query={quote(title, safe='')}"
    )

    address: str | None = None
    address_raw = place_payload.get("formattedAddress")
    if isinstance(address_raw, str) and address_raw.strip():
        address = address_raw.strip()

    rating: float | None = None
    rating_raw = place_payload.get("rating")
    if isinstance(rating_raw, (int, float)) and not isinstance(rating_raw, bool):
        rating = float(rating_raw)

    rating_count: int | None = None
    rating_count_raw = place_payload.get("userRatingCount")
    if isinstance(rating_count_raw, int) and not isinstance(rating_count_raw, bool):
        rating_count = rating_count_raw

    open_now: bool | None = None
    opening_hours = place_payload.get("regularOpeningHours")
    if isinstance(opening_hours, dict):
        open_now_raw = opening_hours.get("openNow")
        if isinstance(open_now_raw, bool):
            open_now = open_now_raw

    business_status: str | None = None
    business_status_raw = place_payload.get("businessStatus")
    if isinstance(business_status_raw, str) and business_status_raw.strip():
        business_status = business_status_raw.strip()

    return {
        "title": title,
        "source": source,
        # Place facts travel as structured fields; the snippet carries the
        # human-readable address for the citation.
        "snippet": address or title,
        "published_at": None,
        "address": address,
        "distance_meters": distance_meters,
        "rating": rating,
        "rating_count": rating_count,
        "open_now": open_now,
        "business_status": business_status,
    }


def _execute_maps_directions(input_payload: dict[str, Any]) -> dict[str, Any]:
    # Input is validator-normalized: origin/destination are non-empty str | None
    # (a missing endpoint is a clarification), travel_mode is an allowed mode,
    # waypoints is a stripped tuple (<=10), optimize_order is a bool.
    origin = input_payload["origin"]
    destination = input_payload["destination"]
    if origin is None:
        raise RuntimeError("maps_origin_required")
    if destination is None:
        raise RuntimeError("maps_destination_required")
    travel_mode = input_payload["travel_mode"]
    waypoints: tuple[str, ...] = input_payload["waypoints"]
    optimize_order = input_payload["optimize_order"]
    routes_travel_mode = _MAPS_TRAVEL_MODE_TO_ROUTES[travel_mode]

    api_key = _maps_api_key()
    request_body: dict[str, Any] = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": routes_travel_mode,
    }
    if routes_travel_mode == "DRIVE":
        request_body["routingPreference"] = "TRAFFIC_AWARE"
    if waypoints:
        request_body["intermediates"] = [{"address": waypoint} for waypoint in waypoints]
        if optimize_order:
            request_body["optimizeWaypointOrder"] = True
    else:
        # Alternatives and intermediates are mutually exclusive on the Routes API.
        request_body["computeAlternativeRoutes"] = True
    response = _maps_request_with_retry(
        method="POST",
        endpoint=_MAPS_ROUTES_ENDPOINT,
        headers={
            "content-type": "application/json",
            "x-goog-api-key": api_key,
            "x-goog-fieldmask": _MAPS_ROUTES_FIELD_MASK,
        },
        json_payload=request_body,
    )
    _raise_for_maps_status(response)
    payload = _maps_response_json(response)

    retrieved_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    routes: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for route_payload in _maps_route_candidates(payload):
        route = _build_maps_route_result(
            route_payload=route_payload,
            origin=origin,
            destination=destination,
            travel_mode=travel_mode,
            waypoints=waypoints,
        )
        routes.append(route)
        results.append(
            {
                "title": f"Route from {origin} to {destination}",
                "source": route["source"],
                # The snippet carries the provider's human-readable route
                # descriptor for the citation; route facts are structured fields.
                "snippet": route["description"] or f"{travel_mode} route",
                "published_at": None,
            }
        )
    return {
        "origin": origin,
        "destination": destination,
        "waypoints": list(waypoints),
        "travel_mode": travel_mode,
        "retrieved_at": retrieved_at,
        "uncertainty": "insufficient_evidence" if not routes else None,
        "routes": routes,
        "results": results,
    }


def _maps_geocode_location(location_context: str) -> tuple[float, float]:
    api_key = _maps_api_key()
    response = _maps_request_with_retry(
        method="GET",
        endpoint=_MAPS_GEOCODE_ENDPOINT,
        headers={"accept": "application/json"},
        params={"address": location_context, "key": api_key},
    )
    _raise_for_maps_status(response)
    payload = _maps_response_json(response)
    status = payload.get("status")
    if status == "OK":
        results_raw = payload.get("results")
        if isinstance(results_raw, list):
            for result in results_raw:
                if not isinstance(result, dict):
                    continue
                geometry = result.get("geometry")
                location = geometry.get("location") if isinstance(geometry, dict) else None
                if not isinstance(location, dict):
                    continue
                lat = location.get("lat")
                lng = location.get("lng")
                if (
                    isinstance(lat, (int, float))
                    and not isinstance(lat, bool)
                    and isinstance(lng, (int, float))
                    and not isinstance(lng, bool)
                ):
                    return float(lat), float(lng)
        raise RuntimeError("provider_invalid_payload")
    if status == "ZERO_RESULTS":
        # The location text is well-formed but Google resolved no place for it.
        # This is a clarification condition, not a provider fault: the assistant
        # must ask for a clearer location rather than retry the same string.
        raise RuntimeError("maps_location_not_found")
    if status == "INVALID_REQUEST":
        raise RuntimeError("provider_request_rejected")
    if status == "OVER_QUERY_LIMIT":
        raise RuntimeError("provider_rate_limited")
    if status in {"REQUEST_DENIED", "OVER_DAILY_LIMIT"}:
        raise RuntimeError("provider_permission_denied")
    raise RuntimeError("provider_upstream_failure")


def _execute_maps_search_places(input_payload: dict[str, Any]) -> dict[str, Any]:
    # Input is validator-normalized: query is a non-empty str, radius_meters is a
    # valid int, location_context is a non-empty str | None (None is a clarification).
    query = input_payload["query"]
    location_context = input_payload["location_context"]
    if location_context is None:
        raise RuntimeError("maps_location_context_required")
    radius_meters = input_payload["radius_meters"]

    center_lat, center_lng = _maps_geocode_location(location_context)
    api_key = _maps_api_key()
    request_body: dict[str, Any] = {
        "textQuery": f"{query} near {location_context}",
        "pageSize": _MAPS_PLACE_RESULT_LIMIT,
        "locationBias": {
            "circle": {
                "center": {"latitude": center_lat, "longitude": center_lng},
                "radius": float(radius_meters),
            }
        },
    }
    response = _maps_request_with_retry(
        method="POST",
        endpoint=_MAPS_PLACES_SEARCH_ENDPOINT,
        headers={
            "content-type": "application/json",
            "x-goog-api-key": api_key,
            "x-goog-fieldmask": _MAPS_PLACES_FIELD_MASK,
        },
        json_payload=request_body,
    )
    _raise_for_maps_status(response)
    payload = _maps_response_json(response)

    retrieved_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    for candidate in _maps_places_candidates(payload):
        normalized = _build_maps_place_result(
            place_payload=candidate,
            center_lat=center_lat,
            center_lng=center_lng,
            radius_meters=radius_meters,
        )
        if normalized is None:
            continue
        results.append(normalized)
    uncertainty = "insufficient_evidence" if not results else None
    return {
        "query": query,
        "location_context": location_context,
        "radius_meters": radius_meters,
        "retrieved_at": retrieved_at,
        "uncertainty": uncertainty,
        "results": results,
    }


def _declare_maps_directions_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": _MAPS_ROUTES_ENDPOINT,
            "payload": {
                "origin": input_payload.get("origin"),
                "destination": input_payload.get("destination"),
                "travel_mode": input_payload.get("travel_mode"),
                "waypoints": input_payload.get("waypoints"),
                "optimize_order": input_payload.get("optimize_order"),
            },
        }
    ]


def _declare_maps_search_places_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": _MAPS_GEOCODE_ENDPOINT,
            "payload": {"location_context": input_payload.get("location_context")},
        },
        {
            "destination": _MAPS_PLACES_SEARCH_ENDPOINT,
            "payload": {
                "query": input_payload.get("query"),
                "location_context": input_payload.get("location_context"),
                "radius_meters": input_payload.get("radius_meters"),
            },
        },
    ]


def _declare_google_calendar_list_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "payload": {
                "window_start": input_payload["window_start"],
                "window_end": input_payload["window_end"],
            },
        }
    ]


def _declare_google_calendar_propose_slots_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = {
        "window_start": input_payload["window_start"],
        "window_end": input_payload["window_end"],
        "duration_minutes": input_payload["duration_minutes"],
    }
    attendees_raw = input_payload.get("attendees", [])
    attendees = attendees_raw if isinstance(attendees_raw, list) else []
    declarations: list[dict[str, Any]] = [
        {
            "destination": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "payload": payload,
        }
    ]
    if attendees:
        declarations.append(
            {
                "destination": "https://www.googleapis.com/calendar/v3/freeBusy",
                "payload": {"attendees": attendees},
            }
        )
    return declarations


def _declare_google_email_search_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            "payload": {"query": input_payload["query"]},
        }
    ]


def _declare_google_email_read_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    thread_id = input_payload.get("thread_id")
    mode = input_payload.get("mode")
    if isinstance(thread_id, str) and thread_id:
        return [
            {
                "destination": (
                    f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}"
                ),
                "payload": {"thread_id": thread_id, "mode": mode},
            }
        ]
    message_id = input_payload["message_id"]
    return [
        {
            "destination": (
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
            ),
            "payload": {"message_id": message_id, "mode": mode},
        }
    ]


def _declare_google_calendar_create_event_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    calendar_id = quote(str(input_payload.get("calendar_id") or "primary"), safe="")
    payload: dict[str, Any] = {
        "title": input_payload["title"],
        "start_time": input_payload["start_time"],
        "end_time": input_payload["end_time"],
        "idempotency_key": input_payload["idempotency_key"],
    }
    for key in ("source_evidence_id", "user_instruction_ref"):
        if key in input_payload:
            payload[key] = input_payload[key]
    if isinstance(input_payload.get("description"), str):
        payload["description"] = input_payload["description"]
    if isinstance(input_payload.get("location"), str):
        payload["location"] = input_payload["location"]
    attendees_raw = input_payload.get("attendees", [])
    attendees = attendees_raw if isinstance(attendees_raw, list) else []
    if attendees:
        payload["attendees"] = attendees
    return [
        {
            "destination": (
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
            ),
            "payload": payload,
        }
    ]


def _declare_google_calendar_update_event_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    calendar_id = quote(str(input_payload.get("calendar_id") or "primary"), safe="")
    event_id = quote(str(input_payload["event_id"]), safe="")
    payload = {
        "event_id": input_payload["event_id"],
        "idempotency_key": input_payload["idempotency_key"],
    }
    for key in ("source_evidence_id", "user_instruction_ref"):
        if key in input_payload:
            payload[key] = input_payload[key]
    for key in ("title", "start_time", "end_time", "description", "location", "attendees"):
        if key in input_payload:
            payload[key] = input_payload[key]
    return [
        {
            "destination": (
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"
            ),
            "payload": payload,
        }
    ]


def _declare_google_calendar_respond_to_event_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    calendar_id = quote(str(input_payload.get("calendar_id") or "primary"), safe="")
    event_id = quote(str(input_payload["event_id"]), safe="")
    return [
        {
            "destination": (
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"
            ),
            "payload": {
                "event_id": input_payload["event_id"],
                "attendee_email": input_payload["attendee_email"],
                "response_status": input_payload["response_status"],
                "idempotency_key": input_payload["idempotency_key"],
                "source_evidence_id": input_payload.get("source_evidence_id"),
                "user_instruction_ref": input_payload.get("user_instruction_ref"),
            },
        }
    ]


def _declare_google_email_draft_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            "payload": {
                "to": input_payload["to"],
                "cc": input_payload["cc"],
                "bcc": input_payload["bcc"],
                "subject": input_payload["subject"],
            },
        }
    ]


def _declare_google_email_send_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            "payload": {
                "to": input_payload["to"],
                "cc": input_payload["cc"],
                "bcc": input_payload["bcc"],
                "subject": input_payload["subject"],
            },
        }
    ]


def _declare_google_email_archive_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            "payload": {
                "message_ids": input_payload["message_ids"],
                "remove_label_names": ["INBOX"],
            },
        }
    ]


def _declare_google_email_trash_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": (
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/trash"
            ),
            "payload": {"message_id": message_id},
        }
        for message_id in input_payload["message_ids"]
    ]


def _declare_google_email_labels_modify_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            "payload": {
                "message_ids": input_payload["message_ids"],
                "add_labels": input_payload["add_labels"],
                "remove_labels": input_payload["remove_labels"],
            },
        }
    ]


def _declare_google_email_undo_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    del input_payload
    return [
        {
            "destination": "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            "payload": {"operation": "undo"},
        }
    ]


def _declare_google_drive_search_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "https://www.googleapis.com/drive/v3/files",
            "payload": {"query": input_payload["query"]},
        }
    ]


def _declare_google_drive_read_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    file_id = input_payload["file_id"]
    return [
        {
            "destination": f"https://www.googleapis.com/drive/v3/files/{file_id}",
            "payload": {"file_id": file_id},
        }
    ]


def _declare_google_drive_share_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    file_id = input_payload["file_id"]
    payload = {
        "file_id": file_id,
        "grantee_email": input_payload["grantee_email"],
        "role": input_payload["role"],
        "idempotency_key": input_payload["idempotency_key"],
    }
    for key in ("source_evidence_id", "user_instruction_ref"):
        if key in input_payload:
            payload[key] = input_payload[key]
    return [
        {
            "destination": f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
            "payload": payload,
        }
    ]


class _WeatherProviderAdapter(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def endpoint(self) -> str: ...

    def declare_egress_intent(self, *, location: str, timeframe: str) -> list[dict[str, Any]]: ...

    def fetch_forecast(self, *, location: str, timeframe: str) -> dict[str, Any]: ...


def _weather_provider_mode() -> str:
    return AppSettings().weather_provider_mode


def _weather_production_endpoint() -> str:
    default_endpoint = "https://api.tomorrow.io/v4/weather/forecast"
    normalized = AppSettings().weather_production_endpoint.strip()
    if not normalized:
        return default_endpoint
    parsed = urlparse(normalized)
    if parsed.scheme:
        return normalized
    if "://" in normalized:
        return default_endpoint
    return f"https://{normalized.lstrip('/')}"


def _weather_dev_fallback_endpoint() -> str:
    default_endpoint = "https://wttr.in"
    normalized = AppSettings().weather_dev_endpoint.strip()
    if not normalized:
        return default_endpoint
    parsed = urlparse(normalized)
    if parsed.scheme:
        return normalized
    if "://" in normalized:
        return default_endpoint
    return f"https://{normalized.lstrip('/')}"


def _weather_production_timeout_seconds() -> float:
    return AppSettings().weather_production_timeout_seconds


def _weather_dev_timeout_seconds() -> float:
    return AppSettings().weather_dev_timeout_seconds


def _weather_production_api_key() -> str | None:
    return AppSettings().weather_production_api_key


def _weather_timesteps_for_timeframe(timeframe: str) -> str:
    if timeframe in {"today", "tomorrow"}:
        return "1d"
    return "1h"


def _build_weather_output(
    *,
    provider_id: str,
    source: str,
    location: str,
    timeframe: str,
    summary: str,
    forecast_timestamp: str | None,
) -> dict[str, Any]:
    retrieved_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    normalized_forecast_timestamp = (
        _normalize_optional_timestamp(forecast_timestamp) or retrieved_at
    )
    normalized_summary = summary.strip() or "forecast data available"
    return {
        "provider": provider_id,
        "location": location,
        "timeframe": timeframe,
        "forecast_timestamp": normalized_forecast_timestamp,
        "retrieved_at": retrieved_at,
        "results": [
            {
                "title": f"{provider_id} forecast for {location}",
                "source": source,
                "snippet": normalized_summary,
                "published_at": normalized_forecast_timestamp,
            }
        ],
    }


@dataclass(frozen=True, slots=True)
class _TomorrowIoWeatherAdapter:
    endpoint: str
    timeout_seconds: float
    api_key: str | None
    provider_id: str = "tomorrow.io"

    def declare_egress_intent(self, *, location: str, timeframe: str) -> list[dict[str, Any]]:
        return [
            {
                "destination": self.endpoint,
                "payload": {
                    "provider": self.provider_id,
                    "location": location,
                    "timeframe": timeframe,
                },
            }
        ]

    def fetch_forecast(self, *, location: str, timeframe: str) -> dict[str, Any]:
        if self.api_key is None:
            raise RuntimeError("weather provider credentials are not configured")
        endpoint_host = _endpoint_host(self.endpoint)
        endpoint_parsed = urlparse(self.endpoint)
        if endpoint_host is None or endpoint_parsed.scheme.lower() not in {"http", "https"}:
            raise RuntimeError("weather provider endpoint invalid")

        try:
            response = httpx.get(
                self.endpoint,
                params={
                    "location": location,
                    "timesteps": _weather_timesteps_for_timeframe(timeframe),
                    "apikey": self.api_key,
                },
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("weather provider timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("weather provider network failure") from exc

        if response.status_code == 429:
            raise RuntimeError("weather provider rate limited")
        if response.status_code >= 500:
            raise RuntimeError("weather provider upstream failure")
        if response.status_code >= 400:
            raise RuntimeError("weather provider request rejected")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("weather provider returned invalid json") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("weather provider returned invalid payload")

        timelines = payload.get("timelines")
        intervals: list[dict[str, Any]] = []
        if isinstance(timelines, dict):
            if timeframe in {"today", "tomorrow"}:
                daily_payload = timelines.get("daily")
                daily_index = 1 if timeframe == "tomorrow" else 0
                if isinstance(daily_payload, list) and len(daily_payload) > daily_index:
                    daily_item = daily_payload[daily_index]
                    if isinstance(daily_item, dict):
                        intervals.append(daily_item)
            else:
                hourly_payload = timelines.get("hourly")
                limit = 24 if timeframe == "next_24h" else 1
                if isinstance(hourly_payload, list):
                    intervals.extend(
                        item for item in hourly_payload[:limit] if isinstance(item, dict)
                    )
        value_sets: list[dict[str, Any]] = []
        for item in intervals:
            values = item.get("values")
            if isinstance(values, dict):
                value_sets.append(values)
        first_values = value_sets[0] if value_sets else {}

        summary_parts: list[str] = []
        temperatures: list[float] = []
        temperature_mins: list[float] = []
        temperature_maxes: list[float] = []
        wind_speeds: list[float] = []
        for values in value_sets:
            temperature = values.get("temperature")
            if isinstance(temperature, (int, float)):
                temperatures.append(float(temperature))
            temperature_min = values.get("temperatureMin")
            if isinstance(temperature_min, (int, float)):
                temperature_mins.append(float(temperature_min))
            temperature_max = values.get("temperatureMax")
            if isinstance(temperature_max, (int, float)):
                temperature_maxes.append(float(temperature_max))
            wind_speed = values.get("windSpeed")
            if isinstance(wind_speed, (int, float)):
                wind_speeds.append(float(wind_speed))
        if len(temperatures) > 1:
            summary_parts.append(f"temperature {min(temperatures)}-{max(temperatures)}C")
        elif temperatures:
            summary_parts.append(f"temperature {temperatures[0]}C")
        elif temperature_mins and temperature_maxes:
            summary_parts.append(f"temperature {min(temperature_mins)}-{max(temperature_maxes)}C")
        weather_code = first_values.get("weatherCode")
        if isinstance(weather_code, (int, float, str)):
            summary_parts.append(f"code {weather_code}")
        if wind_speeds:
            summary_parts.append(f"wind {max(wind_speeds)} m/s")
        summary = ", ".join(summary_parts) or "forecast data available"
        forecast_timestamp = intervals[0].get("time") if intervals else payload.get("updatedTime")
        return _build_weather_output(
            provider_id=self.provider_id,
            source=self.endpoint,
            location=location,
            timeframe=timeframe,
            summary=summary,
            forecast_timestamp=forecast_timestamp if isinstance(forecast_timestamp, str) else None,
        )


@dataclass(frozen=True, slots=True)
class _WttrDevWeatherAdapter:
    endpoint: str
    timeout_seconds: float
    provider_id: str = "wttr.dev"

    def declare_egress_intent(self, *, location: str, timeframe: str) -> list[dict[str, Any]]:
        return [
            {
                "destination": self.endpoint,
                "payload": {
                    "provider": self.provider_id,
                    "location": location,
                    "timeframe": timeframe,
                },
            }
        ]

    def fetch_forecast(self, *, location: str, timeframe: str) -> dict[str, Any]:
        endpoint_host = _endpoint_host(self.endpoint)
        endpoint_parsed = urlparse(self.endpoint)
        if endpoint_host is None or endpoint_parsed.scheme.lower() not in {"http", "https"}:
            raise RuntimeError("weather provider endpoint invalid")

        encoded_location = quote(location.strip(), safe="")
        request_url = f"{self.endpoint.rstrip('/')}/{encoded_location}"
        try:
            response = httpx.get(
                request_url,
                params={"format": "j1"},
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("weather provider timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("weather provider network failure") from exc

        if response.status_code == 429:
            raise RuntimeError("weather provider rate limited")
        if response.status_code >= 500:
            raise RuntimeError("weather provider upstream failure")
        if response.status_code >= 400:
            raise RuntimeError("weather provider request rejected")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("weather provider returned invalid json") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("weather provider returned invalid payload")

        current = None
        current_condition = payload.get("current_condition")
        if (
            isinstance(current_condition, list)
            and current_condition
            and isinstance(current_condition[0], dict)
        ):
            current = current_condition[0]

        summary_parts: list[str] = []
        if isinstance(current, dict):
            weather_desc = current.get("weatherDesc")
            if (
                isinstance(weather_desc, list)
                and weather_desc
                and isinstance(weather_desc[0], dict)
            ):
                description = weather_desc[0].get("value")
                if isinstance(description, str) and description.strip():
                    summary_parts.append(description.strip())
            temp_c = current.get("temp_C")
            if isinstance(temp_c, str) and temp_c.strip():
                summary_parts.append(f"{temp_c.strip()}C")
        summary = ", ".join(summary_parts) or "forecast data available"

        forecast_timestamp: str | None = None
        weather_payload = payload.get("weather")
        if (
            isinstance(weather_payload, list)
            and weather_payload
            and isinstance(weather_payload[0], dict)
        ):
            date_value = weather_payload[0].get("date")
            if isinstance(date_value, str) and date_value.strip():
                forecast_timestamp = f"{date_value.strip()}T00:00:00Z"
        return _build_weather_output(
            provider_id=self.provider_id,
            source=self.endpoint,
            location=location,
            timeframe=timeframe,
            summary=summary,
            forecast_timestamp=forecast_timestamp,
        )


def _weather_provider_adapter() -> _WeatherProviderAdapter:
    if _weather_provider_mode() == "dev_fallback":
        return _WttrDevWeatherAdapter(
            endpoint=_weather_dev_fallback_endpoint(),
            timeout_seconds=_weather_dev_timeout_seconds(),
        )
    return _TomorrowIoWeatherAdapter(
        endpoint=_weather_production_endpoint(),
        timeout_seconds=_weather_production_timeout_seconds(),
        api_key=_weather_production_api_key(),
    )


def _weather_allowed_destinations() -> tuple[str, ...]:
    if _weather_provider_mode() == "dev_fallback":
        dev_host = _endpoint_host(_weather_dev_fallback_endpoint())
        return (dev_host,) if dev_host is not None else ("wttr.in",)
    production_host = _endpoint_host(_weather_production_endpoint())
    return (production_host,) if production_host is not None else ("api.tomorrow.io",)


def _declare_weather_forecast_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    location_raw = input_payload.get("location")
    timeframe_raw = input_payload.get("timeframe")
    location = location_raw if isinstance(location_raw, str) else ""
    timeframe = timeframe_raw if isinstance(timeframe_raw, str) else "today"
    adapter = _weather_provider_adapter()
    return adapter.declare_egress_intent(location=location, timeframe=timeframe)


def _execute_weather_forecast(input_payload: dict[str, Any]) -> dict[str, Any]:
    location_raw = input_payload.get("location")
    if not isinstance(location_raw, str) or not location_raw.strip():
        raise RuntimeError("weather_location_required")
    timeframe_raw = input_payload.get("timeframe")
    timeframe = (
        timeframe_raw if isinstance(timeframe_raw, str) and timeframe_raw.strip() else "today"
    )
    adapter = _weather_provider_adapter()
    return adapter.fetch_forecast(
        location=location_raw.strip(), timeframe=timeframe.strip().lower()
    )


def _execute_agency_runtime(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("agency_runtime_not_bound")


def _declare_agency_run_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": "agency.daemon.local",
            "payload": {
                "repo_root": input_payload["repo_root"],
                "name": input_payload["name"],
                "base_branch": input_payload.get("base_branch"),
                "runner": input_payload.get("runner"),
            },
        }
    ]


def _declare_agency_request_pr_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": "agency.daemon.local",
            "payload": {
                "job_id": input_payload.get("job_id"),
                "repo_id": input_payload.get("repo_id"),
                "task_id": input_payload.get("task_id"),
                "invocation_id": input_payload.get("invocation_id"),
                "worktree_id": input_payload.get("worktree_id"),
            },
        }
    ]


def _execute_attachment_runtime(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("attachment_runtime_not_bound")


_CAPABILITY_REGISTRY: dict[str, CapabilityDefinition] = {
    "cap.calendar.list": CapabilityDefinition(
        capability_id="cap.calendar.list",
        version="2.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "calendar_window_v1",
            "output_schema": "google_calendar_events_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_CALENDAR_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_list_input,
        execute=None,
        declare_egress_intent=_declare_google_calendar_list_egress_intent,
    ),
    "cap.calendar.propose_slots": CapabilityDefinition(
        capability_id="cap.calendar.propose_slots",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "calendar_slot_planning_v1",
            "output_schema": "calendar_slot_options_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_CALENDAR_READ_SCOPE],
            "attendee_intersection_scope": _GOOGLE_CALENDAR_FREEBUSY_SCOPE,
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_propose_slots_input,
        execute=None,
        declare_egress_intent=_declare_google_calendar_propose_slots_egress_intent,
    ),
    "cap.email.search": CapabilityDefinition(
        capability_id="cap.email.search",
        version="2.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "email_search_v1",
            "output_schema": "google_gmail_message_refs_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_GMAIL_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_search_input,
        execute=None,
        declare_egress_intent=_declare_google_email_search_egress_intent,
    ),
    "cap.email.read": CapabilityDefinition(
        capability_id="cap.email.read",
        version="2.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "email_read_v1",
            "output_schema": "google_gmail_message_evidence_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_GMAIL_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_read_input,
        execute=None,
        declare_egress_intent=_declare_google_email_read_egress_intent,
    ),
    "cap.drive.search": CapabilityDefinition(
        capability_id="cap.drive.search",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "drive_search_query_v1",
            "output_schema": "drive_search_results_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_DRIVE_METADATA_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_drive_search_input,
        execute=None,
        declare_egress_intent=_declare_google_drive_search_egress_intent,
    ),
    "cap.drive.read": CapabilityDefinition(
        capability_id="cap.drive.read",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "drive_read_v1",
            "output_schema": "drive_read_result_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_DRIVE_READ_SCOPE],
            "bounded_output": "excerpt_and_typed_outcome",
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_drive_read_input,
        execute=None,
        declare_egress_intent=_declare_google_drive_read_egress_intent,
    ),
    "cap.maps.directions": CapabilityDefinition(
        capability_id="cap.maps.directions",
        version="2.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "maps_directions_query_v2",
            "output_schema": "maps_directions_result_v2",
            "idempotency": "deterministic_read",
        },
        allowed_egress_destinations=(_MAPS_ROUTES_HOST,),
        validate_input=_validate_maps_directions_input,
        execute=_execute_maps_directions,
        declare_egress_intent=_declare_maps_directions_egress_intent,
    ),
    "cap.maps.search_places": CapabilityDefinition(
        capability_id="cap.maps.search_places",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "maps_search_places_query_v1",
            "output_schema": "maps_search_places_result_v2",
            "idempotency": "deterministic_read",
        },
        allowed_egress_destinations=(_MAPS_GEOCODE_HOST, _MAPS_PLACES_HOST),
        validate_input=_validate_maps_search_places_input,
        execute=_execute_maps_search_places,
        declare_egress_intent=_declare_maps_search_places_egress_intent,
    ),
    "cap.calendar.create_event": CapabilityDefinition(
        capability_id="cap.calendar.create_event",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "calendar_create_event_v1",
            "output_schema": "calendar_create_result_v1",
            "idempotency": "client_idempotency_key",
            "required_scopes": [_GOOGLE_CALENDAR_WRITE_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_create_event_input,
        execute=None,
        declare_egress_intent=_declare_google_calendar_create_event_egress_intent,
    ),
    "cap.calendar.update_event": CapabilityDefinition(
        capability_id="cap.calendar.update_event",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "calendar_update_event_v1",
            "output_schema": "calendar_update_result_v1",
            "idempotency": "client_idempotency_key",
            "required_scopes": [_GOOGLE_CALENDAR_WRITE_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_update_event_input,
        execute=None,
        declare_egress_intent=_declare_google_calendar_update_event_egress_intent,
    ),
    "cap.calendar.respond_to_event": CapabilityDefinition(
        capability_id="cap.calendar.respond_to_event",
        version="1.0",
        impact_level="external_send",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "calendar_respond_to_event_v1",
            "output_schema": "calendar_response_result_v1",
            "idempotency": "client_idempotency_key",
            "required_scopes": [_GOOGLE_CALENDAR_WRITE_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_respond_to_event_input,
        execute=None,
        declare_egress_intent=_declare_google_calendar_respond_to_event_egress_intent,
    ),
    "cap.email.draft": CapabilityDefinition(
        capability_id="cap.email.draft",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "email_compose_v1",
            "output_schema": "email_draft_result_v1",
            "idempotency": "action_attempt_id",
            "required_scopes": [_GOOGLE_GMAIL_COMPOSE_SCOPE],
            "delivery_state": "draft_only",
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_draft_input,
        execute=None,
        declare_egress_intent=_declare_google_email_draft_egress_intent,
    ),
    "cap.email.send": CapabilityDefinition(
        capability_id="cap.email.send",
        version="1.0",
        impact_level="external_send",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "email_compose_v1",
            "output_schema": "email_send_result_v1",
            "idempotency": "action_attempt_id",
            "required_scopes": [_GOOGLE_GMAIL_SEND_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_send_input,
        execute=None,
        declare_egress_intent=_declare_google_email_send_egress_intent,
    ),
    "cap.email.archive": CapabilityDefinition(
        capability_id="cap.email.archive",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "email_message_batch_mutation_v1",
            "output_schema": "email_mailbox_mutation_result_v1",
            "idempotency": "client_key",
            "required_scopes": [_GOOGLE_GMAIL_MODIFY_SCOPE],
            "mutation": "remove_inbox_label",
            "undo": "required",
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_message_batch_mutation_input,
        execute=None,
        declare_egress_intent=_declare_google_email_archive_egress_intent,
    ),
    "cap.email.trash": CapabilityDefinition(
        capability_id="cap.email.trash",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "email_message_batch_mutation_v1",
            "output_schema": "email_mailbox_mutation_result_v1",
            "idempotency": "client_key",
            "required_scopes": [_GOOGLE_GMAIL_MODIFY_SCOPE],
            "mutation": "move_to_trash",
            "permanent_delete": False,
            "undo": "required",
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_message_batch_mutation_input,
        execute=None,
        declare_egress_intent=_declare_google_email_trash_egress_intent,
    ),
    "cap.email.labels.modify": CapabilityDefinition(
        capability_id="cap.email.labels.modify",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "email_labels_modify_v1",
            "output_schema": "email_label_mutation_result_v1",
            "idempotency": "client_key",
            "required_scopes": [_GOOGLE_GMAIL_MODIFY_SCOPE],
            "label_resolution": "execution_time_with_action_record_reuse",
            "undo": "required",
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_labels_modify_input,
        execute=None,
        declare_egress_intent=_declare_google_email_labels_modify_egress_intent,
    ),
    "cap.email.undo": CapabilityDefinition(
        capability_id="cap.email.undo",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "email_undo_v1",
            "output_schema": "email_undo_result_v1",
            "idempotency": "client_key",
            "required_scopes": [_GOOGLE_GMAIL_MODIFY_SCOPE],
            "undo_source": "provider_write_receipt",
            "supported_capabilities": [
                "cap.email.archive",
                "cap.email.trash",
                "cap.email.labels.modify",
            ],
        },
        allowed_egress_destinations=_GOOGLE_GMAIL_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_undo_input,
        execute=None,
        declare_egress_intent=_declare_google_email_undo_egress_intent,
    ),
    "cap.drive.share": CapabilityDefinition(
        capability_id="cap.drive.share",
        version="1.0",
        impact_level="external_send",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "drive_share_v1",
            "output_schema": "drive_share_result_v1",
            "idempotency": "client_idempotency_key",
            "required_scopes": [_GOOGLE_DRIVE_SHARE_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_drive_share_input,
        execute=None,
        declare_egress_intent=_declare_google_drive_share_egress_intent,
    ),
    "cap.web.extract": CapabilityDefinition(
        capability_id="cap.web.extract",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "url_extract_v1",
            "output_schema": "url_extract_result_v1",
            "idempotency": "deterministic_read",
            "execution_mode": "capability_runtime_only",
            "bounded_output": "structured_blocks_with_partial_disclosure",
            "safety_preflight": "strict_fail_closed",
        },
        allowed_egress_destinations=("api.search.brave.com",),
        validate_input=_validate_web_extract_input,
        execute=_execute_web_extract,
        declare_egress_intent=_declare_web_extract_egress_intent,
    ),
    "cap.search.web": CapabilityDefinition(
        capability_id="cap.search.web",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "search_query_v1",
            "output_schema": "search_results_v1",
            "idempotency": "deterministic_read",
        },
        allowed_egress_destinations=("api.search.brave.com",),
        validate_input=_validate_search_web_input,
        execute=_execute_search_web,
        declare_egress_intent=_declare_search_web_egress_intent,
    ),
    "cap.search.news": CapabilityDefinition(
        capability_id="cap.search.news",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "search_query_v1",
            "output_schema": "search_results_v1",
            "idempotency": "deterministic_read",
        },
        allowed_egress_destinations=("api.search.brave.com",),
        validate_input=_validate_search_news_input,
        execute=_execute_search_news,
        declare_egress_intent=_declare_search_news_egress_intent,
    ),
    "cap.weather.forecast": CapabilityDefinition(
        capability_id="cap.weather.forecast",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "weather_forecast_query_v1",
            "output_schema": "weather_forecast_v1",
            "idempotency": "deterministic_read",
            "provider_mode": {
                "default": "production",
                "fallback": "dev_fallback",
            },
        },
        allowed_egress_destinations=("api.tomorrow.io",),
        validate_input=_validate_weather_forecast_input,
        execute=_execute_weather_forecast,
        declare_egress_intent=_declare_weather_forecast_egress_intent,
    ),
    "cap.attachment.read": CapabilityDefinition(
        capability_id="cap.attachment.read",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "attachment_read_v1",
            "output_schema": "attachment_read_result_v1",
            "idempotency": "deterministic_read",
            "execution_mode": "attachment_runtime_only",
            "bounded_output": "structured_blocks_with_typed_outcome",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_attachment_read_input,
        execute=_execute_attachment_runtime,
    ),
    "cap.agency.run": CapabilityDefinition(
        capability_id="cap.agency.run",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "agency_run_v1",
            "output_schema": "agency_task_start_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "agency_daemon_unix_socket",
        },
        allowed_egress_destinations=("agency.daemon.local",),
        validate_input=_validate_agency_run_input,
        execute=_execute_agency_runtime,
        declare_egress_intent=_declare_agency_run_egress_intent,
    ),
    "cap.agency.status": CapabilityDefinition(
        capability_id="cap.agency.status",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "agency_job_lookup_v1",
            "output_schema": "agency_status_v1",
            "idempotency": "deterministic_read",
            "execution_mode": "agency_daemon_unix_socket",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_agency_job_lookup_input,
        execute=_execute_agency_runtime,
    ),
    "cap.agency.artifacts": CapabilityDefinition(
        capability_id="cap.agency.artifacts",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "agency_job_lookup_v1",
            "output_schema": "agency_artifacts_v1",
            "idempotency": "deterministic_read",
            "execution_mode": "agency_daemon_unix_socket",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_agency_job_lookup_input,
        execute=_execute_agency_runtime,
    ),
    "cap.agency.request_pr": CapabilityDefinition(
        capability_id="cap.agency.request_pr",
        version="1.0",
        impact_level="external_send",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "agency_request_pr_v1",
            "output_schema": "agency_request_pr_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "agency_daemon_unix_socket",
        },
        allowed_egress_destinations=("agency.daemon.local",),
        validate_input=_validate_agency_request_pr_input,
        execute=_execute_agency_runtime,
        declare_egress_intent=_declare_agency_request_pr_egress_intent,
    ),
    "cap.memory.recall": CapabilityDefinition(
        capability_id="cap.memory.recall",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_recall_v1",
            "output_schema": "memory_recall_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_recall_input,
        execute=None,
    ),
    "cap.memory.remember": CapabilityDefinition(
        capability_id="cap.memory.remember",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_remember_v1",
            "output_schema": "memory_remember_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_remember_input,
        execute=None,
    ),
    "cap.memory.search": CapabilityDefinition(
        capability_id="cap.memory.search",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_search_v1",
            "output_schema": "memory_search_result_v1",
            "idempotency": "deterministic_read",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_search_input,
        execute=None,
    ),
    "cap.memory.read": CapabilityDefinition(
        capability_id="cap.memory.read",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_read_v1",
            "output_schema": "memory_read_result_v1",
            "idempotency": "deterministic_read",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_read_input,
        execute=None,
    ),
    "cap.memory.note.create": CapabilityDefinition(
        capability_id="cap.memory.note.create",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_note_create_v1",
            "output_schema": "memory_note_create_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_note_create_input,
        execute=None,
    ),
    "cap.memory.note.edit": CapabilityDefinition(
        capability_id="cap.memory.note.edit",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_note_edit_v1",
            "output_schema": "memory_note_edit_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_note_edit_input,
        execute=None,
    ),
    "cap.memory.note.delete": CapabilityDefinition(
        capability_id="cap.memory.note.delete",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "memory_note_delete_v1",
            "output_schema": "memory_note_delete_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "memory_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_memory_note_delete_input,
        execute=None,
    ),
    "cap.proactive.schedule": CapabilityDefinition(
        capability_id="cap.proactive.schedule",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "proactive_schedule_v1",
            "output_schema": "proactive_schedule_result_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "scheduler_runtime_only",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_proactive_schedule_input,
        execute=None,
    ),
    "cap.research.investigate": CapabilityDefinition(
        capability_id="cap.research.investigate",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "research_investigate_v1",
            "output_schema": "research_task_start_v1",
            "idempotency": "action_attempt_id",
            "execution_mode": "background_task_enqueue",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_research_investigate_input,
        execute=None,
    ),
}


def get_capability(capability_id: str) -> CapabilityDefinition | None:
    capability = _CAPABILITY_REGISTRY.get(capability_id)
    if capability is None:
        return None
    if capability_id == "cap.search.web":
        return replace(capability, allowed_egress_destinations=_search_web_allowed_destinations())
    if capability_id == "cap.search.news":
        return replace(capability, allowed_egress_destinations=_search_news_allowed_destinations())
    if capability_id == "cap.weather.forecast":
        return replace(capability, allowed_egress_destinations=_weather_allowed_destinations())
    if capability_id == "cap.web.extract":
        return replace(capability, allowed_egress_destinations=_web_extract_allowed_destinations())
    return capability


_ACTION_LABELS_BY_CAPABILITY_ID = {
    "cap.agency.run": "Start coding job",
    "cap.agency.request_pr": "Create or update pull request",
    "cap.calendar.create_event": "Create calendar event",
    "cap.calendar.update_event": "Update calendar event",
    "cap.calendar.respond_to_event": "Respond to calendar event",
    "cap.drive.share": "Share Drive file",
    "cap.email.archive": "Archive email",
    "cap.email.draft": "Draft email",
    "cap.email.labels.modify": "Update email labels",
    "cap.email.send": "Send email",
    "cap.email.trash": "Move email to trash",
    "cap.email.undo": "Undo email change",
}


def capability_action_label(capability_id: str) -> str:
    label = _ACTION_LABELS_BY_CAPABILITY_ID.get(capability_id)
    if label is not None:
        return label
    if capability_id.startswith("cap.agency."):
        return "Agency action"
    if capability_id.startswith("cap.calendar."):
        return "Calendar action"
    if capability_id.startswith("cap.drive."):
        return "Drive action"
    if capability_id.startswith("cap.email."):
        return "Email action"
    if capability_id.startswith("cap.memory."):
        return "Memory action"
    return "Action"


def internal_callable_capability_ids() -> list[str]:
    return list(_CAPABILITY_REGISTRY)


_RUN_CALLABLE_ALIASES = {
    "agency.artifacts": "cap.agency.artifacts",
    "agency.request_pr": "cap.agency.request_pr",
    "agency.run": "cap.agency.run",
    "agency.status": "cap.agency.status",
    "attachment.read": "cap.attachment.read",
    "calendar.create_event": "cap.calendar.create_event",
    "calendar.list": "cap.calendar.list",
    "calendar.propose_slots": "cap.calendar.propose_slots",
    "calendar.respond_to_event": "cap.calendar.respond_to_event",
    "calendar.update_event": "cap.calendar.update_event",
    "drive.read": "cap.drive.read",
    "drive.search": "cap.drive.search",
    "drive.share": "cap.drive.share",
    "email.archive": "cap.email.archive",
    "email.draft": "cap.email.draft",
    "email.labels.modify": "cap.email.labels.modify",
    "email.read": "cap.email.read",
    "email.search": "cap.email.search",
    "email.send": "cap.email.send",
    "email.trash": "cap.email.trash",
    "email.undo": "cap.email.undo",
    "maps.directions": "cap.maps.directions",
    "maps.search_places": "cap.maps.search_places",
    "memory.note.create": "cap.memory.note.create",
    "memory.note.delete": "cap.memory.note.delete",
    "memory.note.edit": "cap.memory.note.edit",
    "memory.read": "cap.memory.read",
    "memory.recall": "cap.memory.recall",
    "memory.remember": "cap.memory.remember",
    "memory.search": "cap.memory.search",
    "proactive.schedule": "cap.proactive.schedule",
    "research.investigate": "cap.research.investigate",
    "search.news": "cap.search.news",
    "search.web": "cap.search.web",
    "weather.forecast": "cap.weather.forecast",
    "web.extract": "cap.web.extract",
}


def capability_id_for_run_callable(name: str) -> str | None:
    return _RUN_CALLABLE_ALIASES.get(name)


def run_callable_name_for_capability_id(capability_id: str) -> str | None:
    for name, mapped_capability_id in _RUN_CALLABLE_ALIASES.items():
        if mapped_capability_id == capability_id:
            return name
    return None


def capability_contract_hash(capability: CapabilityDefinition) -> str:
    contract_payload = {
        "capability_id": capability.capability_id,
        "version": capability.version,
        "impact_level": capability.impact_level,
        "policy_decision": capability.policy_decision,
        "contract_metadata": capability.contract_metadata,
        "allowed_egress_destinations": sorted(capability.allowed_egress_destinations),
    }
    canonical = json.dumps(contract_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_action_payload(
    *, capability_id: str, input_payload: dict[str, Any]
) -> dict[str, Any]:
    return {"capability_id": capability_id, "input": input_payload}


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
