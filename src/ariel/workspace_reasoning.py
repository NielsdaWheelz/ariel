from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum
import math
from typing import assert_never


class CandidateKind(StrEnum):
    COMMITMENT = "commitment"
    DECISION = "decision"
    WAITING_ON_USER = "waiting_on_user"
    WAITING_ON_COUNTERPARTY = "waiting_on_counterparty"
    DEADLINE = "deadline"
    MEETING_REQUEST = "meeting_request"
    SCHEDULE_PROPOSAL = "schedule_proposal"
    RESOLVED_COMMITMENT = "resolved_commitment"
    NOT_ACTIONABLE = "not_actionable"


class CommitmentOwner(StrEnum):
    USER = "user"
    COUNTERPARTY = "counterparty"
    SHARED = "shared"
    UNKNOWN = "unknown"


class CommitmentState(StrEnum):
    CANDIDATE = "candidate"
    NEEDS_REVIEW = "needs_review"
    ACTIVE = "active"
    WAITING_ON_USER = "waiting_on_user"
    WAITING_ON_COUNTERPARTY = "waiting_on_counterparty"
    SCHEDULED = "scheduled"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    DISMISSED = "dismissed"
    REJECTED = "rejected"
    STALE = "stale"
    EXPIRED = "expired"
    DELETED = "deleted"


class FollowUpKind(StrEnum):
    DUE_DATE = "due_date"
    WAITING_ON_USER = "waiting_on_user"
    WAITING_ON_COUNTERPARTY = "waiting_on_counterparty"


class FollowUpAction(StrEnum):
    DELIBERATE = "deliberate"
    WAIT = "wait"
    NO_OP = "no_op"


@dataclass(frozen=True, slots=True)
class EvidenceBlock:
    block_id: str
    evidence_id: str
    source_timestamp: datetime


@dataclass(frozen=True, slots=True)
class DueWindow:
    start_at: datetime
    end_at: datetime | None
    source_text: str

    @property
    def due_at(self) -> datetime:
        return self.end_at if self.end_at is not None else self.start_at


@dataclass(frozen=True, slots=True)
class CommitmentCandidate:
    kind: CandidateKind
    action_text: str
    owner: CommitmentOwner
    confidence: float
    evidence_block_ids: tuple[str, ...]
    due_expression: str | None = None


@dataclass(frozen=True, slots=True)
class CommitmentCandidateValidation:
    accepted: bool
    reason: str | None
    source_blocks: tuple[EvidenceBlock, ...]
    due_window: DueWindow | None


@dataclass(frozen=True, slots=True)
class LifecycleTransitionValidation:
    allowed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class WorkCommitment:
    commitment_id: str
    state: CommitmentState
    owner: CommitmentOwner
    action_text: str
    evidence_block_ids: tuple[str, ...]
    due_window: DueWindow | None = None


@dataclass(frozen=True, slots=True)
class FollowUpLoop:
    loop_id: str
    kind: FollowUpKind
    commitment_id: str
    version: int
    scheduled_version: int
    scheduled_for: datetime
    stale_after: datetime
    snoozed_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class FollowUpEvaluation:
    action: FollowUpAction
    reason: str
    next_check_at: datetime | None = None


def validate_commitment_candidate(
    candidate: CommitmentCandidate,
    *,
    evidence_blocks: tuple[EvidenceBlock, ...],
) -> CommitmentCandidateValidation:
    if not isinstance(candidate.kind, CandidateKind):
        return CommitmentCandidateValidation(
            accepted=False,
            reason="invalid_candidate_kind",
            source_blocks=(),
            due_window=None,
        )
    if not isinstance(candidate.owner, CommitmentOwner):
        return CommitmentCandidateValidation(
            accepted=False,
            reason="invalid_owner",
            source_blocks=(),
            due_window=None,
        )
    if not candidate.evidence_block_ids:
        return CommitmentCandidateValidation(
            accepted=False,
            reason="missing_evidence_anchor",
            source_blocks=(),
            due_window=None,
        )
    if not candidate.action_text.strip():
        return CommitmentCandidateValidation(
            accepted=False,
            reason="empty_action_text",
            source_blocks=(),
            due_window=None,
        )
    if (
        isinstance(candidate.confidence, bool)
        or not isinstance(candidate.confidence, int | float)
        or not math.isfinite(float(candidate.confidence))
        or candidate.confidence < 0.0
        or candidate.confidence > 1.0
    ):
        return CommitmentCandidateValidation(
            accepted=False,
            reason="confidence_out_of_range",
            source_blocks=(),
            due_window=None,
        )

    blocks_by_id = {block.block_id: block for block in evidence_blocks}
    source_blocks: list[EvidenceBlock] = []
    for block_id in candidate.evidence_block_ids:
        block = blocks_by_id.get(block_id)
        if block is None:
            return CommitmentCandidateValidation(
                accepted=False,
                reason="unknown_evidence_anchor",
                source_blocks=(),
                due_window=None,
            )
        source_blocks.append(block)

    due_window = None
    if candidate.due_expression is not None:
        due_window = normalize_due_window(
            candidate.due_expression,
            source_timestamp=source_blocks[0].source_timestamp,
        )
        if due_window is None:
            return CommitmentCandidateValidation(
                accepted=True,
                reason="due_window_unparseable",
                source_blocks=tuple(source_blocks),
                due_window=None,
            )

    return CommitmentCandidateValidation(
        accepted=True,
        reason=None,
        source_blocks=tuple(source_blocks),
        due_window=due_window,
    )


def normalize_due_window(
    due_expression: str | None,
    *,
    source_timestamp: datetime,
) -> DueWindow | None:
    if due_expression is None:
        return None
    if source_timestamp.tzinfo is None or source_timestamp.utcoffset() is None:
        return None

    normalized = due_expression.strip()
    if not normalized:
        return None

    try:
        due_date = date.fromisoformat(normalized)
    except ValueError:
        due_date = None
    if due_date is not None and normalized == due_date.isoformat():
        return _date_due_window(
            due_date=due_date,
            source_timezone=source_timestamp.tzinfo,
            source_text=due_expression,
        )

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return DueWindow(start_at=parsed, end_at=None, source_text=due_expression)


def _date_due_window(*, due_date: date, source_timezone: tzinfo, source_text: str) -> DueWindow:
    start_at = datetime.combine(due_date, time.min, tzinfo=source_timezone)
    return DueWindow(
        start_at=start_at,
        end_at=start_at + timedelta(days=1),
        source_text=source_text,
    )


def validate_lifecycle_transition(
    current: CommitmentState,
    target: CommitmentState,
    *,
    source_evidence_is_newer: bool = False,
    user_action: bool = False,
) -> LifecycleTransitionValidation:
    if current == target:
        return LifecycleTransitionValidation(allowed=True, reason=None)

    allowed_targets = _allowed_lifecycle_targets(current)
    if target not in allowed_targets:
        return LifecycleTransitionValidation(allowed=False, reason="transition_not_allowed")
    if target == CommitmentState.RESOLVED and not (source_evidence_is_newer or user_action):
        return LifecycleTransitionValidation(allowed=False, reason="resolution_requires_authority")
    if target == CommitmentState.SUPERSEDED and not source_evidence_is_newer:
        return LifecycleTransitionValidation(
            allowed=False, reason="supersession_requires_later_evidence"
        )
    return LifecycleTransitionValidation(allowed=True, reason=None)


def _allowed_lifecycle_targets(current: CommitmentState) -> set[CommitmentState]:
    match current:
        case CommitmentState.CANDIDATE:
            return {
                CommitmentState.NEEDS_REVIEW,
                CommitmentState.ACTIVE,
                CommitmentState.WAITING_ON_USER,
                CommitmentState.WAITING_ON_COUNTERPARTY,
                CommitmentState.SCHEDULED,
                CommitmentState.REJECTED,
                CommitmentState.DISMISSED,
                CommitmentState.DELETED,
            }
        case CommitmentState.NEEDS_REVIEW:
            return {
                CommitmentState.ACTIVE,
                CommitmentState.WAITING_ON_USER,
                CommitmentState.WAITING_ON_COUNTERPARTY,
                CommitmentState.SCHEDULED,
                CommitmentState.REJECTED,
                CommitmentState.DISMISSED,
                CommitmentState.DELETED,
            }
        case (
            CommitmentState.ACTIVE
            | CommitmentState.WAITING_ON_USER
            | CommitmentState.WAITING_ON_COUNTERPARTY
            | CommitmentState.SCHEDULED
            | CommitmentState.SNOOZED
        ):
            return {
                CommitmentState.ACTIVE,
                CommitmentState.WAITING_ON_USER,
                CommitmentState.WAITING_ON_COUNTERPARTY,
                CommitmentState.SCHEDULED,
                CommitmentState.SNOOZED,
                CommitmentState.RESOLVED,
                CommitmentState.SUPERSEDED,
                CommitmentState.DISMISSED,
                CommitmentState.STALE,
                CommitmentState.EXPIRED,
                CommitmentState.DELETED,
            }
        case (
            CommitmentState.RESOLVED
            | CommitmentState.SUPERSEDED
            | CommitmentState.DISMISSED
            | CommitmentState.REJECTED
            | CommitmentState.STALE
            | CommitmentState.EXPIRED
            | CommitmentState.DELETED
        ):
            return set()
        case _:
            assert_never(current)


def evaluate_follow_up(
    *,
    commitment: WorkCommitment,
    loop: FollowUpLoop,
    now: datetime,
) -> FollowUpEvaluation:
    if loop.commitment_id != commitment.commitment_id:
        return FollowUpEvaluation(action=FollowUpAction.NO_OP, reason="commitment_mismatch")
    if loop.scheduled_version != loop.version:
        return FollowUpEvaluation(action=FollowUpAction.NO_OP, reason="stale_loop")
    if now >= loop.stale_after:
        return FollowUpEvaluation(action=FollowUpAction.NO_OP, reason="stale_loop")
    if _commitment_terminal_state(commitment.state):
        return FollowUpEvaluation(action=FollowUpAction.NO_OP, reason=commitment.state.value)
    if commitment.state in {CommitmentState.CANDIDATE, CommitmentState.NEEDS_REVIEW}:
        return FollowUpEvaluation(action=FollowUpAction.NO_OP, reason=commitment.state.value)
    if loop.snoozed_until is not None and now < loop.snoozed_until:
        return FollowUpEvaluation(
            action=FollowUpAction.NO_OP,
            reason="snoozed",
            next_check_at=loop.snoozed_until,
        )
    if now < loop.scheduled_for:
        return FollowUpEvaluation(
            action=FollowUpAction.WAIT,
            reason="not_scheduled",
            next_check_at=loop.scheduled_for,
        )

    match loop.kind:
        case FollowUpKind.DUE_DATE:
            return FollowUpEvaluation(
                action=FollowUpAction.DELIBERATE,
                reason="scheduled_due_date",
            )
        case FollowUpKind.WAITING_ON_USER:
            if commitment.state != CommitmentState.WAITING_ON_USER:
                return FollowUpEvaluation(
                    action=FollowUpAction.NO_OP,
                    reason="not_waiting_on_user",
                )
            return FollowUpEvaluation(
                action=FollowUpAction.DELIBERATE,
                reason="scheduled_waiting_on_user",
            )
        case FollowUpKind.WAITING_ON_COUNTERPARTY:
            if commitment.state != CommitmentState.WAITING_ON_COUNTERPARTY:
                return FollowUpEvaluation(
                    action=FollowUpAction.NO_OP,
                    reason="not_waiting_on_counterparty",
                )
            return FollowUpEvaluation(
                action=FollowUpAction.DELIBERATE,
                reason="scheduled_waiting_on_counterparty",
            )
        case _:
            assert_never(loop.kind)


def _commitment_terminal_state(state: CommitmentState) -> bool:
    match state:
        case (
            CommitmentState.RESOLVED
            | CommitmentState.SUPERSEDED
            | CommitmentState.DISMISSED
            | CommitmentState.REJECTED
            | CommitmentState.STALE
            | CommitmentState.EXPIRED
            | CommitmentState.DELETED
        ):
            return True
        case (
            CommitmentState.CANDIDATE
            | CommitmentState.NEEDS_REVIEW
            | CommitmentState.ACTIVE
            | CommitmentState.WAITING_ON_USER
            | CommitmentState.WAITING_ON_COUNTERPARTY
            | CommitmentState.SCHEDULED
            | CommitmentState.SNOOZED
        ):
            return False
        case _:
            assert_never(state)
