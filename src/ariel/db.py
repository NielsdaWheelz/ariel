from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "alembic_version",
    "sessions",
    "session_rotations",
    "turns",
    "turn_idempotency_keys",
    "captures",
    "events",
    "ai_judgments",
    "action_attempts",
    "action_private_payloads",
    "approval_requests",
    "artifacts",
    "attachment_blobs",
    "attachment_sources",
    "attachment_extractions",
    "memory_evidence",
    "memory_entities",
    "memory_relationships",
    "memory_assertions",
    "memory_assertion_evidence",
    "memory_episodes",
    "memory_reasoning_traces",
    "memory_action_traces",
    "memory_procedures",
    "memory_reviews",
    "memory_conflict_sets",
    "memory_conflict_members",
    "memory_salience",
    "memory_scope_bindings",
    "memory_retention_policies",
    "memory_sensitivity_labels",
    "memory_versions",
    "memory_deletions",
    "memory_projection_jobs",
    "memory_embedding_projections",
    "memory_temporal_projections",
    "memory_symbol_projections",
    "memory_keyword_projections",
    "memory_entity_projections",
    "memory_graph_projections",
    "memory_topics",
    "memory_topic_members",
    "memory_context_blocks",
    "memory_export_artifacts",
    "memory_eval_runs",
    "project_state_snapshots",
    "weather_default_locations",
    "google_connectors",
    "google_oauth_states",
    "google_connector_events",
    "connector_subscriptions",
    "sync_cursors",
    "provider_events",
    "sync_runs",
    "workspace_items",
    "workspace_item_events",
    "google_provider_objects",
    "provider_evidence",
    "provider_evidence_blocks",
    "work_people",
    "work_threads",
    "work_commitments",
    "work_commitment_sources",
    "work_follow_up_loops",
    "work_follow_up_events",
    "provider_write_receipts",
    "proactive_observations",
    "proactive_cases",
    "proactive_case_events",
    "proactive_context_snapshots",
    "proactive_decisions",
    "proactive_policy_validations",
    "proactive_turns",
    "proactive_action_plans",
    "proactive_action_executions",
    "autonomy_scopes",
    "proactive_feedback",
    "proactive_learning_records",
    "background_tasks",
    "agency_events",
    "jobs",
    "job_events",
    "notifications",
    "notification_deliveries",
    "email_actions",
    "email_thread_watches",
)

REQUIRED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "sessions": (
        "is_active",
        "lifecycle_state",
        "memory_mode",
        "rotated_from_session_id",
        "rotation_reason",
        "created_at",
        "updated_at",
    ),
    "ai_judgments": (
        "judgment_type",
        "source_type",
        "source_id",
        "status",
        "prompt_version",
        "provider_response_id",
        "input_refs",
        "output",
        "confidence",
        "parse_status",
        "validation_status",
        "failure_code",
        "created_at",
        "updated_at",
    ),
    "google_provider_objects": (
        "provider_account_id",
        "object_type",
        "external_id",
        "thread_external_id",
        "calendar_id",
        "ical_uid",
        "status",
        "source_timestamp",
        "observed_at",
        "provider_url",
        "metadata_json",
        "content_digest",
        "created_at",
        "updated_at",
    ),
    "provider_evidence": (
        "provider_object_id",
        "content_digest",
        "retention_policy",
        "extraction_status",
        "lifecycle_state",
    ),
    "background_tasks": (
        "idempotency_key",
        "work_follow_up_loop_id",
        "work_follow_up_loop_version",
        "work_follow_up_scheduled_for",
        "provider_write_receipt_id",
    ),
    "provider_write_receipts": (
        "provider",
        "provider_account_id",
        "action_attempt_id",
        "capability_id",
        "idempotency_key",
        "status",
        "provider_object_ids",
        "request_digest",
        "response_payload",
        "ambiguity_reason",
        "provider_timestamp",
        "provider_etag",
        "provider_history_id",
        "response_digest",
        "created_at",
        "updated_at",
    ),
    "jobs": (
        "agency_sandbox_policy",
        "agency_egress_policy",
    ),
    "action_private_payloads": (
        "payload_kind",
        "payload_digest",
        "payload_enc",
        "encryption_key_version",
    ),
    "work_commitments": ("dedupe_digest",),
    "memory_evidence": (
        "source_turn_id",
        "source_session_id",
        "actor_id",
        "content_class",
        "trust_boundary",
        "lifecycle_state",
        "redaction_posture",
    ),
    "memory_assertions": (
        "subject_entity_id",
        "subject_key",
        "predicate",
        "scope_key",
        "object_value",
        "assertion_type",
        "is_multi_valued",
        "lifecycle_state",
        "confidence",
        "invalidated_at",
        "superseded_by_assertion_id",
    ),
    "memory_versions": (
        "canonical_table",
        "canonical_id",
        "version",
        "change_type",
        "redaction_posture",
        "projection_invalidation",
    ),
    "memory_deletions": (
        "target_table",
        "target_id",
        "deletion_type",
        "redaction_posture",
        "projection_invalidation",
    ),
    "memory_projection_jobs": (
        "projection_kind",
        "target_table",
        "target_id",
        "lifecycle_state",
        "attempts",
        "max_retries",
        "claimed_by",
        "attempt_token",
        "last_heartbeat",
        "run_after",
    ),
    "memory_embedding_projections": (
        "assertion_id",
        "projection_version",
        "source_memory_version",
        "embedding_provider",
        "embedding_model",
        "embedding_dimensions",
        "search_text",
    ),
    "memory_keyword_projections": (
        "canonical_table",
        "canonical_id",
        "projection_version",
        "source_memory_version",
        "search_document",
        "search_vector",
    ),
    "memory_entity_projections": (
        "canonical_table",
        "canonical_id",
        "entity_id",
        "projection_version",
        "source_memory_version",
        "mention_text",
        "features",
    ),
    "memory_graph_projections": (
        "source_entity_id",
        "target_entity_id",
        "projection_version",
        "source_memory_versions",
        "source_projection_versions",
        "relationship_path",
        "distance",
        "score",
    ),
    "memory_temporal_projections": (
        "canonical_table",
        "canonical_id",
        "temporal_kind",
        "projection_version",
        "source_memory_version",
        "metadata_json",
    ),
    "memory_symbol_projections": (
        "canonical_table",
        "canonical_id",
        "repo_key",
        "symbol",
        "path",
        "projection_version",
        "source_memory_version",
        "metadata_json",
    ),
    "memory_context_blocks": (
        "block_type",
        "scope_key",
        "topic_id",
        "lifecycle_state",
        "source_assertion_ids",
        "source_episode_ids",
        "source_trace_ids",
        "source_action_trace_ids",
        "source_procedure_ids",
        "source_project_state_snapshot_ids",
        "source_memory_versions",
        "source_projection_versions",
        "projection_version",
    ),
    "memory_export_artifacts": (
        "scope_key",
        "artifact_kind",
        "export_format",
        "status",
        "projection_version",
        "redaction_posture",
        "source_counts",
        "source_memory_versions",
        "source_projection_versions",
    ),
}

REQUIRED_CONSTRAINTS: Final[dict[str, tuple[str, ...]]] = {
    "sessions": (
        "ck_session_rotation_reason",
        "ck_session_lifecycle_state",
        "ck_session_memory_mode",
        "ck_session_lifecycle_matches_is_active",
        "ck_session_rotation_fields_paired",
    ),
    "ai_judgments": (
        "ck_ai_judgment_type",
        "ck_ai_judgment_status",
        "ck_ai_judgment_parse_status",
        "ck_ai_judgment_validation_status",
        "ck_ai_judgment_failure_code",
    ),
    "google_provider_objects": (
        "ck_google_provider_object_type",
        "ck_google_provider_object_status",
        "ck_google_provider_object_calendar_identity",
    ),
    "provider_evidence": (
        "ck_provider_evidence_retention_policy",
        "ck_provider_evidence_extraction_status",
        "ck_provider_evidence_lifecycle_state",
    ),
    "provider_evidence_blocks": (
        "ck_provider_evidence_block_index",
        "ck_provider_evidence_block_kind",
    ),
    "background_tasks": (
        "ck_background_task_type",
        "ck_background_task_status",
        "ck_background_task_work_follow_up_shape",
        "ck_background_task_provider_write_reconcile_shape",
    ),
    "provider_write_receipts": (
        "ck_provider_write_receipt_provider",
        "ck_provider_write_receipt_capability",
        "ck_provider_write_receipt_status",
        "ck_provider_write_receipt_ambiguity_reason",
    ),
    "jobs": (
        "ck_jobs_agency_sandbox_policy_object",
        "ck_jobs_agency_egress_policy_object",
    ),
    "action_private_payloads": (
        "ck_action_private_payload_kind",
        "ck_action_private_payload_digest",
    ),
    "work_commitments": (
        "ck_work_commitment_lifecycle_state",
        "ck_work_commitment_review_state",
        "ck_work_commitment_due_interval",
    ),
    "work_follow_up_loops": (
        "ck_work_follow_up_loop_owner",
        "ck_work_follow_up_loop_kind",
        "ck_work_follow_up_loop_state",
        "ck_work_follow_up_loop_version",
    ),
    "work_follow_up_events": ("ck_work_follow_up_event_type",),
    "memory_evidence": (
        "ck_memory_evidence_content_class",
        "ck_memory_evidence_trust_boundary",
        "ck_memory_evidence_lifecycle_state",
        "ck_memory_evidence_redaction_posture",
    ),
    "memory_entities": ("ck_memory_entity_type",),
    "memory_relationships": (
        "ck_memory_relationship_lifecycle_state",
        "ck_memory_relationship_confidence_range",
        "ck_memory_relationship_valid_interval",
    ),
    "memory_assertions": (
        "ck_memory_assertion_type",
        "ck_memory_assertion_lifecycle_state",
        "ck_memory_assertion_confidence_range",
        "ck_memory_assertion_valid_interval",
        "ck_memory_assertion_superseded_link",
    ),
    "memory_episodes": (
        "ck_memory_episode_type",
        "ck_memory_episode_lifecycle_state",
        "ck_memory_episode_valid_interval",
    ),
    "memory_reasoning_traces": (
        "ck_memory_reasoning_trace_type",
        "ck_memory_reasoning_trace_outcome",
        "ck_memory_reasoning_trace_lifecycle_state",
    ),
    "memory_action_traces": (
        "ck_memory_action_trace_type",
        "ck_memory_action_trace_outcome",
        "ck_memory_action_trace_lifecycle_state",
    ),
    "memory_procedures": (
        "ck_memory_procedure_lifecycle_state",
        "ck_memory_procedure_review_state",
        "ck_memory_procedure_valid_interval",
    ),
    "memory_reviews": ("ck_memory_review_decision",),
    "memory_conflict_sets": ("ck_memory_conflict_set_lifecycle_state",),
    "memory_salience": (
        "ck_memory_salience_user_priority",
        "ck_memory_salience_score_non_negative",
    ),
    "memory_scope_bindings": (
        "ck_memory_scope_binding_scope_type",
        "ck_memory_scope_binding_memory_mode",
    ),
    "memory_retention_policies": (
        "ck_memory_retention_policy_kind",
        "ck_memory_retention_policy_lifecycle_state",
        "ck_memory_retention_policy_days_positive",
    ),
    "memory_sensitivity_labels": (
        "ck_memory_sensitivity_label_canonical_table",
        "ck_memory_sensitivity_label",
        "ck_memory_sensitivity_label_lifecycle_state",
    ),
    "memory_versions": (
        "ck_memory_version_canonical_table",
        "ck_memory_version_positive",
        "ck_memory_version_change_type",
        "ck_memory_version_redaction_posture",
    ),
    "memory_deletions": (
        "ck_memory_deletion_target_table",
        "ck_memory_deletion_type",
        "ck_memory_deletion_redaction_posture",
    ),
    "memory_projection_jobs": (
        "ck_memory_projection_job_kind",
        "ck_memory_projection_job_lifecycle_state",
        "ck_memory_projection_job_attempts",
        "ck_memory_projection_job_max_retries",
    ),
    "memory_embedding_projections": (
        "ck_memory_embedding_projection_dimensions",
        "ck_memory_embedding_projection_source_memory_version",
    ),
    "memory_keyword_projections": (
        "ck_memory_keyword_projection_canonical_table",
        "ck_memory_keyword_projection_source_memory_version",
    ),
    "memory_entity_projections": (
        "ck_memory_entity_projection_canonical_table",
        "ck_memory_entity_projection_source_memory_version",
    ),
    "memory_graph_projections": (
        "ck_memory_graph_projection_distance",
        "ck_memory_graph_projection_score",
        "ck_memory_graph_projection_source_memory_versions_object",
        "ck_memory_graph_projection_source_projection_versions_object",
    ),
    "memory_temporal_projections": (
        "ck_memory_temporal_projection_canonical_table",
        "ck_memory_temporal_projection_kind",
        "ck_memory_temporal_projection_valid_interval",
        "ck_memory_temporal_projection_source_memory_version",
    ),
    "memory_symbol_projections": (
        "ck_memory_symbol_projection_canonical_table",
        "ck_memory_symbol_projection_source_memory_version",
    ),
    "memory_context_blocks": (
        "ck_memory_context_block_type",
        "ck_memory_context_block_lifecycle_state",
        "ck_memory_context_block_topic_binding",
        "ck_memory_context_block_source_memory_versions_object",
        "ck_memory_context_block_source_projection_versions_object",
    ),
    "memory_topics": (
        "ck_memory_topic_family",
        "ck_memory_topic_lifecycle_state",
    ),
    "memory_topic_members": (
        "ck_memory_topic_member_canonical_table",
        "ck_memory_topic_member_kind",
        "ck_memory_topic_member_rank_nonnegative",
    ),
    "memory_export_artifacts": (
        "ck_memory_export_artifact_kind",
        "ck_memory_export_artifact_format",
        "ck_memory_export_artifact_status",
        "ck_memory_export_artifact_redaction_posture",
        "ck_memory_export_artifact_source_memory_versions_object",
        "ck_memory_export_artifact_source_projection_versions_object",
    ),
    "memory_eval_runs": ("ck_memory_eval_run_status",),
}

REQUIRED_CHECK_SQL_FRAGMENTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "sessions": {
        "ck_session_rotation_reason": ("'threshold_context_pressure'",),
        "ck_session_lifecycle_state": ("'active'", "'rotating'", "'recovery_needed'"),
        "ck_session_memory_mode": ("'normal'", "'temporary'", "'no_memory'"),
        "ck_session_lifecycle_matches_is_active": ("is_active", "lifecycle_state"),
        "ck_session_rotation_fields_paired": ("rotation_reason", "rotated_from_session_id"),
    },
    "ai_judgments": {
        "ck_ai_judgment_type": (
            "'memory_extraction'",
            "'workspace_commitment_extraction'",
        ),
        "ck_ai_judgment_status": ("'succeeded'", "'failed'"),
        "ck_ai_judgment_parse_status": ("'schema_invalid'",),
        "ck_ai_judgment_validation_status": ("'valid'", "'not_validated'"),
        "ck_ai_judgment_failure_code": ("'E_AI_JUDGMENT_BUDGET'",),
    },
    "google_provider_objects": {
        "ck_google_provider_object_type": ("'gmail_message'", "'calendar_event'"),
        "ck_google_provider_object_status": ("'active'", "'deleted'", "'stale'", "'unavailable'"),
        "ck_google_provider_object_calendar_identity": ("calendar_id IS NOT NULL",),
    },
    "provider_evidence": {
        "ck_provider_evidence_lifecycle_state": ("'stale'", "'unavailable'"),
        "ck_provider_evidence_retention_policy": ("'provider_source'", "'short_lived'"),
        "ck_provider_evidence_extraction_status": ("'pending'", "'failed'"),
    },
    "provider_evidence_blocks": {
        "ck_provider_evidence_block_kind": ("'calendar_description'", "'availability'"),
        "ck_provider_evidence_block_index": ("block_index", ">=", "0"),
    },
    "provider_write_receipts": {
        "ck_provider_write_receipt_provider": ("'google'", "'agency'"),
        "ck_provider_write_receipt_status": ("'executing'", "'ambiguous'"),
        "ck_provider_write_receipt_capability": (
            "'cap.email.draft'",
            "'cap.calendar.respond_to_event'",
            "'cap.drive.share'",
            "'cap.agency.request_pr'",
        ),
        "ck_provider_write_receipt_ambiguity_reason": (
            "'ambiguous'",
            "ambiguity_reason IS NOT NULL",
        ),
    },
    "jobs": {
        "ck_jobs_agency_sandbox_policy_object": ("jsonb_typeof", "agency_sandbox_policy"),
        "ck_jobs_agency_egress_policy_object": ("jsonb_typeof", "agency_egress_policy"),
    },
    "action_private_payloads": {
        "ck_action_private_payload_kind": ("'google_provider_write_input'",),
        "ck_action_private_payload_digest": ("length", "payload_digest", "64"),
    },
    "background_tasks": {
        "ck_background_task_type": (
            "'workspace_commitment_extraction_due'",
            "'work_follow_up_evaluate_due'",
            "'provider_write_reconcile_due'",
        ),
        "ck_background_task_status": ("'pending'", "'dead_letter'"),
        "ck_background_task_work_follow_up_shape": (
            "work_follow_up_loop_id IS NOT NULL",
            "work_follow_up_scheduled_for IS NOT NULL",
        ),
        "ck_background_task_provider_write_reconcile_shape": (
            "provider_write_reconcile_due",
            "provider_write_receipt_id IS NOT NULL",
        ),
    },
    "work_commitments": {
        "ck_work_commitment_lifecycle_state": ("'waiting_on_user'", "'superseded'"),
        "ck_work_commitment_review_state": ("'review_required'", "'rejected'"),
        "ck_work_commitment_due_interval": ("due_start", "<", "due_end"),
    },
    "work_follow_up_loops": {
        "ck_work_follow_up_loop_owner": ("commitment_id IS NOT NULL", "thread_id IS NULL"),
        "ck_work_follow_up_loop_kind": (
            "'due_date'",
            "'waiting_for_reply'",
            "'needs_user_reply'",
        ),
        "ck_work_follow_up_loop_state": ("'suppressed'", "'deleted'"),
        "ck_work_follow_up_loop_version": ("version", ">", "0"),
    },
    "work_follow_up_events": {
        "ck_work_follow_up_event_type": ("'notified'", "'stale_noop'", "'suppressed'"),
    },
    "memory_evidence": {
        "ck_memory_evidence_content_class": ("'user_message'", "'tool_output'", "'rotation'"),
        "ck_memory_evidence_trust_boundary": ("'trusted_user'", "'untrusted_tool'"),
        "ck_memory_evidence_lifecycle_state": ("'available'", "'privacy_deleted'"),
        "ck_memory_evidence_redaction_posture": ("'none'", "'privacy_deleted'"),
    },
    "memory_entities": {
        "ck_memory_entity_type": ("'user'", "'repo'", "'assertion_subject'"),
    },
    "memory_relationships": {
        "ck_memory_relationship_lifecycle_state": ("'active'", "'deleted'"),
        "ck_memory_relationship_confidence_range": ("confidence", "0.0", "1.0"),
        "ck_memory_relationship_valid_interval": ("valid_from", "valid_to"),
    },
    "memory_assertions": {
        "ck_memory_assertion_type": ("'fact'", "'procedure'", "'project_state'"),
        "ck_memory_assertion_lifecycle_state": ("'active'", "'conflicted'", "'privacy_deleted'"),
        "ck_memory_assertion_confidence_range": ("confidence", "0.0", "1.0"),
        "ck_memory_assertion_valid_interval": ("valid_from", "valid_to"),
        "ck_memory_assertion_superseded_link": (
            "superseded",
            "superseded_by_assertion_id IS NOT NULL",
        ),
    },
    "memory_episodes": {
        "ck_memory_episode_type": ("'task_event'", "'action_outcome'"),
        "ck_memory_episode_lifecycle_state": ("'active'", "'deleted'"),
        "ck_memory_episode_valid_interval": ("valid_from", "valid_to"),
    },
    "memory_reasoning_traces": {
        "ck_memory_reasoning_trace_type": ("'action_path'", "'user_correction'"),
        "ck_memory_reasoning_trace_outcome": ("'succeeded'", "'corrected'"),
        "ck_memory_reasoning_trace_lifecycle_state": ("'active'", "'deleted'"),
    },
    "memory_action_traces": {
        "ck_memory_action_trace_type": ("'proposal'", "'execution'", "'outcome'"),
        "ck_memory_action_trace_outcome": ("'succeeded'", "'undone'"),
        "ck_memory_action_trace_lifecycle_state": ("'active'", "'privacy_deleted'"),
    },
    "memory_procedures": {
        "ck_memory_procedure_lifecycle_state": ("'active'", "'rejected'", "'deleted'"),
        "ck_memory_procedure_review_state": ("'approved'", "'needs_user_review'"),
        "ck_memory_procedure_valid_interval": ("valid_from", "valid_to"),
    },
    "memory_reviews": {
        "ck_memory_review_decision": ("'approved'", "'needs_user_review'", "'merged'"),
    },
    "memory_conflict_sets": {
        "ck_memory_conflict_set_lifecycle_state": ("'open'", "'resolved'"),
    },
    "memory_salience": {
        "ck_memory_salience_user_priority": ("'pinned'", "'deprioritized'"),
        "ck_memory_salience_score_non_negative": ("score", ">=", "0.0"),
    },
    "memory_scope_bindings": {
        "ck_memory_scope_binding_scope_type": ("'project'", "'repo'", "'proactive_case'"),
        "ck_memory_scope_binding_memory_mode": ("'normal'", "'temporary'", "'no_memory'"),
    },
    "memory_retention_policies": {
        "ck_memory_retention_policy_kind": ("'never_remember'", "'review_after'"),
        "ck_memory_retention_policy_lifecycle_state": ("'active'", "'deleted'"),
        "ck_memory_retention_policy_days_positive": ("retention_days", ">", "0"),
    },
    "memory_sensitivity_labels": {
        "ck_memory_sensitivity_label_canonical_table": ("'memory_evidence'", "'memory_procedures'"),
        "ck_memory_sensitivity_label": ("'secret'", "'source_confidential'"),
        "ck_memory_sensitivity_label_lifecycle_state": ("'active'", "'deleted'"),
    },
    "memory_versions": {
        "ck_memory_version_canonical_table": (
            "'memory_assertions'",
            "'memory_export_artifacts'",
        ),
        "ck_memory_version_positive": ("version", ">", "0"),
        "ck_memory_version_change_type": ("'deleted'", "'privacy_deleted'", "'exported'"),
        "ck_memory_version_redaction_posture": ("'none'", "'privacy_deleted'"),
    },
    "memory_deletions": {
        "ck_memory_deletion_target_table": ("'memory_evidence'", "'memory_assertions'"),
        "ck_memory_deletion_type": ("'delete'", "'privacy_delete'", "'retract'"),
        "ck_memory_deletion_redaction_posture": ("'none'", "'privacy_deleted'"),
    },
    "memory_projection_jobs": {
        "ck_memory_projection_job_kind": ("'embedding'", "'hot_index'"),
        "ck_memory_projection_job_lifecycle_state": ("'pending'", "'dead_letter'"),
        "ck_memory_projection_job_attempts": ("attempts", ">=", "0"),
        "ck_memory_projection_job_max_retries": ("max_retries", ">=", "0"),
    },
    "memory_embedding_projections": {
        "ck_memory_embedding_projection_dimensions": ("embedding_dimensions", "1536"),
        "ck_memory_embedding_projection_source_memory_version": (
            "source_memory_version",
            ">",
            "0",
        ),
    },
    "memory_keyword_projections": {
        "ck_memory_keyword_projection_canonical_table": (
            "'memory_assertions'",
            "'memory_evidence'",
        ),
        "ck_memory_keyword_projection_source_memory_version": (
            "source_memory_version",
            ">",
            "0",
        ),
    },
    "memory_entity_projections": {
        "ck_memory_entity_projection_canonical_table": (
            "'memory_assertions'",
            "'project_state_snapshots'",
        ),
        "ck_memory_entity_projection_source_memory_version": (
            "source_memory_version",
            ">",
            "0",
        ),
    },
    "memory_graph_projections": {
        "ck_memory_graph_projection_distance": ("distance", ">=", "0"),
        "ck_memory_graph_projection_score": ("score", ">=", "0.0"),
        "ck_memory_graph_projection_source_memory_versions_object": (
            "jsonb_typeof",
            "source_memory_versions",
            "object",
        ),
        "ck_memory_graph_projection_source_projection_versions_object": (
            "jsonb_typeof",
            "source_projection_versions",
            "object",
        ),
    },
    "memory_temporal_projections": {
        "ck_memory_temporal_projection_canonical_table": (
            "'memory_assertions'",
            "'project_state_snapshots'",
        ),
        "ck_memory_temporal_projection_kind": ("'validity'", "'retention'"),
        "ck_memory_temporal_projection_valid_interval": ("valid_from", "valid_to"),
        "ck_memory_temporal_projection_source_memory_version": (
            "source_memory_version",
            ">",
            "0",
        ),
    },
    "memory_symbol_projections": {
        "ck_memory_symbol_projection_canonical_table": (
            "'memory_assertions'",
            "'project_state_snapshots'",
        ),
        "ck_memory_symbol_projection_source_memory_version": (
            "source_memory_version",
            ">",
            "0",
        ),
    },
    "memory_context_blocks": {
        "ck_memory_context_block_type": ("'hot_index'", "'topic'", "'project_state'"),
        "ck_memory_context_block_lifecycle_state": ("'active'", "'deleted'"),
        "ck_memory_context_block_topic_binding": ("topic_id IS NOT NULL", "topic_id IS NULL"),
        "ck_memory_context_block_source_memory_versions_object": (
            "jsonb_typeof",
            "source_memory_versions",
            "object",
        ),
        "ck_memory_context_block_source_projection_versions_object": (
            "jsonb_typeof",
            "source_projection_versions",
            "object",
        ),
    },
    "memory_topics": {
        "ck_memory_topic_family": ("'user-profile'", "'repo-conventions'", "'open-risks'"),
        "ck_memory_topic_lifecycle_state": ("'active'", "'deleted'"),
    },
    "memory_topic_members": {
        "ck_memory_topic_member_canonical_table": ("'memory_assertions'", "'memory_procedures'"),
        "ck_memory_topic_member_kind": ("'source'", "'summary'"),
        "ck_memory_topic_member_rank_nonnegative": ("rank", ">=", "0"),
    },
    "memory_export_artifacts": {
        "ck_memory_export_artifact_kind": ("'memory_snapshot'", "'agents_md'"),
        "ck_memory_export_artifact_format": ("'json'", "'markdown'"),
        "ck_memory_export_artifact_status": ("'created'", "'failed'"),
        "ck_memory_export_artifact_redaction_posture": ("'redacted'", "'privacy_deleted'"),
        "ck_memory_export_artifact_source_memory_versions_object": (
            "jsonb_typeof",
            "source_memory_versions",
            "object",
        ),
        "ck_memory_export_artifact_source_projection_versions_object": (
            "jsonb_typeof",
            "source_projection_versions",
            "object",
        ),
    },
    "memory_eval_runs": {
        "ck_memory_eval_run_status": ("'completed'", "'failed'"),
    },
}

FORBIDDEN_CHECK_SQL_FRAGMENTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "ai_judgments": {
        "ck_ai_judgment_type": ("'tool_strategy'",),
    },
    "memory_scope_bindings": {
        # Session memory mode lives on SessionRecord; "session" is not a binding scope.
        "ck_memory_scope_binding_scope_type": ("'session'",),
    },
}

REQUIRED_FOREIGN_KEYS: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "action_attempts": {
        "turn_id": ("turns", "RESTRICT"),
    },
    "turn_idempotency_keys": {
        "session_id": ("sessions", "RESTRICT"),
        "turn_id": ("turns", "RESTRICT"),
    },
}

REQUIRED_INDEXES: Final[dict[str, tuple[str, ...]]] = {
    "sessions": (
        "ix_single_active_session",
        "ix_sessions_rotated_from_session_id_unique",
    ),
    "ai_judgments": (
        "ix_ai_judgments_judgment_type",
        "ix_ai_judgments_source_type",
        "ix_ai_judgments_source_id",
    ),
    "background_tasks": (
        "ix_background_tasks_idempotency_key_unique",
        "ix_background_tasks_work_follow_up_unique",
        "ix_background_tasks_provider_write_reconcile_unique",
    ),
    "google_provider_objects": (
        "ix_google_provider_object_identity_unique",
        "ix_google_provider_objects_calendar_event_identity_unique",
        "ix_google_provider_objects_thread",
        "ix_google_provider_objects_calendar_id",
        "ix_google_provider_objects_content_digest",
    ),
    "provider_evidence": (
        "ix_provider_evidence_identity_digest_unique",
        "ix_provider_evidence_source",
    ),
    "provider_evidence_blocks": ("ix_provider_evidence_blocks_unique",),
    "work_commitments": ("ix_work_commitments_active_source_unique",),
    "work_follow_up_loops": ("ix_work_follow_up_loops_due",),
    "provider_write_receipts": (
        "ix_provider_write_receipts_idempotency_unique",
        "ix_provider_write_receipts_attempt_idempotency_unique",
        "ix_provider_write_receipts_action_attempt_id",
        "ix_provider_write_receipts_provider_timestamp",
    ),
    "action_private_payloads": ("ix_action_private_payloads_action_attempt_id",),
}

REQUIRED_UNIQUE_INDEXES: Final[dict[str, tuple[str, ...]]] = {
    "sessions": (
        "ix_single_active_session",
        "ix_sessions_rotated_from_session_id_unique",
    ),
    "background_tasks": (
        "ix_background_tasks_idempotency_key_unique",
        "ix_background_tasks_work_follow_up_unique",
        "ix_background_tasks_provider_write_reconcile_unique",
    ),
    "google_provider_objects": (
        "ix_google_provider_object_identity_unique",
        "ix_google_provider_objects_calendar_event_identity_unique",
    ),
    "provider_evidence": ("ix_provider_evidence_identity_digest_unique",),
    "provider_evidence_blocks": ("ix_provider_evidence_blocks_unique",),
    "work_commitments": ("ix_work_commitments_active_source_unique",),
    "provider_write_receipts": (
        "ix_provider_write_receipts_idempotency_unique",
        "ix_provider_write_receipts_attempt_idempotency_unique",
    ),
    "action_private_payloads": ("ix_action_private_payloads_action_attempt_id",),
}

REQUIRED_INDEX_SQL_FRAGMENTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "sessions": {
        "ix_single_active_session": ("is_active",),
        "ix_sessions_rotated_from_session_id_unique": ("rotated_from_session_id IS NOT NULL",),
    },
    "background_tasks": {
        "ix_background_tasks_idempotency_key_unique": ("idempotency_key IS NOT NULL",),
        "ix_background_tasks_work_follow_up_unique": ("work_follow_up_evaluate_due",),
        "ix_background_tasks_provider_write_reconcile_unique": ("provider_write_reconcile_due",),
    },
    "google_provider_objects": {
        "ix_google_provider_object_identity_unique": ("calendar_event", "<>"),
        "ix_google_provider_objects_calendar_event_identity_unique": ("calendar_event", "="),
    },
    "work_commitments": {
        "ix_work_commitments_active_source_unique": ("active",),
    },
    "provider_write_receipts": {
        "ix_provider_write_receipts_idempotency_unique": ("idempotency_key IS NOT NULL",),
        "ix_provider_write_receipts_attempt_idempotency_unique": ("idempotency_key IS NOT NULL",),
    },
}

REQUIRED_INDEX_COLUMNS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "sessions": {
        "ix_single_active_session": ("is_active",),
        "ix_sessions_rotated_from_session_id_unique": ("rotated_from_session_id",),
    },
    "ai_judgments": {
        "ix_ai_judgments_judgment_type": ("judgment_type",),
        "ix_ai_judgments_source_type": ("source_type",),
        "ix_ai_judgments_source_id": ("source_id",),
    },
    "background_tasks": {
        "ix_background_tasks_idempotency_key_unique": ("idempotency_key",),
        "ix_background_tasks_work_follow_up_unique": (
            "work_follow_up_loop_id",
            "work_follow_up_loop_version",
            "work_follow_up_scheduled_for",
        ),
        "ix_background_tasks_provider_write_reconcile_unique": ("provider_write_receipt_id",),
    },
    "google_provider_objects": {
        "ix_google_provider_object_identity_unique": (
            "provider_account_id",
            "object_type",
            "external_id",
        ),
        "ix_google_provider_objects_calendar_event_identity_unique": (
            "provider_account_id",
            "object_type",
            "calendar_id",
            "external_id",
        ),
        "ix_google_provider_objects_thread": ("provider_account_id", "thread_external_id"),
        "ix_google_provider_objects_calendar_id": ("calendar_id",),
        "ix_google_provider_objects_content_digest": ("content_digest",),
    },
    "provider_evidence": {
        "ix_provider_evidence_identity_digest_unique": ("provider_object_id", "content_digest"),
        "ix_provider_evidence_source": (
            "provider",
            "provider_account_id",
            "source_kind",
            "external_id",
        ),
    },
    "provider_evidence_blocks": {
        "ix_provider_evidence_blocks_unique": ("evidence_id", "block_index"),
    },
    "work_commitments": {
        "ix_work_commitments_active_source_unique": (
            "provider",
            "provider_account_id",
            "dedupe_digest",
        ),
    },
    "work_follow_up_loops": {
        "ix_work_follow_up_loops_due": ("state", "next_check_at", "id"),
    },
    "provider_write_receipts": {
        "ix_provider_write_receipts_idempotency_unique": (
            "provider",
            "provider_account_id",
            "idempotency_key",
        ),
        "ix_provider_write_receipts_attempt_idempotency_unique": (
            "action_attempt_id",
            "idempotency_key",
        ),
        "ix_provider_write_receipts_action_attempt_id": ("action_attempt_id",),
        "ix_provider_write_receipts_provider_timestamp": ("provider_timestamp",),
    },
    "action_private_payloads": {
        "ix_action_private_payloads_action_attempt_id": ("action_attempt_id",),
    },
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    project_root = _project_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def run_migrations(database_url: str, *, revision: str = "head") -> None:
    command.upgrade(_alembic_config(database_url), revision)


def reset_schema_for_tests(engine: Engine, database_url: str) -> None:
    if engine.dialect.name != "postgresql":
        msg = "test schema reset only supports postgresql"
        raise RuntimeError(msg)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(database_url)


def missing_required_tables(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    missing = [
        f"missing_table:{table_name}"
        for table_name in REQUIRED_TABLES
        if not inspector.has_table(table_name)
    ]
    if missing:
        return missing

    with engine.connect() as connection:
        current_revision = connection.execute(text("SELECT version_num FROM alembic_version"))
        current_revisions = {str(row[0]) for row in current_revision}
    heads = set(ScriptDirectory.from_config(_alembic_config(str(engine.url))).get_heads())
    for head in sorted(heads - current_revisions):
        missing.append(f"missing_alembic_head:{head}")

    for table_name, column_names in REQUIRED_COLUMNS.items():
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if column_name not in existing_columns:
                missing.append(f"missing_column:{table_name}.{column_name}")

    for table_name, constraint_names in REQUIRED_CONSTRAINTS.items():
        existing_constraints = {
            constraint["name"]: str(constraint.get("sqltext") or "")
            for constraint in inspector.get_check_constraints(table_name)
            if isinstance(constraint.get("name"), str)
        }
        for constraint_name in constraint_names:
            if constraint_name not in existing_constraints:
                missing.append(f"missing_constraint:{table_name}.{constraint_name}")
                continue
            sql_text = existing_constraints[constraint_name]
            for fragment in REQUIRED_CHECK_SQL_FRAGMENTS.get(table_name, {}).get(
                constraint_name,
                (),
            ):
                if fragment not in sql_text:
                    missing.append(f"missing_constraint_fragment:{table_name}.{constraint_name}")
                    break
            for fragment in FORBIDDEN_CHECK_SQL_FRAGMENTS.get(table_name, {}).get(
                constraint_name,
                (),
            ):
                if fragment in sql_text:
                    missing.append(f"forbidden_constraint_fragment:{table_name}.{constraint_name}")
                    break

    for table_name, foreign_keys in REQUIRED_FOREIGN_KEYS.items():
        existing_foreign_keys: dict[str, dict[str, Any]] = {}
        for reflected_foreign_key in inspector.get_foreign_keys(table_name):
            constrained_columns = reflected_foreign_key.get("constrained_columns")
            if not isinstance(constrained_columns, list) or len(constrained_columns) != 1:
                continue
            column_name = constrained_columns[0]
            if isinstance(column_name, str):
                existing_foreign_keys[column_name] = dict(reflected_foreign_key)
        for column_name, expected in foreign_keys.items():
            expected_table, expected_ondelete = expected
            existing_foreign_key = existing_foreign_keys.get(column_name)
            if existing_foreign_key is None:
                missing.append(f"missing_foreign_key:{table_name}.{column_name}")
                continue
            if existing_foreign_key.get("referred_table") != expected_table:
                missing.append(f"wrong_foreign_key_table:{table_name}.{column_name}")
            options = existing_foreign_key.get("options")
            actual_ondelete = (
                str(options.get("ondelete")).upper()
                if isinstance(options, dict) and options.get("ondelete") is not None
                else ""
            )
            if actual_ondelete != expected_ondelete:
                missing.append(f"wrong_foreign_key_ondelete:{table_name}.{column_name}")

    for table_name, index_names in REQUIRED_INDEXES.items():
        existing_indexes = {
            str(index["name"]): index
            for index in inspector.get_indexes(table_name)
            if isinstance(index.get("name"), str)
        }
        for index_name in index_names:
            if index_name not in existing_indexes:
                missing.append(f"missing_index:{table_name}.{index_name}")
                continue
            if index_name in REQUIRED_UNIQUE_INDEXES.get(table_name, ()):
                if existing_indexes[index_name].get("unique") is not True:
                    missing.append(f"missing_unique_index:{table_name}.{index_name}")
            expected_columns = REQUIRED_INDEX_COLUMNS.get(table_name, {}).get(index_name)
            if expected_columns is not None:
                actual_columns = tuple(
                    str(column_name)
                    for column_name in existing_indexes[index_name].get("column_names") or ()
                )
                if actual_columns != expected_columns:
                    missing.append(f"missing_index_columns:{table_name}.{index_name}")
            dialect_options = existing_indexes[index_name].get("dialect_options")
            dialect_text = str(dialect_options or "")
            for fragment in REQUIRED_INDEX_SQL_FRAGMENTS.get(table_name, {}).get(index_name, ()):
                if fragment not in dialect_text:
                    missing.append(f"missing_index_fragment:{table_name}.{index_name}")
                    break

    return missing
