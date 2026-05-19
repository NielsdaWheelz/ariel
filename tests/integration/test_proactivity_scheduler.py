from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any

from fastapi.testclient import TestClient
import pytest
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
from ariel.worker import process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import responses_run_message, run_function_calls

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


# ===========================================================================
# (b) The schedule syscall writes one agent_wake row
# ===========================================================================


def test_schedule_syscall_writes_an_agent_wake_background_task(
    session_factory: sessionmaker[Session],
) -> None:
    """A program calling ``cap.proactive.schedule`` runs inline — no durable
    execution queue — and writes exactly one ``background_tasks`` row with
    ``task_type=agent_wake``, the note as its payload, and ``run_after`` set to
    the requested wake time. The syscall returns the new task identity."""

    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_sched",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_sched",
                    session_id="ses_sched",
                    user_message="set a reminder",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "trn_sched")
            assert turn is not None
            ctx = run_function_calls(
                db=db,
                session_id="ses_sched",
                turn=turn,
                function_calls_raw=[
                    {
                        "call_id": "call_sched",
                        "capability_id": "cap.proactive.schedule",
                        "input": {
                            "when": "2026-06-02T09:00:00Z",
                            "note": "check whether the PR landed",
                        },
                        "influenced_by_untrusted_content": False,
                    }
                ],
                approval_ttl_seconds=300,
                approval_actor_id="usr_sched",
                add_event=lambda event_type, payload: events.append((event_type, payload)),
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_sched_1",
                allowed_capability_ids=["cap.proactive.schedule"],
                runtime_provenance=RuntimeProvenance(status="clean"),
            )

    assert ctx.blocked_reasons == []
    assert len(ctx.inline_results) == 1
    output = ctx.inline_results[0]["output"]
    assert output["status"] == "scheduled"
    assert output["run_after"] == "2026-06-02T09:00:00Z"
    scheduled_task_id = output["task_id"]

    with session_factory() as db:
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
        ).all()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.id == scheduled_task_id
        assert task.payload == {"note": "check whether the PR landed"}
        assert task.run_after == datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
        assert task.recurrence_seconds is None
        assert task.attempts == 0
        # The syscall is inline: it never produced an execute_action_attempt row.
        execute_tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "execute_action_attempt"
            )
        ).all()
        assert execute_tasks == []


def test_schedule_syscall_rejects_a_malformed_when(
    session_factory: sessionmaker[Session],
) -> None:
    """A bad ``when`` fails the syscall closed: the call is blocked, no
    ``agent_wake`` row is written, and the program sees a failure."""

    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_bad",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_bad",
                    session_id="ses_bad",
                    user_message="set a reminder",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "trn_bad")
            assert turn is not None
            ctx = run_function_calls(
                db=db,
                session_id="ses_bad",
                turn=turn,
                function_calls_raw=[
                    {
                        "call_id": "call_bad",
                        "capability_id": "cap.proactive.schedule",
                        "input": {"when": "tomorrow morning", "note": "do the thing"},
                        "influenced_by_untrusted_content": False,
                    }
                ],
                approval_ttl_seconds=300,
                approval_actor_id="usr_bad",
                add_event=lambda event_type, payload: events.append((event_type, payload)),
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_bad_1",
                allowed_capability_ids=["cap.proactive.schedule"],
                runtime_provenance=RuntimeProvenance(status="clean"),
            )

    assert ctx.blocked_reasons != []
    assert ctx.inline_results == []
    with session_factory() as db:
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
        ).all()
        assert tasks == []


# ===========================================================================
# (c) The worker's agent_wake arm wakes the agent
# ===========================================================================


@dataclass
class _WakeAdapter:
    """A model adapter whose single ``run`` program emits a message. It records
    the ``user_message`` of every turn so the test can assert the worker handed
    the scheduled note to ``_wake``."""

    provider: str = "provider.wake"
    model: str = "model.wake-v1"
    user_messages_seen: list[str] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, history, context_bundle
        self.user_messages_seen.append(user_message)
        return responses_run_message(
            assistant_text="handled the scheduled wake",
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_wake_{len(self.user_messages_seen)}",
        )


def test_worker_agent_wake_arm_invokes_wake_for_a_due_task(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due ``agent_wake`` row is a normal turn: the worker resolves the active
    session, builds a ``scheduled_task`` wake-context from the row's note, and
    runs the agent loop. The turn is recorded and the task completes."""

    _stub_memory_retriever(monkeypatch)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    adapter = _WakeAdapter()
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
        reset_database=True,
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        with session_factory() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="agent_wake",
                    payload={"note": "follow up on the deploy"},
                    now=now - timedelta(minutes=5),
                    run_after=now - timedelta(minutes=1),
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

    assert adapter.user_messages_seen == ["follow up on the deploy"]
    with session_factory() as db:
        with db.begin():
            # A one-shot task is deleted on success: no agent_wake row remains.
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
            assert wake_tasks == []
            # _wake recorded the scheduled wake as a session turn carrying the
            # note as the turn's user_message.
            turn = db.scalar(
                select(TurnRecord).where(TurnRecord.user_message == "follow up on the deploy")
            )
            assert turn is not None
            assert turn.status == "completed"


def test_worker_user_message_arm_invokes_wake_for_target_session(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due ``user_message`` row targets the specified session: the worker builds a
    ``user_message`` wake-context from the payload and calls ``_wake`` on exactly
    the session_id supplied in the task — without calling
    ``_get_or_create_active_session``. The turn is recorded and the task deleted."""

    _stub_memory_retriever(monkeypatch)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    adapter = _WakeAdapter()
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
        reset_database=True,
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        # Seed an active session by hitting the sessions endpoint, which calls
        # _get_or_create_active_session and creates a row in the database.
        session_response = client.get("/v1/sessions/active")
        assert session_response.status_code == 200
        session_id = session_response.json()["session"]["id"]

        with session_factory() as db:
            with db.begin():
                enqueue_background_task(
                    db,
                    task_type="user_message",
                    payload={
                        "session_id": session_id,
                        "message": "what is on my calendar today?",
                        "discord_context": None,
                        "attachment_sources": None,
                    },
                    now=now - timedelta(minutes=5),
                    run_after=now - timedelta(minutes=1),
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

    assert adapter.user_messages_seen == ["what is on my calendar today?"]
    with session_factory() as db:
        with db.begin():
            # A one-shot task is deleted on success: no user_message row remains.
            user_msg_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "user_message")
            ).all()
            assert user_msg_tasks == []
            # _wake recorded the turn with the message text.
            turn = db.scalar(
                select(TurnRecord).where(TurnRecord.user_message == "what is on my calendar today?")
            )
            assert turn is not None
            assert turn.status == "completed"
