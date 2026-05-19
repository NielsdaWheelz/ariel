from __future__ import annotations

from ariel.capability_registry import (
    PROACTIVE_CAPABILITY_IDS,
    _validate_proactive_schedule_input,
    capability_id_for_run_callable,
    get_capability,
    run_callable_name_for_capability_id,
)


def test_schedule_input_accepts_a_valid_when_and_note() -> None:
    """A well-formed ``{when, note}`` validates: ``when`` is normalized to a
    canonical RFC3339 ``Z`` timestamp and ``note`` is stripped."""

    normalized, error = _validate_proactive_schedule_input(
        {"when": "2026-06-01T09:00:00+00:00", "note": "  check the deploy  "}
    )

    assert error is None
    assert normalized == {"when": "2026-06-01T09:00:00Z", "note": "check the deploy"}


def test_schedule_input_rejects_bad_input() -> None:
    """Every ill-formed payload fails closed with ``schema_invalid``: an unknown
    or missing key, a non-string field, an empty note, an over-length note, and
    a ``when`` that is not a parseable timestamp."""

    over_length_note = "x" * 12_001
    bad_inputs: list[dict[str, object]] = [
        {},
        {"when": "2026-06-01T09:00:00Z"},
        {"note": "remind me"},
        {"when": "2026-06-01T09:00:00Z", "note": "ok", "extra": "no"},
        {"when": "2026-06-01T09:00:00Z", "note": ""},
        {"when": "2026-06-01T09:00:00Z", "note": "   "},
        {"when": "2026-06-01T09:00:00Z", "note": over_length_note},
        {"when": "not-a-timestamp", "note": "ok"},
        {"when": 1717230000, "note": "ok"},
        {"when": "2026-06-01T09:00:00Z", "note": 7},
    ]
    for raw_input in bad_inputs:
        normalized, error = _validate_proactive_schedule_input(raw_input)
        assert normalized is None, raw_input
        assert error == "schema_invalid", raw_input


def test_schedule_capability_is_an_inline_reversible_syscall() -> None:
    """``cap.proactive.schedule`` is the agent's whole scheduling surface: it is
    the only proactive capability, resolves from the ``proactive.schedule``
    run-callable alias, and carries the ``allow_inline`` / ``write_reversible``
    policy so the program reaches it without an approval round."""

    assert PROACTIVE_CAPABILITY_IDS == {"cap.proactive.schedule"}
    assert capability_id_for_run_callable("proactive.schedule") == "cap.proactive.schedule"
    assert run_callable_name_for_capability_id("cap.proactive.schedule") == "proactive.schedule"

    capability = get_capability("cap.proactive.schedule")
    assert capability is not None
    assert capability.policy_decision == "allow_inline"
    assert capability.impact_level == "write_reversible"
    assert capability.execute is None
