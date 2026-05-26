from __future__ import annotations

import json

from ariel.conversational_continuity import (
    EXTERNAL_EVENT_TYPES,
    _compact_event_payload,
)


def test_external_event_types_excludes_loop_trace() -> None:
    # Loop bookkeeping must never enter the recent-events block; the agent has
    # no use for model timing, action.proposed (superseded by succeeded/failed),
    # policy decisions, intermediate started markers, or intra-turn emit_value.
    excluded = {
        "evt.model.started",
        "evt.model.completed",
        "evt.action.proposed",
        "evt.action.policy_decided",
        "evt.action.execution.started",
        "evt.action.execution.retrying",
        "evt.agent.value_emitted",
        "evt.agent.output_not_applied",
        "evt.agent.premature_synthesis_rejected",
        "evt.ai_judgment.completed",
        "evt.ai_judgment.failed",
        "evt.research.started",
        "evt.connector.google.connect.failed",
        "evt.connector.google.connect.started",
        "evt.connector.google.connect.succeeded",
        "evt.connector.google.reconnect.failed",
        "evt.connector.google.reconnect.started",
        "evt.connector.google.refresh.failed",
        "evt.connector.google.refresh.succeeded",
        "evt.memory.recall_failed",
        "evt.provider_write.reconcile_unavailable",
    }
    assert excluded.isdisjoint(EXTERNAL_EVENT_TYPES)


def test_external_event_types_includes_state_changes() -> None:
    # Turn lifecycle, assistant speech, real tool outcomes, approvals, research
    # findings, provider write reconciliation, and recall outcomes — these are
    # the events the next-turn agent cares about.
    expected = {
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
    assert expected == EXTERNAL_EVENT_TYPES


def test_compact_event_payload_passes_small_payload_through() -> None:
    payload = {"capability_id": "cap.email.search", "status": "succeeded", "count": 3}
    assert _compact_event_payload(payload, cap=4096) == payload


def test_compact_event_payload_preserves_top_level_scalars_when_truncated() -> None:
    long_blob = "x" * 6000
    payload = {
        "capability_id": "cap.email.read",
        "status": "succeeded",
        "execution_output": {"message_body": long_blob},
    }
    out = _compact_event_payload(payload, cap=512)
    assert out["_truncated"] is True
    assert out["capability_id"] == "cap.email.read"
    assert out["status"] == "succeeded"


def test_compact_event_payload_preserves_nested_canonical_ids() -> None:
    payload = {
        "capability_id": "cap.email.read",
        "status": "succeeded",
        "execution_output": {
            "message": {
                "message_id": "19c638912663c9e5",
                "thread_id": "19c638912663c9e5",
                "subject": "x" * 400,
                "body": "y" * 6000,
            }
        },
    }
    out = _compact_event_payload(payload, cap=512)
    assert out["_truncated"] is True
    msg = out["execution_output"]["message"]
    assert msg["message_id"] == "19c638912663c9e5"
    assert msg["thread_id"] == "19c638912663c9e5"


def test_compact_event_payload_preserves_ids_list_under_long_keys() -> None:
    payload = {
        "capability_id": "cap.email.trash",
        "status": "succeeded",
        "execution_output": {
            "message_ids": ["19c638912663c9e5", "19c63892fa1b4c10"],
            "noise": "z" * 6000,
        },
    }
    out = _compact_event_payload(payload, cap=512)
    assert out["_truncated"] is True
    assert out["execution_output"]["message_ids"] == [
        "19c638912663c9e5",
        "19c63892fa1b4c10",
    ]


def test_compact_event_payload_summarizes_long_strings() -> None:
    payload = {
        "capability_id": "cap.web.extract",
        "status": "succeeded",
        "execution_output": {"document": {"text": "p" * 5000}},
    }
    out = _compact_event_payload(payload, cap=512)
    text_field = out["execution_output"]["document"]["text"]
    assert isinstance(text_field, dict)
    assert text_field["_truncated_str"] is True
    assert text_field["_byte_size"] == 5000
    assert text_field["_preview"].startswith("p")


def test_compact_event_payload_samples_long_lists() -> None:
    payload = {
        "capability_id": "cap.email.search",
        "status": "succeeded",
        "execution_output": {
            "messages": [{"message_id": f"id_{i}", "subject": f"s_{i}"} for i in range(200)]
        },
    }
    out = _compact_event_payload(payload, cap=512)
    messages = out["execution_output"]["messages"]
    assert isinstance(messages, dict)
    assert messages["_kind"] == "list"
    assert messages["_size"] == 200
    assert len(messages["_sampled"]) == 50
    # canonical IDs survive inside the sampled subset
    assert messages["_sampled"][0]["message_id"] == "id_0"


def test_compact_event_payload_idempotent_on_compacted_view() -> None:
    payload = {
        "capability_id": "cap.email.read",
        "execution_output": {"message": {"message_id": "m1", "body": "x" * 6000}},
    }
    once = _compact_event_payload(payload, cap=512)
    twice = _compact_event_payload(once, cap=512)
    # The compacted view itself is small enough to be returned verbatim the
    # second time around; nothing surprising happens.
    encoded_once = json.dumps(once, sort_keys=True, separators=(",", ":"))
    if len(encoded_once.encode("utf-8")) <= 512:
        assert twice == once
