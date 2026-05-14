from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
import json
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import AppSettings
from .persistence import (
    AIJudgmentRecord,
    MemoryActionTraceRecord,
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryConflictMemberRecord,
    MemoryConflictSetRecord,
    MemoryContextBlockRecord,
    MemoryDeletionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEntityProjectionRecord,
    MemoryEntityRecord,
    MemoryEvalRunRecord,
    MemoryEpisodeRecord,
    MemoryEvidenceRecord,
    MemoryExportArtifactRecord,
    MemoryGraphProjectionRecord,
    MemoryKeywordProjectionRecord,
    MemoryProcedureRecord,
    MemoryProjectionJobRecord,
    MemoryRelationshipRecord,
    MemoryReviewRecord,
    MemoryRetentionPolicyRecord,
    MemorySalienceRecord,
    MemoryScopeBindingRecord,
    MemorySensitivityLabelRecord,
    MemorySymbolProjectionRecord,
    MemoryTemporalProjectionRecord,
    MemoryTopicMemberRecord,
    MemoryTopicRecord,
    MemoryVersionRecord,
    ProjectStateSnapshotRecord,
    SessionRecord,
    TurnRecord,
    to_rfc3339,
)
from .redaction import redact_text


MEMORY_CONTEXT_SCHEMA_VERSION = "memory.sota.v1"
MEMORY_PROJECTION_VERSION = "embedding-v1"
MEMORY_CURATION_PROMPT_VERSION = "memory-curation-v1"
MEMORY_CONTINUITY_PROMPT_VERSION = "memory-continuity-v1"
USER_SUBJECT_KEY = "user:default"
ALLOWED_MEMORY_ASSERTION_TYPES = {
    "fact",
    "profile",
    "preference",
    "commitment",
    "decision",
    "project_state",
    "procedure",
    "domain_concept",
}


class AIJudgmentFailure(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        safe_reason: str,
        retryable: bool,
        parse_status: str,
        validation_status: str,
        provider_response_id: str | None = None,
    ) -> None:
        super().__init__(safe_reason)
        self.code = code
        self.safe_reason = safe_reason
        self.retryable = retryable
        self.parse_status = parse_status
        self.validation_status = validation_status
        self.provider_response_id = provider_response_id


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


def _memory_version_number(db: Session, *, table: str, record_id: str) -> int:
    return (
        db.scalar(
            select(func.max(MemoryVersionRecord.version)).where(
                MemoryVersionRecord.canonical_table == table,
                MemoryVersionRecord.canonical_id == record_id,
            )
        )
        or 1
    )


def session_allows_memory_operation(
    db: Session,
    *,
    session_id: str,
    operation: str,
    subject_key: str | None = None,
    scope_key: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    actor_id: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    session = db.get(SessionRecord, session_id)
    if session is None:
        return False, {
            "session_id": session_id,
            "operation": operation,
            "actor_id": actor_id,
            "memory_mode": "missing_session",
            "binding_id": None,
            "reason": "session not found",
        }

    now = datetime.now(UTC)
    candidate_scopes: list[tuple[int, str, str]] = []
    if thread_id is not None:
        candidate_scopes.append((10, "thread", thread_id))
    if proactive_case_id is not None:
        candidate_scopes.append((20, "proactive_case", proactive_case_id))
    for key in (scope_key, subject_key):
        if key is None:
            continue
        if key.startswith("project:"):
            candidate_scopes.append((30, "project", key))
            candidate_scopes.append((30, "project", key.removeprefix("project:")))
        if key.startswith("repo:"):
            candidate_scopes.append((30, "repo", key))
            candidate_scopes.append((30, "repo", key.removeprefix("repo:")))
    candidate_scopes.append((40, "session", session_id))
    candidate_scopes.append((50, "user", USER_SUBJECT_KEY))
    candidate_scopes.append((50, "user", "default"))

    bindings = db.scalars(
        select(MemoryScopeBindingRecord)
        .where(
            (MemoryScopeBindingRecord.expires_at.is_(None))
            | (MemoryScopeBindingRecord.expires_at > now)
        )
        .order_by(MemoryScopeBindingRecord.updated_at.desc(), MemoryScopeBindingRecord.id.asc())
    ).all()
    binding: MemoryScopeBindingRecord | None = None
    binding_priority = 100
    for priority, scope_type, key in candidate_scopes:
        for candidate in bindings:
            if candidate.scope_type == scope_type and candidate.scope_key == key:
                if priority < binding_priority:
                    binding = candidate
                    binding_priority = priority
                break

    if session.memory_mode != "normal":
        return False, {
            "session_id": session_id,
            "operation": operation,
            "actor_id": actor_id,
            "memory_mode": session.memory_mode,
            "binding_id": binding.id if binding is not None else None,
            "binding_scope_type": binding.scope_type if binding is not None else None,
            "binding_scope_key": binding.scope_key if binding is not None else None,
            "scope_resolution": [
                {"scope_type": scope_type, "scope_key": key, "priority": priority}
                for priority, scope_type, key in candidate_scopes
            ],
            "reason": f"session memory mode is {session.memory_mode}",
        }

    if binding is None:
        return True, {
            "session_id": session_id,
            "operation": operation,
            "actor_id": actor_id,
            "memory_mode": session.memory_mode,
            "binding_id": None,
            "binding_scope_type": None,
            "binding_scope_key": None,
            "scope_resolution": [
                {"scope_type": scope_type, "scope_key": key, "priority": priority}
                for priority, scope_type, key in candidate_scopes
            ],
            "reason": "session memory mode allows operation",
        }

    if operation == "recall":
        allowed = binding.memory_mode == "normal" and binding.recall_enabled
    else:
        allowed = binding.memory_mode == "normal" and binding.extraction_enabled
    return allowed, {
        "session_id": session_id,
        "operation": operation,
        "actor_id": actor_id,
        "memory_mode": binding.memory_mode,
        "binding_id": binding.id,
        "binding_scope_type": binding.scope_type,
        "binding_scope_key": binding.scope_key,
        "scope_resolution": [
            {"scope_type": scope_type, "scope_key": key, "priority": priority}
            for priority, scope_type, key in candidate_scopes
        ],
        "reason": binding.reason or "session memory binding applied",
    }


def scope_allows_memory_operation(
    db: Session,
    *,
    scope_key: str,
    operation: str,
    actor_id: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    now = datetime.now(UTC)
    candidate_scopes: list[tuple[int, str, str]] = []
    if scope_key.startswith("project:"):
        candidate_scopes.append((10, "project", scope_key))
        candidate_scopes.append((10, "project", scope_key.removeprefix("project:")))
    elif scope_key.startswith("repo:"):
        candidate_scopes.append((10, "repo", scope_key))
        candidate_scopes.append((10, "repo", scope_key.removeprefix("repo:")))
    elif scope_key.startswith("session:"):
        candidate_scopes.append((10, "session", scope_key.removeprefix("session:")))
    elif scope_key not in {"global", USER_SUBJECT_KEY, "default"}:
        candidate_scopes.append((10, "project", scope_key))
        candidate_scopes.append((10, "repo", scope_key))
    candidate_scopes.append((50, "user", USER_SUBJECT_KEY))
    candidate_scopes.append((50, "user", "default"))

    binding: MemoryScopeBindingRecord | None = None
    binding_priority = 100
    bindings = db.scalars(
        select(MemoryScopeBindingRecord)
        .where(
            (MemoryScopeBindingRecord.expires_at.is_(None))
            | (MemoryScopeBindingRecord.expires_at > now)
        )
        .order_by(MemoryScopeBindingRecord.updated_at.desc(), MemoryScopeBindingRecord.id.asc())
    ).all()
    for priority, scope_type, key in candidate_scopes:
        for candidate in bindings:
            if candidate.scope_type == scope_type and candidate.scope_key == key:
                if priority < binding_priority:
                    binding = candidate
                    binding_priority = priority
                break

    if binding is None:
        return True, {
            "scope_key": scope_key,
            "operation": operation,
            "actor_id": actor_id,
            "memory_mode": "normal",
            "binding_id": None,
            "binding_scope_type": None,
            "binding_scope_key": None,
            "scope_resolution": [
                {"scope_type": scope_type, "scope_key": key, "priority": priority}
                for priority, scope_type, key in candidate_scopes
            ],
            "reason": "scope has no blocking memory binding",
        }

    if operation == "recall":
        allowed = binding.memory_mode == "normal" and binding.recall_enabled
    else:
        allowed = binding.memory_mode == "normal" and binding.extraction_enabled
    return allowed, {
        "scope_key": scope_key,
        "operation": operation,
        "actor_id": actor_id,
        "memory_mode": binding.memory_mode,
        "binding_id": binding.id,
        "binding_scope_type": binding.scope_type,
        "binding_scope_key": binding.scope_key,
        "scope_resolution": [
            {"scope_type": scope_type, "scope_key": key, "priority": priority}
            for priority, scope_type, key in candidate_scopes
        ],
        "reason": binding.reason or "scope memory binding applied",
    }


def _matching_scope_key_for_text(db: Session, *, text: str) -> str | None:
    query_terms = set(_terms(text))
    if not query_terms:
        return None
    now = datetime.now(UTC)
    for binding in db.scalars(
        select(MemoryScopeBindingRecord)
        .where(
            MemoryScopeBindingRecord.scope_type.in_(("project", "repo")),
            (MemoryScopeBindingRecord.expires_at.is_(None))
            | (MemoryScopeBindingRecord.expires_at > now),
        )
        .order_by(MemoryScopeBindingRecord.updated_at.desc(), MemoryScopeBindingRecord.id.asc())
    ).all():
        if query_terms.intersection(set(_terms(binding.scope_key))):
            return binding.scope_key
    return None


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json_value(item) for key, item in value.items()}
    return value


def _never_remember_rule_for_text(
    db: Session,
    *,
    source_session_id: str,
    scope_key: str,
    text: str,
) -> MemoryRetentionPolicyRecord | None:
    text_lower = text.lower()
    for rule in db.scalars(
        select(MemoryRetentionPolicyRecord)
        .where(
            MemoryRetentionPolicyRecord.lifecycle_state == "active",
            MemoryRetentionPolicyRecord.policy_kind == "never_remember",
            MemoryRetentionPolicyRecord.scope_key.in_(
                ("global", scope_key, f"session:{source_session_id}")
            ),
        )
        .order_by(
            MemoryRetentionPolicyRecord.created_at.asc(), MemoryRetentionPolicyRecord.id.asc()
        )
    ).all():
        pattern = rule.pattern.strip().lower()
        if pattern == "*" or (pattern and pattern in text_lower):
            return rule
    return None


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
    stored_text = redact_text(text)
    redaction_posture = "redacted" if stored_text != text else "none"
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
        source_text=stored_text,
        evidence_snippet=_clean_text(stored_text, max_chars=360),
        redaction_posture=redaction_posture,
        metadata_json=metadata,
        created_at=now,
        updated_at=now,
    )
    db.add(evidence)
    db.flush()
    _record_version(
        db,
        table="memory_evidence",
        record_id=evidence.id,
        change_type="created",
        actor_id=actor_id,
        reason="memory evidence recorded",
        new_state={
            "source_session_id": session_id,
            "source_turn_id": turn_id,
            "content_class": content_class,
            "trust_boundary": trust_boundary,
            "lifecycle_state": evidence.lifecycle_state,
            "redaction_posture": redaction_posture,
        },
        redaction_posture=redaction_posture,
        now=now,
        new_id_fn=new_id_fn,
    )
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
    new_state: dict[str, Any] | None = None,
    prior_state: dict[str, Any] | None = None,
    redaction_posture: str = "none",
    now: datetime,
    new_id_fn: Callable[[str], str],
    projection_invalidation: dict[str, Any] | None = None,
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
            prior_state=prior_state,
            new_state=new_state,
            redaction_posture=redaction_posture,
            projection_invalidation=projection_invalidation or {},
            created_at=now,
        )
    )


def _record_deletion(
    db: Session,
    *,
    target_table: str = "memory_assertions",
    target_id: str,
    deletion_type: str,
    actor_id: str,
    reason: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
    redaction_posture: str = "none",
    projection_invalidation: dict[str, Any] | None = None,
) -> None:
    db.add(
        MemoryDeletionRecord(
            id=new_id_fn("mdl"),
            target_table=target_table,
            target_id=target_id,
            deletion_type=deletion_type,
            actor_id=actor_id,
            reason=reason,
            redaction_posture=redaction_posture,
            projection_invalidation=projection_invalidation or {target_table: target_id},
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


def _delete_projection_rows(
    db: Session,
    *,
    assertion_id: str,
    now: datetime,
    redaction_posture: str = "none",
) -> dict[str, Any]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    assertion_text = _assertion_text(assertion).lower() if assertion is not None else ""
    scrub_content = redaction_posture in {"redacted", "privacy_deleted"} and assertion_text != ""
    embedding_ids = list(
        db.scalars(
            select(MemoryEmbeddingProjectionRecord.id).where(
                MemoryEmbeddingProjectionRecord.assertion_id == assertion_id
            )
        ).all()
    )
    projection_job_ids = list(
        db.scalars(
            select(MemoryProjectionJobRecord.id).where(
                MemoryProjectionJobRecord.target_table == "memory_assertions",
                MemoryProjectionJobRecord.target_id == assertion_id,
            )
        ).all()
    )
    keyword_ids = list(
        db.scalars(
            select(MemoryKeywordProjectionRecord.id).where(
                MemoryKeywordProjectionRecord.canonical_table == "memory_assertions",
                MemoryKeywordProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    entity_projection_ids = list(
        db.scalars(
            select(MemoryEntityProjectionRecord.id).where(
                MemoryEntityProjectionRecord.canonical_table == "memory_assertions",
                MemoryEntityProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    salience_ids = list(
        db.scalars(
            select(MemorySalienceRecord.id).where(MemorySalienceRecord.assertion_id == assertion_id)
        ).all()
    )
    topic_member_ids = list(
        db.scalars(
            select(MemoryTopicMemberRecord.id).where(
                MemoryTopicMemberRecord.canonical_table == "memory_assertions",
                MemoryTopicMemberRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    source_topic_ids = list(
        db.scalars(
            select(MemoryTopicMemberRecord.topic_id).where(
                MemoryTopicMemberRecord.canonical_table == "memory_assertions",
                MemoryTopicMemberRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    topics_to_delete = [
        topic
        for topic in db.scalars(
            select(MemoryTopicRecord).where(MemoryTopicRecord.lifecycle_state == "active")
        ).all()
        if topic.id in source_topic_ids
        or (
            scrub_content
            and assertion_text
            in json.dumps(
                {"title": topic.title, "summary": topic.summary, "metadata": topic.metadata_json},
                sort_keys=True,
            ).lower()
        )
    ]
    topic_ids = [topic.id for topic in topics_to_delete]
    temporal_ids = list(
        db.scalars(
            select(MemoryTemporalProjectionRecord.id).where(
                MemoryTemporalProjectionRecord.canonical_table == "memory_assertions",
                MemoryTemporalProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    symbol_ids = list(
        db.scalars(
            select(MemorySymbolProjectionRecord.id).where(
                MemorySymbolProjectionRecord.canonical_table == "memory_assertions",
                MemorySymbolProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    graph_ids: list[str] = []
    if assertion is not None:
        graph_ids = list(
            db.scalars(
                select(MemoryGraphProjectionRecord.id).where(
                    (MemoryGraphProjectionRecord.source_entity_id == assertion.subject_entity_id)
                    | (MemoryGraphProjectionRecord.target_entity_id == assertion.subject_entity_id)
                )
            ).all()
        )
    procedures_to_delete = [
        procedure
        for procedure in db.scalars(
            select(MemoryProcedureRecord).where(MemoryProcedureRecord.lifecycle_state == "active")
        ).all()
        if procedure.source_assertion_id == assertion_id
        or (
            scrub_content
            and assertion_text
            in json.dumps(
                {"title": procedure.title, "instruction": procedure.instruction},
                sort_keys=True,
            ).lower()
        )
    ]
    procedure_ids = [procedure.id for procedure in procedures_to_delete]
    snapshots_to_delete = [
        snapshot
        for snapshot in db.scalars(
            select(ProjectStateSnapshotRecord).where(
                ProjectStateSnapshotRecord.lifecycle_state == "active"
            )
        ).all()
        if assertion_id in snapshot.source_assertion_ids
        or (
            scrub_content
            and assertion_text
            in json.dumps(
                {"summary": snapshot.summary, "state": snapshot.state},
                sort_keys=True,
            ).lower()
        )
    ]
    snapshot_ids = [snapshot.id for snapshot in snapshots_to_delete]
    blocks_to_delete = [
        block
        for block in db.scalars(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.lifecycle_state == "active"
            )
        ).all()
        if assertion_id in block.source_assertion_ids
        or (scrub_content and assertion_text in block.content.lower())
    ]
    context_block_ids = [block.id for block in blocks_to_delete]
    export_artifact_ids: list[str] = []
    for artifact in db.scalars(select(MemoryExportArtifactRecord)).all():
        artifact_content = json.dumps(artifact.content, sort_keys=True)
        if assertion_id not in artifact_content and not (
            scrub_content and assertion_text in artifact_content.lower()
        ):
            continue
        export_artifact_ids.append(artifact.id)
        artifact.status = "failed"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            artifact.content = {}
            artifact.redaction_posture = redaction_posture
        artifact.source_counts = {
            **artifact.source_counts,
            "invalidated_by_assertion_id": assertion_id,
            "invalidated_at": to_rfc3339(now),
            "redaction_posture": redaction_posture,
        }
        artifact.updated_at = now

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
    db.execute(
        delete(MemoryTopicMemberRecord).where(
            MemoryTopicMemberRecord.canonical_table == "memory_assertions",
            MemoryTopicMemberRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemoryTemporalProjectionRecord).where(
            MemoryTemporalProjectionRecord.canonical_table == "memory_assertions",
            MemoryTemporalProjectionRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemorySymbolProjectionRecord).where(
            MemorySymbolProjectionRecord.canonical_table == "memory_assertions",
            MemorySymbolProjectionRecord.canonical_id == assertion_id,
        )
    )
    if assertion is not None:
        db.execute(
            delete(MemoryGraphProjectionRecord).where(
                (MemoryGraphProjectionRecord.source_entity_id == assertion.subject_entity_id)
                | (MemoryGraphProjectionRecord.target_entity_id == assertion.subject_entity_id)
            )
        )
    for procedure in procedures_to_delete:
        procedure.lifecycle_state = "deleted"
        procedure.valid_to = now
        if redaction_posture in {"redacted", "privacy_deleted"}:
            procedure.title = f"[{redaction_posture}]"
            procedure.instruction = f"[{redaction_posture}]"
            procedure.metadata_json = {
                **procedure.metadata_json,
                "redaction_posture": redaction_posture,
            }
        procedure.updated_at = now
    for snapshot in snapshots_to_delete:
        snapshot.lifecycle_state = "deleted"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            snapshot.summary = f"[{redaction_posture}]"
            snapshot.state = {"redaction_posture": redaction_posture}
        snapshot.updated_at = now
    for block in blocks_to_delete:
        block.lifecycle_state = "deleted"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            block.content = f"[{redaction_posture}]"
        block.updated_at = now
    for topic in topics_to_delete:
        topic.lifecycle_state = "deleted"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            topic.title = f"[{redaction_posture}]"
            topic.summary = f"[{redaction_posture}]"
            topic.metadata_json = {"redaction_posture": redaction_posture}
        topic.updated_at = now
    invalidation = {
        "assertion_id": assertion_id,
        "deleted_rows": {
            "memory_embedding_projections": embedding_ids,
            "memory_projection_jobs": projection_job_ids,
            "memory_keyword_projections": keyword_ids,
            "memory_entity_projections": entity_projection_ids,
            "memory_salience": salience_ids,
            "memory_topic_members": topic_member_ids,
            "memory_temporal_projections": temporal_ids,
            "memory_symbol_projections": symbol_ids,
            "memory_graph_projections": graph_ids,
        },
        "marked_deleted": {
            "memory_procedures": procedure_ids,
            "project_state_snapshots": snapshot_ids,
            "memory_context_blocks": context_block_ids,
            "memory_topics": topic_ids,
        },
        "invalidated_exports": export_artifact_ids,
    }
    return invalidation


def _record_projection_rows(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    subject_entity_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    source_memory_version = _memory_version_number(
        db, table="memory_assertions", record_id=assertion.id
    )
    search_text = _assertion_search_text(assertion)
    db.add(
        MemoryKeywordProjectionRecord(
            id=new_id_fn("mkp"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            projection_version=MEMORY_PROJECTION_VERSION,
            source_memory_version=source_memory_version,
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
            source_memory_version=source_memory_version,
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
    db.add(
        MemoryTemporalProjectionRecord(
            id=new_id_fn("mtpj"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            temporal_kind="validity",
            valid_from=assertion.valid_from,
            valid_to=assertion.valid_to,
            occurred_at=None,
            projection_version=MEMORY_PROJECTION_VERSION,
            source_memory_version=source_memory_version,
            metadata_json={"source": "assertion_validity"},
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
    worker_id: str = "memory-worker",
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
            job.claimed_by = worker_id
            job.attempt_token = new_id_fn("mpa")
            job.last_heartbeat = now
            job.updated_at = now
            job_id = job.id
            attempt_token = str(job.attempt_token)
            if job.target_table != "memory_assertions":
                job.lifecycle_state = "dead_letter"
                job.error = f"malformed embedding projection target table: {job.target_table}"
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.run_after = now
                job.updated_at = now
                return True
            assertion = db.get(MemoryAssertionRecord, job.target_id)
            if assertion is None:
                job.lifecycle_state = "dead_letter"
                job.error = "malformed embedding projection missing assertion"
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.run_after = now
                job.updated_at = now
                return True
            if assertion.lifecycle_state != "active":
                job.lifecycle_state = "completed"
                job.error = None
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.updated_at = now
                return True
            assertion_id = assertion.id
            search_text = _assertion_search_text(assertion)

    try:
        vector = embed_memory_text(search_text, settings=settings)
    except Exception as exc:
        with session_factory() as db:
            with db.begin():
                job = db.scalar(
                    select(MemoryProjectionJobRecord)
                    .where(
                        MemoryProjectionJobRecord.id == job_id,
                        MemoryProjectionJobRecord.lifecycle_state == "running",
                        MemoryProjectionJobRecord.attempt_token == attempt_token,
                    )
                    .with_for_update()
                )
                if job is not None:
                    now = now_fn()
                    job.lifecycle_state = (
                        "dead_letter" if job.attempts >= job.max_retries else "pending"
                    )
                    job.error = _clean_text(str(exc), max_chars=500)
                    job.claimed_by = None
                    job.attempt_token = None
                    job.last_heartbeat = None
                    job.run_after = (
                        now if job.lifecycle_state == "dead_letter" else now + timedelta(seconds=30)
                    )
                    job.updated_at = now
        return True

    with session_factory() as db:
        with db.begin():
            now = now_fn()
            job = db.scalar(
                select(MemoryProjectionJobRecord)
                .where(
                    MemoryProjectionJobRecord.id == job_id,
                    MemoryProjectionJobRecord.lifecycle_state == "running",
                    MemoryProjectionJobRecord.attempt_token == attempt_token,
                )
                .with_for_update()
            )
            if job is None:
                return True
            assertion = db.get(MemoryAssertionRecord, assertion_id)
            if assertion is None or assertion.lifecycle_state != "active":
                job.lifecycle_state = "completed"
                job.error = None
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.updated_at = now
                return True

            search_text = _assertion_search_text(assertion)
            source_memory_version = (
                db.scalar(
                    select(func.max(MemoryVersionRecord.version)).where(
                        MemoryVersionRecord.canonical_table == "memory_assertions",
                        MemoryVersionRecord.canonical_id == assertion.id,
                    )
                )
                or 1
            )
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
                        source_memory_version=source_memory_version,
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
                row.source_memory_version = source_memory_version
                row.search_text = search_text
                row.embedding = vector
                row.updated_at = now

            job.lifecycle_state = "completed"
            job.error = None
            job.claimed_by = None
            job.attempt_token = None
            job.last_heartbeat = None
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
                score=0.0,
                signals=signals,
                created_at=now,
                updated_at=now,
            )
        )
        return
    row.user_priority = user_priority
    row.score = 0.0
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
    text = redact_text(_assertion_text(assertion))
    source_evidence_ids = list(
        db.scalars(
            select(MemoryAssertionEvidenceRecord.evidence_id).where(
                MemoryAssertionEvidenceRecord.assertion_id == assertion.id
            )
        ).all()
    )
    source_versions = {
        "memory_assertions": {
            assertion.id: _memory_version_number(
                db, table="memory_assertions", record_id=assertion.id
            )
        }
    }
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
        source_evidence_ids=source_evidence_ids,
        lifecycle_state="active",
        projection_version=MEMORY_PROJECTION_VERSION,
        created_at=now,
        updated_at=now,
    )
    db.add(snapshot)
    topic_key = f"{assertion.subject_key}:project-state"
    topic = db.scalar(
        select(MemoryTopicRecord)
        .where(
            MemoryTopicRecord.scope_key == assertion.subject_key,
            MemoryTopicRecord.topic_key == topic_key,
        )
        .limit(1)
    )
    if topic is None:
        topic = MemoryTopicRecord(
            id=new_id_fn("mtp"),
            topic_key=topic_key,
            family="active-projects",
            scope_key=assertion.subject_key,
            title=f"{assertion.subject_key} project state",
            summary=text,
            lifecycle_state="active",
            projection_version=MEMORY_PROJECTION_VERSION,
            metadata_json={"subject_key": assertion.subject_key},
            created_at=now,
            updated_at=now,
        )
        db.add(topic)
        db.flush()
    else:
        topic.summary = text
        topic.lifecycle_state = "active"
        topic.projection_version = MEMORY_PROJECTION_VERSION
        topic.updated_at = now
    topic_member = db.scalar(
        select(MemoryTopicMemberRecord)
        .where(
            MemoryTopicMemberRecord.topic_id == topic.id,
            MemoryTopicMemberRecord.canonical_table == "memory_assertions",
            MemoryTopicMemberRecord.canonical_id == assertion.id,
        )
        .limit(1)
    )
    if topic_member is None:
        db.add(
            MemoryTopicMemberRecord(
                id=new_id_fn("mtm"),
                topic_id=topic.id,
                canonical_table="memory_assertions",
                canonical_id=assertion.id,
                membership_kind="source",
                rank=0,
                metadata_json={"assertion_type": assertion.assertion_type},
                created_at=now,
            )
        )
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
                topic_id=None,
                lifecycle_state="active",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_action_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                source_memory_versions=source_versions,
                source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        block.content = f"{assertion.subject_key}: {text}"
        block.lifecycle_state = "active"
        block.source_assertion_ids = [assertion.id]
        block.source_project_state_snapshot_ids = [snapshot.id]
        block.source_memory_versions = source_versions
        block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
        block.updated_at = now
    hot_block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "hot_index",
            MemoryContextBlockRecord.scope_key == assertion.subject_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if hot_block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="hot_index",
                scope_key=assertion.subject_key,
                content=f"project state: {assertion.subject_key}: {text}",
                topic_id=None,
                lifecycle_state="active",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_action_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                source_memory_versions=source_versions,
                source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        hot_block.content = f"project state: {assertion.subject_key}: {text}"
        hot_block.lifecycle_state = "active"
        hot_block.source_assertion_ids = [assertion.id]
        hot_block.source_project_state_snapshot_ids = [snapshot.id]
        hot_block.source_memory_versions = source_versions
        hot_block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
        hot_block.updated_at = now
    topic_block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "topic",
            MemoryContextBlockRecord.scope_key == assertion.subject_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if topic_block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="topic",
                scope_key=assertion.subject_key,
                content=text,
                topic_id=topic.id,
                lifecycle_state="active",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_action_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                source_memory_versions=source_versions,
                source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
        return
    topic_block.content = text
    topic_block.topic_id = topic.id
    topic_block.lifecycle_state = "active"
    topic_block.source_assertion_ids = [assertion.id]
    topic_block.source_project_state_snapshot_ids = [snapshot.id]
    topic_block.source_memory_versions = source_versions
    topic_block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
    topic_block.updated_at = now


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
    actor_id: str,
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

    assertion_prior_state = {"lifecycle_state": assertion.lifecycle_state}
    assertion.lifecycle_state = "conflicted"
    assertion.updated_at = now
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason="candidate entered open conflict",
        prior_state=assertion_prior_state,
        new_state={"lifecycle_state": "conflicted"},
        now=now,
        new_id_fn=new_id_fn,
    )
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
        if member.id != assertion.id and member.lifecycle_state == "active":
            prior_state = {"lifecycle_state": member.lifecycle_state}
            member.updated_at = now
            projection_invalidation = _delete_projection_rows(db, assertion_id=member.id, now=now)
            _record_version(
                db,
                table="memory_assertions",
                record_id=member.id,
                change_type="updated",
                actor_id=actor_id,
                reason="active memory projection invalidated by open conflict",
                prior_state=prior_state,
                new_state={"lifecycle_state": "active", "conflict_set_id": conflict.id},
                projection_invalidation=projection_invalidation,
                now=now,
                new_id_fn=new_id_fn,
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
            projection_invalidation = _delete_projection_rows(db, assertion_id=existing.id, now=now)
            _record_version(
                db,
                table="memory_assertions",
                record_id=existing.id,
                change_type="superseded",
                actor_id=actor_id,
                reason="single-valued assertion replaced",
                new_state={"superseded_by_assertion_id": assertion.id},
                projection_invalidation=projection_invalidation,
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
) -> tuple[list[dict[str, Any]], str | None]:
    matched_scope_key = _matching_scope_key_for_text(db, text=user_message)
    allowed, _policy = session_allows_memory_operation(
        db,
        session_id=session_id,
        operation="write",
        subject_key=matched_scope_key,
        scope_key=matched_scope_key,
        actor_id=actor_id,
    )
    if not allowed:
        return [], None
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
    source_evidence_id: str | None = None,
) -> list[dict[str, Any]]:
    allowed, _policy = session_allows_memory_operation(
        db,
        session_id=source_session_id,
        operation="write",
        subject_key=subject_key,
        scope_key=scope_key,
    )
    if not allowed:
        return []
    if (
        _never_remember_rule_for_text(
            db,
            source_session_id=source_session_id,
            scope_key=scope_key,
            text=evidence_text,
        )
        is not None
    ):
        return []
    now = now_fn()
    evidence = db.get(MemoryEvidenceRecord, source_evidence_id) if source_evidence_id else None
    if evidence is not None and evidence.lifecycle_state != "available":
        return []
    evidence_was_existing = evidence is not None
    if evidence is None:
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
    events = []
    if not evidence_was_existing:
        events.append(
            {
                "event_type": "evt.memory.evidence_recorded",
                "payload": {
                    "evidence_id": evidence.id,
                    "source_turn_id": None,
                    "source_session_id": source_session_id,
                    "content_class": evidence.content_class,
                    "trust_boundary": evidence.trust_boundary,
                },
            }
        )
    events.extend(
        [
            {
                "event_type": "evt.memory.candidate_proposed",
                "payload": _event_payload(assertion, evidence_id=evidence.id),
            },
            {"event_type": "evt.memory.review_required", "payload": _event_payload(assertion)},
        ]
    )
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
                actor_id=actor_id,
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
    if assertion is None or assertion.lifecycle_state != "candidate":
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
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
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
        projection_invalidation=projection_invalidation,
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
    if old_assertion.lifecycle_state not in {"active", "candidate", "conflicted"}:
        return []
    entity = db.get(MemoryEntityRecord, old_assertion.subject_entity_id)
    if entity is None:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": old_assertion.lifecycle_state,
        "object_value": old_assertion.object_value,
        "superseded_by_assertion_id": old_assertion.superseded_by_assertion_id,
    }
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
    projection_invalidation = _delete_projection_rows(db, assertion_id=old_assertion.id, now=now)
    _record_version(
        db,
        table="memory_assertions",
        record_id=old_assertion.id,
        change_type="superseded",
        actor_id=actor_id,
        reason="assertion corrected",
        prior_state=prior_state,
        new_state={"superseded_by_assertion_id": new_assertion.id},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
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
    if assertion is None or assertion.lifecycle_state in {
        "deleted",
        "privacy_deleted",
        "retracted",
    }:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "retracted"
    assertion.valid_to = now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_deletion(
        db,
        target_id=assertion.id,
        deletion_type="retract",
        actor_id=actor_id,
        reason="assertion retracted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="retracted",
        actor_id=actor_id,
        reason="assertion retracted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "retracted"},
        projection_invalidation=projection_invalidation,
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
    if assertion is None or assertion.lifecycle_state in {
        "deleted",
        "privacy_deleted",
        "retracted",
    }:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "deleted"
    assertion.valid_to = now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_deletion(
        db,
        target_id=assertion.id,
        deletion_type="delete",
        actor_id=actor_id,
        reason="assertion deleted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="deleted",
        actor_id=actor_id,
        reason="assertion deleted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "deleted"},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.assertion_deleted", "payload": _event_payload(assertion)}]


def privacy_delete_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state == "privacy_deleted":
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "privacy_deleted"
    assertion.object_value = {"text": "[privacy_deleted]"}
    assertion.valid_to = now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(
        db,
        assertion_id=assertion.id,
        now=now,
        redaction_posture="privacy_deleted",
    )
    evidence_ids = [
        evidence_id
        for evidence_id in db.scalars(
            select(MemoryAssertionEvidenceRecord.evidence_id).where(
                MemoryAssertionEvidenceRecord.assertion_id == assertion.id
            )
        ).all()
        if isinstance(evidence_id, str)
    ]
    for version in db.scalars(
        select(MemoryVersionRecord).where(
            MemoryVersionRecord.canonical_table == "memory_assertions",
            MemoryVersionRecord.canonical_id == assertion.id,
        )
    ).all():
        for state_field in ("prior_state", "new_state"):
            state = getattr(version, state_field)
            if isinstance(state, dict) and "object_value" in state:
                setattr(
                    version, state_field, {**state, "object_value": {"text": "[privacy_deleted]"}}
                )
        version.redaction_posture = "privacy_deleted"
    for evidence_id in evidence_ids:
        evidence = db.get(MemoryEvidenceRecord, evidence_id)
        if evidence is None:
            continue
        evidence_prior_state = {
            "lifecycle_state": evidence.lifecycle_state,
            "redaction_posture": evidence.redaction_posture,
        }
        evidence.lifecycle_state = "privacy_deleted"
        evidence.source_text = "[privacy_deleted]"
        evidence.evidence_snippet = "[privacy_deleted]"
        evidence.redaction_posture = "privacy_deleted"
        evidence.updated_at = now
        _record_deletion(
            db,
            target_table="memory_evidence",
            target_id=evidence.id,
            deletion_type="privacy_delete",
            actor_id=actor_id,
            reason=reason or "evidence privacy deleted",
            redaction_posture="privacy_deleted",
            now=now,
            new_id_fn=new_id_fn,
        )
        _record_version(
            db,
            table="memory_evidence",
            record_id=evidence.id,
            change_type="privacy_deleted",
            actor_id=actor_id,
            reason=reason or "evidence privacy deleted",
            prior_state=evidence_prior_state,
            new_state={
                "lifecycle_state": "privacy_deleted",
                "redaction_posture": "privacy_deleted",
            },
            redaction_posture="privacy_deleted",
            now=now,
            new_id_fn=new_id_fn,
        )
        for linked_assertion_id in db.scalars(
            select(MemoryAssertionEvidenceRecord.assertion_id).where(
                MemoryAssertionEvidenceRecord.evidence_id == evidence.id,
                MemoryAssertionEvidenceRecord.assertion_id != assertion.id,
            )
        ).all():
            linked_assertion = db.get(MemoryAssertionRecord, linked_assertion_id)
            if linked_assertion is None or linked_assertion.lifecycle_state not in {
                "active",
                "candidate",
                "conflicted",
            }:
                continue
            linked_prior_state = {
                "lifecycle_state": linked_assertion.lifecycle_state,
                "object_value": linked_assertion.object_value,
            }
            linked_assertion.lifecycle_state = "privacy_deleted"
            linked_assertion.object_value = {"text": "[privacy_deleted]"}
            linked_assertion.valid_to = now
            linked_assertion.updated_at = now
            linked_invalidation = _delete_projection_rows(
                db,
                assertion_id=linked_assertion.id,
                now=now,
                redaction_posture="privacy_deleted",
            )
            _record_deletion(
                db,
                target_id=linked_assertion.id,
                deletion_type="privacy_delete",
                actor_id=actor_id,
                reason=reason or "shared privacy-deleted evidence invalidated assertion",
                now=now,
                new_id_fn=new_id_fn,
                redaction_posture="privacy_deleted",
                projection_invalidation=linked_invalidation,
            )
            _record_version(
                db,
                table="memory_assertions",
                record_id=linked_assertion.id,
                change_type="privacy_deleted",
                actor_id=actor_id,
                reason=reason or "shared privacy-deleted evidence invalidated assertion",
                prior_state=linked_prior_state,
                new_state={
                    "lifecycle_state": "privacy_deleted",
                    "object_value": {"text": "[privacy_deleted]"},
                    "redaction_posture": "privacy_deleted",
                },
                redaction_posture="privacy_deleted",
                now=now,
                new_id_fn=new_id_fn,
                projection_invalidation=linked_invalidation,
            )
    _record_deletion(
        db,
        target_id=assertion.id,
        deletion_type="privacy_delete",
        actor_id=actor_id,
        reason=reason or "assertion privacy deleted",
        redaction_posture="privacy_deleted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="privacy_deleted",
        actor_id=actor_id,
        reason=reason or "assertion privacy deleted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "privacy_deleted", "redaction_posture": "privacy_deleted"},
        redaction_posture="privacy_deleted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    return [
        {
            "event_type": "evt.memory.assertion_deleted",
            "payload": {
                **_event_payload(assertion),
                "deletion_type": "privacy_delete",
                "evidence_ids": evidence_ids,
            },
        }
    ]


def redact_evidence(
    db: Session,
    *,
    evidence_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    evidence = db.get(MemoryEvidenceRecord, evidence_id)
    if evidence is None or evidence.lifecycle_state == "privacy_deleted":
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": evidence.lifecycle_state,
        "redaction_posture": evidence.redaction_posture,
    }
    evidence.lifecycle_state = "redacted"
    evidence.source_text = "[redacted]"
    evidence.evidence_snippet = "[redacted]"
    evidence.redaction_posture = "redacted"
    evidence.updated_at = now
    _record_deletion(
        db,
        target_table="memory_evidence",
        target_id=evidence.id,
        deletion_type="redact",
        actor_id=actor_id,
        reason=reason or "evidence redacted",
        redaction_posture="redacted",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_evidence",
        record_id=evidence.id,
        change_type="redacted",
        actor_id=actor_id,
        reason=reason or "evidence redacted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "redacted", "redaction_posture": "redacted"},
        redaction_posture="redacted",
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {"event_type": "evt.memory.evidence_redacted", "payload": {"evidence_id": evidence.id}}
    ]
    for assertion_id in db.scalars(
        select(MemoryAssertionEvidenceRecord.assertion_id).where(
            MemoryAssertionEvidenceRecord.evidence_id == evidence.id
        )
    ).all():
        assertion = db.get(MemoryAssertionRecord, assertion_id)
        if assertion is None or assertion.lifecycle_state not in {
            "active",
            "candidate",
            "conflicted",
        }:
            continue
        assertion_prior_state: dict[str, Any] = {
            "lifecycle_state": assertion.lifecycle_state,
            "object_value": assertion.object_value,
        }
        assertion.lifecycle_state = "retracted"
        assertion.object_value = {"text": "[redacted]"}
        assertion.valid_to = now
        assertion.updated_at = now
        projection_invalidation = _delete_projection_rows(
            db, assertion_id=assertion.id, now=now, redaction_posture="redacted"
        )
        _record_deletion(
            db,
            target_id=assertion.id,
            deletion_type="redact",
            actor_id=actor_id,
            reason=reason or "source evidence redacted",
            redaction_posture="redacted",
            projection_invalidation=projection_invalidation,
            now=now,
            new_id_fn=new_id_fn,
        )
        _record_version(
            db,
            table="memory_assertions",
            record_id=assertion.id,
            change_type="redacted",
            actor_id=actor_id,
            reason=reason or "source evidence redacted",
            prior_state=assertion_prior_state,
            new_state={
                "lifecycle_state": "retracted",
                "object_value": {"text": "[redacted]"},
                "redaction_posture": "redacted",
            },
            redaction_posture="redacted",
            projection_invalidation=projection_invalidation,
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {"event_type": "evt.memory.assertion_redacted", "payload": _event_payload(assertion)}
        )
    return events


def set_assertion_priority(
    db: Session,
    *,
    assertion_id: str,
    priority: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state != "active":
        return None
    now = now_fn()
    previous = db.scalar(
        select(MemorySalienceRecord)
        .where(MemorySalienceRecord.assertion_id == assertion.id)
        .limit(1)
    )
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "user_priority": previous.user_priority if previous is not None else None,
    }
    _record_salience(
        db,
        assertion=assertion,
        user_priority=priority,
        now=now,
        new_id_fn=new_id_fn,
    )
    assertion.updated_at = now
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason=f"assertion priority set to {priority}",
        prior_state=prior_state,
        new_state={"lifecycle_state": assertion.lifecycle_state, "user_priority": priority},
        now=now,
        new_id_fn=new_id_fn,
    )
    return serialize_assertion(assertion)


def set_never_remember_rule(
    db: Session,
    *,
    scope_key: str,
    pattern: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    normalized_pattern = _clean_text(pattern, max_chars=500).lower()
    if not normalized_pattern:
        return None
    now = now_fn()
    rule = db.scalar(
        select(MemoryRetentionPolicyRecord)
        .where(
            MemoryRetentionPolicyRecord.scope_key == scope_key,
            MemoryRetentionPolicyRecord.policy_kind == "never_remember",
            MemoryRetentionPolicyRecord.pattern == normalized_pattern,
        )
        .limit(1)
    )
    if rule is None:
        rule = MemoryRetentionPolicyRecord(
            id=new_id_fn("mrp"),
            scope_key=scope_key,
            policy_kind="never_remember",
            pattern=normalized_pattern,
            retention_days=None,
            lifecycle_state="active",
            reason=reason,
            metadata_json={"actor_id": actor_id},
            created_at=now,
            updated_at=now,
        )
        db.add(rule)
        db.flush()
        change_type = "created"
    else:
        rule.lifecycle_state = "active"
        rule.reason = reason
        rule.metadata_json = {"actor_id": actor_id}
        rule.updated_at = now
        change_type = "updated"
    _record_version(
        db,
        table="memory_retention_policies",
        record_id=rule.id,
        change_type=change_type,
        actor_id=actor_id,
        reason=reason or "never-remember rule set",
        new_state={
            "scope_key": rule.scope_key,
            "policy_kind": rule.policy_kind,
            "pattern": rule.pattern,
            "lifecycle_state": rule.lifecycle_state,
        },
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "id": rule.id,
        "scope_key": rule.scope_key,
        "policy_kind": rule.policy_kind,
        "pattern": rule.pattern,
        "state": rule.lifecycle_state,
        "reason": rule.reason,
        "created_at": to_rfc3339(rule.created_at),
        "updated_at": to_rfc3339(rule.updated_at),
    }


def set_memory_scope_binding(
    db: Session,
    *,
    scope_type: str,
    scope_key: str,
    memory_mode: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    normalized_scope_type = _clean_text(scope_type, max_chars=32).lower()
    normalized_scope_key = _clean_text(scope_key, max_chars=200)
    normalized_memory_mode = _clean_text(memory_mode, max_chars=32).lower()
    if normalized_scope_type not in {
        "user",
        "project",
        "repo",
        "session",
        "thread",
        "proactive_case",
    }:
        return None
    if normalized_memory_mode not in {"normal", "temporary", "no_memory"}:
        return None
    if not normalized_scope_key:
        return None
    now = now_fn()
    binding = db.scalar(
        select(MemoryScopeBindingRecord)
        .where(
            MemoryScopeBindingRecord.scope_type == normalized_scope_type,
            MemoryScopeBindingRecord.scope_key == normalized_scope_key,
            MemoryScopeBindingRecord.actor_id == actor_id,
        )
        .limit(1)
    )
    enabled = normalized_memory_mode == "normal"
    metadata = {"source": "memory_scope_mode"}
    if binding is None:
        binding = MemoryScopeBindingRecord(
            id=new_id_fn("msb"),
            scope_type=normalized_scope_type,
            scope_key=normalized_scope_key,
            actor_id=actor_id,
            memory_mode=normalized_memory_mode,
            extraction_enabled=enabled,
            recall_enabled=enabled,
            reason=reason or "memory scope mode updated",
            expires_at=None,
            metadata_json=metadata,
            created_at=now,
            updated_at=now,
        )
        db.add(binding)
        db.flush()
    else:
        binding.memory_mode = normalized_memory_mode
        binding.extraction_enabled = enabled
        binding.recall_enabled = enabled
        binding.reason = reason or "memory scope mode updated"
        binding.expires_at = None
        binding.metadata_json = metadata
        binding.updated_at = now
    return {
        "id": binding.id,
        "scope_type": binding.scope_type,
        "scope_key": binding.scope_key,
        "actor_id": binding.actor_id,
        "memory_mode": binding.memory_mode,
        "extraction_enabled": binding.extraction_enabled,
        "recall_enabled": binding.recall_enabled,
        "reason": binding.reason,
        "expires_at": to_rfc3339(binding.expires_at) if binding.expires_at else None,
        "created_at": to_rfc3339(binding.created_at),
        "updated_at": to_rfc3339(binding.updated_at),
    }


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
    member_ids = [
        row.assertion_id
        for row in db.scalars(
            select(MemoryConflictMemberRecord)
            .where(MemoryConflictMemberRecord.conflict_set_id == conflict.id)
            .order_by(
                MemoryConflictMemberRecord.created_at.asc(), MemoryConflictMemberRecord.id.asc()
            )
        ).all()
    ]
    if assertion.id not in member_ids or assertion.lifecycle_state not in {"active", "conflicted"}:
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
    for losing_id in member_ids:
        if losing_id == assertion.id:
            continue
        losing_assertion = db.get(MemoryAssertionRecord, losing_id)
        if losing_assertion is None:
            continue
        if losing_assertion.lifecycle_state in {
            "active",
            "candidate",
            "conflicted",
            "superseded",
        }:
            losing_assertion.lifecycle_state = "rejected"
            losing_assertion.valid_to = losing_assertion.valid_to or now
            losing_assertion.superseded_by_assertion_id = None
            losing_assertion.updated_at = now
            projection_invalidation = _delete_projection_rows(
                db, assertion_id=losing_assertion.id, now=now
            )
            _record_review(
                db,
                assertion_id=losing_assertion.id,
                decision="rejected",
                actor_id=actor_id,
                reason="conflict resolved in favor of another assertion",
                now=now,
                new_id_fn=new_id_fn,
            )
            _record_version(
                db,
                table="memory_assertions",
                record_id=losing_assertion.id,
                change_type="reviewed",
                actor_id=actor_id,
                reason="conflict loser rejected",
                new_state={"lifecycle_state": "rejected"},
                projection_invalidation=projection_invalidation,
                now=now,
                new_id_fn=new_id_fn,
            )
            events.append(
                {
                    "event_type": "evt.memory.candidate_rejected",
                    "payload": _event_payload(losing_assertion),
                }
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


def edit_candidate(
    db: Session,
    *,
    assertion_id: str,
    value: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "object_value": assertion.object_value,
    }
    assertion.object_value = {"text": _clean_text(value)}
    assertion.updated_at = now
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason="candidate edited",
        prior_state=prior_state,
        new_state={
            "lifecycle_state": assertion.lifecycle_state,
            "object_value": assertion.object_value,
        },
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.candidate_edited", "payload": _event_payload(assertion)}]


def merge_candidates(
    db: Session,
    *,
    assertion_ids: Sequence[str],
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    ids = [assertion_id for assertion_id in assertion_ids if assertion_id]
    if len(ids) < 2:
        return []
    assertions = [db.get(MemoryAssertionRecord, assertion_id) for assertion_id in ids]
    if any(
        assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}
        for assertion in assertions
    ):
        return []
    now = now_fn()
    winner = assertions[0]
    assert winner is not None
    merged_values = [
        _assertion_text(assertion)
        for assertion in assertions
        if assertion is not None and _assertion_text(assertion)
    ]
    prior_state = {"lifecycle_state": winner.lifecycle_state, "object_value": winner.object_value}
    winner.object_value = {"text": _clean_text("; ".join(dict.fromkeys(merged_values)))}
    winner.lifecycle_state = "candidate"
    winner.updated_at = now
    events = [{"event_type": "evt.memory.candidates_merged", "payload": _event_payload(winner)}]
    _record_version(
        db,
        table="memory_assertions",
        record_id=winner.id,
        change_type="updated",
        actor_id=actor_id,
        reason="candidate merge winner updated",
        prior_state=prior_state,
        new_state={"lifecycle_state": winner.lifecycle_state, "object_value": winner.object_value},
        now=now,
        new_id_fn=new_id_fn,
    )
    for loser in assertions[1:]:
        assert loser is not None
        loser_prior_state = {"lifecycle_state": loser.lifecycle_state}
        loser.lifecycle_state = "rejected"
        loser.valid_to = now
        loser.updated_at = now
        projection_invalidation = _delete_projection_rows(db, assertion_id=loser.id, now=now)
        _record_review(
            db,
            assertion_id=loser.id,
            decision="rejected",
            actor_id=actor_id,
            reason=f"merged into {winner.id}",
            now=now,
            new_id_fn=new_id_fn,
        )
        _record_version(
            db,
            table="memory_assertions",
            record_id=loser.id,
            change_type="reviewed",
            actor_id=actor_id,
            reason=f"candidate merged into {winner.id}",
            prior_state=loser_prior_state,
            new_state={"lifecycle_state": "rejected", "merged_into_assertion_id": winner.id},
            projection_invalidation=projection_invalidation,
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {"event_type": "evt.memory.candidate_rejected", "payload": _event_payload(loser)}
        )
    return events


def mark_assertion_stale(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"active", "candidate", "conflicted"}:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "stale"
    assertion.valid_to = assertion.valid_to or now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="needs_operator_review",
        actor_id=actor_id,
        reason=reason or "assertion marked stale",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason=reason or "assertion marked stale",
        prior_state=prior_state,
        new_state={"lifecycle_state": "stale"},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    return [
        {"event_type": "evt.memory.assertion_marked_stale", "payload": _event_payload(assertion)}
    ]


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
    if (
        source is None
        or target is None
        or evidence is None
        or evidence.lifecycle_state != "available"
    ):
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
            source_memory_versions={
                "memory_relationships": {relationship.id: 1},
                "memory_evidence": {evidence.id: 1},
            },
            source_projection_versions={"memory_graph_projections": MEMORY_PROJECTION_VERSION},
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


def consolidate_memory(
    db: Session,
    *,
    scope_key: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    source_session_id: str | None = None,
) -> dict[str, Any]:
    now = now_fn()
    if source_session_id is not None:
        allowed, memory_policy = session_allows_memory_operation(
            db,
            session_id=source_session_id,
            operation="write",
            scope_key=scope_key,
            subject_key=scope_key,
        )
        if not allowed:
            return {
                "scope_key": scope_key,
                "status": "skipped",
                "reason": memory_policy["reason"],
                "memory_policy": memory_policy,
                "selected_source_ids": [],
                "omitted_sources": [],
                "proposed_changes": [],
                "applied_projection_changes": [],
                "rejected_changes": [],
            }
    else:
        allowed, memory_policy = scope_allows_memory_operation(
            db,
            scope_key=scope_key,
            operation="write",
            actor_id=actor_id,
        )
        if not allowed:
            return {
                "scope_key": scope_key,
                "status": "skipped",
                "reason": memory_policy["reason"],
                "memory_policy": memory_policy,
                "selected_source_ids": [],
                "omitted_sources": [],
                "proposed_changes": [],
                "applied_projection_changes": [],
                "rejected_changes": [],
            }

    assertions = db.scalars(
        select(MemoryAssertionRecord)
        .where(MemoryAssertionRecord.lifecycle_state == "active")
        .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
    ).all()
    if scope_key != "global":
        assertions = [
            assertion
            for assertion in assertions
            if assertion.scope_key == scope_key or assertion.subject_key == scope_key
        ]
    selected_assertions = assertions[:24]
    source_assertion_ids = [assertion.id for assertion in selected_assertions]
    source_versions = {
        "memory_assertions": {
            assertion.id: _memory_version_number(
                db, table="memory_assertions", record_id=assertion.id
            )
            for assertion in selected_assertions
        }
    }
    omitted_sources = [
        {"id": assertion.id, "kind": "semantic_assertion", "reason": "outside consolidation budget"}
        for assertion in assertions[24:]
    ]
    proposed_changes: list[dict[str, Any]] = []
    applied_projection_changes: list[dict[str, Any]] = []
    rejected_changes: list[dict[str, Any]] = []

    duplicates: dict[tuple[str, str, str], list[MemoryAssertionRecord]] = {}
    for assertion in selected_assertions:
        if assertion.is_multi_valued:
            continue
        duplicates.setdefault(
            (assertion.subject_key, assertion.predicate, assertion.scope_key), []
        ).append(assertion)
    for duplicate_group in duplicates.values():
        if len(duplicate_group) < 2:
            continue
        for duplicate in duplicate_group[1:]:
            _record_review(
                db,
                assertion_id=duplicate.id,
                decision="needs_operator_review",
                actor_id="system",
                reason="consolidation found duplicate single-valued active memory",
                now=now,
                new_id_fn=new_id_fn,
            )
            proposed_changes.append(
                {
                    "kind": "merge_or_supersede",
                    "assertion_id": duplicate.id,
                    "winner_assertion_id": duplicate_group[0].id,
                    "review_state": "needs_operator_review",
                }
            )

    for assertion in selected_assertions:
        if assertion.valid_to is not None and assertion.valid_to <= now:
            _record_review(
                db,
                assertion_id=assertion.id,
                decision="needs_operator_review",
                actor_id="system",
                reason="consolidation found expired active memory",
                now=now,
                new_id_fn=new_id_fn,
            )
            proposed_changes.append(
                {
                    "kind": "mark_stale",
                    "assertion_id": assertion.id,
                    "review_state": "needs_operator_review",
                }
            )

    topic_family_by_type = {
        "profile": "user-profile",
        "preference": "user-preferences",
        "project_state": "active-projects",
        "decision": "architecture-decisions",
        "commitment": "commitments",
        "procedure": "procedures",
    }
    for assertion_type, family in topic_family_by_type.items():
        family_assertions = [
            assertion
            for assertion in selected_assertions
            if assertion.assertion_type == assertion_type
        ]
        if not family_assertions:
            continue
        topic_key = f"{scope_key}:{family}"
        topic = db.scalar(
            select(MemoryTopicRecord)
            .where(
                MemoryTopicRecord.scope_key == scope_key, MemoryTopicRecord.topic_key == topic_key
            )
            .limit(1)
        )
        summary = "; ".join(_assertion_text(assertion) for assertion in family_assertions[:6])
        if topic is None:
            topic = MemoryTopicRecord(
                id=new_id_fn("mtp"),
                topic_key=topic_key,
                family=family,
                scope_key=scope_key,
                title=family.replace("-", " "),
                summary=_clean_text(summary, max_chars=700),
                lifecycle_state="active",
                projection_version=MEMORY_PROJECTION_VERSION,
                metadata_json={"source": "consolidation"},
                created_at=now,
                updated_at=now,
            )
            db.add(topic)
            db.flush()
        else:
            topic.family = family
            topic.summary = _clean_text(summary, max_chars=700)
            topic.lifecycle_state = "active"
            topic.projection_version = MEMORY_PROJECTION_VERSION
            topic.updated_at = now
        rank = 0
        for assertion in family_assertions[:12]:
            member = db.scalar(
                select(MemoryTopicMemberRecord)
                .where(
                    MemoryTopicMemberRecord.topic_id == topic.id,
                    MemoryTopicMemberRecord.canonical_table == "memory_assertions",
                    MemoryTopicMemberRecord.canonical_id == assertion.id,
                )
                .limit(1)
            )
            if member is None:
                db.add(
                    MemoryTopicMemberRecord(
                        id=new_id_fn("mtm"),
                        topic_id=topic.id,
                        canonical_table="memory_assertions",
                        canonical_id=assertion.id,
                        membership_kind="source",
                        rank=rank,
                        metadata_json={"source": "consolidation"},
                        created_at=now,
                    )
                )
            else:
                member.rank = rank
                member.metadata_json = {"source": "consolidation"}
            rank += 1
        topic_block = db.scalar(
            select(MemoryContextBlockRecord)
            .where(
                MemoryContextBlockRecord.block_type == "topic",
                MemoryContextBlockRecord.scope_key == scope_key,
                MemoryContextBlockRecord.topic_id == topic.id,
                MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
            )
            .limit(1)
        )
        if topic_block is None:
            db.add(
                MemoryContextBlockRecord(
                    id=new_id_fn("mcb"),
                    block_type="topic",
                    scope_key=scope_key,
                    content=topic.summary,
                    topic_id=topic.id,
                    lifecycle_state="active",
                    source_assertion_ids=[assertion.id for assertion in family_assertions[:12]],
                    source_episode_ids=[],
                    source_trace_ids=[],
                    source_action_trace_ids=[],
                    source_procedure_ids=[],
                    source_project_state_snapshot_ids=[],
                    source_memory_versions=source_versions,
                    source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                    projection_version=MEMORY_PROJECTION_VERSION,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            topic_block.content = topic.summary
            topic_block.lifecycle_state = "active"
            topic_block.source_assertion_ids = [
                assertion.id for assertion in family_assertions[:12]
            ]
            topic_block.source_memory_versions = source_versions
            topic_block.source_projection_versions = {
                "memory_context_blocks": MEMORY_PROJECTION_VERSION
            }
            topic_block.updated_at = now
        applied_projection_changes.append(
            {
                "kind": "topic_block",
                "topic_id": topic.id,
                "family": family,
                "source_assertion_ids": [assertion.id for assertion in family_assertions[:12]],
            }
        )

    action_traces = db.scalars(
        select(MemoryActionTraceRecord)
        .where(
            MemoryActionTraceRecord.lifecycle_state == "active",
            MemoryActionTraceRecord.outcome == "succeeded",
        )
        .order_by(MemoryActionTraceRecord.updated_at.desc(), MemoryActionTraceRecord.id.asc())
        .limit(50)
    ).all()
    if scope_key != "global":
        action_traces = [
            trace
            for trace in action_traces
            if trace.scope_key == scope_key or trace.scope_key == f"session:{scope_key}"
        ]
    traces_by_capability: dict[str, list[MemoryActionTraceRecord]] = {}
    for trace in action_traces:
        if trace.capability_id is None:
            continue
        traces_by_capability.setdefault(trace.capability_id, []).append(trace)
    for capability_id, traces in traces_by_capability.items():
        if len(traces) < 2:
            continue
        procedure_key = _memory_key(f"successful {capability_id}")
        procedure = db.scalar(
            select(MemoryProcedureRecord)
            .where(
                MemoryProcedureRecord.procedure_key == procedure_key,
                MemoryProcedureRecord.scope_key == scope_key,
            )
            .limit(1)
        )
        if procedure is not None:
            rejected_changes.append(
                {
                    "kind": "procedure_candidate",
                    "capability_id": capability_id,
                    "reason": "procedure already exists",
                }
            )
            continue
        db.add(
            MemoryProcedureRecord(
                id=new_id_fn("mpr"),
                procedure_key=procedure_key,
                scope_key=scope_key,
                title=f"Successful {capability_id}",
                instruction=(
                    "Review repeated successful action traces before converting this "
                    "candidate into durable procedure memory."
                ),
                lifecycle_state="candidate",
                review_state="needs_operator_review",
                source_assertion_id=None,
                primary_evidence_id=traces[0].primary_evidence_id,
                valid_from=now,
                valid_to=None,
                metadata_json={
                    "source": "consolidation",
                    "capability_id": capability_id,
                    "source_action_trace_ids": [trace.id for trace in traces[:5]],
                },
                created_at=now,
                updated_at=now,
            )
        )
        proposed_changes.append(
            {
                "kind": "procedure_candidate",
                "capability_id": capability_id,
                "source_action_trace_ids": [trace.id for trace in traces[:5]],
                "review_state": "needs_operator_review",
            }
        )

    content = json.dumps(
        {
            "scope_key": scope_key,
            "generated_at": to_rfc3339(now),
            "selected_source_ids": source_assertion_ids,
            "omitted_sources": omitted_sources,
            "proposed_changes": proposed_changes,
            "applied_projection_changes": applied_projection_changes,
            "rejected_changes": rejected_changes,
        },
        sort_keys=True,
    )
    block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "hot_index",
            MemoryContextBlockRecord.scope_key == scope_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if block is None:
        block = MemoryContextBlockRecord(
            id=new_id_fn("mcb"),
            block_type="hot_index",
            scope_key=scope_key,
            content=content,
            topic_id=None,
            lifecycle_state="active",
            source_assertion_ids=source_assertion_ids,
            source_episode_ids=[],
            source_trace_ids=[],
            source_action_trace_ids=[],
            source_procedure_ids=[],
            source_project_state_snapshot_ids=[],
            source_memory_versions=source_versions,
            source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
            projection_version=MEMORY_PROJECTION_VERSION,
            created_at=now,
            updated_at=now,
        )
        db.add(block)
    else:
        block.content = content
        block.lifecycle_state = "active"
        block.source_assertion_ids = source_assertion_ids
        block.source_memory_versions = source_versions
        block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
        block.updated_at = now
    return {
        "scope_key": scope_key,
        "status": "completed",
        "context_block_id": block.id,
        "selected_source_ids": source_assertion_ids,
        "omitted_sources": omitted_sources,
        "proposed_changes": proposed_changes,
        "applied_projection_changes": [
            *applied_projection_changes,
            {
                "kind": "hot_index",
                "context_block_id": block.id,
                "source_assertion_ids": source_assertion_ids,
            },
        ],
        "rejected_changes": rejected_changes,
        "updated_at": to_rfc3339(block.updated_at),
    }


def export_memory(
    db: Session,
    *,
    scope_key: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    source_session_id: str | None = None,
) -> dict[str, Any]:
    now = now_fn()
    if source_session_id is not None:
        allowed, memory_policy = session_allows_memory_operation(
            db,
            session_id=source_session_id,
            operation="recall",
            scope_key=scope_key,
            subject_key=scope_key,
            actor_id=actor_id,
        )
        if not allowed:
            return {
                "id": None,
                "scope_key": scope_key,
                "export_format": "json",
                "status": "skipped",
                "redaction_posture": "redacted",
                "source_counts": {},
                "content": {},
                "memory_policy": memory_policy,
                "created_at": to_rfc3339(now),
            }
    else:
        allowed, memory_policy = scope_allows_memory_operation(
            db,
            scope_key=scope_key,
            operation="recall",
            actor_id=actor_id,
        )
        if not allowed:
            return {
                "id": None,
                "scope_key": scope_key,
                "export_format": "json",
                "status": "skipped",
                "redaction_posture": "redacted",
                "source_counts": {},
                "content": {},
                "memory_policy": memory_policy,
                "created_at": to_rfc3339(now),
            }
    payload = list_memory(db)
    if scope_key != "global":
        for key in ("active_assertions", "candidates"):
            payload[key] = [
                item
                for item in payload[key]
                if item["scope_key"] == scope_key or item["subject_key"] == scope_key
            ]
        payload["conflicts"] = [
            item for item in payload["conflicts"] if item["scope_key"] == scope_key
        ]
        payload["project_state"] = [
            item
            for item in payload["project_state"]
            if f"project:{item['project_key']}" == scope_key
        ]
        for key in ("procedures", "action_traces", "topics", "context_blocks", "scope_bindings"):
            payload[key] = [item for item in payload[key] if item["scope_key"] == scope_key]
        allowed_evidence_ids = {
            ref["evidence_id"]
            for key in ("active_assertions", "candidates")
            for item in payload[key]
            for ref in item.get("evidence_refs", [])
            if isinstance(ref, dict) and isinstance(ref.get("evidence_id"), str)
        }
        allowed_evidence_ids.update(
            evidence_id
            for item in payload["project_state"]
            for evidence_id in item.get("source_evidence_ids", [])
            if isinstance(evidence_id, str)
        )
        allowed_evidence_ids.update(
            item["primary_evidence_id"]
            for item in payload["action_traces"]
            if isinstance(item.get("primary_evidence_id"), str)
        )
        payload["evidence"] = [
            item for item in payload["evidence"] if item["id"] in allowed_evidence_ids
        ]
        payload["deletions"] = []
        payload["retention_policies"] = [
            item for item in payload["retention_policies"] if item["scope_key"] == scope_key
        ]
        payload["sensitivity_labels"] = []
        payload["export_artifacts"] = []
        payload["eval_runs"] = []
    artifact = MemoryExportArtifactRecord(
        id=new_id_fn("mea"),
        scope_key=scope_key,
        export_format="json",
        status="created",
        projection_version=MEMORY_PROJECTION_VERSION,
        redaction_posture="redacted",
        content=payload,
        source_counts={
            "active_assertions": len(payload["active_assertions"]),
            "project_state": len(payload["project_state"]),
            "procedures": len(payload["procedures"]),
        },
        source_memory_versions={
            "memory_assertions": {
                item["id"]: _memory_version_number(
                    db, table="memory_assertions", record_id=item["id"]
                )
                for item in payload["active_assertions"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            },
            "memory_context_blocks": {
                item["id"]: _memory_version_number(
                    db, table="memory_context_blocks", record_id=item["id"]
                )
                for item in payload["context_blocks"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            },
        },
        source_projection_versions={
            "memory_export_artifacts": MEMORY_PROJECTION_VERSION,
            "memory_context_blocks": MEMORY_PROJECTION_VERSION,
        },
        actor_id=actor_id,
        created_at=now,
        updated_at=now,
    )
    db.add(artifact)
    db.flush()
    _record_version(
        db,
        table="memory_export_artifacts",
        record_id=artifact.id,
        change_type="exported",
        actor_id=actor_id,
        reason="memory exported",
        new_state={"scope_key": artifact.scope_key, "source_counts": artifact.source_counts},
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "id": artifact.id,
        "scope_key": artifact.scope_key,
        "export_format": artifact.export_format,
        "status": artifact.status,
        "projection_version": artifact.projection_version,
        "redaction_posture": artifact.redaction_posture,
        "source_counts": artifact.source_counts,
        "source_memory_versions": artifact.source_memory_versions,
        "source_projection_versions": artifact.source_projection_versions,
        "content": artifact.content,
        "created_at": to_rfc3339(artifact.created_at),
    }


def import_memory_candidates(
    db: Session,
    *,
    source_session_id: str,
    actor_id: str,
    candidates: Sequence[dict[str, Any]],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    cutover_enabled: bool = False,
) -> list[str]:
    if not cutover_enabled:
        return []
    imported_ids: list[str] = []
    for item in candidates[:50]:
        evidence_text = item.get("evidence_text")
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        assertion_type = item.get("assertion_type")
        value = item.get("value")
        confidence = item.get("confidence")
        scope_key = item.get("scope_key")
        is_multi_valued = item.get("is_multi_valued")
        if (
            not isinstance(evidence_text, str)
            or not evidence_text.strip()
            or not isinstance(subject_key, str)
            or not subject_key.strip()
            or not isinstance(predicate, str)
            or not predicate.strip()
            or assertion_type not in ALLOWED_MEMORY_ASSERTION_TYPES
            or not isinstance(value, str)
            or not value.strip()
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or float(confidence) < 0.0
            or float(confidence) > 1.0
            or float(confidence) != float(confidence)
            or not isinstance(scope_key, str)
            or not scope_key.strip()
            or not isinstance(is_multi_valued, bool)
        ):
            continue
        evidence_text = evidence_text.strip()
        subject_key = subject_key.strip()
        predicate = predicate.strip()
        value = value.strip()
        scope_key = scope_key.strip()
        existing = db.scalar(
            select(MemoryAssertionRecord)
            .where(
                MemoryAssertionRecord.subject_key == subject_key,
                MemoryAssertionRecord.predicate == predicate,
                MemoryAssertionRecord.assertion_type == assertion_type,
                MemoryAssertionRecord.scope_key == scope_key,
                MemoryAssertionRecord.object_value == {"text": _clean_text(value)},
                MemoryAssertionRecord.lifecycle_state.in_(("candidate", "conflicted", "active")),
            )
            .order_by(MemoryAssertionRecord.created_at.asc(), MemoryAssertionRecord.id.asc())
            .limit(1)
        )
        if existing is not None:
            imported_ids.append(existing.id)
            continue
        events = propose_memory_candidate(
            db,
            source_session_id=source_session_id,
            actor_id=actor_id,
            evidence_text=evidence_text,
            subject_key=subject_key,
            predicate=predicate,
            assertion_type=assertion_type,
            value=value,
            confidence=float(confidence),
            scope_key=scope_key,
            is_multi_valued=is_multi_valued,
            valid_from=None,
            valid_to=None,
            extraction_model=None,
            extraction_prompt_version="memory-import-v1",
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        for event in events:
            payload = event.get("payload")
            if (
                event.get("event_type") == "evt.memory.candidate_proposed"
                and isinstance(payload, dict)
                and isinstance(payload.get("assertion_id"), str)
            ):
                imported_ids.append(payload["assertion_id"])
    return imported_ids


def run_memory_eval(
    db: Session,
    *,
    eval_name: str,
    cases: Sequence[dict[str, Any]],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    settings: AppSettings | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    now = now_fn()
    case_results: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    for index, raw_case in enumerate(cases[:100], start=1):
        query = raw_case.get("query")
        if not isinstance(query, str) or not query.strip():
            case_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "reason": "case missing query",
                    "selected_memory_ids": [],
                    "omitted_memory_count": 0,
                }
            )
            failed_count += 1
            continue
        expected_ids = [
            item for item in raw_case.get("expected_memory_ids", []) if isinstance(item, str)
        ]
        forbidden_ids = [
            item for item in raw_case.get("forbidden_memory_ids", []) if isinstance(item, str)
        ]
        expected_kinds = [
            item for item in raw_case.get("expected_kinds", []) if isinstance(item, str)
        ]
        expected_text = raw_case.get("expected")
        forbidden_texts = [
            item.lower()
            for item in raw_case.get("forbidden_texts", [])
            if isinstance(item, str) and item
        ]
        try:
            memory_context, recall_event = build_memory_context(
                db,
                user_message=query,
                max_recalled_assertions=8,
                settings=settings,
                current_session_id=current_session_id,
            )
        except AIJudgmentFailure as exc:
            case_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "reason": exc.safe_reason,
                    "selected_memory_ids": [],
                    "omitted_memory_count": 0,
                    "parse_status": exc.parse_status,
                    "validation_status": exc.validation_status,
                }
            )
            failed_count += 1
            continue

        recall_window = memory_context["recall_window"]
        selected_memory_ids = [
            item for item in recall_window["selected_memory_ids"] if isinstance(item, str)
        ]
        selected_kinds = [
            item["kind"]
            for item in recall_window["selected_memories"]
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
        ]
        selected_text = json.dumps(
            {
                "hot_index": memory_context["hot_index"],
                "topic_index": memory_context["topic_index"],
                "semantic_assertions": memory_context["semantic_assertions"],
                "project_state": memory_context["project_state"],
                "procedural_memory": memory_context["procedural_memory"],
                "action_traces": memory_context["action_traces"],
            },
            sort_keys=True,
        ).lower()
        failures: list[str] = []
        for expected_id in expected_ids:
            if expected_id not in selected_memory_ids:
                failures.append(f"missing expected memory {expected_id}")
        for forbidden_id in forbidden_ids:
            if forbidden_id in selected_memory_ids:
                failures.append(f"selected forbidden memory {forbidden_id}")
        for expected_kind in expected_kinds:
            if expected_kind not in selected_kinds:
                failures.append(f"missing expected kind {expected_kind}")
        if isinstance(expected_text, str) and expected_text.strip():
            expected_terms = set(_terms(expected_text))
            selected_terms = set(_terms(selected_text))
            if expected_terms and not expected_terms.intersection(selected_terms):
                failures.append("missing expected text signal")
        for forbidden_text in forbidden_texts:
            if forbidden_text in selected_text:
                failures.append("selected forbidden text")
        if (
            bool(raw_case.get("expect_policy_blocked"))
            and memory_context.get("memory_policy") is None
        ):
            failures.append("expected memory policy block")

        if failures:
            status = "failed"
            failed_count += 1
        else:
            status = "passed"
            passed_count += 1
        case_results.append(
            {
                "index": index,
                "status": status,
                "query": _clean_text(query),
                "failures": failures,
                "selected_memory_ids": selected_memory_ids,
                "selected_memory_kinds": selected_kinds,
                "omitted_memory_count": recall_window["omitted_memory_count"],
                "memory_candidate_count": recall_window["memory_candidate_count"],
                "curation_confidence": recall_window["curation_confidence"],
                "memory_policy": memory_context.get("memory_policy"),
                "projection_health": memory_context["projection_health"],
                "recall_diagnostics": recall_event,
            }
        )
    active_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryAssertionRecord)
            .where(MemoryAssertionRecord.lifecycle_state == "active")
        )
        or 0
    )
    candidate_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryAssertionRecord)
            .where(MemoryAssertionRecord.lifecycle_state.in_(("candidate", "conflicted")))
        )
        or 0
    )
    conflict_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryConflictSetRecord)
            .where(MemoryConflictSetRecord.lifecycle_state == "open")
        )
        or 0
    )
    failed_projection_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryProjectionJobRecord)
            .where(MemoryProjectionJobRecord.lifecycle_state.in_(("failed", "dead_letter")))
        )
        or 0
    )
    run = MemoryEvalRunRecord(
        id=new_id_fn("mer"),
        eval_name=_clean_text(eval_name, max_chars=200) or "memory eval",
        status="completed" if failed_count == 0 else "failed",
        metrics={
            "active_assertions": int(active_count),
            "pending_candidates": int(candidate_count),
            "open_conflicts": int(conflict_count),
            "failed_projection_jobs": int(failed_projection_count),
            "case_count": len(cases),
            "passed_cases": passed_count,
            "failed_cases": failed_count,
            "pass_rate": passed_count / len(case_results) if case_results else 1.0,
        },
        cases=case_results,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.flush()
    _record_version(
        db,
        table="memory_eval_runs",
        record_id=run.id,
        change_type="created",
        actor_id="system",
        reason="memory eval completed",
        new_state={"metrics": run.metrics},
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "id": run.id,
        "eval_name": run.eval_name,
        "status": run.status,
        "metrics": run.metrics,
        "cases": run.cases,
        "created_at": to_rfc3339(run.created_at),
        "updated_at": to_rfc3339(run.updated_at),
    }


def retry_projection_job(
    db: Session,
    *,
    job_id: str,
    now_fn: Callable[[], datetime],
) -> dict[str, Any] | None:
    job = db.get(MemoryProjectionJobRecord, job_id)
    if job is None:
        return None
    now = now_fn()
    job.lifecycle_state = "pending"
    job.error = None
    job.run_after = now
    job.updated_at = now
    return {
        "id": job.id,
        "projection_kind": job.projection_kind,
        "target_table": job.target_table,
        "target_id": job.target_id,
        "state": job.lifecycle_state,
        "run_after": to_rfc3339(job.run_after),
    }


def record_rotation_context_block(
    db: Session,
    *,
    rotation_id: str,
    prior_session_id: str,
    new_session_id: str,
    rotation_reason: str,
    prior_turns: Sequence[TurnRecord],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    now = now_fn()
    source_turns = [
        {
            "turn_id": turn.id,
            "user_message": _clean_text(turn.user_message, max_chars=900),
            "assistant_message": _clean_text(turn.assistant_message or "", max_chars=900),
            "status": turn.status,
            "created_at": to_rfc3339(turn.created_at),
        }
        for turn in prior_turns
    ]
    source_turn_ids = [turn["turn_id"] for turn in source_turns]
    input_refs = {
        "rotation_id": rotation_id,
        "rotation_reason": rotation_reason,
        "prior_session_id": prior_session_id,
        "new_session_id": new_session_id,
        "source_turn_ids": source_turn_ids,
    }
    ai_judgment_id = new_id_fn("ajg")
    if source_turns:
        try:
            payload = _curate_rotation_context_with_model(
                rotation_reason=rotation_reason,
                prior_session_id=prior_session_id,
                new_session_id=new_session_id,
                source_turns=source_turns,
                settings=settings,
            )
        except AIJudgmentFailure as exc:
            db.add(
                AIJudgmentRecord(
                    id=ai_judgment_id,
                    judgment_type="continuity_compaction",
                    source_type="session_rotation",
                    source_id=rotation_id,
                    status="failed",
                    model=settings.model_name,
                    prompt_version=MEMORY_CONTINUITY_PROMPT_VERSION,
                    provider_response_id=exc.provider_response_id,
                    input_summary="session-rotation continuity compaction",
                    input_refs=input_refs,
                    selected=[],
                    omitted=[],
                    output={},
                    rationale=None,
                    uncertainty=None,
                    confidence=None,
                    parse_status=exc.parse_status,
                    validation_status=exc.validation_status,
                    failure_code=exc.code,
                    failure_reason=exc.safe_reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            raise
        except Exception as exc:
            failure = AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason=f"continuity curation failed: {exc.__class__.__name__}",
                retryable=True,
                parse_status="missing_output",
                validation_status="not_validated",
            )
            db.add(
                AIJudgmentRecord(
                    id=ai_judgment_id,
                    judgment_type="continuity_compaction",
                    source_type="session_rotation",
                    source_id=rotation_id,
                    status="failed",
                    model=settings.model_name,
                    prompt_version=MEMORY_CONTINUITY_PROMPT_VERSION,
                    provider_response_id=None,
                    input_summary="session-rotation continuity compaction",
                    input_refs=input_refs,
                    selected=[],
                    omitted=[],
                    output={},
                    rationale=None,
                    uncertainty=None,
                    confidence=None,
                    parse_status=failure.parse_status,
                    validation_status=failure.validation_status,
                    failure_code=failure.code,
                    failure_reason=failure.safe_reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            raise failure from exc
        summary = _clean_text(str(payload["summary"]), max_chars=2000)
        decisions = payload.get("decisions")
        open_loops = payload.get("open_loops")
        unresolved_uncertainty = payload.get("unresolved_uncertainty")
        important_omissions = payload.get("important_omissions")
        preserved_turn_refs = payload.get("preserved_turn_refs")
        omitted_turn_refs = payload.get("omitted_turn_refs")
        user_commitments = payload.get("user_commitments")
        assistant_commitments = payload.get("assistant_commitments")
        tool_action_outcomes = payload.get("tool_action_outcomes")
        confidence = payload.get("confidence")
        model = payload.get("model")
        parse_status = payload.get("parse_status")
        validation_status = payload.get("validation_status")
        provider_response_id = payload.get("provider_response_id")
    else:
        summary = "No source turns required continuity curation."
        decisions = []
        open_loops = []
        unresolved_uncertainty = []
        important_omissions = []
        preserved_turn_refs = []
        omitted_turn_refs = []
        user_commitments = []
        assistant_commitments = []
        tool_action_outcomes = []
        confidence = 1.0
        model = None
        parse_status = "not_required_no_candidates"
        validation_status = "not_validated"
        provider_response_id = None
    db.add(
        AIJudgmentRecord(
            id=ai_judgment_id,
            judgment_type="continuity_compaction",
            source_type="session_rotation",
            source_id=rotation_id,
            status="succeeded",
            model=model
            if isinstance(model, str)
            else settings.model_name
            if source_turns
            else None,
            prompt_version=MEMORY_CONTINUITY_PROMPT_VERSION,
            provider_response_id=provider_response_id
            if isinstance(provider_response_id, str)
            else None,
            input_summary="session-rotation continuity compaction",
            input_refs=input_refs,
            selected=preserved_turn_refs if isinstance(preserved_turn_refs, list) else [],
            omitted=omitted_turn_refs if isinstance(omitted_turn_refs, list) else [],
            output={
                "continuity_compaction": {
                    "summary": summary,
                    "source_turn_ids": source_turn_ids,
                    "preserved_turn_refs": preserved_turn_refs
                    if isinstance(preserved_turn_refs, list)
                    else [],
                    "omitted_turn_refs": omitted_turn_refs
                    if isinstance(omitted_turn_refs, list)
                    else [],
                    "provider_response_id": provider_response_id
                    if isinstance(provider_response_id, str)
                    else None,
                    "decisions": decisions if isinstance(decisions, list) else [],
                    "open_loops": open_loops if isinstance(open_loops, list) else [],
                    "unresolved_uncertainty": unresolved_uncertainty
                    if isinstance(unresolved_uncertainty, list)
                    else [],
                    "important_omissions": important_omissions
                    if isinstance(important_omissions, list)
                    else [],
                }
            },
            rationale=summary if source_turns else None,
            uncertainty=None,
            confidence=max(0.0, min(float(confidence), 1.0))
            if isinstance(confidence, int | float)
            else None,
            parse_status=parse_status if isinstance(parse_status, str) else "parsed",
            validation_status="valid" if source_turns else "not_validated",
            failure_code=None,
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        ProjectStateSnapshotRecord(
            id=new_id_fn("pss"),
            project_key="session_continuity",
            summary=summary,
            state={
                "ai_judgment_id": ai_judgment_id,
                "rotation_id": rotation_id,
                "rotation_reason": rotation_reason,
                "prior_session_id": prior_session_id,
                "new_session_id": new_session_id,
                "provider_response_id": provider_response_id
                if isinstance(provider_response_id, str)
                else None,
                "source_turn_ids": source_turn_ids,
                "preserved_turn_refs": preserved_turn_refs
                if isinstance(preserved_turn_refs, list)
                else [],
                "omitted_turn_refs": omitted_turn_refs
                if isinstance(omitted_turn_refs, list)
                else [],
                "user_commitments": user_commitments if isinstance(user_commitments, list) else [],
                "assistant_commitments": assistant_commitments
                if isinstance(assistant_commitments, list)
                else [],
                "decisions": decisions if isinstance(decisions, list) else [],
                "open_loops": open_loops if isinstance(open_loops, list) else [],
                "unresolved_uncertainty": unresolved_uncertainty
                if isinstance(unresolved_uncertainty, list)
                else [],
                "tool_action_outcomes": tool_action_outcomes
                if isinstance(tool_action_outcomes, list)
                else [],
                "important_omissions": important_omissions
                if isinstance(important_omissions, list)
                else [],
                "confidence": max(0.0, min(float(confidence), 1.0))
                if isinstance(confidence, int | float)
                else None,
                "model": model
                if isinstance(model, str)
                else settings.model_name
                if source_turns
                else None,
                "prompt_version": MEMORY_CONTINUITY_PROMPT_VERSION,
                "parse_status": parse_status if isinstance(parse_status, str) else "parsed",
                "validation_status": validation_status
                if isinstance(validation_status, str)
                else "valid",
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


def _curate_rotation_context_with_model(
    *,
    rotation_reason: str,
    prior_session_id: str,
    new_session_id: str,
    source_turns: Sequence[dict[str, Any]],
    settings: AppSettings,
) -> dict[str, Any]:
    if settings.openai_api_key is None:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_CREDENTIALS",
            safe_reason="AI continuity curation requires ARIEL_OPENAI_API_KEY",
            retryable=False,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
            json={
                "model": settings.model_name,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You are Ariel's continuity curator. Summarize the closed session for "
                            "future turns. Return JSON only with summary, preserved_turn_refs, "
                            "omitted_turn_refs, user_commitments, assistant_commitments, decisions, "
                            "open_loops, tool_action_outcomes, unresolved_uncertainty, "
                            "important_omissions, and confidence. Every source turn must appear "
                            "exactly once in preserved_turn_refs or omitted_turn_refs with a reason. "
                            "Do not answer the user."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "prompt_version": MEMORY_CONTINUITY_PROMPT_VERSION,
                                "rotation_reason": rotation_reason,
                                "prior_session_id": prior_session_id,
                                "new_session_id": new_session_id,
                                "source_turns": list(source_turns),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                "store": False,
                "text": {"verbosity": "low"},
            },
            timeout=settings.model_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_TIMEOUT",
            safe_reason="continuity curation model timed out",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    except httpx.HTTPError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="continuity curation model network request failed",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    if response.status_code >= 400:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason=f"continuity curation model returned HTTP {response.status_code}",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="continuity curation provider returned invalid JSON",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    provider_response_id = response_payload.get("id")
    provider_response_id = provider_response_id if isinstance(provider_response_id, str) else None
    try:
        payload = json.loads(_extract_output_text(response_payload.get("output")))
    except json.JSONDecodeError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_INVALID_JSON",
            safe_reason="continuity curation model returned malformed JSON",
            retryable=False,
            parse_status="invalid_json",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        ) from exc
    return validate_continuity_compaction_payload(
        payload,
        source_turn_ids=[
            turn["turn_id"]
            for turn in source_turns
            if isinstance(turn, dict) and isinstance(turn.get("turn_id"), str)
        ],
        model=settings.model_name,
        provider_response_id=provider_response_id,
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
) -> dict[str, Any]:
    return {
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


def validate_continuity_compaction_payload(
    raw_payload: Any,
    *,
    source_turn_ids: Sequence[str],
    model: str | None,
    provider_response_id: str | None,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="continuity compaction model returned a non-object JSON value",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )

    required_lists = (
        "preserved_turn_refs",
        "omitted_turn_refs",
        "user_commitments",
        "assistant_commitments",
        "decisions",
        "open_loops",
        "tool_action_outcomes",
        "unresolved_uncertainty",
        "important_omissions",
    )
    summary = raw_payload.get("summary")
    confidence = raw_payload.get("confidence")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(confidence, int | float)
        or any(not isinstance(raw_payload.get(key), list) for key in required_lists)
    ):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="continuity compaction JSON missing required fields",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )
    if float(confidence) < 0.0 or float(confidence) > 1.0:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="continuity compaction confidence must be between 0 and 1",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )

    known_turn_ids = set(source_turn_ids)
    seen_turn_ids: set[str] = set()
    preserved_turn_refs: list[dict[str, str]] = []
    omitted_turn_refs: list[dict[str, str]] = []
    for key, target in (
        ("preserved_turn_refs", preserved_turn_refs),
        ("omitted_turn_refs", omitted_turn_refs),
    ):
        for ref in raw_payload[key]:
            if (
                not isinstance(ref, dict)
                or not isinstance(ref.get("turn_id"), str)
                or not isinstance(ref.get("reason"), str)
                or not ref["reason"].strip()
            ):
                raise AIJudgmentFailure(
                    code="E_AI_JUDGMENT_SCHEMA",
                    safe_reason=f"continuity compaction {key} entries need turn_id and reason",
                    retryable=False,
                    parse_status="schema_invalid",
                    validation_status="invalid",
                    provider_response_id=provider_response_id,
                )
            turn_id = ref["turn_id"]
            if turn_id not in known_turn_ids:
                raise AIJudgmentFailure(
                    code="E_AI_JUDGMENT_VALIDATION",
                    safe_reason="continuity compaction referenced an unknown source turn",
                    retryable=False,
                    parse_status="parsed",
                    validation_status="invalid",
                    provider_response_id=provider_response_id,
                )
            if turn_id in seen_turn_ids:
                raise AIJudgmentFailure(
                    code="E_AI_JUDGMENT_VALIDATION",
                    safe_reason="continuity compaction duplicated a source turn reference",
                    retryable=False,
                    parse_status="parsed",
                    validation_status="invalid",
                    provider_response_id=provider_response_id,
                )
            seen_turn_ids.add(turn_id)
            target.append({"turn_id": turn_id, "reason": _clean_text(ref["reason"], max_chars=500)})

    if seen_turn_ids != known_turn_ids:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="continuity compaction must account for every source turn",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )

    payload = {
        "summary": _clean_text(summary, max_chars=2000),
        "source_turn_ids": list(source_turn_ids),
        "preserved_turn_refs": preserved_turn_refs,
        "omitted_turn_refs": omitted_turn_refs,
        "user_commitments": list(raw_payload["user_commitments"]),
        "assistant_commitments": list(raw_payload["assistant_commitments"]),
        "decisions": list(raw_payload["decisions"]),
        "open_loops": list(raw_payload["open_loops"]),
        "tool_action_outcomes": list(raw_payload["tool_action_outcomes"]),
        "unresolved_uncertainty": list(raw_payload["unresolved_uncertainty"]),
        "important_omissions": list(raw_payload["important_omissions"]),
        "confidence": float(confidence),
        "model": model,
        "prompt_version": MEMORY_CONTINUITY_PROMPT_VERSION,
        "provider_response_id": provider_response_id,
        "parse_status": "parsed",
        "validation_status": "valid",
    }
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
    action_traces = db.scalars(
        select(MemoryActionTraceRecord)
        .where(MemoryActionTraceRecord.lifecycle_state == "active")
        .order_by(MemoryActionTraceRecord.updated_at.desc(), MemoryActionTraceRecord.id.asc())
        .limit(50)
    ).all()
    topics = db.scalars(
        select(MemoryTopicRecord)
        .where(MemoryTopicRecord.lifecycle_state == "active")
        .order_by(MemoryTopicRecord.updated_at.desc(), MemoryTopicRecord.id.asc())
        .limit(50)
    ).all()
    context_blocks = db.scalars(
        select(MemoryContextBlockRecord)
        .where(MemoryContextBlockRecord.lifecycle_state == "active")
        .order_by(MemoryContextBlockRecord.updated_at.desc(), MemoryContextBlockRecord.id.asc())
        .limit(50)
    ).all()
    deletions = db.scalars(
        select(MemoryDeletionRecord)
        .order_by(MemoryDeletionRecord.created_at.desc(), MemoryDeletionRecord.id.desc())
        .limit(50)
    ).all()
    scope_bindings = db.scalars(
        select(MemoryScopeBindingRecord)
        .order_by(MemoryScopeBindingRecord.updated_at.desc(), MemoryScopeBindingRecord.id.asc())
        .limit(50)
    ).all()
    retention_policies = db.scalars(
        select(MemoryRetentionPolicyRecord)
        .order_by(
            MemoryRetentionPolicyRecord.updated_at.desc(), MemoryRetentionPolicyRecord.id.asc()
        )
        .limit(50)
    ).all()
    sensitivity_labels = db.scalars(
        select(MemorySensitivityLabelRecord)
        .where(MemorySensitivityLabelRecord.lifecycle_state == "active")
        .order_by(
            MemorySensitivityLabelRecord.updated_at.desc(), MemorySensitivityLabelRecord.id.asc()
        )
        .limit(50)
    ).all()
    export_artifacts = db.scalars(
        select(MemoryExportArtifactRecord)
        .order_by(
            MemoryExportArtifactRecord.created_at.desc(), MemoryExportArtifactRecord.id.desc()
        )
        .limit(20)
    ).all()
    eval_runs = db.scalars(
        select(MemoryEvalRunRecord)
        .order_by(MemoryEvalRunRecord.created_at.desc(), MemoryEvalRunRecord.id.desc())
        .limit(20)
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
                "state": _redact_json_value(snapshot.state),
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
        "action_traces": [
            {
                "id": trace.id,
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "action_attempt_id": trace.action_attempt_id,
                "source_turn_id": trace.source_turn_id,
                "capability_id": trace.capability_id,
                "summary": redact_text(trace.summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "result_refs": trace.result_refs,
                "created_at": to_rfc3339(trace.created_at),
                "updated_at": to_rfc3339(trace.updated_at),
            }
            for trace in action_traces
        ],
        "topics": [
            {
                "id": topic.id,
                "topic_key": topic.topic_key,
                "family": topic.family,
                "scope_key": topic.scope_key,
                "title": topic.title,
                "summary": redact_text(topic.summary),
                "state": topic.lifecycle_state,
                "projection_version": topic.projection_version,
                "created_at": to_rfc3339(topic.created_at),
                "updated_at": to_rfc3339(topic.updated_at),
            }
            for topic in topics
        ],
        "context_blocks": [
            {
                "id": block.id,
                "block_type": block.block_type,
                "scope_key": block.scope_key,
                "topic_id": block.topic_id,
                "content": redact_text(block.content),
                "state": block.lifecycle_state,
                "source_assertion_ids": block.source_assertion_ids,
                "source_episode_ids": block.source_episode_ids,
                "source_trace_ids": block.source_trace_ids,
                "source_action_trace_ids": block.source_action_trace_ids,
                "source_procedure_ids": block.source_procedure_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "source_memory_versions": block.source_memory_versions,
                "source_projection_versions": block.source_projection_versions,
                "projection_version": block.projection_version,
                "created_at": to_rfc3339(block.created_at),
                "updated_at": to_rfc3339(block.updated_at),
            }
            for block in context_blocks
        ],
        "deletions": [
            {
                "id": deletion.id,
                "target_table": deletion.target_table,
                "target_id": deletion.target_id,
                "deletion_type": deletion.deletion_type,
                "actor_id": deletion.actor_id,
                "reason": deletion.reason,
                "redaction_posture": deletion.redaction_posture,
                "projection_invalidation": deletion.projection_invalidation,
                "created_at": to_rfc3339(deletion.created_at),
            }
            for deletion in deletions
        ],
        "scope_bindings": [
            {
                "id": binding.id,
                "scope_type": binding.scope_type,
                "scope_key": binding.scope_key,
                "actor_id": binding.actor_id,
                "memory_mode": binding.memory_mode,
                "extraction_enabled": binding.extraction_enabled,
                "recall_enabled": binding.recall_enabled,
                "reason": binding.reason,
                "expires_at": to_rfc3339(binding.expires_at) if binding.expires_at else None,
                "created_at": to_rfc3339(binding.created_at),
                "updated_at": to_rfc3339(binding.updated_at),
            }
            for binding in scope_bindings
        ],
        "retention_policies": [
            {
                "id": policy.id,
                "scope_key": policy.scope_key,
                "policy_kind": policy.policy_kind,
                "pattern": policy.pattern,
                "retention_days": policy.retention_days,
                "state": policy.lifecycle_state,
                "reason": policy.reason,
                "created_at": to_rfc3339(policy.created_at),
                "updated_at": to_rfc3339(policy.updated_at),
            }
            for policy in retention_policies
        ],
        "sensitivity_labels": [
            {
                "id": label.id,
                "canonical_table": label.canonical_table,
                "canonical_id": label.canonical_id,
                "label": label.label,
                "actor_id": label.actor_id,
                "state": label.lifecycle_state,
                "reason": label.reason,
                "created_at": to_rfc3339(label.created_at),
                "updated_at": to_rfc3339(label.updated_at),
            }
            for label in sensitivity_labels
        ],
        "export_artifacts": [
            {
                "id": artifact.id,
                "scope_key": artifact.scope_key,
                "export_format": artifact.export_format,
                "status": artifact.status,
                "projection_version": artifact.projection_version,
                "redaction_posture": artifact.redaction_posture,
                "source_counts": artifact.source_counts,
                "source_memory_versions": artifact.source_memory_versions,
                "source_projection_versions": artifact.source_projection_versions,
                "created_at": to_rfc3339(artifact.created_at),
                "updated_at": to_rfc3339(artifact.updated_at),
            }
            for artifact in export_artifacts
        ],
        "eval_runs": [
            {
                "id": run.id,
                "eval_name": run.eval_name,
                "status": run.status,
                "metrics": run.metrics,
                "created_at": to_rfc3339(run.created_at),
                "updated_at": to_rfc3339(run.updated_at),
            }
            for run in eval_runs
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
            "running_jobs": db.scalar(
                select(func.count())
                .select_from(MemoryProjectionJobRecord)
                .where(MemoryProjectionJobRecord.lifecycle_state == "running")
            )
            or 0,
        },
    }


def _validated_memory_curation(
    raw_payload: Any,
    *,
    candidate_ids: set[str],
    candidate_kinds: dict[str, str],
    max_selected: int,
    model: str,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="memory curation model returned a non-object JSON value",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
        )

    selected_raw = raw_payload.get("selected_memories")
    omitted_raw = raw_payload.get("omitted_memories")
    rationale = raw_payload.get("rationale")
    uncertainty = raw_payload.get("uncertainty")
    confidence = raw_payload.get("confidence")
    if not (
        isinstance(selected_raw, list)
        and isinstance(omitted_raw, list)
        and isinstance(rationale, str)
        and isinstance(uncertainty, str)
        and isinstance(confidence, int | float)
    ):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="memory curation JSON missing required fields",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
        )

    selected: list[dict[str, str]] = []
    selected_ids: list[str] = []
    for item in selected_raw:
        if not isinstance(item, dict):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation selected_memories entries must be objects",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        memory_id = item.get("id")
        item_rationale = item.get("rationale")
        if not isinstance(memory_id, str) or not isinstance(item_rationale, str):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation selected memory missing id or rationale",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        if memory_id not in candidate_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation selected an unknown memory id",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        if memory_id in selected_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation selected duplicate memory ids",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        selected_ids.append(memory_id)
        selected.append(
            {
                "id": memory_id,
                "kind": candidate_kinds[memory_id],
                "rationale": _clean_text(item_rationale, max_chars=300),
            }
        )

    if len(selected) > max_selected:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="memory curation selected too many memories",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )

    omitted: list[dict[str, str]] = []
    omitted_ids: list[str] = []
    for item in omitted_raw:
        if not isinstance(item, dict):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation omitted_memories entries must be objects",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        memory_id = item.get("id")
        reason = item.get("reason")
        if not isinstance(memory_id, str) or not isinstance(reason, str):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation omitted memory missing id or reason",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        if memory_id not in candidate_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation omitted an unknown memory id",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        if memory_id in omitted_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation omitted duplicate memory ids",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        omitted_ids.append(memory_id)
        omitted.append(
            {"id": memory_id, "kind": candidate_kinds[memory_id], "reason": _clean_text(reason)}
        )

    if set(selected_ids) | set(omitted_ids) != candidate_ids:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="memory curation must account for every memory candidate",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )
    if set(selected_ids).intersection(omitted_ids):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="memory curation cannot select and omit the same memory",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )

    return {
        "selected_memories": selected,
        "omitted_memories": omitted,
        "rationale": _clean_text(rationale, max_chars=700),
        "uncertainty": _clean_text(uncertainty, max_chars=500),
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "model": model,
        "prompt_version": MEMORY_CURATION_PROMPT_VERSION,
        "parse_status": "parsed",
    }


def _curate_memory_context_with_model(
    *,
    user_message: str,
    history: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    max_selected: int,
    settings: AppSettings,
) -> dict[str, Any]:
    if settings.openai_api_key is None:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_CREDENTIALS",
            safe_reason="AI memory curation requires ARIEL_OPENAI_API_KEY",
            retryable=False,
            parse_status="missing_output",
            validation_status="not_validated",
        )

    prompt = (
        "You are Ariel's memory curator. Select only memories that matter for the "
        "current turn. Use the user request, recent history, memory values, evidence, "
        "validity, conflicts, and provenance. Do not select memories just because they "
        "appear first. Return JSON only with selected_memories, omitted_memories, "
        "rationale, uncertainty, and confidence. selected_memories must contain objects "
        "with id and rationale. omitted_memories must contain every unselected candidate "
        "with id and reason. Select at most the provided max_selected."
    )
    try:
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
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "prompt_version": MEMORY_CURATION_PROMPT_VERSION,
                                "max_selected": max_selected,
                                "user_request": user_message,
                                "recent_history": list(history),
                                "memory_candidates": list(candidates),
                            }
                        ),
                    },
                ],
                "store": False,
                "text": {"verbosity": "low"},
            },
            timeout=settings.model_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_TIMEOUT",
            safe_reason="memory curation model timed out",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    except httpx.HTTPError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="memory curation model network request failed",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    if response.status_code >= 400:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason=f"memory curation model returned HTTP {response.status_code}",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="memory curation provider returned invalid JSON",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    provider_response_id = response_payload.get("id")
    provider_response_id = provider_response_id if isinstance(provider_response_id, str) else None
    text = _extract_output_text(response_payload.get("output"))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_INVALID_JSON",
            safe_reason="memory curation model returned malformed JSON",
            retryable=False,
            parse_status="invalid_json",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        ) from exc
    try:
        curation = _validated_memory_curation(
            payload,
            candidate_ids={str(candidate["id"]) for candidate in candidates},
            candidate_kinds={
                str(candidate["id"]): str(candidate.get("kind") or "memory")
                for candidate in candidates
            },
            max_selected=max_selected,
            model=settings.model_name,
        )
    except AIJudgmentFailure as exc:
        exc.provider_response_id = provider_response_id
        raise
    curation["provider_response_id"] = provider_response_id
    return curation


def search_memory(
    db: Session,
    *,
    query: str,
    limit: int,
    settings: AppSettings | None = None,
    current_session_id: str | None = None,
    scope_key: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    actor_id: str | None = None,
) -> list[dict[str, Any]]:
    memory_context, _ = build_memory_context(
        db,
        user_message=query,
        max_recalled_assertions=limit,
        settings=settings,
        current_session_id=current_session_id,
        scope_key=scope_key,
        thread_id=thread_id,
        proactive_case_id=proactive_case_id,
        actor_id=actor_id,
    )
    recall_window = memory_context.get("recall_window")
    if not isinstance(recall_window, dict):
        return []
    selected_rationales = {
        item["id"]: item.get("rationale")
        for item in recall_window.get("selected_memories", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    results = []
    for item in recall_window.get("candidate_memories", []):
        if isinstance(item, dict) and item.get("id") in selected_rationales:
            memory_id = item.get("id")
            kind = item.get("kind")
            if isinstance(memory_id, str) and isinstance(kind, str):
                results.append(
                    {
                        "id": memory_id,
                        "kind": kind,
                        "rationale": selected_rationales[memory_id],
                        "value": item.get("value") or item.get("summary") or item.get("content"),
                        "evidence_refs": item.get("evidence_refs", []),
                        "retrieval_features": item.get("retrieval_features", {}),
                        "conflict_status": item.get("conflict_status"),
                        "projection_version": item.get(
                            "projection_version", MEMORY_PROJECTION_VERSION
                        ),
                    }
                )
    return results[:limit]


def build_memory_context(
    db: Session,
    *,
    user_message: str,
    max_recalled_assertions: int,
    settings: AppSettings | None = None,
    current_session_id: str | None = None,
    scope_key: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    actor_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_settings = settings or AppSettings()
    context: dict[str, Any]
    event_payload: dict[str, Any]
    memory_policy: dict[str, Any] | None = None
    query_terms = set(_terms(user_message))
    effective_scope_key = scope_key
    if effective_scope_key is None and query_terms:
        effective_scope_key = _matching_scope_key_for_text(db, text=user_message)
    scope_aliases: set[str] = set()
    if effective_scope_key is not None and effective_scope_key != "global":
        scope_aliases.add(effective_scope_key)
        if effective_scope_key.startswith("project:"):
            scope_aliases.add(effective_scope_key.removeprefix("project:"))
        elif effective_scope_key.startswith("repo:"):
            scope_aliases.add(effective_scope_key.removeprefix("repo:"))
        else:
            scope_aliases.add(f"project:{effective_scope_key}")
            scope_aliases.add(f"repo:{effective_scope_key}")
    if current_session_id is not None:
        allowed, memory_policy = session_allows_memory_operation(
            db,
            session_id=current_session_id,
            operation="recall",
            subject_key=effective_scope_key,
            scope_key=effective_scope_key,
            thread_id=thread_id,
            proactive_case_id=proactive_case_id,
            actor_id=actor_id,
        )
        if not allowed:
            context = {
                "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "hot_index": [],
                "topic_index": [],
                "pinned_core": [],
                "project_state": [],
                "commitments_and_decisions": [],
                "semantic_assertions": [],
                "episodic_evidence": [],
                "procedural_memory": [],
                "action_traces": [],
                "conflicts": [],
                "recall_window": {
                    "max_selected_memories": max_recalled_assertions,
                    "selected_memory_count": 0,
                    "memory_candidate_count": 0,
                    "omitted_memory_count": 0,
                    "selected_memory_ids": [],
                    "selected_memories": [],
                    "omitted_memories": [],
                    "candidate_memory_ids": [],
                    "candidate_memories": [],
                    "curation_rationale": (f"Memory recall skipped: {memory_policy['reason']}."),
                    "curation_uncertainty": "",
                    "curation_confidence": 1.0,
                    "curation_model": None,
                    "curation_prompt_version": MEMORY_CURATION_PROMPT_VERSION,
                    "curation_parse_status": "not_required_no_candidates",
                    "curation_provider_response_id": None,
                },
                "memory_policy": memory_policy,
                "projection_health": {
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "selected_assertion_count": 0,
                    "selected_memory_count": 0,
                },
            }
            event_payload = {
                "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
                "projection_version": MEMORY_PROJECTION_VERSION,
                **context["recall_window"],
                "conflict_ids": [],
                "memory_policy": memory_policy,
            }
            return context, event_payload
    candidate_limit = max(50, max_recalled_assertions * 8)
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

    candidate_ids: set[str] = set()
    vector_distance_by_assertion_id: dict[str, float] = {}
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
            .limit(candidate_limit)
        ).all()
        for assertion_id, distance in vector_rows:
            if distance is not None:
                candidate_ids.add(assertion_id)
                vector_distance_by_assertion_id[assertion_id] = float(distance)

    keywords = {
        row.canonical_id: row
        for row in db.scalars(
            select(MemoryKeywordProjectionRecord).where(
                MemoryKeywordProjectionRecord.canonical_table == "memory_assertions",
                MemoryKeywordProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            )
        ).all()
    }
    keyword_terms_by_assertion_id: dict[str, list[str]] = {}
    for assertion_id, keyword in keywords.items():
        matched_terms = sorted(query_terms.intersection(set(keyword.weighted_terms)))
        if matched_terms:
            candidate_ids.add(assertion_id)
            keyword_terms_by_assertion_id[assertion_id] = matched_terms
    entity_scope = matching_entity_ids | graph_neighbors
    entity_ids_by_assertion_id: dict[str, list[str]] = {}
    if entity_scope:
        for row in db.scalars(
            select(MemoryEntityProjectionRecord).where(
                MemoryEntityProjectionRecord.canonical_table == "memory_assertions",
                MemoryEntityProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                MemoryEntityProjectionRecord.entity_id.in_(entity_scope),
            )
        ).all():
            candidate_ids.add(row.canonical_id)
            entity_ids_by_assertion_id.setdefault(row.canonical_id, []).append(row.entity_id)
    symbol_features_by_assertion_id: dict[str, list[dict[str, str | None]]] = {}
    for symbol_projection in db.scalars(
        select(MemorySymbolProjectionRecord).where(
            MemorySymbolProjectionRecord.canonical_table == "memory_assertions",
            MemorySymbolProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
    ).all():
        if not query_terms.intersection(
            set(
                _terms(
                    f"{symbol_projection.repo_key} "
                    f"{symbol_projection.symbol} "
                    f"{symbol_projection.path}"
                )
            )
        ):
            continue
        candidate_ids.add(symbol_projection.canonical_id)
        symbol_features_by_assertion_id.setdefault(symbol_projection.canonical_id, []).append(
            {
                "repo_key": symbol_projection.repo_key,
                "symbol": symbol_projection.symbol,
                "path": symbol_projection.path,
                "language": symbol_projection.language,
            }
        )

    candidate_assertions: list[MemoryAssertionRecord] = []
    if candidate_ids:
        candidate_assertions.extend(
            db.scalars(
                select(MemoryAssertionRecord)
                .where(
                    MemoryAssertionRecord.lifecycle_state == "active",
                    MemoryAssertionRecord.id.in_(candidate_ids),
                )
                .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
                .limit(candidate_limit)
            ).all()
        )
    if scope_aliases:
        candidate_assertions = [
            assertion
            for assertion in candidate_assertions
            if assertion.scope_key in scope_aliases or assertion.subject_key in scope_aliases
        ]
    candidate_ids = {assertion.id for assertion in candidate_assertions}
    salience_by_assertion_id = {
        row.assertion_id: {"user_priority": row.user_priority, "score": row.score}
        for row in db.scalars(
            select(MemorySalienceRecord).where(MemorySalienceRecord.assertion_id.in_(candidate_ids))
        ).all()
    }
    temporal_by_assertion_id = {
        row.canonical_id: {
            "temporal_kind": row.temporal_kind,
            "valid_from": to_rfc3339(row.valid_from) if row.valid_from else None,
            "valid_to": to_rfc3339(row.valid_to) if row.valid_to else None,
        }
        for row in db.scalars(
            select(MemoryTemporalProjectionRecord).where(
                MemoryTemporalProjectionRecord.canonical_table == "memory_assertions",
                MemoryTemporalProjectionRecord.canonical_id.in_(candidate_ids),
                MemoryTemporalProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            )
        ).all()
    }
    evidence_refs = _evidence_refs_by_assertion(
        db, [assertion.id for assertion in candidate_assertions]
    )
    open_conflict_ids_by_assertion_id: dict[str, list[str]] = {}
    if candidate_assertions:
        for conflict_id, assertion_id in db.execute(
            select(
                MemoryConflictMemberRecord.conflict_set_id, MemoryConflictMemberRecord.assertion_id
            )
            .join(
                MemoryConflictSetRecord,
                MemoryConflictSetRecord.id == MemoryConflictMemberRecord.conflict_set_id,
            )
            .where(
                MemoryConflictSetRecord.lifecycle_state == "open",
                MemoryConflictMemberRecord.assertion_id.in_(
                    [assertion.id for assertion in candidate_assertions]
                ),
            )
        ).all():
            open_conflict_ids_by_assertion_id.setdefault(assertion_id, []).append(conflict_id)
    candidate_payloads: list[dict[str, Any]] = []
    for assertion in candidate_assertions:
        refs = evidence_refs.get(assertion.id, [])
        candidate_payload = serialize_assertion(
            assertion,
            evidence_refs=refs,
        )
        candidate_payload["kind"] = "semantic_assertion"
        candidate_payload["lifecycle_state"] = assertion.lifecycle_state
        candidate_payload["trust_boundary"] = (
            refs[0]["trust_boundary"]
            if refs and isinstance(refs[0].get("trust_boundary"), str)
            else "reviewed_memory"
        )
        candidate_payload["taint"] = {
            "provenance_status": candidate_payload["trust_boundary"],
            "evidence_ids": [
                ref["evidence_id"] for ref in refs if isinstance(ref.get("evidence_id"), str)
            ],
        }
        candidate_payload["retrieval_features"] = {
            "vector_distance": vector_distance_by_assertion_id.get(assertion.id),
            "keyword_terms": keyword_terms_by_assertion_id.get(assertion.id, []),
            "entity_ids": sorted(entity_ids_by_assertion_id.get(assertion.id, [])),
            "symbol_matches": symbol_features_by_assertion_id.get(assertion.id, []),
            "salience": salience_by_assertion_id.get(assertion.id),
            "temporal": temporal_by_assertion_id.get(assertion.id),
            "candidate_source": "retrieval_match"
            if (
                assertion.id in vector_distance_by_assertion_id
                or assertion.id in keyword_terms_by_assertion_id
                or assertion.id in entity_ids_by_assertion_id
                or assertion.id in symbol_features_by_assertion_id
            )
            else "projection_match",
            "updated_at_order": len(candidate_payloads) + 1,
        }
        candidate_payload["transport_order"] = len(candidate_payloads) + 1
        candidate_payload["conflict_status"] = (
            {
                "state": "open",
                "conflict_ids": sorted(open_conflict_ids_by_assertion_id[assertion.id]),
            }
            if assertion.id in open_conflict_ids_by_assertion_id
            else {"state": "none", "conflict_ids": []}
        )
        candidate_payloads.append(candidate_payload)

    candidate_project_snapshots = db.scalars(
        select(ProjectStateSnapshotRecord)
        .where(ProjectStateSnapshotRecord.lifecycle_state == "active")
        .order_by(
            ProjectStateSnapshotRecord.updated_at.desc(),
            ProjectStateSnapshotRecord.id.desc(),
        )
        .limit(8)
    ).all()
    if scope_aliases:
        candidate_project_snapshots = [
            snapshot
            for snapshot in candidate_project_snapshots
            if snapshot.project_key in scope_aliases
            or f"project:{snapshot.project_key}" in scope_aliases
        ]
    for snapshot in candidate_project_snapshots:
        candidate_payloads.append(
            {
                "id": snapshot.id,
                "kind": "project_state",
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "state": _redact_json_value(snapshot.state),
                "lifecycle_state": snapshot.lifecycle_state,
                "source_assertion_ids": snapshot.source_assertion_ids,
                "source_evidence_ids": snapshot.source_evidence_ids,
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "retrieval_features": {
                    "source": "active_project_state",
                    "updated_at_order": len(candidate_payloads) + 1,
                },
                "transport_order": len(candidate_payloads) + 1,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "updated_at": to_rfc3339(snapshot.updated_at),
            }
        )

    episode_query = select(MemoryEpisodeRecord).where(
        MemoryEpisodeRecord.lifecycle_state == "active"
    )
    if current_session_id is not None:
        episode_query = episode_query.where(
            MemoryEpisodeRecord.scope_key != f"session:{current_session_id}"
        )
    candidate_episodes = db.scalars(
        episode_query.order_by(
            MemoryEpisodeRecord.occurred_at.desc(), MemoryEpisodeRecord.id.asc()
        ).limit(6)
    ).all()
    if scope_aliases:
        candidate_episodes = [
            episode for episode in candidate_episodes if episode.scope_key in scope_aliases
        ]
    for episode in candidate_episodes:
        candidate_payloads.append(
            {
                "id": episode.id,
                "kind": "episode",
                "episode_type": episode.episode_type,
                "scope_key": episode.scope_key,
                "summary": redact_text(episode.summary),
                "outcome": redact_text(episode.outcome) if episode.outcome else None,
                "primary_evidence_id": episode.primary_evidence_id,
                "lifecycle_state": episode.lifecycle_state,
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "retrieval_features": {
                    "source": "active_episode",
                    "occurred_at_order": len(candidate_payloads) + 1,
                },
                "transport_order": len(candidate_payloads) + 1,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "occurred_at": to_rfc3339(episode.occurred_at),
            }
        )

    candidate_procedures = db.scalars(
        select(MemoryProcedureRecord)
        .where(
            MemoryProcedureRecord.lifecycle_state == "active",
            MemoryProcedureRecord.review_state.in_(("approved", "auto_approved")),
        )
        .order_by(MemoryProcedureRecord.updated_at.desc(), MemoryProcedureRecord.id.asc())
        .limit(8)
    ).all()
    if scope_aliases:
        candidate_procedures = [
            procedure for procedure in candidate_procedures if procedure.scope_key in scope_aliases
        ]
    for procedure in candidate_procedures:
        candidate_payloads.append(
            {
                "id": procedure.id,
                "kind": "procedure",
                "procedure_key": procedure.procedure_key,
                "scope_key": procedure.scope_key,
                "instruction": redact_text(procedure.instruction),
                "source_assertion_id": procedure.source_assertion_id,
                "lifecycle_state": procedure.lifecycle_state,
                "review_state": procedure.review_state,
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "retrieval_features": {
                    "source": "approved_procedure",
                    "updated_at_order": len(candidate_payloads) + 1,
                },
                "transport_order": len(candidate_payloads) + 1,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "updated_at": to_rfc3339(procedure.updated_at),
            }
        )

    action_trace_query = select(MemoryActionTraceRecord).where(
        MemoryActionTraceRecord.lifecycle_state == "active"
    )
    if current_session_id is not None:
        action_trace_query = action_trace_query.where(
            MemoryActionTraceRecord.scope_key != f"session:{current_session_id}"
        )
    candidate_action_traces = db.scalars(
        action_trace_query.order_by(
            MemoryActionTraceRecord.updated_at.desc(), MemoryActionTraceRecord.id.asc()
        ).limit(8)
    ).all()
    if scope_aliases:
        candidate_action_traces = [
            trace for trace in candidate_action_traces if trace.scope_key in scope_aliases
        ]
    for trace in candidate_action_traces:
        candidate_payloads.append(
            {
                "id": trace.id,
                "kind": "action_trace",
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "action_attempt_id": trace.action_attempt_id,
                "source_turn_id": trace.source_turn_id,
                "capability_id": trace.capability_id,
                "summary": redact_text(trace.summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "retrieval_features": {
                    "source": "active_action_trace",
                    "updated_at_order": len(candidate_payloads) + 1,
                },
                "transport_order": len(candidate_payloads) + 1,
                "updated_at": to_rfc3339(trace.updated_at),
            }
        )

    candidate_hot_blocks = db.scalars(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "hot_index",
            MemoryContextBlockRecord.lifecycle_state == "active",
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .order_by(MemoryContextBlockRecord.updated_at.desc(), MemoryContextBlockRecord.id.asc())
        .limit(8)
    ).all()
    if scope_aliases:
        candidate_hot_blocks = [
            block for block in candidate_hot_blocks if block.scope_key in scope_aliases
        ]
    for block in candidate_hot_blocks:
        candidate_payloads.append(
            {
                "id": block.id,
                "kind": "hot_index",
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "retrieval_features": {
                    "source": "active_hot_index",
                    "updated_at_order": len(candidate_payloads) + 1,
                },
                "transport_order": len(candidate_payloads) + 1,
                "projection_version": block.projection_version,
                "updated_at": to_rfc3339(block.updated_at),
            }
        )

    candidate_topic_blocks = db.scalars(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "topic",
            MemoryContextBlockRecord.lifecycle_state == "active",
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .order_by(MemoryContextBlockRecord.updated_at.desc(), MemoryContextBlockRecord.id.asc())
        .limit(8)
    ).all()
    if scope_aliases:
        candidate_topic_blocks = [
            block for block in candidate_topic_blocks if block.scope_key in scope_aliases
        ]
    for block in candidate_topic_blocks:
        candidate_payloads.append(
            {
                "id": block.id,
                "kind": "topic",
                "topic_id": block.topic_id,
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "retrieval_features": {
                    "source": "active_topic",
                    "updated_at_order": len(candidate_payloads) + 1,
                },
                "transport_order": len(candidate_payloads) + 1,
                "projection_version": block.projection_version,
                "updated_at": to_rfc3339(block.updated_at),
            }
        )

    recent_turns = list(
        reversed(
            db.scalars(
                select(TurnRecord)
                .order_by(TurnRecord.created_at.desc(), TurnRecord.id.desc())
                .limit(8)
            ).all()
        )
    )
    history = [
        {
            "turn_id": turn.id,
            "user_message": _clean_text(turn.user_message, max_chars=500),
            "assistant_message": _clean_text(turn.assistant_message or "", max_chars=500),
            "status": turn.status,
        }
        for turn in recent_turns
    ]

    if candidate_payloads:
        curation = _curate_memory_context_with_model(
            user_message=user_message,
            history=history,
            candidates=candidate_payloads,
            max_selected=max_recalled_assertions,
            settings=resolved_settings,
        )
    else:
        curation = {
            "selected_memories": [],
            "omitted_memories": [],
            "rationale": "No memory candidates were available for AI curation.",
            "uncertainty": "",
            "confidence": 1.0,
            "model": None,
            "prompt_version": MEMORY_CURATION_PROMPT_VERSION,
            "parse_status": "not_required_no_candidates",
        }

    selected_ids = [item["id"] for item in curation["selected_memories"]]
    selected_by_kind: dict[str, list[str]] = {}
    for item in curation["selected_memories"]:
        selected_by_kind.setdefault(item["kind"], []).append(item["id"])
    assertions_by_id = {assertion.id: assertion for assertion in candidate_assertions}
    selected_assertions = [
        assertions_by_id[memory_id]
        for memory_id in selected_by_kind.get("semantic_assertion", [])
        if memory_id in assertions_by_id
    ]
    candidate_payloads_by_id = {item["id"]: item for item in candidate_payloads}
    semantic_assertions = []
    for assertion in selected_assertions:
        serialized = serialize_assertion(
            assertion,
            evidence_refs=evidence_refs.get(assertion.id, []),
        )
        candidate_payload = candidate_payloads_by_id.get(assertion.id, {})
        serialized["conflict_status"] = candidate_payload.get(
            "conflict_status", {"state": "none", "conflict_ids": []}
        )
        serialized["retrieval_features"] = candidate_payload.get("retrieval_features", {})
        semantic_assertions.append(serialized)
    conflicts = db.scalars(
        select(MemoryConflictSetRecord)
        .where(MemoryConflictSetRecord.lifecycle_state == "open")
        .order_by(MemoryConflictSetRecord.updated_at.desc(), MemoryConflictSetRecord.id.asc())
    ).all()
    snapshots_by_id = {snapshot.id: snapshot for snapshot in candidate_project_snapshots}
    project_snapshots = [
        snapshots_by_id[memory_id]
        for memory_id in selected_by_kind.get("project_state", [])
        if memory_id in snapshots_by_id
    ]
    episodes_by_id = {episode.id: episode for episode in candidate_episodes}
    selected_episodes = [
        episodes_by_id[memory_id]
        for memory_id in selected_by_kind.get("episode", [])
        if memory_id in episodes_by_id
    ]
    procedures_by_id = {procedure.id: procedure for procedure in candidate_procedures}
    procedures = [
        procedures_by_id[memory_id]
        for memory_id in selected_by_kind.get("procedure", [])
        if memory_id in procedures_by_id
    ]
    action_traces_by_id = {trace.id: trace for trace in candidate_action_traces}
    action_traces = [
        action_traces_by_id[memory_id]
        for memory_id in selected_by_kind.get("action_trace", [])
        if memory_id in action_traces_by_id
    ]
    hot_blocks_by_id = {block.id: block for block in candidate_hot_blocks}
    hot_blocks = [
        hot_blocks_by_id[memory_id]
        for memory_id in selected_by_kind.get("hot_index", [])
        if memory_id in hot_blocks_by_id
    ]
    topic_blocks_by_id = {block.id: block for block in candidate_topic_blocks}
    topic_blocks = [
        topic_blocks_by_id[memory_id]
        for memory_id in selected_by_kind.get("topic", [])
        if memory_id in topic_blocks_by_id
    ]
    recall_window: dict[str, Any] = {
        "max_selected_memories": max_recalled_assertions,
        "selected_memory_count": len(curation["selected_memories"]),
        "memory_candidate_count": len(candidate_payloads),
        "omitted_memory_count": len(curation["omitted_memories"]),
        "selected_memory_ids": selected_ids,
        "selected_memories": list(curation["selected_memories"]),
        "omitted_memories": list(curation["omitted_memories"]),
        "candidate_memory_ids": [item["id"] for item in candidate_payloads],
        "candidate_memories": candidate_payloads,
        "curation_rationale": curation["rationale"],
        "curation_uncertainty": curation["uncertainty"],
        "curation_confidence": curation["confidence"],
        "curation_model": curation["model"],
        "curation_prompt_version": curation["prompt_version"],
        "curation_parse_status": curation["parse_status"],
        "curation_provider_response_id": curation.get("provider_response_id"),
    }
    context = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "projection_version": MEMORY_PROJECTION_VERSION,
        "hot_index": [
            {
                "id": block.id,
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "projection_version": block.projection_version,
                "updated_at": to_rfc3339(block.updated_at),
            }
            for block in hot_blocks
        ],
        "topic_index": [
            {
                "id": block.id,
                "topic_id": block.topic_id,
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "projection_version": block.projection_version,
                "updated_at": to_rfc3339(block.updated_at),
            }
            for block in topic_blocks
        ],
        "pinned_core": [
            item for item in semantic_assertions if item["type"] in {"profile", "preference"}
        ],
        "project_state": [
            {
                "id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "state": _redact_json_value(snapshot.state),
                "source_assertion_ids": snapshot.source_assertion_ids,
                "source_evidence_ids": snapshot.source_evidence_ids,
                "created_at": to_rfc3339(snapshot.created_at),
                "updated_at": to_rfc3339(snapshot.updated_at),
            }
            for snapshot in project_snapshots
        ],
        "commitments_and_decisions": [
            item for item in semantic_assertions if item["type"] in {"commitment", "decision"}
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
            for episode in selected_episodes
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
        "action_traces": [
            {
                "id": trace.id,
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "action_attempt_id": trace.action_attempt_id,
                "source_turn_id": trace.source_turn_id,
                "capability_id": trace.capability_id,
                "summary": redact_text(trace.summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "updated_at": to_rfc3339(trace.updated_at),
            }
            for trace in action_traces
        ],
        "conflicts": [_serialize_conflict(conflict) for conflict in conflicts],
        "recall_window": recall_window,
        "memory_policy": memory_policy,
        "projection_health": {
            "projection_version": MEMORY_PROJECTION_VERSION,
            "selected_assertion_count": len(selected_assertions),
            "selected_memory_count": len(curation["selected_memories"]),
        },
    }
    event_payload = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "projection_version": MEMORY_PROJECTION_VERSION,
        **recall_window,
        "conflict_ids": [conflict.id for conflict in conflicts],
        "memory_policy": memory_policy,
    }
    return context, event_payload


def context_text(memory_context: dict[str, Any]) -> str:
    lines = ["memory context:"]
    for item in memory_context.get("hot_index", []):
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            lines.append("- hot: " + item["content"])
    for item in memory_context.get("topic_index", []):
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            lines.append("- topic: " + item["content"])
    for item in memory_context.get("project_state", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- project: " + item["summary"])
    for item in memory_context.get("commitments_and_decisions", []):
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            conflict_status = item.get("conflict_status")
            if isinstance(conflict_status, dict) and conflict_status.get("state") == "open":
                lines.append("- conflicted commitment/decision: " + item["value"])
                continue
            lines.append("- commitment/decision: " + item["value"])
    for item in memory_context.get("semantic_assertions", []):
        if not isinstance(item, dict):
            continue
        memory_type = item.get("type")
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        value = item.get("value")
        if all(isinstance(part, str) for part in (memory_type, subject_key, predicate, value)):
            conflict_status = item.get("conflict_status")
            if isinstance(conflict_status, dict) and conflict_status.get("state") == "open":
                conflict_ids = conflict_status.get("conflict_ids")
                conflict_suffix = (
                    f" conflicts={','.join(conflict_ids)}"
                    if isinstance(conflict_ids, list)
                    and all(isinstance(conflict_id, str) for conflict_id in conflict_ids)
                    else ""
                )
                lines.append(
                    f"- conflict: {memory_type}: {subject_key} {predicate} = {value}"
                    f"{conflict_suffix}"
                )
                continue
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
    for item in memory_context.get("action_traces", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- action: " + item["summary"])
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
    evidence_id = task_payload.get("evidence_id")
    session_id = task_payload.get("session_id")
    if not isinstance(evidence_id, str) or not isinstance(session_id, str):
        raise RuntimeError("memory extraction task payload is malformed")

    with session_factory() as db:
        with db.begin():
            evidence = db.get(MemoryEvidenceRecord, evidence_id)
            if evidence is None or evidence.lifecycle_state != "available":
                return
            matched_scope_key = _matching_scope_key_for_text(db, text=evidence.source_text)
            allowed, _policy = session_allows_memory_operation(
                db,
                session_id=session_id,
                operation="extract",
                subject_key=matched_scope_key,
                scope_key=matched_scope_key,
            )
            if not allowed:
                return
            if (
                _never_remember_rule_for_text(
                    db,
                    source_session_id=session_id,
                    scope_key="global",
                    text=evidence.source_text,
                )
                is not None
            ):
                return
            source_text = evidence.source_text

    if not settings.openai_api_key:
        raise RuntimeError("memory extraction requires ARIEL_OPENAI_API_KEY")

    prompt = (
        "Extract durable Ariel memory candidates from the evidence. "
        "Return JSON only with a top-level candidates array. Each candidate must have "
        "subject_key, predicate, assertion_type, value, confidence, is_multi_valued. "
        "Use assertion_type values fact, profile, preference, commitment, decision, "
        "project_state, procedure, or domain_concept. Return an empty array when the "
        "evidence has no durable memory."
    )
    provider_response_id: str | None = None
    try:
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
    except httpx.HTTPError as exc:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=None,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code="E_AI_JUDGMENT_REQUIRED",
                        failure_reason=_clean_text(str(exc), max_chars=500),
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError("memory extraction model request failed") from exc
    if response.status_code >= 400:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=None,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={"status_code": response.status_code},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code="E_AI_JUDGMENT_REQUIRED",
                        failure_reason=f"HTTP {response.status_code}",
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError(f"memory extraction model returned HTTP {response.status_code}")
    response_payload = response.json()
    raw_provider_response_id = (
        response_payload.get("id") if isinstance(response_payload, dict) else None
    )
    provider_response_id = (
        raw_provider_response_id if isinstance(raw_provider_response_id, str) else None
    )
    text = _extract_output_text(
        response_payload.get("output") if isinstance(response_payload, dict) else None
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=provider_response_id,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="invalid_json",
                        validation_status="invalid",
                        failure_code="E_AI_JUDGMENT_INVALID_JSON",
                        failure_reason="memory extraction model returned malformed JSON",
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError("memory extraction model returned malformed JSON") from exc
    candidates = payload.get("candidates")
    schema_error = None
    if not isinstance(candidates, list):
        schema_error = "memory extraction JSON missing candidates array"
    elif len(candidates) > 8:
        schema_error = "memory extraction JSON returned too many candidates"
    else:
        for raw_candidate in candidates:
            if not isinstance(raw_candidate, dict) or set(raw_candidate.keys()) != {
                "subject_key",
                "predicate",
                "assertion_type",
                "value",
                "confidence",
                "is_multi_valued",
            }:
                schema_error = "memory extraction candidate schema invalid"
                break
            confidence = raw_candidate.get("confidence")
            if (
                not isinstance(raw_candidate.get("subject_key"), str)
                or not raw_candidate["subject_key"].strip()
                or not isinstance(raw_candidate.get("predicate"), str)
                or not raw_candidate["predicate"].strip()
                or raw_candidate.get("assertion_type") not in ALLOWED_MEMORY_ASSERTION_TYPES
                or not isinstance(raw_candidate.get("value"), str)
                or not raw_candidate["value"].strip()
                or isinstance(confidence, bool)
                or not isinstance(confidence, int | float)
                or float(confidence) < 0.0
                or float(confidence) > 1.0
                or float(confidence) != float(confidence)
                or not isinstance(raw_candidate.get("is_multi_valued"), bool)
            ):
                schema_error = "memory extraction candidate schema invalid"
                break
    if schema_error is not None:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=provider_response_id,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="schema_invalid",
                        validation_status="invalid",
                        failure_code="E_AI_JUDGMENT_SCHEMA",
                        failure_reason=schema_error,
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError(schema_error)

    with session_factory() as db:
        with db.begin():
            evidence = db.get(MemoryEvidenceRecord, evidence_id)
            if evidence is None or evidence.lifecycle_state != "available":
                return
            matched_scope_key = _matching_scope_key_for_text(db, text=evidence.source_text)
            allowed, _policy = session_allows_memory_operation(
                db,
                session_id=session_id,
                operation="extract",
                subject_key=matched_scope_key,
                scope_key=matched_scope_key,
            )
            if not allowed:
                return
            proposed_candidate_ids: list[str] = []
            for raw_candidate in candidates:
                subject_key = raw_candidate.get("subject_key")
                predicate = raw_candidate.get("predicate")
                assertion_type = raw_candidate.get("assertion_type")
                value = raw_candidate.get("value")
                confidence = raw_candidate.get("confidence")
                is_multi_valued = raw_candidate.get("is_multi_valued")
                memory_events = propose_memory_candidate(
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
                    source_evidence_id=evidence_id,
                )
                for event in memory_events:
                    payload_data = event.get("payload")
                    if (
                        event.get("event_type") == "evt.memory.candidate_proposed"
                        and isinstance(payload_data, dict)
                        and isinstance(payload_data.get("assertion_id"), str)
                    ):
                        proposed_candidate_ids.append(payload_data["assertion_id"])
            now = now_fn()
            db.add(
                AIJudgmentRecord(
                    id=new_id_fn("ajg"),
                    judgment_type="memory_extraction",
                    source_type="memory_evidence",
                    source_id=evidence_id,
                    status="succeeded",
                    model=settings.model_name,
                    prompt_version="memory-extraction-v1",
                    provider_response_id=provider_response_id,
                    input_summary="memory extraction for turn evidence",
                    input_refs={"session_id": session_id, "evidence_id": evidence_id},
                    selected=[
                        {"assertion_id": assertion_id} for assertion_id in proposed_candidate_ids
                    ],
                    omitted=[],
                    output={"candidate_count": len(proposed_candidate_ids)},
                    rationale="extracted durable memory candidates from evidence",
                    uncertainty=None,
                    confidence=None,
                    parse_status="parsed",
                    validation_status="valid",
                    failure_code=None,
                    failure_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
