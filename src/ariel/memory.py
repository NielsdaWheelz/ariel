from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
import json
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import AppSettings
from .persistence import (
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryConflictMemberRecord,
    MemoryConflictSetRecord,
    MemoryContextBlockRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEntityProjectionRecord,
    MemoryEntityRecord,
    MemoryEpisodeRecord,
    MemoryEvidenceRecord,
    MemoryGraphProjectionRecord,
    MemoryKeywordProjectionRecord,
    MemoryProcedureRecord,
    MemoryProjectionJobRecord,
    MemoryRelationshipRecord,
    MemoryReviewRecord,
    MemorySalienceRecord,
    MemoryVersionRecord,
    ProjectStateSnapshotRecord,
    TurnRecord,
    to_rfc3339,
)
from .redaction import redact_text


MEMORY_CONTEXT_SCHEMA_VERSION = "memory.sota.v1"
MEMORY_PROJECTION_VERSION = "embedding-v1"
USER_SUBJECT_KEY = "user:default"

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "again",
    "be",
    "do",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
}


def _clean_text(value: str, *, max_chars: int = 700) -> str:
    return " ".join(value.strip().split())[:max_chars]


def _memory_key(value: str) -> str:
    pieces: list[str] = []
    last_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            pieces.append(char)
            last_was_separator = False
        elif not last_was_separator:
            pieces.append("_")
            last_was_separator = True
    return "".join(pieces).strip("_") or "general"


def _terms(value: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    for char in value.lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if token not in _STOPWORDS:
                terms.append(token)
            current = []
    if current:
        token = "".join(current)
        if token not in _STOPWORDS:
            terms.append(token)
    return terms


def _weighted_terms(value: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for term in _terms(value):
        weights[term] = weights.get(term, 0.0) + 1.0
    return weights


def embed_memory_text(text: str, *, settings: AppSettings) -> list[float]:
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


def _assertion_text(assertion: MemoryAssertionRecord) -> str:
    value = assertion.object_value if isinstance(assertion.object_value, dict) else {}
    text = value.get("text")
    return text if isinstance(text, str) else ""


def _assertion_search_text(assertion: MemoryAssertionRecord) -> str:
    return " ".join(
        (
            assertion.subject_key,
            assertion.predicate,
            assertion.assertion_type,
            _assertion_text(assertion),
        )
    )


def _entity_type(subject_key: str, assertion_type: str) -> str:
    if subject_key.startswith("project:"):
        return "project"
    if subject_key.startswith("repo:"):
        return "repo"
    if assertion_type == "commitment":
        return "commitment"
    if assertion_type == "decision":
        return "decision"
    if assertion_type == "procedure":
        return "procedure"
    if assertion_type == "preference":
        return "preference"
    return "user"


def _ensure_entity(
    db: Session,
    *,
    subject_key: str,
    assertion_type: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryEntityRecord:
    entity_type = _entity_type(subject_key, assertion_type)
    entity = db.scalar(
        select(MemoryEntityRecord)
        .where(
            MemoryEntityRecord.entity_type == entity_type,
            MemoryEntityRecord.entity_key == subject_key,
        )
        .limit(1)
    )
    if entity is not None:
        return entity
    entity = MemoryEntityRecord(
        id=new_id_fn("men"),
        entity_type=entity_type,
        entity_key=subject_key,
        display_name=subject_key,
        summary=None,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(entity)
    db.flush()
    return entity


def _record_evidence(
    db: Session,
    *,
    session_id: str,
    turn_id: str | None,
    actor_id: str,
    content_class: str,
    trust_boundary: str,
    source_text: str,
    source_uri: str | None,
    metadata: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryEvidenceRecord:
    text = _clean_text(source_text, max_chars=12_000)
    evidence = MemoryEvidenceRecord(
        id=new_id_fn("mev"),
        source_turn_id=turn_id,
        source_session_id=session_id,
        actor_id=actor_id,
        content_class=content_class,
        trust_boundary=trust_boundary,
        lifecycle_state="available",
        source_uri=source_uri,
        source_artifact_id=None,
        source_text=text,
        evidence_snippet=redact_text(_clean_text(text, max_chars=360)),
        redaction_posture="none",
        metadata_json=metadata,
        created_at=now,
        updated_at=now,
    )
    db.add(evidence)
    db.flush()
    return evidence


def _record_review(
    db: Session,
    *,
    assertion_id: str,
    decision: str,
    actor_id: str,
    reason: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    db.add(
        MemoryReviewRecord(
            id=new_id_fn("mrv"),
            assertion_id=assertion_id,
            decision=decision,
            reason=reason,
            actor_id=actor_id,
            created_at=now,
        )
    )


def _record_version(
    db: Session,
    *,
    table: str,
    record_id: str,
    change_type: str,
    actor_id: str,
    reason: str | None,
    new_state: dict[str, Any] | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    last_version = db.scalar(
        select(MemoryVersionRecord.version)
        .where(
            MemoryVersionRecord.canonical_table == table,
            MemoryVersionRecord.canonical_id == record_id,
        )
        .order_by(MemoryVersionRecord.version.desc())
        .limit(1)
    )
    db.add(
        MemoryVersionRecord(
            id=new_id_fn("mvr"),
            canonical_table=table,
            canonical_id=record_id,
            version=1 if last_version is None else last_version + 1,
            change_type=change_type,
            actor_id=actor_id,
            reason=reason,
            prior_state=None,
            new_state=new_state,
            redaction_posture="none",
            projection_invalidation={},
            created_at=now,
        )
    )


def _event_payload(
    assertion: MemoryAssertionRecord, *, evidence_id: str | None = None
) -> dict[str, Any]:
    payload = {
        "assertion_id": assertion.id,
        "subject_key": assertion.subject_key,
        "predicate": assertion.predicate,
        "assertion_type": assertion.assertion_type,
        "lifecycle_state": assertion.lifecycle_state,
        "value_preview": redact_text(_assertion_text(assertion)),
        "confidence": assertion.confidence,
    }
    if evidence_id is not None:
        payload["evidence_id"] = evidence_id
    return payload


def _active_single_assertions(
    db: Session,
    *,
    subject_entity_id: str,
    predicate: str,
    scope_key: str,
) -> list[MemoryAssertionRecord]:
    return list(
        db.scalars(
            select(MemoryAssertionRecord)
            .where(
                MemoryAssertionRecord.subject_entity_id == subject_entity_id,
                MemoryAssertionRecord.predicate == predicate,
                MemoryAssertionRecord.scope_key == scope_key,
                MemoryAssertionRecord.is_multi_valued.is_(False),
                MemoryAssertionRecord.lifecycle_state == "active",
            )
            .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
        ).all()
    )


def _delete_projection_rows(db: Session, *, assertion_id: str) -> None:
    db.execute(
        delete(MemoryEmbeddingProjectionRecord).where(
            MemoryEmbeddingProjectionRecord.assertion_id == assertion_id
        )
    )
    db.execute(
        delete(MemoryProjectionJobRecord).where(
            MemoryProjectionJobRecord.target_table == "memory_assertions",
            MemoryProjectionJobRecord.target_id == assertion_id,
        )
    )
    db.execute(
        delete(MemoryKeywordProjectionRecord).where(
            MemoryKeywordProjectionRecord.canonical_table == "memory_assertions",
            MemoryKeywordProjectionRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemoryEntityProjectionRecord).where(
            MemoryEntityProjectionRecord.canonical_table == "memory_assertions",
            MemoryEntityProjectionRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemorySalienceRecord).where(MemorySalienceRecord.assertion_id == assertion_id)
    )


def _record_projection_rows(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    subject_entity_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    _delete_projection_rows(db, assertion_id=assertion.id)
    search_text = _assertion_search_text(assertion)
    db.add(
        MemoryKeywordProjectionRecord(
            id=new_id_fn("mkp"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            projection_version=MEMORY_PROJECTION_VERSION,
            search_text=search_text,
            weighted_terms=_weighted_terms(search_text),
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryEntityProjectionRecord(
            id=new_id_fn("mep"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            entity_id=subject_entity_id,
            projection_version=MEMORY_PROJECTION_VERSION,
            mention_text=assertion.subject_key,
            features={"role": "subject"},
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryProjectionJobRecord(
            id=new_id_fn("mpj"),
            projection_kind="embedding",
            target_table="memory_assertions",
            target_id=assertion.id,
            lifecycle_state="pending",
            attempts=0,
            max_retries=3,
            error=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )


def process_memory_projection_job(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> bool:
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            job = db.scalar(
                select(MemoryProjectionJobRecord)
                .where(
                    MemoryProjectionJobRecord.projection_kind == "embedding",
                    MemoryProjectionJobRecord.lifecycle_state == "pending",
                    MemoryProjectionJobRecord.run_after <= now,
                )
                .order_by(
                    MemoryProjectionJobRecord.run_after.asc(),
                    MemoryProjectionJobRecord.created_at.asc(),
                    MemoryProjectionJobRecord.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return False

            job.lifecycle_state = "running"
            job.attempts += 1
            job.updated_at = now
            job_id = job.id
            assertion = db.get(MemoryAssertionRecord, job.target_id)
            if (
                job.target_table != "memory_assertions"
                or assertion is None
                or assertion.lifecycle_state != "active"
            ):
                job.lifecycle_state = "completed"
                job.error = None
                return True
            assertion_id = assertion.id
            search_text = _assertion_search_text(assertion)

    try:
        vector = embed_memory_text(search_text, settings=settings)
    except Exception as exc:
        with session_factory() as db:
            with db.begin():
                job = db.get(MemoryProjectionJobRecord, job_id)
                if job is not None:
                    now = now_fn()
                    job.lifecycle_state = (
                        "dead_letter" if job.attempts >= job.max_retries else "pending"
                    )
                    job.error = _clean_text(str(exc), max_chars=500)
                    job.run_after = (
                        now if job.lifecycle_state == "dead_letter" else now + timedelta(seconds=30)
                    )
                    job.updated_at = now
        return True

    with session_factory() as db:
        with db.begin():
            now = now_fn()
            job = db.get(MemoryProjectionJobRecord, job_id)
            assertion = db.get(MemoryAssertionRecord, assertion_id)
            if job is None:
                return True
            if assertion is None or assertion.lifecycle_state != "active":
                job.lifecycle_state = "completed"
                job.error = None
                job.updated_at = now
                return True

            search_text = _assertion_search_text(assertion)
            row = db.scalar(
                select(MemoryEmbeddingProjectionRecord)
                .where(
                    MemoryEmbeddingProjectionRecord.assertion_id == assertion.id,
                    MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                )
                .limit(1)
            )
            if row is None:
                db.add(
                    MemoryEmbeddingProjectionRecord(
                        id=new_id_fn("mep"),
                        assertion_id=assertion.id,
                        projection_version=MEMORY_PROJECTION_VERSION,
                        embedding_provider=settings.memory_embedding_provider,
                        embedding_model=settings.memory_embedding_model,
                        embedding_dimensions=settings.memory_embedding_dimensions,
                        search_text=search_text,
                        embedding=vector,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.embedding_provider = settings.memory_embedding_provider
                row.embedding_model = settings.memory_embedding_model
                row.embedding_dimensions = settings.memory_embedding_dimensions
                row.search_text = search_text
                row.embedding = vector
                row.updated_at = now

            job.lifecycle_state = "completed"
            job.error = None
            job.updated_at = now
    return True


def _record_salience(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    user_priority: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    score = 1.0 + assertion.confidence
    if assertion.assertion_type in {"commitment", "decision", "project_state"}:
        score += 2.0
    if user_priority == "pinned":
        score += 10.0
    if user_priority == "deprioritized":
        score = 0.1
    row = db.scalar(
        select(MemorySalienceRecord)
        .where(MemorySalienceRecord.assertion_id == assertion.id)
        .limit(1)
    )
    signals = {
        "assertion_type": assertion.assertion_type,
        "confidence": assertion.confidence,
        "user_priority": user_priority,
    }
    if row is None:
        db.add(
            MemorySalienceRecord(
                id=new_id_fn("msl"),
                assertion_id=assertion.id,
                user_priority=user_priority,
                score=score,
                signals=signals,
                created_at=now,
                updated_at=now,
            )
        )
        return
    row.user_priority = user_priority
    row.score = score
    row.signals = signals
    row.updated_at = now


def _record_project_snapshot(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    if assertion.assertion_type != "project_state":
        return
    text = _assertion_text(assertion)
    snapshot = ProjectStateSnapshotRecord(
        id=new_id_fn("pss"),
        project_key=assertion.subject_key.removeprefix("project:"),
        summary=text,
        state={
            "subject_key": assertion.subject_key,
            "predicate": assertion.predicate,
            "value": text,
        },
        source_assertion_ids=[assertion.id],
        source_episode_ids=[],
        source_evidence_ids=[],
        lifecycle_state="active",
        projection_version=MEMORY_PROJECTION_VERSION,
        created_at=now,
        updated_at=now,
    )
    db.add(snapshot)
    block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "project_state",
            MemoryContextBlockRecord.scope_key == assertion.subject_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="project_state",
                scope_key=assertion.subject_key,
                content=f"{assertion.subject_key}: {text}",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
        return
    block.content = f"{assertion.subject_key}: {text}"
    block.source_assertion_ids = [assertion.id]
    block.source_project_state_snapshot_ids = [snapshot.id]
    block.updated_at = now


def _record_procedure(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    evidence_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    if assertion.assertion_type != "procedure":
        return
    procedure_key = _memory_key(assertion.predicate)
    procedure = db.scalar(
        select(MemoryProcedureRecord)
        .where(
            MemoryProcedureRecord.procedure_key == procedure_key,
            MemoryProcedureRecord.scope_key == assertion.scope_key,
        )
        .limit(1)
    )
    if procedure is None:
        db.add(
            MemoryProcedureRecord(
                id=new_id_fn("mpr"),
                procedure_key=procedure_key,
                scope_key=assertion.scope_key,
                title=assertion.predicate,
                instruction=_assertion_text(assertion),
                lifecycle_state="active",
                review_state="approved",
                source_assertion_id=assertion.id,
                primary_evidence_id=evidence_id,
                valid_from=assertion.valid_from,
                valid_to=assertion.valid_to,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
        )
        return
    procedure.title = assertion.predicate
    procedure.instruction = _assertion_text(assertion)
    procedure.lifecycle_state = "active"
    procedure.review_state = "approved"
    procedure.source_assertion_id = assertion.id
    procedure.primary_evidence_id = evidence_id
    procedure.valid_from = assertion.valid_from
    procedure.valid_to = assertion.valid_to
    procedure.updated_at = now


def _create_assertion(
    db: Session,
    *,
    entity: MemoryEntityRecord,
    evidence: MemoryEvidenceRecord,
    subject_key: str,
    predicate: str,
    assertion_type: str,
    value: str,
    confidence: float,
    scope_key: str,
    is_multi_valued: bool,
    lifecycle_state: str,
    valid_from: datetime | None,
    valid_to: datetime | None,
    extraction_model: str | None,
    extraction_prompt_version: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryAssertionRecord:
    assertion = MemoryAssertionRecord(
        id=new_id_fn("mas"),
        subject_entity_id=entity.id,
        subject_key=subject_key,
        predicate=predicate,
        scope_key=scope_key,
        object_value={"text": _clean_text(value)},
        assertion_type=assertion_type,
        is_multi_valued=is_multi_valued,
        scope={"kind": "global", "key": scope_key},
        lifecycle_state=lifecycle_state,
        confidence=max(0.0, min(confidence, 1.0)),
        valid_from=valid_from,
        valid_to=valid_to,
        superseded_by_assertion_id=None,
        extraction_model=extraction_model,
        extraction_prompt_version=extraction_prompt_version,
        last_verified_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(assertion)
    db.flush()
    db.add(
        MemoryAssertionEvidenceRecord(
            id=new_id_fn("mae"),
            assertion_id=assertion.id,
            evidence_id=evidence.id,
            created_at=now,
        )
    )
    return assertion


def _open_conflict(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    active_assertions: Sequence[MemoryAssertionRecord],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    conflict = db.scalar(
        select(MemoryConflictSetRecord)
        .where(
            MemoryConflictSetRecord.subject_entity_id == assertion.subject_entity_id,
            MemoryConflictSetRecord.predicate == assertion.predicate,
            MemoryConflictSetRecord.scope_key == assertion.scope_key,
            MemoryConflictSetRecord.lifecycle_state == "open",
        )
        .limit(1)
    )
    if conflict is None:
        conflict = MemoryConflictSetRecord(
            id=new_id_fn("mcf"),
            subject_entity_id=assertion.subject_entity_id,
            predicate=assertion.predicate,
            scope_key=assertion.scope_key,
            lifecycle_state="open",
            resolution_assertion_id=None,
            reason="candidate contradicts active memory",
            created_at=now,
            updated_at=now,
        )
        db.add(conflict)
        db.flush()

    assertion.lifecycle_state = "conflicted"
    assertion.updated_at = now
    for member in [assertion, *active_assertions]:
        exists = db.scalar(
            select(MemoryConflictMemberRecord)
            .where(
                MemoryConflictMemberRecord.conflict_set_id == conflict.id,
                MemoryConflictMemberRecord.assertion_id == member.id,
            )
            .limit(1)
        )
        if exists is None:
            db.add(
                MemoryConflictMemberRecord(
                    id=new_id_fn("mcm"),
                    conflict_set_id=conflict.id,
                    assertion_id=member.id,
                    created_at=now,
                )
            )

    return {
        "event_type": "evt.memory.conflict_opened",
        "payload": {
            "conflict_set_id": conflict.id,
            "subject_key": assertion.subject_key,
            "predicate": assertion.predicate,
            "assertion_ids": [assertion.id, *[item.id for item in active_assertions]],
        },
    }


def _activate_assertion(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    actor_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not assertion.is_multi_valued:
        for existing in _active_single_assertions(
            db,
            subject_entity_id=assertion.subject_entity_id,
            predicate=assertion.predicate,
            scope_key=assertion.scope_key,
        ):
            if existing.id == assertion.id:
                continue
            existing.lifecycle_state = "superseded"
            existing.superseded_by_assertion_id = assertion.id
            existing.valid_to = now
            existing.updated_at = now
            _delete_projection_rows(db, assertion_id=existing.id)
            _record_version(
                db,
                table="memory_assertions",
                record_id=existing.id,
                change_type="superseded",
                actor_id=actor_id,
                reason="single-valued assertion replaced",
                new_state={"superseded_by_assertion_id": assertion.id},
                now=now,
                new_id_fn=new_id_fn,
            )
            events.append(
                {
                    "event_type": "evt.memory.assertion_superseded",
                    "payload": _event_payload(existing),
                }
            )

    evidence_id = db.scalar(
        select(MemoryAssertionEvidenceRecord.evidence_id)
        .where(MemoryAssertionEvidenceRecord.assertion_id == assertion.id)
        .order_by(MemoryAssertionEvidenceRecord.created_at.asc())
        .limit(1)
    )
    assertion.lifecycle_state = "active"
    assertion.valid_from = assertion.valid_from or now
    assertion.last_verified_at = now
    assertion.updated_at = now
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="approved",
        actor_id=actor_id,
        reason="reviewed memory activated",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_projection_rows(
        db,
        assertion=assertion,
        subject_entity_id=assertion.subject_entity_id,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_salience(
        db,
        assertion=assertion,
        user_priority="none",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_project_snapshot(db, assertion=assertion, now=now, new_id_fn=new_id_fn)
    if evidence_id is not None:
        _record_procedure(
            db,
            assertion=assertion,
            evidence_id=evidence_id,
            now=now,
            new_id_fn=new_id_fn,
        )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="reviewed",
        actor_id=actor_id,
        reason="activated",
        new_state={"lifecycle_state": "active"},
        now=now,
        new_id_fn=new_id_fn,
    )
    events.append(
        {"event_type": "evt.memory.assertion_activated", "payload": _event_payload(assertion)}
    )
    events.append(
        {
            "event_type": "evt.memory.projection_rebuilt",
            "payload": {
                "assertion_id": assertion.id,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "projection_kinds": ["keyword", "entity", "salience"],
                "queued_projection_kinds": ["embedding"],
            },
        }
    )
    return events


def record_turn_memory_evidence(
    db: Session,
    *,
    session_id: str,
    source_turn_id: str,
    user_message: str,
    assistant_message: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> tuple[list[dict[str, Any]], str]:
    now = now_fn()
    user_evidence = _record_evidence(
        db,
        session_id=session_id,
        turn_id=source_turn_id,
        actor_id=actor_id,
        content_class="user_message",
        trust_boundary="trusted_user",
        source_text=user_message,
        source_uri=None,
        metadata={"capture_mode": "turn_evidence"},
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {
            "event_type": "evt.memory.evidence_recorded",
            "payload": {
                "evidence_id": user_evidence.id,
                "source_turn_id": source_turn_id,
                "source_session_id": session_id,
                "content_class": user_evidence.content_class,
                "trust_boundary": user_evidence.trust_boundary,
            },
        }
    ]
    if assistant_message:
        assistant_evidence = _record_evidence(
            db,
            session_id=session_id,
            turn_id=source_turn_id,
            actor_id="assistant",
            content_class="assistant_message",
            trust_boundary="assistant",
            source_text=assistant_message,
            source_uri=None,
            metadata={"capture_mode": "turn_evidence"},
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {
                "event_type": "evt.memory.evidence_recorded",
                "payload": {
                    "evidence_id": assistant_evidence.id,
                    "source_turn_id": source_turn_id,
                    "source_session_id": session_id,
                    "content_class": assistant_evidence.content_class,
                    "trust_boundary": assistant_evidence.trust_boundary,
                },
            }
        )
    db.add(
        MemoryEpisodeRecord(
            id=new_id_fn("mep"),
            episode_type="task_event",
            scope_key=f"session:{session_id}",
            title=_clean_text(user_message, max_chars=160),
            summary=_clean_text(user_message, max_chars=700),
            outcome=_clean_text(assistant_message, max_chars=700) if assistant_message else None,
            occurred_at=now,
            valid_from=now,
            valid_to=None,
            lifecycle_state="active",
            primary_evidence_id=user_evidence.id,
            related_entity_ids=[],
            related_assertion_ids=[],
            metadata_json={"turn_id": source_turn_id},
            created_at=now,
            updated_at=now,
        )
    )
    return events, user_evidence.id


def propose_memory_candidate(
    db: Session,
    *,
    source_session_id: str,
    actor_id: str,
    evidence_text: str,
    subject_key: str,
    predicate: str,
    assertion_type: str,
    value: str,
    confidence: float,
    scope_key: str,
    is_multi_valued: bool,
    valid_from: datetime | None,
    valid_to: datetime | None,
    extraction_model: str | None,
    extraction_prompt_version: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    now = now_fn()
    evidence = _record_evidence(
        db,
        session_id=source_session_id,
        turn_id=None,
        actor_id=actor_id,
        content_class="system",
        trust_boundary="system" if actor_id == "system" else "trusted_user",
        source_text=evidence_text,
        source_uri=None,
        metadata={"capture_mode": "candidate_proposal"},
        now=now,
        new_id_fn=new_id_fn,
    )
    entity = _ensure_entity(
        db,
        subject_key=subject_key,
        assertion_type=assertion_type,
        now=now,
        new_id_fn=new_id_fn,
    )
    assertion = _create_assertion(
        db,
        entity=entity,
        evidence=evidence,
        subject_key=subject_key,
        predicate=predicate,
        assertion_type=assertion_type,
        value=value,
        confidence=confidence,
        scope_key=scope_key,
        is_multi_valued=is_multi_valued,
        lifecycle_state="candidate",
        valid_from=valid_from,
        valid_to=valid_to,
        extraction_model=extraction_model,
        extraction_prompt_version=extraction_prompt_version,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="needs_user_review",
        actor_id="system",
        reason="candidate memory requires review",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="created",
        actor_id=actor_id,
        reason="candidate proposed",
        new_state={"lifecycle_state": assertion.lifecycle_state},
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {
            "event_type": "evt.memory.evidence_recorded",
            "payload": {
                "evidence_id": evidence.id,
                "source_turn_id": None,
                "source_session_id": source_session_id,
                "content_class": evidence.content_class,
                "trust_boundary": evidence.trust_boundary,
            },
        },
        {
            "event_type": "evt.memory.candidate_proposed",
            "payload": _event_payload(assertion, evidence_id=evidence.id),
        },
        {"event_type": "evt.memory.review_required", "payload": _event_payload(assertion)},
    ]
    active_assertions = _active_single_assertions(
        db,
        subject_entity_id=entity.id,
        predicate=assertion.predicate,
        scope_key=assertion.scope_key,
    )
    if active_assertions and not is_multi_valued:
        events.append(
            _open_conflict(
                db,
                assertion=assertion,
                active_assertions=active_assertions,
                now=now,
                new_id_fn=new_id_fn,
            )
        )
    return events


def approve_candidate(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}:
        return []
    now = now_fn()
    events = [{"event_type": "evt.memory.candidate_approved", "payload": _event_payload(assertion)}]
    events.extend(
        _activate_assertion(
            db,
            assertion=assertion,
            actor_id=actor_id,
            now=now,
            new_id_fn=new_id_fn,
        )
    )
    return events


def reject_candidate(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}:
        return []
    now = now_fn()
    assertion.lifecycle_state = "rejected"
    assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=assertion.id)
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="rejected",
        actor_id=actor_id,
        reason=_clean_text(reason) if reason else "candidate rejected",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="reviewed",
        actor_id=actor_id,
        reason="candidate rejected",
        new_state={"lifecycle_state": "rejected"},
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.candidate_rejected", "payload": _event_payload(assertion)}]


def correct_assertion(
    db: Session,
    *,
    assertion_id: str,
    value: str,
    source_session_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    old_assertion = db.get(MemoryAssertionRecord, assertion_id)
    if old_assertion is None:
        return []
    entity = db.get(MemoryEntityRecord, old_assertion.subject_entity_id)
    if entity is None:
        return []
    now = now_fn()
    evidence = _record_evidence(
        db,
        session_id=source_session_id,
        turn_id=None,
        actor_id=actor_id,
        content_class="system",
        trust_boundary="trusted_user",
        source_text=value,
        source_uri=None,
        metadata={"capture_mode": "manual_correction"},
        now=now,
        new_id_fn=new_id_fn,
    )
    new_assertion = _create_assertion(
        db,
        entity=entity,
        evidence=evidence,
        subject_key=old_assertion.subject_key,
        predicate=old_assertion.predicate,
        assertion_type=old_assertion.assertion_type,
        value=value,
        confidence=1.0,
        scope_key=old_assertion.scope_key,
        is_multi_valued=old_assertion.is_multi_valued,
        lifecycle_state="candidate",
        valid_from=now,
        valid_to=None,
        extraction_model=None,
        extraction_prompt_version=None,
        now=now,
        new_id_fn=new_id_fn,
    )
    old_assertion.lifecycle_state = "superseded"
    old_assertion.superseded_by_assertion_id = new_assertion.id
    old_assertion.valid_to = now
    old_assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=old_assertion.id)
    events = [
        {"event_type": "evt.memory.evidence_recorded", "payload": {"evidence_id": evidence.id}},
        {"event_type": "evt.memory.assertion_superseded", "payload": _event_payload(old_assertion)},
    ]
    events.extend(
        _activate_assertion(
            db,
            assertion=new_assertion,
            actor_id=actor_id,
            now=now,
            new_id_fn=new_id_fn,
        )
    )
    return events


def retract_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None:
        return []
    now = now_fn()
    assertion.lifecycle_state = "retracted"
    assertion.valid_to = now
    assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=assertion.id)
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="retracted",
        actor_id=actor_id,
        reason="assertion retracted",
        new_state={"lifecycle_state": "retracted"},
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.assertion_retracted", "payload": _event_payload(assertion)}]


def delete_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None:
        return []
    now = now_fn()
    assertion.lifecycle_state = "deleted"
    assertion.valid_to = now
    assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=assertion.id)
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="deleted",
        actor_id=actor_id,
        reason="assertion deleted",
        new_state={"lifecycle_state": "deleted"},
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.assertion_deleted", "payload": _event_payload(assertion)}]


def set_assertion_priority(
    db: Session,
    *,
    assertion_id: str,
    priority: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state != "active":
        return None
    _record_salience(
        db,
        assertion=assertion,
        user_priority=priority,
        now=now_fn(),
        new_id_fn=new_id_fn,
    )
    return serialize_assertion(assertion)


def resolve_conflict(
    db: Session,
    *,
    conflict_set_id: str,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    conflict = db.get(MemoryConflictSetRecord, conflict_set_id)
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if conflict is None or assertion is None or conflict.lifecycle_state != "open":
        return []
    now = now_fn()
    conflict.lifecycle_state = "resolved"
    conflict.resolution_assertion_id = assertion.id
    conflict.updated_at = now
    events = _activate_assertion(
        db,
        assertion=assertion,
        actor_id=actor_id,
        now=now,
        new_id_fn=new_id_fn,
    )
    events.append(
        {
            "event_type": "evt.memory.conflict_resolved",
            "payload": {
                "conflict_set_id": conflict.id,
                "resolution_assertion_id": assertion.id,
            },
        }
    )
    return events


def create_relationship(
    db: Session,
    *,
    source_entity_id: str,
    target_entity_id: str,
    relationship_type: str,
    evidence_id: str,
    scope_key: str,
    confidence: float,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    source = db.get(MemoryEntityRecord, source_entity_id)
    target = db.get(MemoryEntityRecord, target_entity_id)
    evidence = db.get(MemoryEvidenceRecord, evidence_id)
    if source is None or target is None or evidence is None:
        return None
    now = now_fn()
    relationship = MemoryRelationshipRecord(
        id=new_id_fn("mrl"),
        source_entity_id=source.id,
        target_entity_id=target.id,
        relationship_type=_memory_key(relationship_type),
        scope_key=scope_key,
        lifecycle_state="active",
        confidence=max(0.0, min(confidence, 1.0)),
        valid_from=now,
        valid_to=None,
        evidence_id=evidence.id,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(relationship)
    db.flush()
    db.add(
        MemoryGraphProjectionRecord(
            id=new_id_fn("mgp"),
            source_entity_id=source.id,
            target_entity_id=target.id,
            projection_version=MEMORY_PROJECTION_VERSION,
            relationship_path=[
                {
                    "relationship_id": relationship.id,
                    "relationship_type": relationship.relationship_type,
                }
            ],
            distance=1,
            score=relationship.confidence,
            created_at=now,
            updated_at=now,
        )
    )
    _record_version(
        db,
        table="memory_relationships",
        record_id=relationship.id,
        change_type="created",
        actor_id=actor_id,
        reason="relationship created",
        new_state={"lifecycle_state": "active"},
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "relationship_id": relationship.id,
        "source_entity_id": source.id,
        "target_entity_id": target.id,
        "relationship_type": relationship.relationship_type,
    }


def record_rotation_context_block(
    db: Session,
    *,
    prior_session_id: str,
    new_session_id: str,
    rotation_reason: str,
    prior_turns: Sequence[TurnRecord],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    now = now_fn()
    snippets = []
    for turn in prior_turns[-3:]:
        assistant = _clean_text(turn.assistant_message or "", max_chars=260)
        user = _clean_text(turn.user_message, max_chars=260)
        snippets.append(f"user={user}; assistant={assistant}" if assistant else user)
    summary = _clean_text(" | ".join(snippets), max_chars=1200) or "session rotated"
    db.add(
        ProjectStateSnapshotRecord(
            id=new_id_fn("pss"),
            project_key="session_continuity",
            summary=summary,
            state={
                "rotation_reason": rotation_reason,
                "prior_session_id": prior_session_id,
                "new_session_id": new_session_id,
            },
            source_assertion_ids=[],
            source_episode_ids=[],
            source_evidence_ids=[],
            lifecycle_state="active",
            projection_version=MEMORY_PROJECTION_VERSION,
            created_at=now,
            updated_at=now,
        )
    )


def _evidence_refs_by_assertion(
    db: Session,
    assertion_ids: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    if not assertion_ids:
        return {}
    rows = db.execute(
        select(MemoryAssertionEvidenceRecord.assertion_id, MemoryEvidenceRecord)
        .join(
            MemoryEvidenceRecord,
            MemoryEvidenceRecord.id == MemoryAssertionEvidenceRecord.evidence_id,
        )
        .where(MemoryAssertionEvidenceRecord.assertion_id.in_(assertion_ids))
        .order_by(
            MemoryAssertionEvidenceRecord.assertion_id.asc(),
            MemoryAssertionEvidenceRecord.created_at.asc(),
            MemoryAssertionEvidenceRecord.id.asc(),
        )
    ).all()
    result: dict[str, list[dict[str, Any]]] = {assertion_id: [] for assertion_id in assertion_ids}
    for assertion_id, evidence in rows:
        result.setdefault(assertion_id, []).append(
            {
                "evidence_id": evidence.id,
                "snippet": evidence.evidence_snippet
                or redact_text(_clean_text(evidence.source_text)),
                "source_turn_id": evidence.source_turn_id,
                "source_session_id": evidence.source_session_id,
                "content_class": evidence.content_class,
                "trust_boundary": evidence.trust_boundary,
                "created_at": to_rfc3339(evidence.created_at),
            }
        )
    return result


def serialize_assertion(
    assertion: MemoryAssertionRecord,
    *,
    evidence_refs: Sequence[dict[str, Any]] = (),
    rank_reason: str | None = None,
    rank_score: float | None = None,
) -> dict[str, Any]:
    payload = {
        "id": assertion.id,
        "subject_key": assertion.subject_key,
        "predicate": assertion.predicate,
        "type": assertion.assertion_type,
        "state": assertion.lifecycle_state,
        "value": redact_text(_assertion_text(assertion)),
        "confidence": assertion.confidence,
        "scope_key": assertion.scope_key,
        "is_multi_valued": assertion.is_multi_valued,
        "valid_from": to_rfc3339(assertion.valid_from) if assertion.valid_from else None,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
        "last_verified_at": to_rfc3339(assertion.last_verified_at),
        "created_at": to_rfc3339(assertion.created_at),
        "updated_at": to_rfc3339(assertion.updated_at),
        "superseded_by_id": assertion.superseded_by_assertion_id,
        "evidence_refs": list(evidence_refs),
        "projection_version": MEMORY_PROJECTION_VERSION,
    }
    if rank_reason is not None:
        payload["rank_reason"] = rank_reason
    if rank_score is not None:
        payload["rank_score"] = round(rank_score, 4)
    return payload


def _serialize_conflict(conflict: MemoryConflictSetRecord) -> dict[str, Any]:
    return {
        "id": conflict.id,
        "subject_entity_id": conflict.subject_entity_id,
        "predicate": conflict.predicate,
        "scope_key": conflict.scope_key,
        "state": conflict.lifecycle_state,
        "resolution_assertion_id": conflict.resolution_assertion_id,
        "reason": conflict.reason,
        "created_at": to_rfc3339(conflict.created_at),
        "updated_at": to_rfc3339(conflict.updated_at),
    }


def list_memory(db: Session) -> dict[str, Any]:
    assertions = db.scalars(
        select(MemoryAssertionRecord).order_by(
            MemoryAssertionRecord.updated_at.desc(),
            MemoryAssertionRecord.id.asc(),
        )
    ).all()
    evidence_refs = _evidence_refs_by_assertion(db, [assertion.id for assertion in assertions])
    conflicts = db.scalars(
        select(MemoryConflictSetRecord).order_by(
            MemoryConflictSetRecord.updated_at.desc(),
            MemoryConflictSetRecord.id.asc(),
        )
    ).all()
    evidence_rows = db.scalars(
        select(MemoryEvidenceRecord)
        .order_by(MemoryEvidenceRecord.created_at.desc(), MemoryEvidenceRecord.id.desc())
        .limit(50)
    ).all()
    procedures = db.scalars(
        select(MemoryProcedureRecord)
        .where(MemoryProcedureRecord.lifecycle_state == "active")
        .order_by(MemoryProcedureRecord.updated_at.desc(), MemoryProcedureRecord.id.asc())
        .limit(50)
    ).all()
    project_state = db.scalars(
        select(ProjectStateSnapshotRecord)
        .where(ProjectStateSnapshotRecord.lifecycle_state == "active")
        .order_by(
            ProjectStateSnapshotRecord.updated_at.desc(),
            ProjectStateSnapshotRecord.id.desc(),
        )
        .limit(50)
    ).all()
    return {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "active_assertions": [
            serialize_assertion(assertion, evidence_refs=evidence_refs.get(assertion.id, []))
            for assertion in assertions
            if assertion.lifecycle_state == "active"
        ],
        "candidates": [
            serialize_assertion(assertion, evidence_refs=evidence_refs.get(assertion.id, []))
            for assertion in assertions
            if assertion.lifecycle_state in {"candidate", "conflicted"}
        ],
        "conflicts": [_serialize_conflict(conflict) for conflict in conflicts],
        "project_state": [
            {
                "id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "state": snapshot.state,
                "source_assertion_ids": snapshot.source_assertion_ids,
                "source_evidence_ids": snapshot.source_evidence_ids,
                "created_at": to_rfc3339(snapshot.created_at),
                "updated_at": to_rfc3339(snapshot.updated_at),
            }
            for snapshot in project_state
        ],
        "evidence": [
            {
                "id": evidence.id,
                "source_turn_id": evidence.source_turn_id,
                "source_session_id": evidence.source_session_id,
                "content_class": evidence.content_class,
                "trust_boundary": evidence.trust_boundary,
                "state": evidence.lifecycle_state,
                "snippet": evidence.evidence_snippet
                or redact_text(_clean_text(evidence.source_text)),
                "created_at": to_rfc3339(evidence.created_at),
            }
            for evidence in evidence_rows
        ],
        "procedures": [
            {
                "id": procedure.id,
                "procedure_key": procedure.procedure_key,
                "scope_key": procedure.scope_key,
                "title": procedure.title,
                "instruction": redact_text(procedure.instruction),
                "state": procedure.lifecycle_state,
                "review_state": procedure.review_state,
                "source_assertion_id": procedure.source_assertion_id,
                "created_at": to_rfc3339(procedure.created_at),
                "updated_at": to_rfc3339(procedure.updated_at),
            }
            for procedure in procedures
        ],
        "projection_health": {
            "projection_version": MEMORY_PROJECTION_VERSION,
            "pending_jobs": db.scalar(
                select(func.count())
                .select_from(MemoryProjectionJobRecord)
                .where(MemoryProjectionJobRecord.lifecycle_state == "pending")
            )
            or 0,
            "failed_jobs": db.scalar(
                select(func.count())
                .select_from(MemoryProjectionJobRecord)
                .where(MemoryProjectionJobRecord.lifecycle_state.in_(("failed", "dead_letter")))
            )
            or 0,
        },
    }


def search_memory(
    db: Session,
    *,
    query: str,
    limit: int,
    settings: AppSettings | None = None,
) -> list[dict[str, Any]]:
    memory_context, _ = build_memory_context(
        db,
        user_message=query,
        max_recalled_assertions=limit,
        settings=settings,
    )
    return list(memory_context["semantic_assertions"])


def build_memory_context(
    db: Session,
    *,
    user_message: str,
    max_recalled_assertions: int,
    settings: AppSettings | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    query_terms = set(_terms(user_message))
    entity_rows = db.scalars(select(MemoryEntityRecord)).all()
    matching_entity_ids = {
        entity.id
        for entity in entity_rows
        if query_terms.intersection(set(_terms(f"{entity.entity_key} {entity.display_name}")))
    }
    graph_neighbors = {
        row.target_entity_id
        for row in db.scalars(
            select(MemoryRelationshipRecord).where(
                MemoryRelationshipRecord.lifecycle_state == "active",
                MemoryRelationshipRecord.source_entity_id.in_(matching_entity_ids or {""}),
            )
        ).all()
    } | {
        row.source_entity_id
        for row in db.scalars(
            select(MemoryRelationshipRecord).where(
                MemoryRelationshipRecord.lifecycle_state == "active",
                MemoryRelationshipRecord.target_entity_id.in_(matching_entity_ids or {""}),
            )
        ).all()
    }

    active_assertions: Sequence[MemoryAssertionRecord] = ()
    ranked: list[tuple[float, str, MemoryAssertionRecord, str]] = []
    resolved_settings = settings or AppSettings()
    embedding_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryEmbeddingProjectionRecord)
            .where(
                MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                MemoryEmbeddingProjectionRecord.embedding_provider
                == resolved_settings.memory_embedding_provider,
                MemoryEmbeddingProjectionRecord.embedding_model
                == resolved_settings.memory_embedding_model,
                MemoryEmbeddingProjectionRecord.embedding_dimensions
                == resolved_settings.memory_embedding_dimensions,
            )
        )
        or 0
    )
    if embedding_count:
        query_vector = embed_memory_text(user_message, settings=resolved_settings)
        vector_distance = MemoryEmbeddingProjectionRecord.embedding.cosine_distance(query_vector)
        vector_rows = db.execute(
            select(
                MemoryEmbeddingProjectionRecord.assertion_id,
                vector_distance.label("distance"),
            )
            .where(
                MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                MemoryEmbeddingProjectionRecord.embedding_provider
                == resolved_settings.memory_embedding_provider,
                MemoryEmbeddingProjectionRecord.embedding_model
                == resolved_settings.memory_embedding_model,
                MemoryEmbeddingProjectionRecord.embedding_dimensions
                == resolved_settings.memory_embedding_dimensions,
            )
            .order_by(vector_distance.asc(), MemoryEmbeddingProjectionRecord.assertion_id.asc())
            .limit(max(50, max_recalled_assertions * 8))
        ).all()
        semantic_scores = {
            assertion_id: max(0.0, 1.0 - float(distance))
            for assertion_id, distance in vector_rows
            if distance is not None
        }
        keywords = {
            row.canonical_id: row
            for row in db.scalars(
                select(MemoryKeywordProjectionRecord).where(
                    MemoryKeywordProjectionRecord.canonical_table == "memory_assertions",
                    MemoryKeywordProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                )
            ).all()
        }
        keyword_candidate_ids = {
            assertion_id
            for assertion_id, keyword in keywords.items()
            if query_terms.intersection(set(keyword.weighted_terms))
        }
        entity_candidate_ids = {
            row.canonical_id
            for row in db.scalars(
                select(MemoryEntityProjectionRecord).where(
                    MemoryEntityProjectionRecord.canonical_table == "memory_assertions",
                    MemoryEntityProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                    MemoryEntityProjectionRecord.entity_id.in_(
                        matching_entity_ids | graph_neighbors or {""}
                    ),
                )
            ).all()
        }
        missing_score_ids = (keyword_candidate_ids | entity_candidate_ids) - set(semantic_scores)
        if missing_score_ids:
            distance_rows = db.execute(
                select(
                    MemoryEmbeddingProjectionRecord.assertion_id,
                    vector_distance.label("distance"),
                ).where(
                    MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                    MemoryEmbeddingProjectionRecord.embedding_provider
                    == resolved_settings.memory_embedding_provider,
                    MemoryEmbeddingProjectionRecord.embedding_model
                    == resolved_settings.memory_embedding_model,
                    MemoryEmbeddingProjectionRecord.embedding_dimensions
                    == resolved_settings.memory_embedding_dimensions,
                    MemoryEmbeddingProjectionRecord.assertion_id.in_(missing_score_ids),
                )
            ).all()
            for assertion_id, distance in distance_rows:
                if distance is not None:
                    semantic_scores[assertion_id] = max(0.0, 1.0 - float(distance))

        candidate_ids = set(semantic_scores)
        if candidate_ids:
            active_assertions = db.scalars(
                select(MemoryAssertionRecord)
                .where(
                    MemoryAssertionRecord.lifecycle_state == "active",
                    MemoryAssertionRecord.id.in_(candidate_ids),
                )
                .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
            ).all()
            salience = {
                row.assertion_id: row
                for row in db.scalars(
                    select(MemorySalienceRecord).where(
                        MemorySalienceRecord.assertion_id.in_(candidate_ids)
                    )
                ).all()
            }
            for assertion in active_assertions:
                reasons: list[str] = []
                score = assertion.confidence
                semantic_score = semantic_scores.get(assertion.id, 0.0)
                if semantic_score > 0.0:
                    score += semantic_score * 6.0
                    reasons.append("semantic_vector")

                keyword = keywords.get(assertion.id)
                if keyword is not None:
                    keyword_score = sum(
                        float(keyword.weighted_terms.get(term, 0.0)) for term in query_terms
                    )
                    if keyword_score > 0.0:
                        score += keyword_score * 1.5
                        reasons.append("keyword")

                if assertion.subject_entity_id in matching_entity_ids:
                    score += 3.0
                    reasons.append("entity")
                if assertion.subject_entity_id in graph_neighbors:
                    score += 2.0
                    reasons.append("graph")
                if assertion.assertion_type in {"commitment", "decision"}:
                    score += 2.0
                    reasons.append("commitment_or_decision")
                if (
                    assertion.assertion_type == "project_state"
                    and "project" in user_message.lower()
                ):
                    score += 2.0
                    reasons.append("project_state")
                salience_row = salience.get(assertion.id)
                if salience_row is not None:
                    score += salience_row.score
                    if salience_row.user_priority == "pinned":
                        reasons.append("pinned")
                    if salience_row.user_priority == "deprioritized":
                        score -= 100.0
                        reasons.append("deprioritized")
                if score <= 1.0 and not reasons:
                    continue
                ranked.append((score, assertion.id, assertion, "+".join(reasons) or "salience"))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    selected = ranked[:max_recalled_assertions]
    omitted = ranked[max_recalled_assertions:]
    selected_ids = [assertion.id for _, _, assertion, _ in selected]
    assertion_ids = [assertion.id for assertion in active_assertions]
    evidence_refs = _evidence_refs_by_assertion(db, selected_ids + assertion_ids)
    semantic_assertions = [
        serialize_assertion(
            assertion,
            evidence_refs=evidence_refs.get(assertion.id, []),
            rank_reason=reason,
            rank_score=score,
        )
        for score, _, assertion, reason in selected
    ]
    conflicts = db.scalars(
        select(MemoryConflictSetRecord)
        .where(MemoryConflictSetRecord.lifecycle_state == "open")
        .order_by(MemoryConflictSetRecord.updated_at.desc(), MemoryConflictSetRecord.id.asc())
    ).all()
    project_snapshots = db.scalars(
        select(ProjectStateSnapshotRecord)
        .where(ProjectStateSnapshotRecord.lifecycle_state == "active")
        .order_by(
            ProjectStateSnapshotRecord.updated_at.desc(),
            ProjectStateSnapshotRecord.id.desc(),
        )
        .limit(8)
    ).all()
    procedures = db.scalars(
        select(MemoryProcedureRecord)
        .where(
            MemoryProcedureRecord.lifecycle_state == "active",
            MemoryProcedureRecord.review_state.in_(("approved", "auto_approved")),
        )
        .order_by(MemoryProcedureRecord.updated_at.desc(), MemoryProcedureRecord.id.asc())
        .limit(8)
    ).all()
    reasoning_traces = db.scalars(
        select(MemoryEpisodeRecord)
        .where(MemoryEpisodeRecord.lifecycle_state == "active")
        .order_by(MemoryEpisodeRecord.occurred_at.desc(), MemoryEpisodeRecord.id.asc())
        .limit(6)
    ).all()
    recall_window: dict[str, Any] = {
        "max_recalled_items": max_recalled_assertions,
        "included_memory_count": len(semantic_assertions),
        "omitted_memory_count": len(omitted),
        "included_memory_ids": [item["id"] for item in semantic_assertions],
        "omitted_memories": [
            {"id": assertion.id, "kind": "semantic_assertion", "reason": "top_k_bounded"}
            for _, _, assertion, _ in omitted
        ],
    }
    context = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "projection_version": MEMORY_PROJECTION_VERSION,
        "pinned_core": [
            item
            for item in semantic_assertions
            if item["type"] in {"profile", "preference"}
            and "pinned" in str(item.get("rank_reason"))
        ],
        "project_state": [
            {
                "id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "source_assertion_ids": snapshot.source_assertion_ids,
                "source_evidence_ids": snapshot.source_evidence_ids,
            }
            for snapshot in project_snapshots
        ],
        "commitments_and_decisions": [
            serialize_assertion(assertion, evidence_refs=evidence_refs.get(assertion.id, []))
            for assertion in active_assertions
            if assertion.assertion_type in {"commitment", "decision"}
        ][:12],
        "semantic_assertions": semantic_assertions,
        "episodic_evidence": [
            {
                "id": episode.id,
                "type": episode.episode_type,
                "scope_key": episode.scope_key,
                "summary": redact_text(episode.summary),
                "outcome": redact_text(episode.outcome) if episode.outcome else None,
                "primary_evidence_id": episode.primary_evidence_id,
                "occurred_at": to_rfc3339(episode.occurred_at),
            }
            for episode in reasoning_traces
        ],
        "procedural_memory": [
            {
                "id": procedure.id,
                "procedure_key": procedure.procedure_key,
                "scope_key": procedure.scope_key,
                "instruction": redact_text(procedure.instruction),
                "source_assertion_id": procedure.source_assertion_id,
            }
            for procedure in procedures
        ],
        "conflicts": [_serialize_conflict(conflict) for conflict in conflicts],
        "recall_window": recall_window,
        "projection_health": {
            "projection_version": MEMORY_PROJECTION_VERSION,
            "selected_assertion_count": len(selected),
        },
    }
    event_payload = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "projection_version": MEMORY_PROJECTION_VERSION,
        **recall_window,
        "conflict_ids": [conflict.id for conflict in conflicts],
    }
    return context, event_payload


def context_text(memory_context: dict[str, Any]) -> str:
    lines = ["memory context:"]
    for item in memory_context.get("project_state", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- project: " + item["summary"])
    for item in memory_context.get("commitments_and_decisions", []):
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            lines.append("- commitment/decision: " + item["value"])
    for item in memory_context.get("semantic_assertions", []):
        if not isinstance(item, dict):
            continue
        memory_type = item.get("type")
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        value = item.get("value")
        if all(isinstance(part, str) for part in (memory_type, subject_key, predicate, value)):
            lines.append(f"- {memory_type}: {subject_key} {predicate} = {value}")
            evidence_refs = item.get("evidence_refs")
            if isinstance(evidence_refs, list) and evidence_refs:
                first_ref = evidence_refs[0]
                if isinstance(first_ref, dict) and isinstance(first_ref.get("snippet"), str):
                    lines.append(f"  evidence: {first_ref['snippet']}")
    for item in memory_context.get("episodic_evidence", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- episode: " + item["summary"])
    for item in memory_context.get("procedural_memory", []):
        if isinstance(item, dict) and isinstance(item.get("instruction"), str):
            lines.append("- procedure: " + item["instruction"])
    conflicts = memory_context.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        lines.append("- unresolved memory conflicts exist; state uncertainty when relevant")
    return "\n".join(lines)


def _extract_output_text(output_items: Any) -> str:
    if not isinstance(output_items, list):
        return ""
    parts: list[str] = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if isinstance(content_item, dict) and content_item.get("type") == "output_text":
                text = content_item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()


def process_memory_extract_turn(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    if not settings.openai_api_key:
        raise RuntimeError("memory extraction requires ARIEL_OPENAI_API_KEY")
    evidence_id = task_payload.get("evidence_id")
    session_id = task_payload.get("session_id")
    if not isinstance(evidence_id, str) or not isinstance(session_id, str):
        raise RuntimeError("memory extraction task payload is malformed")

    with session_factory() as db:
        with db.begin():
            evidence = db.get(MemoryEvidenceRecord, evidence_id)
            if evidence is None or evidence.lifecycle_state != "available":
                return
            source_text = evidence.source_text

    prompt = (
        "Extract durable Ariel memory candidates from the evidence. "
        "Return JSON only with a top-level candidates array. Each candidate must have "
        "subject_key, predicate, assertion_type, value, confidence, is_multi_valued. "
        "Use assertion_type values fact, profile, preference, commitment, decision, "
        "project_state, procedure, or domain_concept. Return an empty array when the "
        "evidence has no durable memory."
    )
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={
            "authorization": f"Bearer {settings.openai_api_key}",
            "content-type": "application/json",
        },
        json={
            "model": settings.model_name,
            "input": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": source_text},
            ],
            "store": False,
            "text": {"verbosity": "low"},
        },
        timeout=settings.model_timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"memory extraction model returned HTTP {response.status_code}")
    text = _extract_output_text(response.json().get("output"))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("memory extraction model returned malformed JSON") from exc
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise RuntimeError("memory extraction JSON missing candidates array")

    with session_factory() as db:
        with db.begin():
            for raw_candidate in candidates[:8]:
                if not isinstance(raw_candidate, dict):
                    continue
                subject_key = raw_candidate.get("subject_key")
                predicate = raw_candidate.get("predicate")
                assertion_type = raw_candidate.get("assertion_type")
                value = raw_candidate.get("value")
                confidence = raw_candidate.get("confidence")
                is_multi_valued = raw_candidate.get("is_multi_valued")
                if not (
                    isinstance(subject_key, str)
                    and isinstance(predicate, str)
                    and isinstance(assertion_type, str)
                    and isinstance(value, str)
                    and isinstance(confidence, int | float)
                    and isinstance(is_multi_valued, bool)
                ):
                    continue
                propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text=source_text,
                    subject_key=subject_key,
                    predicate=predicate,
                    assertion_type=assertion_type,
                    value=value,
                    confidence=float(confidence),
                    scope_key="global",
                    is_multi_valued=is_multi_valued,
                    valid_from=None,
                    valid_to=None,
                    extraction_model=settings.model_name,
                    extraction_prompt_version="memory-extraction-v1",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
