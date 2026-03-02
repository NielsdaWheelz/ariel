from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from ariel.capability_registry import canonical_action_payload, get_capability, payload_hash
from ariel.executor import (
    append_turn_event,
    build_assistant_action_appendix,
    execute_capability,
    next_turn_event_sequence,
)
from ariel.persistence import ActionAttemptRecord, ApprovalRequestRecord, TurnRecord, to_rfc3339
from ariel.policy_engine import evaluate_proposal


class ActionRuntimeError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any],
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable


@dataclass(slots=True)
class ProposalProcessingResult:
    assistant_message: str
    action_attempts: list[ActionAttemptRecord]


@dataclass(slots=True)
class ApprovalDecisionResult:
    approval: ApprovalRequestRecord
    action_attempt: ActionAttemptRecord
    assistant_message: str


def process_action_proposals(
    *,
    db: Session,
    session_id: str,
    turn: TurnRecord,
    assistant_message: str,
    proposals_raw: Any,
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> ProposalProcessingResult:
    inline_results: list[dict[str, Any]] = []
    pending_approvals: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []
    created_action_attempts: list[ActionAttemptRecord] = []
    pending_approval_created = False

    proposals = proposals_raw if isinstance(proposals_raw, list) else []
    for proposal_index, proposal_raw in enumerate(proposals, start=1):
        proposal_payload = proposal_raw if isinstance(proposal_raw, dict) else {}
        capability_id_raw = proposal_payload.get("capability_id")
        capability_id = (
            capability_id_raw.strip()
            if isinstance(capability_id_raw, str) and capability_id_raw.strip()
            else "invalid.capability"
        )
        raw_input_payload = proposal_payload.get("input")
        input_payload = jsonable_encoder(raw_input_payload) if isinstance(raw_input_payload, dict) else {}
        evaluation = evaluate_proposal(
            capability_id=capability_id,
            input_payload=input_payload,
            pending_approval_exists=pending_approval_created,
        )

        now_action = now_fn()
        frozen_input_payload = (
            evaluation.normalized_input if evaluation.normalized_input is not None else input_payload
        )
        frozen_payload = canonical_action_payload(
            capability_id=capability_id,
            input_payload=frozen_input_payload,
        )
        action_attempt = ActionAttemptRecord(
            id=new_id_fn("aat"),
            session_id=session_id,
            turn_id=turn.id,
            proposal_index=proposal_index,
            capability_id=capability_id,
            capability_version="1.0",
            impact_level=evaluation.impact_level,
            proposed_input=frozen_input_payload,
            payload_hash=payload_hash(frozen_payload),
            policy_decision="deny",
            policy_reason=None,
            status="proposed",
            approval_required=False,
            execution_output=None,
            execution_error=None,
            created_at=now_action,
            updated_at=now_action,
        )
        db.add(action_attempt)
        db.flush()
        created_action_attempts.append(action_attempt)
        add_event(
            "evt.action.proposed",
            {
                "action_attempt_id": action_attempt.id,
                "capability_id": action_attempt.capability_id,
                "input": action_attempt.proposed_input,
            },
        )

        if evaluation.decision == "deny":
            action_attempt.status = "rejected"
            action_attempt.policy_decision = "deny"
            action_attempt.policy_reason = evaluation.reason
            action_attempt.updated_at = now_fn()
            blocked_reasons.append(f"{capability_id}: {evaluation.reason}")
            add_event(
                "evt.action.policy_decided",
                {
                    "action_attempt_id": action_attempt.id,
                    "decision": "deny",
                    "reason": evaluation.reason,
                },
            )
            continue

        if evaluation.decision == "requires_approval":
            action_attempt.status = "awaiting_approval"
            action_attempt.policy_decision = "requires_approval"
            action_attempt.policy_reason = evaluation.reason
            action_attempt.approval_required = True
            action_attempt.updated_at = now_fn()
            add_event(
                "evt.action.policy_decided",
                {
                    "action_attempt_id": action_attempt.id,
                    "decision": "requires_approval",
                    "reason": evaluation.reason,
                },
            )

            approval_expires_at = now_fn() + timedelta(seconds=approval_ttl_seconds)
            approval_request = ApprovalRequestRecord(
                id=new_id_fn("apr"),
                action_attempt_id=action_attempt.id,
                session_id=session_id,
                turn_id=turn.id,
                actor_id=approval_actor_id,
                status="pending",
                payload_hash=action_attempt.payload_hash,
                expires_at=approval_expires_at,
                decision_reason=None,
                decided_at=None,
                created_at=now_fn(),
                updated_at=now_fn(),
            )
            db.add(approval_request)
            db.flush()
            action_attempt.approval_request = approval_request
            pending_approval_created = True
            pending_approvals.append(
                {
                    "approval_id": approval_request.id,
                    "capability_id": capability_id,
                    "expires_at": to_rfc3339(approval_request.expires_at),
                }
            )
            add_event(
                "evt.action.approval.requested",
                {
                    "action_attempt_id": action_attempt.id,
                    "approval_id": approval_request.id,
                    "actor_id": approval_request.actor_id,
                    "expires_at": to_rfc3339(approval_request.expires_at),
                },
            )
            continue

        if evaluation.capability is None or evaluation.normalized_input is None:
            action_attempt.status = "rejected"
            action_attempt.policy_decision = "deny"
            action_attempt.policy_reason = "policy_invariant_violation"
            action_attempt.updated_at = now_fn()
            blocked_reasons.append(f"{capability_id}: policy_invariant_violation")
            add_event(
                "evt.action.policy_decided",
                {
                    "action_attempt_id": action_attempt.id,
                    "decision": "deny",
                    "reason": "policy_invariant_violation",
                },
            )
            continue

        action_attempt.status = "executing"
        action_attempt.policy_decision = "allow_inline"
        action_attempt.policy_reason = None
        action_attempt.updated_at = now_fn()
        add_event(
            "evt.action.policy_decided",
            {
                "action_attempt_id": action_attempt.id,
                "decision": "allow_inline",
                "reason": evaluation.reason,
            },
        )
        add_event(
            "evt.action.execution.started",
            {
                "action_attempt_id": action_attempt.id,
                "capability_id": capability_id,
            },
        )
        execution_result = execute_capability(
            capability=evaluation.capability,
            normalized_input=evaluation.normalized_input,
        )
        if execution_result.status == "succeeded" and execution_result.output is not None:
            action_attempt.execution_output = execution_result.output
            action_attempt.execution_error = None
            action_attempt.status = "succeeded"
            action_attempt.updated_at = now_fn()
            inline_results.append(
                {
                    "capability_id": capability_id,
                    "output": execution_result.output,
                }
            )
            add_event(
                "evt.action.execution.succeeded",
                {
                    "action_attempt_id": action_attempt.id,
                    "output": execution_result.output,
                },
            )
            continue

        action_attempt.execution_output = None
        action_attempt.execution_error = execution_result.error or "execution_output_missing"
        action_attempt.status = "failed"
        action_attempt.updated_at = now_fn()
        blocked_reasons.append(f"{capability_id}: execution_failed")
        add_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "error": execution_result.error,
            },
        )

    appendix = build_assistant_action_appendix(
        inline_results=inline_results,
        pending_approvals=pending_approvals,
        blocked_reasons=blocked_reasons,
    )
    final_assistant_message = f"{assistant_message}\n{appendix}" if appendix else assistant_message
    return ProposalProcessingResult(
        assistant_message=final_assistant_message,
        action_attempts=created_action_attempts,
    )


def resolve_approval_decision(
    *,
    db: Session,
    approval_id: str,
    decision: Literal["approve", "deny"],
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> ApprovalDecisionResult:
    approval = db.scalar(
        select(ApprovalRequestRecord)
        .where(ApprovalRequestRecord.id == approval_id)
        .with_for_update()
        .limit(1)
    )
    if approval is None:
        raise ActionRuntimeError(
            status_code=404,
            code="E_APPROVAL_NOT_FOUND",
            message="approval request not found",
            details={"approval_id": approval_id},
            retryable=False,
        )

    action_attempt = db.scalar(
        select(ActionAttemptRecord)
        .where(ActionAttemptRecord.id == approval.action_attempt_id)
        .with_for_update()
        .limit(1)
    )
    if action_attempt is None:
        msg = "approval references missing action attempt"
        raise RuntimeError(msg)

    if actor_id != approval.actor_id:
        raise ActionRuntimeError(
            status_code=403,
            code="E_APPROVAL_ACTOR_MISMATCH",
            message="approval actor does not match the pending request",
            details={
                "approval_id": approval.id,
                "expected_actor_id": approval.actor_id,
                "received_actor_id": actor_id,
            },
            retryable=False,
        )

    if approval.status != "pending":
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_NOT_PENDING",
            message="approval request is already resolved",
            details={
                "approval_id": approval.id,
                "status": approval.status,
                "action_attempt_id": approval.action_attempt_id,
            },
            retryable=False,
        )

    sequence = next_turn_event_sequence(db=db, turn_id=approval.turn_id) - 1

    def add_approval_event(event_type: str, payload_data: dict[str, Any]) -> None:
        nonlocal sequence
        sequence += 1
        append_turn_event(
            db=db,
            session_id=approval.session_id,
            turn_id=approval.turn_id,
            sequence=sequence,
            event_type=event_type,
            payload_data=payload_data,
            new_id_fn=new_id_fn,
            now_fn=now_fn,
        )

    now = now_fn()
    if now > approval.expires_at:
        approval.status = "expired"
        approval.decision_reason = "approval_expired"
        approval.decided_at = now
        approval.updated_at = now
        action_attempt.status = "expired"
        action_attempt.policy_reason = "approval_expired"
        action_attempt.updated_at = now
        add_approval_event(
            "evt.action.approval.expired",
            {
                "action_attempt_id": action_attempt.id,
                "approval_id": approval.id,
                "reason": "approval_expired",
            },
        )
        db.flush()
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_EXPIRED",
            message="approval request has expired",
            details={
                "approval_id": approval.id,
                "action_attempt_id": action_attempt.id,
                "expires_at": to_rfc3339(approval.expires_at),
            },
            retryable=False,
        )

    if decision == "deny":
        approval.status = "denied"
        approval.decision_reason = reason or "denied_by_actor"
        approval.decided_at = now
        approval.updated_at = now
        action_attempt.status = "denied"
        action_attempt.policy_reason = "approval_denied"
        action_attempt.updated_at = now
        add_approval_event(
            "evt.action.approval.denied",
            {
                "action_attempt_id": action_attempt.id,
                "approval_id": approval.id,
                "actor_id": approval.actor_id,
                "reason": approval.decision_reason,
            },
        )
        db.flush()
        return ApprovalDecisionResult(
            approval=approval,
            action_attempt=action_attempt,
            assistant_message="approval denied. action was not executed.",
        )

    expected_hash = payload_hash(
        canonical_action_payload(
            capability_id=action_attempt.capability_id,
            input_payload=action_attempt.proposed_input,
        )
    )
    if expected_hash != approval.payload_hash or expected_hash != action_attempt.payload_hash:
        approval.status = "expired"
        approval.decision_reason = "payload_hash_mismatch"
        approval.decided_at = now
        approval.updated_at = now
        action_attempt.status = "failed"
        action_attempt.execution_error = "approval payload mismatch"
        action_attempt.policy_reason = "payload_hash_mismatch"
        action_attempt.updated_at = now
        add_approval_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "approval_id": approval.id,
                "error": "approval payload mismatch",
            },
        )
        db.flush()
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_PAYLOAD_MISMATCH",
            message="approval payload mismatch",
            details={
                "approval_id": approval.id,
                "action_attempt_id": action_attempt.id,
            },
            retryable=False,
        )

    approval.status = "approved"
    approval.decision_reason = reason or "approved_by_actor"
    approval.decided_at = now
    approval.updated_at = now
    action_attempt.status = "approved"
    action_attempt.policy_reason = "approval_approved"
    action_attempt.updated_at = now
    add_approval_event(
        "evt.action.approval.approved",
        {
            "action_attempt_id": action_attempt.id,
            "approval_id": approval.id,
            "actor_id": approval.actor_id,
        },
    )

    action_attempt.status = "executing"
    action_attempt.updated_at = now_fn()
    add_approval_event(
        "evt.action.execution.started",
        {
            "action_attempt_id": action_attempt.id,
            "capability_id": action_attempt.capability_id,
        },
    )

    capability = get_capability(action_attempt.capability_id)
    if capability is None:
        action_attempt.status = "failed"
        action_attempt.execution_error = "unknown_capability"
        action_attempt.policy_reason = "unknown_capability"
        action_attempt.updated_at = now_fn()
        add_approval_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "error": "unknown_capability",
            },
        )
    else:
        normalized_input, input_error = capability.validate_input(action_attempt.proposed_input)
        if input_error is not None or normalized_input is None:
            action_attempt.status = "failed"
            action_attempt.execution_error = "schema_invalid"
            action_attempt.policy_reason = "schema_invalid"
            action_attempt.updated_at = now_fn()
            add_approval_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": "schema_invalid",
                },
            )
        else:
            execution_result = execute_capability(
                capability=capability,
                normalized_input=normalized_input,
            )
            if execution_result.status == "succeeded" and execution_result.output is not None:
                action_attempt.status = "succeeded"
                action_attempt.execution_output = execution_result.output
                action_attempt.execution_error = None
                action_attempt.updated_at = now_fn()
                add_approval_event(
                    "evt.action.execution.succeeded",
                    {
                        "action_attempt_id": action_attempt.id,
                        "output": execution_result.output,
                    },
                )
            else:
                action_attempt.status = "failed"
                action_attempt.execution_output = None
                action_attempt.execution_error = execution_result.error or "execution_output_missing"
                action_attempt.updated_at = now_fn()
                add_approval_event(
                    "evt.action.execution.failed",
                    {
                        "action_attempt_id": action_attempt.id,
                        "error": execution_result.error,
                    },
                )

    db.flush()
    assistant_message = (
        "approved action executed successfully."
        if action_attempt.status == "succeeded"
        else "approval recorded, but action execution failed."
    )
    return ApprovalDecisionResult(
        approval=approval,
        action_attempt=action_attempt,
        assistant_message=assistant_message,
    )
