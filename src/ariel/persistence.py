from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ariel.redaction import redact_json_value, redact_text


def to_rfc3339(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    rotated_from_session_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    rotation_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    turns: Mapped[list["TurnRecord"]] = relationship(back_populates="session")

    __table_args__ = (
        CheckConstraint(
            (
                "(rotation_reason IS NULL) OR "
                "(rotation_reason IN ('user_initiated', 'threshold_turn_count', "
                "'threshold_age', 'threshold_context_pressure'))"
            ),
            name="ck_session_rotation_reason",
        ),
        CheckConstraint(
            "lifecycle_state IN ('active', 'rotating', 'closed', 'recovery_needed')",
            name="ck_session_lifecycle_state",
        ),
        CheckConstraint(
            (
                "(is_active IS TRUE AND lifecycle_state = 'active') OR "
                "(is_active IS FALSE AND lifecycle_state IN ('rotating', 'closed', 'recovery_needed'))"
            ),
            name="ck_session_lifecycle_matches_is_active",
        ),
        CheckConstraint(
            (
                "(rotation_reason IS NULL AND rotated_from_session_id IS NULL) OR "
                "(rotation_reason IS NOT NULL AND rotated_from_session_id IS NOT NULL)"
            ),
            name="ck_session_rotation_fields_paired",
        ),
        Index(
            "ix_single_active_session",
            "is_active",
            unique=True,
            postgresql_where=(is_active.is_(True)),
        ),
        Index(
            "ix_sessions_rotated_from_session_id_unique",
            "rotated_from_session_id",
            unique=True,
            postgresql_where=(rotated_from_session_id.is_not(None)),
        ),
    )


class SessionRotationRecord(Base):
    __tablename__ = "session_rotations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rotated_from_session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rotated_to_session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            (
                "reason IN ('user_initiated', 'threshold_turn_count', "
                "'threshold_age', 'threshold_context_pressure')"
            ),
            name="ck_session_rotation_reason_type",
        ),
        Index(
            "ix_session_rotations_idempotency_key_unique",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.is_not(None)),
        ),
    )


class TurnRecord(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[SessionRecord] = relationship(back_populates="turns")
    events: Mapped[list["EventRecord"]] = relationship(back_populates="turn")
    action_attempts: Mapped[list["ActionAttemptRecord"]] = relationship(back_populates="turn")

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="ck_turn_status",
        ),
    )


class TurnIdempotencyRecord(Base):
    __tablename__ = "turn_idempotency_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        Index(
            "ix_turn_idempotency_session_key_unique",
            "session_id",
            "idempotency_key",
            unique=True,
        ),
    )


class CaptureRecord(Base):
    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    capture_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    normalized_turn_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_session_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    terminal_state: Mapped[str] = mapped_column(String(32), nullable=False)
    ingest_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingest_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingest_error_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    ingest_error_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "capture_kind IN ('text', 'url', 'shared_content', 'unknown')",
            name="ck_capture_kind",
        ),
        CheckConstraint(
            "terminal_state IN ('turn_created', 'ingest_failed')",
            name="ck_capture_terminal_state",
        ),
        CheckConstraint(
            (
                "(terminal_state = 'turn_created' "
                "AND turn_id IS NOT NULL "
                "AND effective_session_id IS NOT NULL "
                "AND normalized_turn_input IS NOT NULL "
                "AND ingest_error_code IS NULL "
                "AND ingest_error_message IS NULL "
                "AND ingest_error_details IS NULL "
                "AND ingest_error_retryable IS NULL) "
                "OR "
                "(terminal_state = 'ingest_failed' "
                "AND turn_id IS NULL "
                "AND effective_session_id IS NULL "
                "AND normalized_turn_input IS NULL "
                "AND ingest_error_code IS NOT NULL "
                "AND ingest_error_message IS NOT NULL "
                "AND ingest_error_details IS NOT NULL "
                "AND ingest_error_retryable IS NOT NULL)"
            ),
            name="ck_capture_terminal_linkage",
        ),
        Index(
            "ix_captures_idempotency_key_unique",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.is_not(None)),
        ),
    )


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    turn: Mapped[TurnRecord] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_event_sequence_positive"),
        Index("ix_turn_sequence_unique", "turn_id", "sequence", unique=True),
    )


class ActionAttemptRecord(Base):
    __tablename__ = "action_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposal_index: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    capability_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_level: Mapped[str] = mapped_column(String(32), nullable=False)
    proposed_input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    turn: Mapped[TurnRecord] = relationship(back_populates="action_attempts")
    approval_request: Mapped["ApprovalRequestRecord | None"] = relationship(
        back_populates="action_attempt",
        uselist=False,
    )
    artifacts: Mapped[list["ArtifactRecord"]] = relationship(back_populates="action_attempt")

    __table_args__ = (
        CheckConstraint("proposal_index > 0", name="ck_action_attempt_proposal_index_positive"),
        CheckConstraint(
            ("impact_level IN ('read', 'write_reversible', 'write_irreversible', 'external_send')"),
            name="ck_action_attempt_impact_level",
        ),
        CheckConstraint(
            (
                "status IN ('proposed', 'rejected', 'awaiting_approval', 'approved', "
                "'denied', 'expired', 'executing', 'succeeded', 'failed')"
            ),
            name="ck_action_attempt_status",
        ),
        CheckConstraint(
            "policy_decision IN ('allow_inline', 'requires_approval', 'deny')",
            name="ck_action_attempt_policy_decision",
        ),
        Index("ix_turn_proposal_index_unique", "turn_id", "proposal_index", unique=True),
    )


class ApprovalRequestRecord(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    action_attempt_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("action_attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    action_attempt: Mapped[ActionAttemptRecord] = relationship(back_populates="approval_request")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_request_status",
        ),
    )


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_attempt_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("action_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    action_attempt: Mapped[ActionAttemptRecord] = relationship(back_populates="artifacts")

    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('retrieval_provenance')",
            name="ck_artifact_type",
        ),
    )


class AttachmentBlobRecord(Base):
    __tablename__ = "attachment_blobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sniffed_mime_type: Mapped[str] = mapped_column(String(256), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(32), nullable=False)
    scanner_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_attachment_blob_size_nonnegative"),
        CheckConstraint(
            "scan_status IN ('clean', 'unsafe', 'scan_failed')",
            name="ck_attachment_blob_scan_status",
        ),
    )


class AttachmentSourceRecord(Base):
    __tablename__ = "attachment_sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_transport: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_guild_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_author_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_attachment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attachment_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    declared_content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    declared_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acquisition_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquisition_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    blob_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("attachment_blobs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("source_transport IN ('discord')", name="ck_attachment_source_transport"),
        CheckConstraint(
            "(declared_size_bytes IS NULL) OR (declared_size_bytes >= 0)",
            name="ck_attachment_source_declared_size_nonnegative",
        ),
        Index(
            "ix_attachment_sources_session_turn_ref",
            "session_id",
            "turn_id",
            "attachment_ref",
            unique=True,
        ),
    )


class AttachmentExtractionRecord(Base):
    __tablename__ = "attachment_extractions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("attachment_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    blob_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("attachment_blobs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    modality: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "modality IN ('text', 'document', 'image', 'audio', 'unknown')",
            name="ck_attachment_extraction_modality",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'failed')", name="ck_attachment_extraction_status"
        ),
        CheckConstraint(
            (
                "outcome IN ('ok', 'unsupported_type', 'too_large', 'expired', "
                "'unavailable', 'unsafe', 'scan_failed', 'extract_failed', "
                "'provider_timeout', 'provider_unavailable', 'resource_limit')"
            ),
            name="ck_attachment_extraction_outcome",
        ),
    )


class MemoryEvidenceRecord(Base):
    __tablename__ = "memory_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_class: Mapped[str] = mapped_column(String(32), nullable=False)
    trust_boundary: Mapped[str] = mapped_column(String(32), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "content_class IN "
            "('user_message', 'assistant_message', 'tool_output', 'web_content', "
            "'file_content', 'system', 'rotation')",
            name="ck_memory_evidence_content_class",
        ),
        CheckConstraint(
            "trust_boundary IN "
            "('trusted_user', 'system', 'assistant', 'untrusted_tool', "
            "'untrusted_web', 'untrusted_file')",
            name="ck_memory_evidence_trust_boundary",
        ),
    )


class MemoryEntityRecord(Base):
    __tablename__ = "memory_entities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN "
            "('user', 'project', 'repo', 'artifact', 'task', 'commitment', "
            "'preference', 'procedure', 'assertion_subject')",
            name="ck_memory_entity_type",
        ),
        Index(
            "ix_memory_entities_type_key_unique",
            "entity_type",
            "entity_key",
            unique=True,
        ),
    )


class MemoryAssertionRecord(Base):
    __tablename__ = "memory_assertions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject_entity_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_entities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subject_key: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    object_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    assertion_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_assertion_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "assertion_type IN "
            "('fact', 'preference', 'commitment', 'decision', 'project_state', 'procedure')",
            name="ck_memory_assertion_type",
        ),
        CheckConstraint(
            "lifecycle_state IN "
            "('candidate', 'active', 'conflicted', 'superseded', 'retracted', "
            "'rejected', 'deleted')",
            name="ck_memory_assertion_lifecycle_state",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_assertion_confidence_range",
        ),
        CheckConstraint(
            "(valid_to IS NULL) OR (valid_from IS NULL) OR (valid_from < valid_to)",
            name="ck_memory_assertion_valid_interval",
        ),
        Index(
            "ix_memory_assertions_subject_predicate_state",
            "subject_entity_id",
            "predicate",
            "lifecycle_state",
        ),
        Index(
            "ix_memory_assertions_scope_key",
            "scope_key",
        ),
    )


class MemoryAssertionEvidenceRecord(Base):
    __tablename__ = "memory_assertion_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_evidence.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "assertion_id",
            "evidence_id",
            name="uq_memory_assertion_evidence_pair",
        ),
    )


class MemoryReviewRecord(Base):
    __tablename__ = "memory_reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN "
            "('pending', 'approved', 'rejected', 'auto_approved', "
            "'needs_user_review', 'needs_operator_review')",
            name="ck_memory_review_decision",
        ),
    )


class MemoryConflictSetRecord(Base):
    __tablename__ = "memory_conflict_sets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject_entity_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_entities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution_assertion_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('open', 'resolved')",
            name="ck_memory_conflict_set_lifecycle_state",
        ),
        Index(
            "ix_memory_conflict_sets_open_unique",
            "subject_entity_id",
            "predicate",
            "scope_key",
            unique=True,
            postgresql_where=(lifecycle_state == "open"),
        ),
    )


class MemoryConflictMemberRecord(Base):
    __tablename__ = "memory_conflict_members"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conflict_set_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_conflict_sets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assertion_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "conflict_set_id",
            "assertion_id",
            name="uq_memory_conflict_member_pair",
        ),
    )


class MemorySalienceRecord(Base):
    __tablename__ = "memory_salience"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_priority: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "user_priority IN ('none', 'pinned', 'deprioritized')",
            name="ck_memory_salience_user_priority",
        ),
        CheckConstraint(
            "score >= 0.0",
            name="ck_memory_salience_score_non_negative",
        ),
    )


class MemoryProjectionJobRecord(Base):
    __tablename__ = "memory_projection_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    projection_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_table: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "projection_kind IN ('embedding', 'context_block', 'graph_cache', 'project_state')",
            name="ck_memory_projection_job_kind",
        ),
        CheckConstraint(
            "lifecycle_state IN ('pending', 'running', 'completed', 'failed', 'dead_letter')",
            name="ck_memory_projection_job_lifecycle_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_memory_projection_job_attempts"),
        CheckConstraint("max_retries >= 0", name="ck_memory_projection_job_max_retries"),
    )


class MemoryEmbeddingProjectionRecord(Base):
    __tablename__ = "memory_embedding_projections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_assertions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    projection_version: Mapped[str] = mapped_column(String(32), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        Index(
            "ix_memory_embedding_projection_unique",
            "assertion_id",
            "projection_version",
            unique=True,
        ),
    )


class MemoryContextBlockRecord(Base):
    __tablename__ = "memory_context_blocks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_assertion_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    projection_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "block_type IN ('pinned_core', 'project_state', 'procedure')",
            name="ck_memory_context_block_type",
        ),
        Index(
            "ix_memory_context_blocks_unique",
            "block_type",
            "scope_key",
            "projection_version",
            unique=True,
        ),
    )


class ProjectStateSnapshotRecord(Base):
    __tablename__ = "project_state_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_assertion_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class WeatherDefaultLocationRecord(Base):
    __tablename__ = "weather_default_locations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    default_location: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('bootstrap', 'user')",
            name="ck_weather_default_location_source",
        ),
    )


class GoogleConnectorRecord(Base):
    __tablename__ = "google_connectors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="google")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    account_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    granted_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    token_obtained_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    encryption_key_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    events: Mapped[list["GoogleConnectorEventRecord"]] = relationship(back_populates="connector")

    __table_args__ = (
        CheckConstraint(
            "provider IN ('google')",
            name="ck_google_connector_provider",
        ),
        CheckConstraint(
            "status IN ('not_connected', 'connected', 'error', 'revoked')",
            name="ck_google_connector_status",
        ),
    )


class GoogleOAuthStateRecord(Base):
    __tablename__ = "google_oauth_states"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    state_handle: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    flow: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    pkce_verifier_enc: Mapped[str] = mapped_column(Text, nullable=False)
    pkce_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "flow IN ('connect', 'reconnect')",
            name="ck_google_oauth_state_flow",
        ),
    )


class GoogleConnectorEventRecord(Base):
    __tablename__ = "google_connector_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    connector_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("google_connectors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    connector: Mapped[GoogleConnectorRecord] = relationship(back_populates="events")


class ConnectorSubscriptionRecord(Base):
    __tablename__ = "connector_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    renew_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "resource_type", "resource_id", name="uq_subscription_resource"
        ),
        CheckConstraint("provider IN ('google')", name="ck_connector_subscription_provider"),
        CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_connector_subscription_resource_type",
        ),
        CheckConstraint(
            "status IN ('active', 'renewal_due', 'expired', 'error', 'revoked')",
            name="ck_connector_subscription_status",
        ),
        Index("ix_connector_subscriptions_renewal", "status", "renew_after", "id"),
    )


class SyncCursorRecord(Base):
    __tablename__ = "sync_cursors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "resource_type", "resource_id", name="uq_sync_cursor_resource"
        ),
        CheckConstraint("provider IN ('google')", name="ck_sync_cursor_provider"),
        CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_sync_cursor_resource_type",
        ),
        CheckConstraint(
            "status IN ('ready', 'syncing', 'invalid', 'error', 'revoked')",
            name="ck_sync_cursor_status",
        ),
    )


class ProviderEventRecord(Base):
    __tablename__ = "provider_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    body_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint("provider IN ('google')", name="ck_provider_event_provider"),
        CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_provider_event_resource_type",
        ),
        CheckConstraint(
            "status IN ('accepted', 'processed', 'failed')",
            name="ck_provider_event_status",
        ),
        Index("ix_provider_events_resource", "provider", "resource_type", "resource_id"),
    )


class SyncRunRecord(Base):
    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("provider_events.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    cursor_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint("provider IN ('google')", name="ck_sync_run_provider"),
        CheckConstraint(
            "resource_type IN ('calendar', 'gmail', 'drive')",
            name="ck_sync_run_resource_type",
        ),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="ck_sync_run_status"),
        CheckConstraint("item_count >= 0", name="ck_sync_run_item_count"),
        CheckConstraint("signal_count >= 0", name="ck_sync_run_signal_count"),
    )


class WorkspaceItemRecord(Base):
    __tablename__ = "workspace_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    item_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("provider", "item_type", "external_id", name="uq_workspace_item_external"),
        CheckConstraint("provider IN ('google', 'ariel')", name="ck_workspace_item_provider"),
        CheckConstraint(
            "item_type IN ('calendar_event', 'email_message', 'drive_file', 'internal_state')",
            name="ck_workspace_item_type",
        ),
        CheckConstraint("status IN ('active', 'deleted')", name="ck_workspace_item_status"),
        Index("ix_workspace_items_provider_type", "provider", "item_type", "updated_at"),
    )


class WorkspaceItemEventRecord(Base):
    __tablename__ = "workspace_item_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_item_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspace_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    provider_event_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("provider_events.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'updated', 'deleted', 'restored')",
            name="ck_workspace_item_event_type",
        ),
    )


class AttentionSignalRecord(Base):
    __tablename__ = "attention_signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_item_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("workspace_items.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(32), nullable=False)
    urgency: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    taint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            (
                "source_type IN ('workspace_item', 'job', 'approval_request', "
                "'memory_assertion', 'google_connector', 'capture')"
            ),
            name="ck_attention_signal_source_type",
        ),
        CheckConstraint(
            "status IN ('new', 'reviewed', 'dismissed', 'superseded')",
            name="ck_attention_signal_status",
        ),
        CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_signal_priority",
        ),
        CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_signal_urgency",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_attention_signal_confidence",
        ),
        Index("ix_attention_signals_status_priority", "status", "priority", "updated_at"),
        Index("ix_attention_signals_source", "source_type", "source_id"),
    )


class AttentionItemRecord(Base):
    __tablename__ = "attention_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_signal_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(32), nullable=False)
    urgency: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    taint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    next_follow_up_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    events: Mapped[list["AttentionItemEventRecord"]] = relationship(back_populates="item")

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('attention_signal')",
            name="ck_attention_item_source_type",
        ),
        CheckConstraint(
            (
                "status IN ('open', 'notified', 'acknowledged', 'snoozed', 'resolved', "
                "'expired', 'cancelled', 'superseded')"
            ),
            name="ck_attention_item_status",
        ),
        CheckConstraint(
            "priority IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_item_priority",
        ),
        CheckConstraint(
            "urgency IN ('critical', 'high', 'normal', 'low')",
            name="ck_attention_item_urgency",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_attention_item_confidence",
        ),
        Index("ix_attention_items_status_priority", "status", "priority", "updated_at"),
        Index("ix_attention_items_follow_up_due", "status", "next_follow_up_after", "id"),
        Index("ix_attention_items_source", "source_type", "source_id"),
    )


class AttentionItemEventRecord(Base):
    __tablename__ = "attention_item_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attention_item_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("attention_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    item: Mapped[AttentionItemRecord] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint(
            (
                "event_type IN ('detected', 'updated', 'notified', 'acknowledged', "
                "'snoozed', 'resolved', 'cancelled', 'expired', 'follow_up_queued', "
                "'refreshed')"
            ),
            name="ck_attention_item_event_type",
        ),
    )


class ProactiveFeedbackRecord(Base):
    __tablename__ = "proactive_feedback"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attention_item_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("attention_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    feedback_type: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "feedback_type IN ('important', 'noise', 'wrong', 'useful')",
            name="ck_proactive_feedback_type",
        ),
    )


class ActionProposalRecord(Base):
    __tablename__ = "action_proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attention_item_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("attention_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="ck_action_proposal_status",
        ),
    )


class BackgroundTaskRecord(Base):
    __tablename__ = "background_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            (
                "task_type IN ('agency_event_received', 'deliver_discord_notification', "
                "'expire_approvals', 'reap_stale_tasks', "
                "'provider_subscription_renewal_due', 'provider_event_received', "
                "'provider_sync_due', 'workspace_signal_derivation_due', "
                "'attention_review_due', 'attention_item_follow_up_due', "
                "'action_proposal_review_due')"
            ),
            name="ck_background_task_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'dead_letter')",
            name="ck_background_task_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_background_task_attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="ck_background_task_max_attempts_positive"),
        Index(
            "ix_background_tasks_claimable",
            "status",
            "run_after",
            "created_at",
        ),
    )


class AgencyEventRecord(Base):
    __tablename__ = "agency_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("source", "external_event_id", name="uq_agency_event_source_external_id"),
        CheckConstraint(
            (
                "event_type IN ('heartbeat', 'job.queued', 'job.started', 'job.progress', "
                "'job.waiting', 'job.completed', 'job.failed', 'job.cancelled', 'job.timed_out')"
            ),
            name="ck_agency_event_type",
        ),
        CheckConstraint(
            "status IN ('accepted', 'processed', 'failed')",
            name="ck_agency_event_status",
        ),
    )


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action_attempt_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("action_attempts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    agency_repo_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    agency_repo_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agency_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agency_invocation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agency_worktree_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agency_worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    agency_branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    agency_runner: Mapped[str | None] = mapped_column(Text, nullable=True)
    agency_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agency_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agency_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agency_pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    events: Mapped[list["JobEventRecord"]] = relationship(back_populates="job")

    __table_args__ = (
        UniqueConstraint("source", "external_job_id", name="uq_job_source_external_id"),
        CheckConstraint(
            (
                "status IN ('queued', 'running', 'waiting_approval', 'succeeded', "
                "'failed', 'cancelled', 'timed_out')"
            ),
            name="ck_job_status",
        ),
    )


class JobEventRecord(Base):
    __tablename__ = "job_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agency_event_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("agency_events.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    job: Mapped[JobRecord] = relationship(back_populates="events")


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliveries: Mapped[list["NotificationDeliveryRecord"]] = relationship(
        back_populates="notification"
    )

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('agency_event', 'attention_item', 'approval', 'connector_event')",
            name="ck_notification_source_type",
        ),
        CheckConstraint("channel IN ('discord')", name="ck_notification_channel"),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'acknowledged')",
            name="ck_notification_status",
        ),
    )


class NotificationDeliveryRecord(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    notification_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    notification: Mapped[NotificationRecord] = relationship(back_populates="deliveries")

    __table_args__ = (
        CheckConstraint("channel IN ('discord')", name="ck_notification_delivery_channel"),
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_notification_delivery_status",
        ),
    )


def serialize_session(session: SessionRecord) -> dict[str, Any]:
    return {
        "id": session.id,
        "is_active": session.is_active,
        "lifecycle_state": session.lifecycle_state,
        "created_at": to_rfc3339(session.created_at),
        "updated_at": to_rfc3339(session.updated_at),
    }


def serialize_capture(capture: CaptureRecord) -> dict[str, Any]:
    ingest_failure = (
        {
            "code": capture.ingest_error_code,
            "message": capture.ingest_error_message,
            "details": redact_json_value(capture.ingest_error_details or {}),
            "retryable": bool(capture.ingest_error_retryable),
        }
        if capture.terminal_state == "ingest_failed"
        else None
    )
    return {
        "id": capture.id,
        "kind": capture.capture_kind,
        "terminal_state": capture.terminal_state,
        "effective_session_id": capture.effective_session_id,
        "turn_id": capture.turn_id,
        "idempotency_key": capture.idempotency_key,
        "ingest_failure": ingest_failure,
        "created_at": to_rfc3339(capture.created_at),
        "updated_at": to_rfc3339(capture.updated_at),
    }


def serialize_event(event: EventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "turn_id": event.turn_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": to_rfc3339(event.created_at),
    }


def serialize_background_task(task: BackgroundTaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "payload": redact_json_value(task.payload),
        "status": task.status,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "error": redact_text(task.error) if task.error is not None else None,
        "claimed_by": task.claimed_by,
        "run_after": to_rfc3339(task.run_after),
        "last_heartbeat": (
            to_rfc3339(task.last_heartbeat) if task.last_heartbeat is not None else None
        ),
        "created_at": to_rfc3339(task.created_at),
        "updated_at": to_rfc3339(task.updated_at),
    }


def serialize_agency_event(event: AgencyEventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "source": event.source,
        "external_event_id": event.external_event_id,
        "event_type": event.event_type,
        "external_job_id": event.external_job_id,
        "payload": redact_json_value(event.payload),
        "status": event.status,
        "error": redact_text(event.error) if event.error is not None else None,
        "received_at": to_rfc3339(event.received_at),
        "processed_at": to_rfc3339(event.processed_at) if event.processed_at is not None else None,
    }


def serialize_job(job: JobRecord) -> dict[str, Any]:
    return {
        "id": job.id,
        "session_id": job.session_id,
        "turn_id": job.turn_id,
        "action_attempt_id": job.action_attempt_id,
        "source": job.source,
        "external_job_id": job.external_job_id,
        "title": redact_text(job.title) if job.title is not None else None,
        "status": job.status,
        "summary": redact_text(job.summary) if job.summary is not None else None,
        "latest_payload": redact_json_value(job.latest_payload),
        "agency": {
            "repo_root": redact_text(job.agency_repo_root)
            if job.agency_repo_root is not None
            else None,
            "repo_id": job.agency_repo_id,
            "task_id": job.agency_task_id,
            "invocation_id": job.agency_invocation_id,
            "worktree_id": job.agency_worktree_id,
            "worktree_path": (
                redact_text(job.agency_worktree_path)
                if job.agency_worktree_path is not None
                else None
            ),
            "branch": redact_text(job.agency_branch) if job.agency_branch is not None else None,
            "runner": redact_text(job.agency_runner) if job.agency_runner is not None else None,
            "request_id": job.agency_request_id,
            "last_synced_at": (
                to_rfc3339(job.agency_last_synced_at)
                if job.agency_last_synced_at is not None
                else None
            ),
            "pr_number": job.agency_pr_number,
            "pr_url": redact_text(job.agency_pr_url) if job.agency_pr_url is not None else None,
        },
        "discord_thread_id": job.discord_thread_id,
        "created_at": to_rfc3339(job.created_at),
        "updated_at": to_rfc3339(job.updated_at),
    }


def serialize_job_event(event: JobEventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "agency_event_id": event.agency_event_id,
        "event_type": event.event_type,
        "payload": redact_json_value(event.payload),
        "created_at": to_rfc3339(event.created_at),
    }


def serialize_connector_subscription(subscription: ConnectorSubscriptionRecord) -> dict[str, Any]:
    return {
        "id": subscription.id,
        "provider": subscription.provider,
        "resource_type": subscription.resource_type,
        "resource_id": subscription.resource_id,
        "channel_id": subscription.channel_id,
        "provider_subscription_id": subscription.provider_subscription_id,
        "status": subscription.status,
        "expires_at": to_rfc3339(subscription.expires_at) if subscription.expires_at else None,
        "renew_after": to_rfc3339(subscription.renew_after) if subscription.renew_after else None,
        "last_error_code": subscription.last_error_code,
        "last_error_at": (
            to_rfc3339(subscription.last_error_at) if subscription.last_error_at else None
        ),
        "created_at": to_rfc3339(subscription.created_at),
        "updated_at": to_rfc3339(subscription.updated_at),
    }


def serialize_sync_cursor(cursor: SyncCursorRecord) -> dict[str, Any]:
    return {
        "id": cursor.id,
        "provider": cursor.provider,
        "resource_type": cursor.resource_type,
        "resource_id": cursor.resource_id,
        "cursor_value": redact_text(cursor.cursor_value) if cursor.cursor_value else None,
        "cursor_version": cursor.cursor_version,
        "status": cursor.status,
        "last_successful_sync_at": (
            to_rfc3339(cursor.last_successful_sync_at) if cursor.last_successful_sync_at else None
        ),
        "last_error_code": cursor.last_error_code,
        "last_error_at": to_rfc3339(cursor.last_error_at) if cursor.last_error_at else None,
        "created_at": to_rfc3339(cursor.created_at),
        "updated_at": to_rfc3339(cursor.updated_at),
    }


def serialize_provider_event(event: ProviderEventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "provider": event.provider,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "external_event_id": event.external_event_id,
        "event_type": event.event_type,
        "headers": redact_json_value(event.headers),
        "payload": redact_json_value(event.payload),
        "body_digest": event.body_digest,
        "status": event.status,
        "error": redact_text(event.error) if event.error is not None else None,
        "received_at": to_rfc3339(event.received_at),
        "processed_at": to_rfc3339(event.processed_at) if event.processed_at else None,
    }


def serialize_sync_run(run: SyncRunRecord) -> dict[str, Any]:
    return {
        "id": run.id,
        "provider": run.provider,
        "resource_type": run.resource_type,
        "resource_id": run.resource_id,
        "provider_event_id": run.provider_event_id,
        "cursor_before": redact_text(run.cursor_before) if run.cursor_before else None,
        "cursor_after": redact_text(run.cursor_after) if run.cursor_after else None,
        "status": run.status,
        "item_count": run.item_count,
        "signal_count": run.signal_count,
        "error": redact_text(run.error) if run.error is not None else None,
        "started_at": to_rfc3339(run.started_at) if run.started_at is not None else None,
        "completed_at": to_rfc3339(run.completed_at) if run.completed_at is not None else None,
        "created_at": to_rfc3339(run.created_at),
    }


def serialize_workspace_item(item: WorkspaceItemRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider": item.provider,
        "item_type": item.item_type,
        "external_id": item.external_id,
        "title": redact_text(item.title),
        "summary": redact_text(item.summary),
        "source_uri": redact_text(item.source_uri) if item.source_uri else None,
        "status": item.status,
        "metadata": redact_json_value(item.item_metadata),
        "observed_at": to_rfc3339(item.observed_at),
        "deleted_at": to_rfc3339(item.deleted_at) if item.deleted_at else None,
        "created_at": to_rfc3339(item.created_at),
        "updated_at": to_rfc3339(item.updated_at),
    }


def serialize_workspace_item_event(event: WorkspaceItemEventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "workspace_item_id": event.workspace_item_id,
        "dedupe_key": event.dedupe_key,
        "provider_event_id": event.provider_event_id,
        "event_type": event.event_type,
        "payload": redact_json_value(event.payload),
        "created_at": to_rfc3339(event.created_at),
    }


def serialize_attention_signal(signal: AttentionSignalRecord) -> dict[str, Any]:
    return {
        "id": signal.id,
        "workspace_item_id": signal.workspace_item_id,
        "source_type": signal.source_type,
        "source_id": signal.source_id,
        "dedupe_key": signal.dedupe_key,
        "status": signal.status,
        "priority": signal.priority,
        "urgency": signal.urgency,
        "confidence": signal.confidence,
        "title": redact_text(signal.title),
        "body": redact_text(signal.body),
        "reason": redact_text(signal.reason),
        "evidence": redact_json_value(signal.evidence),
        "taint": redact_json_value(signal.taint),
        "created_at": to_rfc3339(signal.created_at),
        "updated_at": to_rfc3339(signal.updated_at),
    }


def serialize_action_proposal(proposal: ActionProposalRecord) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "attention_item_id": proposal.attention_item_id,
        "capability_id": proposal.capability_id,
        "payload": redact_json_value(proposal.payload),
        "payload_hash": proposal.payload_hash,
        "status": proposal.status,
        "policy_state": redact_json_value(proposal.policy_state),
        "evidence": redact_json_value(proposal.evidence),
        "created_at": to_rfc3339(proposal.created_at),
        "updated_at": to_rfc3339(proposal.updated_at),
    }


def serialize_proactive_feedback(feedback: ProactiveFeedbackRecord) -> dict[str, Any]:
    return {
        "id": feedback.id,
        "attention_item_id": feedback.attention_item_id,
        "feedback_type": feedback.feedback_type,
        "note": redact_text(feedback.note) if feedback.note is not None else None,
        "created_at": to_rfc3339(feedback.created_at),
    }


def serialize_attention_item(item: AttentionItemRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "source_signal_ids": item.source_signal_ids,
        "dedupe_key": item.dedupe_key,
        "status": item.status,
        "priority": item.priority,
        "urgency": item.urgency,
        "confidence": item.confidence,
        "title": redact_text(item.title),
        "body": redact_text(item.body),
        "reason": redact_text(item.reason),
        "evidence": redact_json_value(item.evidence),
        "taint": redact_json_value(item.taint),
        "expires_at": to_rfc3339(item.expires_at) if item.expires_at is not None else None,
        "next_follow_up_after": (
            to_rfc3339(item.next_follow_up_after) if item.next_follow_up_after is not None else None
        ),
        "last_notified_at": (
            to_rfc3339(item.last_notified_at) if item.last_notified_at is not None else None
        ),
        "created_at": to_rfc3339(item.created_at),
        "updated_at": to_rfc3339(item.updated_at),
    }


def serialize_attention_item_event(event: AttentionItemEventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "attention_item_id": event.attention_item_id,
        "event_type": event.event_type,
        "payload": redact_json_value(event.payload),
        "created_at": to_rfc3339(event.created_at),
    }


def serialize_notification(notification: NotificationRecord) -> dict[str, Any]:
    return {
        "id": notification.id,
        "dedupe_key": notification.dedupe_key,
        "source_type": notification.source_type,
        "source_id": notification.source_id,
        "channel": notification.channel,
        "status": notification.status,
        "title": redact_text(notification.title),
        "body": redact_text(notification.body),
        "payload": redact_json_value(notification.payload),
        "created_at": to_rfc3339(notification.created_at),
        "updated_at": to_rfc3339(notification.updated_at),
        "delivered_at": (
            to_rfc3339(notification.delivered_at) if notification.delivered_at is not None else None
        ),
        "acked_at": to_rfc3339(notification.acked_at)
        if notification.acked_at is not None
        else None,
    }


def _execution_view_status(action_attempt: ActionAttemptRecord) -> str:
    if action_attempt.status in {"succeeded", "failed"}:
        return action_attempt.status
    if action_attempt.status == "executing":
        return "in_progress"
    return "not_executed"


def serialize_approval_request(approval: ApprovalRequestRecord) -> dict[str, Any]:
    return {
        "id": approval.id,
        "action_attempt_id": approval.action_attempt_id,
        "actor_id": approval.actor_id,
        "status": approval.status,
        "expires_at": to_rfc3339(approval.expires_at),
        "decision_reason": approval.decision_reason,
        "decided_at": to_rfc3339(approval.decided_at) if approval.decided_at is not None else None,
        "created_at": to_rfc3339(approval.created_at),
        "updated_at": to_rfc3339(approval.updated_at),
    }


def serialize_action_attempt(
    action_attempt: ActionAttemptRecord,
    *,
    approval: ApprovalRequestRecord | None,
) -> dict[str, Any]:
    return {
        "id": action_attempt.id,
        "turn_id": action_attempt.turn_id,
        "proposal_index": action_attempt.proposal_index,
        "capability_id": action_attempt.capability_id,
        "capability_version": action_attempt.capability_version,
        "capability_contract_hash": action_attempt.capability_contract_hash,
        "impact_level": action_attempt.impact_level,
        "status": action_attempt.status,
        "proposal_input": action_attempt.proposed_input,
        "policy_decision": action_attempt.policy_decision,
        "policy_reason": action_attempt.policy_reason,
        "approval_required": action_attempt.approval_required,
        "approval": serialize_approval_request(approval) if approval is not None else None,
        "execution": {
            "status": _execution_view_status(action_attempt),
            "output": action_attempt.execution_output,
            "error": action_attempt.execution_error,
        },
        "created_at": to_rfc3339(action_attempt.created_at),
        "updated_at": to_rfc3339(action_attempt.updated_at),
    }


def _redacted_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return redact_text(normalized)


def serialize_artifact(artifact: ArtifactRecord) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "type": artifact.artifact_type,
        "title": redact_text(artifact.title),
        "source": redact_text(artifact.source),
        "retrieved_at": to_rfc3339(artifact.retrieved_at),
        "published_at": to_rfc3339(artifact.published_at)
        if artifact.published_at is not None
        else None,
    }


def _policy_reasons_by_action_attempt(events: list[EventRecord]) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for event in events:
        if event.event_type != "evt.action.policy_decided":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        action_attempt_id = payload.get("action_attempt_id")
        reason = payload.get("reason")
        if isinstance(action_attempt_id, str) and isinstance(reason, str) and reason.strip():
            reasons[action_attempt_id] = reason
    return reasons


def _serialize_surface_action_lifecycle(
    *,
    action_attempts: list[dict[str, Any]],
    events: list[EventRecord],
) -> list[dict[str, Any]]:
    policy_reasons = _policy_reasons_by_action_attempt(events)
    lifecycle_items: list[dict[str, Any]] = []

    for action_attempt in action_attempts:
        action_attempt_id = action_attempt.get("id")
        proposal_index = action_attempt.get("proposal_index")
        capability_id = action_attempt.get("capability_id")
        policy_decision = action_attempt.get("policy_decision")
        policy_reason = (
            policy_reasons.get(action_attempt_id) if isinstance(action_attempt_id, str) else None
        )
        if policy_reason is None:
            policy_reason = action_attempt.get("policy_reason")

        approval_payload = action_attempt.get("approval")
        if isinstance(approval_payload, dict):
            approval_reference = (
                approval_payload.get("id") if isinstance(approval_payload.get("id"), str) else None
            )
            approval_status_raw = approval_payload.get("status")
            approval_status = (
                approval_status_raw if isinstance(approval_status_raw, str) else "unknown"
            )
            approval_reason = _redacted_optional_text(approval_payload.get("decision_reason"))
            expires_at = (
                approval_payload.get("expires_at")
                if isinstance(approval_payload.get("expires_at"), str)
                else None
            )
            decided_at = (
                approval_payload.get("decided_at")
                if isinstance(approval_payload.get("decided_at"), str)
                else None
            )
        else:
            approval_reference = None
            approval_status = "not_requested"
            approval_reason = None
            expires_at = None
            decided_at = None

        execution_payload = action_attempt.get("execution")
        if isinstance(execution_payload, dict):
            execution_status_raw = execution_payload.get("status")
            execution_status = (
                execution_status_raw if isinstance(execution_status_raw, str) else "not_executed"
            )
            execution_output = redact_json_value(execution_payload.get("output"))
            execution_error = _redacted_optional_text(execution_payload.get("error"))
        else:
            execution_status = "not_executed"
            execution_output = None
            execution_error = None

        lifecycle_items.append(
            {
                "action_attempt_id": action_attempt_id
                if isinstance(action_attempt_id, str)
                else "",
                "proposal_index": proposal_index if isinstance(proposal_index, int) else 0,
                "proposal": {
                    "capability_id": (
                        capability_id if isinstance(capability_id, str) else "unknown.capability"
                    ),
                    "input_summary": redact_json_value(action_attempt.get("proposal_input")),
                },
                "policy": {
                    "decision": policy_decision if isinstance(policy_decision, str) else "deny",
                    "reason": _redacted_optional_text(policy_reason),
                },
                "approval": {
                    "status": approval_status,
                    "reference": approval_reference,
                    "reason": approval_reason,
                    "expires_at": expires_at,
                    "decided_at": decided_at,
                },
                "execution": {
                    "status": execution_status,
                    "output": execution_output,
                    "error": execution_error,
                },
            }
        )

    return lifecycle_items


def serialize_turn(
    turn: TurnRecord,
    *,
    events: list[EventRecord],
    action_attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    serialized_events = [serialize_event(event) for event in events]
    return {
        "id": turn.id,
        "session_id": turn.session_id,
        "user_message": turn.user_message,
        "assistant_message": turn.assistant_message,
        "status": turn.status,
        "created_at": to_rfc3339(turn.created_at),
        "updated_at": to_rfc3339(turn.updated_at),
        "events": serialized_events,
        "surface_action_lifecycle": _serialize_surface_action_lifecycle(
            action_attempts=action_attempts,
            events=events,
        ),
    }
