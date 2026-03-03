from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ariel.capability_registry import (
    CapabilityDefinition,
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.executor import (
    append_turn_event,
    build_assistant_action_appendix,
    execute_capability,
    next_turn_event_sequence,
)
from ariel.persistence import ActionAttemptRecord, ApprovalRequestRecord, TurnRecord, to_rfc3339
from ariel.policy_engine import evaluate_proposal

_SIDE_EFFECT_EXECUTION_LOCK_ID = 24_310_002

ModelDeclaredTaintStatus = Literal["missing", "true", "false", "malformed"]
ProposalProvenanceStatus = Literal["clean", "tainted", "ambiguous"]


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


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    status: Literal["clean", "tainted"]
    evidence: tuple[dict[str, Any], ...] = ()


def _execution_integrity_error(
    *,
    action_attempt: ActionAttemptRecord,
    capability: CapabilityDefinition,
) -> str | None:
    if action_attempt.capability_id != capability.capability_id:
        return "integrity_mismatch:capability_id"
    if action_attempt.capability_version != capability.version:
        return "integrity_mismatch:capability_version"
    runtime_contract_hash = capability_contract_hash(capability)
    if action_attempt.capability_contract_hash != runtime_contract_hash:
        return "integrity_mismatch:capability_contract"
    return None


def _acquire_side_effect_execution_lock(
    *,
    db: Session,
    impact_level: str,
) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    if impact_level == "read":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _SIDE_EFFECT_EXECUTION_LOCK_ID},
    )


def _model_declared_taint_status(proposal_payload: dict[str, Any]) -> ModelDeclaredTaintStatus:
    if "influenced_by_untrusted_content" not in proposal_payload:
        return "missing"
    raw_value = proposal_payload.get("influenced_by_untrusted_content")
    if raw_value is True:
        return "true"
    if raw_value is False:
        return "false"
    return "malformed"


def _effective_provenance_status(
    *,
    runtime_provenance: RuntimeProvenance | None,
    model_declared_taint_status: ModelDeclaredTaintStatus,
) -> ProposalProvenanceStatus:
    if runtime_provenance is None:
        if model_declared_taint_status == "true":
            return "tainted"
        return "ambiguous"
    if runtime_provenance.status == "tainted":
        return "tainted"
    if runtime_provenance.status != "clean":
        return "ambiguous"
    if model_declared_taint_status == "true":
        return "tainted"
    if model_declared_taint_status == "malformed":
        return "ambiguous"
    return "clean"


def _taint_event_payload(
    *,
    provenance_status: ProposalProvenanceStatus,
    runtime_provenance: RuntimeProvenance | None,
    model_declared_taint_status: ModelDeclaredTaintStatus,
) -> dict[str, Any]:
    runtime_status = runtime_provenance.status if runtime_provenance is not None else "ambiguous"
    evidence: list[dict[str, Any]] = []
    if runtime_provenance is None:
        evidence.append({"kind": "runtime_provenance_missing"})
    else:
        for item in runtime_provenance.evidence:
            if isinstance(item, dict):
                evidence.append(dict(item))
            else:
                evidence.append({"kind": "runtime_provenance_evidence_malformed"})
    return {
        "influenced_by_untrusted_content": provenance_status in {"tainted", "ambiguous"},
        "provenance_status": provenance_status,
        "runtime_provenance": {
            "status": runtime_status,
            "evidence": evidence,
        },
        "model_declared_taint": {
            "status": model_declared_taint_status,
        },
    }


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
    runtime_provenance: RuntimeProvenance | None = None,
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
        model_declared_taint_status = _model_declared_taint_status(proposal_payload)
        provenance_status = _effective_provenance_status(
            runtime_provenance=runtime_provenance,
            model_declared_taint_status=model_declared_taint_status,
        )
        taint_payload = _taint_event_payload(
            provenance_status=provenance_status,
            runtime_provenance=runtime_provenance,
            model_declared_taint_status=model_declared_taint_status,
        )
        evaluation = evaluate_proposal(
            capability_id=capability_id,
            input_payload=input_payload,
            pending_approval_exists=pending_approval_created,
            influenced_by_untrusted_content=taint_payload["influenced_by_untrusted_content"],
            provenance_status=provenance_status,
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
            capability_version=(
                evaluation.capability.version if evaluation.capability is not None else "unknown"
            ),
            capability_contract_hash=(
                capability_contract_hash(evaluation.capability)
                if evaluation.capability is not None
                else payload_hash({"capability_id": capability_id, "contract": "unknown"})
            ),
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
                "taint": taint_payload,
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
                    "taint": taint_payload,
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
                    "taint": taint_payload,
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
                    "approval_ref": approval_request.id,
                    "capability_id": capability_id,
                    "expires_at": to_rfc3339(approval_request.expires_at),
                }
            )
            add_event(
                "evt.action.approval.requested",
                {
                    "action_attempt_id": action_attempt.id,
                    "approval_ref": approval_request.id,
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
                    "taint": taint_payload,
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
                "taint": taint_payload,
            },
        )
        integrity_error = _execution_integrity_error(
            action_attempt=action_attempt,
            capability=evaluation.capability,
        )
        if integrity_error is not None:
            action_attempt.execution_output = None
            action_attempt.execution_error = integrity_error
            action_attempt.status = "failed"
            action_attempt.policy_reason = "integrity_mismatch"
            action_attempt.updated_at = now_fn()
            blocked_reasons.append(f"{capability_id}: {integrity_error}")
            add_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": integrity_error,
                },
            )
            continue

        add_event(
            "evt.action.execution.started",
            {
                "action_attempt_id": action_attempt.id,
                "capability_id": capability_id,
            },
        )
        _acquire_side_effect_execution_lock(
            db=db,
            impact_level=evaluation.capability.impact_level,
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
        blocked_reasons.append(
            f"{capability_id}: {execution_result.error or 'execution_output_missing'}"
        )
        add_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "error": action_attempt.execution_error,
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
    approval_ref: str,
    decision: Literal["approve", "deny"],
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> ApprovalDecisionResult:
    approval = db.scalar(
        select(ApprovalRequestRecord)
        .where(ApprovalRequestRecord.id == approval_ref)
        .with_for_update()
        .limit(1)
    )
    if approval is None:
        raise ActionRuntimeError(
            status_code=404,
            code="E_APPROVAL_NOT_FOUND",
            message="approval request not found",
            details={"approval_ref": approval_ref},
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
                "approval_ref": approval.id,
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
                "approval_ref": approval.id,
                "status": approval.status,
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
                "approval_ref": approval.id,
                "reason": "approval_expired",
            },
        )
        db.flush()
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_EXPIRED",
            message="approval request has expired",
            details={
                "approval_ref": approval.id,
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
                "approval_ref": approval.id,
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
                "approval_ref": approval.id,
                "error": "approval payload mismatch",
            },
        )
        db.flush()
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_PAYLOAD_MISMATCH",
            message="approval payload mismatch",
            details={
                "approval_ref": approval.id,
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
            "approval_ref": approval.id,
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
        integrity_error = _execution_integrity_error(
            action_attempt=action_attempt,
            capability=capability,
        )
        if integrity_error is not None:
            action_attempt.status = "failed"
            action_attempt.execution_error = integrity_error
            action_attempt.policy_reason = "integrity_mismatch"
            action_attempt.updated_at = now_fn()
            add_approval_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": integrity_error,
                },
            )
        else:
            _acquire_side_effect_execution_lock(
                db=db,
                impact_level=action_attempt.impact_level,
            )
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
                    action_attempt.execution_error = (
                        execution_result.error or "execution_output_missing"
                    )
                    action_attempt.updated_at = now_fn()
                    add_approval_event(
                        "evt.action.execution.failed",
                        {
                            "action_attempt_id": action_attempt.id,
                            "error": action_attempt.execution_error,
                        },
                    )

    db.flush()
    if action_attempt.status == "succeeded":
        assistant_message = "approved action executed successfully."
    else:
        failure_reason = action_attempt.execution_error or "execution_failed"
        if failure_reason.startswith("integrity_mismatch"):
            failure_reason = failure_reason.replace("integrity_mismatch", "integrity mismatch", 1)
        assistant_message = f"approval recorded, but action execution failed: {failure_reason}"
    return ApprovalDecisionResult(
        approval=approval,
        action_attempt=action_attempt,
        assistant_message=assistant_message,
    )
