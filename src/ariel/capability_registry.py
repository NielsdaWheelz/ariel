from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from ipaddress import ip_address
import hashlib
import json
import os
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx
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


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    version: str
    impact_level: str
    policy_decision: PolicyDecision
    contract_metadata: dict[str, Any]
    allowed_egress_destinations: tuple[str, ...]
    validate_input: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str | None]]
    execute: Callable[[dict[str, Any]], dict[str, Any]]
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


def _validate_read_echo_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="text", max_length=4000)


def _validate_read_private_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="text", max_length=4000)


def _validate_write_note_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="note", max_length=500)


def _validate_write_draft_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="note", max_length=500)


def _validate_search_web_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


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
    if not set(raw_input.keys()).issubset(
        {"window_start", "window_end", "duration_minutes", "attendees"}
    ):
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

    return {
        "window_start": window_start,
        "window_end": window_end,
        "duration_minutes": duration_raw,
        "attendees": attendees,
    }, None


def _validate_email_search_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


def _validate_email_read_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="message_id", max_length=256)


def _validate_drive_search_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


def _validate_drive_read_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="file_id", max_length=256)


_MAPS_ALLOWED_TRAVEL_MODES = {"driving", "walking", "bicycling", "transit"}


def _validate_maps_directions_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"origin", "destination", "travel_mode"}):
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

    return {
        "origin": origin,
        "destination": destination,
        "travel_mode": travel_mode,
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


def _validate_drive_share_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"file_id", "grantee_email", "role"}):
        return None, "schema_invalid"

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
    }, None


def _validate_calendar_create_event_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset(
        {"title", "start_time", "end_time", "description", "location", "attendees"}
    ):
        return None, "schema_invalid"

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

    return {
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "description": description,
        "location": location,
        "attendees": attendees,
    }, None


def _validate_email_composition_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not set(raw_input.keys()).issubset({"to", "cc", "bcc", "subject", "body"}):
        return None, "schema_invalid"

    to_recipients = _normalize_email_recipients(raw_input.get("to"))
    cc_recipients = _normalize_email_recipients(raw_input.get("cc"))
    bcc_recipients = _normalize_email_recipients(raw_input.get("bcc"))
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
    }, None


def _validate_email_draft_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_email_composition_input(raw_input)


def _validate_email_send_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_email_composition_input(raw_input)


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


def _validate_external_notify_input(
    raw_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if set(raw_input.keys()) != {"destination", "message"}:
        return None, "schema_invalid"
    destination_raw = raw_input.get("destination")
    message_raw = raw_input.get("message")
    if not isinstance(destination_raw, str) or not isinstance(message_raw, str):
        return None, "schema_invalid"
    destination = destination_raw.strip()
    message = message_raw.strip()
    if not destination or not message:
        return None, "schema_invalid"
    if len(destination) > 500 or len(message) > 500:
        return None, "schema_invalid"
    return {"destination": destination, "message": message}, None


def _execute_read_echo(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"text": input_payload["text"]}


def _execute_read_private(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"text": input_payload["text"], "classification": "private"}


def _execute_write_note(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": "recorded", "note": input_payload["note"]}


def _execute_write_draft(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": "drafted", "note": input_payload["note"]}


def _search_brave_base_url() -> str:
    default_base_url = "https://api.search.brave.com/res/v1"
    configured_base_url = os.getenv("ARIEL_SEARCH_BRAVE_BASE_URL")
    if configured_base_url is None:
        return default_base_url
    normalized = configured_base_url.strip().rstrip("/")
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
    configured_timeout = os.getenv("ARIEL_SEARCH_WEB_TIMEOUT_SECONDS")
    if configured_timeout is None:
        return 8.0
    normalized = configured_timeout.strip()
    if not normalized:
        return 8.0
    try:
        parsed = float(normalized)
    except ValueError:
        return 8.0
    if parsed <= 0:
        return 8.0
    return parsed


def _search_web_api_key() -> str | None:
    configured_api_key = os.getenv("ARIEL_SEARCH_WEB_API_KEY")
    if configured_api_key is None:
        return None
    normalized = configured_api_key.strip()
    return normalized or None


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
    configured_timeout = os.getenv("ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS")
    if configured_timeout is None:
        return 8.0
    normalized = configured_timeout.strip()
    if not normalized:
        return 8.0
    try:
        parsed = float(normalized)
    except ValueError:
        return 8.0
    if parsed <= 0:
        return 8.0
    return parsed


def _search_news_api_key() -> str | None:
    configured_api_key = os.getenv("ARIEL_SEARCH_NEWS_API_KEY")
    if configured_api_key is None:
        return _search_web_api_key()
    normalized = configured_api_key.strip()
    return normalized or _search_web_api_key()


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
    configured_endpoint = os.getenv("ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT")
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
    configured_timeout = os.getenv("ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS")
    if configured_timeout is None:
        return 10.0
    normalized = configured_timeout.strip()
    if not normalized:
        return 10.0
    try:
        parsed = float(normalized)
    except ValueError:
        return 10.0
    if parsed <= 0:
        return 10.0
    return parsed


def _web_extract_max_retries() -> int:
    configured_retries = os.getenv("ARIEL_WEB_EXTRACT_MAX_RETRIES")
    if configured_retries is None:
        return 2
    normalized = configured_retries.strip()
    if not normalized:
        return 2
    try:
        parsed = int(normalized)
    except ValueError:
        return 2
    if parsed < 0:
        return 2
    return min(parsed, 5)


def _web_extract_api_key() -> str | None:
    configured_api_key = os.getenv("ARIEL_WEB_EXTRACT_API_KEY")
    if configured_api_key is None:
        return _search_web_api_key()
    normalized = configured_api_key.strip()
    return normalized or _search_web_api_key()


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


def _maps_provider_endpoint() -> str:
    default_endpoint = "https://maps.googleapis.com/maps/api"
    configured_endpoint = os.getenv("ARIEL_MAPS_PROVIDER_ENDPOINT")
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


def _maps_directions_endpoint() -> str:
    return f"{_maps_provider_endpoint().rstrip('/')}/directions"


def _maps_search_places_endpoint() -> str:
    return f"{_maps_provider_endpoint().rstrip('/')}/search_places"


def _maps_provider_timeout_seconds() -> float:
    configured_timeout = os.getenv("ARIEL_MAPS_PROVIDER_TIMEOUT_SECONDS")
    if configured_timeout is None:
        return 8.0
    normalized = configured_timeout.strip()
    if not normalized:
        return 8.0
    try:
        parsed = float(normalized)
    except ValueError:
        return 8.0
    if parsed <= 0:
        return 8.0
    return parsed


def _maps_provider_api_key_encrypted() -> str | None:
    configured = os.getenv("ARIEL_MAPS_PROVIDER_API_KEY_ENC")
    if configured is None:
        return None
    normalized = configured.strip()
    return normalized or None


def _maps_connector_encryption_secret() -> str:
    configured = os.getenv("ARIEL_CONNECTOR_ENCRYPTION_SECRET")
    if configured is None:
        return "dev-local-connector-secret"
    normalized = configured.strip()
    return normalized or "dev-local-connector-secret"


def _maps_connector_encryption_key_version() -> str:
    configured = os.getenv("ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION")
    if configured is None:
        return "v1"
    normalized = configured.strip()
    return normalized or "v1"


def _maps_connector_encryption_keys() -> str | None:
    configured = os.getenv("ARIEL_CONNECTOR_ENCRYPTION_KEYS")
    if configured is None:
        return None
    normalized = configured.strip()
    return normalized or None


def _maps_provider_api_key() -> str:
    # Import lazily to avoid pulling connector runtime dependencies into registry import-time.
    from ariel.google_connector import ConnectorTokenCipher

    encrypted_api_key = _maps_provider_api_key_encrypted()
    if encrypted_api_key is None:
        raise RuntimeError("provider_credentials_missing")
    try:
        cipher = ConnectorTokenCipher.from_config(
            active_key_version=_maps_connector_encryption_key_version(),
            configured_keys=_maps_connector_encryption_keys(),
            fallback_secret=_maps_connector_encryption_secret(),
        )
        api_key = cipher.decrypt(encrypted_api_key).strip()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("provider_credentials_invalid") from exc
    if not api_key:
        raise RuntimeError("provider_credentials_invalid")
    return api_key


def _maps_provider_unreachable(endpoint: str) -> bool:
    endpoint_host = _endpoint_host(endpoint)
    endpoint_parsed = urlparse(endpoint)
    if endpoint_host is None:
        return True
    if endpoint_parsed.scheme.lower() not in {"http", "https"}:
        return True
    return False


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


def _maps_route_candidate(payload: dict[str, Any]) -> dict[str, Any] | None:
    routes_raw = payload.get("routes")
    if isinstance(routes_raw, list):
        for raw_route in routes_raw:
            if isinstance(raw_route, dict):
                return raw_route
    if isinstance(routes_raw, dict):
        return routes_raw
    result_route = payload.get("route")
    if isinstance(result_route, dict):
        return result_route
    return None


def _maps_places_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    places_raw = payload.get("places")
    if not isinstance(places_raw, list):
        places_raw = payload.get("results")
    if not isinstance(places_raw, list):
        return []
    candidates: list[dict[str, Any]] = []
    for raw_place in places_raw:
        if isinstance(raw_place, dict):
            candidates.append(raw_place)
        if len(candidates) >= 5:
            break
    return candidates


def _build_maps_route_result(
    *,
    endpoint: str,
    route_payload: dict[str, Any],
    origin: str,
    destination: str,
    travel_mode: str,
) -> dict[str, Any]:
    title_raw = route_payload.get("title")
    summary_raw = route_payload.get("summary")
    title = (
        title_raw.strip()
        if isinstance(title_raw, str) and title_raw.strip()
        else (
            summary_raw.strip()
            if isinstance(summary_raw, str) and summary_raw.strip()
            else "Route guidance"
        )
    )
    source_raw = route_payload.get("source")
    route_url_raw = route_payload.get("route_url")
    if source_raw is None:
        source_raw = route_url_raw
    source = (
        source_raw.strip()
        if isinstance(source_raw, str) and source_raw.strip()
        else (
            f"{endpoint}?origin={quote(origin, safe='')}"
            f"&destination={quote(destination, safe='')}"
            f"&mode={quote(travel_mode, safe='')}"
        )
    )
    distance_meters = (
        _normalize_int_like(route_payload.get("distance_meters"))
        or _normalize_int_like(route_payload.get("distanceMeters"))
        or _normalize_int_like(route_payload.get("distance"))
    )
    duration_seconds = (
        _normalize_int_like(route_payload.get("duration_seconds"))
        or _normalize_int_like(route_payload.get("durationSeconds"))
        or _normalize_int_like(route_payload.get("duration"))
    )
    snippet_parts: list[str] = []
    if distance_meters is not None:
        snippet_parts.append(f"distance_meters={distance_meters}")
    if duration_seconds is not None:
        snippet_parts.append(f"duration_seconds={duration_seconds}")
    if isinstance(summary_raw, str) and summary_raw.strip():
        snippet_parts.append(summary_raw.strip())
    snippet = " ".join(snippet_parts) or "route evidence available"
    return {
        "title": title,
        "source": source,
        "snippet": snippet,
        "published_at": _normalize_optional_timestamp(route_payload.get("published_at")),
        "distance_meters": distance_meters,
        "duration_seconds": duration_seconds,
    }


def _build_maps_place_result(
    *, endpoint: str, place_payload: dict[str, Any]
) -> dict[str, Any] | None:
    name_raw = place_payload.get("name")
    title_raw = place_payload.get("title")
    title = (
        name_raw.strip()
        if isinstance(name_raw, str) and name_raw.strip()
        else (title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else None)
    )
    if title is None:
        return None
    source_raw = place_payload.get("source")
    maps_url_raw = place_payload.get("maps_url")
    if source_raw is None:
        source_raw = maps_url_raw
    source = (
        source_raw.strip()
        if isinstance(source_raw, str) and source_raw.strip()
        else f"{endpoint}?place={quote(title, safe='')}"
    )
    snippet_parts: list[str] = []
    address_raw = place_payload.get("address")
    if address_raw is None:
        address_raw = place_payload.get("formatted_address")
    if address_raw is None:
        address_raw = place_payload.get("vicinity")
    if isinstance(address_raw, str) and address_raw.strip():
        snippet_parts.append(f"address={address_raw.strip()}")
    distance_meters = (
        _normalize_int_like(place_payload.get("distance_meters"))
        or _normalize_int_like(place_payload.get("distanceMeters"))
        or _normalize_int_like(place_payload.get("distance"))
    )
    if distance_meters is not None:
        snippet_parts.append(f"distance_meters={distance_meters}")
    rating_raw = place_payload.get("rating")
    if isinstance(rating_raw, (int, float)):
        snippet_parts.append(f"rating={rating_raw}")
    open_now_raw = place_payload.get("open_now")
    if isinstance(open_now_raw, bool):
        snippet_parts.append(f"open_now={str(open_now_raw).lower()}")
    snippet = " ".join(snippet_parts) or "place evidence available"
    return {
        "title": title,
        "source": source,
        "snippet": snippet,
        "published_at": _normalize_optional_timestamp(place_payload.get("published_at")),
    }


def _execute_maps_directions(input_payload: dict[str, Any]) -> dict[str, Any]:
    origin_raw = input_payload.get("origin")
    destination_raw = input_payload.get("destination")
    if not isinstance(origin_raw, str) or not origin_raw.strip():
        raise RuntimeError("maps_origin_required")
    if not isinstance(destination_raw, str) or not destination_raw.strip():
        raise RuntimeError("maps_destination_required")
    origin = origin_raw.strip()
    destination = destination_raw.strip()
    travel_mode_raw = input_payload.get("travel_mode")
    travel_mode = (
        travel_mode_raw.strip().lower()
        if isinstance(travel_mode_raw, str) and travel_mode_raw.strip()
        else "driving"
    )

    api_key = _maps_provider_api_key()
    endpoint = _maps_directions_endpoint()
    if _maps_provider_unreachable(endpoint):
        raise RuntimeError("provider_unreachable")
    try:
        response = httpx.get(
            endpoint,
            params={
                "origin": origin,
                "destination": destination,
                "mode": travel_mode,
            },
            headers={"accept": "application/json", "x-api-key": api_key},
            timeout=_maps_provider_timeout_seconds(),
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError("provider_timeout") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("provider_network_failure") from exc

    if response.status_code == 429:
        raise RuntimeError("provider_rate_limited")
    if response.status_code >= 500:
        raise RuntimeError("provider_upstream_failure")
    if response.status_code in {401, 403}:
        raise RuntimeError("provider_permission_denied")
    if response.status_code >= 400:
        raise RuntimeError("provider_request_rejected")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("provider_invalid_payload") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("provider_invalid_payload")

    retrieved_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    route_payload = _maps_route_candidate(payload)
    results: list[dict[str, Any]] = []
    uncertainty: str | None = None
    distance_meters: int | None = None
    duration_seconds: int | None = None
    if route_payload is None:
        uncertainty = "insufficient_evidence"
    else:
        result = _build_maps_route_result(
            endpoint=endpoint,
            route_payload=route_payload,
            origin=origin,
            destination=destination,
            travel_mode=travel_mode,
        )
        distance_meters = result.pop("distance_meters")
        duration_seconds = result.pop("duration_seconds")
        results.append(result)
    return {
        "origin": origin,
        "destination": destination,
        "travel_mode": travel_mode,
        "distance_meters": distance_meters,
        "duration_seconds": duration_seconds,
        "retrieved_at": retrieved_at,
        "uncertainty": uncertainty,
        "results": results,
    }


def _execute_maps_search_places(input_payload: dict[str, Any]) -> dict[str, Any]:
    query_raw = input_payload.get("query")
    if not isinstance(query_raw, str) or not query_raw.strip():
        raise RuntimeError("provider_request_rejected")
    location_context_raw = input_payload.get("location_context")
    if not isinstance(location_context_raw, str) or not location_context_raw.strip():
        raise RuntimeError("maps_location_context_required")
    radius_raw = input_payload.get("radius_meters")
    radius_meters = radius_raw if isinstance(radius_raw, int) else 2000

    api_key = _maps_provider_api_key()
    endpoint = _maps_search_places_endpoint()
    if _maps_provider_unreachable(endpoint):
        raise RuntimeError("provider_unreachable")
    query = query_raw.strip()
    location_context = location_context_raw.strip()
    try:
        response = httpx.get(
            endpoint,
            params={
                "query": query,
                "location_context": location_context,
                "radius_meters": radius_meters,
            },
            headers={"accept": "application/json", "x-api-key": api_key},
            timeout=_maps_provider_timeout_seconds(),
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError("provider_timeout") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("provider_network_failure") from exc

    if response.status_code == 429:
        raise RuntimeError("provider_rate_limited")
    if response.status_code >= 500:
        raise RuntimeError("provider_upstream_failure")
    if response.status_code in {401, 403}:
        raise RuntimeError("provider_permission_denied")
    if response.status_code >= 400:
        raise RuntimeError("provider_request_rejected")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("provider_invalid_payload") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("provider_invalid_payload")

    retrieved_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    for candidate in _maps_places_candidates(payload):
        normalized = _build_maps_place_result(endpoint=endpoint, place_payload=candidate)
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
            "destination": _maps_directions_endpoint(),
            "payload": {
                "origin": input_payload.get("origin"),
                "destination": input_payload.get("destination"),
                "travel_mode": input_payload.get("travel_mode"),
            },
        }
    ]


def _declare_maps_search_places_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "destination": _maps_search_places_endpoint(),
            "payload": {
                "query": input_payload.get("query"),
                "location_context": input_payload.get("location_context"),
                "radius_meters": input_payload.get("radius_meters"),
            },
        }
    ]


def _maps_allowed_destinations() -> tuple[str, ...]:
    directions_host = _endpoint_host(_maps_directions_endpoint())
    places_host = _endpoint_host(_maps_search_places_endpoint())
    hosts = {host for host in (directions_host, places_host) if host is not None}
    if hosts:
        return tuple(sorted(hosts))
    return ("maps.googleapis.com",)


def _execute_google_calendar_list(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_calendar_propose_slots(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_email_search(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_email_read(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_calendar_create_event(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_email_draft(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_email_send(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_drive_search(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_drive_read(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


def _execute_google_drive_share(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("google_runtime_not_bound")


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
    return [
        {
            "destination": (
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{input_payload['message_id']}"
            ),
            "payload": {"message_id": input_payload["message_id"]},
        }
    ]


def _declare_google_calendar_create_event_egress_intent(
    input_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "title": input_payload["title"],
        "start_time": input_payload["start_time"],
        "end_time": input_payload["end_time"],
    }
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
            "destination": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "payload": payload,
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
    return [
        {
            "destination": f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
            "payload": {
                "file_id": file_id,
                "grantee_email": input_payload["grantee_email"],
                "role": input_payload["role"],
            },
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
    configured_mode = os.getenv("ARIEL_WEATHER_PROVIDER_MODE")
    if configured_mode is None:
        return "production"
    normalized = configured_mode.strip().lower()
    if normalized in {"dev", "dev_fallback", "fallback"}:
        return "dev_fallback"
    return "production"


def _weather_production_endpoint() -> str:
    default_endpoint = "https://api.tomorrow.io/v4/weather/forecast"
    configured = os.getenv("ARIEL_WEATHER_PRODUCTION_ENDPOINT")
    if configured is None:
        return default_endpoint
    normalized = configured.strip()
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
    configured = os.getenv("ARIEL_WEATHER_DEV_ENDPOINT")
    if configured is None:
        return default_endpoint
    normalized = configured.strip()
    if not normalized:
        return default_endpoint
    parsed = urlparse(normalized)
    if parsed.scheme:
        return normalized
    if "://" in normalized:
        return default_endpoint
    return f"https://{normalized.lstrip('/')}"


def _weather_timeout_seconds(*, env_key: str, default: float) -> float:
    configured_timeout = os.getenv(env_key)
    if configured_timeout is None:
        return default
    normalized = configured_timeout.strip()
    if not normalized:
        return default
    try:
        parsed = float(normalized)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


def _weather_production_timeout_seconds() -> float:
    return _weather_timeout_seconds(env_key="ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS", default=8.0)


def _weather_dev_timeout_seconds() -> float:
    return _weather_timeout_seconds(env_key="ARIEL_WEATHER_DEV_TIMEOUT_SECONDS", default=8.0)


def _weather_production_api_key() -> str | None:
    configured_api_key = os.getenv("ARIEL_WEATHER_PRODUCTION_API_KEY")
    if configured_api_key is None:
        return None
    normalized = configured_api_key.strip()
    return normalized or None


def _weather_timesteps_for_timeframe(timeframe: str) -> str:
    if timeframe == "tomorrow":
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
        hourly_first: dict[str, Any] | None = None
        if isinstance(timelines, dict):
            hourly_payload = timelines.get("hourly")
            if (
                isinstance(hourly_payload, list)
                and hourly_payload
                and isinstance(hourly_payload[0], dict)
            ):
                hourly_first = hourly_payload[0]
        values = hourly_first.get("values") if isinstance(hourly_first, dict) else None
        if not isinstance(values, dict):
            values = {}

        summary_parts: list[str] = []
        temperature = values.get("temperature")
        if isinstance(temperature, (int, float)):
            summary_parts.append(f"temperature {temperature}C")
        weather_code = values.get("weatherCode")
        if isinstance(weather_code, (int, float, str)):
            summary_parts.append(f"code {weather_code}")
        wind_speed = values.get("windSpeed")
        if isinstance(wind_speed, (int, float)):
            summary_parts.append(f"wind {wind_speed} m/s")
        summary = ", ".join(summary_parts) or "forecast data available"
        forecast_timestamp = (
            hourly_first.get("time")
            if isinstance(hourly_first, dict)
            else payload.get("updatedTime")
        )
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


def _execute_external_notify(input_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "sent",
        "destination": input_payload["destination"],
        "message": input_payload["message"],
    }


def _declare_external_notify_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": input_payload["destination"],
            "payload": {"message": input_payload["message"]},
        }
    ]


_CAPABILITY_REGISTRY: dict[str, CapabilityDefinition] = {
    "cap.calendar.list": CapabilityDefinition(
        capability_id="cap.calendar.list",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "calendar_window_v1",
            "output_schema": "calendar_list_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_CALENDAR_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_list_input,
        execute=_execute_google_calendar_list,
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
        execute=_execute_google_calendar_propose_slots,
        declare_egress_intent=_declare_google_calendar_propose_slots_egress_intent,
    ),
    "cap.email.search": CapabilityDefinition(
        capability_id="cap.email.search",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "email_search_v1",
            "output_schema": "email_search_results_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_GMAIL_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_search_input,
        execute=_execute_google_email_search,
        declare_egress_intent=_declare_google_email_search_egress_intent,
    ),
    "cap.email.read": CapabilityDefinition(
        capability_id="cap.email.read",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "email_read_v1",
            "output_schema": "email_read_result_v1",
            "idempotency": "deterministic_read",
            "required_scopes": [_GOOGLE_GMAIL_READ_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_read_input,
        execute=_execute_google_email_read,
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
        execute=_execute_google_drive_search,
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
        execute=_execute_google_drive_read,
        declare_egress_intent=_declare_google_drive_read_egress_intent,
    ),
    "cap.maps.directions": CapabilityDefinition(
        capability_id="cap.maps.directions",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "maps_directions_query_v1",
            "output_schema": "maps_directions_result_v1",
            "idempotency": "deterministic_read",
            "credentials_mode": "server_managed_encrypted",
            "location_inference": "explicit_only",
        },
        allowed_egress_destinations=_maps_allowed_destinations(),
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
            "output_schema": "maps_search_places_result_v1",
            "idempotency": "deterministic_read",
            "credentials_mode": "server_managed_encrypted",
            "location_inference": "explicit_only",
        },
        allowed_egress_destinations=_maps_allowed_destinations(),
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
            "idempotency": "action_attempt_id",
            "required_scopes": [_GOOGLE_CALENDAR_WRITE_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_calendar_create_event_input,
        execute=_execute_google_calendar_create_event,
        declare_egress_intent=_declare_google_calendar_create_event_egress_intent,
    ),
    "cap.email.draft": CapabilityDefinition(
        capability_id="cap.email.draft",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "email_compose_v1",
            "output_schema": "email_draft_result_v1",
            "idempotency": "action_attempt_id",
            "required_scopes": [_GOOGLE_GMAIL_COMPOSE_SCOPE],
            "delivery_state": "draft_only",
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_draft_input,
        execute=_execute_google_email_draft,
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
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_email_send_input,
        execute=_execute_google_email_send,
        declare_egress_intent=_declare_google_email_send_egress_intent,
    ),
    "cap.drive.share": CapabilityDefinition(
        capability_id="cap.drive.share",
        version="1.0",
        impact_level="external_send",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "drive_share_v1",
            "output_schema": "drive_share_result_v1",
            "idempotency": "action_attempt_id",
            "required_scopes": [_GOOGLE_DRIVE_SHARE_SCOPE],
        },
        allowed_egress_destinations=_GOOGLE_ALLOWED_EGRESS_DESTINATIONS,
        validate_input=_validate_drive_share_input,
        execute=_execute_google_drive_share,
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
        allowed_egress_destinations=_web_extract_allowed_destinations(),
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
        allowed_egress_destinations=_search_web_allowed_destinations(),
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
        allowed_egress_destinations=_search_news_allowed_destinations(),
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
        allowed_egress_destinations=_weather_allowed_destinations(),
        validate_input=_validate_weather_forecast_input,
        execute=_execute_weather_forecast,
        declare_egress_intent=_declare_weather_forecast_egress_intent,
    ),
    "cap.framework.read_echo": CapabilityDefinition(
        capability_id="cap.framework.read_echo",
        version="1.0",
        impact_level="read",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "text_v1",
            "output_schema": "text_v1",
            "idempotency": "deterministic_read",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_read_echo_input,
        execute=_execute_read_echo,
    ),
    "cap.framework.read_private": CapabilityDefinition(
        capability_id="cap.framework.read_private",
        version="1.0",
        impact_level="read",
        policy_decision="deny",
        contract_metadata={
            "input_schema": "text_v1",
            "output_schema": "private_text_v1",
            "idempotency": "deterministic_read",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_read_private_input,
        execute=_execute_read_private,
    ),
    "cap.framework.write_note": CapabilityDefinition(
        capability_id="cap.framework.write_note",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "note_v1",
            "output_schema": "write_receipt_v1",
            "idempotency": "action_attempt_id",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_write_note_input,
        execute=_execute_write_note,
    ),
    "cap.framework.write_draft": CapabilityDefinition(
        capability_id="cap.framework.write_draft",
        version="1.0",
        impact_level="write_reversible",
        policy_decision="allow_inline",
        contract_metadata={
            "input_schema": "note_v1",
            "output_schema": "draft_receipt_v1",
            "idempotency": "action_attempt_id",
        },
        allowed_egress_destinations=(),
        validate_input=_validate_write_draft_input,
        execute=_execute_write_draft,
    ),
    "cap.framework.external_notify": CapabilityDefinition(
        capability_id="cap.framework.external_notify",
        version="1.0",
        impact_level="external_send",
        policy_decision="requires_approval",
        contract_metadata={
            "input_schema": "external_notify_v1",
            "output_schema": "external_notify_receipt_v1",
            "idempotency": "action_attempt_id",
        },
        allowed_egress_destinations=("api.framework.local",),
        validate_input=_validate_external_notify_input,
        execute=_execute_external_notify,
        declare_egress_intent=_declare_external_notify_egress_intent,
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
    if capability_id in {"cap.maps.directions", "cap.maps.search_places"}:
        return replace(capability, allowed_egress_destinations=_maps_allowed_destinations())
    if capability_id == "cap.weather.forecast":
        return replace(capability, allowed_egress_destinations=_weather_allowed_destinations())
    if capability_id == "cap.web.extract":
        return replace(capability, allowed_egress_destinations=_web_extract_allowed_destinations())
    return capability


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
