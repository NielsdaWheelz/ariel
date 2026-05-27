from __future__ import annotations

from typing import Any

from ariel.worker import _provider_sync_review_context


def _payload(*, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "provider_sync_review",
        "provider": "google",
        "resource_type": "gmail",
        "resource_id": "primary",
        "sync_run_id": "syn_test",
        "provider_event_id": "pev_event_test",
        "item_count": len(items),
        "observation_count": len(items),
        "items": items,
        "omitted_item_count": 0,
    }


def test_wake_message_contains_grounding_block_when_items_require_body_claims() -> None:
    payload = _payload(
        items=[
            {
                "change": "messagesAdded",
                "message_id": "msg_abc",
                "thread_id": "thr_abc",
                "subject": "Quarterly review",
                "requires_read_for_body_claims": True,
                "provider_evidence_refs": [
                    {"provider_evidence_id": "pev_xyz"},
                ],
            }
        ],
    )

    text, _provenance = _provider_sync_review_context(payload)

    assert "Grounding requirement:" in text
    assert "msg_abc" in text
    assert "email.read" in text
    assert "pev_xyz" in text
    assert "agent.finish_silent" in text


def test_wake_message_omits_grounding_block_when_no_items_require_it() -> None:
    payload = _payload(
        items=[
            {
                "change": "messagesAdded",
                "message_id": "msg_abc",
                "thread_id": "thr_abc",
                "subject": "Quarterly review",
            }
        ],
    )

    text, _provenance = _provider_sync_review_context(payload)

    assert "Grounding requirement:" not in text
