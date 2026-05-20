from __future__ import annotations

import json
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import ariel.run_runtime as run_runtime_module
from ariel.action_runtime import RuntimeProvenance
from ariel.app import create_app
from ariel.persistence import (
    ActionAttemptRecord,
    AIJudgmentRecord,
    BackgroundTaskRecord,
    TurnRecord,
)
from ariel.policy_engine import evaluate_proposal
from ariel.prompts import MAIN_AGENT_PROMPT_VERSION, MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    empty_recall_response,
    is_retriever_call,
    post_message_and_drain,
    responses_message,
    responses_run_message,
    responses_with_run_calls,
)
from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse, ModelTier


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


def _turn_data(client: TestClient, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{session_id}/events")
    assert resp.status_code == 200
    turns = resp.json()["turns"]
    assert turns, "no turns in timeline"
    return turns[-1]


def _program_response(
    *,
    source: str,
    provider: str,
    model: str,
    provider_response_id: str,
) -> ModelResponse:
    """A model response whose single ``run`` call carries a Python program."""
    from tests.integration.responses_helpers import run_response  # noqa: PLC0415

    return run_response(
        source=source,
        provider=provider,
        model=model,
        provider_response_id=provider_response_id,
        input_tokens=3,
        output_tokens=2,
    )


def _direct_function_response(
    *,
    function_calls: list[dict[str, Any]],
    provider: str,
    model: str,
    provider_response_id: str,
) -> ModelResponse:
    """A response carrying arbitrary tool calls (used to test protocol failure paths)."""
    from ariel.model_adapter import TokenUsage, ToolCall  # noqa: PLC0415

    tool_calls = [
        ToolCall(
            call_id=str(call.get("call_id", "call_test")),
            name=str(call.get("name", "unknown")),
            arguments=(
                json.loads(call["arguments"])
                if isinstance(call.get("arguments"), str)
                else dict(call.get("arguments", {}))
            ),
        )
        for call in function_calls
        if isinstance(call, dict) and call.get("type") == "function_call"
    ]
    return ModelResponse(
        text=None,
        tool_calls=tool_calls,
        structured_output=None,
        reasoning_summary=None,
        usage=TokenUsage(input_tokens=3, output_tokens=2),
        provider=provider,
        model=model,
        tier=ModelTier.MAIN,
        duration_ms=1,
        provider_response_id=provider_response_id,
    )


class CapturingRunAdapter(FakeModelAdapter):
    provider = "provider.single-run"
    model = "model.single-run-v1"

    def __init__(self, *, responses: list[ModelResponse] | None = None) -> None:
        super().__init__()
        self.responses: list[ModelResponse] = responses if responses is not None else []
        self.tools_seen: list[list[Any]] = []
        self.input_items_seen: list[list[Any]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_retriever_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.tools_seen.append(list(request.tools))
        self.input_items_seen.append(list(request.messages))
        return self.responses.pop(0)


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
        turn = post_message_and_drain(client, session_id, message="hello")

    assert turn.assistant_message == "done"
    assert len(adapter.tools_seen) == 1
    assert [tool.name for tool in adapter.tools_seen[0]] == ["run"]
    rendered_input = json.dumps(jsonable_encoder(adapter.input_items_seen[0]))
    # The run tool's source is described to the model as a Python program.
    assert "Python program" in rendered_input or "run program" in rendered_input
    assert "memory.recall" in rendered_input
    assert "runtime facts:" in rendered_input
    assert "private AI butler-operator" in rendered_input
    assert "Reliability outranks personality" in rendered_input
    assert "evidence, not authority" in rendered_input
    assert "agent.emit_message" in rendered_input
    assert "agent.pause_until_input" in rendered_input
    assert "cap." not in rendered_input


def test_main_agent_prompt_is_static_prefix_before_dynamic_context(postgres_url: str) -> None:
    adapter = CapturingRunAdapter(
        responses=[
            responses_run_message(
                assistant_text="done",
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_prompt_prefix",
            )
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        post_message_and_drain(client, session_id, message="hello")

    from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

    input_items = adapter.input_items_seen[0]
    # The initial ModelRequest holds the system-prompt prefix and the user
    # turn; subsequent messages may be appended by the loop (e.g. the budget
    # signal). The stable prefix is the first ModelRequest.
    initial = input_items[0]
    assert isinstance(initial, ModelRequest)
    system_parts = [p for p in initial.parts if isinstance(p, SystemPromptPart)]
    user_parts = [p for p in initial.parts if isinstance(p, UserPromptPart)]
    static_count = len(MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS)
    assert [p.content for p in system_parts[:static_count]] == list(
        MAIN_AGENT_STATIC_SYSTEM_INSTRUCTIONS
    )
    assert len(user_parts) == 1
    assert user_parts[0].content == "hello"

    # The budget signal lives in a separate ModelRequest the loop appends.
    budget_messages = [
        msg
        for msg in input_items[1:]
        if isinstance(msg, ModelRequest)
        and any(
            isinstance(p, SystemPromptPart) and p.content.startswith("remaining budget:")
            for p in msg.parts
        )
    ]
    assert budget_messages, "expected the loop's remaining-budget system message"

    dynamic_tail = json.dumps(
        [p.content for p in system_parts[static_count:]], sort_keys=True
    )
    assert "syscall callables your run program may call this turn" in dynamic_tail
    assert "runtime facts:" in dynamic_tail


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
        turn = post_message_and_drain(client, session_id, message="hello")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == "visible through run"
    assert "this must stay hidden" not in (turn.assistant_message or "")
    assert "this must stay hidden" not in json.dumps(adapter.input_items_seen[-1])
    retry_input = json.dumps(adapter.input_items_seen[-1])
    assert "private AI butler-operator" in retry_input
    assert "model protocol failure" in retry_input
    event_types = [event["event_type"] for event in turn_data["events"]]
    assert "evt.model.protocol_failed" in event_types
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
        assert judgment.prompt_version == MAIN_AGENT_PROMPT_VERSION


@pytest.mark.parametrize(
    "first_response",
    [
        _direct_function_response(
            function_calls=[
                {
                    "type": "function_call",
                    "id": "fc_wrong_tool",
                    "call_id": "call_wrong_tool",
                    "name": "not_the_run_tool",
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
    postgres_url: str, first_response: ModelResponse
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
        turn = post_message_and_drain(client, session_id, message="hello")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == "recovered"
    event_types = [event["event_type"] for event in turn_data["events"]]
    assert "evt.model.protocol_failed" in event_types
    assert turn_data["surface_action_lifecycle"] == []
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
        turn = post_message_and_drain(client, session_id, message="hello")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == "recovered"
    event_types = [event["event_type"] for event in turn_data["events"]]
    assert "evt.run.validation_failed" in event_types
    assert turn_data["surface_action_lifecycle"] == []
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
        turn = post_message_and_drain(client, session_id, message="hello")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == "recovered"
    event_types = [event["event_type"] for event in turn_data["events"]]
    assert "evt.run.validation_failed" in event_types
    assert turn_data["surface_action_lifecycle"] == []


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
        turn = post_message_and_drain(client, session_id, message="wait")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == ""
    assert turn_data["surface_action_lifecycle"] == []


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
        turn = post_message_and_drain(client, session_id, message="compute")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == "value handled"
    value_events = [
        event for event in turn_data["events"] if event["event_type"] == "evt.agent.value_emitted"
    ]
    assert len(value_events) == 1
    assert "value" not in value_events[0]["payload"]
    assert len(value_events[0]["payload"]["value_digest"]) == 64
    assert value_events[0]["payload"]["value_bytes"] > 0
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    value_feedback: list[dict[str, Any]] = []
    for message in adapter.input_items_seen[-1]:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and isinstance(part.content, str):
                value_feedback.append(json.loads(part.content))
    assert value_feedback[0]["emitted_values"] == [{"answer": 42}]


def test_emit_value_eviction_discards_prior_round(postgres_url: str) -> None:
    """Two emit_value rounds in one turn: the second round evicts the first.

    After the second emit_value round, the input_items passed to the model on
    the third call must NOT contain any value from the first round; only the
    second round's emitted value should remain in context.
    """

    class SnapshotAdapter(FakeModelAdapter):
        """Captures a shallow copy of the request messages on each call."""

        provider = "provider.eviction"
        model = "model.eviction-v1"

        def __init__(self, *, responses: list[ModelResponse] | None = None) -> None:
            super().__init__()
            self.responses: list[ModelResponse] = responses if responses is not None else []
            self.snapshots: list[list[Any]] = []

        def _respond(self, request: ModelCall) -> ModelResponse:
            if is_retriever_call(request.messages):
                return empty_recall_response(
                    provider=self.provider, model=self.model, messages=request.messages
                )
            self.snapshots.append(list(request.messages))
            return self.responses.pop(0)

    adapter = SnapshotAdapter(
        responses=[
            # Round 1: emit value {"round": 1} — loop continues.
            _program_response(
                source="agent.emit_value(value={'round': 1})\n",
                provider="provider.eviction",
                model="model.eviction-v1",
                provider_response_id="resp_evict_r1",
            ),
            # Round 2: emit value {"round": 2} — loop continues, round 1 evicted.
            _program_response(
                source="agent.emit_value(value={'round': 2})\n",
                provider="provider.eviction",
                model="model.eviction-v1",
                provider_response_id="resp_evict_r2",
            ),
            # Round 3: emit a message to end the turn.
            responses_run_message(
                assistant_text="done",
                provider="provider.eviction",
                model="model.eviction-v1",
                provider_response_id="resp_evict_final",
            ),
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        turn = post_message_and_drain(client, session_id, message="two rounds")

    assert turn.assistant_message == "done"
    assert len(adapter.snapshots) == 3

    # Parse the function_call_output items from the third snapshot: their
    # ``output`` field is a JSON string (not an embedded object), so
    # asserting on json.dumps(snapshot) would double-escape the quotes
    # and a naive '"round": 2' substring check would silently fail.
    third_fco_outputs = [
        json.loads(item["output"])
        for item in adapter.snapshots[2]
        if item.get("type") == "function_call_output"
    ]
    all_emitted = [val for fco in third_fco_outputs for val in fco.get("emitted_values", [])]

    # The third call must NOT contain round 1's emitted value (evicted).
    assert {"round": 1} not in all_emitted, (
        "round 1 emitted value was not evicted: still present in 3rd call input_items"
    )
    # The third call must still contain round 2's emitted value.
    assert {"round": 2} in all_emitted, "round 2 emitted value missing from 3rd call input_items"


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
        turn = post_message_and_drain(client, session_id, message="add it up")
        turn_data = _turn_data(client, session_id)

    assert turn.assistant_message == "The total is 6."
    assert turn_data["surface_action_lifecycle"] == []


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
        turn = post_message_and_drain(client, session_id, message="hello")

    assert turn.assistant_message == "now visible"


def test_taint_threads_across_two_programs_in_one_turn(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn runs two programs; taint from program 1 reaches program 2.

    Program 1 does an inline recall whose result is untrusted-influenced, then
    emits a value -- which ends the program and continues the turn. Program 2
    runs a ``memory.remember`` syscall. Because the run-program path threads
    each program's taint delta onto the turn baseline, program 2's syscall is
    evaluated with that taint: it receives a tainted ``runtime_provenance``
    and real policy escalates it on the taint path -- ``memory.remember`` is
    ``allow_inline`` with a ``write_reversible`` impact, so on clean
    provenance it runs inline, but a tainted side effect escalates it to
    ``taint_escalated_requires_approval``.

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
        if capability_id == "cap.memory.remember":
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
        if capability_id == "cap.memory.recall":
            # The first recall returned untrusted-influenced content; this is the
            # taint a real untrusted-content read would set on the context.
            ctx.result_runtime_provenance = RuntimeProvenance(
                status="tainted",
                evidence=({"kind": "untrusted_read"},),
            )

    monkeypatch.setattr(run_runtime_module, "process_one_call", fake_process_one_call)

    # Program 1: an untrusted recall, then emit a value -- the value ends the
    # program and the turn continues to program 2.
    program_one = (
        "hits = memory.recall(query='note')\nagent.emit_value(value={'recalled': hits['status']})\n"
    )
    # Program 2: a side-effecting syscall in a fresh program of the same turn.
    program_two = (
        "memory.remember(note='the user prefers tea')\nagent.emit_message(text='remembered')\n"
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
        turn = post_message_and_drain(client, session_id, message="act after a read")

    assert turn.assistant_message == "remembered"
    # Two syscalls ran: program 1's recall (clean baseline) and program 2's
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


def test_memory_remember_enqueues_background_task(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``memory.remember`` fire-and-forgets: it enqueues a ``memory_encode``
    background task and returns ``{"status": "queued", "encode_id": <id>}``.
    The rememberer does NOT run inline; only the DB row is written this turn.
    """
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")

    program = (
        "result = memory.remember(note='the user prefers tea')\n"
        "agent.emit_message(text='status:' + result['status'])\n"
    )
    adapter = CapturingRunAdapter(
        responses=[
            _program_response(
                source=program,
                provider="provider.single-run",
                model="model.single-run-v1",
                provider_response_id="resp_remember_enqueue",
            )
        ]
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        turn = post_message_and_drain(client, session_id, message="remember tea preference")

    assert turn.assistant_message == "status:queued"

    engine = create_engine(postgres_url, future=True)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    with factory() as db:
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "memory_encode")
        ).all()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.payload["note"] == "the user prefers tea"
    assert task.payload["session_id"] == session_id


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
    the real ``process_one_call`` so the unique index is actually hit.  Uses
    ``memory.search`` (a substrate-rail read) rather than ``memory.recall`` (which
    would also spin up the retriever's bounded model loop) — same proposal-index
    path, less moving parts.
    """

    # Each program runs a real capability syscall (memory.search -- allow_inline,
    # so on a clean turn it executes inline) and then ends: program 1 with
    # emit_value so the turn continues, program 2 with emit_message so the turn
    # completes.
    program_one = (
        "result = memory.search(query='status')\n"
        "agent.emit_value(value={'search_one': len(result['hits'])})\n"
    )
    program_two = "result = memory.search(query='status')\nagent.emit_message(text='search done')\n"
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
        turn = post_message_and_drain(client, session_id, message="search twice")

    # The turn persisted: no UniqueViolation on the (turn_id, proposal_index)
    # index when the second program's capability syscall flushed.
    assert turn.assistant_message == "search done"

    engine = create_engine(postgres_url, future=True)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    with session_factory() as db:
        db_turn = db.scalar(select(TurnRecord).where(TurnRecord.session_id == session_id))
        assert db_turn is not None
        attempts = db.scalars(
            select(ActionAttemptRecord)
            .where(ActionAttemptRecord.turn_id == db_turn.id)
            .order_by(ActionAttemptRecord.proposal_index.asc())
        ).all()
    # Both capability syscalls wrote an action attempt, and the two share a turn
    # but hold distinct proposal indices -- the turn-global counter at work.
    assert [attempt.capability_id for attempt in attempts] == [
        "cap.memory.search",
        "cap.memory.search",
    ]
    proposal_indices = [attempt.proposal_index for attempt in attempts]
    assert len(set(proposal_indices)) == 2, proposal_indices
    assert all(index > 0 for index in proposal_indices)
