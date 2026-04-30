from __future__ import annotations

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
        }
    )
    assert normalized_slots is None
    assert slots_error == "schema_invalid"


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
    assert output["results"][0]["title"] == "team sync"


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
                "snippet": "payment confirmed",
                "internalDate": "1709462400000",
                "payload": {"headers": [{"name": "Subject", "value": "Invoice #44"}]},
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=2)
    output = provider.email_read(
        access_token="tok_live",
        normalized_input={"message_id": "msg_1"},
    )

    assert len(calls) == 2
    assert output["results"][0]["title"] == "Invoice #44"
    assert "payment confirmed" in output["results"][0]["snippet"]


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
