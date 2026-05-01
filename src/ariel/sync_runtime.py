from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.config import AppSettings
from ariel.google_connector import (
    DefaultGoogleOAuthClient,
    DefaultGoogleWorkspaceProvider,
    GoogleConnectorRuntime,
)
from ariel.persistence import (
    BackgroundTaskRecord,
    ProviderEventRecord,
    SyncCursorRecord,
    SyncRunRecord,
    WorkspaceItemEventRecord,
    WorkspaceItemRecord,
    to_rfc3339,
)
from ariel.proactivity import upsert_attention_signal


def process_provider_event_received(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
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
            db.add(
                BackgroundTaskRecord(
                    id=new_id_fn("tsk"),
                    task_type="provider_sync_due",
                    payload={
                        "provider_event_id": event.id,
                        "provider": event.provider,
                        "resource_type": event.resource_type,
                        "resource_id": event.resource_id,
                    },
                    status="pending",
                    attempts=0,
                    max_attempts=5,
                    error=None,
                    claimed_by=None,
                    run_after=now,
                    last_heartbeat=None,
                    created_at=now,
                    updated_at=now,
                )
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
                    signal_count=0,
                    error=None,
                    started_at=now,
                    completed_at=None,
                    created_at=now,
                )
            )
            cursor_before = cursor.cursor_value

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
    )

    try:
        with session_factory() as db:
            with db.begin():
                access_token = runtime.access_token_for_background_sync(
                    db=db,
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                )
        if resource_type == "calendar":
            output = runtime.workspace_provider.calendar_list_event_deltas(
                access_token=access_token,
                sync_token=cursor_before,
                time_min=None if cursor_before else to_rfc3339(now - timedelta(days=30)),
            )
        elif resource_type == "gmail":
            if cursor_before is None:
                raise RuntimeError("gmail_sync_cursor_missing")
            output = runtime.workspace_provider.email_list_history(
                access_token=access_token,
                start_history_id=cursor_before,
            )
        else:
            if cursor_before is None:
                output = runtime.workspace_provider.drive_get_start_page_token(
                    access_token=access_token
                )
            else:
                output = runtime.workspace_provider.drive_list_changes(
                    access_token=access_token,
                    page_token=cursor_before,
                )
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
        raise

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

            now = now_fn()
            item_count = 0
            signal_count = 0
            cursor_after = cursor_before
            if resource_type == "calendar":
                cursor_after = _payload_text(output, "nextSyncToken") or cursor_before
                raw_items = output.get("items")
                items = raw_items if isinstance(raw_items, list) else []
                for item in items:
                    if isinstance(item, dict) and _sync_calendar_item(
                        db,
                        cast(dict[str, Any], item),
                        resource_id=resource_id,
                        provider_event_id=provider_event_id,
                        now=now,
                        new_id_fn=new_id_fn,
                    ):
                        item_count += 1
                        signal_count += 1
            elif resource_type == "gmail":
                cursor_after = _payload_text(output, "historyId") or cursor_before
                raw_histories = output.get("history")
                histories = raw_histories if isinstance(raw_histories, list) else []
                for history in histories:
                    if isinstance(history, dict):
                        added, signaled = _sync_gmail_history(
                            db,
                            cast(dict[str, Any], history),
                            resource_id=resource_id,
                            provider_event_id=provider_event_id,
                            now=now,
                            new_id_fn=new_id_fn,
                        )
                        item_count += added
                        signal_count += signaled
            else:
                cursor_after = (
                    _payload_text(output, "newStartPageToken")
                    or _payload_text(output, "nextPageToken")
                    or _payload_text(output, "startPageToken")
                    or cursor_before
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
                        signal_count += 1

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
            run.signal_count = signal_count
            run.completed_at = now
            if event is not None:
                event.status = "processed"
                event.processed_at = now
            if signal_count:
                db.add(
                    BackgroundTaskRecord(
                        id=new_id_fn("tsk"),
                        task_type="attention_feature_extraction_due",
                        payload={},
                        status="pending",
                        attempts=0,
                        max_attempts=3,
                        error=None,
                        claimed_by=None,
                        run_after=now,
                        last_heartbeat=None,
                        created_at=now,
                        updated_at=now,
                    )
                )


def _sync_calendar_item(
    db: Session,
    item: dict[str, Any],
    *,
    resource_id: str,
    provider_event_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> bool:
    external_id = _payload_text(item, "id")
    if external_id is None:
        return False
    status = "deleted" if item.get("status") == "cancelled" else "active"
    title = _payload_text(item, "summary") or "Calendar event"
    updated = _payload_text(item, "updated") or to_rfc3339(now)
    return _upsert_workspace_item_event_and_signal(
        db,
        provider="google",
        item_type="calendar_event",
        external_id=external_id,
        title=title,
        summary=title,
        source_uri=_payload_text(item, "htmlLink"),
        status=status,
        metadata={"resource_id": resource_id, "updated": updated},
        event_type="deleted" if status == "deleted" else "updated",
        event_dedupe_key=f"google:calendar:{resource_id}:{external_id}:{status}:{updated}",
        provider_event_id=provider_event_id,
        signal_title=f"Calendar changed: {title}",
        signal_body=title,
        signal_reason="Google Calendar delta changed this event.",
        now=now,
        new_id_fn=new_id_fn,
    )


def _sync_gmail_history(
    db: Session,
    history: dict[str, Any],
    *,
    resource_id: str,
    provider_event_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> tuple[int, int]:
    history_id = _payload_text(history, "id") or to_rfc3339(now)
    item_count = 0
    signal_count = 0
    for key, status in (
        ("messagesAdded", "active"),
        ("labelsAdded", "active"),
        ("labelsRemoved", "active"),
        ("messagesDeleted", "deleted"),
    ):
        raw_entries = history.get(key)
        entries = raw_entries if isinstance(raw_entries, list) else []
        for entry in entries:
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict):
                continue
            message_id = _payload_text(message, "id")
            if message_id is None:
                continue
            changed = _upsert_workspace_item_event_and_signal(
                db,
                provider="google",
                item_type="email_message",
                external_id=message_id,
                title=f"Email message {message_id}",
                summary=f"Gmail history {key} for message {message_id}.",
                source_uri=f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
                status=status,
                metadata={"resource_id": resource_id, "history_id": history_id, "change": key},
                event_type="deleted" if status == "deleted" else "updated",
                event_dedupe_key=f"google:gmail:{resource_id}:{message_id}:{history_id}:{key}",
                provider_event_id=provider_event_id,
                signal_title=f"Gmail changed: message {message_id}",
                signal_body=f"Gmail reported {key} for message {message_id}.",
                signal_reason="Gmail history changed this message.",
                now=now,
                new_id_fn=new_id_fn,
            )
            item_count += 1 if changed else 0
            signal_count += 1 if changed else 0
    return item_count, signal_count


def _sync_drive_change(
    db: Session,
    change: dict[str, Any],
    *,
    resource_id: str,
    provider_event_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> bool:
    file_id = _payload_text(change, "fileId")
    raw_file_payload = change.get("file")
    file_payload = (
        cast(dict[str, Any], raw_file_payload) if isinstance(raw_file_payload, dict) else {}
    )
    if file_id is None:
        file_id = _payload_text(file_payload, "id")
    if file_id is None:
        return False
    status = "deleted" if change.get("removed") is True else "active"
    title = _payload_text(file_payload, "name") or f"Drive file {file_id}"
    changed_at = _payload_text(change, "time") or _payload_text(file_payload, "modifiedTime")
    changed_at = changed_at or to_rfc3339(now)
    return _upsert_workspace_item_event_and_signal(
        db,
        provider="google",
        item_type="drive_file",
        external_id=file_id,
        title=title,
        summary=title,
        source_uri=_payload_text(file_payload, "webViewLink"),
        status=status,
        metadata={"resource_id": resource_id, "changed_at": changed_at},
        event_type="deleted" if status == "deleted" else "updated",
        event_dedupe_key=f"google:drive:{resource_id}:{file_id}:{status}:{changed_at}",
        provider_event_id=provider_event_id,
        signal_title=f"Drive changed: {title}",
        signal_body=title,
        signal_reason="Google Drive delta changed this file.",
        now=now,
        new_id_fn=new_id_fn,
    )


def _upsert_workspace_item_event_and_signal(
    db: Session,
    *,
    provider: str,
    item_type: str,
    external_id: str,
    title: str,
    summary: str,
    source_uri: str | None,
    status: str,
    metadata: dict[str, Any],
    event_type: str,
    event_dedupe_key: str,
    provider_event_id: str | None,
    signal_title: str,
    signal_body: str,
    signal_reason: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> bool:
    existing_event = db.scalar(
        select(WorkspaceItemEventRecord)
        .where(WorkspaceItemEventRecord.dedupe_key == event_dedupe_key)
        .limit(1)
    )
    if existing_event is not None:
        return False

    item = db.scalar(
        select(WorkspaceItemRecord)
        .where(
            WorkspaceItemRecord.provider == provider,
            WorkspaceItemRecord.item_type == item_type,
            WorkspaceItemRecord.external_id == external_id,
        )
        .with_for_update()
        .limit(1)
    )
    if item is None:
        item = WorkspaceItemRecord(
            id=new_id_fn("wki"),
            provider=provider,
            item_type=item_type,
            external_id=external_id,
            title=title,
            summary=summary,
            source_uri=source_uri,
            status=status,
            item_metadata=metadata,
            observed_at=now,
            deleted_at=now if status == "deleted" else None,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.flush()
        item_event_type = "deleted" if status == "deleted" else "created"
    else:
        item.title = title
        item.summary = summary
        item.source_uri = source_uri
        item.status = status
        item.item_metadata = metadata
        item.observed_at = now
        item.deleted_at = now if status == "deleted" else None
        item.updated_at = now
        item_event_type = event_type

    event = WorkspaceItemEventRecord(
        id=new_id_fn("wie"),
        workspace_item_id=item.id,
        dedupe_key=event_dedupe_key,
        provider_event_id=provider_event_id,
        event_type=item_event_type,
        payload={"title": title, "status": status, "metadata": metadata},
        created_at=now,
    )
    db.add(event)
    db.flush()
    upsert_attention_signal(
        db,
        dedupe_key=f"workspace-event:{event.dedupe_key}",
        source_type="workspace_item",
        source_id=item.id,
        workspace_item_id=item.id,
        priority="normal",
        urgency="normal",
        confidence=1.0,
        title=signal_title,
        body=signal_body,
        reason=signal_reason,
        evidence={
            "workspace_item_id": item.id,
            "workspace_item_event_id": event.id,
            "provider_event_id": provider_event_id,
        },
        taint={"provenance_status": "tainted", "source": "google_workspace"},
        now=now,
        new_id_fn=new_id_fn,
    )
    return True


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
                cursor.status = "error"
                cursor.last_error_code = error[:64]
                cursor.last_error_at = now
                cursor.updated_at = now
            if provider_event_id is not None:
                event = db.get(ProviderEventRecord, provider_event_id)
                if event is not None:
                    event.status = "failed"
                    event.error = error


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
