from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariel.agency_daemon import AgencyDaemonError
from ariel.attachment_content import AttachmentContentRuntime
from ariel.capability_registry import (
    AGENCY_CAPABILITY_IDS,
    ATTACHMENT_CAPABILITY_IDS,
    CapabilityDefinition,
    DISCORD_CAPABILITY_IDS,
    EMAIL_MUTATION_CAPABILITY_IDS,
    MAPS_CAPABILITY_IDS,
    MEMORY_CAPABILITY_IDS,
    PROACTIVE_CAPABILITY_IDS,
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.config import AppSettings
from ariel.executor import (
    ExecutionResult,
    append_turn_event,
    execute_capability,
    next_turn_event_sequence,
    preflight_capability_execution,
)
from ariel.google_connector import (
    GOOGLE_CAPABILITY_IDS,
    GOOGLE_CONNECTOR_ID,
    GOOGLE_READ_CAPABILITY_IDS,
    GOOGLE_WRITE_CAPABILITY_IDS,
    GoogleCapabilityExecutionResult,
    GoogleConnectorRuntime,
    _decrypt_secret,
    _encrypt_secret,
)
from ariel.memory import run_rememberer, run_retriever
from ariel.persistence import (
    ActionAttemptRecord,
    ActionPrivatePayloadRecord,
    ApprovalRequestRecord,
    ArtifactRecord,
    BackgroundTaskRecord,
    EmailThreadWatchRecord,
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    ProviderEvidenceBlockRecord,
    ProviderEvidenceRecord,
    ProviderWriteReceiptRecord,
    TurnRecord,
    enqueue_background_task,
    to_rfc3339,
)
from ariel.policy_engine import evaluate_proposal
from ariel.redaction import safe_failure_reason
from ariel.weather_state import resolve_weather_location

_SIDE_EFFECT_EXECUTION_LOCK_ID = 24_310_002
_EMAIL_THREAD_WATCH_CAPABILITY_IDS = {
    "cap.email.thread_watch.create",
    "cap.email.thread_watch.cancel",
}
_EMAIL_TRANSIENT_PROVIDER_ERRORS = {
    "google_upstream_429",
    "google_upstream_500",
    "google_upstream_502",
    "google_upstream_503",
    "google_upstream_504",
    "provider_timeout",
    "provider_network_failure",
    "provider_rate_limited",
    "provider_upstream_failure",
    "provider_unreachable",
    "token_expired",
}
_MAX_GMAIL_EVIDENCE_BLOCKS = 12
_MAX_GMAIL_EVIDENCE_BLOCK_CHARS = 2000
_GOOGLE_RECEIPT_CAPABILITY_IDS = GOOGLE_WRITE_CAPABILITY_IDS
_AGENCY_RECEIPT_CAPABILITY_IDS = {"cap.agency.request_pr"}


ModelDeclaredTaintStatus = Literal["missing", "true", "false", "malformed"]
ProposalProvenanceStatus = Literal["clean", "tainted", "ambiguous"]
ProviderWriteReceiptStatus = Literal["executing", "succeeded", "failed", "ambiguous"]


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
class _FunctionCallProcessingContext:
    """Cross-call mutable state shared by every `process_one_call` iteration."""

    inline_results: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    function_call_outputs: list[dict[str, Any]] = field(default_factory=list)
    created_action_attempts: list[ActionAttemptRecord] = field(default_factory=list)
    pending_approval_created: bool = False
    retrieval_requested: bool = False
    retrieval_errors: list[str] = field(default_factory=list)
    retrieval_sources: list[dict[str, Any]] = field(default_factory=list)
    retrieval_capability_ids: set[str] = field(default_factory=set)
    result_runtime_provenance: RuntimeProvenance | None = None
    silent_response: bool = False
    call_ids_by_attempt_id: dict[str, str] = field(default_factory=dict)
    taint_by_attempt_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    interpreter_reason_codes_by_attempt_id: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalDecisionResult:
    approval: ApprovalRequestRecord
    action_attempt: ActionAttemptRecord
    assistant_message: str
    execution_task_id: str | None = None


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
) -> BackgroundTaskRecord:
    return enqueue_background_task(
        db,
        task_type="execute_action_attempt",
        payload={"action_attempt_id": action_attempt.id},
        now=now_fn(),
    )


def _email_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: Any) -> str:
    return _email_hash(json.dumps(jsonable_encoder(value), sort_keys=True, separators=(",", ":")))


def _google_private_input_keys(capability_id: str) -> tuple[str, ...]:
    if capability_id in {"cap.email.draft", "cap.email.send"}:
        return ("body",)
    if capability_id in {"cap.calendar.create_event", "cap.calendar.update_event"}:
        return ("description",)
    return ()


def _private_payload_marker(value: str) -> dict[str, Any]:
    return {
        "redacted": True,
        "digest": _email_hash(value),
        "char_count": len(value),
        "private_payload": True,
    }


def _stored_action_input_payload(
    *,
    capability_id: str,
    input_payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    private_keys = _google_private_input_keys(capability_id)
    if not private_keys:
        return input_payload, None

    stored_payload = dict(input_payload)
    private_payload_required = False
    for key in private_keys:
        value = stored_payload.get(key)
        if isinstance(value, str):
            stored_payload[key] = _private_payload_marker(value)
            private_payload_required = True
    return stored_payload, dict(input_payload) if private_payload_required else None


def _store_action_private_payload(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    private_payload: dict[str, Any],
    google_runtime: GoogleConnectorRuntime,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    db.add(
        ActionPrivatePayloadRecord(
            id=new_id_fn("app"),
            action_attempt_id=action_attempt.id,
            payload_kind="google_provider_write_input",
            payload_digest=_json_digest(private_payload),
            payload_enc=_encrypt_secret(
                plaintext=json.dumps(
                    jsonable_encoder(private_payload),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                secret=google_runtime.encryption_secret,
                key_version=google_runtime.encryption_key_version,
                encryption_keys=google_runtime.encryption_keys,
            ),
            encryption_key_version=google_runtime.encryption_key_version,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()


def _full_action_input_payload(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    google_runtime: GoogleConnectorRuntime | None,
) -> tuple[dict[str, Any] | None, str | None]:
    stored_payload = action_attempt.proposed_input
    if not isinstance(stored_payload, dict):
        return None, "action_input_invalid"

    private_payload_required = False
    for key in _google_private_input_keys(action_attempt.capability_id):
        value = stored_payload.get(key)
        if isinstance(value, str):
            return None, "private_action_payload_not_sealed"
        if isinstance(value, dict) and value.get("private_payload") is True:
            private_payload_required = True

    if not private_payload_required:
        return dict(stored_payload), None
    if google_runtime is None:
        return None, "google_runtime_not_bound"

    private_payload_record = db.scalar(
        select(ActionPrivatePayloadRecord)
        .where(
            ActionPrivatePayloadRecord.action_attempt_id == action_attempt.id,
            ActionPrivatePayloadRecord.payload_kind == "google_provider_write_input",
        )
        .limit(1)
    )
    if private_payload_record is None:
        return None, "private_action_payload_missing"
    try:
        plaintext = _decrypt_secret(
            ciphertext=private_payload_record.payload_enc,
            secret=google_runtime.encryption_secret,
            expected_key_version=google_runtime.encryption_key_version,
            encryption_keys=google_runtime.encryption_keys,
        )
        decoded_payload = json.loads(plaintext)
    except (RuntimeError, ValueError):
        return None, "private_action_payload_unreadable"
    if not isinstance(decoded_payload, dict):
        return None, "private_action_payload_invalid"
    full_payload = jsonable_encoder(decoded_payload)
    if not isinstance(full_payload, dict):
        return None, "private_action_payload_invalid"
    if _json_digest(full_payload) != private_payload_record.payload_digest:
        return None, "private_action_payload_digest_mismatch"
    expected_hash = payload_hash(
        canonical_action_payload(
            capability_id=action_attempt.capability_id,
            input_payload=full_payload,
        )
    )
    if expected_hash != action_attempt.payload_hash:
        return None, "private_action_payload_hash_mismatch"
    return full_payload, None


def _email_idempotency_key(
    *,
    capability_id: str,
    provider_account_id: str,
    client_key: str,
) -> str:
    raw = f"{capability_id}\x1fgoogle\x1f{provider_account_id}\x1f{client_key}"
    return "email:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _email_provider_error_is_retryable(error: str) -> bool:
    return error in _EMAIL_TRANSIENT_PROVIDER_ERRORS or error.startswith("google_upstream_5")


def _current_google_provider_account_id(db: Session) -> str | None:
    connector = db.scalar(
        select(GoogleConnectorRecord)
        .where(GoogleConnectorRecord.id == GOOGLE_CONNECTOR_ID)
        .limit(1)
    )
    if connector is None or connector.status != "connected":
        return None
    account_subject = connector.account_subject
    if account_subject is None or not account_subject.strip():
        return None
    return account_subject


def _email_advisory_lock_id(*parts: str) -> int:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def _acquire_email_advisory_lock(db: Session, *parts: str) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _email_advisory_lock_id(*parts)},
    )


def _email_receipt_result_payload(
    *,
    receipt: ProviderWriteReceiptRecord,
    provider_result: dict[str, Any],
    undo_token: str | None = None,
) -> dict[str, Any]:
    before_state = receipt.before_state if isinstance(receipt.before_state, dict) else {}
    after_state = receipt.after_state if isinstance(receipt.after_state, dict) else {}
    thread_ids = _email_thread_ids_from_state(before_state)
    for thread_id in _email_thread_ids_from_state(after_state):
        if thread_id not in thread_ids:
            thread_ids.append(thread_id)
    payload: dict[str, Any] = {
        "status": "succeeded",
        "email_action_id": receipt.id,
        "capability_id": receipt.capability_id,
        "provider": receipt.provider,
        "provider_account_id": receipt.provider_account_id,
        "message_ids": _email_message_ids_from_state(before_state),
        "thread_ids": thread_ids,
        "before_state": before_state,
        "after_state": after_state,
        "provider_result": provider_result,
        "undo_available": receipt.undo_token_hash is not None,
        "undo_expires_at": to_rfc3339(receipt.undo_expires_at)
        if receipt.undo_expires_at is not None
        else None,
    }
    if undo_token is not None:
        payload["undo_token"] = undo_token
    return payload


def _email_provider_state_lists(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    before_state_raw = output.get("before_state")
    after_state_raw = output.get("after_state")
    if not isinstance(before_state_raw, list):
        raise RuntimeError("email_before_state_missing")
    if not isinstance(after_state_raw, list):
        raise RuntimeError("email_after_state_missing")
    return {"messages": before_state_raw}, {"messages": after_state_raw}


def _email_thread_ids_from_state(state: dict[str, Any]) -> list[str]:
    thread_ids: list[str] = []
    for entry in state.get("messages", []):
        if not isinstance(entry, dict):
            continue
        thread_id = entry.get("thread_id")
        if isinstance(thread_id, str) and thread_id and thread_id not in thread_ids:
            thread_ids.append(thread_id)
    return thread_ids


def _email_message_ids_from_state(state: dict[str, Any]) -> list[str]:
    message_ids: list[str] = []
    for entry in state.get("messages", []):
        if not isinstance(entry, dict):
            continue
        message_id = entry.get("message_id")
        if isinstance(message_id, str) and message_id and message_id not in message_ids:
            message_ids.append(message_id)
    return message_ids


def _redacted_provider_text_marker(value: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["digest"] = _email_hash(value)
        marker["char_count"] = len(value)
    return marker


def _redact_google_action_input_for_event(
    *,
    capability_id: str,
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    redacted = dict(input_payload)
    if capability_id in {"cap.calendar.create_event", "cap.calendar.update_event"}:
        description = redacted.get("description")
        if isinstance(description, str):
            redacted["description"] = _redacted_provider_text_marker(description)
    if capability_id in {"cap.email.draft", "cap.email.send"}:
        body = redacted.get("body")
        if isinstance(body, str):
            redacted["body"] = _redacted_provider_text_marker(body)
    return redacted


def _redact_evidence_blocks(raw_blocks: Any) -> list[dict[str, Any]]:
    redacted_blocks: list[dict[str, Any]] = []
    for block in raw_blocks if isinstance(raw_blocks, list) else []:
        if not isinstance(block, dict):
            continue
        redacted_block = dict(block)
        text = redacted_block.pop("text", None)
        if isinstance(text, str):
            redacted_block["text_redacted"] = True
            redacted_block["text_digest"] = str(redacted_block.get("digest") or _email_hash(text))
            redacted_block["text_char_count"] = len(text)
        redacted_blocks.append(redacted_block)
    return redacted_blocks


def _redact_google_provider_output(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]:
    redacted = jsonable_encoder(output_payload)
    if not isinstance(redacted, dict):
        return {}

    if capability_id == "cap.email.read":
        evidence = redacted.get("evidence")
        if isinstance(evidence, dict):
            evidence["blocks"] = _redact_evidence_blocks(evidence.get("blocks"))
        message = redacted.get("message")
        if isinstance(message, dict):
            for key in ("body", "body_text", "body_html", "snippet"):
                value = message.pop(key, None)
                if isinstance(value, str):
                    message[f"{key}_redacted"] = _redacted_provider_text_marker(value)
        return redacted

    if capability_id == "cap.calendar.list":
        events = redacted.get("events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                event["description_blocks"] = _redact_evidence_blocks(
                    event.get("description_blocks")
                )
                description = event.pop("description", None)
                if isinstance(description, str):
                    event["description_redacted"] = _redacted_provider_text_marker(description)
        return redacted

    if capability_id in {
        "cap.calendar.create_event",
        "cap.calendar.update_event",
        "cap.calendar.respond_to_event",
    }:
        description = redacted.pop("description", None)
        if isinstance(description, str):
            redacted["description_redacted"] = _redacted_provider_text_marker(description)
        event = redacted.get("event")
        if isinstance(event, dict):
            event_description = event.pop("description", None)
            if isinstance(event_description, str):
                event["description_redacted"] = _redacted_provider_text_marker(event_description)
            event["description_blocks"] = _redact_evidence_blocks(event.get("description_blocks"))
        return redacted

    if capability_id in {"cap.email.draft", "cap.email.send"}:
        body = redacted.pop("body", None)
        if isinstance(body, str):
            redacted["body_redacted"] = _redacted_provider_text_marker(body)
        draft = redacted.get("draft")
        if isinstance(draft, dict):
            draft_body = draft.pop("body", None)
            if isinstance(draft_body, str):
                draft["body_redacted"] = _redacted_provider_text_marker(draft_body)
        message = redacted.get("message")
        if isinstance(message, dict):
            message_body = message.pop("body", None)
            if isinstance(message_body, str):
                message["body_redacted"] = _redacted_provider_text_marker(message_body)
        return redacted

    return redacted


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
    output: dict[str, Any] | None = None,
) -> None:
    action_attempt.status = "failed"
    action_attempt.execution_output = output
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
            "output": output,
            "approval_ref": approval.id if approval is not None else None,
        },
        now_fn=now_fn,
        new_id_fn=new_id_fn,
    )


def _execute_memory_capability(
    *,
    session_factory: sessionmaker[Session],
    capability_id: str,
    normalized_input: dict[str, Any],
    session_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    """Run one memory syscall: ``cap.memory.recall`` delegates to the retriever
    subagent, ``cap.memory.remember`` to the rememberer. Both subagents own their
    own sessions and transactions, so they take ``session_factory`` rather than a
    caller-held ``Session``. The main agent reaches memory only through these two
    syscalls; it never touches the fact store, the profile, or the digest
    directly."""
    if settings is None:
        raise RuntimeError("memory_runtime_settings_not_bound")
    if capability_id == "cap.memory.recall":
        facts = run_retriever(
            session_factory=session_factory,
            query=str(normalized_input["query"]),
            source_type="session",
            source_id=session_id,
            settings=settings,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        return {"status": "recalled", "facts": [fact.content for fact in facts]}
    if capability_id == "cap.memory.remember":
        output = run_rememberer(
            session_factory=session_factory,
            settings=settings,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
            trigger="note",
            note=str(normalized_input["note"]),
            session_id=session_id,
        )
        return {
            "status": "remembered",
            "operation_count": len(output.operations),
            "profile_updated": output.profile is not None,
            "digest_updated": output.digest is not None,
        }
    raise RuntimeError("unknown_memory_capability")


def _execute_proactive_capability(
    *,
    db: Session,
    capability_id: str,
    normalized_input: dict[str, Any],
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    """Run one proactive syscall. ``cap.proactive.schedule`` writes a one-shot
    ``agent_wake`` row to ``background_tasks``: ``run_after`` is the requested
    wake time and ``payload`` carries the AI-authored note the future wake
    receives as its prompt. The agent's whole scheduling surface is this one
    syscall; it never touches the queue directly."""
    if capability_id != "cap.proactive.schedule":
        raise RuntimeError("unknown_proactive_capability")
    when = str(normalized_input["when"])
    run_after = datetime.fromisoformat(when.replace("Z", "+00:00"))
    task = enqueue_background_task(
        db,
        task_type="agent_wake",
        payload={"note": str(normalized_input["note"])},
        now=now_fn(),
        run_after=run_after,
    )
    return {"status": "scheduled", "task_id": task.id, "run_after": to_rfc3339(run_after)}


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
_WEB_EXTRACT_RETRIEVAL_CAPABILITY_IDS = {"cap.web.extract"}
_GROUNDED_RETRIEVAL_CAPABILITIES = {
    "cap.search.web",
    "cap.search.news",
    "cap.weather.forecast",
    *MAPS_CAPABILITY_IDS,
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


def _extract_search_source_candidates(
    *,
    output_payload: Any,
    now_fn: Callable[[], datetime],
) -> list[GroundedSourceCandidate]:
    if not isinstance(output_payload, dict):
        return []

    retrieved_at = _parse_rfc3339_timestamp(output_payload.get("retrieved_at")) or now_fn()
    candidates: list[GroundedSourceCandidate] = []

    raw_message = output_payload.get("message")
    if isinstance(raw_message, dict):
        subject_raw = raw_message.get("subject")
        subject = (
            subject_raw.strip() if isinstance(subject_raw, str) and subject_raw.strip() else "email"
        )
        source_raw = raw_message.get("provider_url")
        source = (
            source_raw.strip()
            if isinstance(source_raw, str) and source_raw.strip()
            else "gmail://message"
        )
        published_at = _parse_rfc3339_timestamp(output_payload.get("published_at"))
        raw_evidence = output_payload.get("evidence")
        evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
        raw_blocks = evidence.get("blocks")
        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text_raw = block.get("text")
            if isinstance(text_raw, str) and text_raw.strip():
                snippet = _truncate_snippet(text_raw)
            else:
                block_id = block.get("block_id")
                digest = block.get("digest") or block.get("text_digest")
                if not isinstance(block_id, str) and not isinstance(digest, str):
                    continue
                snippet = _truncate_snippet(
                    "Gmail body evidence recorded: "
                    f"block={block_id if isinstance(block_id, str) else 'unknown'} "
                    f"digest={digest if isinstance(digest, str) else 'unknown'}"
                )
            if not snippet:
                continue
            candidates.append(
                GroundedSourceCandidate(
                    title=subject,
                    source=source,
                    snippet=snippet,
                    retrieved_at=retrieved_at,
                    published_at=published_at,
                )
            )
        return candidates

    raw_thread = output_payload.get("thread")
    raw_evidence = output_payload.get("evidence")
    if isinstance(raw_thread, dict) and isinstance(raw_evidence, dict):
        thread_id_raw = raw_thread.get("thread_id")
        thread_id = (
            thread_id_raw.strip()
            if isinstance(thread_id_raw, str) and thread_id_raw.strip()
            else "thread"
        )
        title = "email thread"
        source = f"gmail://thread/{thread_id}"
        raw_messages_for_title = output_payload.get("messages")
        if isinstance(raw_messages_for_title, list):
            for message in raw_messages_for_title:
                if not isinstance(message, dict):
                    continue
                subject_raw = message.get("subject")
                if isinstance(subject_raw, str) and subject_raw.strip():
                    title = subject_raw.strip()
                source_raw = message.get("provider_url")
                if isinstance(source_raw, str) and source_raw.strip():
                    source = source_raw.strip()
                break
        published_at = _parse_rfc3339_timestamp(output_payload.get("published_at"))
        raw_blocks = raw_evidence.get("blocks")
        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text_raw = block.get("text")
            if isinstance(text_raw, str) and text_raw.strip():
                snippet = _truncate_snippet(text_raw)
            else:
                block_id = block.get("block_id")
                digest = block.get("digest") or block.get("text_digest")
                if not isinstance(block_id, str) and not isinstance(digest, str):
                    continue
                snippet = _truncate_snippet(
                    "Gmail thread body evidence recorded: "
                    f"block={block_id if isinstance(block_id, str) else 'unknown'} "
                    f"digest={digest if isinstance(digest, str) else 'unknown'}"
                )
            if not snippet:
                continue
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

    raw_events = output_payload.get("events")
    if isinstance(raw_events, list):
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            summary_raw = raw_event.get("summary")
            source_raw = raw_event.get("provider_url")
            start_raw = raw_event.get("start")
            end_raw = raw_event.get("end")
            updated_raw = raw_event.get("updated")
            title = (
                summary_raw.strip()
                if isinstance(summary_raw, str) and summary_raw.strip()
                else "calendar event"
            )
            source = (
                source_raw.strip()
                if isinstance(source_raw, str) and source_raw.strip()
                else f"calendar://{raw_event.get('event_id', 'event')}"
            )
            start_value = start_raw.get("value") if isinstance(start_raw, dict) else None
            end_value = end_raw.get("value") if isinstance(end_raw, dict) else None
            snippet_parts = [
                item
                for item in (
                    start_value if isinstance(start_value, str) else None,
                    "to",
                    end_value if isinstance(end_value, str) else None,
                    title,
                )
                if item
            ]
            raw_blocks = raw_event.get("description_blocks")
            blocks = raw_blocks if isinstance(raw_blocks, list) else []
            if blocks and isinstance(blocks[0], dict):
                block_id = blocks[0].get("block_id")
                digest = blocks[0].get("digest") or blocks[0].get("text_digest")
                if isinstance(block_id, str) or isinstance(digest, str):
                    snippet_parts.append(
                        "calendar description evidence recorded: "
                        f"block={block_id if isinstance(block_id, str) else 'unknown'} "
                        f"digest={digest if isinstance(digest, str) else 'unknown'}"
                    )
            candidates.append(
                GroundedSourceCandidate(
                    title=title,
                    source=source,
                    snippet=_truncate_snippet(" ".join(snippet_parts)),
                    retrieved_at=retrieved_at,
                    published_at=_parse_rfc3339_timestamp(updated_raw),
                )
            )
        return candidates

    raw_results = output_payload.get("results")
    if not isinstance(raw_results, list):
        return []
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


def _persist_google_provider_evidence(
    *,
    db: Session,
    capability_id: str,
    output_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    now = now_fn()
    provider_account_id_raw = output_payload.get("provider_account_id")
    provider_account_id = (
        provider_account_id_raw.strip()
        if isinstance(provider_account_id_raw, str) and provider_account_id_raw.strip()
        else None
    )

    def evidence_ref(stored: ProviderEvidenceRecord) -> dict[str, Any]:
        block_ids = db.scalars(
            select(ProviderEvidenceBlockRecord.id)
            .where(ProviderEvidenceBlockRecord.evidence_id == stored.id)
            .order_by(ProviderEvidenceBlockRecord.block_index.asc())
        ).all()
        return {
            "provider_evidence_id": stored.id,
            "read_receipt_id": stored.id,
            "source_kind": stored.source_kind,
            "external_id": stored.external_id,
            "thread_external_id": stored.thread_external_id,
            "block_ids": block_ids,
            "citation_refs": [
                {"kind": "provider_evidence_block", "block_id": block_id} for block_id in block_ids
            ],
        }

    if capability_id == "cap.email.read":
        message = output_payload.get("message")
        evidence = output_payload.get("evidence")
        read_outcome = output_payload.get("read_outcome")
        if not isinstance(read_outcome, dict):
            return []
        read_status = read_outcome.get("status")
        if read_status not in {"ok", "body_too_large", "decode_failed", "no_body"}:
            return []
        raw_blocks = evidence.get("blocks") if isinstance(evidence, dict) else None
        if not isinstance(raw_blocks, list):
            return []
        if read_status == "ok" and not raw_blocks:
            return []
        if read_status != "ok" and raw_blocks:
            return []
        if len(raw_blocks) > _MAX_GMAIL_EVIDENCE_BLOCKS:
            return []
        if read_status == "ok":
            for block in raw_blocks:
                if not isinstance(block, dict):
                    return []
                text = block.get("text")
                if not isinstance(text, str) or not text.strip():
                    return []
                if len(text) > _MAX_GMAIL_EVIDENCE_BLOCK_CHARS:
                    return []
        if not isinstance(evidence, dict):
            return []
        if not isinstance(message, dict) or not isinstance(evidence, dict):
            thread = output_payload.get("thread")
            if not isinstance(thread, dict) or not isinstance(evidence, dict):
                return []
            if provider_account_id is None:
                messages = output_payload.get("messages")
                if isinstance(messages, list):
                    for candidate in messages:
                        if not isinstance(candidate, dict):
                            continue
                        account_raw = candidate.get("provider_account_id")
                        if isinstance(account_raw, str) and account_raw.strip():
                            provider_account_id = account_raw.strip()
                            break
            if provider_account_id is None:
                return []
            thread_id = thread.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                return []
            source_timestamp = _parse_rfc3339_timestamp(output_payload.get("published_at"))
            provider_url = f"https://mail.google.com/mail/u/0/#all/{thread_id}"
            messages = output_payload.get("messages")
            if isinstance(messages, list):
                for candidate in messages:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_url = candidate.get("provider_url")
                    if isinstance(candidate_url, str) and candidate_url.strip():
                        provider_url = candidate_url.strip()
                        break
            provider_object = db.scalar(
                select(GoogleProviderObjectRecord)
                .where(
                    GoogleProviderObjectRecord.provider_account_id == provider_account_id,
                    GoogleProviderObjectRecord.object_type == "gmail_thread",
                    GoogleProviderObjectRecord.external_id == thread_id,
                )
                .with_for_update()
                .limit(1)
            )
            if provider_object is None:
                provider_object = GoogleProviderObjectRecord(
                    id=new_id_fn("gpo"),
                    provider_account_id=provider_account_id,
                    object_type="gmail_thread",
                    external_id=thread_id,
                    thread_external_id=thread_id,
                    calendar_id=None,
                    ical_uid=None,
                    status="active",
                    source_timestamp=source_timestamp,
                    observed_at=now,
                    provider_url=provider_url,
                    metadata_json={
                        "mode": output_payload.get("mode"),
                        "message_count": thread.get("message_count"),
                        "anchor_message_id": thread.get("anchor_message_id"),
                    },
                    content_digest=evidence.get("body_digest")
                    if isinstance(evidence.get("body_digest"), str)
                    else None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(provider_object)
                db.flush()
            else:
                provider_object.thread_external_id = thread_id
                provider_object.status = "active"
                provider_object.source_timestamp = source_timestamp
                provider_object.observed_at = now
                provider_object.provider_url = provider_url
                provider_object.content_digest = (
                    evidence.get("body_digest")
                    if isinstance(evidence.get("body_digest"), str)
                    else provider_object.content_digest
                )
                provider_object.updated_at = now

            content_digest = evidence.get("body_digest")
            if not isinstance(content_digest, str) or not content_digest:
                return []
            existing = db.scalar(
                select(ProviderEvidenceRecord)
                .where(
                    ProviderEvidenceRecord.provider_object_id == provider_object.id,
                    ProviderEvidenceRecord.content_digest == content_digest,
                )
                .limit(1)
            )
            if existing is not None:
                return [evidence_ref(existing)]
            stored = ProviderEvidenceRecord(
                id=new_id_fn("pev"),
                provider_object_id=provider_object.id,
                provider="google",
                provider_account_id=provider_account_id,
                source_kind="gmail_thread",
                external_id=thread_id,
                thread_external_id=thread_id,
                calendar_id=None,
                source_uri=provider_url,
                source_timestamp=source_timestamp,
                content_digest=content_digest,
                metadata_json={
                    "mode": output_payload.get("mode"),
                    "decode_notes": evidence.get("decode_notes", []),
                    "html_security": evidence.get("html_security"),
                    "read_outcome": read_outcome,
                    "anchor_message_id": thread.get("anchor_message_id"),
                },
                taint="provider_untrusted",
                sensitivity="private",
                retention_policy="provider_source",
                extraction_status="pending" if read_status == "ok" else "failed",
                lifecycle_state="available" if read_status == "ok" else "unavailable",
                observed_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(stored)
            db.flush()
            for index, block in enumerate(raw_blocks if isinstance(raw_blocks, list) else []):
                if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                    continue
                kind = block.get("kind")
                db.add(
                    ProviderEvidenceBlockRecord(
                        id=new_id_fn("peb"),
                        evidence_id=stored.id,
                        block_index=index,
                        block_kind=kind
                        if kind in {"body", "quote", "signature", "forwarded"}
                        else "body",
                        text=block["text"],
                        digest=str(block.get("digest") or _email_hash(block["text"])),
                        source_offsets={
                            "block_id": block.get("block_id"),
                            "source_message_id": block.get("source_message_id"),
                            "source_thread_id": block.get("source_thread_id"),
                        },
                        metadata_json={
                            "source_mime_type": block.get("source_mime_type"),
                            "charset": block.get("charset"),
                            "truncated": block.get("truncated"),
                        },
                        created_at=now,
                    )
                )
            return [evidence_ref(stored)]
        if provider_account_id is None:
            account_raw = message.get("provider_account_id")
            if isinstance(account_raw, str) and account_raw.strip():
                provider_account_id = account_raw.strip()
        if provider_account_id is None:
            return []
        message_id = message.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return []
        source_timestamp = _parse_rfc3339_timestamp(output_payload.get("published_at"))
        provider_object = db.scalar(
            select(GoogleProviderObjectRecord)
            .where(
                GoogleProviderObjectRecord.provider_account_id == provider_account_id,
                GoogleProviderObjectRecord.object_type == "gmail_message",
                GoogleProviderObjectRecord.external_id == message_id,
            )
            .with_for_update()
            .limit(1)
        )
        if provider_object is None:
            provider_object = GoogleProviderObjectRecord(
                id=new_id_fn("gpo"),
                provider_account_id=provider_account_id,
                object_type="gmail_message",
                external_id=message_id,
                thread_external_id=message.get("thread_id")
                if isinstance(message.get("thread_id"), str)
                else None,
                calendar_id=None,
                ical_uid=None,
                status="active",
                source_timestamp=source_timestamp,
                observed_at=now,
                provider_url=message.get("provider_url")
                if isinstance(message.get("provider_url"), str)
                else None,
                metadata_json={
                    "subject": message.get("subject"),
                    "subject_key": message.get("subject_key"),
                    "direction": message.get("direction"),
                    "labels": message.get("labels"),
                    "attachments": message.get("attachments"),
                },
                content_digest=message.get("raw_payload_digest")
                if isinstance(message.get("raw_payload_digest"), str)
                else None,
                created_at=now,
                updated_at=now,
            )
            db.add(provider_object)
            db.flush()
        else:
            provider_object.thread_external_id = (
                message.get("thread_id") if isinstance(message.get("thread_id"), str) else None
            )
            provider_object.status = "active"
            provider_object.source_timestamp = source_timestamp
            provider_object.observed_at = now
            provider_object.provider_url = (
                message.get("provider_url")
                if isinstance(message.get("provider_url"), str)
                else None
            )
            provider_object.content_digest = (
                message.get("raw_payload_digest")
                if isinstance(message.get("raw_payload_digest"), str)
                else provider_object.content_digest
            )
            provider_object.updated_at = now

        if read_status != "ok":
            return []

        content_digest = evidence.get("body_digest")
        if not isinstance(content_digest, str) or not content_digest:
            return []
        existing = db.scalar(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.provider_object_id == provider_object.id,
                ProviderEvidenceRecord.content_digest == content_digest,
            )
            .limit(1)
        )
        if existing is not None:
            return [evidence_ref(existing)]
        stored = ProviderEvidenceRecord(
            id=new_id_fn("pev"),
            provider_object_id=provider_object.id,
            provider="google",
            provider_account_id=provider_account_id,
            source_kind="gmail_message",
            external_id=message_id,
            thread_external_id=message.get("thread_id")
            if isinstance(message.get("thread_id"), str)
            else None,
            calendar_id=None,
            source_uri=message.get("provider_url")
            if isinstance(message.get("provider_url"), str)
            else None,
            source_timestamp=source_timestamp,
            content_digest=content_digest,
            metadata_json={
                "decode_notes": evidence.get("decode_notes", []),
                "html_security": evidence.get("html_security"),
                "read_outcome": read_outcome,
            },
            taint="provider_untrusted",
            sensitivity="private",
            retention_policy="provider_source",
            extraction_status="pending" if read_status == "ok" else "failed",
            lifecycle_state="available" if read_status == "ok" else "unavailable",
            observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(stored)
        db.flush()
        for index, block in enumerate(raw_blocks if isinstance(raw_blocks, list) else []):
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            kind = block.get("kind")
            db.add(
                ProviderEvidenceBlockRecord(
                    id=new_id_fn("peb"),
                    evidence_id=stored.id,
                    block_index=index,
                    block_kind=kind
                    if kind in {"body", "quote", "signature", "forwarded"}
                    else "body",
                    text=block["text"],
                    digest=str(block.get("digest") or _email_hash(block["text"])),
                    source_offsets={"block_id": block.get("block_id")},
                    metadata_json={
                        "source_mime_type": block.get("source_mime_type"),
                        "charset": block.get("charset"),
                        "truncated": block.get("truncated"),
                    },
                    created_at=now,
                )
            )
        return [evidence_ref(stored)]

    if capability_id == "cap.calendar.propose_slots":
        if provider_account_id is None:
            return []
        slots = output_payload.get("slots")
        if not isinstance(slots, list):
            return []
        content_payload = {
            "schema_version": output_payload.get("schema_version"),
            "window_start": output_payload.get("window_start"),
            "window_end": output_payload.get("window_end"),
            "duration_minutes": output_payload.get("duration_minutes"),
            "attendees_considered": output_payload.get("attendees_considered"),
            "availability_scope": output_payload.get("availability_scope"),
            "partial": output_payload.get("partial"),
            "partial_reason": output_payload.get("partial_reason"),
            "timezone": output_payload.get("timezone"),
            "source_evidence_refs": output_payload.get("source_evidence_refs"),
            "constraints_used": output_payload.get("constraints_used"),
            "freebusy_diagnostics": output_payload.get("freebusy_diagnostics"),
            "slots": slots,
            "no_slots_reason": output_payload.get("no_slots_reason"),
        }
        content_digest = _email_hash(
            json.dumps(content_payload, sort_keys=True, separators=(",", ":"))
        )
        external_id = f"availability:{content_digest[:32]}"
        source_timestamp = _parse_rfc3339_timestamp(output_payload.get("retrieved_at"))
        provider_object = db.scalar(
            select(GoogleProviderObjectRecord)
            .where(
                GoogleProviderObjectRecord.provider_account_id == provider_account_id,
                GoogleProviderObjectRecord.object_type == "calendar_availability",
                GoogleProviderObjectRecord.external_id == external_id,
            )
            .with_for_update()
            .limit(1)
        )
        if provider_object is None:
            provider_object = GoogleProviderObjectRecord(
                id=new_id_fn("gpo"),
                provider_account_id=provider_account_id,
                object_type="calendar_availability",
                external_id=external_id,
                thread_external_id=None,
                calendar_id=None,
                ical_uid=None,
                status="active",
                source_timestamp=source_timestamp,
                observed_at=now,
                provider_url=f"calendar://availability/{content_digest[:16]}",
                metadata_json=content_payload,
                content_digest=content_digest,
                created_at=now,
                updated_at=now,
            )
            db.add(provider_object)
            db.flush()
        else:
            provider_object.status = "active"
            provider_object.source_timestamp = source_timestamp
            provider_object.observed_at = now
            provider_object.provider_url = f"calendar://availability/{content_digest[:16]}"
            provider_object.metadata_json = content_payload
            provider_object.content_digest = content_digest
            provider_object.updated_at = now

        existing = db.scalar(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.provider_object_id == provider_object.id,
                ProviderEvidenceRecord.content_digest == content_digest,
            )
            .limit(1)
        )
        if existing is not None:
            return [evidence_ref(existing)]
        stored = ProviderEvidenceRecord(
            id=new_id_fn("pev"),
            provider_object_id=provider_object.id,
            provider="google",
            provider_account_id=provider_account_id,
            source_kind="calendar_availability",
            external_id=external_id,
            thread_external_id=None,
            calendar_id=None,
            source_uri=f"calendar://availability/{content_digest[:16]}",
            source_timestamp=source_timestamp,
            content_digest=content_digest,
            metadata_json=content_payload,
            taint="provider_untrusted",
            sensitivity="private",
            retention_policy="provider_source",
            extraction_status="pending",
            lifecycle_state="available",
            observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(stored)
        db.flush()
        block_texts: list[str] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            start = slot.get("start")
            end = slot.get("end")
            start_value = start.get("value") if isinstance(start, dict) else None
            end_value = end.get("value") if isinstance(end, dict) else None
            if not isinstance(start_value, str) or not isinstance(end_value, str):
                continue
            availability_scope = slot.get("availability_scope")
            partial = slot.get("partial")
            block_texts.append(
                f"{start_value} to {end_value} availability_scope={availability_scope} partial={partial}"
            )
        if not block_texts and output_payload.get("no_slots_reason") == "no_slots_available":
            block_texts.append("No matching availability was found in the requested window.")
        for index, text in enumerate(block_texts):
            db.add(
                ProviderEvidenceBlockRecord(
                    id=new_id_fn("peb"),
                    evidence_id=stored.id,
                    block_index=index,
                    block_kind="availability",
                    text=text,
                    digest=_email_hash(text),
                    source_offsets={"slot_index": index},
                    metadata_json={},
                    created_at=now,
                )
            )
        return [evidence_ref(stored)]

    if capability_id != "cap.calendar.list":
        return []
    raw_events = output_payload.get("events")
    stored_refs: list[dict[str, Any]] = []
    for event in raw_events if isinstance(raw_events, list) else []:
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        calendar_id = event.get("calendar_id")
        if not isinstance(calendar_id, str) or not calendar_id:
            continue
        event_provider_account_id_raw = event.get("provider_account_id")
        event_provider_account_id = (
            event_provider_account_id_raw.strip()
            if isinstance(event_provider_account_id_raw, str)
            and event_provider_account_id_raw.strip()
            else provider_account_id
        )
        if event_provider_account_id is None:
            continue
        source_timestamp = _parse_rfc3339_timestamp(event.get("updated"))
        provider_object = db.scalar(
            select(GoogleProviderObjectRecord)
            .where(
                GoogleProviderObjectRecord.provider_account_id == event_provider_account_id,
                GoogleProviderObjectRecord.object_type == "calendar_event",
                GoogleProviderObjectRecord.calendar_id == calendar_id,
                GoogleProviderObjectRecord.external_id == event_id,
            )
            .with_for_update()
            .limit(1)
        )
        if provider_object is None:
            provider_object = GoogleProviderObjectRecord(
                id=new_id_fn("gpo"),
                provider_account_id=event_provider_account_id,
                object_type="calendar_event",
                external_id=event_id,
                thread_external_id=None,
                calendar_id=calendar_id,
                ical_uid=event.get("ical_uid") if isinstance(event.get("ical_uid"), str) else None,
                status="deleted" if event.get("status") == "cancelled" else "active",
                source_timestamp=source_timestamp,
                observed_at=now,
                provider_url=event.get("provider_url")
                if isinstance(event.get("provider_url"), str)
                else None,
                metadata_json={
                    "summary": event.get("summary"),
                    "start": event.get("start"),
                    "end": event.get("end"),
                    "attendees": event.get("attendees"),
                    "organizer": event.get("organizer"),
                },
                content_digest=event.get("raw_payload_digest")
                if isinstance(event.get("raw_payload_digest"), str)
                else None,
                created_at=now,
                updated_at=now,
            )
            db.add(provider_object)
            db.flush()
        else:
            provider_object.status = "deleted" if event.get("status") == "cancelled" else "active"
            provider_object.source_timestamp = source_timestamp
            provider_object.observed_at = now
            provider_object.provider_url = (
                event.get("provider_url") if isinstance(event.get("provider_url"), str) else None
            )
            provider_object.content_digest = (
                event.get("raw_payload_digest")
                if isinstance(event.get("raw_payload_digest"), str)
                else provider_object.content_digest
            )
            provider_object.updated_at = now
        content_digest = event.get("raw_payload_digest")
        if not isinstance(content_digest, str) or not content_digest:
            continue
        existing = db.scalar(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.provider_object_id == provider_object.id,
                ProviderEvidenceRecord.content_digest == content_digest,
            )
            .limit(1)
        )
        if existing is not None:
            stored_refs.append(evidence_ref(existing))
            continue
        event_deleted = event.get("status") == "cancelled"
        stored = ProviderEvidenceRecord(
            id=new_id_fn("pev"),
            provider_object_id=provider_object.id,
            provider="google",
            provider_account_id=event_provider_account_id,
            source_kind="calendar_event",
            external_id=event_id,
            thread_external_id=None,
            calendar_id=calendar_id,
            source_uri=event.get("provider_url")
            if isinstance(event.get("provider_url"), str)
            else None,
            source_timestamp=source_timestamp,
            content_digest=content_digest,
            metadata_json={"summary": event.get("summary"), "status": event.get("status")},
            taint="provider_untrusted",
            sensitivity="private",
            retention_policy="provider_source",
            extraction_status="not_actionable" if event_deleted else "pending",
            lifecycle_state="deleted" if event_deleted else "available",
            observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(stored)
        db.flush()
        raw_blocks = event.get("description_blocks")
        for index, block in enumerate(raw_blocks if isinstance(raw_blocks, list) else []):
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            db.add(
                ProviderEvidenceBlockRecord(
                    id=new_id_fn("peb"),
                    evidence_id=stored.id,
                    block_index=index,
                    block_kind="calendar_description",
                    text=block["text"],
                    digest=str(block.get("digest") or _email_hash(block["text"])),
                    source_offsets={"block_id": block.get("block_id")},
                    metadata_json={"truncated": block.get("truncated")},
                    created_at=now,
                )
            )
        stored_refs.append(evidence_ref(stored))
    return stored_refs


def _provider_write_response_string(
    payload: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    provider_result = payload.get("provider_result")
    if isinstance(provider_result, dict):
        for key in keys:
            value = provider_result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _provider_write_response_timestamp(payload: dict[str, Any]) -> datetime | None:
    for key in ("provider_timestamp", "updated", "created", "sent_at"):
        value = payload.get(key)
        parsed = _parse_rfc3339_timestamp(value)
        if parsed is not None:
            return parsed
    provider_result = payload.get("provider_result")
    if isinstance(provider_result, dict):
        for key in ("provider_timestamp", "updated", "created", "sent_at"):
            parsed = _parse_rfc3339_timestamp(provider_result.get(key))
            if parsed is not None:
                return parsed
    return None


def _latest_approval_ref(db: Session, *, action_attempt_id: str) -> str | None:
    approval_id = db.scalar(
        select(ApprovalRequestRecord.id)
        .where(ApprovalRequestRecord.action_attempt_id == action_attempt_id)
        .order_by(ApprovalRequestRecord.created_at.desc(), ApprovalRequestRecord.id.desc())
        .limit(1)
    )
    return approval_id if isinstance(approval_id, str) else None


def _provider_source_evidence_target_error(
    *,
    action_attempt: ActionAttemptRecord,
    input_payload: dict[str, Any],
    source_evidence: ProviderEvidenceRecord,
) -> str | None:
    if action_attempt.capability_id in EMAIL_MUTATION_CAPABILITY_IDS:
        message_ids = input_payload.get("message_ids")
        if not isinstance(message_ids, list):
            return None
        if source_evidence.source_kind not in {"gmail_message", "gmail_thread"}:
            return "provider_source_evidence_target_mismatch"
        message_id_values = {
            message_id for message_id in message_ids if isinstance(message_id, str)
        }
        if (
            source_evidence.source_kind == "gmail_message"
            and source_evidence.external_id not in message_id_values
        ):
            return "provider_source_evidence_target_mismatch"
    if action_attempt.capability_id in {
        "cap.calendar.update_event",
        "cap.calendar.respond_to_event",
    }:
        if source_evidence.source_kind != "calendar_event":
            return "provider_source_evidence_target_mismatch"
        event_id = input_payload.get("event_id")
        if isinstance(event_id, str) and source_evidence.external_id != event_id:
            return "provider_source_evidence_target_mismatch"
        calendar_id = input_payload.get("calendar_id")
        if (
            isinstance(calendar_id, str)
            and source_evidence.calendar_id is not None
            and source_evidence.calendar_id != calendar_id
        ):
            return "provider_source_evidence_target_mismatch"
    return None


def _provider_write_authority_payload(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    normalized_input: dict[str, Any] | None,
    provider_account_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    input_payload = normalized_input if isinstance(normalized_input, dict) else {}
    authority: dict[str, str] = {}
    for key in ("source_evidence_id", "user_instruction_ref"):
        value = input_payload.get(key)
        if isinstance(value, str) and value.strip():
            authority[key] = value.strip()

    if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS and len(authority) != 1:
        return None, "provider_write_authority_invalid"

    source_evidence_id = authority.get("source_evidence_id")
    if source_evidence_id is not None:
        source_evidence = db.scalar(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.id == source_evidence_id,
                ProviderEvidenceRecord.provider == "google",
                ProviderEvidenceRecord.provider_account_id == provider_account_id,
                ProviderEvidenceRecord.lifecycle_state == "available",
            )
            .limit(1)
        )
        if source_evidence is None:
            return None, "provider_source_evidence_not_found"
        target_error = _provider_source_evidence_target_error(
            action_attempt=action_attempt,
            input_payload=input_payload,
            source_evidence=source_evidence,
        )
        if target_error is not None:
            return None, target_error

    user_instruction_ref = authority.get("user_instruction_ref")
    instruction_turn_id = None
    if user_instruction_ref is not None:
        if not user_instruction_ref.startswith("turn:"):
            return None, "provider_user_instruction_ref_invalid"
        instruction_turn_id = user_instruction_ref.removeprefix("turn:").strip()
        if not instruction_turn_id:
            return None, "provider_user_instruction_ref_invalid"
        instruction_turn = db.scalar(
            select(TurnRecord)
            .where(
                TurnRecord.id == instruction_turn_id,
                TurnRecord.session_id == action_attempt.session_id,
            )
            .limit(1)
        )
        if instruction_turn is None:
            return None, "provider_user_instruction_not_found"

    if not authority:
        return None, None

    authority_payload: dict[str, Any] = {
        "source_type": next(iter(authority)),
        **authority,
        "turn_id": instruction_turn_id or action_attempt.turn_id,
        "action_turn_id": action_attempt.turn_id,
        "session_id": action_attempt.session_id,
    }
    approval_ref = _latest_approval_ref(db, action_attempt_id=action_attempt.id)
    if approval_ref is not None:
        authority_payload["approval_ref"] = approval_ref
    return authority_payload, None


def _record_provider_write_receipt(
    *,
    db: Session,
    provider: str = "google",
    action_attempt: ActionAttemptRecord,
    status: ProviderWriteReceiptStatus,
    normalized_input: dict[str, Any] | None,
    provider_account_id: str | None,
    output_payload: dict[str, Any] | None = None,
    error: str | None = None,
    ambiguity_reason: str | None = None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> ProviderWriteReceiptRecord:
    resolved_provider_account_id = (
        provider_account_id
        or (_current_google_provider_account_id(db) if provider == "google" else None)
        or provider
    )
    idempotency_key = _provider_write_idempotency_key(
        action_attempt=action_attempt,
        provider=provider,
        provider_account_id=resolved_provider_account_id,
        normalized_input=normalized_input,
    )
    receipt = db.scalar(
        select(ProviderWriteReceiptRecord)
        .where(
            ProviderWriteReceiptRecord.provider == provider,
            ProviderWriteReceiptRecord.provider_account_id == resolved_provider_account_id,
            ProviderWriteReceiptRecord.idempotency_key == idempotency_key,
        )
        .with_for_update()
        .limit(1)
    )
    raw_response_payload = dict(output_payload) if output_payload is not None else {}
    if error is not None:
        raw_response_payload["error"] = error
    response_digest = _json_digest(raw_response_payload)
    response_payload = (
        _redact_google_provider_output(
            capability_id=action_attempt.capability_id,
            output_payload=raw_response_payload,
        )
        if provider == "google"
        else dict(raw_response_payload)
    )
    if "undo_token" in response_payload:
        response_payload["undo_token"] = "[redacted]"
    if provider == "google":
        authority_payload, authority_error = _provider_write_authority_payload(
            db=db,
            action_attempt=action_attempt,
            normalized_input=normalized_input,
            provider_account_id=resolved_provider_account_id,
        )
        if authority_payload is not None:
            response_payload["authority"] = authority_payload
        elif authority_error is not None:
            response_payload["authority_error"] = authority_error
    else:
        approval_ref = _latest_approval_ref(db, action_attempt_id=action_attempt.id)
        if approval_ref is not None:
            response_payload["authority"] = {
                "approval_ref": approval_ref,
                "action_turn_id": action_attempt.turn_id,
                "session_id": action_attempt.session_id,
            }
    provider_object_ids = _provider_write_object_ids(
        normalized_input=normalized_input,
        response_payload=response_payload,
    )
    if provider == "google" and authority_payload is not None:
        for key in ("source_evidence_id", "user_instruction_ref"):
            value = authority_payload.get(key)
            if isinstance(value, str):
                provider_object_ids[key] = value
    provider_timestamp = _provider_write_response_timestamp(raw_response_payload)
    provider_etag = _provider_write_response_string(
        raw_response_payload,
        keys=("etag", "provider_etag"),
    )
    provider_history_id = _provider_write_response_string(
        raw_response_payload,
        keys=("history_id", "provider_history_id"),
    )
    now = now_fn()
    if receipt is None:
        receipt = ProviderWriteReceiptRecord(
            id=new_id_fn("pwr"),
            provider=provider,
            provider_account_id=resolved_provider_account_id,
            action_attempt_id=action_attempt.id,
            capability_id=action_attempt.capability_id,
            idempotency_key=idempotency_key,
            status=status,
            provider_object_ids=provider_object_ids,
            request_digest=action_attempt.payload_hash,
            response_payload=response_payload,
            ambiguity_reason=ambiguity_reason if status == "ambiguous" else None,
            provider_timestamp=provider_timestamp,
            provider_etag=provider_etag,
            provider_history_id=provider_history_id,
            response_digest=response_digest,
            created_at=now,
            updated_at=now,
        )
        db.add(receipt)
        db.flush()
        return receipt
    if receipt.capability_id != action_attempt.capability_id:
        raise RuntimeError("idempotency_key_input_mismatch")
    if receipt.request_digest != action_attempt.payload_hash:
        raise RuntimeError("idempotency_key_input_mismatch")
    if receipt.status == "succeeded":
        return receipt
    receipt.status = status
    receipt.provider_object_ids = provider_object_ids
    receipt.response_payload = response_payload
    receipt.ambiguity_reason = ambiguity_reason if status == "ambiguous" else None
    receipt.provider_timestamp = provider_timestamp
    receipt.provider_etag = provider_etag
    receipt.provider_history_id = provider_history_id
    receipt.response_digest = response_digest
    receipt.updated_at = now
    return receipt


def _provider_write_idempotency_key(
    *,
    action_attempt: ActionAttemptRecord,
    provider: str = "google",
    provider_account_id: str,
    normalized_input: dict[str, Any] | None,
) -> str:
    client_key_raw = (
        normalized_input.get("idempotency_key") if isinstance(normalized_input, dict) else None
    )
    if isinstance(client_key_raw, str) and client_key_raw.strip():
        raw = (
            f"{action_attempt.capability_id}\x1f{provider}\x1f"
            f"{provider_account_id}\x1f{client_key_raw.strip()}"
        )
        return "provider-write:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"provider-write:{provider}:{action_attempt.id}:{action_attempt.payload_hash}"


def _provider_write_object_ids(
    *,
    normalized_input: dict[str, Any] | None,
    response_payload: dict[str, Any],
) -> dict[str, Any]:
    provider_result = response_payload.get("provider_result")
    agency_pr = response_payload.get("pr")
    sources = [
        normalized_input if isinstance(normalized_input, dict) else {},
        response_payload,
        provider_result if isinstance(provider_result, dict) else {},
        agency_pr if isinstance(agency_pr, dict) else {},
    ]
    provider_object_ids: dict[str, Any] = {}
    for source in sources:
        for key in (
            "message_id",
            "message_ids",
            "mutated_message_ids",
            "attempted_message_ids",
            "thread_id",
            "thread_ids",
            "draft_id",
            "event_id",
            "calendar_id",
            "etag",
            "updated",
            "ical_uid",
            "file_id",
            "permission_id",
            "grantee_email",
            "role",
            "provider_message_ref",
            "provider_draft_ref",
            "provider_event_ref",
            "job_id",
            "repo_id",
            "invocation_id",
            "worktree_id",
            "pr_number",
            "pr_url",
            "request_id",
        ):
            value = source.get(key)
            if value is not None and key not in provider_object_ids:
                provider_object_ids[key] = value
    return provider_object_ids


def _provider_write_success_identity_error(
    *,
    capability_id: str,
    provider_object_ids: dict[str, Any],
) -> str | None:
    if capability_id == "cap.email.draft":
        if not (
            isinstance(provider_object_ids.get("draft_id"), str)
            or isinstance(provider_object_ids.get("provider_draft_ref"), str)
        ):
            return "provider_write_identity_missing"
        return None
    if capability_id == "cap.email.send":
        if not (
            isinstance(provider_object_ids.get("message_id"), str)
            or isinstance(provider_object_ids.get("provider_message_ref"), str)
        ):
            return "provider_write_identity_missing"
        return None
    if capability_id in EMAIL_MUTATION_CAPABILITY_IDS:
        message_ids = provider_object_ids.get("message_ids")
        if not isinstance(message_ids, list) or not message_ids:
            return "provider_write_identity_missing"
        return None
    if capability_id in {
        "cap.calendar.create_event",
        "cap.calendar.update_event",
        "cap.calendar.respond_to_event",
    }:
        if not isinstance(provider_object_ids.get("event_id"), str) or not isinstance(
            provider_object_ids.get("calendar_id"),
            str,
        ):
            return "provider_write_identity_missing"
        return None
    if capability_id == "cap.drive.share":
        for key in ("file_id", "permission_id", "grantee_email", "role"):
            if not isinstance(provider_object_ids.get(key), str):
                return "provider_write_identity_missing"
        return None
    if capability_id == "cap.agency.request_pr":
        if not (
            isinstance(provider_object_ids.get("pr_url"), str)
            or isinstance(provider_object_ids.get("pr_number"), int)
            or isinstance(provider_object_ids.get("request_id"), str)
        ):
            return "provider_write_identity_missing"
        return None
    return None


def _provider_write_receipt_for_attempt(
    *,
    db: Session,
    provider: str = "google",
    action_attempt: ActionAttemptRecord,
    provider_account_id: str | None,
    normalized_input: dict[str, Any] | None,
) -> ProviderWriteReceiptRecord | None:
    resolved_provider_account_id = (
        provider_account_id
        or (_current_google_provider_account_id(db) if provider == "google" else None)
        or provider
    )
    idempotency_key = _provider_write_idempotency_key(
        action_attempt=action_attempt,
        provider=provider,
        provider_account_id=resolved_provider_account_id,
        normalized_input=normalized_input,
    )
    return db.scalar(
        select(ProviderWriteReceiptRecord)
        .where(
            ProviderWriteReceiptRecord.provider == provider,
            ProviderWriteReceiptRecord.provider_account_id == resolved_provider_account_id,
            ProviderWriteReceiptRecord.idempotency_key == idempotency_key,
        )
        .with_for_update()
        .limit(1)
    )


def _provider_account_id_from_execution_output(
    action_attempt: ActionAttemptRecord,
) -> str | None:
    output = action_attempt.execution_output
    if not isinstance(output, dict):
        return None
    provider_account_id = output.get("provider_account_id")
    if isinstance(provider_account_id, str) and provider_account_id.strip():
        return provider_account_id.strip()
    return None


def _provider_write_failure_receipt_status(
    *,
    error: str,
    output_payload: dict[str, Any] | None,
) -> ProviderWriteReceiptStatus:
    if output_payload is not None:
        return "failed"
    if error == "provider_result_unknown" or _email_provider_error_is_retryable(error):
        return "ambiguous"
    return "failed"


def _append_provider_write_reconcile_unavailable_event(
    *,
    db: Session,
    action_attempt: ActionAttemptRecord,
    receipt: ProviderWriteReceiptRecord,
    reason: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    now = now_fn()
    idempotency_key = f"provider_write_reconcile:{receipt.id}"
    reconcile_task = enqueue_background_task(
        db,
        task_type="provider_write_reconcile_due",
        idempotency_key=idempotency_key,
        payload={
            "provider_write_receipt_id": receipt.id,
            "action_attempt_id": action_attempt.id,
            "receipt_response_digest": receipt.response_digest,
        },
        now=now,
    )
    _append_action_execution_event(
        db=db,
        action_attempt=action_attempt,
        event_type="evt.provider_write.reconcile_unavailable",
        payload_data={
            "action_attempt_id": action_attempt.id,
            "provider_write_receipt_id": receipt.id,
            "status": receipt.status,
            "reason": reason,
            "reconcile_task_enqueued": True,
            "reconcile_task_id": reconcile_task.id,
        },
        now_fn=lambda: now,
        new_id_fn=new_id_fn,
    )


def _response_function_call_output(*, call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(jsonable_encoder(payload), sort_keys=True, separators=(",", ":")),
    }


def process_provider_write_reconcile_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    agency_runtime: Any | None = None,
) -> bool:
    receipt_id = task_payload.get("provider_write_receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        raise RuntimeError("provider_write_reconcile_due missing provider_write_receipt_id")

    agency_prepared: dict[str, Any] | None = None
    indeterminate_reason = "provider_reconcile_requires_provider_specific_probe"

    with session_factory() as db:
        with db.begin():
            receipt = db.scalar(
                select(ProviderWriteReceiptRecord)
                .where(ProviderWriteReceiptRecord.id == receipt_id)
                .with_for_update()
                .limit(1)
            )
            if receipt is None:
                raise RuntimeError("provider_write_receipt_not_found")
            action_attempt = db.scalar(
                select(ActionAttemptRecord)
                .where(ActionAttemptRecord.id == receipt.action_attempt_id)
                .with_for_update()
                .limit(1)
            )
            if action_attempt is None:
                raise RuntimeError("provider_write_action_attempt_not_found")
            if receipt.status != "ambiguous":
                return False

            if (
                agency_runtime is not None
                and receipt.provider == "agency"
                and receipt.capability_id == "cap.agency.request_pr"
            ):
                provider_object_ids = (
                    receipt.provider_object_ids
                    if isinstance(receipt.provider_object_ids, dict)
                    else {}
                )
                response_payload = (
                    receipt.response_payload if isinstance(receipt.response_payload, dict) else {}
                )
                proposed_input = (
                    action_attempt.proposed_input
                    if isinstance(action_attempt.proposed_input, dict)
                    else {}
                )

                def text_value(key: str) -> str | None:
                    for source in (provider_object_ids, response_payload, proposed_input):
                        value = source.get(key)
                        if isinstance(value, str) and value:
                            return value
                    return None

                job_id = text_value("job_id")
                repo_id = text_value("repo_id")
                invocation_id = text_value("invocation_id")
                worktree_id = text_value("worktree_id")
                client_request_id = text_value("client_request_id") or receipt.id
                if (
                    job_id is not None
                    and repo_id is not None
                    and invocation_id is not None
                    and worktree_id is not None
                ):
                    agency_prepared = {
                        "job_id": job_id,
                        "repo_id": repo_id,
                        "invocation_id": invocation_id,
                        "worktree_id": worktree_id,
                        "allow_dirty": bool(proposed_input.get("allow_dirty")),
                        "force_with_lease": bool(proposed_input.get("force_with_lease")),
                        "client_request_id": client_request_id,
                        "land_client_request_id": (
                            text_value("land_client_request_id") or f"{client_request_id}:land"
                        ),
                        "pr_sync_client_request_id": (
                            text_value("pr_sync_client_request_id")
                            or f"{client_request_id}:pr-sync"
                        ),
                    }
                else:
                    indeterminate_reason = "agency_reconcile_identity_missing"

            if agency_prepared is None:
                now = now_fn()
                response_payload = dict(receipt.response_payload)
                response_payload["reconciliation"] = {
                    "status": "indeterminate",
                    "reason": indeterminate_reason,
                    "checked_at": to_rfc3339(now),
                }
                receipt.response_payload = response_payload
                receipt.response_digest = _json_digest(response_payload)
                receipt.updated_at = now
                _append_action_execution_event(
                    db=db,
                    action_attempt=action_attempt,
                    event_type="evt.provider_write.reconcile_unavailable",
                    payload_data={
                        "action_attempt_id": action_attempt.id,
                        "provider_write_receipt_id": receipt.id,
                        "status": receipt.status,
                        "reason": indeterminate_reason,
                        "reconcile_task_enqueued": False,
                    },
                    now_fn=lambda: now,
                    new_id_fn=new_id_fn,
                )
                return True

    assert agency_runtime is not None
    assert agency_prepared is not None
    try:
        agency_result = agency_runtime.request_pr(prepared=agency_prepared)
    except AgencyDaemonError as exc:
        indeterminate_reason = safe_failure_reason(
            str(exc),
            fallback="agency_reconcile_probe_failed",
        )
        with session_factory() as db:
            with db.begin():
                receipt = db.scalar(
                    select(ProviderWriteReceiptRecord)
                    .where(ProviderWriteReceiptRecord.id == receipt_id)
                    .with_for_update()
                    .limit(1)
                )
                if receipt is None:
                    raise RuntimeError("provider_write_receipt_not_found")
                action_attempt = db.scalar(
                    select(ActionAttemptRecord)
                    .where(ActionAttemptRecord.id == receipt.action_attempt_id)
                    .with_for_update()
                    .limit(1)
                )
                if action_attempt is None:
                    raise RuntimeError("provider_write_action_attempt_not_found")
                if receipt.status != "ambiguous":
                    return False
                now = now_fn()
                response_payload = dict(receipt.response_payload)
                response_payload["reconciliation"] = {
                    "status": "indeterminate",
                    "reason": indeterminate_reason,
                    "checked_at": to_rfc3339(now),
                }
                receipt.response_payload = response_payload
                receipt.response_digest = _json_digest(response_payload)
                receipt.updated_at = now
                _append_action_execution_event(
                    db=db,
                    action_attempt=action_attempt,
                    event_type="evt.provider_write.reconcile_unavailable",
                    payload_data={
                        "action_attempt_id": action_attempt.id,
                        "provider_write_receipt_id": receipt.id,
                        "status": receipt.status,
                        "reason": indeterminate_reason,
                        "reconcile_task_enqueued": False,
                    },
                    now_fn=lambda: now,
                    new_id_fn=new_id_fn,
                )
        raise

    with session_factory() as db:
        with db.begin():
            receipt = db.scalar(
                select(ProviderWriteReceiptRecord)
                .where(ProviderWriteReceiptRecord.id == receipt_id)
                .with_for_update()
                .limit(1)
            )
            if receipt is None:
                raise RuntimeError("provider_write_receipt_not_found")
            action_attempt = db.scalar(
                select(ActionAttemptRecord)
                .where(ActionAttemptRecord.id == receipt.action_attempt_id)
                .with_for_update()
                .limit(1)
            )
            if action_attempt is None:
                raise RuntimeError("provider_write_action_attempt_not_found")
            if receipt.status != "ambiguous":
                return False
            normalized_input = (
                action_attempt.proposed_input
                if isinstance(action_attempt.proposed_input, dict)
                else {}
            )
            agency_output = agency_runtime.record_request_pr(
                db=db,
                prepared=agency_prepared,
                result=agency_result,
                now_fn=now_fn,
            )
            agency_output = {
                **agency_output,
                "client_request_id": agency_prepared["client_request_id"],
                "land_client_request_id": agency_prepared["land_client_request_id"],
                "pr_sync_client_request_id": agency_prepared["pr_sync_client_request_id"],
            }
            receipt = _record_provider_write_receipt(
                db=db,
                provider="agency",
                action_attempt=action_attempt,
                status="succeeded",
                normalized_input=normalized_input,
                provider_account_id=agency_prepared["repo_id"],
                output_payload=agency_output,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            action_attempt.status = "succeeded"
            action_attempt.execution_output = receipt.response_payload
            action_attempt.execution_error = None
            action_attempt.updated_at = now_fn()
            _append_action_execution_event(
                db=db,
                action_attempt=action_attempt,
                event_type="evt.action.execution.succeeded",
                payload_data={
                    "action_attempt_id": action_attempt.id,
                    "output": receipt.response_payload,
                    "provider_write_receipt_id": receipt.id,
                    "reconciled": True,
                },
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            return True


def process_one_call(
    *,
    ctx: _FunctionCallProcessingContext,
    function_call_index: int,
    function_call_raw: Any,
    db: Session,
    session_factory: sessionmaker[Session] | None,
    session_id: str,
    turn: TurnRecord,
    approval_ttl_seconds: int,
    approval_actor_id: str,
    add_event: Callable[[str, dict[str, Any]], None],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    runtime_provenance: RuntimeProvenance | None,
    google_runtime: GoogleConnectorRuntime | None,
    execute_google_reads_outside_transaction: bool,
    agency_runtime: Any | None,
    attachment_runtime: AttachmentContentRuntime | None,
    allowed_capability_id_set: set[str],
    settings: AppSettings | None,
) -> None:
    function_call_payload = function_call_raw if isinstance(function_call_raw, dict) else {}
    call_id_raw = function_call_payload.get("call_id")
    call_id = call_id_raw.strip() if isinstance(call_id_raw, str) else ""
    tool_name_raw = function_call_payload.get("tool_name")
    capability_id_raw = function_call_payload.get("capability_id")
    if isinstance(capability_id_raw, str):
        capability_id = capability_id_raw.strip()
    else:
        capability_id = "invalid.capability"
    tool_name = (
        tool_name_raw.strip()
        if isinstance(tool_name_raw, str) and tool_name_raw.strip()
        else capability_id
    )
    if capability_id not in allowed_capability_id_set:
        ctx.blocked_reasons.append(f"{capability_id}: tool_not_in_turn_scope")
        add_event(
            "evt.action.call_denied",
            {
                "call_index": function_call_index,
                "call_id": call_id or None,
                "tool_name": tool_name,
                "capability_id": capability_id,
                "reason": "tool_not_in_turn_scope",
            },
        )
        if call_id:
            ctx.function_call_outputs.append(
                _response_function_call_output(
                    call_id=call_id,
                    payload={
                        "status": "denied",
                        "capability_id": capability_id,
                        "error": "tool_not_in_turn_scope",
                    },
                )
            )
        return
    is_google_capability_call = capability_id in GOOGLE_CAPABILITY_IDS
    is_agency_capability_call = capability_id in AGENCY_CAPABILITY_IDS
    is_discord_capability_call = capability_id in DISCORD_CAPABILITY_IDS
    is_attachment_capability_call = capability_id in ATTACHMENT_CAPABILITY_IDS
    is_memory_capability_call = capability_id in MEMORY_CAPABILITY_IDS
    is_proactive_capability_call = capability_id in PROACTIVE_CAPABILITY_IDS
    is_retrieval_call = capability_id in _GROUNDED_RETRIEVAL_CAPABILITIES
    is_weather_forecast_call = capability_id == "cap.weather.forecast"
    if is_retrieval_call:
        ctx.retrieval_requested = True
        ctx.retrieval_capability_ids.add(capability_id)
    decoded_arguments = function_call_payload.get("input")
    input_payload = (
        jsonable_encoder(decoded_arguments) if isinstance(decoded_arguments, dict) else {}
    )
    if is_weather_forecast_call and set(input_payload.keys()).issubset({"location", "timeframe"}):
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
        pending_approval_exists=ctx.pending_approval_created,
        influenced_by_untrusted_content=taint_payload["influenced_by_untrusted_content"],
        provenance_status=provenance_status,
    )

    now_action = now_fn()
    frozen_input_payload = (
        evaluation.normalized_input if evaluation.normalized_input is not None else input_payload
    )
    stored_input_payload, private_input_payload = _stored_action_input_payload(
        capability_id=capability_id,
        input_payload=frozen_input_payload,
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
        proposed_input=stored_input_payload,
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
    if private_input_payload is not None and google_runtime is not None:
        _store_action_private_payload(
            db=db,
            action_attempt=action_attempt,
            private_payload=private_input_payload,
            google_runtime=google_runtime,
            now=now_action,
            new_id_fn=new_id_fn,
        )
    ctx.created_action_attempts.append(action_attempt)
    if call_id:
        ctx.call_ids_by_attempt_id[action_attempt.id] = call_id
    ctx.taint_by_attempt_id[action_attempt.id] = taint_payload
    add_event(
        "evt.action.proposed",
        {
            "action_attempt_id": action_attempt.id,
            "capability_id": action_attempt.capability_id,
            "input": _redact_google_action_input_for_event(
                capability_id=action_attempt.capability_id,
                input_payload=action_attempt.proposed_input,
            )
            if is_google_capability_call
            else action_attempt.proposed_input,
            "taint": taint_payload,
        },
    )

    if private_input_payload is not None and google_runtime is None:
        action_attempt.status = "rejected"
        action_attempt.policy_decision = "deny"
        action_attempt.policy_reason = "private_action_payload_storage_unavailable"
        action_attempt.updated_at = now_fn()
        ctx.blocked_reasons.append(f"{capability_id}: private_action_payload_storage_unavailable")
        if call_id:
            ctx.function_call_outputs.append(
                _response_function_call_output(
                    call_id=call_id,
                    payload={
                        "status": "blocked",
                        "capability_id": capability_id,
                        "reason": "private_action_payload_storage_unavailable",
                    },
                )
            )
        return

    if evaluation.decision == "deny":
        action_attempt.status = "rejected"
        action_attempt.policy_decision = "deny"
        action_attempt.policy_reason = evaluation.reason
        action_attempt.updated_at = now_fn()
        ctx.blocked_reasons.append(f"{capability_id}: {evaluation.reason}")
        if call_id:
            ctx.function_call_outputs.append(
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
            ctx.retrieval_errors.append(evaluation.reason)
        return

    if evaluation.decision == "requires_approval":
        if evaluation.capability is None or evaluation.normalized_input is None:
            action_attempt.status = "rejected"
            action_attempt.policy_decision = "deny"
            action_attempt.policy_reason = "policy_invariant_violation"
            action_attempt.updated_at = now_fn()
            ctx.blocked_reasons.append(f"{capability_id}: policy_invariant_violation")
            if call_id:
                ctx.function_call_outputs.append(
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
            return

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
            ctx.blocked_reasons.append(f"{capability_id}: {preflight_error}")
            if call_id:
                ctx.function_call_outputs.append(
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
            return

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
        ctx.pending_approval_created = True
        ctx.pending_approvals.append(
            {
                "approval_ref": approval_request.id,
                "capability_id": capability_id,
                "expires_at": to_rfc3339(approval_request.expires_at),
            }
        )
        if call_id:
            ctx.function_call_outputs.append(
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
            ctx.retrieval_errors.append(evaluation.reason)
        return

    if evaluation.capability is None or evaluation.normalized_input is None:
        action_attempt.status = "rejected"
        action_attempt.policy_decision = "deny"
        action_attempt.policy_reason = "policy_invariant_violation"
        action_attempt.updated_at = now_fn()
        ctx.blocked_reasons.append(f"{capability_id}: policy_invariant_violation")
        if call_id:
            ctx.function_call_outputs.append(
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
            ctx.retrieval_errors.append("policy_invariant_violation")
        return

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
        ctx.blocked_reasons.append(f"{capability_id}: {integrity_error}")
        if call_id:
            ctx.function_call_outputs.append(
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
            ctx.retrieval_errors.append(integrity_error)
        return

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
        ctx.blocked_reasons.append(f"{capability_id}: {preflight_error}")
        if call_id:
            ctx.function_call_outputs.append(
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
            ctx.retrieval_errors.append(preflight_error)
        return

    if capability_id == "cap.email.thread_watch.list":
        provider_account_id = _current_google_provider_account_id(db)
        if provider_account_id is None:
            action_attempt.execution_output = None
            action_attempt.execution_error = "google_account_identity_missing"
            action_attempt.status = "failed"
            action_attempt.updated_at = now_fn()
            ctx.blocked_reasons.append(f"{capability_id}: google_account_identity_missing")
            if call_id:
                ctx.function_call_outputs.append(
                    _response_function_call_output(
                        call_id=call_id,
                        payload={
                            "status": "failed",
                            "capability_id": capability_id,
                            "error": "google_account_identity_missing",
                        },
                    )
                )
            add_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": "google_account_identity_missing",
                },
            )
            return
        watches = db.scalars(
            select(EmailThreadWatchRecord)
            .where(
                EmailThreadWatchRecord.provider == "google",
                EmailThreadWatchRecord.provider_account_id == provider_account_id,
                EmailThreadWatchRecord.status == "active",
            )
            .order_by(EmailThreadWatchRecord.deadline.asc(), EmailThreadWatchRecord.id.asc())
            .limit(100)
        ).all()
        output = {
            "status": "listed",
            "watches": [
                {
                    "watch_id": watch.id,
                    "provider_thread_id": watch.provider_thread_id,
                    "anchor_message_id": watch.anchor_message_id,
                    "condition": watch.condition,
                    "deadline": to_rfc3339(watch.deadline),
                    "note": watch.note,
                    "status": watch.status,
                }
                for watch in watches
            ],
        }
        action_attempt.execution_output = output
        action_attempt.execution_error = None
        action_attempt.status = "succeeded"
        action_attempt.updated_at = now_fn()
        ctx.inline_results.append({"capability_id": capability_id, "output": output})
        if call_id:
            ctx.function_call_outputs.append(
                _response_function_call_output(
                    call_id=call_id,
                    payload={"status": "succeeded", "capability_id": capability_id, **output},
                )
            )
        add_event(
            "evt.action.execution.succeeded",
            {
                "action_attempt_id": action_attempt.id,
                "output": output,
            },
        )
        return

    # Memory and proactive syscalls are write_reversible but execute inline:
    # each runs host-side and returns its result (recalled facts, a remember
    # summary, a scheduled task) into the program, so they skip the durable
    # execution queue.
    if (
        evaluation.capability.impact_level != "read"
        and not is_memory_capability_call
        and not is_proactive_capability_call
    ):
        task = _enqueue_action_execution_task(
            db=db,
            action_attempt=action_attempt,
            now_fn=now_fn,
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
        ctx.inline_results.append({"capability_id": capability_id, "output": queued_output})
        if call_id:
            ctx.function_call_outputs.append(
                _response_function_call_output(
                    call_id=call_id,
                    payload={
                        "status": "queued",
                        "capability_id": capability_id,
                        "task_id": task.id,
                    },
                )
            )
        return

    add_event(
        "evt.action.execution.started",
        {
            "action_attempt_id": action_attempt.id,
            "capability_id": capability_id,
        },
    )
    execution_result: ExecutionResult | GoogleCapabilityExecutionResult
    if is_google_capability_call and google_runtime is not None:
        if (
            execute_google_reads_outside_transaction
            and capability_id in GOOGLE_READ_CAPABILITY_IDS
            and session_factory is not None
        ):
            db.flush()
            db.commit()
            google_runtime.refresh_access_token_for_capability(
                session_factory=session_factory,
                capability_id=capability_id,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            with session_factory() as access_db:
                with access_db.begin():
                    (
                        access_token,
                        granted_scopes,
                        provider_account_id,
                        access_failure,
                    ) = google_runtime.prepare_capability_access_without_refresh(
                        db=access_db,
                        capability_id=capability_id,
                        now_fn=now_fn,
                    )
            if access_failure is not None:
                google_execution_result = access_failure
            elif access_token is None:
                google_execution_result = GoogleCapabilityExecutionResult(
                    status="failed",
                    output=None,
                    auth_failure=None,
                    error="token_expired",
                )
            else:
                google_execution_result = google_runtime.execute_provider_capability(
                    capability_id=capability_id,
                    normalized_input=evaluation.normalized_input,
                    access_token=access_token,
                    granted_scopes=granted_scopes,
                    provider_account_id=provider_account_id,
                )
        else:
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
                ctx.function_call_outputs.append(
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
            ctx.blocked_reasons.append(blocked_reason)
            if is_retrieval_call:
                ctx.retrieval_errors.append(error_reason)
            add_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": error_reason,
                    "output": action_attempt.execution_output,
                },
            )
            return
    elif is_google_capability_call:
        execution_result = ExecutionResult(
            status="failed",
            output=None,
            error="google_runtime_not_bound",
        )
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
    elif is_memory_capability_call and session_factory is None:
        execution_result = ExecutionResult(
            status="failed",
            output=None,
            error="memory_runtime_not_bound",
        )
    elif is_memory_capability_call:
        # The prior branch handles a missing session_factory; here it is bound.
        assert session_factory is not None
        try:
            memory_output = _execute_memory_capability(
                session_factory=session_factory,
                capability_id=capability_id,
                normalized_input=evaluation.normalized_input,
                session_id=session_id,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            execution_result = ExecutionResult(
                status="failed",
                output=None,
                error=safe_failure_reason(
                    str(exc),
                    fallback=f"unexpected {exc.__class__.__name__}",
                ),
            )
        else:
            execution_result = ExecutionResult(
                status="succeeded",
                output=memory_output,
                error=None,
            )
    elif is_proactive_capability_call:
        # The schedule syscall writes one agent_wake row to the caller's
        # transaction and returns the task identity into the program.
        try:
            proactive_output = _execute_proactive_capability(
                db=db,
                capability_id=capability_id,
                normalized_input=evaluation.normalized_input,
                now_fn=now_fn,
            )
        except Exception as exc:  # noqa: BLE001
            execution_result = ExecutionResult(
                status="failed",
                output=None,
                error=safe_failure_reason(
                    str(exc),
                    fallback=f"unexpected {exc.__class__.__name__}",
                ),
            )
        else:
            execution_result = ExecutionResult(
                status="succeeded",
                output=proactive_output,
                error=None,
            )
    else:
        _acquire_side_effect_execution_lock(
            db=db,
            impact_level=evaluation.capability.impact_level,
        )
        normalized_for_execution = evaluation.normalized_input
        execution_result = execute_capability(
            capability=evaluation.capability,
            normalized_input=normalized_for_execution,
        )
    if execution_result.status == "succeeded" and execution_result.output is not None:
        if is_google_capability_call and isinstance(execution_result.output, dict):
            output_payload = execution_result.output
            provider_evidence_refs = _persist_google_provider_evidence(
                db=db,
                capability_id=capability_id,
                output_payload=output_payload,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
            if provider_evidence_refs:
                output_payload["provider_evidence_refs"] = provider_evidence_refs
            elif capability_id == "cap.email.read":
                read_outcome_raw = output_payload.get("read_outcome")
                read_outcome = read_outcome_raw if isinstance(read_outcome_raw, dict) else {}
                if read_outcome.get("status") == "ok":
                    execution_result = ExecutionResult(
                        status="failed",
                        output=None,
                        error="gmail_read_evidence_missing",
                    )
            elif capability_id == "cap.calendar.list":
                raw_events = output_payload.get("events")
                if isinstance(raw_events, list) and raw_events:
                    execution_result = ExecutionResult(
                        status="failed",
                        output=None,
                        error="calendar_event_evidence_missing",
                    )
            if execution_result.status == "succeeded":
                execution_result = ExecutionResult(
                    status="succeeded",
                    output=_redact_google_provider_output(
                        capability_id=capability_id,
                        output_payload=output_payload,
                    ),
                    error=None,
                )
        if execution_result.status != "succeeded" or execution_result.output is None:
            error_reason = execution_result.error or "execution_output_missing"
            if call_id:
                ctx.function_call_outputs.append(
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
            ctx.blocked_reasons.append(f"{capability_id}: {error_reason}")
            if is_retrieval_call:
                ctx.retrieval_errors.append(error_reason)
            add_event(
                "evt.action.execution.failed",
                {
                    "action_attempt_id": action_attempt.id,
                    "error": error_reason,
                },
            )
            return
        action_attempt.execution_output = execution_result.output
        action_attempt.execution_error = None
        action_attempt.status = "succeeded"
        action_attempt.updated_at = now_fn()
        _append_reason_codes(
            ctx.interpreter_reason_codes_by_attempt_id,
            action_attempt_id=action_attempt.id,
            reason_codes=_tool_result_interpretation_reason_codes(execution_result.output),
        )
        if is_discord_capability_call:
            ctx.silent_response = True
        else:
            ctx.inline_results.append(
                {
                    "capability_id": capability_id,
                    "output": execution_result.output,
                }
            )
        if call_id:
            ctx.function_call_outputs.append(
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
                    ctx.retrieval_errors.append(read_status)
                provenance_raw = execution_result.output.get("runtime_provenance")
                provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
                attachment_provenance_status = provenance.get("status")
                evidence_raw = provenance.get("evidence")
                if attachment_provenance_status == "tainted" and isinstance(evidence_raw, list):
                    evidence = [item for item in evidence_raw if isinstance(item, dict)]
                    ctx.result_runtime_provenance = RuntimeProvenance(
                        status="tainted",
                        evidence=tuple(evidence),
                    )
            remaining_citations = _MAX_CITED_SOURCES - len(ctx.retrieval_sources)
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
                    ctx.retrieval_sources.extend(persisted_sources)
                else:
                    ctx.retrieval_errors.append(
                        "gmail_read_required"
                        if capability_id == "cap.email.search"
                        else "insufficient_evidence"
                    )
        add_event(
            "evt.action.execution.succeeded",
            {
                "action_attempt_id": action_attempt.id,
                "output": execution_result.output,
            },
        )
        return

    action_attempt.execution_output = None
    action_attempt.execution_error = execution_result.error or "execution_output_missing"
    action_attempt.status = "failed"
    action_attempt.updated_at = now_fn()
    ctx.blocked_reasons.append(
        f"{capability_id}: {execution_result.error or 'execution_output_missing'}"
    )
    if call_id:
        ctx.function_call_outputs.append(
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
        ctx.retrieval_errors.append(action_attempt.execution_error)
    add_event(
        "evt.action.execution.failed",
        {
            "action_attempt_id": action_attempt.id,
            "error": action_attempt.execution_error,
            "output": action_attempt.execution_output,
        },
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
    google_runtime: GoogleConnectorRuntime | None = None,
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

    full_input_payload, full_input_error = _full_action_input_payload(
        db=db,
        action_attempt=action_attempt,
        google_runtime=google_runtime,
    )
    if full_input_error is not None or full_input_payload is None:
        approval.status = "expired"
        approval.decision_reason = full_input_error or "private_action_payload_invalid"
        approval.decided_at = now
        approval.updated_at = now
        action_attempt.status = "failed"
        action_attempt.execution_error = full_input_error or "private_action_payload_invalid"
        action_attempt.policy_reason = "private_action_payload_invalid"
        action_attempt.updated_at = now
        add_approval_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "approval_ref": approval.id,
                "error": action_attempt.execution_error,
            },
        )
        db.flush()
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_PAYLOAD_MISMATCH",
            message="approval payload mismatch",
            details={"approval_ref": approval.id},
            retryable=False,
        )

    expected_hash = payload_hash(
        canonical_action_payload(
            capability_id=action_attempt.capability_id,
            input_payload=full_input_payload,
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

    revalidation_provenance_status: Literal["clean", "tainted"] = "clean"
    revalidation_untrusted = False
    if action_attempt.policy_reason == "taint_escalated_requires_approval":
        revalidation_provenance_status = "tainted"
        revalidation_untrusted = True

    policy = evaluate_proposal(
        capability_id=action_attempt.capability_id,
        input_payload=full_input_payload,
        pending_approval_exists=False,
        influenced_by_untrusted_content=revalidation_untrusted,
        provenance_status=revalidation_provenance_status,
    )
    if policy.decision != "requires_approval":
        approval.status = "expired"
        approval.decision_reason = f"policy_revalidation_{policy.decision}"
        approval.decided_at = now
        approval.updated_at = now
        action_attempt.status = "failed"
        action_attempt.execution_error = "approval policy revalidation failed"
        action_attempt.policy_reason = policy.reason
        action_attempt.updated_at = now
        add_approval_event(
            "evt.action.execution.failed",
            {
                "action_attempt_id": action_attempt.id,
                "approval_ref": approval.id,
                "error": "approval policy revalidation failed",
            },
        )
        db.flush()
        raise ActionRuntimeError(
            status_code=409,
            code="E_APPROVAL_POLICY_CHANGED",
            message="approval policy changed before execution",
            details={
                "approval_ref": approval.id,
                "policy_decision": policy.decision,
                "policy_reason": policy.reason,
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

    execution_task_id = None
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
            normalized_input, input_error = capability.validate_input(full_input_payload)
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
                    )
                    add_approval_event(
                        "evt.action.execution.started",
                        {
                            "action_attempt_id": action_attempt.id,
                            "capability_id": action_attempt.capability_id,
                            "task_id": task.id,
                        },
                    )
                    execution_task_id = task.id

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
        execution_task_id=execution_task_id if action_attempt.status == "executing" else None,
    )


def process_action_execution_task(
    *,
    session_factory: sessionmaker[Session],
    action_attempt_id: str,
    google_runtime: GoogleConnectorRuntime | None,
    agency_runtime: Any | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    settings: AppSettings | None = None,
) -> bool:
    provider_call: tuple[str, dict[str, Any], str, set[str], str | None] | None = None
    email_provider_call: tuple[str, str, dict[str, Any], str, set[str], str] | None = None
    email_undo_prior_receipt_id: str | None = None
    email_lock_parts: tuple[str, ...] | None = None
    agency_call: tuple[str, dict[str, Any], dict[str, Any], str | None] | None = None
    agency_result: (
        tuple[
            str,
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            str | None,
        ]
        | None
    ) = None
    local_call: tuple[CapabilityDefinition, dict[str, Any], str] | None = None
    thread_watch_result: dict[str, Any] | None = None
    execution_result: ExecutionResult | GoogleCapabilityExecutionResult | None = None
    retryable_provider_error: str | None = None
    provider_write_failure_payload: dict[str, Any] | None = None
    provider_write_failure_status: ProviderWriteReceiptStatus | None = None

    if google_runtime is not None:
        with session_factory() as db:
            with db.begin():
                action_attempt = db.get(ActionAttemptRecord, action_attempt_id)
                google_capability_id = (
                    action_attempt.capability_id
                    if action_attempt is not None
                    and action_attempt.status == "executing"
                    and action_attempt.capability_id in GOOGLE_CAPABILITY_IDS
                    else None
                )
        if google_capability_id is not None:
            google_runtime.refresh_access_token_for_capability(
                session_factory=session_factory,
                capability_id=google_capability_id,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
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
                dispatch_capability = get_capability(action_attempt.capability_id)
                dispatch_normalized_input: dict[str, Any] | None = None
                if dispatch_capability is not None:
                    dispatch_full_input, dispatch_full_error = _full_action_input_payload(
                        db=db,
                        action_attempt=action_attempt,
                        google_runtime=google_runtime,
                    )
                    if dispatch_full_error is None and dispatch_full_input is not None:
                        dispatch_normalized_input, _ = dispatch_capability.validate_input(
                            dispatch_full_input
                        )
                dispatch_provider_account_id = _provider_account_id_from_execution_output(
                    action_attempt
                )
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS:
                    existing_receipt = _provider_write_receipt_for_attempt(
                        db=db,
                        action_attempt=action_attempt,
                        provider_account_id=dispatch_provider_account_id,
                        normalized_input=dispatch_normalized_input,
                    )
                    if existing_receipt is not None and existing_receipt.request_digest != (
                        action_attempt.payload_hash
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt is not None and existing_receipt.status == "succeeded":
                        action_attempt.status = "succeeded"
                        action_attempt.execution_output = existing_receipt.response_payload
                        action_attempt.execution_error = None
                        action_attempt.updated_at = now_fn()
                        event_output = dict(existing_receipt.response_payload)
                        if "undo_token" in event_output:
                            event_output["undo_token"] = "[redacted]"
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.action.execution.succeeded",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "output": event_output,
                                "replayed_provider_write_receipt_id": existing_receipt.id,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    existing_error = (
                        existing_receipt.response_payload.get("error")
                        if existing_receipt is not None
                        and isinstance(existing_receipt.response_payload, dict)
                        else None
                    )
                    if (
                        action_attempt.capability_id in EMAIL_MUTATION_CAPABILITY_IDS
                        and existing_receipt is not None
                        and existing_receipt.status == "failed"
                        and isinstance(existing_error, str)
                        and _email_provider_error_is_retryable(existing_error)
                    ):
                        pass
                    else:
                        receipt = existing_receipt
                        if receipt is None or receipt.status != "ambiguous":
                            receipt = _record_provider_write_receipt(
                                db=db,
                                action_attempt=action_attempt,
                                status="ambiguous",
                                normalized_input=dispatch_normalized_input,
                                provider_account_id=dispatch_provider_account_id,
                                error="provider_result_unknown",
                                ambiguity_reason="provider_result_unknown",
                                now_fn=now_fn,
                                new_id_fn=new_id_fn,
                            )
                        _append_provider_write_reconcile_unavailable_event(
                            db=db,
                            action_attempt=action_attempt,
                            receipt=receipt,
                            reason="provider_result_unknown",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="provider_result_unknown",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                elif action_attempt.capability_id in _AGENCY_RECEIPT_CAPABILITY_IDS:
                    receipt_id = (
                        action_attempt.execution_output.get("provider_write_receipt_id")
                        if isinstance(action_attempt.execution_output, dict)
                        else None
                    )
                    existing_receipt = (
                        db.get(ProviderWriteReceiptRecord, receipt_id)
                        if isinstance(receipt_id, str) and receipt_id
                        else None
                    )
                    if existing_receipt is None:
                        existing_receipt = _provider_write_receipt_for_attempt(
                            db=db,
                            provider="agency",
                            action_attempt=action_attempt,
                            provider_account_id=dispatch_provider_account_id,
                            normalized_input=dispatch_normalized_input,
                        )
                    if existing_receipt is not None and existing_receipt.request_digest != (
                        action_attempt.payload_hash
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt is not None and existing_receipt.status == "succeeded":
                        action_attempt.status = "succeeded"
                        action_attempt.execution_output = existing_receipt.response_payload
                        action_attempt.execution_error = None
                        action_attempt.updated_at = now_fn()
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.action.execution.succeeded",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "output": existing_receipt.response_payload,
                                "replayed_provider_write_receipt_id": existing_receipt.id,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt is not None and existing_receipt.status == "failed":
                        existing_error = (
                            existing_receipt.response_payload.get("error")
                            if isinstance(existing_receipt.response_payload, dict)
                            else None
                        )
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=existing_error
                            if isinstance(existing_error, str)
                            else "provider_result_unknown",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    receipt = existing_receipt
                    if receipt is None:
                        receipt = _record_provider_write_receipt(
                            db=db,
                            provider="agency",
                            action_attempt=action_attempt,
                            status="ambiguous",
                            normalized_input=dispatch_normalized_input,
                            provider_account_id=dispatch_provider_account_id,
                            output_payload={"dispatch_state": "provider_call_started"},
                            error="provider_result_unknown",
                            ambiguity_reason="provider_result_unknown",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                    elif receipt.status != "ambiguous":
                        now = now_fn()
                        response_payload = (
                            dict(receipt.response_payload)
                            if isinstance(receipt.response_payload, dict)
                            else {}
                        )
                        response_payload["error"] = "provider_result_unknown"
                        receipt.status = "ambiguous"
                        receipt.ambiguity_reason = "provider_result_unknown"
                        receipt.response_payload = response_payload
                        receipt.response_digest = _json_digest(response_payload)
                        receipt.updated_at = now
                    _append_provider_write_reconcile_unavailable_event(
                        db=db,
                        action_attempt=action_attempt,
                        receipt=receipt,
                        reason="provider_result_unknown",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="provider_result_unknown",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                elif (
                    action_attempt.capability_id not in EMAIL_MUTATION_CAPABILITY_IDS
                    and action_attempt.capability_id not in _EMAIL_THREAD_WATCH_CAPABILITY_IDS
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
            full_input_payload, full_input_error = _full_action_input_payload(
                db=db,
                action_attempt=action_attempt,
                google_runtime=google_runtime,
            )
            if full_input_error is not None or full_input_payload is None:
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error=full_input_error or "private_action_payload_invalid",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True
            normalized_input, input_error = capability.validate_input(full_input_payload)
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

            if action_attempt.capability_id in MEMORY_CAPABILITY_IDS:
                try:
                    memory_output = _execute_memory_capability(
                        session_factory=session_factory,
                        capability_id=action_attempt.capability_id,
                        normalized_input=normalized_input,
                        session_id=action_attempt.session_id,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                        settings=settings,
                    )
                except Exception as exc:  # noqa: BLE001
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error=safe_failure_reason(
                            str(exc),
                            fallback=f"unexpected {exc.__class__.__name__}",
                        ),
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                action_attempt.status = "succeeded"
                action_attempt.execution_output = memory_output
                action_attempt.execution_error = None
                action_attempt.updated_at = now_fn()
                _append_action_execution_event(
                    db=db,
                    action_attempt=action_attempt,
                    event_type="evt.action.execution.succeeded",
                    payload_data={
                        "action_attempt_id": action_attempt.id,
                        "output": memory_output,
                    },
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True

            if action_attempt.capability_id in _EMAIL_THREAD_WATCH_CAPABILITY_IDS:
                now = now_fn()
                if action_attempt.capability_id == "cap.email.thread_watch.create":
                    if google_runtime is None:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="google_runtime_not_bound",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    _, _, prepared_provider_account_id, access_failure = (
                        google_runtime.prepare_capability_access_without_refresh(
                            db=db,
                            capability_id=action_attempt.capability_id,
                            now_fn=now_fn,
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
                    provider_account_id = prepared_provider_account_id
                    if provider_account_id is None:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="google_account_identity_missing",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    _acquire_email_advisory_lock(
                        db,
                        "email_thread_watch",
                        "google",
                        provider_account_id,
                        str(normalized_input["provider_thread_id"]),
                    )
                    idempotency_key = _email_idempotency_key(
                        capability_id=action_attempt.capability_id,
                        provider_account_id=provider_account_id,
                        client_key=str(normalized_input["idempotency_key"]),
                    )
                    watch = db.scalar(
                        select(EmailThreadWatchRecord)
                        .where(EmailThreadWatchRecord.idempotency_key == idempotency_key)
                        .with_for_update()
                        .limit(1)
                    )
                    if watch is None:
                        watch = EmailThreadWatchRecord(
                            id=new_id_fn("etw"),
                            provider="google",
                            provider_account_id=provider_account_id,
                            provider_thread_id=str(normalized_input["provider_thread_id"]),
                            anchor_message_id=str(normalized_input["anchor_message_id"]),
                            condition=str(normalized_input["condition"]),
                            deadline=datetime.fromisoformat(
                                str(normalized_input["deadline"]).replace("Z", "+00:00")
                            ),
                            note=str(normalized_input["note"]),
                            status="active",
                            idempotency_key=idempotency_key,
                            cancel_idempotency_key=None,
                            created_by_action_attempt_id=action_attempt.id,
                            matched_message_id=None,
                            matched_at=None,
                            canceled_at=None,
                            completed_at=None,
                            created_at=now,
                            updated_at=now,
                        )
                        db.add(watch)
                        db.flush()
                    elif (
                        watch.provider_thread_id != str(normalized_input["provider_thread_id"])
                        or watch.anchor_message_id != str(normalized_input["anchor_message_id"])
                        or watch.condition != str(normalized_input["condition"])
                        or to_rfc3339(watch.deadline) != str(normalized_input["deadline"])
                        or watch.note != str(normalized_input["note"])
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    thread_watch_result = {
                        "status": watch.status,
                        "watch_id": watch.id,
                        "provider_thread_id": watch.provider_thread_id,
                        "anchor_message_id": watch.anchor_message_id,
                        "condition": watch.condition,
                        "deadline": to_rfc3339(watch.deadline),
                        "note": watch.note,
                    }
                else:
                    provider_account_id = _current_google_provider_account_id(db)
                    if provider_account_id is None:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="google_account_identity_missing",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    watch = db.scalar(
                        select(EmailThreadWatchRecord)
                        .where(
                            EmailThreadWatchRecord.id == normalized_input["watch_id"],
                            EmailThreadWatchRecord.provider == "google",
                            EmailThreadWatchRecord.provider_account_id == provider_account_id,
                        )
                        .with_for_update()
                        .limit(1)
                    )
                    if watch is None:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="thread_watch_not_found",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    _acquire_email_advisory_lock(
                        db,
                        "email_thread_watch",
                        watch.provider,
                        watch.provider_account_id,
                        watch.provider_thread_id,
                    )
                    cancel_idempotency_key = _email_idempotency_key(
                        capability_id=action_attempt.capability_id,
                        provider_account_id=watch.provider_account_id,
                        client_key=str(normalized_input["idempotency_key"]),
                    )
                    existing_cancel = db.scalar(
                        select(EmailThreadWatchRecord)
                        .where(
                            EmailThreadWatchRecord.cancel_idempotency_key == cancel_idempotency_key
                        )
                        .with_for_update()
                        .limit(1)
                    )
                    if existing_cancel is not None and existing_cancel.id != watch.id:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if watch.status == "canceled":
                        if watch.cancel_idempotency_key != cancel_idempotency_key:
                            _fail_action_execution(
                                db=db,
                                action_attempt=action_attempt,
                                error="idempotency_key_input_mismatch",
                                now_fn=now_fn,
                                new_id_fn=new_id_fn,
                            )
                            return True
                    elif watch.status == "active":
                        watch.status = "canceled"
                        watch.canceled_at = now
                        watch.cancel_idempotency_key = cancel_idempotency_key
                        watch.updated_at = now
                    thread_watch_result = {
                        "status": watch.status,
                        "watch_id": watch.id,
                        "provider_thread_id": watch.provider_thread_id,
                        "condition": watch.condition,
                    }
                action_attempt.status = "succeeded"
                action_attempt.execution_output = thread_watch_result
                action_attempt.execution_error = None
                action_attempt.updated_at = now
                _append_action_execution_event(
                    db=db,
                    action_attempt=action_attempt,
                    event_type="evt.action.execution.succeeded",
                    payload_data={
                        "action_attempt_id": action_attempt.id,
                        "output": thread_watch_result,
                    },
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                return True

            if action_attempt.capability_id in EMAIL_MUTATION_CAPABILITY_IDS:
                if google_runtime is None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="google_runtime_not_bound",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                access_token, granted_scopes, provider_account_id, access_failure = (
                    google_runtime.prepare_capability_access_without_refresh(
                        db=db,
                        capability_id=action_attempt.capability_id,
                        now_fn=now_fn,
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
                if provider_account_id is None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="google_account_identity_missing",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                _, authority_error = _provider_write_authority_payload(
                    db=db,
                    action_attempt=action_attempt,
                    normalized_input=normalized_input,
                    provider_account_id=provider_account_id,
                )
                if authority_error is not None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error=authority_error,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                existing_receipt = _provider_write_receipt_for_attempt(
                    db=db,
                    action_attempt=action_attempt,
                    provider_account_id=provider_account_id,
                    normalized_input=normalized_input,
                )
                if existing_receipt is not None:
                    if existing_receipt.request_digest != action_attempt.payload_hash:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt.status in {"succeeded", "undone"}:
                        action_attempt.status = "succeeded"
                        action_attempt.execution_output = existing_receipt.response_payload
                        action_attempt.execution_error = None
                        action_attempt.updated_at = now_fn()
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.action.execution.succeeded",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "output": existing_receipt.response_payload,
                                "replayed_provider_write_receipt_id": existing_receipt.id,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    existing_error = (
                        existing_receipt.response_payload.get("error")
                        if isinstance(existing_receipt.response_payload, dict)
                        else None
                    )
                    if existing_receipt.status == "executing":
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="provider_write_in_progress",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if not (
                        existing_receipt.status == "failed"
                        and isinstance(existing_error, str)
                        and _email_provider_error_is_retryable(existing_error)
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=existing_error
                            if isinstance(existing_error, str)
                            else "provider_write_failed",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                if action_attempt.capability_id == "cap.email.undo":
                    undo_token_hash = _email_hash(str(normalized_input["undo_token"]))
                    prior_receipt = db.scalar(
                        select(ProviderWriteReceiptRecord)
                        .where(ProviderWriteReceiptRecord.undo_token_hash == undo_token_hash)
                        .with_for_update()
                        .limit(1)
                    )
                    prior_before_messages = (
                        prior_receipt.before_state.get("messages")
                        if prior_receipt is not None
                        and isinstance(prior_receipt.before_state, dict)
                        else None
                    )
                    if (
                        prior_receipt is None
                        or prior_receipt.status != "succeeded"
                        or prior_receipt.undo_expires_at is None
                        or prior_receipt.undo_expires_at <= now_fn()
                        or prior_receipt.provider != "google"
                        or prior_receipt.provider_account_id != provider_account_id
                        or not isinstance(prior_before_messages, list)
                        or not prior_before_messages
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="undo_unavailable",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    email_undo_prior_receipt_id = prior_receipt.id
                    provider_message_ids = _email_message_ids_from_state(
                        {"messages": prior_before_messages}
                    )
                    provider_input = {
                        "message_ids": provider_message_ids,
                        "before_state": prior_before_messages,
                        "idempotency_key": normalized_input["idempotency_key"],
                    }
                else:
                    provider_input = dict(normalized_input)
                    provider_message_ids = list(normalized_input["message_ids"])
                    captured_before_messages = (
                        existing_receipt.before_state.get("messages")
                        if existing_receipt is not None
                        and isinstance(existing_receipt.before_state, dict)
                        else None
                    )
                    if isinstance(captured_before_messages, list):
                        provider_input["before_state"] = captured_before_messages
                email_lock_parts = (
                    "email_mutation",
                    "google",
                    provider_account_id,
                    ",".join(sorted(provider_message_ids)),
                )
                _acquire_email_advisory_lock(db, *email_lock_parts)
                receipt = _record_provider_write_receipt(
                    db=db,
                    action_attempt=action_attempt,
                    status="executing",
                    normalized_input=normalized_input,
                    provider_account_id=provider_account_id,
                    output_payload={"dispatch_state": "provider_call_started"},
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
                action_attempt.execution_output = {
                    "dispatch_state": "provider_call_started",
                    "provider_account_id": provider_account_id,
                }
                action_attempt.updated_at = now_fn()
                email_provider_call = (
                    receipt.id,
                    action_attempt.capability_id,
                    provider_input,
                    access_token,
                    granted_scopes,
                    provider_account_id,
                )
            elif action_attempt.capability_id in GOOGLE_CAPABILITY_IDS:
                if google_runtime is None:
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="google_runtime_not_bound",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                access_token, granted_scopes, provider_account_id, access_failure = (
                    google_runtime.prepare_capability_access_without_refresh(
                        db=db,
                        capability_id=action_attempt.capability_id,
                        now_fn=now_fn,
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
                if (
                    action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS
                    and provider_account_id is None
                ):
                    _fail_action_execution(
                        db=db,
                        action_attempt=action_attempt,
                        error="google_account_identity_missing",
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    return True
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS:
                    assert provider_account_id is not None
                    existing_receipt = _provider_write_receipt_for_attempt(
                        db=db,
                        action_attempt=action_attempt,
                        provider_account_id=provider_account_id,
                        normalized_input=normalized_input,
                    )
                    if existing_receipt is not None and existing_receipt.request_digest != (
                        action_attempt.payload_hash
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt is not None and existing_receipt.status == "succeeded":
                        action_attempt.status = "succeeded"
                        action_attempt.execution_output = existing_receipt.response_payload
                        action_attempt.execution_error = None
                        action_attempt.updated_at = now_fn()
                        event_output = dict(existing_receipt.response_payload)
                        if "undo_token" in event_output:
                            event_output["undo_token"] = "[redacted]"
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.action.execution.succeeded",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "output": event_output,
                                "replayed_provider_write_receipt_id": existing_receipt.id,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt is not None:
                        if existing_receipt.status == "executing":
                            _fail_action_execution(
                                db=db,
                                action_attempt=action_attempt,
                                error="provider_write_in_progress",
                                now_fn=now_fn,
                                new_id_fn=new_id_fn,
                            )
                            return True
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=str(
                                existing_receipt.response_payload.get("error")
                                or "provider_write_failed"
                            ),
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    _, authority_error = _provider_write_authority_payload(
                        db=db,
                        action_attempt=action_attempt,
                        normalized_input=normalized_input,
                        provider_account_id=provider_account_id,
                    )
                    if authority_error is not None:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=authority_error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                action_attempt.execution_output = {
                    "dispatch_state": "provider_call_started",
                    "provider_account_id": provider_account_id,
                }
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS:
                    _record_provider_write_receipt(
                        db=db,
                        action_attempt=action_attempt,
                        status="executing",
                        normalized_input=normalized_input,
                        provider_account_id=provider_account_id,
                        output_payload={"dispatch_state": "provider_call_started"},
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                action_attempt.updated_at = now_fn()
                provider_call = (
                    action_attempt.capability_id,
                    normalized_input,
                    access_token,
                    granted_scopes,
                    provider_account_id,
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
                agency_receipt_id: str | None = None
                if action_attempt.capability_id == "cap.agency.request_pr":
                    try:
                        agency_context = agency_runtime.prepare_request_pr(
                            db=db,
                            input_payload=normalized_input,
                            action_attempt_id=action_attempt.id,
                        )
                    except AgencyDaemonError as exc:
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=str(exc) or "agency_prepare_failed",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    existing_receipt = _provider_write_receipt_for_attempt(
                        db=db,
                        provider="agency",
                        action_attempt=action_attempt,
                        provider_account_id=str(agency_context["repo_id"]),
                        normalized_input=normalized_input,
                    )
                    if existing_receipt is not None and existing_receipt.request_digest != (
                        action_attempt.payload_hash
                    ):
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error="idempotency_key_input_mismatch",
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    if existing_receipt is not None and existing_receipt.status == "succeeded":
                        action_attempt.status = "succeeded"
                        action_attempt.execution_output = existing_receipt.response_payload
                        action_attempt.execution_error = None
                        action_attempt.updated_at = now_fn()
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.action.execution.succeeded",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "output": existing_receipt.response_payload,
                                "replayed_provider_write_receipt_id": existing_receipt.id,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                    receipt = _record_provider_write_receipt(
                        db=db,
                        provider="agency",
                        action_attempt=action_attempt,
                        status="executing",
                        normalized_input=normalized_input,
                        provider_account_id=str(agency_context["repo_id"]),
                        output_payload={
                            "dispatch_state": "provider_call_started",
                            "client_request_id": str(agency_context["client_request_id"]),
                        },
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    agency_receipt_id = receipt.id
                    agency_context["client_request_id"] = receipt.id
                    agency_context["land_client_request_id"] = f"{receipt.id}:land"
                    agency_context["pr_sync_client_request_id"] = f"{receipt.id}:pr-sync"
                    receipt.response_payload = {
                        "dispatch_state": "provider_call_started",
                        "job_id": agency_context["job_id"],
                        "repo_id": agency_context["repo_id"],
                        "invocation_id": agency_context["invocation_id"],
                        "worktree_id": agency_context["worktree_id"],
                        "client_request_id": receipt.id,
                        "land_client_request_id": agency_context["land_client_request_id"],
                        "pr_sync_client_request_id": agency_context["pr_sync_client_request_id"],
                    }
                    receipt.provider_object_ids = _provider_write_object_ids(
                        normalized_input=normalized_input,
                        response_payload=receipt.response_payload,
                    )
                    receipt.response_digest = _json_digest(receipt.response_payload)
                    receipt.updated_at = now_fn()
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
                if agency_receipt_id is not None:
                    action_attempt.execution_output["provider_write_receipt_id"] = agency_receipt_id
                action_attempt.updated_at = now_fn()
                agency_call = (
                    action_attempt.capability_id,
                    normalized_input,
                    agency_context,
                    agency_receipt_id,
                )
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
                if action_attempt.impact_level != "read":
                    _acquire_side_effect_execution_lock(
                        db=db,
                        impact_level=action_attempt.impact_level,
                    )
                local_call = (
                    capability,
                    normalized_input,
                    action_attempt.id,
                )

    if email_provider_call is not None:
        (
            receipt_id,
            capability_id,
            normalized_input,
            access_token,
            granted_scopes,
            provider_account_id,
        ) = email_provider_call
        assert google_runtime is not None
        lock_db: Session | None = None
        lock_id: int | None = None
        if email_lock_parts is not None:
            lock_db = session_factory()
            bind = lock_db.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                lock_id = _email_advisory_lock_id(*email_lock_parts)
                lock_db.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
                lock_db.commit()
            else:
                lock_db.close()
                lock_db = None
        try:
            if capability_id != "cap.email.undo" and "before_state" not in normalized_input:
                message_ids = normalized_input["message_ids"]
                before_state_output = (
                    google_runtime.workspace_provider.email_get_message_label_state(
                        access_token=access_token,
                        normalized_input={"message_ids": message_ids},
                    )
                )
                before_messages_raw = before_state_output.get("state")
                if not isinstance(before_messages_raw, list):
                    raise RuntimeError("email_before_state_missing")
                before_message_ids: list[str] = []
                for before_message in before_messages_raw:
                    if not isinstance(before_message, dict):
                        raise RuntimeError("email_before_state_missing")
                    before_message_id = before_message.get("message_id")
                    if not isinstance(before_message_id, str) or not before_message_id:
                        raise RuntimeError("email_before_state_missing")
                    before_message_ids.append(before_message_id)
                if sorted(before_message_ids) != sorted(message_ids):
                    raise RuntimeError("email_before_state_missing")
                before_messages = before_messages_raw
                normalized_input = {**normalized_input, "before_state": before_messages}
                with session_factory() as db:
                    with db.begin():
                        receipt = db.scalar(
                            select(ProviderWriteReceiptRecord)
                            .where(ProviderWriteReceiptRecord.id == receipt_id)
                            .with_for_update()
                            .limit(1)
                        )
                        if receipt is not None:
                            receipt.before_state = {"messages": before_messages}
                            receipt.updated_at = now_fn()
            email_provider_call = (
                receipt_id,
                capability_id,
                normalized_input,
                access_token,
                granted_scopes,
                provider_account_id,
            )
            execution_result = google_runtime.execute_provider_capability(
                capability_id=capability_id,
                normalized_input=normalized_input,
                access_token=access_token,
                granted_scopes=granted_scopes,
                provider_account_id=provider_account_id,
            )
        finally:
            if lock_db is not None and lock_id is not None:
                lock_db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": lock_id},
                )
                lock_db.commit()
                lock_db.close()
    elif provider_call is not None:
        capability_id, normalized_input, access_token, granted_scopes, provider_account_id = (
            provider_call
        )
        assert google_runtime is not None
        execution_result = google_runtime.execute_provider_capability(
            capability_id=capability_id,
            normalized_input=normalized_input,
            access_token=access_token,
            granted_scopes=granted_scopes,
            provider_account_id=provider_account_id,
        )
    elif agency_call is not None:
        capability_id, normalized_input, agency_context, agency_receipt_id = agency_call
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
        except AgencyDaemonError as exc:
            execution_result = ExecutionResult(status="failed", output=None, error=str(exc))
        else:
            if result is not None:
                agency_result = (
                    capability_id,
                    normalized_input,
                    agency_context,
                    result,
                    agency_receipt_id,
                )
                execution_result = ExecutionResult(status="succeeded", output={}, error=None)
    elif local_call is not None:
        capability, normalized_input, action_attempt_id = local_call
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
                capability_id, normalized_input, agency_context, result, agency_receipt_id = (
                    agency_result
                )
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
                    agency_output = agency_runtime.record_request_pr(
                        db=db,
                        prepared=agency_context,
                        result=result,
                        now_fn=now_fn,
                    )
                    if agency_receipt_id is not None:
                        agency_output = {
                            **agency_output,
                            "client_request_id": agency_receipt_id,
                            "land_client_request_id": f"{agency_receipt_id}:land",
                            "pr_sync_client_request_id": f"{agency_receipt_id}:pr-sync",
                        }
                    execution_result = ExecutionResult(
                        status="succeeded",
                        output=agency_output,
                        error=None,
                    )
                    receipt = _record_provider_write_receipt(
                        db=db,
                        provider="agency",
                        action_attempt=action_attempt,
                        status="succeeded",
                        normalized_input=normalized_input,
                        provider_account_id=str(agency_context["repo_id"]),
                        output_payload=agency_output,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    if agency_receipt_id is not None and receipt.id != agency_receipt_id:
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.provider_write.receipt_reconciled",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "expected_provider_write_receipt_id": agency_receipt_id,
                                "provider_write_receipt_id": receipt.id,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
            if email_provider_call is not None:
                receipt_id, capability_id, provider_input, _, _, provider_account_id = (
                    email_provider_call
                )
                receipt = db.scalar(
                    select(ProviderWriteReceiptRecord)
                    .where(ProviderWriteReceiptRecord.id == receipt_id)
                    .with_for_update()
                    .limit(1)
                )
                if receipt is None:
                    raise RuntimeError("provider_write_receipt_not_found")
                if execution_result.status == "succeeded" and execution_result.output is not None:
                    try:
                        before_state, after_state = _email_provider_state_lists(
                            execution_result.output
                        )
                    except RuntimeError as exc:
                        if str(exc) != "email_before_state_missing":
                            raise
                        captured_before_messages = provider_input.get("before_state")
                        if not isinstance(captured_before_messages, list):
                            raise
                        before_state = {"messages": captured_before_messages}
                        after_state_raw = execution_result.output.get("after_state")
                        after_state = {
                            "messages": after_state_raw if isinstance(after_state_raw, list) else []
                        }
                        provider_result_raw = execution_result.output.get("provider_result")
                        if isinstance(provider_result_raw, dict):
                            provider_result_raw["before_state_error"] = str(exc)
                        else:
                            execution_result.output["provider_result"] = {
                                "before_state_error": str(exc)
                            }
                    receipt.before_state = before_state
                    receipt.after_state = after_state
                    receipt.updated_at = now_fn()
                    provider_result_raw = execution_result.output.get("provider_result")
                    provider_result: dict[str, Any] = (
                        provider_result_raw
                        if isinstance(provider_result_raw, dict)
                        else execution_result.output
                    )
                    provider_status = execution_result.output.get("status")
                    provider_error: str | None = None
                    provider_error_raw = provider_result.get("error")
                    if isinstance(provider_error_raw, str) and provider_error_raw:
                        provider_error = provider_error_raw
                    after_state_error_raw = provider_result.get("after_state_error")
                    if (
                        provider_error is None
                        and isinstance(after_state_error_raw, str)
                        and after_state_error_raw
                    ):
                        provider_error = after_state_error_raw
                    if provider_error is None and provider_status in {
                        "failed",
                        "partially_failed",
                    }:
                        provider_error = str(provider_status)
                    if provider_error is not None:
                        provider_write_failure_payload = dict(execution_result.output)
                        provider_write_failure_status = "failed"
                        if _email_provider_error_is_retryable(provider_error):
                            retryable_provider_error = provider_error
                        execution_result = ExecutionResult(
                            status="failed",
                            output=None,
                            error=provider_error,
                        )
                    else:
                        undo_token: str | None = None
                        if capability_id != "cap.email.undo":
                            undo_token = secrets.token_urlsafe(32)
                            receipt.undo_token_hash = _email_hash(undo_token)
                            receipt.undo_expires_at = now_fn() + timedelta(days=30)
                        elif email_undo_prior_receipt_id is not None:
                            prior_receipt = db.scalar(
                                select(ProviderWriteReceiptRecord)
                                .where(ProviderWriteReceiptRecord.id == email_undo_prior_receipt_id)
                                .with_for_update()
                                .limit(1)
                            )
                            if prior_receipt is not None and prior_receipt.status == "succeeded":
                                prior_receipt.status = "undone"
                                prior_receipt.updated_at = now_fn()
                        execution_result = ExecutionResult(
                            status="succeeded",
                            output=_email_receipt_result_payload(
                                receipt=receipt,
                                provider_result=provider_result,
                                undo_token=undo_token,
                            ),
                            error=None,
                        )
                else:
                    error = execution_result.error or "execution_output_missing"
                    if (
                        isinstance(execution_result, GoogleCapabilityExecutionResult)
                        and execution_result.auth_failure is not None
                    ):
                        error = execution_result.auth_failure.failure_class
                    provider_write_failure_status = _provider_write_failure_receipt_status(
                        error=error,
                        output_payload=None,
                    )
                    if (
                        provider_write_failure_status != "ambiguous"
                        and _email_provider_error_is_retryable(error)
                    ):
                        retryable_provider_error = error
            if retryable_provider_error is not None:
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS:
                    receipt_status = provider_write_failure_status or (
                        _provider_write_failure_receipt_status(
                            error=retryable_provider_error,
                            output_payload=provider_write_failure_payload,
                        )
                    )
                    receipt = _record_provider_write_receipt(
                        db=db,
                        action_attempt=action_attempt,
                        status=receipt_status,
                        normalized_input=(
                            provider_input if email_provider_call is not None else None
                        ),
                        provider_account_id=(
                            provider_account_id if email_provider_call is not None else None
                        ),
                        output_payload=provider_write_failure_payload,
                        error=retryable_provider_error,
                        ambiguity_reason=retryable_provider_error
                        if receipt_status == "ambiguous"
                        else None,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    if receipt_status == "ambiguous":
                        _append_provider_write_reconcile_unavailable_event(
                            db=db,
                            action_attempt=action_attempt,
                            receipt=receipt,
                            reason=retryable_provider_error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=retryable_provider_error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        retryable_provider_error = None
                    else:
                        action_attempt.execution_error = retryable_provider_error
                        action_attempt.updated_at = now_fn()
                        _append_action_execution_event(
                            db=db,
                            action_attempt=action_attempt,
                            event_type="evt.action.execution.retrying",
                            payload_data={
                                "action_attempt_id": action_attempt.id,
                                "error": retryable_provider_error,
                            },
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                else:
                    action_attempt.execution_error = retryable_provider_error
                    action_attempt.updated_at = now_fn()
                    _append_action_execution_event(
                        db=db,
                        action_attempt=action_attempt,
                        event_type="evt.action.execution.retrying",
                        payload_data={
                            "action_attempt_id": action_attempt.id,
                            "error": retryable_provider_error,
                        },
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
            elif execution_result.status == "succeeded" and execution_result.output is not None:
                provider_write_normalized_input = None
                provider_write_provider_account_id = None
                if email_provider_call is not None:
                    (
                        _,
                        _,
                        provider_write_normalized_input,
                        _,
                        _,
                        provider_write_provider_account_id,
                    ) = email_provider_call
                elif provider_call is not None:
                    (
                        _,
                        provider_write_normalized_input,
                        _,
                        _,
                        provider_write_provider_account_id,
                    ) = provider_call
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS and isinstance(
                    execution_result.output, dict
                ):
                    provider_write_identity_error = _provider_write_success_identity_error(
                        capability_id=action_attempt.capability_id,
                        provider_object_ids=_provider_write_object_ids(
                            normalized_input=provider_write_normalized_input,
                            response_payload=execution_result.output,
                        ),
                    )
                    if provider_write_identity_error is not None:
                        receipt = _record_provider_write_receipt(
                            db=db,
                            action_attempt=action_attempt,
                            status="ambiguous",
                            normalized_input=provider_write_normalized_input,
                            provider_account_id=provider_write_provider_account_id,
                            output_payload=execution_result.output,
                            error=provider_write_identity_error,
                            ambiguity_reason=provider_write_identity_error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        _append_provider_write_reconcile_unavailable_event(
                            db=db,
                            action_attempt=action_attempt,
                            receipt=receipt,
                            reason=provider_write_identity_error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        _fail_action_execution(
                            db=db,
                            action_attempt=action_attempt,
                            error=provider_write_identity_error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                        return True
                public_output = execution_result.output
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS and isinstance(
                    execution_result.output, dict
                ):
                    public_output = _redact_google_provider_output(
                        capability_id=action_attempt.capability_id,
                        output_payload=execution_result.output,
                    )
                action_attempt.status = "succeeded"
                action_attempt.execution_output = public_output
                action_attempt.execution_error = None
                action_attempt.updated_at = now_fn()
                if action_attempt.capability_id in _GOOGLE_RECEIPT_CAPABILITY_IDS and isinstance(
                    execution_result.output, dict
                ):
                    _record_provider_write_receipt(
                        db=db,
                        action_attempt=action_attempt,
                        status="succeeded",
                        normalized_input=provider_write_normalized_input,
                        provider_account_id=provider_write_provider_account_id,
                        output_payload=execution_result.output,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                event_output = dict(public_output)
                if "undo_token" in event_output:
                    event_output["undo_token"] = "[redacted]"
                _append_action_execution_event(
                    db=db,
                    action_attempt=action_attempt,
                    event_type="evt.action.execution.succeeded",
                    payload_data={
                        "action_attempt_id": action_attempt.id,
                        "output": event_output,
                    },
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
            else:
                provider_write_provider = "google"
                provider_write_normalized_input = None
                provider_write_provider_account_id = None
                failure_agency_receipt_id: str | None = None
                if email_provider_call is not None:
                    (
                        _,
                        _,
                        provider_write_normalized_input,
                        _,
                        _,
                        provider_write_provider_account_id,
                    ) = email_provider_call
                elif provider_call is not None:
                    (
                        _,
                        provider_write_normalized_input,
                        _,
                        _,
                        provider_write_provider_account_id,
                    ) = provider_call
                elif agency_call is not None:
                    (
                        agency_capability_id,
                        agency_input,
                        agency_context,
                        failure_agency_receipt_id,
                    ) = agency_call
                    if agency_capability_id == "cap.agency.request_pr":
                        provider_write_provider = "agency"
                        provider_write_normalized_input = agency_input
                        provider_write_provider_account_id = str(agency_context["repo_id"])
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
                if action_attempt.capability_id in (
                    _GOOGLE_RECEIPT_CAPABILITY_IDS | _AGENCY_RECEIPT_CAPABILITY_IDS
                ):
                    failure_payload = provider_write_failure_payload
                    if failure_payload is None and isinstance(execution_result.output, dict):
                        failure_payload = execution_result.output
                    if (
                        action_attempt.capability_id in _AGENCY_RECEIPT_CAPABILITY_IDS
                        and failure_payload is None
                        and failure_agency_receipt_id is not None
                    ):
                        existing_receipt = db.get(
                            ProviderWriteReceiptRecord,
                            failure_agency_receipt_id,
                        )
                        if existing_receipt is not None and isinstance(
                            existing_receipt.response_payload,
                            dict,
                        ):
                            failure_payload = {
                                **existing_receipt.response_payload,
                                "error": error,
                            }
                    if action_attempt.capability_id in _AGENCY_RECEIPT_CAPABILITY_IDS:
                        receipt_status = provider_write_failure_status or "ambiguous"
                    else:
                        receipt_status = provider_write_failure_status or (
                            _provider_write_failure_receipt_status(
                                error=error,
                                output_payload=failure_payload,
                            )
                        )
                    receipt = _record_provider_write_receipt(
                        db=db,
                        provider=provider_write_provider,
                        action_attempt=action_attempt,
                        status=receipt_status,
                        normalized_input=provider_write_normalized_input,
                        provider_account_id=provider_write_provider_account_id,
                        output_payload=failure_payload,
                        error=error,
                        ambiguity_reason=error if receipt_status == "ambiguous" else None,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                    if receipt_status == "ambiguous":
                        _append_provider_write_reconcile_unavailable_event(
                            db=db,
                            action_attempt=action_attempt,
                            receipt=receipt,
                            reason=error,
                            now_fn=now_fn,
                            new_id_fn=new_id_fn,
                        )
                _fail_action_execution(
                    db=db,
                    action_attempt=action_attempt,
                    error=error,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
    if retryable_provider_error is not None:
        raise RuntimeError(retryable_provider_error)
    return True
