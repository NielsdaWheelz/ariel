from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ariel.google_connector import DefaultGoogleWorkspaceProvider


def _json_response(*, status_code: int, payload: dict[str, Any], url: str) -> httpx.Response:
    request = httpx.Request("POST", url)
    return httpx.Response(status_code=status_code, json=payload, request=request)


def test_gmail_register_watch_posts_topic_and_parses_epoch_millis_expiration(
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
        return _json_response(
            status_code=200,
            payload={"historyId": "987654", "expiration": "1778256000000"},
            url=url,
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    result = provider.gmail_register_watch(
        access_token="tok_live",
        topic_name="projects/ariel/topics/gmail-watch",
        label_ids=["INBOX"],
    )

    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/gmail/v1/users/me/watch")
    assert calls[0]["json"] == {
        "topicName": "projects/ariel/topics/gmail-watch",
        "labelIds": ["INBOX"],
    }
    assert result["historyId"] == "987654"
    assert result["expiration"] == datetime(2026, 5, 8, 16, 0, tzinfo=UTC)


def test_gmail_stop_watch_posts_stop_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
        del headers, params, json, timeout
        calls.append(f"{method} {url}")
        request = httpx.Request(method, url)
        return httpx.Response(204, request=request)

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    provider.gmail_stop_watch(access_token="tok_live")

    assert len(calls) == 1
    assert calls[0].endswith("/gmail/v1/users/me/stop")
    assert calls[0].startswith("POST ")


def test_calendar_register_watch_posts_web_hook_channel_with_ttl(
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
        return _json_response(
            status_code=200,
            payload={"resourceId": "res_abc", "expiration": "1778256000000"},
            url=url,
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    result = provider.calendar_register_watch(
        access_token="tok_live",
        calendar_id="primary",
        channel_id="wch_001",
        channel_token="tok_channel",
        address="https://ariel.example/v1/providers/google/events",
        ttl_seconds=518400,
    )

    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/calendar/v3/calendars/primary/events/watch")
    assert calls[0]["json"] == {
        "id": "wch_001",
        "type": "web_hook",
        "address": "https://ariel.example/v1/providers/google/events",
        "token": "tok_channel",
        "params": {"ttl": "518400"},
    }
    assert result["resourceId"] == "res_abc"
    assert result["expiration"] == datetime(2026, 5, 8, 16, 0, tzinfo=UTC)


def test_calendar_stop_watch_posts_channel_id_and_resource_id(
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
        request = httpx.Request(method, url)
        return httpx.Response(204, request=request)

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)
    provider.calendar_stop_watch(
        access_token="tok_live",
        channel_id="wch_001",
        provider_resource_id="res_abc",
    )

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/calendar/v3/channels/stop")
    assert calls[0]["json"] == {"id": "wch_001", "resourceId": "res_abc"}


def test_request_json_maps_410_gone_to_sync_token_invalid(
    monkeypatch: pytest.MonkeyPatch,
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
        return httpx.Response(
            410, json={"error": {"message": "sync token expired"}}, request=request
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    provider = DefaultGoogleWorkspaceProvider(timeout_seconds=5.0, max_attempts=1)

    with pytest.raises(RuntimeError, match="sync_token_invalid"):
        provider.calendar_list_event_deltas(
            access_token="tok_live",
            calendar_id="primary",
            sync_token="stale-token",
        )
