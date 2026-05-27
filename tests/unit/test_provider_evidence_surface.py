from __future__ import annotations

import pytest

from ariel.provider_evidence_surface import (
    provider_capability_output_for_agent,
    provider_capability_output_for_audit,
)


def _email_read_payload() -> dict:
    return {
        "schema_version": "google.gmail.message_evidence.v1",
        "status": "succeeded",
        "mode": "message",
        "message": {
            "message_id": "msg_1",
            "thread_id": "thr_1",
            "subject": "Facade schedule",
            "sender": {"email": "manager@example.com"},
            "body": {
                "preferred_mime_type": "text/html",
                "truncated": False,
                "body_digest": "digest_1",
            },
        },
        "evidence": {
            "source_kind": "gmail_message",
            "message_id": "msg_1",
            "thread_id": "thr_1",
            "body_digest": "digest_1",
            "truncated": False,
            "blocks": [
                {
                    "block_id": "gmail:msg_1:body:0:digest_1",
                    "kind": "body",
                    "text": "Stacks 09 and 11, then 01 and 03, then 02.",
                    "digest": "digest_1",
                    "truncated": False,
                    "source_mime_type": "text/html",
                    "charset": "utf-8",
                }
            ],
        },
        "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
        "provider_evidence_refs": [
            {
                "provider_evidence_id": "pev_1",
                "read_receipt_id": "pev_1",
                "source_kind": "gmail_message",
                "external_id": "msg_1",
                "thread_external_id": "thr_1",
                "block_ids": ["peb_1"],
                "citation_refs": [{"kind": "provider_evidence_block", "block_id": "peb_1"}],
            }
        ],
    }


def test_email_read_agent_surface_keeps_bounded_block_text() -> None:
    output = provider_capability_output_for_agent(
        capability_id="cap.email.read",
        output_payload=_email_read_payload(),
    )

    assert output["evidence"]["blocks"][0]["text"] == ("Stacks 09 and 11, then 01 and 03, then 02.")
    assert output["provider_evidence_refs"][0]["provider_evidence_id"] == "pev_1"


def test_email_read_audit_surface_redacts_block_text() -> None:
    output = provider_capability_output_for_audit(
        capability_id="cap.email.read",
        output_payload=_email_read_payload(),
    )
    block = output["evidence"]["blocks"][0]

    assert "text" not in block
    assert block["text_redacted"] is True
    assert block["text_digest"] == "digest_1"
    assert block["text_char_count"] == len("Stacks 09 and 11, then 01 and 03, then 02.")
    assert output["provider_evidence_refs"][0]["provider_evidence_id"] == "pev_1"


def test_email_read_agent_surface_rejects_redacted_ok_body() -> None:
    payload = _email_read_payload()
    block = payload["evidence"]["blocks"][0]
    block.pop("text")
    block["text_redacted"] = True

    with pytest.raises(RuntimeError, match="gmail_read_agent_evidence_missing"):
        provider_capability_output_for_agent(
            capability_id="cap.email.read",
            output_payload=payload,
        )


def test_provider_evidence_read_surfaces_split_body_text() -> None:
    payload = {
        "schema_version": "provider.evidence_blocks.v1",
        "status": "succeeded",
        "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
        "provider_evidence": {
            "provider_evidence_id": "pev_1",
            "provider": "google",
            "source_kind": "gmail_message",
            "external_id": "msg_1",
            "thread_external_id": "thr_1",
            "content_digest": "digest_1",
            "taint": "provider_untrusted",
            "sensitivity": "private",
            "lifecycle_state": "available",
            "observed_at": "2026-05-27T00:48:30Z",
        },
        "blocks": [
            {
                "block_id": "peb_1",
                "block_index": 0,
                "kind": "body",
                "text": "Full bounded evidence.",
                "digest": "digest_1",
                "truncated": False,
                "source_offsets": {},
            }
        ],
    }

    assert (
        provider_capability_output_for_agent(
            capability_id="cap.provider_evidence.read",
            output_payload=payload,
        )["blocks"][0]["text"]
        == "Full bounded evidence."
    )
    assert (
        "text"
        not in provider_capability_output_for_audit(
            capability_id="cap.provider_evidence.read",
            output_payload=payload,
        )["blocks"][0]
    )
