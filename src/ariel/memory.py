from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .persistence import (
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryConflictMemberRecord,
    MemoryConflictSetRecord,
    MemoryContextBlockRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEntityRecord,
    MemoryEvidenceRecord,
    MemoryProjectionJobRecord,
    MemoryReviewRecord,
    MemorySalienceRecord,
    ProjectStateSnapshotRecord,
    TurnRecord,
    to_rfc3339,
)
from .redaction import redact_text


MEMORY_CONTEXT_SCHEMA_VERSION = "2.0"
MEMORY_PROJECTION_VERSION = "semantic-v1"
USER_SUBJECT_KEY = "user:default"


class MemoryAssertionValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)


class TemporalScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "global"
    key: str = "global"


class SalienceSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: float
    user_priority: str
    assertion_type: str
    source_trust: str


def _clean_text(value: str, *, max_chars: int = 500) -> str:
    return " ".join(value.strip().split())[:max_chars]


def _key(value: str) -> str:
    pieces: list[str] = []
    last_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            pieces.append(char)
            last_was_separator = False
        elif not last_was_separator:
            pieces.append("_")
            last_was_separator = True
    normalized = "".join(pieces).strip("_")
    return normalized or "general"


def _terms(value: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
    }
    terms: set[str] = set()
    current: list[str] = []
    for char in value.lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if token not in stopwords:
                terms.add(token)
            current = []
    if current:
        token = "".join(current)
        if token not in stopwords:
            terms.add(token)
    return terms


def _statement_from_user_message(user_message: str) -> dict[str, Any] | None:
    message = _clean_text(user_message, max_chars=2000)
    lowered = message.lower()

    action = ""
    body = ""
    for candidate in ("remember", "correct", "forget"):
        if lowered == candidate:
            return None
        prefix = candidate + " "
        if lowered.startswith(prefix):
            action = candidate
            body = message[len(prefix) :].strip()
            break

    if not action:
        if lowered.startswith("i like ") and len(message) > len("i like "):
            candidate_value = _clean_text(message[len("i like ") :])
            return {
                "action": "candidate",
                "assertion_type": "preference",
                "subject_key": USER_SUBJECT_KEY,
                "predicate": "preference.general",
                "value": candidate_value,
                "confidence": 0.55,
                "metadata": {"capture_mode": "inferred_user_statement"},
            }
        return None

    if lowered.startswith("remember that my "):
        body = message[len("remember that my ") :].strip()
        action = "remember"
        marker = " is "
        if marker in body.lower():
            lower_body = body.lower()
            index = lower_body.index(marker)
            name = body[:index]
            profile_value = body[index + len(marker) :]
            return {
                "action": action,
                "assertion_type": "fact",
                "subject_key": USER_SUBJECT_KEY,
                "predicate": "profile." + _key(name),
                "value": _clean_text(profile_value),
                "confidence": 1.0,
                "metadata": {"capture_mode": "explicit_user_statement"},
            }

    if lowered.startswith("remember that i prefer "):
        preference_value = message[len("remember that i prefer ") :].strip()
        return {
            "action": "remember",
            "assertion_type": "preference",
            "subject_key": USER_SUBJECT_KEY,
            "predicate": "preference.general",
            "value": _clean_text(preference_value),
            "confidence": 1.0,
            "metadata": {"capture_mode": "explicit_user_statement"},
        }

    parsed_value: str | None
    requires_value = action in {"remember", "correct"}
    if requires_value:
        if "=" not in body:
            return None
        left, raw_value = body.split("=", 1)
        parsed_value = _clean_text(raw_value)
        if not parsed_value:
            return None
    else:
        left = body
        parsed_value = None

    parts = left.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    kind = parts[0].strip().lower()
    name = _key(parts[1])

    if kind in {"fact", "profile"}:
        assertion_type = "fact"
        subject_key = USER_SUBJECT_KEY
        predicate = "profile." + name
    elif kind == "preference":
        assertion_type = "preference"
        subject_key = USER_SUBJECT_KEY
        predicate = "preference." + name
    elif kind == "project":
        assertion_type = "project_state"
        subject_key = "project:" + name
        predicate = "project.state"
    elif kind == "commitment":
        assertion_type = "commitment"
        subject_key = USER_SUBJECT_KEY
        predicate = "commitment." + name
    elif kind == "decision":
        assertion_type = "decision"
        subject_key = USER_SUBJECT_KEY
        predicate = "decision." + name
    elif kind == "procedure":
        assertion_type = "procedure"
        subject_key = USER_SUBJECT_KEY
        predicate = "procedure." + name
    else:
        return None

    return {
        "action": action,
        "assertion_type": assertion_type,
        "subject_key": subject_key,
        "predicate": predicate,
        "value": parsed_value,
        "confidence": 1.0,
        "metadata": {"capture_mode": "explicit_memory_command"},
    }


def _entity_type(subject_key: str, assertion_type: str) -> str:
    if subject_key.startswith("project:"):
        return "project"
    if assertion_type == "preference":
        return "preference"
    if assertion_type == "procedure":
        return "procedure"
    return "user"


def _entity(
    db: Session,
    *,
    subject_key: str,
    assertion_type: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryEntityRecord:
    entity_type = _entity_type(subject_key, assertion_type)
    existing = db.scalar(
        select(MemoryEntityRecord)
        .where(
            MemoryEntityRecord.entity_type == entity_type,
            MemoryEntityRecord.entity_key == subject_key,
        )
        .limit(1)
    )
    if existing is not None:
        return existing
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


def _evidence(
    db: Session,
    *,
    session_id: str,
    turn_id: str | None,
    actor_id: str,
    source_text: str,
    metadata: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryEvidenceRecord:
    evidence = MemoryEvidenceRecord(
        id=new_id_fn("mev"),
        source_turn_id=turn_id,
        source_session_id=session_id,
        actor_id=actor_id,
        content_class="user_message",
        trust_boundary="trusted_user",
        source_text=_clean_text(source_text, max_chars=2000),
        metadata_json=dict(metadata),
        created_at=now,
    )
    db.add(evidence)
    db.flush()
    return evidence


def _active_assertions(
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
                MemoryAssertionRecord.lifecycle_state == "active",
            )
            .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
        ).all()
    )


def _assertion_text(assertion: MemoryAssertionRecord) -> str:
    value = assertion.object_value if isinstance(assertion.object_value, dict) else {}
    text = value.get("text")
    return text if isinstance(text, str) else ""


def _event_payload(
    assertion: MemoryAssertionRecord,
    *,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    return {
        "assertion_id": assertion.id,
        "evidence_id": evidence_id,
        "subject_key": assertion.subject_key,
        "predicate": assertion.predicate,
        "assertion_type": assertion.assertion_type,
        "lifecycle_state": assertion.lifecycle_state,
        "value_preview": redact_text(_assertion_text(assertion)),
        "confidence": assertion.confidence,
    }


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


def _record_salience(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    user_priority: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    base_score = 3.0 if assertion.assertion_type in {"commitment", "project_state"} else 1.0
    if user_priority == "pinned":
        base_score += 10.0
    if user_priority == "deprioritized":
        base_score = 0.1
    signals = SalienceSignals(
        base=base_score,
        user_priority=user_priority,
        assertion_type=assertion.assertion_type,
        source_trust="trusted_user",
    ).model_dump(mode="json")
    existing = db.scalar(
        select(MemorySalienceRecord)
        .where(MemorySalienceRecord.assertion_id == assertion.id)
        .limit(1)
    )
    if existing is None:
        db.add(
            MemorySalienceRecord(
                id=new_id_fn("msl"),
                assertion_id=assertion.id,
                user_priority=user_priority,
                score=base_score,
                signals=signals,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        existing.user_priority = user_priority
        existing.score = base_score
        existing.signals = signals
        existing.updated_at = now


def _record_projection_rows(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    text = _assertion_text(assertion)
    existing_embedding = db.scalar(
        select(MemoryEmbeddingProjectionRecord)
        .where(
            MemoryEmbeddingProjectionRecord.assertion_id == assertion.id,
            MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if existing_embedding is None:
        db.add(
            MemoryEmbeddingProjectionRecord(
                id=new_id_fn("mep"),
                assertion_id=assertion.id,
                projection_version=MEMORY_PROJECTION_VERSION,
                search_text=f"{assertion.subject_key} {assertion.predicate} {text}",
                embedding={"terms": sorted(_terms(text + " " + assertion.predicate))},
                created_at=now,
                updated_at=now,
            )
        )
    else:
        existing_embedding.search_text = f"{assertion.subject_key} {assertion.predicate} {text}"
        existing_embedding.embedding = {"terms": sorted(_terms(text + " " + assertion.predicate))}
        existing_embedding.updated_at = now

    db.add(
        MemoryProjectionJobRecord(
            id=new_id_fn("mpj"),
            projection_kind="embedding",
            target_table="memory_assertions",
            target_id=assertion.id,
            lifecycle_state="completed",
            attempts=1,
            max_retries=3,
            error=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )


def _delete_projection_rows(db: Session, *, assertion_id: str) -> None:
    db.execute(
        delete(MemoryEmbeddingProjectionRecord).where(
            MemoryEmbeddingProjectionRecord.assertion_id == assertion_id
        )
    )
    db.execute(
        delete(MemorySalienceRecord).where(MemorySalienceRecord.assertion_id == assertion_id)
    )


def _evidence_ids_by_assertion(
    db: Session,
    assertion_ids: Sequence[str],
) -> dict[str, list[str]]:
    if not assertion_ids:
        return {}
    rows = db.execute(
        select(
            MemoryAssertionEvidenceRecord.assertion_id,
            MemoryAssertionEvidenceRecord.evidence_id,
        )
        .where(MemoryAssertionEvidenceRecord.assertion_id.in_(assertion_ids))
        .order_by(
            MemoryAssertionEvidenceRecord.assertion_id.asc(),
            MemoryAssertionEvidenceRecord.created_at.asc(),
            MemoryAssertionEvidenceRecord.id.asc(),
        )
    ).all()
    result: dict[str, list[str]] = {assertion_id: [] for assertion_id in assertion_ids}
    for assertion_id, evidence_id in rows:
        result.setdefault(assertion_id, []).append(evidence_id)
    return result


def _record_project_snapshot(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    if assertion.assertion_type != "project_state":
        return
    project_key = assertion.subject_key.removeprefix("project:")
    text = _assertion_text(assertion)
    snapshot = ProjectStateSnapshotRecord(
        id=new_id_fn("pss"),
        project_key=project_key,
        summary=text,
        state={
            "subject_key": assertion.subject_key,
            "predicate": assertion.predicate,
            "value": text,
        },
        source_assertion_ids=[assertion.id],
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
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        block.content = f"{assertion.subject_key}: {text}"
        block.source_assertion_ids = [assertion.id]
        block.updated_at = now


def _create_assertion(
    db: Session,
    *,
    entity: MemoryEntityRecord,
    evidence: MemoryEvidenceRecord,
    statement: dict[str, Any],
    lifecycle_state: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryAssertionRecord:
    value = MemoryAssertionValue(text=str(statement["value"])).model_dump(mode="json")
    scope = TemporalScope().model_dump(mode="json")
    assertion = MemoryAssertionRecord(
        id=new_id_fn("mas"),
        subject_entity_id=entity.id,
        subject_key=str(statement["subject_key"]),
        predicate=str(statement["predicate"]),
        scope_key="global",
        object_value=value,
        assertion_type=str(statement["assertion_type"]),
        scope=scope,
        lifecycle_state=lifecycle_state,
        confidence=float(statement["confidence"]),
        valid_from=now if lifecycle_state == "active" else None,
        valid_to=None,
        superseded_by_assertion_id=None,
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


def _activate_assertion(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    actor_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for existing in _active_assertions(
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
        events.append(
            {"event_type": "evt.memory.assertion_superseded", "payload": _event_payload(existing)}
        )

    assertion.lifecycle_state = "active"
    assertion.valid_from = assertion.valid_from or now
    assertion.last_verified_at = now
    assertion.updated_at = now
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="auto_approved" if actor_id == "system" else "approved",
        actor_id=actor_id,
        reason="explicit approval",
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
    _record_projection_rows(db, assertion=assertion, now=now, new_id_fn=new_id_fn)
    _record_project_snapshot(db, assertion=assertion, now=now, new_id_fn=new_id_fn)
    events.append(
        {"event_type": "evt.memory.assertion_activated", "payload": _event_payload(assertion)}
    )
    events.append(
        {
            "event_type": "evt.memory.projection_rebuilt",
            "payload": {
                "assertion_id": assertion.id,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "projection_kinds": ["embedding", "salience", "project_state"],
            },
        }
    )
    return events


def _open_conflict(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    active_assertions: Sequence[MemoryAssertionRecord],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    conflict_set = db.scalar(
        select(MemoryConflictSetRecord)
        .where(
            MemoryConflictSetRecord.subject_entity_id == assertion.subject_entity_id,
            MemoryConflictSetRecord.predicate == assertion.predicate,
            MemoryConflictSetRecord.scope_key == assertion.scope_key,
            MemoryConflictSetRecord.lifecycle_state == "open",
        )
        .limit(1)
    )
    if conflict_set is None:
        conflict_set = MemoryConflictSetRecord(
            id=new_id_fn("mcf"),
            subject_entity_id=assertion.subject_entity_id,
            predicate=assertion.predicate,
            scope_key=assertion.scope_key,
            lifecycle_state="open",
            resolution_assertion_id=None,
            reason="candidate contradicts active assertion",
            created_at=now,
            updated_at=now,
        )
        db.add(conflict_set)
        db.flush()

    assertion.lifecycle_state = "conflicted"
    assertion.updated_at = now
    for member in [assertion, *active_assertions]:
        exists = db.scalar(
            select(MemoryConflictMemberRecord)
            .where(
                MemoryConflictMemberRecord.conflict_set_id == conflict_set.id,
                MemoryConflictMemberRecord.assertion_id == member.id,
            )
            .limit(1)
        )
        if exists is None:
            db.add(
                MemoryConflictMemberRecord(
                    id=new_id_fn("mcm"),
                    conflict_set_id=conflict_set.id,
                    assertion_id=member.id,
                    created_at=now,
                )
            )

    return {
        "event_type": "evt.memory.conflict_opened",
        "payload": {
            "conflict_set_id": conflict_set.id,
            "subject_key": assertion.subject_key,
            "predicate": assertion.predicate,
            "assertion_ids": [assertion.id, *[item.id for item in active_assertions]],
        },
    }


def record_memory_from_user_message(
    db: Session,
    *,
    session_id: str,
    source_turn_id: str,
    user_message: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    statement = _statement_from_user_message(user_message)
    if statement is None:
        return []

    now = now_fn()
    evidence = _evidence(
        db,
        session_id=session_id,
        turn_id=source_turn_id,
        actor_id=actor_id,
        source_text=user_message,
        metadata=dict(statement["metadata"]),
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {
            "event_type": "evt.memory.evidence_recorded",
            "payload": {
                "evidence_id": evidence.id,
                "source_turn_id": source_turn_id,
                "source_session_id": session_id,
                "content_class": evidence.content_class,
                "trust_boundary": evidence.trust_boundary,
            },
        }
    ]

    if statement["action"] == "forget":
        events.extend(
            forget_by_subject_predicate(
                db,
                subject_key=str(statement["subject_key"]),
                predicate=str(statement["predicate"]),
                actor_id=actor_id,
                now_fn=now_fn,
            )
        )
        return events

    entity = _entity(
        db,
        subject_key=str(statement["subject_key"]),
        assertion_type=str(statement["assertion_type"]),
        now=now,
        new_id_fn=new_id_fn,
    )
    assertion = _create_assertion(
        db,
        entity=entity,
        evidence=evidence,
        statement=statement,
        lifecycle_state="candidate",
        now=now,
        new_id_fn=new_id_fn,
    )
    events.append(
        {
            "event_type": "evt.memory.candidate_proposed",
            "payload": _event_payload(assertion, evidence_id=evidence.id),
        }
    )

    active_assertions = _active_assertions(
        db,
        subject_entity_id=entity.id,
        predicate=assertion.predicate,
        scope_key=assertion.scope_key,
    )
    if statement["action"] == "candidate":
        _record_review(
            db,
            assertion_id=assertion.id,
            decision="needs_user_review",
            actor_id="system",
            reason="inferred memory requires review",
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {"event_type": "evt.memory.review_required", "payload": _event_payload(assertion)}
        )
        if active_assertions:
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

    if statement["action"] in {"remember", "correct"}:
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
    events = [
        {
            "event_type": "evt.memory.candidate_approved",
            "payload": _event_payload(assertion),
        }
    ]
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
    old_assertion.lifecycle_state = "superseded"
    old_assertion.valid_to = now
    old_assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=old_assertion.id)
    evidence = _evidence(
        db,
        session_id=source_session_id,
        turn_id=None,
        actor_id=actor_id,
        source_text=value,
        metadata={"capture_mode": "manual_correction"},
        now=now,
        new_id_fn=new_id_fn,
    )
    statement = {
        "assertion_type": old_assertion.assertion_type,
        "subject_key": old_assertion.subject_key,
        "predicate": old_assertion.predicate,
        "value": _clean_text(value),
        "confidence": 1.0,
    }
    new_assertion = _create_assertion(
        db,
        entity=entity,
        evidence=evidence,
        statement=statement,
        lifecycle_state="candidate",
        now=now,
        new_id_fn=new_id_fn,
    )
    old_assertion.superseded_by_assertion_id = new_assertion.id
    events = [
        {"event_type": "evt.memory.assertion_superseded", "payload": _event_payload(old_assertion)}
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


def forget_by_subject_predicate(
    db: Session,
    *,
    subject_key: str,
    predicate: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
) -> list[dict[str, Any]]:
    del actor_id
    now = now_fn()
    assertions = db.scalars(
        select(MemoryAssertionRecord).where(
            MemoryAssertionRecord.subject_key == subject_key,
            MemoryAssertionRecord.predicate == predicate,
            MemoryAssertionRecord.lifecycle_state.in_(("active", "candidate", "conflicted")),
        )
    ).all()
    events: list[dict[str, Any]] = []
    for assertion in assertions:
        assertion.lifecycle_state = "retracted"
        assertion.valid_to = now
        assertion.updated_at = now
        _delete_projection_rows(db, assertion_id=assertion.id)
        events.append(
            {"event_type": "evt.memory.assertion_retracted", "payload": _event_payload(assertion)}
        )
    return events


def retract_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
) -> list[dict[str, Any]]:
    del actor_id
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None:
        return []
    now = now_fn()
    assertion.lifecycle_state = "retracted"
    assertion.valid_to = now
    assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=assertion.id)
    return [{"event_type": "evt.memory.assertion_retracted", "payload": _event_payload(assertion)}]


def delete_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
) -> list[dict[str, Any]]:
    del actor_id
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None:
        return []
    now = now_fn()
    assertion.lifecycle_state = "deleted"
    assertion.valid_to = now
    assertion.updated_at = now
    _delete_projection_rows(db, assertion_id=assertion.id)
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
    snippets: list[str] = []
    for turn in prior_turns[-3:]:
        user_text = _clean_text(turn.user_message)
        assistant_text = _clean_text(turn.assistant_message or "")
        snippets.append(
            f"user={user_text}; assistant={assistant_text}" if assistant_text else user_text
        )
    summary = (
        _clean_text(" | ".join(snippets), max_chars=1200) or "session rotated with no prior turns"
    )
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
            created_at=now,
            updated_at=now,
        )
    )
    block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "project_state",
            MemoryContextBlockRecord.scope_key == f"session:{prior_session_id}",
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="project_state",
                scope_key=f"session:{prior_session_id}",
                content=summary,
                source_assertion_ids=[],
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        block.content = summary
        block.updated_at = now


def serialize_assertion(
    assertion: MemoryAssertionRecord,
    *,
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "assertion_id": assertion.id,
        "subject_key": assertion.subject_key,
        "predicate": assertion.predicate,
        "assertion_type": assertion.assertion_type,
        "lifecycle_state": assertion.lifecycle_state,
        "value": redact_text(_assertion_text(assertion)),
        "confidence": assertion.confidence,
        "scope": assertion.scope,
        "scope_key": assertion.scope_key,
        "valid_from": to_rfc3339(assertion.valid_from)
        if assertion.valid_from is not None
        else None,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to is not None else None,
        "last_verified_at": to_rfc3339(assertion.last_verified_at),
        "created_at": to_rfc3339(assertion.created_at),
        "updated_at": to_rfc3339(assertion.updated_at),
        "superseded_by_assertion_id": assertion.superseded_by_assertion_id,
        "evidence_ids": list(evidence_ids),
    }


def list_memory(db: Session) -> dict[str, Any]:
    assertions = db.scalars(
        select(MemoryAssertionRecord).order_by(
            MemoryAssertionRecord.updated_at.desc(),
            MemoryAssertionRecord.id.asc(),
        )
    ).all()
    conflicts = db.scalars(
        select(MemoryConflictSetRecord).order_by(
            MemoryConflictSetRecord.updated_at.desc(),
            MemoryConflictSetRecord.id.asc(),
        )
    ).all()
    project_state = db.scalars(
        select(ProjectStateSnapshotRecord)
        .order_by(
            ProjectStateSnapshotRecord.updated_at.desc(),
            ProjectStateSnapshotRecord.id.desc(),
        )
        .limit(50)
    ).all()
    evidence_ids = _evidence_ids_by_assertion(db, [assertion.id for assertion in assertions])
    return {
        "assertions": [
            serialize_assertion(assertion, evidence_ids=evidence_ids.get(assertion.id, []))
            for assertion in assertions
            if assertion.lifecycle_state in {"active", "retracted", "conflicted", "candidate"}
        ],
        "candidates": [
            serialize_assertion(assertion, evidence_ids=evidence_ids.get(assertion.id, []))
            for assertion in assertions
            if assertion.lifecycle_state in {"candidate", "conflicted"}
        ],
        "conflicts": [
            {
                "conflict_set_id": conflict.id,
                "subject_entity_id": conflict.subject_entity_id,
                "predicate": conflict.predicate,
                "scope_key": conflict.scope_key,
                "lifecycle_state": conflict.lifecycle_state,
                "resolution_assertion_id": conflict.resolution_assertion_id,
                "reason": conflict.reason,
                "created_at": to_rfc3339(conflict.created_at),
                "updated_at": to_rfc3339(conflict.updated_at),
            }
            for conflict in conflicts
        ],
        "project_state": [
            {
                "snapshot_id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "state": snapshot.state,
                "source_assertion_ids": snapshot.source_assertion_ids,
                "created_at": to_rfc3339(snapshot.created_at),
                "updated_at": to_rfc3339(snapshot.updated_at),
            }
            for snapshot in project_state
        ],
    }


def search_memory(db: Session, *, query: str, limit: int) -> list[dict[str, Any]]:
    memory_context, _ = build_memory_context(db, user_message=query, max_recalled_assertions=limit)
    return list(memory_context["assertions"])


def build_memory_context(
    db: Session,
    *,
    user_message: str,
    max_recalled_assertions: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    query_terms = _terms(user_message)
    salience_rows = db.scalars(select(MemorySalienceRecord)).all()
    salience_by_assertion_id = {row.assertion_id: row for row in salience_rows}
    active_assertions = db.scalars(
        select(MemoryAssertionRecord)
        .where(MemoryAssertionRecord.lifecycle_state.in_(("active", "conflicted")))
        .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
    ).all()

    ranked: list[tuple[float, str, MemoryAssertionRecord, str]] = []
    for assertion in active_assertions:
        text = _assertion_text(assertion)
        overlap = len(
            query_terms.intersection(
                _terms(f"{assertion.subject_key} {assertion.predicate} {text}")
            )
        )
        reason = "semantic_keyword_graph_match" if overlap else "salience_or_commitment"
        score = float(overlap)
        if assertion.assertion_type == "commitment":
            score += 2.0
        if assertion.assertion_type == "project_state" and "project" in user_message.lower():
            score += 2.0
        salience = salience_by_assertion_id.get(assertion.id)
        if salience is not None:
            score += salience.score
            if salience.user_priority == "pinned":
                reason = "pinned"
            elif salience.user_priority == "deprioritized":
                score -= 100.0
                reason = "deprioritized"
        if score <= 0.0:
            continue
        ranked.append((score, assertion.id, assertion, reason))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    selected = ranked[:max_recalled_assertions]
    omitted = ranked[max_recalled_assertions:]
    evidence_ids = _evidence_ids_by_assertion(
        db,
        [assertion.id for _, _, assertion, _ in selected]
        + [
            assertion.id
            for assertion in active_assertions
            if assertion.lifecycle_state == "active" and assertion.assertion_type == "commitment"
        ],
    )
    selected_assertions = [
        {
            **serialize_assertion(assertion, evidence_ids=evidence_ids.get(assertion.id, [])),
            "rank_reason": reason,
            "rank_score": score,
        }
        for score, _, assertion, reason in selected
        if assertion.lifecycle_state == "active"
    ]

    conflict_ids = [
        row.id
        for row in db.scalars(
            select(MemoryConflictSetRecord)
            .where(MemoryConflictSetRecord.lifecycle_state == "open")
            .order_by(MemoryConflictSetRecord.updated_at.desc(), MemoryConflictSetRecord.id.asc())
        ).all()
    ]

    context_blocks = db.scalars(
        select(MemoryContextBlockRecord)
        .order_by(MemoryContextBlockRecord.updated_at.desc(), MemoryContextBlockRecord.id.desc())
        .limit(10)
    ).all()
    session_continuity = db.scalars(
        select(ProjectStateSnapshotRecord)
        .where(ProjectStateSnapshotRecord.project_key == "session_continuity")
        .order_by(
            ProjectStateSnapshotRecord.updated_at.desc(), ProjectStateSnapshotRecord.id.desc()
        )
        .limit(3)
    ).all()
    active_project_state = [
        assertion
        for assertion in active_assertions
        if assertion.lifecycle_state == "active" and assertion.assertion_type == "project_state"
    ][:10]
    active_commitments = [
        serialize_assertion(assertion, evidence_ids=evidence_ids.get(assertion.id, []))
        for assertion in active_assertions
        if assertion.lifecycle_state == "active" and assertion.assertion_type == "commitment"
    ][:12]

    recall_window: dict[str, Any] = {
        "max_recalled_assertions": max_recalled_assertions,
        "included_assertion_count": len(selected_assertions),
        "omitted_assertion_count": len(omitted),
        "included_assertion_ids": [item["assertion_id"] for item in selected_assertions],
        "omitted_assertions": [
            {"assertion_id": assertion.id, "reason": "top_k_bounded"}
            for _, _, assertion, _ in omitted
        ],
    }
    context = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "pinned_core": [
            {
                "context_block_id": block.id,
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
            }
            for block in context_blocks
            if block.block_type in {"pinned_core", "procedure"}
        ],
        "project_state": [
            {
                "snapshot_id": assertion.id,
                "project_key": assertion.subject_key.removeprefix("project:"),
                "summary": redact_text(_assertion_text(assertion)),
                "source_assertion_ids": [assertion.id],
            }
            for assertion in active_project_state
        ]
        + [
            {
                "snapshot_id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "source_assertion_ids": snapshot.source_assertion_ids,
            }
            for snapshot in session_continuity
        ],
        "active_commitments": active_commitments,
        "assertions": selected_assertions,
        "evidence_snippets": [],
        "conflicts": [{"conflict_set_id": conflict_id} for conflict_id in conflict_ids],
        "recall_window": recall_window,
    }
    event_payload = {
        **recall_window,
        "conflict_set_ids": conflict_ids,
    }
    return context, event_payload


def context_text(memory_context: dict[str, Any]) -> str:
    lines = ["memory context:"]
    for block in memory_context.get("pinned_core", []):
        if isinstance(block, dict) and isinstance(block.get("content"), str):
            lines.append("- core: " + block["content"])
    for item in memory_context.get("project_state", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- project: " + item["summary"])
    for item in memory_context.get("active_commitments", []):
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            lines.append("- commitment: " + item["value"])
    for item in memory_context.get("assertions", []):
        if not isinstance(item, dict):
            continue
        assertion_type = item.get("assertion_type")
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        assertion_value = item.get("value")
        values_are_strings = all(
            isinstance(part, str)
            for part in (assertion_type, subject_key, predicate, assertion_value)
        )
        if values_are_strings:
            lines.append(f"- {assertion_type}: {subject_key} {predicate} = {assertion_value}")
    conflicts = memory_context.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        lines.append("- unresolved conflicts exist; state uncertainty when relevant")
    return "\n".join(lines)
