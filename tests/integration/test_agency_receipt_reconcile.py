from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import (
    process_action_execution_task,
    process_provider_write_reconcile_due,
    resolve_approval_decision,
)
from ariel.agency_daemon import AgencyDaemonError, AgencyRuntime
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.persistence import (
    ActionAttemptRecord,
    ApprovalRequestRecord,
    BackgroundTaskRecord,
    EventRecord,
    JobEventRecord,
    JobRecord,
    ProviderWriteReceiptRecord,
    SessionRecord,
    TurnRecord,
)
from ariel.worker import process_one_task
from tests.integration.app_helpers import create_test_app


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)


@dataclass
class FakeAgencyClient:
    fail_pr_sync_once: bool = False
    land_calls: list[str] | None = None
    pr_sync_calls: list[str] | None = None

    def get_invocation(self, *, repo_id: str, invocation_ref: str) -> dict[str, Any]:
        assert repo_id == "repo_1"
        assert invocation_ref == "inv_1"
        return {"landing_status": "pending"}

    def land_invocation(
        self,
        *,
        repo_id: str,
        invocation_ref: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        del repo_id, invocation_ref
        if self.land_calls is None:
            self.land_calls = []
        self.land_calls.append(client_request_id)
        return {"request_id": "land_req_1"}

    def worktree_pr_sync(
        self,
        *,
        repo_id: str,
        worktree_ref: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        del repo_id, worktree_ref
        if self.pr_sync_calls is None:
            self.pr_sync_calls = []
        self.pr_sync_calls.append(client_request_id)
        if self.fail_pr_sync_once:
            self.fail_pr_sync_once = False
            raise AgencyDaemonError("agency_pr_sync_timeout")
        return {
            "pr_url": "https://github.test/acme/repo/pull/7",
            "pr_number": 7,
            "request_id": "pr_req_1",
        }


@dataclass
class FakeAgencyRunClient:
    task_start_calls: list[dict[str, Any]] | None = None

    def task_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.task_start_calls is None:
            self.task_start_calls = []
        self.task_start_calls.append(payload)
        return {
            "task_id": "task_run_1",
            "task_name": payload["name"],
            "repo_id": "repo_run_1",
            "state": "running",
            "invocation_id": "inv_run_1",
            "worktree_id": "wt_run_1",
            "worktree_path": str(Path.cwd() / ".agency" / "wt_run_1"),
            "branch": "agency/smoke-run",
            "runner": payload["runner"],
            "request_id": "req_run_1",
        }


def _id_factory(label: str) -> Any:
    counts: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}_{label}_{counts[prefix]}"

    return new_id


def _seed_agency_run_approval(
    session_factory: sessionmaker[Session],
    *,
    action_id: str,
    approval_id: str,
    actor_id: str = "user:agency",
) -> None:
    capability = get_capability("cap.agency.run")
    assert capability is not None
    input_payload: dict[str, Any] = {
        "repo_root": str(Path.cwd()),
        "name": "Smoke Agency run",
        "prompt": "Run the Agency approval smoke task.",
        "base_branch": None,
        "runner": None,
        "runner_args": [],
        "env": [],
        "no_include_untracked": False,
    }
    normalized_input, input_error = capability.validate_input(input_payload)
    assert normalized_input is not None
    assert input_error is None
    action_hash = payload_hash(
        canonical_action_payload(
            capability_id=capability.capability_id,
            input_payload=normalized_input,
        )
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_agency_run",
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
                    id="turn_agency_run",
                    session_id="ses_agency_run",
                    user_message="start agency job",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            db.add(
                ActionAttemptRecord(
                    id=action_id,
                    session_id="ses_agency_run",
                    turn_id="turn_agency_run",
                    proposal_index=1,
                    capability_id=capability.capability_id,
                    capability_version=capability.version,
                    capability_contract_hash=capability_contract_hash(capability),
                    impact_level=capability.impact_level,
                    proposed_input=normalized_input,
                    payload_hash=action_hash,
                    policy_decision="requires_approval",
                    policy_reason="approval_required",
                    status="awaiting_approval",
                    approval_required=True,
                    execution_output=None,
                    execution_error=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                ApprovalRequestRecord(
                    id=approval_id,
                    action_attempt_id=action_id,
                    session_id="ses_agency_run",
                    turn_id="turn_agency_run",
                    actor_id=actor_id,
                    status="pending",
                    payload_hash=action_hash,
                    expires_at=NOW.replace(year=2030),
                    decision_reason=None,
                    decided_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


def test_agency_run_approval_decision_worker_execution_records_job_once(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_agency_run_approval(
        session_factory,
        action_id="aat_agency_run",
        approval_id="apr_agency_run",
    )
    client = FakeAgencyRunClient()
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )

    with session_factory() as db:
        with db.begin():
            decision = resolve_approval_decision(
                db=db,
                approval_ref="apr_agency_run",
                decision="approve",
                actor_id="user:agency",
                reason="manual smoke approval",
                now_fn=lambda: NOW,
                new_id_fn=_id_factory("agency_run_approval"),
            )
            execution_task_id = decision.execution_task_id
            assert decision.assistant_message == "approval recorded. action execution queued."

    assert execution_task_id is not None
    monkeypatch.setattr("ariel.worker.build_agency_runtime", lambda _settings: runtime)

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        approval = db.get(ApprovalRequestRecord, "apr_agency_run")
        action = db.get(ActionAttemptRecord, "aat_agency_run")
        task = db.get(BackgroundTaskRecord, execution_task_id)
        job = db.scalar(select(JobRecord).where(JobRecord.action_attempt_id == "aat_agency_run"))
        job_event = (
            None
            if job is None
            else db.scalar(select(JobEventRecord).where(JobEventRecord.job_id == job.id))
        )
        event_types = [
            row[0]
            for row in db.execute(
                select(EventRecord.event_type).order_by(EventRecord.sequence.asc())
            ).all()
        ]

    assert approval is not None
    assert approval.status == "approved"
    assert approval.decision_reason == "manual smoke approval"
    assert action is not None
    assert action.status == "succeeded"
    assert action.execution_error is None
    assert action.execution_output is not None
    assert job is not None
    assert action.execution_output["job_id"] == job.id
    assert action.execution_output["agency"]["task_id"] == "task_run_1"
    assert job.source == "agency.daemon"
    assert job.external_job_id == "task_run_1"
    assert job.status == "running"
    assert job.agency_repo_id == "repo_run_1"
    assert job.agency_task_id == "task_run_1"
    assert job.agency_invocation_id == "inv_run_1"
    assert job.agency_worktree_id == "wt_run_1"
    assert job.agency_branch == "agency/smoke-run"
    assert job.agency_sandbox_policy["client_request_id"] == "aat_agency_run"
    assert job.agency_egress_policy["client_request_id"] == "aat_agency_run"
    assert job_event is not None
    assert job_event.event_type == "agency.task.started"
    assert task is None
    assert event_types == [
        "evt.action.approval.approved",
        "evt.action.execution.started",
        "evt.action.execution.succeeded",
    ]
    assert client.task_start_calls is not None
    assert len(client.task_start_calls) == 1
    assert client.task_start_calls[0]["client_request_id"] == "aat_agency_run"
    assert client.task_start_calls[0]["sandbox_policy"]["env_values_redacted"] is True

    assert (
        process_action_execution_task(
            session_factory=session_factory,
            action_attempt_id="aat_agency_run",
            google_runtime=None,
            agency_runtime=runtime,
            now_fn=lambda: NOW,
            new_id_fn=_id_factory("agency_run_replay"),
        )
        is False
    )
    assert len(client.task_start_calls) == 1


def test_agency_run_provider_call_started_replay_does_not_call_daemon(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_agency_run_approval(
        session_factory,
        action_id="aat_agency_run_started",
        approval_id="apr_agency_run_started",
    )
    with session_factory() as db:
        with db.begin():
            action = db.get(ActionAttemptRecord, "aat_agency_run_started")
            approval = db.get(ApprovalRequestRecord, "apr_agency_run_started")
            assert action is not None
            assert approval is not None
            action.status = "executing"
            action.policy_reason = "approval_approved"
            action.execution_output = {"dispatch_state": "provider_call_started"}
            action.updated_at = NOW
            approval.status = "approved"
            approval.decision_reason = "manual smoke approval"
            approval.decided_at = NOW
            approval.updated_at = NOW

    client = FakeAgencyRunClient()
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="aat_agency_run_started",
        google_runtime=None,
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_run_started"),
    )

    with session_factory() as db:
        action = db.get(ActionAttemptRecord, "aat_agency_run_started")
        events = db.scalars(select(EventRecord).order_by(EventRecord.sequence.asc())).all()

    assert client.task_start_calls is None
    assert action is not None
    assert action.status == "failed"
    assert action.execution_error == "provider_result_unknown"
    assert [event.event_type for event in events] == ["evt.action.execution.failed"]
    assert events[0].payload["error"] == "provider_result_unknown"


def test_approval_decision_api_uses_default_actor_when_actor_id_is_omitted(
    postgres_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    _seed_agency_run_approval(
        session_factory,
        action_id="aat_default_actor",
        approval_id="apr_default_actor",
        actor_id="user.local",
    )

    with TestClient(create_test_app(database_url=postgres_url)) as client:
        response = client.post(
            "/v1/approvals",
            json={
                "approval_ref": "apr_default_actor",
                "decision": "approve",
                "reason": "discord button approval",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["approval"]["status"] == "approved"
    assert payload["execution_task_id"] is not None

    with session_factory() as db:
        approval = db.get(ApprovalRequestRecord, "apr_default_actor")
        action = db.get(ActionAttemptRecord, "aat_default_actor")
        task = db.get(BackgroundTaskRecord, payload["execution_task_id"])

    assert approval is not None
    assert approval.actor_id == "user.local"
    assert approval.status == "approved"
    assert approval.decision_reason == "discord button approval"
    assert action is not None
    assert action.status == "executing"
    assert task is not None
    assert task.task_type == "execute_action_attempt"


def _seed_request_pr_action(session_factory: sessionmaker[Session], *, action_id: str) -> None:
    capability = get_capability("cap.agency.request_pr")
    assert capability is not None
    input_payload = {
        "job_id": "job_1",
        "repo_id": None,
        "task_id": None,
        "invocation_id": None,
        "worktree_id": None,
    }
    normalized_input, input_error = capability.validate_input(input_payload)
    assert normalized_input is not None
    assert input_error is None
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_agency",
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
                    id="turn_agency",
                    session_id="ses_agency",
                    user_message="request pr",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            db.add(
                JobRecord(
                    id="job_1",
                    session_id="ses_agency",
                    turn_id="turn_agency",
                    action_attempt_id=None,
                    source="agency.daemon",
                    external_job_id="task_1",
                    title="Agency job",
                    status="succeeded",
                    summary=None,
                    latest_payload={},
                    agency_repo_root=str(Path.cwd()),
                    agency_repo_id="repo_1",
                    agency_task_id="task_1",
                    agency_invocation_id="inv_1",
                    agency_worktree_id="wt_1",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id=action_id,
                    session_id="ses_agency",
                    turn_id="turn_agency",
                    proposal_index=1,
                    capability_id=capability.capability_id,
                    capability_version=capability.version,
                    capability_contract_hash=capability_contract_hash(capability),
                    impact_level=capability.impact_level,
                    proposed_input=normalized_input,
                    payload_hash=payload_hash(
                        canonical_action_payload(
                            capability_id=capability.capability_id,
                            input_payload=normalized_input,
                        )
                    ),
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


def test_agency_request_pr_receipt_ids_are_replayed_without_daemon_call(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_request_pr_action(session_factory, action_id="aat_agency_pr")
    client = FakeAgencyClient()
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="aat_agency_pr",
        google_runtime=None,
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_success"),
    )

    with session_factory() as db:
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        action = db.get(ActionAttemptRecord, "aat_agency_pr")
        assert receipt is not None
        assert action is not None
        assert receipt.status == "succeeded"
        assert client.land_calls == [f"{receipt.id}:land"]
        assert client.pr_sync_calls == [f"{receipt.id}:pr-sync"]
        action.status = "executing"
        action.execution_output = None
        action.execution_error = None
        db.commit()

    client.land_calls = []
    client.pr_sync_calls = []
    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="aat_agency_pr",
        google_runtime=None,
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_replay"),
    )

    with session_factory() as db:
        action = db.get(ActionAttemptRecord, "aat_agency_pr")
        assert action is not None
        assert action.status == "succeeded"
        assert client.land_calls == []
        assert client.pr_sync_calls == []


def test_agency_request_pr_ambiguous_receipt_reconciles_with_preserved_identity(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_request_pr_action(session_factory, action_id="aat_agency_timeout")
    client = FakeAgencyClient(fail_pr_sync_once=True)
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="aat_agency_timeout",
        google_runtime=None,
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_timeout"),
    )

    with session_factory() as db:
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        action = db.get(ActionAttemptRecord, "aat_agency_timeout")
        assert receipt is not None
        assert action is not None
        assert receipt.status == "ambiguous"
        assert receipt.provider_object_ids["job_id"] == "job_1"
        assert receipt.provider_object_ids["repo_id"] == "repo_1"
        assert receipt.provider_object_ids["invocation_id"] == "inv_1"
        assert receipt.provider_object_ids["worktree_id"] == "wt_1"
        assert action.status == "failed"
        assert action.execution_error == "agency_pr_sync_timeout"
        assert receipt.ambiguity_reason == "agency_pr_sync_timeout"
        assert receipt.response_payload["error"] == "agency_pr_sync_timeout"
        receipt_id = receipt.id

    monkeypatch.setattr("ariel.worker.build_agency_runtime", lambda _settings: runtime)
    with session_factory() as db:
        with db.begin():
            task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.provider_write_receipt_id == receipt_id
                )
            )
            assert task is not None
            task_id = task.id
            task.run_after = NOW

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        receipt = db.get(ProviderWriteReceiptRecord, receipt_id)
        action = db.get(ActionAttemptRecord, "aat_agency_timeout")
        task = db.get(BackgroundTaskRecord, task_id)
        assert receipt is not None
        assert action is not None
        assert task is None
        assert receipt.status == "succeeded"
        assert action.status == "succeeded"
        assert receipt.response_payload["client_request_id"] == receipt_id
        assert receipt.response_payload["land_client_request_id"] == f"{receipt_id}:land"
        assert receipt.response_payload["pr_sync_client_request_id"] == f"{receipt_id}:pr-sync"


def test_agency_request_pr_reconcile_probe_failure_retries(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_request_pr_action(session_factory, action_id="aat_agency_retry")
    client = FakeAgencyClient(fail_pr_sync_once=True)
    runtime = AgencyRuntime(
        client=client,  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="aat_agency_retry",
        google_runtime=None,
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_retry_start"),
    )

    with session_factory() as db:
        receipt = db.scalar(select(ProviderWriteReceiptRecord).limit(1))
        assert receipt is not None
        assert receipt.status == "ambiguous"
        receipt_id = receipt.id

    client.fail_pr_sync_once = True
    with pytest.raises(AgencyDaemonError, match="agency_pr_sync_timeout"):
        process_provider_write_reconcile_due(
            session_factory=session_factory,
            task_payload={"provider_write_receipt_id": receipt_id},
            agency_runtime=runtime,
            now_fn=lambda: NOW,
            new_id_fn=_id_factory("agency_retry_fail"),
        )

    with session_factory() as db:
        receipt = db.get(ProviderWriteReceiptRecord, receipt_id)
        events = db.scalars(
            select(EventRecord).where(
                EventRecord.event_type == "evt.provider_write.reconcile_unavailable"
            )
        ).all()
        assert receipt is not None
        assert receipt.status == "ambiguous"
        assert receipt.response_payload["reconciliation"]["status"] == "indeterminate"
        assert receipt.response_payload["reconciliation"]["reason"] == "agency_pr_sync_timeout"
        assert any(event.payload["reconcile_task_enqueued"] is False for event in events)
        assert any(event.payload["reason"] == "agency_pr_sync_timeout" for event in events)

    assert process_provider_write_reconcile_due(
        session_factory=session_factory,
        task_payload={"provider_write_receipt_id": receipt_id},
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_retry_success"),
    )

    with session_factory() as db:
        receipt = db.get(ProviderWriteReceiptRecord, receipt_id)
        action = db.get(ActionAttemptRecord, "aat_agency_retry")
        assert receipt is not None
        assert action is not None
        assert receipt.status == "succeeded"
        assert action.status == "succeeded"


def test_agency_provider_call_started_replay_marks_receipt_ambiguous(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_request_pr_action(session_factory, action_id="aat_agency_started")
    with session_factory() as db:
        with db.begin():
            action = db.get(ActionAttemptRecord, "aat_agency_started")
            assert action is not None
            response_payload = {
                "dispatch_state": "provider_call_started",
                "job_id": "job_1",
                "repo_id": "repo_1",
                "invocation_id": "inv_1",
                "worktree_id": "wt_1",
                "client_request_id": "pwr_started",
                "land_client_request_id": "pwr_started:land",
                "pr_sync_client_request_id": "pwr_started:pr-sync",
            }
            db.add(
                ProviderWriteReceiptRecord(
                    id="pwr_started",
                    provider="agency",
                    provider_account_id="repo_1",
                    action_attempt_id=action.id,
                    capability_id=action.capability_id,
                    idempotency_key="provider-write:agency:started",
                    status="executing",
                    provider_object_ids=response_payload,
                    request_digest=action.payload_hash,
                    response_payload=response_payload,
                    ambiguity_reason=None,
                    provider_timestamp=None,
                    provider_etag=None,
                    provider_history_id=None,
                    response_digest="0" * 64,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            action.execution_output = {
                "dispatch_state": "provider_call_started",
                "provider_write_receipt_id": "pwr_started",
            }

    assert process_action_execution_task(
        session_factory=session_factory,
        action_attempt_id="aat_agency_started",
        google_runtime=None,
        agency_runtime=None,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_started"),
    )

    with session_factory() as db:
        receipt = db.get(ProviderWriteReceiptRecord, "pwr_started")
        action = db.get(ActionAttemptRecord, "aat_agency_started")
        task = db.scalar(
            select(BackgroundTaskRecord)
            .where(BackgroundTaskRecord.provider_write_receipt_id == "pwr_started")
            .limit(1)
        )
        assert receipt is not None
        assert action is not None
        assert task is not None
        assert receipt.status == "ambiguous"
        assert receipt.ambiguity_reason == "provider_result_unknown"
        assert action.status == "failed"
        assert action.execution_error == "provider_result_unknown"


def test_agency_request_pr_reconcile_records_identity_missing_without_retrying(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_request_pr_action(session_factory, action_id="aat_agency_missing_identity")
    with session_factory() as db:
        with db.begin():
            action = db.get(ActionAttemptRecord, "aat_agency_missing_identity")
            assert action is not None
            db.add(
                ProviderWriteReceiptRecord(
                    id="pwr_missing_identity",
                    provider="agency",
                    provider_account_id="repo_1",
                    action_attempt_id=action.id,
                    capability_id=action.capability_id,
                    idempotency_key="provider-write:agency:missing",
                    status="ambiguous",
                    provider_object_ids={},
                    request_digest=action.payload_hash,
                    response_payload={"dispatch_state": "provider_call_started"},
                    ambiguity_reason="agency_timeout",
                    provider_timestamp=None,
                    provider_etag=None,
                    provider_history_id=None,
                    response_digest="0" * 64,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    runtime = AgencyRuntime(
        client=FakeAgencyClient(),  # type: ignore[arg-type]
        allowed_repo_roots=(str(Path.cwd()),),
        default_base_branch="main",
        default_runner="codex",
    )
    assert process_provider_write_reconcile_due(
        session_factory=session_factory,
        task_payload={"provider_write_receipt_id": "pwr_missing_identity"},
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_missing"),
    )

    with session_factory() as db:
        event = db.scalar(
            select(EventRecord)
            .where(EventRecord.event_type == "evt.provider_write.reconcile_unavailable")
            .limit(1)
        )
        assert event is not None
        assert event.payload["reason"] == "agency_reconcile_identity_missing"
        assert event.payload["reconcile_task_enqueued"] is False
