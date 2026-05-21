from __future__ import annotations

import pytest

from ariel.response_contracts import (
    ResponseContractViolation,
    _project_surface_event_payload,
)


def test_single_run_protocol_events_are_surfaceable() -> None:
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
            "input": {"url": "https://www.anthropic.com/news"},
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
        }
    ]


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
