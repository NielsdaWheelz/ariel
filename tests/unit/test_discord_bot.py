from __future__ import annotations

import asyncio
from datetime import datetime, timezone, tzinfo
from typing import Any, Self, cast

import discord
from fastapi.testclient import TestClient
import httpx
import pytest

from ariel.config import AppSettings
from ariel.discord_bot import (
    ArielDiscordBot,
    ArielDiscordReply,
    ArielDiscordError,
    ArielActionView,
    ack_notification,
    ack_attention_item,
    decide_approval,
    DiscordBotConfigError,
    configured_discord_bot,
    create_discord_bot,
    format_discord_message,
    get_status,
    refresh_attention_item,
    list_jobs,
    list_memory,
    record_capture,
    refresh_job,
    resolve_attention_item,
    snooze_attention_item,
    submit_discord_turn,
    _is_ariel_custom_id,
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


class FakeTyping:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    async def __aenter__(self) -> None:
        self.channel.events.append("typing_enter")

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.channel.events.append("typing_exit")


class FakeChannel:
    def __init__(
        self,
        *,
        channel_id: int,
        fetched_message: FakeDiscordMessage | None = None,
        parent_channel_id: int | None = None,
    ) -> None:
        self.id = channel_id
        self.fetched_message = fetched_message
        self.parent_id = parent_channel_id
        self.events: list[str] = []

    async def fetch_message(self, message_id: int) -> FakeDiscordMessage:
        assert self.fetched_message is not None
        assert self.fetched_message.id == message_id
        return self.fetched_message

    def typing(self) -> FakeTyping:
        return FakeTyping(self)


class FakeAttachment:
    def __init__(
        self,
        *,
        attachment_id: int = 555,
        filename: str = "notes.txt",
        content_type: str | None = "text/plain",
        size: int = 12,
        url: str = "https://cdn.example.test/notes.txt",
    ) -> None:
        self.id = attachment_id
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.url = url


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
        attachments: list[FakeAttachment] | None = None,
        message_type: discord.MessageType = discord.MessageType.default,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author or FakeUser(user_id=3)
        self.channel = channel or FakeChannel(channel_id=2)
        self.guild = guild
        self.mentions = mentions or []
        self.reference = reference
        self.attachments = attachments or []
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
        self.channel.events.append("reply")
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
        self.deferrals: list[dict[str, Any]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, thinking: bool = False, ephemeral: bool = False) -> None:
        self._done = True
        self.deferrals.append({"thinking": thinking, "ephemeral": ephemeral})

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


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self.messages.append(
            {
                "content": content,
                "ephemeral": ephemeral,
                "allowed_mentions": allowed_mentions,
            }
        )


class FakeInteraction:
    def __init__(
        self,
        *,
        custom_id: str = "",
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
        self.followup = FakeFollowup()


def _bot() -> ArielDiscordBot:
    bot = create_discord_bot(
        guild_id=1,
        channel_id=2,
        user_id=3,
        ariel_base_url="http://127.0.0.1:8000",
    )
    setattr(bot._connection, "user", cast(Any, FakeUser(user_id=999, bot=True)))
    return bot


def _stub_discord_turn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_submit_discord_turn(
        *,
        ariel_base_url: str,
        prompt: str,
        discord_message_id: int,
        allowed_user_id: int | None = None,
        discord_context: dict[str, Any] | None = None,
    ) -> ArielDiscordReply:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "prompt": prompt,
                "discord_message_id": discord_message_id,
                "allowed_user_id": allowed_user_id,
                "discord_context": discord_context,
            }
        )
        return ArielDiscordReply(content=f"assistant::{prompt}")

    monkeypatch.setattr("ariel.discord_bot.submit_discord_turn", fake_submit_discord_turn)
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


def test_discord_bot_registers_only_deterministic_ops_slash_commands() -> None:
    bot = _bot()

    assert bot.tree.get_command("ariel") is None
    assert bot.tree.get_command("ask") is None

    assert bot.tree.get_command("status") is not None
    assert bot.tree.get_command("jobs") is not None
    assert bot.tree.get_command("memory") is not None
    assert bot.tree.get_command("capture") is not None


def test_format_discord_message_truncates_to_safe_size() -> None:
    formatted = format_discord_message("x" * 2000)
    assert formatted.endswith("\n[truncated]")
    assert len(formatted) <= 1900


def test_submit_discord_turn_posts_message_with_discord_message_idempotency(
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

    reply = submit_discord_turn(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="status please",
        discord_message_id=123,
    )

    assert reply.content == "hello"
    assert fake_clients[0].calls == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/sessions/active"},
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/sessions/ses_test/message",
            "headers": {"Idempotency-Key": "discord-message-123"},
            "json": {"message": "status please"},
        },
    ]


def test_submit_discord_turn_posts_discord_context_as_separate_field(
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

    reply = submit_discord_turn(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="status please",
        discord_message_id=123,
        discord_context={
            "guild_id": 1,
            "channel_id": 88,
            "message_id": 123,
            "author_id": 3,
            "attachments": [{"filename": "report.pdf"}],
        },
    )

    assert reply.content == "hello"
    assert fake_clients[0].calls[1]["json"] == {
        "message": "status please",
        "discord": {
            "guild_id": 1,
            "channel_id": 88,
            "message_id": 123,
            "author_id": 3,
            "attachments": [{"filename": "report.pdf"}],
        },
    }


def test_submit_discord_turn_supports_silent_assistant_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        return FakeHttpClient(
            responses=[
                httpx.Response(200, json={"ok": True, "session": {"id": "ses_test"}}),
                httpx.Response(
                    200,
                    json={"ok": True, "assistant": {"silent": True}},
                ),
            ]
        )

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    reply = submit_discord_turn(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="status please",
        discord_message_id=123,
    )

    assert reply.silent is True
    assert reply.content == ""


def test_submit_discord_turn_includes_pending_approval_affordance(
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

    reply = submit_discord_turn(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="send it",
        discord_message_id=123,
    )

    message = reply.content
    assert "I need approval." in message
    assert "Approval pending (cap.email.send): apr_123" in message
    assert "Use the buttons below." in message
    assert "approve apr_123" not in message
    assert "deny apr_123" not in message


def test_submit_discord_turn_adds_approval_buttons(
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

    reply = submit_discord_turn(
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


def test_ack_attention_item_posts_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "attention_item": {
                            "id": "ati_123",
                            "title": "Review inbox conflict",
                        },
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = ack_attention_item(
        ariel_base_url="http://127.0.0.1:8000",
        attention_item_id="ati_123",
    )

    assert message == "Attention item acknowledged: Review inbox conflict (ati_123)"
    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/attention-items/ati_123/ack",
            "headers": None,
            "json": {},
        }
    ]


def test_snooze_attention_item_posts_24_hour_snooze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clients: list[FakeHttpClient] = []

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            assert tz is timezone.utc
            return cls(2026, 4, 30, 12, 0, tzinfo=timezone.utc)

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "attention_item": {
                            "id": "ati_123",
                            "title": "Review inbox conflict",
                        },
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.datetime", FrozenDateTime)
    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = snooze_attention_item(
        ariel_base_url="http://127.0.0.1:8000",
        attention_item_id="ati_123",
    )

    assert message == "Attention item snoozed: Review inbox conflict (ati_123)"
    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/attention-items/ati_123/snooze",
            "headers": None,
            "json": {"snooze_until": "2026-05-01T12:00:00+00:00"},
        }
    ]


def test_resolve_attention_item_posts_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "attention_item": {
                            "id": "ati_123",
                            "reason": "Calendar conflict was handled",
                        },
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = resolve_attention_item(
        ariel_base_url="http://127.0.0.1:8000",
        attention_item_id="ati_123",
    )

    assert message == "Attention item resolved: Calendar conflict was handled (ati_123)"
    assert fake_clients[0].calls[0]["url"] == (
        "http://127.0.0.1:8000/v1/attention-items/ati_123/resolve"
    )
    assert fake_clients[0].calls[0]["json"] == {}


def test_refresh_attention_item_posts_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "attention_item": {
                            "id": "ati_123",
                            "title": "Review inbox conflict",
                        },
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = refresh_attention_item(
        ariel_base_url="http://127.0.0.1:8000",
        attention_item_id="ati_123",
    )

    assert message == "Attention item refreshed: Review inbox conflict (ati_123)"
    assert fake_clients[0].calls[0]["url"] == (
        "http://127.0.0.1:8000/v1/attention-items/ati_123/refresh"
    )
    assert fake_clients[0].calls[0]["json"] == {}


def test_status_command_fetches_only_deterministic_ops_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(200, json={"ok": True}),
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "session": {"id": "ses_123", "lifecycle_state": "active"},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "jobs": [
                            {"id": "job_1", "status": "running", "title": "Do work"},
                            {"id": "job_2", "status": "succeeded", "title": "Done"},
                        ],
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "notifications": [
                            {"id": "ntf_1", "status": "delivered", "title": "Review"},
                            {"id": "ntf_2", "status": "acknowledged", "title": "Done"},
                        ],
                    },
                ),
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = get_status(ariel_base_url="http://127.0.0.1:8000")

    assert "Ariel status: ok" in message
    assert "Active session: ses_123 (active)" in message
    assert "Recent jobs: 2 total, 1 active" in message
    assert "Notifications needing attention: 1" in message
    assert fake_clients[0].calls == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/health"},
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/sessions/active"},
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/jobs?limit=5"},
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/notifications?limit=5"},
    ]


def test_jobs_command_fetches_job_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "jobs": [{"id": "job_123", "status": "running", "title": "Agency bridge"}],
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = list_jobs(ariel_base_url="http://127.0.0.1:8000")

    assert message == "Recent jobs:\n- job_123: running: Agency bridge"
    assert fake_clients[0].calls == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/jobs?limit=10"}
    ]


def test_memory_command_fetches_memory_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "schema_version": "memory.sota.v1",
                        "active_assertions": [
                            {
                                "subject_key": "user:default",
                                "predicate": "preference.notebook_style",
                                "state": "active",
                                "value": "short bullets",
                            },
                        ],
                        "candidates": [],
                        "conflicts": [],
                        "project_state": [],
                        "evidence": [],
                        "procedures": [],
                        "projection_health": {
                            "projection_version": "embedding-v1",
                            "pending_jobs": 0,
                            "failed_jobs": 0,
                        },
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = list_memory(ariel_base_url="http://127.0.0.1:8000")

    assert message == "Active memory:\n- user:default preference.notebook_style: short bullets"
    assert fake_clients[0].calls == [{"method": "GET", "url": "http://127.0.0.1:8000/v1/memory"}]


def test_capture_command_records_capture_without_message_endpoint(
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
                        "capture": {"id": "cpt_123", "terminal_state": "turn_created"},
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = record_capture(
        ariel_base_url="http://127.0.0.1:8000",
        text="save this",
        discord_interaction_id=987,
    )

    assert message == "Capture recorded: cpt_123 (turn_created)"
    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/captures/record",
            "headers": {"Idempotency-Key": "discord-capture-987"},
            "json": {"kind": "text", "text": "save this"},
        }
    ]


def test_slash_status_sends_ephemeral_deterministic_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_status(*, ariel_base_url: str) -> str:
        assert ariel_base_url == "http://127.0.0.1:8000"
        return "Ariel status: ok"

    monkeypatch.setattr("ariel.discord_bot.get_status", fake_get_status)
    bot = _bot()
    interaction = FakeInteraction()

    asyncio.run(bot._slash_status(cast(discord.Interaction, interaction)))

    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == "Ariel status: ok"
    assert interaction.followup.messages[0]["ephemeral"] is True


def test_slash_status_rejects_wrong_user() -> None:
    bot = _bot()
    interaction = FakeInteraction(user_id=44)

    asyncio.run(bot._slash_status(cast(discord.Interaction, interaction)))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_slash_capture_sends_ephemeral_deterministic_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_record_capture(
        *,
        ariel_base_url: str,
        text: str,
        discord_interaction_id: int,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "text": text,
                "discord_interaction_id": discord_interaction_id,
            }
        )
        return "Capture recorded: cpt_123 (turn_created)"

    monkeypatch.setattr("ariel.discord_bot.record_capture", fake_record_capture)
    bot = _bot()
    interaction = FakeInteraction()

    asyncio.run(bot._slash_capture(cast(discord.Interaction, interaction), "save this"))

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "text": "save this",
            "discord_interaction_id": 987,
        }
    ]
    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == "Capture recorded: cpt_123 (turn_created)"
    assert interaction.followup.messages[0]["ephemeral"] is True


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


def test_action_view_uses_custom_ids_for_attention_item_actions() -> None:
    view = ArielActionView(
        ariel_base_url="http://127.0.0.1:8000",
        attention_item_id="ati_123",
        allowed_user_id=3,
    )

    custom_ids = [cast(Any, item).custom_id for item in view.children]
    assert custom_ids == [
        "ariel:attention:ack:ati_123",
        "ariel:attention:snooze:ati_123",
        "ariel:attention:resolve:ati_123",
        "ariel:attention:refresh:ati_123",
    ]


@pytest.mark.parametrize(
    "custom_id",
    [
        "ariel:approval:approve:apr_123",
        "ariel:job:refresh:job_123",
        "ariel:notification:ack:ntf_123",
        "ariel:attention:ack:ati_123",
        "ariel:attention:snooze:ati_123",
        "ariel:attention:resolve:ati_123",
        "ariel:attention:refresh:ati_123",
    ],
)
def test_is_ariel_custom_id_recognizes_supported_action_prefixes(custom_id: str) -> None:
    assert _is_ariel_custom_id(custom_id) is True


def test_is_ariel_custom_id_rejects_unknown_attention_action() -> None:
    assert _is_ariel_custom_id("ariel:attention:delete:ati_123") is False


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
    interaction = FakeInteraction(custom_id="ariel:approval:approve:apr_123", channel_id=88)

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


@pytest.mark.parametrize(
    ("custom_id", "function_name", "expected_content", "expected_view"),
    [
        (
            "ariel:attention:ack:ati_123",
            "ack_attention_item",
            "Attention item acknowledged: ati_123",
            None,
        ),
        (
            "ariel:attention:snooze:ati_123",
            "snooze_attention_item",
            "Attention item snoozed: ati_123",
            None,
        ),
        (
            "ariel:attention:resolve:ati_123",
            "resolve_attention_item",
            "Attention item resolved: ati_123",
            None,
        ),
        (
            "ariel:attention:refresh:ati_123",
            "refresh_attention_item",
            "Attention item refreshed: ati_123",
            "refresh_view",
        ),
    ],
)
def test_on_interaction_handles_attention_item_custom_ids(
    monkeypatch: pytest.MonkeyPatch,
    custom_id: str,
    function_name: str,
    expected_content: str,
    expected_view: str | None,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_attention_action(*, ariel_base_url: str, attention_item_id: str) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "attention_item_id": attention_item_id,
                "function_name": function_name,
            }
        )
        return expected_content

    monkeypatch.setattr(f"ariel.discord_bot.{function_name}", fake_attention_action)
    bot = _bot()
    interaction = FakeInteraction(custom_id=custom_id, channel_id=88)

    _send_interaction(bot, interaction)

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "attention_item_id": "ati_123",
            "function_name": function_name,
        }
    ]
    assert interaction.response.edits[0]["content"] == expected_content
    if expected_view is None:
        assert interaction.response.edits[0]["view"] is None
    else:
        view = interaction.response.edits[0]["view"]
        assert view is not None
        custom_ids = [cast(Any, item).custom_id for item in view.children]
        assert custom_ids == [
            "ariel:attention:ack:ati_123",
            "ariel:attention:snooze:ati_123",
            "ariel:attention:resolve:ati_123",
            "ariel:attention:refresh:ati_123",
        ]


def test_on_interaction_rejects_wrong_user() -> None:
    bot = _bot()
    interaction = FakeInteraction(
        custom_id="ariel:job:refresh:job_123",
        user_id=44,
    )

    _send_interaction(bot, interaction)

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_submit_discord_turn_surfaces_safe_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
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
        submit_discord_turn(
            ariel_base_url="http://127.0.0.1:8000",
            prompt="status please",
            discord_message_id=123,
        )


def test_on_message_answers_configured_user_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    channel = FakeChannel(channel_id=77)
    message = FakeDiscordMessage(
        message_id=321,
        content="hello dm",
        guild=None,
        channel=channel,
    )

    _send_message(bot, message)

    assert calls[0]["ariel_base_url"] == "http://127.0.0.1:8000"
    assert calls[0]["prompt"] == "hello dm"
    assert calls[0]["discord_message_id"] == 321
    assert calls[0]["allowed_user_id"] == 3
    assert calls[0]["discord_context"] == {
        "guild_id": None,
        "channel_id": 77,
        "message_id": 321,
        "author_id": 3,
        "mentioned_bot": False,
    }
    assert message.replies[0]["content"] == "assistant::hello dm"
    assert message.replies[0]["mention_author"] is False
    assert channel.events == ["typing_enter", "typing_exit", "reply"]


def test_on_message_answers_home_guild_message_in_any_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=456,
        content="hello channel",
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=88, parent_channel_id=2),
        attachments=[
            FakeAttachment(
                attachment_id=777,
                filename="report.pdf",
                content_type="application/pdf",
                size=2048,
                url="https://cdn.example.test/report.pdf",
            )
        ],
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "hello channel"
    assert calls[0]["discord_context"] == {
        "guild_id": 1,
        "channel_id": 88,
        "message_id": 456,
        "author_id": 3,
        "mentioned_bot": False,
        "thread_id": 88,
        "parent_channel_id": 2,
        "attachments": [
            {
                "source": "discord",
                "source_attachment_id": 777,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 2048,
                "attachment_ref": "discord:777",
                "download_url": "https://cdn.example.test/report.pdf",
            }
        ],
    }
    assert calls[0]["discord_message_id"] == 456
    assert message.replies[0]["content"] == "assistant::hello channel"


def test_on_message_answers_attachment_only_home_guild_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=654,
        content="",
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=88),
        attachments=[FakeAttachment(filename="photo.png", content_type="image/png")],
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "What would you like me to do with the attachment(s)?"
    assert calls[0]["discord_context"]["attachments"][0]["filename"] == "photo.png"
    assert "Uploaded attachment(s)." not in calls[0]["prompt"]
    assert (
        message.replies[0]["content"]
        == "assistant::What would you like me to do with the attachment(s)?"
    )


def test_on_message_sends_legacy_approval_text_as_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
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


def test_on_message_strips_direct_bot_mention_from_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="<@999> hello home",
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=88),
        mentions=[FakeUser(user_id=999, bot=True)],
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "hello home"


def test_on_message_sends_no_reply_for_silent_assistant_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_submit_ambient_turn(
        *,
        prompt: str,
        discord_message_id: int,
        discord_context: dict[str, Any] | None = None,
    ) -> ArielDiscordReply:
        calls.append(
            {
                "prompt": prompt,
                "discord_message_id": discord_message_id,
                "discord_context": discord_context,
            }
        )
        return ArielDiscordReply(content="", silent=True)

    bot = _bot()
    monkeypatch.setattr(bot, "_submit_ambient_turn", fake_submit_ambient_turn)
    channel = FakeChannel(channel_id=2)
    message = FakeDiscordMessage(
        message_id=789,
        content="quietly note this",
        guild=FakeGuild(guild_id=1),
        channel=channel,
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "quietly note this"
    assert message.replies == []
    assert channel.events == ["typing_enter", "typing_exit"]


def test_on_message_ignores_other_server_direct_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="<@999> hello elsewhere",
        guild=FakeGuild(guild_id=99),
        channel=FakeChannel(channel_id=88),
        mentions=[FakeUser(user_id=999, bot=True)],
    )

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []


def test_on_message_ignores_other_server_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_discord_turn(monkeypatch)
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

    assert calls == []
    assert message.replies == []


def test_on_message_ignores_other_server_unmentioned_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
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
    from ariel.app import create_app

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
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []
