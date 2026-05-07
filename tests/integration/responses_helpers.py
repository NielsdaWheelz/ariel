from __future__ import annotations

import copy
import json
from typing import Any

from ariel.capability_registry import response_tool_name_for_capability_id


def _assistant_text_from_function_outputs(
    *,
    input_items: list[dict[str, Any]],
    direct_assistant_text: str,
) -> str:
    interpreted = _tool_result_interpretation_from_messages(input_items)
    if interpreted is not None:
        findings = interpreted.get("findings")
        citation_refs = interpreted.get("citation_refs")
        parts = (
            [item for item in findings if isinstance(item, str)]
            if isinstance(findings, list)
            else []
        )
        if isinstance(citation_refs, list):
            parts.extend(f"[{index}]" for index, _ in enumerate(citation_refs, start=1))
        if any("your calendar only" in part.lower() for part in parts) and not any(
            "reconnect" in part.lower() for part in parts
        ):
            parts.append("Reconnect calendar free/busy access for attendee availability.")
        return " ".join(parts) if parts else direct_assistant_text

    outputs = _function_output_payloads(input_items)
    if any(output.get("status") == "approval_required" for output in outputs):
        return "approval required before I can run that action."
    if any(
        output.get("status") == "succeeded"
        and output.get("capability_id") == "cap.discord.no_response"
        for output in outputs
    ):
        return ""

    failures = [
        str(output.get("error") or output.get("reason"))
        for output in outputs
        if output.get("status") in {"failed", "blocked"}
        and (output.get("error") or output.get("reason"))
    ]
    successes = [output for output in outputs if output.get("status") == "succeeded"]
    if failures and successes:
        success_text = _grounded_results_text(successes) or ""
        timeout_word = " timeout" if any("timed out" in failure for failure in failures) else ""
        return (
            f"Partial result: {success_text} {'; '.join(failures)}{timeout_word}. "
            "Retry the failed source."
        ).strip()
    if failures:
        failure_text = "; ".join(failures)
        if "weather_location_required" in failure_text:
            return "I need the location or city before checking the weather. Where should I use?"
        if "weather provider timed out" in failure_text:
            return "I am uncertain because the weather provider timed out. Retry shortly."
        if any(
            item in failure_text for item in ("consent_required", "scope_missing", "access_revoked")
        ):
            return f"blocked: {failure_text}. Reconnect the provider."
        if "token_expired" in failure_text:
            return f"blocked: {failure_text}. Retry after token refresh or reconnect."
        if "maps_origin_required" in failure_text:
            return (
                "blocked: maps_origin_required. I cannot infer the route origin location; "
                "provide the origin."
            )
        if "maps_destination_required" in failure_text:
            return (
                "blocked: maps_destination_required. I cannot infer the route destination "
                "location; provide the destination."
            )
        if "maps_location_context_required" in failure_text:
            return (
                "blocked: maps_location_context_required. I cannot infer a nearby location "
                "context; provide the location."
            )
        if any(
            item in failure_text
            for item in ("provider_credentials_missing", "provider_credentials_invalid")
        ):
            return f"blocked: {failure_text}. Operator credential setup is required."
        if "provider_rate_limited" in failure_text:
            return f"blocked: {failure_text}. Wait for the provider rate limit, then retry."
        if "provider_permission_denied" in failure_text:
            return f"blocked: {failure_text}. Check provider permission before retrying."
        if "provider_request_rejected" in failure_text:
            return f"blocked: {failure_text}. Verify the request and retry."
        if "provider_unreachable" in failure_text:
            return f"blocked: {failure_text}. Operator endpoint setup is required."
        if any(
            item in failure_text
            for item in (
                "provider_timeout",
                "provider_network_failure",
                "provider_upstream_failure",
                "provider_invalid_payload",
            )
        ):
            return f"blocked: {failure_text}. Retry the provider request."
        if "egress_destination_denied" in failure_text:
            return f"maps runtime failure: {failure_text}. Check the egress allowlist."
        if "url_invalid" in failure_text:
            return f"blocked: {failure_text}. Provide a valid public URL."
        if "url_scheme_unsupported" in failure_text:
            return f"blocked: {failure_text}. Use an http or https URL."
        if "url_destination_unsafe" in failure_text:
            return f"blocked: {failure_text}. Use a public URL."
        if "access_restricted" in failure_text:
            return f"blocked: {failure_text}. Use a public page or another source."
        if "unsupported_format" in failure_text:
            return f"blocked: {failure_text}. Use a text-readable page or document."
        return f"blocked: {failure_text}"
    if any(output.get("status") == "queued" for output in outputs):
        return "queued for execution."

    attachment_text = _attachment_text(successes)
    if attachment_text is not None:
        return f"attachment content: {attachment_text} [1]"

    search_text = _search_text(successes)
    if search_text is not None:
        return search_text
    weather_text = _weather_text(successes)
    if weather_text is not None:
        return weather_text
    grounded_text = _grounded_results_text(successes)
    if grounded_text is not None:
        return grounded_text

    for output in successes:
        payload = output.get("output")
        if isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str):
                return text

    for item in reversed(input_items):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if item.get("role") == "system" and isinstance(content, str):
            prefix = "audited tool summary:\n"
            if content.startswith(prefix):
                return content.removeprefix(prefix)

    return direct_assistant_text


def _function_output_payloads(input_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        raw_output = item.get("output")
        try:
            output = json.loads(raw_output) if isinstance(raw_output, str) else {}
        except ValueError:
            output = {}
        if isinstance(output, dict):
            outputs.append(output)
    return outputs


def _tool_result_interpretation_from_messages(
    input_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in reversed(input_items):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        prefix = "AI tool-result interpretation:\n"
        if not content.startswith(prefix):
            continue
        try:
            parsed = json.loads(content.removeprefix(prefix))
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _attachment_text(outputs: list[dict[str, Any]]) -> str | None:
    for output in outputs:
        if output.get("capability_id") != "cap.attachment.read":
            continue
        payload = output.get("output")
        if not isinstance(payload, dict):
            continue
        blocks = payload.get("blocks")
        if not isinstance(blocks, list):
            continue
        text_parts = [
            str(block["text"])
            for block in blocks
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if text_parts:
            return " ".join(text_parts)
    return None


def _search_text(outputs: list[dict[str, Any]]) -> str | None:
    for output in outputs:
        if output.get("capability_id") not in {"cap.search.web", "cap.search.news"}:
            continue
        payload = output.get("output")
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return "I am uncertain from the available evidence. Try another source or query."
        snippets = [
            str(result.get("snippet") or result.get("title") or "").strip()
            for result in results
            if isinstance(result, dict)
        ]
        citations = " ".join(f"[{index}]" for index, _ in enumerate(snippets, start=1))
        message = " ".join(snippet for snippet in snippets if snippet)
        if output.get("capability_id") == "cap.search.news" and any(
            isinstance(result, dict) and result.get("published_at") is None for result in results
        ):
            message += " Freshness note: one source has missing or ambiguous timing."
        if output.get("capability_id") == "cap.search.news" and any(
            isinstance(result, dict)
            and isinstance(result.get("published_at"), str)
            and result["published_at"] < "2026-01-01"
            for result in results
        ):
            message += " Freshness note: one source may be stale."
        return f"{message} {citations}".strip()
    return None


def _weather_text(outputs: list[dict[str, Any]]) -> str | None:
    for output in outputs:
        if output.get("capability_id") != "cap.weather.forecast":
            continue
        payload = output.get("output")
        if not isinstance(payload, dict):
            continue
        location = str(payload.get("location") or "")
        timeframe = str(payload.get("timeframe") or "")
        forecast_timestamp = str(payload.get("forecast_timestamp") or "")
        results = payload.get("results")
        citations = ""
        snippets: list[str] = []
        if isinstance(results, list):
            snippets = [
                str(result.get("snippet") or result.get("title") or "").strip()
                for result in results
                if isinstance(result, dict)
            ]
            citations = " ".join(f"[{index}]" for index, _ in enumerate(snippets, start=1))
        return (
            f"{location} {timeframe} {forecast_timestamp} {' '.join(snippets)} {citations}".strip()
        )
    return None


def _grounded_results_text(outputs: list[dict[str, Any]]) -> str | None:
    for output in outputs:
        payload = output.get("output")
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            continue
        snippets = [
            str(result.get("snippet") or result.get("title") or "").strip()
            for result in results
            if isinstance(result, dict)
        ]
        if not snippets:
            continue
        citations = " ".join(f"[{index}]" for index, _ in enumerate(snippets, start=1))
        prefix = ""
        capability_id = output.get("capability_id")
        if capability_id == "cap.calendar.list":
            prefix = "schedule "
        elif capability_id == "cap.calendar.propose_slots":
            prefix = "attendee availability "
        elif capability_id in {"cap.email.search", "cap.email.read"}:
            prefix = "email "
        outcome = payload.get("extract_outcome")
        if isinstance(outcome, dict) and outcome.get("status") == "partial":
            recovery = outcome.get("recovery")
            prefix = (
                f"partial extraction. {recovery} "
                if isinstance(recovery, str) and recovery
                else "partial extraction. Narrow or focus the request. "
            )
        message = f"{prefix}{' '.join(snippets)} {citations}".strip()
        if capability_id == "cap.calendar.propose_slots" and any(
            "your calendar only" in snippet.lower() for snippet in snippets
        ):
            message += " Reconnect calendar free/busy access for attendee availability."
        return message
    return None


def _tool_result_interpreter_json(input_items: list[dict[str, Any]]) -> str:
    raw_content = "{}"
    for item in input_items:
        content = item.get("content")
        if item.get("role") == "user" and isinstance(content, str):
            raw_content = content
            break
    try:
        interpreter_input = json.loads(raw_content)
    except ValueError:
        interpreter_input = {}
    audited_outputs = (
        interpreter_input.get("audited_tool_outputs")
        if isinstance(interpreter_input, dict)
        else None
    )
    findings: list[str] = []
    selected_refs: list[str] = []
    citation_refs: list[Any] = []
    artifact_refs: list[Any] = []
    if isinstance(audited_outputs, list):
        for audited in audited_outputs:
            if not isinstance(audited, dict):
                continue
            output_ref = audited.get("output_ref")
            if isinstance(output_ref, str):
                selected_refs.append(output_ref)
            payload = audited.get("output")
            if not isinstance(payload, dict):
                continue
            results = payload.get("results")
            if isinstance(results, list) and results:
                if audited.get("capability_id") == "cap.calendar.list":
                    findings.append("schedule")
                if audited.get("capability_id") == "cap.calendar.propose_slots":
                    findings.append("attendee slot availability")
                if audited.get("capability_id") == "cap.search.news" and any(
                    isinstance(result, dict) and result.get("published_at") is None
                    for result in results
                ):
                    findings.append("Freshness note: one source has missing or ambiguous timing.")
                if audited.get("capability_id") == "cap.search.news" and any(
                    isinstance(result, dict)
                    and isinstance(result.get("published_at"), str)
                    and result["published_at"] < "2026-01-01"
                    for result in results
                ):
                    findings.append("Freshness note: one source may be stale.")
                snippets = [
                    str(result.get("snippet") or result.get("title") or "")
                    for result in results
                    if isinstance(result, dict)
                ]
                capital_claims = [
                    snippet for snippet in snippets if "capital of" in snippet.lower()
                ]
                if len(capital_claims) > 1 and len(set(capital_claims)) > 1:
                    findings.append(
                        "I am uncertain because the sources conflict. Retry with a narrower source."
                    )
                for result in results:
                    if isinstance(result, dict):
                        findings.append(str(result.get("snippet") or result.get("title") or ""))
                if audited.get("capability_id") == "cap.calendar.propose_slots" and any(
                    "your calendar only" in snippet.lower() for snippet in snippets
                ):
                    findings.append(
                        "Reconnect calendar free/busy access for attendee availability."
                    )
            elif isinstance(results, list):
                findings.append("I am uncertain from the available evidence. Try another source.")
    for key, target in (("citation_refs", citation_refs), ("artifact_refs", artifact_refs)):
        values = interpreter_input.get(key) if isinstance(interpreter_input, dict) else None
        if isinstance(values, list):
            target.extend(values)
    return json.dumps(
        {
            "findings": [finding for finding in findings if finding],
            "contradictions": [],
            "uncertainty": [],
            "selected_output_refs": selected_refs,
            "omitted_output_refs": [],
            "citation_refs": citation_refs,
            "artifact_refs": artifact_refs,
            "recommended_next_evidence": [],
            "confidence": 0.91,
        },
        sort_keys=True,
    )


def _has_interpreter_input(input_items: list[dict[str, Any]]) -> bool:
    for item in input_items:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("audited_tool_outputs"), list):
            return True
    return False


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
    if not proposals and _has_interpreter_input(input_items):
        return responses_message(
            assistant_text=_tool_result_interpreter_json(input_items),
            provider=provider,
            model=model,
            provider_response_id=provider_response_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    if any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in input_items
    ):
        return responses_message(
            assistant_text=_assistant_text_from_function_outputs(
                input_items=input_items,
                direct_assistant_text=assistant_text,
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
