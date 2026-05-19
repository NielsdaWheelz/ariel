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
    "evt.memory.recall_failed",
    "evt.memory.remember_queued",
    "evt.ai_judgment.failed",
    "evt.ai_judgment.completed",
    "evt.assistant.emitted",
    "evt.turn.failed",
    "evt.turn.completed",
    "evt.model.started",
    "evt.model.completed",
    "evt.model.failed",
    "evt.model.protocol_failed",
    "evt.run.validation_failed",
    "evt.agent.value_emitted",
    "evt.agent.output_not_applied",
    "evt.action.call_denied",
    "evt.action.proposed",
    "evt.action.policy_decided",
    "evt.action.approval.requested",
    "evt.action.approval.expired",
    "evt.action.approval.denied",
    "evt.action.approval.approved",
    "evt.action.execution.started",
    "evt.action.execution.succeeded",
    "evt.action.execution.failed",
    "evt.action.execution.retrying",
    "evt.provider_write.reconcile_unavailable",
    "evt.provider_write.receipt_reconciled",
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
    main_turn_budget_seconds: float
    agent_loop_max_model_calls: int


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
    current_turn_id: str | None = None
    recent_window: SurfaceContextRecentWindowContract


class SurfaceModelUsageContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class SurfaceTaintEvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "prior_tool_output_in_context",
        "runtime_provenance_missing",
        "runtime_provenance_evidence_malformed",
        "capture_shared_content_ingress",
        "attachment_content_read",
    ]
    turn_id: str | None = None
    action_attempt_id: str | None = None
    capability_id: str | None = None
    impact_level: str | None = None
    attachment_ref: str | None = None
    filename: str | None = None
    modality: str | None = None


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
    discord: dict[str, Any] | None


class SurfaceEventMemoryRecalledPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    recalled_fact_ids: list[str]


class SurfaceEventMemoryRecallFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    failure_reason: str


class SurfaceEventMemoryRememberQueuedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    turn_id: str


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
    model_call_count: int


class SurfaceEventModelCompletedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    duration_ms: int
    usage: SurfaceModelUsageContract | None = None
    provider_response_id: str | None = None
    model_call_count: int


class SurfaceEventModelFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    duration_ms: int
    failure_reason: str
    model_call_count: int
    failure_code: str | None = None
    parse_status: str | None = None
    validation_status: Literal["valid", "invalid", "not_validated"] | None = None
    usage: SurfaceModelUsageContract | None = None
    provider_response_id: str | None = None
    response_output_shape: dict[str, Any] | None = None


class SurfaceEventModelProtocolFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    model_call_count: int
    provider_response_id: str | None = None


class SurfaceEventRunValidationFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    errors: list[str]
    model_call_count: int
    provider_response_id: str | None = None


class SurfaceEventAgentValueEmittedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    value_digest: str
    value_bytes: int
    model_call_count: int
    provider_response_id: str | None = None


class SurfaceEventAgentOutputNotAppliedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    current_turn_id: str | None = None


class SurfaceEventActionProposedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    capability_id: str
    input: dict[str, Any]
    taint: SurfaceTaintPayloadContract


class SurfaceEventActionCallDeniedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_index: int
    call_id: str | None = None
    tool_name: str
    capability_id: str
    reason: str


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
    task_id: str | None = None


class SurfaceEventActionExecutionSucceededPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    output: Any
    provider_write_receipt_id: str | None = None
    replayed_provider_write_receipt_id: str | None = None
    reconciled: bool | None = None


class SurfaceEventActionExecutionFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    error: str
    approval_ref: str | None = None
    output: Any = None


class SurfaceEventActionExecutionRetryingPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    error: str


class SurfaceEventProviderWriteReconcileUnavailablePayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    provider_write_receipt_id: str
    status: Literal["succeeded", "failed", "ambiguous"]
    reason: str
    reconcile_task_enqueued: bool
    reconcile_task_id: str | None = None


class SurfaceEventProviderWriteReceiptReconciledPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_attempt_id: str
    expected_provider_write_receipt_id: str
    provider_write_receipt_id: str


class SurfaceEventAIJudgmentPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judgment_type: Literal["memory_recall", "memory_remember", "model_output"]
    parse_status: (
        Literal[
            "parsed",
            "invalid_json",
            "missing_output",
            "schema_invalid",
        ]
        | None
    ) = None
    validation_status: Literal["valid", "invalid", "not_validated"] | None = None
    code: str | None = None
    failure_code: (
        Literal[
            "E_AI_JUDGMENT_REQUIRED",
            "E_AI_JUDGMENT_CREDENTIALS",
            "E_AI_JUDGMENT_TIMEOUT",
            "E_AI_JUDGMENT_INVALID_JSON",
            "E_AI_JUDGMENT_SCHEMA",
            "E_AI_JUDGMENT_VALIDATION",
            "E_AI_JUDGMENT_BUDGET",
        ]
        | None
    ) = None
    failure_reason: str | None = None
    prompt_version: str | None = None
    source_id: str | None = None
    source_turn_ids: list[str] | None = None
    input_refs: dict[str, Any] | None = None
    retryable: bool | None = None
    provider: str | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None
    provider_response_id: str | None = None
    response_output_shape: dict[str, Any] | None = None
    reason_codes: list[str] | None = None
    model_call_count: int | None = None
    agent_loop_max_model_calls: int | None = None
    omitted_turn_count: int | None = None
    eligible_capability_count: int | None = None


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
    silent: bool = False


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


class SurfaceErrorContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any]
    retryable: bool


class SurfaceCaptureIngestFailureContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any]
    retryable: bool


class SurfaceCaptureContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["text", "url", "shared_content", "unknown"]
    terminal_state: Literal["turn_created", "ingest_failed"]
    effective_session_id: str | None
    turn_id: str | None
    idempotency_key: str | None
    ingest_failure: SurfaceCaptureIngestFailureContract | None
    created_at: str
    updated_at: str


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
    action_attempt_id: str | None = None
    execution_task_id: str | None = None


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


class SurfaceSyncCursorContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google"]
    resource_type: Literal["calendar", "gmail", "drive"]
    resource_id: str
    cursor_value: str | None
    cursor_version: int
    status: Literal["ready", "syncing", "invalid", "error", "revoked"]
    last_successful_sync_at: str | None
    last_error_code: str | None
    last_error_at: str | None
    created_at: str
    updated_at: str


class SurfaceProviderEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google"]
    resource_type: Literal["calendar", "gmail", "drive"]
    resource_id: str
    external_event_id: str
    event_type: str
    headers: dict[str, Any]
    payload: dict[str, Any]
    body_digest: str | None
    status: Literal["accepted", "processed", "failed"]
    error: str | None
    received_at: str
    processed_at: str | None


class SurfaceSyncRunContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google"]
    resource_type: Literal["calendar", "gmail", "drive"]
    resource_id: str
    provider_event_id: str | None
    cursor_before: str | None
    cursor_after: str | None
    status: Literal["running", "succeeded", "failed"]
    item_count: int
    observation_count: int
    error: str | None
    started_at: str | None
    completed_at: str | None
    created_at: str


class SurfaceEmailActionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google"]
    provider_account_id: str
    action_attempt_id: str
    capability_id: Literal[
        "cap.email.archive",
        "cap.email.trash",
        "cap.email.labels.modify",
        "cap.email.undo",
    ]
    input_hash: str
    idempotency_key: str
    status: Literal["executing", "succeeded", "failed", "ambiguous", "undone"]
    approval_id: str | None
    provider_message_ids: list[str]
    provider_thread_ids: list[str]
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    provider_result: dict[str, Any]
    undo_available: bool
    undo_expires_at: str | None
    failure_code: str | None
    created_at: str
    updated_at: str


class SurfaceDiscordMessageContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    message_id: str
    title: str
    summary: str
    source_uri: str | None
    status: Literal["active", "deleted"]
    metadata: dict[str, Any]
    observed_at: str
    deleted_at: str | None
    created_at: str
    updated_at: str


class SurfaceDiscordMessageEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    discord_message_id: str
    dedupe_key: str
    provider_event_id: str | None
    event_type: Literal["created"]
    payload: dict[str, Any]
    created_at: str


class SurfaceSyncCursorListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    cursors: list[SurfaceSyncCursorContract]


class SurfaceProviderEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    events: list[SurfaceProviderEventContract]


class SurfaceSyncRunListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    sync_runs: list[SurfaceSyncRunContract]


class SurfaceEmailActionListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    email_actions: list[SurfaceEmailActionContract]


class SurfaceEmailActionResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    email_action: SurfaceEmailActionContract


class SurfaceDiscordMessageListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    discord_messages: list[SurfaceDiscordMessageContract]


class SurfaceDiscordMessageEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    discord_message_id: str
    events: list[SurfaceDiscordMessageEventContract]


class SurfaceCaptureSuccessResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]
    capture: SurfaceCaptureContract
    session: SurfaceSessionContract
    turn: SurfaceTurnContract
    assistant: SurfaceAssistantContract


class SurfaceCaptureFailureResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[False]
    capture: SurfaceCaptureContract
    error: SurfaceErrorContract


class MemoryRecallItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    layer: Literal["log", "note"]
    created_at: str
    content: str
    taint: Literal["clean", "tainted"]


class MemoryRecallV1Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    items: list[MemoryRecallItemContract]
    status: Literal["complete", "partial"]


def validate_memory_recall_v1(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a ``recall_v1`` finding dict, or raise
    ``ResponseContractViolation``."""
    return _validate_contract("memory_recall_v1", MemoryRecallV1Contract, payload)


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


def _project_surface_event_payload(
    event_type: SurfaceEventType, raw_payload: Any
) -> dict[str, Any]:
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
    if event_type == "evt.memory.recall_failed":
        return _validate_contract(
            "surface_event_payload.evt.memory.recall_failed",
            SurfaceEventMemoryRecallFailedPayloadContract,
            payload,
        )
    if event_type == "evt.memory.remember_queued":
        return _validate_contract(
            "surface_event_payload.evt.memory.remember_queued",
            SurfaceEventMemoryRememberQueuedPayloadContract,
            payload,
        )
    if event_type.startswith("evt.ai_judgment."):
        return _validate_contract(
            f"surface_event_payload.{event_type}",
            SurfaceEventAIJudgmentPayloadContract,
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
        return _validate_contract(
            "surface_event_payload.evt.model.started",
            SurfaceEventModelStartedPayloadContract,
            payload,
        )
    if event_type == "evt.model.completed":
        return _validate_contract(
            "surface_event_payload.evt.model.completed",
            SurfaceEventModelCompletedPayloadContract,
            payload,
        )
    if event_type == "evt.model.failed":
        return _validate_contract(
            "surface_event_payload.evt.model.failed",
            SurfaceEventModelFailedPayloadContract,
            payload,
        )
    if event_type == "evt.model.protocol_failed":
        return _validate_contract(
            "surface_event_payload.evt.model.protocol_failed",
            SurfaceEventModelProtocolFailedPayloadContract,
            payload,
        )
    if event_type == "evt.run.validation_failed":
        return _validate_contract(
            "surface_event_payload.evt.run.validation_failed",
            SurfaceEventRunValidationFailedPayloadContract,
            payload,
        )
    if event_type == "evt.agent.value_emitted":
        return _validate_contract(
            "surface_event_payload.evt.agent.value_emitted",
            SurfaceEventAgentValueEmittedPayloadContract,
            payload,
        )
    if event_type == "evt.agent.output_not_applied":
        return _validate_contract(
            "surface_event_payload.evt.agent.output_not_applied",
            SurfaceEventAgentOutputNotAppliedPayloadContract,
            payload,
        )
    if event_type == "evt.action.proposed":
        return _validate_contract(
            "surface_event_payload.evt.action.proposed",
            SurfaceEventActionProposedPayloadContract,
            payload,
        )
    if event_type == "evt.action.call_denied":
        return _validate_contract(
            "surface_event_payload.evt.action.call_denied",
            SurfaceEventActionCallDeniedPayloadContract,
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
    if event_type == "evt.action.execution.retrying":
        return _validate_contract(
            "surface_event_payload.evt.action.execution.retrying",
            SurfaceEventActionExecutionRetryingPayloadContract,
            payload,
        )
    if event_type == "evt.provider_write.reconcile_unavailable":
        return _validate_contract(
            "surface_event_payload.evt.provider_write.reconcile_unavailable",
            SurfaceEventProviderWriteReconcileUnavailablePayloadContract,
            payload,
        )
    if event_type == "evt.provider_write.receipt_reconciled":
        return _validate_contract(
            "surface_event_payload.evt.provider_write.receipt_reconciled",
            SurfaceEventProviderWriteReceiptReconciledPayloadContract,
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
    return _validate_contract(
        "surface_lifecycle_item", SurfaceLifecycleItemContract, lifecycle_payload
    )


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


def _project_surface_capture(raw_capture: Any) -> dict[str, Any]:
    capture_payload = raw_capture if isinstance(raw_capture, dict) else {}
    return _validate_contract("surface_capture", SurfaceCaptureContract, capture_payload)


def _project_surface_error(raw_error: Any) -> dict[str, Any]:
    error_payload = raw_error if isinstance(raw_error, dict) else {}
    return _validate_contract("surface_error", SurfaceErrorContract, error_payload)


def build_surface_message_response(
    *,
    session: Any,
    turn: Any,
    assistant_message: Any,
    assistant_sources: Any,
    assistant_silent: bool,
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
            "assistant": {
                "message": assistant_message,
                "sources": sources_payload,
                "silent": assistant_silent,
            },
        },
    )


def build_surface_capture_success_response(
    *,
    capture: Any,
    session: Any,
    turn: Any,
    assistant_message: Any,
    assistant_sources: Any,
) -> dict[str, Any]:
    sources_payload = assistant_sources if isinstance(assistant_sources, list) else []
    return _validate_contract(
        "surface_capture_success_response",
        SurfaceCaptureSuccessResponseContract,
        {
            "ok": True,
            "capture": _project_surface_capture(capture),
            "session": _project_surface_session(session),
            "turn": _project_surface_turn(turn),
            "assistant": {"message": assistant_message, "sources": sources_payload},
        },
    )


def build_surface_capture_failure_response(
    *,
    capture: Any,
    error: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_capture_failure_response",
        SurfaceCaptureFailureResponseContract,
        {
            "ok": False,
            "capture": _project_surface_capture(capture),
            "error": _project_surface_error(error),
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
    action_attempt_id: Any = None,
    execution_task_id: Any = None,
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
            "action_attempt_id": action_attempt_id,
            "execution_task_id": execution_task_id,
        },
    )


def build_surface_sync_cursor_list_response(*, cursors: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_sync_cursor_list_response",
        SurfaceSyncCursorListResponseContract,
        {
            "ok": True,
            "cursors": cursors if isinstance(cursors, list) else [],
        },
    )


def build_surface_provider_event_list_response(*, events: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_provider_event_list_response",
        SurfaceProviderEventListResponseContract,
        {
            "ok": True,
            "events": events if isinstance(events, list) else [],
        },
    )


def build_surface_sync_run_list_response(*, sync_runs: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_sync_run_list_response",
        SurfaceSyncRunListResponseContract,
        {
            "ok": True,
            "sync_runs": sync_runs if isinstance(sync_runs, list) else [],
        },
    )


def build_surface_email_action_list_response(*, email_actions: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_email_action_list_response",
        SurfaceEmailActionListResponseContract,
        {"ok": True, "email_actions": email_actions},
    )


def build_surface_email_action_response(*, email_action: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_email_action_response",
        SurfaceEmailActionResponseContract,
        {"ok": True, "email_action": email_action},
    )


def build_surface_discord_message_list_response(*, discord_messages: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_discord_message_list_response",
        SurfaceDiscordMessageListResponseContract,
        {
            "ok": True,
            "discord_messages": discord_messages if isinstance(discord_messages, list) else [],
        },
    )


def build_surface_discord_message_event_list_response(
    *,
    discord_message_id: Any,
    events: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_discord_message_event_list_response",
        SurfaceDiscordMessageEventListResponseContract,
        {
            "ok": True,
            "discord_message_id": discord_message_id,
            "events": events if isinstance(events, list) else [],
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
