"""Main-loop recovery tests: typed nudges, semantic stuck rail, and
deterministic budget exhaustion.

These exercise three new behaviours layered on top of ``run_agent_loop``:

- Layer 1+2: when the model authors ``agent.emit_finding(...)`` inside the
  main loop, the host returns the typed error string from ``run_runtime``
  and the next round receives a specialised nudge whose text contains the
  substring ``"is not available in this loop"``.
- Layer 3: a semantic stuck rail halts the loop when ``program_errors``
  repeats across consecutive rounds even if the source bytes differ.
- Layer 4: on budget exhaustion the loop returns the deterministic rail
  outcome; the main driver emits the canned line without another model call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, ToolReturnPart
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ariel.model_adapter import ModelCall, ModelResponse
from ariel.persistence import MemoryLogRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_memory_subsystem_call,
    post_message_and_drain,
)


def _run_response(source: str, *, provider: str, model: str, rid: str) -> ModelResponse:
    """Wrap a raw run-program source as a ``run`` tool-call ``ModelResponse``."""
    from tests.integration.responses_helpers import run_response  # noqa: PLC0415

    return run_response(source=source, provider=provider, model=model, provider_response_id=rid)


def _tool_returns(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """Extract the JSON-decoded ``ToolReturnPart.content`` payloads from
    ``messages``.

    The agent loop appends one ``ModelRequest`` per round whose ``parts``
    include a ``ToolReturnPart`` per syscall return; the ``content`` is a
    JSON document carrying ``status``, ``emitted_values``, ``action_attempts``,
    etc. This helper returns the parsed bodies in message order so tests can
    assert on the cross-round payload contract.
    """
    bodies: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and isinstance(part.content, str):
                try:
                    body = json.loads(part.content)
                except json.JSONDecodeError:
                    continue
                if isinstance(body, dict):
                    bodies.append(body)
    return bodies


_EMIT_FINDING_MAIN_ERROR = (
    "agent.emit_finding is not available in the main agent loop; "
    "finish the main loop with agent.emit_message"
)

_INVALID_FINDING_SOURCE = "agent.emit_finding(summary='x', claims=[], gaps=[], sources=[])\n"

_VALID_MESSAGE_SOURCE = "agent.emit_message(text='Hello, here is the answer.')\n"


def _build_client(postgres_url: str, adapter: Any) -> TestClient:
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


# ===========================================================================
# Test 1 — main-loop emit_finding mis-call recovers via typed nudge.
# ===========================================================================


class _FindingThenMessageAdapter(FakeModelAdapter):
    """Round 1: invalid emit_finding (main-loop). Round 2: valid emit_message."""

    provider = "provider.recovery"
    model = "model.recovery-v1"

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count: int = 0
        self.last_messages: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.main_call_count += 1
        self.last_messages.append(list(request.messages))
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
        recovery_messages = adapter.last_messages[1]
        system_contents = [
            part.content
            for message in recovery_messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        ]
        nudge_seen = any("is not available in this loop" in content for content in system_contents)
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


class _WhitespaceVariantFindingAdapter(FakeModelAdapter):
    """Returns whitespace-different but semantically identical emit_finding calls."""

    provider = "provider.stuck-semantic"
    model = "model.stuck-semantic-v1"

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
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
# Test 3 — budget exhaustion does not make a second model-facing call.
# ===========================================================================


class _BudgetExhaustedAdapter(FakeModelAdapter):
    """Round 1+: invalid emit_finding. A later tools=[] call is a defect — the
    canned-line emission path runs without a constrained model call."""

    provider = "provider.summary"
    model = "model.summary-v1"

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count: int = 0
        self.constrained_calls: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.main_call_count += 1
        if request.tools == []:
            self.constrained_calls += 1
            raise AssertionError("budget exhaustion must not call the model with tools=[]")
        return _run_response(
            _INVALID_FINDING_SOURCE,
            provider=self.provider,
            model=self.model,
            rid=f"resp_summary_main_{self.main_call_count}",
        )


def test_main_loop_budget_exhaustion_uses_canned_line_without_summary_call(
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

    adapter = _BudgetExhaustedAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = client.get("/v1/sessions/active").json()["session"]["id"]
        turn = post_message_and_drain(client, session_id, message="check my calendar")

        assert turn.status == "completed"
        assert turn.assistant_message == ("I wasn't able to finish that within the time available.")
        assert adapter.constrained_calls == 0


# ===========================================================================
# Test 4 — cross-round read data is carried only through agent.emit_value.
#
# Capability results return inline to the program. If the model wants facts in
# a later model round, the program must deliberately emit them with
# ``agent.emit_value``; the attempt ledger must not auto-echo execution_output.
# ===========================================================================


_DISTINCTIVE_SNIPPET = "Weekly Career Meeting at Fractal Tech"


class _SyscallThenMessageAdapter(FakeModelAdapter):
    """Round 1: program runs a read cap and deliberately emits the facts it
    wants in the next round. Round 2: program emits a grounded message."""

    provider = "provider.exec-output"
    model = "model.exec-output-v1"

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count: int = 0
        self.last_messages: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.main_call_count += 1
        self.last_messages.append(list(request.messages))
        if self.main_call_count == 1:
            source = (
                "result = memory.search(query='career meeting')\n"
                "agent.emit_value(value={'hits': result['hits']})\n"
            )
            return _run_response(
                source, provider=self.provider, model=self.model, rid="resp_round1"
            )
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


def test_emit_value_carries_read_facts_to_next_round_context(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round must use ``agent.emit_value`` to carry read facts forward."""

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

    round2_messages = adapter.last_messages[1]
    tool_returns = _tool_returns(round2_messages)
    found_emit_value = False
    for body in tool_returns:
        emitted_values = body.get("emitted_values")
        if isinstance(emitted_values, list) and any(
            _DISTINCTIVE_SNIPPET in json.dumps(value) for value in emitted_values
        ):
            found_emit_value = True
        attempts = body.get("action_attempts")
        if isinstance(attempts, list):
            assert all("execution_output" not in attempt for attempt in attempts)
    assert found_emit_value


# ===========================================================================
# Test 5 — premature-synthesis rail: a round-one program that both performs
# a read capability call and emits a user-visible message has its message
# dropped, because the message text was authored before the call's result
# was observed.  The loop continues so the model must fetch deliberately before
# answering.
#
# This is the structural fix for the synthesis-question bug: a synthesis
# prompt ("given my calendar and emails, what's most important") used to
# terminate in 2 model rounds (retriever + main agent's single "fetch +
# fabricate" program), producing a hollow paragraph that quoted nothing the
# tools returned.  The rail forces the model into a real reason→act→observe
# cadence instead of delivering first-round prose.
# ===========================================================================


class _PrematureSynthesisAdapter(FakeModelAdapter):
    """Round 1: memory.search + agent.emit_message (the synthesis-question
    bug shape). Round 2: fetch again and emit the facts for the next round.
    Round 3: answer from the emitted facts.

    Without the premature-synthesis rail, round 1 emits a hollow message and
    the loop ends in one main-agent model call.  With the rail, the round-1
    message is dropped, the loop continues, and the round-3 message is
    delivered as the assistant message.
    """

    provider = "provider.synthesis"
    model = "model.synthesis-v1"
    round_one_message = "The most important thing today is the career meeting at Fractal Tech."
    round_two_message = (
        "After looking at the search results, the most important thing today is "
        "the career meeting at Fractal Tech."
    )

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count: int = 0
        self.last_messages: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.main_call_count += 1
        self.last_messages.append(list(request.messages))
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
        if self.main_call_count == 2:
            return _run_response(
                "result = memory.search(query='career meeting')\n"
                "agent.emit_value(value={'hits': result['hits']})\n",
                provider=self.provider,
                model=self.model,
                rid="resp_synthesis_round2",
            )
        return _run_response(
            f"agent.emit_message(text={self.round_two_message!r})\n",
            provider=self.provider,
            model=self.model,
            rid="resp_synthesis_round3",
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
    # The loop ran three main-agent model rounds, not one — the rail forced a
    # deliberate fetch/emitted-value round before the answer.
    assert adapter.main_call_count == 3
    # The delivered message is the final grounded text, not round one's
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


class _GreetingOnlyAdapter(FakeModelAdapter):
    """Round 1: only ``agent.emit_message`` with no capability call.

    A pure-greeting round must not be dropped — the rail's domain is
    ``read capability + emit_message``, not every round-one emit.
    """

    provider = "provider.greeting"
    model = "model.greeting-v1"
    greeting_text = "Hello."

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
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
# Test 6 — failed program preserves the attempt ledger without payload echo.
#
# When a program ran a successful syscall and then raised, the recovery round
# needs the action-attempt status for diagnosis. It must not receive the read
# payload; failed programs scrub emitted values.
# ===========================================================================


_FAILED_PROGRAM_DISTINCTIVE_SNIPPET = "Acme term sheet revision from counsel"


class _SearchThenRaiseAdapter(FakeModelAdapter):
    """Round 1: program runs memory.search (succeeds) then raises NameError
    on the next line. Round 2: program emits a recovery message."""

    provider = "provider.failed-with-syscalls"
    model = "model.failed-with-syscalls-v1"

    def __init__(self) -> None:
        super().__init__()
        self.main_call_count: int = 0
        self.last_messages: list[list[ModelMessage]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.main_call_count += 1
        self.last_messages.append(list(request.messages))
        if self.main_call_count == 1:
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


def test_failed_program_preserves_action_attempt_status_without_output_echo(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed-program recovery sees attempt status, not read payloads."""

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

    round2_messages = adapter.last_messages[1]
    tool_returns = _tool_returns(round2_messages)
    found_succeeded_attempt = False
    for body in tool_returns:
        if body.get("status") != "failed":
            continue
        attempts = body.get("action_attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            assert "execution_output" not in attempt
            if (
                attempt.get("capability_id") == "cap.memory.search"
                and attempt.get("status") == "succeeded"
            ):
                found_succeeded_attempt = True
        if found_succeeded_attempt:
            break
    assert found_succeeded_attempt
