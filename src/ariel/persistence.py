from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def to_rfc3339(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    turns: Mapped[list["TurnRecord"]] = relationship(back_populates="session")

    __table_args__ = (
        Index(
            "ix_single_active_session",
            "is_active",
            unique=True,
            postgresql_where=(is_active.is_(True)),
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


def serialize_session(session: SessionRecord) -> dict[str, Any]:
    return {
        "id": session.id,
        "is_active": session.is_active,
        "created_at": to_rfc3339(session.created_at),
        "updated_at": to_rfc3339(session.updated_at),
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


def serialize_turn(
    turn: TurnRecord,
    *,
    events: list[EventRecord],
    action_attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": turn.id,
        "session_id": turn.session_id,
        "user_message": turn.user_message,
        "assistant_message": turn.assistant_message,
        "status": turn.status,
        "created_at": to_rfc3339(turn.created_at),
        "updated_at": to_rfc3339(turn.updated_at),
        "action_attempts": action_attempts,
        "events": [serialize_event(event) for event in events],
    }
