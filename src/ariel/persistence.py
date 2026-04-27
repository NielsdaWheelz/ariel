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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
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
            (
                "impact_level IN ('read', 'write_reversible', 'write_irreversible', "
                "'external_send')"
            ),
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
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
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
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    action_attempt: Mapped[ActionAttemptRecord] = relationship(back_populates="artifacts")

    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('retrieval_provenance')",
            name="ck_artifact_type",
        ),
    )


class MemoryItemRecord(Base):
    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_class: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_key: Mapped[str] = mapped_column(Text, nullable=False)
    active_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint(
            (
                "memory_class IN "
                "('profile', 'preference', 'project', 'commitment', 'episodic_summary')"
            ),
            name="ck_memory_item_class",
        ),
        Index(
            "ix_memory_items_class_key_unique",
            "memory_class",
            "memory_key",
            unique=True,
        ),
    )


class MemoryRevisionRecord(Base):
    __tablename__ = "memory_revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_item_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("memory_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
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
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('candidate', 'validated', 'superseded', 'retracted')",
            name="ck_memory_revision_lifecycle_state",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_revision_confidence_range",
        ),
        CheckConstraint(
            (
                "(lifecycle_state = 'retracted' AND value IS NULL) OR "
                "(lifecycle_state <> 'retracted' AND value IS NOT NULL)"
            ),
            name="ck_memory_revision_value_presence",
        ),
        Index(
            "ix_memory_revisions_item_created",
            "memory_item_id",
            "created_at",
        ),
    )


class WeatherDefaultLocationRecord(Base):
    __tablename__ = "weather_default_locations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    default_location: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    token_obtained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    encryption_key_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    connector: Mapped[GoogleConnectorRecord] = relationship(back_populates="events")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint(
            (
                "task_type IN ('agency_event_received', 'deliver_discord_notification', "
                "'expire_approvals', 'reap_stale_tasks')"
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
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
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
    agency_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agency_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agency_pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliveries: Mapped[list["NotificationDeliveryRecord"]] = relationship(
        back_populates="notification"
    )

    __table_args__ = (
        CheckConstraint("source_type IN ('agency_event')", name="ck_notification_source_type"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

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
            "repo_root": redact_text(job.agency_repo_root) if job.agency_repo_root is not None else None,
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
            to_rfc3339(notification.delivered_at)
            if notification.delivered_at is not None
            else None
        ),
        "acked_at": to_rfc3339(notification.acked_at) if notification.acked_at is not None else None,
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
        "published_at": to_rfc3339(artifact.published_at) if artifact.published_at is not None else None,
    }


def serialize_memory_projection_item(
    *,
    item: MemoryItemRecord,
    active_revision: MemoryRevisionRecord | None,
    revision_count: int,
) -> dict[str, Any]:
    if active_revision is None:
        return {
            "memory_item_id": item.id,
            "memory_key": item.memory_key,
            "memory_class": item.memory_class,
            "revision_id": None,
            "revision_count": revision_count,
            "lifecycle_state": "retracted",
            "value": "",
            "confidence": 0.0,
            "source_turn_id": None,
            "source_session_id": None,
            "evidence": {},
            "last_verified_at": to_rfc3339(item.updated_at),
            "created_at": to_rfc3339(item.created_at),
            "updated_at": to_rfc3339(item.updated_at),
        }
    return {
        "memory_item_id": item.id,
        "memory_key": item.memory_key,
        "memory_class": item.memory_class,
        "revision_id": active_revision.id,
        "revision_count": revision_count,
        "lifecycle_state": active_revision.lifecycle_state,
        "value": redact_text(active_revision.value or ""),
        "confidence": active_revision.confidence,
        "source_turn_id": active_revision.source_turn_id,
        "source_session_id": active_revision.source_session_id,
        "evidence": redact_json_value(active_revision.evidence),
        "last_verified_at": to_rfc3339(active_revision.last_verified_at),
        "created_at": to_rfc3339(item.created_at),
        "updated_at": to_rfc3339(item.updated_at),
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
        policy_reason = policy_reasons.get(action_attempt_id) if isinstance(action_attempt_id, str) else None
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
                "action_attempt_id": action_attempt_id if isinstance(action_attempt_id, str) else "",
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
