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
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ariel.app import create_app
from ariel.persistence import MemoryLogRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_memory_subsystem_call,
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
        if is_memory_subsystem_call(input_items):
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
        if is_memory_subsystem_call(input_items):
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
        if is_memory_subsystem_call(input_items):
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
        if is_memory_subsystem_call(input_items):
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


# ===========================================================================
# Test 4 — succeeded read capability's execution_output is surfaced to the
# model on the next round so its emit_message can be grounded.
#
# Structural bug fixed: prior to this, the syscall-trace branch of
# ``run_agent_loop`` fed back only ``{action_attempt_id, capability_id,
# status, policy_decision, approval_required}`` — stripping the actual
# ``execution_output``. The model saw "cap.X succeeded" with no payload and
# either fabricated absence ("no events found") or guessed at content. The
# rail closes the data-flow loop: capability → program → model context.
# ===========================================================================


_DISTINCTIVE_SNIPPET = "Weekly Career Meeting at Fractal Tech"


@dataclass
class _SyscallThenMessageAdapter:
    """Round 1: program runs a read cap, emits NO message (falls into the
    syscall-trace branch). Round 2: program emits a grounded message. The
    test asserts round 2's input_items carry the round-1 cap's
    ``execution_output`` — without the fix, only a status summary appears.
    """

    provider: str = "provider.exec-output"
    model: str = "model.exec-output-v1"
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
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle
        self.main_call_count += 1
        self.last_input_items.append(list(input_items))
        if self.main_call_count == 1:
            # Round 1: search memory, store nothing in scratch, emit no message
            # — this exercises the syscall-trace branch on the next round.
            source = (
                "result = memory.search(query='career meeting')\n"
                # No emit_message — the loop must continue.
            )
            return _run_response(
                source, provider=self.provider, model=self.model, rid="resp_round1"
            )
        # Round 2: emit a grounded message. (The structural fix asserted here
        # is about the input_items the model SEES, not the message text the
        # adapter fabricates.)
        return _run_response(
            "agent.emit_message(text='Found one hit on your career meeting.')\n",
            provider=self.provider,
            model=self.model,
            rid="resp_round2",
        )


def _seed_memory_log_hit(postgres_url: str, snippet: str) -> None:
    """Insert a memory_log row the search query can find, so ``cap.memory.search``
    returns a non-empty ``hits`` payload (the data that must reach the model)."""
    engine = create_engine(postgres_url, future=True)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    with session_factory() as db:
        with db.begin():
            db.add(
                MemoryLogRecord(
                    id="mev_test_distinctive_hit",
                    created_at=datetime.now(tz=UTC),
                    kind="user_message",
                    content=snippet,
                    embedding=None,
                    session_id=None,
                    turn_id=None,
                    taint="clean",
                    source_ref=None,
                )
            )


def test_succeeded_read_execution_output_reaches_next_round_context(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round that runs ``memory.search`` and emits no message must surface
    that call's ``execution_output`` (containing the seeded snippet) in the
    NEXT round's ``input_items`` — both in the structured ``function_call_output``
    payload and in the human-readable syscall-trace system message.

    Without the fix the model sees only ``{capability_id, status, ...}`` with
    no payload, is structurally blind to what its tool returned, and produces
    hollow responses.
    """

    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = _SyscallThenMessageAdapter()
    with _build_client(postgres_url, adapter) as client:
        _seed_memory_log_hit(postgres_url, _DISTINCTIVE_SNIPPET)
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="what's on my career meeting?")

    assert turn.status == "completed"
    assert adapter.main_call_count == 2

    # Round 2 input_items must contain the seeded snippet — proof the
    # execution_output of round 1's memory.search reached the model context.
    round2_items = adapter.last_input_items[1]

    # The function_call_output for the run call must carry the action_attempts
    # observation list, and that list must include the execution_output payload
    # (the hits with the seeded snippet) — not just a status summary.
    function_call_outputs = [
        item for item in round2_items if item.get("type") == "function_call_output"
    ]
    found_output_in_structured = False
    for fc_out in function_call_outputs:
        body = json.loads(fc_out["output"])
        attempts = body.get("action_attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            exec_output = attempt.get("execution_output")
            if not isinstance(exec_output, dict):
                continue
            hits = exec_output.get("hits")
            if isinstance(hits, list) and any(
                isinstance(h, dict) and _DISTINCTIVE_SNIPPET in str(h.get("snippet", ""))
                for h in hits
            ):
                found_output_in_structured = True
                break
        if found_output_in_structured:
            break
    assert found_output_in_structured, (
        "Round 2 must receive the round-1 memory.search execution_output "
        "(carrying the seeded distinctive snippet) in the function_call_output "
        "payload. Without this the model is blind to capability results."
    )

    # The human-readable system "syscall trace" block must also contain the
    # snippet — defense in depth for models that parse system text more than
    # structured tool outputs.
    system_blocks = [
        item.get("content", "")
        for item in round2_items
        if item.get("role") == "system" and isinstance(item.get("content"), str)
    ]
    assert any(_DISTINCTIVE_SNIPPET in block for block in system_blocks), (
        "Round 2 must include the syscall-trace system block with the "
        "memory.search execution_output containing the seeded snippet."
    )


# ===========================================================================
# Test 5 — premature-synthesis rail: a round-one program that both performs
# a read capability call and emits a user-visible message has its message
# dropped, because the message text was authored before the call's result
# was observed.  The loop continues; the model authors a second round whose
# emit_message is grounded in the round-one observation.
#
# This is the structural fix for the synthesis-question bug: a synthesis
# prompt ("given my calendar and emails, what's most important") used to
# terminate in 2 model rounds (retriever + main agent's single "fetch +
# fabricate" program), producing a hollow paragraph that quoted nothing the
# tools returned.  The rail forces the model into a real reason→act→observe
# cadence: round 1 observes, round 2+ synthesises.
# ===========================================================================


@dataclass
class _PrematureSynthesisAdapter:
    """Round 1: memory.search + agent.emit_message (the synthesis-question
    bug shape).  Round 2: a grounded agent.emit_message.

    Without the premature-synthesis rail, round 1 emits a hollow message and
    the loop ends in one main-agent model call.  With the rail, the round-1
    message is dropped, the loop continues, and the round-2 message is
    delivered as the assistant message.
    """

    provider: str = "provider.synthesis"
    model: str = "model.synthesis-v1"
    main_call_count: int = 0
    last_input_items: list[list[dict[str, Any]]] = field(default_factory=list)
    round_one_message: str = "The most important thing today is the career meeting at Fractal Tech."
    round_two_message: str = (
        "After looking at the search results, the most important thing today is "
        "the career meeting at Fractal Tech."
    )

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle
        self.main_call_count += 1
        self.last_input_items.append(list(input_items))
        if self.main_call_count == 1:
            # The bug shape: a read capability + an emit_message in the same
            # program.  The message text was authored before the search ran.
            source = (
                "result = memory.search(query='career meeting')\n"
                f"agent.emit_message(text={self.round_one_message!r})\n"
            )
            return _run_response(
                source,
                provider=self.provider,
                model=self.model,
                rid="resp_synthesis_round1",
            )
        # Round 2: a clean emit_message (grounded in the round-1 observation).
        return _run_response(
            f"agent.emit_message(text={self.round_two_message!r})\n",
            provider=self.provider,
            model=self.model,
            rid="resp_synthesis_round2",
        )


def test_main_loop_premature_synthesis_rail_drops_round_one_message_and_forces_a_deliberation_round(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round-one program that runs a read capability AND emits a message
    has its message dropped.  The loop continues; round two's emit_message
    becomes the assistant message.

    Without the structural rail the loop would exit at round 1 — the model
    would have emitted exactly once, and the hollow paragraph it authored
    before observing the search result would be delivered to the user.
    """

    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = _PrematureSynthesisAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(
            client,
            session_id,
            message="given my notes, what's the most important thing for me today?",
        )

    assert turn.status == "completed"
    # The loop ran two main-agent model rounds, not one — the rail forced
    # the deliberation round.
    assert adapter.main_call_count == 2
    # The delivered message is round two's grounded text, not round one's
    # hollow paragraph.
    assert turn.assistant_message == adapter.round_two_message

    # The premature-synthesis rail event was emitted with the expected payload
    # (rejected message length and the read capability_id that triggered it).
    timeline = client.get(f"/v1/sessions/{session_id}/events").json()
    events = timeline["turns"][0]["events"]
    rejection_events = [
        event for event in events if event["event_type"] == "evt.agent.premature_synthesis_rejected"
    ]
    assert len(rejection_events) == 1
    payload = rejection_events[0]["payload"]
    assert payload["model_call_count"] == 1
    assert payload["rejected_message_chars"] == len(adapter.round_one_message)
    assert payload["read_capability_ids"] == ["cap.memory.search"]


@dataclass
class _GreetingOnlyAdapter:
    """Round 1: only ``agent.emit_message`` with no capability call.

    A pure-greeting round must not be dropped — the rail's domain is
    ``read capability + emit_message``, not every round-one emit.
    """

    provider: str = "provider.greeting"
    model: str = "model.greeting-v1"
    main_call_count: int = 0
    greeting_text: str = "Hello."

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle, input_items
        self.main_call_count += 1
        return _run_response(
            f"agent.emit_message(text={self.greeting_text!r})\n",
            provider=self.provider,
            model=self.model,
            rid="resp_greeting",
        )


def test_main_loop_pure_emit_message_round_one_is_not_dropped(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round-one program that emits a message with no capability call is
    delivered as-is; the rail only fires on ``read capability + emit_message``."""

    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")

    adapter = _GreetingOnlyAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="hi")

    assert turn.status == "completed"
    assert adapter.main_call_count == 1
    assert turn.assistant_message == adapter.greeting_text

    timeline = client.get(f"/v1/sessions/{session_id}/events").json()
    event_types = [event["event_type"] for event in timeline["turns"][0]["events"]]
    assert "evt.agent.premature_synthesis_rejected" not in event_types


# ===========================================================================
# Test 6 — failed program preserves the succeeded action_attempts'
# execution_output in the next round's function_call_output payload.
#
# Bug fixed: the failed-program branch of ``run_agent_loop`` previously fed
# back only ``{status: "failed", errors: [...]}`` — stripping the
# action_attempts list entirely. When a program ran a successful syscall
# (e.g. ``memory.search``) and THEN raised Python (NameError, ImportError,
# AttributeError, etc.), the recovery round was blind to what actually ran.
# The fix mirrors the clean-program and emit_value branches, both of which
# already pass ``_action_attempt_observations(...)`` through.
# ===========================================================================


_FAILED_PROGRAM_DISTINCTIVE_SNIPPET = "Acme term sheet revision from counsel"


@dataclass
class _SearchThenRaiseAdapter:
    """Round 1: program runs memory.search (succeeds) then raises NameError
    on the next line. Round 2: program emits a recovery message. The test
    asserts round 2's function_call_output carries the round-1 search's
    ``execution_output`` (containing the seeded snippet) — without the fix,
    the payload is ``{status: "failed", errors: [...]}`` only.
    """

    provider: str = "provider.failed-with-syscalls"
    model: str = "model.failed-with-syscalls-v1"
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
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        del tools, user_message, history, context_bundle
        self.main_call_count += 1
        self.last_input_items.append(list(input_items))
        if self.main_call_count == 1:
            # Round 1: search succeeds, then a NameError raises on the next
            # line. The bug-1 fix must still pass the succeeded search's
            # execution_output forward to round 2.
            source = "result = memory.search(query='term sheet')\nraise NameError('e')\n"
            return _run_response(
                source,
                provider=self.provider,
                model=self.model,
                rid="resp_failed_round1",
            )
        return _run_response(
            "agent.emit_message(text='Recovered after the prior round raised.')\n",
            provider=self.provider,
            model=self.model,
            rid="resp_failed_round2",
        )


def test_failed_program_preserves_succeeded_action_attempt_output_for_recovery(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a program runs ``memory.search`` successfully and then raises
    Python, the next round MUST receive that succeeded call's
    ``execution_output`` (with the seeded snippet) inside the
    function_call_output payload's ``action_attempts`` list.

    Without the fix the recovery round sees only ``{status: "failed",
    errors: [...]}`` and is structurally blind to what its own program
    accomplished before the crash. With the fix the model can reason from
    real data on its recovery round.
    """

    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "20000")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")

    adapter = _SearchThenRaiseAdapter()
    with _build_client(postgres_url, adapter) as client:
        _seed_memory_log_hit(postgres_url, _FAILED_PROGRAM_DISTINCTIVE_SNIPPET)
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(
            client, session_id, message="what's in my term sheet revision?"
        )

    assert turn.status == "completed"
    assert adapter.main_call_count == 2

    # Round 2 must see the round-1 succeeded memory.search's execution_output
    # in the function_call_output for the failed run — proof the
    # action_attempts list is preserved across the failure recovery path.
    round2_items = adapter.last_input_items[1]
    function_call_outputs = [
        item for item in round2_items if item.get("type") == "function_call_output"
    ]
    found_output_in_failed_payload = False
    for fc_out in function_call_outputs:
        body = json.loads(fc_out["output"])
        if body.get("status") != "failed":
            continue
        attempts = body.get("action_attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            exec_output = attempt.get("execution_output")
            if not isinstance(exec_output, dict):
                continue
            hits = exec_output.get("hits")
            if isinstance(hits, list) and any(
                isinstance(h, dict)
                and _FAILED_PROGRAM_DISTINCTIVE_SNIPPET in str(h.get("snippet", ""))
                for h in hits
            ):
                found_output_in_failed_payload = True
                break
        if found_output_in_failed_payload:
            break
    assert found_output_in_failed_payload, (
        "Round 2 must receive the round-1 memory.search execution_output "
        "(carrying the seeded snippet) inside the failed-program's "
        "function_call_output action_attempts list. Without this the model "
        "is blind on recovery to what its own program actually accomplished "
        "before the crash."
    )
