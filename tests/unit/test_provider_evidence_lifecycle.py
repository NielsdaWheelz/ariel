from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from ariel.persistence import ProviderEvidenceBlockRecord, ProviderEvidenceRecord
from ariel.provider_evidence_lifecycle import (
    ProviderEvidenceBlockInput,
    ensure_provider_evidence_blocks,
    mark_provider_object_evidence_deleted,
    record_available_evidence,
    record_deleted_evidence,
    record_unavailable_evidence,
    restore_observed_evidence,
)


NOW = datetime(2026, 5, 24, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


class _Scalars:
    def __init__(self, rows: list[ProviderEvidenceRecord]) -> None:
        self._rows = rows

    def all(self) -> list[ProviderEvidenceRecord]:
        return self._rows


def test_provider_evidence_persistence_stays_in_lifecycle_owner() -> None:
    forbidden_fragments = ("ProviderEvidenceRecord(", "ProviderEvidenceBlockRecord")
    offenders = [
        path.relative_to(ROOT)
        for path in (ROOT / "src/ariel/action_runtime.py", ROOT / "src/ariel/sync_runtime.py")
        if any(fragment in path.read_text(encoding="utf-8") for fragment in forbidden_fragments)
    ]

    assert offenders == []


class _Session:
    def __init__(
        self,
        superseded_rows: list[ProviderEvidenceRecord] | None = None,
        scalar_row: ProviderEvidenceRecord | None = None,
    ) -> None:
        self._superseded_rows = superseded_rows or []
        self._scalar_row = scalar_row
        self.added: list[object] = []
        self.flushed = False

    def scalar(self, _statement: object) -> ProviderEvidenceRecord | None:
        return self._scalar_row

    def scalars(self, _statement: object) -> _Scalars:
        return _Scalars(self._superseded_rows)

    def add(self, row: object) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushed = True


def _evidence(
    *,
    evidence_id: str = "pev_1",
    digest: str = "digest_1",
    lifecycle_state: str = "available",
    extraction_status: str = "pending",
    source_uri: str | None = "https://mail.google.com/mail/u/0/#inbox/msg_1",
    source_timestamp: datetime | None = NOW,
    metadata_json: dict[str, Any] | None = None,
    thread_external_id: str | None = "thr_1",
    calendar_id: str | None = None,
) -> ProviderEvidenceRecord:
    return ProviderEvidenceRecord(
        id=evidence_id,
        provider_object_id="gpo_1",
        provider="google",
        provider_account_id="acct_1",
        source_kind="gmail_message",
        external_id="msg_1",
        thread_external_id=thread_external_id,
        calendar_id=calendar_id,
        source_uri=source_uri,
        source_timestamp=source_timestamp,
        content_digest=digest,
        metadata_json=metadata_json or {"read_outcome": "ok"},
        taint="provider_untrusted",
        sensitivity="private",
        retention_policy="provider_source",
        extraction_status=extraction_status,
        lifecycle_state=lifecycle_state,
        observed_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_new"


def test_restore_observed_evidence_leaves_redacted_rows_unchanged() -> None:
    evidence = _evidence(lifecycle_state="redacted", extraction_status="extracted")

    restored = restore_observed_evidence(
        db=cast(Session, _Session()),
        evidence=evidence,
        source_uri="https://mail.google.com/mail/u/0/#inbox/new",
        source_timestamp=NOW + timedelta(minutes=5),
        metadata_json={"read_outcome": "updated"},
        observed_at=NOW,
        extraction_status="pending",
        thread_external_id="thr_new",
        calendar_id=None,
    )

    assert restored is False
    assert evidence.lifecycle_state == "redacted"
    assert evidence.extraction_status == "extracted"
    assert evidence.thread_external_id == "thr_1"
    assert evidence.metadata_json == {"read_outcome": "ok"}


def test_restore_observed_evidence_supersedes_other_available_digests() -> None:
    evidence = _evidence(digest="digest_new")
    previous = _evidence(evidence_id="pev_old", digest="digest_old")

    restored = restore_observed_evidence(
        db=cast(Session, _Session([previous])),
        evidence=evidence,
        source_uri=evidence.source_uri,
        source_timestamp=evidence.source_timestamp,
        metadata_json=evidence.metadata_json,
        observed_at=NOW,
        extraction_status="pending",
        thread_external_id=evidence.thread_external_id,
        calendar_id=evidence.calendar_id,
    )

    assert restored is True
    assert evidence.lifecycle_state == "available"
    assert previous.lifecycle_state == "superseded"
    assert previous.updated_at == NOW


def test_restore_observed_evidence_preserves_extracted_status_for_unchanged_available_row() -> None:
    evidence = _evidence(extraction_status="extracted")

    restored = restore_observed_evidence(
        db=cast(Session, _Session()),
        evidence=evidence,
        source_uri=evidence.source_uri,
        source_timestamp=evidence.source_timestamp,
        metadata_json=evidence.metadata_json,
        observed_at=NOW,
        extraction_status="pending",
        thread_external_id=evidence.thread_external_id,
        calendar_id=evidence.calendar_id,
    )

    assert restored is True
    assert evidence.extraction_status == "extracted"
    assert evidence.observed_at == NOW
    assert evidence.updated_at == NOW


def test_restore_observed_evidence_resets_extraction_on_material_change() -> None:
    evidence = _evidence(extraction_status="extracted")

    restored = restore_observed_evidence(
        db=cast(Session, _Session()),
        evidence=evidence,
        source_uri="https://mail.google.com/mail/u/0/#inbox/msg_1_changed",
        source_timestamp=NOW + timedelta(minutes=5),
        metadata_json={"read_outcome": "ok", "labels": ["INBOX"]},
        observed_at=NOW,
        extraction_status="pending",
        thread_external_id="thr_2",
        calendar_id="primary",
    )

    assert restored is True
    assert evidence.extraction_status == "pending"
    assert evidence.thread_external_id == "thr_2"
    assert evidence.calendar_id == "primary"
    assert evidence.metadata_json == {"read_outcome": "ok", "labels": ["INBOX"]}


def test_record_available_evidence_creates_row_and_supersedes_prior_available() -> None:
    previous = _evidence(evidence_id="pev_old", digest="digest_old")
    session = _Session(superseded_rows=[previous])

    evidence = record_available_evidence(
        db=cast(Session, session),
        new_id_fn=_new_id,
        provider_object_id="gpo_1",
        provider="google",
        provider_account_id="acct_1",
        source_kind="gmail_message",
        external_id="msg_1",
        thread_external_id="thr_1",
        calendar_id=None,
        source_uri="https://mail.google.com/mail/u/0/#inbox/msg_1",
        source_timestamp=NOW,
        content_digest="digest_new",
        metadata_json={"read_outcome": "ok"},
        observed_at=NOW,
    )

    assert evidence is not None
    assert evidence.id == "pev_new"
    assert evidence.lifecycle_state == "available"
    assert evidence.extraction_status == "pending"
    assert previous.lifecycle_state == "superseded"
    assert session.added == [evidence]
    assert session.flushed is True


def test_ensure_provider_evidence_blocks_inserts_once() -> None:
    evidence = _evidence()
    session = _Session()

    ensure_provider_evidence_blocks(
        db=cast(Session, session),
        evidence=evidence,
        blocks=[
            ProviderEvidenceBlockInput(
                block_kind="body",
                text="hello",
                digest="digest_block",
                source_offsets={"block_id": "b1"},
                metadata_json={"truncated": False},
            )
        ],
        new_id_fn=_new_id,
        created_at=NOW,
    )

    assert len(session.added) == 1
    block = cast(ProviderEvidenceBlockRecord, session.added[0])
    assert block.id == "peb_new"
    assert block.evidence_id == evidence.id
    assert block.text == "hello"


def test_mark_provider_object_evidence_deleted_skips_redacted_rows() -> None:
    previous = _evidence(evidence_id="pev_old", digest="digest_old")
    redacted = _evidence(
        evidence_id="pev_redacted", digest="digest_redacted", lifecycle_state="redacted"
    )

    marked = mark_provider_object_evidence_deleted(
        db=cast(Session, _Session(superseded_rows=[previous, redacted])),
        provider_object_id="gpo_1",
        observed_at=NOW,
    )

    assert marked == 1
    assert previous.lifecycle_state == "deleted"
    assert redacted.lifecycle_state == "redacted"


def test_record_unavailable_evidence_updates_existing_non_redacted_row() -> None:
    existing = _evidence(lifecycle_state="available", extraction_status="pending")

    evidence = record_unavailable_evidence(
        db=cast(Session, _Session(scalar_row=existing)),
        new_id_fn=_new_id,
        provider_object_id=existing.provider_object_id,
        provider="google",
        provider_account_id=existing.provider_account_id,
        source_kind=existing.source_kind,
        external_id=existing.external_id,
        thread_external_id="thr_updated",
        calendar_id=None,
        source_uri=existing.source_uri,
        source_timestamp=NOW,
        content_digest=existing.content_digest,
        metadata_json={"read_outcome": {"status": "decode_failed"}},
        observed_at=NOW,
    )

    assert evidence is existing
    assert existing.lifecycle_state == "unavailable"
    assert existing.extraction_status == "failed"
    assert existing.thread_external_id == "thr_updated"
    assert existing.metadata_json == {"read_outcome": {"status": "decode_failed"}}


def test_record_deleted_evidence_marks_other_rows_deleted_and_updates_matching_digest() -> None:
    previous = _evidence(evidence_id="pev_old", digest="digest_old")
    redacted = _evidence(
        evidence_id="pev_redacted", digest="digest_redacted", lifecycle_state="redacted"
    )
    existing = _evidence(digest="digest_cancelled")
    session = _Session(superseded_rows=[previous, redacted], scalar_row=existing)

    evidence = record_deleted_evidence(
        db=cast(Session, session),
        new_id_fn=_new_id,
        provider_object_id=existing.provider_object_id,
        provider="google",
        provider_account_id=existing.provider_account_id,
        source_kind="calendar_event",
        external_id=existing.external_id,
        thread_external_id=None,
        calendar_id="primary",
        source_uri=existing.source_uri,
        source_timestamp=NOW,
        content_digest=existing.content_digest,
        metadata_json={"status": "cancelled"},
        observed_at=NOW,
    )

    assert evidence is existing
    assert existing.lifecycle_state == "deleted"
    assert existing.extraction_status == "not_actionable"
    assert existing.calendar_id == "primary"
    assert previous.lifecycle_state == "deleted"
    assert redacted.lifecycle_state == "redacted"
