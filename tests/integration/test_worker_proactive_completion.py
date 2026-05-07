from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests
from ariel.persistence import BackgroundTaskRecord, ConnectorSubscriptionRecord
from ariel.worker import enqueue_background_task, process_one_task


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


def test_worker_owns_periodic_ambient_observation_derivation(
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
            assert task.task_type == "workspace_observation_derivation_due"
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
