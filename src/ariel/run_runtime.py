from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

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
from .model_adapter import ToolCall, ToolSpec
from .persistence import ActionAttemptRecord, TurnRecord
from .sandbox_runtime import ProgramResult, RunSandbox

if TYPE_CHECKING:
    from .model_adapter import ModelAdapter

_MAX_RUN_SOURCE_CHARS = 20000

# The three agent-output syscalls. They are always eligible; capability syscalls
# are added per turn from the allowed capability ids.
_AGENT_EMIT_MESSAGE = "agent.emit_message"
_AGENT_EMIT_VALUE = "agent.emit_value"
_AGENT_PAUSE_UNTIL_INPUT = "agent.pause_until_input"
_AGENT_SYSCALL_NAMES = (_AGENT_EMIT_MESSAGE, _AGENT_EMIT_VALUE, _AGENT_PAUSE_UNTIL_INPUT)

# Non-main-loop terminal output syscalls. Always bound into the sandbox so a
# misuse in the main loop surfaces as a typed host-callback error rather than an
# AttributeError traceback. The gate is the host callback, keyed on the
# main-agent loop. agent.emit_finding exits a retriever/researcher run;
# agent.emit_done exits a rememberer run.
_AGENT_EMIT_FINDING = "agent.emit_finding"
_AGENT_EMIT_DONE = "agent.emit_done"

_AGENT_EMIT_FINDING_MAIN_LOOP_ERROR = (
    "agent.emit_finding is not available in the main agent loop; "
    "finish the main loop with agent.emit_message"
)
_AGENT_EMIT_DONE_MAIN_LOOP_ERROR = (
    "agent.emit_done is not available in the main agent loop; "
    "finish the main loop with agent.emit_message"
)

# Host-side per-turn scratch store syscalls — always eligible, not capabilities.
_SCRATCH_SET = "scratch.set"
_SCRATCH_GET = "scratch.get"
_SCRATCH_SYSCALL_NAMES = (_SCRATCH_SET, _SCRATCH_GET)

# Scratch store bounds.
_SCRATCH_MAX_ENTRIES = 64
_SCRATCH_MAX_VALUE_BYTES = 512 * 1024  # 512 KiB per value
_SCRATCH_MAX_TOTAL_BYTES = 4 * 1024 * 1024  # 4 MiB total

_MAX_EMITTED_VALUES = 10
_MAX_EMITTED_VALUE_BYTES = 12000
_MAX_EMITTED_FINDING_BYTES = 64000


@dataclass(slots=True, frozen=True)
class ScratchEntry:
    """One entry in the host-side per-turn scratch store.

    ``value`` is the stored value (JSON-encodable).  ``provenance`` is the
    taint of the program that called ``scratch.set``; ``scratch.get`` re-applies
    it so untrusted data carried across programs stays tainted.
    """

    value: Any
    provenance: RuntimeProvenance | None


@dataclass(slots=True)
class RunProgramResult:
    """Outcome of one model-authored ``run`` program executed in the sandbox.

    ``program_ok`` is the sandbox ``ProgramResult.ok``: ``False`` means the
    program did not complete cleanly. Failed programs surface no proposals, so
    ``emitted_message``/``emitted_values``/``emitted_finding``/``paused`` are
    scrubbed here and the staged ``ApprovalRequestRecord`` rows the syscalls
    wrote are left for the caller's transaction to roll back. ``action_attempts``
    is still the syscall trace — the audit spine — and is returned regardless.

    ``runtime_provenance`` is this program's taint delta: a tainted
    ``RuntimeProvenance`` carrying the evidence its syscalls produced, or
    ``None`` if no syscall returned untrusted-influenced content. The caller
    merges it into the turn baseline so the next program in the same turn is
    evaluated with that taint. It is returned regardless of ``program_ok``: an
    inline read that tainted the program already returned its result and stands
    even if a later syscall raised.

    ``emitted_finding`` is set only when the program was run with
    ``is_main_agent_loop=False`` and the program called ``agent.emit_finding``; it is
    ``None`` in main-agent runs and on ``program_ok=False``.

    ``emitted_done`` is set only when the program was run with
    ``is_main_agent_loop=False`` and the program called ``agent.emit_done``; it is
    ``None`` in main-agent runs and on ``program_ok=False``.  When set, the value
    is the summary string the model passed (possibly empty string).
    """

    emitted_message: str
    emitted_values: list[Any]
    emitted_finding: dict[str, Any] | None
    emitted_done: str | None
    paused: bool
    action_attempts: list[ActionAttemptRecord]
    program_ok: bool
    program_error: str | None
    callback_errors: list[str]
    runtime_provenance: RuntimeProvenance | None


def run_tool_definitions() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="run",
            description=(
                "Execute one Ariel run program. The source is a Python program run in a "
                "sandbox: it may use variables, if/for/while, comprehensions, exception "
                "handling, and the safe standard library (json, re, datetime, math). Every "
                "effect is a typed syscall to a namespaced host callable -- "
                "agent.emit_message for user-visible output, agent.emit_value for internal "
                "data, and capability syscalls such as memory.recall, email.search, or "
                "agency.run. A syscall returns its result into the program; an "
                "approval-gated syscall returns a pending value and is not executed inline. "
                "Call exactly one run tool with the program as the source string."
            ),
            parameters={
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
        )
    ]


def parse_run_function_call(
    tool_calls: list[ToolCall],
) -> tuple[str | None, str | None]:
    """Validate the model's tool-call envelope for the ``run`` protocol.

    The model must emit exactly one ``run`` tool call whose ``arguments`` carry
    a single non-empty ``source`` string bounded by ``_MAX_RUN_SOURCE_CHARS``.
    Returns ``(source, None)`` on success or ``(None, reason)`` on protocol
    failure; the source itself is executed by the sandbox, not parsed here.
    """
    if len(tool_calls) != 1:
        return None, "run_protocol_requires_exactly_one_tool_call"
    tool_call = tool_calls[0]
    if tool_call.name != "run":
        return None, "run_protocol_requires_run_tool"
    arguments = tool_call.arguments
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

    The three ``agent.*`` output syscalls, the two non-main-loop terminal output
    syscalls (``agent.emit_finding``, ``agent.emit_done``), the two ``scratch.*``
    store syscalls, plus the run-callable name of every allowed capability id.
    A capability with no run-callable alias is dropped: it cannot be named in a
    program.

    ``agent.emit_finding`` and ``agent.emit_done`` are always bound so a main-loop
    misuse surfaces as a typed host-callback error (see the callback) rather than
    a sandbox ``AttributeError`` traceback.
    """

    names: set[str] = (
        set(_AGENT_SYSCALL_NAMES)
        | set(_SCRATCH_SYSCALL_NAMES)
        | {_AGENT_EMIT_FINDING, _AGENT_EMIT_DONE}
    )
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
        # A succeeded capability call nests its result under "output".
        return True, payload["output"]
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
    # justify-defect: process_one_call owns this finite status channel; an
    # unknown status means the producer contract changed without this consumer.
    raise RuntimeError(f"unknown_call_status: {status}")


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
    scratch: dict[str, ScratchEntry],
    model_adapter: ModelAdapter | None = None,
    is_main_agent_loop: bool = True,
) -> RunProgramResult:
    """Run one model-authored Python ``run`` program inside the sandbox.

    Each syscall is dispatched host-side: the three ``agent.*`` output syscalls
    are handled inline here; every other syscall is a capability call routed
    through ``process_one_call``, which owns policy, taint, approval, egress,
    guardrails, and the action ledger. Taint accumulates within the program:
    after a capability syscall sets ``ctx.result_runtime_provenance``, that
    provenance is merged into the threaded provenance so later syscalls in the
    same program see it.

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
    emitted_finding: dict[str, Any] | None = None
    emitted_done: str | None = None
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
        nonlocal emitted_message, emitted_values, emitted_finding, emitted_done, paused, call_index

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

        if name == _SCRATCH_SET:
            if set(syscall_input.keys()) != {"key", "value"}:
                callback_errors.append("scratch_set_schema_invalid")
                return False, "scratch_set_schema_invalid"
            key = syscall_input["key"]
            value = syscall_input["value"]
            if not isinstance(key, str) or not key:
                callback_errors.append("scratch_key_invalid")
                return False, "scratch_key_invalid"
            try:
                value_bytes = json.dumps(value, sort_keys=True).encode("utf-8")
            except TypeError:
                callback_errors.append("scratch_value_too_large")
                return False, "scratch_value_too_large"
            if len(value_bytes) > _SCRATCH_MAX_VALUE_BYTES:
                callback_errors.append("scratch_value_too_large")
                return False, "scratch_value_too_large"
            current_total = sum(
                len(json.dumps(e.value, sort_keys=True).encode("utf-8"))
                for e in scratch.values()
                if e.value is not None
            )
            if key not in scratch and len(scratch) >= _SCRATCH_MAX_ENTRIES:
                callback_errors.append("scratch_store_full")
                return False, "scratch_store_full"
            projected = current_total + len(value_bytes)
            if key in scratch:
                projected -= len(json.dumps(scratch[key].value, sort_keys=True).encode("utf-8"))
            if projected > _SCRATCH_MAX_TOTAL_BYTES:
                callback_errors.append("scratch_store_full")
                return False, "scratch_store_full"
            scratch[key] = ScratchEntry(value=value, provenance=current_provenance[0])
            return True, None

        if name == _SCRATCH_GET:
            if set(syscall_input.keys()) != {"key"}:
                callback_errors.append("scratch_get_schema_invalid")
                return False, "scratch_get_schema_invalid"
            key = syscall_input["key"]
            if not isinstance(key, str) or key not in scratch:
                return False, "scratch_key_missing"
            entry = scratch[key]
            if entry.provenance is not None and entry.provenance.status == "tainted":
                current_provenance[0] = entry.provenance
                program_taint_evidence.extend(entry.provenance.evidence)
            return True, entry.value

        if name == _AGENT_EMIT_FINDING:
            # agent.emit_finding is the investigation run's terminal output. The
            # syscall is bound regardless of is_main_agent_loop so a main-loop
            # misuse surfaces here as a typed error the model can recover from
            # rather than an AttributeError traceback.
            if is_main_agent_loop:
                callback_errors.append(_AGENT_EMIT_FINDING_MAIN_LOOP_ERROR)
                return False, _AGENT_EMIT_FINDING_MAIN_LOOP_ERROR
            summary = syscall_input.get("summary")
            claims = syscall_input.get("claims")
            gaps = syscall_input.get("gaps")
            sources = syscall_input.get("sources")
            if (
                set(syscall_input.keys()) != {"summary", "claims", "gaps", "sources"}
                or not isinstance(summary, str)
                or not isinstance(claims, list)
                or not isinstance(gaps, list)
                or not isinstance(sources, list)
            ):
                callback_errors.append("agent_emit_finding_schema_invalid")
                return False, "agent_emit_finding_schema_invalid"
            try:
                encoded = json.dumps(syscall_input, sort_keys=True)
            except TypeError:
                callback_errors.append("agent_emit_finding_schema_invalid")
                return False, "agent_emit_finding_schema_invalid"
            if len(encoded.encode("utf-8")) > _MAX_EMITTED_FINDING_BYTES:
                callback_errors.append("agent_emit_finding_too_large")
                return False, "agent_emit_finding_too_large"
            emitted_finding = syscall_input
            return True, None

        if name == _AGENT_EMIT_DONE:
            # agent.emit_done is the rememberer's terminal output. Bound
            # regardless of is_main_agent_loop; a main-loop misuse surfaces as a
            # typed error here.
            if is_main_agent_loop:
                callback_errors.append(_AGENT_EMIT_DONE_MAIN_LOOP_ERROR)
                return False, _AGENT_EMIT_DONE_MAIN_LOOP_ERROR
            if set(syscall_input.keys()) - {"summary"}:
                callback_errors.append("agent_emit_done_schema_invalid")
                return False, "agent_emit_done_schema_invalid"
            summary_val = syscall_input.get("summary", "")
            if not isinstance(summary_val, str):
                callback_errors.append("agent_emit_done_schema_invalid")
                return False, "agent_emit_done_schema_invalid"
            emitted_done = summary_val
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
            sandbox=sandbox,
            model_adapter=model_adapter,
        )
        # Within-program taint: a syscall that returned untrusted-influenced
        # content taints every later syscall in this program, and contributes
        # to the program's taint delta returned to the caller.
        if ctx.result_runtime_provenance is not None:
            current_provenance[0] = ctx.result_runtime_provenance
            program_taint_evidence.extend(ctx.result_runtime_provenance.evidence)

        new_outputs = ctx.function_call_outputs[outputs_before:]
        if len(new_outputs) != 1:
            # justify-defect: process_one_call appends exactly one output per
            # call; this path always supplies call_id, so any other count means
            # the action runtime contract broke.
            raise RuntimeError(f"{name}: process_one_call_output_count:{len(new_outputs)}")
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
            emitted_finding=None,
            emitted_done=None,
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
        emitted_finding=emitted_finding,
        emitted_done=emitted_done,
        paused=paused,
        action_attempts=ctx.created_action_attempts,
        program_ok=True,
        program_error=None,
        callback_errors=callback_errors,
        runtime_provenance=program_taint,
    )
