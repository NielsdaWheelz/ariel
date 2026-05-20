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
from datetime import UTC, datetime
from types import FrameType
from typing import Any

import ulid
from google.cloud import pubsub_v1  # type: ignore[import-untyped]
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .config import AppSettings
from .persistence import (
    GoogleConnectorRecord,
    ProviderEventRecord,
    SubscriberHeartbeatRecord,
    enqueue_background_task,
)

_log = logging.getLogger(__name__)

SUBSCRIBER_NAME = "gmail_pubsub"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


def handle_message(
    session_factory: sessionmaker[Session],
    message: Any,
) -> None:
    """Process one Pub/Sub message: decode, dedup, insert, enqueue, ack.

    Malformed payload → nack (Pub/Sub redelivers up to ``max_delivery_attempts``,
    then dead-letters). Unknown account → ack (drop). Duplicate messageId → ack.
    DB serialization failure → no ack; Pub/Sub redelivers; dedup catches.
    """
    try:
        decoded = json.loads(message.data.decode("utf-8"))
        email_address = decoded["emailAddress"]
        history_id = str(decoded["historyId"])
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError, AttributeError) as exc:
        _log.warning("malformed Pub/Sub payload (message_id=%s): %s", message.message_id, exc)
        message.nack()
        return

    publish_time = getattr(message, "publish_time", None)
    publish_time_iso = publish_time.isoformat() if isinstance(publish_time, datetime) else None
    dedup_input = f"google:gmail:{email_address}:pubsub:{message.message_id}"
    dedup_key = "google:" + hashlib.sha256(dedup_input.encode("utf-8")).hexdigest()

    with session_factory() as db:
        with db.begin():
            connector = db.scalar(
                select(GoogleConnectorRecord)
                .where(GoogleConnectorRecord.account_email == email_address)
                .limit(1)
            )
            if connector is None or connector.account_subject is None:
                _log.info(
                    "Pub/Sub message for unknown account %s (message_id=%s); acking",
                    email_address,
                    message.message_id,
                )
                message.ack_with_response().result(timeout=30)
                return

            existing = db.scalar(
                select(ProviderEventRecord)
                .where(ProviderEventRecord.dedupe_key == dedup_key)
                .with_for_update()
                .limit(1)
            )
            if existing is not None:
                message.ack_with_response().result(timeout=30)
                return

            now = _utcnow()
            event_id = _new_id("pev")
            db.add(
                ProviderEventRecord(
                    id=event_id,
                    provider="google",
                    resource_type="gmail",
                    resource_id=connector.account_subject,
                    external_event_id=f"pubsub:{message.message_id}",
                    dedupe_key=dedup_key,
                    event_type="pubsub_notification",
                    headers={
                        "pubsub_message_id": message.message_id,
                        "publish_time": publish_time_iso,
                    },
                    payload={
                        "emailAddress": email_address,
                        "historyId": history_id,
                        "pubsub_message_id": message.message_id,
                        "publish_time": publish_time_iso,
                    },
                    body_digest=hashlib.sha256(message.data).hexdigest(),
                    status="accepted",
                    error=None,
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

    message.ack_with_response().result(timeout=30)
    _write_heartbeat(session_factory, last_message=True)


def _write_heartbeat(
    session_factory: sessionmaker[Session],
    *,
    last_message: bool = False,
) -> None:
    now = _utcnow()
    with session_factory() as db:
        with db.begin():
            row = db.get(SubscriberHeartbeatRecord, SUBSCRIBER_NAME)
            if row is None:
                db.add(
                    SubscriberHeartbeatRecord(
                        subscriber_name=SUBSCRIBER_NAME,
                        last_seen_at=now,
                        last_message_at=now if last_message else None,
                        in_flight_count=0,
                        errors_in_window=0,
                        last_error_code=None,
                        last_error_at=None,
                        updated_at=now,
                    )
                )
                return
            row.last_seen_at = now
            if last_message:
                row.last_message_at = now
            row.updated_at = now


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
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path

    engine = create_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        isolation_level="SERIALIZABLE",
    )
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = settings.google_pubsub_subscription
    # Fail loudly if the subscription or our SA's binding is missing.
    subscriber.get_subscription(subscription=subscription_path)

    flow_control = pubsub_v1.types.FlowControl(
        max_messages=20,
        max_bytes=10 * 1024 * 1024,
        max_lease_duration=600,
    )

    def _callback(message: Any) -> None:
        try:
            handle_message(session_factory, message)
        except Exception:
            _log.exception("Pub/Sub message handler raised; message will be redelivered")

    future = subscriber.subscribe(
        subscription_path,
        callback=_callback,
        flow_control=flow_control,
    )

    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            try:
                _write_heartbeat(session_factory)
            except Exception:
                _log.exception("subscriber heartbeat write failed")
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
