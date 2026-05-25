from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.capability_registry import (
    canonical_action_payload,
    capability_contract_hash,
    get_capability,
    payload_hash,
)
from ariel.persistence import (
    ActionAttemptRecord,
    AgencyEventRecord,
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


NOW = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)


def _seed_agency_event_task(
    db: Session,
    *,
    event_id: str,
    event_type: str,
    external_job_id: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(
        AgencyEventRecord(
            id=f"age_{event_id}",
            source="agency.daemon",
            external_event_id=event_id,
            event_type=event_type,
            external_job_id=external_job_id,
            payload=payload or {},
            status="accepted",
            error=None,
            created_at=NOW,
            received_at=NOW,
            processed_at=None,
        )
    )
    db.add(
        BackgroundTaskRecord(
            id=f"tsk_{event_id}",
            task_type="agency_event_received",
            idempotency_key=None,
            provider_write_receipt_id=None,
            payload={"agency_event_id": f"age_{event_id}"},
            attempts=0,
            recurrence_seconds=None,
            run_after=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _seed_provider_write_reconcile_task(
    db: Session,
    *,
    receipt_id: str = "pwr_worker_reconcile",
    task_id: str = "tsk_worker_reconcile",
    idempotency_key: str | None = "provider_write_reconcile:pwr_worker_reconcile",
) -> None:
    session = SessionRecord(
        id="ses_worker_reconcile",
        is_active=True,
        lifecycle_state="active",
        rotated_from_session_id=None,
        rotation_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )
    turn = TurnRecord(
        id="trn_worker_reconcile",
        session_id=session.id,
        user_message="reconcile",
        assistant_message=None,
        status="completed",
        kind="agent_turn",
        source_background_task_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    action_attempt = ActionAttemptRecord(
        id="act_worker_reconcile",
        session_id=session.id,
        turn_id=turn.id,
        proposal_index=1,
        capability_id="cap.calendar.update_event",
        capability_version="1.0",
        capability_contract_hash="c" * 64,
        impact_level="write_reversible",
        proposed_input={"event_id": "evt_worker_reconcile"},
        payload_hash="p" * 64,
        policy_decision="requires_approval",
        policy_reason=None,
        status="succeeded",
        approval_required=True,
        execution_output=None,
        execution_error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    receipt = ProviderWriteReceiptRecord(
        id=receipt_id,
        provider="google",
        provider_account_id="acct_worker_reconcile",
        action_attempt_id=action_attempt.id,
        capability_id="cap.calendar.update_event",
        idempotency_key="calendar-update-worker-reconcile",
        status="ambiguous",
        provider_object_ids={"event_id": "evt_worker_reconcile"},
        request_digest="r" * 64,
        response_payload={},
        ambiguity_reason="provider_call_started",
        provider_timestamp=None,
        provider_etag=None,
        provider_history_id=None,
        response_digest=None,
        undo_token_hash=None,
        undo_expires_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add_all([session, turn, action_attempt])
    db.flush()
    db.add(receipt)
    db.flush()
    db.add(
        BackgroundTaskRecord(
            id=task_id,
            task_type="provider_write_reconcile_due",
            idempotency_key=idempotency_key,
            provider_write_receipt_id=receipt_id,
            payload={"stale": "ignored"},
            attempts=0,
            recurrence_seconds=None,
            run_after=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def test_worker_provider_write_reconcile_due_arm_reconciles_and_deletes_task(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_process_provider_write_reconcile_due(**kwargs: Any) -> None:
        calls.append(kwargs["task_payload"])

    monkeypatch.setattr(
        "ariel.worker.process_provider_write_reconcile_due",
        fake_process_provider_write_reconcile_due,
    )
    with session_factory() as db:
        with db.begin():
            _seed_provider_write_reconcile_task(db)

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        task = db.get(BackgroundTaskRecord, "tsk_worker_reconcile")

    assert calls == [
        {
            "provider_write_receipt_id": "pwr_worker_reconcile",
            "idempotency_key": "provider_write_reconcile:pwr_worker_reconcile",
        }
    ]
    assert task is None


def test_worker_provider_write_reconcile_due_retries_bad_task_shape(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_process_provider_write_reconcile_due(**_: Any) -> None:
        raise AssertionError("bad reconcile task shape must not reach the reconcile arm")

    monkeypatch.setattr(
        "ariel.worker.process_provider_write_reconcile_due",
        fail_process_provider_write_reconcile_due,
    )
    with session_factory() as db:
        with db.begin():
            _seed_provider_write_reconcile_task(
                db,
                idempotency_key="provider_write_reconcile:pwr_other",
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        task = db.get(BackgroundTaskRecord, "tsk_worker_reconcile")

    assert task is not None
    assert task.attempts == 1


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("job.completed", "succeeded"),
        ("job.failed", "failed"),
        ("job.cancelled", "cancelled"),
        ("job.timed_out", "timed_out"),
    ],
)
def test_agency_event_received_upserts_job_event_wake_and_deletes_task(
    session_factory: sessionmaker[Session],
    event_type: str,
    expected_status: str,
) -> None:
    event_label = event_type.replace(".", "_")
    with session_factory() as db:
        with db.begin():
            _seed_agency_event_task(
                db,
                event_id=event_label,
                event_type=event_type,
                external_job_id=f"task_{event_label}",
                payload={
                    "title": "Manual smoke job",
                    "summary": "Job reached a terminal state.",
                    "detail": {"changed_files": 2},
                },
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        agency_event = db.get(AgencyEventRecord, f"age_{event_label}")
        job = db.scalar(
            select(JobRecord).where(
                JobRecord.source == "agency.daemon",
                JobRecord.external_job_id == f"task_{event_label}",
            )
        )
        job_event = (
            None
            if job is None
            else db.scalar(select(JobEventRecord).where(JobEventRecord.job_id == job.id))
        )
        wakes = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
        ).all()
        source_task = db.get(BackgroundTaskRecord, f"tsk_{event_label}")

    assert agency_event is not None
    assert agency_event.status == "processed"
    assert agency_event.processed_at is not None
    assert job is not None
    assert job.status == expected_status
    assert job.title == "Manual smoke job"
    assert job.summary == "Job reached a terminal state."
    assert job.latest_payload["detail"] == {"changed_files": 2}
    assert job_event is not None
    assert job_event.event_type == event_type
    assert job_event.agency_event_id == f"age_{event_label}"
    assert len(wakes) == 1
    assert "Manual smoke job" in wakes[0].payload["note"]
    assert expected_status in wakes[0].payload["note"]
    assert source_task is None


def test_agency_event_received_heartbeat_marks_processed_without_job_or_wake(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_agency_event_task(
                db,
                event_id="heartbeat",
                event_type="heartbeat",
                external_job_id=None,
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        agency_event = db.get(AgencyEventRecord, "age_heartbeat")
        job_count = len(db.scalars(select(JobRecord)).all())
        wake_count = len(
            db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
        )
        source_task = db.get(BackgroundTaskRecord, "tsk_heartbeat")

    assert agency_event is not None
    assert agency_event.status == "processed"
    assert agency_event.processed_at is not None
    assert job_count == 0
    assert wake_count == 0
    assert source_task is None


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("job.queued", "queued"),
        ("job.started", "running"),
        ("job.progress", "running"),
    ],
)
def test_agency_event_received_progress_updates_job_without_wake(
    session_factory: sessionmaker[Session],
    event_type: str,
    expected_status: str,
) -> None:
    event_label = event_type.replace(".", "_")
    with session_factory() as db:
        with db.begin():
            _seed_agency_event_task(
                db,
                event_id=event_label,
                event_type=event_type,
                external_job_id=f"task_{event_label}",
                payload={"title": "Progress job", "summary": "Halfway done."},
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        job = db.scalar(select(JobRecord).where(JobRecord.external_job_id == f"task_{event_label}"))
        job_event = (
            None
            if job is None
            else db.scalar(select(JobEventRecord).where(JobEventRecord.job_id == job.id))
        )
        wake_count = len(
            db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
        )
        source_task = db.get(BackgroundTaskRecord, f"tsk_{event_label}")

    assert job is not None
    assert job.status == expected_status
    assert job_event is not None
    assert job_event.event_type == event_type
    assert wake_count == 0
    assert source_task is None


def test_agency_event_received_waiting_wakes_agent_for_approval_state(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_agency_event_task(
                db,
                event_id="waiting",
                event_type="job.waiting",
                external_job_id="task_waiting",
                payload={"title": "Waiting job", "summary": "Needs input."},
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        job = db.scalar(select(JobRecord).where(JobRecord.external_job_id == "task_waiting"))
        wakes = db.scalars(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
        ).all()
        source_task = db.get(BackgroundTaskRecord, "tsk_waiting")

    assert job is not None
    assert job.status == "waiting_approval"
    assert len(wakes) == 1
    assert "Waiting job" in wakes[0].payload["note"]
    assert "waiting" in wakes[0].payload["note"]
    assert source_task is None


def test_agency_event_received_missing_job_id_fails_event_and_retries_task(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_agency_event_task(
                db,
                event_id="missing_job",
                event_type="job.completed",
                external_job_id=None,
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        agency_event = db.get(AgencyEventRecord, "age_missing_job")
        source_task = db.get(BackgroundTaskRecord, "tsk_missing_job")
        job_count = len(db.scalars(select(JobRecord)).all())

    assert agency_event is not None
    assert agency_event.status == "failed"
    assert agency_event.error == "job event missing external_job_id"
    assert agency_event.processed_at is not None
    assert source_task is None
    assert job_count == 0


def _seed_pending_approval(
    db: Session,
    *,
    session_id: str,
    turn_id: str,
    action_attempt_id: str,
    approval_id: str,
    proposal_index: int,
    expires_at: datetime,
) -> None:
    capability = get_capability("cap.agency.run")
    assert capability is not None
    proposed_input = {
        "repo_root": "/srv/ariel",
        "name": action_attempt_id,
        "prompt": "Run the smoke task.",
    }
    db.add(
        ActionAttemptRecord(
            id=action_attempt_id,
            session_id=session_id,
            turn_id=turn_id,
            proposal_index=proposal_index,
            capability_id=capability.capability_id,
            capability_version=capability.version,
            capability_contract_hash=capability_contract_hash(capability),
            impact_level=capability.impact_level,
            proposed_input=proposed_input,
            payload_hash=payload_hash(
                canonical_action_payload(
                    capability_id=capability.capability_id,
                    input_payload=proposed_input,
                )
            ),
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
            action_attempt_id=action_attempt_id,
            session_id=session_id,
            turn_id=turn_id,
            actor_id="user:smoke",
            status="pending",
            payload_hash=payload_hash(
                canonical_action_payload(
                    capability_id=capability.capability_id,
                    input_payload=proposed_input,
                )
            ),
            expires_at=expires_at,
            decision_reason=None,
            decided_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def test_expire_approvals_task_expires_pending_once_and_rearms(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_expiry",
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
                    id="trn_expiry",
                    session_id="ses_expiry",
                    user_message="approval expiry smoke",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            _seed_pending_approval(
                db,
                session_id="ses_expiry",
                turn_id="trn_expiry",
                action_attempt_id="aat_expired",
                approval_id="apr_expired",
                proposal_index=1,
                expires_at=NOW - timedelta(minutes=5),
            )
            _seed_pending_approval(
                db,
                session_id="ses_expiry",
                turn_id="trn_expiry",
                action_attempt_id="aat_pending",
                approval_id="apr_pending",
                proposal_index=2,
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
            db.add(
                BackgroundTaskRecord(
                    id="tsk_expire_approvals",
                    task_type="expire_approvals",
                    idempotency_key=None,
                    provider_write_receipt_id=None,
                    payload={"origin": "test"},
                    attempts=0,
                    recurrence_seconds=60,
                    run_after=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        expired_approval = db.get(ApprovalRequestRecord, "apr_expired")
        pending_approval = db.get(ApprovalRequestRecord, "apr_pending")
        expired_attempt = db.get(ActionAttemptRecord, "aat_expired")
        pending_attempt = db.get(ActionAttemptRecord, "aat_pending")
        expiry_task = db.get(BackgroundTaskRecord, "tsk_expire_approvals")
        expired_events = db.scalars(
            select(EventRecord).where(EventRecord.event_type == "evt.action.approval.expired")
        ).all()
        first_rearm_after = expiry_task.run_after if expiry_task is not None else None

    assert expired_approval is not None
    assert expired_approval.status == "expired"
    assert expired_approval.decision_reason == "approval_expired"
    assert expired_approval.decided_at is not None
    assert expired_attempt is not None
    assert expired_attempt.status == "expired"
    assert expired_attempt.policy_reason == "approval_expired"
    assert pending_approval is not None
    assert pending_approval.status == "pending"
    assert pending_attempt is not None
    assert pending_attempt.status == "awaiting_approval"
    assert expiry_task is not None
    assert first_rearm_after is not None
    assert first_rearm_after > NOW
    assert len(expired_events) == 1
    assert expired_events[0].payload == {
        "action_attempt_id": "aat_expired",
        "approval_ref": "apr_expired",
        "reason": "approval_expired",
    }

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_expire_approvals")
            assert task is not None
            task.run_after = NOW

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        expired_events = db.scalars(
            select(EventRecord).where(EventRecord.event_type == "evt.action.approval.expired")
        ).all()

    assert len(expired_events) == 1
