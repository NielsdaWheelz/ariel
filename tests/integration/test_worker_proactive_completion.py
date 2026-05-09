from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
import json
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests
from ariel.persistence import (
    ActionAttemptRecord,
    BackgroundTaskRecord,
    ConnectorSubscriptionRecord,
    EmailThreadWatchRecord,
    ProactiveCaseRecord,
    ProactiveObservationRecord,
    SessionRecord,
    TurnRecord,
    WorkspaceItemEventRecord,
    WorkspaceItemRecord,
)
from ariel.worker import enqueue_background_task, process_one_task


class WatchAmbientAdapter:
    def __init__(self) -> None:
        self.candidates: list[dict[str, Any]] = []

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, user_message, history
        assert context_bundle["origin"] == "ambient_interpretation"
        raw_content = input_items[1]["content"]
        assert isinstance(raw_content, str)
        payload = json.loads(raw_content)
        self.candidates = [
            candidate for candidate in payload["candidates"] if isinstance(candidate, dict)
        ]
        candidate = self.candidates[0]
        return {
            "provider": "provider.watch-test",
            "model": "model.watch-test",
            "provider_response_id": "resp_watch_test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "observations": [
                                        {
                                            "candidate_id": str(candidate["candidate_id"]),
                                            "observation_key": "email-thread-watch-due",
                                            "case_key": "email-thread-watch:etw_due",
                                            "observation_type": "email_thread_watch_due",
                                            "subject": "Email thread watch due",
                                            "summary": "The watched thread reached its deadline.",
                                            "payload": {"watch_id": "etw_due"},
                                            "evidence": {"watch_id": "etw_due"},
                                            "rationale": "The watch due signal should resurface.",
                                        }
                                    ],
                                    "omitted": [],
                                    "rationale": "Fixture selected the watch signal.",
                                },
                                sort_keys=True,
                            ),
                        }
                    ],
                }
            ],
        }


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def session_factory(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    try:
        yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    finally:
        engine.dispose()


def test_worker_resurfaces_due_email_thread_watches_through_ambient_proactivity(
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
            watch_events = db.scalars(
                select(WorkspaceItemEventRecord)
                .join(
                    WorkspaceItemRecord,
                    WorkspaceItemEventRecord.workspace_item_id == WorkspaceItemRecord.id,
                )
                .where(
                    WorkspaceItemRecord.provider == "ariel",
                    WorkspaceItemRecord.item_type == "internal_state",
                    WorkspaceItemRecord.external_id == "email-thread-watch:etw_due",
                )
            ).all()
            assert len(watch_events) == 1
            assert watch_events[0].payload["metadata"]["signal"] == "email_thread_watch_due"
            assert watch_events[0].payload["metadata"]["watch_id"] == "etw_due"
            ambient_task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "ambient_interpretation_due"
                )
            )
            assert ambient_task is not None
            assert ambient_task.payload == {"workspace_item_event_id": watch_events[0].id}
            assert ambient_task.status == "pending"

    adapter = WatchAmbientAdapter()
    assert process_one_task(
        session_factory=session_factory,
        settings=cast(Any, AppSettings)(_env_file=None),
        worker_id="w-watch",
        model_adapter=adapter,
    )

    assert len(adapter.candidates) == 1
    assert adapter.candidates[0]["source_type"] == "workspace_item"
    raw_event = adapter.candidates[0]["raw_event"]
    assert raw_event["workspace_item"]["metadata"]["signal"] == "email_thread_watch_due"

    with session_factory() as db:
        with db.begin():
            observation = db.scalar(select(ProactiveObservationRecord))
            case = db.scalar(select(ProactiveCaseRecord))
            proactive_task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "proactive_deliberation_due"
                )
            )
            ambient_task = db.scalar(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "ambient_interpretation_due"
                )
            )
            assert observation is not None
            assert observation.observation_type == "email_thread_watch_due"
            assert observation.payload == {"watch_id": "etw_due"}
            assert case is not None
            assert case.latest_observation_id == observation.id
            assert proactive_task is not None
            assert proactive_task.payload == {"case_id": case.id}
            assert ambient_task is not None
            assert ambient_task.status == "completed"


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


def test_subscription_renewal_task_enqueues_provider_sync_work(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    monkeypatch.setattr("ariel.worker._utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            db.add(
                ConnectorSubscriptionRecord(
                    id="sub_calendar_primary",
                    provider="google",
                    resource_type="calendar",
                    resource_id="primary",
                    channel_id="channel-1",
                    channel_token=None,
                    provider_subscription_id="provider-sub-1",
                    status="active",
                    expires_at=now + timedelta(days=1),
                    renew_after=now,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                )
            )
            enqueue_background_task(
                db,
                task_type="provider_subscription_renewal_due",
                payload={"subscription_id": "sub_calendar_primary"},
                now=now,
                max_attempts=5,
            )

    assert process_one_task(
        session_factory=session_factory,
        settings=cast(Any, AppSettings)(_env_file=None, proactive_worker_max_attempts=4),
        worker_id="w1",
    )

    with session_factory() as db:
        with db.begin():
            subscription = db.get(ConnectorSubscriptionRecord, "sub_calendar_primary")
            assert subscription is not None
            assert subscription.status == "renewal_due"
            assert subscription.renew_after == now
            sync_task = db.scalar(
                select(BackgroundTaskRecord)
                .where(
                    BackgroundTaskRecord.task_type == "provider_sync_due",
                    BackgroundTaskRecord.status == "pending",
                )
                .limit(1)
            )
            assert sync_task is not None
            assert sync_task.payload == {
                "provider": "google",
                "resource_type": "calendar",
                "resource_id": "primary",
                "subscription_id": "sub_calendar_primary",
                "reason": "subscription_renewal_due",
            }
            assert sync_task.max_attempts == 4
