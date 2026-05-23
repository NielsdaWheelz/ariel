from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.model_adapter import ModelAdapter
from tests.integration.app_helpers import create_test_app
from tests.integration.responses_helpers import empty_recall_response, is_memory_subsystem_call
from ariel.config import AppSettings
from ariel.google_connector import (
    GOOGLE_CONNECTOR_ID,
    GoogleConnectorRuntime,
    GoogleProviderRequestFailure,
    GoogleWatchRegistrationFailure,
)
from ariel.persistence import (
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    GoogleConnectorEventRecord,
    ProviderEventRecord,
    ProviderWatchChannelRecord,
    SyncCursorRecord,
    SyncRunRecord,
    enqueue_background_task,
)
from ariel.secret_cipher import encrypt_secret
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
# Watch registration fires on connect and persists a row.
# --------------------------------------------------------------------------


@dataclass
class WatchRecordingProvider:
    gmail_watch_calls: list[dict[str, Any]] = field(default_factory=list)
    calendar_watch_calls: list[dict[str, Any]] = field(default_factory=list)
    gmail_stop_calls: list[str] = field(default_factory=list)
    calendar_stop_calls: list[dict[str, str]] = field(default_factory=list)
    gmail_expiration: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    calendar_expiration: datetime = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)

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

    def gmail_stop_watch(self, *, access_token: str) -> None:
        self.gmail_stop_calls.append(access_token)

    def calendar_stop_watch(
        self,
        *,
        access_token: str,
        channel_id: str,
        provider_resource_id: str,
    ) -> None:
        self.calendar_stop_calls.append(
            {
                "access_token": access_token,
                "channel_id": channel_id,
                "provider_resource_id": provider_resource_id,
            }
        )


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
        if is_memory_subsystem_call(input_items):
            return empty_recall_response(
                provider=self.provider, model=self.model, input_items=input_items
            )
        raise AssertionError("model should not be called in this test")


def test_connect_registers_watches_and_calendar_push_accepts_persisted_token(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", PUBSUB_TOPIC)
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", PUBLIC_WEBHOOK_BASE_URL)
    provider = WatchRecordingProvider()
    oauth_client = ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE])
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _NoCallAdapter()),
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
        calendar_watch_call = provider.calendar_watch_calls[0]
        assert calendar_watch_call["address"] == EXPECTED_CALENDAR_WATCH_ADDRESS
        assert calendar_watch_call["calendar_id"] == "primary"

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
        calendar_channel_id = calendar_channel.channel_id
        calendar_channel_token = calendar_channel.channel_token
        assert calendar_channel_id is not None
        assert calendar_channel_token is not None
        assert calendar_watch_call["channel_id"] == calendar_channel_id
        assert calendar_watch_call["channel_token"] == calendar_channel_token
        assert calendar_channel_token != "provider-token"

        provider_event = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-ID": calendar_channel_id,
                "X-Goog-Channel-Token": calendar_channel_token,
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-ID": "res-watch-1",
                "X-Goog-Resource-State": "exists",
            },
            json={},
        )
        assert provider_event.status_code == 202
        assert provider_event.json()["duplicate"] is False


def test_disconnect_clears_google_provider_ingestion_state(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", PUBSUB_TOPIC)
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", PUBLIC_WEBHOOK_BASE_URL)
    provider = WatchRecordingProvider()
    oauth_client = ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE])
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _NoCallAdapter()),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        app_state = cast(Any, client.app).state
        app_state.google_oauth_client = oauth_client
        app_state.google_workspace_provider = provider

        started = client.post("/v1/connectors/google/start")
        state = started.json()["oauth"]["state"]
        callback = client.get(
            "/v1/connectors/google/callback",
            params={"state": state, "code": "connect-watch"},
        )
        assert callback.status_code == 200

        with app_state.session_factory() as db:
            calendar_channel = db.scalar(
                select(ProviderWatchChannelRecord).where(
                    ProviderWatchChannelRecord.resource_type == "calendar"
                )
            )
            assert calendar_channel is not None
            calendar_channel_id = calendar_channel.channel_id
            calendar_channel_token = calendar_channel.channel_token
            assert calendar_channel_id is not None
            assert calendar_channel_token is not None

        provider_event = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-ID": calendar_channel_id,
                "X-Goog-Channel-Token": calendar_channel_token,
                "X-Goog-Message-Number": "1",
                "X-Goog-Resource-ID": "res-watch-1",
                "X-Goog-Resource-State": "exists",
            },
            json={},
        )
        assert provider_event.status_code == 202

        now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        with app_state.session_factory() as db:
            with db.begin():
                db.add(
                    SyncCursorRecord(
                        id="cur_disconnect",
                        provider="google",
                        resource_type="calendar",
                        resource_id="primary",
                        cursor_value="sync-token",
                        cursor_version=1,
                        status="ready",
                        last_successful_sync_at=now,
                        last_error_code=None,
                        last_error_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                enqueue_background_task(
                    db,
                    task_type="provider_sync_due",
                    payload={
                        "provider": "google",
                        "resource_type": "calendar",
                        "resource_id": "primary",
                    },
                    now=now,
                )

        disconnected = client.delete("/v1/connectors/google")
        assert disconnected.status_code == 200

        stale_event = client.post(
            "/v1/providers/google/events?resource_type=calendar&resource_id=primary",
            headers={
                "X-Goog-Channel-ID": calendar_channel_id,
                "X-Goog-Channel-Token": calendar_channel_token,
                "X-Goog-Message-Number": "2",
                "X-Goog-Resource-ID": "res-watch-1",
                "X-Goog-Resource-State": "exists",
            },
            json={},
        )
        assert stale_event.status_code == 401

        with app_state.session_factory() as db:
            channels = db.scalars(select(ProviderWatchChannelRecord)).all()
            cursors = db.scalars(select(SyncCursorRecord)).all()
            events = db.scalars(select(ProviderEventRecord)).all()
            one_shot_provider_tasks = db.scalars(
                select(BackgroundTaskRecord).where(
                    BackgroundTaskRecord.task_type.in_(
                        ("provider_event_received", "provider_sync_due")
                    ),
                    BackgroundTaskRecord.recurrence_seconds.is_(None),
                )
            ).all()
            disconnected_event = db.scalar(
                select(GoogleConnectorEventRecord)
                .where(GoogleConnectorEventRecord.event_type == "evt.connector.google.disconnected")
                .order_by(GoogleConnectorEventRecord.created_at.desc())
                .limit(1)
            )

        assert channels == []
        assert cursors == []
        assert [(event.status, event.error) for event in events] == [
            ("failed", "google_connector_disconnected")
        ]
        assert one_shot_provider_tasks == []
        assert provider.calendar_stop_calls == [
            {
                "access_token": "tok_access_watch",
                "channel_id": calendar_channel_id,
                "provider_resource_id": "res-watch-1",
            }
        ]
        assert provider.gmail_stop_calls == ["tok_access_watch"]
        assert disconnected_event is not None
        assert disconnected_event.payload["cleanup_counts"] == {
            "sync_cursors_deleted": 1,
            "provider_events_failed": 1,
            "background_tasks_deleted": 2,
            "provider_watch_channels": 2,
        }


def test_connect_watch_registration_failure_fails_callback(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", PUBSUB_TOPIC)

    @dataclass
    class FailingWatchProvider:
        def gmail_register_watch(self, **_: Any) -> dict[str, Any]:
            raise GoogleWatchRegistrationFailure(code="google_upstream_timeout")

    oauth_client = ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE])
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _NoCallAdapter()),
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
        assert callback.status_code == 502
        error = callback.json()["error"]
        assert error["code"] == "E_CONNECTOR_CALLBACK_FAILED"
        assert error["details"]["reason"] == "google_upstream_timeout"

        with cast(Any, client.app).state.session_factory() as db:
            connector = db.get(GoogleConnectorRecord, GOOGLE_CONNECTOR_ID)
            assert connector is not None
            assert connector.status == "error"
            assert connector.last_error_code == "google_upstream_timeout"
            channels = db.scalars(select(ProviderWatchChannelRecord)).all()
            event_types = [
                row[0]
                for row in db.execute(
                    select(GoogleConnectorEventRecord.event_type).order_by(
                        GoogleConnectorEventRecord.created_at.asc()
                    )
                ).all()
            ]
    assert channels == []
    assert "evt.connector.google.connect.failed" in event_types
    assert "evt.connector.google.connect.succeeded" not in event_types


def test_connect_watch_registration_defect_propagates_without_connector_error(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", PUBSUB_TOPIC)

    @dataclass
    class DefectiveWatchProvider:
        def gmail_register_watch(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("google_upstream_timeout")

    oauth_client = ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE])
    app = create_test_app(
        database_url=postgres_url,
        model_adapter=cast(ModelAdapter, _NoCallAdapter()),
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app) as client:
        app_state = cast(Any, client.app).state
        app_state.google_oauth_client = oauth_client
        app_state.google_workspace_provider = DefectiveWatchProvider()

        started = client.post("/v1/connectors/google/start")
        state = started.json()["oauth"]["state"]
        with pytest.raises(RuntimeError, match="google_upstream_timeout"):
            client.get(
                "/v1/connectors/google/callback",
                params={"state": state, "code": "connect-defect"},
            )

        with cast(Any, client.app).state.session_factory() as db:
            connector = db.get(GoogleConnectorRecord, GOOGLE_CONNECTOR_ID)
            assert connector is not None
            assert connector.status == "not_connected"
            assert connector.last_error_code is None
            channels = db.scalars(select(ProviderWatchChannelRecord)).all()
            event_types = [
                row[0]
                for row in db.execute(
                    select(GoogleConnectorEventRecord.event_type).order_by(
                        GoogleConnectorEventRecord.created_at.asc()
                    )
                ).all()
            ]
    assert channels == []
    assert "evt.connector.google.connect.failed" not in event_types
    assert "evt.connector.google.connect.succeeded" not in event_types


# --------------------------------------------------------------------------
# The renewal handler re-arms a near-expiry channel.
# --------------------------------------------------------------------------


def _seed_connected_connector(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    settings: AppSettings,
    granted_scopes: list[str],
    status: str = "connected",
    account_subject: str | None = "sub_connected",
    account_email: str | None = "connected@example.com",
    last_error_code: str | None = None,
    last_error_at: datetime | None = None,
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
                    granted_scopes=granted_scopes,
                    access_token_enc=encrypt_secret(
                        plaintext="tok_access_live",
                        secret=settings.connector_encryption_secret,
                        key_version=settings.connector_encryption_key_version,
                        encryption_keys=settings.connector_encryption_keys,
                    ),
                    refresh_token_enc=encrypt_secret(
                        plaintext="tok_refresh_live",
                        secret=settings.connector_encryption_secret,
                        key_version=settings.connector_encryption_key_version,
                        encryption_keys=settings.connector_encryption_keys,
                    ),
                    access_token_expires_at=now + timedelta(hours=1),
                    token_obtained_at=now,
                    encryption_key_version=settings.connector_encryption_key_version,
                    last_error_code=last_error_code,
                    last_error_at=last_error_at,
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


def test_watch_renew_handler_error_connector_does_not_register_watch(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    _seed_connected_connector(
        session_factory,
        now=now,
        settings=settings,
        granted_scopes=[GMAIL_READ_SCOPE],
        status="error",
        last_error_code="account_identity_missing",
        last_error_at=now,
    )
    with session_factory() as db:
        with db.begin():
            db.add(
                ProviderWatchChannelRecord(
                    id="wch_error_connector",
                    provider="google",
                    resource_type="gmail",
                    resource_id="sub_connected",
                    channel_id=None,
                    channel_token=None,
                    provider_resource_id=None,
                    cursor_seed="hist-old",
                    status="active",
                    expires_at=now + timedelta(hours=3),
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
    with session_factory() as db:
        with db.begin():
            connector = db.get(GoogleConnectorRecord, GOOGLE_CONNECTOR_ID)
            tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
            channel = db.get(ProviderWatchChannelRecord, "wch_error_connector")
    assert connector is not None
    assert connector.status == "error"
    assert connector.last_error_code == "account_identity_missing"
    assert tasks == []
    assert channel is not None
    assert channel.cursor_seed == "hist-old"


def test_watch_renew_handler_raises_after_recording_registration_failure(
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
                    id="wch_failing",
                    provider="google",
                    resource_type="gmail",
                    resource_id="sub_connected",
                    channel_id=None,
                    channel_token=None,
                    provider_resource_id=None,
                    cursor_seed="hist-old",
                    status="active",
                    expires_at=now + timedelta(hours=3),
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    @dataclass
    class FailingWatchProvider:
        def gmail_register_watch(self, **_: Any) -> dict[str, Any]:
            raise GoogleWatchRegistrationFailure(code="google_upstream_timeout")

    runtime = GoogleConnectorRuntime(
        oauth_client=ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE]),
        workspace_provider=cast(Any, FailingWatchProvider()),
        redirect_uri=settings.google_oauth_redirect_uri,
        oauth_state_ttl_seconds=settings.google_oauth_state_ttl_seconds,
        encryption_secret=settings.connector_encryption_secret,
        encryption_key_version=settings.connector_encryption_key_version,
        encryption_keys=settings.connector_encryption_keys,
        pubsub_topic=PUBSUB_TOPIC,
        public_webhook_base_url=None,
    )
    monkeypatch.setattr("ariel.worker.build_google_runtime", lambda _settings: runtime)

    with pytest.raises(GoogleWatchRegistrationFailure, match="google_upstream_timeout"):
        process_provider_watch_renew_due(
            session_factory=session_factory,
            settings=settings,
            now_fn=lambda: now,
            new_id_fn=IdFactory(),
        )

    with session_factory() as db:
        with db.begin():
            channel = db.get(ProviderWatchChannelRecord, "wch_failing")
            assert channel is not None
            assert channel.status == "failed"
            assert channel.last_error_code == "google_upstream_timeout"
            assert channel.last_error_at == now


def test_watch_renew_handler_propagates_registration_defect_without_recording_failure(
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
                    id="wch_defect",
                    provider="google",
                    resource_type="gmail",
                    resource_id="sub_connected",
                    channel_id=None,
                    channel_token=None,
                    provider_resource_id=None,
                    cursor_seed="hist-old",
                    status="active",
                    expires_at=now + timedelta(hours=3),
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    @dataclass
    class DefectiveWatchProvider:
        def gmail_register_watch(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("google_upstream_timeout")

    runtime = GoogleConnectorRuntime(
        oauth_client=ConnectOAuthClient(granted_scopes=[GMAIL_READ_SCOPE]),
        workspace_provider=cast(Any, DefectiveWatchProvider()),
        redirect_uri=settings.google_oauth_redirect_uri,
        oauth_state_ttl_seconds=settings.google_oauth_state_ttl_seconds,
        encryption_secret=settings.connector_encryption_secret,
        encryption_key_version=settings.connector_encryption_key_version,
        encryption_keys=settings.connector_encryption_keys,
        pubsub_topic=PUBSUB_TOPIC,
        public_webhook_base_url=None,
    )
    monkeypatch.setattr("ariel.worker.build_google_runtime", lambda _settings: runtime)

    with pytest.raises(RuntimeError, match="google_upstream_timeout"):
        process_provider_watch_renew_due(
            session_factory=session_factory,
            settings=settings,
            now_fn=lambda: now,
            new_id_fn=IdFactory(),
        )

    with session_factory() as db:
        with db.begin():
            channel = db.get(ProviderWatchChannelRecord, "wch_defect")
            assert channel is not None
            assert channel.status == "active"
            assert channel.last_error_code is None
            assert channel.last_error_at is None


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
# The reconcile handler enqueues provider_sync_due per cursor.
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
# A stale Calendar cursor clears state and re-enqueues a full sync.
# --------------------------------------------------------------------------


def test_calendar_410_clears_cursor_and_reenqueues_full_sync(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaleCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            raise GoogleProviderRequestFailure("sync_token_invalid")

    class FakeRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = StaleCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeRuntime)
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    new_id = IdFactory()
    _seed_connected_connector(
        session_factory,
        now=now,
        settings=settings,
        granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE],
    )
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
        settings=settings,
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
            raise GoogleProviderRequestFailure("resource_not_found")

    class FakeRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = StaleGmailProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeRuntime)
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    settings = _settings()
    new_id = IdFactory()
    _seed_connected_connector(
        session_factory,
        now=now,
        settings=settings,
        granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE],
    )
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
        settings=settings,
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
# A sync that finds new items enqueues an agent_wake.
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
    settings = _settings()
    new_id = IdFactory()
    _seed_connected_connector(
        session_factory,
        now=now,
        settings=settings,
        granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE],
    )
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
        settings=settings,
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
    settings = _settings()
    new_id = IdFactory()
    _seed_connected_connector(
        session_factory,
        now=now,
        settings=settings,
        granted_scopes=[GMAIL_READ_SCOPE, CALENDAR_READ_SCOPE],
    )
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
        settings=settings,
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            wake_tasks = db.scalars(
                select(BackgroundTaskRecord).where(BackgroundTaskRecord.task_type == "agent_wake")
            ).all()
    assert wake_tasks == []
