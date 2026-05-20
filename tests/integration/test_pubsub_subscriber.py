"""End-to-end tests for the Gmail Pub/Sub subscriber callback.

Exercises ``pubsub_subscriber.handle_message`` against a real Postgres
(``session_factory`` fixture) and the in-memory ``FakePubSubMessage`` — no real
Google SDK in the loop. The DB writes and the ack/nack ledger on the fake are
the assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
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


def _seed_connector(
    session_factory: sessionmaker[Session],
    *,
    account_email: str = "user@example.com",
    account_subject: str = "sub_user",
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                GoogleConnectorRecord(
                    id=GOOGLE_CONNECTOR_ID,
                    provider="google",
                    status="connected",
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


def test_handle_message_happy_path(session_factory: sessionmaker[Session]) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-1",
        data=b'{"emailAddress": "user@example.com", "historyId": 12345}',
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
        heartbeat = db.get(SubscriberHeartbeatRecord, SUBSCRIBER_NAME)

    assert len(events) == 1
    event = events[0]
    assert event.provider == "google"
    assert event.resource_type == "gmail"
    assert event.resource_id == "sub_user"
    assert event.event_type == "pubsub_notification"
    assert event.dedupe_key.startswith("google:")
    assert len(tasks) == 1
    assert tasks[0].payload == {"provider_event_id": event.id}
    assert len(message.ack_calls) == 1
    assert message.nack_calls == []
    assert heartbeat is not None
    assert heartbeat.last_message_at is not None


def test_handle_message_duplicate_dedups(session_factory: sessionmaker[Session]) -> None:
    _seed_connector(session_factory)
    payload = b'{"emailAddress": "user@example.com", "historyId": 12345}'
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


def test_handle_message_malformed_payload_nacks(session_factory: sessionmaker[Session]) -> None:
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
    assert message.ack_calls == []
    assert len(message.nack_calls) == 1


def test_handle_message_missing_email_field_nacks(session_factory: sessionmaker[Session]) -> None:
    _seed_connector(session_factory)
    message = FakePubSubMessage(
        message_id="pubsub-msg-no-email",
        data=b'{"historyId": 42}',
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
    assert message.ack_calls == []
    assert len(message.nack_calls) == 1


def test_handle_message_unknown_account_acks_and_drops(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_connector(session_factory, account_email="user@example.com")
    message = FakePubSubMessage(
        message_id="pubsub-msg-stranger",
        data=b'{"emailAddress": "stranger@example.com", "historyId": 1}',
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


def test_write_heartbeat_creates_then_updates(session_factory: sessionmaker[Session]) -> None:
    pubsub_subscriber._write_heartbeat(session_factory)

    with session_factory() as db:
        rows = db.scalars(select(SubscriberHeartbeatRecord)).all()
    assert len(rows) == 1
    first_row = rows[0]
    assert first_row.subscriber_name == SUBSCRIBER_NAME
    assert first_row.last_seen_at is not None
    assert first_row.last_message_at is None
    first_seen_at = first_row.last_seen_at

    pubsub_subscriber._write_heartbeat(session_factory)

    with session_factory() as db:
        rows = db.scalars(select(SubscriberHeartbeatRecord)).all()
    assert len(rows) == 1
    second_row = rows[0]
    assert second_row.last_message_at is None
    assert second_row.last_seen_at >= first_seen_at
