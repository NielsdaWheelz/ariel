from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, cast

from ariel.action_runtime import RuntimeProvenance, process_response_function_calls
from ariel.capability_registry import (
    capability_id_for_response_tool_name,
    response_tool_definitions,
    response_tool_name_for_capability_id,
)
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


def test_process_response_function_calls_returns_function_call_output_for_inline_capability() -> None:
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
    assert "action result (cap.framework.read_echo)" in result.assistant_message
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
