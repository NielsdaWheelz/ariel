"""Integration tests for the research subagent wiring — P3 end to end.

These cover the three coupled pieces that connect ``cap.research.investigate``
to the research loop and its finding back to the main agent:

1. the ``action_runtime`` execute branch — a ``research.investigate`` syscall
   runs inline, writes a ``research_run`` ``background_tasks`` row carrying
   ``{question, mode, session_id}`` (CONTRACT A), and returns
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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import ariel.memory as memory
from ariel.action_runtime import RuntimeProvenance
from ariel.app import create_app
from ariel.persistence import (
    BackgroundTaskRecord,
    SessionRecord,
    TurnRecord,
    enqueue_background_task,
)
from ariel.research_runtime import ResearchFinding, render_finding
from ariel.worker import _agent_wake_context, process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import (
    empty_recall_response,
    is_retriever_call,
    run_function_calls,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _stub_memory_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the retriever's bounded model call so a wake's per-turn ``recall``
    is hermetic: the retriever subagent selects no facts from the empty store."""

    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "id": "resp_retriever_stub",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps({"facts": []})}],
                    }
                ],
            }

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(memory.httpx, "post", lambda *args, **kwargs: _Response())


def _seed_active_session(session_factory: sessionmaker[Session], session_id: str) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id=session_id,
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


def _seed_turn(session_factory: sessionmaker[Session], *, session_id: str, turn_id: str) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                TurnRecord(
                    id=turn_id,
                    session_id=session_id,
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


@pytest.mark.parametrize("mode", ["web", "personal"])
def test_research_investigate_syscall_enqueues_a_research_run_task(
    session_factory: sessionmaker[Session],
    mode: str,
) -> None:
    """A ``cap.research.investigate`` call runs inline — no durable execution
    queue — and writes exactly one ``research_run`` ``background_tasks`` row
    whose payload carries the question, the mode, and the originating
    ``session_id`` (CONTRACT A). The syscall returns ``{status: "queued",
    research_id}`` — the ``research_task_start_v1`` output."""

    _seed_active_session(session_factory, "ses_res")
    _seed_turn(session_factory, session_id="ses_res", turn_id="trn_res")

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "trn_res")
            assert turn is not None
            ctx = run_function_calls(
                db=db,
                session_id="ses_res",
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
        # CONTRACT A: question, mode, and the originating session_id.
        assert task.payload == {
            "question": "What changed in the API this week?",
            "mode": mode,
            "session_id": "ses_res",
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

    _seed_active_session(session_factory, "ses_resbad")
    _seed_turn(session_factory, session_id="ses_resbad", turn_id="trn_resbad")

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "trn_resbad")
            assert turn is not None
            ctx = run_function_calls(
                db=db,
                session_id="ses_resbad",
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


# ===========================================================================
# 2. The worker research_run arm — run_research, then a completion agent_wake
# ===========================================================================


_FINDING_PROGRAM = (
    "research.finding(\n"
    "    summary='France is in Europe.',\n"
    "    claims=[{'statement': 'Paris is the capital', "
    "'sources': ['https://example.test'], 'confidence': 'high'}],\n"
    "    gaps=['Population unknown.'],\n"
    "    sources=[{'title': 'Example', 'reference': 'https://example.test', "
    "'retrieved_at': '2026-06-01T12:00:00Z'}],\n"
    ")\n"
)


@dataclass
class _ResearchRunAdapter:
    """A model adapter whose single ``run`` program calls ``research.finding``.

    Records the ``input_items`` of every call so a test can assert what the
    research loop and the completion wake placed in the model's context."""

    provider: str = "provider.research"
    model: str = "model.research-v1"
    snapshots: list[list[dict[str, Any]]] = field(default_factory=list)
    program_source: str = _FINDING_PROGRAM

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(provider=self.provider, model=self.model)
        del tools, user_message, history, context_bundle
        self.snapshots.append(list(input_items))
        call_index = len(self.snapshots)
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            "provider_response_id": f"resp_research_{call_index}",
            "output": [
                {
                    "type": "function_call",
                    "id": f"fc_research_{call_index}",
                    "call_id": f"call_research_{call_index}",
                    "name": "run",
                    "arguments": json.dumps({"source": self.program_source}, sort_keys=True),
                    "status": "completed",
                }
            ],
        }


def test_worker_research_run_arm_runs_research_and_enqueues_completion_wake(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due ``research_run`` row drives ``run_research`` in the worker: the run
    is recorded as a ``kind="research"`` ``TurnRecord``, and on completion the
    worker enqueues an ``agent_wake`` carrying the finding back to the
    dispatching session (CONTRACT B). The research_run row is then deleted."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: NOW)
    adapter = _ResearchRunAdapter()
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
        reset_database=True,
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        session_response = client.get("/v1/sessions/active")
        assert session_response.status_code == 200
        session_id = session_response.json()["session"]["id"]

        with session_factory() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="research_run",
                    payload={
                        "question": "What is the capital of France?",
                        "mode": "web",
                        "session_id": session_id,
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
            # CONTRACT B: a completion agent_wake carries the full finding plus
            # the originating session_id, distinguishable from a plain note wake.
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
            assert len(wake_tasks) == 1
            payload = wake_tasks[0].payload
            assert payload["session_id"] == session_id
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


def test_worker_research_run_arm_rejects_a_bad_payload(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``research_run`` row missing ``session_id`` is a bad shape: the arm
    raises, the worker marks the row failed (attempts increments), and no
    completion ``agent_wake`` is enqueued."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: NOW)
    app = create_app(
        database_url=postgres_url,
        model_adapter=_ResearchRunAdapter(),
        sandbox=FakeSandboxRuntime(),
        reset_database=True,
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        with session_factory() as db:
            with db.begin():
                bad_task = enqueue_background_task(
                    db,
                    task_type="research_run",
                    payload={"question": "no session here", "mode": "web"},
                    now=NOW - timedelta(minutes=1),
                )
                bad_task_id = bad_task.id

        for _ in range(6):
            process_one_task(
                session_factory=session_factory,
                settings=runtime.settings,
                runtime=runtime,
            )
            with session_factory() as db:
                task = db.get(BackgroundTaskRecord, bad_task_id)
            if task is not None and task.attempts > 0:
                break

        with session_factory() as db:
            task = db.get(BackgroundTaskRecord, bad_task_id)
            # The bad row failed and was retried (not deleted on success).
            assert task is not None
            assert task.attempts > 0
            # No completion wake was enqueued for a bad research_run.
            assert (
                db.scalars(
                    select(BackgroundTaskRecord).where(
                        BackgroundTaskRecord.task_type == "agent_wake"
                    )
                ).all()
                == []
            )


# ===========================================================================
# 3. The completion agent_wake arm — finding rendered into context, tainted
# ===========================================================================


def test_agent_wake_context_research_completion_is_tainted_and_targets_session() -> None:
    """A completion ``agent_wake`` payload builds a ``research_completion``
    ``WakeContext`` that targets the carried ``session_id``, renders the finding
    into ``prompt_text``, and carries a TAINTED ``ingress_provenance`` — the
    finding's text is model-authored over untrusted content, so taint is the
    containment that keeps a prompt-injected finding from authorizing an action.
    A plain note wake keeps its untainted ``scheduled_task`` path."""

    finding = ResearchFinding(
        question="What changed?",
        mode="web",
        status="complete",
        summary="A summary.",
        claims=[{"statement": "X", "sources": [], "confidence": "low"}],
        gaps=["Y"],
        sources=[{"title": "S", "reference": "r", "retrieved_at": "2026-06-01T12:00:00Z"}],
    )
    session_id, wake_context = _agent_wake_context(
        {
            "research_finding": {
                "question": finding.question,
                "mode": finding.mode,
                "status": finding.status,
                "summary": finding.summary,
                "claims": finding.claims,
                "gaps": finding.gaps,
                "sources": finding.sources,
            },
            "session_id": "ses_target",
        }
    )
    # Targets the carried session, not the active one.
    assert session_id == "ses_target"
    assert wake_context.trigger_kind == "research_completion"
    # The finding is rendered as a clearly-attributed block.
    assert wake_context.prompt_text == render_finding(finding)
    assert "Research run result" in wake_context.prompt_text
    assert "status: complete" in wake_context.prompt_text
    assert "A summary." in wake_context.prompt_text
    # The wake is carried TAINTED.
    assert wake_context.ingress_provenance is not None
    assert wake_context.ingress_provenance.status == "tainted"

    # A plain note wake keeps None target (active session) and no taint.
    plain_session_id, plain_context = _agent_wake_context({"note": "follow up"})
    assert plain_session_id is None
    assert plain_context.trigger_kind == "scheduled_task"
    assert plain_context.prompt_text == "follow up"
    assert plain_context.ingress_provenance is None


def test_worker_completion_wake_renders_finding_into_main_agent_context(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due completion ``agent_wake`` wakes the main agent: the finding is
    rendered into the model's context as a clearly-attributed research-result
    block, and the agent answers from it. The wake is run through ``_wake`` on
    the carried session, exactly as the agency-completion class of wake."""

    _stub_memory_retriever(monkeypatch)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: NOW)

    @dataclass
    class _MainAgentAdapter:
        """The main agent: emits a message; records its context items."""

        provider: str = "provider.main"
        model: str = "model.main-v1"
        snapshots: list[list[dict[str, Any]]] = field(default_factory=list)

        def create_response(
            self,
            *,
            input_items: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            user_message: str,
            history: list[dict[str, Any]],
            context_bundle: dict[str, Any],
        ) -> dict[str, Any]:
            if is_retriever_call(input_items):
                return empty_recall_response(provider=self.provider, model=self.model)
            del tools, user_message, history, context_bundle
            self.snapshots.append(list(input_items))
            source = "agent.emit_message(text='Here is what the research found.')\n"
            return {
                "provider": self.provider,
                "model": self.model,
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                "provider_response_id": f"resp_main_{len(self.snapshots)}",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_main",
                        "call_id": "call_main",
                        "name": "run",
                        "arguments": json.dumps({"source": source}, sort_keys=True),
                        "status": "completed",
                    }
                ],
            }

    adapter = _MainAgentAdapter()
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
        reset_database=True,
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        session_response = client.get("/v1/sessions/active")
        assert session_response.status_code == 200
        session_id = session_response.json()["session"]["id"]

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
                        "session_id": session_id,
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
    rendered = json.dumps(adapter.snapshots[0])
    assert "Research run result" in rendered
    assert "Paris is the capital of France." in rendered
    assert "research.investigate call" in rendered

    with session_factory() as db:
        # The completion wake ran as a normal agent_turn on the carried session
        # and the agent answered from the finding.
        turn = db.scalar(
            select(TurnRecord)
            .where(TurnRecord.session_id == session_id, TurnRecord.kind == "agent_turn")
            .order_by(TurnRecord.created_at.desc())
        )
        assert turn is not None
        assert turn.status == "completed"
        assert turn.assistant_message == "Here is what the research found."
