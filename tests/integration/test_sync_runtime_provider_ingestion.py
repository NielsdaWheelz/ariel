from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from ariel.config import AppSettings
from ariel.google_connector import GOOGLE_CONNECTOR_ID, GoogleProviderRequestFailure
from ariel.google_workspace_normalization import normalize_calendar_event
from ariel.persistence import (
    BackgroundTaskRecord,
    GoogleConnectorRecord,
    GoogleProviderObjectRecord,
    ProviderEvidenceBlockRecord,
    ProviderEvidenceRecord,
    SyncCursorRecord,
    SyncRunRecord,
)
from ariel.sync_runtime import (
    ProviderSyncFailure,
    _acquire_provider_sync_lock,
    _provider_sync_lock_id,
    _release_provider_sync_lock,
    process_provider_sync_due,
)


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
    direction: str = "received",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    label_values = labels or ["INBOX"]
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
            "direction": direction,
            "labels": label_values,
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
class FakeGmailLifecycleProvider:
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def _request_json(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("existing Gmail cursor should use history pages")

    def email_list_history(self, **_: Any) -> dict[str, Any]:
        return {
            "historyId": "hist-4",
            "history": [
                {
                    "id": "history-lifecycle",
                    "labelsRemoved": [
                        {
                            "message": {
                                "id": "msg-label",
                                "threadId": "thr-label",
                                "labelIds": ["INBOX"],
                            }
                        }
                    ],
                    "messagesDeleted": [
                        {"message": {"id": "msg-delete", "threadId": "thr-delete"}}
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
            "message_id": "msg-label",
            "thread_id": None,
            "mode": "message",
        }
        return gmail_message_read_output(
            message_id="msg-label",
            thread_id="thr-label",
            published_at="2026-05-07T11:00:00Z",
        )


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


def test_provider_sync_lock_pins_database_backend_until_release(postgres_url: str) -> None:
    engine = create_engine(
        postgres_url, future=True, pool_pre_ping=True, pool_size=2, max_overflow=0
    )
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    lock_conn = None
    lock_id = None
    expected_lock_id = _provider_sync_lock_id("provider_sync", "google", "calendar", "primary")
    try:
        lock_conn, lock_id = _acquire_provider_sync_lock(
            factory,
            provider="google",
            resource_type="calendar",
            resource_id="primary",
        )
        assert lock_id == expected_lock_id

        with factory() as contender:
            assert (
                contender.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": expected_lock_id},
                )
                is False
            )

        _release_provider_sync_lock(lock_conn, lock_id)
        lock_conn = None
        lock_id = None

        with factory() as contender:
            assert (
                contender.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": expected_lock_id},
                )
                is True
            )
            contender.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": expected_lock_id},
            )
            contender.commit()
    finally:
        _release_provider_sync_lock(lock_conn, lock_id)
        engine.dispose()


def test_provider_sync_lock_busy_fails_fast_without_sync_run(
    session_factory: sessionmaker[Session],
) -> None:
    lock_id = _provider_sync_lock_id("provider_sync", "google", "calendar", "primary")
    lock_db = session_factory()
    try:
        assert (
            lock_db.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
            is True
        )

        with pytest.raises(ProviderSyncFailure, match="provider_sync_lock_busy"):
            process_provider_sync_due(
                session_factory=session_factory,
                task_payload={
                    "provider": "google",
                    "resource_type": "calendar",
                    "resource_id": "primary",
                },
                settings=_settings(),
                now_fn=lambda: datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
                new_id_fn=IdFactory(),
            )
    finally:
        lock_db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
        lock_db.commit()
        lock_db.close()

    with session_factory() as db:
        assert db.scalars(select(SyncRunRecord)).all() == []


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
    # Each sync run with a new inbound message wakes the agent; label and delete
    # deltas only update provider state.
    assert [task.task_type for task in tasks] == ["agent_wake", "agent_wake"]
    first_payload = tasks[0].payload
    assert first_payload["kind"] == "provider_sync_review"
    assert first_payload["provider"] == "google"
    assert first_payload["resource_type"] == "gmail"
    assert first_payload["resource_id"] == "primary"
    assert first_payload["item_count"] == 1
    assert first_payload["omitted_item_count"] == 0
    assert first_payload["items"][0]["message_id"] == "msg-1"
    assert first_payload["items"][0]["subject"] == "Follow up"
    assert first_payload["items"][0]["preview_kind"] == "provider_sync_preview"
    assert first_payload["items"][0]["preview_truncated"] is False
    assert first_payload["items"][0]["requires_read_for_body_claims"] is True
    assert first_payload["items"][0]["provider_evidence_refs"][0][
        "provider_evidence_id"
    ].startswith("pev_")
    assert first_payload["items"][0]["evidence_blocks"][0]["text"] == (
        "Thanks, I will follow up by Friday."
    )
    assert first_payload["items"][0]["evidence_blocks"][0]["preview_truncated"] is False


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


def test_gmail_sync_records_sent_messages_without_waking_agent(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSentGmailProvider:
        def _request_json(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("existing Gmail cursor should use history pages")

        def email_list_history(self, **_: Any) -> dict[str, Any]:
            return {
                "historyId": "hist-2",
                "history": [
                    {
                        "id": "history-sent",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "msg-sent",
                                    "threadId": "thr-sent",
                                    "labelIds": ["SENT"],
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
            assert normalized_input == {
                "message_id": "msg-sent",
                "thread_id": None,
                "mode": "message",
            }
            return gmail_message_read_output(
                message_id="msg-sent",
                thread_id="thr-sent",
                published_at="2026-05-07T09:00:00Z",
                direction="sent",
                labels=["SENT"],
            )

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeSentGmailProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeSentGmailProvider()

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

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        run = db.scalar(select(SyncRunRecord).limit(1))
        provider_object = db.scalar(select(GoogleProviderObjectRecord).limit(1))
        tasks = db.scalars(select(BackgroundTaskRecord)).all()

    assert run is not None
    assert run.status == "succeeded"
    assert run.item_count == 1
    assert provider_object is not None
    assert provider_object.metadata_json["direction"] == "sent"
    assert tasks == []


def test_gmail_sync_restores_same_digest_superseded_body_evidence(
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
    message_time = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
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
    with session_factory() as db:
        with db.begin():
            provider_object = GoogleProviderObjectRecord(
                id=new_id("gpo"),
                provider_account_id=PROVIDER_ACCOUNT_ID,
                object_type="gmail_message",
                external_id="msg-body",
                thread_external_id="thr-body",
                calendar_id=None,
                ical_uid=None,
                status="active",
                source_timestamp=message_time,
                observed_at=now,
                provider_url="https://mail.google.com/mail/u/0/#inbox/msg-body",
                metadata_json={"history_id": "history-stale"},
                content_digest="r" * 64,
                created_at=now,
                updated_at=now,
            )
            db.add(provider_object)
            db.flush()
            db.add_all(
                [
                    ProviderEvidenceRecord(
                        id=new_id("pev"),
                        provider_object_id=provider_object.id,
                        provider="google",
                        provider_account_id=PROVIDER_ACCOUNT_ID,
                        source_kind="gmail_message",
                        external_id="msg-body",
                        thread_external_id="thr-body",
                        calendar_id=None,
                        source_uri=provider_object.provider_url,
                        source_timestamp=message_time,
                        content_digest="b" * 64,
                        metadata_json={"history_id": "history-stale"},
                        taint="provider_untrusted",
                        sensitivity="private",
                        retention_policy="provider_source",
                        extraction_status="failed",
                        lifecycle_state="superseded",
                        observed_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                    ProviderEvidenceRecord(
                        id=new_id("pev"),
                        provider_object_id=provider_object.id,
                        provider="google",
                        provider_account_id=PROVIDER_ACCOUNT_ID,
                        source_kind="gmail_message",
                        external_id="msg-body",
                        thread_external_id="thr-body",
                        calendar_id=None,
                        source_uri=provider_object.provider_url,
                        source_timestamp=message_time,
                        content_digest="c" * 64,
                        metadata_json={"history_id": "history-newer"},
                        taint="provider_untrusted",
                        sensitivity="private",
                        retention_policy="provider_source",
                        extraction_status="extracted",
                        lifecycle_state="available",
                        observed_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
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
            restored = db.scalar(
                select(ProviderEvidenceRecord).where(
                    ProviderEvidenceRecord.content_digest == "b" * 64
                )
            )
            superseded = db.scalar(
                select(ProviderEvidenceRecord).where(
                    ProviderEvidenceRecord.content_digest == "c" * 64
                )
            )
            block = db.scalar(select(ProviderEvidenceBlockRecord).limit(1))

    assert providers[0].read_calls == [
        {"message_id": "msg-body", "thread_id": None, "mode": "message"}
    ]
    assert restored is not None
    assert restored.lifecycle_state == "available"
    assert restored.extraction_status == "pending"
    assert restored.metadata_json == {
        "history_id": "history-body",
        "label_ids": ["INBOX"],
        "change": "messagesAdded",
        "decode_notes": [],
        "html_security": None,
    }
    assert block is not None
    assert block.block_kind == "body"
    assert superseded is not None
    assert superseded.lifecycle_state == "superseded"


def test_gmail_sync_preserves_extracted_same_digest_body_evidence(
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
    message_time = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
    expected_metadata = {
        "history_id": "history-body",
        "label_ids": ["INBOX"],
        "change": "messagesAdded",
        "decode_notes": [],
        "html_security": None,
    }
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
    with session_factory() as db:
        with db.begin():
            provider_object = GoogleProviderObjectRecord(
                id=new_id("gpo"),
                provider_account_id=PROVIDER_ACCOUNT_ID,
                object_type="gmail_message",
                external_id="msg-body",
                thread_external_id="thr-body",
                calendar_id=None,
                ical_uid=None,
                status="active",
                source_timestamp=message_time,
                observed_at=now,
                provider_url="https://mail.google.com/mail/u/0/#inbox/msg-body",
                metadata_json={"history_id": "history-stale"},
                content_digest="r" * 64,
                created_at=now,
                updated_at=now,
            )
            db.add(provider_object)
            db.flush()
            db.add(
                ProviderEvidenceRecord(
                    id=new_id("pev"),
                    provider_object_id=provider_object.id,
                    provider="google",
                    provider_account_id=PROVIDER_ACCOUNT_ID,
                    source_kind="gmail_message",
                    external_id="msg-body",
                    thread_external_id="thr-body",
                    calendar_id=None,
                    source_uri=provider_object.provider_url,
                    source_timestamp=message_time,
                    content_digest="b" * 64,
                    metadata_json=expected_metadata,
                    taint="provider_untrusted",
                    sensitivity="private",
                    retention_policy="provider_source",
                    extraction_status="extracted",
                    lifecycle_state="available",
                    observed_at=now,
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
            evidence = db.scalar(select(ProviderEvidenceRecord).limit(1))
            block = db.scalar(select(ProviderEvidenceBlockRecord).limit(1))

    assert providers[0].read_calls == [
        {"message_id": "msg-body", "thread_id": None, "mode": "message"}
    ]
    assert evidence is not None
    assert evidence.lifecycle_state == "available"
    assert evidence.extraction_status == "extracted"
    assert evidence.metadata_json == expected_metadata
    assert block is not None
    assert block.evidence_id == evidence.id


def test_gmail_sync_hydrates_label_changes_and_deletions_into_evidence_lifecycle(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[FakeGmailLifecycleProvider] = []

    class FakeGoogleConnectorRuntime:
        workspace_provider: FakeGmailLifecycleProvider

        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeGmailLifecycleProvider()
            providers.append(self.workspace_provider)

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
        cursor_value="hist-3",
        now=now,
    )
    with session_factory() as db:
        with db.begin():
            label_object = GoogleProviderObjectRecord(
                id=new_id("gpo"),
                provider_account_id=PROVIDER_ACCOUNT_ID,
                object_type="gmail_message",
                external_id="msg-label",
                thread_external_id="thr-label",
                calendar_id=None,
                ical_uid=None,
                status="active",
                source_timestamp=datetime(2026, 5, 7, 10, 0, tzinfo=UTC),
                observed_at=now,
                provider_url="https://mail.google.com/mail/u/0/#all/msg-label",
                metadata_json={
                    "history_id": "history-before",
                    "label_ids": ["INBOX", "IMPORTANT"],
                    "change": "messagesAdded",
                },
                content_digest="r" * 64,
                created_at=now,
                updated_at=now,
            )
            delete_object = GoogleProviderObjectRecord(
                id=new_id("gpo"),
                provider_account_id=PROVIDER_ACCOUNT_ID,
                object_type="gmail_message",
                external_id="msg-delete",
                thread_external_id="thr-delete",
                calendar_id=None,
                ical_uid=None,
                status="active",
                source_timestamp=datetime(2026, 5, 7, 10, 0, tzinfo=UTC),
                observed_at=now,
                provider_url="https://mail.google.com/mail/u/0/#all/msg-delete",
                metadata_json={
                    "history_id": "history-before",
                    "label_ids": ["INBOX"],
                    "change": "messagesAdded",
                },
                content_digest="s" * 64,
                created_at=now,
                updated_at=now,
            )
            db.add_all([label_object, delete_object])
            db.flush()
            db.add_all(
                [
                    ProviderEvidenceRecord(
                        id=new_id("pev"),
                        provider_object_id=label_object.id,
                        provider="google",
                        provider_account_id=PROVIDER_ACCOUNT_ID,
                        source_kind="gmail_message",
                        external_id="msg-label",
                        thread_external_id="thr-label",
                        calendar_id=None,
                        source_uri=label_object.provider_url,
                        source_timestamp=label_object.source_timestamp,
                        content_digest="b" * 64,
                        metadata_json={
                            "history_id": "history-before",
                            "label_ids": ["INBOX", "IMPORTANT"],
                            "change": "messagesAdded",
                        },
                        taint="provider_untrusted",
                        sensitivity="private",
                        retention_policy="provider_source",
                        extraction_status="extracted",
                        lifecycle_state="available",
                        observed_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                    ProviderEvidenceRecord(
                        id=new_id("pev"),
                        provider_object_id=delete_object.id,
                        provider="google",
                        provider_account_id=PROVIDER_ACCOUNT_ID,
                        source_kind="gmail_message",
                        external_id="msg-delete",
                        thread_external_id="thr-delete",
                        calendar_id=None,
                        source_uri=delete_object.provider_url,
                        source_timestamp=delete_object.source_timestamp,
                        content_digest="c" * 64,
                        metadata_json={
                            "history_id": "history-before",
                            "label_ids": ["INBOX"],
                            "change": "messagesAdded",
                        },
                        taint="provider_untrusted",
                        sensitivity="private",
                        retention_policy="provider_source",
                        extraction_status="extracted",
                        lifecycle_state="available",
                        observed_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
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
            updated_label_object = db.scalar(
                select(GoogleProviderObjectRecord).where(
                    GoogleProviderObjectRecord.external_id == "msg-label"
                )
            )
            updated_delete_object = db.scalar(
                select(GoogleProviderObjectRecord).where(
                    GoogleProviderObjectRecord.external_id == "msg-delete"
                )
            )
            label_evidence = db.scalar(
                select(ProviderEvidenceRecord).where(
                    ProviderEvidenceRecord.external_id == "msg-label"
                )
            )
            delete_evidence = db.scalar(
                select(ProviderEvidenceRecord).where(
                    ProviderEvidenceRecord.external_id == "msg-delete"
                )
            )
            tasks = db.scalars(select(BackgroundTaskRecord)).all()

    assert len(providers) == 1
    assert providers[0].read_calls == [
        {"message_id": "msg-label", "thread_id": None, "mode": "message"}
    ]
    assert run is not None
    assert run.status == "succeeded"
    assert run.item_count == 2
    assert run.observation_count == 1
    assert run.cursor_after == "hist-4"

    assert updated_label_object is not None
    assert updated_label_object.status == "active"
    assert updated_label_object.metadata_json["history_id"] == "history-lifecycle"
    assert updated_label_object.metadata_json["label_ids"] == ["INBOX"]
    assert updated_label_object.metadata_json["change"] == "labelsRemoved"
    assert label_evidence is not None
    assert label_evidence.lifecycle_state == "available"
    assert label_evidence.extraction_status == "pending"
    assert label_evidence.metadata_json["label_ids"] == ["INBOX"]
    assert label_evidence.metadata_json["change"] == "labelsRemoved"

    assert updated_delete_object is not None
    assert updated_delete_object.status == "deleted"
    assert delete_evidence is not None
    assert delete_evidence.lifecycle_state == "deleted"
    assert tasks == []


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


def test_calendar_sync_refreshes_same_digest_cancelled_evidence(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled_event = {
        "id": "evt-cancelled",
        "status": "cancelled",
        "summary": "Cancelled standup",
        "updated": "2026-05-22T10:00:00Z",
        "htmlLink": "https://calendar.google.com/event?eid=evt-cancelled",
    }
    content_digest = normalize_calendar_event(
        cancelled_event,
        provider_account_id=PROVIDER_ACCOUNT_ID,
        calendar_id="primary",
    ).raw_payload_digest

    class FakeCalendarProvider:
        def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
            return {"nextSyncToken": "sync-token-2", "items": [cancelled_event]}

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeCalendarProvider()

        def access_token_for_background_sync(self, **_: Any) -> str:
            return "access-token"

    monkeypatch.setattr("ariel.sync_runtime.GoogleConnectorRuntime", FakeGoogleConnectorRuntime)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    stale_time = datetime(2026, 5, 21, 9, 0, tzinfo=UTC)
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
    with session_factory() as db:
        with db.begin():
            provider_object = GoogleProviderObjectRecord(
                id=new_id("gpo"),
                provider_account_id=PROVIDER_ACCOUNT_ID,
                object_type="calendar_event",
                external_id="evt-cancelled",
                thread_external_id=None,
                calendar_id="primary",
                ical_uid=None,
                status="active",
                source_timestamp=stale_time,
                observed_at=stale_time,
                provider_url="https://calendar.google.com/event?eid=stale",
                metadata_json={"summary": "Stale", "status": "confirmed"},
                content_digest=content_digest,
                created_at=stale_time,
                updated_at=stale_time,
            )
            db.add(provider_object)
            db.flush()
            db.add(
                ProviderEvidenceRecord(
                    id=new_id("pev"),
                    provider_object_id=provider_object.id,
                    provider="google",
                    provider_account_id=PROVIDER_ACCOUNT_ID,
                    source_kind="calendar_event",
                    external_id="evt-cancelled",
                    thread_external_id="stale-thread",
                    calendar_id="stale-calendar",
                    source_uri="https://calendar.google.com/event?eid=stale",
                    source_timestamp=stale_time,
                    content_digest=content_digest,
                    metadata_json={"summary": "Stale", "status": "confirmed"},
                    taint="provider_untrusted",
                    sensitivity="private",
                    retention_policy="provider_source",
                    extraction_status="extracted",
                    lifecycle_state="available",
                    observed_at=stale_time,
                    created_at=stale_time,
                    updated_at=stale_time,
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
            evidence = db.scalar(select(ProviderEvidenceRecord).limit(1))

    assert evidence is not None
    assert evidence.lifecycle_state == "deleted"
    assert evidence.extraction_status == "not_actionable"
    assert evidence.thread_external_id is None
    assert evidence.calendar_id == "primary"
    assert evidence.source_uri == "https://calendar.google.com/event?eid=evt-cancelled"
    assert evidence.source_timestamp == datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
    assert evidence.metadata_json["summary"] == "Cancelled standup"
    assert evidence.metadata_json["status"] == "cancelled"
    assert evidence.observed_at == now
    assert evidence.updated_at == now


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


def test_gmail_sync_skips_invalid_typed_output_without_crashing_batch(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_id = "msg-bad"
    message_ids = ("msg-good-1", bad_id, "msg-good-2")

    class FakeMixedValidityGmailProvider:
        def email_list_history(self, **_: Any) -> dict[str, Any]:
            return {
                "historyId": "hist-2",
                "history": [
                    {
                        "id": "history-mixed",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": mid,
                                    "threadId": f"thr-{mid}",
                                    "labelIds": ["INBOX"],
                                }
                            }
                            for mid in message_ids
                        ],
                    }
                ],
            }

        def email_read(self, *, normalized_input: dict[str, Any], **_: Any) -> dict[str, Any]:
            message_id = normalized_input["message_id"]
            output = gmail_message_read_output(
                message_id=message_id,
                thread_id=f"thr-{message_id}",
                published_at="2026-05-07T12:00:00Z",
            )
            if message_id == bad_id:
                # Validator rejects: status="no_body" forbids a non-None body_digest.
                output["read_outcome"] = {
                    "status": "no_body",
                    "reason_code": "gmail_message_unavailable",
                    "recovery": None,
                }
            return output

    class FakeGoogleConnectorRuntime:
        def __init__(self, **_: Any) -> None:
            self.workspace_provider = FakeMixedValidityGmailProvider()

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

    process_provider_sync_due(
        session_factory=session_factory,
        task_payload={"provider": "google", "resource_type": "gmail", "resource_id": "primary"},
        settings=_settings(),
        now_fn=lambda: now,
        new_id_fn=new_id,
    )

    with session_factory() as db:
        run = db.scalar(select(SyncRunRecord).limit(1))
        tasks = db.scalars(select(BackgroundTaskRecord)).all()

    assert run is not None and run.status == "succeeded" and run.error is None
    assert [task.task_type for task in tasks] == ["agent_wake"]
    items = tasks[0].payload["items"]
    assert {item["message_id"] for item in items} == {"msg-good-1", "msg-good-2"}
    for item in items:
        assert item["preview_kind"] == "provider_sync_preview"
        assert item["requires_read_for_body_claims"] is True
        assert item["provider_evidence_refs"][0]["provider_evidence_id"].startswith("pev_")
        assert item["evidence_blocks"][0]["text"] == "Thanks, I will follow up by Friday."
