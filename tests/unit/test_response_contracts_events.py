from __future__ import annotations

from ariel.response_contracts import _project_surface_event_payload


def test_single_run_protocol_events_are_surfaceable() -> None:
    assert _project_surface_event_payload(
        "evt.model.protocol_failed",
        {"reason": "run_protocol_requires_run_tool", "attempt": 1},
    ) == {
        "reason": "run_protocol_requires_run_tool",
        "attempt": 1,
        "provider_response_id": None,
    }
    assert _project_surface_event_payload(
        "evt.run.validation_failed",
        {"errors": ["run_source_invalid_json"], "attempt": 1},
    ) == {
        "errors": ["run_source_invalid_json"],
        "attempt": 1,
        "provider_response_id": None,
    }
    assert _project_surface_event_payload(
        "evt.agent.value_emitted",
        {"index": 1, "value_digest": "0" * 64, "value_bytes": 13, "attempt": 1},
    ) == {
        "index": 1,
        "value_digest": "0" * 64,
        "value_bytes": 13,
        "attempt": 1,
        "provider_response_id": None,
    }
    assert _project_surface_event_payload(
        "evt.agent.output_not_applied",
        {"reason": "stale_turn", "current_turn_id": "trn_new"},
    ) == {
        "reason": "stale_turn",
        "current_turn_id": "trn_new",
    }
