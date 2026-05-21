from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, SystemPromptPart, ToolReturnPart, UserPromptPart
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import (
    RuntimeProvenance,
    _FunctionCallProcessingContext,
    process_action_execution_task,
    process_one_call,
)
from ariel.app import _new_id, _utcnow
from ariel.config import MEMORY_EMBEDDING_DIMENSIONS
from ariel.google_connector import GoogleConnectorRuntime
from ariel.model_adapter import (
    ModelAdapter,
    ModelCall,
    ModelMessage,
    ModelResponse,
    ModelTier,
    ToolCall,
    TokenUsage,
)
from ariel.model_tiers import TierBinding
from ariel.persistence import BackgroundTaskRecord, TurnRecord
from ariel.worker import process_one_task


def post_message_and_drain(
    client: TestClient,
    session_id: str,
    *,
    message: str,
    headers: dict[str, str] | None = None,
    json_extra: dict[str, Any] | None = None,
) -> TurnRecord:
    """POST a user message, assert 202, drain the enqueued task via the worker,
    and return the completed TurnRecord.

    Use this in every test that sends a user message: POST → assert 202 →
    drain until the specific task_id is gone → return TurnRecord. The caller
    reads turn outcome and events from the TurnRecord or from GET
    /v1/sessions/{id}/events.

    Loops process_one_task until the specific task is consumed, so maintenance
    tasks processed ahead of the user_message task do not cause a stale read.
    Queries TurnRecord without filtering by session_id so rotation tests work
    correctly (the new session's turn is still found).
    """
    posted_at = _utcnow()
    body: dict[str, Any] = {"message": message}
    if json_extra:
        body.update(json_extra)
    resp = client.post(
        f"/v1/sessions/{session_id}/message",
        json=body,
        headers=headers or {},
    )
    assert resp.status_code == 202, f"expected 202, got {resp.status_code}: {resp.text}"
    task_id = resp.json()["task_id"]

    app_state = cast(Any, client.app).state
    runtime = app_state.runtime

    for _ in range(20):
        process_one_task(
            session_factory=runtime.session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )
        with runtime.session_factory() as db:
            still_pending = db.get(BackgroundTaskRecord, task_id)
        if still_pending is None:
            break
    else:
        raise AssertionError(f"task {task_id} was not consumed after 20 process_one_task calls")

    with runtime.session_factory() as db:
        turn = db.scalar(
            select(TurnRecord)
            .where(TurnRecord.created_at >= posted_at)
            .where(TurnRecord.user_message == message)
            .order_by(TurnRecord.created_at.desc())
            .limit(1)
        )
    assert turn is not None, (
        f"no TurnRecord found for message {message!r} after draining task {task_id}"
    )
    return turn


def drain_task(client: TestClient, task_id: str) -> None:
    """Drive the worker until the given task_id is consumed.

    Use this when you already have a task_id from a 202 response and want to
    drain it without posting a message again. Loops process_one_task up to 20
    times until the task row is gone from the DB.
    """
    app_state = cast(Any, client.app).state
    runtime = app_state.runtime

    for _ in range(20):
        process_one_task(
            session_factory=runtime.session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )
        with runtime.session_factory() as db:
            still_pending = db.get(BackgroundTaskRecord, task_id)
        if still_pending is None:
            return
    raise AssertionError(f"task {task_id} was not consumed after 20 process_one_task calls")


def run_function_calls(
    *,
    db: Session,
    session_id: str,
    turn: TurnRecord,
    function_calls_raw: list[dict[str, Any]],
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    allowed_capability_ids: list[str],
    session_factory: sessionmaker[Session] | None = None,
    runtime_provenance: RuntimeProvenance | None = None,
    google_runtime: GoogleConnectorRuntime | None = None,
    execute_google_reads_outside_transaction: bool = False,
    agency_runtime: Any | None = None,
    attachment_runtime: Any | None = None,
    settings: Any | None = None,
) -> _FunctionCallProcessingContext:
    """Drive a list of capability calls through ``process_one_call``.

    The run-program host path dispatches each program syscall through
    ``process_one_call``; this helper applies the same per-call lifecycle to a
    plain call list so action-runtime tests can assert capability behavior
    without authoring a sandbox program. It returns the shared context whose
    ``created_action_attempts`` and ``function_call_outputs`` carry the results.
    """

    ctx = _FunctionCallProcessingContext()
    allowed = set(allowed_capability_ids)
    for index, function_call_raw in enumerate(function_calls_raw, start=1):
        process_one_call(
            ctx=ctx,
            function_call_index=index,
            function_call_raw=function_call_raw,
            db=db,
            session_factory=session_factory,
            session_id=session_id,
            turn=turn,
            approval_ttl_seconds=approval_ttl_seconds,
            approval_actor_id=approval_actor_id,
            add_event=add_event,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
            runtime_provenance=runtime_provenance,
            google_runtime=google_runtime,
            execute_google_reads_outside_transaction=execute_google_reads_outside_transaction,
            agency_runtime=agency_runtime,
            attachment_runtime=attachment_runtime,
            allowed_capability_id_set=allowed,
            settings=settings,
        )
    return ctx


def _build_response(
    *,
    text: str | None,
    tool_calls: list[ToolCall],
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        structured_output=None,
        reasoning_summary=None,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        provider=provider,
        model=model,
        tier=ModelTier.MAIN,
        duration_ms=1,
        provider_response_id=provider_response_id,
    )


def responses_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> ModelResponse:
    """A canned ``ModelResponse`` carrying assistant text (no tool calls).

    The agent loop ignores plain text in the main path — only the
    budget-exhausted summary call returns this shape — but tests use it to
    drive specific assistant-text scenarios.
    """
    return _build_response(
        text=assistant_text,
        tool_calls=[],
        provider=provider,
        model=model,
        provider_response_id=provider_response_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def responses_run_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> ModelResponse:
    return responses_with_run_calls(
        assistant_text=assistant_text,
        calls=[{"name": "agent.emit_message", "input": {"text": assistant_text}}],
        provider=provider,
        model=model,
        provider_response_id=provider_response_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def run_program_source_from_calls(calls: list[dict[str, Any]]) -> str:
    """Translate a flat ``[{"name", "input"}, ...]`` call list into a run program.

    Each call becomes one ``namespace.member(**kwargs)`` statement, in order. This
    adapts the turn-test suite onto the Python-program ``run`` source at one
    point: tests still describe the calls they expect, and this renders the
    equivalent linear program.
    """

    statements: list[str] = []
    for call in calls:
        name = call["name"]
        call_input = call.get("input") or {}
        if not isinstance(call_input, dict):
            raise AssertionError(f"run call {name!r} input must be an object")
        kwargs = ", ".join(f"{key}={value!r}" for key, value in call_input.items())
        statements.append(f"{name}({kwargs})")
    return "\n".join(statements) + "\n"


def responses_with_run_calls(
    *,
    assistant_text: str,
    calls: list[dict[str, Any]],
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> ModelResponse:
    del assistant_text
    if not calls:
        raise AssertionError("responses_with_run_calls requires at least one run call")
    return _build_response(
        text=None,
        tool_calls=[
            ToolCall(
                call_id="call_run_test",
                name="run",
                arguments={"source": run_program_source_from_calls(calls)},
            )
        ],
        provider=provider,
        model=model,
        provider_response_id=provider_response_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def run_response(
    *,
    source: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> ModelResponse:
    """A canned ``ModelResponse`` carrying one ``run(source=...)`` tool call."""
    return _build_response(
        text=None,
        tool_calls=[
            ToolCall(
                call_id=f"call_{provider_response_id}",
                name="run",
                arguments={"source": source},
            )
        ],
        provider=provider,
        model=model,
        provider_response_id=provider_response_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def last_user_message(messages: list[ModelMessage]) -> str:
    """Return the most recent user-prompt text from ``messages``.

    Test fakes use this to recover the user message string the agent loop
    derived from the initial ``ModelRequest`` so they can echo or branch on
    it without unpacking the message graph by hand.
    """
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return part.content
    return ""


def has_tool_returns(messages: list[ModelMessage]) -> bool:
    """True iff ``messages`` carries any ``ToolReturnPart`` (a prior round's
    tool returns).

    Test fakes that respond with a syscall on round 1 use this to detect the
    next round and switch to an ``agent.emit_message`` finalisation — the
    equivalent of the legacy ``input_items`` ``function_call_output`` check
    against the pre-cutover dict-shaped input.
    """
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        if any(isinstance(part, ToolReturnPart) for part in message.parts):
            return True
    return False


def _detect_memory_subsystem(messages: list[ModelMessage]) -> str | None:
    """Inspect the stable-prefix system prompts of ``messages`` and return the
    memory-subsystem configuration (``retriever`` | ``encoder`` | ``dreamer``)
    or ``None`` when none is detected."""
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, SystemPromptPart):
                continue
            content = part.content
            if "Ariel's memory retriever" in content:
                return "retriever"
            if "Ariel's memory encoder" in content:
                return "encoder"
            if "Ariel's memory dreamer" in content:
                return "dreamer"
    return None


def is_retriever_call(messages: list[ModelMessage]) -> bool:
    """Detects a memory subsystem model call by its system prompt.

    Catches all three memory configurations of ``run_agent_loop``: the
    pre-turn retriever (``Ariel's memory retriever``), the agent-invoked
    encoder (``Ariel's memory encoder``), and the scheduled dreamer
    (``Ariel's memory dreamer``). Tests that filter retriever calls also need
    to filter rememberer calls, because the worker's ``memory_dream`` task is
    enqueued by ``enqueue_due_memory_dream`` on every ``process_one_task``
    and runs in the same drain loop as ``user_message`` tasks.

    The name remains ``is_retriever_call`` for callsite compatibility; the
    contract is "memory-subsystem call, return a synthesized done response."
    """
    return _detect_memory_subsystem(messages) is not None


def empty_recall_response(
    *,
    provider: str,
    model: str,
    provider_response_id: str | None = None,
    messages: list[ModelMessage] | None = None,
) -> ModelResponse:
    """A canned response that exits a memory-subsystem loop immediately.

    For the retriever (``output_mode='finding'``) the program emits an empty
    ``recall_v1`` finding; for the encoder / dreamer (``output_mode='operations'``)
    it calls ``agent.emit_done``. The configuration is sniffed from
    ``messages`` when supplied; the retriever finding is the safe default
    for the original callers that pre-date the rememberer.

    Lets tests' canned-response queues stay focused on the main agent.
    """
    rid = provider_response_id or "resp_retriever_empty"
    config = _detect_memory_subsystem(messages) if messages is not None else "retriever"
    if config in ("encoder", "dreamer"):
        program = "agent.emit_done(summary='')"
    else:
        program = 'agent.emit_finding(summary="", claims=[], gaps=[], sources=[])'
    return run_response(
        source=program,
        provider=provider,
        model=model,
        provider_response_id=rid,
        input_tokens=0,
        output_tokens=0,
    )


class FakeModelAdapter(ModelAdapter):
    """Base ``ModelAdapter`` subclass for tests.

    Bypasses ``ModelAdapter.__init__`` (which resolves real tiers and would
    require API keys); subclasses override ``_respond`` to return a typed
    ``ModelResponse`` from the call's ``messages``/``tools`` inputs.

    Subclasses customise ``provider`` / ``model`` for the ``tier_binding``
    surface the loop reads for its ``evt.model.started`` event.
    """

    provider: str = "provider.fake"
    model: str = "model.fake"

    def __init__(self) -> None:
        # Deliberately skip ``ModelAdapter.__init__`` — no settings, no tier
        # resolution. ``call`` and ``tier_binding`` are the only surfaces the
        # loop touches; both are overridden here.
        pass

    def tier_binding(self, tier: ModelTier) -> TierBinding:
        del tier
        return TierBinding(
            provider=self.provider,
            model=self.model,
            max_context_tokens=200_000,
            supports_tools=True,
            supports_structured_output=True,
            supports_vision=False,
        )

    def _respond(self, request: ModelCall) -> ModelResponse:
        raise NotImplementedError("FakeModelAdapter subclasses must override _respond")

    async def call(self, request: ModelCall) -> ModelResponse:  # type: ignore[override]  # justify-test-fake
        return self._respond(request)

    async def embed(  # type: ignore[override]  # justify-test-fake
        self, texts: list[str], tier: ModelTier = ModelTier.EMBEDDING
    ) -> list[list[float]]:
        del tier
        # Stub: zero vectors of the configured DB-column width. Tests that
        # exercise memory writes/searches go through ``embed_text``; the
        # production adapter would hit OpenAIEmbeddingModel which the fake
        # never instantiated (``__init__`` is bypassed). Override on the
        # subclass if a test cares about vector content / ranking.
        return [[0.0] * MEMORY_EMBEDDING_DIMENSIONS for _ in texts]


def process_queued_action_execution(client: TestClient, approval_payload: dict[str, Any]) -> bool:
    action_attempt_id = approval_payload.get("action_attempt_id")
    if not isinstance(action_attempt_id, str):
        raise AssertionError("approval response did not include action_attempt_id")
    app_state = cast(Any, client.app).state
    return process_action_execution_task(
        session_factory=app_state.session_factory,
        action_attempt_id=action_attempt_id,
        google_runtime=GoogleConnectorRuntime(
            oauth_client=app_state.google_oauth_client,
            workspace_provider=app_state.google_workspace_provider,
            redirect_uri=str(app_state.google_oauth_redirect_uri),
            oauth_state_ttl_seconds=int(app_state.google_oauth_state_ttl_seconds),
            encryption_secret=str(app_state.connector_encryption_secret),
            encryption_key_version=str(app_state.connector_encryption_key_version),
            encryption_keys=(
                str(app_state.connector_encryption_keys)
                if app_state.connector_encryption_keys is not None
                else None
            ),
        ),
        agency_runtime=None,
        now_fn=_utcnow,
        new_id_fn=_new_id,
    )
