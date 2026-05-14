from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
from ariel import action_runtime
from ariel.action_runtime import RuntimeProvenance, process_response_function_calls
from ariel.app import ModelAdapterError, _call_tool_result_interpreter, _call_tool_strategy
from ariel.capability_registry import (
    capability_id_for_response_tool_name,
    get_capability,
    production_response_capability_ids,
    response_tool_definitions,
    response_tool_name_for_capability_id,
)
from ariel.executor import ExecutionResult
from ariel.persistence import TurnRecord
from sqlalchemy.orm import Session


def _production_response_tool_definitions() -> list[dict[str, Any]]:
    return response_tool_definitions(production_response_capability_ids())


def test_response_tool_schemas_are_strict_objects() -> None:
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

    for tool in _production_response_tool_definitions():
        assert tool["type"] == "function"
        assert tool["strict"] is True
        assert_strict_object_schema(tool["parameters"], tool["name"])


def test_response_tool_names_round_trip_without_dotted_names() -> None:
    for tool in _production_response_tool_definitions():
        tool_name = tool["name"]
        capability_id = capability_id_for_response_tool_name(tool_name)

        assert "." not in tool_name
        assert capability_id is not None
        assert response_tool_name_for_capability_id(capability_id) == tool_name


def test_production_response_tools_exclude_framework_fixtures() -> None:
    for tool in _production_response_tool_definitions():
        capability_id = capability_id_for_response_tool_name(tool["name"])
        assert capability_id is not None
        assert not capability_id.startswith("cap.framework.")


def test_framework_fixture_tools_cannot_be_exposed_explicitly() -> None:
    assert get_capability("cap.framework.read_echo") is None
    with pytest.raises(RuntimeError, match="unknown Responses capability"):
        response_tool_definitions(["cap.framework.read_echo"])


def test_tool_strategy_uses_no_tools_and_accepts_valid_selected_ids() -> None:
    class StrategyAdapter:
        provider = "provider.strategy-test"
        model = "model.strategy-test"

        def create_response(
            self,
            *,
            input_items: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            user_message: str,
            history: list[dict[str, Any]],
            context_bundle: dict[str, Any],
        ) -> dict[str, Any]:
            del user_message, history, context_bundle
            assert tools == []
            assert input_items[0]["role"] == "system"
            strategy_input = json.loads(str(input_items[1]["content"]))
            assert "eligible_tools" not in strategy_input
            assert strategy_input["available_capability_families"] == [
                {
                    "family": "email",
                    "description": "Gmail search, read, drafts, approved sends, and mail organization.",
                    "capability_ids": ["cap.email.send"],
                },
                {
                    "family": "memory",
                    "description": (
                        "Memory inspection, recall diagnostics, policy, mutation, "
                        "consolidation, and export."
                    ),
                    "capability_ids": ["cap.memory.search"],
                },
            ]
            assert strategy_input["runtime_facts"] == {"google": {"connected": False}}
            assert strategy_input["bounded_context"] == {"case": "unit"}
            return {
                "provider": self.provider,
                "model": self.model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "decision": "selected_tools",
                                        "selected_capability_ids": [
                                            "cap.memory.search",
                                            "cap.email.send",
                                        ],
                                        "rationale": "Need search and send.",
                                        "unavailable_reason": None,
                                        "confidence": 0.8,
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }

    selected, _, parsed = _call_tool_strategy(
        model_adapter=StrategyAdapter(),  # type: ignore[arg-type]
        user_message="Find the note and email it.",
        context_bundle={"case": "unit"},
        tool_surface_facts={"google": {"connected": False}},
        eligible_capability_ids=["cap.memory.search", "cap.email.send"],
    )

    assert selected == ["cap.memory.search", "cap.email.send"]
    assert parsed["rationale"] == "Need search and send."


@pytest.mark.parametrize(
    ("selected_capability_ids", "eligible_capability_ids", "expected_code", "expected_reason"),
    [
        (
            ["cap.framework.read_echo"],
            ["cap.memory.search"],
            "E_AI_JUDGMENT_VALIDATION",
            "ineligible capability",
        ),
        (
            ["cap.memory.search", "cap.memory.search"],
            ["cap.memory.search"],
            "E_AI_JUDGMENT_VALIDATION",
            "duplicate capability",
        ),
        (
            [
                "cap.memory.inspect",
                "cap.memory.search",
                "cap.memory.propose",
                "cap.memory.review",
                "cap.memory.correct",
                "cap.memory.retract",
                "cap.memory.delete",
                "cap.memory.privacy_delete",
                "cap.memory.redact_evidence",
            ],
            [
                "cap.memory.inspect",
                "cap.memory.search",
                "cap.memory.propose",
                "cap.memory.review",
                "cap.memory.correct",
                "cap.memory.retract",
                "cap.memory.delete",
                "cap.memory.privacy_delete",
                "cap.memory.redact_evidence",
            ],
            "E_AI_JUDGMENT_SCHEMA",
            "schema validation",
        ),
    ],
)
def test_tool_strategy_rejects_invalid_selected_ids(
    selected_capability_ids: list[str],
    eligible_capability_ids: list[str],
    expected_code: str,
    expected_reason: str,
) -> None:
    class StrategyAdapter:
        provider = "provider.strategy-test"
        model = "model.strategy-test"

        def create_response(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {
                "provider": self.provider,
                "model": self.model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "decision": "selected_tools",
                                        "selected_capability_ids": selected_capability_ids,
                                        "rationale": "bad selection",
                                        "unavailable_reason": None,
                                        "confidence": 0.8,
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }

    with pytest.raises(ModelAdapterError) as exc_info:
        _call_tool_strategy(
            model_adapter=StrategyAdapter(),  # type: ignore[arg-type]
            user_message="Find the note.",
            context_bundle={},
            tool_surface_facts={},
            eligible_capability_ids=eligible_capability_ids,
        )

    assert exc_info.value.code == expected_code
    assert expected_reason in exc_info.value.safe_reason
    assert exc_info.value.validation_status == "invalid"


def test_tool_strategy_rejects_schema_invalid_json_object() -> None:
    class StrategyAdapter:
        provider = "provider.strategy-test"
        model = "model.strategy-test"

        def create_response(
            self,
            *,
            input_items: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            user_message: str,
            history: list[dict[str, Any]],
            context_bundle: dict[str, Any],
        ) -> dict[str, Any]:
            del input_items, tools, user_message, history, context_bundle
            return {
                "provider": self.provider,
                "model": self.model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"selected_capability_ids": "cap.memory.search"}
                                ),
                            }
                        ],
                    }
                ],
            }

    with pytest.raises(ModelAdapterError) as exc_info:
        _call_tool_strategy(
            model_adapter=StrategyAdapter(),  # type: ignore[arg-type]
            user_message="Find the note.",
            context_bundle={},
            tool_surface_facts={},
            eligible_capability_ids=["cap.memory.search"],
        )

    assert exc_info.value.code == "E_AI_JUDGMENT_SCHEMA"
    assert exc_info.value.validation_status == "invalid"


def test_attachment_read_response_tool_contract_is_strict() -> None:
    tool_name = response_tool_name_for_capability_id("cap.attachment.read")
    tools_by_name = {tool["name"]: tool for tool in _production_response_tool_definitions()}

    assert capability_id_for_response_tool_name(tool_name) == "cap.attachment.read"
    assert tool_name in tools_by_name

    tool = tools_by_name[tool_name]
    assert tool["strict"] is True
    assert tool["parameters"] == {
        "type": "object",
        "properties": {
            "attachment_ref": {"type": "string", "maxLength": 256},
            "intent": {
                "type": "string",
                "enum": ["summarize", "ocr", "transcribe", "extract_text", "answer"],
            },
        },
        "required": ["attachment_ref", "intent"],
        "additionalProperties": False,
    }


def test_memory_response_tools_are_exposed_to_the_model() -> None:
    expected_capability_ids = {
        "cap.memory.inspect",
        "cap.memory.search",
        "cap.memory.recall_diagnostics",
        "cap.memory.propose",
        "cap.memory.review",
        "cap.memory.edit_candidate",
        "cap.memory.merge_candidates",
        "cap.memory.correct",
        "cap.memory.retract",
        "cap.memory.delete",
        "cap.memory.privacy_delete",
        "cap.memory.redact_evidence",
        "cap.memory.set_never_remember",
        "cap.memory.set_scope_mode",
        "cap.memory.resolve_conflict",
        "cap.memory.prioritize",
        "cap.memory.deprioritize",
        "cap.memory.mark_stale",
        "cap.memory.consolidate",
        "cap.memory.export",
    }
    tools_by_name = {tool["name"]: tool for tool in _production_response_tool_definitions()}

    assert {
        capability_id
        for name in tools_by_name
        if (capability_id := capability_id_for_response_tool_name(name)) is not None
        and capability_id.startswith("cap.memory.")
    } == expected_capability_ids
    for capability_id in expected_capability_ids:
        capability = get_capability(capability_id)
        assert capability is not None
        assert response_tool_name_for_capability_id(capability_id) in tools_by_name
        assert capability.allowed_egress_destinations == ()

    inspect_capability = get_capability("cap.memory.inspect")
    search_capability = get_capability("cap.memory.search")
    assert inspect_capability is not None
    assert search_capability is not None
    assert inspect_capability.policy_decision == "allow_inline"
    assert search_capability.policy_decision == "allow_inline"
    for capability_id in expected_capability_ids:
        capability = get_capability(capability_id)
        assert capability is not None
        if capability.impact_level != "read":
            assert capability.policy_decision == "requires_approval"


def test_memory_import_response_tool_is_not_model_visible() -> None:
    default_tool_names = {tool["name"] for tool in _production_response_tool_definitions()}
    import_tool_name = response_tool_name_for_capability_id("cap.memory.import")

    assert import_tool_name not in default_tool_names
    import_capability = get_capability("cap.memory.import")
    assert import_capability is not None
    assert import_capability.policy_decision == "requires_approval"


def test_missing_memory_surface_tools_have_explicit_contracts() -> None:
    tools_by_name = {tool["name"]: tool for tool in _production_response_tool_definitions()}

    inspect_tool = tools_by_name[response_tool_name_for_capability_id("cap.memory.inspect")]
    inspect_capability = get_capability("cap.memory.inspect")
    assert inspect_capability is not None
    for section in inspect_tool["parameters"]["properties"]["section"]["enum"]:
        assert inspect_capability.validate_input({"section": section, "limit": 1})[1] is None

    recall_tool = tools_by_name[
        response_tool_name_for_capability_id("cap.memory.recall_diagnostics")
    ]
    assert recall_tool["parameters"]["properties"] == {
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "scope_key": {"type": ["string", "null"], "minLength": 1, "maxLength": 200},
    }

    context_tool = response_tool_definitions(["cap.memory.context_blocks"])[0]
    assert context_tool["parameters"]["properties"]["block_type"]["enum"] == [
        "all",
        "hot_index",
        "topic",
        "pinned_core",
        "project_state",
        "procedure",
        "episodic",
        "reasoning",
    ]
    assert context_tool["parameters"]["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    }
    assert context_tool["parameters"]["properties"]["topic_id"] == {
        "type": ["string", "null"],
        "minLength": 1,
        "maxLength": 32,
    }

    scope_mode_tool = tools_by_name[
        response_tool_name_for_capability_id("cap.memory.set_scope_mode")
    ]
    assert scope_mode_tool["parameters"]["properties"] == {
        "scope_type": {
            "type": "string",
            "enum": ["user", "project", "repo", "session", "thread", "proactive_case"],
        },
        "scope_key": {"type": "string", "minLength": 1, "maxLength": 200},
        "memory_mode": {"type": "string", "enum": ["normal", "temporary", "no_memory"]},
        "reason": {"type": ["string", "null"], "maxLength": 500},
    }

    import_tool = response_tool_definitions(["cap.memory.import"])[0]
    candidate_schema = import_tool["parameters"]["properties"]["candidates"]["items"]
    assert set(candidate_schema["properties"]) == {
        "subject_key",
        "predicate",
        "assertion_type",
        "value",
        "evidence_text",
        "confidence",
        "scope_key",
        "is_multi_valued",
        "valid_from",
        "valid_to",
    }

    eval_tool = response_tool_definitions(["cap.memory.eval"])[0]
    assert set(eval_tool["parameters"]["properties"]["cases"]["items"]["properties"]) == {
        "case_id",
        "query",
        "expected",
        "expected_memory_ids",
        "forbidden_memory_ids",
        "expected_kinds",
        "forbidden_texts",
        "expect_policy_blocked",
        "notes",
    }

    retry_tool = response_tool_definitions(["cap.memory.retry_projection_job"])[0]
    assert retry_tool["parameters"]["properties"] == {
        "job_id": {"type": "string", "minLength": 1, "maxLength": 32}
    }


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

    def scope_mode(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["scope_mode"] = kwargs
        return {"scope_key": kwargs["scope_key"], "memory_mode": kwargs["memory_mode"]}

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
                "type": "function_call",
                "call_id": "call_1",
                "name": response_tool_name_for_capability_id("cap.discord.no_response"),
                "arguments": json.dumps({"reason": "nothing useful to add"}),
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
        "capability_id": "cap.discord.no_response",
        "error": "tool_not_in_turn_scope",
    }
    assert events == [
        (
            "evt.action.call_denied",
            {
                "call_index": 1,
                "call_id": "call_1",
                "tool_name": "cap_discord_no_response",
                "capability_id": "cap.discord.no_response",
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
                "type": "function_call",
                "call_id": "call_1",
                "name": response_tool_name_for_capability_id("cap.discord.no_response"),
                "arguments": json.dumps({"reason": "nothing useful to add"}),
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
        "capability_id": "cap.discord.no_response",
        "error": "tool_not_in_turn_scope",
    }


def test_process_response_function_calls_treats_discord_no_response_as_silent() -> None:
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

    def new_id(prefix: str) -> str:
        id_counts[prefix] = id_counts.get(prefix, 0) + 1
        return f"{prefix}_{id_counts[prefix]}"

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
        assistant_message="",
        function_calls_raw=[
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": response_tool_name_for_capability_id("cap.discord.no_response"),
                "arguments": json.dumps({"reason": "nothing useful to add"}),
                "influenced_by_untrusted_content": False,
            }
        ],
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        now_fn=lambda: fixed_now,
        new_id_fn=new_id,
        runtime_provenance=RuntimeProvenance(status="clean"),
        allowed_capability_ids=["cap.discord.no_response"],
    )

    assert result.silent_response is True
    assert result.assistant_message == ""
    assert json.loads(result.function_call_outputs[0]["output"]) == {
        "status": "succeeded",
        "capability_id": "cap.discord.no_response",
        "output": {"reason": "nothing useful to add"},
    }
    assert result.action_attempts[0].capability_id == "cap.discord.no_response"
    assert result.action_attempts[0].status == "succeeded"


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
                "type": "function_call",
                "call_id": "call_1",
                "name": response_tool_name_for_capability_id("cap.attachment.read"),
                "arguments": json.dumps({"attachment_ref": "discord:777", "intent": "summarize"}),
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
                "type": "function_call",
                "call_id": "call_1",
                "name": response_tool_name_for_capability_id("cap.attachment.read"),
                "arguments": json.dumps({"attachment_ref": "discord:777", "intent": "summarize"}),
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
