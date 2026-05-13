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
    GoogleProviderObjectRecord,
    ProactiveCaseRecord,
    ProactiveObservationRecord,
    ProviderEvidenceBlockRecord,
    ProviderEvidenceRecord,
    SessionRecord,
    SyncCursorRecord,
    SyncRunRecord,
    TurnRecord,
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


def gmail_message_read_output(
    *,
    message_id: str,
    thread_id: str,
    published_at: str,
    body_text: str = "Thanks, I will follow up by Friday.",
) -> dict[str, Any]:
    return {
        "schema_version": "google.gmail.message_evidence.v1",
        "mode": "message",
        "message": {
            "provider_account_id": "con_google",
            "message_id": message_id,
            "thread_id": thread_id,
            "history_id": "hist-2",
            "rfc_message_id": f"<{message_id}@example.com>",
            "subject": "Follow up",
            "subject_key": "follow up",
            "sender": {"email": "manager@example.com", "display_name": "Manager"},
            "recipients": [{"email": "user@example.com", "display_name": "User"}],
            "cc": [],
            "bcc": [],
            "reply_to": [],
            "internal_date_ms": 1778173200000,
            "header_date": published_at,
            "direction": "received",
            "labels": ["INBOX"],
            "attachments": [],
            "body": {
                "preferred_mime_type": "text/plain",
                "truncated": False,
                "body_digest": "b" * 64,
                "decode_notes": [],
            },
            "provider_url": f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
            "raw_payload_digest": "r" * 64,
        },
        "published_at": published_at,
        "evidence": {
            "source_kind": "gmail_message",
            "message_id": message_id,
            "thread_id": thread_id,
            "body_digest": "b" * 64,
            "blocks": [
                {
                    "block_id": "block-1",
                    "kind": "body",
                    "source_mime_type": "text/plain",
                    "charset": "utf-8",
                    "text": body_text,
                    "digest": "d" * 64,
                    "truncated": False,
                }
            ],
            "truncated": False,
            "decode_notes": [],
        },
        "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
        "retrieved_at": published_at,
    }


@dataclass
class FakePagedGmailProvider:
    history_calls: list[dict[str, str | None]] = field(default_factory=list)
    read_calls: list[dict[str, Any]] = field(default_factory=list)

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

    def email_read(self, *, access_token: str, normalized_input: dict[str, Any]) -> dict[str, Any]:
        assert access_token == "access-token"
        self.read_calls.append(normalized_input)
        message_id = normalized_input["message_id"]
        assert normalized_input in [
            {"message_id": "msg-1", "thread_id": None, "mode": "message"},
            {"message_id": "msg-3", "thread_id": None, "mode": "message"},
        ]
        return gmail_message_read_output(
            message_id=message_id,
            thread_id="thr-1" if message_id == "msg-1" else "thr-3",
            published_at="2026-05-07T12:00:00Z",
        )


@dataclass
class FakeFullBodyGmailProvider:
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def _request_json(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("existing Gmail cursor should use history pages")

    def email_list_history(self, **_: Any) -> dict[str, Any]:
        return {
            "historyId": "hist-2",
            "history": [
                {
                    "id": "history-body",
                    "messagesAdded": [
                        {
                            "message": {
                                "id": "msg-body",
                                "threadId": "thr-body",
                                "labelIds": ["INBOX"],
                                "internalDate": "1778173200000",
                            }
                        }
                    ],
                }
            ],
        }

    def email_read(self, *, access_token: str, normalized_input: dict[str, Any]) -> dict[str, Any]:
        assert access_token == "access-token"
        self.read_calls.append(normalized_input)
        assert normalized_input == {
            "message_id": "msg-body",
            "thread_id": None,
            "mode": "message",
        }
        return {
            "schema_version": "google.gmail.message_evidence.v1",
            "mode": "message",
            "message": {
                "provider_account_id": "con_google",
                "message_id": "msg-body",
                "thread_id": "thr-body",
                "history_id": "hist-2",
                "rfc_message_id": "<msg-body@example.com>",
                "subject": "Follow up on launch checklist",
                "subject_key": "follow up on launch checklist",
                "sender": {"email": "manager@example.com", "display_name": "Manager"},
                "recipients": [{"email": "user@example.com", "display_name": "User"}],
                "cc": [],
                "bcc": [],
                "reply_to": [],
                "internal_date_ms": 1778173200000,
                "header_date": "2026-05-07T09:00:00Z",
                "direction": "received",
                "labels": ["INBOX"],
                "attachments": [],
                "body": {
                    "preferred_mime_type": "text/plain",
                    "truncated": False,
                    "body_digest": "b" * 64,
                    "decode_notes": [],
                },
                "provider_url": "https://mail.google.com/mail/u/0/#inbox/msg-body",
                "raw_payload_digest": "r" * 64,
            },
            "published_at": "2026-05-07T09:00:00Z",
            "evidence": {
                "source_kind": "gmail_message",
                "message_id": "msg-body",
                "thread_id": "thr-body",
                "body_digest": "b" * 64,
                "blocks": [
                    {
                        "block_id": "block-1",
                        "kind": "body",
                        "source_mime_type": "text/plain",
                        "charset": "utf-8",
                        "text": "Please send the launch checklist by Friday at 5pm.",
                        "digest": "d" * 64,
                        "truncated": False,
                    }
                ],
                "truncated": False,
                "decode_notes": [],
            },
            "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
            "retrieved_at": "2026-05-07T09:01:00Z",
        }


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
            tasks = db.scalars(
                select(BackgroundTaskRecord).order_by(BackgroundTaskRecord.id.asc())
            ).all()
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
    assert providers[0].read_calls == [
        {"message_id": "msg-1", "thread_id": None, "mode": "message"},
        {"message_id": "msg-3", "thread_id": None, "mode": "message"},
    ]
    assert providers[1].read_calls == providers[0].read_calls
    assert [run.item_count for run in runs] == [4, 4]
    assert [run.observation_count for run in runs] == [0, 0]
    assert [run.cursor_after for run in runs] == ["hist-3", "hist-3"]
    ambient_tasks = [task for task in tasks if task.task_type == "ambient_interpretation_due"]
    extraction_tasks = [
        task for task in tasks if task.task_type == "workspace_commitment_extraction_due"
    ]
    assert ambient_tasks == []
    assert [task.task_type for task in extraction_tasks] == [
        "workspace_commitment_extraction_due",
        "workspace_commitment_extraction_due",
    ]
    assert all(set(task.payload) == {"evidence_id"} for task in extraction_tasks)
    assert observation_count == 0
    assert case_count == 0


def test_gmail_sync_hydrates_added_messages_into_body_evidence(
    db_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[FakeFullBodyGmailProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeFullBodyGmailProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeFullBodyGmailProvider()
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

    process_provider_sync_due(
        session_factory=db_sessions,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with db_sessions() as db:
        with db.begin():
            run = db.scalar(select(SyncRunRecord).limit(1))
            provider_object = db.scalar(select(GoogleProviderObjectRecord).limit(1))
            evidence = db.scalar(select(ProviderEvidenceRecord).limit(1))
            block = db.scalar(select(ProviderEvidenceBlockRecord).limit(1))
            tasks = db.scalars(
                select(BackgroundTaskRecord).order_by(BackgroundTaskRecord.id.asc())
            ).all()

    assert len(providers) == 1
    assert providers[0].read_calls == [
        {"message_id": "msg-body", "thread_id": None, "mode": "message"}
    ]
    assert run is not None
    assert run.status == "succeeded"
    assert run.item_count == 1
    assert provider_object is not None
    assert provider_object.external_id == "msg-body"
    assert provider_object.thread_external_id == "thr-body"
    assert provider_object.content_digest == "r" * 64
    assert provider_object.metadata_json == {
        "history_id": "history-body",
        "label_ids": ["INBOX"],
        "change": "messagesAdded",
        "subject": "Follow up on launch checklist",
        "subject_key": "follow up on launch checklist",
        "direction": "received",
        "attachments": [],
        "read_outcome": {"status": "ok", "reason_code": None, "recovery": None},
    }
    assert evidence is not None
    assert evidence.provider_object_id == provider_object.id
    assert evidence.external_id == "msg-body"
    assert evidence.thread_external_id == "thr-body"
    assert evidence.content_digest == "b" * 64
    assert evidence.taint == "provider_untrusted"
    assert block is not None
    assert block.evidence_id == evidence.id
    assert block.block_index == 0
    assert block.block_kind == "body"
    assert block.text == "Please send the launch checklist by Friday at 5pm."
    assert block.digest == "d" * 64
    assert [task.task_type for task in tasks] == ["workspace_commitment_extraction_due"]
    assert tasks[0].payload == {"evidence_id": evidence.id}


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
            assert run.item_count == 4
            tasks = db.scalars(select(BackgroundTaskRecord)).all()
            ambient_tasks = [
                task for task in tasks if task.task_type == "ambient_interpretation_due"
            ]
            extraction_tasks = [
                task for task in tasks if task.task_type == "workspace_commitment_extraction_due"
            ]
            assert ambient_tasks == []
            assert len(extraction_tasks) == 2
            evidence_rows = db.scalars(select(ProviderEvidenceRecord)).all()
            assert len(evidence_rows) == 2
            assert {evidence.taint for evidence in evidence_rows} == {"provider_untrusted"}


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

        def email_read(
            self, *, access_token: str, normalized_input: dict[str, Any]
        ) -> dict[str, Any]:
            assert access_token == "access-token"
            assert normalized_input == {
                "message_id": "msg-reply",
                "thread_id": None,
                "mode": "message",
            }
            return gmail_message_read_output(
                message_id="msg-reply",
                thread_id="thr-delay",
                published_at="2026-05-08T11:00:00Z",
            )

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

        def email_read(
            self, *, access_token: str, normalized_input: dict[str, Any]
        ) -> dict[str, Any]:
            assert access_token == "access-token"
            assert normalized_input == {
                "message_id": "msg-late",
                "thread_id": None,
                "mode": "message",
            }
            return gmail_message_read_output(
                message_id="msg-late",
                thread_id="thr-late",
                published_at="2026-05-08T13:00:00Z",
            )

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
