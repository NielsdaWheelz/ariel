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
    ArielDiscordReply,
    ArielDiscordError,
    ArielActionView,
    ack_notification,
    decide_approval,
    DiscordBotConfigError,
    ask_ariel,
    ask_ariel_reply,
    configured_discord_bot,
    create_discord_bot,
    format_discord_message,
    refresh_job,
)


class StaticModelAdapter:
    provider = "test.responses"
    model = "test-model"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history, context_bundle
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "provider_response_id": "resp_test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
        }


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
        view: discord.ui.View | None = None,
    ) -> None:
        self.replies.append(
            {
                "content": content,
                "mention_author": mention_author,
                "allowed_mentions": allowed_mentions,
                "view": view,
            }
        )


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self._done = True
        self.messages.append(
            {
                "content": content,
                "ephemeral": ephemeral,
                "allowed_mentions": allowed_mentions,
            }
        )

    async def edit_message(
        self,
        *,
        content: str,
        view: discord.ui.View | None,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self._done = True
        self.edits.append(
            {
                "content": content,
                "view": view,
                "allowed_mentions": allowed_mentions,
            }
        )


class FakeInteraction:
    def __init__(
        self,
        *,
        custom_id: str,
        user_id: int = 3,
        guild_id: int | None = 1,
        channel_id: int = 2,
    ) -> None:
        self.id = 987
        self.data = {"custom_id": custom_id}
        self.user = FakeUser(user_id=user_id)
        self.guild = FakeGuild(guild_id=guild_id) if guild_id is not None else None
        self.channel_id = channel_id
        self.response = FakeInteractionResponse()


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

    def fake_ask_ariel_reply(
        *,
        ariel_base_url: str,
        prompt: str,
        discord_message_id: int,
        allowed_user_id: int | None = None,
    ) -> ArielDiscordReply:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "prompt": prompt,
                "discord_message_id": discord_message_id,
                "allowed_user_id": allowed_user_id,
            }
        )
        return ArielDiscordReply(content=f"assistant::{prompt}")

    monkeypatch.setattr("ariel.discord_bot.ask_ariel_reply", fake_ask_ariel_reply)
    return calls


def _send_message(bot: ArielDiscordBot, message: FakeDiscordMessage) -> None:
    asyncio.run(bot.on_message(cast(discord.Message, message)))


def _send_interaction(bot: ArielDiscordBot, interaction: FakeInteraction) -> None:
    asyncio.run(bot.on_interaction(cast(discord.Interaction, interaction)))


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


def test_discord_bot_registers_ariel_slash_command() -> None:
    bot = _bot()

    command = bot.tree.get_command("ariel")

    assert command is not None
    assert command.description == "Ask Ariel through the local API."


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
    assert "Use the buttons below." in message
    assert "approve apr_123" not in message
    assert "deny apr_123" not in message


def test_ask_ariel_reply_adds_approval_buttons(
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
                                    },
                                }
                            ]
                        },
                    },
                ),
            ]
        )

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    reply = ask_ariel_reply(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="send it",
        discord_message_id=123,
        allowed_user_id=3,
    )

    assert reply.view is not None
    custom_ids = [cast(Any, item).custom_id for item in reply.view.children]
    assert custom_ids == [
        "ariel:approval:approve:apr_123",
        "ariel:approval:deny:apr_123",
    ]


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


def test_refresh_job_fetches_job_and_events(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "job": {
                            "id": "job_123",
                            "status": "completed",
                            "title": "Agency bridge",
                            "summary": "done",
                        },
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "job_id": "job_123",
                        "events": [
                            {
                                "event_type": "completed",
                                "created_at": "2026-04-27T12:00:00Z",
                            }
                        ],
                    },
                ),
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = refresh_job(ariel_base_url="http://127.0.0.1:8000", job_id="job_123")

    assert "Job job_123: completed" in message
    assert "Agency bridge" in message
    assert "- completed at 2026-04-27T12:00:00Z" in message
    assert fake_clients[0].calls[:2] == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/jobs/job_123"},
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/jobs/job_123/events"},
    ]


def test_ack_notification_posts_ack_and_returns_job_id(
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
                        "notification": {
                            "id": "ntf_123",
                            "title": "Agency completed",
                            "payload": {"job_id": "job_123"},
                        },
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message, job_id = ack_notification(
        ariel_base_url="http://127.0.0.1:8000",
        notification_id="ntf_123",
    )

    assert message == "Notification acknowledged: Agency completed (ntf_123)"
    assert job_id == "job_123"
    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/notifications/ntf_123/ack",
            "headers": None,
            "json": {},
        }
    ]


def test_action_view_uses_custom_ids_for_job_refresh_and_notification_ack() -> None:
    view = ArielActionView(
        ariel_base_url="http://127.0.0.1:8000",
        job_id="job_123",
        notification_id="ntf_123",
        allowed_user_id=3,
    )

    custom_ids = [cast(Any, item).custom_id for item in view.children]
    assert custom_ids == [
        "ariel:job:refresh:job_123",
        "ariel:notification:ack:ntf_123",
    ]


def test_on_interaction_handles_approval_custom_id(monkeypatch: pytest.MonkeyPatch) -> None:
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
        return "Approval approved: apr_123"

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fake_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id="ariel:approval:approve:apr_123")

    _send_interaction(bot, interaction)

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "approval_ref": "apr_123",
            "decision": "approve",
            "reason": None,
        }
    ]
    assert interaction.response.edits[0]["content"] == "Approval approved: apr_123"
    assert interaction.response.edits[0]["view"] is None


def test_on_interaction_rejects_wrong_user() -> None:
    bot = _bot()
    interaction = FakeInteraction(
        custom_id="ariel:job:refresh:job_123",
        user_id=44,
    )

    _send_interaction(bot, interaction)

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


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
            "allowed_user_id": 3,
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


def test_on_message_sends_legacy_approval_text_as_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_ask_ariel(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=456,
        content="deny apr_456 not right now",
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=2),
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "deny apr_456 not right now"
    assert message.replies[0]["content"] == "assistant::deny apr_456 not right now"


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
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        model_adapter=StaticModelAdapter(),
    )
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
