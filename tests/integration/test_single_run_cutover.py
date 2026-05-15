from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ariel.app import ModelAdapter, create_app
from ariel.persistence import AIJudgmentRecord, TerminalCommandRecord
from tests.integration.responses_helpers import responses_message, responses_run_message


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _run_response(
    *,
    calls: list[dict[str, Any]],
    provider: str,
    model: str,
    provider_response_id: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "function_call",
                "id": "fc_run_test",
                "call_id": "call_run_test",
                "name": "run",
                "arguments": json.dumps(
                    {"source": json.dumps({"calls": calls}, sort_keys=True)},
                    sort_keys=True,
                ),
                "status": "completed",
            }
        ],
    }


def _direct_function_response(
    *,
    function_calls: list[dict[str, Any]],
    provider: str,
    model: str,
    provider_response_id: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "provider_response_id": provider_response_id,
        "output": function_calls,
    }


@dataclass
class CapturingRunAdapter:
    provider: str = "provider.single-run"
    model: str = "model.single-run-v1"
    responses: list[dict[str, Any]] = field(default_factory=list)
    tools_seen: list[list[dict[str, Any]]] = field(default_factory=list)
    input_items_seen: list[list[dict[str, Any]]] = field(default_factory=list)

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
        self.tools_seen.append(tools)
        self.input_items_seen.append(input_items)
        return self.responses.pop(0)


def _event_types(payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in payload["turn"]["events"]]


def test_normal_turn_exposes_only_strict_run_tool(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            responses_run_message(
                assistant_text="done",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_run_only",
            )
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "hello"})

    assert sent.status_code == 200
    assert sent.json()["assistant"]["message"] == "done"
    assert len(adapter.tools_seen) == 1
    assert [tool["name"] for tool in adapter.tools_seen[0]] == ["run"]
    assert adapter.tools_seen[0][0]["strict"] is True
    rendered_input = json.dumps(adapter.input_items_seen[0])
    assert "eligible run callables for this turn:" in rendered_input
    assert "terminal.run" in rendered_input
    assert "runtime facts:" in rendered_input


def test_plain_assistant_text_is_protocol_feedback_not_visible(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            responses_message(
                assistant_text="this must stay hidden",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_plain_text",
            ),
            responses_run_message(
                assistant_text="visible through run",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_retry_visible",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "hello"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "visible through run"
    assert "this must stay hidden" not in payload["assistant"]["message"]
    assert "this must stay hidden" not in json.dumps(adapter.input_items_seen[-1])
    assert "evt.model.protocol_failed" in _event_types(payload)
    engine = create_engine(postgres_url, future=True)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    with session_factory() as db:
        judgment = db.scalar(
            select(AIJudgmentRecord).where(
                AIJudgmentRecord.provider_response_id == "resp_plain_text",
                AIJudgmentRecord.status == "failed",
            )
        )
        assert judgment is not None


@pytest.mark.parametrize(
    "first_response",
    [
        _direct_function_response(
            function_calls=[
                {
                    "type": "function_call",
                    "id": "fc_wrong_tool",
                    "call_id": "call_wrong_tool",
                    "name": "cap.terminal.run",
                    "arguments": json.dumps({"cwd": ".", "command": "pwd"}),
                    "status": "completed",
                }
            ],
            provider="provider.single-run",
            model="model.single-run-v1",
            provider_response_id="resp_wrong_direct_tool",
        ),
        _direct_function_response(
            function_calls=[
                {
                    "type": "function_call",
                    "id": "fc_run_one",
                    "call_id": "call_run_one",
                    "name": "run",
                    "arguments": json.dumps({"source": json.dumps({"calls": []})}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "fc_run_two",
                    "call_id": "call_run_two",
                    "name": "run",
                    "arguments": json.dumps({"source": json.dumps({"calls": []})}),
                    "status": "completed",
                },
            ],
            provider="provider.single-run",
            model="model.single-run-v1",
            provider_response_id="resp_multiple_direct_tools",
        ),
        _direct_function_response(
            function_calls=[
                {
                    "type": "function_call",
                    "id": "fc_bad_args",
                    "call_id": "call_bad_args",
                    "name": "run",
                    "arguments": json.dumps({"source": 7}),
                    "status": "completed",
                }
            ],
            provider="provider.single-run",
            model="model.single-run-v1",
            provider_response_id="resp_invalid_run_arguments",
        ),
    ],
)
def test_invalid_direct_tool_protocol_retries_without_executing(
    postgres_url: str, first_response: dict[str, Any]
) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            first_response,
            responses_run_message(
                assistant_text="recovered",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_protocol_recovered",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "hello"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "recovered"
    assert "evt.model.protocol_failed" in _event_types(payload)
    assert payload["turn"]["surface_action_lifecycle"] == []
    assert any(item.get("type") == "function_call_output" for item in adapter.input_items_seen[-1])


def test_invalid_run_source_retries_without_executing_bad_call(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            _run_response(
                calls=[{"name": "does.not.exist", "input": {}}],
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_bad_source",
            ),
            responses_run_message(
                assistant_text="recovered",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_recovered",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "hello"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "recovered"
    assert "evt.run.validation_failed" in _event_types(payload)
    assert payload["turn"]["surface_action_lifecycle"] == []


def test_capability_ids_inside_run_source_are_feedback_not_actions(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            _run_response(
                calls=[
                    {
                        "name": "cap.terminal.run",
                        "input": {"cwd": "/tmp", "command": "pwd", "purpose": "legacy call"},
                    }
                ],
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_capability_id_source",
            ),
            responses_run_message(
                assistant_text="recovered",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_capability_id_recovered",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "hello"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "recovered"
    assert payload["turn"]["surface_action_lifecycle"] == []
    assert "evt.run.validation_failed" in _event_types(payload)
    assert "capability_ids_are_not_run_callables" in json.dumps(adapter.input_items_seen[-1])


def test_pause_until_input_ends_turn_without_visible_output(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            _run_response(
                calls=[{"name": "agent.pause_until_input", "input": {}}],
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_pause",
            )
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "wait"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == ""
    assert payload["assistant"]["silent"] is True
    assert payload["turn"]["surface_action_lifecycle"] == []


def test_emit_value_is_internal_feedback_with_digest_surface(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            _run_response(
                calls=[{"name": "agent.emit_value", "input": {"value": {"answer": 42}}}],
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_value",
            ),
            responses_run_message(
                assistant_text="value handled",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_value_final",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "compute"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "value handled"
    value_events = [
        event
        for event in payload["turn"]["events"]
        if event["event_type"] == "evt.agent.value_emitted"
    ]
    assert len(value_events) == 1
    assert "value" not in value_events[0]["payload"]
    assert len(value_events[0]["payload"]["value_digest"]) == 64
    assert value_events[0]["payload"]["value_bytes"] > 0
    value_feedback = [
        json.loads(item["output"])
        for item in adapter.input_items_seen[-1]
        if item.get("type") == "function_call_output"
    ]
    assert value_feedback[0]["emitted_values"] == [{"answer": 42}]


def test_approval_required_run_surfaces_host_approval_text(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            _run_response(
                calls=[
                    {
                        "name": "terminal.run",
                        "input": {
                            "cwd": str(Path.cwd()),
                            "command": "touch approval-required-test",
                            "purpose": "request approval",
                        },
                    }
                ],
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_approval",
            )
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "make a file"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "approval required. review the pending action."
    lifecycle = payload["turn"]["surface_action_lifecycle"]
    assert lifecycle[0]["proposal"]["capability_id"] == "cap.terminal.run"
    assert lifecycle[0]["policy"]["decision"] == "requires_approval"
    assert lifecycle[0]["approval"]["status"] == "pending"


def test_terminal_run_result_is_fed_back_before_final_answer(
    postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", str(tmp_path / "terminal"))

    @dataclass
    class TerminalAdapter:
        provider: str = "provider.terminal"
        model: str = "model.terminal-v1"
        tools_seen: list[list[dict[str, Any]]] = field(default_factory=list)
        terminal_outputs_seen: list[dict[str, Any]] = field(default_factory=list)

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
            self.tools_seen.append(tools)
            for item in input_items:
                if item.get("type") != "function_call_output":
                    continue
                parsed = json.loads(item["output"])
                outputs = parsed.get("internal_call_outputs")
                if isinstance(outputs, list) and outputs:
                    output = outputs[0].get("output")
                    if isinstance(output, dict):
                        self.terminal_outputs_seen.append(output)
            if self.terminal_outputs_seen:
                stdout = self.terminal_outputs_seen[-1]["output"]["stdout"].strip()
                return responses_run_message(
                    assistant_text=f"terminal cwd {stdout}",
                    provider=self.provider,
                    model=self.model,
                    provider_response_id="resp_terminal_final",
                )
            return _run_response(
                calls=[
                    {
                        "name": "terminal.run",
                        "input": {
                            "cwd": str(Path.cwd()),
                            "command": "pwd",
                            "purpose": "verify terminal feedback",
                        },
                    }
                ],
                provider=self.provider,
                model=self.model,
                provider_response_id="resp_terminal_run",
            )

    adapter = TerminalAdapter()
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "run pwd"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == f"terminal cwd {Path.cwd()}"
    assert adapter.terminal_outputs_seen
    terminal_output = adapter.terminal_outputs_seen[0]
    assert terminal_output["status"] == "succeeded"
    assert terminal_output["output"]["stdout"].strip() == str(Path.cwd())
    assert terminal_output["output"]["stdout_ref"].endswith("/stdout.txt")
    lifecycle = payload["turn"]["surface_action_lifecycle"]
    assert lifecycle[0]["proposal"]["capability_id"] == "cap.terminal.run"
    assert lifecycle[0]["policy"]["decision"] == "allow_inline"
    assert lifecycle[0]["execution"]["status"] == "succeeded"

    engine = create_engine(postgres_url, future=True)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    try:
        with session_factory() as db:
            terminal_record = db.scalar(
                select(TerminalCommandRecord)
                .where(
                    TerminalCommandRecord.session_id == session_id,
                    TerminalCommandRecord.command_id == terminal_output["output"]["command_id"],
                )
                .limit(1)
            )
    finally:
        engine.dispose()
    assert terminal_record is not None
    assert terminal_record.action_attempt_id == lifecycle[0]["action_attempt_id"]
    assert terminal_record.kind == "foreground"
    assert terminal_record.status == "completed"
    assert terminal_record.exit_code == 0
    assert terminal_record.stdout_path.endswith("/stdout.txt")
    assert terminal_record.stderr_path.endswith("/stderr.txt")
