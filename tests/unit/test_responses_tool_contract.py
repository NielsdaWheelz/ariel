from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
from ariel import action_runtime
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
    get_capability,
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
    assert parse_run_function_call([{"name": "cap_memory_search", "arguments": "{}"}]) == (
        None,
        "run_protocol_requires_run_tool",
    )


def test_run_callable_aliases_are_unique_and_deliberate() -> None:
    aliases: dict[str, str] = {}
    internal_only = {"cap.memory.eval"}
    for capability_id in internal_callable_capability_ids():
        alias = run_callable_name_for_capability_id(capability_id)
        if capability_id in internal_only:
            assert alias is None
            continue
        assert alias is not None, capability_id
        assert not alias.startswith("cap.")
        assert alias not in aliases, alias
        aliases[alias] = capability_id
        assert capability_id_for_run_callable(alias) == capability_id

    assert capability_id_for_run_callable("discord.no_response") is None
    assert capability_id_for_run_callable("memory.eval") is None


def test_internal_callable_eligibility_is_default_deny() -> None:
    capability_ids = set(
        _eligible_internal_callable_capability_ids(
            tool_surface_facts={
                "discord": {"available": True, "attachment_count": 0},
                "runtime_bindings": {},
            }
        )
    )

    assert "cap.memory.search" in capability_ids
    assert "cap.memory.deprioritize" in capability_ids
    assert "cap.memory.eval" not in capability_ids
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
        "results = memory.search(query='project phoenix')\n"
        "agent.emit_message(text='Found ' + str(len(results)) + ' memories.')\n"
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


def test_memory_runtime_handles_projection_read_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    calls: list[tuple[str, int]] = []

    def bounded_memory_payload(_: Any, *, section: str, limit: int) -> dict[str, Any]:
        calls.append((section, limit))
        return {
            "schema_version": "memory.sota.v2",
            "topics": [{"id": "mt_1"}] if section == "topics" else [],
            "context_blocks": [{"id": "mcb_1", "block_type": section, "topic_id": "mtp_1"}],
            "deletions": [{"id": "md_1"}] if section == "deletions" else [],
            "scope_bindings": [{"id": "msb_1"}] if section == "scope_bindings" else [],
            "projection_health": {"failed_jobs": 0},
        }

    monkeypatch.setattr(action_runtime, "_memory_actor_id", lambda **_: "assistant")
    monkeypatch.setattr(action_runtime, "_bounded_memory_payload", bounded_memory_payload)

    class Attempt:
        session_id = "ses_1"

    for capability_id, normalized_input, expected_section in [
        ("cap.memory.topics", {"limit": 7}, "topics"),
        ("cap.memory.hot_index", {"limit": 8}, "hot_index"),
        ("cap.memory.deletions", {"limit": 9}, "deletions"),
        ("cap.memory.scope_bindings", {"limit": 10}, "scope_bindings"),
    ]:
        output = action_runtime._execute_memory_capability(
            db=cast(Session, object()),
            capability_id=capability_id,
            normalized_input=normalized_input,
            action_attempt=cast(Any, Attempt()),
            now_fn=lambda: fixed_now,
            new_id_fn=lambda prefix: f"{prefix}_1",
        )
        assert output["status"] == "listed"
        assert output["memory"]["schema_version"] == "memory.sota.v2"
        assert calls[-1] == (expected_section, normalized_input["limit"])

    output = action_runtime._execute_memory_capability(
        db=cast(Session, object()),
        capability_id="cap.memory.context_blocks",
        normalized_input={"block_type": "topic", "limit": 11, "topic_id": "mtp_1"},
        action_attempt=cast(Any, Attempt()),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
    )
    assert output["status"] == "listed"
    assert calls[-1] == ("context_blocks", 100)

    event_calls: list[dict[str, Any]] = []

    def list_events(_: Any, **kwargs: Any) -> list[dict[str, Any]]:
        event_calls.append(kwargs)
        return [{"id": "mev_1"}]

    monkeypatch.setattr(action_runtime, "list_memory_events", list_events)
    events_output = action_runtime._execute_memory_capability(
        db=cast(Session, object()),
        capability_id="cap.memory.events",
        normalized_input={
            "scope_key": "project:phoenix",
            "event_type": "evt.memory.candidate_proposed",
            "since": "2026-04-01T00:00:00Z",
            "until": "2026-04-27T00:00:00Z",
            "limit": 12,
        },
        action_attempt=cast(Any, Attempt()),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
    )
    assert events_output["status"] == "listed"
    assert events_output["events"] == [{"id": "mev_1"}]
    assert event_calls[-1]["scope_key"] == "project:phoenix"
    assert event_calls[-1]["event_type"] == "evt.memory.candidate_proposed"
    assert event_calls[-1]["limit"] == 12

    # The registry validator accepts the optional filters and bounded limit, and
    # rejects an out-of-range limit, an unparseable timestamp, and unknown keys.
    events_capability = get_capability("cap.memory.events")
    assert events_capability is not None
    normalized, error = events_capability.validate_input(
        {
            "scope_key": "project:phoenix",
            "event_type": "evt.memory.candidate_proposed",
            "since": "2026-04-01T00:00:00Z",
            "until": "2026-04-27T00:00:00Z",
            "limit": 12,
        }
    )
    assert error is None
    assert normalized == {
        "scope_key": "project:phoenix",
        "event_type": "evt.memory.candidate_proposed",
        "since": "2026-04-01T00:00:00Z",
        "until": "2026-04-27T00:00:00Z",
        "limit": 12,
    }
    accepts_nulls, accepts_nulls_error = events_capability.validate_input(
        {"scope_key": None, "event_type": None, "since": None, "until": None, "limit": 1}
    )
    assert accepts_nulls_error is None
    assert accepts_nulls == {
        "scope_key": None,
        "event_type": None,
        "since": None,
        "until": None,
        "limit": 1,
    }
    for rejected in (
        {"scope_key": None, "event_type": None, "since": None, "until": None, "limit": 0},
        {"scope_key": None, "event_type": None, "since": None, "until": None, "limit": 201},
        {"scope_key": None, "event_type": None, "since": "not-a-time", "until": None, "limit": 5},
        {"scope_key": None, "event_type": None, "since": None, "until": None},
    ):
        rejected_normalized, rejected_error = events_capability.validate_input(rejected)
        assert rejected_normalized is None
        assert rejected_error == "schema_invalid"


def test_memory_runtime_handles_diagnostics_import_eval_and_projection_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    observed: dict[str, Any] = {}

    class Attempt:
        session_id = "ses_1"

    def build_context(
        db: Any,
        *,
        user_message: str,
        max_recalled_assertions: int,
        current_session_id: str | None,
        scope_key: str | None,
        actor_id: str | None,
        settings: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del settings
        observed["diagnostics"] = (
            db,
            user_message,
            max_recalled_assertions,
            current_session_id,
            scope_key,
            actor_id,
        )
        return (
            {
                "schema_version": "memory.sota.v2",
                "hot_index": [{"id": "mcb_hot"}],
                "topic_index": [],
                "semantic_assertions": [],
                "project_state": [],
                "procedural_memory": [],
                "action_traces": [],
                "conflicts": [],
                "memory_policy": {"reason": "normal"},
                "projection_health": {"failed_jobs": 1},
            },
            {"selected_memory_ids": ["mem_1"], "omitted_memories": []},
        )

    monkeypatch.setattr(action_runtime, "_memory_actor_id", lambda **_: "assistant")
    monkeypatch.setattr(action_runtime, "build_memory_context", build_context)

    def import_candidates(*_args: Any, **kwargs: Any) -> list[str]:
        observed["import"] = kwargs
        return ["mem_candidate_1"]

    def run_eval(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["eval"] = kwargs
        return {"id": "mer_1"}

    def retry_job(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["retry"] = kwargs
        return {"id": "mpj_1", "state": "pending"}

    monkeypatch.setattr(action_runtime, "import_memory_candidates", import_candidates)
    monkeypatch.setattr(action_runtime, "run_memory_eval", run_eval)
    monkeypatch.setattr(action_runtime, "retry_projection_job", retry_job)
    monkeypatch.setattr(
        action_runtime,
        "_bounded_memory_payload",
        lambda *_args, **_kwargs: {"schema_version": "memory.sota.v2"},
    )

    diagnostics = action_runtime._execute_memory_capability(
        db=cast(Session, object()),
        capability_id="cap.memory.recall_diagnostics",
        normalized_input={"query": "phoenix", "limit": 5, "scope_key": "project:phoenix"},
        action_attempt=cast(Any, Attempt()),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
    )
    assert diagnostics["status"] == "diagnosed"
    assert diagnostics["recall_diagnostics"]["selected_memory_ids"] == ["mem_1"]
    assert observed["diagnostics"][1:] == ("phoenix", 5, "ses_1", "project:phoenix", "assistant")

    candidate = {
        "subject_key": "project:phoenix",
        "predicate": "preference",
        "assertion_type": "preference",
        "value": "Use matte notebooks.",
        "evidence_text": "The user said to remember matte notebooks.",
        "confidence": 0.9,
        "scope_key": "global",
        "is_multi_valued": False,
        "valid_from": None,
        "valid_to": None,
    }
    imported = action_runtime._execute_memory_capability(
        db=cast(Session, object()),
        capability_id="cap.memory.import",
        normalized_input={"candidates": [candidate]},
        action_attempt=cast(Any, Attempt()),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        memory_import_cutover_enabled=True,
    )
    assert imported["status"] == "imported"
    assert observed["import"]["source_session_id"] == "ses_1"
    assert observed["import"]["candidates"] == [candidate]

    evaluated = action_runtime._execute_memory_capability(
        db=cast(Session, object()),
        capability_id="cap.memory.eval",
        normalized_input={
            "eval_name": "memory smoke",
            "cases": [
                {
                    "case_id": "case_1",
                    "query": "phoenix",
                    "expected": "remember phoenix",
                    "expected_memory_ids": ["mem_1"],
                    "forbidden_memory_ids": ["mem_2"],
                    "expected_kinds": ["semantic_assertion"],
                    "forbidden_texts": ["forbidden"],
                    "expect_policy_blocked": False,
                    "notes": None,
                }
            ],
        },
        action_attempt=cast(Any, Attempt()),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
    )
    assert evaluated == {"status": "evaluated", "eval": {"id": "mer_1"}}
    assert observed["eval"]["eval_name"] == "memory smoke"

    retried = action_runtime._execute_memory_capability(
        db=cast(Session, object()),
        capability_id="cap.memory.retry_projection_job",
        normalized_input={"job_id": "mpj_1"},
        action_attempt=cast(Any, Attempt()),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
    )
    assert retried["status"] == "queued"
    assert observed["retry"]["job_id"] == "mpj_1"


def test_memory_runtime_passes_scoped_mutation_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    observed: dict[str, Any] = {}

    class Attempt:
        session_id = "ses_1"

    monkeypatch.setattr(action_runtime, "_memory_actor_id", lambda **_: "assistant")
    monkeypatch.setattr(action_runtime, "emit_memory_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        action_runtime,
        "_bounded_memory_payload",
        lambda *_args, **_kwargs: {"schema_version": "memory.sota.v2"},
    )

    def export(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["export"] = kwargs
        return {"id": "mea_1"}

    def consolidate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["consolidate"] = kwargs
        return {"scope_key": kwargs["scope_key"]}

    def never_remember(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["never_remember"] = kwargs
        return {"scope_key": kwargs["scope_key"], "pattern": kwargs["pattern"]}

    def scope_mode(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed["scope_mode"] = kwargs
        return [
            {
                "event_type": "evt.memory.scope_binding_changed",
                "payload": {
                    "scope_key": kwargs["scope_key"],
                    "memory_mode": kwargs["memory_mode"],
                },
            }
        ]

    monkeypatch.setattr(action_runtime, "export_memory", export)
    monkeypatch.setattr(action_runtime, "consolidate_memory", consolidate)
    monkeypatch.setattr(action_runtime, "set_never_remember_rule", never_remember)
    monkeypatch.setattr(action_runtime, "set_memory_scope_binding", scope_mode)

    for capability_id, normalized_input, observed_key in [
        ("cap.memory.export", {"scope_key": "project:phoenix"}, "export"),
        ("cap.memory.consolidate", {"scope_key": "project:phoenix"}, "consolidate"),
        (
            "cap.memory.set_never_remember",
            {"scope_key": "project:phoenix", "rule": "do not store launch codes"},
            "never_remember",
        ),
        (
            "cap.memory.set_scope_mode",
            {
                "scope_type": "project",
                "scope_key": "project:phoenix",
                "memory_mode": "no_memory",
                "reason": "user request",
            },
            "scope_mode",
        ),
    ]:
        output = action_runtime._execute_memory_capability(
            db=cast(Session, object()),
            capability_id=capability_id,
            normalized_input=normalized_input,
            action_attempt=cast(Any, Attempt()),
            now_fn=lambda: fixed_now,
            new_id_fn=lambda prefix: f"{prefix}_1",
        )
        assert output["status"] in {"exported", "consolidated", "recorded"}
        assert observed[observed_key]["scope_key"] == "project:phoenix"


def test_memory_runtime_handles_candidate_and_priority_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    observed: dict[str, Any] = {}

    class Attempt:
        session_id = "ses_1"

    monkeypatch.setattr(action_runtime, "_memory_actor_id", lambda **_: "assistant")
    monkeypatch.setattr(action_runtime, "emit_memory_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        action_runtime,
        "_bounded_memory_payload",
        lambda *_args, **_kwargs: {"schema_version": "memory.sota.v2"},
    )

    def edit(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed["edit"] = kwargs
        return [{"event_type": "evt.memory.candidate_edited", "payload": {}}]

    def merge(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed["merge"] = kwargs
        return [{"event_type": "evt.memory.candidates_merged", "payload": {}}]

    def priority(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed[str(kwargs["priority"])] = kwargs
        return {"id": kwargs["assertion_id"], "priority": kwargs["priority"]}

    def stale(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed["stale"] = kwargs
        return [{"event_type": "evt.memory.assertion_marked_stale", "payload": {}}]

    monkeypatch.setattr(action_runtime, "edit_candidate", edit)
    monkeypatch.setattr(action_runtime, "merge_candidates", merge)
    monkeypatch.setattr(action_runtime, "set_assertion_priority", priority)
    monkeypatch.setattr(action_runtime, "mark_assertion_stale", stale)

    cases: list[tuple[str, dict[str, Any], str]] = [
        (
            "cap.memory.edit_candidate",
            {"assertion_id": "mas_1", "value": "new"},
            "edited",
        ),
        (
            "cap.memory.merge_candidates",
            {"assertion_ids": ["mas_1", "mas_2"]},
            "merged",
        ),
        ("cap.memory.prioritize", {"assertion_id": "mas_1"}, "prioritized"),
        ("cap.memory.deprioritize", {"assertion_id": "mas_1"}, "deprioritized"),
        (
            "cap.memory.mark_stale",
            {"assertion_id": "mas_1", "reason": "old"},
            "stale",
        ),
    ]
    for capability_id, normalized_input, expected_status in cases:
        output = action_runtime._execute_memory_capability(
            db=cast(Session, object()),
            capability_id=capability_id,
            normalized_input=normalized_input,
            action_attempt=cast(Any, Attempt()),
            now_fn=lambda: fixed_now,
            new_id_fn=lambda prefix: f"{prefix}_1",
        )
        assert output["status"] == expected_status

    assert observed["edit"]["actor_id"] == "assistant"
    assert observed["merge"]["assertion_ids"] == ["mas_1", "mas_2"]
    assert observed["pinned"]["assertion_id"] == "mas_1"
    assert observed["pinned"]["actor_id"] == "assistant"
    assert observed["deprioritized"]["assertion_id"] == "mas_1"
    assert observed["deprioritized"]["actor_id"] == "assistant"
    assert observed["stale"]["reason"] == "old"


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
        memory_import_cutover_enabled=False,
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
