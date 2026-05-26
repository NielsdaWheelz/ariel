"""Memory substrate contract tests.

These tests define the memory subsystem's required behavior.
Contracts: ``memory_log``/``memory_notes`` schema; append-only trigger;
capability surface; pre-turn retrieval; recall non-fatality; ``memory.remember``
enqueueing; worker dispatch; ai_judgments types; log event accumulation; note
mutability; append-only via SQLAlchemy session.

Every canned response is a structural fixture and every assertion is structural.
No test asserts that the model "chose correctly."
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import process_action_execution_task
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    capability_id_for_run_callable,
    get_capability,
    payload_hash,
)
from ariel.config import AppSettings
from ariel.model_adapter import ModelAdapter, ModelCall, ModelResponse
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    EventRecord,
    MemoryLogRecord,
    MemoryNoteRecord,
    TurnRecord,
    enqueue_background_task,
)
from ariel.worker import process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import (
    FakeModelAdapter,
    drain_task,
    last_user_message,
    post_message_and_drain,
    run_response,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_id_counter = count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_mt_{next(_id_counter)}"


NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)

_RETRIEVER_PROGRAM = "agent.emit_finding(summary='no memories',claims=[],gaps=[],sources=[])\n"

_EMIT_MSG = "agent.emit_message(text='hello')\n"


def _app(postgres_url: str, adapter: ModelAdapter, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a migrated app with embed_text stubbed out."""
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    return create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )


def _seed_executing_memory_action(
    session_factory: sessionmaker[Session],
    *,
    action_attempt_id: str,
    capability_id: str,
    proposed_input: dict[str, Any],
) -> None:
    capability = get_capability(capability_id)
    assert capability is not None
    with session_factory() as db:
        with db.begin():
            db.add(
                TurnRecord(
                    id="trn_memory_action",
                    user_message="memory action",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id=action_attempt_id,
                    turn_id="trn_memory_action",
                    proposal_index=1,
                    capability_id=capability_id,
                    capability_version=capability.version,
                    capability_contract_hash=capability_contract_hash(capability),
                    impact_level=capability.impact_level,
                    proposed_input=proposed_input,
                    payload_hash=payload_hash(
                        canonical_action_payload(
                            capability_id=capability_id,
                            input_payload=proposed_input,
                        )
                    ),
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


def _sf(client: TestClient) -> sessionmaker[Session]:
    return cast(Any, client.app).state.runtime.session_factory


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _run_response(source: str, *, idx: int) -> ModelResponse:
    """Build a one-tool-call ``ModelResponse`` carrying the run program ``source``."""
    return run_response(
        source=source,
        provider="provider.test",
        model="model.test",
        provider_response_id=f"resp_{idx}",
        input_tokens=1,
        output_tokens=1,
    )


class _TwoPhaseAdapter(FakeModelAdapter):
    """Odd calls → retriever (emit_finding); even calls → main agent (emit_message)."""

    provider = "provider.test"
    model = "model.test"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0
        self.snapshots: list[list[Any]] = []

    def _respond(self, request: ModelCall) -> ModelResponse:
        self.call_count += 1
        self.snapshots.append(list(request.messages))
        source = _RETRIEVER_PROGRAM if self.call_count % 2 == 1 else _EMIT_MSG
        return _run_response(source, idx=self.call_count)


class _MemoryRecallSyscallAdapter(FakeModelAdapter):
    """Pre-turn retriever, main-agent recall syscall, then syscall retriever."""

    provider = "provider.test"
    model = "model.test"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        self.call_count += 1
        if self.call_count == 2:
            source = (
                "result = memory.recall(query='incident recovery')\n"
                "assert result['status'] == 'recalled', result\n"
                "agent.emit_value(value={'recall': 'ok'})\n"
            )
        elif self.call_count == 4:
            source = "agent.emit_message(text='recall syscall ok')\n"
        else:
            source = _RETRIEVER_PROGRAM
        return _run_response(source, idx=self.call_count)


class _FailingRetrieverAdapter(FakeModelAdapter):
    """Retriever emits the same source twice → stuck-detection ends it with no finding.

    On calls 1 and 2 (both retriever rounds) the same emit_value source is
    returned; stuck-detection fires after the duplicate and the retriever exits
    with ``budget_exhausted`` and no ``emitted_finding``. Call 3 is the main
    agent, which emits a message.
    """

    provider = "provider.test"
    model = "model.test"
    _stuck_source = "agent.emit_value(value={'stuck':1})\n"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        self.call_count += 1
        source = self._stuck_source if self.call_count <= 2 else _EMIT_MSG
        return _run_response(source, idx=self.call_count)


class _InvalidRecallAdapter(FakeModelAdapter):
    """Retriever emits a malformed recall_v1 finding; main agent still answers."""

    provider = "provider.test"
    model = "model.test"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        self.call_count += 1
        if self.call_count == 1:
            source = (
                "agent.emit_finding("
                "summary='bad recall',"
                "claims=[{'id':'missing required recall fields'}],"
                "gaps=[],sources=[])\n"
            )
        else:
            source = _EMIT_MSG
        return _run_response(source, idx=self.call_count)


class _RememberAdapter(FakeModelAdapter):
    """Retriever on odd calls; main agent calls memory.remember then emits on even."""

    provider = "provider.test"
    model = "model.test"
    note_text = "user prefers dark mode"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        self.call_count += 1
        if self.call_count % 2 == 1:
            source = _RETRIEVER_PROGRAM
        else:
            source = f"memory.remember(note={self.note_text!r})\n{_EMIT_MSG}"
        return _run_response(source, idx=self.call_count)


class _RememberThenEncodeAdapter(FakeModelAdapter):
    """Main turn enqueues a remember request; rememberer completes the encode task."""

    provider = "provider.test"
    model = "model.test"
    note_text = "user prefers dark mode"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        self.call_count += 1
        user_message = last_user_message(request.messages)
        if user_message != "remember this":
            source = "agent.emit_done(summary='remembered preference')\n"
        elif self.call_count % 2 == 1:
            source = _RETRIEVER_PROGRAM
        else:
            source = f"memory.remember(note={self.note_text!r})\n{_EMIT_MSG}"
        return _run_response(source, idx=self.call_count)


# ===========================================================================
# 1a. Schema — only memory_log and memory_notes under memory_*
# ===========================================================================


def test_schema_memory_tables_are_only_log_and_notes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``memory_log`` and ``memory_notes`` are the only ``memory_*`` tables."""
    engine = create_engine(postgres_url, future=True)
    with TestClient(_app(postgres_url, _TwoPhaseAdapter(), monkeypatch)):
        table_names = set(inspect(engine).get_table_names())
    memory_tables = {t for t in table_names if t.startswith("memory_")}
    assert memory_tables == {"memory_log", "memory_notes"}, (
        f"unexpected memory_* tables: {memory_tables}"
    )


# ===========================================================================
# 1b. Schema — memory_log is append-only (UPDATE raises)
# ===========================================================================


def test_schema_memory_log_append_only_update_raises(
    session_factory: sessionmaker[Session],
) -> None:
    """A raw UPDATE on ``memory_log`` raises with the trigger's message."""
    row = MemoryLogRecord(
        id=_new_id("mev"),
        created_at=datetime.now(tz=UTC),
        kind="user_message",
        content="original",
        embedding=None,
        turn_id=None,
        taint="clean",
        source_ref=None,
    )
    with session_factory() as db:
        with db.begin():
            db.add(row)
    with pytest.raises(
        (IntegrityError, OperationalError, ProgrammingError),
        match="memory_log is append-only",
    ):
        with session_factory() as db:
            with db.begin():
                db.execute(text("UPDATE memory_log SET content='x' WHERE id=:id"), {"id": row.id})


def test_schema_memory_log_append_only_delete_raises(
    session_factory: sessionmaker[Session],
) -> None:
    """A raw DELETE on ``memory_log`` raises with the trigger's message."""
    row = MemoryLogRecord(
        id=_new_id("mev"),
        created_at=datetime.now(tz=UTC),
        kind="user_message",
        content="to delete",
        embedding=None,
        turn_id=None,
        taint="clean",
        source_ref=None,
    )
    with session_factory() as db:
        with db.begin():
            db.add(row)
    with pytest.raises(
        (IntegrityError, OperationalError, ProgrammingError),
        match="memory_log is append-only",
    ):
        with session_factory() as db:
            with db.begin():
                db.execute(text("DELETE FROM memory_log WHERE id=:id"), {"id": row.id})


# ===========================================================================
# 3a. Capability surface — memory.* run-callables resolve correctly
# ===========================================================================

_EXPECTED_MEMORY_CALLABLES = {
    "memory.recall",
    "memory.remember",
    "memory.search",
    "memory.read",
    "memory.note.create",
    "memory.note.edit",
    "memory.note.delete",
}


def test_capability_surface_memory_callables_resolve() -> None:
    """Expected memory callables resolve to registered capabilities."""
    for name in _EXPECTED_MEMORY_CALLABLES:
        cap_id = capability_id_for_run_callable(name)
        assert cap_id is not None, f"{name!r} must resolve to a capability_id"
        assert get_capability(cap_id) is not None, f"capability {cap_id!r} must be registered"


def test_memory_recall_capability_is_inline_read() -> None:
    """``memory.recall`` reads memory; side effects motivated by it escalate later."""
    capability = get_capability("cap.memory.recall")
    assert capability is not None
    assert capability.policy_decision == "allow_inline"
    assert capability.impact_level == "read"


# ===========================================================================
# 4. Retriever fires pre-turn; injects recall_v1
# ===========================================================================


def test_retriever_fires_preturn_and_injects_recall_context(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The main agent's context includes the ``recall_v1`` reconstruction."""
    adapter = _TwoPhaseAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        post_message_and_drain(client, message="hello")

    assert adapter.call_count >= 2, "expected retriever + main agent calls"
    rendered = json.dumps(jsonable_encoder(adapter.snapshots[1]))  # main-agent snapshot
    assert "memory recall:" in rendered


def test_memory_recall_syscall_runs_retriever_inline(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _MemoryRecallSyscallAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        turn = post_message_and_drain(client, message="recall via syscall")
        timeline = client.get("/v1/events").json()

    assert turn.assistant_message == "recall syscall ok"
    assert adapter.call_count == 4
    attempts = [
        attempt
        for saved_turn in timeline["turns"]
        for attempt in saved_turn["surface_action_lifecycle"]
        if attempt["proposal"]["capability_id"] == "cap.memory.recall"
    ]
    assert len(attempts) == 1
    assert attempts[0]["execution"]["status"] == "succeeded"
    assert attempts[0]["execution"]["output"]["status"] == "recalled"


# ===========================================================================
# 5. Recall failure is non-fatal
# ===========================================================================


def test_recall_failure_is_nonfatal(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the retriever emits the same source twice (stuck-detection fires, no
    finding emitted), the main-agent turn still completes with the assistant message."""
    adapter = _FailingRetrieverAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        turn = post_message_and_drain(client, message="ping")
    assert turn.status == "completed"
    assert turn.assistant_message == "hello"


def test_recall_contract_violation_is_typed_nonfatal(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed recall findings are classified, not silently converted inside
    the retriever."""
    adapter = _InvalidRecallAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        turn = post_message_and_drain(client, message="ping")
        timeline = client.get("/v1/events").json()

    assert turn.status == "completed"
    assert turn.assistant_message == "hello"
    assert adapter.call_count == 2
    event_types = [
        event["event_type"] for saved_turn in timeline["turns"] for event in saved_turn["events"]
    ]
    assert "evt.memory.recall_failed" in event_types


# ===========================================================================
# 7. memory.remember dispatches a memory_encode task
# ===========================================================================


def test_memory_remember_enqueues_memory_encode_task(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``memory.remember(note='...')`` syscall enqueues exactly one
    ``memory_encode`` background task whose payload carries the note."""
    adapter = _RememberAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        sf = _sf(client)
        post_message_and_drain(client, message="remember this")
        with sf() as db:
            tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "memory_encode"
                )
            ).all()

    assert len(tasks) == 1, f"expected 1 memory_encode task, got {len(tasks)}"
    assert tasks[0].payload.get("note") == adapter.note_text


def test_memory_remember_enqueues_and_worker_records_encode_turn(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _RememberThenEncodeAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        sf = _sf(client)
        post_message_and_drain(client, message="remember this")
        runtime = cast(Any, client.app).state.runtime

        with sf() as db:
            task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "memory_encode"
                )
            )
            assert task is not None
            task_id = task.id

        for _ in range(5):
            assert process_one_task(session_factory=sf, settings=runtime.settings, runtime=runtime)
            with sf() as db:
                if db.get(BackgroundTaskRecord, task_id) is None:
                    break

        with sf() as db:
            assert db.get(BackgroundTaskRecord, task_id) is None
            encode_turn = db.scalar(
                select(TurnRecord).where(TurnRecord.source_background_task_id == task_id)
            )

        assert encode_turn is not None
        assert encode_turn.kind == "memory_encode"
        assert encode_turn.status == "completed"


def test_approved_memory_note_missing_fails_with_typed_memory_error(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    settings = AppSettings()
    _seed_executing_memory_action(
        session_factory,
        action_attempt_id="act_memory_missing_note",
        capability_id="cap.memory.note.edit",
        proposed_input={"id": "mno_missing", "content": "updated note"},
    )

    processed = process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_memory_missing_note",
        google_runtime=None,
        agency_runtime=None,
        model_adapter=FakeModelAdapter(),
        settings=settings,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_memory_missing_note",
    )

    assert processed is True
    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_memory_missing_note")
    assert action_attempt is not None
    assert action_attempt.status == "failed"
    assert action_attempt.execution_error == "memory_note_not_found"


def test_approved_memory_action_unexpected_defect_propagates(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    settings = AppSettings()
    _seed_executing_memory_action(
        session_factory,
        action_attempt_id="act_memory_defect",
        capability_id="cap.memory.remember",
        proposed_input={"note": "remember this"},
    )

    def fail_memory(**_: Any) -> dict[str, Any]:
        raise RuntimeError("memory queue defect")

    monkeypatch.setattr("ariel.action_runtime._execute_memory_capability", fail_memory)
    with pytest.raises(RuntimeError, match="memory queue defect"):
        process_action_execution_task(
            session_factory=session_factory,
            action_attempt_id="act_memory_defect",
            google_runtime=None,
            agency_runtime=None,
            settings=settings,
            now_fn=lambda: NOW,
            new_id_fn=lambda prefix: f"{prefix}_memory_defect",
        )

    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_memory_defect")
    assert action_attempt is not None
    assert action_attempt.status == "executing"
    assert action_attempt.execution_error is None


# ===========================================================================
# 7b. Retriever and main proposal_index sharing
# ===========================================================================


class _RetrieverSearchesThenMainSearchesAdapter(FakeModelAdapter):
    """Retriever calls ``memory.search``; main agent then calls ``memory.search``.

    The main-agent search and message are split across two rounds because
    ``run_agent_loop`` rejects messages authored before read observations.
    """

    provider = "provider.test"
    model = "model.test"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        self.call_count += 1
        if self.call_count == 1:
            source = (
                "memory.search(query='ping', limit=3)\n"
                "agent.emit_finding(summary='ok',claims=[],gaps=[],sources=[])\n"
            )
        elif self.call_count == 2:
            source = "memory.search(query='ping', limit=3)\n"
        else:
            source = _EMIT_MSG
        return _run_response(source, idx=self.call_count)


def test_retriever_and_main_loop_action_attempts_do_not_collide(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the retriever creates ``action_attempts`` on the parent turn, the
    main loop's first capability call must not violate ``(turn_id,
    proposal_index)`` uniqueness."""
    adapter = _RetrieverSearchesThenMainSearchesAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        turn = post_message_and_drain(client, message="ping")
    assert turn.status == "completed", f"turn status={turn.status!r}"
    assert turn.assistant_message == "hello"


# ===========================================================================
# 8. Worker runs memory_encode and memory_dream
# ===========================================================================


def test_worker_accepts_memory_encode_and_memory_dream(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``memory_encode`` and ``memory_dream`` dispatch to the rememberer."""
    from ariel.persistence import TurnRecord, enqueue_background_task

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=_DreamCompleteAdapter(),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = cast(Any, client.app).state.runtime
        sf = runtime.session_factory
        for task_type, payload in [
            ("memory_encode", {"note": "n"}),
            ("memory_dream", {}),
        ]:
            with sf() as db:
                with db.begin():
                    task = enqueue_background_task(
                        db, task_type=task_type, payload=payload, now=NOW
                    )
                    task_id = task.id

            assert process_one_task(session_factory=sf, settings=runtime.settings, runtime=runtime)

            with sf() as db:
                assert db.get(BackgroundTaskRecord, task_id) is None
                turns = db.scalars(select(TurnRecord).where(TurnRecord.kind == task_type)).all()
            assert len(turns) == 1, f"expected one {task_type} turn"
            assert turns[0].status == "completed"


def test_worker_memory_task_replay_reuses_completed_turn_without_model_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed rememberer source turn makes task replay a no-op, not a
    second rememberer model run."""

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    adapter = _DreamCompleteAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = cast(Any, client.app).state.runtime
        sf = runtime.session_factory
        with sf() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="memory_dream",
                    payload={},
                    now=NOW,
                )
                task_id = task.id
                db.add(
                    TurnRecord(
                        id="trn_memory_replay_done",
                        user_message='{"note": null, "prompt_version": "memory-rememberer-dream-v3", "trigger": "dream"}',
                        assistant_message=None,
                        status="completed",
                        kind="memory_dream",
                        source_background_task_id=task_id,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

        assert process_one_task(session_factory=sf, settings=runtime.settings, runtime=runtime)

        assert adapter.call_count == 0
        with sf() as db:
            assert db.get(BackgroundTaskRecord, task_id) is None
            turns = db.scalars(
                select(TurnRecord).where(TurnRecord.source_background_task_id == task_id)
            ).all()
            assert len(turns) == 1


def test_worker_memory_task_replay_fails_interrupted_turn_without_model_call(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted rememberer source turn is failed on replay instead of
    running the rememberer again."""

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    adapter = _DreamCompleteAdapter()
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = cast(Any, client.app).state.runtime
        sf = runtime.session_factory
        with sf() as db:
            with db.begin():
                task = enqueue_background_task(
                    db,
                    task_type="memory_dream",
                    payload={},
                    now=NOW,
                )
                task_id = task.id
                db.add(
                    TurnRecord(
                        id="trn_memory_replay_interrupted",
                        user_message='{"note": null, "prompt_version": "memory-rememberer-dream-v3", "trigger": "dream"}',
                        assistant_message=None,
                        status="in_progress",
                        kind="memory_dream",
                        source_background_task_id=task_id,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

        assert process_one_task(session_factory=sf, settings=runtime.settings, runtime=runtime)

        assert adapter.call_count == 0
        with sf() as db:
            assert db.get(BackgroundTaskRecord, task_id) is None
            turn = db.get(TurnRecord, "trn_memory_replay_interrupted")
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


def test_background_tasks_rejects_unknown_task_type(
    session_factory: sessionmaker[Session],
) -> None:
    """Unknown task types violate the CHECK constraint."""
    with pytest.raises(IntegrityError):
        with session_factory() as db:
            with db.begin():
                db.execute(
                    text(
                        "INSERT INTO background_tasks"
                        " (id,task_type,payload,attempts,run_after,created_at,updated_at)"
                        " VALUES (:id,:tt,'{}',0,now(),now(),now())"
                    ),
                    {"id": _new_id("bgt"), "tt": "not_a_task_type"},
                )


# ===========================================================================
# 9. ai_judgments CHECK accepts persisted types and rejects unknown types
# ===========================================================================

_AJ_INSERT = text(
    "INSERT INTO ai_judgments"
    " (id,judgment_type,source_type,source_id,status,model,prompt_version,"
    "  input_summary,input_refs,output,parse_status,validation_status,created_at)"
    " VALUES (:id,:jt,'turn','trn_t','succeeded','mdl','v1','t','{}','{}','parsed','valid',now())"
)


def test_ai_judgments_accepts_persisted_types_and_rejects_unknown_type(
    session_factory: sessionmaker[Session],
) -> None:
    """Persisted judgment types insert cleanly; unknown types violate the CHECK constraint."""
    for jt in ("memory_recall", "memory_encode", "memory_dream", "model_output"):
        with session_factory() as db:
            with db.begin():
                db.execute(_AJ_INSERT, {"id": _new_id("ajg"), "jt": jt})

    with pytest.raises(IntegrityError):
        with session_factory() as db:
            with db.begin():
                db.execute(_AJ_INSERT, {"id": _new_id("ajg"), "jt": "not_a_judgment_type"})


# ===========================================================================
# 10. Memory log accumulates events after a user-message turn
# ===========================================================================


def test_memory_log_accumulates_events_after_turn(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After one user-message turn, ``memory_log`` holds at least one each of
    ``user_message``, ``agent_round``, and ``assistant_message`` events, all
    sharing the same ``turn_id``."""
    adapter = _TwoPhaseAdapter()
    with TestClient(_app(postgres_url, adapter, monkeypatch)) as client:
        sf = _sf(client)
        turn = post_message_and_drain(client, message="what day is it")
        with sf() as db:
            events = db.scalars(
                select(MemoryLogRecord).where(
                    MemoryLogRecord.turn_id == turn.id,
                )
            ).all()

    kinds = {e.kind for e in events}
    assert "user_message" in kinds, f"got kinds={kinds}"
    assert "agent_round" in kinds, f"got kinds={kinds}"
    assert "assistant_message" in kinds, f"got kinds={kinds}"
    for e in events:
        assert e.turn_id == turn.id


# ===========================================================================
# 11. Notes are editable (UPDATE and DELETE succeed on memory_notes)
# ===========================================================================


def test_notes_are_editable_and_deletable(
    session_factory: sessionmaker[Session],
) -> None:
    """``memory_notes`` permits UPDATE and DELETE (the trigger only guards ``memory_log``)."""
    now = datetime.now(tz=UTC)
    note_id = _new_id("mno")
    with session_factory() as db:
        with db.begin():
            db.add(
                MemoryNoteRecord(
                    id=note_id,
                    content="original",
                    embedding=None,
                    taint="clean",
                    created_at=now,
                    updated_at=now,
                )
            )

    with session_factory() as db:
        with db.begin():
            db.execute(
                text("UPDATE memory_notes SET content=:c WHERE id=:id"),
                {"c": "updated", "id": note_id},
            )
    with session_factory() as db:
        assert db.get(MemoryNoteRecord, note_id).content == "updated"  # type: ignore[union-attr]

    with session_factory() as db:
        with db.begin():
            db.execute(text("DELETE FROM memory_notes WHERE id=:id"), {"id": note_id})
    with session_factory() as db:
        assert db.get(MemoryNoteRecord, note_id) is None


def test_memory_notes_route_lists_operator_visible_notes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    with TestClient(_app(postgres_url, _TwoPhaseAdapter(), monkeypatch)) as client:
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                db.add(
                    MemoryNoteRecord(
                        id="mno_route_visible",
                        content="Remember the incident checklist.",
                        embedding=None,
                        taint="clean",
                        created_at=now,
                        updated_at=now,
                    )
                )

        response = client.get("/v1/memory/notes?limit=5")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "notes": [
            {
                "id": "mno_route_visible",
                "content": "Remember the incident checklist.",
                "created_at": "2026-05-22T12:00:00Z",
                "updated_at": "2026-05-22T12:00:00Z",
                "taint": "clean",
            }
        ],
    }


def test_memory_log_route_lists_operator_visible_log_rows(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    with TestClient(_app(postgres_url, _TwoPhaseAdapter(), monkeypatch)) as client:
        with cast(Any, client.app).state.session_factory() as db:
            with db.begin():
                db.add(
                    MemoryLogRecord(
                        id="mev_route_visible",
                        kind="recall",
                        content="Remember the incident checklist.",
                        embedding=None,
                        turn_id=None,
                        taint="clean",
                        source_ref="manual-smoke",
                        created_at=now,
                    )
                )

        response = client.get("/v1/memory/log?limit=5")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "log": [
            {
                "id": "mev_route_visible",
                "created_at": "2026-05-22T12:00:00Z",
                "kind": "recall",
                "content": "Remember the incident checklist.",
                "turn_id": None,
                "taint": "clean",
                "source_ref": "manual-smoke",
            }
        ],
    }


# ===========================================================================
# 12. Append-only trigger via SQLAlchemy session
# ===========================================================================


def test_memory_log_append_only_via_sqlalchemy_session(
    session_factory: sessionmaker[Session],
) -> None:
    """ORM-inserted ``MemoryLogRecord`` then raw UPDATE raises the trigger's error."""
    now = datetime.now(tz=UTC)
    with session_factory() as db:
        with db.begin():
            row = MemoryLogRecord(
                id=_new_id("mev"),
                created_at=now,
                kind="assistant_message",
                content="immutable",
                embedding=None,
                turn_id=None,
                taint="clean",
                source_ref=None,
            )
            db.add(row)
        row_id = row.id

    with pytest.raises(
        (IntegrityError, OperationalError, ProgrammingError),
        match="memory_log is append-only",
    ):
        with session_factory() as db:
            with db.begin():
                db.execute(
                    text("UPDATE memory_log SET content=:c WHERE id=:id"),
                    {"c": "y", "id": row_id},
                )


# ===========================================================================
# 14. run_rememberer(trigger="dream") inserts cleanly on a fresh DB
# ===========================================================================


class _DreamCompleteAdapter(FakeModelAdapter):
    """One call → the rememberer emits ``agent.emit_done(...)`` and the loop ends."""

    provider = "provider.test"
    model = "model.test"

    def __init__(self) -> None:
        super().__init__()
        self.call_count: int = 0

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        self.call_count += 1
        return _run_response("agent.emit_done(summary='dreamt')\n", idx=self.call_count)


def test_run_rememberer_dream_succeeds_on_fresh_db(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh DB accepts a ``dream`` run."""
    from ariel.config import AppSettings
    from ariel.capability_registry import REMEMBERER_CAPABILITY_IDS
    from ariel.memory import run_rememberer
    from ariel.persistence import TurnRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    settings = AppSettings()

    sandbox = FakeSandboxRuntime()
    sandbox.start()
    try:
        run_rememberer(
            trigger="dream",
            sandbox=sandbox,
            session_factory=session_factory,
            settings=settings,
            model_adapter=_DreamCompleteAdapter(),
            google_runtime=None,
            agency_runtime=None,
            attachment_runtime=None,
            note=None,
            allowed_capability_ids=REMEMBERER_CAPABILITY_IDS,
            approval_ttl_seconds=int(settings.approval_ttl_seconds),
            approval_actor_id=str(settings.approval_actor_id),
            add_event=lambda *_args, **_kwargs: None,
            now_fn=lambda: datetime.now(tz=UTC),
            new_id_fn=_new_id,
        )

        with session_factory() as db:
            dream_turns = db.scalars(
                select(TurnRecord).where(TurnRecord.kind == "memory_dream")
            ).all()
        assert len(dream_turns) == 1, f"expected 1 memory_dream turn, got {len(dream_turns)}"
        assert dream_turns[0].status == "completed"
    finally:
        sandbox.close()


def test_worker_memory_dream_task_inserts_turn(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued ``memory_dream`` task runs cleanly on a fresh DB."""
    from ariel.persistence import TurnRecord, enqueue_background_task

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=_DreamCompleteAdapter(),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = cast(Any, client.app).state.runtime
        sf = runtime.session_factory

        with sf() as db:
            with db.begin():
                task = enqueue_background_task(db, task_type="memory_dream", payload={}, now=NOW)
                task_id = task.id

        drain_task(client, task_id)
        with sf() as db:
            dream_turns = db.scalars(
                select(TurnRecord).where(TurnRecord.kind == "memory_dream")
            ).all()
        assert dream_turns, "memory_dream task must produce a memory_dream turn"
        # Status is the strong contract: if the FK insert had failed, the turn
        # row wouldn't exist; if the loop crashed mid-run, status would still
        # be 'in_progress'.
        assert all(t.status == "completed" for t in dream_turns), (
            "every memory_dream turn must reach status='completed'"
        )


# ===========================================================================
# Assistant failure-message filter — these messages poison retrieval
# when they live in ``memory_log``: the retriever surfaces them as evidence
# of failure even when the capability is now succeeding with real data.
# ===========================================================================


@pytest.mark.parametrize(
    "failure_text",
    [
        "Calendar fetch failed",
        "Calendar query failed",
        "Calendar fetch failed.",
        "calendar fetch failed",
        "Email search failed",
        "Drive errored",
        "Inbox unavailable",
        "Memory query failed",
        "Email errored — please re-link in settings.",
        "Try to re-link in settings and retry.",
    ],
)
def test_append_log_event_skips_assistant_failure_messages(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    failure_text: str,
) -> None:
    """An ``assistant_message`` whose text matches the failure-message regex is
    skipped at the write site: ``append_log_event`` returns ``None`` and no
    row is inserted into ``memory_log``."""
    from ariel.config import AppSettings
    from ariel.memory import append_log_event
    from ariel.persistence import MemoryLogRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()

    with session_factory() as db:
        with db.begin():
            result = append_log_event(
                db,
                kind="assistant_message",
                content=failure_text,
                turn_id=None,
                taint="clean",
                source_ref=None,
                adapter=adapter,
                settings=settings,
                now=now,
                new_id_fn=_new_id,
            )
        assert result is None, f"expected failure message {failure_text!r} to be filtered"

    with session_factory() as db:
        rows = db.scalars(
            select(MemoryLogRecord).where(MemoryLogRecord.content == failure_text)
        ).all()
    assert rows == [], f"failure message {failure_text!r} must not be written to memory_log"


@pytest.mark.parametrize(
    "legitimate_text",
    [
        "No emails from today.",
        "Calendar — no events in the next 7 days.",
        "You have 3 meetings tomorrow.",
        "Drive search returned 2 files.",
        "hello",
        # "no X" phrases must pass — they describe a real empty result, not
        # a connector failure.
        "no events found",
        "no matches for that query",
    ],
)
def test_append_log_event_preserves_legitimate_assistant_messages(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    legitimate_text: str,
) -> None:
    """An ``assistant_message`` that does not match the failure-message regex
    is written normally: ``append_log_event`` returns the row, and the row
    exists in ``memory_log``."""
    from ariel.config import AppSettings
    from ariel.memory import append_log_event
    from ariel.persistence import MemoryLogRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()

    with session_factory() as db:
        with db.begin():
            result = append_log_event(
                db,
                kind="assistant_message",
                content=legitimate_text,
                turn_id=None,
                taint="clean",
                source_ref=None,
                adapter=adapter,
                settings=settings,
                now=now,
                new_id_fn=_new_id,
            )
        assert result is not None, f"legitimate message {legitimate_text!r} must be written"
        written_id = result.id

    with session_factory() as db:
        row = db.get(MemoryLogRecord, written_id)
    assert row is not None
    assert row.kind == "assistant_message"
    assert row.content == legitimate_text


def test_append_log_event_filter_applies_only_to_assistant_messages(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter only suppresses ``assistant_message`` rows. Other event
    kinds (``user_message``, ``agent_round``, ``tool_observation``,
    ``proactive_trigger``) with matching text are written normally — the
    user is allowed to say "calendar fetch failed" and the agent_round
    JSON may legitimately contain that phrase as part of a syscall trace.
    """
    from ariel.config import AppSettings
    from ariel.memory import append_log_event
    from ariel.persistence import MemoryLogRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()
    failure_text = "Calendar fetch failed"

    with session_factory() as db:
        with db.begin():
            for kind in (
                "user_message",
                "agent_round",
                "tool_observation",
                "proactive_trigger",
            ):
                result = append_log_event(
                    db,
                    kind=kind,  # type: ignore[arg-type]
                    content=failure_text,
                    turn_id=None,
                    taint="clean",
                    source_ref=None,
                    adapter=adapter,
                    settings=settings,
                    now=now,
                    new_id_fn=_new_id,
                )
                assert result is not None, f"non-assistant kind {kind} must not be filtered"

    with session_factory() as db:
        rows = db.scalars(
            select(MemoryLogRecord).where(MemoryLogRecord.content == failure_text)
        ).all()
    kinds = {r.kind for r in rows}
    assert kinds == {"user_message", "agent_round", "tool_observation", "proactive_trigger"}


def test_append_log_event_records_pending_embedding_when_embedding_fails(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the embedding provider raises, the row is written with ``embedding=None``."""
    from ariel.config import AppSettings
    from ariel.memory import append_log_event

    def failing(*_: Any, **__: Any) -> list[float]:
        raise RuntimeError("memory embedding failed: provider down")

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", failing)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()

    with session_factory() as db:
        with db.begin():
            result = append_log_event(
                db,
                kind="user_message",
                content="project phoenix update",
                turn_id=None,
                taint="clean",
                source_ref=None,
                adapter=adapter,
                settings=settings,
                now=now,
                new_id_fn=_new_id,
            )
        assert result is not None
        written_id = result.id

    with session_factory() as db:
        row = db.get(MemoryLogRecord, written_id)
    assert row is not None
    assert row.embedding is None


def test_note_mutations_record_pending_embeddings_when_embedding_fails(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ariel.config import AppSettings
    from ariel.memory import create_note, edit_note

    def failing(*_: Any, **__: Any) -> list[float]:
        raise RuntimeError("memory embedding failed: provider down")

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", failing)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()

    with session_factory() as db:
        with db.begin():
            note = create_note(
                db,
                content="initial note",
                taint="clean",
                adapter=adapter,
                settings=settings,
                now=now,
                new_id_fn=_new_id,
            )
            note_id = note.id
        with db.begin():
            edit_note(
                db,
                note_id=note_id,
                content="updated note",
                adapter=adapter,
                settings=settings,
                now=now,
                new_id_fn=_new_id,
            )

    with session_factory() as db:
        stored_note = db.get(MemoryNoteRecord, note_id)
        log_rows = db.scalars(
            select(MemoryLogRecord)
            .where(MemoryLogRecord.source_ref == note_id)
            .order_by(MemoryLogRecord.kind.asc())
        ).all()

    assert stored_note is not None
    assert stored_note.content == "updated note"
    assert stored_note.embedding is None
    assert {row.kind for row in log_rows} == {"note_create", "note_edit"}
    assert all(row.embedding is None for row in log_rows)


def test_search_memory_uses_keyword_hits_when_embedding_fails(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ariel.config import AppSettings
    from ariel.memory import search_memory

    def failing(*_: Any, **__: Any) -> list[float]:
        raise RuntimeError("memory embedding failed: provider down")

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", failing)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()
    log_id = _insert_log_row_directly(
        session_factory,
        kind="user_message",
        content="Project Phoenix launch notes",
        created_at=now,
    )

    with session_factory() as db:
        with db.begin():
            hits = search_memory(
                db, query="project phoenix", adapter=adapter, settings=settings, limit=24
            )

    assert log_id in {hit["id"] for hit in hits}


def test_append_log_event_propagates_embedding_response_defect(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dimension-mismatch defect from the embedding provider propagates as
    ``MemoryEmbeddingResponseError`` (configuration bug, not fail-soft)."""
    from ariel.config import AppSettings
    from ariel.memory import MemoryEmbeddingResponseError, append_log_event

    def malformed(*_: Any, **__: Any) -> list[float]:
        raise MemoryEmbeddingResponseError("memory embedding response missing vector")

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", malformed)
    settings = AppSettings()
    adapter = FakeModelAdapter()

    with pytest.raises(MemoryEmbeddingResponseError):
        with session_factory() as db:
            with db.begin():
                append_log_event(
                    db,
                    kind="user_message",
                    content="project phoenix update",
                    turn_id=None,
                    taint="clean",
                    source_ref=None,
                    adapter=adapter,
                    settings=settings,
                    now=datetime.now(tz=UTC),
                    new_id_fn=_new_id,
                )


# ===========================================================================
# Retrieval-side filter — directly inserted polluting rows must NOT surface
# from ``search_memory``. The same regex applies symmetrically on both sides.
# ===========================================================================


def _insert_log_row_directly(
    session_factory: sessionmaker[Session],
    *,
    kind: str,
    content: str,
    created_at: datetime,
) -> str:
    """Insert a ``memory_log`` row via the ORM, bypassing ``append_log_event``.

    The append-only trigger only blocks UPDATE/DELETE; raw INSERTs go through.
    This seeds rows that bypass ``append_log_event`` so the retrieval filter is
    exercised against stored data already present in the append-only log.
    """
    row_id = _new_id("mev")
    with session_factory() as db:
        with db.begin():
            db.add(
                MemoryLogRecord(
                    id=row_id,
                    created_at=created_at,
                    kind=kind,
                    content=content,
                    embedding=None,
                    turn_id=None,
                    taint="clean",
                    source_ref=None,
                )
            )
    return row_id


def test_search_memory_skips_directly_inserted_error_assistant_messages(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``assistant_message`` whose content matches the error regex is seeded
    directly into ``memory_log`` and ``search_memory`` must not return it. A
    legitimate ``assistant_message`` ("No emails today") seeded the same way is
    returned.
    """
    from ariel.config import AppSettings
    from ariel.memory import search_memory

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()

    polluting_id = _insert_log_row_directly(
        session_factory,
        kind="assistant_message",
        content="Calendar fetch failed",
        created_at=now,
    )
    legitimate_id = _insert_log_row_directly(
        session_factory,
        kind="assistant_message",
        content="No emails from today.",
        created_at=now,
    )

    with session_factory() as db:
        with db.begin():
            calendar_hits = search_memory(
                db, query="calendar", adapter=adapter, settings=settings, limit=24
            )
        with db.begin():
            email_hits = search_memory(
                db, query="emails today", adapter=adapter, settings=settings, limit=24
            )

    calendar_ids = {h["id"] for h in calendar_hits}
    email_ids = {h["id"] for h in email_hits}

    assert polluting_id not in calendar_ids, (
        f"polluting failure-message row must not surface; got hits={calendar_hits!r}"
    )
    assert legitimate_id in email_ids, (
        f"legitimate 'no emails today' row must surface; got hits={email_hits!r}"
    )


def test_search_memory_does_not_filter_non_assistant_kinds(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retrieval-side filter only suppresses ``assistant_message`` rows.
    A ``user_message`` row whose content happens to contain "calendar fetch
    failed" (the user complaining) must still surface — the filter is
    symmetric with the write site, which also only filters
    ``assistant_message``.
    """
    from ariel.config import AppSettings
    from ariel.memory import search_memory

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, adapter, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
    adapter = FakeModelAdapter()

    user_id = _insert_log_row_directly(
        session_factory,
        kind="user_message",
        content="Calendar fetch failed for me yesterday — can you check?",
        created_at=now,
    )

    with session_factory() as db:
        with db.begin():
            hits = search_memory(db, query="calendar", adapter=adapter, settings=settings, limit=24)

    assert user_id in {h["id"] for h in hits}, (
        f"user_message with matching text must surface; got hits={hits!r}"
    )
