from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.action_runtime import (
    RuntimeProvenance,
    process_action_execution_task,
    process_response_function_calls,
)
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
    response_tool_name_for_capability_id,
)
from ariel.db import reset_schema_for_tests
from ariel.google_connector import (
    GOOGLE_CONNECTOR_ID,
    GOOGLE_GMAIL_MODIFY_SCOPE,
    GoogleCapabilityExecutionResult,
    _encrypt_secret,
)
from ariel.persistence import (
    ActionAttemptRecord,
    ActionPrivatePayloadRecord,
    BackgroundTaskRecord,
    EmailActionRecord,
    EmailThreadWatchRecord,
    EventRecord,
    GoogleConnectorRecord,
    MemoryActionTraceRecord,
    MemoryEvidenceRecord,
    ProviderWriteReceiptRecord,
    SessionRecord,
    TurnRecord,
    serialize_action_attempt,
)


NOW = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
PROVIDER_ACCOUNT_ID = "acct_google"


def _email_idempotency_key(
    *,
    capability_id: str,
    provider_account_id: str,
    client_key: str,
) -> str:
    raw = f"{capability_id}\x1fgoogle\x1f{provider_account_id}\x1f{client_key}"
    return "email:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        yield postgres.get_connection_url().replace("psycopg2", "psycopg")


@pytest.fixture
def session_factory(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    try:
        yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    finally:
        engine.dispose()


@dataclass
class FakeWorkspaceProvider:
    before_state: list[dict[str, Any]]
    state_payload: dict[str, Any] | None = None
    state_reads: int = 0

    def email_get_message_label_state(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token, normalized_input
        self.state_reads += 1
        if self.state_payload is not None:
            return self.state_payload
        return {"state": self.before_state}


@dataclass
class FakeGoogleRuntime:
    workspace_provider: FakeWorkspaceProvider
    execution_output: dict[str, Any] | None = None
    execution_status: Literal["succeeded", "failed"] = "succeeded"
    execution_error: str | None = None
    executions: list[dict[str, Any]] = field(default_factory=list)
    encryption_secret: str = "test-secret"
    encryption_key_version: str = "v1"
    encryption_keys: str | None = None

    def refresh_access_token_for_capability(
        self,
        *,
        session_factory: sessionmaker[Session],
        capability_id: str,
        now_fn: Any,
        new_id_fn: Any,
    ) -> None:
        del session_factory, capability_id, now_fn, new_id_fn

    def prepare_capability_access_without_refresh(
        self,
        *,
        db: Session,
        capability_id: str,
        now_fn: Any,
    ) -> tuple[str, set[str], str, None]:
        del db, capability_id, now_fn
        return "tok_live", {GOOGLE_GMAIL_MODIFY_SCOPE}, PROVIDER_ACCOUNT_ID, None

    def prepare_capability_access(
        self,
        *,
        db: Session,
        capability_id: str,
        now_fn: Any,
        new_id_fn: Any,
    ) -> tuple[str, set[str], str, None]:
        del db, capability_id, now_fn, new_id_fn
        return "tok_live", {GOOGLE_GMAIL_MODIFY_SCOPE}, PROVIDER_ACCOUNT_ID, None

    def execute_provider_capability(
        self,
        *,
        capability_id: str,
        normalized_input: dict[str, Any],
        access_token: str,
        granted_scopes: set[str],
        provider_account_id: str | None = None,
    ) -> GoogleCapabilityExecutionResult:
        del capability_id, access_token, granted_scopes, provider_account_id
        self.executions.append(normalized_input)
        if self.execution_status == "succeeded":
            assert self.execution_output is not None
        return GoogleCapabilityExecutionResult(
            status=self.execution_status,
            output=self.execution_output,
            auth_failure=None,
            error=self.execution_error,
        )


def _id_factory(suffix: str) -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{suffix}_{counters[prefix]}"

    return new_id


def _seed_action_attempt(
    session_factory: sessionmaker[Session],
    *,
    action_attempt_id: str,
    capability_id: str,
    proposed_input: dict[str, Any],
    proposal_index: int,
) -> str:
    capability = get_capability(capability_id)
    assert capability is not None
    full_input = dict(proposed_input)
    if capability_id in {
        "cap.email.archive",
        "cap.email.trash",
        "cap.email.labels.modify",
        "cap.email.undo",
    } and not any(
        isinstance(full_input.get(key), str) and full_input[key]
        for key in ("source_evidence_id", "commitment_id", "user_instruction_ref")
    ):
        full_input["user_instruction_ref"] = "turn:turn_email"
    stored_input = dict(full_input)
    private_payload_required = False
    private_keys = (
        ("body",)
        if capability_id in {"cap.email.draft", "cap.email.send"}
        else ("description",)
        if capability_id in {"cap.calendar.create_event", "cap.calendar.update_event"}
        else ()
    )
    for key in private_keys:
        value = stored_input.get(key)
        if isinstance(value, str):
            stored_input[key] = {
                "redacted": True,
                "digest": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "char_count": len(value),
                "private_payload": True,
            }
            private_payload_required = True
    private_payload_json = json.dumps(full_input, sort_keys=True, separators=(",", ":"))
    action_hash = payload_hash(
        canonical_action_payload(
            capability_id=capability_id,
            input_payload=full_input,
        )
    )
    with session_factory() as db:
        with db.begin():
            if db.get(SessionRecord, "ses_email") is None:
                db.add(
                    SessionRecord(
                        id="ses_email",
                        is_active=True,
                        lifecycle_state="active",
                        rotated_from_session_id=None,
                        rotation_reason=None,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                db.add(
                    TurnRecord(
                        id="turn_email",
                        session_id="ses_email",
                        user_message="declutter email",
                        assistant_message=None,
                        status="in_progress",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            db.add(
                ActionAttemptRecord(
                    id=action_attempt_id,
                    session_id="ses_email",
                    turn_id="turn_email",
                    proposal_index=proposal_index,
                    capability_id=capability_id,
                    capability_version=capability.version,
                    capability_contract_hash=capability_contract_hash(capability),
                    impact_level=capability.impact_level,
                    proposed_input=stored_input,
                    payload_hash=action_hash,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="executing",
                    approval_required=True,
                    execution_output=None,
                    execution_error=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            if private_payload_required:
                db.add(
                    ActionPrivatePayloadRecord(
                        id=f"app_{action_attempt_id}",
                        action_attempt_id=action_attempt_id,
                        payload_kind="google_provider_write_input",
                        payload_digest=hashlib.sha256(
                            private_payload_json.encode("utf-8")
                        ).hexdigest(),
                        payload_enc=_encrypt_secret(
                            plaintext=private_payload_json,
                            secret="test-secret",
                            key_version="v1",
                        ),
                        encryption_key_version="v1",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
    return action_hash


def _seed_google_connector(
    session_factory: sessionmaker[Session],
    *,
    account_subject: str = PROVIDER_ACCOUNT_ID,
) -> None:
    with session_factory() as db:
        with db.begin():
            db.merge(
                GoogleConnectorRecord(
                    id=GOOGLE_CONNECTOR_ID,
                    provider="google",
                    status="connected",
                    account_subject=account_subject,
                    account_email=f"{account_subject}@example.test",
                    granted_scopes=[],
                    access_token_enc=None,
                    refresh_token_enc=None,
                    access_token_expires_at=None,
                    token_obtained_at=None,
                    encryption_key_version="v1",
                    last_error_code=None,
                    last_error_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


def _seed_memory_action_trace(
    session_factory: sessionmaker[Session],
    *,
    action_attempt_id: str,
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                MemoryEvidenceRecord(
                    id=f"mev_{action_attempt_id}",
                    source_turn_id="turn_email",
                    source_session_id="ses_email",
                    actor_id="user:default",
                    content_class="user_message",
                    trust_boundary="trusted_user",
                    lifecycle_state="available",
                    source_uri=None,
                    source_artifact_id=None,
                    source_text="declutter email",
                    evidence_snippet="declutter email",
                    redaction_posture="none",
                    metadata_json={},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            db.add(
                MemoryActionTraceRecord(
                    id=f"mat_{action_attempt_id}",
                    scope_key="session:ses_email",
                    trace_type="policy_decision",
                    action_attempt_id=action_attempt_id,
                    source_turn_id="turn_email",
                    primary_evidence_id=f"mev_{action_attempt_id}",
                    capability_id="cap.email.archive",
                    summary="cap.email.archive unknown for proposal 1",
                    outcome="unknown",
                    result_refs={"execution_status": "executing"},
                    lifecycle_state="active",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


def test_memory_inspect_capability_executes_inline(
    session_factory: sessionmaker[Session],
) -> None:
    events: list[dict[str, Any]] = []
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_memory",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            turn = TurnRecord(
                id="turn_memory",
                session_id="ses_memory",
                user_message="inspect memory",
                assistant_message=None,
                status="in_progress",
                created_at=NOW,
                updated_at=NOW,
            )
            db.add(turn)
            db.flush()

            result = process_response_function_calls(
                db=db,
                session_id="ses_memory",
                turn=turn,
                assistant_message="",
                function_calls_raw=[
                    {
                        "call_id": "call_memory_inspect",
                        "name": response_tool_name_for_capability_id("cap.memory.inspect"),
                        "arguments": json.dumps({"section": "all", "limit": 10}),
                    }
                ],
                approval_ttl_seconds=300,
                approval_actor_id="user:default",
                add_event=lambda event_type, payload: events.append(
                    {"event_type": event_type, "payload": payload}
                ),
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_memory",
                allowed_capability_ids=["cap.memory.inspect"],
            )

    assert result.action_attempts[0].capability_id == "cap.memory.inspect"
    assert result.action_attempts[0].status == "succeeded"
    function_output = json.loads(result.function_call_outputs[0]["output"])
    assert function_output["status"] == "succeeded"
    assert function_output["capability_id"] == "cap.memory.inspect"
    assert function_output["output"]["status"] == "inspected"
    assert function_output["output"]["memory"]["active_assertions"] == []
    assert [event["event_type"] for event in events] == [
        "evt.action.proposed",
        "evt.action.policy_decided",
        "evt.action.execution.started",
        "evt.action.execution.succeeded",
    ]


def test_email_action_partial_provider_failure_retries_without_false_success(
    session_factory: sessionmaker[Session],
) -> None:
    before_state = [
        {"message_id": "msg_ok", "thread_id": "thr_1", "label_ids": ["INBOX"]},
        {"message_id": "msg_fail", "thread_id": "thr_1", "label_ids": ["INBOX"]},
    ]
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=before_state),
        execution_output={
            "status": "partially_failed",
            "operation": "trash",
            "message_ids": ["msg_ok", "msg_fail"],
            "before_state": before_state,
            "after_state": [
                {"message_id": "msg_ok", "thread_id": "thr_1", "label_ids": ["TRASH"]},
                {"message_id": "msg_fail", "thread_id": "thr_1", "label_ids": ["INBOX"]},
            ],
            "provider_result": {
                "operation": "trash",
                "provider": "gmail",
                "mutated_message_ids": ["msg_ok"],
                "attempted_message_ids": ["msg_ok", "msg_fail"],
                "failed_provider_call": {
                    "api": "users.messages.trash",
                    "message_id": "msg_fail",
                    "error": "google_upstream_500",
                },
                "error": "google_upstream_500",
            },
        },
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_partial",
        capability_id="cap.email.trash",
        proposed_input={"message_ids": ["msg_ok", "msg_fail"], "idempotency_key": "trash-1"},
        proposal_index=1,
    )

    with pytest.raises(RuntimeError, match="google_upstream_500"):
        process_action_execution_task(
            session_factory=session_factory,
            action_attempt_id="act_partial",
            google_runtime=runtime,  # type: ignore[arg-type]
            agency_runtime=None,
            now_fn=lambda: NOW,
            new_id_fn=lambda prefix: f"{prefix}_partial",
        )

    assert runtime.workspace_provider.state_reads == 1
    assert runtime.executions[0]["before_state"] == before_state
    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_partial")
        assert action_attempt is not None
        assert action_attempt.status == "executing"
        assert action_attempt.execution_error == "google_upstream_500"
        email_action = db.scalar(select(EmailActionRecord).limit(1))
        assert email_action is not None
        assert email_action.status == "executing"
        assert email_action.failure_code is None
        assert email_action.before_state == {"messages": before_state}
        assert email_action.after_state["messages"][0]["label_ids"] == ["TRASH"]
        assert email_action.provider_result["error"] == "google_upstream_500"
        assert email_action.intended_state["before_state"] == before_state
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        assert receipt is not None
        assert receipt.status == "failed"
        assert receipt.provider_account_id == PROVIDER_ACCOUNT_ID
        assert receipt.action_attempt_id == "act_partial"
        assert receipt.request_digest == action_attempt.payload_hash
        assert receipt.ambiguity_reason is None
        assert receipt.idempotency_key is not None
        assert "act_partial" not in receipt.idempotency_key
        assert receipt.response_payload["error"] == "google_upstream_500"
        assert receipt.response_payload["provider_result"]["mutated_message_ids"] == ["msg_ok"]
        assert receipt.provider_object_ids["message_ids"] == ["msg_ok", "msg_fail"]


def test_email_action_malformed_before_state_fails_before_provider_mutation(
    session_factory: sessionmaker[Session],
) -> None:
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=[], state_payload={}),
        execution_output={
            "status": "archived",
            "operation": "archive",
            "message_ids": ["msg_1"],
            "before_state": [],
            "after_state": [],
            "provider_result": {"operation": "archive"},
        },
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_bad_before",
        capability_id="cap.email.archive",
        proposed_input={"message_ids": ["msg_1"], "idempotency_key": "archive-bad-before"},
        proposal_index=1,
    )

    with pytest.raises(RuntimeError, match="email_before_state_missing"):
        process_action_execution_task(
            session_factory=session_factory,
            action_attempt_id="act_bad_before",
            google_runtime=runtime,  # type: ignore[arg-type]
            agency_runtime=None,
            now_fn=lambda: NOW,
            new_id_fn=lambda prefix: f"{prefix}_bad_before",
        )

    assert runtime.executions == []
    with session_factory() as db:
        email_action = db.scalar(select(EmailActionRecord).limit(1))
        assert email_action is not None
        assert email_action.status == "executing"
        assert email_action.before_state == {}


def test_email_action_provider_timeout_records_ambiguous_receipt_and_fails_closed(
    session_factory: sessionmaker[Session],
) -> None:
    before_state = [{"message_id": "msg_1", "thread_id": "thr_1", "label_ids": ["INBOX"]}]
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=before_state),
        execution_output=None,
        execution_status="failed",
        execution_error="provider_timeout",
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_timeout",
        capability_id="cap.email.archive",
        proposed_input={"message_ids": ["msg_1"], "idempotency_key": "archive-timeout"},
        proposal_index=1,
    )
    new_id = _id_factory("timeout")

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_timeout",
        google_runtime=runtime,  # type: ignore[arg-type]
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_timeout")
        assert action_attempt is not None
        assert action_attempt.status == "failed"
        assert action_attempt.execution_error == "provider_timeout"
        email_action = db.scalar(select(EmailActionRecord).limit(1))
        assert email_action is not None
        assert email_action.status == "failed"
        assert email_action.failure_code == "provider_timeout"
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        assert receipt is not None
        assert receipt.status == "ambiguous"
        assert receipt.ambiguity_reason == "provider_timeout"
        assert receipt.response_payload["error"] == "provider_timeout"
        assert receipt.provider_object_ids["message_ids"] == ["msg_1"]
        event_types = [
            row[0]
            for row in db.execute(
                select(EventRecord.event_type).order_by(EventRecord.sequence.asc())
            ).all()
        ]
        assert event_types == [
            "evt.provider_write.reconcile_unavailable",
            "evt.action.execution.failed",
        ]
        reconcile_event = db.scalar(
            select(EventRecord)
            .where(EventRecord.event_type == "evt.provider_write.reconcile_unavailable")
            .limit(1)
        )
        assert reconcile_event is not None
        assert reconcile_event.payload["provider_write_receipt_id"] == receipt.id
        assert reconcile_event.payload["reconcile_task_enqueued"] is True
        reconcile_task = db.scalar(
            select(BackgroundTaskRecord)
            .where(BackgroundTaskRecord.task_type == "provider_write_reconcile_due")
            .limit(1)
        )
        assert reconcile_task is not None
        assert reconcile_task.provider_write_receipt_id == receipt.id


def test_email_action_success_redacts_undo_token_from_event_audit(
    session_factory: sessionmaker[Session],
) -> None:
    before_state = [{"message_id": "msg_1", "thread_id": "thr_1", "label_ids": ["INBOX"]}]
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=before_state),
        execution_output={
            "status": "archived",
            "operation": "archive",
            "message_ids": ["msg_1"],
            "before_state": before_state,
            "after_state": [{"message_id": "msg_1", "thread_id": "thr_1", "label_ids": []}],
            "provider_result": {
                "operation": "archive",
                "provider": "gmail",
                "mutated_message_ids": ["msg_1"],
                "attempted_message_ids": ["msg_1"],
            },
        },
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_success",
        capability_id="cap.email.archive",
        proposed_input={"message_ids": ["msg_1"], "idempotency_key": "archive-success"},
        proposal_index=1,
    )
    _seed_memory_action_trace(session_factory, action_attempt_id="act_success")

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_success",
        google_runtime=runtime,  # type: ignore[arg-type]
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_success",
    )

    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_success")
        assert action_attempt is not None
        assert action_attempt.execution_output is not None
        undo_token = action_attempt.execution_output["undo_token"]
        assert isinstance(undo_token, str)
        assert undo_token
        email_action = db.scalar(select(EmailActionRecord).limit(1))
        assert email_action is not None
        assert email_action.undo_token_hash is not None
        assert email_action.undo_token_hash != undo_token
        event = db.scalar(
            select(EventRecord)
            .where(EventRecord.event_type == "evt.action.execution.succeeded")
            .limit(1)
        )
        assert event is not None
        assert event.payload["output"]["undo_token"] == "[redacted]"
        trace = db.get(MemoryActionTraceRecord, "mat_act_success")
        assert trace is not None
        assert trace.trace_type == "execution"
        assert trace.outcome == "succeeded"
        assert trace.summary == "cap.email.archive succeeded for proposal 1"
        assert trace.result_refs["execution_status"] == "succeeded"
        assert trace.result_refs["execution_output_available"] is True
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        assert receipt is not None
        assert receipt.status == "succeeded"
        assert receipt.provider_account_id == PROVIDER_ACCOUNT_ID
        assert receipt.action_attempt_id == "act_success"
        assert receipt.request_digest == action_attempt.payload_hash
        assert receipt.ambiguity_reason is None
        assert receipt.idempotency_key is not None
        assert "act_success" not in receipt.idempotency_key
        assert receipt.response_payload["email_action_id"] == email_action.id
        assert receipt.provider_object_ids["message_ids"] == ["msg_1"]
        assert receipt.response_payload["undo_token"] == "[redacted]"
        assert isinstance(receipt.response_digest, str)
        assert len(receipt.response_digest) == 64


def test_email_draft_write_receipt_redacts_body_and_records_authority(
    session_factory: sessionmaker[Session],
) -> None:
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=[]),
        execution_output={
            "status": "drafted_not_sent",
            "delivery_state": "draft_only",
            "sent": False,
            "draft": {
                "to": ["teammate@example.com"],
                "subject": "Follow up",
                "body": "Private draft body should not be in receipts.",
            },
            "provider_draft_ref": "gmail-draft-1",
            "history_id": "hist_draft_1",
            "provider_timestamp": "2026-05-08T12:00:01Z",
        },
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_draft",
        capability_id="cap.email.draft",
        proposed_input={
            "to": ["teammate@example.com"],
            "subject": "Follow up",
            "body": "Private draft body should not be in receipts.",
            "idempotency_key": "draft-private-body",
            "user_instruction_ref": "turn:turn_email",
        },
        proposal_index=1,
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_draft",
        google_runtime=runtime,  # type: ignore[arg-type]
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_draft",
    )

    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_draft")
        assert action_attempt is not None
        assert action_attempt.status == "succeeded", action_attempt.execution_error
        assert action_attempt.execution_output is not None
        assert action_attempt.proposed_input["body"]["private_payload"] is True
        assert "Private draft body should not be in receipts." not in json.dumps(
            action_attempt.proposed_input,
            sort_keys=True,
        )
        assert action_attempt.execution_output["draft"]["body_redacted"]["redacted"] is True
        serialized_attempt = serialize_action_attempt(action_attempt, approval=None)
        serialized_json = json.dumps(serialized_attempt, sort_keys=True)
        assert "Private draft body should not be in receipts." not in serialized_json
        assert serialized_attempt["proposal_input"]["body"]["redacted"] is True
        private_payload = db.scalar(select(ActionPrivatePayloadRecord).limit(1))
        assert private_payload is not None
        assert "Private draft body should not be in receipts." not in private_payload.payload_enc
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        assert receipt is not None
        assert receipt.status == "succeeded"
        assert receipt.provider_history_id == "hist_draft_1"
        assert receipt.provider_timestamp == datetime(2026, 5, 8, 12, 0, 1, tzinfo=UTC)
        assert isinstance(receipt.response_digest, str)
        assert len(receipt.response_digest) == 64
        assert receipt.response_payload["authority"]["source_type"] == "user_instruction_ref"
        assert receipt.response_payload["authority"]["turn_id"] == "turn_email"
        receipt_json = json.dumps(receipt.response_payload, sort_keys=True)
        assert "Private draft body should not be in receipts." not in receipt_json
        event = db.scalar(
            select(EventRecord)
            .where(EventRecord.event_type == "evt.action.execution.succeeded")
            .limit(1)
        )
        assert event is not None
        assert "Private draft body should not be in receipts." not in json.dumps(
            event.payload,
            sort_keys=True,
        )


def test_email_action_idempotency_replay_returns_existing_result_without_provider_call(
    session_factory: sessionmaker[Session],
) -> None:
    proposed_input = {"message_ids": ["msg_1"], "idempotency_key": "archive-1"}
    original_hash = _seed_action_attempt(
        session_factory,
        action_attempt_id="act_original",
        capability_id="cap.email.archive",
        proposed_input=proposed_input,
        proposal_index=1,
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_replay",
        capability_id="cap.email.archive",
        proposed_input=proposed_input,
        proposal_index=2,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                EmailActionRecord(
                    id="ema_original",
                    provider="google",
                    provider_account_id=PROVIDER_ACCOUNT_ID,
                    action_attempt_id="act_original",
                    capability_id="cap.email.archive",
                    input_hash=original_hash,
                    idempotency_key=_email_idempotency_key(
                        capability_id="cap.email.archive",
                        provider_account_id=PROVIDER_ACCOUNT_ID,
                        client_key="archive-1",
                    ),
                    status="succeeded",
                    approval_id=None,
                    provider_message_ids=["msg_1"],
                    provider_thread_ids=["thr_1"],
                    before_state={"messages": []},
                    intended_state=proposed_input,
                    after_state={"messages": []},
                    provider_result={"operation": "archive"},
                    undo_token_hash="hash_only",
                    undo_expires_at=NOW,
                    execution_attempts=1,
                    failure_code=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=[]),
        execution_output=None,
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_replay",
        google_runtime=runtime,  # type: ignore[arg-type]
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_replay",
    )

    assert runtime.workspace_provider.state_reads == 0
    assert runtime.executions == []
    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_replay")
        assert action_attempt is not None
        assert action_attempt.status == "succeeded"
        assert action_attempt.execution_output is not None
        assert action_attempt.execution_output["email_action_id"] == "ema_original"
        assert action_attempt.execution_output["undo_available"] is False
        assert "undo_token" not in action_attempt.execution_output


def test_email_undo_idempotency_replay_does_not_revalidate_prior_action(
    session_factory: sessionmaker[Session],
) -> None:
    proposed_input = {"undo_token": "token no longer valid", "idempotency_key": "undo-1"}
    undo_hash = _seed_action_attempt(
        session_factory,
        action_attempt_id="act_undo_original",
        capability_id="cap.email.undo",
        proposed_input=proposed_input,
        proposal_index=1,
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_undo_replay",
        capability_id="cap.email.undo",
        proposed_input=proposed_input,
        proposal_index=2,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                EmailActionRecord(
                    id="ema_undo_original",
                    provider="google",
                    provider_account_id=PROVIDER_ACCOUNT_ID,
                    action_attempt_id="act_undo_original",
                    capability_id="cap.email.undo",
                    input_hash=undo_hash,
                    idempotency_key=_email_idempotency_key(
                        capability_id="cap.email.undo",
                        provider_account_id=PROVIDER_ACCOUNT_ID,
                        client_key="undo-1",
                    ),
                    status="succeeded",
                    approval_id=None,
                    provider_message_ids=["msg_1"],
                    provider_thread_ids=["thr_1"],
                    before_state={"messages": []},
                    intended_state={"message_ids": ["msg_1"]},
                    after_state={"messages": []},
                    provider_result={"operation": "undo"},
                    undo_token_hash=None,
                    undo_expires_at=None,
                    execution_attempts=1,
                    failure_code=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    runtime = FakeGoogleRuntime(
        workspace_provider=FakeWorkspaceProvider(before_state=[]),
        execution_output=None,
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_undo_replay",
        google_runtime=runtime,  # type: ignore[arg-type]
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_undo_replay",
    )

    assert runtime.workspace_provider.state_reads == 0
    assert runtime.executions == []
    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_undo_replay")
        assert action_attempt is not None
        assert action_attempt.status == "succeeded"
        assert action_attempt.execution_output is not None
        assert action_attempt.execution_output["email_action_id"] == "ema_undo_original"


def test_email_thread_watch_list_is_scoped_to_current_google_account(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_google_connector(session_factory)
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_watch_seed",
        capability_id="cap.email.thread_watch.create",
        proposed_input={
            "provider_thread_id": "thr_owner",
            "anchor_message_id": "msg_owner",
            "condition": "no_reply_by_deadline",
            "deadline": "2026-05-08T12:00:00Z",
            "note": "seed watch",
            "idempotency_key": "watch-seed",
        },
        proposal_index=1,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                TurnRecord(
                    id="turn_watch_list",
                    session_id="ses_email",
                    user_message="list watches",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            for watch_id, account_id, thread_id in [
                ("etw_owner", PROVIDER_ACCOUNT_ID, "thr_owner"),
                ("etw_other", "other_google_account", "thr_other"),
            ]:
                db.add(
                    EmailThreadWatchRecord(
                        id=watch_id,
                        provider="google",
                        provider_account_id=account_id,
                        provider_thread_id=thread_id,
                        anchor_message_id=f"msg_{watch_id}",
                        condition="no_reply_by_deadline",
                        deadline=NOW,
                        note="watch thread",
                        status="active",
                        idempotency_key=f"watch-{watch_id}",
                        cancel_idempotency_key=None,
                        created_by_action_attempt_id="act_watch_seed",
                        matched_message_id=None,
                        matched_at=None,
                        canceled_at=None,
                        completed_at=None,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

    events: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as db:
        with db.begin():
            turn = db.get(TurnRecord, "turn_watch_list")
            assert turn is not None
            result = process_response_function_calls(
                db=db,
                session_id="ses_email",
                turn=turn,
                assistant_message="list watches",
                function_calls_raw=[
                    {
                        "type": "function_call",
                        "call_id": "call_watch_list",
                        "name": response_tool_name_for_capability_id("cap.email.thread_watch.list"),
                        "arguments": json.dumps({}),
                        "influenced_by_untrusted_content": False,
                    }
                ],
                approval_ttl_seconds=300,
                approval_actor_id="usr_email",
                add_event=lambda event_type, payload: events.append((event_type, payload)),
                now_fn=lambda: NOW,
                new_id_fn=lambda prefix: f"{prefix}_watch_list",
                runtime_provenance=RuntimeProvenance(status="clean"),
                allowed_capability_ids=["cap.email.thread_watch.list"],
            )

    assert len(result.function_call_outputs) == 1
    payload = json.loads(result.function_call_outputs[0]["output"])
    assert [watch["watch_id"] for watch in payload["watches"]] == ["etw_owner"]


def test_email_thread_watch_cancel_enforces_cancel_idempotency_key(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_google_connector(session_factory)
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_cancel",
        capability_id="cap.email.thread_watch.cancel",
        proposed_input={"watch_id": "etw_cancel", "idempotency_key": "cancel-1"},
        proposal_index=1,
    )
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_cancel_conflict",
        capability_id="cap.email.thread_watch.cancel",
        proposed_input={"watch_id": "etw_cancel", "idempotency_key": "cancel-2"},
        proposal_index=2,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                EmailThreadWatchRecord(
                    id="etw_cancel",
                    provider="google",
                    provider_account_id=PROVIDER_ACCOUNT_ID,
                    provider_thread_id="thr_cancel",
                    anchor_message_id="msg_anchor",
                    condition="no_reply_by_deadline",
                    deadline=NOW,
                    note="cancel me",
                    status="active",
                    idempotency_key="watch-cancel",
                    cancel_idempotency_key=None,
                    created_by_action_attempt_id="act_cancel",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_cancel",
        google_runtime=None,
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_cancel",
    )
    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_cancel_conflict",
        google_runtime=None,
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_cancel_conflict",
    )

    with session_factory() as db:
        watch = db.get(EmailThreadWatchRecord, "etw_cancel")
        assert watch is not None
        assert watch.status == "canceled"
        assert watch.cancel_idempotency_key == _email_idempotency_key(
            capability_id="cap.email.thread_watch.cancel",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            client_key="cancel-1",
        )
        conflict = db.get(ActionAttemptRecord, "act_cancel_conflict")
        assert conflict is not None
        assert conflict.status == "failed"
        assert conflict.execution_error == "idempotency_key_input_mismatch"


def test_email_thread_watch_cancel_denies_cross_account_watch_id(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_google_connector(session_factory)
    _seed_action_attempt(
        session_factory,
        action_attempt_id="act_cancel_other",
        capability_id="cap.email.thread_watch.cancel",
        proposed_input={"watch_id": "etw_other", "idempotency_key": "cancel-other"},
        proposal_index=1,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                EmailThreadWatchRecord(
                    id="etw_other",
                    provider="google",
                    provider_account_id="other_google_account",
                    provider_thread_id="thr_other",
                    anchor_message_id="msg_anchor",
                    condition="no_reply_by_deadline",
                    deadline=NOW,
                    note="not this account",
                    status="active",
                    idempotency_key="watch-other",
                    cancel_idempotency_key=None,
                    created_by_action_attempt_id="act_cancel_other",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="act_cancel_other",
        google_runtime=None,
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=lambda prefix: f"{prefix}_cancel_other",
    )

    with session_factory() as db:
        action_attempt = db.get(ActionAttemptRecord, "act_cancel_other")
        watch = db.get(EmailThreadWatchRecord, "etw_other")
        assert action_attempt is not None
        assert action_attempt.status == "failed"
        assert action_attempt.execution_error == "thread_watch_not_found"
        assert watch is not None
        assert watch.status == "active"
