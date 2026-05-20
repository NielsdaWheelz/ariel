"""Main-loop recovery tests: typed nudges, semantic stuck rail, and the
budget-exhausted summary call.

These exercise three new behaviours layered on top of ``run_agent_loop``:

- Layer 1+2: when the model authors ``agent.emit_finding(...)`` inside the
  main loop, the host returns the typed error string from ``run_runtime``
  and the next round receives a specialised nudge whose text contains the
  substring ``"is not available in this loop"``.
- Layer 3: a semantic stuck rail halts the loop when ``program_errors``
  repeats across consecutive rounds even if the source bytes differ.
- Layer 4: on budget exhaustion the main loop attempts one constrained
  model call (``tools=[]``) for a summary; usable plain text becomes the
  assistant message, otherwise the existing canned line is emitted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ariel.app import create_app
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_retriever_call,
    post_message_and_drain,
    responses_message,
)


def _run_response(source: str, *, provider: str, model: str, rid: str) -> dict[str, Any]:
    """Wrap a raw run-program source as a Responses-API function_call payload."""
    return {
        "provider": provider,
        "model": model,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "provider_response_id": rid,
        "output": [
            {
                "type": "function_call",
                "id": f"fc_{rid}",
                "call_id": f"call_{rid}",
                "name": "run",
                "arguments": json.dumps({"source": source}, sort_keys=True),
                "status": "completed",
            }
        ],
    }


_EMIT_FINDING_MAIN_ERROR = (
    "agent.emit_finding is only available inside a research run; "
    "finish the main loop with agent.emit_message"
)

_INVALID_FINDING_SOURCE = "agent.emit_finding(summary='x', claims=[], gaps=[], sources=[])\n"

_VALID_MESSAGE_SOURCE = "agent.emit_message(text='Hello, here is the answer.')\n"


def _build_client(postgres_url: str, adapter: Any) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


# ===========================================================================
# Test 1 — main-loop emit_finding mis-call recovers via typed nudge.
# ===========================================================================


@dataclass
class _FindingThenMessageAdapter:
    """Round 1: invalid emit_finding (main-loop). Round 2: valid emit_message."""

    provider: str = "provider.recovery"
    model: str = "model.recovery-v1"
    main_call_count: int = 0
    last_input_items: list[list[dict[str, Any]]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle
        self.main_call_count += 1
        self.last_input_items.append(list(input_items))
        if self.main_call_count == 1:
            return _run_response(
                _INVALID_FINDING_SOURCE,
                provider=self.provider,
                model=self.model,
                rid="resp_finding_misuse",
            )
        return _run_response(
            _VALID_MESSAGE_SOURCE,
            provider=self.provider,
            model=self.model,
            rid="resp_recovered_msg",
        )


def test_main_loop_emit_finding_misuse_recovers_with_typed_nudge(
    postgres_url: str,
) -> None:
    adapter = _FindingThenMessageAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="answer this")

        assert turn.status == "completed"
        assert turn.assistant_message == "Hello, here is the answer."
        assert adapter.main_call_count == 2

        timeline = client.get(f"/v1/sessions/{session_id}/events").json()
        events = timeline["turns"][0]["events"]
        event_types = [event["event_type"] for event in events]
        assert "evt.run.validation_failed" in event_types
        assert "evt.turn.completed" in event_types
        assert "evt.turn.failed" not in event_types

        validation_failed = next(
            event for event in events if event["event_type"] == "evt.run.validation_failed"
        )
        assert any(
            _EMIT_FINDING_MAIN_ERROR in err for err in validation_failed["payload"]["errors"]
        )

        # The second model call sees the specialised typed nudge, not the
        # generic program-failure nudge.
        recovery_items = adapter.last_input_items[1]
        nudge_seen = any(
            isinstance(item.get("content"), str)
            and "is not available in this loop" in item["content"]
            for item in recovery_items
        )
        assert nudge_seen


# ===========================================================================
# Test 2 — semantic stuck rail halts on repeated program_errors across
# whitespace-different sources.
# ===========================================================================


_WHITESPACE_VARIANTS = (
    "agent.emit_finding(summary='a', claims=[], gaps=[], sources=[])\n",
    "agent.emit_finding(summary = 'a', claims=[],  gaps=[], sources=[])\n",
    "agent.emit_finding(summary='a',\n    claims=[], gaps=[], sources=[])\n",
)


@dataclass
class _WhitespaceVariantFindingAdapter:
    """Returns whitespace-different but semantically identical emit_finding calls."""

    provider: str = "provider.stuck-semantic"
    model: str = "model.stuck-semantic-v1"
    main_call_count: int = 0

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle, input_items
        index = self.main_call_count
        self.main_call_count += 1
        # Beyond the prepared variants, keep returning the last one so the
        # loop must end via stuck-detection rather than running out of canned
        # responses.
        source = _WHITESPACE_VARIANTS[min(index, len(_WHITESPACE_VARIANTS) - 1)]
        return _run_response(
            source,
            provider=self.provider,
            model=self.model,
            rid=f"resp_whitespace_{index}",
        )


def test_main_loop_semantic_stuck_rail_halts_on_repeated_program_errors(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Generous budget and backstop so wall-clock / call-count rails do not
    # fire — the semantic stuck rail must be the one that halts the loop.
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = _WhitespaceVariantFindingAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="answer this")

        # The loop halted gracefully (not a failed turn) before consuming
        # all three whitespace variants — the semantic stuck rail trips on
        # repeated program_errors.
        assert turn.status == "completed"
        assert adapter.main_call_count <= 3

        timeline = client.get(f"/v1/sessions/{session_id}/events").json()
        turn_data = timeline["turns"][0]
        event_types = [event["event_type"] for event in turn_data["events"]]
        assert "evt.run.validation_failed" in event_types
        assert "evt.turn.completed" in event_types

        validation_failures = [
            event
            for event in turn_data["events"]
            if event["event_type"] == "evt.run.validation_failed"
        ]
        # Each program_errors carries the same typed error string — that
        # repetition is what the semantic rail observes.
        for failure in validation_failures:
            assert any(_EMIT_FINDING_MAIN_ERROR in err for err in failure["payload"]["errors"])


# ===========================================================================
# Test 3 — budget exhaustion triggers the constrained model summary call.
# ===========================================================================


@dataclass
class _BudgetExhaustedSummaryAdapter:
    """Round 1: invalid emit_finding. Round 2 (constrained, tools=[]): plain text."""

    provider: str = "provider.summary"
    model: str = "model.summary-v1"
    final_summary_text: str = (
        "I started looking up your calendar but ran out of time before producing an answer."
    )
    main_call_count: int = 0
    constrained_tools_snapshot: list[list[dict[str, Any]]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del user_message, history, context_bundle
        self.main_call_count += 1
        # The constrained final-summary call carries tools=[]. Return a
        # plain message response, which becomes the assistant message.
        if tools == []:
            self.constrained_tools_snapshot.append(list(input_items))
            return responses_message(
                assistant_text=self.final_summary_text,
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_summary",
                input_tokens=2,
                output_tokens=8,
            )
        return _run_response(
            _INVALID_FINDING_SOURCE,
            provider=self.provider,
            model=self.model,
            rid=f"resp_summary_main_{self.main_call_count}",
        )


def test_main_loop_budget_exhaustion_invokes_model_summary(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    # Tiny budget — wall-clock rail trips quickly. The fake perf_counter
    # advances 0.1s per call so the budget check fires on the next round.
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "0.001")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.1
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = _BudgetExhaustedSummaryAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="check my calendar")

        assert turn.status == "completed"
        # The model-authored summary becomes the assistant message — not
        # the canned "I wasn't able to finish..." line.
        assert turn.assistant_message == adapter.final_summary_text
        # The constrained call (tools=[]) was made exactly once.
        assert len(adapter.constrained_tools_snapshot) == 1


@dataclass
class _BudgetExhaustedEmptySummaryAdapter:
    """Constrained call returns empty/garbage; the canned line is emitted."""

    provider: str = "provider.empty-summary"
    model: str = "model.empty-summary-v1"
    main_call_count: int = 0
    constrained_calls: int = 0

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del user_message, history, context_bundle, input_items
        self.main_call_count += 1
        if tools == []:
            self.constrained_calls += 1
            return responses_message(
                assistant_text="",
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_empty_summary",
                input_tokens=1,
                output_tokens=1,
            )
        return _run_response(
            _INVALID_FINDING_SOURCE,
            provider=self.provider,
            model=self.model,
            rid=f"resp_empty_summary_main_{self.main_call_count}",
        )


def test_main_loop_budget_exhaustion_uses_canned_line_when_summary_empty(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "0.001")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    counter = {"seconds": 0.0}

    def fake_perf_counter() -> float:
        counter["seconds"] += 0.1
        return counter["seconds"]

    monkeypatch.setattr("ariel.app.time.perf_counter", fake_perf_counter)

    adapter = _BudgetExhaustedEmptySummaryAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="check my calendar")

        assert turn.status == "completed"
        # Empty model summary → existing canned line emission path.
        assert turn.assistant_message == ("I wasn't able to finish that within the time available.")
        assert adapter.constrained_calls == 1
