"""The shared agent loop — one body, three output-mode configurations.

``run_agent_loop`` is the single while-True loop that drives every
configuration of the agent: the main conversational turn (``_wake``), the
read-only research subagent (``run_research``), and the future rememberer
(``output_mode="operations"``).

A **configuration** is a ``LoopConfig`` frozen dataclass that captures every
axis on which the three configurations differ: the output mode (what terminal
result exits the loop), the wall-clock budget, the model-call backstop, the
is-research flag, the judgment-recording flag and type, the retry policy, and
the system-message strings emitted on each non-terminal branch.

The callers — ``_wake`` and ``run_research`` — build their configuration, call
``run_agent_loop``, and map the ``LoopResult`` to their own post-loop
behaviour.  All per-round work (budget check, budget-signal update, model call,
protocol parsing, stuck-detection, ``execute_run_program``, per-program commit,
emit_value eviction, round-history eviction, judgment recording, retry) lives
inside ``run_agent_loop`` and does not appear in either driver.

This module imports only ``config``, ``persistence``, ``run_runtime``,
``ai_judgments``, ``sandbox_runtime``, ``google_connector``,
``agency_daemon``, ``attachment_content``, ``action_runtime``, and stdlib /
SQLAlchemy.  It must never import from ``app.py`` or ``research_runtime.py``.
``ResearchFinding`` is defined here so ``research_runtime.py`` can import it
without creating a cycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, assert_never

from fastapi.encoders import jsonable_encoder
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse as PydAIModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .action_runtime import RuntimeProvenance
from .agency_daemon import AgencyRuntime
from .ai_judgments import AIJudgmentFailure, record_ai_judgment
from .attachment_content import AttachmentContentRuntime
from .config import AppSettings
from .google_connector import GoogleConnectorRuntime
from .model_adapter import (
    ModelAdapter,
    ModelCall,
    ModelMessage,
    ModelResponse,
    ModelTier,
    ReasoningConfig,
    ToolCall,
    ToolSpec,
)
from .persistence import ActionAttemptRecord, TurnRecord
from .run_runtime import (
    ScratchEntry,
    execute_run_program,
    parse_run_function_call,
)
from .sandbox_runtime import RunSandbox


# ---------------------------------------------------------------------------
# ResearchFinding — defined here so research_runtime can import it without
# creating a cycle through app.py.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ResearchFinding:
    """The typed result of one research run.

    ``status`` is one of three values matching the ``research_finding_v1``
    contract:

    - ``"complete"``: the run called ``agent.emit_finding``. ``summary``,
      ``claims``, ``gaps``, and ``sources`` are the four fields the model
      passed to ``agent.emit_finding``.
    - ``"partial"``: the wall-clock budget, the model-call backstop, or
      stuck-detection ended the run before a finding was emitted. The loop
      ran cleanly; it just did not converge. ``summary`` is a short honest
      non-convergence note; the three lists are empty.
    - ``"failed"``: the model call raised an exception. ``summary`` is a
      short honest failure note; the three lists are empty.

    The text fields are model-authored over untrusted content; the caller
    carries this finding with tainted provenance.
    """

    question: str
    mode: str
    status: str
    summary: str
    claims: list[Any]
    gaps: list[Any]
    sources: list[Any]


# ---------------------------------------------------------------------------
# Configuration and result dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LoopConfig:
    """All axes on which the three loop configurations differ.

    Pass one ``LoopConfig`` instance to ``run_agent_loop``; the loop reads only
    this object — it never inspects the caller's local state.

    Attributes
    ----------
    output_mode:
        Which terminal ``RunProgramResult`` field exits the loop.
        ``"message"`` exits on ``emitted_message``, an ``awaiting_approval``
        action attempt, or ``paused``.  ``"finding"`` exits on
        ``emitted_finding``.  ``"operations"`` exits on ``emitted_operations``
        (the summary string from ``agent.emit_done``).
    finding_mode:
        The research mode string (e.g. ``"web"``, ``"personal"``) embedded in
        the returned ``ResearchFinding`` when ``output_mode="finding"``.
        Ignored for other output modes.
    prompt_version:
        The semantic version of the system prompt for this loop configuration.
        Recorded with any ``ai_judgments`` row the loop writes.
    budget_seconds:
        Wall-clock budget for the loop.
    max_model_calls:
        Backstop on the number of model calls before graceful exhaustion.
    is_research_run:
        Passed through to ``execute_run_program``; enables ``agent.emit_finding``
        and ``agent.emit_done`` in the sandbox's syscall whitelist.
    record_judgments:
        When True, the loop records an ``ai_judgments`` row on protocol failure
        and on program failure (the ``_wake`` behaviour).  When False it does
        not (the ``run_research`` behaviour — the loop just loops).
    judgment_type:
        The ``ai_judgments.judgment_type`` to use when ``record_judgments`` is
        True.  Must be ``None`` when ``record_judgments`` is False.
    retry_on_model_error:
        True for ``_wake`` (retries exceptions carrying ``retryable=True``),
        False for ``run_research`` (exits on any model call failure).
    protocol_nudge:
        The system-message text appended on a ``run``-protocol failure.
    program_failure_nudge:
        The suffix appended to the program-failure system message (after the
        serialised errors and a space).
    action_trace_nudge:
        The system-message suffix appended when the program ran syscalls but
        emitted no terminal output.
    emit_value_nudge:
        The system-message text appended when the program emitted internal
        values via ``agent.emit_value``.
    fallback_nudge:
        The system-message text appended when the program completed but
        produced no visible output and no other branch matched.
    void_failed_program_approvals:
        When True, any approval proposals staged by a program that did not
        complete cleanly are voided before the per-program commit (the
        ``_wake`` behaviour).  When False (``run_research``), approvals are
        impossible since the research capability whitelist contains only
        ``impact_level="read"`` capabilities.
    """

    output_mode: Literal["message", "finding", "operations"]
    finding_mode: str
    prompt_version: str
    budget_seconds: float
    max_model_calls: int
    is_research_run: bool
    record_judgments: bool
    judgment_type: (
        Literal["memory_recall", "memory_encode", "memory_dream", "model_output", "research"] | None
    )
    retry_on_model_error: bool
    void_failed_program_approvals: bool
    protocol_nudge: str
    program_failure_nudge: str
    action_trace_nudge: str
    emit_value_nudge: str
    fallback_nudge: str


@dataclass(slots=True, frozen=True)
class LoopResult:
    """The outcome of one ``run_agent_loop`` call.

    ``outcome`` is one of:

    - ``"message"`` — the program emitted a user-visible message.
    - ``"finding"`` — the program emitted a research finding.
    - ``"operations"`` — the program called ``agent.emit_done``; ``emitted_operations``
      is the summary string.
    - ``"approval"`` — the program staged an approval proposal without
      emitting a message; the awaiting action attempt is in
      ``awaiting_approval``.
    - ``"paused"`` — the program called ``agent.pause_until_input``.
    - ``"budget_exhausted"`` — the wall-clock budget, the model-call backstop,
      or stuck-detection ended the run without a terminal result.
    - ``"model_failed"`` — a model call raised an unretryable exception (or
      ``retry_on_model_error`` is False).
    - ``"bounded_failure"`` — reserved for callers that enforce per-response
      token limits; not set by the loop itself.
    """

    outcome: Literal[
        "message",
        "finding",
        "operations",
        "approval",
        "paused",
        "budget_exhausted",
        "model_failed",
        "bounded_failure",
    ]
    emitted_message: str | None
    emitted_finding: ResearchFinding | None
    emitted_operations: str | None
    model_call_count: int
    created_action_attempt_count: int
    awaiting_approval: ActionAttemptRecord | None
    bounded_failure_details: dict[str, Any] | None
    runtime_provenance: RuntimeProvenance | None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_agent_loop(
    cfg: LoopConfig,
    *,
    sandbox: RunSandbox,
    db: Session,
    session_factory: sessionmaker[Session],
    session_id: str,
    turn: TurnRecord,
    settings: AppSettings,
    model_adapter: ModelAdapter,
    messages: list[ModelMessage],
    tools: list[ToolSpec],
    allowed_capability_ids: frozenset[str],
    scratch: dict[str, ScratchEntry],
    proposal_index_start: int,
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    runtime_provenance: RuntimeProvenance | None,
    google_runtime: GoogleConnectorRuntime | None,
    execute_google_reads_outside_transaction: bool,
    agency_runtime: AgencyRuntime | None,
    attachment_runtime: AttachmentContentRuntime | None,
) -> LoopResult:
    """Drive the shared agent loop and return a typed result.

    The loop runs until one of the terminal outcomes occurs.  The caller is
    responsible for all pre-loop and post-loop work: building the initial
    ``messages`` list (system + user), the ``turn`` record, ``scratch``, and
    mapping the returned ``LoopResult`` to a domain-level outcome.

    ``messages`` is the caller-owned conversation history as pydantic-ai
    ``ModelMessage``\\ s; the loop appends one ``ModelResponse`` (the model's
    reply) and one ``ModelRequest`` (tool returns + any system nudge) per
    round. The model adapter (``ModelAdapter.call``) is async; we bridge with
    ``asyncio.run`` at the call sites because the rest of the agent path —
    DB session, FastAPI handlers, worker — is sync.
    """

    loop_started_at = time.perf_counter()
    budget_item_index: int | None = None

    # The stable prefix length — messages present before the loop started
    # (system prompts, user message, etc.).  Round eviction never touches these.
    stable_prefix_len = len(messages)

    # Tracks (start_index, end_index) of each round's appended messages so the
    # oldest round can be evicted when the live window overflows.
    round_spans: list[tuple[int, int]] = []

    # Tail index of the messages appended by the most recent emit_value round.
    # ``None`` before the first emit_value round.
    last_emit_value_tail: int | None = None

    prev_run_source: str | None = None
    last_program_errors: list[str] | None = None
    model_call_count = 0
    # Start past any earlier loop's attempts on this turn (e.g. retriever
    # before main loop) — the (turn_id, proposal_index) unique index is the rail.
    existing_attempt_count = int(
        db.scalar(
            select(func.count())
            .select_from(ActionAttemptRecord)
            .where(ActionAttemptRecord.turn_id == turn.id)
        )
        or 0
    )
    created_action_attempt_count = max(proposal_index_start, existing_attempt_count)
    final_runtime_provenance = runtime_provenance

    while True:
        # --- Budget and backstop checks ---
        elapsed_s = time.perf_counter() - loop_started_at
        if elapsed_s > cfg.budget_seconds or model_call_count > cfg.max_model_calls:
            return _budget_exhausted_result(
                cfg=cfg,
                model_adapter=model_adapter,
                messages=messages,
                add_event=add_event,
                model_call_count=model_call_count,
                created_action_attempt_count=created_action_attempt_count,
                final_runtime_provenance=final_runtime_provenance,
            )

        # Remaining-budget signal: replace the previous round's message in-place.
        remaining_s = max(0.0, cfg.budget_seconds - elapsed_s)
        budget_line = _system_message(f"remaining budget: {remaining_s:.0f}s")
        if budget_item_index is None:
            messages.append(budget_line)
            budget_item_index = len(messages) - 1
        else:
            messages[budget_item_index] = budget_line

        binding = model_adapter.tier_binding(ModelTier.MAIN)
        model_call_count += 1
        add_event(
            "evt.model.started",
            {
                "provider": binding.provider,
                "model": binding.model,
                "model_call_count": model_call_count,
            },
        )
        model_started_at = time.perf_counter()
        try:
            # asyncio.run is fine to call twice per loop invocation: each call
            # spins up a fresh event loop, runs to completion, and tears down.
            # The surrounding code (FastAPI handlers, worker, DB session) is
            # sync; bridging here is the smallest change.
            candidate_response = asyncio.run(
                model_adapter.call(
                    ModelCall(
                        tier=ModelTier.MAIN,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        reasoning=ReasoningConfig(effort=settings.model_reasoning_effort),
                        max_output_tokens=settings.max_response_tokens,
                    )
                )
            )
            add_event(
                "evt.model.completed",
                {
                    "provider": candidate_response.provider,
                    "model": candidate_response.model,
                    "duration_ms": candidate_response.duration_ms,
                    "usage": _usage_payload(candidate_response),
                    "provider_response_id": candidate_response.provider_response_id,
                    "model_call_count": model_call_count,
                },
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - model_started_at) * 1000)
            should_retry = cfg.retry_on_model_error and bool(getattr(exc, "retryable", False))
            add_event(
                "evt.model.failed",
                {
                    "provider": binding.provider,
                    "model": binding.model,
                    "duration_ms": duration_ms,
                    "failure_reason": getattr(exc, "safe_reason", str(exc)),
                    "model_call_count": model_call_count,
                },
            )
            if should_retry:
                continue
            return LoopResult(
                outcome="model_failed",
                emitted_message=None,
                emitted_finding=None,
                emitted_operations=None,
                model_call_count=model_call_count,
                created_action_attempt_count=created_action_attempt_count,
                awaiting_approval=None,
                bounded_failure_details=None,
                runtime_provenance=final_runtime_provenance,
            )

        tool_calls = candidate_response.tool_calls
        run_source, run_protocol_error = parse_run_function_call(tool_calls)

        if run_protocol_error is not None or run_source is None:
            # Protocol failure: the model did not emit a valid run call.
            if cfg.record_judgments:
                _record_judgment(
                    db=db,
                    cfg=cfg,
                    response=candidate_response,
                    session_id=session_id,
                    turn=turn,
                    model_call_count=model_call_count,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                    input_summary="run protocol validation for model response",
                    input_refs={
                        "tool_calls": [
                            {"name": t.name, "arguments": t.arguments} for t in tool_calls
                        ],
                    },
                    output={},
                    failure_reason=run_protocol_error or "model failed the run tool protocol",
                )
            round_start = len(messages)
            failure_payload = json.dumps(
                {
                    "status": "failed",
                    "error": run_protocol_error or "model failed the run tool protocol",
                },
                sort_keys=True,
            )
            messages.append(_assistant_tool_call_message(tool_calls))
            messages.append(
                _tool_returns_with_nudge(
                    returns=[(t.call_id, failure_payload) for t in tool_calls if t.call_id],
                    nudge=cfg.protocol_nudge,
                )
            )
            add_event(
                "evt.model.protocol_failed",
                {
                    "reason": run_protocol_error,
                    "model_call_count": model_call_count,
                    "provider_response_id": candidate_response.provider_response_id,
                },
            )
            _evict_oldest_round(
                messages,
                round_spans,
                round_start,
                stable_prefix_len,
                budget_item_index,
                settings.agent_loop_live_rounds,
            )
            continue

        # Stuck-detection: identical source in consecutive rounds → budget_exhausted.
        if run_source == prev_run_source:
            return _budget_exhausted_result(
                cfg=cfg,
                model_adapter=model_adapter,
                messages=messages,
                add_event=add_event,
                model_call_count=model_call_count,
                created_action_attempt_count=created_action_attempt_count,
                final_runtime_provenance=final_runtime_provenance,
            )
        prev_run_source = run_source

        run_call_id = tool_calls[0].call_id

        run_program_result = execute_run_program(
            sandbox=sandbox,
            source=run_source,
            db=db,
            session_factory=session_factory,
            session_id=session_id,
            turn=turn,
            proposal_index_start=created_action_attempt_count,
            approval_ttl_seconds=approval_ttl_seconds,
            approval_actor_id=approval_actor_id,
            add_event=add_event,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
            runtime_provenance=final_runtime_provenance,
            google_runtime=google_runtime,
            execute_google_reads_outside_transaction=execute_google_reads_outside_transaction,
            agency_runtime=agency_runtime,
            attachment_runtime=attachment_runtime,
            allowed_capability_ids=set(allowed_capability_ids),
            settings=settings,
            scratch=scratch,
            model_adapter=model_adapter,
            is_research_run=cfg.is_research_run,
        )
        created_action_attempt_count += len(run_program_result.action_attempts)
        # Thread taint across programs in the same turn.
        if run_program_result.runtime_provenance is not None:
            final_runtime_provenance = _merge_provenance(
                baseline=final_runtime_provenance,
                ingress=run_program_result.runtime_provenance,
            )

        if not run_program_result.program_ok:
            # Program failure.
            program_errors = [
                e for e in [run_program_result.program_error] if e is not None
            ] + run_program_result.callback_errors
            # Stuck-detection: identical program_errors in consecutive rounds.
            if program_errors and program_errors == last_program_errors:
                return _budget_exhausted_result(
                    cfg=cfg,
                    model_adapter=model_adapter,
                    messages=messages,
                    add_event=add_event,
                    model_call_count=model_call_count,
                    created_action_attempt_count=created_action_attempt_count,
                    final_runtime_provenance=final_runtime_provenance,
                )
            last_program_errors = program_errors
            if cfg.record_judgments:
                _record_judgment(
                    db=db,
                    cfg=cfg,
                    response=candidate_response,
                    session_id=session_id,
                    turn=turn,
                    model_call_count=model_call_count,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                    input_summary="run program execution for model response",
                    input_refs={"source": run_source},
                    output={},
                    failure_reason=json.dumps(program_errors, sort_keys=True),
                )
            round_start = len(messages)
            nudge = cfg.program_failure_nudge
            for err in program_errors:
                if "agent.emit_finding is only available inside a research run" in err:
                    nudge = (
                        "agent.emit_finding is not available in this loop. "
                        "Continue with one run call that ends by calling "
                        "agent.emit_message(text=...) to reply to the user."
                    )
                    break
                if "agent.emit_done is only available inside memory subsystem runs" in err:
                    nudge = (
                        "agent.emit_done is not available in this loop. "
                        "Continue with one run call that ends by calling "
                        "agent.emit_message(text=...) to reply to the user."
                    )
                    break
            failure_output = json.dumps(
                {"status": "failed", "errors": program_errors}, sort_keys=True
            )
            nudge_text = (
                "run program did not complete: "
                + json.dumps(program_errors, sort_keys=True)
                + ". "
                + nudge
            )
            messages.append(_assistant_tool_call_message(tool_calls))
            messages.append(
                _tool_returns_with_nudge(
                    returns=[(run_call_id, failure_output)] if run_call_id else [],
                    nudge=nudge_text,
                )
            )
            add_event(
                "evt.run.validation_failed",
                {
                    "errors": program_errors,
                    "model_call_count": model_call_count,
                    "provider_response_id": candidate_response.provider_response_id,
                },
            )
            if cfg.void_failed_program_approvals:
                _void_approvals(
                    db=db,
                    action_attempts=run_program_result.action_attempts,
                    add_event=add_event,
                    now_fn=now_fn,
                )
            db.commit()
            _evict_oldest_round(
                messages,
                round_spans,
                round_start,
                stable_prefix_len,
                budget_item_index,
                settings.agent_loop_live_rounds,
            )
            continue

        # Clean program.
        last_program_errors = None
        if cfg.record_judgments:
            _record_judgment(
                db=db,
                cfg=cfg,
                response=candidate_response,
                session_id=session_id,
                turn=turn,
                model_call_count=model_call_count,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
                input_summary="executed run program for model response",
                input_refs={
                    "source": run_source,
                    "tool_calls": [
                        {"name": t.name, "arguments": t.arguments} for t in tool_calls
                    ],
                },
                output={
                    "emitted_message": bool(run_program_result.emitted_message),
                    "paused": run_program_result.paused,
                    "emitted_value_count": len(run_program_result.emitted_values),
                    "action_attempt_count": len(run_program_result.action_attempts),
                },
                failure_reason=None,
            )

        db.commit()

        # Append an agent_round event to the raw memory log for main-agent
        # turns (not research/retriever runs).  This is the within-turn
        # round-eviction complement: evicted rounds live in the log and can
        # be recalled by memory.recall if a later round needs them.
        # Imported lazily to avoid the agent_loop ↔ memory circular import.
        if not cfg.is_research_run:
            from .memory import append_log_event  # noqa: PLC0415

            action_summary = [
                {"capability_id": a.capability_id, "status": a.status}
                for a in run_program_result.action_attempts
            ]
            round_content = json.dumps(
                {
                    "source": run_source,
                    "action_attempts": action_summary,
                    "emitted_message": bool(run_program_result.emitted_message),
                    "emitted_values": len(run_program_result.emitted_values),
                    "paused": run_program_result.paused,
                },
                sort_keys=True,
            )
            taint: Literal["clean", "tainted"] = (
                "tainted"
                if (
                    final_runtime_provenance is not None
                    and final_runtime_provenance.status == "tainted"
                )
                else "clean"
            )
            append_log_event(
                db,
                kind="agent_round",
                content=round_content,
                session_id=session_id,
                turn_id=turn.id,
                taint=taint,
                source_ref=turn.id,
                adapter=model_adapter,
                settings=settings,
                now=now_fn(),
                new_id_fn=new_id_fn,
            )
            db.commit()

        # --- Terminal branches (exhaustive over output_mode) ---

        match cfg.output_mode:
            case "finding":
                if run_program_result.emitted_finding is not None:
                    raw = run_program_result.emitted_finding
                    return LoopResult(
                        outcome="finding",
                        emitted_message=None,
                        emitted_finding=ResearchFinding(
                            # ``question`` is the caller's: it knows the
                            # question it dispatched. The loop carries the
                            # model-authored body only.
                            question="",
                            mode=cfg.finding_mode,
                            status="complete",
                            summary=str(raw["summary"]),
                            claims=list(raw["claims"]),
                            gaps=list(raw["gaps"]),
                            sources=list(raw["sources"]),
                        ),
                        emitted_operations=None,
                        model_call_count=model_call_count,
                        created_action_attempt_count=created_action_attempt_count,
                        awaiting_approval=None,
                        bounded_failure_details=None,
                        runtime_provenance=final_runtime_provenance,
                    )
            case "operations":
                if run_program_result.emitted_done is not None:
                    return LoopResult(
                        outcome="operations",
                        emitted_message=None,
                        emitted_finding=None,
                        emitted_operations=run_program_result.emitted_done,
                        model_call_count=model_call_count,
                        created_action_attempt_count=created_action_attempt_count,
                        awaiting_approval=None,
                        bounded_failure_details=None,
                        runtime_provenance=final_runtime_provenance,
                    )
            case "message":
                if run_program_result.emitted_message:
                    return LoopResult(
                        outcome="message",
                        emitted_message=run_program_result.emitted_message,
                        emitted_finding=None,
                        emitted_operations=None,
                        model_call_count=model_call_count,
                        created_action_attempt_count=created_action_attempt_count,
                        awaiting_approval=None,
                        bounded_failure_details=None,
                        runtime_provenance=final_runtime_provenance,
                    )

                awaiting = next(
                    (
                        a
                        for a in run_program_result.action_attempts
                        if a.status == "awaiting_approval"
                    ),
                    None,
                )
                if awaiting is not None:
                    return LoopResult(
                        outcome="approval",
                        emitted_message=None,
                        emitted_finding=None,
                        emitted_operations=None,
                        model_call_count=model_call_count,
                        created_action_attempt_count=created_action_attempt_count,
                        awaiting_approval=awaiting,
                        bounded_failure_details=None,
                        runtime_provenance=final_runtime_provenance,
                    )

                if run_program_result.paused:
                    return LoopResult(
                        outcome="paused",
                        emitted_message=None,
                        emitted_finding=None,
                        emitted_operations=None,
                        model_call_count=model_call_count,
                        created_action_attempt_count=created_action_attempt_count,
                        awaiting_approval=None,
                        bounded_failure_details=None,
                        runtime_provenance=final_runtime_provenance,
                    )
            case _:
                assert_never(cfg.output_mode)

        # --- Non-terminal branches (continue the loop) ---

        round_start = len(messages)

        if run_program_result.emitted_values:
            # Surface one evt.agent.value_emitted event per value (digest only,
            # not the value itself — values stay internal feedback).
            for index, value in enumerate(run_program_result.emitted_values):
                encoded = json.dumps(value, sort_keys=True).encode()
                add_event(
                    "evt.agent.value_emitted",
                    {
                        "index": index,
                        "value_digest": hashlib.sha256(encoded).hexdigest(),
                        "value_bytes": len(encoded),
                        "model_call_count": model_call_count,
                        "provider_response_id": candidate_response.provider_response_id,
                    },
                )
            # Emit_value round: evict the previous emit_value round's messages
            # so only the most recent round's values are in context.
            if last_emit_value_tail is not None:
                del messages[last_emit_value_tail:]
                round_start = len(messages)
                round_spans = [(s, e) for s, e in round_spans if e <= last_emit_value_tail]
            last_emit_value_tail = round_start
            emitted_values_output = json.dumps(
                {
                    "status": "completed",
                    "emitted_values": jsonable_encoder(run_program_result.emitted_values),
                },
                sort_keys=True,
            )
            messages.append(_assistant_tool_call_message(tool_calls))
            messages.append(
                _tool_returns_with_nudge(
                    returns=[(run_call_id, emitted_values_output)] if run_call_id else [],
                    nudge=cfg.emit_value_nudge,
                )
            )
            _evict_oldest_round(
                messages,
                round_spans,
                round_start,
                stable_prefix_len,
                budget_item_index,
                settings.agent_loop_live_rounds,
            )
            continue

        if run_program_result.action_attempts:
            # Syscall trace: feed back so the model can author the next program.
            action_attempt_summary = [
                {
                    "action_attempt_id": a.id,
                    "capability_id": a.capability_id,
                    "status": a.status,
                    "policy_decision": a.policy_decision,
                    "approval_required": a.approval_required,
                }
                for a in run_program_result.action_attempts
            ]
            trace_output = json.dumps(
                {
                    "status": "completed",
                    "message_emitted": False,
                    "action_attempts": action_attempt_summary,
                },
                sort_keys=True,
            )
            trace_nudge = (
                "run program syscall trace:\n"
                + json.dumps(action_attempt_summary, sort_keys=True)
                + "\n"
                + cfg.action_trace_nudge
            )
            messages.append(_assistant_tool_call_message(tool_calls))
            messages.append(
                _tool_returns_with_nudge(
                    returns=[(run_call_id, trace_output)] if run_call_id else [],
                    nudge=trace_nudge,
                )
            )
            _evict_oldest_round(
                messages,
                round_spans,
                round_start,
                stable_prefix_len,
                budget_item_index,
                settings.agent_loop_live_rounds,
            )
            continue

        # Fallback: the program ran but produced no terminal output.
        messages.append(_system_message(cfg.fallback_nudge))
        add_event(
            "evt.model.protocol_failed",
            {
                "reason": "run_completed_without_visible_output",
                "model_call_count": model_call_count,
                "provider_response_id": candidate_response.provider_response_id,
            },
        )
        _evict_oldest_round(
            messages,
            round_spans,
            round_start,
            stable_prefix_len,
            budget_item_index,
            settings.agent_loop_live_rounds,
        )
        continue


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_BUDGET_EXHAUSTED_SUMMARY_INSTRUCTION = (
    "Your time has run out. In one short paragraph of plain text, tell the "
    "user what you did and what remains. Do not call any tools — return the "
    "text directly."
)


def _system_message(content: str) -> ModelMessage:
    """A ``ModelRequest`` carrying one system instruction part."""
    return ModelRequest(parts=[SystemPromptPart(content=content)])


def _assistant_tool_call_message(tool_calls: list[ToolCall]) -> ModelMessage:
    """The assistant turn's ``ModelResponse`` for a set of tool calls.

    Pydantic AI's request/response graph expects an assistant ``ModelResponse``
    holding the model's ``ToolCallPart``\\ s before the next ``ModelRequest``
    can carry their ``ToolReturnPart``\\ s; we synthesise that response from
    the typed ``ToolCall`` list our adapter returned.
    """
    return PydAIModelResponse(
        parts=[
            ToolCallPart(tool_name=tc.name, args=tc.arguments, tool_call_id=tc.call_id)
            for tc in tool_calls
        ]
    )


def _tool_returns_with_nudge(
    *,
    returns: list[tuple[str, str]],
    nudge: str | None,
) -> ModelMessage:
    """Build the next-round ``ModelRequest`` carrying tool returns and a nudge.

    ``returns`` is a list of ``(tool_call_id, json_output_string)`` pairs;
    ``nudge`` is an optional system instruction appended after the returns.
    """
    parts: list[Any] = [
        ToolReturnPart(tool_name="run", content=output, tool_call_id=call_id)
        for call_id, output in returns
    ]
    if nudge is not None:
        parts.append(SystemPromptPart(content=nudge))
    return ModelRequest(parts=parts)


def _usage_payload(response: ModelResponse) -> dict[str, int]:
    """Render ``ModelResponse.usage`` into the ``SurfaceModelUsageContract`` shape."""
    usage = response.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cached_tokens": usage.cached_tokens,
    }


def _budget_exhausted_result(
    *,
    cfg: LoopConfig,
    model_adapter: ModelAdapter,
    messages: list[ModelMessage],
    add_event: Callable[[str, dict[str, Any]], None],
    model_call_count: int,
    created_action_attempt_count: int,
    final_runtime_provenance: RuntimeProvenance | None,
) -> LoopResult:
    """Build the budget-exhausted result, attempting a one-shot summary call
    on the main loop (``output_mode == "message"``).

    On main-loop budget exhaustion, makes one constrained model call with
    ``tools=[]`` and a tight system instruction asking the model to summarise
    what it did and what remains. On usable text, returns an ``outcome="message"``
    result so the caller's existing assistant-message path handles it. On any
    failure, empty text, or non-message output mode, returns the unchanged
    ``outcome="budget_exhausted"`` result.
    """
    if cfg.output_mode != "message":
        return LoopResult(
            outcome="budget_exhausted",
            emitted_message=None,
            emitted_finding=None,
            emitted_operations=None,
            model_call_count=model_call_count,
            created_action_attempt_count=created_action_attempt_count,
            awaiting_approval=None,
            bounded_failure_details=None,
            runtime_provenance=final_runtime_provenance,
        )

    summary_messages = [
        *messages,
        _system_message(_BUDGET_EXHAUSTED_SUMMARY_INSTRUCTION),
    ]
    binding = model_adapter.tier_binding(ModelTier.MAIN)
    model_call_count += 1
    add_event(
        "evt.model.started",
        {
            "provider": binding.provider,
            "model": binding.model,
            "model_call_count": model_call_count,
        },
    )
    started_at = time.perf_counter()
    try:
        summary_response = asyncio.run(
            model_adapter.call(
                ModelCall(
                    tier=ModelTier.MAIN,
                    messages=summary_messages,
                    tools=[],
                    tool_choice="auto",
                    reasoning=ReasoningConfig(),
                )
            )
        )
    except Exception as exc:
        add_event(
            "evt.model.failed",
            {
                "provider": binding.provider,
                "model": binding.model,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "failure_reason": getattr(exc, "safe_reason", str(exc)),
                "model_call_count": model_call_count,
            },
        )
        return LoopResult(
            outcome="budget_exhausted",
            emitted_message=None,
            emitted_finding=None,
            emitted_operations=None,
            model_call_count=model_call_count,
            created_action_attempt_count=created_action_attempt_count,
            awaiting_approval=None,
            bounded_failure_details=None,
            runtime_provenance=final_runtime_provenance,
        )
    add_event(
        "evt.model.completed",
        {
            "provider": summary_response.provider,
            "model": summary_response.model,
            "duration_ms": summary_response.duration_ms,
            "usage": _usage_payload(summary_response),
            "provider_response_id": summary_response.provider_response_id,
            "model_call_count": model_call_count,
        },
    )

    summary_text = (summary_response.text or "").strip()
    if summary_response.tool_calls or not summary_text:
        return LoopResult(
            outcome="budget_exhausted",
            emitted_message=None,
            emitted_finding=None,
            emitted_operations=None,
            model_call_count=model_call_count,
            created_action_attempt_count=created_action_attempt_count,
            awaiting_approval=None,
            bounded_failure_details=None,
            runtime_provenance=final_runtime_provenance,
        )

    return LoopResult(
        outcome="message",
        emitted_message=summary_text,
        emitted_finding=None,
        emitted_operations=None,
        model_call_count=model_call_count,
        created_action_attempt_count=created_action_attempt_count,
        awaiting_approval=None,
        bounded_failure_details=None,
        runtime_provenance=final_runtime_provenance,
    )


def _merge_provenance(
    *,
    baseline: RuntimeProvenance | None,
    ingress: RuntimeProvenance | None,
) -> RuntimeProvenance | None:
    if baseline is None:
        return ingress
    if ingress is None:
        return baseline
    merged_status: Literal["clean", "tainted"] = (
        "tainted" if baseline.status == "tainted" or ingress.status == "tainted" else "clean"
    )
    return RuntimeProvenance(
        status=merged_status,
        evidence=tuple([*baseline.evidence, *ingress.evidence]),
    )


def _record_judgment(
    *,
    db: Session,
    cfg: LoopConfig,
    response: ModelResponse,
    session_id: str,
    turn: TurnRecord,
    model_call_count: int,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    input_summary: str,
    input_refs: dict[str, Any],
    output: dict[str, Any],
    failure_reason: str | None,
) -> None:
    record_ai_judgment(
        db,
        judgment_type=cfg.judgment_type or "model_output",
        source_type="turn",
        source_id=turn.id,
        model=response.model,
        prompt_version=cfg.prompt_version,
        provider_response_id=response.provider_response_id,
        input_summary=input_summary,
        input_refs={
            "session_id": session_id,
            "turn_id": turn.id,
            "model_call_count": model_call_count,
            **input_refs,
        },
        output=output,
        now=now_fn(),
        new_id=new_id_fn,
        failure=AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason=failure_reason,
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )
        if failure_reason is not None
        else None,
    )


def _evict_oldest_round(
    messages: list[ModelMessage],
    round_spans: list[tuple[int, int]],
    round_start: int,
    stable_prefix_len: int,
    budget_item_index: int | None,
    live_rounds: int,
) -> None:
    """Record the current round's span and evict the oldest if the window overflows.

    The stable prefix (system prompts, user message) and the budget-signal
    slot are never evicted.  Each round's messages are tracked as a ``(start,
    end)`` slice of ``messages``.  When tracked rounds exceed ``live_rounds``,
    the oldest is spliced out and all subsequent spans are adjusted.
    """
    round_end = len(messages)
    if round_start >= round_end:
        return
    round_spans.append((round_start, round_end))
    if len(round_spans) <= live_rounds:
        return

    old_start, old_end = round_spans.pop(0)
    # Safety: never evict messages from the stable prefix or the budget slot.
    if old_start < stable_prefix_len:
        return
    if budget_item_index is not None and old_start <= budget_item_index < old_end:
        return

    evict_count = old_end - old_start
    del messages[old_start:old_end]
    for i, (s, e) in enumerate(round_spans):
        round_spans[i] = (s - evict_count, e - evict_count)


def _void_approvals(
    *,
    db: Session,
    action_attempts: list[ActionAttemptRecord],
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
) -> None:
    """Void approval proposals staged by a program that did not complete cleanly.

    Per the cutover's "Program Failure", a program that fails commits no
    proposals.  Any approval the failed program staged must never surface as a
    live pending action: the approval and its action attempt move to
    ``"expired"`` so nothing is left ``"pending"`` for the user to act on.
    The syscall trace (the ``action_attempts`` rows) is preserved as the audit
    record.
    """
    now = now_fn()
    for action_attempt in action_attempts:
        if action_attempt.status != "awaiting_approval":
            continue
        approval = action_attempt.approval_request
        if approval is not None and approval.status == "pending":
            approval.status = "expired"
            approval.decision_reason = "program_failed"
            approval.decided_at = now
            approval.updated_at = now
        action_attempt.status = "expired"
        action_attempt.policy_reason = "program_failed"
        action_attempt.updated_at = now
        add_event(
            "evt.action.approval.expired",
            {
                "action_attempt_id": action_attempt.id,
                "approval_ref": approval.id if approval is not None else None,
                "reason": "program_failed",
            },
        )
    db.flush()
