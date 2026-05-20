from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import empty_recall_response, is_retriever_call
from ariel.config import AppSettings
from ariel.google_connector import (
    GOOGLE_CONNECTOR_ID,
    GoogleConnectorRuntime,
    _encrypt_secret,
)
from ariel.persistence import (
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    ProviderWatchChannelRecord,
    SyncCursorRecord,
    SyncRunRecord,
)
from ariel.sync_runtime import process_provider_sync_due
from ariel.worker import (
    process_provider_reconcile_sync_due,
    process_provider_watch_renew_due,
    seed_provider_maintenance_tasks,
)
from tests.fake_sandbox import FakeSandboxRuntime


GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
PUBSUB_TOPIC = "projects/ariel/topics/gmail-watch"
PUBLIC_WEBHOOK_BASE_URL = "https://ariel.example"
EXPECTED_CALENDAR_WATCH_ADDRESS = f"{PUBLIC_WEBHOOK_BASE_URL}/v1/providers/google/events?resource_type=calendar&resource_id=primary"


@dataclass
class IdFactory:
    counters: dict[str, int] = field(default_factory=dict)

    def __call__(self, prefix: str) -> str:
        next_value = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = next_value
        return f"{prefix}_{next_value:028d}"


def _settings() -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


# --------------------------------------------------------------------------
# PART 2: watch registration fires on connect and persists a row
# --------------------------------------------------------------------------


@dataclass
class WatchRecordingProvider:
    gmail_watch_calls: list[dict[str, Any]] = field(default_factory=list)
    calendar_watch_calls: list[dict[str, Any]] = field(default_factory=list)
    gmail_expiration: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    calendar_expiration: datetime = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)

    def gmail_register_watch(
        self,
        *,
        access_token: str,
        topic_name: str,
        label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self.gmail_watch_calls.append(
            {"access_token": access_token, "topic_name": topic_name, "label_ids": label_ids}
        )
        return {"historyId": "hist-watch-1", "expiration": self.gmail_expiration}

    def calendar_register_watch(
        self,
        *,
        access_token: str,
        calendar_id: str,
        channel_id: str,
        channel_token: str,
        address: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        self.calendar_watch_calls.append(
            {
                "access_token": access_token,
                "calendar_id": calendar_id,
                "channel_id": channel_id,
                "channel_token": channel_token,
                "address": address,
                "ttl_seconds": ttl_seconds,
            }
        )
        return {"resourceId": "res-watch-1", "expiration": self.calendar_expiration}


@dataclass
class ConnectOAuthClient:
    granted_scopes: list[str]

    def build_authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        scopes: list[str],
        redirect_uri: str,
        prompt_consent: bool,
    ) -> str:
        del code_challenge, scopes, redirect_uri, prompt_consent
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code_for_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        state: str,
    ) -> dict[str, Any]:
        del code, code_verifier, redirect_uri, state
        return {
            "account_subject": "sub_watch",
            "account_email": "watch@example.com",
            "granted_scopes": list(self.granted_scopes),
            "access_token": "tok_access_watch",
            "refresh_token": "tok_refresh_watch",
            "expires_in_seconds": 3600,
        }

    def refresh_access_token(self, *, refresh_token: str) -> dict[str, Any]:
        return {
            "access_token": f"refreshed::{refresh_token}",
            "refresh_token": refresh_token,
            "expires_in_seconds": 3600,
        }

    def revoke_token(self, *, token: str) -> None:
        del token


@dataclass
class _NoCallAdapter:
    provider: str = "provider.test"
    model: str = "model.test"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        **_: Any,
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(provider=self.provider, model=self.model)
        raise AssertionError("model should not be called in this test")


def test_connect_registers_gmail_and_calendar_watches_and_persists_rows(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", PUBSUB_TOPIC)
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", PUBLIC_WEBHOOK_BASE_URL)
    provider = WatchRecordingProvider()
    oauth_client = ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE])
    app = create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _NoCallAdapter()),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        app_state = cast(Any, client.app).state
        app_state.google_oauth_client = oauth_client
        app_state.google_workspace_provider = provider

        started = client.post("/v1/connectors/google/start")
        assert started.status_code == 200
        state = started.json()["oauth"]["state"]
        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-watch"},
        )
        assert callback.status_code == 200
        assert callback.json()["connector"]["status"] == "connected"

        with cast(Any, client.app).state.session_factory() as db:
            channels = db.scalars(
                select(ProviderWatchChannelRecord).order_by(
                    ProviderWatchChannelRecord.resource_type.asc()
                )
            ).all()

    assert len(provider.gmail_watch_calls) == 1
    assert provider.gmail_watch_calls[0]["topic_name"] == PUBSUB_TOPIC
    assert len(provider.calendar_watch_calls) == 1
    assert provider.calendar_watch_calls[0]["address"] == EXPECTED_CALENDAR_WATCH_ADDRESS
    assert provider.calendar_watch_calls[0]["calendar_id"] == "primary"

    by_type = {channel.resource_type: channel for channel in channels}
    assert set(by_type) == {"calendar", "gmail"}
    gmail_channel = by_type["gmail"]
    assert gmail_channel.status == "active"
    assert gmail_channel.resource_id == "sub_watch"
    assert gmail_channel.cursor_seed == "hist-watch-1"
    assert gmail_channel.expires_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    calendar_channel = by_type["calendar"]
    assert calendar_channel.status == "active"
    assert calendar_channel.resource_id == "primary"
    assert calendar_channel.provider_resource_id == "res-watch-1"
    assert calendar_channel.channel_id is not None
    assert calendar_channel.channel_token is not None


def test_connect_watch_registration_failure_is_non_fatal(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", PUBSUB_TOPIC)

    @dataclass
    class FailingWatchProvider:
        def gmail_register_watch(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("google_upstream_timeout")

    oauth_client = ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE])
    app = create_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _NoCallAdapter()),
        reset_database=True,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        app_state = cast(Any, client.app).state
        app_state.google_oauth_client = oauth_client
        app_state.google_workspace_provider = FailingWatchProvider()

        started = client.post("/v1/connectors/google/start")
        state = started.json()["oauth"]["state"]
        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-fail"},
        )
        # The connector still connects — the reconcile poll is the backstop.
        assert callback.status_code == 200
        assert callback.json()["connector"]["status"] == "connected"

        with cast(Any, client.app).state.session_factory() as db:
            channels = db.scalars(select(ProviderWatchChannelRecord)).all()
    assert channels == []


# --------------------------------------------------------------------------
# PART 3: the renewal handler re-arms a near-expiry channel
# --------------------------------------------------------------------------


def _seed_connected_connector(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    settings: AppSettings,
    granted_scopes: list[str],
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                GoogleConnectorRecord(
                    id=GOOGLE_CONNECTOR_ID,
                    provider="google",
                    status="connected",
                    account_subject="sub_connected",
                    account_email="connected@example.com",
                    granted_scopes=granted_scopes,
                    access_token_enc=_encrypt_secret(
                        plaintext="tok_access_live",
                        secret=settings.connector_encryption_secret,
                        key_version=settings.connector_encryption_key_version,
                        encryption_keys=settings.connector_encryption_keys,
                    ),
                    refresh_token_enc=_encrypt_secret(
                        plaintext="tok_refresh_live",
                        secret=settings.connector_encryption_secret,
                        key_version=settings.connector_encryption_key_version,
                        encryption_keys=settings.connector_encryption_keys,
                    ),
                    access_token_expires_at=now + timedelta(hours=1),
                    token_obtained_at=now,
                    encryption_key_version=settings.connector_encryption_key_version,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def test_watch_renew_handler_rearms_near_expiry_channel(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    _seed_connected_connector(
        session_factory, now=now, settings=settings, granted_scopes=[GMAIL_READ_SCOPE]
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                ProviderWatchChannelRecord(
                    id="wch_existing",
                    provider="google",
                    resource_type="gmail",
                    resource_id="sub_connected",
                    channel_id=None,
                    channel_token=None,
                    provider_resource_id=None,
                    cursor_seed="hist-old",
                    status="active",
                    # Within the 6-day renewal window.
                    expires_at=now + timedelta(hours=3),
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    provider = WatchRecordingProvider(gmail_expiration=datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
    runtime = GoogleConnectorRuntime(
        oauth_client=ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE]),
        workspace_provider=cast(Any, provider),
        redirect_uri=settings.google_oauth_redirect_uri,
        oauth_state_ttl_seconds=settings.google_oauth_state_ttl_seconds,
        encryption_secret=settings.connector_encryption_secret,
        encryption_key_version=settings.connector_encryption_key_version,
        encryption_keys=settings.connector_encryption_keys,
        pubsub_topic=PUBSUB_TOPIC,
        public_webhook_base_url=None,
    )
    monkeypatch.setattr("ariel.worker.build_google_runtime", lambda _settings: runtime)

    process_provider_watch_renew_due(
        session_factory=session_factory,
        settings=settings,
        now_fn=lambda: now,
        new_id_fn=IdFactory(),
    )

    assert len(provider.gmail_watch_calls) == 1
    with session_factory() as db:
        with db.begin():
            channel = db.get(ProviderWatchChannelRecord, "wch_existing")
            assert channel is not None
            assert channel.status == "active"
            assert channel.cursor_seed == "hist-watch-1"
            assert channel.expires_at == datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def test_watch_renew_handler_skips_when_no_channel_near_expiry(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    _seed_connected_connector(
        session_factory, now=now, settings=settings, granted_scopes=[GMAIL_READ_SCOPE]
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                ProviderWatchChannelRecord(
                    id="wch_fresh",
                    provider="google",
                    resource_type="gmail",
                    resource_id="sub_connected",
                    channel_id=None,
                    channel_token=None,
                    provider_resource_id=None,
                    cursor_seed="hist-fresh",
                    status="active",
                    # Far beyond the 6-day renewal window.
                    expires_at=now + timedelta(days=10),
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    provider = WatchRecordingProvider()
    runtime = GoogleConnectorRuntime(
        oauth_client=ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE]),
        workspace_provider=cast(Any, provider),
        redirect_uri=settings.google_oauth_redirect_uri,
        oauth_state_ttl_seconds=settings.google_oauth_state_ttl_seconds,
        encryption_secret=settings.connector_encryption_secret,
        encryption_key_version=settings.connector_encryption_key_version,
        encryption_keys=settings.connector_encryption_keys,
        pubsub_topic=PUBSUB_TOPIC,
        public_webhook_base_url=None,
    )
    monkeypatch.setattr("ariel.worker.build_google_runtime", lambda _settings: runtime)

    process_provider_watch_renew_due(
        session_factory=session_factory,
        settings=settings,
        now_fn=lambda: now,
        new_id_fn=IdFactory(),
    )

    assert provider.gmail_watch_calls == []


# --------------------------------------------------------------------------
# PART 3: the reconcile handler enqueues provider_sync_due per cursor
# --------------------------------------------------------------------------


def test_reconcile_handler_enqueues_provider_sync_due_for_each_cursor(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    _seed_connected_connector(
        session_factory,
        now=now,
        settings=settings,
        granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE],
    )
    with session_factory() as db:
        with db.begin():
            for resource_type in ("gmail", "calendar"):
                db.add(
                    SyncCursorRecord(
                        id=f"cur_{resource_type}",
                        provider="google",
                        resource_type=resource_type,
                        resource_id="primary",
                        cursor_value="cursor-1",
                        cursor_version=1,
                        status="ready",
                        last_successful_sync_at=None,
                        last_error_code=None,
                        last_error_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

    process_provider_reconcile_sync_due(
        session_factory=session_factory,
        now_fn=lambda: now,
        new_id_fn=IdFactory(),
    )

    with session_factory() as db:
        with db.begin():
            tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "provider_sync_due"
                )
            ).all()
    resource_types = sorted(task.payload["resource_type"] for task in tasks)
    assert resource_types == ["calendar", "gmail"]
    assert all(task.payload["provider"] == "google" for task in tasks)


def test_seed_provider_maintenance_tasks_creates_recurring_rows_once(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    with session_factory() as db:
        with db.begin():
            seed_provider_maintenance_tasks(db, settings=settings, now=now)
    # A second pass must not create duplicates.
    with session_factory() as db:
        with db.begin():
            seed_provider_maintenance_tasks(db, settings=settings, now=now)

    with session_factory() as db:
        with db.begin():
            tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type.in_(
                        ("provider_watch_renew_due", "provider_reconcile_sync_due")
                    )
                )
            ).all()
    by_type = {task.task_type: task for task in tasks}
    assert set(by_type) == {"provider_watch_renew_due", "provider_reconcile_sync_due"}
    assert by_type["provider_watch_renew_due"].recurrence_seconds == 6 * 3600
    assert (
        by_type["provider_reconcile_sync_due"].recurrence_seconds
        == settings.provider_reconcile_sync_interval_seconds
    )


# --------------------------------------------------------------------------
# PART 4: a stale Calendar cursor clears state and re-enqueues a full sync
# --------------------------------------------------------------------------


def test_calendar_410_clears_cursor_and_reenqueues_full_sync(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaleCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("sync_token_invalid")

    class FakeRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = StaleCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeRuntime)
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with session_factory() as db:
        with db.begin():
            db.add(
                SyncCursorRecord(
                    id=new_id("cur"),
                    provider="google",
                    resource_type="calendar",
                    resource_id="primary",
                    cursor_value="stale-sync-token",
                    cursor_version=4,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={
            "provider": "google",
            "resource_type": "calendar",
            "resource_id": "primary",
        },
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            run = db.scalar(select(SyncRunRecord).limit(1))
            resync_tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "provider_sync_due"
                )
            ).all()
    assert cursor is not None
    assert cursor.cursor_value is None
    assert cursor.status == "ready"
    assert cursor.last_error_code == "sync_token_invalid"
    assert run is not None
    assert run.status == "failed"
    assert run.error == "sync_token_invalid"
    assert len(resync_tasks) == 1
    assert resync_tasks[0].payload == {
        "provider": "google",
        "resource_type": "calendar",
        "resource_id": "primary",
    }


def test_gmail_404_clears_cursor_and_reenqueues_full_sync(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaleGmailProvider:
        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("existing Gmail cursor should use history pages")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("resource_not_found")

    class FakeRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = StaleGmailProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeRuntime)
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with session_factory() as db:
        with db.begin():
            db.add(
                SyncCursorRecord(
                    id=new_id("cur"),
                    provider="google",
                    resource_type="gmail",
                    resource_id="primary",
                    cursor_value="stale-history-id",
                    cursor_version=2,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={
            "provider": "google",
            "resource_type": "gmail",
            "resource_id": "primary",
        },
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            resync_tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type == "provider_sync_due"
                )
            ).all()
    assert cursor is not None
    # Recoverable: the cursor is cleared and ready, not permanently invalid.
    assert cursor.cursor_value is None
    assert cursor.status == "ready"
    assert cursor.last_error_code == "resource_not_found"
    assert len(resync_tasks) == 1


# --------------------------------------------------------------------------
# PART 5: a sync that finds new items enqueues an agent_wake
# --------------------------------------------------------------------------


def test_sync_with_new_items_enqueues_agent_wake(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NewEventsCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            return {
                "nextSyncToken": "sync-token-2",
                "items": [
                    {
                        "id": "evt-new-1",
                        "status": "confirmed",
                        "summary": "Quarterly review",
                        "updated": "2026-05-18T09:00:00Z",
                        "start": {"dateTime": "2026-05-20T10:00:00Z"},
                        "end": {"dateTime": "2026-05-20T11:00:00Z"},
                    }
                ],
            }

    class FakeRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = NewEventsCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeRuntime)
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with session_factory() as db:
        with db.begin():
            db.add(
                SyncCursorRecord(
                    id=new_id("cur"),
                    provider="google",
                    resource_type="calendar",
                    resource_id="primary",
                    cursor_value="sync-token-1",
                    cursor_version=1,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={
            "provider": "google",
            "resource_type": "calendar",
            "resource_id": "primary",
        },
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            run = db.scalar(select(SyncRunRecord).limit(1))
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
    assert run is not None
    assert run.item_count == 1
    assert len(wake_tasks) == 1
    note = wake_tasks[0].payload["note"]
    assert isinstance(note, str)
    assert "Calendar" in note
    assert "1 new or changed item" in note


def test_sync_with_no_new_items_does_not_enqueue_agent_wake(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            return {"nextSyncToken": "sync-token-2", "items": []}

    class FakeRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = EmptyCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeRuntime)
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with session_factory() as db:
        with db.begin():
            db.add(
                SyncCursorRecord(
                    id=new_id("cur"),
                    provider="google",
                    resource_type="calendar",
                    resource_id="primary",
                    cursor_value="sync-token-1",
                    cursor_version=1,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={
            "provider": "google",
            "resource_type": "calendar",
            "resource_id": "primary",
        },
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
    assert wake_tasks == []
