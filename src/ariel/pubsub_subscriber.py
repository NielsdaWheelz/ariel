"""Gmail Pub/Sub subscriber sidecar.

Runs as the ``ariel-pubsub`` systemd unit. StreamingPull from the Gmail watch
subscription, insert one ``ProviderEventRecord`` row + enqueue one
``provider_event_received`` background task per delivered message, ack. The
worker handles every downstream step.

The runtime SA's JSON key lives on disk; this module enforces chmod 600 at
boot. The subscription itself is operator-provisioned by
``scripts/gcp_provision_pubsub.sh`` — this module verifies it exists and fails
loudly if not.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from types import FrameType
from typing import Any, Literal

from google.cloud import pubsub_v1  # type: ignore[import-untyped]
from google.cloud.pubsub_v1.subscriber.exceptions import (  # type: ignore[import-untyped]
    AcknowledgeError,
)
from google.oauth2 import service_account  # type: ignore[import-untyped]
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .clock import utcnow
from .config import AppSettings
from .db_errors import is_retryable_dbapi_failure, is_unique_constraint_failure
from .ids import new_id
from .google_connector import google_connected_account_subject
from .persistence import (
    GoogleConnectorRecord,
    ProviderEventRecord,
    SubscriberHeartbeatRecord,
    enqueue_background_task,
)

_log = logging.getLogger(__name__)

SUBSCRIBER_NAME = "gmail_pubsub"
_PROVIDER_EVENT_DEDUPE_CONSTRAINT = "provider_events_dedupe_key_key"
_HEARTBEAT_SUBSCRIBER_CONSTRAINT = "uq_subscriber_heartbeat_subscriber_name"


class MalformedPubSubPayload(ValueError):
    pass


class PubSubMessageHandlingFailure(RuntimeError):
    pass


PubSubAckFailureCode = Literal[
    "ack_timeout",
    "ack_rejected",
    "ack_status_not_success",
]


class PubSubAckFailure(PubSubMessageHandlingFailure):
    def __init__(self, *, code: PubSubAckFailureCode, detail: str | None = None) -> None:
        message = code if detail is None else f"{code}:{detail}"
        super().__init__(message)
        self.code = code
        self.detail = detail


class PubSubProviderEventPersistenceFailure(PubSubMessageHandlingFailure):
    def __init__(self) -> None:
        super().__init__("provider_event_write_failed")


class PubSubHeartbeatWriteFailure(RuntimeError):
    def __init__(self) -> None:
        super().__init__("subscriber_heartbeat_write_failed")


ProviderEventDeliveryOutcome = Literal["accepted", "duplicate", "unknown_account"]


@dataclass(frozen=True, slots=True)
class GmailPubSubNotification:
    message_id: str
    data: bytes
    email_address: str
    history_id: str
    publish_time_iso: str | None


def _normalize_gmail_history_id(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return None


def _parse_message_payload(message: Any) -> GmailPubSubNotification:
    message_id_raw = getattr(message, "message_id", None)
    if not isinstance(message_id_raw, str) or not message_id_raw.strip():
        raise MalformedPubSubPayload("message_id_missing")
    message_id = message_id_raw.strip()
    data = getattr(message, "data", None)
    if not isinstance(data, bytes):
        raise MalformedPubSubPayload("data_missing")
    try:
        raw_payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedPubSubPayload("payload_invalid_json") from exc
    if not isinstance(raw_payload, dict):
        raise MalformedPubSubPayload("payload_not_object")
    email_address_raw = raw_payload.get("emailAddress")
    history_id_raw = raw_payload.get("historyId")
    history_id = _normalize_gmail_history_id(history_id_raw)
    if not isinstance(email_address_raw, str) or not email_address_raw.strip():
        raise MalformedPubSubPayload("email_address_invalid")
    if history_id is None:
        raise MalformedPubSubPayload("history_id_invalid")
    publish_time = getattr(message, "publish_time", None)
    return GmailPubSubNotification(
        message_id=message_id,
        data=data,
        email_address=email_address_raw.strip(),
        history_id=history_id,
        publish_time_iso=publish_time.isoformat() if isinstance(publish_time, datetime) else None,
    )


def _ack_message(message: Any) -> None:
    try:
        result = message.ack_with_response().result(timeout=30)
    except FutureTimeoutError as exc:
        raise PubSubAckFailure(code="ack_timeout") from exc
    except AcknowledgeError as exc:
        raise PubSubAckFailure(code="ack_rejected", detail=str(exc)) from exc
    if result != "SUCCESS" and getattr(result, "name", None) != "SUCCESS":
        raise PubSubAckFailure(code="ack_status_not_success", detail=str(result))


def _provider_account_subject(connector: GoogleConnectorRecord | None) -> str | None:
    if connector is None or connector.status != "connected":
        return None
    return google_connected_account_subject(connector)


def handle_message(
    session_factory: sessionmaker[Session],
    message: Any,
) -> None:
    """Process one Pub/Sub message: decode, dedup, insert, enqueue, ack.

    Malformed payload → ack/drop because the Pub/Sub data is immutable and a
    redelivery cannot repair it. Unknown or inactive account → ack/drop.
    Duplicate messageId → ack. DB serialization failure → no ack; Pub/Sub
    redelivers; dedup catches.
    """
    try:
        notification = _parse_message_payload(message)
    except MalformedPubSubPayload as exc:
        message_id = getattr(message, "message_id", "<unknown>")
        _log.warning("malformed Pub/Sub payload (message_id=%s): %s", message_id, exc)
        _ack_message(message)
        return

    dedup_input = f"google:gmail:{notification.email_address}:pubsub:{notification.message_id}"
    dedup_key = "google:" + hashlib.sha256(dedup_input.encode("utf-8")).hexdigest()

    outcome = _persist_provider_event(
        session_factory, notification=notification, dedup_key=dedup_key
    )
    _ack_message(message)
    if outcome == "accepted":
        _write_last_message_heartbeat(session_factory)


def _persist_provider_event(
    session_factory: sessionmaker[Session],
    *,
    notification: GmailPubSubNotification,
    dedup_key: str,
) -> ProviderEventDeliveryOutcome:
    try:
        with session_factory() as db:
            with db.begin():
                connector = db.scalar(
                    select(GoogleConnectorRecord)
                    .where(GoogleConnectorRecord.account_email == notification.email_address)
                    .limit(1)
                )
                provider_account_subject = _provider_account_subject(connector)
                if provider_account_subject is None:
                    _log.info(
                        "Pub/Sub message for unknown or inactive account %s "
                        "(message_id=%s); acking",
                        notification.email_address,
                        notification.message_id,
                    )
                    return "unknown_account"

                existing = db.scalar(
                    select(ProviderEventRecord)
                    .where(ProviderEventRecord.dedupe_key == dedup_key)
                    .with_for_update()
                    .limit(1)
                )
                if existing is not None:
                    return "duplicate"

                now = utcnow()
                event_id = new_id("pev")
                db.add(
                    ProviderEventRecord(
                        id=event_id,
                        provider="google",
                        resource_type="gmail",
                        resource_id=provider_account_subject,
                        external_event_id=f"pubsub:{notification.message_id}",
                        dedupe_key=dedup_key,
                        event_type="pubsub_notification",
                        headers={
                            "pubsub_message_id": notification.message_id,
                            "publish_time": notification.publish_time_iso,
                        },
                        payload={
                            "emailAddress": notification.email_address,
                            "historyId": notification.history_id,
                            "pubsub_message_id": notification.message_id,
                            "publish_time": notification.publish_time_iso,
                        },
                        body_digest=hashlib.sha256(notification.data).hexdigest(),
                        status="accepted",
                        error=None,
                        created_at=now,
                        received_at=now,
                        processed_at=None,
                    )
                )
                enqueue_background_task(
                    db,
                    task_type="provider_event_received",
                    payload={"provider_event_id": event_id},
                    now=now,
                )
    except IntegrityError as exc:
        if not is_unique_constraint_failure(exc, _PROVIDER_EVENT_DEDUPE_CONSTRAINT):
            raise
        raise PubSubProviderEventPersistenceFailure() from exc
    except DBAPIError as exc:
        if not is_retryable_dbapi_failure(exc):
            raise
        raise PubSubProviderEventPersistenceFailure() from exc
    return "accepted"


def _handle_streaming_message(
    session_factory: sessionmaker[Session],
    message: Any,
) -> None:
    try:
        handle_message(session_factory, message)
    except PubSubMessageHandlingFailure:
        # The StreamingPull callback is the subscriber boundary. Do not ack or
        # nack here: unresolved messages stay eligible for Pub/Sub redelivery.
        _log.exception("Pub/Sub message handler failed; message will be redelivered")


def _write_heartbeat(
    session_factory: sessionmaker[Session],
    *,
    last_message: bool = False,
) -> None:
    now = utcnow()
    try:
        with session_factory() as db:
            with db.begin():
                row = db.scalar(
                    select(SubscriberHeartbeatRecord)
                    .where(SubscriberHeartbeatRecord.subscriber_name == SUBSCRIBER_NAME)
                    .with_for_update()
                    .limit(1)
                )
                if row is None:
                    db.add(
                        SubscriberHeartbeatRecord(
                            id=new_id("shb"),
                            subscriber_name=SUBSCRIBER_NAME,
                            last_seen_at=now,
                            last_message_at=now if last_message else None,
                            in_flight_count=0,
                            errors_in_window=0,
                            last_error_code=None,
                            last_error_at=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    return
                row.last_seen_at = now
                if last_message:
                    row.last_message_at = now
                row.updated_at = now
    except IntegrityError as exc:
        if not is_unique_constraint_failure(exc, _HEARTBEAT_SUBSCRIBER_CONSTRAINT):
            raise
        raise PubSubHeartbeatWriteFailure() from exc
    except DBAPIError as exc:
        if not is_retryable_dbapi_failure(exc):
            raise
        raise PubSubHeartbeatWriteFailure() from exc


def _write_last_message_heartbeat(session_factory: sessionmaker[Session]) -> None:
    try:
        _write_heartbeat(session_factory, last_message=True)
    except PubSubHeartbeatWriteFailure:
        # justify-ignore-error: the provider event is durable and acked; the
        # heartbeat loop retries liveness writes independently.
        _log.exception("subscriber heartbeat write failed after Pub/Sub ack")


def _run_heartbeat_tick(session_factory: sessionmaker[Session]) -> None:
    try:
        _write_heartbeat(session_factory)
    except PubSubHeartbeatWriteFailure:
        # justify-ignore-error: heartbeat writes are retried on the next tick.
        _log.exception("subscriber heartbeat write failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = AppSettings()
    if (
        settings.google_pubsub_subscription is None
        or settings.google_application_credentials_path is None
    ):
        raise RuntimeError(
            "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION and "
            "ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH must both be set"
        )

    sa_path = settings.google_application_credentials_path
    sa_stat = os.stat(sa_path)
    if (sa_stat.st_mode & 0o077) != 0:
        raise RuntimeError(f"{sa_path} must be chmod 600 (group/other bits must be 0)")
    credentials = service_account.Credentials.from_service_account_file(sa_path)

    engine = create_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        isolation_level="SERIALIZABLE",
    )
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    subscription_path = settings.google_pubsub_subscription
    # Fail loudly if the subscription or our SA's binding is missing.
    subscriber.get_subscription(subscription=subscription_path)

    flow_control = pubsub_v1.types.FlowControl(
        max_messages=20,
        max_bytes=10 * 1024 * 1024,
        max_lease_duration=600,
    )

    def _callback(message: Any) -> None:
        _handle_streaming_message(session_factory, message)

    future = subscriber.subscribe(
        subscription_path,
        callback=_callback,
        flow_control=flow_control,
    )

    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            _run_heartbeat_tick(session_factory)
            stop_event.wait(settings.subscriber_heartbeat_interval_seconds)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        _log.info("received signal %d; shutting down subscriber", signum)
        stop_event.set()
        future.cancel()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _log.info("Pub/Sub subscriber listening on %s", subscription_path)
    try:
        future.result()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        subscriber.close()
        engine.dispose()


if __name__ == "__main__":
    main()
