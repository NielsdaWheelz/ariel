"""Ariel's memory substrate — a two-layer append-only raw log plus an editable
curated note layer, with agentic retrieval and encoding/dreaming.

Three functions drive the substrate, all of them ``run_agent_loop`` in a
different configuration:

- ``run_retriever`` — fires every wake; reconstructs the working context
  agentically by searching and reading the substrate, then returns a
  ``recall_v1`` finding.
- ``run_rememberer`` — writes the curated layer on ``encode`` (agent-invoked)
  or ``dream`` (scheduled) triggers; never the raw log.
- The raw log is written only by ``append_log_event`` (a rail); the curated
  layer only by ``create_note`` / ``edit_note`` / ``delete_note`` (rails that
  also append ``note_*`` events to the log).

Deterministic code here does exactly five things, all rails: durable substrate
storage, embedding computation, hybrid search, loop configuration, and
background-task enqueueing.  It makes no relevance, importance, ranking, or
"worth remembering" judgment and summarises nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import httpx
import ulid
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import AppSettings
from .persistence import (
    BackgroundTaskRecord,
    MemoryLogRecord,
    MemoryNoteRecord,
    TurnRecord,
    enqueue_background_task,
    to_rfc3339,
)
from .run_runtime import run_tool_definitions

if TYPE_CHECKING:
    from .agency_daemon import AgencyRuntime
    from .app import ModelAdapter
    from .attachment_content import AttachmentContentRuntime
    from .google_connector import GoogleConnectorRuntime
    from .sandbox_runtime import RunSandbox

from .agent_loop import LoopConfig, LoopResult, run_agent_loop
from .response_contracts import validate_memory_recall_v1


# ---------------------------------------------------------------------------
# Prompt-version constants
# ---------------------------------------------------------------------------

RETRIEVER_PROMPT_VERSION = "memory-retriever-v1"
REMEMBERER_ENCODE_PROMPT_VERSION = "memory-rememberer-encode-v1"
REMEMBERER_DREAM_PROMPT_VERSION = "memory-rememberer-dream-v1"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_RETRIEVER_PROMPT = (
    "You are Ariel's memory retriever. Your job is to reconstruct the working "
    "context for the current wake by agentically searching the memory substrate. "
    "You may call memory.search(query, limit, since, kinds) to search both the "
    "raw log and the curated notes, and memory.read(id) to fetch the full content "
    "of any row by id. Search broadly, read what looks relevant, and follow up "
    "across multiple rounds if the first results lead to more useful entries. "
    "When satisfied — or when you have covered both relevance-based recall and "
    "recent-session continuity — call agent.emit_finding with: "
    '{"summary": "<the reconstructed working context in plain language>", '
    '"items": [{"id": ..., "layer": ..., "created_at": ..., "content": ..., '
    '"taint": ...}, ...], "status": "complete"}. '
    "If you exhaust your budget before finishing, the loop ends with status "
    '"partial" automatically. The retriever fires on every wake — cover both '
    "semantic relevance and recent session continuity so the main agent can act "
    "without missing recent history. Do not invent ids; every id in items must "
    "be a real id returned by a memory.search or memory.read call."
)

_REMEMBERER_ENCODE_PROMPT = (
    "You are Ariel's memory encoder. The user message will include "
    '{"trigger": "encode", "note": "<what to remember>", ...}. '
    "Your job is to write this to the curated note layer, editing rather than "
    "duplicating when a related note already exists. "
    "First call memory.search (with kinds omitted to search the curated layer) "
    "to find relevant existing notes. Read candidates with memory.read(id) to "
    "inspect their content. Then apply memory.note.create(content), "
    "memory.note.edit(id, content), and/or memory.note.delete(id) as needed — "
    "edit a note when the new material updates or extends it; delete a note "
    "only when it is fully superseded. When done, call agent.emit_done with a "
    "short string describing what you did (e.g. 'edited note mno_... to add "
    "preference for dark mode'). The raw log is append-only and must never be "
    "the target of note operations."
)

_REMEMBERER_DREAM_PROMPT = (
    "You are Ariel's memory dreamer. The user message will include "
    '{"trigger": "dream"}. '
    "Your job is to consolidate the recent raw log into the curated note layer: "
    "generalizations, summaries, connections, topic abstractions. "
    "Read recent log events with memory.search (over the log; use the since "
    "parameter to scope to recent time). Also search the curated layer for "
    "existing notes to edit or delete rather than duplicate. Write new notes "
    "with memory.note.create(content), update existing ones with "
    "memory.note.edit(id, content), and delete superseded ones with "
    "memory.note.delete(id). The raw log is append-only — only memory_notes "
    "is mutable; never use note operations with a log id. When satisfied, call "
    "agent.emit_done with a short summary of what you consolidated."
)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def embed_text(text: str, *, settings: AppSettings) -> list[float]:
    """Embed ``text`` with the configured OpenAI embedding model."""
    if settings.memory_embedding_provider != "openai":
        raise RuntimeError(
            f"unsupported memory embedding provider: {settings.memory_embedding_provider}"
        )
    if settings.openai_api_key is None:
        raise RuntimeError("ARIEL_OPENAI_API_KEY is required for memory embeddings")

    response = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.memory_embedding_model,
            "input": " ".join(text.split()),
            "dimensions": settings.memory_embedding_dimensions,
            "encoding_format": "float",
        },
        timeout=settings.model_timeout_seconds,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"memory embedding request failed: HTTP {exc.response.status_code}"
        ) from exc

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    vector = first.get("embedding") if isinstance(first, dict) else None
    if not isinstance(vector, list):
        raise RuntimeError("memory embedding response missing vector")
    if len(vector) != settings.memory_embedding_dimensions:
        raise RuntimeError(
            "memory embedding response dimension mismatch: "
            f"expected {settings.memory_embedding_dimensions}, got {len(vector)}"
        )
    if not all(isinstance(item, int | float) for item in vector):
        raise RuntimeError("memory embedding response vector must be numeric")
    return [float(item) for item in vector]


# ---------------------------------------------------------------------------
# Substrate rails — raw log
# ---------------------------------------------------------------------------

_LogKind = Literal[
    "user_message",
    "agent_round",
    "assistant_message",
    "tool_observation",
    "proactive_trigger",
    "note_create",
    "note_edit",
    "note_delete",
    "recall",
    "research_finding",
]


def append_log_event(
    db: Session,
    *,
    kind: _LogKind,
    content: str,
    session_id: str | None,
    turn_id: str | None,
    taint: Literal["clean", "tainted"],
    source_ref: str | None,
    settings: AppSettings,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryLogRecord:
    """Append one event to the raw log inside the caller's transaction.

    Computes the embedding via ``embed_text``; on ``RuntimeError`` (network
    failure) inserts with ``embedding=None`` (null = pending, to be backfilled).
    """
    try:
        embedding: list[float] | None = embed_text(content, settings=settings)
    except RuntimeError:
        embedding = None

    record = MemoryLogRecord(
        id=new_id_fn("mev"),
        created_at=now,
        kind=kind,
        content=content,
        embedding=embedding,
        session_id=session_id,
        turn_id=turn_id,
        taint=taint,
        source_ref=source_ref,
    )
    db.add(record)
    db.flush()
    return record


def read_log_entry(db: Session, log_id: str) -> MemoryLogRecord | None:
    """Return the log row for ``log_id``, or ``None`` if not found."""
    return db.scalar(select(MemoryLogRecord).where(MemoryLogRecord.id == log_id))


# ---------------------------------------------------------------------------
# Substrate rails — curated notes
# ---------------------------------------------------------------------------


def read_note(db: Session, note_id: str) -> MemoryNoteRecord | None:
    """Return the note row for ``note_id``, or ``None`` if not found."""
    return db.scalar(select(MemoryNoteRecord).where(MemoryNoteRecord.id == note_id))


def create_note(
    db: Session,
    *,
    content: str,
    taint: Literal["clean", "tainted"],
    settings: AppSettings,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryNoteRecord:
    """Insert a new curated note and append a ``note_create`` log event."""
    try:
        embedding: list[float] | None = embed_text(content, settings=settings)
    except RuntimeError:
        embedding = None

    note = MemoryNoteRecord(
        id=new_id_fn("mno"),
        content=content,
        embedding=embedding,
        taint=taint,
        created_at=now,
        updated_at=now,
    )
    db.add(note)
    db.flush()

    append_log_event(
        db,
        kind="note_create",
        content=content,
        session_id=None,
        turn_id=None,
        taint=taint,
        source_ref=note.id,
        settings=settings,
        now=now,
        new_id_fn=new_id_fn,
    )
    return note


def edit_note(
    db: Session,
    *,
    note_id: str,
    content: str,
    settings: AppSettings,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryNoteRecord:
    """Rewrite a note's content in place and append a ``note_edit`` log event."""
    note = db.scalar(select(MemoryNoteRecord).where(MemoryNoteRecord.id == note_id))
    if note is None:
        raise RuntimeError(f"edit_note: note {note_id!r} does not exist")

    try:
        embedding: list[float] | None = embed_text(content, settings=settings)
    except RuntimeError:
        embedding = None

    note.content = content
    note.embedding = embedding
    note.updated_at = now
    db.flush()

    note_taint: Literal["clean", "tainted"] = "tainted" if note.taint == "tainted" else "clean"
    append_log_event(
        db,
        kind="note_edit",
        content=content,
        session_id=None,
        turn_id=None,
        taint=note_taint,
        source_ref=note.id,
        settings=settings,
        now=now,
        new_id_fn=new_id_fn,
    )
    return note


def delete_note(
    db: Session,
    *,
    note_id: str,
    settings: AppSettings,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    """Delete a curated note and append a ``note_delete`` log event."""
    note = db.scalar(select(MemoryNoteRecord).where(MemoryNoteRecord.id == note_id))
    if note is None:
        raise RuntimeError(f"delete_note: note {note_id!r} does not exist")

    note_taint: Literal["clean", "tainted"] = "tainted" if note.taint == "tainted" else "clean"
    db.delete(note)
    db.flush()

    append_log_event(
        db,
        kind="note_delete",
        content=note_id,
        session_id=None,
        turn_id=None,
        taint=note_taint,
        source_ref=note_id,
        settings=settings,
        now=now,
        new_id_fn=new_id_fn,
    )


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------


def search_memory(
    db: Session,
    *,
    query: str,
    settings: AppSettings,
    limit: int = 24,
    since: datetime | None = None,
    kinds: tuple[str, ...] | None = None,
    layers: tuple[Literal["log", "note"], ...] = ("log", "note"),
) -> list[dict[str, Any]]:
    """Hybrid search across ``memory_log`` and ``memory_notes``.

    Computes the query embedding once; for each requested layer runs a keyword
    tsquery match against ``search_vector`` and a vector cosine-distance match
    against ``embedding`` (skipped when no rows have embeddings yet). Unions
    the results, deduplicates by id, applies ``since``/``kinds`` filters on the
    log layer, and returns up to ``limit`` hits ordered by ``created_at DESC``
    (transport order — the model ranks, code does not).

    Returns ``[{"id", "layer", "kind", "created_at", "snippet", "taint"}, ...]``.
    ``"kind"`` is ``None`` for note-layer hits.
    """
    try:
        query_embedding: list[float] | None = embed_text(query, settings=settings)
    except RuntimeError:
        query_embedding = None

    hits: dict[str, dict[str, Any]] = {}

    if "log" in layers:
        tsquery = func.websearch_to_tsquery("english", query)
        stmt = select(MemoryLogRecord).where(MemoryLogRecord.search_vector.op("@@")(tsquery))
        if since is not None:
            stmt = stmt.where(MemoryLogRecord.created_at >= since)
        if kinds is not None:
            stmt = stmt.where(MemoryLogRecord.kind.in_(kinds))
        for row in db.scalars(stmt.order_by(MemoryLogRecord.created_at.desc()).limit(limit)).all():
            hits[row.id] = {
                "id": row.id,
                "layer": "log",
                "kind": row.kind,
                "created_at": to_rfc3339(row.created_at),
                "snippet": row.content[:200],
                "taint": row.taint,
            }

        has_log_embeddings = db.scalar(
            select(MemoryLogRecord.id).where(MemoryLogRecord.embedding.is_not(None)).limit(1)
        )
        if query_embedding is not None and has_log_embeddings is not None:
            vec_stmt = select(MemoryLogRecord).where(MemoryLogRecord.embedding.is_not(None))
            if since is not None:
                vec_stmt = vec_stmt.where(MemoryLogRecord.created_at >= since)
            if kinds is not None:
                vec_stmt = vec_stmt.where(MemoryLogRecord.kind.in_(kinds))
            distance = MemoryLogRecord.embedding.cosine_distance(query_embedding)
            for row in db.scalars(vec_stmt.order_by(distance.asc()).limit(limit)).all():
                if row.id not in hits:
                    hits[row.id] = {
                        "id": row.id,
                        "layer": "log",
                        "kind": row.kind,
                        "created_at": to_rfc3339(row.created_at),
                        "snippet": row.content[:200],
                        "taint": row.taint,
                    }

    if "note" in layers:
        tsquery = func.websearch_to_tsquery("english", query)
        note_stmt = select(MemoryNoteRecord).where(MemoryNoteRecord.search_vector.op("@@")(tsquery))
        for note_row in db.scalars(
            note_stmt.order_by(MemoryNoteRecord.created_at.desc()).limit(limit)
        ).all():
            hits[note_row.id] = {
                "id": note_row.id,
                "layer": "note",
                "kind": None,
                "created_at": to_rfc3339(note_row.created_at),
                "snippet": note_row.content[:200],
                "taint": note_row.taint,
            }

        has_note_embeddings = db.scalar(
            select(MemoryNoteRecord.id).where(MemoryNoteRecord.embedding.is_not(None)).limit(1)
        )
        if query_embedding is not None and has_note_embeddings is not None:
            distance = MemoryNoteRecord.embedding.cosine_distance(query_embedding)
            for note_row in db.scalars(
                select(MemoryNoteRecord)
                .where(MemoryNoteRecord.embedding.is_not(None))
                .order_by(distance.asc())
                .limit(limit)
            ).all():
                if note_row.id not in hits:
                    hits[note_row.id] = {
                        "id": note_row.id,
                        "layer": "note",
                        "kind": None,
                        "created_at": to_rfc3339(note_row.created_at),
                        "snippet": note_row.content[:200],
                        "taint": note_row.taint,
                    }

    return sorted(hits.values(), key=lambda h: h["created_at"], reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Loop helpers shared by run_retriever / run_rememberer
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# The retriever — run_agent_loop in investigation/memories/lite config
# ---------------------------------------------------------------------------


def run_retriever(
    *,
    sandbox: RunSandbox,
    db: Session,
    session_factory: sessionmaker[Session],
    session_id: str,
    turn: TurnRecord,
    settings: AppSettings,
    model_adapter: ModelAdapter,
    google_runtime: GoogleConnectorRuntime | None,
    agency_runtime: AgencyRuntime | None,
    attachment_runtime: AttachmentContentRuntime | None,
    query: str,
    allowed_capability_ids: frozenset[str],
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Run the retriever and return a ``recall_v1`` dict.

    Fires the shared loop in ``investigation``/``memories``/``lite``
    configuration.  The retriever builds its own input items — context
    firewall: its rounds never enter the main agent's context.  On any
    failure (budget, model error, contract violation) returns a minimal
    partial dict rather than raising; recall failure is non-fatal.
    """
    eligible_callables = ["memory.search", "memory.read", "agent.emit_finding"]
    callable_lines = "\n".join(f"- {name}" for name in eligible_callables)

    responses_input_items: list[dict[str, Any]] = [
        {"role": "system", "content": _RETRIEVER_PROMPT},
        {
            "role": "system",
            "content": json.dumps(
                {"prompt_version": RETRIEVER_PROMPT_VERSION, "wake_context": query},
                sort_keys=True,
            ),
        },
        {
            "role": "system",
            "content": (
                "syscall callables available this run "
                "(call as namespace.member(...); results are returned inline):\n"
            )
            + callable_lines,
        },
        {"role": "user", "content": query},
    ]

    cfg = LoopConfig(
        output_mode="finding",
        finding_mode="memory_recall",
        budget_seconds=float(settings.memory_recall_budget_seconds),
        max_model_calls=int(settings.agent_loop_max_model_calls),
        is_research_run=True,
        record_judgments=True,
        judgment_type="memory_recall",
        retry_on_model_error=False,
        void_failed_program_approvals=False,
        protocol_nudge=(
            "memory retriever protocol failure: call exactly one run tool "
            'with {"source": "..."} where source is a Python program; '
            "finish by calling agent.emit_finding."
        ),
        program_failure_nudge=(
            "No effects committed. Retry with one run call whose program "
            "completes cleanly; finish by calling agent.emit_finding."
        ),
        action_trace_nudge=("Continue searching; finish by calling agent.emit_finding."),
        emit_value_nudge=(
            "Values emitted. Continue with one run call; finish by calling agent.emit_finding."
        ),
        fallback_nudge=(
            "Program completed without a finding. Continue with one run call; "
            "finish by calling agent.emit_finding."
        ),
    )

    loop_result: LoopResult = run_agent_loop(
        cfg,
        sandbox=sandbox,
        db=db,
        session_factory=session_factory,
        session_id=session_id,
        turn=turn,
        settings=settings,
        model_adapter=model_adapter,
        responses_input_items=responses_input_items,
        tools=run_tool_definitions(),
        user_message=query,
        history=[],
        context_bundle={},
        allowed_capability_ids=allowed_capability_ids,
        scratch={},
        proposal_index_start=0,
        approval_ttl_seconds=approval_ttl_seconds,
        approval_actor_id=approval_actor_id,
        add_event=add_event,
        now_fn=now_fn,
        new_id_fn=new_id_fn,
        runtime_provenance=None,
        google_runtime=google_runtime,
        execute_google_reads_outside_transaction=False,
        agency_runtime=agency_runtime,
        attachment_runtime=attachment_runtime,
    )

    if loop_result.emitted_finding is not None:
        raw = loop_result.emitted_finding
        payload = {
            "summary": raw.summary,
            "items": raw.claims,  # finding carries items in claims for recall_v1
            "status": raw.status,
        }
        try:
            return validate_memory_recall_v1(payload)
        except Exception:
            pass

    return {"summary": "", "items": [], "status": "partial"}


# ---------------------------------------------------------------------------
# The rememberer — run_agent_loop in rememberer/encode|dream config
# ---------------------------------------------------------------------------


def run_rememberer(
    *,
    trigger: Literal["encode", "dream"],
    sandbox: RunSandbox,
    db: Session,
    session_factory: sessionmaker[Session],
    session_id: str | None,
    settings: AppSettings,
    model_adapter: ModelAdapter,
    google_runtime: GoogleConnectorRuntime | None,
    agency_runtime: None,
    attachment_runtime: None,
    note: str | None,
    allowed_capability_ids: frozenset[str],
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    """Run the rememberer (encode or dream) as a background task.

    Builds its own ``TurnRecord`` (kind ``memory_encode`` or ``memory_dream``),
    calls ``run_agent_loop`` in the rememberer configuration, and returns
    ``None``.  The loop applies note mutations via ``memory.note.*`` syscalls
    (which call ``create_note`` / ``edit_note`` / ``delete_note`` above, which
    also log the events).  Never raises.
    """
    effective_session_id = session_id or _new_id("ses")

    now = now_fn()
    system_prompt = _REMEMBERER_ENCODE_PROMPT if trigger == "encode" else _REMEMBERER_DREAM_PROMPT
    prompt_version = (
        REMEMBERER_ENCODE_PROMPT_VERSION if trigger == "encode" else REMEMBERER_DREAM_PROMPT_VERSION
    )
    judgment_type: Literal["memory_encode", "memory_dream"] = (
        "memory_encode" if trigger == "encode" else "memory_dream"
    )
    budget = (
        float(settings.memory_encode_budget_seconds)
        if trigger == "encode"
        else float(settings.memory_dream_budget_seconds)
    )

    user_payload = {"prompt_version": prompt_version, "trigger": trigger, "note": note}

    with session_factory() as task_db:
        with task_db.begin():
            turn = TurnRecord(
                id=new_id_fn("trn"),
                session_id=effective_session_id,
                user_message=json.dumps(user_payload, sort_keys=True),
                assistant_message=None,
                status="in_progress",
                kind=judgment_type,
                created_at=now,
                updated_at=now,
            )
            task_db.add(turn)
            task_db.flush()
            turn_id = turn.id

    eligible_callables = [
        "memory.search",
        "memory.read",
        "memory.note.create",
        "memory.note.edit",
        "memory.note.delete",
        "agent.emit_done",
    ]
    callable_lines = "\n".join(f"- {name}" for name in eligible_callables)

    responses_input_items: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "system",
            "content": (
                "syscall callables available this run "
                "(call as namespace.member(...); results are returned inline):\n"
            )
            + callable_lines,
        },
        {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
    ]

    cfg = LoopConfig(
        output_mode="operations",
        finding_mode=trigger,
        budget_seconds=budget,
        max_model_calls=int(settings.agent_loop_max_model_calls),
        is_research_run=True,
        record_judgments=True,
        judgment_type=judgment_type,
        retry_on_model_error=False,
        void_failed_program_approvals=False,
        protocol_nudge=(
            f"memory {trigger} protocol failure: call exactly one run tool "
            'with {"source": "..."} where source is a Python program; '
            "finish by calling agent.emit_done."
        ),
        program_failure_nudge=(
            "No effects committed. Retry with one run call whose program "
            "completes cleanly; finish by calling agent.emit_done."
        ),
        action_trace_nudge=("Continue; finish by calling agent.emit_done."),
        emit_value_nudge=(
            "Values emitted. Continue with one run call; finish by calling agent.emit_done."
        ),
        fallback_nudge=(
            "Program completed without finishing. Continue with one run call; "
            "finish by calling agent.emit_done."
        ),
    )

    with session_factory() as loop_db:
        with loop_db.begin():
            loop_turn = loop_db.get(TurnRecord, turn_id)
            if loop_turn is None:
                raise RuntimeError(f"run_rememberer: turn {turn_id!r} missing")
            run_agent_loop(
                cfg,
                sandbox=sandbox,
                db=loop_db,
                session_factory=session_factory,
                session_id=effective_session_id,
                turn=loop_turn,
                settings=settings,
                model_adapter=model_adapter,
                responses_input_items=responses_input_items,
                tools=run_tool_definitions(),
                user_message=json.dumps(user_payload, sort_keys=True),
                history=[],
                context_bundle={},
                allowed_capability_ids=allowed_capability_ids,
                scratch={},
                proposal_index_start=0,
                approval_ttl_seconds=approval_ttl_seconds,
                approval_actor_id=approval_actor_id,
                add_event=add_event,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
                runtime_provenance=None,
                google_runtime=google_runtime,
                execute_google_reads_outside_transaction=False,
                agency_runtime=None,
                attachment_runtime=None,
            )
            loop_turn.status = "completed"
            loop_turn.updated_at = now_fn()


# ---------------------------------------------------------------------------
# Background-task enqueuers
# ---------------------------------------------------------------------------


def enqueue_memory_encode(
    db: Session,
    *,
    note: str,
    session_id: str | None,
    now: datetime,
) -> str:
    """Enqueue a ``memory_encode`` background task. Returns the task id."""
    stable_id = _new_id("enc")
    task = enqueue_background_task(
        db,
        task_type="memory_encode",
        idempotency_key=f"memory_encode:{stable_id}",
        payload={"note": note, "session_id": session_id},
        now=now,
    )
    return task.id


def enqueue_due_memory_dream(
    db: Session,
    *,
    settings: AppSettings,
    now: datetime,
) -> str | None:
    """Self-gating periodic dream enqueuer.

    Enqueues one ``memory_dream`` task when no dream has been enqueued within
    the configured interval; otherwise returns ``None``.
    """
    interval = timedelta(seconds=settings.memory_dream_interval_seconds)
    recent = db.scalar(
        select(BackgroundTaskRecord.id)
        .where(
            BackgroundTaskRecord.task_type == "memory_dream",
            BackgroundTaskRecord.created_at > now - interval,
        )
        .limit(1)
    )
    if recent is not None:
        return None
    task = enqueue_background_task(db, task_type="memory_dream", payload={}, now=now)
    return task.id
