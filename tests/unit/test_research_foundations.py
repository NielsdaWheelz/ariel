"""Unit tests for research configuration and capability validation.

Covers:
- ``config.py``: ``research_run_budget_seconds`` default, env override, validator.
- ``capability_registry.py``: ``cap.research.investigate`` validation, contract shape,
  and run-callable alias.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from ariel.capability_registry import (
    capability_id_for_run_callable,
    get_capability,
    run_callable_name_for_capability_id,
)
from ariel.config import AppSettings


# ---------------------------------------------------------------------------
# config.py — research_run_budget_seconds
# ---------------------------------------------------------------------------


def test_research_run_budget_seconds_default_is_300(monkeypatch: pytest.MonkeyPatch) -> None:
    """``research_run_budget_seconds`` defaults to 300.0 when not set."""
    monkeypatch.delenv("ARIEL_RESEARCH_RUN_BUDGET_SECONDS", raising=False)

    settings = AppSettings.model_validate({})
    assert settings.research_run_budget_seconds == 300.0


def test_research_run_budget_seconds_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARIEL_RESEARCH_RUN_BUDGET_SECONDS`` overrides the default."""
    monkeypatch.setenv("ARIEL_RESEARCH_RUN_BUDGET_SECONDS", "600.0")

    settings = AppSettings()
    assert settings.research_run_budget_seconds == 600.0


def test_research_run_budget_seconds_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero value for ``research_run_budget_seconds`` fails validation."""
    monkeypatch.setenv("ARIEL_RESEARCH_RUN_BUDGET_SECONDS", "0")

    with pytest.raises(ValidationError):
        AppSettings()


def test_research_run_budget_seconds_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative value for ``research_run_budget_seconds`` fails validation."""
    with pytest.raises(ValidationError):
        cast(Any, AppSettings)(_env_file=None, research_run_budget_seconds=-1.0)


# ---------------------------------------------------------------------------
# capability_registry.py — cap.research.investigate validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_input", "expected"),
    [
        (
            {"question": "  What is the status of X?  ", "mode": "web"},
            {"question": "What is the status of X?", "mode": "web"},
        ),
        (
            {"question": "Find emails from Alice about the budget", "mode": "personal"},
            {"question": "Find emails from Alice about the budget", "mode": "personal"},
        ),
        (
            {"question": "What did I decide about Q2 retainers?", "mode": "memories"},
            {"question": "What did I decide about Q2 retainers?", "mode": "memories"},
        ),
    ],
)
def test_research_investigate_input_accepts_valid_modes(
    raw_input: dict[str, object],
    expected: dict[str, str],
) -> None:
    """Every research mode accepts a well-formed question."""
    capability = get_capability("cap.research.investigate")
    assert capability is not None

    normalized, error = capability.validate_input(raw_input)
    assert error is None
    assert normalized == expected


def test_research_investigate_input_rejects_bad_inputs() -> None:
    """Every ill-formed payload fails closed with ``schema_invalid``."""
    capability = get_capability("cap.research.investigate")
    assert capability is not None

    over_length_question = "x" * 4001
    bad_inputs: list[dict[str, object]] = [
        {},
        {"question": "why?"},
        {"mode": "web"},
        {"question": "why?", "mode": "web", "extra": "no"},
        {"question": "", "mode": "web"},
        {"question": "   ", "mode": "web"},
        {"question": over_length_question, "mode": "web"},
        {"question": "why?", "mode": "hybrid"},
        {"question": "why?", "mode": ""},
        {"question": "why?", "mode": 42},
        {"question": 999, "mode": "web"},
    ]
    for raw_input in bad_inputs:
        normalized, error = capability.validate_input(raw_input)
        assert normalized is None, raw_input
        assert error == "schema_invalid", raw_input


def test_research_investigate_input_rejects_status_poll_questions() -> None:
    """Queued research handles are not investigation questions.

    The validator must reject the two precise poll shapes so a malformed status
    check never enqueues another research run.
    """
    capability = get_capability("cap.research.investigate")
    assert capability is not None

    poll_questions: list[str] = [
        "status:tsk_01ks4etnmd54cb2qg1z2khe58d",
        "Status:tsk_01abc",  # case-insensitive
        "STATUS:anything",
        "status: tsk_01abc",  # space after colon
        "tsk_01abc",  # short bare task id
        "tsk_01ks4et",
        "is tsk_01abc?",  # < 20 chars with tsk_
    ]
    for question in poll_questions:
        normalized, error = capability.validate_input({"question": question, "mode": "personal"})
        assert normalized is None, question
        assert error == "schema_invalid", question


def test_research_investigate_input_accepts_real_questions_mentioning_tsk() -> None:
    """The status-poll guard must not swallow legitimate longer questions
    that happen to contain a ``tsk_`` substring (e.g. a question about an
    earlier task). The guard is bounded to short questions only.
    """
    legitimate = (
        "What did the earlier research task tsk_01abc actually find about "
        "the Q2 retainer revisions?"
    )
    capability = get_capability("cap.research.investigate")
    assert capability is not None

    normalized, error = capability.validate_input({"question": legitimate, "mode": "personal"})
    assert error is None
    assert normalized == {"question": legitimate, "mode": "personal"}


# ---------------------------------------------------------------------------
# capability_registry.py — cap.research.investigate contract
# ---------------------------------------------------------------------------


def test_research_investigate_capability_contract() -> None:
    """``cap.research.investigate`` is allow_inline / read, has execute=None, and
    no egress destinations — it enqueues a task rather than reaching out itself."""
    capability = get_capability("cap.research.investigate")
    assert capability is not None
    assert capability.capability_id == "cap.research.investigate"
    assert capability.version == "1.0"
    assert capability.impact_level == "read"
    assert capability.policy_decision == "allow_inline"
    assert capability.execute is None
    assert capability.allowed_egress_destinations == ()
    assert capability.contract_metadata["input_schema"] == "research_investigate_v1"
    assert capability.contract_metadata["output_schema"] == "research_task_start_v1"
    assert capability.contract_metadata["idempotency"] == "action_attempt_id"
    assert capability.contract_metadata["execution_mode"] == "background_task_enqueue"


def test_research_investigate_run_callable_alias() -> None:
    """The ``research.investigate`` alias round-trips through both lookup helpers."""
    assert capability_id_for_run_callable("research.investigate") == "cap.research.investigate"
    assert run_callable_name_for_capability_id("cap.research.investigate") == "research.investigate"
