from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
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
                    "description": ("Memory inspection, proposal, review, correction, and export."),
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
        "cap.memory.propose",
        "cap.memory.review",
        "cap.memory.correct",
        "cap.memory.retract",
        "cap.memory.delete",
        "cap.memory.privacy_delete",
        "cap.memory.redact_evidence",
        "cap.memory.set_never_remember",
        "cap.memory.resolve_conflict",
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
    export_capability = get_capability("cap.memory.export")
    review_capability = get_capability("cap.memory.review")
    assert inspect_capability is not None
    assert search_capability is not None
    assert export_capability is not None
    assert review_capability is not None
    assert inspect_capability.policy_decision == "allow_inline"
    assert search_capability.policy_decision == "allow_inline"
    assert export_capability.policy_decision == "allow_inline"
    assert review_capability.policy_decision == "requires_approval"


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
