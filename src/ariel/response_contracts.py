from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
    memory_mode: Literal["normal", "temporary", "no_memory"]
    created_at: str
    updated_at: str


SurfaceEventType = Literal[
    "evt.turn.started",
    "evt.memory.curated",
    "evt.memory.evidence_recorded",
    "evt.memory.candidate_proposed",
    "evt.memory.review_required",
    "evt.memory.candidate_approved",
    "evt.memory.candidate_rejected",
    "evt.memory.assertion_activated",
    "evt.memory.assertion_superseded",
    "evt.memory.assertion_retracted",
    "evt.memory.assertion_deleted",
    "evt.memory.conflict_opened",
    "evt.memory.conflict_resolved",
    "evt.memory.projection_rebuilt",
    "evt.memory.recall_omitted_item",
    "evt.memory.extraction_queued",
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


class SurfaceMemoryRecallExclusionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    reason: str


class SurfaceMemoryRecallSelectionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    rationale: str


class SurfaceMemoryRailsExclusionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    table: str
    reason: str


class SurfaceEventMemoryCuratedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    projection_version: str
    max_selected_memories: int
    selected_memory_count: int
    memory_candidate_count: int
    omitted_memory_count: int
    rails_excluded_count: int
    selected_memory_ids: list[str]
    selected_memories: list[SurfaceMemoryRecallSelectionContract]
    omitted_memories: list[SurfaceMemoryRecallExclusionContract]
    candidate_memory_ids: list[str]
    candidate_memories: list[dict[str, Any]]
    rails_excluded: list[SurfaceMemoryRailsExclusionContract]
    curation_rationale: str
    curation_uncertainty: str
    curation_confidence: float
    curation_model: str | None
    curation_prompt_version: str
    curation_parse_status: str
    curation_provider_response_id: str | None = None
    conflict_ids: list[str]
    memory_policy: dict[str, Any] | None = None


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
    failure_code: str | None = None
    parse_status: str | None = None
    validation_status: Literal["valid", "invalid", "not_validated"] | None = None
    usage: SurfaceModelUsageContract | None = None
    provider_response_id: str | None = None
    response_output_shape: dict[str, Any] | None = None


class SurfaceEventModelProtocolFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    attempt: int
    provider_response_id: str | None = None


class SurfaceEventRunValidationFailedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    errors: list[str]
    attempt: int
    provider_response_id: str | None = None


class SurfaceEventAgentValueEmittedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    value_digest: str
    value_bytes: int
    attempt: int
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


class SurfaceEventMemoryAssertionPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    subject_key: str
    predicate: str
    assertion_type: str
    lifecycle_state: str
    value_preview: str
    confidence: float
    evidence_id: str | None = None
    deletion_type: str | None = None
    evidence_ids: list[str] | None = None


class SurfaceEventMemoryEvidencePayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_turn_id: str | None = None
    source_session_id: str | None = None
    content_class: str | None = None
    trust_boundary: str | None = None


class SurfaceEventMemoryProjectionRebuiltPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    projection_version: str
    projection_kinds: list[str]
    queued_projection_kinds: list[str]


class SurfaceEventMemoryConflictOpenedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_set_id: str
    subject_key: str
    predicate: str
    assertion_ids: list[str]


class SurfaceEventMemoryConflictResolvedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_set_id: str
    resolution_assertion_id: str


class SurfaceEventMemoryRecallOmittedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    reason: str


class SurfaceEventMemoryExtractionQueuedPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    turn_id: str
    evidence_id: str


class SurfaceEventAIJudgmentPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judgment_type: Literal[
        "memory_curation",
        "tool_result_interpretation",
        "memory_extraction",
        "continuity_compaction",
        "feedback_learning",
        "ambient_interpretation",
        "proactive_deliberation",
        "model_output",
        "workspace_commitment_extraction",
    ]
    parse_status: (
        Literal[
            "not_required_no_candidates",
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
    attempt: int | None = None
    max_model_attempts: int | None = None
    last_tool_result_interpreter_judgment_id: str | None = None
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


class SurfaceMemoryEvidenceRefContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    snippet: str
    source_turn_id: str | None
    source_session_id: str
    content_class: str
    trust_boundary: str
    created_at: str


class SurfaceMemoryAssertionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject_key: str
    predicate: str
    type: str
    state: str
    value: str
    confidence: float
    scope_key: str
    is_multi_valued: bool
    valid_from: str | None
    valid_to: str | None
    last_verified_at: str
    created_at: str
    updated_at: str
    superseded_by_id: str | None
    evidence_refs: list[SurfaceMemoryEvidenceRefContract]
    projection_version: str


class SurfaceMemoryConflictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject_entity_id: str
    predicate: str
    scope_key: str
    state: str
    resolution_assertion_id: str | None
    reason: str | None
    created_at: str
    updated_at: str


class SurfaceProjectStateContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    summary: str
    state: Any | None = None
    source_assertion_ids: list[str]
    source_evidence_ids: list[str]
    created_at: str
    updated_at: str


class SurfaceMemoryEvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source_turn_id: str | None
    source_session_id: str
    content_class: str
    trust_boundary: str
    state: str
    snippet: str
    created_at: str


class SurfaceMemoryProcedureContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    procedure_key: str
    scope_key: str
    title: str
    instruction: str
    state: str
    review_state: str
    source_assertion_id: str | None
    created_at: str
    updated_at: str


class SurfaceMemoryProjectionHealthContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_version: str
    pending_jobs: int
    failed_jobs: int
    running_jobs: int = 0


class SurfaceMemoryActionTraceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope_key: str
    trace_type: str
    action_attempt_id: str | None
    source_turn_id: str | None
    capability_id: str | None
    summary: str
    outcome: str
    primary_evidence_id: str
    result_refs: Any
    created_at: str
    updated_at: str


class SurfaceMemoryReasoningTraceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope_key: str
    trace_type: str
    task_summary: str
    trace_summary: str
    outcome: str
    primary_evidence_id: str
    source_turn_id: str | None
    related_entity_ids: list[str]
    related_assertion_ids: list[str]
    created_at: str
    updated_at: str


class SurfaceMemoryTopicContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    topic_key: str
    family: str
    scope_key: str
    title: str
    summary: str
    state: str
    projection_version: str
    created_at: str
    updated_at: str


class SurfaceMemoryContextBlockContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    block_type: str
    scope_key: str
    topic_id: str | None
    content: str
    state: str
    source_assertion_ids: list[str]
    source_episode_ids: list[str]
    source_trace_ids: list[str]
    source_action_trace_ids: list[str]
    source_procedure_ids: list[str]
    source_project_state_snapshot_ids: list[str]
    source_memory_versions: dict[str, Any]
    source_projection_versions: dict[str, Any]
    projection_version: str
    created_at: str
    updated_at: str


class SurfaceMemoryDeletionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    target_table: str
    target_id: str
    deletion_type: str
    actor_id: str
    reason: str | None
    redaction_posture: str
    projection_invalidation: dict[str, Any]
    created_at: str


class SurfaceMemoryScopeBindingContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope_type: str
    scope_key: str
    actor_id: str
    memory_mode: str
    extraction_enabled: bool
    recall_enabled: bool
    reason: str | None
    expires_at: str | None
    created_at: str
    updated_at: str


class SurfaceMemoryRetentionPolicyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope_key: str
    policy_kind: str
    pattern: str
    retention_days: int | None
    state: str
    reason: str | None
    created_at: str
    updated_at: str


class SurfaceMemorySensitivityLabelContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    canonical_table: str
    canonical_id: str
    label: str
    actor_id: str
    state: str
    reason: str | None
    created_at: str
    updated_at: str


class SurfaceMemoryExportArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope_key: str
    artifact_kind: str
    export_format: str
    status: str
    projection_version: str
    redaction_posture: str
    source_counts: Any
    source_memory_versions: dict[str, Any]
    source_projection_versions: dict[str, Any]
    created_at: str
    updated_at: str


class SurfaceMemoryEvalRunContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    eval_name: str
    status: str
    metrics: Any
    created_at: str
    updated_at: str


class SurfaceMemoryEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    event_type: str
    scope_key: str
    actor_id: str
    entry_path: str
    subject_refs: dict[str, Any]
    payload: dict[str, Any]
    source_turn_id: str | None
    created_at: str


class SurfaceMemoryResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    schema_version: str
    active_assertions: list[SurfaceMemoryAssertionContract]
    candidates: list[SurfaceMemoryAssertionContract]
    conflicts: list[SurfaceMemoryConflictContract]
    project_state: list[SurfaceProjectStateContract]
    evidence: list[SurfaceMemoryEvidenceContract]
    procedures: list[SurfaceMemoryProcedureContract]
    action_traces: list[SurfaceMemoryActionTraceContract] = Field(default_factory=list)
    reasoning_traces: list[SurfaceMemoryReasoningTraceContract] = Field(default_factory=list)
    topics: list[SurfaceMemoryTopicContract] = Field(default_factory=list)
    context_blocks: list[SurfaceMemoryContextBlockContract] = Field(default_factory=list)
    deletions: list[SurfaceMemoryDeletionContract] = Field(default_factory=list)
    scope_bindings: list[SurfaceMemoryScopeBindingContract] = Field(default_factory=list)
    retention_policies: list[SurfaceMemoryRetentionPolicyContract] = Field(default_factory=list)
    sensitivity_labels: list[SurfaceMemorySensitivityLabelContract] = Field(default_factory=list)
    export_artifacts: list[SurfaceMemoryExportArtifactContract] = Field(default_factory=list)
    eval_runs: list[SurfaceMemoryEvalRunContract] = Field(default_factory=list)
    projection_health: SurfaceMemoryProjectionHealthContract


class SurfaceMemorySearchResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    rationale: str | None = None
    value: Any = None
    evidence_refs: Any = None
    retrieval_features: Any = None
    conflict_status: Any = None
    projection_version: str | None = None


class SurfaceMemorySearchResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    schema_version: str
    results: list[SurfaceMemorySearchResultContract]


class SurfaceMemoryEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    schema_version: str
    events: list[SurfaceMemoryEventContract]


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
    status: Literal["pending", "executing", "succeeded", "failed", "undone"]
    approval_id: str | None
    provider_message_ids: list[str]
    provider_thread_ids: list[str]
    before_state: dict[str, Any]
    intended_state: dict[str, Any]
    after_state: dict[str, Any]
    provider_result: dict[str, Any]
    undo_available: bool
    undo_expires_at: str | None
    execution_attempts: int
    failure_code: str | None
    created_at: str
    updated_at: str


class SurfaceEmailThreadWatchContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google"]
    provider_account_id: str
    provider_thread_id: str
    anchor_message_id: str
    condition: Literal["no_reply_by_deadline", "any_reply_arrives"]
    deadline: str
    note: str
    idempotency_key: str
    cancel_idempotency_key: str | None
    status: Literal["active", "due", "completed", "canceled", "failed"]
    created_by_action_attempt_id: str
    matched_message_id: str | None
    matched_at: str | None
    canceled_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str


class SurfaceWorkspaceItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google", "ariel", "discord"]
    item_type: Literal[
        "calendar_event",
        "email_message",
        "drive_file",
        "internal_state",
        "discord_message",
    ]
    external_id: str
    title: str
    summary: str
    source_uri: str | None
    status: Literal["active", "deleted"]
    metadata: dict[str, Any]
    observed_at: str
    deleted_at: str | None
    created_at: str
    updated_at: str


class SurfaceWorkspaceItemEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_item_id: str
    dedupe_key: str
    provider_event_id: str | None
    event_type: Literal["created", "updated", "deleted", "restored"]
    payload: dict[str, Any]
    created_at: str


class SurfaceProactiveObservationContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_item_id: str | None
    source_type: Literal[
        "workspace_item",
        "job",
        "approval_request",
        "memory_assertion",
        "google_connector",
        "capture",
    ]
    source_id: str
    dedupe_key: str
    observation_type: str
    subject: str
    summary: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    taint: dict[str, Any]
    trust_boundary: Literal["trusted_internal", "reviewed_memory", "user", "provider", "tainted"]
    status: Literal["new", "linked", "ignored"]
    observed_at: str
    created_at: str
    updated_at: str


class SurfaceProactiveCaseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case_key: str
    status: Literal[
        "open",
        "waiting",
        "spoken",
        "acted",
        "asked",
        "ignored",
        "acknowledged",
        "resolved",
        "failed",
    ]
    title: str
    summary: str
    latest_observation_id: str
    last_decision_id: str | None
    next_recheck_after: str | None
    created_at: str
    updated_at: str


class SurfaceProactiveCaseEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case_id: str
    event_type: Literal[
        "opened",
        "updated",
        "context_built",
        "decided",
        "validated",
        "turn_created",
        "action_planned",
        "action_executed",
        "waiting",
        "acknowledged",
        "resolved",
        "feedback_recorded",
        "failed",
    ]
    payload: dict[str, Any]
    created_at: str


class SurfaceProactiveDecisionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case_id: str
    context: dict[str, Any]
    model_input: list[dict[str, Any]]
    omitted_context: dict[str, Any]
    context_taint: dict[str, Any]
    ai_judgment_id: str
    decision_type: Literal[
        "ignore",
        "remember",
        "wait",
        "observe_more",
        "speak_now",
        "ask_user",
        "act_now",
        "speak_and_act",
    ]
    status: Literal["proposed", "invalid", "validated", "executed", "ignored"]
    confidence: float
    urgency: Literal["critical", "high", "normal", "low"]
    user_visible_message: str | None
    rationale: str
    evidence_refs: list[str]
    tool_refs: list[str]
    actions: list[dict[str, Any]]
    follow_up: dict[str, Any] | None
    memory_payload: dict[str, Any] | None
    policy_result: (
        Literal[
            "authorized",
            "authorized_with_constraints",
            "denied",
            "needs_user_authority",
            "stale_context",
            "invalid_decision",
            "duplicate",
            "dead_letter",
        ]
        | None
    )
    policy_version: str | None
    action_plan_hash: str | None
    policy_constraints: dict[str, Any] | None
    denial_reason: str | None
    created_at: str


class SurfaceProactiveActionPlanContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case_id: str
    decision_id: str
    plan_key: str
    action_type: str
    target: str
    payload: dict[str, Any]
    payload_hash: str
    risk_tier: Literal["low", "medium", "high", "blocked"]
    status: Literal[
        "proposed", "authorized", "denied", "executing", "succeeded", "failed", "cancelled"
    ]
    created_at: str
    updated_at: str


class SurfaceProactiveActionExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action_plan_id: str
    idempotency_key: str
    status: Literal["pending", "running", "succeeded", "failed"]
    external_receipt: dict[str, Any] | None
    error: str | None
    started_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str


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


class SurfaceEmailThreadWatchListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    email_thread_watches: list[SurfaceEmailThreadWatchContract]


class SurfaceEmailThreadWatchResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    email_thread_watch: SurfaceEmailThreadWatchContract


class SurfaceWorkspaceItemListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    workspace_items: list[SurfaceWorkspaceItemContract]


class SurfaceWorkspaceItemEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    workspace_item_id: str
    events: list[SurfaceWorkspaceItemEventContract]


class SurfaceProactiveObservationListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    observations: list[SurfaceProactiveObservationContract]


class SurfaceProactiveCaseListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    cases: list[SurfaceProactiveCaseContract]


class SurfaceProactiveCaseResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    case: SurfaceProactiveCaseContract


class SurfaceProactiveCaseEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    case_id: str
    events: list[SurfaceProactiveCaseEventContract]


class SurfaceProactiveDecisionListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    case_id: str
    decisions: list[SurfaceProactiveDecisionContract]


class SurfaceProactiveActionListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    case_id: str | None
    action_plans: list[SurfaceProactiveActionPlanContract]
    action_executions: list[SurfaceProactiveActionExecutionContract]


class SurfaceAutonomyScopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope_key: str
    actor: str
    source_context: dict[str, Any]
    action_type: str
    target_system: str
    allowed_target_systems: list[str]
    allowed_payload: dict[str, Any]
    allowed_payload_shape: dict[str, Any]
    max_impact: Literal["low", "medium", "high"]
    revocation_rule: str
    notification_rule: Literal["silent_audit", "notify_after", "notify_before"]
    audit_visibility: Literal["private", "operator_visible"]
    version: int
    status: Literal["active", "paused", "revoked"]
    revoked_at: str | None
    created_at: str
    updated_at: str


class SurfaceAutonomyScopeListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    autonomy_scopes: list[SurfaceAutonomyScopeContract]


class SurfaceAutonomyScopeResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    autonomy_scope: SurfaceAutonomyScopeContract


class SurfaceProactiveFeedbackContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    case_id: str
    feedback_type: Literal[
        "ack",
        "correct",
        "stop_pattern",
        "more_aggressive",
        "useful",
        "wrong",
        "automatic_next_time",
    ]
    note: str | None
    payload: dict[str, Any]
    created_at: str


class SurfaceProactiveFeedbackResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    case: SurfaceProactiveCaseContract
    feedback: SurfaceProactiveFeedbackContract


class SurfaceProactiveLearningRecordContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    feedback_id: str | None
    record_type: Literal[
        "instruction",
        "example",
        "calibration",
        "preference",
        "source_preference",
        "prompt_instruction",
        "autonomy_request",
    ]
    status: Literal["active", "superseded", "rejected"]
    content: dict[str, Any]
    model: str | None
    prompt_version: str
    provider_response_id: str | None
    parse_status: Literal["parsed", "invalid_json", "missing_output", "schema_invalid"]
    validation_status: Literal["valid", "invalid"]
    created_at: str
    updated_at: str


class SurfaceProactiveLearningRecordListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    learning_records: list[SurfaceProactiveLearningRecordContract]


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
    if event_type == "evt.memory.curated":
        return _validate_contract(
            "surface_event_payload.evt.memory.curated",
            SurfaceEventMemoryCuratedPayloadContract,
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
    if event_type == "evt.memory.evidence_recorded":
        return _validate_contract(
            "surface_event_payload.evt.memory.evidence_recorded",
            SurfaceEventMemoryEvidencePayloadContract,
            payload,
        )
    if event_type in {
        "evt.memory.candidate_proposed",
        "evt.memory.review_required",
        "evt.memory.candidate_approved",
        "evt.memory.candidate_rejected",
        "evt.memory.assertion_activated",
        "evt.memory.assertion_superseded",
        "evt.memory.assertion_deleted",
    }:
        return _validate_contract(
            f"surface_event_payload.{event_type}",
            SurfaceEventMemoryAssertionPayloadContract,
            payload,
        )
    if event_type == "evt.memory.assertion_retracted":
        if "assertion_id" in payload:
            return _validate_contract(
                "surface_event_payload.evt.memory.assertion_retracted",
                SurfaceEventMemoryAssertionPayloadContract,
                payload,
            )
        return _validate_contract(
            "surface_event_payload.evt.memory.assertion_retracted",
            SurfaceEventMemoryEvidencePayloadContract,
            payload,
        )
    if event_type == "evt.memory.conflict_opened":
        return _validate_contract(
            "surface_event_payload.evt.memory.conflict_opened",
            SurfaceEventMemoryConflictOpenedPayloadContract,
            payload,
        )
    if event_type == "evt.memory.conflict_resolved":
        return _validate_contract(
            "surface_event_payload.evt.memory.conflict_resolved",
            SurfaceEventMemoryConflictResolvedPayloadContract,
            payload,
        )
    if event_type == "evt.memory.projection_rebuilt":
        return _validate_contract(
            "surface_event_payload.evt.memory.projection_rebuilt",
            SurfaceEventMemoryProjectionRebuiltPayloadContract,
            payload,
        )
    if event_type == "evt.memory.recall_omitted_item":
        return _validate_contract(
            "surface_event_payload.evt.memory.recall_omitted_item",
            SurfaceEventMemoryRecallOmittedPayloadContract,
            payload,
        )
    if event_type == "evt.memory.extraction_queued":
        return _validate_contract(
            "surface_event_payload.evt.memory.extraction_queued",
            SurfaceEventMemoryExtractionQueuedPayloadContract,
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


def build_surface_memory_response(
    *,
    schema_version: Any,
    active_assertions: Any,
    candidates: Any,
    conflicts: Any,
    project_state: Any,
    evidence: Any,
    procedures: Any,
    projection_health: Any,
    action_traces: Any = None,
    reasoning_traces: Any = None,
    topics: Any = None,
    context_blocks: Any = None,
    deletions: Any = None,
    scope_bindings: Any = None,
    retention_policies: Any = None,
    sensitivity_labels: Any = None,
    export_artifacts: Any = None,
    eval_runs: Any = None,
) -> dict[str, Any]:
    payload = {
        "ok": True,
        "schema_version": schema_version,
        "active_assertions": active_assertions,
        "candidates": candidates,
        "conflicts": conflicts,
        "project_state": project_state,
        "evidence": evidence,
        "procedures": procedures,
        "projection_health": projection_health,
    }
    for key, value in (
        ("action_traces", action_traces),
        ("reasoning_traces", reasoning_traces),
        ("topics", topics),
        ("context_blocks", context_blocks),
        ("deletions", deletions),
        ("scope_bindings", scope_bindings),
        ("retention_policies", retention_policies),
        ("sensitivity_labels", sensitivity_labels),
        ("export_artifacts", export_artifacts),
        ("eval_runs", eval_runs),
    ):
        if value is not None:
            payload[key] = value
    return _validate_contract(
        "surface_memory_response",
        SurfaceMemoryResponseContract,
        payload,
    )


def build_surface_memory_search_response(*, schema_version: Any, results: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_memory_search_response",
        SurfaceMemorySearchResponseContract,
        {
            "ok": True,
            "schema_version": schema_version,
            "results": results,
        },
    )


def build_surface_memory_event_list_response(*, schema_version: Any, events: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_memory_event_list_response",
        SurfaceMemoryEventListResponseContract,
        {
            "ok": True,
            "schema_version": schema_version,
            "events": events if isinstance(events, list) else [],
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


def build_surface_email_thread_watch_list_response(
    *,
    email_thread_watches: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_email_thread_watch_list_response",
        SurfaceEmailThreadWatchListResponseContract,
        {"ok": True, "email_thread_watches": email_thread_watches},
    )


def build_surface_email_thread_watch_response(
    *,
    email_thread_watch: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_email_thread_watch_response",
        SurfaceEmailThreadWatchResponseContract,
        {"ok": True, "email_thread_watch": email_thread_watch},
    )


def build_surface_workspace_item_list_response(*, workspace_items: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_workspace_item_list_response",
        SurfaceWorkspaceItemListResponseContract,
        {
            "ok": True,
            "workspace_items": workspace_items if isinstance(workspace_items, list) else [],
        },
    )


def build_surface_workspace_item_event_list_response(
    *,
    workspace_item_id: Any,
    events: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_workspace_item_event_list_response",
        SurfaceWorkspaceItemEventListResponseContract,
        {
            "ok": True,
            "workspace_item_id": workspace_item_id,
            "events": events if isinstance(events, list) else [],
        },
    )


def build_surface_proactive_observation_list_response(*, observations: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_observation_list_response",
        SurfaceProactiveObservationListResponseContract,
        {
            "ok": True,
            "observations": observations if isinstance(observations, list) else [],
        },
    )


def build_surface_proactive_case_list_response(*, cases: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_case_list_response",
        SurfaceProactiveCaseListResponseContract,
        {
            "ok": True,
            "cases": cases if isinstance(cases, list) else [],
        },
    )


def build_surface_proactive_case_response(*, case: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_case_response",
        SurfaceProactiveCaseResponseContract,
        {"ok": True, "case": case if isinstance(case, dict) else {}},
    )


def build_surface_proactive_case_event_list_response(
    *,
    case_id: Any,
    events: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_case_event_list_response",
        SurfaceProactiveCaseEventListResponseContract,
        {"ok": True, "case_id": case_id, "events": events if isinstance(events, list) else []},
    )


def build_surface_proactive_decision_list_response(
    *,
    case_id: Any,
    decisions: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_decision_list_response",
        SurfaceProactiveDecisionListResponseContract,
        {
            "ok": True,
            "case_id": case_id,
            "decisions": decisions if isinstance(decisions, list) else [],
        },
    )


def build_surface_proactive_action_list_response(
    *,
    case_id: Any,
    action_plans: Any,
    action_executions: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_action_list_response",
        SurfaceProactiveActionListResponseContract,
        {
            "ok": True,
            "case_id": case_id,
            "action_plans": action_plans if isinstance(action_plans, list) else [],
            "action_executions": (action_executions if isinstance(action_executions, list) else []),
        },
    )


def build_surface_autonomy_scope_list_response(*, autonomy_scopes: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_autonomy_scope_list_response",
        SurfaceAutonomyScopeListResponseContract,
        {
            "ok": True,
            "autonomy_scopes": autonomy_scopes if isinstance(autonomy_scopes, list) else [],
        },
    )


def build_surface_autonomy_scope_response(*, autonomy_scope: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_autonomy_scope_response",
        SurfaceAutonomyScopeResponseContract,
        {
            "ok": True,
            "autonomy_scope": autonomy_scope if isinstance(autonomy_scope, dict) else {},
        },
    )


def build_surface_proactive_feedback_response(
    *,
    case: Any,
    feedback: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_feedback_response",
        SurfaceProactiveFeedbackResponseContract,
        {
            "ok": True,
            "case": case if isinstance(case, dict) else {},
            "feedback": feedback if isinstance(feedback, dict) else {},
        },
    )


def build_surface_proactive_learning_record_list_response(
    *,
    learning_records: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_learning_record_list_response",
        SurfaceProactiveLearningRecordListResponseContract,
        {
            "ok": True,
            "learning_records": learning_records if isinstance(learning_records, list) else [],
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
