"""End-to-end tests for the Gmail Pub/Sub subscriber callback.

Exercises ``pubsub_subscriber.handle_message`` against a real Postgres
(``session_factory`` fixture) and the in-memory ``FakePubSubMessage`` — no real
Google SDK in the loop. The DB writes and the ack/nack ledger on the fake are
the assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ariel import pubsub_subscriber
from ariel.google_connector import GOOGLE_CONNECTOR_ID
from ariel.persistence import (
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    ProviderEventRecord,
    SubscriberHeartbeatRecord,
)
from ariel.pubsub_subscriber import SUBSCRIBER_NAME, handle_message
from tests.fake_pubsub import FakePubSubMessage


_NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)


class _DbDiag:
    def __init__(self, constraint_name: str | None = None) -> None:
        self.constraint_name = constraint_name


class _DbOrig(Exception):
    def __init__(self, *, sqlstate: str, constraint_name: str | None = None) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        self.diag = _DbDiag(constraint_name)


def _seed_connector(
    session_factory: sessionmaker[Session],
    *,
    status: str = "connected",
    account_email: str | None = "user@example.com",
    account_subject: str | None = "sub_user",
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                GoogleConnectorRecord(
                    id=GOOGLE_CONNECTOR_ID,
                    provider="google",
                    status=status,
                    account_subject=account_subject,
                    account_email=account_email,
                    granted_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                    access_token_enc=None,
                    refresh_token_enc=None,
                    access_token_expires_at=None,
                    token_obtained_at=None,
                    encryption_key_version="v1",
                    last_error_code=None,
                    last_error_at=None,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )


def _events_and_tasks(
    session_factory: sessionmaker[Session],
) -> tuple[list[ProviderEventRecord], list[BackgroundTaskRecord]]:
    with session_factory() as db:
        events = list(db.scalars(select(ProviderEventRecord)).all())
        tasks = list(
            db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "provider_event_received"
                )
            ).all()
        )
    return events, tasks


def test_handle_message_happy_path(session_factory: sessionmaker[Session]) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-1",
        data=b'{"emailAddress": "user@example.com", "historyId": "12345"}',
        publish_time=_NOW,
    )

    handle_message(session_factory, message)

    with session_factory() as db:
        events = db.scalars(select(ProviderEventRecord)).all()
        heartbeat = db.scalar(
            select(SubscriberHeartbeatRecord).where(
                SubscriberHeartbeatRecord.subscriber_name == SUBSCRIBER_NAME
            )
        )
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "provider_event_received"
            )
        ).all()

    assert len(events) == 1
    event = events[0]
    assert event.provider == "google"
    assert event.resource_type == "gmail"
    assert event.resource_id == "sub_user"
    assert event.event_type == "pubsub_notification"
    assert event.dedupe_key.startswith("google:")
    assert event.created_at is not None
    assert len(tasks) == 1
    assert tasks[0].payload == {"provider_event_id": event.id}
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []
    assert heartbeat is not None
    assert heartbeat.created_at is not None
    assert heartbeat.last_message_at is not None


def test_handle_message_duplicate_dedups(session_factory: sessionmaker[Session]) -> None:
    _seed_connector(session_factory)
    payload = b'{"emailAddress": "user@example.com", "historyId": "12345"}'
    first = FakePubSubMessage(message_id="pubsub-msg-dup", data=payload, publish_time=_NOW)
    second = FakePubSubMessage(message_id="pubsub-msg-dup", data=payload, publish_time=_NOW)

    handle_message(session_factory, first)
    handle_message(session_factory, second)

    with session_factory() as db:
        events = db.scalars(select(ProviderEventRecord)).all()
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "provider_event_received"
            )
        ).all()

    assert len(events) == 1
    assert len(tasks) == 1
    assert len(first.ack_calls) == 1
    assert len(second.ack_calls) == 1
    assert first.nack_calls == []
    assert second.nack_calls == []


def test_handle_message_malformed_payload_acks_and_drops(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-bad",
        data=b"not-json",
        publish_time=_NOW,
    )

    handle_message(session_factory, message)

    with session_factory() as db:
        events = db.scalars(select(ProviderEventRecord)).all()
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "provider_event_received"
            )
        ).all()

    assert events == []
    assert tasks == []
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []


def test_handle_message_missing_email_field_acks_and_drops(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-no-email",
        data=b'{"historyId": "42"}',
        publish_time=_NOW,
    )

    handle_message(session_factory, message)

    with session_factory() as db:
        events = db.scalars(select(ProviderEventRecord)).all()
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "provider_event_received"
            )
        ).all()

    assert events == []
    assert tasks == []
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"emailAddress": "", "historyId": "1"}',
        b'{"emailAddress": ["user@example.com"], "historyId": "1"}',
        b'{"emailAddress": "user@example.com"}',
        b'{"emailAddress": "user@example.com", "historyId": ""}',
        b'{"emailAddress": "user@example.com", "historyId": 1}',
    ],
)
def test_handle_message_invalid_payload_shape_acks_and_drops(
    session_factory: sessionmaker[Session],
    payload: bytes,
) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-invalid-shape",
        data=payload,
        publish_time=_NOW,
    )

    handle_message(session_factory, message)

    events, tasks = _events_and_tasks(session_factory)
    assert events == []
    assert tasks == []
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []


def test_handle_message_unknown_account_acks_and_drops(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_connector(session_factory, account_email="user@example.com")
    message = FakePubSubMessage(
        message_id="pubsub-msg-stranger",
        data=b'{"emailAddress": "stranger@example.com", "historyId": "1"}',
        publish_time=_NOW,
    )

    handle_message(session_factory, message)

    with session_factory() as db:
        events = db.scalars(select(ProviderEventRecord)).all()
        tasks = db.scalars(
            select(BackgroundTaskRecord).where(
                BackgroundTaskRecord.task_type == "provider_event_received"
            )
        ).all()

    assert events == []
    assert tasks == []
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []


def test_handle_message_inactive_connector_acks_and_drops(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_connector(
        session_factory,
        status="error",
        account_subject="sub_user",
        account_email="user@example.com",
    )
    message = FakePubSubMessage(
        message_id="pubsub-msg-inactive-connector",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
    )

    handle_message(session_factory, message)

    events, tasks = _events_and_tasks(session_factory)
    assert events == []
    assert tasks == []
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []


def test_handle_message_ack_failure_raises_before_success_heartbeat(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-ack-fail",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
        ack_succeeded=False,
    )

    with pytest.raises(pubsub_subscriber.PubSubAckFailure, match="ack_status_not_success"):
        handle_message(session_factory, message)

    with session_factory() as db:
        heartbeat = db.scalar(
            select(SubscriberHeartbeatRecord).where(
                SubscriberHeartbeatRecord.subscriber_name == SUBSCRIBER_NAME
            )
        )

    assert len(message.ack_calls) == 1
    assert message.nack_calls == []
    assert heartbeat is None


def test_handle_message_wraps_retryable_db_failure(
    session_factory: sessionmaker[Session],
) -> None:
    del session_factory

    def fail_session_factory() -> None:
        raise OperationalError("SELECT 1", {}, _DbOrig(sqlstate="40001"))

    message = FakePubSubMessage(
        message_id="pubsub-msg-retryable-db",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
    )

    with pytest.raises(pubsub_subscriber.PubSubProviderEventPersistenceFailure):
        handle_message(cast(sessionmaker[Session], fail_session_factory), message)

    assert message.ack_calls == []
    assert message.nack_calls == []


def test_handle_message_propagates_nonretryable_db_failure(
    session_factory: sessionmaker[Session],
) -> None:
    del session_factory

    def fail_session_factory() -> None:
        raise ProgrammingError("SELECT 1", {}, _DbOrig(sqlstate="42P01"))

    message = FakePubSubMessage(
        message_id="pubsub-msg-schema-error",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
    )

    with pytest.raises(ProgrammingError):
        handle_message(cast(sessionmaker[Session], fail_session_factory), message)

    assert message.ack_calls == []
    assert message.nack_calls == []


def test_streaming_callback_logs_named_failure_and_leaves_message_unacked(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_handler(_session_factory: sessionmaker[Session], _message: Any) -> None:
        raise pubsub_subscriber.PubSubProviderEventPersistenceFailure()

    monkeypatch.setattr(pubsub_subscriber, "handle_message", fail_handler)
    message = FakePubSubMessage(
        message_id="pubsub-msg-handler-fails",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
    )

    with caplog.at_level("ERROR", logger="ariel.pubsub_subscriber"):
        pubsub_subscriber._handle_streaming_message(session_factory, message)

    assert "Pub/Sub message handler failed; message will be redelivered" in caplog.text
    assert caplog.records[-1].exc_info is not None
    assert caplog.records[-1].exc_info[0] is (
        pubsub_subscriber.PubSubProviderEventPersistenceFailure
    )
    assert message.ack_calls == []
    assert message.nack_calls == []


def test_streaming_callback_propagates_unexpected_handler_error(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_handler(_session_factory: sessionmaker[Session], _message: Any) -> None:
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(pubsub_subscriber, "handle_message", fail_handler)
    message = FakePubSubMessage(
        message_id="pubsub-msg-handler-bug",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
    )

    with (
        caplog.at_level("ERROR", logger="ariel.pubsub_subscriber"),
        pytest.raises(RuntimeError, match="programmer bug"),
    ):
        pubsub_subscriber._handle_streaming_message(session_factory, message)

    assert "message will be redelivered" not in caplog.text
    assert message.ack_calls == []
    assert message.nack_calls == []


def test_streaming_callback_logs_ack_failure_without_success_heartbeat(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-streaming-ack-fail",
        data=b'{"emailAddress": "user@example.com", "historyId": "1"}',
        publish_time=_NOW,
        ack_succeeded=False,
    )

    with caplog.at_level("ERROR", logger="ariel.pubsub_subscriber"):
        pubsub_subscriber._handle_streaming_message(session_factory, message)

    events, tasks = _events_and_tasks(session_factory)
    with session_factory() as db:
        heartbeat = db.scalar(
            select(SubscriberHeartbeatRecord).where(
                SubscriberHeartbeatRecord.subscriber_name == SUBSCRIBER_NAME
            )
        )

    assert "Pub/Sub message handler failed; message will be redelivered" in caplog.text
    assert "ack_status_not_success" in caplog.text
    assert len(events) == 1
    assert len(tasks) == 1
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []
    assert heartbeat is None


def test_write_heartbeat_creates_then_updates(session_factory: sessionmaker[Session]) -> None:
    pubsub_subscriber._write_heartbeat(session_factory)

    with session_factory() as db:
        rows = db.scalars(select(SubscriberHeartbeatRecord)).all()
    assert len(rows) == 1
    first_row = rows[0]
    assert first_row.id.startswith("shb_")
    assert len(first_row.id) <= 32
    assert first_row.subscriber_name == SUBSCRIBER_NAME
    assert first_row.created_at is not None
    assert first_row.last_seen_at is not None
    assert first_row.last_message_at is None
    first_seen_at = first_row.last_seen_at

    pubsub_subscriber._write_heartbeat(session_factory)

    with session_factory() as db:
        rows = db.scalars(select(SubscriberHeartbeatRecord)).all()
    assert len(rows) == 1
    second_row = rows[0]
    assert second_row.id == first_row.id
    assert second_row.last_message_at is None
    assert second_row.last_seen_at >= first_seen_at


def test_heartbeat_tick_logs_named_write_failure(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_heartbeat(_session_factory: sessionmaker[Session]) -> None:
        raise pubsub_subscriber.PubSubHeartbeatWriteFailure()

    monkeypatch.setattr(pubsub_subscriber, "_write_heartbeat", fail_heartbeat)

    with caplog.at_level("ERROR", logger="ariel.pubsub_subscriber"):
        pubsub_subscriber._run_heartbeat_tick(session_factory)

    assert "subscriber heartbeat write failed" in caplog.text
    assert caplog.records[-1].exc_info is not None
    assert caplog.records[-1].exc_info[0] is pubsub_subscriber.PubSubHeartbeatWriteFailure


def test_heartbeat_tick_propagates_unexpected_write_error(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_heartbeat(_session_factory: sessionmaker[Session]) -> None:
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(pubsub_subscriber, "_write_heartbeat", fail_heartbeat)

    with (
        caplog.at_level("ERROR", logger="ariel.pubsub_subscriber"),
        pytest.raises(RuntimeError, match="programmer bug"),
    ):
        pubsub_subscriber._run_heartbeat_tick(session_factory)

    assert "subscriber heartbeat write failed" not in caplog.text
