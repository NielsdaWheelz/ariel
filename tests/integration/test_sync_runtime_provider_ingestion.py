from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests
from ariel.persistence import (
    ProactiveCaseRecord,
    ProactiveObservationRecord,
    SyncCursorRecord,
    SyncRunRecord,
    WorkspaceItemEventRecord,
    WorkspaceItemRecord,
)
from ariel.sync_runtime import process_provider_sync_due


@dataclass
class IdFactory:
    counters: dict[str, int] = field(default_factory=dict)

    def __call__(self, prefix: str) -> str:
        next_value = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = next_value
        return f"{prefix}_{next_value:028d}"


@dataclass
class FakeGmailBootstrapProvider:
    gmail_api_base_url: str = "https://gmail.example"
    profile_calls: int = 0
    history_calls: int = 0

    def _request_json(self, **kwargs: Any) -> dict[str, Any]:
        self.profile_calls += 1
        assert kwargs["method"] == "GET"
        assert kwargs["url"] == "https://gmail.example/users/me/profile"
        assert kwargs["access_token"] == "access-token"
        return {"historyId": "hist-bootstrap"}

    def email_list_history(self, **_: Any) -> dict[str, Any]:
        self.history_calls += 1
        raise AssertionError("empty Gmail cursor should bootstrap from profile")


@dataclass
class FakePagedGmailProvider:
    history_calls: list[dict[str, str | None]] = field(default_factory=list)

    def _request_json(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("existing Gmail cursor should use history pages")

    def email_list_history(
        self,
        *,
        access_token: str,
        start_history_id: str | None = None,
        user_id: str = "me",
        page_token: str | None = None,
        max_results: int | None = None,
        history_types: list[str] | None = None,
        label_id: str | None = None,
    ) -> dict[str, Any]:
        del user_id, max_results, history_types, label_id
        assert access_token == "access-token"
        self.history_calls.append({"start_history_id": start_history_id, "page_token": page_token})
        if page_token is None:
            return {
                "historyId": "hist-2",
                "nextPageToken": "page-2",
                "history": [
                    {
                        "id": "history-1",
                        "messagesAdded": [{"message": {"id": "msg-1"}}],
                    }
                ],
            }
        if page_token == "page-2":
            return {
                "historyId": "hist-3",
                "history": [
                    {
                        "id": "history-2",
                        "messagesAdded": [{"message": {"id": "msg-1"}}],
                        "messagesDeleted": [{"message": {"id": "msg-2"}}],
                    }
                ],
            }
        raise AssertionError(f"unexpected page token: {page_token}")


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def db_sessions(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    engine.dispose()


def _settings() -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def test_gmail_sync_bootstraps_empty_cursor_from_profile(
    db_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[FakeGmailBootstrapProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeGmailBootstrapProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeGmailBootstrapProvider()
            providers.append(self.workspace_provider)

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()

    process_provider_sync_due(
        session_factory=db_sessions,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with db_sessions() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            run = db.scalar(select(SyncRunRecord).limit(1))
            assert cursor is not None
            assert run is not None
            assert cursor.cursor_value == "hist-bootstrap"
            assert cursor.cursor_version == 1
            assert cursor.status == "ready"
            assert run.cursor_before is None
            assert run.cursor_after == "hist-bootstrap"
            assert run.item_count == 0
            assert run.observation_count == 0

    assert len(providers) == 1
    assert providers[0].profile_calls == 1
    assert providers[0].history_calls == 0


def test_gmail_sync_follows_history_pages_and_dedupes_replayed_events(
    db_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[FakePagedGmailProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakePagedGmailProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakePagedGmailProvider()
            providers.append(self.workspace_provider)

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with db_sessions() as db:
        with db.begin():
            db.add(
                SyncCursorRecord(
                    id=new_id("cur"),
                    provider="google",
                    resource_type="gmail",
                    resource_id="primary",
                    cursor_value="hist-1",
                    cursor_version=7,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    for _ in range(2):
        process_provider_sync_due(
            session_factory=db_sessions,
            task_payload={
                "provider": "google",
                "resource_type": "gmail",
                "resource_id": "primary",
            },
            settings=_settings(),
            now_fn=lambda: now,
            new_id_fn=new_id,
        )
        with db_sessions() as db:
            with db.begin():
                cursor = db.scalar(select(SyncCursorRecord).limit(1))
                assert cursor is not None
                cursor.cursor_value = "hist-1"

    with db_sessions() as db:
        with db.begin():
            runs = db.scalars(select(SyncRunRecord).order_by(SyncRunRecord.id.asc())).all()
            workspace_item_count = db.scalar(select(func.count()).select_from(WorkspaceItemRecord))
            event_count = db.scalar(select(func.count()).select_from(WorkspaceItemEventRecord))
            observation_count = db.scalar(
                select(func.count()).select_from(ProactiveObservationRecord)
            )
            case_count = db.scalar(select(func.count()).select_from(ProactiveCaseRecord))

    assert len(providers) == 2
    assert providers[0].history_calls == [
        {"start_history_id": "hist-1", "page_token": None},
        {"start_history_id": "hist-1", "page_token": "page-2"},
    ]
    assert providers[1].history_calls == providers[0].history_calls
    assert [run.item_count for run in runs] == [3, 0]
    assert [run.observation_count for run in runs] == [3, 0]
    assert [run.cursor_after for run in runs] == ["hist-3", "hist-3"]
    assert workspace_item_count == 2
    assert event_count == 3
    assert observation_count == 3
    assert case_count == 2
