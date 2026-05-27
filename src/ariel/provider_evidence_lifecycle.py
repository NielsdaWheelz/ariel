from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ariel.persistence import ProviderEvidenceBlockRecord, ProviderEvidenceRecord


@dataclass(frozen=True)
class ProviderEvidenceBlockInput:
    block_kind: str
    text: str
    digest: str
    source_offsets: dict[str, Any]
    metadata_json: dict[str, Any]


def provider_evidence_ref(db: Session, evidence: ProviderEvidenceRecord) -> dict[str, Any]:
    block_ids = db.scalars(
        select(ProviderEvidenceBlockRecord.id)
        .where(ProviderEvidenceBlockRecord.evidence_id == evidence.id)
        .order_by(ProviderEvidenceBlockRecord.block_index.asc())
    ).all()
    return {
        "provider_evidence_id": evidence.id,
        "read_receipt_id": evidence.id,
        "source_kind": evidence.source_kind,
        "external_id": evidence.external_id,
        "thread_external_id": evidence.thread_external_id,
        "block_ids": block_ids,
        "citation_refs": [
            {"kind": "provider_evidence_block", "block_id": block_id} for block_id in block_ids
        ],
    }


def read_provider_evidence_blocks(
    *,
    db: Session,
    evidence_id: str,
    block_ids: list[str],
    max_blocks: int,
) -> tuple[ProviderEvidenceRecord | None, list[ProviderEvidenceBlockRecord], bool]:
    evidence = db.get(ProviderEvidenceRecord, evidence_id)
    if evidence is None:
        return None, [], False
    query = (
        select(ProviderEvidenceBlockRecord)
        .where(ProviderEvidenceBlockRecord.evidence_id == evidence.id)
        .order_by(ProviderEvidenceBlockRecord.block_index.asc())
    )
    if block_ids:
        query = query.where(ProviderEvidenceBlockRecord.id.in_(block_ids))
    blocks = list(db.scalars(query).all())
    missing_requested_block = bool(block_ids) and {block.id for block in blocks} != set(block_ids)
    return evidence, blocks[:max_blocks], missing_requested_block


def ensure_provider_evidence_blocks(
    *,
    db: Session,
    evidence: ProviderEvidenceRecord,
    blocks: list[ProviderEvidenceBlockInput],
    new_id_fn: Callable[[str], str],
    created_at: datetime,
) -> None:
    existing_block_id = db.scalar(
        select(ProviderEvidenceBlockRecord.id)
        .where(ProviderEvidenceBlockRecord.evidence_id == evidence.id)
        .limit(1)
    )
    if existing_block_id is not None:
        return

    for index, block in enumerate(blocks):
        db.add(
            ProviderEvidenceBlockRecord(
                id=new_id_fn("peb"),
                evidence_id=evidence.id,
                block_index=index,
                block_kind=block.block_kind,
                text=block.text,
                digest=block.digest,
                source_offsets=block.source_offsets,
                metadata_json=block.metadata_json,
                created_at=created_at,
            )
        )


def mark_provider_object_evidence_deleted(
    *,
    db: Session,
    provider_object_id: str,
    observed_at: datetime,
) -> int:
    evidence_rows = db.scalars(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == provider_object_id,
            ProviderEvidenceRecord.lifecycle_state.notin_(("deleted", "redacted")),
        )
        .with_for_update()
    ).all()
    marked_count = 0
    for evidence_row in evidence_rows:
        if evidence_row.lifecycle_state == "redacted":
            continue
        evidence_row.lifecycle_state = "deleted"
        evidence_row.updated_at = observed_at
        marked_count += 1
    return marked_count


def record_available_evidence(
    *,
    db: Session,
    new_id_fn: Callable[[str], str],
    provider_object_id: str,
    provider: str,
    provider_account_id: str,
    source_kind: str,
    external_id: str,
    thread_external_id: str | None,
    calendar_id: str | None,
    source_uri: str | None,
    source_timestamp: datetime | None,
    content_digest: str,
    metadata_json: dict[str, Any],
    observed_at: datetime,
    extraction_status: str = "pending",
    taint: str = "provider_untrusted",
    sensitivity: str = "private",
    retention_policy: str = "provider_source",
    blocks: list[ProviderEvidenceBlockInput] | None = None,
) -> ProviderEvidenceRecord | None:
    evidence = db.scalar(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == provider_object_id,
            ProviderEvidenceRecord.content_digest == content_digest,
        )
        .with_for_update()
        .limit(1)
    )
    if evidence is not None:
        if not restore_observed_evidence(
            db=db,
            evidence=evidence,
            source_uri=source_uri,
            source_timestamp=source_timestamp,
            metadata_json=metadata_json,
            observed_at=observed_at,
            extraction_status=extraction_status,
            thread_external_id=thread_external_id,
            calendar_id=calendar_id,
        ):
            return None
        ensure_provider_evidence_blocks(
            db=db,
            evidence=evidence,
            blocks=blocks or [],
            new_id_fn=new_id_fn,
            created_at=observed_at,
        )
        return evidence

    superseded_rows = db.scalars(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == provider_object_id,
            ProviderEvidenceRecord.content_digest != content_digest,
            ProviderEvidenceRecord.lifecycle_state == "available",
        )
        .with_for_update()
    ).all()
    for superseded_row in superseded_rows:
        superseded_row.lifecycle_state = "superseded"
        superseded_row.updated_at = observed_at

    evidence = ProviderEvidenceRecord(
        id=new_id_fn("pev"),
        provider_object_id=provider_object_id,
        provider=provider,
        provider_account_id=provider_account_id,
        source_kind=source_kind,
        external_id=external_id,
        thread_external_id=thread_external_id,
        calendar_id=calendar_id,
        source_uri=source_uri,
        source_timestamp=source_timestamp,
        content_digest=content_digest,
        metadata_json=metadata_json,
        taint=taint,
        sensitivity=sensitivity,
        retention_policy=retention_policy,
        extraction_status=extraction_status,
        lifecycle_state="available",
        observed_at=observed_at,
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(evidence)
    db.flush()
    ensure_provider_evidence_blocks(
        db=db,
        evidence=evidence,
        blocks=blocks or [],
        new_id_fn=new_id_fn,
        created_at=observed_at,
    )
    return evidence


def record_unavailable_evidence(
    *,
    db: Session,
    new_id_fn: Callable[[str], str],
    provider_object_id: str,
    provider: str,
    provider_account_id: str,
    source_kind: str,
    external_id: str,
    thread_external_id: str | None,
    calendar_id: str | None,
    source_uri: str | None,
    source_timestamp: datetime | None,
    content_digest: str,
    metadata_json: dict[str, Any],
    observed_at: datetime,
    taint: str = "provider_untrusted",
    sensitivity: str = "private",
    retention_policy: str = "provider_source",
    blocks: list[ProviderEvidenceBlockInput] | None = None,
) -> ProviderEvidenceRecord | None:
    evidence = db.scalar(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == provider_object_id,
            ProviderEvidenceRecord.content_digest == content_digest,
        )
        .with_for_update()
        .limit(1)
    )
    if evidence is None:
        evidence = ProviderEvidenceRecord(
            id=new_id_fn("pev"),
            provider_object_id=provider_object_id,
            provider=provider,
            provider_account_id=provider_account_id,
            source_kind=source_kind,
            external_id=external_id,
            thread_external_id=thread_external_id,
            calendar_id=calendar_id,
            source_uri=source_uri,
            source_timestamp=source_timestamp,
            content_digest=content_digest,
            metadata_json=metadata_json,
            taint=taint,
            sensitivity=sensitivity,
            retention_policy=retention_policy,
            extraction_status="failed",
            lifecycle_state="unavailable",
            observed_at=observed_at,
            created_at=observed_at,
            updated_at=observed_at,
        )
        db.add(evidence)
        db.flush()
        ensure_provider_evidence_blocks(
            db=db,
            evidence=evidence,
            blocks=blocks or [],
            new_id_fn=new_id_fn,
            created_at=observed_at,
        )
        return evidence

    if evidence.lifecycle_state == "redacted":
        return None

    evidence.thread_external_id = thread_external_id
    evidence.calendar_id = calendar_id
    evidence.source_uri = source_uri
    evidence.source_timestamp = source_timestamp
    evidence.metadata_json = metadata_json
    evidence.extraction_status = "failed"
    evidence.lifecycle_state = "unavailable"
    evidence.observed_at = observed_at
    evidence.updated_at = observed_at
    ensure_provider_evidence_blocks(
        db=db,
        evidence=evidence,
        blocks=blocks or [],
        new_id_fn=new_id_fn,
        created_at=observed_at,
    )
    return evidence


def record_deleted_evidence(
    *,
    db: Session,
    new_id_fn: Callable[[str], str],
    provider_object_id: str,
    provider: str,
    provider_account_id: str,
    source_kind: str,
    external_id: str,
    thread_external_id: str | None,
    calendar_id: str | None,
    source_uri: str | None,
    source_timestamp: datetime | None,
    content_digest: str,
    metadata_json: dict[str, Any],
    observed_at: datetime,
    mark_other_digests: bool = True,
    taint: str = "provider_untrusted",
    sensitivity: str = "private",
    retention_policy: str = "provider_source",
    blocks: list[ProviderEvidenceBlockInput] | None = None,
) -> ProviderEvidenceRecord | None:
    other_rows_query = select(ProviderEvidenceRecord).where(
        ProviderEvidenceRecord.provider_object_id == provider_object_id,
        ProviderEvidenceRecord.lifecycle_state.notin_(("deleted", "redacted")),
    )
    if mark_other_digests:
        other_rows_query = other_rows_query.where(
            ProviderEvidenceRecord.content_digest != content_digest
        )
    other_rows = db.scalars(other_rows_query.with_for_update()).all()
    for other_row in other_rows:
        if other_row.lifecycle_state == "redacted":
            continue
        other_row.lifecycle_state = "deleted"
        other_row.updated_at = observed_at

    evidence = db.scalar(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == provider_object_id,
            ProviderEvidenceRecord.content_digest == content_digest,
        )
        .with_for_update()
        .limit(1)
    )
    if evidence is None:
        evidence = ProviderEvidenceRecord(
            id=new_id_fn("pev"),
            provider_object_id=provider_object_id,
            provider=provider,
            provider_account_id=provider_account_id,
            source_kind=source_kind,
            external_id=external_id,
            thread_external_id=thread_external_id,
            calendar_id=calendar_id,
            source_uri=source_uri,
            source_timestamp=source_timestamp,
            content_digest=content_digest,
            metadata_json=metadata_json,
            taint=taint,
            sensitivity=sensitivity,
            retention_policy=retention_policy,
            extraction_status="not_actionable",
            lifecycle_state="deleted",
            observed_at=observed_at,
            created_at=observed_at,
            updated_at=observed_at,
        )
        db.add(evidence)
        db.flush()
        ensure_provider_evidence_blocks(
            db=db,
            evidence=evidence,
            blocks=blocks or [],
            new_id_fn=new_id_fn,
            created_at=observed_at,
        )
        return evidence

    if evidence.lifecycle_state == "redacted":
        return None

    evidence.thread_external_id = thread_external_id
    evidence.calendar_id = calendar_id
    evidence.source_uri = source_uri
    evidence.source_timestamp = source_timestamp
    evidence.metadata_json = metadata_json
    evidence.extraction_status = "not_actionable"
    evidence.lifecycle_state = "deleted"
    evidence.observed_at = observed_at
    evidence.updated_at = observed_at
    ensure_provider_evidence_blocks(
        db=db,
        evidence=evidence,
        blocks=blocks or [],
        new_id_fn=new_id_fn,
        created_at=observed_at,
    )
    return evidence


def restore_observed_evidence(
    *,
    db: Session,
    evidence: ProviderEvidenceRecord,
    source_uri: str | None,
    source_timestamp: datetime | None,
    metadata_json: dict[str, Any],
    observed_at: datetime,
    extraction_status: str,
    thread_external_id: str | None,
    calendar_id: str | None,
) -> bool:
    if evidence.lifecycle_state == "redacted":
        return False

    material_changed = (
        evidence.source_uri != source_uri
        or evidence.source_timestamp != source_timestamp
        or evidence.metadata_json != metadata_json
        or evidence.thread_external_id != thread_external_id
        or evidence.calendar_id != calendar_id
    )
    reset_extraction = evidence.lifecycle_state != "available" or material_changed

    superseded_rows = db.scalars(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == evidence.provider_object_id,
            ProviderEvidenceRecord.content_digest != evidence.content_digest,
            ProviderEvidenceRecord.lifecycle_state == "available",
        )
        .with_for_update()
    ).all()
    for superseded_row in superseded_rows:
        superseded_row.lifecycle_state = "superseded"
        superseded_row.updated_at = observed_at

    evidence.thread_external_id = thread_external_id
    evidence.calendar_id = calendar_id
    evidence.source_uri = source_uri
    evidence.source_timestamp = source_timestamp
    evidence.metadata_json = metadata_json
    if reset_extraction:
        evidence.extraction_status = extraction_status
    evidence.lifecycle_state = "available"
    evidence.observed_at = observed_at
    evidence.updated_at = observed_at
    return True
