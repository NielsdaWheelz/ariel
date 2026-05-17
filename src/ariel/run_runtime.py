from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from .action_runtime import (
    RuntimeProvenance,
    _FunctionCallProcessingContext,
    process_one_call,
)
from .attachment_content import AttachmentContentRuntime
from .capability_registry import (
    capability_id_for_run_callable,
    run_callable_name_for_capability_id,
)
from .config import AppSettings
from .google_connector import GoogleConnectorRuntime
from .persistence import ActionAttemptRecord, TurnRecord
from .sandbox_runtime import ProgramResult, RunSandbox

_MAX_RUN_SOURCE_CHARS = 20000

# The three agent-output syscalls. They are always eligible; capability syscalls
# are added per turn from the allowed capability ids.
_AGENT_EMIT_MESSAGE = "agent.emit_message"
_AGENT_EMIT_VALUE = "agent.emit_value"
_AGENT_PAUSE_UNTIL_INPUT = "agent.pause_until_input"
_AGENT_SYSCALL_NAMES = (_AGENT_EMIT_MESSAGE, _AGENT_EMIT_VALUE, _AGENT_PAUSE_UNTIL_INPUT)

_MAX_EMITTED_VALUES = 10
_MAX_EMITTED_VALUE_BYTES = 12000


@dataclass(slots=True)
class RunProgramResult:
    """Outcome of one model-authored ``run`` program executed in the sandbox.

    ``program_ok`` is the sandbox ``ProgramResult.ok``: ``False`` means the
    program did not complete cleanly. Per the cutover's "Program Failure", a
    program that does not complete cleanly surfaces no proposals, so on failure
    ``emitted_message``/``emitted_values``/``paused`` are scrubbed here and the
    staged ``ApprovalRequestRecord`` rows the syscalls wrote are left for the
    caller's transaction to roll back. ``action_attempts`` is still the syscall
    trace — the audit spine — and is returned regardless.

    ``runtime_provenance`` is this program's taint delta: a tainted
    ``RuntimeProvenance`` carrying the evidence its syscalls produced, or
    ``None`` if no syscall returned untrusted-influenced content. The caller
    merges it into the turn baseline so the next program in the same turn is
    evaluated with that taint. It is returned regardless of ``program_ok``: an
    inline read that tainted the program already returned its result and stands
    even if a later syscall raised.
    """

    emitted_message: str
    emitted_values: list[Any]
    paused: bool
    action_attempts: list[ActionAttemptRecord]
    program_ok: bool
    program_error: str | None
    callback_errors: list[str]
    runtime_provenance: RuntimeProvenance | None


def run_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "run",
            "description": (
                "Execute one Ariel run program. The source is a Python program run in a "
                "sandbox: it may use variables, if/for/while, comprehensions, exception "
                "handling, and the safe standard library (json, re, datetime, math). Every "
                "effect is a typed syscall to a namespaced host callable -- "
                "agent.emit_message for user-visible output, agent.emit_value for internal "
                "data, and capability syscalls such as memory.search, email.search, or "
                "agency.run. A syscall returns its result into the program; an "
                "approval-gated syscall returns a pending value and is not executed inline. "
                "Call exactly one run tool with the program as the source string."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20000,
                    }
                },
                "required": ["source"],
            },
            "strict": True,
        }
    ]


def parse_run_function_call(
    function_calls: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    if len(function_calls) != 1:
        return None, "run_protocol_requires_exactly_one_tool_call"
    function_call = function_calls[0]
    if function_call.get("name") != "run":
        return None, "run_protocol_requires_run_tool"
    raw_arguments = function_call.get("arguments")
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
    except ValueError:
        return None, "run_arguments_invalid_json"
    if set(arguments.keys()) != {"source"} or not isinstance(arguments.get("source"), str):
        return None, "run_arguments_schema_invalid"
    source = arguments["source"].strip()
    if not source:
        return None, "run_source_empty"
    if len(source) > _MAX_RUN_SOURCE_CHARS:
        return None, "run_source_too_large"
    return source, None


def _eligible_syscall_names(allowed_capability_ids: set[str]) -> tuple[str, ...]:
    """The syscall callables the program may call this turn.

    The three ``agent.*`` output syscalls plus the run-callable name of every
    allowed capability id. A capability with no run-callable alias is dropped:
    it cannot be named in a program.
    """

    names: set[str] = set(_AGENT_SYSCALL_NAMES)
    for capability_id in allowed_capability_ids:
        run_callable_name = run_callable_name_for_capability_id(capability_id)
        if run_callable_name is not None:
            names.add(run_callable_name)
    return tuple(sorted(names))


def _capability_syscall_value(function_call_output: dict[str, Any]) -> tuple[bool, Any]:
    """Derive ``(ok, value)`` for the program from one ``process_one_call`` output.

    ``process_one_call`` appends exactly one ``function_call_output`` per call,
    with ``output`` a JSON string of a payload carrying ``status``. Map the
    host-side status to the program-visible syscall result.
    """

    payload = json.loads(function_call_output["output"])
    status = payload.get("status")
    if status == "succeeded":
        # Most capabilities nest the result under "output"; thread_watch.list
        # spreads it inline alongside status/capability_id. Handle both.
        if "output" in payload:
            return True, payload["output"]
        return True, {
            key: value for key, value in payload.items() if key not in {"status", "capability_id"}
        }
    if status == "approval_required":
        return True, {
            "status": "approval_required",
            "approval_ref": payload.get("approval_ref"),
        }
    if status == "queued":
        return True, payload
    if status in {"blocked", "denied"}:
        return False, str(payload.get("reason") or payload.get("error") or status)
    if status == "failed":
        return False, str(payload.get("error") or payload.get("reason") or status)
    # process_one_call only emits the statuses above; an unknown one is a defect.
    return False, f"unknown_call_status: {status}"


def execute_run_program(
    *,
    sandbox: RunSandbox,
    source: str,
    db: Session,
    session_factory: sessionmaker[Session] | None,
    session_id: str,
    turn: TurnRecord,
    proposal_index_start: int,
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    runtime_provenance: RuntimeProvenance | None,
    google_runtime: GoogleConnectorRuntime | None,
    execute_google_reads_outside_transaction: bool,
    agency_runtime: Any | None,
    attachment_runtime: AttachmentContentRuntime | None,
    allowed_capability_ids: set[str],
    settings: AppSettings | None,
    memory_import_cutover_enabled: bool,
) -> RunProgramResult:
    """Run one model-authored Python ``run`` program inside the sandbox.

    Each syscall is dispatched host-side: the three ``agent.*`` output syscalls
    are handled inline here; every other syscall is a capability call routed
    through ``process_one_call`` — the same per-call lifecycle the flat-list path
    used — so policy, taint, approval, egress, guardrails, and the action ledger
    all still apply. Taint accumulates within the program: after a capability
    syscall sets ``ctx.result_runtime_provenance``, that provenance is merged
    into the threaded provenance so later syscalls in the same program see it.

    ``proposal_index_start`` is the count of capability syscalls already made by
    earlier programs in this turn. Each capability syscall here is numbered
    ``proposal_index_start + n`` (n starting at 1), so ``proposal_index`` — and
    the synthesized ``call_id`` — stay unique across every program in the turn,
    satisfying the ``(turn_id, proposal_index)`` constraint. The caller advances
    its counter by ``len(RunProgramResult.action_attempts)`` after each program.

    The conversation runs on the caller's thread (see ``_drive_program``), so the
    callback below safely shares ``db``.
    """

    ctx = _FunctionCallProcessingContext()
    syscall_names = _eligible_syscall_names(allowed_capability_ids)

    emitted_message = ""
    emitted_values: list[Any] = []
    paused = False
    callback_errors: list[str] = []
    # Boxed so the callback closure can advance taint between syscalls.
    current_provenance: list[RuntimeProvenance | None] = [runtime_provenance]
    # The taint this program produced: every syscall that returned
    # untrusted-influenced content contributes its evidence here. Returned as
    # the program's taint delta so the caller can thread it onto the turn
    # baseline for the next program in the same turn. Kept separate from
    # ``current_provenance`` so the within-program threading is unchanged.
    program_taint_evidence: list[dict[str, Any]] = []
    call_index = 0

    def syscall_callback(name: str, syscall_input: dict[str, Any]) -> tuple[bool, Any]:
        nonlocal emitted_message, emitted_values, paused, call_index

        if name == _AGENT_EMIT_MESSAGE:
            text = syscall_input.get("text")
            if (
                set(syscall_input.keys()) != {"text"}
                or not isinstance(text, str)
                or not text.strip()
            ):
                callback_errors.append("agent_emit_message_schema_invalid")
                return False, "agent_emit_message_schema_invalid"
            if emitted_message:
                callback_errors.append("agent_emit_message_must_be_unique")
                return False, "agent_emit_message_must_be_unique"
            emitted_message = text.strip()
            return True, None

        if name == _AGENT_EMIT_VALUE:
            if set(syscall_input.keys()) != {"value"}:
                callback_errors.append("agent_emit_value_schema_invalid")
                return False, "agent_emit_value_schema_invalid"
            value = syscall_input["value"]
            try:
                encoded = json.dumps(value, sort_keys=True)
            except TypeError:
                callback_errors.append("agent_emit_value_schema_invalid")
                return False, "agent_emit_value_schema_invalid"
            if len(emitted_values) >= _MAX_EMITTED_VALUES:
                callback_errors.append("agent_emit_value_too_many")
                return False, "agent_emit_value_too_many"
            if len(encoded.encode("utf-8")) > _MAX_EMITTED_VALUE_BYTES:
                callback_errors.append("agent_emit_value_too_large")
                return False, "agent_emit_value_too_large"
            emitted_values.append(value)
            return True, None

        if name == _AGENT_PAUSE_UNTIL_INPUT:
            if syscall_input:
                callback_errors.append("agent_pause_until_input_schema_invalid")
                return False, "agent_pause_until_input_schema_invalid"
            paused = True
            return True, None

        capability_id = capability_id_for_run_callable(name)
        if capability_id is None:
            callback_errors.append(f"{name}: unknown_callable")
            return False, "unknown_callable"

        call_index += 1
        # Turn-global index: capability syscalls in earlier programs of this
        # turn already consumed proposal indices, so offset by their count to
        # keep proposal_index and call_id unique across the whole turn.
        turn_call_index = proposal_index_start + call_index
        outputs_before = len(ctx.function_call_outputs)
        ctx.result_runtime_provenance = None
        process_one_call(
            ctx=ctx,
            function_call_index=turn_call_index,
            function_call_raw={
                "call_id": f"run_call_{turn_call_index}",
                "tool_name": name,
                "capability_id": capability_id,
                "input": syscall_input,
            },
            db=db,
            session_factory=session_factory,
            session_id=session_id,
            turn=turn,
            approval_ttl_seconds=approval_ttl_seconds,
            approval_actor_id=approval_actor_id,
            add_event=add_event,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
            runtime_provenance=current_provenance[0],
            google_runtime=google_runtime,
            execute_google_reads_outside_transaction=execute_google_reads_outside_transaction,
            agency_runtime=agency_runtime,
            attachment_runtime=attachment_runtime,
            allowed_capability_id_set=allowed_capability_ids,
            settings=settings,
            memory_import_cutover_enabled=memory_import_cutover_enabled,
        )
        # Within-program taint: a syscall that returned untrusted-influenced
        # content taints every later syscall in this program, and contributes
        # to the program's taint delta returned to the caller.
        if ctx.result_runtime_provenance is not None:
            current_provenance[0] = ctx.result_runtime_provenance
            program_taint_evidence.extend(ctx.result_runtime_provenance.evidence)

        new_outputs = ctx.function_call_outputs[outputs_before:]
        if len(new_outputs) != 1:
            # process_one_call appends exactly one output per call; anything
            # else (e.g. a no-call_id path) is unreachable here since call_id
            # is always set, but fail closed rather than guess.
            callback_errors.append(f"{name}: missing_call_output")
            return False, "missing_call_output"
        return _capability_syscall_value(new_outputs[0])

    program_result: ProgramResult = sandbox.run_program(
        source=source,
        syscall_names=syscall_names,
        syscall_callback=syscall_callback,
    )

    # The program's taint delta: the evidence its syscalls produced, or None if
    # none did. None merges as a no-op; a tainted delta threads onto the turn
    # baseline so the next program in the same turn sees it.
    program_taint: RuntimeProvenance | None = (
        RuntimeProvenance(status="tainted", evidence=tuple(program_taint_evidence))
        if program_taint_evidence
        else None
    )

    if not program_result.ok:
        # Program Failure: the program did not complete cleanly, so no proposal
        # is surfaced as intended — discard emitted output. The staged action
        # attempts remain as the syscall trace (audit), but the caller's
        # transaction must not commit the staged ApprovalRequestRecord rows.
        return RunProgramResult(
            emitted_message="",
            emitted_values=[],
            paused=False,
            action_attempts=ctx.created_action_attempts,
            program_ok=False,
            program_error=program_result.error,
            callback_errors=callback_errors,
            runtime_provenance=program_taint,
        )

    return RunProgramResult(
        emitted_message=emitted_message,
        emitted_values=emitted_values,
        paused=paused,
        action_attempts=ctx.created_action_attempts,
        program_ok=True,
        program_error=None,
        callback_errors=callback_errors,
        runtime_provenance=program_taint,
    )
