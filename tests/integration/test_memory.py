"""Phase 1 contract tests for the memory substrate cutover.

These tests fail against the pre-cutover ``main`` and define the target.
Contracts: ``memory_log``/``memory_notes`` schema; append-only trigger;
capability surface; pre-turn retrieval; recall non-fatality; no profile/digest;
``memory.remember`` enqueueing; worker dispatch; ai_judgments types; log event
accumulation; note mutability; append-only via SQLAlchemy session.

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

from ariel.agent_loop import run_agent_loop
from ariel.app import ModelAdapter, create_app
from ariel.capability_registry import capability_id_for_run_callable, get_capability
from ariel.persistence import BackgroundTaskRecord, MemoryLogRecord, MemoryNoteRecord
from ariel.worker import UnsupportedTaskType, process_one_task
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import post_message_and_drain

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_id_counter = count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_mt_{next(_id_counter)}"


NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)

# Canned retriever program: emits a finding immediately.
_RETRIEVER_PROGRAM = "agent.emit_finding(summary='no memories',claims=[],gaps=[],sources=[])\n"

# Canned main-agent program: emits a message.
_EMIT_MSG = "agent.emit_message(text='hello')\n"


def _app(postgres_url: str, adapter: ModelAdapter, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a ``create_app`` instance with embed_text stubbed out."""
    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    return create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
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
        # Calls 1 and 2: retriever stuck-detection (same source twice → exits).
        # Call 3: main agent emits a message.
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
    """``memory_log`` and ``memory_notes`` are the only ``memory_*`` tables;
    ``memory_facts`` and ``memory_profile`` do not exist."""
    engine = create_engine(postgres_url, future=True)
    with TestClient(_app(postgres_url, cast(ModelAdapter, _TwoPhaseAdapter()), monkeypatch)):
        table_names = set(inspect(engine).get_table_names())
    memory_tables = {t for t in table_names if t.startswith("memory_")}
    assert memory_tables == {"memory_log", "memory_notes"}, (
        f"unexpected memory_* tables: {memory_tables}"
    )
    assert "memory_facts" not in table_names
    assert "memory_profile" not in table_names


# ===========================================================================
# 1b. Schema — sessions has no digest column
# ===========================================================================


def test_schema_sessions_has_no_digest_column(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sessions`` table has no ``digest`` column."""
    engine = create_engine(postgres_url, future=True)
    with TestClient(_app(postgres_url, cast(ModelAdapter, _TwoPhaseAdapter()), monkeypatch)):
        cols = {c["name"] for c in inspect(engine).get_columns("sessions")}
    assert "digest" not in cols


# ===========================================================================
# 1c. Schema — memory_log is append-only (UPDATE raises)
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
# 2. run_agent_loop exists and is callable
# ===========================================================================


def test_run_agent_loop_is_callable() -> None:
    """``run_agent_loop`` is exported from ``ariel.agent_loop`` and is callable."""
    assert callable(run_agent_loop)


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
_RETIRED_MEMORY_CALLABLES = {
    "memory.consolidate",
    "memory.propose",
    "memory.delete",
    "memory.inspect",
    "memory.extract",
    "memory.sweep",
}


def test_capability_surface_memory_callables_resolve() -> None:
    """Expected callables resolve to registered capabilities; retired ones do not."""
    for name in _EXPECTED_MEMORY_CALLABLES:
        cap_id = capability_id_for_run_callable(name)
        assert cap_id is not None, f"{name!r} must resolve to a capability_id"
        assert get_capability(cap_id) is not None, f"capability {cap_id!r} must be registered"
    for name in _RETIRED_MEMORY_CALLABLES:
        assert capability_id_for_run_callable(name) is None, (
            f"retired callable {name!r} must not resolve"
        )


# ===========================================================================
# 3b. research.investigate mode enum includes "memories"
# ===========================================================================


def test_research_investigate_mode_includes_memories() -> None:
    """``research.investigate`` accepts ``mode='memories'``, ``'web'``, and
    ``'personal'``; invalid modes are rejected."""
    cap = get_capability(capability_id_for_run_callable("research.investigate") or "")
    assert cap is not None
    for mode in ("memories", "web", "personal"):
        ok, err = cap.validate_input({"question": "q", "mode": mode})
        assert err is None, f"mode={mode!r} must be valid; got {err!r}"
    _, bad = cap.validate_input({"question": "q", "mode": "hybrid"})
    assert bad is not None


# ===========================================================================
# 4. Retriever fires pre-turn; injects recall_v1; no profile or digest
# ===========================================================================


def test_retriever_fires_preturn_and_no_profile_or_digest_injected(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The main agent's context includes ``memory recall:`` (the recall_v1
    reconstruction) and does NOT include a ``user profile`` or ``session digest``
    block."""
    adapter = _TwoPhaseAdapter()
    with TestClient(_app(postgres_url, cast(ModelAdapter, adapter), monkeypatch)) as client:
        sid = _session_id(client)
        post_message_and_drain(client, sid, message="hello")

    assert adapter.call_count >= 2, "expected retriever + main agent calls"
    rendered = json.dumps(adapter.snapshots[1])  # main-agent snapshot
    assert "memory recall:" in rendered
    assert "user profile" not in rendered.lower()
    assert "session digest" not in rendered.lower()


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
# 8. Worker accepts memory_encode and memory_dream; rejects retired task_types
# ===========================================================================


def test_worker_accepts_memory_encode_and_memory_dream(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``memory_encode`` and ``memory_dream`` are dispatched without raising
    ``UnsupportedTaskType`` (other errors for missing runtime config are fine)."""
    from ariel.persistence import enqueue_background_task

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    app = create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _TwoPhaseAdapter()),
        reset_database=True,
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
                    enqueue_background_task(db, task_type=task_type, payload=payload, now=NOW)
            try:
                process_one_task(session_factory=sf, settings=runtime.settings, runtime=runtime)
            except UnsupportedTaskType:
                pytest.fail(f"{task_type} raised UnsupportedTaskType")
            except Exception:
                pass  # missing runtime config etc. is fine


def test_retired_task_types_rejected_by_check_constraint(
    session_factory: sessionmaker[Session],
) -> None:
    """``memory_remember`` and ``memory_sweep`` violate the CHECK constraint."""
    for bad in ("memory_remember", "memory_sweep"):
        with pytest.raises(IntegrityError):
            with session_factory() as db:
                with db.begin():
                    db.execute(
                        text(
                            "INSERT INTO background_tasks"
                            " (id,task_type,payload,attempts,run_after,created_at,updated_at)"
                            " VALUES (:id,:tt,'{}',0,now(),now(),now())"
                        ),
                        {"id": _new_id("bgt"), "tt": bad},
                    )


# ===========================================================================
# 9. ai_judgments CHECK accepts new types; rejects memory_remember
# ===========================================================================

_AJ_INSERT = text(
    "INSERT INTO ai_judgments"
    " (id,judgment_type,source_type,source_id,status,model,prompt_version,"
    "  input_summary,input_refs,output,parse_status,validation_status,created_at)"
    " VALUES (:id,:jt,'turn','trn_t','succeeded','mdl','v1','t','{}','{}','parsed','valid',now())"
)


def test_ai_judgments_accepts_new_types_and_rejects_memory_remember(
    session_factory: sessionmaker[Session],
) -> None:
    """New judgment types insert cleanly; ``memory_remember`` raises ``IntegrityError``."""
    for jt in ("memory_recall", "memory_encode", "memory_dream", "model_output"):
        with session_factory() as db:
            with db.begin():
                db.execute(_AJ_INSERT, {"id": _new_id("ajg"), "jt": jt})

    with pytest.raises(IntegrityError):
        with session_factory() as db:
            with db.begin():
                db.execute(_AJ_INSERT, {"id": _new_id("ajg"), "jt": "memory_remember"})


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


# ===========================================================================
# 13. System session row exists after migrations; supports background turns
# ===========================================================================


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

    # Idempotent on a DB where the row was already seeded by the migration.
    with session_factory() as db:
        with db.begin():
            sid = ensure_system_session(db, now=now)
        assert sid == SYSTEM_SESSION_ID

    # Self-heal: wipe the row, ensure_system_session re-creates it.
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
    """A fresh DB (no user session, never had one) accepts a ``dream`` run:
    the ``TurnRecord`` is inserted against the system session, status reaches
    ``completed``, no ``ForeignKeyViolation`` is raised. Regression for the
    production ``turns_session_id_fkey`` failure."""
    from ariel.config import AppSettings
    from ariel.capability_registry import REMEMBERER_CAPABILITY_IDS
    from ariel.memory import run_rememberer
    from ariel.persistence import SYSTEM_SESSION_ID, SessionRecord, TurnRecord

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    settings = AppSettings()

    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    from ariel.db import reset_schema_for_tests

    reset_schema_for_tests(engine, postgres_url)
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
    from ariel.db import reset_schema_for_tests

    reset_schema_for_tests(engine, postgres_url)
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
    cleanly on a fresh DB, the turn row is attached to the system session, and
    no ``ForeignKeyViolation`` marks it failed. Regression for the production
    ``turns_session_id_fkey`` failure where the dream task looped on retries.

    ``process_one_task`` is called repeatedly until a ``memory_dream`` turn
    exists; each call processes at most one task and the worker also seeds
    provider-maintenance tasks alongside the dream, so the dream may not be
    the first task popped on a single iteration."""
    from ariel.persistence import SYSTEM_SESSION_ID, TurnRecord, enqueue_background_task

    monkeypatch.setenv("ARIEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ariel.memory.embed_text", lambda t, *, settings: None)
    app = create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _DreamCompleteAdapter()),
        reset_database=True,
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
            try:
                processed = process_one_task(
                    session_factory=sf, settings=runtime.settings, runtime=runtime
                )
            except Exception:
                # The worker arms its own failure path; some tasks (e.g. the
                # google provider maintenance ones) have no configured runtime
                # in this test and the worker marks them failed without
                # raising. A surprise raise here is itself a regression.
                pytest.fail("process_one_task must never raise")
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
