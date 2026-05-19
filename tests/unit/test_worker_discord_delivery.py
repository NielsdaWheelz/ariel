from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from ariel.app import TurnExecutionOutcome
from ariel.config import AppSettings
from ariel.worker import _deliver_to_discord


def _settings(**overrides: Any) -> AppSettings:
    base: dict[str, Any] = {
        "_env_file": None,
        "discord_bot_token": "test-bot-token",
        "discord_channel_id": 123456789,
    }
    base.update(overrides)
    return AppSettings(**base)  # type: ignore[arg-type]


def _outcome(
    *,
    status_code: int = 200,
    message: str = "Hello from Ariel",
    silent: bool = False,
) -> TurnExecutionOutcome:
    return TurnExecutionOutcome(
        turn_id="trn_test",
        effective_session_id="ses_test",
        status_code=status_code,
        response_payload={
            "ok": True,
            "assistant": {
                "message": message,
                "sources": [],
                "silent": silent,
            },
        },
    )


def test_deliver_to_discord_posts_on_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful turn with a non-silent message POSTs to the Discord REST API."""
    posts: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    settings = _settings()
    _deliver_to_discord(outcome=_outcome(), settings=settings)

    assert len(posts) == 1
    post = posts[0]
    assert post["url"] == "https://discord.com/api/v10/channels/123456789/messages"
    assert post["headers"] == {"Authorization": "Bot test-bot-token"}
    assert post["json"] == {"content": "Hello from Ariel"}
    assert post["timeout"] == settings.discord_notification_timeout_seconds


def test_deliver_to_discord_silent_turn_does_not_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn where assistant.silent is True must not POST to Discord."""
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        posts.append({"url": url})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    _deliver_to_discord(outcome=_outcome(silent=True), settings=_settings())

    assert posts == []


def test_deliver_to_discord_unconfigured_token_does_not_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Discord token means no POST attempt."""
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        posts.append({"url": url})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    _deliver_to_discord(outcome=_outcome(), settings=_settings(discord_bot_token=None))

    assert posts == []


def test_deliver_to_discord_unconfigured_channel_does_not_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Discord channel_id means no POST attempt."""
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        posts.append({"url": url})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    _deliver_to_discord(outcome=_outcome(), settings=_settings(discord_channel_id=None))

    assert posts == []


def test_deliver_to_discord_non_200_outcome_does_not_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed turn (status_code != 200) must not POST to Discord."""
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        posts.append({"url": url})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    _deliver_to_discord(
        outcome=TurnExecutionOutcome(
            turn_id="trn_test",
            effective_session_id="ses_test",
            status_code=503,
            response_payload={"ok": False, "error": {"message": "model unavailable"}},
        ),
        settings=_settings(),
    )

    assert posts == []


def test_deliver_to_discord_timeout_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TimeoutException from httpx must be swallowed — the turn already committed."""

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.TimeoutException("timeout", request=None)  # type: ignore[arg-type]

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    # Must not raise
    _deliver_to_discord(outcome=_outcome(), settings=_settings())


def test_deliver_to_discord_http_error_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTPError from httpx must be swallowed — the turn already committed."""

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.HTTPError("connection failed")

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    # Must not raise
    _deliver_to_discord(outcome=_outcome(), settings=_settings())


def test_deliver_to_discord_http_4xx_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx response from Discord must be silently swallowed."""

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        response = MagicMock()
        response.status_code = 401
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    # Must not raise
    _deliver_to_discord(outcome=_outcome(), settings=_settings())


def test_deliver_to_discord_truncates_long_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message longer than 1900 characters is truncated with a [truncated] suffix."""
    posted_content: list[str] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posted_content.append(json["content"])
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    long_message = "x" * 2500
    _deliver_to_discord(outcome=_outcome(message=long_message), settings=_settings())

    assert len(posted_content) == 1
    content = posted_content[0]
    assert content.endswith("\n[truncated]")
    assert len(content) <= 1900
