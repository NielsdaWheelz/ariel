from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ariel.config import AppSettings
from ariel.google_connector import (
    DefaultGoogleOAuthClient,
    DefaultGoogleWorkspaceProvider,
    GOOGLE_CONNECTOR_ID,
    GoogleConnectorRuntime,
    is_typed_google_read_output,
)
from ariel.google_workspace_normalization import normalize_calendar_event
from ariel.persistence import (
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    ProviderEvidenceBlockRecord,
    ProviderEvidenceRecord,
    ProviderEventRecord,
    SyncCursorRecord,
    SyncRunRecord,
    enqueue_background_task,
    to_rfc3339,
)


def _provider_sync_lock_id(*parts: str) -> int:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def _json_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _acquire_provider_sync_lock(
    session_factory: sessionmaker[Session],
    *,
    provider: str,
    resource_type: str,
    resource_id: str,
) -> tuple[Session | None, int | None]:
    lock_db = session_factory()
    bind = lock_db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        lock_db.close()
        return None, None
    lock_id = _provider_sync_lock_id("provider_sync", provider, resource_type, resource_id)
    lock_db.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
    lock_db.commit()
    return lock_db, lock_id


def _release_provider_sync_lock(lock_db: Session | None, lock_id: int | None) -> None:
    if lock_db is None or lock_id is None:
        return
    lock_db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
    lock_db.commit()
    lock_db.close()


def _provider_account_id_for_sync(db: Session) -> str:
    account_subject = db.scalar(
        select(GoogleConnectorRecord.account_subject)
        .where(GoogleConnectorRecord.id == GOOGLE_CONNECTOR_ID)
        .limit(1)
    )
    if isinstance(account_subject, str) and account_subject.strip():
        return account_subject.strip()
    return GOOGLE_CONNECTOR_ID


def process_provider_event_received(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
) -> None:
    provider_event_id = _payload_text(task_payload, "provider_event_id")
    if provider_event_id is None:
        raise RuntimeError("provider_event_received task missing provider_event_id")

    with session_factory() as db:
        with db.begin():
            event = db.scalar(
                select(ProviderEventRecord)
                .where(ProviderEventRecord.id == provider_event_id)
                .with_for_update()
                .limit(1)
            )
            if event is None:
                raise RuntimeError("provider event not found")
            if event.status != "accepted":
                return
            now = now_fn()
            enqueue_background_task(
                db,
                task_type="provider_sync_due",
                payload={
                    "provider_event_id": event.id,
                    "provider": event.provider,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                },
                now=now,
            )


def process_provider_sync_due(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    provider_event_id = _payload_text(task_payload, "provider_event_id")
    provider = _payload_text(task_payload, "provider") or "google"
    resource_type = _payload_text(task_payload, "resource_type")
    resource_id = _payload_text(task_payload, "resource_id") or "primary"
    if provider != "google":
        raise RuntimeError("unsupported provider sync")
    if resource_type not in {"calendar", "gmail", "drive"}:
        raise RuntimeError("provider_sync_due task missing supported resource_type")

    now = now_fn()
    sync_run_id = new_id_fn("syn")
    lock_db, lock_id = _acquire_provider_sync_lock(
        session_factory,
        provider="google",
        resource_type=resource_type,
        resource_id=resource_id,
    )
    try:
        with session_factory() as db:
            with db.begin():
                event: ProviderEventRecord | None = None
                if provider_event_id is not None:
                    event = db.scalar(
                        select(ProviderEventRecord)
                        .where(ProviderEventRecord.id == provider_event_id)
                        .with_for_update()
                        .limit(1)
                    )
                    if event is None:
                        raise RuntimeError("provider event not found")
                    if event.status == "processed":
                        _release_provider_sync_lock(lock_db, lock_id)
                        return
                    resource_type = event.resource_type
                    resource_id = event.resource_id

                cursor = db.scalar(
                    select(SyncCursorRecord)
                    .where(
                        SyncCursorRecord.provider == "google",
                        SyncCursorRecord.resource_type == resource_type,
                        SyncCursorRecord.resource_id == resource_id,
                    )
                    .with_for_update()
                    .limit(1)
                )
                if cursor is None:
                    cursor = SyncCursorRecord(
                        id=new_id_fn("cur"),
                        provider="google",
                        resource_type=resource_type,
                        resource_id=resource_id,
                        cursor_value=None,
                        cursor_version=0,
                        status="ready",
                        last_successful_sync_at=None,
                        last_error_code=None,
                        last_error_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(cursor)
                    db.flush()

                if resource_type == "gmail" and cursor.status == "invalid":
                    db.add(
                        SyncRunRecord(
                            id=sync_run_id,
                            provider="google",
                            resource_type=resource_type,
                            resource_id=resource_id,
                            provider_event_id=event.id if event is not None else None,
                            cursor_before=cursor.cursor_value,
                            cursor_after=None,
                            status="failed",
                            item_count=0,
                            observation_count=0,
                            error="gmail_sync_cursor_invalid",
                            started_at=now,
                            completed_at=now,
                            created_at=now,
                        )
                    )
                    cursor.last_error_code = "gmail_sync_cursor_invalid"
                    cursor.last_error_at = now
                    cursor.updated_at = now
                    if event is not None:
                        event.status = "failed"
                        event.error = "gmail_sync_cursor_invalid"
                    _release_provider_sync_lock(lock_db, lock_id)
                    return

                cursor.status = "syncing"
                cursor.updated_at = now
                db.add(
                    SyncRunRecord(
                        id=sync_run_id,
                        provider="google",
                        resource_type=resource_type,
                        resource_id=resource_id,
                        provider_event_id=event.id if event is not None else None,
                        cursor_before=cursor.cursor_value,
                        cursor_after=None,
                        status="running",
                        item_count=0,
                        observation_count=0,
                        error=None,
                        started_at=now,
                        completed_at=None,
                        created_at=now,
                    )
                )
                cursor_before = cursor.cursor_value
    except Exception:
        _release_provider_sync_lock(lock_db, lock_id)
        raise
    runtime = GoogleConnectorRuntime(
        oauth_client=DefaultGoogleOAuthClient(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            timeout_seconds=settings.google_oauth_timeout_seconds,
        ),
        workspace_provider=DefaultGoogleWorkspaceProvider(),
        redirect_uri=settings.google_oauth_redirect_uri,
        oauth_state_ttl_seconds=settings.google_oauth_state_ttl_seconds,
        encryption_secret=settings.connector_encryption_secret,
        encryption_key_version=settings.connector_encryption_key_version,
        encryption_keys=settings.connector_encryption_keys,
        pubsub_topic=settings.google_pubsub_topic,
        public_webhook_base_url=settings.public_webhook_base_url,
    )

    outputs: list[dict[str, Any]] = []
    sync_provider_account_id = GOOGLE_CONNECTOR_ID
    try:
        sync_capability_id = {
            "calendar": "cap.calendar.list",
            "gmail": "cap.email.search",
            "drive": "cap.drive.search",
        }[resource_type]
        refresh_access_token = getattr(runtime, "refresh_access_token_for_capability", None)
        if callable(refresh_access_token):
            refresh_access_token(
                session_factory=session_factory,
                capability_id=sync_capability_id,
                now_fn=now_fn,
                new_id_fn=new_id_fn,
            )
        with session_factory() as db:
            with db.begin():
                access_token_without_refresh = getattr(
                    runtime,
                    "access_token_for_background_sync_without_refresh",
                    None,
                )
                if callable(access_token_without_refresh):
                    access_token = access_token_without_refresh(db=db, now_fn=now_fn)
                else:
                    access_token = runtime.access_token_for_background_sync(
                        db=db,
                        now_fn=now_fn,
                        new_id_fn=new_id_fn,
                    )
                sync_provider_account_id = _provider_account_id_for_sync(db)
        if resource_type == "calendar":
            page_token: str | None = None
            while True:
                output = runtime.workspace_provider.calendar_list_event_deltas(
                    access_token=access_token,
                    calendar_id=resource_id,
                    sync_token=cursor_before,
                    time_min=None if cursor_before else to_rfc3339(now - timedelta(days=30)),
                    page_token=page_token,
                )
                outputs.append(output)
                page_token = _payload_text(output, "nextPageToken")
                if page_token is None:
                    break
        elif resource_type == "gmail":
            if cursor_before is None:
                history_id = _gmail_bootstrap_history_id(
                    runtime.workspace_provider,
                    access_token=access_token,
                )
                if history_id is None:
                    raise RuntimeError("gmail_sync_cursor_missing")
                outputs.append({"historyId": history_id, "history": []})
            else:
                page_token = None
                while True:
                    output = runtime.workspace_provider.email_list_history(
                        access_token=access_token,
                        start_history_id=cursor_before,
                        page_token=page_token,
                    )
                    outputs.append(output)
                    page_token = _payload_text(output, "nextPageToken")
                    if page_token is None:
                        break
        else:
            if cursor_before is None:
                output = runtime.workspace_provider.drive_get_start_page_token(
                    access_token=access_token
                )
                outputs.append(output)
            else:
                page_token = cursor_before
                while True:
                    output = runtime.workspace_provider.drive_list_changes(
                        access_token=access_token,
                        page_token=page_token,
                    )
                    outputs.append(output)
                    page_token = _payload_text(output, "nextPageToken")
                    if page_token is None:
                        break
    except Exception as exc:
        error = str(exc)
        if _is_stale_cursor_error(resource_type=resource_type, error=error):
            # A stale delta cursor — Gmail 404 or Calendar 410. Clear the
            # cursor and re-enqueue a full sync; the next run bootstraps.
            _handle_stale_cursor(
                session_factory=session_factory,
                provider_event_id=provider_event_id,
                sync_run_id=sync_run_id,
                resource_type=resource_type,
                resource_id=resource_id,
                error=error,
                now=now_fn(),
            )
            _release_provider_sync_lock(lock_db, lock_id)
            return
        _mark_sync_failed(
            session_factory=session_factory,
            provider_event_id=provider_event_id,
            sync_run_id=sync_run_id,
            resource_type=resource_type,
            resource_id=resource_id,
            error=error,
            now=now_fn(),
        )
        _release_provider_sync_lock(lock_db, lock_id)
        raise

    try:
        gmail_read_outputs: dict[str, dict[str, Any]] = {}
        if resource_type == "gmail":
            email_read = getattr(runtime.workspace_provider, "email_read", None)
            seen_message_ids: set[str] = set()
            for output in outputs:
                raw_histories = output.get("history")
                histories = raw_histories if isinstance(raw_histories, list) else []
                for history in histories:
                    if not isinstance(history, dict):
                        continue
                    for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                        raw_entries = history.get(key)
                        entries = raw_entries if isinstance(raw_entries, list) else []
                        for entry in entries:
                            message = entry.get("message") if isinstance(entry, dict) else None
                            if not isinstance(message, dict):
                                continue
                            message_id = _payload_text(message, "id")
                            if message_id is None or message_id in seen_message_ids:
                                continue
                            seen_message_ids.add(message_id)
                            if not callable(email_read):
                                raise RuntimeError("gmail_sync_email_read_unavailable")
                            try:
                                read_output = email_read(
                                    access_token=access_token,
                                    normalized_input={
                                        "message_id": message_id,
                                        "thread_id": None,
                                        "mode": "message",
                                    },
                                )
                            except RuntimeError as exc:
                                reason = str(exc).strip()
                                if reason == "resource_not_found":
                                    reason_code = "gmail_message_unavailable"
                                    status = "no_body"
                                    recovery = "The message changed or disappeared before sync could read it."
                                elif reason == "google_response_too_large":
                                    reason_code = "gmail_body_too_large"
                                    status = "body_too_large"
                                    recovery = (
                                        "Use narrower message context or ask for metadata only."
                                    )
                                else:
                                    raise
                                read_output = {
                                    "schema_version": "google.gmail.message_evidence.v1",
                                    "mode": "message",
                                    "message": {
                                        "provider_account_id": sync_provider_account_id,
                                        "message_id": message_id,
                                        "thread_id": _payload_text(message, "threadId"),
                                        "history_id": None,
                                        "rfc_message_id": None,
                                        "in_reply_to": None,
                                        "references": [],
                                        "subject": None,
                                        "subject_key": None,
                                        "sender": None,
                                        "recipients": [],
                                        "cc": [],
                                        "header_date": None,
                                        "internal_date": None,
                                        "label_ids": [
                                            label_id
                                            for label_id in message.get("labelIds", [])
                                            if isinstance(label_id, str)
                                        ]
                                        if isinstance(message.get("labelIds"), list)
                                        else [],
                                        "direction": "unknown",
                                        "provider_url": (
                                            f"https://mail.google.com/mail/u/0/#all/{message_id}"
                                        ),
                                        "raw_payload_digest": hashlib.sha256(
                                            message_id.encode("utf-8")
                                        ).hexdigest(),
                                        "attachments": [],
                                    },
                                    "published_at": None,
                                    "evidence": {
                                        "source_kind": "gmail_message",
                                        "message_id": message_id,
                                        "thread_id": _payload_text(message, "threadId"),
                                        "blocks": [],
                                        "truncated": False,
                                        "decode_notes": [reason_code],
                                        "html_security": {},
                                    },
                                    "read_outcome": {
                                        "status": status,
                                        "reason_code": reason_code,
                                        "recovery": recovery,
                                    },
                                    "retrieved_at": to_rfc3339(now_fn()),
                                }
                                if isinstance(read_output, dict):
                                    read_message = read_output.get("message")
                                    if isinstance(read_message, dict):
                                        read_message["provider_account_id"] = (
                                            sync_provider_account_id
                                        )
                            if not _gmail_sync_read_output_valid(read_output):
                                raise RuntimeError("gmail_sync_read_output_invalid")
                            gmail_read_outputs[message_id] = read_output
    except Exception as exc:
        _mark_sync_failed(
            session_factory=session_factory,
            provider_event_id=provider_event_id,
            sync_run_id=sync_run_id,
            resource_type=resource_type,
            resource_id=resource_id,
            error=str(exc),
            now=now_fn(),
        )
        _release_provider_sync_lock(lock_db, lock_id)
        raise

    try:
        with session_factory() as db:
            with db.begin():
                event = (
                    db.scalar(
                        select(ProviderEventRecord)
                        .where(ProviderEventRecord.id == provider_event_id)
                        .with_for_update()
                        .limit(1)
                    )
                    if provider_event_id is not None
                    else None
                )
                cursor = db.scalar(
                    select(SyncCursorRecord)
                    .where(
                        SyncCursorRecord.provider == "google",
                        SyncCursorRecord.resource_type == resource_type,
                        SyncCursorRecord.resource_id == resource_id,
                    )
                    .with_for_update()
                    .limit(1)
                )
                run = db.scalar(
                    select(SyncRunRecord)
                    .where(SyncRunRecord.id == sync_run_id)
                    .with_for_update()
                    .limit(1)
                )
                if cursor is None or run is None:
                    raise RuntimeError("sync state missing")
                if cursor.cursor_value != cursor_before:
                    run.status = "failed"
                    run.error = "sync_cursor_changed"
                    run.completed_at = now_fn()
                    cursor.status = "ready"
                    cursor.updated_at = now_fn()
                    if event is not None:
                        event.status = "failed"
                        event.error = "sync_cursor_changed"
                    _release_provider_sync_lock(lock_db, lock_id)
                    return

                now = now_fn()
                item_count = 0
                observation_count = 0
                cursor_after = cursor_before
                provider_account_id = sync_provider_account_id
                if resource_type == "calendar":
                    for output in outputs:
                        cursor_after = _payload_text(output, "nextSyncToken") or cursor_after
                        raw_items = output.get("items")
                        items = raw_items if isinstance(raw_items, list) else []
                        for item in items:
                            if isinstance(item, dict) and _sync_calendar_item(
                                db,
                                cast(dict[str, Any], item),
                                resource_id=resource_id,
                                provider_account_id=provider_account_id,
                                provider_event_id=provider_event_id,
                                now=now,
                                new_id_fn=new_id_fn,
                            ):
                                item_count += 1
                elif resource_type == "gmail":
                    for output in outputs:
                        cursor_after = _payload_text(output, "historyId") or cursor_after
                        raw_histories = output.get("history")
                        histories = raw_histories if isinstance(raw_histories, list) else []
                        for history in histories:
                            if isinstance(history, dict):
                                added, observations = _sync_gmail_history(
                                    db,
                                    cast(dict[str, Any], history),
                                    resource_id=resource_id,
                                    provider_account_id=provider_account_id,
                                    provider_event_id=provider_event_id,
                                    now=now,
                                    new_id_fn=new_id_fn,
                                    gmail_read_outputs=gmail_read_outputs,
                                )
                                item_count += added
                                observation_count += observations
                else:
                    for output in outputs:
                        cursor_after = (
                            _payload_text(output, "newStartPageToken")
                            or _payload_text(output, "startPageToken")
                            or cursor_after
                        )
                        raw_changes = output.get("changes")
                        changes = raw_changes if isinstance(raw_changes, list) else []
                        for change in changes:
                            if isinstance(change, dict) and _sync_drive_change(
                                db,
                                cast(dict[str, Any], change),
                                resource_id=resource_id,
                                provider_event_id=provider_event_id,
                                now=now,
                                new_id_fn=new_id_fn,
                            ):
                                item_count += 1

                cursor.cursor_value = cursor_after
                cursor.cursor_version += 1 if cursor_after != cursor_before else 0
                cursor.status = "ready"
                cursor.last_successful_sync_at = now
                cursor.last_error_code = None
                cursor.last_error_at = None
                cursor.updated_at = now
                run.cursor_after = cursor_after
                run.status = "succeeded"
                run.item_count = item_count
                run.observation_count = observation_count
                run.completed_at = now
                if event is not None:
                    event.status = "processed"
                    event.processed_at = now
                # New or changed provider data wakes the agent: the single
                # convergence point for both the push and poll paths.
                if item_count > 0:
                    enqueue_background_task(
                        db,
                        task_type="agent_wake",
                        payload={"note": _provider_sync_wake_note(resource_type, item_count)},
                        now=now,
                    )
    except Exception:
        _release_provider_sync_lock(lock_db, lock_id)
        raise
    _release_provider_sync_lock(lock_db, lock_id)


def _sync_calendar_item(
    db: Session,
    item: dict[str, Any],
    *,
    resource_id: str,
    provider_account_id: str,
    provider_event_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> bool:
    del provider_event_id
    external_id = _payload_text(item, "id")
    if external_id is None:
        return False
    status = "deleted" if item.get("status") == "cancelled" else "active"
    updated = _payload_text(item, "updated") or to_rfc3339(now)
    normalized = normalize_calendar_event(
        item,
        provider_account_id=provider_account_id,
        calendar_id=resource_id,
    )
    metadata = {
        "summary": normalized.summary,
        "status": normalized.status,
        "start": asdict(normalized.start),
        "end": asdict(normalized.end),
        "all_day": normalized.all_day,
        "attendees": [asdict(attendee) for attendee in normalized.attendees],
        "organizer": asdict(normalized.organizer) if normalized.organizer else None,
        "creator": asdict(normalized.creator) if normalized.creator else None,
        "ical_uid": normalized.ical_uid,
        "recurring_event_id": normalized.recurring_event_id,
        "recurrence": list(normalized.recurrence),
        "location": normalized.location,
        "conference_data": normalized.conference_data,
        "reminders": normalized.reminders,
        "updated": normalized.updated,
        "etag": normalized.etag,
        "provider_url": normalized.provider_url,
        "hangout_link": normalized.hangout_link,
        "raw_payload_digest": normalized.raw_payload_digest,
    }
    content_digest = normalized.raw_payload_digest
    provider_object = db.scalar(
        select(GoogleProviderObjectRecord)
        .where(
            GoogleProviderObjectRecord.provider_account_id == provider_account_id,
            GoogleProviderObjectRecord.object_type == "calendar_event",
            GoogleProviderObjectRecord.calendar_id == resource_id,
            GoogleProviderObjectRecord.external_id == external_id,
        )
        .with_for_update()
        .limit(1)
    )
    if provider_object is None:
        provider_object = GoogleProviderObjectRecord(
            id=new_id_fn("gpo"),
            provider_account_id=provider_account_id,
            object_type="calendar_event",
            external_id=external_id,
            thread_external_id=None,
            calendar_id=resource_id,
            ical_uid=normalized.ical_uid,
            status=status,
            source_timestamp=_parse_provider_timestamp(updated),
            observed_at=now,
            provider_url=normalized.provider_url,
            metadata_json=metadata,
            content_digest=content_digest,
            created_at=now,
            updated_at=now,
        )
        db.add(provider_object)
        db.flush()
    else:
        provider_object.status = status
        provider_object.source_timestamp = _parse_provider_timestamp(updated)
        provider_object.observed_at = now
        provider_object.provider_url = normalized.provider_url
        provider_object.metadata_json = metadata
        provider_object.content_digest = content_digest
        provider_object.updated_at = now

    if status == "deleted":
        evidence_rows = db.scalars(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.provider_object_id == provider_object.id,
                ProviderEvidenceRecord.lifecycle_state != "deleted",
                ProviderEvidenceRecord.content_digest != content_digest,
            )
            .with_for_update()
        ).all()
        for evidence_row in evidence_rows:
            evidence_row.lifecycle_state = "deleted"
            evidence_row.updated_at = now
        cancellation_evidence = db.scalar(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.provider_object_id == provider_object.id,
                ProviderEvidenceRecord.content_digest == content_digest,
            )
            .limit(1)
        )
        if cancellation_evidence is None:
            cancellation_evidence = ProviderEvidenceRecord(
                id=new_id_fn("pev"),
                provider_object_id=provider_object.id,
                provider="google",
                provider_account_id=provider_account_id,
                source_kind="calendar_event",
                external_id=external_id,
                thread_external_id=None,
                calendar_id=resource_id,
                source_uri=normalized.provider_url,
                source_timestamp=_parse_provider_timestamp(updated),
                content_digest=content_digest,
                metadata_json=metadata,
                taint="provider_untrusted",
                sensitivity="private",
                retention_policy="provider_source",
                extraction_status="not_actionable",
                lifecycle_state="deleted",
                observed_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(cancellation_evidence)
        else:
            cancellation_evidence.lifecycle_state = "deleted"
            cancellation_evidence.extraction_status = "not_actionable"
            cancellation_evidence.updated_at = now
        return True

    evidence = db.scalar(
        select(ProviderEvidenceRecord)
        .where(
            ProviderEvidenceRecord.provider_object_id == provider_object.id,
            ProviderEvidenceRecord.content_digest == content_digest,
        )
        .with_for_update()
        .limit(1)
    )
    if evidence is not None and evidence.lifecycle_state != "available":
        return True
    if evidence is None:
        superseded_rows = db.scalars(
            select(ProviderEvidenceRecord)
            .where(
                ProviderEvidenceRecord.provider_object_id == provider_object.id,
                ProviderEvidenceRecord.lifecycle_state == "available",
            )
            .with_for_update()
        ).all()
        for superseded_row in superseded_rows:
            superseded_row.lifecycle_state = "superseded"
            superseded_row.updated_at = now
        evidence = ProviderEvidenceRecord(
            id=new_id_fn("pev"),
            provider_object_id=provider_object.id,
            provider="google",
            provider_account_id=provider_account_id,
            source_kind="calendar_event",
            external_id=external_id,
            thread_external_id=None,
            calendar_id=resource_id,
            source_uri=normalized.provider_url,
            source_timestamp=_parse_provider_timestamp(updated),
            content_digest=content_digest,
            metadata_json=metadata,
            taint="provider_untrusted",
            sensitivity="private",
            retention_policy="provider_source",
            extraction_status="pending",
            lifecycle_state="available",
            observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(evidence)
        db.flush()
        existing_block_id = db.scalar(
            select(ProviderEvidenceBlockRecord.id)
            .where(ProviderEvidenceBlockRecord.evidence_id == evidence.id)
            .limit(1)
        )
        if existing_block_id is None:
            for index, block in enumerate(normalized.description_blocks):
                db.add(
                    ProviderEvidenceBlockRecord(
                        id=new_id_fn("peb"),
                        evidence_id=evidence.id,
                        block_index=index,
                        block_kind="calendar_description",
                        text=block.text,
                        digest=block.digest,
                        source_offsets={"block_id": block.block_id},
                        metadata_json={
                            "source_mime_type": block.source_mime_type,
                            "truncated": block.truncated,
                        },
                        created_at=now,
                    )
                )
    elif evidence.metadata_json != metadata:
        evidence.metadata_json = metadata
        evidence.extraction_status = "pending"
        evidence.observed_at = now
        evidence.updated_at = now
    return True


def _gmail_bootstrap_history_id(
    workspace_provider: object,
    *,
    access_token: str,
) -> str | None:
    request_json = getattr(workspace_provider, "_request_json", None)
    gmail_api_base_url = getattr(workspace_provider, "gmail_api_base_url", None)
    if not callable(request_json) or not isinstance(gmail_api_base_url, str):
        return None
    payload = request_json(
        method="GET",
        url=f"{gmail_api_base_url}/users/me/profile",
        access_token=access_token,
    )
    return _payload_text(payload, "historyId") if isinstance(payload, dict) else None


def _gmail_sync_read_output_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if is_typed_google_read_output(capability_id="cap.email.read", payload=value):
        return True
    if value.get("schema_version") != "google.gmail.message_evidence.v1":
        return False
    if not isinstance(value.get("retrieved_at"), str) or not value["retrieved_at"].strip():
        return False
    read_outcome = value.get("read_outcome")
    if not isinstance(read_outcome, dict):
        return False
    if read_outcome.get("status") not in {"body_too_large", "decode_failed", "no_body"}:
        return False
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("source_kind") != "gmail_message":
        return False
    blocks = evidence.get("blocks")
    if not isinstance(blocks, list) or blocks:
        return False
    message = value.get("message")
    if not isinstance(message, dict):
        return False
    message_id = message.get("message_id")
    return isinstance(message_id, str) and bool(message_id.strip())


def _parse_provider_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sync_gmail_history(
    db: Session,
    history: dict[str, Any],
    *,
    resource_id: str,
    provider_account_id: str,
    provider_event_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
    gmail_read_outputs: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    del resource_id, provider_event_id
    history_id = _payload_text(history, "id") or to_rfc3339(now)
    item_count = 0
    observation_count = 0
    for key in ("messagesAdded", "labelsAdded", "labelsRemoved", "messagesDeleted"):
        raw_entries = history.get(key)
        entries = raw_entries if isinstance(raw_entries, list) else []
        for entry in entries:
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict):
                continue
            message_id = _payload_text(message, "id")
            if message_id is None:
                continue
            thread_id = _payload_text(message, "threadId")
            message_received_at = _gmail_message_received_at(message, fallback=now)
            label_ids_raw = message.get("labelIds")
            label_ids = (
                [label_id for label_id in label_ids_raw if isinstance(label_id, str)]
                if isinstance(label_ids_raw, list)
                else []
            )
            object_status = "deleted" if key == "messagesDeleted" else "active"
            content_digest = _json_digest(message)
            read_output = gmail_read_outputs.get(message_id) if key != "messagesDeleted" else None
            read_message = read_output.get("message") if isinstance(read_output, dict) else None
            read_evidence = read_output.get("evidence") if isinstance(read_output, dict) else None
            read_outcome = read_output.get("read_outcome") if isinstance(read_output, dict) else {}
            read_status = read_outcome.get("status") if isinstance(read_outcome, dict) else None
            if isinstance(read_message, dict) and isinstance(read_evidence, dict):
                read_thread_id = _payload_text(read_message, "thread_id")
                thread_id = read_thread_id or thread_id
                raw_payload_digest = _payload_text(read_message, "raw_payload_digest")
                content_digest = raw_payload_digest or content_digest
                published_at = (
                    _parse_provider_timestamp(_payload_text(read_output, "published_at"))
                    if isinstance(read_output, dict)
                    else None
                )
                if published_at is not None:
                    message_received_at = published_at
            provider_object = db.scalar(
                select(GoogleProviderObjectRecord)
                .where(
                    GoogleProviderObjectRecord.provider_account_id == provider_account_id,
                    GoogleProviderObjectRecord.object_type == "gmail_message",
                    GoogleProviderObjectRecord.external_id == message_id,
                )
                .with_for_update()
                .limit(1)
            )
            if provider_object is None:
                provider_object = GoogleProviderObjectRecord(
                    id=new_id_fn("gpo"),
                    provider_account_id=provider_account_id,
                    object_type="gmail_message",
                    external_id=message_id,
                    thread_external_id=thread_id,
                    calendar_id=None,
                    ical_uid=None,
                    status=object_status,
                    source_timestamp=message_received_at,
                    observed_at=now,
                    provider_url=_payload_text(read_message, "provider_url")
                    if isinstance(read_message, dict)
                    else f"https://mail.google.com/mail/u/0/#all/{message_id}",
                    metadata_json={
                        "history_id": history_id,
                        "label_ids": label_ids,
                        "change": key,
                        "subject": read_message.get("subject")
                        if isinstance(read_message, dict)
                        else None,
                        "subject_key": read_message.get("subject_key")
                        if isinstance(read_message, dict)
                        else None,
                        "direction": read_message.get("direction")
                        if isinstance(read_message, dict)
                        else None,
                        "attachments": read_message.get("attachments")
                        if isinstance(read_message, dict)
                        else None,
                        "read_outcome": read_outcome if isinstance(read_outcome, dict) else None,
                    },
                    content_digest=content_digest,
                    created_at=now,
                    updated_at=now,
                )
                db.add(provider_object)
                db.flush()
            else:
                provider_object.thread_external_id = thread_id
                provider_object.status = object_status
                provider_object.source_timestamp = message_received_at
                provider_object.observed_at = now
                provider_object.provider_url = (
                    _payload_text(read_message, "provider_url")
                    if isinstance(read_message, dict)
                    else f"https://mail.google.com/mail/u/0/#all/{message_id}"
                )
                provider_object.metadata_json = {
                    "history_id": history_id,
                    "label_ids": label_ids,
                    "change": key,
                    "subject": read_message.get("subject")
                    if isinstance(read_message, dict)
                    else None,
                    "subject_key": read_message.get("subject_key")
                    if isinstance(read_message, dict)
                    else None,
                    "direction": read_message.get("direction")
                    if isinstance(read_message, dict)
                    else None,
                    "attachments": read_message.get("attachments")
                    if isinstance(read_message, dict)
                    else None,
                    "read_outcome": read_outcome if isinstance(read_outcome, dict) else None,
                }
                provider_object.content_digest = content_digest
                provider_object.updated_at = now
            item_count += 1
            if object_status == "deleted":
                evidence_rows = db.scalars(
                    select(ProviderEvidenceRecord)
                    .where(
                        ProviderEvidenceRecord.provider_object_id == provider_object.id,
                        ProviderEvidenceRecord.lifecycle_state != "deleted",
                    )
                    .with_for_update()
                ).all()
                for evidence_row in evidence_rows:
                    evidence_row.lifecycle_state = "deleted"
                    evidence_row.updated_at = now
                observation_count += len(evidence_rows)
            elif read_status != "ok" and isinstance(read_outcome, dict):
                unavailable_digest = content_digest
                if isinstance(read_evidence, dict):
                    body_digest = _payload_text(read_evidence, "body_digest")
                    unavailable_digest = body_digest or unavailable_digest
                existing_unavailable = db.scalar(
                    select(ProviderEvidenceRecord)
                    .where(
                        ProviderEvidenceRecord.provider_object_id == provider_object.id,
                        ProviderEvidenceRecord.content_digest == unavailable_digest,
                    )
                    .with_for_update()
                    .limit(1)
                )
                unavailable_metadata = {
                    "history_id": history_id,
                    "label_ids": label_ids,
                    "change": key,
                    "read_outcome": read_outcome,
                    "decode_notes": read_evidence.get("decode_notes", [])
                    if isinstance(read_evidence, dict)
                    else [],
                    "html_security": read_evidence.get("html_security")
                    if isinstance(read_evidence, dict)
                    else None,
                }
                if existing_unavailable is None:
                    db.add(
                        ProviderEvidenceRecord(
                            id=new_id_fn("pev"),
                            provider_object_id=provider_object.id,
                            provider="google",
                            provider_account_id=provider_account_id,
                            source_kind="gmail_message",
                            external_id=message_id,
                            thread_external_id=thread_id,
                            calendar_id=None,
                            source_uri=provider_object.provider_url,
                            source_timestamp=message_received_at,
                            content_digest=unavailable_digest,
                            metadata_json=unavailable_metadata,
                            taint="provider_untrusted",
                            sensitivity="private",
                            retention_policy="provider_source",
                            extraction_status="failed",
                            lifecycle_state="unavailable",
                            observed_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                elif existing_unavailable.lifecycle_state == "unavailable":
                    existing_unavailable.thread_external_id = thread_id
                    existing_unavailable.source_uri = provider_object.provider_url
                    existing_unavailable.source_timestamp = message_received_at
                    existing_unavailable.metadata_json = unavailable_metadata
                    existing_unavailable.lifecycle_state = "unavailable"
                    existing_unavailable.extraction_status = "failed"
                    existing_unavailable.observed_at = now
                    existing_unavailable.updated_at = now
                observation_count += 1
            if read_status == "ok" and isinstance(read_evidence, dict):
                evidence_content_digest = content_digest
                body_digest = _payload_text(read_evidence, "body_digest")
                if body_digest is not None:
                    evidence_content_digest = body_digest
                existing_evidence = db.scalar(
                    select(ProviderEvidenceRecord)
                    .where(
                        ProviderEvidenceRecord.provider_object_id == provider_object.id,
                        ProviderEvidenceRecord.content_digest == evidence_content_digest,
                    )
                    .with_for_update()
                    .limit(1)
                )
                evidence_metadata = {
                    "history_id": history_id,
                    "label_ids": label_ids,
                    "change": key,
                    "decode_notes": read_evidence.get("decode_notes", []),
                    "html_security": read_evidence.get("html_security"),
                }
                if (
                    existing_evidence is not None
                    and existing_evidence.lifecycle_state != "available"
                ):
                    continue
                if existing_evidence is None:
                    superseded_rows = db.scalars(
                        select(ProviderEvidenceRecord)
                        .where(
                            ProviderEvidenceRecord.provider_object_id == provider_object.id,
                            ProviderEvidenceRecord.content_digest != evidence_content_digest,
                            ProviderEvidenceRecord.lifecycle_state == "available",
                        )
                        .with_for_update()
                    ).all()
                    for superseded_row in superseded_rows:
                        superseded_row.lifecycle_state = "superseded"
                        superseded_row.updated_at = now
                    evidence = ProviderEvidenceRecord(
                        id=new_id_fn("pev"),
                        provider_object_id=provider_object.id,
                        provider="google",
                        provider_account_id=provider_account_id,
                        source_kind="gmail_message",
                        external_id=message_id,
                        thread_external_id=thread_id,
                        calendar_id=None,
                        source_uri=provider_object.provider_url,
                        source_timestamp=message_received_at,
                        content_digest=evidence_content_digest,
                        metadata_json=evidence_metadata,
                        taint="provider_untrusted",
                        sensitivity="private",
                        retention_policy="provider_source",
                        extraction_status="pending",
                        lifecycle_state="available",
                        observed_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(evidence)
                    db.flush()
                    existing_block_id = db.scalar(
                        select(ProviderEvidenceBlockRecord.id)
                        .where(ProviderEvidenceBlockRecord.evidence_id == evidence.id)
                        .limit(1)
                    )
                    raw_blocks = read_evidence.get("blocks")
                    if existing_block_id is None:
                        for index, block in enumerate(
                            raw_blocks if isinstance(raw_blocks, list) else []
                        ):
                            if not isinstance(block, dict) or not isinstance(
                                block.get("text"), str
                            ):
                                continue
                            kind = block.get("kind")
                            db.add(
                                ProviderEvidenceBlockRecord(
                                    id=new_id_fn("peb"),
                                    evidence_id=evidence.id,
                                    block_index=index,
                                    block_kind=kind
                                    if kind in {"body", "quote", "signature", "forwarded"}
                                    else "body",
                                    text=block["text"],
                                    digest=str(
                                        block.get("digest") or _json_digest({"text": block["text"]})
                                    ),
                                    source_offsets={
                                        "block_id": block.get("block_id"),
                                        "source_message_id": block.get("source_message_id"),
                                    },
                                    metadata_json={
                                        "source_mime_type": block.get("source_mime_type"),
                                        "charset": block.get("charset"),
                                        "truncated": block.get("truncated"),
                                    },
                                    created_at=now,
                                )
                            )
                else:
                    labels_changed = existing_evidence.metadata_json.get("label_ids") != label_ids
                    existing_evidence.thread_external_id = thread_id
                    existing_evidence.source_uri = provider_object.provider_url
                    existing_evidence.source_timestamp = message_received_at
                    existing_evidence.metadata_json = evidence_metadata
                    existing_evidence.observed_at = now
                    existing_evidence.updated_at = now
                    if labels_changed or key in {"labelsAdded", "labelsRemoved"}:
                        existing_evidence.extraction_status = "pending"
    return item_count, observation_count


def _gmail_message_received_at(message: dict[str, Any], *, fallback: datetime) -> datetime:
    internal_date_raw = message.get("internalDate")
    if isinstance(internal_date_raw, str) and internal_date_raw.strip():
        try:
            millis = int(internal_date_raw.strip())
        except ValueError:
            return fallback
        if millis >= 0:
            return datetime.fromtimestamp(millis / 1000, tz=UTC)
    return fallback


def _sync_drive_change(
    db: Session,
    change: dict[str, Any],
    *,
    resource_id: str,
    provider_event_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> bool:
    del db, change, resource_id, provider_event_id, now, new_id_fn
    return False


def _is_stale_cursor_error(*, resource_type: str, error: str) -> bool:
    if resource_type == "gmail" and error == "resource_not_found":
        return True
    if resource_type == "calendar" and error == "sync_token_invalid":
        return True
    return False


def _handle_stale_cursor(
    *,
    session_factory: sessionmaker[Session],
    provider_event_id: str | None,
    sync_run_id: str,
    resource_type: str,
    resource_id: str,
    error: str,
    now: datetime,
) -> None:
    with session_factory() as db:
        with db.begin():
            run = db.get(SyncRunRecord, sync_run_id)
            if run is not None:
                run.status = "failed"
                run.error = error
                run.completed_at = now
            cursor = db.scalar(
                select(SyncCursorRecord)
                .where(
                    SyncCursorRecord.provider == "google",
                    SyncCursorRecord.resource_type == resource_type,
                    SyncCursorRecord.resource_id == resource_id,
                )
                .with_for_update()
                .limit(1)
            )
            if cursor is not None:
                cursor.cursor_value = None
                cursor.status = "ready"
                cursor.last_error_code = error[:64]
                cursor.last_error_at = now
                cursor.updated_at = now
            if provider_event_id is not None:
                event = db.get(ProviderEventRecord, provider_event_id)
                if event is not None:
                    event.status = "failed"
                    event.error = error
            enqueue_background_task(
                db,
                task_type="provider_sync_due",
                payload={
                    "provider": "google",
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
                now=now,
            )


def _mark_sync_failed(
    *,
    session_factory: sessionmaker[Session],
    provider_event_id: str | None,
    sync_run_id: str,
    resource_type: str,
    resource_id: str,
    error: str,
    now: datetime,
) -> None:
    with session_factory() as db:
        with db.begin():
            run = db.get(SyncRunRecord, sync_run_id)
            if run is not None:
                run.status = "failed"
                run.error = error
                run.completed_at = now
            cursor_before = run.cursor_before if run is not None else None
            cursor = db.scalar(
                select(SyncCursorRecord)
                .where(
                    SyncCursorRecord.provider == "google",
                    SyncCursorRecord.resource_type == resource_type,
                    SyncCursorRecord.resource_id == resource_id,
                )
                .with_for_update()
                .limit(1)
            )
            if cursor is not None:
                if run is None or cursor.cursor_value == cursor_before:
                    # A Gmail 404 (resource_not_found) is recoverable: it is
                    # handled upstream by clearing the cursor for a full
                    # resync, so only an unbootstrappable mailbox is invalid.
                    cursor.status = (
                        "invalid"
                        if resource_type == "gmail" and error == "gmail_sync_cursor_missing"
                        else "error"
                    )
                    cursor.last_error_code = error[:64]
                    cursor.last_error_at = now
                    cursor.updated_at = now
            if provider_event_id is not None:
                event = db.get(ProviderEventRecord, provider_event_id)
                if event is not None:
                    event.status = "failed"
                    event.error = error


def _provider_sync_wake_note(resource_type: str, item_count: int) -> str:
    label = {"gmail": "Gmail", "calendar": "Calendar", "drive": "Drive"}.get(
        resource_type, resource_type
    )
    noun = "item" if item_count == 1 else "items"
    return (
        f"A Google {label} sync found {item_count} new or changed {noun}. "
        "Review the new activity and decide whether anything matters."
    )


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
