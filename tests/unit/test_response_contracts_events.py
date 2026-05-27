from __future__ import annotations

import pytest

from ariel.response_contracts import (
    ResponseContractViolation,
    _project_surface_event,
    _project_surface_event_payload,
)


def _raw_event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": "evn_1",
        "turn_id": "trn_1",
        "sequence": 1,
        "event_type": event_type,
        "payload": payload,
        "created_at": "2026-05-21T00:00:00Z",
    }


def test_run_protocol_events_are_surfaceable() -> None:
    assert _project_surface_event_payload(
        "evt.model.protocol_failed",
        {"reason": "run_protocol_requires_run_tool", "model_call_count": 1},
    ) == {
        "reason": "run_protocol_requires_run_tool",
        "model_call_count": 1,
        "provider_response_id": None,
    }
    assert _project_surface_event_payload(
        "evt.run.validation_failed",
        {"errors": ["run_source_invalid_json"], "model_call_count": 1},
    ) == {
        "errors": ["run_source_invalid_json"],
        "model_call_count": 1,
        "provider_response_id": None,
    }
    assert _project_surface_event_payload(
        "evt.agent.value_emitted",
        {"index": 1, "value_digest": "0" * 64, "value_bytes": 13, "model_call_count": 1},
    ) == {
        "index": 1,
        "value_digest": "0" * 64,
        "value_bytes": 13,
        "model_call_count": 1,
        "provider_response_id": None,
    }
    assert _project_surface_event_payload(
        "evt.agent.output_not_applied",
        {"reason": "stale_turn", "current_turn_id": "trn_new"},
    ) == {
        "reason": "stale_turn",
        "current_turn_id": "trn_new",
    }
    assert _project_surface_event_payload(
        "evt.agent.provider_sync_grounding_rejected",
        {"model_call_count": 2, "rejected_message_chars": 180},
    ) == {
        "model_call_count": 2,
        "rejected_message_chars": 180,
        "provider_response_id": None,
        "exhausted": False,
    }


def test_action_proposed_accepts_research_finding_evidence() -> None:
    """A research-completion ``agent_wake`` carries tainted
    ``ingress_provenance`` whose evidence is
    ``{"kind": "research_finding_in_context", "research_mode": ...,
    "research_status": ...}`` (see ``docs/modules/agent-loop.md`` — The
    completion wake, and ``worker._agent_wake_context``).  When the main agent
    proposes an action in that turn, ``_taint_event_payload`` copies the evidence
    into ``evt.action.proposed``; the surface contract must accept it.
    Regression for a production worker crash where the contract was narrower
    than the documented producer."""

    projected = _project_surface_event_payload(
        "evt.action.proposed",
        {
            "action_attempt_id": "aat_1",
            "capability_id": "cap.web.extract",
            "input": {"url": "https://www.anthropic.com/research"},
            "taint": {
                "influenced_by_untrusted_content": True,
                "provenance_status": "tainted",
                "runtime_provenance": {
                    "status": "tainted",
                    "evidence": [
                        {
                            "kind": "research_finding_in_context",
                            "research_mode": "web",
                            "research_status": "partial",
                        }
                    ],
                },
                "model_declared_taint": {"status": "missing"},
            },
        },
    )
    evidence = projected["taint"]["runtime_provenance"]["evidence"]
    assert evidence == [
        {
            "kind": "research_finding_in_context",
            "turn_id": None,
            "action_attempt_id": None,
            "capability_id": None,
            "impact_level": None,
            "attachment_ref": None,
            "filename": None,
            "modality": None,
            "research_mode": "web",
            "research_status": "partial",
            "provider": None,
            "resource_type": None,
            "resource_id": None,
            "sync_run_id": None,
            "provider_event_id": None,
            "item_count": None,
            "observation_count": None,
            "grounding_items": None,
        }
    ]


def test_action_proposed_accepts_provider_sync_review_evidence() -> None:
    """Provider-sync wakes carry bounded Gmail/Calendar evidence into a normal
    turn. If that tainted context influences a tool call, the surface event
    contract must accept the provider-sync provenance instead of crashing the
    background worker after the turn completes."""

    projected = _project_surface_event_payload(
        "evt.action.proposed",
        {
            "action_attempt_id": "aat_1",
            "capability_id": "cap.memory.search",
            "input": {"query": "Launch checklist due today", "limit": 1},
            "taint": {
                "influenced_by_untrusted_content": True,
                "provenance_status": "tainted",
                "runtime_provenance": {
                    "status": "tainted",
                    "evidence": [
                        {
                            "kind": "provider_sync_review",
                            "provider": "google",
                            "resource_type": "gmail",
                            "resource_id": "primary",
                            "sync_run_id": "syn_provider_sync_review",
                            "provider_event_id": "pev_provider_sync_review",
                            "item_count": 1,
                            "observation_count": 1,
                        }
                    ],
                },
                "model_declared_taint": {"status": "missing"},
            },
        },
    )
    evidence = projected["taint"]["runtime_provenance"]["evidence"]
    assert evidence == [
        {
            "kind": "provider_sync_review",
            "turn_id": None,
            "action_attempt_id": None,
            "capability_id": None,
            "impact_level": None,
            "attachment_ref": None,
            "filename": None,
            "modality": None,
            "research_mode": None,
            "research_status": None,
            "provider": "google",
            "resource_type": "gmail",
            "resource_id": "primary",
            "sync_run_id": "syn_provider_sync_review",
            "provider_event_id": "pev_provider_sync_review",
            "item_count": 1,
            "observation_count": 1,
            "grounding_items": None,
        }
    ]


def test_research_started_round_trips_research_shape_payload() -> None:
    """``evt.research.started`` is the research subagent's turn-open event;
    its payload carries the research question and mode and round-trips through
    the projector."""

    projected = _project_surface_event(
        _raw_event(
            "evt.research.started",
            {"research_question": "What is the capital of France?", "research_mode": "web"},
        )
    )
    assert projected["payload"] == {
        "research_question": "What is the capital of France?",
        "research_mode": "web",
    }


def test_research_terminal_events_round_trip() -> None:
    """The three research terminal events (``finding_emitted``, ``failed``,
    ``partial``) all carry ``{"mode": str}`` and round-trip through the projector.
    Without these branches the read endpoint 500s on any session that contains a
    completed/failed/partial research turn."""

    for event_type in (
        "evt.research.finding_emitted",
        "evt.research.failed",
        "evt.research.partial",
    ):
        assert _project_surface_event(_raw_event(event_type, {"mode": "web"}))["payload"] == {
            "mode": "web"
        }


def test_turn_failed_accepts_operational_error_code() -> None:
    assert _project_surface_event_payload(
        "evt.turn.failed",
        {
            "failure_reason": "background task replay found an interrupted in-progress turn",
            "error_code": "E_BACKGROUND_TURN_INTERRUPTED",
        },
    ) == {
        "failure_reason": "background task replay found an interrupted in-progress turn",
        "error_code": "E_BACKGROUND_TURN_INTERRUPTED",
    }


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("evt.research.started", {"research_question": "q", "research_mode": "other"}),
        ("evt.research.finding_emitted", {"mode": "other"}),
        ("evt.research.failed", {"mode": "other"}),
        ("evt.research.partial", {"mode": "other"}),
    ],
)
def test_research_events_reject_unknown_mode(
    event_type: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ResponseContractViolation) as excinfo:
        _project_surface_event(_raw_event(event_type, payload))
    assert excinfo.value.contract == f"surface_event_payload.{event_type}"


def test_ai_judgment_events_accept_only_persisted_judgment_types() -> None:
    assert _project_surface_event_payload(
        "evt.ai_judgment.completed",
        {"judgment_type": "memory_dream"},
    ) == {
        "judgment_type": "memory_dream",
        "parse_status": None,
        "validation_status": None,
        "code": None,
        "failure_code": None,
        "failure_reason": None,
        "prompt_version": None,
        "source_id": None,
        "source_turn_ids": None,
        "input_refs": None,
        "retryable": None,
        "provider": None,
        "model": None,
        "usage": None,
        "provider_response_id": None,
        "response_output_shape": None,
        "reason_codes": None,
        "model_call_count": None,
        "agent_loop_max_model_calls": None,
        "omitted_turn_count": None,
        "eligible_capability_count": None,
    }

    with pytest.raises(ResponseContractViolation) as excinfo:
        _project_surface_event_payload(
            "evt.ai_judgment.completed",
            {"judgment_type": "research"},
        )
    assert excinfo.value.contract == "surface_event_payload.evt.ai_judgment.completed"


def test_action_proposed_rejects_unknown_evidence_kind() -> None:
    """Evidence kinds remain a closed set: the contract still rejects a kind
    no producer emits.  This pins the discriminator so a producer rename
    cannot quietly slip through ingress with mismatched metadata."""

    with pytest.raises(ResponseContractViolation) as excinfo:
        _project_surface_event_payload(
            "evt.action.proposed",
            {
                "action_attempt_id": "aat_1",
                "capability_id": "cap.web.extract",
                "input": {"url": "https://example.com"},
                "taint": {
                    "influenced_by_untrusted_content": True,
                    "provenance_status": "tainted",
                    "runtime_provenance": {
                        "status": "tainted",
                        "evidence": [{"kind": "definitely_not_a_real_kind"}],
                    },
                    "model_declared_taint": {"status": "missing"},
                },
            },
        )
    assert excinfo.value.contract == "surface_event_payload.evt.action.proposed"


def test_action_execution_succeeded_uses_audit_execution_shape() -> None:
    projected = _project_surface_event_payload(
        "evt.action.execution.succeeded",
        {
            "action_attempt_id": "aat_1",
            "capability_id": "cap.provider_evidence.read",
            "status": "succeeded",
            "execution_output": {
                "schema_version": "provider.evidence_blocks.v1",
                "blocks": [{"block_id": "peb_1", "text_redacted": True}],
            },
        },
    )

    assert projected == {
        "action_attempt_id": "aat_1",
        "capability_id": "cap.provider_evidence.read",
        "status": "succeeded",
        "execution_output": {
            "schema_version": "provider.evidence_blocks.v1",
            "blocks": [{"block_id": "peb_1", "text_redacted": True}],
        },
        "provider_write_receipt_id": None,
        "replayed_provider_write_receipt_id": None,
        "reconciled": None,
    }

    with pytest.raises(ResponseContractViolation) as excinfo:
        _project_surface_event_payload(
            "evt.action.execution.succeeded",
            {"action_attempt_id": "aat_1", "output": {"raw": "legacy"}},
        )
    assert excinfo.value.contract == "surface_event_payload.evt.action.execution.succeeded"
