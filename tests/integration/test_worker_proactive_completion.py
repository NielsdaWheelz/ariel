from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariel.config import AppSettings
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    EmailThreadWatchRecord,
    SessionRecord,
    TurnRecord,
)
from ariel.worker import enqueue_background_task, process_one_task


def test_worker_marks_due_email_thread_watches_without_ambient_bridge(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_email_due",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_email_due",
                    session_id="ses_email_due",
                    user_message="watch email",
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_email_due",
                    session_id="ses_email_due",
                    turn_id="trn_email_due",
                    proposal_index=1,
                    capability_id="cap.email.thread_watch.create",
                    capability_version="1.0",
                    capability_contract_hash="h" * 64,
                    impact_level="write_reversible",
                    proposed_input={},
                    payload_hash="p" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            for watch_id, condition, deadline in [
                ("etw_due", "no_reply_by_deadline", now - timedelta(minutes=1)),
                ("etw_completed", "any_reply_arrives", now - timedelta(minutes=1)),
                ("etw_future", "no_reply_by_deadline", now + timedelta(minutes=1)),
            ]:
                db.add(
                    EmailThreadWatchRecord(
                        id=watch_id,
                        provider="google",
                        provider_account_id="con_google",
                        provider_thread_id=f"thr_{watch_id}",
                        anchor_message_id=f"msg_{watch_id}",
                        condition=condition,
                        deadline=deadline,
                        note="watch thread",
                        status="active",
                        idempotency_key=f"key_{watch_id}",
                        created_by_action_attempt_id="aat_email_due",
                        matched_message_id=None,
                        matched_at=None,
                        canceled_at=None,
                        completed_at=None,
                        created_at=now - timedelta(minutes=5),
                        updated_at=now - timedelta(minutes=5),
                    )
                )

    assert process_one_task(
        session_factory=session_factory,
        settings=cast(Any, AppSettings)(_env_file=None),
        worker_id="w-watch",
    )

    with session_factory() as db:
        with db.begin():
            due_watch = db.get(EmailThreadWatchRecord, "etw_due")
            completed_watch = db.get(EmailThreadWatchRecord, "etw_completed")
            future_watch = db.get(EmailThreadWatchRecord, "etw_future")
            assert due_watch is not None
            assert completed_watch is not None
            assert future_watch is not None
            assert due_watch.status == "due"
            assert due_watch.completed_at is None
            assert completed_watch.status == "failed"
            assert completed_watch.completed_at is None
            assert future_watch.status == "active"
            internal_item_count = db.execute(
                text(
                    "SELECT COUNT(*) FROM workspace_items "
                    "WHERE provider = 'ariel' "
                    "AND item_type = 'internal_state' "
                    "AND external_id = 'email-thread-watch:etw_due'"
                )
            ).scalar_one()
            assert internal_item_count == 0
            ambient_task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "ambient_interpretation_due"
                )
            )
            assert ambient_task is None
            proactive_task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "proactive_deliberation_due"
                )
            )
            assert proactive_task is None
            assert db.execute(text("SELECT COUNT(*) FROM proactive_observations")).scalar_one() == 0
            assert db.execute(text("SELECT COUNT(*) FROM proactive_cases")).scalar_one() == 0


def test_worker_processes_pending_provider_sync_before_due_thread_watch_sweep(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    sync_calls: list[dict[str, Any]] = []

    def fake_process_provider_sync_due(**kwargs: Any) -> None:
        sync_calls.append(dict(kwargs["task_payload"]))

    monkeypatch.setattr("ariel.worker.process_provider_sync_due", fake_process_provider_sync_due)
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id="ses_sync_first",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_sync_first",
                    session_id="ses_sync_first",
                    user_message="watch email",
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_sync_first",
                    session_id="ses_sync_first",
                    turn_id="trn_sync_first",
                    proposal_index=1,
                    capability_id="cap.email.thread_watch.create",
                    capability_version="1.0",
                    capability_contract_hash="h" * 64,
                    impact_level="write_reversible",
                    proposed_input={},
                    payload_hash="p" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                EmailThreadWatchRecord(
                    id="etw_sync_first",
                    provider="google",
                    provider_account_id="con_google",
                    provider_thread_id="thr_sync_first",
                    anchor_message_id="msg_sync_first",
                    condition="no_reply_by_deadline",
                    deadline=now - timedelta(minutes=1),
                    note="provider sync should run first",
                    status="active",
                    idempotency_key="key_sync_first",
                    created_by_action_attempt_id="aat_sync_first",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            enqueue_background_task(
                db,
                task_type="provider_sync_due",
                payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
                now=now,
                max_attempts=3,
            )

    assert process_one_task(
        session_factory=session_factory,
        settings=cast(Any, AppSettings)(_env_file=None),
        worker_id="w-sync-first",
    )

    assert sync_calls == [
        {"provider": "google", "resource_type": "gmail", "resource_id": "primary"}
    ]
    with session_factory() as db:
        watch = db.get(EmailThreadWatchRecord, "etw_sync_first")
        assert watch is not None
        assert watch.status == "active"


def test_worker_owns_periodic_ambient_interpretation(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    settings = cast(Any, AppSettings)(
        _env_file=None,
        proactive_ambient_interval_seconds=60,
        proactive_worker_max_attempts=4,
    )

    assert process_one_task(session_factory=session_factory, settings=settings, worker_id="w1")

    with session_factory() as db:
        with db.begin():
            tasks = db.scalars(select(BackgroundTaskRecord)).all()
            assert len(tasks) == 1
            task = tasks[0]
            assert task.task_type == "ambient_interpretation_due"
            assert task.payload == {"origin": "worker_ambient"}
            assert task.status == "completed"
            assert task.attempts == 1
            assert task.max_attempts == 4

    assert not process_one_task(session_factory=session_factory, settings=settings, worker_id="w1")


def test_failed_proactive_tasks_recover_until_retry_budget_is_exhausted(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 1, 12, 30, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            db.add(
                BackgroundTaskRecord(
                    id="tsk_failed_proactive",
                    task_type="proactive_deliberation_due",
                    payload={"case_id": "pca_missing"},
                    status="failed",
                    attempts=1,
                    max_attempts=3,
                    error="previous transient failure",
                    claimed_by=None,
                    run_after=now - timedelta(minutes=1),
                    last_heartbeat=None,
                    created_at=now - timedelta(minutes=2),
                    updated_at=now - timedelta(minutes=1),
                )
            )

    assert process_one_task(
        session_factory=session_factory,
        settings=cast(Any, AppSettings)(_env_file=None),
        worker_id="w1",
    )

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_failed_proactive")
            assert task is not None
            assert task.status == "pending"
            assert task.attempts == 2
            assert task.error == "proactive case not found"
            assert task.run_after > now


def test_legacy_task_type_dead_letters_without_retry(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 1, 12, 45, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            db.execute(text("ALTER TABLE background_tasks DROP CONSTRAINT ck_background_task_type"))
            db.add(
                BackgroundTaskRecord(
                    id="tsk_legacy_attention",
                    task_type="attention_ranking_due",
                    payload={},
                    status="pending",
                    attempts=0,
                    max_attempts=5,
                    error=None,
                    claimed_by=None,
                    run_after=now - timedelta(minutes=1),
                    last_heartbeat=None,
                    created_at=now - timedelta(minutes=2),
                    updated_at=now - timedelta(minutes=1),
                )
            )

    assert process_one_task(
        session_factory=session_factory,
        settings=cast(Any, AppSettings)(_env_file=None),
        worker_id="w1",
    )

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_legacy_attention")
            assert task is not None
            assert task.status == "dead_letter"
            assert task.attempts == 1
            assert task.max_attempts == 5
            assert task.error == "unsupported task type: attention_ranking_due"
            assert task.run_after == now
