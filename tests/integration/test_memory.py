"""Phase 1 contract tests for the crystallized memory subsystem.

These tests define the target behavior of the memory cutover described in
``docs/modules/memory-cutover.md``: a flat ``memory_facts`` store, a singleton
``memory_profile`` document, a per-session ``digest`` column, and exactly two
memory subagents (the retriever and the rememberer) reached through exactly two
syscalls. They are EXPECTED to fail or error against current ``main`` — the
schema, the module functions, and the syscall surface they assert do not exist
yet. Phases 2-5 make them pass.

The model is faked exactly as the existing memory tests fake it: the main turn
runs through a ``ModelAdapter`` whose ``create_response`` returns a canned
``run`` program, and the bounded retriever/rememberer subagents — raw
``httpx.post`` calls to the OpenAI Responses API — are stubbed by monkeypatching
``memory.httpx.post``, the pattern ``test_north_star_memory_pass.py`` uses for
``process_memory_extract_turn``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
import json
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from ariel.app import ModelAdapter, create_app
from ariel.capability_registry import (
    capability_id_for_run_callable,
    run_callable_name_for_capability_id,
)
import ariel.memory as memory
from ariel.config import AppSettings
from ariel.persistence import AIJudgmentRecord, BackgroundTaskRecord, SessionRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import responses_with_run_calls


_id_counter = count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_mem_{next(_id_counter)}"


def _settings(**overrides: Any) -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None, **overrides))


def _session_factory(client: TestClient) -> sessionmaker[Session]:
    return cast(Any, client.app).state.session_factory


# ---------------------------------------------------------------------------
# Fakes
#
# The 31 legacy tables (memory_assertions, memory_evidence, memory_episodes, …)
# that the cutover deletes. Listing them by name lets a schema test prove each
# one is gone, not merely that the count fell.
# ---------------------------------------------------------------------------
_DELETED_MEMORY_TABLES = (
    "memory_evidence",
    "memory_entities",
    "memory_relationships",
    "memory_assertions",
    "memory_assertion_evidence",
    "memory_episodes",
    "memory_reasoning_traces",
    "memory_action_traces",
    "memory_procedures",
    "memory_reviews",
    "memory_conflict_sets",
    "memory_conflict_members",
    "memory_salience",
    "memory_scope_bindings",
    "memory_retention_policies",
    "memory_sensitivity_labels",
    "memory_versions",
    "memory_deletions",
    "memory_projection_jobs",
    "memory_embedding_projections",
    "memory_temporal_projections",
    "memory_symbol_projections",
    "memory_keyword_projections",
    "memory_entity_projections",
    "memory_graph_projections",
    "memory_topics",
    "memory_topic_members",
    "memory_context_blocks",
    "memory_export_artifacts",
    "memory_eval_runs",
    "memory_events",
)

# The exact column set the spec fixes for ``memory_facts``. A fact is flat
# plain language: there is deliberately no kind/type/category/tag column.
_MEMORY_FACTS_COLUMNS = {
    "id",
    "content",
    "status",
    "source_turn_id",
    "source_excerpt",
    "embedding",
    "search_vector",
    "created_at",
    "updated_at",
    "last_recalled_at",
}


@dataclass
class RunProgramAdapter:
    """A ``ModelAdapter`` whose every turn returns a ``run`` program that just
    emits a message — the main turn does no memory syscall of its own, so each
    test exercises the automatic pre-turn retriever and post-turn rememberer.

    ``context_bundles`` and ``input_items`` record what the turn engine passed
    to the model, so a test can assert the profile, digest, and recalled facts
    were injected.
    """

    provider: str = "provider.memory-cutover"
    model: str = "model.memory-cutover"
    context_bundles: list[dict[str, Any]] = field(default_factory=list)
    input_items: list[list[dict[str, Any]]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, user_message, history
        self.context_bundles.append(json.loads(json.dumps(context_bundle)))
        self.input_items.append(json.loads(json.dumps(input_items)))
        return responses_with_run_calls(
            assistant_text="",
            calls=[{"name": "agent.emit_message", "input": {"text": "ok"}}],
            provider=self.provider,
            model=self.model,
            provider_response_id=f"resp_{_new_id('turn')}",
        )


@dataclass
class _FakeResponse:
    """A canned ``httpx`` response object for a stubbed Responses API call."""

    payload: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload


def _responses_output(body: dict[str, Any]) -> dict[str, Any]:
    """Wrap a JSON object as an OpenAI Responses ``output_text`` message."""

    return {
        "id": f"resp_{_new_id('sub')}",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(body)}],
            }
        ],
    }


def _embeddings_response() -> _FakeResponse:
    """A canned OpenAI embeddings response: a single fixed 1536-d unit vector.

    The rememberer computes an embedding for each written fact through a
    ``httpx.post`` to the embeddings endpoint; this stub keeps that call
    hermetic without coupling to the embedding helper's name.
    """

    vector = [0.0] * 1536
    vector[0] = 1.0
    return _FakeResponse({"data": [{"embedding": vector}]})


def _fake_post(
    *,
    retriever: dict[str, Any] | None = None,
    rememberer: dict[str, Any] | None = None,
    retriever_fails: bool = False,
) -> Any:
    """Build one ``httpx.post`` stub standing in for every memory model call.

    Memory makes two kinds of bounded ``httpx.post`` calls to the OpenAI API:
    the embeddings endpoint (rememberer fact embedding) and the Responses
    endpoint (the retriever and rememberer subagents, shaped like
    ``_curate_memory_context_with_model``). The stub branches on the request
    URL, then on the Responses system prompt to tell a retriever call from a
    rememberer call, returning the matching canned payload. ``retriever_fails``
    makes the retriever Responses call return HTTP 500 so the non-fatal-failure
    path can be exercised.
    """

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        if "embeddings" in url:
            return _embeddings_response()
        body = kwargs.get("json") or {}
        system = ""
        for item in body.get("input", []):
            if item.get("role") == "system":
                system = str(item.get("content", ""))
        is_retriever = "retriev" in system.lower() or "recall" in system.lower()
        if is_retriever:
            if retriever_fails:
                return _FakeResponse({"error": "boom"}, status_code=500)
            return _FakeResponse(_responses_output(retriever or {"facts": []}))
        return _FakeResponse(
            _responses_output(
                rememberer or {"operations": [], "profile": None, "digest": None}
            )
        )

    return fake_post


def _fake_subagent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    retriever: dict[str, Any] | None = None,
    rememberer: dict[str, Any] | None = None,
    retriever_fails: bool = False,
) -> None:
    """Install ``_fake_post`` as ``memory.httpx.post`` for the duration of a test."""

    monkeypatch.setattr(
        memory.httpx,
        "post",
        _fake_post(
            retriever=retriever,
            rememberer=rememberer,
            retriever_fails=retriever_fails,
        ),
    )


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    return TestClient(app)


def _active_session_id(client: TestClient) -> str:
    response = client.get("/v1/sessions/active")
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _send_turn(client: TestClient, session_id: str, message: str) -> dict[str, Any]:
    response = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"message": message},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# 1. Schema: two memory tables, the 31 are gone, sessions.digest exists
# ===========================================================================


def test_schema_has_only_memory_facts_and_memory_profile(postgres_url: str) -> None:
    """The crystallized schema holds exactly ``memory_facts`` and
    ``memory_profile``; every one of the 31 legacy ``memory_*`` tables is
    dropped, and ``sessions`` carries a ``digest`` column."""

    engine = create_engine(postgres_url, future=True)
    create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, RunProgramAdapter()),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    memory_tables = {name for name in table_names if name.startswith("memory_")}
    assert memory_tables == {"memory_facts", "memory_profile"}

    for legacy in _DELETED_MEMORY_TABLES:
        assert legacy not in table_names, f"{legacy} must be dropped by the cutover"

    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    assert "digest" in session_columns


def test_memory_facts_columns_are_flat_with_no_category_field(
    postgres_url: str,
) -> None:
    """``memory_facts`` has exactly the spec's ten columns. There is no
    kind/type/category/tag column: classification is the subagents reading
    content, never a schema column."""

    engine = create_engine(postgres_url, future=True)
    create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, RunProgramAdapter()),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("memory_facts")}

    assert columns == _MEMORY_FACTS_COLUMNS
    for forbidden in ("kind", "type", "category", "tag"):
        assert forbidden not in columns


def test_memory_profile_is_seeded_with_one_row(postgres_url: str) -> None:
    """``memory_profile`` is a singleton: the migration seeds exactly one
    profile row, so the profile document always exists to be injected."""

    engine = create_engine(postgres_url, future=True)
    create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, RunProgramAdapter()),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with engine.connect() as connection:
        profile_rows = connection.execute(
            text("SELECT count(*) FROM memory_profile")
        ).scalar_one()
    assert profile_rows == 1


def test_ai_judgment_type_check_admits_only_the_two_memory_judgments(
    postgres_url: str,
) -> None:
    """The ``ck_ai_judgment_type`` constraint accepts ``memory_recall`` and
    ``memory_remember`` and rejects the four removed memory judgment types
    (``memory_curation``, ``memory_extraction``, ``reflective_consolidation``,
    ``continuity_compaction``)."""

    engine = create_engine(postgres_url, future=True)
    create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, RunProgramAdapter()),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    for judgment_type in ("memory_recall", "memory_remember"):
        with session_factory() as db:
            with db.begin():
                db.add(_judgment_row(judgment_type))

    for removed in (
        "memory_curation",
        "memory_extraction",
        "reflective_consolidation",
        "continuity_compaction",
    ):
        with pytest.raises(Exception, match="ck_ai_judgment_type"):
            with session_factory() as db:
                with db.begin():
                    db.add(_judgment_row(removed))


def _judgment_row(judgment_type: str) -> AIJudgmentRecord:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    return AIJudgmentRecord(
        id=_new_id("aij"),
        judgment_type=judgment_type,
        source_type="turn",
        source_id="turn_memory_contract",
        status="succeeded",
        model="model.memory-cutover",
        prompt_version="memory-contract-v1",
        provider_response_id=None,
        input_summary="memory contract probe",
        input_refs={},
        selected=[],
        omitted=[],
        output={},
        rationale=None,
        uncertainty=None,
        confidence=None,
        parse_status="parsed",
        validation_status="valid",
        failure_code=None,
        failure_reason=None,
        created_at=now,
        updated_at=now,
    )


# ===========================================================================
# 2. The syscall surface is exactly memory.recall and memory.remember
# ===========================================================================


def test_memory_syscall_surface_is_exactly_recall_and_remember() -> None:
    """The model's entire memory surface is two ``allow_inline`` syscalls.
    ``memory.recall`` and ``memory.remember`` resolve to their capabilities;
    every other legacy ``memory.*`` run-callable alias is gone."""

    assert capability_id_for_run_callable("memory.recall") == "cap.memory.recall"
    assert capability_id_for_run_callable("memory.remember") == "cap.memory.remember"

    for removed in (
        "memory.search",
        "memory.inspect",
        "memory.propose",
        "memory.consolidate",
        "memory.delete",
        "memory.recall_diagnostics",
        "memory.export",
        "memory.import",
        "memory.resolve_conflict",
    ):
        assert capability_id_for_run_callable(removed) is None, removed


def test_only_two_memory_capabilities_have_run_callable_aliases() -> None:
    """Exactly two ``cap.memory.*`` capabilities exist and each maps to its
    syscall alias; no third memory capability is reachable from a program."""

    from ariel.capability_registry import MEMORY_CAPABILITY_IDS

    assert MEMORY_CAPABILITY_IDS == {"cap.memory.recall", "cap.memory.remember"}
    assert run_callable_name_for_capability_id("cap.memory.recall") == "memory.recall"
    assert run_callable_name_for_capability_id("cap.memory.remember") == "memory.remember"


def test_memory_syscalls_are_not_approval_gated() -> None:
    """Memory operations are never approval-gated: both syscall capabilities
    carry the ``allow_inline`` policy decision and a reversible impact level, so
    a program calling ``memory.remember`` runs the rememberer inline without an
    approval round."""

    from ariel.capability_registry import get_capability

    for capability_id in ("cap.memory.recall", "cap.memory.remember"):
        capability = get_capability(capability_id)
        assert capability is not None, capability_id
        assert capability.policy_decision == "allow_inline"
        assert capability.impact_level == "write_reversible"


# ===========================================================================
# 3. A written fact lands active immediately and is then recalled
# ===========================================================================


def test_rememberer_writes_a_fact_that_lands_active_immediately(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rememberer's ``write`` operation creates a ``memory_facts`` row that
    is ``status=active`` at once — there is no candidate or review state — and
    the row carries a non-null embedding computed by the handler."""

    _fake_subagent(
        monkeypatch,
        rememberer={
            "operations": [
                {"op": "write", "content": "The user takes their coffee black."}
            ],
            "profile": None,
            "digest": None,
        },
    )
    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _active_session_id(client)

        memory.run_rememberer(
            session_factory=_session_factory(client),
            note="Remember that I take my coffee black.",
            session_id=session_id,
            settings=_settings(openai_api_key="test-key"),
            now_fn=lambda: datetime.now(tz=UTC),
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            facts = list(db.execute(text("SELECT content, status, embedding FROM memory_facts")))
        assert len(facts) == 1
        content, status, embedding = facts[0]
        assert "coffee black" in content
        assert status == "active"
        assert embedding is not None


def test_retriever_surfaces_a_fact_it_judged_relevant(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fact written by the rememberer is then surfaced by the retriever: the
    retriever selects it, the turn injects it as a ``recalled memory`` section,
    and the fact's ``last_recalled_at`` is stamped."""

    # Step 1: write a fact through the rememberer.
    _fake_subagent(
        monkeypatch,
        rememberer={
            "operations": [
                {"op": "write", "content": "The user takes their coffee black."}
            ],
            "profile": None,
            "digest": None,
        },
    )
    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _active_session_id(client)
        memory.run_rememberer(
            session_factory=_session_factory(client),
            note="Remember that I take my coffee black.",
            session_id=session_id,
            settings=_settings(openai_api_key="test-key"),
            now_fn=lambda: datetime.now(tz=UTC),
            new_id_fn=_new_id,
        )
        with _session_factory(client)() as db:
            fact_id = db.execute(text("SELECT id FROM memory_facts LIMIT 1")).scalar_one()

        # Step 2: the pre-turn retriever selects that fact by id.
        _fake_subagent(monkeypatch, retriever={"facts": [{"id": fact_id}]})
        _send_turn(client, session_id, "how do I take my coffee?")

        recalled_section = json.dumps(adapter.context_bundles[-1])
        assert "coffee black" in recalled_section

        with _session_factory(client)() as db:
            last_recalled_at = db.execute(
                text("SELECT last_recalled_at FROM memory_facts WHERE id = :id"),
                {"id": fact_id},
            ).scalar_one()
        assert last_recalled_at is not None


# ===========================================================================
# 4. A turn injects the profile and the session digest
# ===========================================================================


def test_turn_injects_profile_and_session_digest(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every turn injects the always-loaded profile document and the
    per-session digest into the model's context, with no retrieval call and no
    token-budget machinery."""

    _fake_subagent(monkeypatch, retriever={"facts": []})
    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _active_session_id(client)

        # Seed the profile document and this session's digest directly, so the
        # turn-injection rail is what the test exercises — not the rememberer.
        with _session_factory(client)() as db:
            with db.begin():
                db.execute(
                    text("UPDATE memory_profile SET content = :content"),
                    {"content": "The user is a staff engineer who prefers terse answers."},
                )
                session = db.get(SessionRecord, session_id)
                assert session is not None
                session.digest = "The conversation is debugging a flaky deploy pipeline."

        _send_turn(client, session_id, "where were we?")

        rendered = json.dumps(adapter.context_bundles[-1]) + json.dumps(
            adapter.input_items[-1]
        )
        assert "staff engineer who prefers terse answers" in rendered
        assert "debugging a flaky deploy pipeline" in rendered


# ===========================================================================
# 5. A retriever model-call failure is non-fatal
# ===========================================================================


def test_retriever_failure_does_not_fail_the_turn(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retriever model-call failure is non-fatal: the turn still completes on
    the profile and digest alone, and the failure is recorded as an
    ``ai_judgments`` row with ``judgment_type=memory_recall`` and
    ``status=failed``."""

    _fake_subagent(monkeypatch, retriever_fails=True)
    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _active_session_id(client)
        with _session_factory(client)() as db:
            with db.begin():
                db.execute(
                    text("UPDATE memory_profile SET content = :content"),
                    {"content": "The user prefers concise replies."},
                )

        payload = _send_turn(client, session_id, "anything at all")
        # The turn completed despite the failed retriever call.
        assert payload["assistant"]["message"]

        with _session_factory(client)() as db:
            failed_recall = db.scalar(
                select(AIJudgmentRecord).where(
                    AIJudgmentRecord.judgment_type == "memory_recall",
                    AIJudgmentRecord.status == "failed",
                )
            )
        assert failed_recall is not None
        # The profile still reached the model; recall is not on the turn's
        # critical-failure path.
        assert "concise replies" in json.dumps(adapter.context_bundles[-1])


# ===========================================================================
# 6. Post-turn rememberer runs as one bounded, audited call
# ===========================================================================


def test_turn_enqueues_a_memory_remember_task(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a turn completes, the engine enqueues one ``memory_remember``
    background task — the post-turn rememberer trigger — replacing the legacy
    ``memory_extract_turn`` evidence/extraction path."""

    _fake_subagent(monkeypatch, retriever={"facts": []})
    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _active_session_id(client)
        _send_turn(client, session_id, "remember I like espresso")

        with _session_factory(client)() as db:
            remember_tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "memory_remember"
                )
            ).all()
            extract_tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "memory_extract_turn"
                )
            ).all()
        assert len(remember_tasks) == 1
        assert extract_tasks == []


def test_rememberer_call_writes_one_ai_judgment_row(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every rememberer invocation is one bounded model call audited by exactly
    one ``ai_judgments`` row with ``judgment_type=memory_remember``."""

    _fake_subagent(
        monkeypatch,
        rememberer={
            "operations": [{"op": "write", "content": "The user uses an espresso machine."}],
            "profile": None,
            "digest": None,
        },
    )
    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        session_id = _active_session_id(client)
        memory.run_rememberer(
            session_factory=_session_factory(client),
            note="Remember that I use an espresso machine.",
            session_id=session_id,
            settings=_settings(openai_api_key="test-key"),
            now_fn=lambda: datetime.now(tz=UTC),
            new_id_fn=_new_id,
        )

        with _session_factory(client)() as db:
            remember_judgments = db.scalars(
                select(AIJudgmentRecord).where(
                    AIJudgmentRecord.judgment_type == "memory_remember"
                )
            ).all()
        assert len(remember_judgments) == 1
        assert remember_judgments[0].status == "succeeded"


# ===========================================================================
# 7. The periodic memory_sweep forgets stale facts and hard-deletes
#    long-forgotten rows
# ===========================================================================


def test_memory_sweep_forgets_stale_facts_and_deletes_long_forgotten_rows(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The periodic ``memory_sweep`` task drives the rememberer over the store:
    a fact the rememberer judges stale is moved to ``status=forgotten``, and a
    row already ``forgotten`` long enough is hard-deleted."""

    adapter = RunProgramAdapter()
    with _build_client(postgres_url, cast(ModelAdapter, adapter)) as client:
        old = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        fresh_id = _new_id("mfa")
        stale_id = _new_id("mfa")

        # One active fact the rememberer will forget, and one long-forgotten
        # fact the sweep should hard-delete.
        with _session_factory(client)() as db:
            with db.begin():
                db.execute(
                    text(
                        "INSERT INTO memory_facts "
                        "(id, content, status, created_at, updated_at) VALUES "
                        "(:fresh, 'a fact that is now stale', 'active', :now, :now), "
                        "(:stale, 'a fact forgotten long ago', 'forgotten', :old, :old)"
                    ),
                    {"fresh": fresh_id, "stale": stale_id, "now": now, "old": old},
                )

        # The sweep rememberer judges the fresh active fact stale and forgets
        # it; the long-forgotten row is the sweep's own hard-delete target.
        _fake_subagent(
            monkeypatch,
            rememberer={
                "operations": [{"op": "forget", "fact_id": fresh_id}],
                "profile": None,
                "digest": None,
            },
        )

        from ariel.worker import process_one_task

        # Enqueue and run the periodic sweep task.
        with _session_factory(client)() as db:
            with db.begin():
                db.add(
                    BackgroundTaskRecord(
                        id=_new_id("tsk"),
                        task_type="memory_sweep",
                        idempotency_key=None,
                        work_follow_up_loop_id=None,
                        work_follow_up_loop_version=None,
                        work_follow_up_scheduled_for=None,
                        provider_write_receipt_id=None,
                        payload={},
                        status="pending",
                        attempts=0,
                        max_attempts=3,
                        error=None,
                        claimed_by=None,
                        run_after=now,
                        last_heartbeat=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

        assert process_one_task(
            session_factory=_session_factory(client),
            settings=_settings(openai_api_key="test-key"),
            worker_id="worker-memory-sweep",
        )

        with _session_factory(client)() as db:
            fresh_status = db.execute(
                text("SELECT status FROM memory_facts WHERE id = :id"),
                {"id": fresh_id},
            ).scalar_one_or_none()
            stale_row = db.execute(
                text("SELECT id FROM memory_facts WHERE id = :id"),
                {"id": stale_id},
            ).scalar_one_or_none()
        # The stale active fact was forgotten by the rememberer.
        assert fresh_status == "forgotten"
        # The long-forgotten row was hard-deleted by the sweep.
        assert stale_row is None
