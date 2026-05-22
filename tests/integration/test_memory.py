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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ariel.app import ModelAdapter
from tests.integration.app_helpers import create_migrated_app
from ariel.capability_registry import capability_id_for_run_callable, get_capability
from ariel.persistence import BackgroundTaskRecord, MemoryLogRecord, MemoryNoteRecord
from ariel.worker import process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import post_message_and_drain

_id_counter = count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_mt_{next(_id_counter)}"


NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)

_RETRIEVER_PROGRAM = "agent.emit_finding(summary='no memories',claims=[],gaps=[],sources=[])\n"

_EMIT_MSG = "agent.emit_message(text='hello')\n"


def _app(postgres_url: str, adapter: ModelAdapter, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a migrated app with embed_text stubbed out."""
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    return create_migrated_app(
        database_url=postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )


def _session_id(client: TestClient) -> str:
    r = client.get("/v1/sessions/active")
    assert r.status_code == 200
    return r.json()["session"]["id"]


def _sf(client: TestClient) -> sessionmaker[Session]:
    return cast(Any, client.app).state.runtime.session_factory


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _run_response(source: str, *, idx: int, provider: str = "provider.test") -> dict[str, Any]:
    return {
        "provider": provider,
        "model": "model.test",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "provider_response_id": f"resp_{idx}",
        "output": [
            {
                "type": "function_call",
                "id": f"fc_{idx}",
                "call_id": f"call_{idx}",
                "name": "run",
                "arguments": json.dumps({"source": source}, sort_keys=True),
                "status": "completed",
            }
        ],
    }


@dataclass
class _TwoPhaseAdapter:
    """Odd calls → retriever (emit_finding); even calls → main agent (emit_message)."""

    provider: str = "provider.test"
    model: str = "model.test"
    call_count: int = 0
    snapshots: list[list[dict[str, Any]]] = field(default_factory=list)

    def create_response(
        self, *, input_items: Any, tools: Any, user_message: Any, history: Any, context_bundle: Any
    ) -> dict[str, Any]:
        del tools, user_message, history, context_bundle
        self.call_count += 1
        self.snapshots.append(list(input_items))
        source = _RETRIEVER_PROGRAM if self.call_count % 2 == 1 else _EMIT_MSG
        return _run_response(source, idx=self.call_count)


@dataclass
class _FailingRetrieverAdapter:
    """Retriever emits the same source twice → stuck-detection ends it with no finding.

    On calls 1 and 2 (both retriever rounds) the same emit_value source is
    returned; stuck-detection fires after the duplicate and the retriever exits
    with ``budget_exhausted`` and no ``emitted_finding``. Call 3 is the main
    agent, which emits a message.
    """

    provider: str = "provider.test"
    model: str = "model.test"
    call_count: int = 0
    _stuck_source: str = "agent.emit_value(value={'stuck':1})\n"

    def create_response(
        self, *, input_items: Any, tools: Any, user_message: Any, history: Any, context_bundle: Any
    ) -> dict[str, Any]:
        del tools, user_message, history, context_bundle, input_items
        self.call_count += 1
        source = self._stuck_source if self.call_count <= 2 else _EMIT_MSG
        return _run_response(source, idx=self.call_count)


@dataclass
class _RememberAdapter:
    """Retriever on odd calls; main agent calls memory.remember then emits on even."""

    provider: str = "provider.test"
    model: str = "model.test"
    call_count: int = 0
    note_text: str = "user prefers dark mode"

    def create_response(
        self, *, input_items: Any, tools: Any, user_message: Any, history: Any, context_bundle: Any
    ) -> dict[str, Any]:
        del tools, user_message, history, context_bundle, input_items
        self.call_count += 1
        if self.call_count % 2 == 1:
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
    with TestClient(_app(postgres_url, cast(ModelAdapter, _TwoPhaseAdapter()), monkeypatch)):
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
        session_id=None,
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
        session_id=None,
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
    with TestClient(_app(postgres_url, cast(ModelAdapter, adapter), monkeypatch)) as client:
        sid = _session_id(client)
        post_message_and_drain(client, sid, message="hello")

    assert adapter.call_count >= 2, "expected retriever + main agent calls"
    rendered = json.dumps(adapter.snapshots[1])  # main-agent snapshot
    assert "memory recall:" in rendered


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
    with TestClient(_app(postgres_url, cast(ModelAdapter, adapter), monkeypatch)) as client:
        sid = _session_id(client)
        turn = post_message_and_drain(client, sid, message="ping")
    assert turn.status == "completed"
    assert turn.assistant_message == "hello"


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
    with TestClient(_app(postgres_url, cast(ModelAdapter, adapter), monkeypatch)) as client:
        sid = _session_id(client)
        sf = _sf(client)
        post_message_and_drain(client, sid, message="remember this")
        with sf() as db:
            tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "memory_encode"
                )
            ).all()

    assert len(tasks) == 1, f"expected 1 memory_encode task, got {len(tasks)}"
    assert tasks[0].payload.get("note") == adapter.note_text


# ===========================================================================
# 7b. Retriever and main proposal_index sharing
# ===========================================================================


@dataclass
class _RetrieverSearchesThenMainSearchesAdapter:
    """Retriever and main-agent calls share one parent-turn proposal index space.

    The main-agent search and message are split across two rounds because
    ``run_agent_loop`` rejects messages authored before read observations.
    """

    provider: str = "provider.test"
    model: str = "model.test"
    call_count: int = 0

    def create_response(
        self, *, input_items: Any, tools: Any, user_message: Any, history: Any, context_bundle: Any
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history, context_bundle
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
    with TestClient(_app(postgres_url, cast(ModelAdapter, adapter), monkeypatch)) as client:
        sid = _session_id(client)
        turn = post_message_and_drain(client, sid, message="ping")
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
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    app = create_migrated_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _DreamCompleteAdapter()),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = cast(Any, client.app).state.runtime
        sf = runtime.session_factory
        for task_type, payload in [
            ("memory_encode", {"note": "n", "session_id": None}),
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
    sharing the same ``session_id`` and ``turn_id``."""
    adapter = _TwoPhaseAdapter()
    with TestClient(_app(postgres_url, cast(ModelAdapter, adapter), monkeypatch)) as client:
        sid = _session_id(client)
        sf = _sf(client)
        turn = post_message_and_drain(client, sid, message="what day is it")
        with sf() as db:
            events = db.scalars(
                select(MemoryLogRecord).where(
                    MemoryLogRecord.session_id == sid,
                    MemoryLogRecord.turn_id == turn.id,
                )
            ).all()

    kinds = {e.kind for e in events}
    assert "user_message" in kinds, f"got kinds={kinds}"
    assert "agent_round" in kinds, f"got kinds={kinds}"
    assert "assistant_message" in kinds, f"got kinds={kinds}"
    for e in events:
        assert e.session_id == sid
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
                session_id=None,
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


def test_system_session_row_seeded_by_migration(
    session_factory: sessionmaker[Session],
) -> None:
    """The ``ses_system`` singleton row is present after migrations, inactive,
    and ``lifecycle_state='closed'`` (so it never collides with the partial
    unique index ``ix_single_active_session``)."""
    from ariel.persistence import SYSTEM_SESSION_ID, SessionRecord

    with session_factory() as db:
        system_session = db.get(SessionRecord, SYSTEM_SESSION_ID)
    assert system_session is not None, "migration must seed ses_system"
    assert system_session.is_active is False
    assert system_session.lifecycle_state == "closed"
    assert system_session.rotated_from_session_id is None
    assert system_session.rotation_reason is None


def test_ensure_system_session_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    """``ensure_system_session`` returns the singleton id and never duplicates it.
    Re-calling on a fresh DB and on a DB where the row already exists are both
    no-ops; deleting the row and re-calling re-creates it (self-heal)."""
    from ariel.persistence import SYSTEM_SESSION_ID, SessionRecord, ensure_system_session

    now = datetime.now(tz=UTC)

    with session_factory() as db:
        with db.begin():
            sid = ensure_system_session(db, now=now)
        assert sid == SYSTEM_SESSION_ID

    with session_factory() as db:
        with db.begin():
            db.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": SYSTEM_SESSION_ID})
        with db.begin():
            sid = ensure_system_session(db, now=now)
        assert sid == SYSTEM_SESSION_ID
        assert db.get(SessionRecord, SYSTEM_SESSION_ID) is not None


# ===========================================================================
# 14. run_rememberer(trigger="dream") with no user session inserts cleanly
# ===========================================================================


@dataclass
class _DreamCompleteAdapter:
    """One call → the rememberer emits ``agent.emit_done(...)`` and the loop ends."""

    provider: str = "provider.test"
    model: str = "model.test"
    call_count: int = 0

    def create_response(
        self, *, input_items: Any, tools: Any, user_message: Any, history: Any, context_bundle: Any
    ) -> dict[str, Any]:
        del tools, user_message, history, context_bundle, input_items
        self.call_count += 1
        return _run_response("agent.emit_done(summary='dreamt')\n", idx=self.call_count)


def test_run_rememberer_dream_succeeds_with_no_user_session(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh DB with no user session accepts a ``dream`` run against the system session."""
    from ariel.config import AppSettings
    from ariel.capability_registry import REMEMBERER_CAPABILITY_IDS
    from ariel.memory import run_rememberer
    from ariel.persistence import SYSTEM_SESSION_ID, SessionRecord, TurnRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()

    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    from tests.db_helpers import reset_postgres_schema

    reset_postgres_schema(engine, postgres_url)
    sf = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    sandbox = FakeSandboxRuntime()
    sandbox.start()
    try:
        # No user session has ever existed — this is the production dream path.
        with sf() as db:
            assert (
                db.scalar(select(SessionRecord).where(SessionRecord.is_active.is_(True))) is None
            ), "fresh DB must have no active user session"

        with sf() as db:
            run_rememberer(
                trigger="dream",
                sandbox=sandbox,
                db=db,
                session_factory=sf,
                session_id=None,
                settings=settings,
                model_adapter=cast(Any, _DreamCompleteAdapter()),
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

        with sf() as db:
            dream_turns = db.scalars(
                select(TurnRecord).where(TurnRecord.kind == "memory_dream")
            ).all()
        assert len(dream_turns) == 1, f"expected 1 memory_dream turn, got {len(dream_turns)}"
        assert dream_turns[0].session_id == SYSTEM_SESSION_ID
        assert dream_turns[0].status == "completed"
    finally:
        sandbox.close()
        engine.dispose()


def test_run_rememberer_dream_self_heals_if_system_session_missing(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the system session row is wiped, ``run_rememberer(trigger='dream')``
    re-creates it via ``ensure_system_session`` and completes — the loop is
    not permanently broken by an operator wipe."""
    from ariel.config import AppSettings
    from ariel.capability_registry import REMEMBERER_CAPABILITY_IDS
    from ariel.memory import run_rememberer
    from ariel.persistence import SYSTEM_SESSION_ID, SessionRecord, TurnRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()

    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    from tests.db_helpers import reset_postgres_schema

    reset_postgres_schema(engine, postgres_url)
    sf = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    sandbox = FakeSandboxRuntime()
    sandbox.start()
    try:
        # Wipe the system session row to simulate operator deletion.
        with sf() as db:
            with db.begin():
                db.execute(
                    text("DELETE FROM sessions WHERE id = :id"),
                    {"id": SYSTEM_SESSION_ID},
                )
            assert db.get(SessionRecord, SYSTEM_SESSION_ID) is None

        with sf() as db:
            run_rememberer(
                trigger="dream",
                sandbox=sandbox,
                db=db,
                session_factory=sf,
                session_id=None,
                settings=settings,
                model_adapter=cast(Any, _DreamCompleteAdapter()),
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

        with sf() as db:
            assert db.get(SessionRecord, SYSTEM_SESSION_ID) is not None, (
                "self-heal must re-create ses_system"
            )
            dream_turns = db.scalars(
                select(TurnRecord).where(TurnRecord.kind == "memory_dream")
            ).all()
        assert len(dream_turns) == 1
        assert dream_turns[0].session_id == SYSTEM_SESSION_ID
        assert dream_turns[0].status == "completed"
    finally:
        sandbox.close()
        engine.dispose()


def test_worker_memory_dream_task_inserts_turn_against_system_session(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end via ``process_one_task``: a queued ``memory_dream`` task runs
    cleanly on a fresh DB and the turn row is attached to the system session.

    ``process_one_task`` is called repeatedly until a ``memory_dream`` turn
    exists; each call processes at most one task and the worker also seeds
    provider-maintenance tasks alongside the dream, so the dream may not be
    the first task popped on a single iteration."""
    from ariel.persistence import SYSTEM_SESSION_ID, TurnRecord, enqueue_background_task

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    app = create_migrated_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _DreamCompleteAdapter()),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        runtime = cast(Any, client.app).state.runtime
        sf = runtime.session_factory

        with sf() as db:
            with db.begin():
                enqueue_background_task(db, task_type="memory_dream", payload={}, now=NOW)

        # Drain at most a handful of tasks; on a fresh DB the queue is small
        # (memory_dream + the provider-maintenance seeds). The dream is the
        # only task that creates a memory_dream turn; other tasks may fail in
        # the test environment (missing google connector, etc.) which is fine.
        dream_turn_present = False
        for _ in range(6):
            processed = process_one_task(
                session_factory=sf, settings=runtime.settings, runtime=runtime
            )
            with sf() as db:
                dream_turn = db.scalar(select(TurnRecord).where(TurnRecord.kind == "memory_dream"))
            if dream_turn is not None:
                dream_turn_present = True
                break
            if not processed:
                break

        assert dream_turn_present, "memory_dream task must produce a memory_dream turn"
        with sf() as db:
            dream_turns = db.scalars(
                select(TurnRecord).where(TurnRecord.kind == "memory_dream")
            ).all()
        assert all(t.session_id == SYSTEM_SESSION_ID for t in dream_turns), (
            "every memory_dream turn must reference the system session"
        )
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
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)

    with session_factory() as db:
        with db.begin():
            result = append_log_event(
                db,
                kind="assistant_message",
                content=failure_text,
                session_id=None,
                turn_id=None,
                taint="clean",
                source_ref=None,
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
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)

    with session_factory() as db:
        with db.begin():
            result = append_log_event(
                db,
                kind="assistant_message",
                content=legitimate_text,
                session_id=None,
                turn_id=None,
                taint="clean",
                source_ref=None,
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
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)
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
                    session_id=None,
                    turn_id=None,
                    taint="clean",
                    source_ref=None,
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
                    session_id=None,
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
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)

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
            calendar_hits = search_memory(db, query="calendar", settings=settings, limit=24)
        with db.begin():
            email_hits = search_memory(db, query="emails today", settings=settings, limit=24)

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
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()
    now = datetime.now(tz=UTC)

    user_id = _insert_log_row_directly(
        session_factory,
        kind="user_message",
        content="Calendar fetch failed for me yesterday — can you check?",
        created_at=now,
    )

    with session_factory() as db:
        with db.begin():
            hits = search_memory(db, query="calendar", settings=settings, limit=24)

    assert user_id in {h["id"] for h in hits}, (
        f"user_message with matching text must surface; got hits={hits!r}"
    )
