from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class ResponseContractViolation(Exception):
    def __init__(self, *, contract: str, errors: list[Any]) -> None:
        super().__init__(f"response contract validation failed for {contract}")
        self.contract = contract
        self.errors = errors


class SurfaceSessionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    is_active: bool
    lifecycle_state: str
    created_at: str
    updated_at: str


SurfaceEventType = Literal[
    "evt.turn.started",
    "evt.memory.recalled",
    "evt.memory.candidate_proposed",
    "evt.memory.captured",
    "evt.memory.promoted",
    "evt.memory.corrected",
    "evt.memory.retracted",
    "evt.turn.limit_reached",
    "evt.assistant.emitted",
    "evt.turn.failed",
    "evt.turn.completed",
    "evt.model.started",
    "evt.model.completed",
    "evt.model.failed",
    "evt.action.proposed",
    "evt.action.policy_decided",
    "evt.action.approval.requested",
    "evt.action.approval.expired",
    "evt.action.approval.denied",
    "evt.action.approval.approved",
    "evt.action.execution.started",
    "evt.action.execution.succeeded",
    "evt.action.execution.failed",
]


class SurfaceTurnLimitDetailContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget: str
    unit: str
    limit: int
    measured: int


class SurfaceAppliedTurnLimitsContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_recent_turns: int
    max_context_tokens: int
    max_response_tokens: int
    max_model_attempts: int
    max_turn_wall_time_ms: int


class SurfaceBoundedFailureContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    limit: SurfaceTurnLimitDetailContract


class SurfaceContextRecentWindowContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_recent_turns: int
    included_turn_count: int
    omitted_turn_count: int
    included_turn_ids: list[str]


class SurfaceContextMetadataContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    section_order: list[str]
    policy_instruction_count: int
    recent_window: SurfaceContextRecentWindowContract


class SurfaceModelUsageContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class SurfaceTaintEvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "prior_tool_output_in_context",
        "runtime_provenance_missing",
        "runtime_provenance_evidence_malformed",
    ]
    turn_id: str | None = None
    action_attempt_id: str | None = None
    capability_id: str | None = None
    impact_level: str | None = None


class SurfaceRuntimeProvenanceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["clean", "tainted", "ambiguous"]
    evidence: list[SurfaceTaintEvidenceContract]


class SurfaceModelDeclaredTaintContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["missing", "true", "false", "malformed"]


class SurfaceTaintPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    influenced_by_untrusted_content: bool
    provenance_status: Literal["clean", "tainted", "ambiguous"]
    runtime_provenance: SurfaceRuntimeProvenanceContract
    model_declared_taint: SurfaceModelDeclaredTaintContract


class SurfaceEventTurnStartedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class SurfaceMemoryRecallExclusionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    reason: str


class SurfaceEventMemoryRecalledPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_recalled_memories: int
    included_memory_count: int
    omitted_memory_count: int
    included_memory_ids: list[str]
    excluded_memories: list[SurfaceMemoryRecallExclusionContract]


class SurfaceEventTurnLimitReachedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    limit: SurfaceTurnLimitDetailContract
    applied_limits: SurfaceAppliedTurnLimitsContract


class SurfaceEventAssistantEmittedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    bounded_failure: SurfaceBoundedFailureContract | None = None


class SurfaceEventTurnFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_reason: str
    error_code: str | None = None
    limit: SurfaceTurnLimitDetailContract | None = None


class SurfaceEventTurnCompletedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SurfaceEventModelStartedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    context: SurfaceContextMetadataContract
    attempt: int


class SurfaceEventModelCompletedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    duration_ms: int
    usage: SurfaceModelUsageContract | None = None
    provider_response_id: str | None = None
    attempt: int


class SurfaceEventModelFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    duration_ms: int
    failure_reason: str
    attempt: int


class SurfaceEventActionProposedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    capability_id: str
    input: dict[str, Any]
    taint: SurfaceTaintPayloadContract


class SurfaceEventActionPolicyDecidedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    decision: str
    reason: str
    taint: SurfaceTaintPayloadContract


class SurfaceEventActionApprovalRequestedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    approval_ref: str
    actor_id: str
    expires_at: str


class SurfaceEventActionApprovalExpiredPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    approval_ref: str
    reason: str


class SurfaceEventActionApprovalDeniedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    approval_ref: str
    actor_id: str
    reason: str


class SurfaceEventActionApprovalApprovedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    approval_ref: str
    actor_id: str


class SurfaceEventActionExecutionStartedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    capability_id: str


class SurfaceEventActionExecutionSucceededPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    output: Any


class SurfaceEventActionExecutionFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    error: str
    approval_ref: str | None = None


class SurfaceEventMemoryLifecyclePayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_item_id: str
    revision_id: str
    memory_class: str
    lifecycle_state: str
    memory_key: str
    value_preview: str
    confidence: float
    source_turn_id: str | None


class SurfaceEventEnvelopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    turn_id: str
    sequence: int
    event_type: SurfaceEventType
    payload: dict[str, Any]
    created_at: str


class SurfaceEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    turn_id: str
    sequence: int
    event_type: SurfaceEventType
    payload: dict[str, Any]
    created_at: str


class SurfaceLifecycleProposalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    input_summary: Any


class SurfaceLifecyclePolicyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    reason: str | None


class SurfaceLifecycleApprovalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    reference: str | None
    reason: str | None
    expires_at: str | None
    decided_at: str | None


class SurfaceLifecycleExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    output: Any
    error: str | None


class SurfaceLifecycleItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    proposal_index: int
    proposal: SurfaceLifecycleProposalContract
    policy: SurfaceLifecyclePolicyContract
    approval: SurfaceLifecycleApprovalContract
    execution: SurfaceLifecycleExecutionContract


class SurfaceTurnEnvelopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    user_message: str
    assistant_message: str | None
    status: str
    created_at: str
    updated_at: str
    events: list[dict[str, Any]]
    surface_action_lifecycle: list[dict[str, Any]]


class SurfaceTurnContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    user_message: str
    assistant_message: str | None
    status: str
    created_at: str
    updated_at: str
    events: list[SurfaceEventContract]
    surface_action_lifecycle: list[SurfaceLifecycleItemContract]


class SurfaceAssistantContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    sources: list["SurfaceAssistantSourceContract"]


class SurfaceAssistantSourceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    title: str
    source: str
    retrieved_at: str
    published_at: str | None


class SurfaceApprovalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str
    status: str
    reason: str | None
    expires_at: str
    decided_at: str | None


class SurfaceMessageResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    session: SurfaceSessionContract
    turn: SurfaceTurnContract
    assistant: SurfaceAssistantContract


class SurfaceTimelineResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    session_id: str
    turns: list[SurfaceTurnContract]


class SurfaceRotationContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rotation_id: str
    reason: str
    rotated_from_session_id: str
    idempotency_key: str | None
    idempotent_replay: bool


class SurfaceRotationResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    session: SurfaceSessionContract
    rotation: SurfaceRotationContract


class SurfaceRotationListItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rotation_id: str
    reason: str
    rotated_from_session_id: str
    rotated_to_session_id: str
    idempotency_key: str | None
    actor_id: str
    trigger_snapshot: dict[str, Any]
    created_at: str


class SurfaceRotationListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    rotations: list[SurfaceRotationListItemContract]


class SurfaceApprovalResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    approval: SurfaceApprovalContract
    assistant: SurfaceAssistantContract


class SurfaceArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    title: str
    source: str
    retrieved_at: str
    published_at: str | None


class SurfaceArtifactResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    artifact: SurfaceArtifactContract


class SurfaceMemoryProjectionItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_item_id: str
    memory_key: str
    memory_class: str
    revision_id: str | None
    revision_count: int
    lifecycle_state: str
    value: str
    confidence: float
    source_turn_id: str | None
    source_session_id: str | None
    evidence: Any
    last_verified_at: str
    created_at: str
    updated_at: str


class SurfaceMemoryProjectionResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    items: list[SurfaceMemoryProjectionItemContract]


def _validate_contract(
    contract: str,
    model_type: type[BaseModel],
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        validated = model_type.model_validate(payload)
    except ValidationError as exc:
        raise ResponseContractViolation(contract=contract, errors=exc.errors()) from exc
    return validated.model_dump(mode="python")


def _project_surface_session(raw_session: Any) -> dict[str, Any]:
    session_payload = raw_session if isinstance(raw_session, dict) else {}
    return _validate_contract("surface_session", SurfaceSessionContract, session_payload)


def _default_surface_context_metadata() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "section_order": [
            "policy_system_instructions",
            "recent_active_session_turns",
        ],
        "policy_instruction_count": 0,
        "recent_window": {
            "max_recent_turns": 0,
            "included_turn_count": 0,
            "omitted_turn_count": 0,
            "included_turn_ids": [],
        },
    }


def _coerce_surface_model_usage(raw_usage: Any) -> dict[str, Any] | None:
    if not isinstance(raw_usage, dict):
        return None
    usage: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw_usage.get(key)
        usage[key] = int(value) if isinstance(value, int) else None
    return usage


def _project_surface_event_payload(event_type: SurfaceEventType, raw_payload: Any) -> dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    if event_type == "evt.turn.started":
        return _validate_contract(
            "surface_event_payload.evt.turn.started",
            SurfaceEventTurnStartedPayloadContract,
            payload,
        )
    if event_type == "evt.memory.recalled":
        return _validate_contract(
            "surface_event_payload.evt.memory.recalled",
            SurfaceEventMemoryRecalledPayloadContract,
            payload,
        )
    if event_type == "evt.turn.limit_reached":
        return _validate_contract(
            "surface_event_payload.evt.turn.limit_reached",
            SurfaceEventTurnLimitReachedPayloadContract,
            payload,
        )
    if event_type == "evt.assistant.emitted":
        return _validate_contract(
            "surface_event_payload.evt.assistant.emitted",
            SurfaceEventAssistantEmittedPayloadContract,
            payload,
        )
    if event_type == "evt.turn.failed":
        return _validate_contract(
            "surface_event_payload.evt.turn.failed",
            SurfaceEventTurnFailedPayloadContract,
            payload,
        )
    if event_type == "evt.turn.completed":
        return _validate_contract(
            "surface_event_payload.evt.turn.completed",
            SurfaceEventTurnCompletedPayloadContract,
            payload,
        )
    if event_type == "evt.model.started":
        context_payload = payload.get("context")
        normalized_payload = dict(payload)
        if not isinstance(context_payload, dict):
            normalized_payload["context"] = _default_surface_context_metadata()
        if not isinstance(normalized_payload.get("attempt"), int):
            normalized_payload["attempt"] = 1
        return _validate_contract(
            "surface_event_payload.evt.model.started",
            SurfaceEventModelStartedPayloadContract,
            normalized_payload,
        )
    if event_type == "evt.model.completed":
        normalized_payload = dict(payload)
        normalized_payload["usage"] = _coerce_surface_model_usage(payload.get("usage"))
        if not isinstance(normalized_payload.get("attempt"), int):
            normalized_payload["attempt"] = 1
        return _validate_contract(
            "surface_event_payload.evt.model.completed",
            SurfaceEventModelCompletedPayloadContract,
            normalized_payload,
        )
    if event_type == "evt.model.failed":
        normalized_payload = dict(payload)
        if not isinstance(normalized_payload.get("attempt"), int):
            normalized_payload["attempt"] = 1
        return _validate_contract(
            "surface_event_payload.evt.model.failed",
            SurfaceEventModelFailedPayloadContract,
            normalized_payload,
        )
    if event_type == "evt.action.proposed":
        return _validate_contract(
            "surface_event_payload.evt.action.proposed",
            SurfaceEventActionProposedPayloadContract,
            payload,
        )
    if event_type == "evt.action.policy_decided":
        return _validate_contract(
            "surface_event_payload.evt.action.policy_decided",
            SurfaceEventActionPolicyDecidedPayloadContract,
            payload,
        )
    if event_type == "evt.action.approval.requested":
        return _validate_contract(
            "surface_event_payload.evt.action.approval.requested",
            SurfaceEventActionApprovalRequestedPayloadContract,
            payload,
        )
    if event_type == "evt.action.approval.expired":
        return _validate_contract(
            "surface_event_payload.evt.action.approval.expired",
            SurfaceEventActionApprovalExpiredPayloadContract,
            payload,
        )
    if event_type == "evt.action.approval.denied":
        return _validate_contract(
            "surface_event_payload.evt.action.approval.denied",
            SurfaceEventActionApprovalDeniedPayloadContract,
            payload,
        )
    if event_type == "evt.action.approval.approved":
        return _validate_contract(
            "surface_event_payload.evt.action.approval.approved",
            SurfaceEventActionApprovalApprovedPayloadContract,
            payload,
        )
    if event_type == "evt.action.execution.started":
        return _validate_contract(
            "surface_event_payload.evt.action.execution.started",
            SurfaceEventActionExecutionStartedPayloadContract,
            payload,
        )
    if event_type == "evt.action.execution.succeeded":
        return _validate_contract(
            "surface_event_payload.evt.action.execution.succeeded",
            SurfaceEventActionExecutionSucceededPayloadContract,
            payload,
        )
    if event_type == "evt.action.execution.failed":
        return _validate_contract(
            "surface_event_payload.evt.action.execution.failed",
            SurfaceEventActionExecutionFailedPayloadContract,
            payload,
        )
    if event_type in {
        "evt.memory.candidate_proposed",
        "evt.memory.captured",
        "evt.memory.promoted",
        "evt.memory.corrected",
        "evt.memory.retracted",
    }:
        return _validate_contract(
            f"surface_event_payload.{event_type}",
            SurfaceEventMemoryLifecyclePayloadContract,
            payload,
        )
    raise ResponseContractViolation(
        contract="surface_event_payload",
        errors=[
            {
                "loc": ("event_type",),
                "msg": f"unsupported event type: {event_type}",
                "type": "value_error",
            }
        ],
    )


def _project_surface_event(raw_event: Any) -> dict[str, Any]:
    envelope_payload = raw_event if isinstance(raw_event, dict) else {}
    validated_envelope = _validate_contract(
        "surface_event_envelope",
        SurfaceEventEnvelopeContract,
        envelope_payload,
    )
    event_type = validated_envelope["event_type"]
    payload = _project_surface_event_payload(event_type, validated_envelope["payload"])
    return _validate_contract(
        "surface_event",
        SurfaceEventContract,
        {
            "id": validated_envelope["id"],
            "turn_id": validated_envelope["turn_id"],
            "sequence": validated_envelope["sequence"],
            "event_type": event_type,
            "payload": payload,
            "created_at": validated_envelope["created_at"],
        },
    )


def _project_surface_lifecycle_item(raw_item: Any) -> dict[str, Any]:
    lifecycle_payload = raw_item if isinstance(raw_item, dict) else {}
    return _validate_contract("surface_lifecycle_item", SurfaceLifecycleItemContract, lifecycle_payload)


def _project_surface_turn(raw_turn: Any) -> dict[str, Any]:
    turn_payload = raw_turn if isinstance(raw_turn, dict) else {}
    validated_turn_envelope = _validate_contract(
        "surface_turn_envelope",
        SurfaceTurnEnvelopeContract,
        turn_payload,
    )
    events_list = validated_turn_envelope["events"]
    lifecycle_list = validated_turn_envelope["surface_action_lifecycle"]

    return _validate_contract(
        "surface_turn",
        SurfaceTurnContract,
        {
            "id": validated_turn_envelope["id"],
            "session_id": validated_turn_envelope["session_id"],
            "user_message": validated_turn_envelope["user_message"],
            "assistant_message": validated_turn_envelope["assistant_message"],
            "status": validated_turn_envelope["status"],
            "created_at": validated_turn_envelope["created_at"],
            "updated_at": validated_turn_envelope["updated_at"],
            "events": [_project_surface_event(raw_event) for raw_event in events_list],
            "surface_action_lifecycle": [
                _project_surface_lifecycle_item(raw_item) for raw_item in lifecycle_list
            ],
        },
    )


def build_surface_message_response(
    *,
    session: Any,
    turn: Any,
    assistant_message: Any,
    assistant_sources: Any,
) -> dict[str, Any]:
    sources_payload = assistant_sources if isinstance(assistant_sources, list) else []
    return _validate_contract(
        "surface_message_response",
        SurfaceMessageResponseContract,
        {
            "ok": True,
            "session": _project_surface_session(session),
            "turn": _project_surface_turn(turn),
            # PR-06 deprecates assistant.provider/model for surfaced responses.
            "assistant": {"message": assistant_message, "sources": sources_payload},
        },
    )


def build_surface_timeline_response(*, session_id: Any, turns: Any) -> dict[str, Any]:
    turns_payload = turns if isinstance(turns, list) else []
    return _validate_contract(
        "surface_timeline_response",
        SurfaceTimelineResponseContract,
        {
            "ok": True,
            "session_id": session_id,
            "turns": [_project_surface_turn(raw_turn) for raw_turn in turns_payload],
        },
    )


def build_surface_rotation_response(*, session: Any, rotation: Any) -> dict[str, Any]:
    rotation_payload = rotation if isinstance(rotation, dict) else {}
    return _validate_contract(
        "surface_rotation_response",
        SurfaceRotationResponseContract,
        {
            "ok": True,
            "session": _project_surface_session(session),
            "rotation": {
                "rotation_id": rotation_payload.get("rotation_id"),
                "reason": rotation_payload.get("reason"),
                "rotated_from_session_id": rotation_payload.get("rotated_from_session_id"),
                "idempotency_key": rotation_payload.get("idempotency_key"),
                "idempotent_replay": rotation_payload.get("idempotent_replay"),
            },
        },
    )


def build_surface_rotation_list_response(*, rotations: Any) -> dict[str, Any]:
    rotations_payload = rotations if isinstance(rotations, list) else []
    return _validate_contract(
        "surface_rotation_list_response",
        SurfaceRotationListResponseContract,
        {
            "ok": True,
            "rotations": rotations_payload,
        },
    )


def build_surface_approval_response(
    *,
    approval: Any,
    assistant_message: Any,
) -> dict[str, Any]:
    approval_payload = approval if isinstance(approval, dict) else {}
    return _validate_contract(
        "surface_approval_response",
        SurfaceApprovalResponseContract,
        {
            "ok": True,
            "approval": {
                "reference": approval_payload.get("reference"),
                "status": approval_payload.get("status"),
                "reason": approval_payload.get("reason"),
                "expires_at": approval_payload.get("expires_at"),
                "decided_at": approval_payload.get("decided_at"),
            },
            "assistant": {"message": assistant_message, "sources": []},
        },
    )


def build_surface_memory_projection_response(*, items: Any) -> dict[str, Any]:
    items_payload = items if isinstance(items, list) else []
    return _validate_contract(
        "surface_memory_projection_response",
        SurfaceMemoryProjectionResponseContract,
        {
            "ok": True,
            "items": items_payload,
        },
    )


def build_surface_artifact_response(*, artifact: Any) -> dict[str, Any]:
    artifact_payload = artifact if isinstance(artifact, dict) else {}
    return _validate_contract(
        "surface_artifact_response",
        SurfaceArtifactResponseContract,
        {
            "ok": True,
            "artifact": {
                "id": artifact_payload.get("id"),
                "type": artifact_payload.get("type"),
                "title": artifact_payload.get("title"),
                "source": artifact_payload.get("source"),
                "retrieved_at": artifact_payload.get("retrieved_at"),
                "published_at": artifact_payload.get("published_at"),
            },
        },
    )
