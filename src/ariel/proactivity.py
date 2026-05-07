from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.capability_registry import (
    capability_id_for_response_tool_name,
    get_capability,
    response_tool_definitions,
)
from ariel.config import AppSettings
from ariel.executor import execute_capability, preflight_capability_execution
from ariel.memory import AIJudgmentFailure, MEMORY_CURATION_PROMPT_VERSION, build_memory_context
from ariel.persistence import (
    ApprovalRequestRecord,
    AutonomyScopeRecord,
    BackgroundTaskRecord,
    CaptureRecord,
    GoogleConnectorRecord,
    AIJudgmentRecord,
    JobRecord,
    MemoryAssertionRecord,
    MemoryEntityRecord,
    NotificationRecord,
    ProactiveActionExecutionRecord,
    ProactiveActionPlanRecord,
    ProactiveCaseEventRecord,
    ProactiveCaseRecord,
    ProactiveContextSnapshotRecord,
    ProactiveDecisionRecord,
    ProactiveFeedbackRecord,
    ProactiveLearningRecord,
    ProactiveObservationRecord,
    ProactivePolicyValidationRecord,
    ProactiveTurnRecord,
    WorkspaceItemEventRecord,
    WorkspaceItemRecord,
    to_rfc3339,
)
from ariel.redaction import safe_failure_reason


PROACTIVE_POLICY_VERSION = "proactive-ai-deliberation-v1"
PROACTIVE_AMBIENT_INTERPRETATION_PROMPT_VERSION = "proactive-ambient-interpretation-v1"
PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION = "proactive-feedback-learning-v1"
_FOLLOW_UP_INTERVALS = {
    "PT5M": timedelta(minutes=5),
    "PT10M": timedelta(minutes=10),
    "PT15M": timedelta(minutes=15),
    "PT1H": timedelta(hours=1),
}
_IMPACT_ORDER = {"low": 0, "medium": 1, "high": 2}
_FEEDBACK_LEARNING_RECORD_TYPES = {
    "instruction",
    "example",
    "calibration",
    "preference",
    "source_preference",
    "prompt_instruction",
    "autonomy_request",
}


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_text(value: Any, *, max_chars: int = 700) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())[:max_chars]
    return normalized or None


def _memory_key(value: str) -> str:
    pieces: list[str] = []
    last_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            pieces.append(char)
            last_was_separator = False
        elif not last_was_separator:
            pieces.append("_")
            last_was_separator = True
    return "".join(pieces).strip("_") or "general"


def _add_task(
    db: Session,
    *,
    task_type: str,
    payload: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> None:
    db.add(
        BackgroundTaskRecord(
            id=new_id_fn("tsk"),
            task_type=task_type,
            payload=payload,
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            error=None,
            claimed_by=None,
            run_after=run_after or now,
            last_heartbeat=None,
            created_at=now,
            updated_at=now,
        )
    )


def _add_case_event(
    db: Session,
    *,
    case_id: str,
    event_type: str,
    payload: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    db.add(
        ProactiveCaseEventRecord(
            id=new_id_fn("pce"),
            case_id=case_id,
            event_type=event_type,
            payload=payload,
            created_at=now,
        )
    )


def upsert_proactive_observation(
    db: Session,
    *,
    dedupe_key: str,
    case_key: str,
    source_type: str,
    source_id: str,
    observation_type: str,
    subject: str,
    summary: str,
    payload: dict[str, Any],
    evidence: dict[str, Any],
    taint: dict[str, Any],
    trust_boundary: str,
    observed_at: datetime,
    workspace_item_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> str | None:
    observation = db.scalar(
        select(ProactiveObservationRecord)
        .where(ProactiveObservationRecord.dedupe_key == dedupe_key)
        .with_for_update()
        .limit(1)
    )
    if observation is not None:
        return None

    observation = ProactiveObservationRecord(
        id=new_id_fn("obs"),
        workspace_item_id=workspace_item_id,
        source_type=source_type,
        source_id=source_id,
        dedupe_key=dedupe_key,
        observation_type=observation_type,
        subject=subject,
        summary=summary,
        payload=payload,
        evidence=evidence,
        taint=taint,
        trust_boundary=trust_boundary,
        status="new",
        observed_at=observed_at,
        created_at=now,
        updated_at=now,
    )
    db.add(observation)
    db.flush()

    case = db.scalar(
        select(ProactiveCaseRecord)
        .where(ProactiveCaseRecord.case_key == case_key)
        .with_for_update()
        .limit(1)
    )
    if case is None:
        case = ProactiveCaseRecord(
            id=new_id_fn("pca"),
            case_key=case_key,
            status="open",
            title=subject,
            summary=summary,
            latest_observation_id=observation.id,
            last_decision_id=None,
            next_recheck_after=None,
            created_at=now,
            updated_at=now,
        )
        db.add(case)
        db.flush()
        _add_case_event(
            db,
            case_id=case.id,
            event_type="opened",
            payload={"observation_id": observation.id},
            now=now,
            new_id_fn=new_id_fn,
        )
    else:
        case.status = "open" if case.status not in {"resolved", "acknowledged"} else case.status
        case.title = subject
        case.summary = summary
        case.latest_observation_id = observation.id
        case.updated_at = now
        _add_case_event(
            db,
            case_id=case.id,
            event_type="updated",
            payload={"observation_id": observation.id},
            now=now,
            new_id_fn=new_id_fn,
        )

    observation.status = "linked"
    observation.updated_at = now
    _add_task(
        db,
        task_type="proactive_deliberation_due",
        payload={"case_id": case.id},
        now=now,
        new_id_fn=new_id_fn,
    )
    return case.id


def _ambient_interpretation_candidates(
    db: Session,
    *,
    now: datetime,
    workspace_item_event_id: str | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    jobs = db.scalars(
        select(JobRecord).order_by(JobRecord.updated_at.desc(), JobRecord.id.asc()).limit(24)
    ).all()
    for job in jobs:
        candidates.append(
            {
                "candidate_id": f"job:{job.id}:{job.status}:{to_rfc3339(job.updated_at)}",
                "source_type": "job",
                "source_id": job.id,
                "workspace_item_id": None,
                "observed_at": to_rfc3339(job.updated_at),
                "trust_boundary": "trusted_internal",
                "taint": {"provenance_status": "trusted_internal"},
                "raw_event": {
                    "id": job.id,
                    "source": job.source,
                    "external_job_id": job.external_job_id,
                    "status": job.status,
                    "title": job.title,
                    "summary": job.summary,
                    "updated_at": to_rfc3339(job.updated_at),
                },
            }
        )

    approvals = db.scalars(
        select(ApprovalRequestRecord)
        .where(ApprovalRequestRecord.status == "pending")
        .order_by(ApprovalRequestRecord.expires_at.asc(), ApprovalRequestRecord.id.asc())
        .limit(24)
    ).all()
    for approval in approvals:
        candidates.append(
            {
                "candidate_id": f"approval:{approval.id}:{to_rfc3339(approval.expires_at)}",
                "source_type": "approval_request",
                "source_id": approval.id,
                "workspace_item_id": None,
                "observed_at": to_rfc3339(now),
                "trust_boundary": "trusted_internal",
                "taint": {"provenance_status": "trusted_internal"},
                "raw_event": {
                    "id": approval.id,
                    "action_attempt_id": approval.action_attempt_id,
                    "status": approval.status,
                    "expires_at": to_rfc3339(approval.expires_at),
                },
            }
        )

    assertions = db.scalars(
        select(MemoryAssertionRecord)
        .where(MemoryAssertionRecord.lifecycle_state == "active")
        .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
        .limit(24)
    ).all()
    for assertion in assertions:
        candidates.append(
            {
                "candidate_id": f"memory:{assertion.id}:{to_rfc3339(assertion.updated_at)}",
                "source_type": "memory_assertion",
                "source_id": assertion.id,
                "workspace_item_id": None,
                "observed_at": to_rfc3339(assertion.updated_at),
                "trust_boundary": "reviewed_memory",
                "taint": {"provenance_status": "reviewed_memory"},
                "raw_event": {
                    "id": assertion.id,
                    "assertion_type": assertion.assertion_type,
                    "subject_key": assertion.subject_key,
                    "predicate": assertion.predicate,
                    "object_value": assertion.object_value,
                    "confidence": assertion.confidence,
                    "updated_at": to_rfc3339(assertion.updated_at),
                },
            }
        )

    connectors = db.scalars(
        select(GoogleConnectorRecord)
        .order_by(GoogleConnectorRecord.updated_at.desc(), GoogleConnectorRecord.id.asc())
        .limit(24)
    ).all()
    for connector in connectors:
        candidates.append(
            {
                "candidate_id": (
                    f"google-connector:{connector.id}:{connector.status}:"
                    f"{to_rfc3339(connector.updated_at)}"
                ),
                "source_type": "google_connector",
                "source_id": connector.id,
                "workspace_item_id": None,
                "observed_at": to_rfc3339(connector.updated_at),
                "trust_boundary": "trusted_internal",
                "taint": {"provenance_status": "trusted_internal"},
                "raw_event": {
                    "id": connector.id,
                    "status": connector.status,
                    "last_error_code": connector.last_error_code,
                    "updated_at": to_rfc3339(connector.updated_at),
                },
            }
        )

    captures = db.scalars(
        select(CaptureRecord)
        .order_by(CaptureRecord.created_at.desc(), CaptureRecord.id.asc())
        .limit(24)
    ).all()
    for capture in captures:
        candidates.append(
            {
                "candidate_id": f"capture:{capture.id}:{capture.terminal_state}",
                "source_type": "capture",
                "source_id": capture.id,
                "workspace_item_id": None,
                "observed_at": to_rfc3339(capture.created_at),
                "trust_boundary": "trusted_internal",
                "taint": {"provenance_status": "trusted_internal"},
                "raw_event": {
                    "id": capture.id,
                    "capture_kind": capture.capture_kind,
                    "terminal_state": capture.terminal_state,
                    "turn_id": capture.turn_id,
                    "normalized_turn_input": capture.normalized_turn_input,
                    "created_at": to_rfc3339(capture.created_at),
                },
            }
        )

    workspace_event_query = select(WorkspaceItemEventRecord)
    if workspace_item_event_id is not None:
        workspace_event_query = workspace_event_query.where(
            WorkspaceItemEventRecord.id == workspace_item_event_id
        )
    workspace_events = db.scalars(
        workspace_event_query.order_by(
            WorkspaceItemEventRecord.created_at.desc(),
            WorkspaceItemEventRecord.id.asc(),
        ).limit(48)
    ).all()
    for event in workspace_events:
        item = db.get(WorkspaceItemRecord, event.workspace_item_id)
        if item is None:
            continue
        candidates.append(
            {
                "candidate_id": f"workspace-event:{event.dedupe_key}",
                "source_type": "workspace_item",
                "source_id": item.id,
                "workspace_item_id": item.id,
                "observed_at": to_rfc3339(event.created_at),
                "trust_boundary": "provider",
                "taint": {"provenance_status": "tainted", "source": item.provider},
                "raw_event": {
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "provider_event_id": event.provider_event_id,
                    "payload": event.payload,
                    "workspace_item": {
                        "id": item.id,
                        "provider": item.provider,
                        "item_type": item.item_type,
                        "external_id": item.external_id,
                        "title": item.title,
                        "summary": item.summary,
                        "source_uri": item.source_uri,
                        "status": item.status,
                        "metadata": item.item_metadata,
                    },
                },
            }
        )

    return candidates


def process_ambient_interpretation_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    model_adapter: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    workspace_item_event_id = _payload_text(task_payload, "workspace_item_event_id")
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            candidates = _ambient_interpretation_candidates(
                db,
                now=now,
                workspace_item_event_id=workspace_item_event_id,
            )

    source_id = workspace_item_event_id or str(task_payload.get("origin") or "ambient")
    candidate_refs = [
        {
            "candidate_id": candidate["candidate_id"],
            "source_type": candidate["source_type"],
            "source_id": candidate["source_id"],
        }
        for candidate in candidates
    ]
    if not candidates:
        now = now_fn()
        with session_factory() as db:
            with db.begin():
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="ambient_interpretation",
                        source_type="ambient_batch",
                        source_id=source_id,
                        status="succeeded",
                        model=None,
                        prompt_version=PROACTIVE_AMBIENT_INTERPRETATION_PROMPT_VERSION,
                        provider_response_id=None,
                        input_summary="ambient source interpretation",
                        input_refs={
                            "workspace_item_event_id": workspace_item_event_id,
                            "candidate_count": 0,
                            "candidate_refs": [],
                            "task_payload": task_payload,
                        },
                        selected=[],
                        omitted=[],
                        output={"observations": [], "omitted": []},
                        rationale="no ambient interpretation candidates",
                        uncertainty=None,
                        confidence=None,
                        parse_status="not_required_no_candidates",
                        validation_status="not_validated",
                        failure_code=None,
                        failure_reason=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return

    model_input = [
        {
            "role": "system",
            "content": (
                "You are Ariel's ambient event interpreter. Decide whether durable source "
                "records and provider events are worth proactive observations and what they mean. "
                "Return only strict JSON with keys: observations, omitted, rationale. "
                "Each observation must include candidate_id, observation_key, case_key, "
                "observation_type, subject, summary, payload, evidence, rationale. "
                "Return an empty observations array when nothing is worth observing."
            ),
        },
        {
            "role": "system",
            "content": json.dumps(
                {"candidates": candidates}, sort_keys=True, separators=(",", ":")
            ),
        },
    ]

    def record_failed_judgment(
        *,
        response: dict[str, Any] | None,
        parse_status: str,
        validation_status: str,
        failure_code: str,
        failure_reason: str,
    ) -> None:
        now = now_fn()
        response_payload = response or {}
        with session_factory() as db:
            with db.begin():
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="ambient_interpretation",
                        source_type="ambient_batch",
                        source_id=source_id,
                        status="failed",
                        model=_provider_value(response_payload, "model", settings.model_name),
                        prompt_version=PROACTIVE_AMBIENT_INTERPRETATION_PROMPT_VERSION,
                        provider_response_id=_provider_response_id(response_payload),
                        input_summary="ambient source interpretation",
                        input_refs={
                            "workspace_item_event_id": workspace_item_event_id,
                            "candidate_count": len(candidates),
                            "candidate_refs": candidate_refs,
                        },
                        selected=[],
                        omitted=[],
                        output={
                            "response_output": response_payload.get("output")
                            if isinstance(response_payload, dict)
                            else None
                        },
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status=parse_status,
                        validation_status=validation_status,
                        failure_code=failure_code,
                        failure_reason=failure_reason,
                        created_at=now,
                        updated_at=now,
                    )
                )

    try:
        response = _call_direct_json_model(
            model_input=model_input,
            settings=settings,
            model_adapter=model_adapter,
            origin="ambient_interpretation",
        )
    except Exception as exc:
        reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
        record_failed_judgment(
            response=None,
            parse_status="missing_output",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_REQUIRED",
            failure_reason=reason,
        )
        raise RuntimeError(reason) from exc

    try:
        raw_result = _parse_model_json(response)
    except json.JSONDecodeError as exc:
        reason = safe_failure_reason(str(exc), fallback="ambient interpreter returned invalid JSON")
        record_failed_judgment(
            response=response,
            parse_status="invalid_json",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_INVALID_JSON",
            failure_reason=reason,
        )
        raise RuntimeError(reason) from exc
    except RuntimeError as exc:
        reason = safe_failure_reason(str(exc), fallback="ambient interpreter output missing")
        record_failed_judgment(
            response=response,
            parse_status="missing_output",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_SCHEMA",
            failure_reason=reason,
        )
        raise

    observations = raw_result.get("observations")
    if not isinstance(observations, list):
        reason = "ambient interpreter response missing observations"
        record_failed_judgment(
            response=response,
            parse_status="schema_invalid",
            validation_status="invalid",
            failure_code="E_AI_JUDGMENT_SCHEMA",
            failure_reason=reason,
        )
        raise RuntimeError(reason)
    candidate_by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    validated_observations: list[dict[str, Any]] = []
    for raw_observation in observations:
        if not isinstance(raw_observation, dict):
            reason = "ambient interpreter observation must be an object"
            record_failed_judgment(
                response=response,
                parse_status="schema_invalid",
                validation_status="invalid",
                failure_code="E_AI_JUDGMENT_SCHEMA",
                failure_reason=reason,
            )
            raise RuntimeError(reason)
        candidate_id = _payload_text(raw_observation, "candidate_id")
        observation_key = _payload_text(raw_observation, "observation_key")
        case_key = _payload_text(raw_observation, "case_key")
        observation_type = _payload_text(raw_observation, "observation_type")
        subject = _payload_text(raw_observation, "subject")
        summary = _payload_text(raw_observation, "summary")
        rationale = _payload_text(raw_observation, "rationale")
        candidate = candidate_by_id.get(candidate_id or "")
        if (
            candidate is None
            or observation_key is None
            or case_key is None
            or observation_type is None
            or subject is None
            or summary is None
            or rationale is None
        ):
            reason = "ambient interpreter observation failed schema validation"
            record_failed_judgment(
                response=response,
                parse_status="schema_invalid",
                validation_status="invalid",
                failure_code="E_AI_JUDGMENT_SCHEMA",
                failure_reason=reason,
            )
            raise RuntimeError(reason)
        payload = raw_observation.get("payload")
        evidence = raw_observation.get("evidence")
        validated_observations.append(
            {
                "candidate": candidate,
                "candidate_id": candidate_id,
                "observation_key": observation_key,
                "case_key": case_key,
                "observation_type": observation_type,
                "subject": subject,
                "summary": summary,
                "payload": payload if isinstance(payload, dict) else {},
                "evidence": evidence if isinstance(evidence, dict) else {},
                "rationale": rationale,
            }
        )

    with session_factory() as db:
        with db.begin():
            now = now_fn()
            raw_omitted = raw_result.get("omitted")
            omitted = (
                [item for item in raw_omitted if isinstance(item, dict)]
                if isinstance(raw_omitted, list)
                else []
            )
            db.add(
                AIJudgmentRecord(
                    id=new_id_fn("ajg"),
                    judgment_type="ambient_interpretation",
                    source_type="ambient_batch",
                    source_id=source_id,
                    status="succeeded",
                    model=_provider_value(response, "model", settings.model_name),
                    prompt_version=PROACTIVE_AMBIENT_INTERPRETATION_PROMPT_VERSION,
                    provider_response_id=_provider_response_id(response),
                    input_summary="ambient source interpretation",
                    input_refs={
                        "workspace_item_event_id": workspace_item_event_id,
                        "candidate_count": len(candidates),
                        "candidate_refs": candidate_refs,
                    },
                    selected=[
                        {
                            "candidate_id": observation["candidate_id"],
                            "observation_key": observation["observation_key"],
                            "case_key": observation["case_key"],
                            "rationale": observation["rationale"],
                        }
                        for observation in validated_observations
                    ],
                    omitted=omitted,
                    output=raw_result,
                    rationale=raw_result.get("rationale")
                    if isinstance(raw_result.get("rationale"), str)
                    else None,
                    uncertainty=None,
                    confidence=None,
                    parse_status="parsed",
                    validation_status="valid",
                    failure_code=None,
                    failure_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            for observation in validated_observations:
                candidate = observation["candidate"]
                upsert_proactive_observation(
                    db,
                    dedupe_key=(
                        f"ai-ambient:{observation['candidate_id']}:{observation['observation_key']}"
                    ),
                    case_key=str(observation["case_key"]),
                    source_type=str(candidate["source_type"]),
                    source_id=str(candidate["source_id"]),
                    observation_type=str(observation["observation_type"]),
                    subject=str(observation["subject"]),
                    summary=str(observation["summary"]),
                    payload=observation["payload"],
                    evidence={
                        **observation["evidence"],
                        "candidate_id": observation["candidate_id"],
                        "ambient_interpretation": {
                            "provider": _provider_value(response, "provider", "unknown"),
                            "model": _provider_value(response, "model", "unknown"),
                            "provider_response_id": _provider_response_id(response),
                            "prompt_version": PROACTIVE_AMBIENT_INTERPRETATION_PROMPT_VERSION,
                            "parse_status": "parsed",
                            "validation_status": "valid",
                            "rationale": observation["rationale"],
                            "omitted": omitted,
                        },
                        "raw_candidate": candidate,
                    },
                    taint=dict(candidate["taint"]),
                    trust_boundary=str(candidate["trust_boundary"]),
                    observed_at=datetime.fromisoformat(str(candidate["observed_at"])),
                    workspace_item_id=(
                        str(candidate["workspace_item_id"])
                        if candidate.get("workspace_item_id") is not None
                        else None
                    ),
                    now=now,
                    new_id_fn=new_id_fn,
                )


def process_proactive_deliberation_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    model_adapter: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    case_id = _payload_text(task_payload, "case_id")
    if case_id is None:
        raise RuntimeError("proactive_deliberation_due task missing case_id")

    memory_failure: RuntimeError | None = None
    memory_failure_retryable = False
    with session_factory() as db:
        with db.begin():
            case = db.scalar(
                select(ProactiveCaseRecord)
                .where(ProactiveCaseRecord.id == case_id)
                .with_for_update()
                .limit(1)
            )
            if case is None:
                raise RuntimeError("proactive case not found")
            if case.status in {"resolved", "acknowledged"}:
                return

            observation = db.get(ProactiveObservationRecord, case.latest_observation_id)
            if observation is None:
                raise RuntimeError("latest proactive observation not found")

            now = now_fn()
            try:
                memory_context, memory_event = build_memory_context(
                    db,
                    user_message=f"{case.title}\n{case.summary}",
                    max_recalled_assertions=settings.max_recalled_assertions,
                    settings=settings,
                )
            except AIJudgmentFailure as exc:
                safe_reason = safe_failure_reason(
                    exc.safe_reason,
                    fallback=f"unexpected {exc.__class__.__name__}",
                )
                candidate_memory_ids = [
                    memory_id
                    for memory_id in db.scalars(
                        select(MemoryAssertionRecord.id)
                        .where(MemoryAssertionRecord.lifecycle_state == "active")
                        .order_by(MemoryAssertionRecord.updated_at.desc())
                        .limit(max(50, int(settings.max_recalled_assertions) * 8))
                    ).all()
                    if isinstance(memory_id, str)
                ]
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_curation",
                        source_type="proactive_case",
                        source_id=case.id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version=MEMORY_CURATION_PROMPT_VERSION,
                        provider_response_id=exc.provider_response_id,
                        input_summary="memory curation for proactive case",
                        input_refs={
                            "case_id": case.id,
                            "latest_observation_id": observation.id,
                            "candidate_memory_ids": candidate_memory_ids,
                        },
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status=exc.parse_status,
                        validation_status=exc.validation_status,
                        failure_code=exc.code,
                        failure_reason=safe_reason,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="proactive_deliberation",
                        source_type="proactive_case",
                        source_id=case.id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version=PROACTIVE_POLICY_VERSION,
                        provider_response_id=None,
                        input_summary="proactive case deliberation",
                        input_refs={
                            "case_id": case.id,
                            "latest_observation_id": observation.id,
                            "dependency": "memory_curation",
                        },
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code=exc.code,
                        failure_reason=safe_reason,
                        created_at=now,
                        updated_at=now,
                    )
                )
                case.status = "failed"
                case.updated_at = now
                _add_case_event(
                    db,
                    case_id=case.id,
                    event_type="failed",
                    payload={
                        "failure_type": "memory_curation",
                        "failure_code": exc.code,
                        "failure_reason": safe_reason,
                        "parse_status": exc.parse_status,
                        "validation_status": exc.validation_status,
                        "retryable": exc.retryable,
                    },
                    now=now,
                    new_id_fn=new_id_fn,
                )
                memory_failure = RuntimeError(safe_reason)
                memory_failure_retryable = exc.retryable
            else:
                learning_records = db.scalars(
                    select(ProactiveLearningRecord)
                    .where(ProactiveLearningRecord.status == "active")
                    .order_by(
                        ProactiveLearningRecord.updated_at.desc(),
                        ProactiveLearningRecord.id.asc(),
                    )
                    .limit(20)
                ).all()
                context = {
                    "case": {
                        "id": case.id,
                        "status": case.status,
                        "title": case.title,
                        "summary": case.summary,
                    },
                    "latest_observation": {
                        "id": observation.id,
                        "source_type": observation.source_type,
                        "source_id": observation.source_id,
                        "observation_type": observation.observation_type,
                        "subject": observation.subject,
                        "summary": observation.summary,
                        "payload": observation.payload,
                        "evidence": observation.evidence,
                        "trust_boundary": observation.trust_boundary,
                        "observed_at": to_rfc3339(observation.observed_at),
                    },
                    "memory_context": memory_context,
                    "memory_recall": memory_event,
                    "learning_records": [
                        {
                            "id": record.id,
                            "record_type": record.record_type,
                            "content": record.content,
                        }
                        for record in learning_records
                    ],
                }
                model_input = [
                    {
                        "role": "system",
                        "content": (
                            "You are Ariel's proactive deliberation engine. Decide whether to "
                            "ignore, remember, wait, observe_more, speak_now, ask_user, act_now, "
                            "or speak_and_act. Return only strict JSON with keys: decision, "
                            "confidence, urgency, user_visible_message, rationale, evidence_refs, "
                            "tool_refs, actions, follow_up."
                        ),
                    },
                    {
                        "role": "system",
                        "content": json.dumps(context, sort_keys=True, separators=(",", ":")),
                    },
                ]
                snapshot = ProactiveContextSnapshotRecord(
                    id=new_id_fn("pcs"),
                    case_id=case.id,
                    snapshot_key=f"case:{case.id}:context:{now.timestamp()}",
                    context=context,
                    model_input=model_input,
                    omitted_context={},
                    taint={"latest_observation": observation.taint},
                    created_at=now,
                )
                db.add(snapshot)
                db.flush()
                snapshot_id = snapshot.id
                _add_case_event(
                    db,
                    case_id=case.id,
                    event_type="context_built",
                    payload={"context_snapshot_id": snapshot.id},
                    now=now,
                    new_id_fn=new_id_fn,
                )

    if memory_failure is not None:
        if memory_failure_retryable:
            raise memory_failure
        return

    try:
        response = _call_deliberation_model(
            model_input=model_input,
            settings=settings,
            model_adapter=model_adapter,
        )
    except Exception as exc:
        reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
        with session_factory() as db:
            with db.begin():
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="proactive_deliberation",
                        source_type="proactive_case",
                        source_id=case_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version=PROACTIVE_POLICY_VERSION,
                        provider_response_id=None,
                        input_summary="proactive case deliberation",
                        input_refs={
                            "case_id": case_id,
                            "context_snapshot_id": snapshot_id,
                        },
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code="E_AI_JUDGMENT_REQUIRED",
                        failure_reason=reason,
                        created_at=now_fn(),
                        updated_at=now_fn(),
                    )
                )
        raise RuntimeError(reason) from exc
    try:
        raw_decision = _parse_model_json(response)
    except (json.JSONDecodeError, RuntimeError) as exc:
        with session_factory() as db:
            with db.begin():
                case = db.scalar(
                    select(ProactiveCaseRecord)
                    .where(ProactiveCaseRecord.id == case_id)
                    .with_for_update()
                    .limit(1)
                )
                if case is None:
                    raise RuntimeError(
                        "proactive case not found after invalid deliberation"
                    ) from exc
                stored_snapshot = db.get(ProactiveContextSnapshotRecord, snapshot_id)
                if stored_snapshot is None:
                    raise RuntimeError(
                        "proactive context snapshot not found after invalid deliberation"
                    ) from exc
                _update_snapshot_tool_context(stored_snapshot, response)
                reason = safe_proactive_error(exc)
                parse_status = (
                    "invalid_json" if isinstance(exc, json.JSONDecodeError) else "missing_output"
                )
                _record_invalid_decision(
                    db=db,
                    case=case,
                    snapshot=stored_snapshot,
                    response=response,
                    reason=reason,
                    parse_status=parse_status,
                    raw_model_output={
                        "parse_error": reason,
                        "response_output": response.get("output"),
                    },
                    now=now_fn(),
                    new_id_fn=new_id_fn,
                )
        return

    with session_factory() as db:
        with db.begin():
            case = db.scalar(
                select(ProactiveCaseRecord)
                .where(ProactiveCaseRecord.id == case_id)
                .with_for_update()
                .limit(1)
            )
            if case is None:
                raise RuntimeError("proactive case not found after deliberation")
            stored_snapshot = db.get(ProactiveContextSnapshotRecord, snapshot_id)
            if stored_snapshot is None:
                raise RuntimeError("proactive context snapshot not found after deliberation")
            _update_snapshot_tool_context(stored_snapshot, response)
            now = now_fn()
            decision_type = str(raw_decision.get("decision") or "")
            confidence = raw_decision.get("confidence")
            urgency = str(raw_decision.get("urgency") or "normal")
            evidence_refs_raw = raw_decision.get("evidence_refs")
            tool_refs_raw = raw_decision.get("tool_refs")
            actions_raw = raw_decision.get("actions")
            follow_up = raw_decision.get("follow_up")
            message = raw_decision.get("user_visible_message")
            rationale = raw_decision.get("rationale")
            remember_payload = _remember_payload(raw_decision)

            evidence_refs = (
                [item for item in evidence_refs_raw if isinstance(item, str)]
                if isinstance(evidence_refs_raw, list)
                else []
            )
            tool_refs = (
                [item for item in tool_refs_raw if isinstance(item, str)]
                if isinstance(tool_refs_raw, list)
                else []
            )
            actions = (
                [item for item in actions_raw if isinstance(item, dict)]
                if isinstance(actions_raw, list)
                else []
            )
            valid = (
                decision_type
                in {
                    "ignore",
                    "remember",
                    "wait",
                    "observe_more",
                    "speak_now",
                    "ask_user",
                    "act_now",
                    "speak_and_act",
                }
                and isinstance(confidence, (int, float))
                and 0.0 <= float(confidence) <= 1.0
                and urgency in {"critical", "high", "normal", "low"}
                and isinstance(rationale, str)
            )
            if decision_type in {"speak_now", "ask_user", "act_now", "speak_and_act"}:
                valid = valid and bool(evidence_refs)
            if decision_type in {"speak_now", "ask_user", "speak_and_act"}:
                valid = valid and isinstance(message, str) and bool(message.strip())
            if decision_type in {"act_now", "speak_and_act"}:
                valid = valid and bool(actions)
            if decision_type in {"wait", "observe_more"}:
                valid = valid and bool(evidence_refs) and _valid_follow_up(follow_up)
            if decision_type == "remember":
                valid = valid and bool(evidence_refs) and _valid_remember_payload(remember_payload)

            decision = ProactiveDecisionRecord(
                id=new_id_fn("pdc"),
                case_id=case.id,
                context_snapshot_id=stored_snapshot.id,
                provider=str(response.get("provider") or "unknown"),
                model=str(response.get("model") or "unknown"),
                provider_response_id=_provider_response_id(response),
                decision_type=decision_type if valid else "ignore",
                status="proposed" if valid else "invalid",
                confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
                urgency=urgency if urgency in {"critical", "high", "normal", "low"} else "normal",
                user_visible_message=message.strip() if isinstance(message, str) else None,
                rationale=rationale.strip() if isinstance(rationale, str) else "invalid decision",
                evidence_refs=evidence_refs,
                tool_refs=tool_refs,
                actions=actions,
                follow_up=follow_up if isinstance(follow_up, dict) else None,
                raw_model_output={
                    **raw_decision,
                    "memory": remember_payload,
                }
                if decision_type == "remember"
                else raw_decision,
                created_at=now,
            )
            db.add(decision)
            db.flush()
            case.last_decision_id = decision.id
            db.add(
                AIJudgmentRecord(
                    id=new_id_fn("ajg"),
                    judgment_type="proactive_deliberation",
                    source_type="proactive_case",
                    source_id=case.id,
                    status="succeeded" if valid else "failed",
                    model=_provider_value(response, "model", "unknown"),
                    prompt_version=PROACTIVE_POLICY_VERSION,
                    provider_response_id=_provider_response_id(response),
                    input_summary="proactive case deliberation",
                    input_refs={
                        "case_id": case.id,
                        "context_snapshot_id": stored_snapshot.id,
                        "latest_observation_id": case.latest_observation_id,
                    },
                    selected=[
                        {
                            "decision_id": decision.id,
                            "decision_type": decision.decision_type,
                            "evidence_refs": evidence_refs,
                            "tool_refs": tool_refs,
                            "action_count": len(actions),
                        }
                    ]
                    if valid
                    else [],
                    omitted=[],
                    output=decision.raw_model_output,
                    rationale=decision.rationale,
                    uncertainty=None,
                    confidence=decision.confidence,
                    parse_status="parsed",
                    validation_status="valid" if valid else "invalid",
                    failure_code=None if valid else "E_AI_JUDGMENT_SCHEMA",
                    failure_reason=None if valid else "model decision failed schema validation",
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_case_event(
                db,
                case_id=case.id,
                event_type="decided",
                payload={"decision_id": decision.id, "decision_type": decision.decision_type},
                now=now,
                new_id_fn=new_id_fn,
            )

            if not valid:
                db.add(
                    ProactivePolicyValidationRecord(
                        id=new_id_fn("ppv"),
                        case_id=case.id,
                        decision_id=decision.id,
                        result="invalid_decision",
                        policy_version=PROACTIVE_POLICY_VERSION,
                        action_plan_hash=_json_hash({"actions": actions}) if actions else None,
                        constraints={},
                        denial_reason="model decision failed schema validation",
                        created_at=now,
                    )
                )
                case.status = "failed"
                case.updated_at = now
                _add_case_event(
                    db,
                    case_id=case.id,
                    event_type="failed",
                    payload={"decision_id": decision.id, "reason": "invalid_decision"},
                    now=now,
                    new_id_fn=new_id_fn,
                )
                return

            _validate_and_apply_decision(
                db=db,
                case=case,
                decision=decision,
                snapshot=stored_snapshot,
                now=now,
                new_id_fn=new_id_fn,
            )


def _call_deliberation_model(
    *,
    model_input: list[dict[str, Any]],
    settings: AppSettings,
    model_adapter: Any | None,
) -> dict[str, Any]:
    tools = _read_only_response_tool_definitions()
    input_items = list(model_input)
    tool_outputs: list[dict[str, Any]] = []
    max_rounds = max(1, int(settings.proactive_deliberation_tool_rounds))
    if model_adapter is not None:
        for _ in range(max_rounds):
            response = model_adapter.create_response(
                input_items=input_items,
                tools=tools,
                user_message="",
                history=[],
                context_bundle={"origin": "proactive", "model_input": input_items},
            )
            calls = _response_function_calls(response)
            if not calls:
                return {**response, "tool_outputs": tool_outputs, "model_input": input_items}
            input_items.extend(calls)
            input_items.extend(_proactive_tool_call_outputs(calls, tool_outputs))
        return {**response, "tool_outputs": tool_outputs, "model_input": input_items}

    if settings.openai_api_key is None:
        raise RuntimeError("model credentials are not configured")
    for _ in range(max_rounds):
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
            json={
                "model": settings.model_name,
                "input": input_items,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "store": False,
                "reasoning": {"effort": settings.model_reasoning_effort},
                "text": {"verbosity": settings.model_verbosity},
            },
            timeout=settings.model_timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"model provider returned HTTP {response.status_code}")
        payload = response.json()
        model_response = {
            "output": payload.get("output"),
            "provider": "openai",
            "model": settings.model_name,
            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            "provider_response_id": payload.get("id"),
        }
        calls = _response_function_calls(model_response)
        if not calls:
            return {**model_response, "tool_outputs": tool_outputs, "model_input": input_items}
        input_items.extend(calls)
        input_items.extend(_proactive_tool_call_outputs(calls, tool_outputs))
    return {**model_response, "tool_outputs": tool_outputs, "model_input": input_items}


def _read_only_response_tool_definitions() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool in response_tool_definitions():
        name = tool.get("name")
        capability_id = (
            capability_id_for_response_tool_name(name) if isinstance(name, str) else None
        )
        capability = get_capability(capability_id) if capability_id is not None else None
        if capability is not None and capability.impact_level == "read":
            tools.append(tool)
    return tools


def _response_function_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    if not isinstance(output, list):
        return []
    return [
        item for item in output if isinstance(item, dict) and item.get("type") == "function_call"
    ]


def _proactive_tool_call_outputs(
    calls: list[dict[str, Any]],
    tool_outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output_items: list[dict[str, Any]] = []
    for call in calls:
        call_id = call.get("call_id")
        name = call.get("name")
        arguments = call.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str):
            continue
        capability_id = capability_id_for_response_tool_name(name)
        capability = get_capability(capability_id) if capability_id is not None else None
        payload: dict[str, Any]
        if capability is None or capability.impact_level != "read":
            payload = {"status": "failed", "error": "proactive_deliberation_tool_denied"}
        else:
            try:
                raw_input = json.loads(arguments) if isinstance(arguments, str) else {}
            except json.JSONDecodeError:
                raw_input = {}
            if not isinstance(raw_input, dict):
                raw_input = {}
            normalized_input, input_error = capability.validate_input(raw_input)
            if normalized_input is None or input_error is not None:
                payload = {"status": "failed", "error": input_error or "schema_invalid"}
            else:
                result = execute_capability(
                    capability=capability,
                    normalized_input=normalized_input,
                )
                payload = {
                    "status": result.status,
                    "output": result.output,
                    "error": result.error,
                }
        tool_outputs.append(
            {
                "call_id": call_id,
                "tool_name": name,
                "capability_id": capability_id,
                "result": payload,
            }
        )
        output_items.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(payload, sort_keys=True),
            }
        )
    return output_items


def _parse_model_json(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, list):
        raise RuntimeError("model response missing output")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if isinstance(text, str):
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
    raise RuntimeError("model response missing JSON decision")


def _call_direct_json_model(
    *,
    model_input: list[dict[str, Any]],
    settings: AppSettings,
    model_adapter: Any | None,
    origin: str,
) -> dict[str, Any]:
    if model_adapter is not None:
        return model_adapter.create_response(
            input_items=model_input,
            tools=[],
            user_message="",
            history=[],
            context_bundle={"origin": origin, "model_input": model_input},
        )
    if settings.openai_api_key is None:
        raise RuntimeError("model credentials are not configured")
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={
            "authorization": f"Bearer {settings.openai_api_key}",
            "content-type": "application/json",
        },
        json={
            "model": settings.model_name,
            "input": model_input,
            "store": False,
            "reasoning": {"effort": settings.model_reasoning_effort},
            "text": {"verbosity": settings.model_verbosity},
        },
        timeout=settings.model_timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"model provider returned HTTP {response.status_code}")
    payload = response.json()
    return {
        "output": payload.get("output"),
        "provider": "openai",
        "model": settings.model_name,
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        "provider_response_id": payload.get("id"),
    }


def _provider_value(response: dict[str, Any], key: str, fallback: str) -> str:
    value = response.get(key)
    return value if isinstance(value, str) and value.strip() else fallback


def _provider_response_id(response: dict[str, Any]) -> str | None:
    value = response.get("provider_response_id")
    return value if isinstance(value, str) and value.strip() else None


def _update_snapshot_tool_context(
    snapshot: ProactiveContextSnapshotRecord,
    response: dict[str, Any],
) -> None:
    tool_outputs = response.get("tool_outputs")
    if isinstance(tool_outputs, list) and tool_outputs:
        context = dict(snapshot.context)
        context["tool_outputs"] = tool_outputs
        snapshot.context = context
    model_input = response.get("model_input")
    if isinstance(model_input, list):
        snapshot.model_input = [item for item in model_input if isinstance(item, dict)]


def _record_invalid_decision(
    *,
    db: Session,
    case: ProactiveCaseRecord,
    snapshot: ProactiveContextSnapshotRecord,
    response: dict[str, Any],
    reason: str,
    parse_status: str,
    raw_model_output: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    decision = ProactiveDecisionRecord(
        id=new_id_fn("pdc"),
        case_id=case.id,
        context_snapshot_id=snapshot.id,
        provider=_provider_value(response, "provider", "unknown"),
        model=_provider_value(response, "model", "unknown"),
        provider_response_id=_provider_response_id(response),
        decision_type="ignore",
        status="invalid",
        confidence=0.0,
        urgency="normal",
        user_visible_message=None,
        rationale=reason,
        evidence_refs=[],
        tool_refs=[],
        actions=[],
        follow_up=None,
        raw_model_output=raw_model_output,
        created_at=now,
    )
    db.add(decision)
    db.flush()
    case.last_decision_id = decision.id
    db.add(
        AIJudgmentRecord(
            id=new_id_fn("ajg"),
            judgment_type="proactive_deliberation",
            source_type="proactive_case",
            source_id=case.id,
            status="failed",
            model=_provider_value(response, "model", "unknown"),
            prompt_version=PROACTIVE_POLICY_VERSION,
            provider_response_id=_provider_response_id(response),
            input_summary="proactive case deliberation",
            input_refs={
                "case_id": case.id,
                "context_snapshot_id": snapshot.id,
                "latest_observation_id": case.latest_observation_id,
            },
            selected=[],
            omitted=[],
            output=raw_model_output,
            rationale=reason,
            uncertainty=None,
            confidence=0.0,
            parse_status=parse_status,
            validation_status="not_validated",
            failure_code=(
                "E_AI_JUDGMENT_INVALID_JSON"
                if parse_status == "invalid_json"
                else "E_AI_JUDGMENT_REQUIRED"
            ),
            failure_reason=reason,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        ProactivePolicyValidationRecord(
            id=new_id_fn("ppv"),
            case_id=case.id,
            decision_id=decision.id,
            result="invalid_decision",
            policy_version=PROACTIVE_POLICY_VERSION,
            action_plan_hash=_json_hash({"actions": []}),
            constraints={},
            denial_reason=reason,
            created_at=now,
        )
    )
    case.status = "failed"
    case.updated_at = now
    _add_case_event(
        db,
        case_id=case.id,
        event_type="decided",
        payload={"decision_id": decision.id, "decision_type": decision.decision_type},
        now=now,
        new_id_fn=new_id_fn,
    )
    _add_case_event(
        db,
        case_id=case.id,
        event_type="failed",
        payload={"decision_id": decision.id, "reason": "invalid_decision"},
        now=now,
        new_id_fn=new_id_fn,
    )


def _json_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _valid_follow_up(follow_up: Any) -> bool:
    return isinstance(follow_up, dict) and follow_up.get("after") in _FOLLOW_UP_INTERVALS


def _remember_payload(raw_decision: dict[str, Any]) -> dict[str, Any] | None:
    memory = raw_decision.get("memory")
    if isinstance(memory, dict):
        return memory
    actions = raw_decision.get("actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        if not isinstance(action, dict) or action.get("action_type") != "remember":
            continue
        payload = action.get("payload")
        if isinstance(payload, dict):
            return payload
    return None


def _valid_remember_payload(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    value = payload.get("value")
    if value is None:
        value = payload.get("text")
    return (
        _normalized_text(payload.get("subject_key")) is not None
        and _normalized_text(payload.get("predicate")) is not None
        and _normalized_text(value) is not None
        and str(payload.get("assertion_type") or "fact")
        in {
            "fact",
            "profile",
            "preference",
            "commitment",
            "decision",
            "project_state",
            "procedure",
            "domain_concept",
        }
    )


def _normalized_remember_payload(payload: dict[str, Any]) -> dict[str, Any]:
    subject_key = _normalized_text(payload.get("subject_key")) or "user:default"
    predicate = _normalized_text(payload.get("predicate")) or "note"
    raw_value = payload.get("value")
    if raw_value is None:
        raw_value = payload.get("text")
    value = _normalized_text(raw_value) or ""
    assertion_type = str(payload.get("assertion_type") or "fact")
    if assertion_type not in {
        "fact",
        "profile",
        "preference",
        "commitment",
        "decision",
        "project_state",
        "procedure",
        "domain_concept",
    }:
        assertion_type = "fact"
    return {
        "subject_key": subject_key,
        "predicate": predicate,
        "value": value,
        "assertion_type": assertion_type,
    }


def _entity_type(subject_key: str, assertion_type: str) -> str:
    if subject_key.startswith("project:"):
        return "project"
    if subject_key.startswith("repo:"):
        return "repo"
    if assertion_type in {"commitment", "decision", "procedure", "preference"}:
        return assertion_type
    return "assertion_subject"


def _action_target_system(action_type: str, action: dict[str, Any]) -> str:
    target_system = action.get("target_system")
    if isinstance(target_system, str) and target_system.strip():
        return target_system.strip()
    if action_type == "send_discord_message":
        return "discord"
    return action_type


def _normalize_action_payload(action_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if action_type != "send_discord_message":
        return payload
    message = _normalized_text(payload.get("message"), max_chars=4000)
    if message is None:
        message = _normalized_text(payload.get("text"), max_chars=4000)
    if message is None:
        return None
    return {"message": message}


def _risk_allowed_by_scope(scope: AutonomyScopeRecord, risk_tier: str) -> bool:
    max_impact = getattr(scope, "max_impact", None)
    return (
        isinstance(max_impact, str)
        and max_impact in _IMPACT_ORDER
        and risk_tier in _IMPACT_ORDER
        and _IMPACT_ORDER[risk_tier] <= _IMPACT_ORDER[max_impact]
    )


def _payload_allowed_by_scope(scope: AutonomyScopeRecord, payload: dict[str, Any]) -> bool:
    allowed_payload = scope.allowed_payload if isinstance(scope.allowed_payload, dict) else {}
    return all(payload.get(key) == value for key, value in allowed_payload.items())


def _payload_shape_value_allowed(value: Any, expected_type: Any) -> bool:
    if not isinstance(expected_type, str):
        return False
    match expected_type.strip().lower():
        case "str" | "string":
            return isinstance(value, str)
        case "list" | "array":
            return isinstance(value, list)
        case "dict" | "object":
            return isinstance(value, dict)
        case "bool" | "boolean":
            return isinstance(value, bool)
        case "int" | "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "float" | "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "null":
            return value is None
        case _:
            return False


def _payload_allowed_by_shape(scope: AutonomyScopeRecord, payload: dict[str, Any]) -> bool:
    shape = scope.allowed_payload_shape
    if not isinstance(shape, dict):
        return False
    if not shape:
        return False

    raw_required = shape.get("required")
    required: dict[str, Any] = {}
    if isinstance(raw_required, dict):
        required = {key: value for key, value in raw_required.items() if isinstance(key, str)}
    elif isinstance(raw_required, list):
        required = {key: None for key in raw_required if isinstance(key, str)}

    raw_properties = shape.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    if properties:
        for key in required:
            if key not in payload:
                return False
        for key, spec in properties.items():
            if not isinstance(key, str) or key not in payload:
                continue
            expected_type = spec.get("type") if isinstance(spec, dict) else spec
            if expected_type is not None and not _payload_shape_value_allowed(
                payload[key], expected_type
            ):
                return False
        if shape.get("additionalProperties") is False:
            allowed_keys = {key for key in properties if isinstance(key, str)}
            return set(payload).issubset(allowed_keys)
        return True

    direct_shape = {
        key: value
        for key, value in shape.items()
        if key not in {"required", "allow_extra", "additionalProperties"}
    }
    if not required:
        required = {key: value for key, value in direct_shape.items() if isinstance(key, str)}
    for key, expected_type in required.items():
        if key not in payload:
            return False
        if expected_type is not None and not _payload_shape_value_allowed(
            payload[key], expected_type
        ):
            return False
    if shape.get("allow_extra") is False or shape.get("additionalProperties") is False:
        allowed_keys = set(required) | {key for key in direct_shape if isinstance(key, str)}
        return set(payload).issubset(allowed_keys)
    return True


def _action_target_allowed_by_scope(scope: AutonomyScopeRecord, target: str) -> bool:
    source_context = scope.source_context if isinstance(scope.source_context, dict) else {}
    allowed_targets = source_context.get("allowed_targets")
    if isinstance(allowed_targets, list):
        return target in {
            item.strip() for item in allowed_targets if isinstance(item, str) and item.strip()
        }
    allowed_target = source_context.get("target")
    if isinstance(allowed_target, str) and allowed_target.strip():
        return target == allowed_target.strip()
    return False


def _payload_recipients(payload: dict[str, Any]) -> list[str]:
    recipients: list[str] = []
    for key in ("recipient", "recipients", "to", "cc", "bcc", "attendees", "grantee_email"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            recipients.append(value.strip().lower())
        elif isinstance(value, list):
            recipients.extend(
                item.strip().lower() for item in value if isinstance(item, str) and item.strip()
            )
    return recipients


def _payload_recipients_allowed_by_scope(
    scope: AutonomyScopeRecord,
    payload: dict[str, Any],
) -> bool:
    source_context = scope.source_context if isinstance(scope.source_context, dict) else {}
    recipients = _payload_recipients(payload)
    if not recipients:
        return True
    allowed_recipients = source_context.get("allowed_recipients")
    if not isinstance(allowed_recipients, list):
        return False
    allowed = {
        item.strip().lower()
        for item in allowed_recipients
        if isinstance(item, str) and item.strip()
    }
    return bool(recipients) and all(recipient in allowed for recipient in recipients)


def _notification_rule_valid(scope: AutonomyScopeRecord) -> bool:
    notification_rule = getattr(scope, "notification_rule", None)
    return notification_rule in {"silent_audit", "notify_after", "notify_before"}


def _find_autonomy_scope(
    db: Session,
    *,
    action_type: str,
    target_system: str,
    target: str,
    payload: dict[str, Any],
    risk_tier: str,
) -> tuple[AutonomyScopeRecord | None, str, str, list[str]]:
    scopes = db.scalars(
        select(AutonomyScopeRecord)
        .where(
            AutonomyScopeRecord.status == "active",
            AutonomyScopeRecord.actor == "proactive",
            AutonomyScopeRecord.action_type == action_type,
        )
        .order_by(AutonomyScopeRecord.created_at.asc(), AutonomyScopeRecord.id.asc())
        .limit(20)
    ).all()
    considered_scope_ids: list[str] = []
    denial_result = "needs_user_authority"
    denial_reason = f"no active autonomy scope for {action_type}"
    for scope in scopes:
        target_system_matches = scope.target_system == target_system
        allowed_target_systems = getattr(scope, "allowed_target_systems", None)
        if isinstance(allowed_target_systems, list) and target_system in {
            item for item in allowed_target_systems if isinstance(item, str)
        }:
            target_system_matches = True
        if not target_system_matches:
            continue

        considered_scope_ids.append(scope.id)
        if not _risk_allowed_by_scope(scope, risk_tier):
            denial_reason = f"risk tier exceeds autonomy scope for {action_type}"
            continue
        if not _notification_rule_valid(scope):
            denial_result = "invalid_decision"
            denial_reason = f"autonomy scope notification rule invalid for {action_type}"
            continue
        if not _action_target_allowed_by_scope(scope, target):
            denial_reason = f"target is outside autonomy scope for {action_type}"
            continue
        if not _payload_recipients_allowed_by_scope(scope, payload):
            denial_reason = f"recipient is outside autonomy scope for {action_type}"
            continue
        if not _payload_allowed_by_shape(scope, payload):
            denial_reason = f"payload shape is outside autonomy scope for {action_type}"
            continue
        if not _payload_allowed_by_scope(scope, payload):
            denial_reason = f"payload is outside autonomy scope for {action_type}"
            continue
        return scope, "authorized", "", considered_scope_ids
    return None, denial_result, denial_reason, considered_scope_ids


def _decision_has_discord_message_action(decision: ProactiveDecisionRecord) -> bool:
    return any(action.get("action_type") == "send_discord_message" for action in decision.actions)


def _taint_blocks_autonomous_write(latest_taint: Any) -> bool:
    return isinstance(latest_taint, dict) and (
        latest_taint.get("provenance_status") in {"tainted", "ambiguous"}
        or latest_taint.get("status") in {"tainted", "ambiguous"}
        or latest_taint.get("reason") == "prompt_injection"
        or latest_taint.get("prompt_injection") is True
    )


def _validate_and_apply_decision(
    *,
    db: Session,
    case: ProactiveCaseRecord,
    decision: ProactiveDecisionRecord,
    snapshot: ProactiveContextSnapshotRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    latest_taint = (
        snapshot.taint.get("latest_observation") if isinstance(snapshot.taint, dict) else {}
    )
    tainted = _taint_blocks_autonomous_write(latest_taint)
    validation_result = "authorized"
    denial_reason = None
    considered_scope_ids: list[str] = []
    normalized_actions: list[dict[str, Any]] = []
    if decision.decision_type in {"act_now", "speak_and_act"}:
        for action in decision.actions:
            action_type = action.get("action_type")
            target = action.get("target")
            payload = action.get("payload")
            risk_tier = action.get("risk_tier")
            if (
                not isinstance(action_type, str)
                or not action_type.strip()
                or not isinstance(target, str)
                or not target.strip()
                or not isinstance(payload, dict)
                or risk_tier not in {"low", "medium", "high", "blocked"}
            ):
                validation_result = "invalid_decision"
                denial_reason = "action schema invalid"
                break
            action_type = action_type.strip()
            target = target.strip()
            capability = None
            if action_type == "send_discord_message":
                normalized_input = _normalize_action_payload(action_type, payload)
                if normalized_input is None:
                    validation_result = "invalid_decision"
                    denial_reason = "discord message action missing message"
                    break
            else:
                capability = get_capability(action_type)
                if capability is None:
                    validation_result = "invalid_decision"
                    denial_reason = f"unknown capability {action_type}"
                    break
                normalized_input, input_error = capability.validate_input(payload)
                if normalized_input is None or input_error is not None:
                    validation_result = "invalid_decision"
                    denial_reason = f"action input invalid for {action_type}"
                    break
            if risk_tier == "blocked":
                validation_result = "denied"
                denial_reason = f"blocked risk tier for {action_type}"
                break
            target_system = _action_target_system(action_type, action)
            scope, scope_result, scope_denial_reason, scope_ids = _find_autonomy_scope(
                db,
                action_type=action_type,
                target_system=target_system,
                target=target.strip(),
                payload=normalized_input,
                risk_tier=risk_tier,
            )
            considered_scope_ids.extend(scope_ids)
            if scope is None:
                validation_result = scope_result
                denial_reason = scope_denial_reason
                break
            if tainted:
                validation_result = "denied"
                denial_reason = (
                    "tainted context cannot execute non-low-risk action"
                    if risk_tier != "low"
                    else "tainted context cannot execute autonomous write"
                )
                break
            if capability is not None:
                preflight_error = preflight_capability_execution(
                    capability=capability,
                    normalized_input=normalized_input,
                )
                if preflight_error is not None:
                    validation_result = "denied"
                    denial_reason = preflight_error
                    break
            normalized_actions.append(
                {
                    **action,
                    "target": target.strip(),
                    "payload": normalized_input,
                    "target_system": target_system,
                    "autonomy_scope_id": scope.id,
                }
            )
        if validation_result in {"authorized", "authorized_with_constraints"}:
            decision.actions = normalized_actions

    action_plan_hash = _json_hash({"actions": decision.actions}) if decision.actions else None

    validation = ProactivePolicyValidationRecord(
        id=new_id_fn("ppv"),
        case_id=case.id,
        decision_id=decision.id,
        result=validation_result,
        policy_version=PROACTIVE_POLICY_VERSION,
        action_plan_hash=action_plan_hash,
        constraints={"considered_scope_ids": considered_scope_ids} if considered_scope_ids else {},
        denial_reason=denial_reason,
        created_at=now,
    )
    db.add(validation)
    db.flush()
    _add_case_event(
        db,
        case_id=case.id,
        event_type="validated",
        payload={"decision_id": decision.id, "result": validation.result},
        now=now,
        new_id_fn=new_id_fn,
    )

    if validation.result not in {"authorized", "authorized_with_constraints"}:
        decision.status = "validated"
        case.status = "failed" if validation.result == "invalid_decision" else "asked"
        case.updated_at = now
        return

    if decision.decision_type == "ignore":
        decision.status = "ignored"
        case.status = "ignored"
        case.updated_at = now
        return

    if decision.decision_type == "remember":
        _apply_remember_decision(
            db=db,
            case=case,
            decision=decision,
            now=now,
            new_id_fn=new_id_fn,
        )
        decision.status = "executed"
        case.status = "resolved"
        case.updated_at = now
        return

    if decision.decision_type in {"wait", "observe_more"}:
        recheck_at = _follow_up_time(decision.follow_up, now)
        case.status = "waiting"
        case.next_recheck_after = recheck_at
        case.updated_at = now
        _add_task(
            db,
            task_type="proactive_follow_up_due",
            payload={"case_id": case.id, "scheduled_for": to_rfc3339(recheck_at)},
            now=now,
            run_after=recheck_at,
            new_id_fn=new_id_fn,
        )
        _add_case_event(
            db,
            case_id=case.id,
            event_type="waiting",
            payload={"scheduled_for": to_rfc3339(recheck_at)},
            now=now,
            new_id_fn=new_id_fn,
        )
        decision.status = "executed"
        return

    should_create_turn = decision.decision_type in {"speak_now", "ask_user"} or (
        decision.decision_type == "speak_and_act"
        and not _decision_has_discord_message_action(decision)
    )
    if should_create_turn:
        _create_proactive_turn(
            db=db,
            case=case,
            decision=decision,
            validation=validation,
            now=now,
            new_id_fn=new_id_fn,
        )

    if decision.decision_type in {"act_now", "speak_and_act"}:
        for index, action in enumerate(decision.actions):
            _create_action_plan(
                db=db,
                case=case,
                decision=decision,
                validation=validation,
                action=action,
                index=index,
                now=now,
                new_id_fn=new_id_fn,
            )

    decision.status = "executed"
    if decision.decision_type == "ask_user":
        case.status = "asked"
    elif decision.decision_type == "speak_and_act":
        case.status = "spoken"
    elif decision.decision_type != "act_now":
        case.status = "spoken"
    case.updated_at = now


def _follow_up_time(follow_up: dict[str, Any] | None, now: datetime) -> datetime:
    raw_after = follow_up.get("after") if isinstance(follow_up, dict) else None
    after = raw_after if isinstance(raw_after, str) else ""
    return now + _FOLLOW_UP_INTERVALS.get(after, timedelta(minutes=15))


def _apply_remember_decision(
    *,
    db: Session,
    case: ProactiveCaseRecord,
    decision: ProactiveDecisionRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    raw_memory = decision.raw_model_output.get("memory")
    if not isinstance(raw_memory, dict):
        return
    memory = _normalized_remember_payload(raw_memory)
    entity_type = _entity_type(memory["subject_key"], memory["assertion_type"])
    entity = db.scalar(
        select(MemoryEntityRecord)
        .where(
            MemoryEntityRecord.entity_type == entity_type,
            MemoryEntityRecord.entity_key == memory["subject_key"],
        )
        .limit(1)
    )
    if entity is None:
        entity = MemoryEntityRecord(
            id=new_id_fn("men"),
            entity_type=entity_type,
            entity_key=memory["subject_key"],
            display_name=memory["subject_key"],
            summary=None,
            metadata_json={"origin": "proactive"},
            created_at=now,
            updated_at=now,
        )
        db.add(entity)
        db.flush()
    else:
        entity.updated_at = now
    assertion = MemoryAssertionRecord(
        id=new_id_fn("mas"),
        subject_entity_id=entity.id,
        subject_key=memory["subject_key"],
        predicate=memory["predicate"],
        scope_key=f"proactive:{case.id}",
        object_value={"text": memory["value"]},
        assertion_type=memory["assertion_type"],
        is_multi_valued=False,
        scope={"kind": "proactive_case", "case_id": case.id},
        lifecycle_state="active",
        confidence=decision.confidence,
        valid_from=now,
        valid_to=None,
        superseded_by_assertion_id=None,
        extraction_model=decision.model,
        extraction_prompt_version=PROACTIVE_POLICY_VERSION,
        last_verified_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(assertion)
    db.flush()
    _add_case_event(
        db,
        case_id=case.id,
        event_type="resolved",
        payload={"memory_assertion_id": assertion.id},
        now=now,
        new_id_fn=new_id_fn,
    )


def _create_proactive_turn(
    *,
    db: Session,
    case: ProactiveCaseRecord,
    decision: ProactiveDecisionRecord,
    validation: ProactivePolicyValidationRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    if decision.user_visible_message is None:
        return
    turn = db.scalar(
        select(ProactiveTurnRecord)
        .where(ProactiveTurnRecord.dedupe_key == f"case:{case.id}:decision:{decision.id}:discord")
        .with_for_update()
        .limit(1)
    )
    if turn is None:
        turn = ProactiveTurnRecord(
            id=new_id_fn("ptr"),
            case_id=case.id,
            decision_id=decision.id,
            dedupe_key=f"case:{case.id}:decision:{decision.id}:discord",
            origin="proactive",
            channel="discord",
            status="pending",
            message=decision.user_visible_message,
            delivery_payload={
                "case_id": case.id,
                "decision_id": decision.id,
                "policy_validation_id": validation.id,
            },
            delivered_at=None,
            acked_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(turn)
        db.flush()
        notification = NotificationRecord(
            id=new_id_fn("ntf"),
            dedupe_key=f"proactive-turn:{turn.id}",
            source_type="proactive_turn",
            source_id=turn.id,
            channel="discord",
            status="pending",
            title=case.title,
            body=turn.message,
            payload={"proactive_turn_id": turn.id, "case_id": case.id},
            created_at=now,
            updated_at=now,
        )
        db.add(notification)
        db.flush()
        _add_task(
            db,
            task_type="deliver_discord_notification",
            payload={"notification_id": notification.id},
            now=now,
            new_id_fn=new_id_fn,
            max_attempts=5,
        )
        _add_case_event(
            db,
            case_id=case.id,
            event_type="turn_created",
            payload={"proactive_turn_id": turn.id},
            now=now,
            new_id_fn=new_id_fn,
        )


def _create_action_plan(
    *,
    db: Session,
    case: ProactiveCaseRecord,
    decision: ProactiveDecisionRecord,
    validation: ProactivePolicyValidationRecord,
    action: dict[str, Any],
    index: int,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    payload = action.get("payload")
    if not isinstance(payload, dict):
        return
    action_type = str(action.get("action_type") or "")
    target = str(action.get("target") or "")
    payload_hash = _json_hash(payload)
    plan = ProactiveActionPlanRecord(
        id=new_id_fn("pap"),
        case_id=case.id,
        decision_id=decision.id,
        plan_key=f"case:{case.id}:decision:{decision.id}:action:{index}:{payload_hash}",
        action_type=action_type,
        target=target,
        payload=payload,
        payload_hash=payload_hash,
        risk_tier=str(action.get("risk_tier") or "low"),
        status="authorized",
        policy_validation_id=validation.id,
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    db.flush()
    _add_case_event(
        db,
        case_id=case.id,
        event_type="action_planned",
        payload={"action_plan_id": plan.id, "action_type": plan.action_type},
        now=now,
        new_id_fn=new_id_fn,
    )
    _add_task(
        db,
        task_type="proactive_action_execution_due",
        payload={"action_plan_id": plan.id},
        now=now,
        new_id_fn=new_id_fn,
    )


def process_proactive_action_execution_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    google_runtime: Any | None = None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    action_plan_id = _payload_text(task_payload, "action_plan_id")
    if action_plan_id is None:
        raise RuntimeError("proactive_action_execution_due task missing action_plan_id")

    capability_payload: dict[str, Any] | None = None
    capability_action_type: str | None = None
    with session_factory() as db:
        with db.begin():
            plan = db.scalar(
                select(ProactiveActionPlanRecord)
                .where(ProactiveActionPlanRecord.id == action_plan_id)
                .with_for_update()
                .limit(1)
            )
            if plan is None:
                raise RuntimeError("proactive action plan not found")
            if plan.status in {"succeeded", "cancelled"}:
                return
            existing = db.scalar(
                select(ProactiveActionExecutionRecord)
                .where(
                    ProactiveActionExecutionRecord.idempotency_key
                    == f"proactive-action:{plan.id}:{plan.payload_hash}"
                )
                .with_for_update()
                .limit(1)
            )
            if existing is not None and existing.status == "succeeded":
                return
            now = now_fn()
            execution = existing or ProactiveActionExecutionRecord(
                id=new_id_fn("pax"),
                action_plan_id=plan.id,
                idempotency_key=f"proactive-action:{plan.id}:{plan.payload_hash}",
                status="pending",
                external_receipt=None,
                error=None,
                started_at=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
            if existing is None:
                db.add(execution)
                db.flush()
            execution.status = "running"
            execution.started_at = execution.started_at or now
            execution.completed_at = None
            execution.error = None
            execution.updated_at = now
            if plan.action_type == "send_discord_message":
                decision = db.get(ProactiveDecisionRecord, plan.decision_id)
                case = db.get(ProactiveCaseRecord, plan.case_id)
                message = plan.payload.get("message")
                if decision is None or case is None:
                    raise RuntimeError("proactive action parent records missing")
                if not isinstance(message, str) or not message.strip():
                    execution.status = "failed"
                    execution.error = "discord message action missing message"
                    execution.completed_at = now
                    plan.status = "failed"
                else:
                    decision.user_visible_message = message.strip()
                    validation = (
                        db.get(ProactivePolicyValidationRecord, plan.policy_validation_id)
                        if plan.policy_validation_id is not None
                        else None
                    )
                    if validation is None:
                        raise RuntimeError("proactive action validation missing")
                    _create_proactive_turn(
                        db=db,
                        case=case,
                        decision=decision,
                        validation=validation,
                        now=now,
                        new_id_fn=new_id_fn,
                    )
                    execution.status = "succeeded"
                    execution.external_receipt = {"kind": "proactive_turn_delivery_queued"}
                    execution.error = None
                    execution.completed_at = now
                    plan.status = "succeeded"
                    plan.updated_at = now
            else:
                capability = get_capability(plan.action_type)
                if capability is None:
                    execution.status = "failed"
                    execution.error = "unknown_capability"
                    execution.completed_at = now
                    plan.status = "failed"
                    plan.updated_at = now
                else:
                    normalized_input, input_error = capability.validate_input(plan.payload)
                    if normalized_input is None or input_error is not None:
                        execution.status = "failed"
                        execution.error = "schema_invalid"
                        execution.completed_at = now
                        plan.status = "failed"
                        plan.updated_at = now
                    else:
                        preflight_error = preflight_capability_execution(
                            capability=capability,
                            normalized_input=normalized_input,
                        )
                        if preflight_error is not None:
                            execution.status = "failed"
                            execution.error = preflight_error
                            execution.completed_at = now
                            plan.status = "failed"
                            plan.updated_at = now
                        else:
                            capability_payload = normalized_input
                            capability_action_type = plan.action_type
                            plan.status = "executing"
                            plan.updated_at = now

    if capability_payload is not None and capability_action_type is not None:
        capability = get_capability(capability_action_type)
        result_status: str
        result_output: dict[str, Any] | None
        result_error: str | None
        if capability is None:
            result_status = "failed"
            result_output = None
            result_error = "unknown_capability"
        elif google_runtime is not None and capability_action_type.startswith(
            ("cap.calendar.", "cap.email.", "cap.drive.")
        ):
            with session_factory() as db:
                with db.begin():
                    access_token, granted_scopes, access_failure = (
                        google_runtime.prepare_capability_access(
                            db=db,
                            capability_id=capability_action_type,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                    )
            if access_failure is not None:
                google_result = access_failure
            elif access_token is None:
                google_result = google_runtime._typed_failure(failure_class="token_expired")
            else:
                google_result = google_runtime.execute_provider_capability(
                    capability_id=capability_action_type,
                    normalized_input=capability_payload,
                    access_token=access_token,
                    granted_scopes=granted_scopes,
                )
            result_status = google_result.status
            result_output = google_result.output
            result_error = google_result.error
        else:
            result = execute_capability(
                capability=capability,
                normalized_input=capability_payload,
            )
            result_status = result.status
            result_output = result.output
            result_error = result.error

        with session_factory() as db:
            with db.begin():
                plan = db.scalar(
                    select(ProactiveActionPlanRecord)
                    .where(ProactiveActionPlanRecord.id == action_plan_id)
                    .with_for_update()
                    .limit(1)
                )
                if plan is None:
                    raise RuntimeError("proactive action plan not found after execution")
                stored_execution = db.scalar(
                    select(ProactiveActionExecutionRecord)
                    .where(
                        ProactiveActionExecutionRecord.idempotency_key
                        == f"proactive-action:{plan.id}:{plan.payload_hash}"
                    )
                    .with_for_update()
                    .limit(1)
                )
                if stored_execution is None:
                    raise RuntimeError("proactive action execution missing after execution")
                now = now_fn()
                if result_status == "succeeded":
                    stored_execution.status = "succeeded"
                    stored_execution.external_receipt = result_output or {}
                    stored_execution.error = None
                    plan.status = "succeeded"
                else:
                    stored_execution.status = "failed"
                    stored_execution.external_receipt = None
                    stored_execution.error = result_error or "execution_failed"
                    plan.status = "failed"
                stored_execution.completed_at = now
                stored_execution.updated_at = now
                plan.updated_at = now
                _refresh_case_action_status(
                    db=db,
                    case_id=plan.case_id,
                    now=now,
                )
                _add_case_event(
                    db,
                    case_id=plan.case_id,
                    event_type="action_executed",
                    payload={"action_plan_id": plan.id, "status": stored_execution.status},
                    now=now,
                    new_id_fn=new_id_fn,
                )
        return

    with session_factory() as db:
        with db.begin():
            plan = db.scalar(
                select(ProactiveActionPlanRecord)
                .where(ProactiveActionPlanRecord.id == action_plan_id)
                .with_for_update()
                .limit(1)
            )
            if plan is None:
                raise RuntimeError("proactive action plan not found after local execution")
            stored_execution = db.scalar(
                select(ProactiveActionExecutionRecord)
                .where(
                    ProactiveActionExecutionRecord.idempotency_key
                    == f"proactive-action:{plan.id}:{plan.payload_hash}"
                )
                .with_for_update()
                .limit(1)
            )
            if stored_execution is None or stored_execution.status == "running":
                return
            now = now_fn()
            stored_execution.updated_at = now
            plan.updated_at = now
            _refresh_case_action_status(
                db=db,
                case_id=plan.case_id,
                now=now,
            )
            _add_case_event(
                db,
                case_id=plan.case_id,
                event_type="action_executed",
                payload={"action_plan_id": plan.id, "status": stored_execution.status},
                now=now,
                new_id_fn=new_id_fn,
            )


def _refresh_case_action_status(
    *,
    db: Session,
    case_id: str,
    now: datetime,
) -> None:
    case = db.scalar(
        select(ProactiveCaseRecord)
        .where(ProactiveCaseRecord.id == case_id)
        .with_for_update()
        .limit(1)
    )
    if case is None or case.status in {"acknowledged", "resolved"}:
        return
    plans = db.scalars(
        select(ProactiveActionPlanRecord).where(ProactiveActionPlanRecord.case_id == case_id)
    ).all()
    if not plans:
        return
    if all(plan.status == "succeeded" for plan in plans):
        case.status = "acted"
        case.updated_at = now
    elif any(plan.status == "failed" for plan in plans):
        case.status = "failed"
        case.updated_at = now


def process_proactive_follow_up_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    case_id = _payload_text(task_payload, "case_id")
    if case_id is None:
        raise RuntimeError("proactive_follow_up_due task missing case_id")
    with session_factory() as db:
        with db.begin():
            case = db.scalar(
                select(ProactiveCaseRecord)
                .where(ProactiveCaseRecord.id == case_id)
                .with_for_update()
                .limit(1)
            )
            if case is None:
                raise RuntimeError("proactive case not found")
            if case.status in {"resolved", "acknowledged"}:
                return
            now = now_fn()
            if case.next_recheck_after is not None and case.next_recheck_after > now:
                return
            case.status = "open"
            case.next_recheck_after = None
            case.updated_at = now
            _add_task(
                db,
                task_type="proactive_deliberation_due",
                payload={"case_id": case.id},
                now=now,
                new_id_fn=new_id_fn,
            )


def _feedback_learning_audit(
    *,
    feedback_id: str,
    response: dict[str, Any],
    parse_status: str,
    validation_status: str,
) -> dict[str, Any]:
    return {
        "source_feedback_id": feedback_id,
        "model": _provider_value(response, "model", "unknown"),
        "provider": _provider_value(response, "provider", "unknown"),
        "provider_response_id": _provider_response_id(response),
        "prompt_version": PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION,
        "parse_status": parse_status,
        "validation_status": validation_status,
    }


def _record_feedback_learning_failure(
    *,
    session_factory: sessionmaker[Session],
    feedback_id: str,
    response: dict[str, Any] | None,
    parse_status: str,
    validation_status: str,
    failure_code: str,
    reason: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    with session_factory() as db:
        with db.begin():
            feedback = db.get(ProactiveFeedbackRecord, feedback_id)
            if feedback is None:
                return
            now = now_fn()
            response_payload = response or {}
            db.add(
                AIJudgmentRecord(
                    id=new_id_fn("ajg"),
                    judgment_type="feedback_learning",
                    source_type="proactive_feedback",
                    source_id=feedback.id,
                    status="failed",
                    model=_provider_value(response_payload, "model", "unknown"),
                    prompt_version=PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION,
                    provider_response_id=_provider_response_id(response_payload),
                    input_summary="proactive feedback learning",
                    input_refs={
                        "feedback_id": feedback.id,
                        "case_id": feedback.case_id,
                    },
                    selected=[],
                    omitted=[],
                    output={
                        "response_output": response_payload.get("output")
                        if isinstance(response_payload, dict)
                        else None
                    },
                    rationale=None,
                    uncertainty=None,
                    confidence=None,
                    parse_status=parse_status,
                    validation_status=validation_status,
                    failure_code=failure_code,
                    failure_reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_case_event(
                db,
                case_id=feedback.case_id,
                event_type="failed",
                payload={
                    "failure_type": "feedback_learning_failed",
                    "feedback_id": feedback.id,
                    "failure_code": failure_code,
                    "reason": reason,
                    "audit": _feedback_learning_audit(
                        feedback_id=feedback.id,
                        response=response or {},
                        parse_status=parse_status,
                        validation_status=validation_status,
                    ),
                },
                now=now,
                new_id_fn=new_id_fn,
            )


def process_proactive_feedback_learning_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    model_adapter: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    feedback_id = _payload_text(task_payload, "feedback_id")
    if feedback_id is None:
        raise RuntimeError("proactive_feedback_learning_due task missing feedback_id")
    with session_factory() as db:
        with db.begin():
            feedback = db.scalar(
                select(ProactiveFeedbackRecord)
                .where(ProactiveFeedbackRecord.id == feedback_id)
                .with_for_update()
                .limit(1)
            )
            if feedback is None:
                raise RuntimeError("proactive feedback not found")
            case = db.get(ProactiveCaseRecord, feedback.case_id)
            if case is None:
                raise RuntimeError("proactive feedback case not found")
            observation = db.get(ProactiveObservationRecord, case.latest_observation_id)
            decision = (
                db.get(ProactiveDecisionRecord, case.last_decision_id)
                if case.last_decision_id is not None
                else None
            )
            snapshot = (
                db.get(ProactiveContextSnapshotRecord, decision.context_snapshot_id)
                if decision is not None
                else None
            )
            turns = db.scalars(
                select(ProactiveTurnRecord)
                .where(ProactiveTurnRecord.case_id == case.id)
                .order_by(ProactiveTurnRecord.created_at.desc(), ProactiveTurnRecord.id.asc())
                .limit(5)
            ).all()
            action_plans = db.scalars(
                select(ProactiveActionPlanRecord)
                .where(ProactiveActionPlanRecord.case_id == case.id)
                .order_by(
                    ProactiveActionPlanRecord.created_at.desc(),
                    ProactiveActionPlanRecord.id.asc(),
                )
                .limit(5)
            ).all()
            action_executions = db.scalars(
                select(ProactiveActionExecutionRecord)
                .where(
                    ProactiveActionExecutionRecord.action_plan_id.in_(
                        [plan.id for plan in action_plans] or [""]
                    )
                )
                .order_by(
                    ProactiveActionExecutionRecord.created_at.desc(),
                    ProactiveActionExecutionRecord.id.asc(),
                )
                .limit(5)
            ).all()
            learning_records = db.scalars(
                select(ProactiveLearningRecord)
                .where(ProactiveLearningRecord.status == "active")
                .order_by(
                    ProactiveLearningRecord.updated_at.desc(),
                    ProactiveLearningRecord.id.asc(),
                )
                .limit(20)
            ).all()
            context = {
                "prompt_version": PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION,
                "feedback": {
                    "id": feedback.id,
                    "feedback_type": feedback.feedback_type,
                    "note": feedback.note,
                    "payload": feedback.payload,
                    "created_at": to_rfc3339(feedback.created_at),
                },
                "case": {
                    "id": case.id,
                    "case_key": case.case_key,
                    "status": case.status,
                    "title": case.title,
                    "summary": case.summary,
                },
                "latest_observation": None
                if observation is None
                else {
                    "id": observation.id,
                    "source_type": observation.source_type,
                    "source_id": observation.source_id,
                    "observation_type": observation.observation_type,
                    "subject": observation.subject,
                    "summary": observation.summary,
                    "payload": observation.payload,
                    "evidence": observation.evidence,
                    "trust_boundary": observation.trust_boundary,
                },
                "decision": None
                if decision is None
                else {
                    "id": decision.id,
                    "decision_type": decision.decision_type,
                    "status": decision.status,
                    "confidence": decision.confidence,
                    "urgency": decision.urgency,
                    "user_visible_message": decision.user_visible_message,
                    "rationale": decision.rationale,
                    "evidence_refs": decision.evidence_refs,
                    "tool_refs": decision.tool_refs,
                    "actions": decision.actions,
                    "follow_up": decision.follow_up,
                    "raw_model_output": decision.raw_model_output,
                },
                "context_snapshot": None
                if snapshot is None
                else {
                    "id": snapshot.id,
                    "context": snapshot.context,
                    "omitted_context": snapshot.omitted_context,
                },
                "turns": [
                    {
                        "id": turn.id,
                        "status": turn.status,
                        "channel": turn.channel,
                        "message": turn.message,
                        "delivery_payload": turn.delivery_payload,
                        "delivered_at": (
                            to_rfc3339(turn.delivered_at) if turn.delivered_at is not None else None
                        ),
                        "acked_at": to_rfc3339(turn.acked_at)
                        if turn.acked_at is not None
                        else None,
                    }
                    for turn in turns
                ],
                "action_plans": [
                    {
                        "id": plan.id,
                        "status": plan.status,
                        "action_type": plan.action_type,
                        "target": plan.target,
                        "payload": plan.payload,
                        "risk_tier": plan.risk_tier,
                    }
                    for plan in action_plans
                ],
                "action_executions": [
                    {
                        "id": execution.id,
                        "action_plan_id": execution.action_plan_id,
                        "status": execution.status,
                        "external_receipt": execution.external_receipt,
                        "error": execution.error,
                    }
                    for execution in action_executions
                ],
                "related_learning_records": [
                    {
                        "id": record.id,
                        "feedback_id": record.feedback_id,
                        "record_type": record.record_type,
                        "content": record.content,
                    }
                    for record in learning_records
                    if record.feedback_id != feedback.id
                ],
            }
            input_refs = {
                "feedback_id": feedback.id,
                "case_id": case.id,
                "latest_observation_id": observation.id if observation is not None else None,
                "decision_id": decision.id if decision is not None else None,
                "context_snapshot_id": snapshot.id if snapshot is not None else None,
                "turn_ids": [turn.id for turn in turns],
                "action_plan_ids": [plan.id for plan in action_plans],
                "action_execution_ids": [execution.id for execution in action_executions],
                "related_learning_record_ids": [
                    record.id for record in learning_records if record.feedback_id != feedback.id
                ],
            }

    model_input = [
        {
            "role": "system",
            "content": (
                "You are Ariel's proactive feedback learner. Interpret the feedback and "
                "context into durable learning records. Return only strict JSON with key "
                "learning_records. Each item must include record_type and content. "
                "Allowed record_type values are instruction, example, calibration, "
                "preference, source_preference, prompt_instruction, autonomy_request. "
                "You may return an empty array. Do not grant autonomy; autonomy_request "
                "records are proposals only. Prompt version: "
                f"{PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION}."
            ),
        },
        {
            "role": "system",
            "content": json.dumps(context, sort_keys=True, separators=(",", ":")),
        },
    ]
    try:
        response = _call_direct_json_model(
            model_input=model_input,
            settings=settings,
            model_adapter=model_adapter,
            origin="feedback_learning",
        )
    except Exception as exc:
        reason = safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
        _record_feedback_learning_failure(
            session_factory=session_factory,
            feedback_id=feedback_id,
            response={"model": settings.model_name},
            parse_status="missing_output",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_REQUIRED",
            reason=reason,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        raise RuntimeError(reason) from exc
    try:
        raw_result = _parse_model_json(response)
    except json.JSONDecodeError as exc:
        reason = f"feedback_learner_parse_failed:{safe_proactive_error(exc)}"
        _record_feedback_learning_failure(
            session_factory=session_factory,
            feedback_id=feedback_id,
            response=response,
            parse_status="invalid_json",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_INVALID_JSON",
            reason=reason,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        raise RuntimeError(reason) from exc
    except RuntimeError as exc:
        reason = f"feedback_learner_parse_failed:{safe_proactive_error(exc)}"
        _record_feedback_learning_failure(
            session_factory=session_factory,
            feedback_id=feedback_id,
            response=response,
            parse_status="missing_output",
            validation_status="not_validated",
            failure_code="E_AI_JUDGMENT_REQUIRED",
            reason=reason,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        raise RuntimeError(reason) from exc

    raw_records = raw_result.get("learning_records")
    if not isinstance(raw_records, list):
        reason = "feedback_learner_validation_failed:missing_learning_records"
        _record_feedback_learning_failure(
            session_factory=session_factory,
            feedback_id=feedback_id,
            response=response,
            parse_status="schema_invalid",
            validation_status="invalid",
            failure_code="E_AI_JUDGMENT_SCHEMA",
            reason=reason,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        raise RuntimeError(reason)
    records: list[tuple[str, dict[str, Any]]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            reason = "feedback_learner_validation_failed:record_not_object"
            _record_feedback_learning_failure(
                session_factory=session_factory,
                feedback_id=feedback_id,
                response=response,
                parse_status="schema_invalid",
                validation_status="invalid",
                failure_code="E_AI_JUDGMENT_SCHEMA",
                reason=reason,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            raise RuntimeError(reason)
        record_type = _payload_text(raw_record, "record_type")
        content = raw_record.get("content")
        if record_type not in _FEEDBACK_LEARNING_RECORD_TYPES or not isinstance(content, dict):
            reason = "feedback_learner_validation_failed:record_schema_invalid"
            _record_feedback_learning_failure(
                session_factory=session_factory,
                feedback_id=feedback_id,
                response=response,
                parse_status="schema_invalid",
                validation_status="invalid",
                failure_code="E_AI_JUDGMENT_SCHEMA",
                reason=reason,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            raise RuntimeError(reason)
        records.append((record_type, content))

    audit = _feedback_learning_audit(
        feedback_id=feedback_id,
        response=response,
        parse_status="parsed",
        validation_status="valid",
    )

    with session_factory() as db:
        with db.begin():
            feedback = db.scalar(
                select(ProactiveFeedbackRecord)
                .where(ProactiveFeedbackRecord.id == feedback_id)
                .with_for_update()
                .limit(1)
            )
            if feedback is None:
                raise RuntimeError("proactive feedback not found after learning")
            now = now_fn()
            selected: list[dict[str, Any]] = []
            for record_type, content in records:
                record = ProactiveLearningRecord(
                    id=new_id_fn("plr"),
                    feedback_id=feedback.id,
                    record_type=record_type,
                    status="active",
                    content=content,
                    model=audit["model"],
                    prompt_version=PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION,
                    provider_response_id=audit["provider_response_id"],
                    parse_status="parsed",
                    validation_status="valid",
                    created_at=now,
                    updated_at=now,
                )
                db.add(record)
                selected.append(
                    {
                        "learning_record_id": record.id,
                        "record_type": record.record_type,
                    }
                )
            confidence = raw_result.get("confidence")
            db.add(
                AIJudgmentRecord(
                    id=new_id_fn("ajg"),
                    judgment_type="feedback_learning",
                    source_type="proactive_feedback",
                    source_id=feedback.id,
                    status="succeeded",
                    model=audit["model"],
                    prompt_version=PROACTIVE_FEEDBACK_LEARNING_PROMPT_VERSION,
                    provider_response_id=audit["provider_response_id"],
                    input_summary="proactive feedback learning",
                    input_refs=input_refs,
                    selected=selected,
                    omitted=[
                        item for item in raw_result.get("omitted", []) if isinstance(item, dict)
                    ]
                    if isinstance(raw_result.get("omitted"), list)
                    else [],
                    output=raw_result,
                    rationale=raw_result.get("rationale")
                    if isinstance(raw_result.get("rationale"), str)
                    else None,
                    uncertainty=raw_result.get("uncertainty")
                    if isinstance(raw_result.get("uncertainty"), str)
                    else None,
                    confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                    parse_status="parsed",
                    validation_status="valid",
                    failure_code=None,
                    failure_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def mark_proactive_turn_delivered(
    *,
    db: Session,
    proactive_turn_id: str,
    now: datetime,
) -> None:
    turn = db.scalar(
        select(ProactiveTurnRecord)
        .where(ProactiveTurnRecord.id == proactive_turn_id)
        .with_for_update()
        .limit(1)
    )
    if turn is not None and turn.status == "pending":
        turn.status = "delivered"
        turn.delivered_at = now
        turn.updated_at = now


def mark_proactive_turn_acknowledged(
    *,
    db: Session,
    proactive_turn_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    turn = db.scalar(
        select(ProactiveTurnRecord)
        .where(ProactiveTurnRecord.id == proactive_turn_id)
        .with_for_update()
        .limit(1)
    )
    if turn is None:
        return
    turn.status = "acknowledged"
    turn.acked_at = now
    turn.updated_at = now
    case = db.scalar(
        select(ProactiveCaseRecord)
        .where(ProactiveCaseRecord.id == turn.case_id)
        .with_for_update()
        .limit(1)
    )
    if case is not None:
        case.status = "acknowledged"
        case.next_recheck_after = None
        case.updated_at = now
        _add_case_event(
            db,
            case_id=case.id,
            event_type="acknowledged",
            payload={"proactive_turn_id": turn.id},
            now=now,
            new_id_fn=new_id_fn,
        )


def safe_proactive_error(exc: Exception) -> str:
    return safe_failure_reason(str(exc), fallback=f"unexpected {exc.__class__.__name__}")
