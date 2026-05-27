from __future__ import annotations

from typing import Any
import hashlib

from fastapi.encoders import jsonable_encoder


def provider_capability_output_for_agent(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]:
    output = jsonable_encoder(output_payload)
    if not isinstance(output, dict):
        raise RuntimeError("provider_output_contract_invalid")
    if capability_id == "cap.email.read":
        _require_email_read_agent_evidence(output)
        _attach_provider_ref_provenance(output)
    if capability_id == "cap.provider_evidence.read":
        _require_provider_evidence_agent_blocks(output)
    return output


def provider_capability_output_for_audit(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]:
    redacted = jsonable_encoder(output_payload)
    if not isinstance(redacted, dict):
        raise RuntimeError("provider_output_contract_invalid")

    if capability_id == "cap.email.read":
        evidence = redacted.get("evidence")
        if isinstance(evidence, dict):
            evidence["blocks"] = _redact_evidence_blocks(evidence.get("blocks"))
        message = redacted.get("message")
        if isinstance(message, dict):
            for key in ("body_text", "body_html", "snippet"):
                value = message.pop(key, None)
                if isinstance(value, str):
                    message[f"{key}_redacted"] = _redacted_provider_text_marker(value)
            body = message.get("body")
            if isinstance(body, str):
                message["body_redacted"] = _redacted_provider_text_marker(message.pop("body"))
        return redacted

    if capability_id == "cap.provider_evidence.read":
        redacted["blocks"] = _redact_evidence_blocks(redacted.get("blocks"))
        return redacted

    if capability_id == "cap.calendar.list":
        events = redacted.get("events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                event["description_blocks"] = _redact_evidence_blocks(
                    event.get("description_blocks")
                )
                description = event.pop("description", None)
                if isinstance(description, str):
                    event["description_redacted"] = _redacted_provider_text_marker(description)
        return redacted

    if capability_id in {
        "cap.calendar.create_event",
        "cap.calendar.update_event",
        "cap.calendar.respond_to_event",
    }:
        description = redacted.pop("description", None)
        if isinstance(description, str):
            redacted["description_redacted"] = _redacted_provider_text_marker(description)
        event = redacted.get("event")
        if isinstance(event, dict):
            event_description = event.pop("description", None)
            if isinstance(event_description, str):
                event["description_redacted"] = _redacted_provider_text_marker(event_description)
            event["description_blocks"] = _redact_evidence_blocks(event.get("description_blocks"))
        return redacted

    if capability_id in {"cap.email.draft", "cap.email.send"}:
        body = redacted.pop("body", None)
        if isinstance(body, str):
            redacted["body_redacted"] = _redacted_provider_text_marker(body)
        draft = redacted.get("draft")
        if isinstance(draft, dict):
            draft_body = draft.pop("body", None)
            if isinstance(draft_body, str):
                draft["body_redacted"] = _redacted_provider_text_marker(draft_body)
        message = redacted.get("message")
        if isinstance(message, dict):
            message_body = message.pop("body", None)
            if isinstance(message_body, str):
                message["body_redacted"] = _redacted_provider_text_marker(message_body)
        return redacted

    return redacted


def provider_capability_output_for_public_transport(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]:
    return provider_capability_output_for_audit(
        capability_id=capability_id,
        output_payload=output_payload,
    )


def _require_email_read_agent_evidence(output: dict[str, Any]) -> None:
    read_outcome = output.get("read_outcome")
    if not isinstance(read_outcome, dict) or read_outcome.get("status") != "ok":
        return
    evidence = output.get("evidence")
    blocks = evidence.get("blocks") if isinstance(evidence, dict) else None
    if not isinstance(blocks, list) or not blocks:
        raise RuntimeError("gmail_read_agent_evidence_missing")
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise RuntimeError("gmail_read_agent_evidence_missing")
    refs = output.get("provider_evidence_refs")
    if not isinstance(refs, list) or not refs:
        raise RuntimeError("gmail_read_provider_evidence_refs_missing")


def _require_provider_evidence_agent_blocks(output: dict[str, Any]) -> None:
    read_outcome = output.get("read_outcome")
    if not isinstance(read_outcome, dict) or read_outcome.get("status") != "ok":
        return
    blocks = output.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise RuntimeError("provider_evidence_read_blocks_missing")
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise RuntimeError("provider_evidence_read_blocks_missing")


def _attach_provider_ref_provenance(output: dict[str, Any]) -> None:
    refs = output.get("provider_evidence_refs")
    if not isinstance(refs, list) or not refs:
        return
    evidence: list[dict[str, str]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        evidence_id = ref.get("provider_evidence_id")
        if isinstance(evidence_id, str) and evidence_id:
            evidence.append(
                {
                    "kind": "provider_evidence",
                    "provider_evidence_id": evidence_id,
                    "taint": "provider_untrusted",
                    "sensitivity": "private",
                }
            )
    if evidence:
        output["runtime_provenance"] = {"status": "tainted", "evidence": evidence}


def _redact_evidence_blocks(raw_blocks: Any) -> list[dict[str, Any]]:
    redacted_blocks: list[dict[str, Any]] = []
    for block in raw_blocks if isinstance(raw_blocks, list) else []:
        if not isinstance(block, dict):
            continue
        redacted_block = dict(block)
        text = redacted_block.pop("text", None)
        if isinstance(text, str):
            redacted_block["text_redacted"] = True
            redacted_block["text_digest"] = str(redacted_block.get("digest") or _hash_text(text))
            redacted_block["text_char_count"] = len(text)
        redacted_blocks.append(redacted_block)
    return redacted_blocks


def _redacted_provider_text_marker(value: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["digest"] = _hash_text(value)
        marker["char_count"] = len(value)
    return marker


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
