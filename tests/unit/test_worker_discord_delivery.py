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


def _outcome_with_approvals(
    *,
    message: str = "Hello from Ariel",
    approvals: list[dict[str, Any]],
) -> TurnExecutionOutcome:
    """Build a TurnExecutionOutcome whose turn carries pending approval items.

    Each approval dict must have at minimum ``ref`` and ``capability_id``.
    Optional ``expires_at`` is forwarded into the approval object.
    """
    lifecycle = []
    for a in approvals:
        approval_obj: dict[str, Any] = {
            "status": "pending",
            "reference": a["ref"],
        }
        if "expires_at" in a:
            approval_obj["expires_at"] = a["expires_at"]
        lifecycle.append(
            {
                "approval": approval_obj,
                "proposal": {"capability_id": a["capability_id"]},
            }
        )
    return TurnExecutionOutcome(
        turn_id="trn_test",
        effective_session_id="ses_test",
        status_code=200,
        response_payload={
            "ok": True,
            "assistant": {"message": message, "sources": [], "silent": False},
            "turn": {"surface_action_lifecycle": lifecycle},
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


def test_deliver_to_discord_no_approvals_posts_no_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn with no pending approvals posts content only — no components key."""
    posted_bodies: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posted_bodies.append(json)
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    _deliver_to_discord(outcome=_outcome(), settings=_settings())

    assert len(posted_bodies) == 1
    assert "components" not in posted_bodies[0]
    assert posted_bodies[0]["content"] == "Hello from Ariel"


def test_deliver_to_discord_pending_approval_adds_approval_line_and_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn with one pending approval appends the approval line and one button row."""
    posted_bodies: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posted_bodies.append(json)
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    outcome = _outcome_with_approvals(
        message="Shall I proceed?",
        approvals=[{"ref": "ref_abc123", "capability_id": "cap.email.send"}],
    )
    _deliver_to_discord(outcome=outcome, settings=_settings())

    assert len(posted_bodies) == 1
    body = posted_bodies[0]

    # Approval line appended after a blank line.
    content = body["content"]
    assert "Shall I proceed?" in content
    assert "Approval pending" in content
    assert "ref_abc123" in content
    assert "Use the buttons below." in content

    # One action row with Approve and Deny buttons.
    components = body["components"]
    assert len(components) == 1
    row = components[0]
    assert row["type"] == 1
    assert len(row["components"]) == 2
    approve, deny = row["components"]
    assert approve["type"] == 2
    assert approve["style"] == 3
    assert approve["label"] == "Approve"
    assert approve["custom_id"] == "ariel:approval:approve:ref_abc123"
    assert deny["type"] == 2
    assert deny["style"] == 4
    assert deny["label"] == "Deny"
    assert deny["custom_id"] == "ariel:approval:deny:ref_abc123"


def test_deliver_to_discord_multiple_pending_approvals_produces_one_row_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple pending approvals each produce one action row and one approval line."""
    posted_bodies: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posted_bodies.append(json)
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    outcome = _outcome_with_approvals(
        message="Two actions staged.",
        approvals=[
            {"ref": "ref_first", "capability_id": "cap.email.send"},
            {"ref": "ref_second", "capability_id": "cap.calendar.event_create"},
        ],
    )
    _deliver_to_discord(outcome=outcome, settings=_settings())

    assert len(posted_bodies) == 1
    body = posted_bodies[0]

    content = body["content"]
    assert "ref_first" in content
    assert "ref_second" in content

    components = body["components"]
    assert len(components) == 2
    assert components[0]["components"][0]["custom_id"] == "ariel:approval:approve:ref_first"
    assert components[0]["components"][1]["custom_id"] == "ariel:approval:deny:ref_first"
    assert components[1]["components"][0]["custom_id"] == "ariel:approval:approve:ref_second"
    assert components[1]["components"][1]["custom_id"] == "ariel:approval:deny:ref_second"


def test_deliver_to_discord_approval_with_expires_at_includes_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval with ``expires_at`` appends the expiry suffix to the approval line."""
    posted_bodies: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posted_bodies.append(json)
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    outcome = _outcome_with_approvals(
        message="Action ready.",
        approvals=[
            {
                "ref": "ref_expiring",
                "capability_id": "cap.email.send",
                "expires_at": "2026-06-01T13:00:00Z",
            }
        ],
    )
    _deliver_to_discord(outcome=outcome, settings=_settings())

    assert len(posted_bodies) == 1
    content = posted_bodies[0]["content"]
    assert "expires_at=2026-06-01T13:00:00Z" in content


def test_deliver_to_discord_dm_posts_to_origin_channel_and_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake originating from a Discord DM must POST to the DM channel and reply to
    the originating message — not to the configured guild notification channel.

    This is the regression for the production incident where a user DM was
    accepted by the API (202), processed by the worker, but the bot's reply
    went to the configured guild channel (the user couldn't see it).
    """
    posts: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posts.append({"url": url, "json": json})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    dm_channel_id = 1506583531923439699
    dm_message_id = 1506583537367912530
    discord_context = {
        "guild_id": None,
        "channel_id": dm_channel_id,
        "channel_type": "private",
        "message_id": dm_message_id,
        "author_id": 481630254318878743,
        "mentioned_bot": False,
    }
    _deliver_to_discord(
        outcome=_outcome(),
        settings=_settings(),
        discord_context=discord_context,
    )

    assert len(posts) == 1
    post = posts[0]
    # URL targets the DM channel, NOT the configured guild channel (123456789).
    assert post["url"] == f"https://discord.com/api/v10/channels/{dm_channel_id}/messages"
    body = post["json"]
    assert body["content"] == "Hello from Ariel"
    # The reply targets the originating message so it threads cleanly.
    assert body["message_reference"] == {
        "message_id": str(dm_message_id),
        "channel_id": str(dm_channel_id),
        "fail_if_not_exists": False,
    }


def test_deliver_to_discord_no_context_falls_back_to_default_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake without a Discord-origin (e.g. a scheduled task) posts to the
    default notification channel and includes no message_reference."""
    posts: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posts.append({"url": url, "json": json})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    _deliver_to_discord(
        outcome=_outcome(),
        settings=_settings(),
        discord_context=None,
    )

    assert len(posts) == 1
    post = posts[0]
    assert post["url"] == "https://discord.com/api/v10/channels/123456789/messages"
    assert "message_reference" not in post["json"]


def test_deliver_to_discord_guild_message_posts_to_origin_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake from a guild message posts to that guild channel, not the
    configured default notification channel."""
    posts: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posts.append({"url": url, "json": json})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    other_channel_id = 999999999
    other_message_id = 888888888
    _deliver_to_discord(
        outcome=_outcome(),
        settings=_settings(),
        discord_context={
            "guild_id": 1234567,
            "channel_id": other_channel_id,
            "channel_type": "GUILD_TEXT",
            "message_id": other_message_id,
            "author_id": 481630254318878743,
            "mentioned_bot": True,
        },
    )

    assert len(posts) == 1
    post = posts[0]
    assert post["url"] == f"https://discord.com/api/v10/channels/{other_channel_id}/messages"
    assert post["json"]["message_reference"]["message_id"] == str(other_message_id)


def test_deliver_to_discord_no_default_channel_with_origin_still_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ARIEL_DISCORD_CHANNEL_ID is unset but a Discord-origin message
    carries a channel_id, delivery still happens — the default channel is a
    fallback, not a gate."""
    posts: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> MagicMock:
        posts.append({"url": url, "json": json})
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("ariel.worker.httpx.post", fake_post)

    settings = _settings(discord_channel_id=None)
    dm_channel_id = 1506583531923439699
    _deliver_to_discord(
        outcome=_outcome(),
        settings=settings,
        discord_context={
            "channel_id": dm_channel_id,
            "message_id": 1506583537367912530,
            "author_id": 481630254318878743,
        },
    )

    assert len(posts) == 1
    assert posts[0]["url"] == f"https://discord.com/api/v10/channels/{dm_channel_id}/messages"
