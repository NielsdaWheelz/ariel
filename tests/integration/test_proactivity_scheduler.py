from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import RuntimeProvenance
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from tests.integration.app_helpers import create_test_app
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    EventRecord,
    MemoryLogRecord,
    SessionRecord,
    TurnRecord,
    enqueue_background_task,
)
from ariel.worker import _deliver_to_discord, _discord_delivery_nonce, process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from ariel.model_adapter import ModelCall, ModelResponse
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    drain_task,
    empty_recall_response,
    is_memory_subsystem_call,
    last_user_message,
    run_response,
    responses_run_message,
    responses_with_run_calls,
    run_function_calls,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _stub_memory_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub embed_text so the per-turn ``recall`` is hermetic: writes get a
    null vector and search runs purely on tsquery — no network."""
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)


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


def test_schedule_syscall_queue_defect_rolls_back_instead_of_failing_action(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_sched_defect",
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
                    id="trn_sched_defect",
                    session_id="ses_sched_defect",
                    user_message="set a reminder",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    def fail_enqueue(*_args: Any, **_kwargs: Any) -> BackgroundTaskRecord:
        raise RuntimeError("queue bug")

    monkeypatch.setattr("ariel.action_runtime.enqueue_background_task", fail_enqueue)

    with pytest.raises(RuntimeError, match="queue bug"):
        with session_factory() as db:
            with db.begin():
                turn = db.get(TurnRecord, "trn_sched_defect")
                assert turn is not None
                run_function_calls(
                    db=db,
                    session_id="ses_sched_defect",
                    turn=turn,
                    function_calls_raw=[
                        {
                            "call_id": "call_sched_defect",
                            "capability_id": "cap.proactive.schedule",
                            "input": {
                                "when": "2026-06-02T09:00:00Z",
                                "note": "check whether the PR landed",
                            },
                            "influenced_by_untrusted_content": False,
                        }
                    ],
                    approval_ttl_seconds=300,
                    approval_actor_id="usr_sched_defect",
                    add_event=lambda _event_type, _payload: None,
                    now_fn=lambda: NOW,
                    new_id_fn=lambda prefix: f"{prefix}_sched_defect_1",
                    allowed_capability_ids=["cap.proactive.schedule"],
                    runtime_provenance=RuntimeProvenance(status="clean"),
                )

    with session_factory() as db:
        attempts = db.scalars(select(ActionAttemptRecord)).all()
        tasks = db.scalars(select(BackgroundTaskRecord)).all()
    assert attempts == []
    assert tasks == []


def test_approved_schedule_queue_defect_retries_task_without_failing_action(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = get_capability("cap.proactive.schedule")
    assert capability is not None
    normalized_input = {
        "when": "2026-06-02T09:00:00Z",
        "note": "check whether the PR landed",
    }
    action_payload = canonical_action_payload(
        capability_id=capability.capability_id,
        input_payload=normalized_input,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_sched_approved_defect",
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
                    id="trn_sched_approved_defect",
                    session_id="ses_sched_approved_defect",
                    user_message="set a reminder",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_sched_approved_defect",
                    session_id="ses_sched_approved_defect",
                    turn_id="trn_sched_approved_defect",
                    proposal_index=1,
                    capability_id=capability.capability_id,
                    capability_version=capability.version,
                    capability_contract_hash=capability_contract_hash(capability),
                    impact_level=capability.impact_level,
                    proposed_input=normalized_input,
                    payload_hash=payload_hash(action_payload),
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="executing",
                    approval_required=True,
                    execution_output=None,
                    execution_error=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                BackgroundTaskRecord(
                    id="tsk_sched_approved_defect",
                    task_type="execute_action_attempt",
                    idempotency_key=None,
                    provider_write_receipt_id=None,
                    payload={"action_attempt_id": "aat_sched_approved_defect"},
                    attempts=0,
                    recurrence_seconds=None,
                    run_after=datetime(2026, 1, 1, tzinfo=UTC),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    def fail_enqueue(*_args: Any, **_kwargs: Any) -> BackgroundTaskRecord:
        raise RuntimeError("queue bug")

    monkeypatch.setattr("ariel.action_runtime.enqueue_background_task", fail_enqueue)

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        action = db.get(ActionAttemptRecord, "aat_sched_approved_defect")
        task = db.get(BackgroundTaskRecord, "tsk_sched_approved_defect")
        agent_wakes = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
        ).all()
    assert action is not None
    assert action.status == "executing"
    assert action.execution_error is None
    assert task is not None
    assert task.attempts == 1
    assert agent_wakes == []


# ===========================================================================
# (c) The worker's agent_wake arm wakes the agent
# ===========================================================================


class _WakeAdapter(FakeModelAdapter):
    """A model adapter whose single ``run`` program emits a message. It records
    the ``user_message`` of every turn so the test can assert the worker handed
    the scheduled note to ``_wake``."""

    provider = "provider.wake"
    model = "model.wake-v1"

    def __init__(self, *, retriever_source: str | None = None) -> None:
        super().__init__()
        self.user_messages_seen: list[str] = []
        self.retriever_source = retriever_source

    def _respond(self, request: ModelCall) -> ModelResponse:
        user_message = last_user_message(request.messages)
        if is_memory_subsystem_call(request.messages):
            if self.retriever_source is not None:
                return run_response(
                    source=self.retriever_source,
                    provider=self.provider,
                    model=self.model,
                    provider_response_id=f"resp_retriever_{len(self.user_messages_seen)}",
                )
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        self.user_messages_seen.append(user_message)
        return responses_run_message(
            assistant_text="handled the scheduled wake",
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_wake_{len(self.user_messages_seen)}",
        )


class _NonTerminalWakeAdapter(FakeModelAdapter):
    provider = "provider.wake"
    model = "model.wake-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        if is_memory_subsystem_call(request.messages):
            return empty_recall_response(
                provider=self.provider, model=self.model, messages=request.messages
            )
        return responses_with_run_calls(
            calls=[{"name": "agent.emit_value", "input": {"value": {"routine": True}}}],
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_nonterminal_wake",
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
    monkeypatch.setattr("ariel.worker.utcnow", lambda: now)
    adapter = _WakeAdapter()
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
            wake_log = db.scalar(
                select(MemoryLogRecord).where(
                    MemoryLogRecord.kind == "proactive_trigger",
                    MemoryLogRecord.content == "follow up on the deploy",
                    MemoryLogRecord.turn_id == turn.id,
                )
            )
            assert wake_log is not None
            assert wake_log.taint == "clean"


def test_worker_provider_sync_agent_wake_invokes_normal_wake_with_tainted_context(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_memory_retriever(monkeypatch)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: now)
    adapter = _WakeAdapter(
        retriever_source=(
            "memory.search(query='Launch checklist due today', limit=1)\n"
            "agent.emit_finding(summary='', claims=[], gaps=[], sources=[])\n"
        )
    )
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
                db.add(
                    BackgroundTaskRecord(
                        id="tsk_provider_sync_review",
                        task_type="agent_wake",
                        idempotency_key=None,
                        provider_write_receipt_id=None,
                        payload={
                            "kind": "provider_sync_review",
                            "provider": "google",
                            "resource_type": "gmail",
                            "resource_id": "primary",
                            "sync_run_id": "syn_provider_sync_review",
                            "provider_event_id": "pev_provider_sync_review",
                            "item_count": 1,
                            "observation_count": 1,
                            "cursor_before": "hist-1",
                            "cursor_after": "hist-2",
                            "items": [
                                {
                                    "change": "messagesAdded",
                                    "message_id": "msg_urgent",
                                    "thread_id": "thr_urgent",
                                    "subject": "Launch checklist due today",
                                    "sender": {"email": "manager@example.com"},
                                    "direction": "received",
                                    "labels": ["INBOX", "IMPORTANT"],
                                    "source_timestamp": "2026-06-01T11:55:00Z",
                                    "read_outcome": "ok",
                                    "evidence_blocks": [
                                        {
                                            "kind": "body",
                                            "text": "Please send the launch checklist by 5pm.",
                                        }
                                    ],
                                }
                            ],
                            "omitted_item_count": 0,
                            "note": (
                                "A Google Gmail sync found 1 new inbound message. "
                                "Review the new mail and decide whether anything matters."
                            ),
                        },
                        attempts=0,
                        recurrence_seconds=None,
                        run_after=now - timedelta(minutes=1),
                        created_at=now - timedelta(minutes=5),
                        updated_at=now - timedelta(minutes=5),
                    )
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

    assert len(adapter.user_messages_seen) == 1
    user_message = adapter.user_messages_seen[0]
    assert "Provider sync wake: Google Gmail" in user_message
    assert "Launch checklist due today" in user_message
    assert "Please send the launch checklist by 5pm." in user_message
    with session_factory() as db:
        task = db.get(BackgroundTaskRecord, "tsk_provider_sync_review")
        turn = db.scalar(
            select(TurnRecord).where(
                TurnRecord.source_background_task_id == "tsk_provider_sync_review"
            )
        )
        wake_log = db.scalar(
            select(MemoryLogRecord).where(
                MemoryLogRecord.kind == "proactive_trigger",
                MemoryLogRecord.content.like("Provider sync wake:%"),
            )
        )

    assert task is None
    assert turn is not None
    assert turn.status == "completed"
    assert wake_log is not None
    assert wake_log.taint == "tainted"
    with session_factory() as db:
        proposed_events = db.scalars(
            select(EventRecord)
            .where(EventRecord.turn_id == turn.id)
            .where(EventRecord.event_type == "evt.action.proposed")
            .order_by(EventRecord.sequence.asc())
        ).all()

    memory_search_events = [
        event
        for event in proposed_events
        if event.payload.get("capability_id") == "cap.memory.search"
    ]
    assert len(memory_search_events) == 1
    taint = memory_search_events[0].payload["taint"]
    assert taint["provenance_status"] == "tainted"
    assert taint["runtime_provenance"]["status"] == "tainted"
    assert taint["runtime_provenance"]["evidence"] == [
        {
            "kind": "provider_sync_review",
            "provider": "google",
            "resource_type": "gmail",
            "resource_id": "primary",
            "sync_run_id": "syn_provider_sync_review",
            "provider_event_id": "pev_provider_sync_review",
            "item_count": 1,
            "observation_count": 1,
        }
    ]


def test_provider_sync_wake_budget_exhaustion_stays_silent(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_memory_retriever(monkeypatch)
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "1")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_SOFT", "300.0")
    monkeypatch.setenv("ARIEL_TURN_BUDGET_SECONDS_HARD", "300.0")
    monkeypatch.setenv("ARIEL_DISCORD_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("ARIEL_DISCORD_CHANNEL_ID", "987654321")
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: now)
    discord_posts: list[dict[str, Any]] = []

    class _DiscordResponse:
        def raise_for_status(self) -> None:
            return None

    def fake_discord_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> _DiscordResponse:
        discord_posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _DiscordResponse()

    monkeypatch.setattr("ariel.worker.httpx.post", fake_discord_post)
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=_NonTerminalWakeAdapter(),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        with session_factory() as db:
            with db.begin():
                db.add(
                    BackgroundTaskRecord(
                        id="tsk_provider_sync_exhaustion",
                        task_type="agent_wake",
                        idempotency_key=None,
                        provider_write_receipt_id=None,
                        payload={
                            "kind": "provider_sync_review",
                            "provider": "google",
                            "resource_type": "gmail",
                            "resource_id": "primary",
                            "sync_run_id": "syn_provider_sync_exhaustion",
                            "provider_event_id": "pev_provider_sync_exhaustion",
                            "item_count": 1,
                            "observation_count": 1,
                            "cursor_before": "hist-1",
                            "cursor_after": "hist-2",
                            "items": [{"change": "messagesAdded", "message_id": "msg_1"}],
                            "omitted_item_count": 0,
                            "note": "A Google Gmail sync found 1 new inbound message.",
                        },
                        attempts=0,
                        recurrence_seconds=None,
                        run_after=now - timedelta(minutes=1),
                        created_at=now - timedelta(minutes=5),
                        updated_at=now - timedelta(minutes=5),
                    )
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

    with session_factory() as db:
        turn = db.scalar(
            select(TurnRecord).where(
                TurnRecord.source_background_task_id == "tsk_provider_sync_exhaustion"
            )
        )

    assert turn is not None
    assert turn.status == "completed"
    assert turn.assistant_message == ""
    assert discord_posts == []


def test_schedule_run_program_worker_drains_due_wake_end_to_end(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual task row created by ``cap.proactive.schedule`` is consumed by
    the worker's ``agent_wake`` arm and recorded as a completed scheduled turn."""

    _stub_memory_retriever(monkeypatch)
    current_now = {"value": NOW}
    monkeypatch.setattr("ariel.worker.utcnow", lambda: current_now["value"])
    monkeypatch.setattr("ariel.app.utcnow", lambda: current_now["value"])
    adapter = _WakeAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        session_response = client.get("/v1/sessions/active")
        assert session_response.status_code == 200
        session_id = session_response.json()["session"]["id"]
        with session_factory() as db:
            with db.begin():
                turn = TurnRecord(
                    id="trn_sched_e2e",
                    session_id=session_id,
                    user_message="schedule this follow-up",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW - timedelta(minutes=5),
                    updated_at=NOW - timedelta(minutes=5),
                )
                db.add(turn)
                ctx = run_function_calls(
                    db=db,
                    session_id=session_id,
                    turn=turn,
                    function_calls_raw=[
                        {
                            "call_id": "call_sched_e2e",
                            "capability_id": "cap.proactive.schedule",
                            "input": {
                                "when": "2026-06-01T11:59:00Z",
                                "note": "follow up on the e2e schedule",
                            },
                            "influenced_by_untrusted_content": False,
                        }
                    ],
                    approval_ttl_seconds=300,
                    approval_actor_id="usr_sched_e2e",
                    add_event=lambda _event_type, _payload: None,
                    now_fn=lambda: NOW,
                    new_id_fn=lambda prefix: f"{prefix}_sched_e2e",
                    allowed_capability_ids=["cap.proactive.schedule"],
                    runtime_provenance=RuntimeProvenance(status="clean"),
                )
                turn.status = "completed"
                turn.assistant_message = "scheduled"
                turn.updated_at = NOW - timedelta(minutes=4)

        assert ctx.blocked_reasons == []
        assert len(ctx.inline_results) == 1
        output = ctx.inline_results[0]["output"]
        assert output["status"] == "scheduled"
        scheduled_task_id = output["task_id"]
        drain_task(client, scheduled_task_id)

    assert adapter.user_messages_seen == ["follow up on the e2e schedule"]
    with session_factory() as db:
        assert db.get(BackgroundTaskRecord, scheduled_task_id) is None
        wake_turn = db.scalar(
            select(TurnRecord).where(TurnRecord.user_message == "follow up on the e2e schedule")
        )
        assert wake_turn is not None
        assert wake_turn.status == "completed"
        wake_log = db.scalar(
            select(MemoryLogRecord).where(
                MemoryLogRecord.kind == "proactive_trigger",
                MemoryLogRecord.content == "follow up on the e2e schedule",
                MemoryLogRecord.turn_id == wake_turn.id,
            )
        )
        assert wake_log is not None
        assert wake_log.taint == "clean"


def test_worker_user_message_arm_invokes_wake_for_target_session(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due ``user_message`` row targets the specified session: the worker builds a
    ``user_message`` wake-context from the payload and calls ``_wake`` on exactly
    the session_id supplied in the task — without creating or loading the active
    session. The turn is recorded and the task deleted."""

    _stub_memory_retriever(monkeypatch)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: now)
    adapter = _WakeAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory

        # Seed an active session through the public session endpoint.
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


def test_worker_user_message_replay_reuses_committed_turn_without_model_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the worker crashes after committing a user-message turn but before
    deleting the task row, replay must not run the model again."""

    _stub_memory_retriever(monkeypatch)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: now)
    monkeypatch.setenv("ARIEL_DISCORD_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("ARIEL_DISCORD_CHANNEL_ID", "987654321")
    discord_posts: list[dict[str, Any]] = []

    class _DiscordResponse:
        def raise_for_status(self) -> None:
            return None

    def fake_discord_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> _DiscordResponse:
        discord_posts.append(
            {
                "url": url,
                "headers": headers,
                "json": dict(json),
                "timeout": timeout,
            }
        )
        return _DiscordResponse()

    monkeypatch.setattr("ariel.worker.httpx.post", fake_discord_post)
    adapter = _WakeAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        session_response = client.get("/v1/sessions/active")
        assert session_response.status_code == 200
        session_id = session_response.json()["session"]["id"]

        with session_factory() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="user_message",
                    payload={
                        "session_id": session_id,
                        "message": "recover this turn exactly once",
                        "discord_context": None,
                        "attachment_sources": None,
                    },
                    now=now - timedelta(minutes=5),
                    run_after=now - timedelta(minutes=1),
                )
                task_id = task.id

        def crash_after_discord_send(**kwargs: Any) -> None:
            _deliver_to_discord(**kwargs)
            raise RuntimeError("simulated crash after committed turn")

        monkeypatch.setattr("ariel.worker._deliver_to_discord", crash_after_discord_send)
        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )
        assert adapter.user_messages_seen == ["recover this turn exactly once"]
        assert len(discord_posts) == 1

        with session_factory() as db:
            with db.begin():
                task = db.get(BackgroundTaskRecord, task_id)
                assert task is not None
                assert task.attempts == 1
                task.run_after = now - timedelta(seconds=1)
                committed_turn = db.scalar(
                    select(TurnRecord).where(TurnRecord.source_background_task_id == task_id)
                )
                assert committed_turn is not None
                assert committed_turn.status == "completed"

        monkeypatch.setattr("ariel.worker._deliver_to_discord", _deliver_to_discord)
        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

        assert adapter.user_messages_seen == ["recover this turn exactly once"]
        assert len(discord_posts) == 2
        assert discord_posts[0]["url"] == discord_posts[1]["url"]
        assert discord_posts[0]["json"] == discord_posts[1]["json"]
        assert discord_posts[0]["json"]["nonce"] == _discord_delivery_nonce(
            turn_id=committed_turn.id,
            channel_id=987654321,
        )
        assert discord_posts[0]["json"]["enforce_nonce"] is True
        with session_factory() as db:
            with db.begin():
                assert db.get(BackgroundTaskRecord, task_id) is None
                turns = db.scalars(
                    select(TurnRecord).where(
                        TurnRecord.user_message == "recover this turn exactly once"
                    )
                ).all()
                assert len(turns) == 1


def test_worker_user_message_replay_fails_interrupted_turn_without_model_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-progress source turn means the earlier process crashed mid-turn.
    Recovery fails that turn and consumes the task instead of replaying model work."""

    _stub_memory_retriever(monkeypatch)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker.utcnow", lambda: now)
    adapter = _WakeAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        session_factory = runtime.session_factory
        session_response = client.get("/v1/sessions/active")
        assert session_response.status_code == 200
        session_id = session_response.json()["session"]["id"]

        with session_factory() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="user_message",
                    payload={
                        "session_id": session_id,
                        "message": "do not replay this interrupted turn",
                        "discord_context": None,
                        "attachment_sources": None,
                    },
                    now=now - timedelta(minutes=5),
                    run_after=now - timedelta(minutes=1),
                )
                task_id = task.id
                turn = TurnRecord(
                    id="trn_interrupted_replay",
                    session_id=session_id,
                    user_message="do not replay this interrupted turn",
                    assistant_message=None,
                    status="in_progress",
                    source_background_task_id=task_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(turn)
                db.add(
                    EventRecord(
                        id="evn_interrupted_started",
                        session_id=session_id,
                        turn_id=turn.id,
                        sequence=1,
                        event_type="evt.turn.started",
                        payload={"message": turn.user_message, "discord": None},
                        created_at=now,
                    )
                )

        assert process_one_task(
            session_factory=session_factory,
            settings=runtime.settings,
            runtime=runtime,
        )

        assert adapter.user_messages_seen == []
        with session_factory() as db:
            with db.begin():
                assert db.get(BackgroundTaskRecord, task_id) is None
                turn = db.get(TurnRecord, "trn_interrupted_replay")
                assert turn is not None
                assert turn.status == "failed"
                failure = db.scalar(
                    select(EventRecord).where(
                        EventRecord.turn_id == turn.id,
                        EventRecord.event_type == "evt.turn.failed",
                    )
                )
                assert failure is not None
                assert failure.payload["error_code"] == "E_BACKGROUND_TURN_INTERRUPTED"
