from __future__ import annotations

import base64
import os
from typing import Any

import httpx
import pytest

from ariel.capability_registry import get_capability
from ariel.google_connector import (
    ConnectorTokenCipher,
    DefaultGoogleWorkspaceProvider,
)


def _response(*, status_code: int, payload: dict[str, Any]) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test")
    return httpx.Response(status_code=status_code, json=payload, request=request)


def _text_response(*, status_code: int, text: str, url: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(status_code=status_code, text=text, request=request)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def test_connector_token_cipher_round_trip_uses_aead_envelope_format() -> None:
    cipher = ConnectorTokenCipher(
        active_key_version="v2",
        keys_by_version={
            "v1": os.urandom(32),
            "v2": os.urandom(32),
        },
    )
    plaintext = "tok_live_secret"

    encrypted = cipher.encrypt(plaintext)
    assert encrypted.startswith("aeadv1:v2:")
    assert plaintext not in encrypted
    assert cipher.decrypt(encrypted) == plaintext


def test_connector_token_cipher_supports_key_rotation_decrypt_of_older_versions() -> None:
    key_v1 = os.urandom(32)
    key_v2 = os.urandom(32)
    old_cipher = ConnectorTokenCipher(
        active_key_version="v1",
        keys_by_version={"v1": key_v1},
    )
    ciphertext = old_cipher.encrypt("tok_before_rotation")

    rotated_cipher = ConnectorTokenCipher(
        active_key_version="v2",
        keys_by_version={"v1": key_v1, "v2": key_v2},
    )
    assert rotated_cipher.decrypt(ciphertext) == "tok_before_rotation"


def test_connector_token_cipher_allows_single_secret_version_relabel_compatibility() -> None:
    v1_cipher = ConnectorTokenCipher.from_config(
        active_key_version="v1",
        configured_keys=None,
        fallback_secret="shared-dev-secret",
    )
    ciphertext = v1_cipher.encrypt("tok_single_secret")

    v2_cipher = ConnectorTokenCipher.from_config(
        active_key_version="v2",
        configured_keys=None,
        fallback_secret="shared-dev-secret",
    )
    assert v2_cipher.decrypt(ciphertext) == "tok_single_secret"


def test_google_calendar_capability_validators_reject_inverted_windows() -> None:
    calendar_list = get_capability("cap.calendar.list")
    assert calendar_list is not None
    normalized, error = calendar_list.validate_input(
        {
            "window_start": "2026-03-05T10:00:00Z",
            "window_end": "2026-03-05T09:00:00Z",
        }
    )
    assert normalized is None
    assert error == "schema_invalid"

    calendar_slots = get_capability("cap.calendar.propose_slots")
    assert calendar_slots is not None
    normalized_slots, slots_error = calendar_slots.validate_input(
        {
            "window_start": "2026-03-05T10:00:00Z",
            "window_end": "2026-03-05T09:00:00Z",
            "duration_minutes": 30,
            "attendees": [],
            "timezone": "UTC",
            "source_evidence_ids": [],
            "quoted_content_caveat": False,
            "participants": [],
            "proposed_windows": [],
            "timezone_evidence": {
                "source": None,
                "rationale": None,
                "confidence": None,
            },
            "constraints": {"hard": [], "soft": [], "attendee_notes": []},
        }
    )
    assert normalized_slots is None
    assert slots_error == "schema_invalid"

    calendar_create = get_capability("cap.calendar.create_event")
    assert calendar_create is not None
    normalized_create, create_error = calendar_create.validate_input(
        {
            "title": "Risk review",
            "start_time": "2026-03-05T10:00:00Z",
            "end_time": "2026-03-05T10:30:00Z",
            "idempotency_key": "cal-create-1",
            "user_instruction_ref": "turn:create-risk-review",
        }
    )
    assert create_error is None
    assert normalized_create is not None
    assert normalized_create["idempotency_key"] == "cal-create-1"

    calendar_update = get_capability("cap.calendar.update_event")
    assert calendar_update is not None
    assert calendar_update.validate_input({"event_id": "evt_1", "title": "Risk review"}) == (
        None,
        "schema_invalid",
    )
    normalized_update, update_error = calendar_update.validate_input(
        {
            "event_id": "evt_1",
            "title": "Risk review",
            "idempotency_key": "cal-update-1",
            "source_evidence_id": "pev_1",
        }
    )
    assert update_error is None
    assert normalized_update is not None
    assert normalized_update["source_evidence_id"] == "pev_1"
    normalized_update_with_null_attendees, update_null_attendees_error = (
        calendar_update.validate_input(
            {
                "event_id": "evt_1",
                "title": "Risk review",
                "attendees": None,
                "idempotency_key": "cal-update-2",
                "source_evidence_id": "pev_1",
            }
        )
    )
    assert update_null_attendees_error is None
    assert normalized_update_with_null_attendees is not None
    assert "attendees" not in normalized_update_with_null_attendees

    calendar_response = get_capability("cap.calendar.respond_to_event")
    assert calendar_response is not None
    normalized_response, response_error = calendar_response.validate_input(
        {
            "event_id": "evt_1",
            "attendee_email": "User@Example.com",
            "response_status": "accepted",
            "idempotency_key": "cal-rsvp-1",
            "source_evidence_id": "pev_1",
        }
    )
    assert response_error is None
    assert normalized_response is not None
    assert normalized_response["attendee_email"] == "user@example.com"


def test_google_email_read_validator_accepts_message_or_thread_modes() -> None:
    email_read = get_capability("cap.email.read")
    assert email_read is not None

    normalized_message, message_error = email_read.validate_input(
        {"message_id": " msg_1 ", "thread_id": None, "mode": "message"}
    )
    assert message_error is None
    assert normalized_message == {
        "message_id": "msg_1",
        "thread_id": None,
        "mode": "message",
    }

    normalized_thread, thread_error = email_read.validate_input(
        {"message_id": None, "thread_id": " thr_1 ", "mode": "thread"}
    )
    assert thread_error is None
    assert normalized_thread == {
        "message_id": None,
        "thread_id": "thr_1",
        "mode": "thread",
    }

    assert email_read.validate_input({"thread_id": "thr_1", "mode": "message"}) == (
        None,
        "schema_invalid",
    )


def test_default_workspace_provider_calendar_list_calls_google_events_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del json
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "params": params or {},
                "timeout": timeout,
            }
        )
        return _response(
            status_code=200,
            payload={
                "items": [
                    {
                        "id": "evt_1",
                        "summary": "team sync",
                        "htmlLink": "https://calendar.google.com/event?eid=evt_1",
                        "start": {"dateTime": "2026-03-04T10:00:00Z"},
                        "end": {"dateTime": "2026-03-04T10:30:00Z"},
                        "updated": "2026-03-03T09:00:00Z",
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=2)
    output = provider.calendar_list(
        access_token="tok_live",
        normalized_input={
            "window_start": "2026-03-04T00:00:00Z",
            "window_end": "2026-03-05T00:00:00Z",
        },
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/calendar/v3/calendars/primary/events")
    assert "authorization" in call["headers"]
    assert call["headers"]["authorization"].startswith("Bearer ")
    assert call["params"]["timeMin"] == "2026-03-04T00:00:00Z"
    assert call["params"]["timeMax"] == "2026-03-05T00:00:00Z"
    assert output["schema_version"] == "google.calendar.events.v1"
    assert output["events"][0]["summary"] == "team sync"
    assert output["events"][0]["event_id"] == "evt_1"
    assert len(output["events"][0]["raw_payload_digest"]) == 64
    assert output["events"][0]["start"]["value"] == "2026-03-04T10:00:00Z"


def test_default_workspace_provider_calendar_slots_do_not_overstate_freebusy_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del method, headers, params, json, timeout
        calls.append(url)
        if url.endswith("/calendar/v3/freeBusy"):
            return _response(
                status_code=200,
                payload={
                    "calendars": {
                        "primary": {"busy": []},
                        "lead@example.com": {
                            "errors": [{"reason": "notFound"}],
                            "busy": [],
                        },
                    }
                },
            )
        return _response(status_code=200, payload={"items": []})

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    output = provider.calendar_propose_slots(
        access_token="tok_live",
        normalized_input={
            "window_start": "2026-03-04T10:00:00Z",
            "window_end": "2026-03-04T11:00:00Z",
            "duration_minutes": 30,
            "attendees": ["lead@example.com"],
            "timezone": "UTC",
            "source_evidence_ids": [],
            "quoted_content_caveat": False,
            "participants": ["lead@example.com"],
            "proposed_windows": [],
            "timezone_evidence": {
                "source": None,
                "rationale": None,
                "confidence": None,
            },
            "constraints": {"hard": [], "soft": [], "attendee_notes": []},
        },
        attendee_intersection_enabled=True,
    )

    assert calls[0].endswith("/calendar/v3/freeBusy")
    assert calls[1].endswith("/calendar/v3/calendars/primary/events")
    assert output["availability_scope"] == "primary_calendar_only"
    assert output["partial"] is True
    assert output["partial_reason"] == "attendee_freebusy_unavailable"
    assert output["slots"][0]["availability_scope"] == "primary_calendar_only"


def test_default_workspace_provider_calendar_update_and_rsvp_patch_google_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del headers, params, timeout
        calls.append({"method": method, "url": url, "json": json or {}})
        return _response(
            status_code=200,
            payload={
                "id": "evt_1",
                "etag": "etag_evt_1",
                "updated": "2026-03-03T12:00:00Z",
                "iCalUID": "evt_1@google.com",
                "status": "confirmed",
                "htmlLink": "https://calendar.google.com/event?eid=evt_1",
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    updated = provider.calendar_update_event(
        access_token="tok_live",
        normalized_input={
            "calendar_id": "primary",
            "event_id": "evt_1",
            "title": "Risk review",
            "start_time": "2026-03-04T10:00:00Z",
            "end_time": "2026-03-04T10:30:00Z",
            "idempotency_key": "cal-update-1",
            "source_evidence_id": "pev_1",
        },
    )
    responded = provider.calendar_respond_to_event(
        access_token="tok_live",
        normalized_input={
            "calendar_id": "primary",
            "event_id": "evt_1",
            "attendee_email": "user@example.com",
            "response_status": "accepted",
            "idempotency_key": "cal-rsvp-1",
            "source_evidence_id": "pev_1",
        },
    )

    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"].endswith("/calendar/v3/calendars/primary/events/evt_1")
    assert calls[0]["json"]["summary"] == "Risk review"
    assert calls[0]["json"]["start"] == {"dateTime": "2026-03-04T10:00:00Z"}
    assert calls[1]["json"] == {
        "attendeesOmitted": True,
        "attendees": [{"email": "user@example.com", "responseStatus": "accepted"}],
    }
    assert updated["schema_version"] == "google.calendar.update_result.v1"
    assert updated["etag"] == "etag_evt_1"
    assert responded["schema_version"] == "google.calendar.response_result.v1"
    assert responded["response_status"] == "accepted"


def test_default_workspace_provider_retries_transient_errors_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del method, url, headers, params, timeout
        calls.append(1)
        if len(calls) == 1:
            return _response(
                status_code=503, payload={"error": {"message": "temporarily unavailable"}}
            )
        return _response(
            status_code=200,
            payload={
                "id": "msg_1",
                "threadId": "thr_1",
                "internalDate": "1709462400000",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "Invoice #44"}],
                    "body": {"data": _b64url(b"payment confirmed")},
                },
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=2)
    output = provider.email_read(
        access_token="tok_live",
        normalized_input={"message_id": "msg_1"},
    )

    assert len(calls) == 2
    assert output["schema_version"] == "google.gmail.message_evidence.v1"
    assert output["message"]["subject"] == "Invoice #44"
    assert "text" not in output["message"]["body"]
    assert "html_text" not in output["message"]["body"]
    assert output["evidence"]["blocks"][0]["text"] == "payment confirmed"
    assert output["read_outcome"]["status"] == "ok"


def test_default_workspace_provider_email_search_fetches_to_cc_and_body_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del headers, json, timeout
        calls.append({"method": method, "url": url, "params": params or {}})
        if url.endswith("/users/me/messages"):
            return _response(status_code=200, payload={"messages": [{"id": "msg_1"}]})
        return _response(
            status_code=200,
            payload={
                "id": "msg_1",
                "threadId": "thr_1",
                "snippet": "short preview",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Invoice"},
                        {"name": "From", "value": "Ada <ada@example.com>"},
                        {"name": "To", "value": "User <user@example.com>"},
                        {"name": "Cc", "value": "Ops <ops@example.com>"},
                    ]
                },
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    output = provider.email_search(
        access_token="tok_live",
        normalized_input={"query": "invoice"},
    )

    assert calls[1]["params"]["metadataHeaders"] == ["Subject", "From", "To", "Cc", "Date"]
    message = output["messages"][0]
    assert message["recipients"][0]["email"] == "user@example.com"
    assert message["cc"][0]["email"] == "ops@example.com"
    assert message["evidence_status"] == "needs_read"


def test_default_workspace_provider_email_read_supports_thread_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del headers, json, timeout
        calls.append({"method": method, "url": url, "params": params or {}})
        if url.endswith("/gmail/v1/users/me/threads/thr_1"):
            return _response(
                status_code=200,
                payload={
                    "id": "thr_1",
                    "historyId": "88",
                    "messages": [{"id": "msg_1"}, {"id": "msg_2"}],
                },
            )
        message_id = url.rsplit("/", 1)[-1]
        body = b"first message" if message_id == "msg_1" else b"second message"
        return _response(
            status_code=200,
            payload={
                "id": message_id,
                "threadId": "thr_1",
                "internalDate": "1709462400000" if message_id == "msg_1" else "1709462500000",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": "Invoice"},
                        {"name": "To", "value": "user@example.com"},
                        {"name": "Cc", "value": "ops@example.com"},
                    ],
                    "body": {"data": _b64url(body)},
                },
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    output = provider.email_read(
        access_token="tok_live",
        normalized_input={"thread_id": "thr_1", "message_id": None, "mode": "thread"},
    )

    assert calls[0]["url"].endswith("/gmail/v1/users/me/threads/thr_1")
    assert calls[0]["params"]["format"] == "metadata"
    assert calls[1]["url"].endswith("/gmail/v1/users/me/messages/msg_1")
    assert calls[2]["url"].endswith("/gmail/v1/users/me/messages/msg_2")
    assert output["mode"] == "thread"
    assert output["thread"]["thread_id"] == "thr_1"
    assert [message["message_id"] for message in output["messages"]] == ["msg_1", "msg_2"]
    assert output["messages"][0]["cc"][0]["email"] == "ops@example.com"
    assert output["evidence"]["source_kind"] == "gmail_thread"
    assert [block["source_message_id"] for block in output["evidence"]["blocks"]] == [
        "msg_1",
        "msg_2",
    ]


def test_default_workspace_provider_drive_search_builds_safe_query_and_shared_drive_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del json
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "params": params or {},
                "timeout": timeout,
            }
        )
        return _response(status_code=200, payload={"files": []})

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    raw_query = r"ops\q4's plan"
    output = provider.drive_search(
        access_token="tok_live",
        normalized_input={"query": raw_query},
    )

    assert len(calls) == 1
    call = calls[0]
    escaped = raw_query.replace("\\", "\\\\").replace("'", "\\'")
    assert call["method"] == "GET"
    assert call["url"].endswith("/drive/v3/files")
    assert call["params"]["q"] == (
        f"(name contains '{escaped}' or fullText contains '{escaped}') and trashed = false"
    )
    assert call["params"]["supportsAllDrives"] == "true"
    assert call["params"]["includeItemsFromAllDrives"] == "true"
    assert output["query"] == raw_query
    assert output["results"] == []


@pytest.mark.parametrize(
    ("size_value", "expected_status", "expected_calls"),
    [
        (131072, "ok", 2),
        (131073, "too_large", 1),
    ],
)
def test_default_workspace_provider_drive_read_enforces_size_boundary(
    monkeypatch: pytest.MonkeyPatch,
    size_value: int,
    expected_status: str,
    expected_calls: int,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del json, headers, timeout
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params or {},
            }
        )
        if len(calls) == 1:
            return _response(
                status_code=200,
                payload={
                    "id": "drv_boundary",
                    "name": "Boundary Doc",
                    "mimeType": "text/plain",
                    "modifiedTime": "2026-03-06T12:00:00Z",
                    "webViewLink": "https://drive.google.com/file/d/drv_boundary/view",
                    "size": str(size_value),
                    "owners": [],
                },
            )
        return _text_response(status_code=200, text="boundary text", url=url)

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    output = provider.drive_read(
        access_token="tok_live",
        normalized_input={"file_id": "drv_boundary"},
    )

    assert output["read_outcome"]["status"] == expected_status
    assert len(calls) == expected_calls
    assert calls[0]["params"]["supportsAllDrives"] == "true"
    if expected_status == "ok":
        assert output["content_excerpt"] == "boundary text"
        assert output["truncated"] is False
        assert calls[1]["params"]["alt"] == "media"
        assert calls[1]["params"]["supportsAllDrives"] == "true"
    else:
        assert output["read_outcome"]["reason_code"] == "drive_read_too_large"
        assert output["truncated"] is False


@pytest.mark.parametrize(
    ("error_payload", "expected_error"),
    [
        (
            {
                "error": {
                    "message": "Request had insufficient authentication scopes.",
                    "errors": [{"reason": "insufficientPermissions"}],
                }
            },
            "insufficient_permissions",
        ),
        (
            {
                "error": {
                    "message": "The user does not have sufficient permissions for this file.",
                    "errors": [{"reason": "insufficientFilePermissions"}],
                }
            },
            "google_forbidden",
        ),
    ],
)
def test_default_workspace_provider_distinguishes_scope_vs_acl_forbidden(
    monkeypatch: pytest.MonkeyPatch,
    error_payload: dict[str, Any],
    expected_error: str,
) -> None:
    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del method, headers, params, json, timeout
        request = httpx.Request("GET", url)
        return httpx.Response(status_code=403, json=error_payload, request=request)

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    with pytest.raises(RuntimeError, match=expected_error):
        provider.drive_read(
            access_token="tok_live",
            normalized_input={"file_id": "drv_acl_scope"},
        )


class _FakeGmailMailbox:
    def __init__(
        self,
        *,
        labels_by_message_id: dict[str, list[str]],
        fail_trash_message_id: str | None = None,
    ) -> None:
        self.labels_by_message_id = {
            message_id: list(label_ids) for message_id, label_ids in labels_by_message_id.items()
        }
        self.fail_trash_message_id = fail_trash_message_id
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del headers, timeout
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params or {},
                "json": json or {},
            }
        )
        request = httpx.Request(method, url)
        if method == "GET" and url.endswith("/labels"):
            return httpx.Response(
                200,
                json={
                    "labels": [
                        {"id": "Label_Client", "name": "Client"},
                        {"id": "Label_Old", "name": "Old"},
                    ]
                },
                request=request,
            )
        if method == "GET" and "/messages/" in url:
            message_id = url.rsplit("/messages/", 1)[1]
            return self._message_response(message_id=message_id, request=request)
        if method == "POST" and url.endswith("/batchModify"):
            payload = json or {}
            for message_id in payload.get("ids", []):
                self._apply_labels(
                    message_id=str(message_id),
                    add_label_ids=payload.get("addLabelIds", []),
                    remove_label_ids=payload.get("removeLabelIds", []),
                )
            return httpx.Response(204, request=request)
        if method == "POST" and url.endswith("/modify"):
            message_id = url.rsplit("/messages/", 1)[1].split("/", 1)[0]
            payload = json or {}
            self._apply_labels(
                message_id=message_id,
                add_label_ids=payload.get("addLabelIds", []),
                remove_label_ids=payload.get("removeLabelIds", []),
            )
            return self._message_response(message_id=message_id, request=request)
        if method == "POST" and url.endswith("/trash"):
            message_id = url.rsplit("/messages/", 1)[1].split("/", 1)[0]
            if message_id == self.fail_trash_message_id:
                return httpx.Response(
                    500,
                    json={"error": {"message": "trash failed"}},
                    request=request,
                )
            self._apply_labels(
                message_id=message_id,
                add_label_ids=["TRASH"],
                remove_label_ids=["INBOX"],
            )
            return self._message_response(message_id=message_id, request=request)
        if method == "POST" and url.endswith("/untrash"):
            message_id = url.rsplit("/messages/", 1)[1].split("/", 1)[0]
            self._apply_labels(
                message_id=message_id,
                add_label_ids=[],
                remove_label_ids=["TRASH"],
            )
            return self._message_response(message_id=message_id, request=request)
        return httpx.Response(404, json={"error": {"message": "not found"}}, request=request)

    def _message_response(self, *, message_id: str, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": message_id,
                "threadId": f"thr_{message_id}",
                "labelIds": self.labels_by_message_id.get(message_id, []),
            },
            request=request,
        )

    def _apply_labels(
        self,
        *,
        message_id: str,
        add_label_ids: list[str],
        remove_label_ids: list[str],
    ) -> None:
        label_ids = set(self.labels_by_message_id.get(message_id, []))
        label_ids.update(add_label_ids)
        label_ids.difference_update(remove_label_ids)
        self.labels_by_message_id[message_id] = sorted(label_ids)


def test_default_workspace_provider_email_archive_preserves_before_and_after_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(
        labels_by_message_id={
            "msg_inbox": ["INBOX", "Label_Client"],
            "msg_done": ["Label_Client"],
        }
    )
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_archive(
        access_token="tok_live",
        normalized_input={"message_ids": ["msg_inbox", "msg_done"]},
    )

    assert output["status"] == "archived"
    assert output["before_labels"]["msg_inbox"] == ["INBOX", "Label_Client"]
    assert output["after_labels"]["msg_inbox"] == ["Label_Client"]
    assert output["provider_result"]["mutated_message_ids"] == ["msg_inbox"]
    assert output["provider_result"]["noop_message_ids"] == ["msg_done"]


def test_default_workspace_provider_email_archive_uses_supplied_before_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(labels_by_message_id={"msg_1": ["INBOX"]})
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_archive(
        access_token="tok_live",
        normalized_input={
            "message_ids": ["msg_1"],
            "before_state": [
                {
                    "message_id": "msg_1",
                    "thread_id": "thr_original",
                    "label_ids": ["INBOX", "Label_Client"],
                }
            ],
        },
    )

    message_get_calls = [
        call for call in mailbox.calls if call["method"] == "GET" and "/messages/" in call["url"]
    ]
    assert len(message_get_calls) == 1
    assert output["before_labels"]["msg_1"] == ["INBOX", "Label_Client"]
    assert output["after_labels"]["msg_1"] == []


def test_default_workspace_provider_email_trash_uses_trash_not_permanent_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(labels_by_message_id={"msg_1": ["INBOX"]})
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_trash(
        access_token="tok_live",
        normalized_input={"message_ids": ["msg_1"]},
    )

    assert output["status"] == "trashed"
    assert output["after_labels"]["msg_1"] == ["TRASH"]
    assert any(call["url"].endswith("/trash") for call in mailbox.calls)
    assert all(call["method"] != "DELETE" for call in mailbox.calls)
    assert all("/delete" not in call["url"] for call in mailbox.calls)


def test_default_workspace_provider_email_trash_partial_failure_keeps_audit_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(
        labels_by_message_id={
            "msg_ok": ["INBOX"],
            "msg_fail": ["INBOX"],
        },
        fail_trash_message_id="msg_fail",
    )
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_trash(
        access_token="tok_live",
        normalized_input={"message_ids": ["msg_ok", "msg_fail"]},
    )

    assert output["status"] == "partially_failed"
    assert output["before_labels"]["msg_ok"] == ["INBOX"]
    assert output["before_labels"]["msg_fail"] == ["INBOX"]
    assert output["after_labels"]["msg_ok"] == ["TRASH"]
    assert output["after_labels"]["msg_fail"] == ["INBOX"]
    assert output["provider_result"]["mutated_message_ids"] == ["msg_ok"]
    assert output["provider_result"]["failed_provider_call"]["message_id"] == "msg_fail"
    assert output["provider_result"]["error"] == "google_upstream_500"


def test_default_workspace_provider_email_labels_modify_reuses_provider_label_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(labels_by_message_id={"msg_1": ["INBOX", "Label_Old"]})
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_modify_labels(
        access_token="tok_live",
        normalized_input={
            "message_ids": ["msg_1"],
            "add_labels": ["Client"],
            "remove_labels": ["Old"],
            "provider_label_ids": {
                "add": ["Label_Client"],
                "remove": ["Label_Old"],
            },
        },
    )

    assert output["status"] == "labels_modified"
    assert output["after_labels"]["msg_1"] == ["INBOX", "Label_Client"]
    assert output["provider_label_ids"] == {
        "add": ["Label_Client"],
        "remove": ["Label_Old"],
    }
    assert not any(call["url"].endswith("/labels") for call in mailbox.calls)


def test_default_workspace_provider_email_labels_modify_rejects_immutable_system_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(labels_by_message_id={"msg_1": ["INBOX"]})
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    with pytest.raises(RuntimeError, match="system_label_requires_dedicated_capability"):
        provider.email_modify_labels(
            access_token="tok_live",
            normalized_input={
                "message_ids": ["msg_1"],
                "add_labels": ["SENT"],
                "remove_labels": [],
            },
        )

    assert not any(call["url"].endswith("/modify") for call in mailbox.calls)


def test_default_workspace_provider_email_labels_modify_allows_modifiable_system_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(labels_by_message_id={"msg_1": ["INBOX"]})
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_modify_labels(
        access_token="tok_live",
        normalized_input={
            "message_ids": ["msg_1"],
            "add_labels": ["STARRED"],
            "remove_labels": [],
        },
    )

    assert output["status"] == "labels_modified"
    assert output["after_labels"]["msg_1"] == ["INBOX", "STARRED"]


def test_default_workspace_provider_email_undo_skips_immutable_system_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = _FakeGmailMailbox(labels_by_message_id={"msg_1": ["INBOX", "Label_Old"]})
    monkeypatch.setattr(httpx, "request", mailbox.request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    output = provider.email_undo(
        access_token="tok_live",
        normalized_input={
            "message_ids": ["msg_1"],
            "before_state": [
                {
                    "message_id": "msg_1",
                    "thread_id": "thr_msg_1",
                    "label_ids": ["DRAFT", "INBOX", "Label_Client", "SENT"],
                }
            ],
        },
    )

    modify_calls = [call for call in mailbox.calls if call["url"].endswith("/modify")]
    assert output["status"] == "partially_failed"
    assert len(modify_calls) == 1
    assert modify_calls[0]["json"] == {
        "addLabelIds": ["Label_Client"],
        "removeLabelIds": ["Label_Old"],
    }
    assert output["provider_result"]["skipped_immutable_label_ids"] == {"msg_1": ["DRAFT", "SENT"]}
    assert output["provider_result"]["restored_message_ids"] == []
    assert output["provider_result"]["error"] == "immutable_label_restore_unsupported"
