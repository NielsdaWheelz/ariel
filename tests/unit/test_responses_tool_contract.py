from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest
from ariel.action_runtime import RuntimeProvenance, process_response_function_calls
from ariel.app import ModelAdapterError, _call_tool_result_interpreter
from ariel.capability_registry import (
    capability_id_for_response_tool_name,
    response_tool_definitions,
    response_tool_name_for_capability_id,
)
from ariel.executor import ExecutionResult
from ariel.persistence import TurnRecord
from sqlalchemy.orm import Session


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

    for tool in response_tool_definitions():
        assert tool["type"] == "function"
        assert tool["strict"] is True
        assert_strict_object_schema(tool["parameters"], tool["name"])


def test_response_tool_names_round_trip_without_dotted_names() -> None:
    for tool in response_tool_definitions():
        tool_name = tool["name"]
        capability_id = capability_id_for_response_tool_name(tool_name)

        assert "." not in tool_name
        assert capability_id is not None
        assert response_tool_name_for_capability_id(capability_id) == tool_name


def test_attachment_read_response_tool_contract_is_strict() -> None:
    tool_name = response_tool_name_for_capability_id("cap.attachment.read")
    tools_by_name = {tool["name"]: tool for tool in response_tool_definitions()}

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
            "audited_tool_outputs": [],
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


def test_process_response_function_calls_returns_function_call_output_for_inline_capability() -> (
    None
):
    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    added_records: list[Any] = []
    events: list[tuple[str, dict[str, Any]]] = []
    id_counts: dict[str, int] = {}

    class Db:
        def add(self, record: Any) -> None:
            added_records.append(record)

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
                "name": response_tool_name_for_capability_id("cap.framework.read_echo"),
                "arguments": json.dumps({"text": " hello "}),
                "influenced_by_untrusted_content": False,
            }
        ],
        approval_ttl_seconds=300,
        approval_actor_id="usr_1",
        add_event=lambda event_type, payload: events.append((event_type, payload)),
        now_fn=lambda: fixed_now,
        new_id_fn=new_id,
        runtime_provenance=RuntimeProvenance(status="clean"),
    )

    assert len(result.function_call_outputs) == 1
    function_call_output = result.function_call_outputs[0]
    assert function_call_output["type"] == "function_call_output"
    assert function_call_output["call_id"] == "call_1"
    assert json.loads(function_call_output["output"]) == {
        "status": "succeeded",
        "capability_id": "cap.framework.read_echo",
        "output": {"text": "hello"},
    }
    assert result.action_attempts[0].capability_id == "cap.framework.read_echo"
    assert result.action_attempts[0].proposed_input == {"text": "hello"}
    assert result.action_attempts[0].status == "succeeded"
    tool_summary = json.loads(result.assistant_message)
    assert tool_summary == {
        "action_attempts": [
            {
                "action_attempt_id": "aat_1",
                "approval_required": False,
                "capability_id": "cap.framework.read_echo",
                "has_execution_output": True,
                "policy_decision": "allow_inline",
                "status": "succeeded",
            }
        ],
        "blocked_reasons": [],
        "inline_result_count": 1,
        "kind": "audited_tool_results",
        "pending_approvals": [],
        "requires_model_final_answer": True,
        "retrieval": {
            "capability_ids": [],
            "errors": [],
            "requested": False,
            "source_count": 0,
            "sources": [],
        },
    }
    assert result.tool_result_interpreter_input is None
    assert result.tool_result_interpreter_output is None
    assert "response_function_call" not in events[0][1]


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
            {
                "results": [
                    {
                        "title": "report one",
                        "source": "discord://channel/1/message/2/attachment/777",
                        "snippet": "source one",
                        "published_at": None,
                    },
                    {
                        "title": "report two",
                        "source": "discord://channel/1/message/2/attachment/888",
                        "snippet": "source two",
                        "published_at": None,
                    },
                ]
            },
            "multi_source",
        ),
        (
            {"contradictions": [{"claim": "structured conflict signal"}]},
            "contradictory",
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
