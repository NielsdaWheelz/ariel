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
    ActionAttemptRecord,
    BackgroundTaskRecord,
    EmailThreadWatchRecord,
    ProactiveCaseRecord,
    ProactiveObservationRecord,
    SessionRecord,
    SyncCursorRecord,
    SyncRunRecord,
    TurnRecord,
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
                        "messagesAdded": [
                            {"message": {"id": "msg-1", "threadId": "thr-1", "labelIds": ["INBOX"]}}
                        ],
                    }
                ],
            }
        if page_token == "page-2":
            return {
                "historyId": "hist-3",
                "history": [
                    {
                        "id": "history-2",
                        "messagesAdded": [
                            {"message": {"id": "msg-1", "threadId": "thr-1", "labelIds": ["INBOX"]}}
                        ],
                        "labelsAdded": [
                            {
                                "message": {
                                    "id": "msg-3",
                                    "threadId": "thr-3",
                                    "labelIds": ["INBOX", "IMPORTANT"],
                                }
                            }
                        ],
                        "messagesDeleted": [{"message": {"id": "msg-2", "threadId": "thr-2"}}],
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
            events = db.scalars(
                select(WorkspaceItemEventRecord).order_by(WorkspaceItemEventRecord.id.asc())
            ).all()
            ambient_tasks = db.scalars(
                select(BackgroundTaskRecord).order_by(BackgroundTaskRecord.id.asc())
            ).all()
            workspace_item_count = db.scalar(select(func.count()).select_from(WorkspaceItemRecord))
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
    assert [run.item_count for run in runs] == [4, 0]
    assert [run.observation_count for run in runs] == [0, 0]
    assert [run.cursor_after for run in runs] == ["hist-3", "hist-3"]
    assert workspace_item_count == 3
    assert len(events) == 4
    assert [task.task_type for task in ambient_tasks] == ["ambient_interpretation_due"] * 4
    assert [task.payload for task in ambient_tasks] == [
        {"workspace_item_event_id": event.id} for event in events
    ]
    assert [task.status for task in ambient_tasks] == ["pending"] * 4
    assert observation_count == 0
    assert case_count == 0


def test_gmail_sync_invalid_cursor_fails_closed_without_provider_call(
    db_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("invalid Gmail cursor should stop before provider access")

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
                    cursor_value="hist-expired",
                    cursor_version=7,
                    status="invalid",
                    last_successful_sync_at=None,
                    last_error_code="resource_not_found",
                    last_error_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

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
            assert cursor.status == "invalid"
            assert cursor.cursor_value == "hist-expired"
            assert cursor.last_error_code == "gmail_sync_cursor_invalid"
            assert run.status == "failed"
            assert run.error == "gmail_sync_cursor_invalid"
            assert run.cursor_before == "hist-expired"


def test_gmail_sync_completes_thread_watch_on_reply(
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
                    cursor_version=1,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                SessionRecord(
                    id="ses_email_watch",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_email_watch",
                    session_id="ses_email_watch",
                    user_message="watch thread",
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_email_watch",
                    session_id="ses_email_watch",
                    turn_id="trn_email_watch",
                    proposal_index=1,
                    capability_id="cap.email.thread_watch.create",
                    capability_version="1.0",
                    capability_contract_hash="h" * 64,
                    impact_level="write_reversible",
                    proposed_input={},
                    payload_hash="p" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                EmailThreadWatchRecord(
                    id="etw_reply",
                    provider="google",
                    provider_account_id="con_google",
                    provider_thread_id="thr-1",
                    anchor_message_id="msg-anchor",
                    condition="no_reply_by_deadline",
                    deadline=datetime(2026, 5, 8, 12, 0, tzinfo=UTC),
                    note="waiting on reply",
                    status="active",
                    idempotency_key="watch-key",
                    created_by_action_attempt_id="aat_email_watch",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                EmailThreadWatchRecord(
                    id="etw_overdue",
                    provider="google",
                    provider_account_id="con_google",
                    provider_thread_id="thr-1",
                    anchor_message_id="msg-anchor-overdue",
                    condition="no_reply_by_deadline",
                    deadline=datetime(2026, 5, 7, 11, 59, tzinfo=UTC),
                    note="deadline already passed",
                    status="active",
                    idempotency_key="watch-overdue-key",
                    created_by_action_attempt_id="aat_email_watch",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                EmailThreadWatchRecord(
                    id="etw_anchor",
                    provider="google",
                    provider_account_id="con_google",
                    provider_thread_id="thr-1",
                    anchor_message_id="msg-1",
                    condition="any_reply_arrives",
                    deadline=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
                    note="anchor should not count as reply",
                    status="active",
                    idempotency_key="watch-anchor-key",
                    created_by_action_attempt_id="aat_email_watch",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=db_sessions,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with db_sessions() as db:
        with db.begin():
            watch = db.get(EmailThreadWatchRecord, "etw_reply")
            assert watch is not None
            assert watch.status == "completed"
            assert watch.matched_message_id == "msg-1"
            assert watch.matched_at == now
            assert watch.completed_at == now
            overdue_watch = db.get(EmailThreadWatchRecord, "etw_overdue")
            assert overdue_watch is not None
            assert overdue_watch.status == "due"
            assert overdue_watch.matched_message_id is None
            assert overdue_watch.matched_at is None
            assert overdue_watch.completed_at is None
            anchor_watch = db.get(EmailThreadWatchRecord, "etw_anchor")
            assert anchor_watch is not None
            assert anchor_watch.status == "active"
            assert anchor_watch.matched_message_id is None
            run = db.scalar(select(SyncRunRecord).limit(1))
            assert run is not None
            assert run.item_count == 6
            signal_events = db.scalars(
                select(WorkspaceItemEventRecord)
                .join(
                    WorkspaceItemRecord,
                    WorkspaceItemEventRecord.workspace_item_id == WorkspaceItemRecord.id,
                )
                .where(
                    WorkspaceItemRecord.provider == "ariel",
                    WorkspaceItemRecord.item_type == "internal_state",
                )
                .order_by(WorkspaceItemRecord.external_id.asc())
            ).all()
            assert len(signal_events) == 2
            signal_metadata = {
                event.payload["metadata"]["watch_id"]: event.payload["metadata"]
                for event in signal_events
            }
            assert signal_metadata["etw_reply"]["signal"] == "email_thread_watch_completed"
            assert signal_metadata["etw_reply"]["matched_message_id"] == "msg-1"
            assert signal_metadata["etw_overdue"]["signal"] == "email_thread_watch_due"
            assert signal_metadata["etw_overdue"]["trigger_message_id"] == "msg-1"
            message_events = db.scalars(
                select(WorkspaceItemEventRecord)
                .join(
                    WorkspaceItemRecord,
                    WorkspaceItemEventRecord.workspace_item_id == WorkspaceItemRecord.id,
                )
                .where(
                    WorkspaceItemRecord.item_type == "email_message",
                    WorkspaceItemRecord.external_id == "msg-1",
                )
            ).all()
            message_signal_metadata = [
                event.payload["metadata"]["email_thread_watch_signals"]
                for event in message_events
                if event.payload["metadata"]["email_thread_watch_signals"]
            ]
            assert len(message_signal_metadata) == 1
            assert {
                signal["watch_id"]: signal["signal"] for signal in message_signal_metadata[0]
            } == {
                "etw_reply": "email_thread_watch_completed",
                "etw_overdue": "email_thread_watch_due",
            }
            ambient_tasks = db.scalars(select(BackgroundTaskRecord)).all()
            assert len(ambient_tasks) == 6
            assert {task.payload["workspace_item_event_id"] for task in ambient_tasks} == {
                event.id for event in db.scalars(select(WorkspaceItemEventRecord)).all()
            }


def test_gmail_sync_uses_message_time_for_thread_watch_deadline(
    db_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_at = datetime(2026, 5, 8, 11, 0, tzinfo=UTC)

    class FakeDelayedReplyProvider:
        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("existing Gmail cursor should use history pages")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            return {
                "historyId": "hist-2",
                "history": [
                    {
                        "id": "history-delayed",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "msg-reply",
                                    "threadId": "thr-delay",
                                    "labelIds": ["INBOX"],
                                    "internalDate": str(int(reply_at.timestamp() * 1000)),
                                }
                            }
                        ],
                    }
                ],
            }

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeDelayedReplyProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeDelayedReplyProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 8, 13, 0, tzinfo=UTC)
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
                    cursor_version=1,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                SessionRecord(
                    id="ses_delayed",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_delayed",
                    session_id="ses_delayed",
                    user_message="watch delayed thread",
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_delayed",
                    session_id="ses_delayed",
                    turn_id="trn_delayed",
                    proposal_index=1,
                    capability_id="cap.email.thread_watch.create",
                    capability_version="1.0",
                    capability_contract_hash="h" * 64,
                    impact_level="write_reversible",
                    proposed_input={},
                    payload_hash="p" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                EmailThreadWatchRecord(
                    id="etw_delayed",
                    provider="google",
                    provider_account_id="con_google",
                    provider_thread_id="thr-delay",
                    anchor_message_id="msg-anchor",
                    condition="no_reply_by_deadline",
                    deadline=datetime(2026, 5, 8, 12, 0, tzinfo=UTC),
                    note="reply before deadline should complete",
                    status="active",
                    idempotency_key="watch-delayed-key",
                    created_by_action_attempt_id="aat_delayed",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=db_sessions,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with db_sessions() as db:
        with db.begin():
            watch = db.get(EmailThreadWatchRecord, "etw_delayed")
            assert watch is not None
            assert watch.status == "completed"
            assert watch.matched_message_id == "msg-reply"
            assert watch.matched_at == reply_at
            assert watch.completed_at == now


def test_gmail_sync_does_not_complete_any_reply_watch_after_deadline(
    db_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_at = datetime(2026, 5, 8, 13, 0, tzinfo=UTC)

    class FakeLateReplyProvider:
        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("existing Gmail cursor should use history pages")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            return {
                "historyId": "hist-2",
                "history": [
                    {
                        "id": "history-late",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "msg-late",
                                    "threadId": "thr-late",
                                    "labelIds": ["INBOX"],
                                    "internalDate": str(int(reply_at.timestamp() * 1000)),
                                }
                            }
                        ],
                    }
                ],
            }

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeLateReplyProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeLateReplyProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 8, 14, 0, tzinfo=UTC)
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
                    cursor_version=1,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                SessionRecord(
                    id="ses_late",
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TurnRecord(
                    id="trn_late",
                    session_id="ses_late",
                    user_message="watch late thread",
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                ActionAttemptRecord(
                    id="aat_late",
                    session_id="ses_late",
                    turn_id="trn_late",
                    proposal_index=1,
                    capability_id="cap.email.thread_watch.create",
                    capability_version="1.0",
                    capability_contract_hash="h" * 64,
                    impact_level="write_reversible",
                    proposed_input={},
                    payload_hash="p" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            db.add(
                EmailThreadWatchRecord(
                    id="etw_late",
                    provider="google",
                    provider_account_id="con_google",
                    provider_thread_id="thr-late",
                    anchor_message_id="msg-anchor",
                    condition="any_reply_arrives",
                    deadline=datetime(2026, 5, 8, 12, 0, tzinfo=UTC),
                    note="late reply should not complete",
                    status="active",
                    idempotency_key="watch-late-key",
                    created_by_action_attempt_id="aat_late",
                    matched_message_id=None,
                    matched_at=None,
                    canceled_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    process_provider_sync_due(
        session_factory=db_sessions,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with db_sessions() as db:
        with db.begin():
            watch = db.get(EmailThreadWatchRecord, "etw_late")
            assert watch is not None
            assert watch.status == "failed"
            assert watch.matched_message_id is None
            assert watch.matched_at is None
            assert watch.completed_at is None
