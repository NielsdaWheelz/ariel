from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.action_runtime import process_action_execution_task, process_provider_write_reconcile_due
from ariel.agency_daemon import AgencyDaemonError, AgencyRuntime
from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.db import reset_schema_for_tests
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    EventRecord,
    JobRecord,
    ProviderWriteReceiptRecord,
    SessionRecord,
    TurnRecord,
)


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)


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
        allow_dirty: bool,
        force_with_lease: bool,
        client_request_id: str,
    ) -> dict[str, Any]:
        del repo_id, worktree_ref, allow_dirty, force_with_lease
        if self.pr_sync_calls is None:
            self.pr_sync_calls = []
        self.pr_sync_calls.append(client_request_id)
        if self.fail_pr_sync_once:
            self.fail_pr_sync_once = False
            raise RuntimeError("agency_pr_sync_timeout")
        return {
            "pr_url": "https://github.test/acme/repo/pull/7",
            "pr_number": 7,
            "request_id": "pr_req_1",
        }


def _id_factory(label: str) -> Any:
    counts: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}_{label}_{counts[prefix]}"

    return new_id


def _seed_request_pr_action(session_factory: sessionmaker[Session], *, action_id: str) -> None:
    capability = get_capability("cap.agency.request_pr")
    assert capability is not None
    input_payload = {
        "job_id": "job_1",
        "repo_id": None,
        "task_id": None,
        "invocation_id": None,
        "worktree_id": None,
        "allow_dirty": False,
        "force_with_lease": True,
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
        receipt_id = receipt.id

    assert process_provider_write_reconcile_due(
        session_factory=session_factory,
        task_payload={"provider_write_receipt_id": receipt_id},
        agency_runtime=runtime,
        now_fn=lambda: NOW,
        new_id_fn=_id_factory("agency_reconcile"),
    )

    with session_factory() as db:
        receipt = db.get(ProviderWriteReceiptRecord, receipt_id)
        action = db.get(ActionAttemptRecord, "aat_agency_timeout")
        assert receipt is not None
        assert action is not None
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
    with pytest.raises(AgencyDaemonError):
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
        assert any(event.payload["reconcile_task_enqueued"] is False for event in events)

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
