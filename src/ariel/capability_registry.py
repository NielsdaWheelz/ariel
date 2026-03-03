from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

PolicyDecision = Literal["allow_inline", "requires_approval", "deny"]


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


def _validate_read_echo_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="text", max_length=4000)


def _validate_read_private_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="text", max_length=4000)


def _validate_write_note_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="note", max_length=500)


def _validate_write_draft_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="note", max_length=500)


def _validate_search_web_input(raw_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    return _validate_exact_text_input(raw_input, field_name="query", max_length=1000)


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


def _search_web_endpoint() -> str:
    default_endpoint = "https://api.search.brave.com/res/v1/web/search"
    configured_endpoint = os.getenv("ARIEL_SEARCH_WEB_ENDPOINT")
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


def _normalize_search_result_item(raw_item: Any) -> dict[str, Any] | None:
    if not isinstance(raw_item, dict):
        return None
    title_raw = raw_item.get("title")
    source_raw = raw_item.get("source")
    if source_raw is None:
        source_raw = raw_item.get("url")
    snippet_raw = raw_item.get("snippet")
    if snippet_raw is None:
        snippet_raw = raw_item.get("description")
    if not isinstance(title_raw, str) or not isinstance(source_raw, str) or not isinstance(snippet_raw, str):
        return None
    title = title_raw.strip()
    source = source_raw.strip()
    snippet = snippet_raw.strip()
    if not title or not source or not snippet:
        return None
    published_at = (
        _normalize_optional_timestamp(raw_item.get("published_at"))
        or _normalize_optional_timestamp(raw_item.get("page_age"))
        or _normalize_optional_timestamp(raw_item.get("age"))
    )
    return {
        "title": title,
        "source": source,
        "snippet": snippet,
        "published_at": published_at,
    }


def _extract_search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    web_payload = payload.get("web")
    if not isinstance(web_payload, dict):
        return []
    raw_results = web_payload.get("results")
    if not isinstance(raw_results, list):
        return []
    normalized_results: list[dict[str, Any]] = []
    for raw_item in raw_results:
        normalized_item = _normalize_search_result_item(raw_item)
        if normalized_item is None:
            continue
        normalized_results.append(normalized_item)
        if len(normalized_results) >= 5:
            break
    return normalized_results


def _execute_search_web(input_payload: dict[str, Any]) -> dict[str, Any]:
    api_key = _search_web_api_key()
    if api_key is None:
        raise RuntimeError("search credentials are not configured")
    endpoint = _search_web_endpoint()
    endpoint_parsed = urlparse(endpoint)
    if endpoint_parsed.hostname is None or endpoint_parsed.scheme.lower() not in {"http", "https"}:
        raise RuntimeError("search endpoint invalid")
    try:
        response = httpx.get(
            endpoint,
            params={"q": input_payload["query"], "count": 5},
            headers={
                "accept": "application/json",
                "x-subscription-token": api_key,
            },
            timeout=_search_web_timeout_seconds(),
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError("search provider timeout") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("search provider network failure") from exc

    if response.status_code == 429:
        raise RuntimeError("search provider rate limited")
    if response.status_code >= 500:
        raise RuntimeError("search provider upstream failure")
    if response.status_code >= 400:
        raise RuntimeError("search provider request rejected")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("search provider returned invalid json") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("search provider returned invalid payload")

    return {
        "query": input_payload["query"],
        "retrieved_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "results": _extract_search_results(payload),
    }


def _declare_search_web_egress_intent(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "destination": _search_web_endpoint(),
            "payload": {"query": input_payload["query"]},
        }
    ]


def _search_web_allowed_destinations() -> tuple[str, ...]:
    endpoint = _search_web_endpoint()
    parsed = urlparse(endpoint)
    if parsed.hostname:
        return (parsed.hostname.lower(),)
    if "://" not in endpoint:
        host = endpoint.split("/", maxsplit=1)[0]
        if ":" in host:
            host = host.split(":", maxsplit=1)[0]
        host = host.strip().lower()
        if host:
            return (host,)
    return ("api.search.brave.com",)


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
    return _CAPABILITY_REGISTRY.get(capability_id)


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


def canonical_action_payload(*, capability_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    return {"capability_id": capability_id, "input": input_payload}


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
