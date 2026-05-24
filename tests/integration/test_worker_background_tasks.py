from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    SessionRecord,
    TurnRecord,
)
from ariel.worker import process_one_task


NOW = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)


def test_agency_event_received_upserts_job_event_wake_and_deletes_task(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                AgencyEventRecord(
                    id="age_success",
                    source="agency.daemon",
                    external_event_id="evt_success",
                    event_type="job.completed",
                    external_job_id="task_success",
                    payload={
                        "title": "Manual smoke job",
                        "summary": "Job completed cleanly.",
                        "detail": {"changed_files": 2},
                    },
                    status="accepted",
                    error=None,
                    created_at=NOW,
                    received_at=NOW,
                    processed_at=None,
                )
            )
            db.add(
                BackgroundTaskRecord(
                    id="tsk_agency_success",
                    task_type="agency_event_received",
                    idempotency_key=None,
                    provider_write_receipt_id=None,
                    payload={"agency_event_id": "age_success"},
                    attempts=0,
                    recurrence_seconds=None,
                    run_after=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    assert process_one_task(session_factory=session_factory) is True

    with session_factory() as db:
        agency_event = db.get(AgencyEventRecord, "age_success")
        job = db.scalar(
            select(JobRecord).where(
                JobRecord.source == "agency.daemon",
                JobRecord.external_job_id == "task_success",
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
        source_task = db.get(BackgroundTaskRecord, "tsk_agency_success")

    assert agency_event is not None
    assert agency_event.status == "processed"
    assert agency_event.processed_at is not None
    assert job is not None
    assert job.status == "succeeded"
    assert job.title == "Manual smoke job"
    assert job.summary == "Job completed cleanly."
    assert job.latest_payload["detail"] == {"changed_files": 2}
    assert job_event is not None
    assert job_event.event_type == "job.completed"
    assert job_event.agency_event_id == "age_success"
    assert len(wakes) == 1
    assert "Manual smoke job" in wakes[0].payload["note"]
    assert "succeeded" in wakes[0].payload["note"]
    assert source_task is None


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
