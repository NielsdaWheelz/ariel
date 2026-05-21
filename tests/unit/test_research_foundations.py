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
    RESEARCH_MEMORIES_CAPABILITY_IDS,
    RESEARCH_PERSONAL_CAPABILITY_IDS,
    RESEARCH_WEB_CAPABILITY_IDS,
    _validate_research_investigate_input,
    capability_id_for_run_callable,
    get_capability,
    run_callable_name_for_capability_id,
    run_callable_signature,
)
from ariel.config import AppSettings
from ariel.research_runtime import _build_research_input_items


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


def test_research_investigate_input_rejects_status_poll_questions() -> None:
    """The model has been observed treating the queued response of
    ``cap.research.investigate`` as something to poll by re-issuing
    ``investigate({question: "status:tsk_...", mode: "personal"})``
    repeatedly — 22 stuck agent wakes built up in 1 minute. The validator
    must reject the two precise poll shapes so a malformed poll never
    enqueues another research run.
    """
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
        normalized, error = _validate_research_investigate_input(
            {"question": question, "mode": "personal"}
        )
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
    normalized, error = _validate_research_investigate_input(
        {"question": legitimate, "mode": "memories"}
    )
    assert error is None
    assert normalized == {"question": legitimate, "mode": "memories"}


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


# ---------------------------------------------------------------------------
# research_runtime.py — eligible-callables block carries precise signatures
# ---------------------------------------------------------------------------


def _eligible_for(mode: str) -> list[str]:
    if mode == "web":
        cap_ids = RESEARCH_WEB_CAPABILITY_IDS
    elif mode == "personal":
        cap_ids = RESEARCH_PERSONAL_CAPABILITY_IDS
    elif mode == "memories":
        cap_ids = RESEARCH_MEMORIES_CAPABILITY_IDS
    else:
        raise AssertionError(f"unknown research mode: {mode}")
    return sorted(
        name
        for name in (run_callable_name_for_capability_id(cap_id) for cap_id in cap_ids)
        if name is not None
    )


def _callables_block(mode: str) -> str:
    """The third system block — the eligible-callables enumeration."""
    items = _build_research_input_items(
        question="probe question",
        mode=mode,
        eligible_callables=_eligible_for(mode),
    )
    # Layout: [system(role framing), system(mode framing), system(callables),
    #          system(finishing), user(question)]
    block = items[2]
    assert block["role"] == "system"
    return cast(str, block["content"])


def test_research_prompt_lists_every_web_callable_with_signature() -> None:
    """Every web-mode callable appears in the prompt rendered as
    ``- name(args)`` — the same shape ``run_callable_signature`` returns."""
    content = _callables_block("web")
    for name in _eligible_for("web"):
        signature = run_callable_signature(name)
        assert signature, f"{name} has no signature in RUN_CALLABLE_SIGNATURES"
        assert f"- {name}{signature}" in content, (
            f"web prompt missing precise signature for {name}: expected line '- {name}{signature}'"
        )


def test_research_prompt_lists_every_personal_callable_with_signature() -> None:
    """Every personal-mode callable appears in the prompt rendered as
    ``- name(args)`` — the same shape ``run_callable_signature`` returns."""
    content = _callables_block("personal")
    for name in _eligible_for("personal"):
        signature = run_callable_signature(name)
        assert signature, f"{name} has no signature in RUN_CALLABLE_SIGNATURES"
        assert f"- {name}{signature}" in content, (
            f"personal prompt missing precise signature for {name}: expected "
            f"line '- {name}{signature}'"
        )


def test_research_prompt_lists_every_memories_callable_with_signature() -> None:
    """Every memories-mode callable appears in the prompt rendered as
    ``- name(args)`` — the same shape ``run_callable_signature`` returns."""
    content = _callables_block("memories")
    for name in _eligible_for("memories"):
        signature = run_callable_signature(name)
        assert signature, f"{name} has no signature in RUN_CALLABLE_SIGNATURES"
        assert f"- {name}{signature}" in content, (
            f"memories prompt missing precise signature for {name}: expected "
            f"line '- {name}{signature}'"
        )


def test_research_prompt_search_web_signature_is_query_only() -> None:
    """``search.web`` shows ``(query: str)`` — no ``max_results``, no ``topn``.

    This is the smoke-trace bug that motivated the fix: the subagent invented
    ``max_results=10`` because the bare-name listing didn't tell it the validator
    rejects everything except ``query``.
    """
    content = _callables_block("web")
    assert "- search.web(query: str)" in content
    assert "max_results" not in content
    assert "topn" not in content


def test_research_prompt_calendar_list_signature_uses_window_keys() -> None:
    """``calendar.list`` shows ``(window_start: str, window_end: str)`` —
    not ``start``/``end`` or ``start_time``/``end_time``.

    This is the second smoke-trace bug: the subagent invented ``start``/``end``
    because the bare-name listing didn't tell it the validator requires
    ``window_start`` / ``window_end``.
    """
    content = _callables_block("personal")
    assert (
        "- calendar.list(window_start: str, window_end: str, calendar_id: str = 'primary')"
        in content
    )
    # Argument names the model previously invented must not appear as keys.
    assert "start_time" not in content
    assert "end_time" not in content
    # ``start``/``end`` appear inside the output schema (e.g. event ``start``,
    # ``end`` fields) for calendar.list, so we cannot blanket-ban them. The
    # precise check above (``window_start``/``window_end`` as the argument
    # signature) is what matters.
