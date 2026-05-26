from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
import httpx
import pytest

from ariel.config import AppSettings
from ariel.discord_bot import (
    ArielDiscordBot,
    ArielDiscordError,
    decide_approval,
    DiscordBotConfigError,
    configured_discord_bot,
    create_discord_bot,
    format_discord_message,
    get_status,
    list_jobs,
    record_capture,
    submit_discord_turn,
)


class FakeHttpClient:
    def __init__(self, *, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> FakeHttpClient:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        call: dict[str, Any] = {"method": "GET", "url": url}
        if headers is not None:
            call["headers"] = headers
        self.calls.append(call)
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
        ariel_auth_token: str | None = None,
        prompt: str,
        discord_message_id: int,
        discord_context: dict[str, Any] | None = None,
    ) -> None:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "ariel_auth_token": ariel_auth_token,
                "prompt": prompt,
                "discord_message_id": discord_message_id,
                "discord_context": discord_context,
            }
        )

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


def test_configured_discord_bot_uses_local_auth_token_when_required() -> None:
    bot = configured_discord_bot(
        cast(Any, AppSettings)(
            _env_file=None,
            local_auth_required=True,
            local_auth_token="local_token_0123456789abcdef012345",
            discord_bot_token="discord-token",
            discord_guild_id=123,
            discord_channel_id=456,
            discord_user_id=789,
        )
    )

    assert bot.ariel_auth_token == "local_token_0123456789abcdef012345"


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
    assert bot.tree.get_command("capture") is not None


def test_setup_hook_syncs_slash_commands_only_to_home_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int | None]] = []
    bot = _bot()

    def fake_copy_global_to(*, guild: discord.Object) -> None:
        calls.append(("copy_global_to", guild.id))

    async def fake_sync(*, guild: discord.Object | None = None) -> list[object]:
        calls.append(("sync", None if guild is None else guild.id))
        return []

    monkeypatch.setattr(bot.tree, "copy_global_to", fake_copy_global_to)
    monkeypatch.setattr(bot.tree, "sync", fake_sync)

    asyncio.run(bot.setup_hook())

    assert calls == [("copy_global_to", 1), ("sync", 1)]


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
                httpx.Response(202, json={"status": "accepted", "task_id": "tsk_1"}),
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    submit_discord_turn(
        ariel_base_url="http://127.0.0.1:8000",
        prompt="status please",
        discord_message_id=123,
    )

    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/messages",
            "headers": {"Idempotency-Key": "discord-message-123"},
            "json": {"message": "status please"},
        },
    ]


def test_submit_discord_turn_sends_local_auth_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(202, json={"status": "accepted", "task_id": "tsk_1"}),
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    submit_discord_turn(
        ariel_base_url="http://127.0.0.1:8000",
        ariel_auth_token="local_token_0123456789abcdef012345",
        prompt="status please",
        discord_message_id=123,
    )

    assert fake_clients[0].calls[0]["headers"] == {
        "Authorization": "Bearer local_token_0123456789abcdef012345",
        "Idempotency-Key": "discord-message-123",
    }


def test_submit_discord_turn_posts_discord_context_as_separate_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clients: list[FakeHttpClient] = []

    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        client = FakeHttpClient(
            responses=[
                httpx.Response(202, json={"status": "accepted", "task_id": "tsk_1"}),
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    submit_discord_turn(
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

    assert fake_clients[0].calls[0]["json"] == {
        "message": "status please",
        "discord": {
            "guild_id": 1,
            "channel_id": 88,
            "message_id": 123,
            "author_id": 3,
            "attachments": [{"filename": "report.pdf"}],
        },
    }


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


def test_decide_approval_posts_discord_denial(
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
                        "approval": {"reference": "apr_123", "status": "denied"},
                        "assistant": {"message": "approval denied"},
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
        decision="deny",
    )

    assert message == "Approval denied: apr_123\napproval denied"
    assert fake_clients[0].calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/v1/approvals",
            "headers": None,
            "json": {"approval_ref": "apr_123", "decision": "deny"},
        }
    ]


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
                        "jobs": [
                            {"id": "job_1", "status": "running", "title": "Do work"},
                            {"id": "job_2", "status": "succeeded", "title": "Done"},
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
    assert "Recent jobs: 2 total, 1 active" in message
    assert fake_clients[0].calls == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/health"},
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/jobs?limit=5"},
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
                        "jobs": [
                            {
                                "id": "job_123",
                                "status": "running",
                                "title": "Draft release notes",
                            }
                        ],
                    },
                )
            ]
        )
        fake_clients.append(client)
        return client

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    message = list_jobs(ariel_base_url="http://127.0.0.1:8000")

    assert message == "Recent jobs:\n- job_123: running: Draft release notes"
    assert fake_clients[0].calls == [
        {"method": "GET", "url": "http://127.0.0.1:8000/v1/jobs?limit=10"}
    ]


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
                        "capture": {"id": "cpt_123"},
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

    assert message == "Capture recorded: cpt_123"
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
    def fake_get_status(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
    ) -> str:
        assert ariel_base_url == "http://127.0.0.1:8000"
        assert ariel_auth_token is None
        return "Ariel status: ok"

    monkeypatch.setattr("ariel.discord_bot.get_status", fake_get_status)
    bot = _bot()
    interaction = FakeInteraction()

    asyncio.run(bot._slash_status(cast(discord.Interaction, interaction)))

    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == "Ariel status: ok"
    assert interaction.followup.messages[0]["ephemeral"] is True


def test_slash_status_rejects_wrong_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_get_status(**_: object) -> str:
        raise AssertionError("wrong-user status command must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.get_status", fail_get_status)
    bot = _bot()
    interaction = FakeInteraction(user_id=44)

    asyncio.run(bot._slash_status(cast(discord.Interaction, interaction)))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_slash_status_rejects_wrong_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_get_status(**_: object) -> str:
        raise AssertionError("wrong-guild status command must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.get_status", fail_get_status)
    bot = _bot()
    interaction = FakeInteraction(guild_id=99)

    asyncio.run(bot._slash_status(cast(discord.Interaction, interaction)))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_slash_status_allows_configured_user_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get_status(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
    ) -> str:
        calls.append({"ariel_base_url": ariel_base_url, "ariel_auth_token": ariel_auth_token})
        return "Ariel status: ok"

    monkeypatch.setattr("ariel.discord_bot.get_status", fake_get_status)
    bot = _bot()
    interaction = FakeInteraction(guild_id=None)

    asyncio.run(bot._slash_status(cast(discord.Interaction, interaction)))

    assert calls == [{"ariel_base_url": "http://127.0.0.1:8000", "ariel_auth_token": None}]
    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == "Ariel status: ok"


def test_slash_jobs_sends_ephemeral_deterministic_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_list_jobs(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
    ) -> str:
        assert ariel_base_url == "http://127.0.0.1:8000"
        assert ariel_auth_token is None
        return "Recent jobs:\n- job_123: running: Draft release notes"

    monkeypatch.setattr("ariel.discord_bot.list_jobs", fake_list_jobs)
    bot = _bot()
    interaction = FakeInteraction()

    asyncio.run(bot._slash_jobs(cast(discord.Interaction, interaction)))

    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == (
        "Recent jobs:\n- job_123: running: Draft release notes"
    )
    assert interaction.followup.messages[0]["ephemeral"] is True


def test_slash_jobs_allows_configured_user_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_list_jobs(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
    ) -> str:
        calls.append({"ariel_base_url": ariel_base_url, "ariel_auth_token": ariel_auth_token})
        return "Recent jobs:\n- job_123: running: Draft release notes"

    monkeypatch.setattr("ariel.discord_bot.list_jobs", fake_list_jobs)
    bot = _bot()
    interaction = FakeInteraction(guild_id=None)

    asyncio.run(bot._slash_jobs(cast(discord.Interaction, interaction)))

    assert calls == [{"ariel_base_url": "http://127.0.0.1:8000", "ariel_auth_token": None}]
    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == (
        "Recent jobs:\n- job_123: running: Draft release notes"
    )


def test_slash_jobs_rejects_wrong_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_list_jobs(**_: object) -> str:
        raise AssertionError("wrong-user jobs command must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.list_jobs", fail_list_jobs)
    bot = _bot()
    interaction = FakeInteraction(user_id=44)

    asyncio.run(bot._slash_jobs(cast(discord.Interaction, interaction)))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_slash_jobs_rejects_wrong_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_list_jobs(**_: object) -> str:
        raise AssertionError("wrong-guild jobs command must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.list_jobs", fail_list_jobs)
    bot = _bot()
    interaction = FakeInteraction(guild_id=99)

    asyncio.run(bot._slash_jobs(cast(discord.Interaction, interaction)))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_slash_capture_sends_ephemeral_deterministic_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_record_capture(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
        text: str,
        discord_interaction_id: int,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "ariel_auth_token": ariel_auth_token,
                "text": text,
                "discord_interaction_id": discord_interaction_id,
            }
        )
        return "Capture recorded: cpt_123"

    monkeypatch.setattr("ariel.discord_bot.record_capture", fake_record_capture)
    bot = _bot()
    interaction = FakeInteraction()

    asyncio.run(bot._slash_capture(cast(discord.Interaction, interaction), "save this"))

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "ariel_auth_token": None,
            "text": "save this",
            "discord_interaction_id": 987,
        }
    ]
    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == "Capture recorded: cpt_123"
    assert interaction.followup.messages[0]["ephemeral"] is True


def test_slash_capture_allows_configured_user_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_record_capture(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
        text: str,
        discord_interaction_id: int,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "ariel_auth_token": ariel_auth_token,
                "text": text,
                "discord_interaction_id": discord_interaction_id,
            }
        )
        return "Capture recorded: cpt_123"

    monkeypatch.setattr("ariel.discord_bot.record_capture", fake_record_capture)
    bot = _bot()
    interaction = FakeInteraction(guild_id=None)

    asyncio.run(bot._slash_capture(cast(discord.Interaction, interaction), "save this"))

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "ariel_auth_token": None,
            "text": "save this",
            "discord_interaction_id": 987,
        }
    ]
    assert interaction.response.deferrals == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages[0]["content"] == "Capture recorded: cpt_123"


def test_slash_capture_rejects_wrong_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_record_capture(**_: object) -> str:
        raise AssertionError("wrong-user capture command must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.record_capture", fail_record_capture)
    bot = _bot()
    interaction = FakeInteraction(user_id=44)

    asyncio.run(bot._slash_capture(cast(discord.Interaction, interaction), "save this"))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_slash_capture_rejects_wrong_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_record_capture(**_: object) -> str:
        raise AssertionError("wrong-guild capture command must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.record_capture", fail_record_capture)
    bot = _bot()
    interaction = FakeInteraction(guild_id=99)

    asyncio.run(bot._slash_capture(cast(discord.Interaction, interaction), "save this"))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


def test_on_interaction_ignores_non_ariel_component_custom_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("non-Ariel component custom id must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id="external:approval:approve:apr_123")

    _send_interaction(bot, interaction)

    assert interaction.response.messages == []
    assert interaction.response.edits == []
    assert interaction.followup.messages == []


@pytest.mark.parametrize("data", [{}, {"custom_id": 123}])
def test_on_interaction_ignores_missing_or_non_string_custom_id(
    monkeypatch: pytest.MonkeyPatch,
    data: dict[str, Any],
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("missing/non-string custom id must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction()
    interaction.data = data

    _send_interaction(bot, interaction)

    assert interaction.response.messages == []
    assert interaction.response.edits == []
    assert interaction.followup.messages == []


def test_on_interaction_ignores_already_acknowledged_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("already-acknowledged interaction must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id="ariel:approval:approve:apr_123")
    interaction.response._done = True

    _send_interaction(bot, interaction)

    assert interaction.response.messages == []
    assert interaction.response.edits == []
    assert interaction.followup.messages == []


def test_on_interaction_handles_approval_custom_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_decide_approval(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
        approval_ref: str,
        decision: str,
        reason: str | None = None,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "ariel_auth_token": ariel_auth_token,
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
            "ariel_auth_token": None,
            "approval_ref": "apr_123",
            "decision": "approve",
            "reason": None,
        }
    ]
    assert interaction.response.edits[0]["content"] == "Approval approved: apr_123"
    assert interaction.response.edits[0]["view"] is None


def test_on_interaction_handles_approval_deny_custom_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_decide_approval(
        *,
        ariel_base_url: str,
        ariel_auth_token: str | None = None,
        approval_ref: str,
        decision: str,
        reason: str | None = None,
    ) -> str:
        calls.append(
            {
                "ariel_base_url": ariel_base_url,
                "ariel_auth_token": ariel_auth_token,
                "approval_ref": approval_ref,
                "decision": decision,
                "reason": reason,
            }
        )
        return "Approval denied: apr_123"

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fake_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id="ariel:approval:deny:apr_123", channel_id=88)

    _send_interaction(bot, interaction)

    assert calls == [
        {
            "ariel_base_url": "http://127.0.0.1:8000",
            "ariel_auth_token": None,
            "approval_ref": "apr_123",
            "decision": "deny",
            "reason": None,
        }
    ]
    assert interaction.response.edits[0]["content"] == "Approval denied: apr_123"
    assert interaction.response.edits[0]["view"] is None


def test_on_interaction_duplicate_approval_click_surfaces_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_decide_approval(**_: object) -> str:
        raise ArielDiscordError("approval is not pending")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fake_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id="ariel:approval:deny:apr_123", channel_id=88)

    _send_interaction(bot, interaction)

    assert interaction.response.edits[0]["content"] == (
        "Ariel request failed: approval is not pending"
    )
    assert interaction.response.edits[0]["view"] is None


@pytest.mark.parametrize(
    "custom_id",
    [
        "ariel:approval:approve:",
        "ariel:approval:approve:   ",
        "ariel:approval:deny:",
        "ariel:approval:deny:\t",
        "ariel:approval:maybe:apr_123",
    ],
)
def test_on_interaction_rejects_invalid_approval_custom_id(
    monkeypatch: pytest.MonkeyPatch,
    custom_id: str,
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("invalid approval custom id must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id=custom_id, channel_id=88)

    _send_interaction(bot, interaction)

    assert interaction.response.messages[0]["content"] == (
        "Ariel action failed: invalid Discord action id."
    )
    assert interaction.response.messages[0]["ephemeral"] is True
    assert isinstance(interaction.response.messages[0]["allowed_mentions"], discord.AllowedMentions)


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_on_interaction_rejects_wrong_user(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("wrong-user approval click must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(
        custom_id=f"ariel:approval:{decision}:apr_123",
        user_id=44,
    )

    _send_interaction(bot, interaction)

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_on_interaction_rejects_wrong_guild(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("wrong-guild approval click must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(
        custom_id=f"ariel:approval:{decision}:apr_123",
        guild_id=99,
    )

    _send_interaction(bot, interaction)

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "limited to the configured Discord user" in interaction.response.messages[0]["content"]


@pytest.mark.parametrize(
    ("custom_id", "kwargs"),
    [
        ("ariel:approval:maybe:apr_123", {"user_id": 44}),
        ("ariel:approval:maybe:apr_123", {"guild_id": 99}),
    ],
)
def test_on_interaction_authorizes_before_parsing_malformed_ariel_custom_id(
    monkeypatch: pytest.MonkeyPatch,
    custom_id: str,
    kwargs: dict[str, int],
) -> None:
    def fail_decide_approval(**_: object) -> str:
        raise AssertionError("unauthorized malformed custom id must not call Ariel API")

    monkeypatch.setattr("ariel.discord_bot.decide_approval", fail_decide_approval)
    bot = _bot()
    interaction = FakeInteraction(custom_id=custom_id, **kwargs)

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


def test_submit_discord_turn_raises_on_message_post_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_client(*, timeout: float) -> FakeHttpClient:
        assert timeout == 60.0
        return FakeHttpClient(
            responses=[
                httpx.Response(
                    422,
                    json={
                        "error": {
                            "code": "E_VALIDATION",
                            "message": "invalid message payload",
                        }
                    },
                ),
            ]
        )

    monkeypatch.setattr("ariel.discord_bot.httpx.Client", fake_client)

    with pytest.raises(ArielDiscordError, match="invalid message payload"):
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
    assert calls[0]["discord_context"] == {
        "guild_id": None,
        "channel_id": 77,
        "message_id": 321,
        "author_id": 3,
        "mentioned_bot": False,
    }
    assert message.replies == []
    assert channel.events == []


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
    assert message.replies == []


def test_on_message_answers_owner_dm_attachment_read_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=457,
        content="read this attachment",
        guild=None,
        channel=FakeChannel(channel_id=77),
        attachments=[
            FakeAttachment(
                attachment_id=778,
                filename="report.pdf",
                content_type="application/pdf",
                size=2048,
                url="https://cdn.example.test/report.pdf",
            )
        ],
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "read this attachment"
    assert calls[0]["discord_context"] == {
        "guild_id": None,
        "channel_id": 77,
        "message_id": 457,
        "author_id": 3,
        "mentioned_bot": False,
        "attachments": [
            {
                "source": "discord",
                "source_attachment_id": 778,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 2048,
                "attachment_ref": "discord:778",
                "download_url": "https://cdn.example.test/report.pdf",
            }
        ],
    }
    assert calls[0]["discord_message_id"] == 457
    assert message.replies == []


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
    assert message.replies == []


def test_on_message_answers_attachment_only_owner_dm_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        message_id=655,
        content="",
        guild=None,
        channel=FakeChannel(channel_id=77),
        attachments=[FakeAttachment(filename="photo.png", content_type="image/png")],
    )

    _send_message(bot, message)

    assert calls[0]["prompt"] == "What would you like me to do with the attachment(s)?"
    assert calls[0]["discord_context"] == {
        "guild_id": None,
        "channel_id": 77,
        "message_id": 655,
        "author_id": 3,
        "mentioned_bot": False,
        "attachments": [
            {
                "source": "discord",
                "source_attachment_id": 555,
                "filename": "photo.png",
                "content_type": "image/png",
                "size_bytes": 12,
                "attachment_ref": "discord:555",
                "download_url": "https://cdn.example.test/notes.txt",
            }
        ],
    }
    assert "Uploaded attachment(s)." not in calls[0]["prompt"]
    assert message.replies == []


def test_on_message_forwards_approval_decision_text_as_prompt(
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
    assert message.replies == []


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


def test_on_message_is_fire_and_forget_never_posts_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_submit_ambient_turn(
        *,
        prompt: str,
        discord_message_id: int,
        discord_context: dict[str, Any] | None = None,
    ) -> None:
        calls.append(
            {
                "prompt": prompt,
                "discord_message_id": discord_message_id,
                "discord_context": discord_context,
            }
        )

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
    assert channel.events == []


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


def test_on_message_ignores_non_owner_home_guild_ambient_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="ambient from someone else",
        author=FakeUser(user_id=44),
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=88),
    )

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []


def test_on_message_ignores_non_owner_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="ambient from someone else",
        author=FakeUser(user_id=44),
        guild=None,
        channel=FakeChannel(channel_id=77),
    )

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []


def test_on_message_ignores_self_authored_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="message from the bot account",
        author=FakeUser(user_id=999, bot=False),
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=88),
    )

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []


def test_on_message_ignores_mention_only_owner_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_discord_turn(monkeypatch)
    bot = _bot()
    message = FakeDiscordMessage(
        content="<@999>   ",
        author=FakeUser(user_id=3),
        guild=FakeGuild(guild_id=1),
        channel=FakeChannel(channel_id=88),
        mentions=[FakeUser(user_id=999, bot=True)],
    )

    _send_message(bot, message)

    assert calls == []
    assert message.replies == []


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
