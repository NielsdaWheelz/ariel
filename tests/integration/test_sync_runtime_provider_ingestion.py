from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ariel.config import AppSettings
from ariel.google_connector import GOOGLE_CONNECTOR_ID, GoogleProviderRequestFailure
from ariel.persistence import (
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    ProviderEvidenceBlockRecord,
    ProviderEvidenceRecord,
    SyncCursorRecord,
    SyncRunRecord,
)
from ariel.sync_runtime import ProviderSyncFailure, process_provider_sync_due


PROVIDER_ACCOUNT_ID = "sub_sync"
PROVIDER_ACCOUNT_EMAIL = "sync@example.com"


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
            "provider_account_id": PROVIDER_ACCOUNT_ID,
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
        "status": "succeeded",
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

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        provider_account_id: str,
    ) -> dict[str, Any]:
        assert access_token == "access-token"
        assert provider_account_id == PROVIDER_ACCOUNT_ID
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

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        provider_account_id: str,
    ) -> dict[str, Any]:
        assert access_token == "access-token"
        assert provider_account_id == PROVIDER_ACCOUNT_ID
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
                "provider_account_id": PROVIDER_ACCOUNT_ID,
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
            "status": "succeeded",
        }


@dataclass
class FakeUnreadableGmailProvider:
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def _request_json(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("existing Gmail cursor should use history pages")

    def email_list_history(self, **_: Any) -> dict[str, Any]:
        return {
            "historyId": "hist-2",
            "history": [
                {
                    "id": "history-unreadable",
                    "messagesAdded": [
                        {
                            "message": {
                                "id": "msg-invalid-date",
                                "threadId": "thr-invalid-date",
                                "labelIds": ["INBOX"],
                                "internalDate": "not-a-millis",
                            }
                        },
                        {
                            "message": {
                                "id": "msg-missing-date",
                                "threadId": "thr-missing-date",
                                "labelIds": ["INBOX"],
                            }
                        },
                        {
                            "message": {
                                "id": "msg-blank-date",
                                "threadId": "thr-blank-date",
                                "labelIds": ["INBOX"],
                                "internalDate": "",
                            }
                        },
                        {
                            "message": {
                                "id": "msg-negative-date",
                                "threadId": "thr-negative-date",
                                "labelIds": ["INBOX"],
                                "internalDate": "-1",
                            }
                        },
                    ],
                }
            ],
        }

    def email_read(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
        provider_account_id: str,
    ) -> dict[str, Any]:
        assert access_token == "access-token"
        assert provider_account_id == PROVIDER_ACCOUNT_ID
        self.read_calls.append(normalized_input)
        raise GoogleProviderRequestFailure("resource_not_found")


def _settings() -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def _seed_sync_cursor(
    session_factory: sessionmaker[Session],
    new_id: IdFactory,
    *,
    resource_type: str,
    resource_id: str,
    cursor_value: str,
    now: datetime,
) -> None:
    with session_factory() as db:
        with db.begin():
            db.add(
                SyncCursorRecord(
                    id=new_id("cur"),
                    provider="google",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    cursor_value=cursor_value,
                    cursor_version=1,
                    status="ready",
                    last_successful_sync_at=None,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def _seed_google_connector(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    status: str = "connected",
    account_subject: str | None = PROVIDER_ACCOUNT_ID,
    account_email: str | None = PROVIDER_ACCOUNT_EMAIL,
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
                    granted_scopes=[
                        "https://www.googleapis.com/auth/calendar.readonly",
                        "https://www.googleapis.com/auth/gmail.readonly",
                    ],
                    access_token_enc=None,
                    refresh_token_enc=None,
                    access_token_expires_at=None,
                    token_obtained_at=None,
                    encryption_key_version="v1",
                    last_error_code=last_error_code,
                    last_error_at=last_error_at,
                    created_at=now,
                    updated_at=now,
                )
            )


def test_gmail_sync_error_connector_fails_before_provider_reads(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class NoCallGmailProvider:
        history_calls: int = 0

        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("connector error should stop before Gmail profile access")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            self.history_calls += 1
            raise AssertionError("connector error should stop before Gmail history reads")

    providers: list[NoCallGmailProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: NoCallGmailProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = NoCallGmailProvider()
            providers.append(self.workspace_provider)

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(
        session_factory,
        now=now,
        status="error",
        last_error_code="account_identity_missing",
        last_error_at=now,
    )
    _seed_sync_cursor(
        session_factory,
        new_id,
        resource_type="gmail",
        resource_id="primary",
        cursor_value="hist-1",
        now=now,
    )

    with pytest.raises(ProviderSyncFailure, match="account_identity_missing"):
        process_provider_sync_due(
            session_factory=session_factory,
            task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
            settings=_settings(),
            now_fn=lambda: now,
            new_id_fn=new_id,
        )

    with session_factory() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            run = db.scalar(select(SyncRunRecord).limit(1))
            provider_objects = db.scalars(select(GoogleProviderObjectRecord)).all()
            evidence_rows = db.scalars(select(ProviderEvidenceRecord)).all()

    assert len(providers) == 1
    assert providers[0].history_calls == 0
    assert cursor is not None
    assert cursor.status == "error"
    assert cursor.last_error_code == "account_identity_missing"
    assert run is not None
    assert run.status == "failed"
    assert run.error == "account_identity_missing"
    assert provider_objects == []
    assert evidence_rows == []


def test_gmail_sync_bootstraps_empty_cursor_from_profile(
    session_factory: sessionmaker[Session],
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
    _seed_google_connector(session_factory, now=now)

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
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
    session_factory: sessionmaker[Session],
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
    _seed_google_connector(session_factory, now=now)
    with session_factory() as db:
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
                assert cursor is not None
                cursor.cursor_value = "hist-1"

    with session_factory() as db:
        with db.begin():
            runs = db.scalars(select(SyncRunRecord).order_by(SyncRunRecord.id.asc())).all()
            tasks = db.scalars(
                select(BackgroundTaskRecord).order_by(BackgroundTaskRecord.id.asc())
            ).all()

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
    # Each sync run that finds new data wakes the agent; nothing else.
    assert [task.task_type for task in tasks] == ["agent_wake", "agent_wake"]


def test_gmail_sync_hydrates_added_messages_into_body_evidence(
    session_factory: sessionmaker[Session],
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
    _seed_google_connector(session_factory, now=now)
    with session_factory() as db:
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
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
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
    assert provider_object.provider_account_id == PROVIDER_ACCOUNT_ID
    assert provider_object.external_id == "msg-body"
    assert provider_object.thread_external_id == "thr-body"
    assert provider_object.source_timestamp == datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
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
    assert evidence.provider_account_id == PROVIDER_ACCOUNT_ID
    assert evidence.external_id == "msg-body"
    assert evidence.thread_external_id == "thr-body"
    assert evidence.source_timestamp == datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
    assert evidence.content_digest == "b" * 64
    assert evidence.taint == "provider_untrusted"
    assert block is not None
    assert block.evidence_id == evidence.id
    assert block.block_index == 0
    assert block.block_kind == "body"
    assert block.text == "Please send the launch checklist by Friday at 5pm."
    assert block.digest == "d" * 64
    # The synced message wakes the agent through the shared push/poll sync path.
    assert [task.task_type for task in tasks] == ["agent_wake"]


def test_gmail_sync_keeps_missing_or_invalid_internal_date_absent(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[FakeUnreadableGmailProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeUnreadableGmailProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeUnreadableGmailProvider()
            providers.append(self.workspace_provider)

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)
    with session_factory() as db:
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
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            provider_objects = db.scalars(
                select(GoogleProviderObjectRecord).order_by(
                    GoogleProviderObjectRecord.external_id.asc()
                )
            ).all()
            evidence_rows = db.scalars(
                select(ProviderEvidenceRecord).order_by(ProviderEvidenceRecord.external_id.asc())
            ).all()

    assert len(providers) == 1
    assert providers[0].read_calls == [
        {"message_id": "msg-invalid-date", "thread_id": None, "mode": "message"},
        {"message_id": "msg-missing-date", "thread_id": None, "mode": "message"},
        {"message_id": "msg-blank-date", "thread_id": None, "mode": "message"},
        {"message_id": "msg-negative-date", "thread_id": None, "mode": "message"},
    ]
    assert [row.external_id for row in provider_objects] == [
        "msg-blank-date",
        "msg-invalid-date",
        "msg-missing-date",
        "msg-negative-date",
    ]
    assert [row.source_timestamp for row in provider_objects] == [None, None, None, None]
    assert [row.observed_at for row in provider_objects] == [now, now, now, now]
    assert [row.provider_account_id for row in provider_objects] == [PROVIDER_ACCOUNT_ID] * 4
    assert [row.external_id for row in evidence_rows] == [
        "msg-blank-date",
        "msg-invalid-date",
        "msg-missing-date",
        "msg-negative-date",
    ]
    assert [row.source_timestamp for row in evidence_rows] == [None, None, None, None]
    assert [row.observed_at for row in evidence_rows] == [now, now, now, now]
    assert [row.provider_account_id for row in evidence_rows] == [PROVIDER_ACCOUNT_ID] * 4
    assert [row.lifecycle_state for row in evidence_rows] == [
        "unavailable",
        "unavailable",
        "unavailable",
        "unavailable",
    ]
    assert [row.extraction_status for row in evidence_rows] == [
        "failed",
        "failed",
        "failed",
        "failed",
    ]
    assert [row.metadata_json["read_outcome"]["reason_code"] for row in evidence_rows] == [
        "gmail_message_unavailable",
        "gmail_message_unavailable",
        "gmail_message_unavailable",
        "gmail_message_unavailable",
    ]


def test_calendar_sync_keeps_missing_or_invalid_updated_timestamp_absent(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            return {
                "nextSyncToken": "sync-token-2",
                "items": [
                    {
                        "id": "evt-invalid-updated",
                        "status": "confirmed",
                        "summary": "Invalid updated",
                        "updated": "not-a-timestamp",
                        "start": {"dateTime": "2026-05-20T10:00:00Z"},
                        "end": {"dateTime": "2026-05-20T11:00:00Z"},
                    },
                    {
                        "id": "evt-missing-updated",
                        "status": "confirmed",
                        "summary": "Missing updated",
                        "start": {"dateTime": "2026-05-21T10:00:00Z"},
                        "end": {"dateTime": "2026-05-21T11:00:00Z"},
                    },
                ],
            }

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)
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
        task_payload={"provider": "google", "resource_type": "calendar", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        with db.begin():
            provider_objects = db.scalars(
                select(GoogleProviderObjectRecord).order_by(
                    GoogleProviderObjectRecord.external_id.asc()
                )
            ).all()
            evidence_rows = db.scalars(
                select(ProviderEvidenceRecord).order_by(ProviderEvidenceRecord.external_id.asc())
            ).all()

    assert [row.external_id for row in provider_objects] == [
        "evt-invalid-updated",
        "evt-missing-updated",
    ]
    assert [row.source_timestamp for row in provider_objects] == [None, None]
    assert [row.observed_at for row in provider_objects] == [now, now]
    assert [row.provider_account_id for row in provider_objects] == [PROVIDER_ACCOUNT_ID] * 2
    assert [row.external_id for row in evidence_rows] == [
        "evt-invalid-updated",
        "evt-missing-updated",
    ]
    assert [row.source_timestamp for row in evidence_rows] == [None, None]
    assert [row.observed_at for row in evidence_rows] == [now, now]
    assert [row.provider_account_id for row in evidence_rows] == [PROVIDER_ACCOUNT_ID] * 2


def test_calendar_sync_uses_bounded_delta_pages(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class BoundedCalendarProvider:
        calls: list[dict[str, Any]] = field(default_factory=list)

        def calendar_list_event_deltas(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            assert kwargs["max_results"] == 10
            if kwargs["page_token"] is None:
                return {"nextPageToken": "page-2", "items": []}
            return {"nextSyncToken": "sync-token-2", "items": []}

    providers: list[BoundedCalendarProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: BoundedCalendarProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = BoundedCalendarProvider()
            providers.append(self.workspace_provider)

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "calendar", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    assert len(providers) == 1
    assert len(providers[0].calls) == 2
    assert providers[0].calls[0]["page_token"] is None
    assert providers[0].calls[0]["time_min"] is not None
    assert providers[0].calls[1]["page_token"] == "page-2"

    with session_factory() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            run = db.scalar(select(SyncRunRecord).limit(1))

    assert cursor is not None
    assert cursor.cursor_value == "sync-token-2"
    assert cursor.status == "ready"
    assert run is not None
    assert run.status == "succeeded"


def test_calendar_provider_request_failure_marks_sync_failed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            raise GoogleProviderRequestFailure("google_upstream_timeout")

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FailingCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)
    _seed_sync_cursor(
        session_factory,
        new_id,
        resource_type="calendar",
        resource_id="primary",
        cursor_value="sync-token-1",
        now=now,
    )

    with pytest.raises(GoogleProviderRequestFailure, match="google_upstream_timeout"):
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
            tasks = db.scalars(select(BackgroundTaskRecord)).all()

    assert cursor is not None
    assert cursor.status == "error"
    assert cursor.last_error_code == "google_upstream_timeout"
    assert run is not None
    assert run.status == "failed"
    assert run.error == "google_upstream_timeout"
    assert tasks == []


def test_unexpected_calendar_provider_defect_is_not_persisted_as_sync_failure(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BuggyCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("calendar bug")

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = BuggyCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)
    _seed_sync_cursor(
        session_factory,
        new_id,
        resource_type="calendar",
        resource_id="primary",
        cursor_value="sync-token-1",
        now=now,
    )

    with pytest.raises(RuntimeError, match="calendar bug"):
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

    assert cursor is not None
    assert cursor.status == "syncing"
    assert cursor.last_error_code is None
    assert run is not None
    assert run.status == "running"
    assert run.error is None


def test_gmail_read_provider_request_failure_marks_sync_failed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingReadGmailProvider:
        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("existing Gmail cursor should use history pages")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            return {
                "historyId": "hist-2",
                "history": [
                    {
                        "id": "history-failing-read",
                        "messagesAdded": [{"message": {"id": "msg-1", "threadId": "thr-1"}}],
                    }
                ],
            }

        def email_read(self, **_: Any) -> dict[str, Any]:
            raise GoogleProviderRequestFailure("google_upstream_timeout")

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FailingReadGmailProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)
    _seed_sync_cursor(
        session_factory,
        new_id,
        resource_type="gmail",
        resource_id="primary",
        cursor_value="hist-1",
        now=now,
    )

    with pytest.raises(GoogleProviderRequestFailure, match="google_upstream_timeout"):
        process_provider_sync_due(
            session_factory=session_factory,
            task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
            settings=_settings(),
            now_fn=lambda: now,
            new_id_fn=new_id,
        )

    with session_factory() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            run = db.scalar(select(SyncRunRecord).limit(1))

    assert cursor is not None
    assert cursor.status == "error"
    assert cursor.last_error_code == "google_upstream_timeout"
    assert run is not None
    assert run.status == "failed"
    assert run.error == "google_upstream_timeout"


def test_unexpected_gmail_read_defect_is_not_persisted_as_sync_failure(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BuggyReadGmailProvider:
        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("existing Gmail cursor should use history pages")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            return {
                "historyId": "hist-2",
                "history": [
                    {
                        "id": "history-buggy-read",
                        "messagesAdded": [{"message": {"id": "msg-1", "threadId": "thr-1"}}],
                    }
                ],
            }

        def email_read(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("read bug")

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = BuggyReadGmailProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    _seed_google_connector(session_factory, now=now)
    _seed_sync_cursor(
        session_factory,
        new_id,
        resource_type="gmail",
        resource_id="primary",
        cursor_value="hist-1",
        now=now,
    )

    with pytest.raises(RuntimeError, match="read bug"):
        process_provider_sync_due(
            session_factory=session_factory,
            task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
            settings=_settings(),
            now_fn=lambda: now,
            new_id_fn=new_id,
        )

    with session_factory() as db:
        with db.begin():
            cursor = db.scalar(select(SyncCursorRecord).limit(1))
            run = db.scalar(select(SyncRunRecord).limit(1))

    assert cursor is not None
    assert cursor.status == "syncing"
    assert cursor.last_error_code is None
    assert run is not None
    assert run.status == "running"
    assert run.error is None


def test_gmail_sync_invalid_cursor_fails_closed_without_provider_call(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("invalid Gmail cursor should stop before provider access")

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    new_id = IdFactory()
    with session_factory() as db:
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
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
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
