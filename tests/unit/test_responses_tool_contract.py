from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

from ariel.action_runtime import (
    RuntimeProvenance,
    _FunctionCallProcessingContext,
    process_one_call,
)
from ariel.app import (
    _POLICY_SYSTEM_INSTRUCTIONS,
    _eligible_internal_callable_capability_ids,
)
from ariel.capability_registry import (
    capability_id_for_run_callable,
    internal_callable_capability_ids,
    run_callable_name_for_capability_id,
)
from ariel.executor import ExecutionResult
from ariel.persistence import TurnRecord
from ariel.run_runtime import parse_run_function_call, run_tool_definitions
from sqlalchemy.orm import Session


def test_normal_response_tool_surface_is_single_strict_run_tool() -> None:
    def assert_strict_object_schema(schema: dict[str, Any], path: str) -> None:
        if schema.get("type") == "object" or "properties" in schema:
            assert schema.get("additionalProperties") is False, path
            properties = schema.get("properties", {})
            assert isinstance(properties, dict), path
            assert set(schema.get("required", [])) == set(properties.keys()), path
            for property_name, property_schema in properties.items():
                if isinstance(property_schema, dict):
                    assert_strict_object_schema(property_schema, f"{path}.{property_name}")
        items = schema.get("items")
        if isinstance(items, dict):
            assert_strict_object_schema(items, f"{path}[]")

    tools = run_tool_definitions()
    assert [tool["name"] for tool in tools] == ["run"]
    assert tools[0]["type"] == "function"
    assert tools[0]["strict"] is True
    assert_strict_object_schema(tools[0]["parameters"], "run")


def test_model_facing_policy_instructions_use_run_callable_names() -> None:
    instructions = "\n".join(_POLICY_SYSTEM_INSTRUCTIONS)

    assert "cap." not in instructions
    assert "agent.pause_until_input" in instructions
    assert "attachment.read" in instructions


def test_run_protocol_requires_exactly_one_run_call() -> None:
    assert parse_run_function_call([]) == (
        None,
        "run_protocol_requires_exactly_one_tool_call",
    )
    assert parse_run_function_call(
        [{"name": "run", "arguments": "{}"}, {"name": "run", "arguments": "{}"}]
    ) == (None, "run_protocol_requires_exactly_one_tool_call")
    assert parse_run_function_call([{"name": "not_the_run_tool", "arguments": "{}"}]) == (
        None,
        "run_protocol_requires_run_tool",
    )


def test_run_callable_aliases_are_unique_and_deliberate() -> None:
    aliases: dict[str, str] = {}
    for capability_id in internal_callable_capability_ids():
        alias = run_callable_name_for_capability_id(capability_id)
        assert alias is not None, capability_id
        assert not alias.startswith("cap.")
        assert alias not in aliases, alias
        aliases[alias] = capability_id
        assert capability_id_for_run_callable(alias) == capability_id

    assert capability_id_for_run_callable("discord.no_response") is None
    assert capability_id_for_run_callable("memory.forget_all") is None


def test_internal_callable_eligibility_is_default_deny() -> None:
    capability_ids = set(
        _eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {},
            }
        )
    )

    assert "cap.memory.recall" in capability_ids
    assert "cap.memory.remember" in capability_ids
    assert "cap.attachment.read" not in capability_ids
    assert "cap.agency.run" not in capability_ids
    assert "cap.search.web" not in capability_ids
    assert "cap.maps.directions" not in capability_ids
    assert "cap.weather.forecast" not in capability_ids


def test_run_source_is_a_python_program_string() -> None:
    """A valid run call carries a Python-program source string; it is not parsed
    as a flat-JSON call list. ``parse_run_function_call`` validates only the
    tool-call envelope and the source-size budget; the program itself runs in
    the sandbox."""

    source = (
        "results = memory.recall(query='project phoenix')\n"
        "agent.emit_message(text='Found ' + str(len(results['facts'])) + ' memories.')\n"
    )
    parsed_source, error = parse_run_function_call(
        [{"name": "run", "arguments": json.dumps({"source": source})}]
    )
    assert error is None
    assert parsed_source == source.strip()


def test_run_source_rejects_blank_and_oversized_programs() -> None:
    assert parse_run_function_call(
        [{"name": "run", "arguments": json.dumps({"source": "   "})}]
    ) == (None, "run_source_empty")
    assert parse_run_function_call(
        [{"name": "run", "arguments": json.dumps({"source": "x" * 20001})}]
    ) == (None, "run_source_too_large")


def test_action_runtime_has_no_deterministic_tool_result_synthesizer() -> None:
    source = (Path(__file__).parents[2] / "src/ariel/action_runtime.py").read_text()

    assert "_synthesize_" not in source
    assert "build_assistant_action_appendix" not in source
    assert "attachment content:" not in source


def _run_one_call(
    *,
    db: Session,
    function_call_raw: dict[str, Any],
    turn: TurnRecord,
    now: datetime,
    new_id_fn: Any,
    add_event: Any,
    runtime_provenance: RuntimeProvenance | None,
    attachment_runtime: Any | None = None,
    allowed_capability_ids: set[str],
) -> _FunctionCallProcessingContext:
    """Run one capability syscall through ``process_one_call``.

    This is the per-call lifecycle a ``run`` program's syscalls dispatch
    through; the run-program host path drives the same function. The tests use
    it directly to assert the per-call rails (turn scope, execution, taint).
    """

    ctx = _FunctionCallProcessingContext()
    process_one_call(
        ctx=ctx,
        function_call_index=1,
        function_call_raw=function_call_raw,
        db=db,
        session_factory=None,
        session_id="ses_1",
        turn=turn,
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=add_event,
        now_fn=lambda: now,
        new_id_fn=new_id_fn,
        runtime_provenance=runtime_provenance,
        google_runtime=None,
        execute_google_reads_outside_transaction=False,
        agency_runtime=None,
        attachment_runtime=attachment_runtime,
        allowed_capability_id_set=allowed_capability_ids,
        settings=None,
    )
    return ctx


def test_process_one_call_default_denies_without_turn_scope() -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    events: list[tuple[str, dict[str, Any]]] = []

    class Db:
        def add(self, record: Any) -> None:
            raise AssertionError(f"unscoped tool created a record: {record!r}")

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    turn = TurnRecord(
        id="trn_1",
        session_id="ses_1",
        user_message="quiet",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.legacy.no_response",
            "input": {"reason": "nothing useful to add"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        runtime_provenance=RuntimeProvenance(status="clean"),
        allowed_capability_ids=set(),
    )

    assert ctx.created_action_attempts == []
    function_call_output = ctx.function_call_outputs[0]
    assert function_call_output["type"] == "function_call_output"
    assert function_call_output["call_id"] == "call_1"
    assert json.loads(function_call_output["output"]) == {
        "status": "denied",
        "capability_id": "cap.legacy.no_response",
        "error": "tool_not_in_turn_scope",
    }
    assert events == [
        (
            "evt.action.call_denied",
            {
                "call_index": 1,
                "call_id": "call_1",
                "tool_name": "cap.legacy.no_response",
                "capability_id": "cap.legacy.no_response",
                "reason": "tool_not_in_turn_scope",
            },
        )
    ]


def test_process_one_call_denies_unscoped_tools() -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    class Db:
        def add(self, record: Any) -> None:
            raise AssertionError(f"unscoped tool created a record: {record!r}")

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    turn = TurnRecord(
        id="trn_1",
        session_id="ses_1",
        user_message="echo",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.legacy.no_response",
            "input": {"reason": "nothing useful to add"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        add_event=lambda _event_type, _payload: None,
        runtime_provenance=None,
        allowed_capability_ids=set(),
    )

    assert ctx.created_action_attempts == []
    assert json.loads(ctx.function_call_outputs[0]["output"]) == {
        "status": "denied",
        "capability_id": "cap.legacy.no_response",
        "error": "tool_not_in_turn_scope",
    }


def test_process_one_call_executes_attachment_read_runtime() -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    events: list[tuple[str, dict[str, Any]]] = []
    id_counts: dict[str, int] = {}

    class Db:
        def add(self, record: Any) -> None:
            return None

        def flush(self) -> None:
            return None

        def get_bind(self) -> None:
            return None

    class AttachmentRuntime:
        def execute_read(self, **_: Any) -> ExecutionResult:
            return ExecutionResult(
                status="succeeded",
                output={
                    "attachment_ref": "discord:777",
                    "filename": "report.txt",
                    "retrieved_at": "2026-04-27T12:00:00Z",
                    "modality": "text",
                    "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
                    "blocks": [{"kind": "text", "text": "quarterly revenue increased"}],
                    "results": [
                        {
                            "title": "report.txt",
                            "source": "discord://channel/1/message/2/attachment/777",
                            "snippet": "quarterly revenue increased",
                            "published_at": None,
                        }
                    ],
                    "runtime_provenance": {
                        "status": "tainted",
                        "evidence": [
                            {
                                "kind": "attachment_content_read",
                                "attachment_ref": "discord:777",
                                "filename": "report.txt",
                                "modality": "text",
                            }
                        ],
                    },
                },
                error=None,
            )

    def new_id(prefix: str) -> str:
        id_counts[prefix] = id_counts.get(prefix, 0) + 1
        return f"{prefix}_{id_counts[prefix]}"

    turn = TurnRecord(
        id="trn_1",
        session_id="ses_1",
        user_message="read the attachment",
        assistant_message=None,
        status="in_progress",
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    ctx = _run_one_call(
        db=cast(Session, Db()),
        function_call_raw={
            "call_id": "call_1",
            "capability_id": "cap.attachment.read",
            "input": {"attachment_ref": "discord:777", "intent": "summarize"},
        },
        turn=turn,
        now=fixed_now,
        new_id_fn=new_id,
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        runtime_provenance=RuntimeProvenance(status="clean"),
        attachment_runtime=cast(Any, AttachmentRuntime()),
        allowed_capability_ids={"cap.attachment.read"},
    )

    assert ctx.created_action_attempts[0].capability_id == "cap.attachment.read"
    assert ctx.created_action_attempts[0].status == "succeeded"
    assert json.loads(ctx.function_call_outputs[0]["output"])["output"]["blocks"] == [
        {"kind": "text", "text": "quarterly revenue increased"}
    ]
    # The attachment read returned untrusted-influenced content; process_one_call
    # records the tainted provenance so a later same-program syscall sees it.
    assert ctx.result_runtime_provenance == RuntimeProvenance(
        status="tainted",
        evidence=(
            {
                "kind": "attachment_content_read",
                "attachment_ref": "discord:777",
                "filename": "report.txt",
                "modality": "text",
            },
        ),
    )
