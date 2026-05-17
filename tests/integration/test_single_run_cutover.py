from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import ariel.run_runtime as run_runtime_module
from ariel.action_runtime import RuntimeProvenance
from ariel.app import ModelAdapter, create_app
from ariel.persistence import ActionAttemptRecord, AIJudgmentRecord, TurnRecord
from ariel.policy_engine import evaluate_proposal
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    responses_message,
    responses_run_message,
    responses_with_run_calls,
)


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
        reset_database=True,
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _program_response(
    *,
    source: str,
    provider: str,
    model: str,
    provider_response_id: str,
) -> dict[str, Any]:
    """A model response whose single ``run`` call carries a Python program."""

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
                "arguments": json.dumps({"source": source}, sort_keys=True),
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
    # The run tool's source is described to the model as a Python program.
    assert "Python program" in rendered_input or "run program" in rendered_input
    assert "memory.search" in rendered_input
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
                    "name": "cap.memory.search",
                    "arguments": json.dumps({"query": "phoenix"}),
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
                    "arguments": json.dumps({"source": "x = 1\n"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "fc_run_two",
                    "call_id": "call_run_two",
                    "name": "run",
                    "arguments": json.dumps({"source": "y = 2\n"}),
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


def test_program_that_raises_is_a_program_failure(postgres_url: str) -> None:
    """A run program that raises mid-execution commits no effects and is fed
    back to the model as a recoverable program failure."""

    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source="raise ValueError('deliberate program failure')\n",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_program_raises",
            ),
            responses_run_message(
                assistant_text="recovered",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_program_recovered",
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
    feedback = json.dumps(adapter.input_items_seen[-1])
    assert "ValueError" in feedback


def test_program_with_syntax_error_is_a_program_failure(postgres_url: str) -> None:
    """A run program that fails to compile commits no effects and is fed back."""

    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source="this is not valid python\n",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_program_syntax",
            ),
            responses_run_message(
                assistant_text="recovered",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_syntax_recovered",
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


def test_pause_until_input_ends_turn_without_visible_output(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source="agent.pause_until_input()\n",
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
            _program_response(
                source="agent.emit_value(value={'answer': 42})\n",
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


def test_program_composes_a_mechanical_answer_in_one_turn(postgres_url: str) -> None:
    """A program may use control flow to compose a mechanical emit_message in
    the same turn -- the program-model relaxation of the flat-list rule."""

    source = (
        "items = [1, 2, 3]\n"
        "total = 0\n"
        "for item in items:\n"
        "    total += item\n"
        "agent.emit_message(text='The total is ' + str(total) + '.')\n"
    )
    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source=source,
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_mechanical",
            )
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "add it up"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "The total is 6."
    assert payload["turn"]["surface_action_lifecycle"] == []


def test_run_program_emitting_no_output_retries(postgres_url: str) -> None:
    """A program that completes cleanly but emits nothing user-visible is fed
    back as a protocol failure and the model retries."""

    adapter = CapturingRunAdapter(
        responses=[
            responses_with_run_calls(
                assistant_text="",
                calls=[{"name": "agent.emit_value", "input": {"value": 1}}],
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_no_visible",
            ),
            responses_run_message(
                assistant_text="now visible",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_now_visible",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "hello"})

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "now visible"


def test_taint_threads_across_two_programs_in_one_turn(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn runs two programs; taint from program 1 reaches program 2.

    Program 1 does an inline read whose result is untrusted-influenced, then
    emits a value -- which ends the program and continues the turn. Program 2
    proposes a side-effecting syscall. Because the run-program path threads each
    program's taint delta onto the turn baseline, program 2's syscall is
    evaluated with that taint: it receives a tainted ``runtime_provenance`` and
    real policy escalates it on the taint path -- reason
    ``taint_escalated_requires_approval`` rather than the clean
    ``approval_required``.

    ``process_one_call`` is stubbed -- as in the within-program taint test --
    so the app-side cross-program taint merge is what is exercised; the stub
    runs the real ``evaluate_proposal`` so the policy decision stays real.
    """

    seen_provenance: list[RuntimeProvenance | None] = []
    policy_decisions: list[tuple[str, str]] = []

    def fake_process_one_call(**kwargs: Any) -> None:
        ctx = kwargs["ctx"]
        index = kwargs["function_call_index"]
        capability_id = kwargs["function_call_raw"]["capability_id"]
        runtime_provenance = kwargs["runtime_provenance"]
        seen_provenance.append(runtime_provenance)
        if capability_id == "cap.memory.propose":
            # A side-effecting syscall: evaluate it through real policy with the
            # taint threaded in from the prior program. evaluate_proposal is a
            # pure function, so no DB write and no proposal_index is needed.
            provenance_status = (
                runtime_provenance.status if runtime_provenance is not None else None
            )
            evaluation = evaluate_proposal(
                capability_id=capability_id,
                input_payload=kwargs["function_call_raw"]["input"],
                pending_approval_exists=False,
                provenance_status=provenance_status,
            )
            policy_decisions.append((evaluation.decision, evaluation.reason))
        ctx.function_call_outputs.append(
            {
                "type": "function_call_output",
                "call_id": f"run_call_{index}",
                "output": '{"status":"succeeded","output":{"ok":true}}',
            }
        )
        if capability_id == "cap.memory.search":
            # The first read returned untrusted-influenced content; this is the
            # taint a real untrusted-content read would set on the context.
            ctx.result_runtime_provenance = RuntimeProvenance(
                status="tainted",
                evidence=({"kind": "untrusted_read"},),
            )

    monkeypatch.setattr(run_runtime_module, "process_one_call", fake_process_one_call)

    # Program 1: an untrusted read, then emit a value -- the value ends the
    # program and the turn continues to program 2.
    program_one = (
        "hits = memory.search(query='note')\nagent.emit_value(value={'searched': hits['ok']})\n"
    )
    # Program 2: a side-effecting syscall in a fresh program of the same turn.
    program_two = (
        "memory.propose(\n"
        "    subject_key='user', predicate='likes', assertion_type='preference',\n"
        "    value='tea', evidence_text='said so', confidence=0.9,\n"
        "    scope_key='global', valid_from=None, valid_to=None,\n"
        ")\n"
        "agent.emit_message(text='proposed')\n"
    )
    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source=program_one,
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_taint_program_one",
            ),
            _program_response(
                source=program_two,
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_taint_program_two",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message", json={"message": "act after a read"}
        )

    payload = sent.json()
    assert sent.status_code == 200
    assert payload["assistant"]["message"] == "proposed"
    # Two syscalls ran: program 1's read (clean baseline) and program 2's
    # side-effecting syscall, which must have seen the taint program 1 produced.
    assert len(seen_provenance) == 2
    assert seen_provenance[0] is None or seen_provenance[0].status == "clean"
    assert seen_provenance[1] is not None
    assert seen_provenance[1].status == "tainted"
    assert {"kind": "untrusted_read"} in seen_provenance[1].evidence

    # Real policy evaluated program 2's side-effecting syscall on the taint
    # path because of the threaded taint -- the taint-escalation reason, not a
    # plain approval, proves the cross-program taint reached it.
    assert policy_decisions == [("requires_approval", "taint_escalated_requires_approval")]


def test_two_programs_with_capability_syscalls_get_distinct_proposal_index(
    postgres_url: str,
) -> None:
    """A turn runs two programs that BOTH make a capability syscall.

    Each capability syscall routes through the real ``process_one_call`` and
    writes one ``ActionAttemptRecord``. ``proposal_index`` is restarted per
    program inside ``execute_run_program``, so without a turn-global offset the
    second program's attempt would collide with the first on the
    ``(turn_id, proposal_index)`` unique index and the turn would fail to
    persist. This asserts both attempts persist with distinct ``proposal_index``.

    Unlike ``test_taint_threads_across_two_programs_in_one_turn``, this exercises
    the real ``process_one_call`` so the unique index is actually hit.
    """

    # Each program runs a real capability read (memory.inspect, an inline,
    # always-in-scope read) and then ends: program 1 with emit_value so the turn
    # continues, program 2 with emit_message so the turn completes.
    program_one = (
        "snapshot = memory.inspect(section='all', limit=5)\n"
        "agent.emit_value(value={'inspected_one': snapshot['status']})\n"
    )
    program_two = (
        "snapshot = memory.inspect(section='all', limit=5)\n"
        "agent.emit_message(text='inspected twice: ' + snapshot['status'])\n"
    )
    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source=program_one,
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_two_caps_program_one",
            ),
            _program_response(
                source=program_two,
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_two_caps_program_two",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "inspect twice"})

    payload = sent.json()
    # The turn persisted: no UniqueViolation on the (turn_id, proposal_index)
    # index when the second program's capability syscall flushed.
    assert sent.status_code == 200, payload
    assert payload["assistant"]["message"] == "inspected twice: inspected"

    engine = create_engine(postgres_url, future=True)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    with session_factory() as db:
        turn = db.scalar(select(TurnRecord).where(TurnRecord.session_id == session_id))
        assert turn is not None
        attempts = db.scalars(
            select(ActionAttemptRecord)
            .where(ActionAttemptRecord.turn_id == turn.id)
            .order_by(ActionAttemptRecord.proposal_index.asc())
        ).all()
    # Both capability syscalls wrote an action attempt, and the two share a turn
    # but hold distinct proposal indices -- the turn-global counter at work.
    assert [attempt.capability_id for attempt in attempts] == [
        "cap.memory.inspect",
        "cap.memory.inspect",
    ]
    proposal_indices = [attempt.proposal_index for attempt in attempts]
    assert len(set(proposal_indices)) == 2, proposal_indices
    assert all(index > 0 for index in proposal_indices)
