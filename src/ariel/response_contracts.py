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


class SurfaceEventMemoryRecalledPayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    projection_version: str
    max_recalled_items: int
    included_memory_count: int
    omitted_memory_count: int
    included_memory_ids: list[str]
    omitted_memories: list[SurfaceMemoryRecallExclusionContract]
    conflict_ids: list[str]


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


class SurfaceEventMemoryPayloadContract(BaseModel):
    model_config = ConfigDict(extra="allow")


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
    rank_reason: str | None = None
    rank_score: float | None = None


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
    projection_health: SurfaceMemoryProjectionHealthContract


class SurfaceMemorySearchResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    schema_version: str
    results: list[SurfaceMemoryAssertionContract]


class SurfaceConnectorSubscriptionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google"]
    resource_type: Literal["calendar", "gmail", "drive"]
    resource_id: str
    channel_id: str
    provider_subscription_id: str | None
    status: Literal["active", "renewal_due", "expired", "error", "revoked"]
    expires_at: str | None
    renew_after: str | None
    last_error_code: str | None
    last_error_at: str | None
    created_at: str
    updated_at: str


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
    signal_count: int
    error: str | None
    started_at: str | None
    completed_at: str | None
    created_at: str


class SurfaceWorkspaceItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["google", "ariel"]
    item_type: Literal["calendar_event", "email_message", "drive_file", "internal_state"]
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


class SurfaceAttentionSignalContract(BaseModel):
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
    status: Literal["new", "reviewed", "dismissed", "superseded"]
    priority: Literal["critical", "high", "normal", "low"]
    urgency: Literal["critical", "high", "normal", "low"]
    confidence: float
    title: str
    body: str
    reason: str
    evidence: dict[str, Any]
    taint: dict[str, Any]
    created_at: str
    updated_at: str


class SurfaceAttentionGroupContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group_key: str
    group_type: Literal["approval", "job", "connector", "memory", "capture", "workspace"]
    status: Literal["active", "suppressed", "resolved"]
    title: str
    summary: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class SurfaceAttentionGroupMemberContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group_id: str
    attention_signal_id: str
    grouping_reason: str
    ranking_version: str
    created_at: str


class SurfaceAttentionRankFeatureContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    attention_signal_id: str
    feature_set_version: str
    features: dict[str, Any]
    score_components: dict[str, Any]
    created_at: str


class SurfaceAttentionRankSnapshotContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group_id: str
    snapshot_key: str
    ranker_version: str
    source_signal_ids: list[str]
    rank_score: float
    rank_inputs: dict[str, Any]
    rank_reason: str
    delivery_decision: Literal["interrupt_now", "queue", "digest", "suppress"]
    delivery_reason: str
    suppression_reason: str | None
    next_follow_up_after: str | None
    priority: Literal["critical", "high", "normal", "low"]
    urgency: Literal["critical", "high", "normal", "low"]
    confidence: float
    title: str
    body: str
    evidence: dict[str, Any]
    taint: dict[str, Any]
    created_at: str


class SurfaceAttentionItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group_id: str
    rank_snapshot_id: str
    source_type: Literal["attention_group"]
    source_id: str
    source_signal_ids: list[str]
    dedupe_key: str
    status: Literal[
        "open",
        "notified",
        "acknowledged",
        "snoozed",
        "resolved",
        "expired",
        "cancelled",
        "superseded",
    ]
    priority: Literal["critical", "high", "normal", "low"]
    urgency: Literal["critical", "high", "normal", "low"]
    confidence: float
    title: str
    body: str
    reason: str
    evidence: dict[str, Any]
    taint: dict[str, Any]
    rank_score: float
    rank_inputs: dict[str, Any]
    rank_reason: str
    delivery_decision: Literal["interrupt_now", "queue", "digest", "suppress"]
    delivery_reason: str
    suppression_reason: str | None
    expires_at: str | None
    next_follow_up_after: str | None
    last_notified_at: str | None
    created_at: str
    updated_at: str


class SurfaceAttentionItemEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    attention_item_id: str
    event_type: Literal[
        "detected",
        "updated",
        "notified",
        "acknowledged",
        "snoozed",
        "resolved",
        "cancelled",
        "expired",
        "follow_up_queued",
        "refreshed",
    ]
    payload: dict[str, Any]
    created_at: str


class SurfaceConnectorSubscriptionListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    subscriptions: list[SurfaceConnectorSubscriptionContract]


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


class SurfaceWorkspaceItemListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    workspace_items: list[SurfaceWorkspaceItemContract]


class SurfaceWorkspaceItemEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    workspace_item_id: str
    events: list[SurfaceWorkspaceItemEventContract]


class SurfaceAttentionSignalListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_signals: list[SurfaceAttentionSignalContract]


class SurfaceAttentionGroupListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_groups: list[SurfaceAttentionGroupContract]


class SurfaceAttentionRankFeatureListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_rank_features: list[SurfaceAttentionRankFeatureContract]


class SurfaceAttentionRankSnapshotListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_rank_snapshots: list[SurfaceAttentionRankSnapshotContract]


class SurfaceProactiveFeedbackRuleContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    rule_key: str
    rule_type: Literal["ranking", "grouping", "delivery", "suppression"]
    status: Literal["active", "paused", "archived"]
    priority: int
    conditions: dict[str, Any]
    effect: dict[str, Any]
    created_at: str
    updated_at: str


class SurfaceProactiveFeedbackRuleListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    proactive_feedback_rules: list[SurfaceProactiveFeedbackRuleContract]


class SurfaceActionProposalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    attention_item_id: str
    capability_id: str
    payload: dict[str, Any]
    payload_hash: str
    status: Literal["proposed", "approved", "rejected", "superseded"]
    policy_state: dict[str, Any]
    evidence: dict[str, Any]
    created_at: str
    updated_at: str


class SurfaceActionProposalListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    action_proposals: list[SurfaceActionProposalContract]


class SurfaceProactiveFeedbackContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    attention_item_id: str
    feedback_type: Literal["important", "noise", "wrong", "useful"]
    note: str | None
    created_at: str


class SurfaceAttentionFeedbackResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_item: SurfaceAttentionItemContract
    feedback: SurfaceProactiveFeedbackContract


class SurfaceAttentionItemResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_item: SurfaceAttentionItemContract


class SurfaceAttentionItemListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_items: list[SurfaceAttentionItemContract]


class SurfaceAttentionItemEventListResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    attention_item_id: str
    events: list[SurfaceAttentionItemEventContract]


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


def _default_surface_context_metadata() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "section_order": [
            "policy_system_instructions",
            "recent_active_session_turns",
            "memory_context",
            "open_commitments_and_jobs",
            "relevant_artifacts_and_signals",
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
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = raw_usage.get(key)
        usage[key] = int(value) if isinstance(value, int) else None
    return usage


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
    if event_type.startswith("evt.memory."):
        return _validate_contract(
            f"surface_event_payload.{event_type}",
            SurfaceEventMemoryPayloadContract,
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
) -> dict[str, Any]:
    return _validate_contract(
        "surface_memory_response",
        SurfaceMemoryResponseContract,
        {
            "ok": True,
            "schema_version": schema_version,
            "active_assertions": active_assertions if isinstance(active_assertions, list) else [],
            "candidates": candidates if isinstance(candidates, list) else [],
            "conflicts": conflicts if isinstance(conflicts, list) else [],
            "project_state": project_state if isinstance(project_state, list) else [],
            "evidence": evidence if isinstance(evidence, list) else [],
            "procedures": procedures if isinstance(procedures, list) else [],
            "projection_health": projection_health if isinstance(projection_health, dict) else {},
        },
    )


def build_surface_memory_search_response(*, schema_version: Any, results: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_memory_search_response",
        SurfaceMemorySearchResponseContract,
        {
            "ok": True,
            "schema_version": schema_version,
            "results": results if isinstance(results, list) else [],
        },
    )


def build_surface_connector_subscription_list_response(*, subscriptions: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_connector_subscription_list_response",
        SurfaceConnectorSubscriptionListResponseContract,
        {
            "ok": True,
            "subscriptions": subscriptions if isinstance(subscriptions, list) else [],
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


def build_surface_attention_signal_list_response(*, attention_signals: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_signal_list_response",
        SurfaceAttentionSignalListResponseContract,
        {
            "ok": True,
            "attention_signals": attention_signals if isinstance(attention_signals, list) else [],
        },
    )


def build_surface_attention_group_list_response(*, attention_groups: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_group_list_response",
        SurfaceAttentionGroupListResponseContract,
        {
            "ok": True,
            "attention_groups": attention_groups if isinstance(attention_groups, list) else [],
        },
    )


def build_surface_attention_rank_feature_list_response(
    *,
    attention_rank_features: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_rank_feature_list_response",
        SurfaceAttentionRankFeatureListResponseContract,
        {
            "ok": True,
            "attention_rank_features": (
                attention_rank_features if isinstance(attention_rank_features, list) else []
            ),
        },
    )


def build_surface_attention_rank_snapshot_list_response(
    *,
    attention_rank_snapshots: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_rank_snapshot_list_response",
        SurfaceAttentionRankSnapshotListResponseContract,
        {
            "ok": True,
            "attention_rank_snapshots": (
                attention_rank_snapshots if isinstance(attention_rank_snapshots, list) else []
            ),
        },
    )


def build_surface_proactive_feedback_rule_list_response(
    *,
    proactive_feedback_rules: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_proactive_feedback_rule_list_response",
        SurfaceProactiveFeedbackRuleListResponseContract,
        {
            "ok": True,
            "proactive_feedback_rules": (
                proactive_feedback_rules if isinstance(proactive_feedback_rules, list) else []
            ),
        },
    )


def build_surface_action_proposal_list_response(*, action_proposals: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_action_proposal_list_response",
        SurfaceActionProposalListResponseContract,
        {
            "ok": True,
            "action_proposals": action_proposals if isinstance(action_proposals, list) else [],
        },
    )


def build_surface_attention_feedback_response(
    *,
    attention_item: Any,
    feedback: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_feedback_response",
        SurfaceAttentionFeedbackResponseContract,
        {
            "ok": True,
            "attention_item": attention_item if isinstance(attention_item, dict) else {},
            "feedback": feedback if isinstance(feedback, dict) else {},
        },
    )


def build_surface_attention_item_response(*, attention_item: Any) -> dict[str, Any]:
    attention_item_payload = attention_item if isinstance(attention_item, dict) else {}
    return _validate_contract(
        "surface_attention_item_response",
        SurfaceAttentionItemResponseContract,
        {"ok": True, "attention_item": attention_item_payload},
    )


def build_surface_attention_item_list_response(*, attention_items: Any) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_item_list_response",
        SurfaceAttentionItemListResponseContract,
        {
            "ok": True,
            "attention_items": attention_items if isinstance(attention_items, list) else [],
        },
    )


def build_surface_attention_item_event_list_response(
    *,
    attention_item_id: Any,
    events: Any,
) -> dict[str, Any]:
    return _validate_contract(
        "surface_attention_item_event_list_response",
        SurfaceAttentionItemEventListResponseContract,
        {
            "ok": True,
            "attention_item_id": attention_item_id,
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
