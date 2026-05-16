from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
from ariel import action_runtime
from ariel.action_runtime import RuntimeProvenance, process_response_function_calls
from ariel.app import (
    ModelAdapterError,
    _POLICY_SYSTEM_INSTRUCTIONS,
    _call_tool_result_interpreter,
    _eligible_internal_callable_capability_ids,
)
from ariel.capability_registry import (
    capability_id_for_run_callable,
    internal_callable_capability_ids,
    run_callable_name_for_capability_id,
)
from ariel.executor import ExecutionResult
from ariel.persistence import TurnRecord
from ariel.run_runtime import parse_run_function_call, run_tool_definitions, unpack_run_source
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

    assert "cap.terminal.run" in capability_ids
    assert "cap.terminal.cancel" in capability_ids
    assert "cap.memory.search" in capability_ids
    assert "cap.memory.deprioritize" in capability_ids
    assert "cap.memory.eval" not in capability_ids
    assert "cap.attachment.read" not in capability_ids
    assert "cap.agency.run" not in capability_ids
    assert "cap.search.web" not in capability_ids
    assert "cap.maps.directions" not in capability_ids
    assert "cap.weather.forecast" not in capability_ids


def test_run_source_rejects_user_output_mixed_with_internal_calls() -> None:
    source = json.dumps(
        {
            "calls": [
                {"name": "agent.emit_message", "input": {"text": "Done."}},
                {
                    "name": "terminal.run",
                    "input": {"cwd": "/tmp", "command": "pwd", "purpose": "inspect cwd"},
                },
                {
                    "name": "memory.search",
                    "input": {"query": "project phoenix", "limit": 3, "scope_key": None},
                },
            ]
        }
    )

    effects = unpack_run_source(source)

    assert effects.errors == ["agent_emit_message_must_not_share_run_with_internal_calls"]
    assert effects.emitted_message == ""
    assert effects.paused is False
    assert effects.function_calls == []


def test_run_source_rejects_user_output_mixed_with_emit_value() -> None:
    effects = unpack_run_source(
        json.dumps(
            {
                "calls": [
                    {"name": "agent.emit_message", "input": {"text": "Done."}},
                    {"name": "agent.emit_value", "input": {"value": {"answer": 42}}},
                ]
            }
        )
    )

    assert effects.errors == ["agent_emit_message_must_not_share_run_with_emit_value"]
    assert effects.emitted_message == ""
    assert effects.emitted_values == []


def test_run_source_rejects_capability_ids_as_call_names() -> None:
    effects = unpack_run_source(
        json.dumps({"calls": [{"name": "cap.memory.search", "input": {"query": "x"}}]})
    )

    assert effects.errors == [
        "cap.memory.search: capability_ids_are_not_run_callables",
        "run_source_no_effect",
    ]


def test_run_source_supports_emit_value_and_terminal_background_calls() -> None:
    source = json.dumps(
        {
            "calls": [
                {"name": "agent.emit_value", "input": {"value": {"answer": 42}}},
                {
                    "name": "terminal.run_background",
                    "input": {"cwd": "/tmp", "command": "sleep 1", "purpose": "demo"},
                },
                {"name": "terminal.status", "input": {"command_id": "a" * 32}},
                {
                    "name": "terminal.read_output",
                    "input": {
                        "command_id": "a" * 32,
                        "stream": "stdout",
                        "offset": 0,
                        "limit": 100,
                    },
                },
            ]
        }
    )

    effects = unpack_run_source(source)

    assert effects.errors == []
    assert effects.emitted_values == [{"answer": 42}]
    assert [call["capability_id"] for call in effects.function_calls] == [
        "cap.terminal.run_background",
        "cap.terminal.status",
        "cap.terminal.read_output",
    ]


def test_run_source_rejects_malformed_emit_value() -> None:
    effects = unpack_run_source(
        json.dumps({"calls": [{"name": "agent.emit_value", "input": {"text": "nope"}}]})
    )

    assert "agent_emit_value_schema_invalid" in effects.errors


def test_run_source_enforces_source_and_call_budgets() -> None:
    assert parse_run_function_call(
        [{"name": "run", "arguments": json.dumps({"source": "x" * 20001})}]
    ) == (None, "run_source_too_large")
    effects = unpack_run_source(
        json.dumps(
            {
                "calls": [
                    {"name": "agent.emit_value", "input": {"value": index}} for index in range(21)
                ]
            }
        )
    )

    assert effects.errors == ["run_source_too_many_calls"]


def test_memory_runtime_handles_projection_read_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    calls: list[tuple[str, int]] = []

    def bounded_memory_payload(_: Any, *, section: str, limit: int) -> dict[str, Any]:
        calls.append((section, limit))
        return {
            "schema_version": "memory.sota.v1",
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
        assert output["memory"]["schema_version"] == "memory.sota.v1"
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
                "schema_version": "memory.sota.v1",
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
        lambda *_args, **_kwargs: {"schema_version": "memory.sota.v1"},
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
        lambda *_args, **_kwargs: {"schema_version": "memory.sota.v1"},
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
        lambda *_args, **_kwargs: {"schema_version": "memory.sota.v1"},
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


def test_tool_result_interpreter_failure_preserves_provider_response_id() -> None:
    class InvalidInterpreterAdapter:
        def create_response(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {
                "provider": "provider.test",
                "model": "model.test",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                "provider_response_id": "resp_interpreter_invalid",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{not-json"}],
                    }
                ],
            }

    with pytest.raises(ModelAdapterError) as exc_info:
        _call_tool_result_interpreter(
            model_adapter=cast(Any, InvalidInterpreterAdapter()),
            interpreter_input={
                "judgment_type": "tool_result_interpretation",
                "audited_tool_outputs": [],
            },
        )

    assert exc_info.value.code == "E_AI_JUDGMENT_INVALID_JSON"
    assert exc_info.value.provider == "provider.test"
    assert exc_info.value.model == "model.test"
    assert exc_info.value.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert exc_info.value.provider_response_id == "resp_interpreter_invalid"
    assert exc_info.value.parse_status == "invalid_json"
    assert exc_info.value.validation_status == "not_validated"
    assert exc_info.value.raw_output_shape == {
        "output_type": "list",
        "output_count": 1,
        "text_present": True,
    }


def test_tool_result_interpreter_success_preserves_provider_metadata() -> None:
    class InterpreterAdapter:
        def create_response(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {
                "provider": "provider.test",
                "model": "model.test",
                "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
                "provider_response_id": "resp_interpreter_valid",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "findings": ["result"],
                                        "contradictions": [],
                                        "uncertainty": [],
                                        "selected_output_refs": ["out_1"],
                                        "omitted_output_refs": [],
                                        "citation_refs": [],
                                        "artifact_refs": [],
                                        "recommended_next_evidence": [],
                                        "confidence": 0.8,
                                    },
                                    sort_keys=True,
                                ),
                            }
                        ],
                    }
                ],
            }

    result = _call_tool_result_interpreter(
        model_adapter=cast(Any, InterpreterAdapter()),
        interpreter_input={
            "judgment_type": "tool_result_interpretation",
            "audited_tool_outputs": [{"output_ref": "out_1"}],
        },
    )

    assert result["provider"] == "provider.test"
    assert result["model"] == "model.test"
    assert result["usage"] == {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}
    assert result["provider_response_id"] == "resp_interpreter_valid"
    assert result["response_output_shape"] == {
        "output_type": "list",
        "output_count": 1,
        "text_present": True,
    }


@pytest.mark.parametrize(
    "interpreter_output",
    [
        {
            "findings": [],
            "contradictions": [],
            "uncertainty": [],
            "selected_output_refs": [],
            "omitted_output_refs": [],
            "citation_refs": [],
            "artifact_refs": [],
            "recommended_next_evidence": [],
            "confidence": 0.8,
            "extra": "not allowed",
        },
        {
            "findings": [],
            "contradictions": [],
            "uncertainty": [],
            "selected_output_refs": ["missing_ref"],
            "omitted_output_refs": [],
            "citation_refs": [],
            "artifact_refs": [],
            "recommended_next_evidence": [],
            "confidence": 0.8,
        },
        {
            "findings": [],
            "contradictions": [],
            "uncertainty": [],
            "selected_output_refs": [],
            "omitted_output_refs": [],
            "citation_refs": [],
            "artifact_refs": [],
            "recommended_next_evidence": [],
            "confidence": 1.1,
        },
    ],
)
def test_tool_result_interpreter_rejects_non_contract_output(
    interpreter_output: dict[str, Any],
) -> None:
    class InterpreterAdapter:
        def create_response(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {
                "provider": "provider.test",
                "model": "model.test",
                "provider_response_id": "resp_interpreter_invalid_contract",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(interpreter_output, sort_keys=True),
                            }
                        ],
                    }
                ],
            }

    with pytest.raises(ModelAdapterError) as exc_info:
        _call_tool_result_interpreter(
            model_adapter=cast(Any, InterpreterAdapter()),
            interpreter_input={
                "judgment_type": "tool_result_interpretation",
                "audited_tool_outputs": [{"output_ref": "out_1"}],
                "citation_refs": [],
                "artifact_refs": [],
            },
        )

    assert exc_info.value.code == "E_AI_JUDGMENT_SCHEMA"
    assert exc_info.value.validation_status == "invalid"
    assert exc_info.value.provider_response_id == "resp_interpreter_invalid_contract"


def test_process_response_function_calls_default_denies_without_turn_scope() -> None:
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

    result = process_response_function_calls(
        db=cast(Session, Db()),
        session_id="ses_1",
        turn=turn,
        assistant_message="done",
        function_calls_raw=[
            {
                "call_id": "call_1",
                "capability_id": "cap.legacy.no_response",
                "input": {"reason": "nothing useful to add"},
                "influenced_by_untrusted_content": False,
            }
        ],
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        runtime_provenance=RuntimeProvenance(status="clean"),
    )

    assert result.action_attempts == []
    function_call_output = result.function_call_outputs[0]
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


def test_process_response_function_calls_denies_unscoped_tools() -> None:
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

    result = process_response_function_calls(
        db=cast(Session, Db()),
        session_id="ses_1",
        turn=turn,
        assistant_message="done",
        function_calls_raw=[
            {
                "call_id": "call_1",
                "capability_id": "cap.legacy.no_response",
                "input": {"reason": "nothing useful to add"},
            }
        ],
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=lambda _event_type, _payload: None,
        now_fn=lambda: fixed_now,
        new_id_fn=lambda prefix: f"{prefix}_1",
        allowed_capability_ids=[],
    )

    assert result.action_attempts == []
    assert json.loads(result.function_call_outputs[0]["output"]) == {
        "status": "denied",
        "capability_id": "cap.legacy.no_response",
        "error": "tool_not_in_turn_scope",
    }


def test_process_response_function_calls_executes_attachment_read_runtime() -> None:
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

    result = process_response_function_calls(
        db=cast(Session, Db()),
        session_id="ses_1",
        turn=turn,
        assistant_message="",
        function_calls_raw=[
            {
                "call_id": "call_1",
                "capability_id": "cap.attachment.read",
                "input": {"attachment_ref": "discord:777", "intent": "summarize"},
                "influenced_by_untrusted_content": False,
            }
        ],
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        now_fn=lambda: fixed_now,
        new_id_fn=new_id,
        runtime_provenance=RuntimeProvenance(status="clean"),
        attachment_runtime=cast(Any, AttachmentRuntime()),
        allowed_capability_ids=["cap.attachment.read"],
    )

    assert result.action_attempts[0].capability_id == "cap.attachment.read"
    assert result.action_attempts[0].status == "succeeded"
    assert json.loads(result.function_call_outputs[0]["output"])["output"]["blocks"] == [
        {"kind": "text", "text": "quarterly revenue increased"}
    ]
    assert result.runtime_provenance == RuntimeProvenance(
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
    tool_summary = json.loads(result.assistant_message)
    assert tool_summary["kind"] == "audited_tool_results"
    assert tool_summary["requires_model_final_answer"] is True
    assert tool_summary["retrieval"] == {
        "capability_ids": ["cap.attachment.read"],
        "errors": [],
        "requested": True,
        "source_count": 1,
        "sources": [
            {
                "artifact_id": "art_1",
                "published_at": None,
                "retrieved_at": "2026-04-27T12:00:00Z",
                "source": "discord://channel/1/message/2/attachment/777",
                "title": "report.txt",
            }
        ],
    }
    assert result.assistant_sources == tool_summary["retrieval"]["sources"]
    assert result.tool_result_interpreter_input is None
    assert result.tool_result_interpreter_output is None


@pytest.mark.parametrize(
    ("output_override", "expected_reason"),
    [
        (
            {"blocks": [{"kind": "text", "text": "x" * 7_000}]},
            "large",
        ),
        (
            {"modality": "image", "blocks": [{"kind": "ocr", "text": "visible text"}]},
            "modality_heavy",
        ),
    ],
)
def test_tool_outputs_requiring_interpretation_are_routed_without_raw_tool_output(
    output_override: dict[str, Any],
    expected_reason: str,
) -> None:
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

    base_output: dict[str, Any] = {
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
    }
    base_output.update(output_override)

    class AttachmentRuntime:
        def execute_read(self, **_: Any) -> ExecutionResult:
            return ExecutionResult(status="succeeded", output=base_output, error=None)

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

    result = process_response_function_calls(
        db=cast(Session, Db()),
        session_id="ses_1",
        turn=turn,
        assistant_message="",
        function_calls_raw=[
            {
                "call_id": "call_1",
                "capability_id": "cap.attachment.read",
                "input": {"attachment_ref": "discord:777", "intent": "summarize"},
                "influenced_by_untrusted_content": False,
            }
        ],
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        now_fn=lambda: fixed_now,
        new_id_fn=new_id,
        runtime_provenance=RuntimeProvenance(status="clean"),
        attachment_runtime=cast(Any, AttachmentRuntime()),
        allowed_capability_ids=["cap.attachment.read"],
    )

    tool_summary = json.loads(result.assistant_message)
    assert tool_summary["kind"] == "audited_tool_results"
    assert tool_summary["requires_model_final_answer"] is False
    assert tool_summary["tool_result_interpreter"]["required"] is True
    assert expected_reason in tool_summary["tool_result_interpreter"]["reason_codes"]
    assert "attachment content:" not in result.assistant_message
    assert "quarterly revenue increased [1]" not in result.assistant_message

    assert result.tool_result_interpreter_input is not None
    assert result.tool_result_interpreter_output is None
    interpreter_input = result.tool_result_interpreter_input
    assert interpreter_input["judgment_type"] == "tool_result_interpretation"
    assert interpreter_input["action_attempt_ids"] == ["aat_1"]
    assert expected_reason in interpreter_input["reason_codes"]
    assert interpreter_input["audited_tool_outputs"][0]["output_ref"] == "aat_1"
    assert interpreter_input["output_contract"] == {
        "artifact_refs": [],
        "citation_refs": [],
        "confidence": None,
        "contradictions": [],
        "findings": [],
        "omitted_output_refs": [],
        "recommended_next_evidence": [],
        "selected_output_refs": [],
        "uncertainty": [],
    }

    function_call_output = json.loads(result.function_call_outputs[0]["output"])
    assert function_call_output == {
        "action_attempt_id": "aat_1",
        "capability_id": "cap.attachment.read",
        "status": "succeeded",
        "tool_result_interpreter": {
            "output": None,
            "output_ref": "aat_1",
            "reason_codes": interpreter_input["audited_tool_outputs"][0]["reason_codes"],
            "required": True,
        },
    }
