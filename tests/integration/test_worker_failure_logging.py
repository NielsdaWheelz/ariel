"""Regression tests for ``process_one_task``'s failure boundary.

The worker is the boundary for arbitrary task-type dispatch: each arm has
its own failure modes (model errors, sandbox crashes, DB conflicts), and a
single task must never down the worker. The catch must therefore *log* the
full traceback before marking the task failed -- silent swallowing makes
production failures undiagnosable.

These tests exercise the failure boundary against a real Postgres-backed
``background_tasks`` queue.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ariel.persistence import BackgroundTaskRecord
from ariel.worker import MAX_TASK_ATTEMPTS, process_one_task


NOW = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)


def _enqueue_unsupported_task(
    session_factory: sessionmaker[Session],
) -> str:
    """Insert a row whose ``task_type`` is allowed by the DB constraint but not
    handled by the worker's dispatch -- ``google_object_hydration_due`` (a
    type the worker has no arm for) is the canonical example.
    """
    with session_factory() as db:
        with db.begin():
            row = BackgroundTaskRecord(
                id="tsk_unsupported_for_test",
                task_type="google_object_hydration_due",
                idempotency_key=None,
                provider_write_receipt_id=None,
                payload={},
                attempts=0,
                recurrence_seconds=None,
                run_after=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            db.add(row)
    return "tsk_unsupported_for_test"


def test_unsupported_task_logs_and_marks_failed(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unsupported ``task_type`` must surface as an ERROR log line and
    increment the task's ``attempts`` -- never a silent swallow.
    """
    task_id = _enqueue_unsupported_task(session_factory)

    caplog.set_level(logging.ERROR, logger="ariel.worker")
    assert process_one_task(session_factory=session_factory) is True

    # The log must name the task id so an operator can correlate the failure
    # to a row in background_tasks.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(task_id in record.getMessage() for record in error_records), (
        "expected an ERROR log mentioning the failed task id; got: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )

    with session_factory() as db:
        task = db.get(BackgroundTaskRecord, task_id)
        assert task is not None
        assert task.attempts == 1


def test_exception_in_arm_logs_traceback_with_task_type(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task whose arm raises an exception must surface the full traceback
    AND the task type via ``logging.exception`` -- the silent-swallow
    regression that allowed mid-loop failures to be invisible in production.

    We trigger this by enqueuing an ``agency_event_received`` row with no
    ``agency_event_id`` in its payload: the arm raises ``RuntimeError``,
    which the worker's boundary catch must log with its traceback.
    """
    with session_factory() as db:
        with db.begin():
            db.add(
                BackgroundTaskRecord(
                    id="tsk_malformed_agy",
                    task_type="agency_event_received",
                    idempotency_key=None,
                    provider_write_receipt_id=None,
                    payload={},
                    attempts=0,
                    recurrence_seconds=None,
                    run_after=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    caplog.set_level(logging.ERROR, logger="ariel.worker")
    assert process_one_task(session_factory=session_factory) is True

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("tsk_malformed_agy" in m for m in messages), f"task id missing from logs: {messages}"
    assert any("agency_event_received" in m for m in messages), (
        f"task_type missing from logs: {messages}"
    )
    # logging.exception attaches traceback info to the record.
    failure_records = [
        r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info is not None
    ]
    assert failure_records, (
        "expected an exception log record with traceback (logging.exception); "
        "got only plain error messages -- the boundary is still silent-swallowing"
    )

    with session_factory() as db:
        task = db.get(BackgroundTaskRecord, "tsk_malformed_agy")
        assert task is not None
        assert task.attempts == 1


def test_repeated_failures_eventually_delete_one_shot_task(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A one-shot task that fails ``MAX_TASK_ATTEMPTS`` times is deleted.

    This is the existing backoff/give-up behavior. Pairing it with the
    logging assertion guards against a silent-swallow regression: every
    failed attempt must produce a log record so an operator can see why
    the task gave up.
    """
    task_id = _enqueue_unsupported_task(session_factory)

    caplog.set_level(logging.ERROR, logger="ariel.worker")
    for _ in range(MAX_TASK_ATTEMPTS):
        # The exponential-backoff push-out means the row is not due on the
        # next pass; nudge it back to NOW to drive successive attempts.
        with session_factory() as db:
            with db.begin():
                row = db.get(BackgroundTaskRecord, task_id)
                if row is not None:
                    row.run_after = NOW
        process_one_task(session_factory=session_factory)

    with session_factory() as db:
        task = db.get(BackgroundTaskRecord, task_id)
        assert task is None

    # Each retry must have logged; otherwise we'd be back in the silent-swallow regime.
    error_count = sum(1 for r in caplog.records if r.levelno >= logging.ERROR)
    assert error_count >= MAX_TASK_ATTEMPTS, (
        f"expected >= {MAX_TASK_ATTEMPTS} error log records across retries; got {error_count}"
    )
