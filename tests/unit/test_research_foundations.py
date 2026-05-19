"""Unit tests for the P3 research foundations.

Covers:
- ``config.py``: ``research_run_budget_seconds`` default, env override, validator.
- ``capability_registry.py``: ``_validate_research_investigate_input`` happy path and
  rejection cases; ``cap.research.investigate`` contract shape; the module-level
  whitelist constants; the run-callable alias.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from ariel.capability_registry import (
    RESEARCH_CAPABILITY_IDS,
    RESEARCH_PERSONAL_CAPABILITY_IDS,
    RESEARCH_WEB_CAPABILITY_IDS,
    _validate_research_investigate_input,
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
# capability_registry.py — _validate_research_investigate_input
# ---------------------------------------------------------------------------


def test_research_investigate_input_accepts_valid_web() -> None:
    """A well-formed web-mode input validates cleanly."""
    normalized, error = _validate_research_investigate_input(
        {"question": "  What is the status of X?  ", "mode": "web"}
    )
    assert error is None
    assert normalized == {"question": "What is the status of X?", "mode": "web"}


def test_research_investigate_input_accepts_valid_personal() -> None:
    """A well-formed personal-mode input validates cleanly."""
    normalized, error = _validate_research_investigate_input(
        {"question": "Find emails from Alice about the budget", "mode": "personal"}
    )
    assert error is None
    assert normalized == {
        "question": "Find emails from Alice about the budget",
        "mode": "personal",
    }


def test_research_investigate_input_rejects_bad_inputs() -> None:
    """Every ill-formed payload fails closed with ``schema_invalid``."""
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
        normalized, error = _validate_research_investigate_input(raw_input)
        assert normalized is None, raw_input
        assert error == "schema_invalid", raw_input


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


def test_research_capability_ids_constant() -> None:
    """``RESEARCH_CAPABILITY_IDS`` contains exactly ``cap.research.investigate``."""
    assert RESEARCH_CAPABILITY_IDS == {"cap.research.investigate"}


def test_research_investigate_run_callable_alias() -> None:
    """The ``research.investigate`` alias round-trips through both lookup helpers."""
    assert capability_id_for_run_callable("research.investigate") == "cap.research.investigate"
    assert run_callable_name_for_capability_id("cap.research.investigate") == "research.investigate"


# ---------------------------------------------------------------------------
# capability_registry.py — whitelist constants
# ---------------------------------------------------------------------------


def test_research_web_whitelist_capabilities_exist_and_are_read() -> None:
    """Every cap id in ``RESEARCH_WEB_CAPABILITY_IDS`` exists in the registry and
    is ``impact_level='read'``."""
    assert RESEARCH_WEB_CAPABILITY_IDS == {
        "cap.search.web",
        "cap.search.news",
        "cap.web.extract",
    }
    for cap_id in RESEARCH_WEB_CAPABILITY_IDS:
        cap = get_capability(cap_id)
        assert cap is not None, f"{cap_id} not found in registry"
        assert cap.impact_level == "read", f"{cap_id} impact_level is {cap.impact_level!r}"


def test_research_personal_whitelist_capabilities_exist_and_are_read() -> None:
    """Every cap id in ``RESEARCH_PERSONAL_CAPABILITY_IDS`` exists in the registry
    and is ``impact_level='read'``."""
    assert RESEARCH_PERSONAL_CAPABILITY_IDS == {
        "cap.email.search",
        "cap.email.read",
        "cap.drive.search",
        "cap.drive.read",
        "cap.calendar.list",
    }
    for cap_id in RESEARCH_PERSONAL_CAPABILITY_IDS:
        cap = get_capability(cap_id)
        assert cap is not None, f"{cap_id} not found in registry"
        assert cap.impact_level == "read", f"{cap_id} impact_level is {cap.impact_level!r}"


def test_research_web_and_personal_whitelists_are_disjoint() -> None:
    """The two research mode whitelists share no capabilities — the Rule of Two."""
    assert RESEARCH_WEB_CAPABILITY_IDS.isdisjoint(RESEARCH_PERSONAL_CAPABILITY_IDS)
