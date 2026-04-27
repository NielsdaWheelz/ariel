from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
from fastapi.testclient import TestClient
import httpx
import pytest

from ariel.app import create_app
from ariel.config import AppSettings
from ariel.discord_bot import (
    ArielDiscordBot,
    ArielDiscordError,
    decide_approval,
    DiscordBotConfigError,
    ask_ariel,
    configured_discord_bot,
    create_discord_bot,
    format_discord_message,
)


class FakeHttpClient:
    def __init__(self, *, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> FakeHttpClient:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, url: str) -> httpx.Response:
        self.calls.append({"method": "GET", "url": url})
        return self.responses.pop(0)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any],
    ) -> httpx.Response:
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


class FakeUser:
    def __init__(self, *, user_id: int, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot


class FakeGuild:
    def __init__(self, *, guild_id: int) -> None:
        self.id = guild_id


class FakeReference:
    def __init__(self, *, message_id: int | None, resolved: object | None = None) -> None:
        self.message_id = message_id
        self.resolved = resolved


class FakeChannel:
    def __init__(self, *, channel_id: int, fetched_message: FakeDiscordMessage | None = None) -> None:
        self.id = channel_id
        self.fetched_message = fetched_message

    async def fetch_message(self, message_id: int) -> FakeDiscordMessage:
        assert self.fetched_message is not None
        assert self.fetched_message.id == message_id
        return self.fetched_message


class FakeDiscordMessage:
    def __init__(
        self,
        *,
        message_id: int = 123,
        content: str = "status please",
        author: FakeUser | None = None,
        channel: FakeChannel | None = None,
        guild: FakeGuild | None = None,
        mentions: list[FakeUser] | None = None,
        reference: FakeReference | None = None,
        message_type: discord.MessageType = discord.MessageType.default,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author or FakeUser(user_id=3)
        self.channel = channel or FakeChannel(channel_id=2)
        self.guild = guild
        self.mentions = mentions or []
        self.reference = reference
        self.type = message_type
        self.replies: list[dict[str, Any]] = []

    async def reply(
        self,
        content: str,
        *,
        mention_author: bool,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self.replies.append(
            {
                "content": content,
                "mention_author": mention_author,
                "allowed_mentions": allowed_mentions,
            }
        )


def _bot() -> ArielDiscordBot:
    bot = create_discord_bot(
        guild_id=1,
        channel_id=2,
        user_id=3,
        ariel_base_url="http://127.0.0.1:8000",
    )
    setattr(bot._connection, "user", cast(Any, FakeUser(user_id=999, bot=True)))
    return bot


def _stub_ask_ariel(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_ask_ariel(
        *,
        ariel_base_url: str,
        prompt: str,
        discord_message_id: int,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "prompt": prompt,
                "discord_message_id": discord_message_id,
            }
        )
        return f"assistant::{prompt}"

    monkeypatch.setattr("ariel.discord_bot.ask_ariel", fake_ask_ariel)
    return calls


def _send_message(bot: ArielDiscordBot, message: FakeDiscordMessage) -> None:
    asyncio.run(bot.on_message(cast(discord.Message, message)))


def test_configured_discord_bot_requires_discord_settings() -> None:
    with pytest.raises(DiscordBotConfigError) as exc_info:
        configured_discord_bot(cast(Any, AppSettings)(_env_file=None))

    message = str(exc_info.value)
    assert "ARIEL_DISCORD_BOT_TOKEN" in message
    assert "ARIEL_DISCORD_GUILD_ID" in message
    assert "ARIEL_DISCORD_CHANNEL_ID" in message
    assert "ARIEL_DISCORD_USER_ID" in message
    assert "ARIEL_DISCORD_APPLICATION_ID" not in message


def test_discord_bot_enables_message_intents() -> None:
    bot = _bot()

    assert bot.intents.guilds is True
    assert bot.intents.messages is True
    assert bot.intents.message_content is True


def test_format_discord_message_truncates_to_safe_size() -> None:
    formatted = format_discord_message("x" * 2000)
    assert formatted.endswith("\n[truncated]")
    assert len(formatted) <= 1900


def test_ask_ariel_posts_message_with_discord_message_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(200, json={"ok": True, "session": {"id": "ses_test"}}),
                httpx.Response(200, json={"ok": True, "assistant": {"message": "hello"}}),
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = ask_ariel(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="status please",
        discord_message_id=123,
    )

    assert message == "hello"
    assert fake_clients[0].calls == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/sessions/active"},
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/sessions/ses_test/message",
            "headers": {"Idempotency-Key": "discord-message-123"},
            "json": {"message": "status please"},
        },
    ]


def test_ask_ariel_includes_pending_approval_affordance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        return FakeHttpClient(
            responses=[
                httpx.Response(200, json={"ok": True, "session": {"id": "ses_test"}}),
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "assistant": {"message": "I need approval."},
                        "turn": {
                            "surface_action_lifecycle": [
                                {
                                    "proposal": {"capability_id": "cap.email.send"},
                                    "approval": {
                                        "status": "pending",
                                        "reference": "apr_123",
                                        "expires_at": "2026-04-27T12:00:00Z",
                                    },
                                }
                            ]
                        },
                    },
                ),
            ]
        )

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = ask_ariel(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="send it",
        discord_message_id=123,
    )

    assert "I need approval." in message
    assert "Approval pending (cap.email.send): apr_123" in message
    assert "approve apr_123" in message
    assert "deny apr_123" in message


def test_decide_approval_posts_discord_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "approval": {"reference": "apr_123", "status": "approved"},
                        "assistant": {"message": "approval recorded"},
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = decide_approval(
        ariel_base_url="http://127.0.0.1:8000",
        approval_ref="apr_123",
        decision="approve",
    )

    assert message == "Approval approved: apr_123\napproval recorded"
    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/approvals",
            "headers": None,
            "json": {"approval_ref": "apr_123", "decision": "approve"},
        }
    ]


def test_ask_ariel_surfaces_safe_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        return FakeHttpClient(
            responses=[
                httpx.Response(
                    503,
                    json={
                        "ok": False,
                        "error": {
                            "code": "E_MODEL_PROVIDER_DOWN",
                            "message": "model provider unavailable",
                        },
                    },
                )
            ]
        )

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    with pytest.raises(ArielDiscordError, match="model provider unavailable"):
        ask_ariel(
            ariel_base_url="http://127.0.0.1:8000",
            prompt="status please",
            discord_message_id=123,
        )


def test_on_message_answers_configured_user_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=321,
        content="hello dm",
        guild=None,
        channel=FakeChannel(channel_id=77),
    )

    _send_message(bot, message)

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "prompt": "hello dm",
            "discord_message_id": 321,
        }
    ]
    assert message.replies[0]["content"] == "assistant::hello dm"
    assert message.replies[0]["mention_author"] is False


def test_on_message_answers_primary_channel_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=456,
        content="hello channel",
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=2),
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "hello channel"
    assert calls[0]["discord_message_id"] == 456
    assert message.replies[0]["content"] == "assistant::hello channel"


def test_on_message_handles_approval_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_decide_approval(
        *,
        ariel_base_url: str,
        approval_ref: str,
        decision: str,
        reason: str | None = None,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "approval_ref": approval_ref,
                "decision": decision,
                "reason": reason,
            }
        )
        return "Approval denied: apr_456"

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fake_decide_approval)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=456,
        content="deny apr_456 not right now",
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=2),
    )

    _send_message(bot, message)

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "approval_ref": "apr_456",
            "decision": "deny",
            "reason": "not right now",
        }
    ]
    assert message.replies[0]["content"] == "Approval denied: apr_456"


def test_on_message_answers_other_server_direct_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="<@999> hello elsewhere",
        guild=FakeGuild(guild_id=99),
        channel=FakeChannel(channel_id=88),
        mentions=[FakeUser(user_id=999, bot=True)],
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "hello elsewhere"
    assert message.replies[0]["content"] == "assistant::hello elsewhere"


def test_on_message_answers_other_server_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()
    referenced = FakeDiscordMessage(
        message_id=777,
        author=FakeUser(user_id=999, bot=True),
        guild=FakeGuild(guild_id=99),
    )
    message = FakeDiscordMessage(
        content="follow up",
        guild=FakeGuild(guild_id=99),
        channel=FakeChannel(channel_id=88),
        reference=FakeReference(message_id=777, resolved=referenced),
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "follow up"
    assert message.replies[0]["content"] == "assistant::follow up"


def test_on_message_ignores_other_server_unmentioned_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="ambient chatter",
        guild=FakeGuild(guild_id=99),
        channel=FakeChannel(channel_id=88),
    )

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []


def test_root_is_discord_primary_status_not_phone_chat() -> None:
    app = create_app(database_url="sqlite+pysqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["ok"] is True
    assert payload["surface"] == "discord"
    assert "Discord" in payload["message"]
    assert payload["api"]["active_session"] == "/v1/sessions/active"
    assert "chat-form" not in response.text
    assert "/v1/sessions/${sessionId}/events" not in response.text


@pytest.mark.parametrize(
    "message",
    [
        FakeDiscordMessage(author=FakeUser(user_id=44)),
        FakeDiscordMessage(author=FakeUser(user_id=3, bot=True)),
        FakeDiscordMessage(content="   "),
        FakeDiscordMessage(message_type=discord.MessageType.pins_add),
    ],
)
def test_on_message_ignores_unsupported_messages(
    monkeypatch: pytest.MonkeyPatch,
    message: FakeDiscordMessage,
) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []
