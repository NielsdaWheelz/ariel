"""Integration tests for research dispatch, worker execution, and completion wakes.

These cover the three coupled pieces that connect ``cap.research.investigate``
to the research loop and its finding back to the main agent:

1. the ``action_runtime`` execute branch — a ``research.investigate`` syscall
   runs inline, writes a ``research_run`` ``background_tasks`` row carrying
   ``{question, mode}`` (CONTRACT A), and returns
   ``{status: "queued", research_id}``;
2. the worker ``research_run`` arm — it drives ``run_research`` and enqueues a
   completion ``agent_wake`` carrying the finding (CONTRACT B);
3. the completion ``agent_wake`` arm — it wakes the main agent with the finding
   rendered as a clearly-attributed block, carried with tainted provenance.

The action-runtime piece is driven through the real ``process_one_call`` (the
``test_proactivity_scheduler`` pattern); the worker arms are driven through
``process_one_task`` over enqueued rows (the ``test_proactivity_scheduler``
worker pattern).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import RuntimeProvenance
from tests.integration.app_helpers import create_test_app
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    EventRecord,
    MemoryLogRecord,
    TurnRecord,
    enqueue_background_task,
)
from ariel.worker import process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    drain_task,
    empty_recall_response,
    is_memory_subsystem_call,
    run_function_calls,
)
from ariel.model_adapter import ModelCall, ModelResponse

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _stub_memory_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub embed_text so a wake's per-turn ``recall`` is hermetic: writes get
    a null vector and search runs purely on tsquery — no network."""
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)


def _seed_turn(session_factory: sessionmaker[Session], *, turn_id: str) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                TurnRecord(
                    id=turn_id,
                    user_message="look into that for me",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


# ===========================================================================
# 1. The action_runtime execute branch — the research_run task and its payload
# ===========================================================================


@pytest.mark.parametrize("mode", ["web", "personal", "memories"])
def test_research_investigate_syscall_enqueues_a_research_run_task(
    session_factory: sessionmaker[Session],
    mode: str,
) -> None:
    """A ``cap.research.investigate`` call runs inline — no durable execution
    queue — and writes exactly one ``research_run`` ``background_tasks`` row
    whose payload carries the question and the mode (CONTRACT A). The syscall
    returns ``{status: "queued", research_id}`` — the
    ``research_task_start_v1`` output."""

    _seed_turn(session_factory, turn_id="trn_res")

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "trn_res")
            assert turn is not None
            ctx = run_function_calls(
                db=db,
                turn=turn,
                function_calls_raw=[
                    {
                        "call_id": "call_res",
                        "capability_id": "cap.research.investigate",
                        "input": {
                            "question": "What changed in the API this week?",
                            "mode": mode,
                        },
                        "influenced_by_untrusted_content": False,
                    }
                ],
                approval_ttl_seconds=300,
                approval_actor_id="usr_res",
                add_event=lambda event_type, payload: events.append((event_type, payload)),
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_res_1",
                allowed_capability_ids=["cap.research.investigate"],
                runtime_provenance=RuntimeProvenance(status="clean"),
            )

    # The syscall executed inline and returned the queued handle.
    assert ctx.blocked_reasons == []
    assert len(ctx.inline_results) == 1
    output = ctx.inline_results[0]["output"]
    assert output["status"] == "queued"
    research_id = output["research_id"]

    with session_factory() as db:
        research_tasks = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "research_run")
        ).all()
        assert len(research_tasks) == 1
        task = research_tasks[0]
        assert task.id == research_id
        # CONTRACT A: question and mode.
        assert task.payload == {
            "question": "What changed in the API this week?",
            "mode": mode,
        }
        # An immediate task: run_after is now, no recurrence.
        assert task.run_after == NOW
        assert task.recurrence_seconds is None
        assert task.attempts == 0
        # The syscall is inline: it never produced an execute_action_attempt row.
        execute_tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "execute_action_attempt"
            )
        ).all()
        assert execute_tasks == []


def test_research_investigate_syscall_rejects_a_bad_mode(
    session_factory: sessionmaker[Session],
) -> None:
    """An invalid ``mode`` fails the syscall closed: the call is blocked, no
    ``research_run`` row is written, and the program sees a failure."""

    _seed_turn(session_factory, turn_id="trn_resbad")

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "trn_resbad")
            assert turn is not None
            ctx = run_function_calls(
                db=db,
                turn=turn,
                function_calls_raw=[
                    {
                        "call_id": "call_resbad",
                        "capability_id": "cap.research.investigate",
                        "input": {"question": "anything", "mode": "hybrid"},
                        "influenced_by_untrusted_content": False,
                    }
                ],
                approval_ttl_seconds=300,
                approval_actor_id="usr_resbad",
                add_event=lambda event_type, payload: events.append((event_type, payload)),
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_resbad_1",
                allowed_capability_ids=["cap.research.investigate"],
                runtime_provenance=RuntimeProvenance(status="clean"),
            )

    assert ctx.blocked_reasons != []
    assert ctx.inline_results == []
    with session_factory() as db:
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "research_run")
        ).all()
        assert tasks == []


def test_research_investigate_queue_defect_rolls_back_instead_of_failing_action(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_turn(session_factory, turn_id="trn_resdefect")

    def fail_enqueue(*_args: Any, **_kwargs: Any) -> BackgroundTaskRecord:
        raise RuntimeError("queue bug")

    monkeypatch.setattr("ariel.action_runtime.enqueue_background_task", fail_enqueue)

    with pytest.raises(RuntimeError, match="queue bug"):
        with session_factory() as db:
            with db.begin():
                turn = db.get(TurnRecord, "trn_resdefect")
                assert turn is not None
                run_function_calls(
                    db=db,
                    turn=turn,
                    function_calls_raw=[
                        {
                            "call_id": "call_resdefect",
                            "capability_id": "cap.research.investigate",
                            "input": {
                                "question": "What changed in the API this week?",
                                "mode": "web",
                            },
                            "influenced_by_untrusted_content": False,
                        }
                    ],
                    approval_ttl_seconds=300,
                    approval_actor_id="usr_resdefect",
                    add_event=lambda _event_type, _payload: None,
                    now_fn=lambda: NOW,
                    new_id_fn=lambda prefix: f"{prefix}_resdefect_1",
                    allowed_capability_ids=["cap.research.investigate"],
                    runtime_provenance=RuntimeProvenance(status="clean"),
                )

    with session_factory() as db:
        attempts = db.scalars(select(ActionAttemptRecord)).all()
        tasks = db.scalars(select(BackgroundTaskRecord)).all()
    assert attempts == []
    assert tasks == []


# ===========================================================================
# 2. The worker research_run arm — run_research, then a completion agent_wake
# ===========================================================================


_FINDING_PROGRAM = (
    "agent.emit_finding(\n"
    "    summary='France is in Europe.',\n"
    "    claims=[{'statement': 'Paris is the capital', "
    "'sources': ['https://example.test'], 'confidence': 'high'}],\n"
    "    gaps=['Population unknown.'],\n"
    "    sources=[{'title': 'Example', 'reference': 'https://example.test', "
    "'retrieved_at': '2026-06-01T12:00:00Z'}],\n"
    ")\n"
)


class _ResearchRunAdapter(FakeModelAdapter):
    """A model adapter whose single ``run`` program calls ``agent.emit_finding``.

    Records the ``messages`` of every call so a test can assert what the
    research loop and the completion wake placed in the model's context."""

    provider = "provider.research"
    model = "model.research-v1"

    def __init__(self) -> None:
        super().__init__()
        self.snapshots: list[list[Any]] = []
        self.program_source = _FINDING_PROGRAM

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.snapshots.append(list(request.messages))
        call_index = len(self.snapshots)
        from tests.integration.responses_helpers import run_response  # noqa: PLC0415

        return run_response(
            source=self.program_source,
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_research_{call_index}",
            input_tokens=3,
            output_tokens=2,
        )


def test_worker_research_run_arm_runs_research_and_enqueues_completion_wake(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due ``research_run`` row drives ``run_research`` in the worker: the run
    is recorded as a ``kind="research"`` ``TurnRecord``, and on completion the
    worker enqueues an ``agent_wake`` carrying the finding back to the
    dispatching session (CONTRACT B). The research_run row is then deleted."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: NOW)
    adapter = _ResearchRunAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        with session_factory() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="research_run",
                    payload={
                        "question": "What is the capital of France?",
                        "mode": "web",
                    },
                    now=NOW - timedelta(minutes=1),
                )

        # Drain only the research_run task (a maintenance task may precede it).
        for _ in range(10):
            process_one_task(
                session_factory=session_factory,
                settings=runtime.settings,
                runtime=runtime,
            )
            with session_factory() as db:
                remaining = db.scalars(
                    select(BackgroundTaskRecord).where(
                        BackgroundTaskRecord.task_type == "research_run"
                    )
                ).all()
            if not remaining:
                break

        with session_factory() as db:
            # The research_run row was deleted on success.
            assert (
                db.scalars(
                    select(BackgroundTaskRecord).where(
                        BackgroundTaskRecord.task_type == "research_run"
                    )
                ).all()
                == []
            )
            # run_research recorded the run as a kind="research" TurnRecord.
            research_turn = db.scalar(select(TurnRecord).where(TurnRecord.kind == "research"))
            assert research_turn is not None
            assert research_turn.status == "completed"
            assert research_turn.user_message == "What is the capital of France?"
            assert research_turn.assistant_message == "France is in Europe."
            # CONTRACT B: a completion agent_wake carries the full finding,
            # distinguishable from a plain note wake.
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
            assert len(wake_tasks) == 1
            payload = wake_tasks[0].payload
            assert "note" not in payload
            finding = payload["research_finding"]
            assert finding["question"] == "What is the capital of France?"
            assert finding["mode"] == "web"
            assert finding["status"] == "complete"
            assert finding["summary"] == "France is in Europe."
            assert finding["claims"] == [
                {
                    "statement": "Paris is the capital",
                    "sources": ["https://example.test"],
                    "confidence": "high",
                }
            ]
            assert finding["gaps"] == ["Population unknown."]
            assert finding["sources"] == [
                {
                    "title": "Example",
                    "reference": "https://example.test",
                    "retrieved_at": "2026-06-01T12:00:00Z",
                }
            ]


def test_worker_research_run_replay_uses_completed_research_turn_without_model_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a crash leaves a completed research turn and the original task row,
    replay must enqueue the completion wake without rerunning research."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: NOW)
    adapter = _ResearchRunAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        with session_factory() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="research_run",
                    payload={
                        "question": "What is the capital of France?",
                        "mode": "web",
                    },
                    now=NOW - timedelta(minutes=5),
                    run_after=NOW - timedelta(minutes=1),
                )
                task_id = task.id
                db.add(
                    TurnRecord(
                        id="trn_research_replay_done",
                        user_message="What is the capital of France?",
                        assistant_message="France is in Europe.",
                        status="completed",
                        kind="research",
                        source_background_task_id=task_id,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                db.add(
                    EventRecord(
                        id="evn_research_replay_finding",
                        turn_id="trn_research_replay_done",
                        sequence=1,
                        event_type="evt.research.finding_emitted",
                        payload={
                            "mode": "web",
                            "finding": {
                                "question": "What is the capital of France?",
                                "mode": "web",
                                "status": "complete",
                                "summary": "France is in Europe.",
                                "claims": [
                                    {
                                        "statement": "Paris is the capital",
                                        "sources": ["https://example.test"],
                                        "confidence": "high",
                                    }
                                ],
                                "gaps": ["Population unknown."],
                                "sources": [
                                    {
                                        "title": "Example",
                                        "reference": "https://example.test",
                                        "retrieved_at": "2026-06-01T12:00:00Z",
                                    }
                                ],
                            },
                        },
                        created_at=NOW,
                    )
                )
                db.add(
                    EventRecord(
                        id="evn_research_replay_completed",
                        turn_id="trn_research_replay_done",
                        sequence=2,
                        event_type="evt.turn.completed",
                        payload={},
                        created_at=NOW,
                    )
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

        assert adapter.snapshots == []
        with session_factory() as db:
            assert db.get(BackgroundTaskRecord, task_id) is None
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
            assert len(wake_tasks) == 1
            finding = wake_tasks[0].payload["research_finding"]
            assert finding["status"] == "complete"
            assert finding["claims"][0]["statement"] == "Paris is the capital"


def test_worker_research_run_replay_fails_interrupted_turn_without_model_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source research turn left in progress is failed on replay instead of
    rerunning the model and duplicating already committed program effects."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: NOW)
    adapter = _ResearchRunAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        with session_factory() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="research_run",
                    payload={
                        "question": "What is the capital of France?",
                        "mode": "web",
                    },
                    now=NOW - timedelta(minutes=5),
                    run_after=NOW - timedelta(minutes=1),
                )
                task_id = task.id
                db.add(
                    TurnRecord(
                        id="trn_research_replay_interrupted",
                        user_message="What is the capital of France?",
                        assistant_message=None,
                        status="in_progress",
                        kind="research",
                        source_background_task_id=task_id,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                db.add(
                    EventRecord(
                        id="evn_research_replay_started",
                        turn_id="trn_research_replay_interrupted",
                        sequence=1,
                        event_type="evt.research.started",
                        payload={
                            "research_question": "What is the capital of France?",
                            "research_mode": "web",
                        },
                        created_at=NOW,
                    )
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

        assert adapter.snapshots == []
        with session_factory() as db:
            assert db.get(BackgroundTaskRecord, task_id) is None
            turn = db.get(TurnRecord, "trn_research_replay_interrupted")
            assert turn is not None
            assert turn.status == "failed"
            wake_task = db.scalar(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            )
            assert wake_task is not None
            finding = wake_task.payload["research_finding"]
            assert finding["status"] == "failed"
            assert (
                finding["summary"] == "The research run was interrupted before producing a finding."
            )
            assert finding["claims"] == []
            assert finding["gaps"] == []
            assert finding["sources"] == []
            event_types = [
                row.event_type
                for row in db.scalars(
                    select(EventRecord)
                    .where(EventRecord.turn_id == "trn_research_replay_interrupted")
                    .order_by(EventRecord.sequence.asc())
                ).all()
            ]
            assert "evt.turn.failed" in event_types
            assert "evt.turn.completed" not in event_types


def test_worker_research_run_replay_rejects_corrupt_terminal_finding(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal research turn without a valid finding is persisted corruption,
    not a partial answer to synthesize during replay."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: NOW)
    adapter = _ResearchRunAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        with session_factory() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="research_run",
                    payload={
                        "question": "What is the capital of France?",
                        "mode": "web",
                    },
                    now=NOW - timedelta(minutes=5),
                    run_after=NOW - timedelta(minutes=1),
                )
                task_id = task.id
                db.add(
                    TurnRecord(
                        id="trn_research_replay_corrupt",
                        user_message="What is the capital of France?",
                        assistant_message="France is in Europe.",
                        status="completed",
                        kind="research",
                        source_background_task_id=task_id,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                db.add(
                    EventRecord(
                        id="evn_research_replay_corrupt",
                        turn_id="trn_research_replay_corrupt",
                        sequence=1,
                        event_type="evt.research.finding_emitted",
                        payload={"mode": "web", "finding": {"status": "complete"}},
                        created_at=NOW,
                    )
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

        assert adapter.snapshots == []
        with session_factory() as db:
            task = db.get(BackgroundTaskRecord, task_id)
            assert task is not None
            assert task.attempts == 1
            assert (
                db.scalars(
                    select(BackgroundTaskRecord).where(
                        BackgroundTaskRecord.task_type == "agent_wake"
                    )
                ).all()
                == []
            )


def test_worker_completion_wake_renders_finding_into_main_agent_context(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due completion ``agent_wake`` wakes the main agent: the finding is
    rendered into the model's context as a clearly-attributed research-result
    block, and the agent answers from it. The wake is run through ``_wake`` on
    the carried session, exactly as the agency-completion class of wake."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: NOW)

    class _MainAgentAdapter(FakeModelAdapter):
        """The main agent: emits a message; records its context items."""

        provider = "provider.main"
        model = "model.main-v1"

        def __init__(self) -> None:
            super().__init__()
            self.snapshots: list[list[Any]] = []

        def _respond(self, request: ModelCall) -> ModelResponse:
            if is_memory_subsystem_call(request.messages):
                return empty_recall_response(
                    provider=self.provider, model=self.model, messages=request.messages
                )
            self.snapshots.append(list(request.messages))
            source = "agent.emit_message(text='Here is what the research found.')\n"
            from tests.integration.responses_helpers import run_response  # noqa: PLC0415

            return run_response(
                source=source,
                provider=self.provider,
                model=self.model,
                provider_response_id=f"resp_main_{len(self.snapshots)}",
                input_tokens=3,
                output_tokens=2,
            )

    adapter = _MainAgentAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        with session_factory() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="agent_wake",
                    payload={
                        "research_finding": {
                            "question": "What is the capital of France?",
                            "mode": "web",
                            "status": "complete",
                            "summary": "Paris is the capital of France.",
                            "claims": [],
                            "gaps": [],
                            "sources": [],
                        },
                    },
                    now=NOW - timedelta(minutes=1),
                )

        for _ in range(10):
            process_one_task(
                session_factory=session_factory,
                settings=runtime.settings,
                runtime=runtime,
            )
            with session_factory() as db:
                remaining = db.scalars(
                    select(BackgroundTaskRecord).where(
                        BackgroundTaskRecord.task_type == "agent_wake"
                    )
                ).all()
            if not remaining:
                break

    # The main agent's context carried the finding as an attributed result block.
    assert adapter.snapshots, "the main agent was never woken"
    rendered = json.dumps(jsonable_encoder(adapter.snapshots[0]))
    assert "Research run result" in rendered
    assert "Paris is the capital of France." in rendered
    assert "research.investigate call" in rendered

    with session_factory() as db:
        # The completion wake ran as a normal agent_turn on the carried session
        # and the agent answered from the finding.
        turn = db.scalar(
            select(TurnRecord)
            .where(TurnRecord.kind == "agent_turn")
            .order_by(TurnRecord.created_at.desc())
        )
        assert turn is not None
        assert turn.status == "completed"
        assert turn.assistant_message == "Here is what the research found."
        wake_log = db.scalar(
            select(MemoryLogRecord).where(
                MemoryLogRecord.kind == "proactive_trigger",
                MemoryLogRecord.turn_id == turn.id,
            )
        )
        assert wake_log is not None
        assert wake_log.taint == "tainted"
        assert "Research run result" in wake_log.content
        assert "Paris is the capital of France." in wake_log.content


class _ResearchEndToEndAdapter(FakeModelAdapter):
    """The first non-memory call completes research; the second answers the wake."""

    provider = "provider.research-e2e"
    model = "model.research-e2e-v1"

    def __init__(self) -> None:
        super().__init__()
        self.snapshots: list[str] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.snapshots.append(json.dumps(jsonable_encoder(request.messages)))
        from tests.integration.responses_helpers import run_response  # noqa: PLC0415

        if len(self.snapshots) == 1:
            source = _FINDING_PROGRAM
        else:
            source = "agent.emit_message(text='Research completion delivered.')\n"
        return run_response(
            source=source,
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_research_e2e_{len(self.snapshots)}",
            input_tokens=3,
            output_tokens=2,
        )


def test_research_investigate_run_program_worker_drains_research_and_completion_wake_end_to_end(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``research_run`` row created by the syscall is drained by the worker,
    then its generated completion wake is drained into the main-agent context."""

    _stub_memory_retriever(monkeypatch)
    current_now = {"value": NOW}
    monkeypatch.setattr("ariel.worker.utcnow", lambda: current_now["value"])
    monkeypatch.setattr("ariel.app.utcnow", lambda: current_now["value"])
    adapter = _ResearchEndToEndAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        with session_factory() as db:
            with db.begin():
                turn = TurnRecord(
                    id="trn_research_e2e",
                    user_message="research this for me",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW - timedelta(minutes=5),
                    updated_at=NOW - timedelta(minutes=5),
                )
                db.add(turn)
                ctx = run_function_calls(
                    db=db,
                    turn=turn,
                    function_calls_raw=[
                        {
                            "call_id": "call_research_e2e",
                            "capability_id": "cap.research.investigate",
                            "input": {
                                "question": "What is the capital of France?",
                                "mode": "web",
                            },
                            "influenced_by_untrusted_content": False,
                        }
                    ],
                    approval_ttl_seconds=300,
                    approval_actor_id="usr_research_e2e",
                    add_event=lambda _event_type, _payload: None,
                    now_fn=lambda: NOW,
                    new_id_fn=lambda prefix: f"{prefix}_research_e2e",
                    allowed_capability_ids=["cap.research.investigate"],
                    runtime_provenance=RuntimeProvenance(status="clean"),
                )
                turn.status = "completed"
                turn.assistant_message = "research queued"
                turn.updated_at = NOW - timedelta(minutes=4)

        assert ctx.blocked_reasons == []
        assert len(ctx.inline_results) == 1
        output = ctx.inline_results[0]["output"]
        assert output["status"] == "queued"
        research_task_id = output["research_id"]
        drain_task(client, research_task_id)

        with session_factory() as db:
            completion_wake = db.scalar(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            )
            assert completion_wake is not None
            completion_wake_id = completion_wake.id
            assert completion_wake.payload["research_finding"]["summary"] == "France is in Europe."

        current_now["value"] = NOW + timedelta(minutes=1)
        drain_task(client, completion_wake_id)

    assert len(adapter.snapshots) == 2
    assert "What is the capital of France?" in adapter.snapshots[0]
    assert "Research run result" in adapter.snapshots[1]
    assert "France is in Europe." in adapter.snapshots[1]
    with session_factory() as db:
        assert db.get(BackgroundTaskRecord, research_task_id) is None
        assert db.get(BackgroundTaskRecord, completion_wake_id) is None
        recorded_turns = db.scalars(
            select(TurnRecord).order_by(TurnRecord.created_at.asc(), TurnRecord.id.asc())
        ).all()
        research_turn = db.scalar(select(TurnRecord).where(TurnRecord.kind == "research"))
        assert research_turn is not None
        assert research_turn.status == "completed"
        completion_turn = db.scalar(
            select(TurnRecord)
            .where(
                TurnRecord.kind == "agent_turn",
                TurnRecord.assistant_message == "Research completion delivered.",
            )
            .order_by(TurnRecord.created_at.desc())
        )
        assert completion_turn is not None, [
            {
                "id": turn.id,
                "kind": turn.kind,
                "status": turn.status,
                "user_message": turn.user_message,
                "assistant_message": turn.assistant_message,
                "created_at": turn.created_at.isoformat(),
            }
            for turn in recorded_turns
        ]
        assert completion_turn.status == "completed"
        assert completion_turn.assistant_message == "Research completion delivered."
        completion_wake_log = db.scalar(
            select(MemoryLogRecord).where(
                MemoryLogRecord.kind == "proactive_trigger",
                MemoryLogRecord.turn_id == completion_turn.id,
            )
        )
        assert completion_wake_log is not None
        assert completion_wake_log.taint == "tainted"
        assert "Research run result" in completion_wake_log.content
        assert "France is in Europe." in completion_wake_log.content
