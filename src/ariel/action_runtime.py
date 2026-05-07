from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariel.attachment_content import AttachmentContentRuntime
from ariel.capability_registry import (
    AGENCY_CAPABILITY_IDS,
    ATTACHMENT_CAPABILITY_IDS,
    CapabilityDefinition,
    DISCORD_CAPABILITY_IDS,
    canonical_action_payload,
    capability_id_for_response_tool_name,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.executor import (
    ExecutionResult,
    append_turn_event,
    execute_capability,
    next_turn_event_sequence,
    preflight_capability_execution,
)
from ariel.google_connector import (
    GOOGLE_CAPABILITY_IDS,
    GOOGLE_READ_CAPABILITY_IDS,
    GoogleCapabilityExecutionResult,
    GoogleConnectorRuntime,
)
from ariel.persistence import (
    ActionAttemptRecord,
    ApprovalRequestRecord,
    ArtifactRecord,
    BackgroundTaskRecord,
    TurnRecord,
    to_rfc3339,
)
from ariel.policy_engine import evaluate_proposal
from ariel.weather_state import resolve_weather_location

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
class FunctionCallProcessingResult:
    assistant_message: str
    function_call_outputs: list[dict[str, Any]]
    action_attempts: list[ActionAttemptRecord]
    assistant_sources: list[dict[str, Any]] = field(default_factory=list)
    silent_response: bool = False
    runtime_provenance: RuntimeProvenance | None = None
    tool_result_interpreter_input: dict[str, Any] | None = None
    tool_result_interpreter_output: dict[str, Any] | None = None


@dataclass(slots=True)
class ApprovalDecisionResult:
    approval: ApprovalRequestRecord
    action_attempt: ActionAttemptRecord
    assistant_message: str


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    status: Literal["clean", "tainted"]
    evidence: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class GroundedSourceCandidate:
    title: str
    source: str
    snippet: str
    retrieved_at: datetime
    published_at: datetime | None


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


def _enqueue_action_execution_task(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> BackgroundTaskRecord:
    now = now_fn()
    task = BackgroundTaskRecord(
        id=new_id_fn("tsk"),
        task_type="execute_action_attempt",
        payload={"action_attempt_id": action_attempt.id},
        status="pending",
        attempts=0,
        max_attempts=3,
        error=None,
        claimed_by=None,
        run_after=now,
        last_heartbeat=None,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.flush()
    return task


def _append_action_execution_event(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    event_type: str,
    payload_data: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    append_turn_event(
        db=db,
        session_id=action_attempt.session_id,
        turn_id=action_attempt.turn_id,
        sequence=next_turn_event_sequence(db=db, turn_id=action_attempt.turn_id),
        event_type=event_type,
        payload_data=payload_data,
        new_id_fn=new_id_fn,
        now_fn=now_fn,
    )


def _fail_action_execution(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    error: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    action_attempt.status = "failed"
    action_attempt.execution_output = None
    action_attempt.execution_error = error
    action_attempt.updated_at = now_fn()
    approval = db.scalar(
        select(ApprovalRequestRecord)
        .where(ApprovalRequestRecord.action_attempt_id == action_attempt.id)
        .limit(1)
    )
    _append_action_execution_event(
        db=db,
        action_attempt=action_attempt,
        event_type="evt.action.execution.failed",
        payload_data={
            "action_attempt_id": action_attempt.id,
            "error": error,
            "approval_ref": approval.id if approval is not None else None,
        },
        now_fn=now_fn,
        new_id_fn=new_id_fn,
    )


def approval_execution_failure_message(error: str) -> str:
    failure_reason = error.strip() or "execution_failed"
    if failure_reason.startswith("integrity_mismatch"):
        failure_reason = failure_reason.replace("integrity_mismatch", "integrity mismatch", 1)

    recovery = _TYPED_AUTH_RECOVERY.get(failure_reason)
    if recovery is None:
        recovery = _TYPED_PROVIDER_RECOVERY.get(failure_reason)

    message = f"approval recorded, but action execution failed: {failure_reason}"
    if recovery is not None:
        return f"{message}. {recovery}"
    return message


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


_MAX_CITED_SOURCES = 4
_MAX_SNIPPET_LENGTH = 320
_MAX_DIRECT_TOOL_OUTPUT_JSON_CHARS = 6_000
_MAX_INTERPRETER_OUTPUT_JSON_CHARS = 16_000
_MODALITY_HEAVY_VALUES = {"audio", "document", "image", "video"}
_CONTRADICTION_SIGNAL_KEYS = {
    "conflict",
    "conflicts",
    "conflicting_results",
    "conflicting_sources",
    "contradiction",
    "contradictions",
    "inconsistent_results",
}
_MAPS_RETRIEVAL_CAPABILITY_IDS = {"cap.maps.directions", "cap.maps.search_places"}
_WEB_EXTRACT_RETRIEVAL_CAPABILITY_IDS = {"cap.web.extract"}
_GROUNDED_RETRIEVAL_CAPABILITIES = {
    "cap.search.web",
    "cap.search.news",
    "cap.weather.forecast",
    *_MAPS_RETRIEVAL_CAPABILITY_IDS,
    *_WEB_EXTRACT_RETRIEVAL_CAPABILITY_IDS,
    *ATTACHMENT_CAPABILITY_IDS,
    *GOOGLE_READ_CAPABILITY_IDS,
}

_TYPED_AUTH_RECOVERY: dict[str, str] = {
    "not_connected": "Connect Google to continue.",
    "consent_required": "Reconnect Google and grant the requested scope.",
    "scope_missing": "Reconnect Google and re-consent to required scopes.",
    "token_expired": "Retry once; if it still fails, reconnect Google.",
    "access_revoked": "Reconnect Google from scratch.",
}

_TYPED_PROVIDER_RECOVERY: dict[str, str] = {
    "provider_timeout": "Google timed out. Retry shortly.",
    "provider_network_failure": "Google had a network failure. Retry shortly.",
    "provider_rate_limited": "Google rate limited this request. Wait briefly, then retry.",
    "provider_upstream_failure": "Google is degraded right now. Retry shortly.",
    "provider_permission_denied": (
        "Google denied provider-level access. Verify file permissions and retry."
    ),
    "provider_request_rejected": "Google rejected this request. Verify inputs and retry.",
    "resource_unavailable": "The file is unavailable. Verify file ID and access, then retry.",
    "provider_invalid_payload": "Google returned an invalid payload. Retry shortly.",
    "provider_unreachable": "Google could not be reached. Retry shortly.",
}


def _parse_rfc3339_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _truncate_snippet(value: str) -> str:
    normalized = value.strip()
    if len(normalized) <= _MAX_SNIPPET_LENGTH:
        return normalized
    return normalized[:_MAX_SNIPPET_LENGTH].rstrip() + "..."


def _json_payload_size(value: Any) -> int:
    try:
        return len(json.dumps(jsonable_encoder(value), sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(value))


def _structured_signal_present(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict):
        return bool(value)
    return False


def _has_contradiction_signal(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if (
                isinstance(key, str)
                and key.strip().lower() in _CONTRADICTION_SIGNAL_KEYS
                and _structured_signal_present(nested_value)
            ):
                return True
            if _has_contradiction_signal(nested_value):
                return True
    if isinstance(value, list):
        for nested_value in value:
            if _has_contradiction_signal(nested_value):
                return True
    return False


def _source_count(output_payload: Any) -> int:
    if not isinstance(output_payload, dict):
        return 0
    raw_results = output_payload.get("results")
    if not isinstance(raw_results, list):
        return 0
    return sum(1 for item in raw_results if isinstance(item, dict))


def _has_modality_heavy_output(output_payload: Any) -> bool:
    if not isinstance(output_payload, dict):
        return False
    modality = output_payload.get("modality")
    if isinstance(modality, str) and modality.strip().lower() in _MODALITY_HEAVY_VALUES:
        return True
    blocks = output_payload.get("blocks")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if isinstance(kind, str) and kind.strip().lower() not in {"", "text"}:
            return True
    return False


def _tool_result_interpretation_reason_codes(output_payload: Any) -> list[str]:
    reason_codes: list[str] = []
    if _json_payload_size(output_payload) > _MAX_DIRECT_TOOL_OUTPUT_JSON_CHARS:
        reason_codes.append("large")
    if _source_count(output_payload) > 1:
        reason_codes.append("multi_source")
    if _has_contradiction_signal(output_payload):
        reason_codes.append("contradictory")
    if _has_modality_heavy_output(output_payload):
        reason_codes.append("modality_heavy")
    return reason_codes


def _bounded_interpreter_output(output_payload: Any) -> tuple[Any, bool]:
    encoded = jsonable_encoder(output_payload)
    try:
        rendered = json.dumps(encoded, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = str(encoded)
    if len(rendered) <= _MAX_INTERPRETER_OUTPUT_JSON_CHARS:
        return encoded, False
    return (
        {
            "bounded": True,
            "original_json_chars": len(rendered),
            "json_prefix": rendered[:_MAX_INTERPRETER_OUTPUT_JSON_CHARS],
        },
        True,
    )


def _append_reason_codes(
    existing: dict[str, list[str]],
    *,
    action_attempt_id: str,
    reason_codes: list[str],
) -> None:
    if not reason_codes:
        return
    current = existing.setdefault(action_attempt_id, [])
    for reason_code in reason_codes:
        if reason_code not in current:
            current.append(reason_code)


def _build_tool_result_interpreter_input(
    *,
    session_id: str,
    turn_id: str,
    action_attempts: list[ActionAttemptRecord],
    reason_codes_by_attempt_id: dict[str, list[str]],
    call_ids_by_attempt_id: dict[str, str],
    taint_by_attempt_id: dict[str, dict[str, Any]],
    retrieval_sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    audited_tool_outputs: list[dict[str, Any]] = []
    output_refs: list[str] = []
    capability_ids: list[str] = []
    omitted_output_refs: list[dict[str, Any]] = []
    typed_tool_failures: list[dict[str, Any]] = []

    for action_attempt in action_attempts:
        if action_attempt.status in {"failed", "rejected", "denied", "expired"}:
            failure_reason = action_attempt.execution_error or action_attempt.policy_reason
            typed_tool_failures.append(
                {
                    "action_attempt_id": action_attempt.id,
                    "capability_id": action_attempt.capability_id,
                    "status": action_attempt.status,
                    "failure_code": failure_reason or action_attempt.status,
                }
            )

        reason_codes = reason_codes_by_attempt_id.get(action_attempt.id)
        if not reason_codes:
            continue
        output_refs.append(action_attempt.id)
        if action_attempt.capability_id not in capability_ids:
            capability_ids.append(action_attempt.capability_id)
        bounded_output, truncated = _bounded_interpreter_output(action_attempt.execution_output)
        if truncated:
            omitted_output_refs.append(
                {
                    "output_ref": action_attempt.id,
                    "reason": "interpreter_input_budget",
                }
            )
        audited_tool_outputs.append(
            {
                "output_ref": action_attempt.id,
                "action_attempt_id": action_attempt.id,
                "call_id": call_ids_by_attempt_id.get(action_attempt.id),
                "capability_id": action_attempt.capability_id,
                "status": action_attempt.status,
                "reason_codes": reason_codes,
                "output": bounded_output,
                "taint": taint_by_attempt_id.get(action_attempt.id),
                "provenance": {
                    "capability_version": action_attempt.capability_version,
                    "capability_contract_hash": action_attempt.capability_contract_hash,
                },
            }
        )

    if not audited_tool_outputs:
        return None

    citation_refs = list(retrieval_sources)
    artifact_refs = [
        source["artifact_id"]
        for source in retrieval_sources
        if isinstance(source.get("artifact_id"), str)
    ]
    reason_codes = sorted(
        {
            reason_code
            for attempt_reason_codes in reason_codes_by_attempt_id.values()
            for reason_code in attempt_reason_codes
        }
    )
    return {
        "judgment_type": "tool_result_interpretation",
        "source_type": "turn",
        "source_id": turn_id,
        "session_id": session_id,
        "action_attempt_ids": output_refs,
        "capability_ids": capability_ids,
        "audited_tool_outputs": audited_tool_outputs,
        "artifact_refs": artifact_refs,
        "citation_refs": citation_refs,
        "taint": [taint_by_attempt_id[item] for item in output_refs if item in taint_by_attempt_id],
        "provenance": {
            "turn_id": turn_id,
            "action_attempt_ids": output_refs,
        },
        "typed_tool_failures": typed_tool_failures,
        "omitted_output_refs": omitted_output_refs,
        "reason_codes": reason_codes,
        "budgets": {
            "max_direct_tool_output_json_chars": _MAX_DIRECT_TOOL_OUTPUT_JSON_CHARS,
            "max_interpreter_output_json_chars": _MAX_INTERPRETER_OUTPUT_JSON_CHARS,
        },
        "output_contract": {
            "findings": [],
            "contradictions": [],
            "uncertainty": [],
            "selected_output_refs": [],
            "omitted_output_refs": [],
            "citation_refs": [],
            "artifact_refs": [],
            "recommended_next_evidence": [],
            "confidence": None,
        },
    }


def _redact_function_outputs_requiring_interpretation(
    *,
    function_call_outputs: list[dict[str, Any]],
    reason_codes_by_call_id: dict[str, dict[str, Any]],
) -> None:
    for function_call_output in function_call_outputs:
        call_id = function_call_output.get("call_id")
        if not isinstance(call_id, str):
            continue
        interpreter_route = reason_codes_by_call_id.get(call_id)
        if interpreter_route is None:
            continue
        function_call_output["output"] = json.dumps(
            jsonable_encoder(
                {
                    "status": "succeeded",
                    "capability_id": interpreter_route["capability_id"],
                    "action_attempt_id": interpreter_route["action_attempt_id"],
                    "tool_result_interpreter": {
                        "required": True,
                        "reason_codes": interpreter_route["reason_codes"],
                        "output_ref": interpreter_route["action_attempt_id"],
                        "output": None,
                    },
                }
            ),
            sort_keys=True,
            separators=(",", ":"),
        )


def _extract_search_source_candidates(
    *,
    output_payload: Any,
    now_fn: Callable[[], datetime],
) -> list[GroundedSourceCandidate]:
    if not isinstance(output_payload, dict):
        return []
    raw_results = output_payload.get("results")
    if not isinstance(raw_results, list):
        return []

    retrieved_at = _parse_rfc3339_timestamp(output_payload.get("retrieved_at")) or now_fn()
    candidates: list[GroundedSourceCandidate] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            continue
        title_raw = raw_result.get("title")
        source_raw = raw_result.get("source")
        snippet_raw = raw_result.get("snippet")
        if (
            not isinstance(title_raw, str)
            or not isinstance(source_raw, str)
            or not isinstance(snippet_raw, str)
        ):
            continue
        title = title_raw.strip()
        source = source_raw.strip()
        snippet = _truncate_snippet(snippet_raw)
        if not title or not source or not snippet:
            continue
        published_at = _parse_rfc3339_timestamp(raw_result.get("published_at"))
        candidates.append(
            GroundedSourceCandidate(
                title=title,
                source=source,
                snippet=snippet,
                retrieved_at=retrieved_at,
                published_at=published_at,
            )
        )
    return candidates


def _persist_retrieval_artifacts(
    *,
    db: Session,
    session_id: str,
    turn_id: str,
    action_attempt: ActionAttemptRecord,
    capability_id: str,
    candidates: list[GroundedSourceCandidate],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assistant_sources: list[dict[str, Any]] = []
    for candidate in candidates:
        now = now_fn()
        artifact = ArtifactRecord(
            id=new_id_fn("art"),
            session_id=session_id,
            turn_id=turn_id,
            action_attempt_id=action_attempt.id,
            artifact_type="retrieval_provenance",
            title=candidate.title,
            source=candidate.source,
            snippet=candidate.snippet,
            retrieved_at=candidate.retrieved_at,
            published_at=candidate.published_at,
            created_at=now,
            updated_at=now,
        )
        db.add(artifact)
        db.flush()
        assistant_sources.append(
            {
                "artifact_id": artifact.id,
                "title": artifact.title,
                "source": artifact.source,
                "retrieved_at": to_rfc3339(artifact.retrieved_at),
                "published_at": (
                    to_rfc3339(artifact.published_at) if artifact.published_at is not None else None
                ),
            }
        )
    return assistant_sources


def _response_function_call_output(*, call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(jsonable_encoder(payload), sort_keys=True, separators=(",", ":")),
    }


def process_response_function_calls(
    *,
    db: Session,
    session_id: str,
    turn: TurnRecord,
    assistant_message: str,
    function_calls_raw: Any,
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    runtime_provenance: RuntimeProvenance | None = None,
    google_runtime: GoogleConnectorRuntime | None = None,
    agency_runtime: Any | None = None,
    attachment_runtime: AttachmentContentRuntime | None = None,
) -> FunctionCallProcessingResult:
    inline_results: list[dict[str, Any]] = []
    pending_approvals: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []
    function_call_outputs: list[dict[str, Any]] = []
    created_action_attempts: list[ActionAttemptRecord] = []
    pending_approval_created = False
    retrieval_requested = False
    retrieval_errors: list[str] = []
    retrieval_sources: list[dict[str, Any]] = []
    retrieval_capability_ids: set[str] = set()
    result_runtime_provenance: RuntimeProvenance | None = None
    silent_response = False
    call_ids_by_attempt_id: dict[str, str] = {}
    taint_by_attempt_id: dict[str, dict[str, Any]] = {}
    interpreter_reason_codes_by_attempt_id: dict[str, list[str]] = {}

    function_calls = function_calls_raw if isinstance(function_calls_raw, list) else []
    for function_call_index, function_call_raw in enumerate(function_calls, start=1):
        function_call_payload = function_call_raw if isinstance(function_call_raw, dict) else {}
        call_id_raw = function_call_payload.get("call_id")
        call_id = call_id_raw.strip() if isinstance(call_id_raw, str) else ""
        tool_name_raw = function_call_payload.get("name")
        tool_name = tool_name_raw.strip() if isinstance(tool_name_raw, str) else ""
        capability_id = capability_id_for_response_tool_name(tool_name) or "invalid.capability"
        is_google_capability_call = capability_id in GOOGLE_CAPABILITY_IDS
        is_agency_capability_call = capability_id in AGENCY_CAPABILITY_IDS
        is_discord_capability_call = capability_id in DISCORD_CAPABILITY_IDS
        is_attachment_capability_call = capability_id in ATTACHMENT_CAPABILITY_IDS
        is_retrieval_call = capability_id in _GROUNDED_RETRIEVAL_CAPABILITIES
        is_weather_forecast_call = capability_id == "cap.weather.forecast"
        if is_retrieval_call:
            retrieval_requested = True
            retrieval_capability_ids.add(capability_id)
        raw_arguments = function_call_payload.get("arguments")
        try:
            decoded_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
        except ValueError:
            decoded_arguments = {}
        input_payload = (
            jsonable_encoder(decoded_arguments) if isinstance(decoded_arguments, dict) else {}
        )
        if is_weather_forecast_call and set(input_payload.keys()).issubset(
            {"location", "timeframe"}
        ):
            explicit_location_raw = input_payload.get("location")
            explicit_location = (
                explicit_location_raw if isinstance(explicit_location_raw, str) else None
            )
            resolved_location, _ = resolve_weather_location(
                db=db,
                explicit_location=explicit_location,
                now_fn=now_fn,
            )
            input_payload["location"] = resolved_location
        model_declared_taint_status = _model_declared_taint_status(function_call_payload)
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
            evaluation.normalized_input
            if evaluation.normalized_input is not None
            else input_payload
        )
        frozen_payload = canonical_action_payload(
            capability_id=capability_id,
            input_payload=frozen_input_payload,
        )
        action_attempt = ActionAttemptRecord(
            id=new_id_fn("aat"),
            session_id=session_id,
            turn_id=turn.id,
            proposal_index=function_call_index,
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
        if call_id:
            call_ids_by_attempt_id[action_attempt.id] = call_id
        taint_by_attempt_id[action_attempt.id] = taint_payload
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
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "blocked",
                            "capability_id": capability_id,
                            "reason": evaluation.reason,
                        },
                    )
                )
            add_event(
                "evt.action.policy_decided",
                {
                    "action_attempt_id": action_attempt.id,
                    "decision": "deny",
                    "reason": evaluation.reason,
                    "taint": taint_payload,
                },
            )
            if is_retrieval_call:
                retrieval_errors.append(evaluation.reason)
            continue

        if evaluation.decision == "requires_approval":
            if evaluation.capability is None or evaluation.normalized_input is None:
                action_attempt.status = "rejected"
                action_attempt.policy_decision = "deny"
                action_attempt.policy_reason = "policy_invariant_violation"
                action_attempt.updated_at = now_fn()
                blocked_reasons.append(f"{capability_id}: policy_invariant_violation")
                if call_id:
                    function_call_outputs.append(
                        _response_function_call_output(
                            call_id=call_id,
                            payload={
                                "status": "blocked",
                                "capability_id": capability_id,
                                "reason": "policy_invariant_violation",
                            },
                        )
                    )
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

            preflight_error = preflight_capability_execution(
                capability=evaluation.capability,
                normalized_input=evaluation.normalized_input,
            )
            if preflight_error is not None:
                action_attempt.status = "failed"
                action_attempt.policy_decision = "deny"
                action_attempt.policy_reason = preflight_error
                action_attempt.execution_error = preflight_error
                action_attempt.updated_at = now_fn()
                blocked_reasons.append(f"{capability_id}: {preflight_error}")
                if call_id:
                    function_call_outputs.append(
                        _response_function_call_output(
                            call_id=call_id,
                            payload={
                                "status": "failed",
                                "capability_id": capability_id,
                                "error": preflight_error,
                            },
                        )
                    )
                add_event(
                    "evt.action.policy_decided",
                    {
                        "action_attempt_id": action_attempt.id,
                        "decision": "deny",
                        "reason": preflight_error,
                        "taint": taint_payload,
                    },
                )
                add_event(
                    "evt.action.execution.failed",
                    {
                        "action_attempt_id": action_attempt.id,
                        "error": preflight_error,
                        "approval_ref": None,
                    },
                )
                continue

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
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "approval_required",
                            "capability_id": capability_id,
                            "approval_ref": approval_request.id,
                            "expires_at": to_rfc3339(approval_request.expires_at),
                        },
                    )
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
            if is_retrieval_call:
                retrieval_errors.append(evaluation.reason)
            continue

        if evaluation.capability is None or evaluation.normalized_input is None:
            action_attempt.status = "rejected"
            action_attempt.policy_decision = "deny"
            action_attempt.policy_reason = "policy_invariant_violation"
            action_attempt.updated_at = now_fn()
            blocked_reasons.append(f"{capability_id}: policy_invariant_violation")
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "blocked",
                            "capability_id": capability_id,
                            "reason": "policy_invariant_violation",
                        },
                    )
                )
            add_event(
                "evt.action.policy_decided",
                {
                    "action_attempt_id": action_attempt.id,
                    "decision": "deny",
                    "reason": "policy_invariant_violation",
                    "taint": taint_payload,
                },
            )
            if is_retrieval_call:
                retrieval_errors.append("policy_invariant_violation")
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
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "failed",
                            "capability_id": capability_id,
                            "error": integrity_error,
                        },
                    )
                )
            add_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": integrity_error,
                },
            )
            if is_retrieval_call:
                retrieval_errors.append(integrity_error)
            continue

        preflight_error = preflight_capability_execution(
            capability=evaluation.capability,
            normalized_input=evaluation.normalized_input,
        )
        if preflight_error is not None:
            action_attempt.execution_output = None
            action_attempt.execution_error = preflight_error
            action_attempt.status = "failed"
            action_attempt.policy_reason = preflight_error
            action_attempt.updated_at = now_fn()
            blocked_reasons.append(f"{capability_id}: {preflight_error}")
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "failed",
                            "capability_id": capability_id,
                            "error": preflight_error,
                        },
                    )
                )
            add_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": preflight_error,
                },
            )
            if is_retrieval_call:
                retrieval_errors.append(preflight_error)
            continue

        if evaluation.capability.impact_level != "read":
            task = _enqueue_action_execution_task(
                db=db,
                action_attempt=action_attempt,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            add_event(
                "evt.action.execution.started",
                {
                    "action_attempt_id": action_attempt.id,
                    "capability_id": capability_id,
                    "task_id": task.id,
                },
            )
            queued_output = {"status": "queued", "task_id": task.id}
            inline_results.append({"capability_id": capability_id, "output": queued_output})
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "queued",
                            "capability_id": capability_id,
                            "task_id": task.id,
                        },
                    )
                )
            continue

        add_event(
            "evt.action.execution.started",
            {
                "action_attempt_id": action_attempt.id,
                "capability_id": capability_id,
            },
        )
        execution_result: ExecutionResult | GoogleCapabilityExecutionResult
        if is_google_capability_call and google_runtime is not None:
            _acquire_side_effect_execution_lock(
                db=db,
                impact_level=evaluation.capability.impact_level,
            )
            google_execution_result = google_runtime.execute_capability(
                db=db,
                capability_id=capability_id,
                normalized_input=evaluation.normalized_input,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            if (
                google_execution_result.status == "succeeded"
                and google_execution_result.output is not None
            ):
                execution_result = google_execution_result
            else:
                error_reason = (
                    google_execution_result.auth_failure.failure_class
                    if google_execution_result.auth_failure is not None
                    else (google_execution_result.error or "execution_output_missing")
                )
                if call_id:
                    function_call_outputs.append(
                        _response_function_call_output(
                            call_id=call_id,
                            payload={
                                "status": "failed",
                                "capability_id": capability_id,
                                "error": error_reason,
                            },
                        )
                    )
                action_attempt.execution_output = None
                action_attempt.execution_error = error_reason
                action_attempt.status = "failed"
                action_attempt.updated_at = now_fn()
                blocked_reason = f"{capability_id}: {error_reason}"
                if google_execution_result.auth_failure is not None:
                    blocked_reason = (
                        f"{blocked_reason} ({google_execution_result.auth_failure.recovery})"
                    )
                blocked_reasons.append(blocked_reason)
                if is_retrieval_call:
                    retrieval_errors.append(error_reason)
                add_event(
                    "evt.action.execution.failed",
                    {
                        "action_attempt_id": action_attempt.id,
                        "error": error_reason,
                    },
                )
                continue
        elif is_agency_capability_call and agency_runtime is not None:
            _acquire_side_effect_execution_lock(
                db=db,
                impact_level=evaluation.capability.impact_level,
            )
            execution_result = agency_runtime.execute_capability(
                db=db,
                capability_id=capability_id,
                normalized_input=evaluation.normalized_input,
                action_attempt=action_attempt,
                session_id=session_id,
                turn_id=turn.id,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        elif is_attachment_capability_call and attachment_runtime is not None:
            execution_result = attachment_runtime.execute_read(
                db=db,
                session_id=session_id,
                turn_id=turn.id,
                normalized_input=evaluation.normalized_input,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        elif is_attachment_capability_call:
            execution_result = ExecutionResult(
                status="failed",
                output=None,
                error="attachment_runtime_not_bound",
            )
        else:
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
            _append_reason_codes(
                interpreter_reason_codes_by_attempt_id,
                action_attempt_id=action_attempt.id,
                reason_codes=_tool_result_interpretation_reason_codes(execution_result.output),
            )
            if is_discord_capability_call:
                silent_response = True
            else:
                inline_results.append(
                    {
                        "capability_id": capability_id,
                        "output": execution_result.output,
                    }
                )
            if call_id:
                function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "succeeded",
                            "capability_id": capability_id,
                            "output": execution_result.output,
                        },
                    )
                )
            if is_retrieval_call:
                if is_attachment_capability_call:
                    read_outcome_raw = execution_result.output.get("read_outcome")
                    read_outcome = read_outcome_raw if isinstance(read_outcome_raw, dict) else {}
                    read_status = read_outcome.get("status")
                    if isinstance(read_status, str) and read_status != "ok":
                        retrieval_errors.append(read_status)
                    provenance_raw = execution_result.output.get("runtime_provenance")
                    provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
                    attachment_provenance_status = provenance.get("status")
                    evidence_raw = provenance.get("evidence")
                    if attachment_provenance_status == "tainted" and isinstance(evidence_raw, list):
                        evidence = [item for item in evidence_raw if isinstance(item, dict)]
                        result_runtime_provenance = RuntimeProvenance(
                            status="tainted",
                            evidence=tuple(evidence),
                        )
                remaining_citations = _MAX_CITED_SOURCES - len(retrieval_sources)
                if remaining_citations > 0:
                    candidates = _extract_search_source_candidates(
                        output_payload=execution_result.output,
                        now_fn=now_fn,
                    )
                    if candidates:
                        persisted_sources = _persist_retrieval_artifacts(
                            db=db,
                            session_id=session_id,
                            turn_id=turn.id,
                            action_attempt=action_attempt,
                            capability_id=capability_id,
                            candidates=candidates[:remaining_citations],
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        retrieval_sources.extend(persisted_sources)
                    else:
                        retrieval_errors.append("insufficient_evidence")
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
        if call_id:
            function_call_outputs.append(
                _response_function_call_output(
                    call_id=call_id,
                    payload={
                        "status": "failed",
                        "capability_id": capability_id,
                        "error": execution_result.error or "execution_output_missing",
                    },
                )
            )
        if is_retrieval_call:
            retrieval_errors.append(action_attempt.execution_error)
        add_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "error": action_attempt.execution_error,
            },
        )

    if len(retrieval_sources) > 1:
        for action_attempt in created_action_attempts:
            if (
                action_attempt.status == "succeeded"
                and action_attempt.capability_id in _GROUNDED_RETRIEVAL_CAPABILITIES
            ):
                _append_reason_codes(
                    interpreter_reason_codes_by_attempt_id,
                    action_attempt_id=action_attempt.id,
                    reason_codes=["multi_source"],
                )

    tool_result_interpreter_input = _build_tool_result_interpreter_input(
        session_id=session_id,
        turn_id=turn.id,
        action_attempts=created_action_attempts,
        reason_codes_by_attempt_id=interpreter_reason_codes_by_attempt_id,
        call_ids_by_attempt_id=call_ids_by_attempt_id,
        taint_by_attempt_id=taint_by_attempt_id,
        retrieval_sources=retrieval_sources,
    )
    tool_result_interpreter_output: dict[str, Any] | None = None
    if tool_result_interpreter_input is not None:
        reason_codes_by_call_id: dict[str, dict[str, Any]] = {}
        for action_attempt in created_action_attempts:
            output_call_id = call_ids_by_attempt_id.get(action_attempt.id)
            reason_codes = interpreter_reason_codes_by_attempt_id.get(action_attempt.id)
            if output_call_id is None or not reason_codes:
                continue
            reason_codes_by_call_id[output_call_id] = {
                "action_attempt_id": action_attempt.id,
                "capability_id": action_attempt.capability_id,
                "reason_codes": reason_codes,
            }
        _redact_function_outputs_requiring_interpretation(
            function_call_outputs=function_call_outputs,
            reason_codes_by_call_id=reason_codes_by_call_id,
        )

    if silent_response:
        final_assistant_message = ""
        assistant_sources = []
    else:
        action_attempt_summaries: list[dict[str, Any]] = []
        for action_attempt in created_action_attempts:
            summary: dict[str, Any] = {
                "action_attempt_id": action_attempt.id,
                "capability_id": action_attempt.capability_id,
                "status": action_attempt.status,
                "policy_decision": action_attempt.policy_decision,
                "approval_required": action_attempt.approval_required,
                "has_execution_output": action_attempt.execution_output is not None,
            }
            if action_attempt.policy_reason is not None:
                summary["policy_reason"] = action_attempt.policy_reason
            if action_attempt.execution_error is not None:
                summary["execution_error"] = action_attempt.execution_error
            action_attempt_summaries.append(summary)

        tool_summary: dict[str, Any] = {
            "kind": "audited_tool_results",
            "requires_model_final_answer": tool_result_interpreter_input is None,
            "action_attempts": action_attempt_summaries,
            "inline_result_count": len(inline_results),
            "pending_approvals": pending_approvals,
            "blocked_reasons": blocked_reasons,
            "retrieval": {
                "requested": retrieval_requested,
                "capability_ids": sorted(retrieval_capability_ids),
                "source_count": len(retrieval_sources),
                "sources": retrieval_sources,
                "errors": retrieval_errors,
            },
        }
        if tool_result_interpreter_input is not None:
            tool_summary["tool_result_interpreter"] = {
                "required": True,
                "reason_codes": tool_result_interpreter_input["reason_codes"],
                "input_refs": {
                    "action_attempt_ids": tool_result_interpreter_input["action_attempt_ids"],
                    "capability_ids": tool_result_interpreter_input["capability_ids"],
                    "audited_tool_output_refs": [
                        item["output_ref"]
                        for item in tool_result_interpreter_input["audited_tool_outputs"]
                    ],
                    "artifact_refs": tool_result_interpreter_input["artifact_refs"],
                    "citation_refs": tool_result_interpreter_input["citation_refs"],
                    "typed_tool_failure_count": len(
                        tool_result_interpreter_input["typed_tool_failures"]
                    ),
                    "omitted_output_refs": tool_result_interpreter_input["omitted_output_refs"],
                },
                "output": tool_result_interpreter_output,
            }
        final_assistant_message = json.dumps(
            jsonable_encoder(tool_summary),
            sort_keys=True,
            separators=(",", ":"),
        )
        assistant_sources = retrieval_sources if retrieval_requested else []
    return FunctionCallProcessingResult(
        assistant_message=final_assistant_message,
        function_call_outputs=function_call_outputs,
        action_attempts=created_action_attempts,
        assistant_sources=assistant_sources,
        silent_response=silent_response,
        runtime_provenance=result_runtime_provenance,
        tool_result_interpreter_input=tool_result_interpreter_input,
        tool_result_interpreter_output=tool_result_interpreter_output,
    )


def _mark_approval_expired(
    *,
    db: Session,
    approval: ApprovalRequestRecord,
    action_attempt: ActionAttemptRecord,
    now: datetime,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    if approval.status != "pending":
        msg = "expiry reconciliation requires a pending approval"
        raise RuntimeError(msg)
    if approval.action_attempt_id != action_attempt.id:
        msg = "approval/action attempt mismatch during expiry reconciliation"
        raise RuntimeError(msg)
    if (
        approval.session_id != action_attempt.session_id
        or approval.turn_id != action_attempt.turn_id
    ):
        msg = "approval/action attempt scope mismatch during expiry reconciliation"
        raise RuntimeError(msg)

    approval.status = "expired"
    approval.decision_reason = "approval_expired"
    approval.decided_at = now
    approval.updated_at = now

    action_attempt.status = "expired"
    action_attempt.policy_reason = "approval_expired"
    action_attempt.updated_at = now

    append_turn_event(
        db=db,
        session_id=approval.session_id,
        turn_id=approval.turn_id,
        sequence=next_turn_event_sequence(db=db, turn_id=approval.turn_id),
        event_type="evt.action.approval.expired",
        payload_data={
            "action_attempt_id": action_attempt.id,
            "approval_ref": approval.id,
            "reason": "approval_expired",
        },
        new_id_fn=new_id_fn,
        now_fn=now_fn,
    )


def reconcile_expired_approvals_for_session(
    *,
    db: Session,
    session_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> int:
    now = now_fn()
    approvals = db.scalars(
        select(ApprovalRequestRecord)
        .where(
            ApprovalRequestRecord.session_id == session_id,
            ApprovalRequestRecord.status == "pending",
            ApprovalRequestRecord.expires_at < now,
        )
        .order_by(
            ApprovalRequestRecord.expires_at.asc(),
            ApprovalRequestRecord.id.asc(),
        )
        .with_for_update()
    ).all()

    reconciled_count = 0
    for approval in approvals:
        action_attempt = db.scalar(
            select(ActionAttemptRecord)
            .where(ActionAttemptRecord.id == approval.action_attempt_id)
            .with_for_update()
            .limit(1)
        )
        if action_attempt is None:
            msg = "approval references missing action attempt"
            raise RuntimeError(msg)
        _mark_approval_expired(
            db=db,
            approval=approval,
            action_attempt=action_attempt,
            now=now,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        reconciled_count += 1

    if reconciled_count > 0:
        db.flush()
    return reconciled_count


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

    now = now_fn()
    if now > approval.expires_at:
        _mark_approval_expired(
            db=db,
            approval=approval,
            action_attempt=action_attempt,
            now=now,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
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
                preflight_error = preflight_capability_execution(
                    capability=capability,
                    normalized_input=normalized_input,
                )
                if preflight_error is not None:
                    action_attempt.status = "failed"
                    action_attempt.execution_error = preflight_error
                    action_attempt.policy_reason = preflight_error
                    action_attempt.updated_at = now_fn()
                    add_approval_event(
                        "evt.action.execution.failed",
                        {
                            "action_attempt_id": action_attempt.id,
                            "error": preflight_error,
                        },
                    )
                else:
                    action_attempt.status = "executing"
                    action_attempt.updated_at = now_fn()
                    task = _enqueue_action_execution_task(
                        db=db,
                        action_attempt=action_attempt,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    add_approval_event(
                        "evt.action.execution.started",
                        {
                            "action_attempt_id": action_attempt.id,
                            "capability_id": action_attempt.capability_id,
                            "task_id": task.id,
                        },
                    )

    db.flush()
    if action_attempt.status == "executing":
        assistant_message = "approval recorded. action execution queued."
    elif action_attempt.status == "failed":
        assistant_message = approval_execution_failure_message(
            action_attempt.execution_error or "execution_failed"
        )
    else:
        assistant_message = "approval recorded."
    return ApprovalDecisionResult(
        approval=approval,
        action_attempt=action_attempt,
        assistant_message=assistant_message,
    )


def process_action_execution_task(
    *,
    session_factory: sessionmaker[Session],
    action_attempt_id: str,
    google_runtime: GoogleConnectorRuntime | None,
    agency_runtime: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> bool:
    provider_call: tuple[str, dict[str, Any], str, set[str]] | None = None
    agency_call: tuple[str, dict[str, Any], dict[str, Any]] | None = None
    agency_result: tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    local_call: tuple[CapabilityDefinition, dict[str, Any]] | None = None
    execution_result: ExecutionResult | GoogleCapabilityExecutionResult | None = None

    with session_factory() as db:
        with db.begin():
            action_attempt = db.scalar(
                select(ActionAttemptRecord)
                .where(ActionAttemptRecord.id == action_attempt_id)
                .with_for_update()
                .limit(1)
            )
            if action_attempt is None:
                raise RuntimeError("action attempt not found")
            if action_attempt.status in {"succeeded", "failed", "rejected", "denied", "expired"}:
                return False
            if action_attempt.status != "executing":
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error="action_not_executable",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True
            if (
                isinstance(action_attempt.execution_output, dict)
                and action_attempt.execution_output.get("dispatch_state") == "provider_call_started"
            ):
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error="provider_result_unknown",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True

            capability = get_capability(action_attempt.capability_id)
            if capability is None:
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error="unknown_capability",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True
            integrity_error = _execution_integrity_error(
                action_attempt=action_attempt,
                capability=capability,
            )
            if integrity_error is not None:
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error=integrity_error,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True
            normalized_input, input_error = capability.validate_input(action_attempt.proposed_input)
            if input_error is not None or normalized_input is None:
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error="schema_invalid",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True
            preflight_error = preflight_capability_execution(
                capability=capability,
                normalized_input=normalized_input,
            )
            if preflight_error is not None:
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error=preflight_error,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True

            if action_attempt.capability_id in GOOGLE_CAPABILITY_IDS:
                if google_runtime is None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="google_runtime_not_bound",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                access_token, granted_scopes, access_failure = (
                    google_runtime.prepare_capability_access(
                        db=db,
                        capability_id=action_attempt.capability_id,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                )
                if access_failure is not None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error=(
                            access_failure.auth_failure.failure_class
                            if access_failure.auth_failure is not None
                            else (access_failure.error or "google_access_failed")
                        ),
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                if access_token is None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="token_expired",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                action_attempt.execution_output = {"dispatch_state": "provider_call_started"}
                action_attempt.updated_at = now_fn()
                provider_call = (
                    action_attempt.capability_id,
                    normalized_input,
                    access_token,
                    granted_scopes,
                )
            elif action_attempt.capability_id in AGENCY_CAPABILITY_IDS:
                if agency_runtime is None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="agency_runtime_not_bound",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                agency_context = {
                    "action_attempt_id": action_attempt.id,
                }
                if action_attempt.capability_id == "cap.agency.request_pr":
                    try:
                        agency_context = agency_runtime.prepare_request_pr(
                            db=db,
                            input_payload=normalized_input,
                        )
                    except Exception as exc:  # noqa: BLE001
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=str(exc) or "agency_prepare_failed",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                elif action_attempt.capability_id != "cap.agency.run":
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="unknown_agency_capability",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                action_attempt.execution_output = {"dispatch_state": "provider_call_started"}
                action_attempt.updated_at = now_fn()
                agency_call = (action_attempt.capability_id, normalized_input, agency_context)
            elif capability.impact_level == "external_send":
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error="egress_adapter_not_bound",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True
            else:
                local_call = (capability, normalized_input)

    if provider_call is not None:
        capability_id, normalized_input, access_token, granted_scopes = provider_call
        assert google_runtime is not None
        execution_result = google_runtime.execute_provider_capability(
            capability_id=capability_id,
            normalized_input=normalized_input,
            access_token=access_token,
            granted_scopes=granted_scopes,
        )
    elif agency_call is not None:
        capability_id, normalized_input, agency_context = agency_call
        assert agency_runtime is not None
        try:
            if capability_id == "cap.agency.run":
                result = agency_runtime.start_run(
                    input_payload=normalized_input,
                    action_attempt_id=agency_context["action_attempt_id"],
                )
            elif capability_id == "cap.agency.request_pr":
                result = agency_runtime.request_pr(prepared=agency_context)
            else:
                execution_result = ExecutionResult(
                    status="failed",
                    output=None,
                    error="unknown_agency_capability",
                )
                result = None
        except Exception as exc:  # noqa: BLE001
            execution_result = ExecutionResult(status="failed", output=None, error=str(exc))
        else:
            if result is not None:
                agency_result = (capability_id, normalized_input, agency_context, result)
                execution_result = ExecutionResult(status="succeeded", output={}, error=None)
    elif local_call is not None:
        capability, normalized_input = local_call
        execution_result = execute_capability(
            capability=capability,
            normalized_input=normalized_input,
        )
    else:
        return True
    if execution_result is None:
        execution_result = ExecutionResult(
            status="failed",
            output=None,
            error="execution_result_missing",
        )

    with session_factory() as db:
        with db.begin():
            action_attempt = db.scalar(
                select(ActionAttemptRecord)
                .where(ActionAttemptRecord.id == action_attempt_id)
                .with_for_update()
                .limit(1)
            )
            if action_attempt is None:
                raise RuntimeError("action attempt not found")
            if action_attempt.status in {"succeeded", "failed", "rejected", "denied", "expired"}:
                return False
            if agency_result is not None:
                capability_id, normalized_input, agency_context, result = agency_result
                assert agency_runtime is not None
                if capability_id == "cap.agency.run":
                    execution_result = ExecutionResult(
                        status="succeeded",
                        output=agency_runtime.record_run_started(
                            db=db,
                            started=result,
                            input_payload=normalized_input,
                            action_attempt=action_attempt,
                            session_id=action_attempt.session_id,
                            turn_id=action_attempt.turn_id,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        ),
                        error=None,
                    )
                elif capability_id == "cap.agency.request_pr":
                    execution_result = ExecutionResult(
                        status="succeeded",
                        output=agency_runtime.record_request_pr(
                            db=db,
                            prepared=agency_context,
                            result=result,
                            now_fn=now_fn,
                        ),
                        error=None,
                    )
            if execution_result.status == "succeeded" and execution_result.output is not None:
                action_attempt.status = "succeeded"
                action_attempt.execution_output = execution_result.output
                action_attempt.execution_error = None
                action_attempt.updated_at = now_fn()
                _append_action_execution_event(
                    db=db,
                    action_attempt=action_attempt,
                    event_type="evt.action.execution.succeeded",
                    payload_data={
                        "action_attempt_id": action_attempt.id,
                        "output": execution_result.output,
                    },
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
            else:
                error = execution_result.error or "execution_output_missing"
                if (
                    isinstance(execution_result, GoogleCapabilityExecutionResult)
                    and execution_result.auth_failure is not None
                ):
                    error = execution_result.auth_failure.failure_class
                    if google_runtime is not None:
                        google_runtime.record_capability_failure(
                            db=db,
                            execution_result=execution_result,
                            now_fn=now_fn,
                        )
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error=error,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
    return True
