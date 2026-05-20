from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import (
    RuntimeProvenance,
    _FunctionCallProcessingContext,
    process_action_execution_task,
    process_one_call,
)
from ariel.app import _new_id, _utcnow
from ariel.google_connector import GoogleConnectorRuntime
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


def responses_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            }
        ],
    }


def responses_run_message(
    *,
    assistant_text: str,
    provider: str,
    model: str,
    provider_response_id: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    del assistant_text
    if not calls:
        raise AssertionError("responses_with_run_calls requires at least one run call")
    return {
        "provider": provider,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "function_call",
                "id": "fc_run_test",
                "call_id": "call_run_test",
                "name": "run",
                "arguments": json.dumps(
                    {"source": run_program_source_from_calls(calls)},
                    sort_keys=True,
                ),
                "status": "completed",
            }
        ],
    }


def is_retriever_call(input_items: list[dict[str, Any]]) -> bool:
    """Detects the retriever's pre-turn investigation loop by its system prompt."""
    for item in input_items:
        if item.get("role") == "system" and "Ariel's memory retriever" in str(
            item.get("content", "")
        ):
            return True
    return False


def empty_recall_response(
    *,
    provider: str,
    model: str,
    provider_response_id: str | None = None,
) -> dict[str, Any]:
    """A retriever-loop response that emits an empty recall_v1 finding immediately.

    Lets tests' canned-response queues stay focused on the main agent.
    """
    rid = provider_response_id or "resp_retriever_empty"
    program = 'agent.emit_finding(summary="", claims=[], gaps=[], sources=[])'
    return {
        "provider": provider,
        "model": model,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "provider_response_id": rid,
        "output": [
            {
                "type": "function_call",
                "id": f"fc_{rid}",
                "call_id": f"call_{rid}",
                "name": "run",
                "arguments": __import__("json").dumps({"source": program}, sort_keys=True),
                "status": "completed",
            }
        ],
    }


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
