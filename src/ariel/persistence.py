from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import ulid
from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    select,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session

from ariel.redaction import redact_json_value, redact_text


MEMORY_EMBEDDING_DIMENSIONS = 1536


def to_rfc3339(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


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
    digest: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="agent_turn")
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
        CheckConstraint(
            "kind IN ('agent_turn', 'research')",
            name="ck_turn_kind",
        ),
    )


class TurnIdempotencyRecord(Base):
    __tablename__ = "turn_idempotency_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="RESTRICT"),
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
        ForeignKey("turns.id", ondelete="RESTRICT"),
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


class AIJudgmentRecord(Base):
    __tablename__ = "ai_judgments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    judgment_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    input_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "judgment_type IN ('memory_recall', 'memory_remember', 'model_output')",
            name="ck_ai_judgment_type",
        ),
        CheckConstraint("status IN ('succeeded', 'failed')", name="ck_ai_judgment_status"),
        CheckConstraint(
            "parse_status IN ('parsed', 'invalid_json', 'missing_output', 'schema_invalid')",
            name="ck_ai_judgment_parse_status",
        ),
        CheckConstraint(
            "validation_status IN ('valid', 'invalid', 'not_validated')",
            name="ck_ai_judgment_validation_status",
        ),
        CheckConstraint(
            (
                "failure_code IS NULL OR failure_code IN ("
                "'E_AI_JUDGMENT_REQUIRED', 'E_AI_JUDGMENT_CREDENTIALS', "
                "'E_AI_JUDGMENT_TIMEOUT', 'E_AI_JUDGMENT_INVALID_JSON', "
                "'E_AI_JUDGMENT_SCHEMA', 'E_AI_JUDGMENT_VALIDATION', "
                "'E_AI_JUDGMENT_BUDGET')"
            ),
            name="ck_ai_judgment_failure_code",
        ),
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


class ActionPrivatePayloadRecord(Base):
    __tablename__ = "action_private_payloads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    action_attempt_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("action_attempts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    payload_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_enc: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "payload_kind IN ('google_provider_write_input')",
            name="ck_action_private_payload_kind",
        ),
        CheckConstraint(
            "length(payload_digest) = 64",
            name="ck_action_private_payload_digest",
        ),
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


class MemoryFactRecord(Base):
    __tablename__ = "memory_facts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(MEMORY_EMBEDDING_DIMENSIONS),
        nullable=True,
    )
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The HNSW index over ``embedding`` is created by migration 0036, mirroring
    # MemoryEmbeddingProjectionRecord — pgvector index DDL is migration-only.
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'forgotten')",
            name="ck_memory_fact_status",
        ),
        Index("ix_memory_facts_status", "status"),
        Index(
            "ix_memory_facts_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )


class MemoryProfileRecord(Base):
    __tablename__ = "memory_profile"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectStateSnapshotRecord(Base):
    __tablename__ = "project_state_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_assertion_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_episode_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    projection_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('active', 'superseded', 'retracted', 'deleted')",
            name="ck_project_state_snapshot_lifecycle_state",
        ),
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


class ProviderWatchChannelRecord(Base):
    __tablename__ = "provider_watch_channels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cursor_seed: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "resource_type",
            "resource_id",
            name="uq_provider_watch_channel_resource",
        ),
        CheckConstraint(
            "provider IN ('google')",
            name="ck_provider_watch_channels_provider",
        ),
        CheckConstraint(
            "resource_type IN ('gmail', 'calendar')",
            name="ck_provider_watch_channels_resource_type",
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'failed')",
            name="ck_provider_watch_channels_status",
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
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
        CheckConstraint("observation_count >= 0", name="ck_sync_run_observation_count"),
    )


class DiscordMessageRecord(Base):
    __tablename__ = "discord_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
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
        CheckConstraint("status IN ('active', 'deleted')", name="ck_discord_message_status"),
    )


class DiscordMessageEventRecord(Base):
    __tablename__ = "discord_message_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    discord_message_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("discord_messages.id", ondelete="RESTRICT"),
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
            "event_type IN ('created')",
            name="ck_discord_message_event_type",
        ),
    )


class GoogleProviderObjectRecord(Base):
    __tablename__ = "google_provider_objects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    thread_external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    calendar_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    ical_uid: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "object_type IN ('gmail_message', 'gmail_thread', 'calendar_event', "
            "'calendar_availability')",
            name="ck_google_provider_object_type",
        ),
        CheckConstraint(
            "status IN ('active', 'deleted', 'stale', 'unavailable')",
            name="ck_google_provider_object_status",
        ),
        CheckConstraint(
            "(object_type != 'calendar_event') OR (calendar_id IS NOT NULL)",
            name="ck_google_provider_object_calendar_identity",
        ),
        Index(
            "ix_google_provider_object_identity_unique",
            "provider_account_id",
            "object_type",
            "external_id",
            unique=True,
            postgresql_where=text("object_type != 'calendar_event'"),
        ),
        Index(
            "ix_google_provider_objects_calendar_event_identity_unique",
            "provider_account_id",
            "object_type",
            "calendar_id",
            "external_id",
            unique=True,
            postgresql_where=text("object_type = 'calendar_event'"),
        ),
        Index(
            "ix_google_provider_objects_thread",
            "provider_account_id",
            "thread_external_id",
        ),
    )


class ProviderEvidenceRecord(Base):
    __tablename__ = "provider_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_object_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("google_provider_objects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    thread_external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    calendar_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    taint: Mapped[str] = mapped_column(String(32), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="provider_source"
    )
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint("provider IN ('google')", name="ck_provider_evidence_provider"),
        CheckConstraint(
            "source_kind IN ('gmail_message', 'gmail_thread', 'calendar_event', "
            "'calendar_availability')",
            name="ck_provider_evidence_source_kind",
        ),
        CheckConstraint(
            "taint IN ('provider_untrusted', 'provider_metadata', 'internal')",
            name="ck_provider_evidence_taint",
        ),
        CheckConstraint(
            "sensitivity IN ('normal', 'private', 'restricted')",
            name="ck_provider_evidence_sensitivity",
        ),
        CheckConstraint(
            "retention_policy IN ('provider_source', 'short_lived', 'user_pinned')",
            name="ck_provider_evidence_retention_policy",
        ),
        CheckConstraint(
            "extraction_status IN ('pending', 'extracted', 'not_actionable', 'failed')",
            name="ck_provider_evidence_extraction_status",
        ),
        CheckConstraint(
            "lifecycle_state IN ('available', 'superseded', 'redacted', 'deleted', "
            "'stale', 'unavailable')",
            name="ck_provider_evidence_lifecycle_state",
        ),
        Index(
            "ix_provider_evidence_identity_digest_unique",
            "provider_object_id",
            "content_digest",
            unique=True,
        ),
        Index(
            "ix_provider_evidence_source",
            "provider",
            "provider_account_id",
            "source_kind",
            "external_id",
        ),
    )


class ProviderEvidenceBlockRecord(Base):
    __tablename__ = "provider_evidence_blocks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("provider_evidence.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_offsets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint("block_index >= 0", name="ck_provider_evidence_block_index"),
        CheckConstraint(
            "block_kind IN ('body', 'html_body', 'quote', 'forwarded', 'signature', "
            "'calendar_description', 'availability')",
            name="ck_provider_evidence_block_kind",
        ),
        Index(
            "ix_provider_evidence_blocks_unique",
            "evidence_id",
            "block_index",
            unique=True,
        ),
    )


class ProviderWriteReceiptRecord(Base):
    __tablename__ = "provider_write_receipts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_attempt_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("action_attempts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_object_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ambiguity_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    provider_etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider_history_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    response_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    undo_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    undo_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "provider IN ('google', 'agency')", name="ck_provider_write_receipt_provider"
        ),
        CheckConstraint(
            "capability_id IN ('cap.email.draft', 'cap.email.send', "
            "'cap.email.archive', 'cap.email.trash', 'cap.email.labels.modify', "
            "'cap.email.undo', 'cap.calendar.create_event', 'cap.calendar.update_event', "
            "'cap.calendar.respond_to_event', 'cap.drive.share', 'cap.agency.request_pr')",
            name="ck_provider_write_receipt_capability",
        ),
        CheckConstraint(
            "status IN ('executing', 'succeeded', 'failed', 'ambiguous', 'undone')",
            name="ck_provider_write_receipt_status",
        ),
        CheckConstraint(
            "(status = 'ambiguous' AND ambiguity_reason IS NOT NULL) OR "
            "(status != 'ambiguous' AND ambiguity_reason IS NULL)",
            name="ck_provider_write_receipt_ambiguity_reason",
        ),
        CheckConstraint(
            "capability_id IN ('cap.email.archive', 'cap.email.trash', "
            "'cap.email.labels.modify', 'cap.email.undo') OR "
            "(before_state IS NULL AND after_state IS NULL AND "
            "undo_token_hash IS NULL AND undo_expires_at IS NULL)",
            name="ck_provider_write_receipt_undo_fields_email_only",
        ),
        CheckConstraint(
            "(undo_token_hash IS NULL) = (undo_expires_at IS NULL)",
            name="ck_provider_write_receipt_undo_fields_paired",
        ),
        CheckConstraint(
            "capability_id NOT IN ('cap.email.archive', 'cap.email.trash', "
            "'cap.email.labels.modify', 'cap.email.undo') OR "
            "capability_id = 'cap.email.undo' OR status != 'succeeded' OR "
            "(undo_token_hash IS NOT NULL AND undo_expires_at IS NOT NULL)",
            name="ck_provider_write_receipt_succeeded_mutation_has_undo",
        ),
        Index(
            "ix_provider_write_receipts_idempotency_unique",
            "provider",
            "provider_account_id",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.is_not(None)),
        ),
        Index(
            "ix_provider_write_receipts_attempt_idempotency_unique",
            "action_attempt_id",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.is_not(None)),
        ),
        Index(
            "ix_provider_write_receipts_undo_token_hash",
            "undo_token_hash",
            unique=True,
            postgresql_where=undo_token_hash.is_not(None),
        ),
    )


class BackgroundTaskRecord(Base):
    __tablename__ = "background_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    provider_write_receipt_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("provider_write_receipts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recurrence_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            (
                "task_type IN ('agency_event_received', 'expire_approvals', "
                "'provider_event_received', 'provider_sync_due', 'memory_remember', "
                "'memory_sweep', 'execute_action_attempt', 'google_object_hydration_due', "
                "'provider_evidence_extraction_due', 'provider_write_reconcile_due', "
                "'agent_wake', 'provider_watch_renew_due', 'provider_reconcile_sync_due', "
                "'user_message', 'research_run')"
            ),
            name="ck_background_task_type",
        ),
        CheckConstraint(
            "(task_type = 'provider_write_reconcile_due' "
            "AND provider_write_receipt_id IS NOT NULL) OR "
            "(task_type != 'provider_write_reconcile_due' "
            "AND provider_write_receipt_id IS NULL)",
            name="ck_background_task_provider_write_reconcile_shape",
        ),
        CheckConstraint("attempts >= 0", name="ck_background_task_attempts_nonnegative"),
        Index(
            "ix_background_tasks_idempotency_key_unique",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.is_not(None)),
        ),
        Index(
            "ix_background_tasks_provider_write_reconcile_unique",
            "provider_write_receipt_id",
            unique=True,
            postgresql_where=(task_type == "provider_write_reconcile_due"),
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
    agency_sandbox_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    agency_egress_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
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
        CheckConstraint(
            "jsonb_typeof(agency_sandbox_policy) = 'object'",
            name="ck_jobs_agency_sandbox_policy_object",
        ),
        CheckConstraint(
            "jsonb_typeof(agency_egress_policy) = 'object'",
            name="ck_jobs_agency_egress_policy_object",
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


def enqueue_background_task(
    db: Session,
    *,
    task_type: str,
    payload: dict[str, Any],
    now: datetime,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    recurrence_seconds: int | None = None,
) -> BackgroundTaskRecord:
    if idempotency_key is not None:
        existing_task = db.scalar(
            select(BackgroundTaskRecord)
            .where(BackgroundTaskRecord.idempotency_key == idempotency_key)
            .limit(1)
        )
        if existing_task is not None:
            return existing_task
    provider_write_receipt_id = None
    if task_type == "provider_write_reconcile_due":
        receipt_id = payload.get("provider_write_receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            raise RuntimeError("provider_write_reconcile_due task payload invalid")
        provider_write_receipt_id = receipt_id
    task = BackgroundTaskRecord(
        id=_new_id("tsk"),
        task_type=task_type,
        idempotency_key=idempotency_key,
        provider_write_receipt_id=provider_write_receipt_id,
        payload=payload,
        attempts=0,
        recurrence_seconds=recurrence_seconds,
        run_after=run_after if run_after is not None else now,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.flush()
    return task


def serialize_background_task(task: BackgroundTaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "payload": redact_json_value(task.payload),
        "attempts": task.attempts,
        "recurrence_seconds": task.recurrence_seconds,
        "run_after": to_rfc3339(task.run_after),
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
            "sandbox_policy": redact_json_value(job.agency_sandbox_policy),
            "egress_policy": redact_json_value(job.agency_egress_policy),
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


def serialize_email_action(receipt: ProviderWriteReceiptRecord) -> dict[str, Any]:
    response_payload = (
        receipt.response_payload if isinstance(receipt.response_payload, dict) else {}
    )
    provider_object_ids = (
        receipt.provider_object_ids if isinstance(receipt.provider_object_ids, dict) else {}
    )
    provider_result = response_payload.get("provider_result")
    if not isinstance(provider_result, dict):
        provider_result = {}
    authority = response_payload.get("authority")
    approval_id = authority.get("approval_ref") if isinstance(authority, dict) else None
    undo_available = (
        receipt.status == "succeeded"
        and receipt.undo_token_hash is not None
        and receipt.undo_expires_at is not None
        and receipt.undo_expires_at > datetime.now(tz=UTC)
    )
    return {
        "id": receipt.id,
        "provider": receipt.provider,
        "provider_account_id": redact_text(receipt.provider_account_id),
        "action_attempt_id": receipt.action_attempt_id,
        "capability_id": receipt.capability_id,
        "input_hash": receipt.request_digest,
        "idempotency_key": redact_text(receipt.idempotency_key)
        if receipt.idempotency_key is not None
        else None,
        "status": receipt.status,
        "approval_id": approval_id if isinstance(approval_id, str) else None,
        "provider_message_ids": redact_json_value(provider_object_ids.get("message_ids", [])),
        "provider_thread_ids": redact_json_value(provider_object_ids.get("thread_ids", [])),
        "before_state": redact_json_value(receipt.before_state or {}),
        "after_state": redact_json_value(receipt.after_state or {}),
        "provider_result": redact_json_value(provider_result),
        "undo_available": undo_available,
        "undo_expires_at": to_rfc3339(receipt.undo_expires_at) if receipt.undo_expires_at else None,
        "failure_code": response_payload.get("error"),
        "created_at": to_rfc3339(receipt.created_at),
        "updated_at": to_rfc3339(receipt.updated_at),
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
        "observation_count": run.observation_count,
        "error": redact_text(run.error) if run.error is not None else None,
        "started_at": to_rfc3339(run.started_at) if run.started_at is not None else None,
        "completed_at": to_rfc3339(run.completed_at) if run.completed_at is not None else None,
        "created_at": to_rfc3339(run.created_at),
    }


def serialize_discord_message(item: DiscordMessageRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "message_id": item.message_id,
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


def serialize_discord_message_event(event: DiscordMessageEventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "discord_message_id": event.discord_message_id,
        "dedupe_key": event.dedupe_key,
        "provider_event_id": event.provider_event_id,
        "event_type": event.event_type,
        "payload": redact_json_value(event.payload),
        "created_at": to_rfc3339(event.created_at),
    }


def serialize_google_provider_object(item: GoogleProviderObjectRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider_account_id": redact_text(item.provider_account_id),
        "object_type": item.object_type,
        "external_id": redact_text(item.external_id),
        "thread_external_id": redact_text(item.thread_external_id)
        if item.thread_external_id
        else None,
        "calendar_id": redact_text(item.calendar_id) if item.calendar_id else None,
        "ical_uid": redact_text(item.ical_uid) if item.ical_uid else None,
        "status": item.status,
        "source_timestamp": (
            to_rfc3339(item.source_timestamp) if item.source_timestamp is not None else None
        ),
        "observed_at": to_rfc3339(item.observed_at),
        "provider_url": redact_text(item.provider_url) if item.provider_url else None,
        "metadata": redact_json_value(item.metadata_json),
        "content_digest": item.content_digest,
        "created_at": to_rfc3339(item.created_at),
        "updated_at": to_rfc3339(item.updated_at),
    }


def serialize_provider_evidence(evidence: ProviderEvidenceRecord) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "provider_object_id": evidence.provider_object_id,
        "provider": evidence.provider,
        "provider_account_id": redact_text(evidence.provider_account_id),
        "source_kind": evidence.source_kind,
        "external_id": redact_text(evidence.external_id),
        "thread_external_id": redact_text(evidence.thread_external_id)
        if evidence.thread_external_id
        else None,
        "calendar_id": redact_text(evidence.calendar_id) if evidence.calendar_id else None,
        "source_uri": redact_text(evidence.source_uri) if evidence.source_uri else None,
        "source_timestamp": (
            to_rfc3339(evidence.source_timestamp) if evidence.source_timestamp is not None else None
        ),
        "content_digest": evidence.content_digest,
        "metadata": redact_json_value(evidence.metadata_json),
        "taint": evidence.taint,
        "sensitivity": evidence.sensitivity,
        "retention_policy": evidence.retention_policy,
        "extraction_status": evidence.extraction_status,
        "lifecycle_state": evidence.lifecycle_state,
        "observed_at": to_rfc3339(evidence.observed_at),
        "created_at": to_rfc3339(evidence.created_at),
        "updated_at": to_rfc3339(evidence.updated_at),
    }


def serialize_provider_evidence_block(block: ProviderEvidenceBlockRecord) -> dict[str, Any]:
    return {
        "id": block.id,
        "evidence_id": block.evidence_id,
        "block_index": block.block_index,
        "block_kind": block.block_kind,
        "text": redact_text(block.text),
        "digest": block.digest,
        "source_offsets": redact_json_value(block.source_offsets),
        "metadata": redact_json_value(block.metadata_json),
        "created_at": to_rfc3339(block.created_at),
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
        "proposal_input": _redact_provider_write_input(
            capability_id=action_attempt.capability_id,
            payload=action_attempt.proposed_input,
        ),
        "policy_decision": action_attempt.policy_decision,
        "policy_reason": action_attempt.policy_reason,
        "approval_required": action_attempt.approval_required,
        "approval": serialize_approval_request(approval) if approval is not None else None,
        "execution": {
            "status": _execution_view_status(action_attempt),
            "output": _redact_provider_write_output(
                capability_id=action_attempt.capability_id,
                payload=action_attempt.execution_output,
            ),
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


def _provider_text_marker(value: str) -> dict[str, Any]:
    return {
        "redacted": True,
        "digest": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "char_count": len(value),
    }


def _redact_provider_write_input(*, capability_id: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return redact_json_value(payload)

    redacted = dict(payload)
    if capability_id in {"cap.email.draft", "cap.email.send"}:
        body = redacted.get("body")
        if isinstance(body, str):
            redacted["body"] = _provider_text_marker(body)
    if capability_id in {"cap.calendar.create_event", "cap.calendar.update_event"}:
        description = redacted.get("description")
        if isinstance(description, str):
            redacted["description"] = _provider_text_marker(description)
    return redact_json_value(redacted)


def _redact_provider_write_output(*, capability_id: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return redact_json_value(payload)
    redacted = dict(payload)
    if capability_id in {"cap.email.draft", "cap.email.send"}:
        for container in (redacted, redacted.get("draft"), redacted.get("message")):
            if isinstance(container, dict) and isinstance(container.get("body"), str):
                container["body"] = _provider_text_marker(container["body"])
    if capability_id in {"cap.calendar.create_event", "cap.calendar.update_event"}:
        for container in (redacted, redacted.get("event")):
            if isinstance(container, dict) and isinstance(container.get("description"), str):
                container["description"] = _provider_text_marker(container["description"])
    return redact_json_value(redacted)


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
            execution_output = _redact_provider_write_output(
                capability_id=capability_id
                if isinstance(capability_id, str)
                else "unknown.capability",
                payload=execution_payload.get("output"),
            )
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
                    "input_summary": _redact_provider_write_input(
                        capability_id=capability_id
                        if isinstance(capability_id, str)
                        else "unknown.capability",
                        payload=action_attempt.get("proposal_input"),
                    ),
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
