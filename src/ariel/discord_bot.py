from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
import httpx

from .config import AppSettings


class DiscordBotConfigError(Exception):
    pass


class ArielDiscordError(Exception):
    pass


_APPROVAL_CUSTOM_ID_PREFIX = "ariel:approval:"
_JOB_REFRESH_CUSTOM_ID_PREFIX = "ariel:job:refresh:"
_NOTIFICATION_ACK_CUSTOM_ID_PREFIX = "ariel:notification:ack:"
_ATTENTION_ACK_CUSTOM_ID_PREFIX = "ariel:attention:ack:"
_ATTENTION_SNOOZE_CUSTOM_ID_PREFIX = "ariel:attention:snooze:"
_ATTENTION_RESOLVE_CUSTOM_ID_PREFIX = "ariel:attention:resolve:"
_ATTENTION_REFRESH_CUSTOM_ID_PREFIX = "ariel:attention:refresh:"


@dataclass(frozen=True, slots=True)
class ArielDiscordReply:
    content: str
    view: discord.ui.View | None = None
    silent: bool = False


def format_discord_message(message: str) -> str:
    normalized = message.strip() or "(empty Ariel response)"
    if len(normalized) <= 1900:
        return normalized
    return f"{normalized[:1886].rstrip()}\n[truncated]"


def submit_discord_turn(
    *,
    ariel_base_url: str,
    prompt: str,
    discord_message_id: int,
    allowed_user_id: int | None = None,
    discord_context: dict[str, Any] | None = None,
) -> ArielDiscordReply:
    with httpx.Client(timeout=60.0) as client:
        session_response = client.get(f"{ariel_base_url}/v1/sessions/active")
        session_payload = _json_response_payload(session_response)
        if session_response.status_code >= 400 or session_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(session_payload))

        session = session_payload.get("session")
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise ArielDiscordError("Ariel returned an invalid active session response.")

        request_payload: dict[str, Any] = {"message": prompt}
        if discord_context is not None:
            request_payload["discord"] = discord_context
        message_response = client.post(
            f"{ariel_base_url}/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": f"discord-message-{discord_message_id}"},
            json=request_payload,
        )
        message_payload = _json_response_payload(message_response)
        if message_response.status_code >= 400 or message_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(message_payload))

        return _message_reply_for_discord(
            payload=message_payload,
            ariel_base_url=ariel_base_url,
            allowed_user_id=allowed_user_id,
        )


def get_status(
    *,
    ariel_base_url: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        health_response = client.get(f"{ariel_base_url}/v1/health")
        health_payload = _json_response_payload(health_response)
        if health_response.status_code >= 400 or health_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(health_payload))

        session_response = client.get(f"{ariel_base_url}/v1/sessions/active")
        session_payload = _json_response_payload(session_response)
        if session_response.status_code >= 400 or session_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(session_payload))

        jobs_response = client.get(f"{ariel_base_url}/v1/jobs?limit=5")
        jobs_payload = _json_response_payload(jobs_response)
        if jobs_response.status_code >= 400 or jobs_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(jobs_payload))

        notifications_response = client.get(f"{ariel_base_url}/v1/notifications?limit=5")
        notifications_payload = _json_response_payload(notifications_response)
        if notifications_response.status_code >= 400 or notifications_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(notifications_payload))

    session = session_payload.get("session")
    if not isinstance(session, dict):
        raise ArielDiscordError("Ariel returned an invalid active session response.")

    jobs = jobs_payload.get("jobs")
    notifications = notifications_payload.get("notifications")
    return _format_status_for_discord(
        session=session,
        jobs=jobs if isinstance(jobs, list) else [],
        notifications=notifications if isinstance(notifications, list) else [],
    )


def list_jobs(
    *,
    ariel_base_url: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        response = client.get(f"{ariel_base_url}/v1/jobs?limit=10")
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ArielDiscordError("Ariel returned an invalid jobs response.")
    return _format_jobs_for_discord(jobs)


def list_memory(
    *,
    ariel_base_url: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        response = client.get(f"{ariel_base_url}/v1/memory")
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    assertions = payload.get("assertions")
    if not isinstance(assertions, list):
        raise ArielDiscordError("Ariel returned an invalid memory response.")
    return _format_memory_for_discord(assertions)


def record_capture(
    *,
    ariel_base_url: str,
    text: str,
    discord_interaction_id: int,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{ariel_base_url}/v1/captures/record",
            headers={"Idempotency-Key": f"discord-capture-{discord_interaction_id}"},
            json={"kind": "text", "text": text},
        )
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    capture = payload.get("capture")
    if not isinstance(capture, dict):
        raise ArielDiscordError("Ariel returned an invalid capture response.")
    capture_id = capture.get("id")
    terminal_state = capture.get("terminal_state")
    if not isinstance(capture_id, str) or not isinstance(terminal_state, str):
        raise ArielDiscordError("Ariel returned an invalid capture response.")
    return f"Capture recorded: {capture_id} ({terminal_state})"


def decide_approval(
    *,
    ariel_base_url: str,
    approval_ref: str,
    decision: str,
    reason: str | None = None,
) -> str:
    payload: dict[str, str] = {"approval_ref": approval_ref, "decision": decision}
    if reason:
        payload["reason"] = reason
    with httpx.Client(timeout=60.0) as client:
        response = client.post(f"{ariel_base_url}/v1/approvals", json=payload)
        response_payload = _json_response_payload(response)
        if response.status_code >= 400 or response_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(response_payload))
        return _format_approval_response_for_discord(response_payload)


def refresh_job(
    *,
    ariel_base_url: str,
    job_id: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        job_response = client.get(f"{ariel_base_url}/v1/jobs/{job_id}")
        job_payload = _json_response_payload(job_response)
        if job_response.status_code >= 400 or job_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(job_payload))

        events_response = client.get(f"{ariel_base_url}/v1/jobs/{job_id}/events")
        events_payload = _json_response_payload(events_response)
        if events_response.status_code >= 400 or events_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(events_payload))

    return _format_job_response_for_discord(job_payload, events_payload)


def ack_notification(
    *,
    ariel_base_url: str,
    notification_id: str,
) -> tuple[str, str | None]:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(f"{ariel_base_url}/v1/notifications/{notification_id}/ack", json={})
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    notification = payload.get("notification")
    if not isinstance(notification, dict):
        raise ArielDiscordError("Ariel returned an invalid notification response.")
    return _format_notification_ack_for_discord(notification), _notification_job_id(notification)


def ack_attention_item(
    *,
    ariel_base_url: str,
    attention_item_id: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{ariel_base_url}/v1/attention-items/{attention_item_id}/ack",
            json={},
        )
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    return _format_attention_item_action_for_discord(payload, action="acknowledged")


def snooze_attention_item(
    *,
    ariel_base_url: str,
    attention_item_id: str,
) -> str:
    snooze_until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{ariel_base_url}/v1/attention-items/{attention_item_id}/snooze",
            json={"snooze_until": snooze_until},
        )
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    return _format_attention_item_action_for_discord(payload, action="snoozed")


def resolve_attention_item(
    *,
    ariel_base_url: str,
    attention_item_id: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{ariel_base_url}/v1/attention-items/{attention_item_id}/resolve",
            json={},
        )
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    return _format_attention_item_action_for_discord(payload, action="resolved")


def refresh_attention_item(
    *,
    ariel_base_url: str,
    attention_item_id: str,
) -> str:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{ariel_base_url}/v1/attention-items/{attention_item_id}/refresh",
            json={},
        )
        payload = _json_response_payload(response)
        if response.status_code >= 400 or payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(payload))
    return _format_attention_item_action_for_discord(payload, action="refreshed")


class ArielDiscordBot(commands.Bot):
    def __init__(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        ariel_base_url: str,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.ariel_guild_id = guild_id
        self.ariel_channel_id = channel_id
        self.ariel_user_id = user_id
        self.ariel_base_url = ariel_base_url
        self.tree.add_command(
            app_commands.Command(
                name="status",
                description="Show Ariel runtime status.",
                callback=self._slash_status,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="jobs",
                description="List recent Ariel jobs.",
                callback=self._slash_jobs,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="memory",
                description="List Ariel memory.",
                callback=self._slash_memory,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="capture",
                description="Record a text capture without invoking the assistant.",
                callback=self._slash_capture,
            )
        )

    async def setup_hook(self) -> None:
        self.tree.copy_global_to(guild=discord.Object(id=self.ariel_guild_id))
        await self.tree.sync(guild=discord.Object(id=self.ariel_guild_id))

    async def on_ready(self) -> None:
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="ambient messages",
            ),
        )

    async def _slash_status(self, interaction: discord.Interaction) -> None:
        if not self._interaction_is_allowed(interaction):
            await interaction.response.send_message(
                "This Ariel command is limited to the configured Discord user and home server.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        content = await self._run_discord_ops_command(get_status)
        await interaction.followup.send(
            format_discord_message(content),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _slash_jobs(self, interaction: discord.Interaction) -> None:
        if not self._interaction_is_allowed(interaction):
            await interaction.response.send_message(
                "This Ariel command is limited to the configured Discord user and home server.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        content = await self._run_discord_ops_command(list_jobs)
        await interaction.followup.send(
            format_discord_message(content),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _slash_memory(self, interaction: discord.Interaction) -> None:
        if not self._interaction_is_allowed(interaction):
            await interaction.response.send_message(
                "This Ariel command is limited to the configured Discord user and home server.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        content = await self._run_discord_ops_command(list_memory)
        await interaction.followup.send(
            format_discord_message(content),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _slash_capture(self, interaction: discord.Interaction, text: str) -> None:
        if not self._interaction_is_allowed(interaction):
            await interaction.response.send_message(
                "This Ariel command is limited to the configured Discord user and home server.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            content = await asyncio.to_thread(
                record_capture,
                ariel_base_url=self.ariel_base_url,
                text=text,
                discord_interaction_id=interaction.id,
            )
        except ArielDiscordError as exc:
            content = f"Ariel request failed: {exc}"
        except httpx.HTTPError:
            content = "Ariel request failed: could not reach the local Ariel API."
        await interaction.followup.send(
            format_discord_message(content),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not isinstance(custom_id, str) or not _is_ariel_custom_id(custom_id):
            return
        if interaction.response.is_done():
            return
        if not self._interaction_is_allowed(interaction):
            await interaction.response.send_message(
                "This Ariel action is limited to the configured Discord user and home server.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._handle_custom_id_interaction(interaction, custom_id)

    async def on_message(self, message: discord.Message) -> None:
        bot_user_id = self.user.id if self.user is not None else None
        if message.author.bot or message.author.id == bot_user_id:
            return
        if message.author.id != self.ariel_user_id:
            return
        if message.type != discord.MessageType.default:
            return

        attachments = getattr(message, "attachments", None) or []
        prompt = message.content.strip()
        if not prompt and not attachments:
            return

        guild_id = message.guild.id if message.guild is not None else None
        if guild_id is not None and guild_id != self.ariel_guild_id:
            return

        if bot_user_id is not None and any(
            mention.id == bot_user_id for mention in message.mentions
        ):
            prompt = (
                prompt.replace(f"<@{bot_user_id}>", "").replace(f"<@!{bot_user_id}>", "").strip()
            )
            if not prompt and not attachments:
                return

        if not prompt:
            prompt = "What would you like me to do with the attachment(s)?"

        discord_context = _discord_context_for_message(message, bot_user_id=bot_user_id)

        async with message.channel.typing():
            reply = await self._submit_ambient_turn(
                prompt=prompt,
                discord_message_id=message.id,
                discord_context=discord_context,
            )

        if reply.silent:
            return

        if reply.view is None:
            await message.reply(
                format_discord_message(reply.content),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.reply(
                format_discord_message(reply.content),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
                view=reply.view,
            )

    async def _submit_ambient_turn(
        self,
        *,
        prompt: str,
        discord_message_id: int,
        discord_context: dict[str, Any] | None = None,
    ) -> ArielDiscordReply:
        try:
            return await asyncio.to_thread(
                submit_discord_turn,
                ariel_base_url=self.ariel_base_url,
                prompt=prompt,
                discord_message_id=discord_message_id,
                allowed_user_id=self.ariel_user_id,
                discord_context=discord_context,
            )
        except ArielDiscordError as exc:
            return ArielDiscordReply(content=f"Ariel request failed: {exc}")
        except httpx.HTTPError:
            return ArielDiscordReply(
                content="Ariel request failed: could not reach the local Ariel API."
            )

    async def _run_discord_ops_command(
        self,
        command: Any,
    ) -> str:
        try:
            return await asyncio.to_thread(command, ariel_base_url=self.ariel_base_url)
        except ArielDiscordError as exc:
            return f"Ariel request failed: {exc}"
        except httpx.HTTPError:
            return "Ariel request failed: could not reach the local Ariel API."

    def _interaction_is_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ariel_user_id:
            return False
        guild_id = interaction.guild.id if interaction.guild is not None else None
        if guild_id is None:
            return True
        return guild_id == self.ariel_guild_id

    async def _handle_custom_id_interaction(
        self,
        interaction: discord.Interaction,
        custom_id: str,
    ) -> None:
        if custom_id.startswith(_APPROVAL_CUSTOM_ID_PREFIX):
            decision_and_ref = custom_id.removeprefix(_APPROVAL_CUSTOM_ID_PREFIX)
            decision, separator, approval_ref = decision_and_ref.partition(":")
            if separator and decision in {"approve", "deny"} and approval_ref:
                await _edit_with_approval_decision(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    approval_ref=approval_ref,
                    decision=decision,
                )
                return
        elif custom_id.startswith(_JOB_REFRESH_CUSTOM_ID_PREFIX):
            job_id = custom_id.removeprefix(_JOB_REFRESH_CUSTOM_ID_PREFIX)
            if job_id:
                await _edit_with_job_refresh(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    job_id=job_id,
                    allowed_user_id=self.ariel_user_id,
                )
                return
        elif custom_id.startswith(_NOTIFICATION_ACK_CUSTOM_ID_PREFIX):
            notification_id = custom_id.removeprefix(_NOTIFICATION_ACK_CUSTOM_ID_PREFIX)
            if notification_id:
                await _edit_with_notification_ack(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    notification_id=notification_id,
                    allowed_user_id=self.ariel_user_id,
                )
                return
        elif custom_id.startswith(_ATTENTION_ACK_CUSTOM_ID_PREFIX):
            attention_item_id = custom_id.removeprefix(_ATTENTION_ACK_CUSTOM_ID_PREFIX)
            if attention_item_id:
                await _edit_with_attention_ack(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    attention_item_id=attention_item_id,
                )
                return
        elif custom_id.startswith(_ATTENTION_SNOOZE_CUSTOM_ID_PREFIX):
            attention_item_id = custom_id.removeprefix(_ATTENTION_SNOOZE_CUSTOM_ID_PREFIX)
            if attention_item_id:
                await _edit_with_attention_snooze(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    attention_item_id=attention_item_id,
                )
                return
        elif custom_id.startswith(_ATTENTION_RESOLVE_CUSTOM_ID_PREFIX):
            attention_item_id = custom_id.removeprefix(_ATTENTION_RESOLVE_CUSTOM_ID_PREFIX)
            if attention_item_id:
                await _edit_with_attention_resolve(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    attention_item_id=attention_item_id,
                )
                return
        elif custom_id.startswith(_ATTENTION_REFRESH_CUSTOM_ID_PREFIX):
            attention_item_id = custom_id.removeprefix(_ATTENTION_REFRESH_CUSTOM_ID_PREFIX)
            if attention_item_id:
                await _edit_with_attention_refresh(
                    interaction=interaction,
                    ariel_base_url=self.ariel_base_url,
                    attention_item_id=attention_item_id,
                    allowed_user_id=self.ariel_user_id,
                )
                return
        await interaction.response.send_message(
            "Ariel action failed: invalid Discord action id.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def create_discord_bot(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    ariel_base_url: str,
) -> ArielDiscordBot:
    return ArielDiscordBot(
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        ariel_base_url=ariel_base_url,
    )


def configured_discord_bot(settings: AppSettings) -> ArielDiscordBot:
    missing = []
    discord_bot_token = settings.discord_bot_token
    discord_guild_id = settings.discord_guild_id
    discord_channel_id = settings.discord_channel_id
    discord_user_id = settings.discord_user_id
    if discord_bot_token is None:
        missing.append("ARIEL_DISCORD_BOT_TOKEN")
    if discord_guild_id is None:
        missing.append("ARIEL_DISCORD_GUILD_ID")
    if discord_channel_id is None:
        missing.append("ARIEL_DISCORD_CHANNEL_ID")
    if discord_user_id is None:
        missing.append("ARIEL_DISCORD_USER_ID")
    if missing:
        raise DiscordBotConfigError(f"missing Discord configuration: {', '.join(missing)}")

    assert discord_guild_id is not None
    assert discord_channel_id is not None
    assert discord_user_id is not None
    return create_discord_bot(
        guild_id=discord_guild_id,
        channel_id=discord_channel_id,
        user_id=discord_user_id,
        ariel_base_url=settings.discord_ariel_base_url,
    )


def _json_response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ArielDiscordError("Ariel returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise ArielDiscordError("Ariel returned an invalid JSON response.")
    return payload


def _safe_ariel_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "Ariel API request failed."


def _message_reply_for_discord(
    *,
    payload: dict[str, Any],
    ariel_base_url: str,
    allowed_user_id: int | None = None,
) -> ArielDiscordReply:
    assistant = payload.get("assistant")
    if isinstance(assistant, dict) and assistant.get("silent") is True:
        return ArielDiscordReply(content="", silent=True)
    content = _format_message_response_for_discord(payload)
    pending_approvals = _pending_approval_refs(payload)
    if not pending_approvals:
        return ArielDiscordReply(content=content)
    return ArielDiscordReply(
        content=content,
        view=ArielActionView(
            ariel_base_url=ariel_base_url,
            approval_refs=pending_approvals,
            allowed_user_id=allowed_user_id,
        ),
    )


def _format_message_response_for_discord(payload: dict[str, Any]) -> str:
    assistant = payload.get("assistant")
    assistant_message = assistant.get("message") if isinstance(assistant, dict) else None
    if not isinstance(assistant_message, str):
        raise ArielDiscordError("Ariel returned an invalid assistant response.")

    pending_approvals = _pending_approval_lines(payload)
    if not pending_approvals:
        return assistant_message
    return "\n".join([assistant_message, "", *pending_approvals])


def _format_approval_response_for_discord(payload: dict[str, Any]) -> str:
    approval = payload.get("approval")
    approval_status = approval.get("status") if isinstance(approval, dict) else None
    approval_ref = approval.get("reference") if isinstance(approval, dict) else None
    assistant = payload.get("assistant")
    assistant_message = assistant.get("message") if isinstance(assistant, dict) else None

    lines = []
    if isinstance(approval_status, str) and isinstance(approval_ref, str):
        lines.append(f"Approval {approval_status}: {approval_ref}")
    if isinstance(assistant_message, str) and assistant_message.strip():
        lines.append(assistant_message.strip())
    if not lines:
        raise ArielDiscordError("Ariel returned an invalid approval response.")
    return "\n".join(lines)


def _pending_approval_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for pending in _pending_approval_items(payload):
        expires_at = pending.get("expires_at")
        suffix = f" expires_at={expires_at}" if isinstance(expires_at, str) else ""
        lines.append(
            f"Approval pending ({pending['capability_id']}): {pending['approval_ref']}{suffix}. "
            "Use the buttons below."
        )
    return lines


def _pending_approval_refs(payload: dict[str, Any]) -> list[str]:
    return [item["approval_ref"] for item in _pending_approval_items(payload)]


def _pending_approval_items(payload: dict[str, Any]) -> list[dict[str, str]]:
    turn = payload.get("turn")
    lifecycle = turn.get("surface_action_lifecycle") if isinstance(turn, dict) else None
    if not isinstance(lifecycle, list):
        return []

    items: list[dict[str, str]] = []
    for item in lifecycle:
        if not isinstance(item, dict):
            continue
        approval = item.get("approval")
        if not isinstance(approval, dict) or approval.get("status") != "pending":
            continue
        approval_ref = approval.get("reference")
        if not isinstance(approval_ref, str) or not approval_ref:
            continue
        proposal = item.get("proposal")
        capability_id = "action"
        if isinstance(proposal, dict):
            capability_id_raw = proposal.get("capability_id")
            if isinstance(capability_id_raw, str):
                capability_id = capability_id_raw
        pending: dict[str, str] = {
            "approval_ref": approval_ref,
            "capability_id": capability_id,
        }
        expires_at = approval.get("expires_at")
        if isinstance(expires_at, str):
            pending["expires_at"] = expires_at
        items.append(pending)
    return items


def _format_job_response_for_discord(
    job_payload: dict[str, Any],
    events_payload: dict[str, Any],
) -> str:
    job = job_payload.get("job")
    if not isinstance(job, dict):
        raise ArielDiscordError("Ariel returned an invalid job response.")

    job_id = job.get("id")
    status = job.get("status")
    title = job.get("title") or job.get("external_job_id")
    if not all(isinstance(value, str) and value for value in (job_id, status, title)):
        raise ArielDiscordError("Ariel returned an invalid job response.")

    lines = [f"Job {job_id}: {status}", str(title)]
    summary = job.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())

    events = events_payload.get("events")
    if isinstance(events, list) and events:
        lines.append("")
        lines.append("Recent events:")
        for event in events[-5:]:
            if not isinstance(event, dict):
                continue
            event_type = event.get("event_type")
            created_at = event.get("created_at")
            if isinstance(event_type, str):
                timestamp = f" at {created_at}" if isinstance(created_at, str) else ""
                lines.append(f"- {event_type}{timestamp}")
    return "\n".join(lines)


def _format_notification_ack_for_discord(notification: dict[str, Any]) -> str:
    notification_id = notification.get("id")
    title = notification.get("title")
    if not isinstance(notification_id, str) or not isinstance(title, str):
        raise ArielDiscordError("Ariel returned an invalid notification response.")
    return f"Notification acknowledged: {title} ({notification_id})"


def _format_attention_item_action_for_discord(payload: dict[str, Any], *, action: str) -> str:
    attention_item = payload.get("attention_item")
    if not isinstance(attention_item, dict):
        raise ArielDiscordError("Ariel returned an invalid attention item response.")
    attention_item_id = attention_item.get("id")
    if not isinstance(attention_item_id, str) or not attention_item_id:
        raise ArielDiscordError("Ariel returned an invalid attention item response.")
    title = attention_item.get("title")
    if not isinstance(title, str) or not title.strip():
        title = attention_item.get("reason")
    if isinstance(title, str) and title.strip():
        return f"Attention item {action}: {title.strip()} ({attention_item_id})"
    return f"Attention item {action}: {attention_item_id}"


def _format_status_for_discord(
    *,
    session: dict[str, Any],
    jobs: list[Any],
    notifications: list[Any],
) -> str:
    session_id = session.get("id")
    session_state = session.get("lifecycle_state")
    if not isinstance(session_id, str) or not isinstance(session_state, str):
        raise ArielDiscordError("Ariel returned an invalid active session response.")

    running_jobs = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("status") in {"queued", "running", "waiting_approval"}:
            running_jobs += 1

    pending_notifications = 0
    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        if notification.get("status") in {"pending", "delivered", "failed"}:
            pending_notifications += 1

    return "\n".join(
        [
            "Ariel status: ok",
            f"Active session: {session_id} ({session_state})",
            f"Recent jobs: {len(jobs)} total, {running_jobs} active",
            f"Notifications needing attention: {pending_notifications}",
        ]
    )


def _format_jobs_for_discord(jobs: list[Any]) -> str:
    if not jobs:
        return "No recent jobs."

    lines = ["Recent jobs:"]
    for job in jobs[:10]:
        if not isinstance(job, dict):
            continue
        job_id = job.get("id")
        status = job.get("status")
        title = job.get("title") or job.get("external_job_id")
        if isinstance(job_id, str) and isinstance(status, str) and isinstance(title, str):
            lines.append(f"- {job_id}: {status}: {title}")
    if len(lines) == 1:
        raise ArielDiscordError("Ariel returned an invalid jobs response.")
    return "\n".join(lines)


def _format_memory_for_discord(items: list[Any]) -> str:
    active_items = []
    for item in items:
        if not isinstance(item, dict) or item.get("lifecycle_state") != "active":
            continue
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        value = item.get("value")
        if (
            isinstance(subject_key, str)
            and isinstance(predicate, str)
            and isinstance(value, str)
            and value.strip()
        ):
            active_items.append((f"{subject_key} {predicate}", value.strip()))

    if not active_items:
        return "No active memory."

    lines = ["Active memory:"]
    for assertion_label, value in active_items[:10]:
        lines.append(f"- {assertion_label}: {value}")
    return "\n".join(lines)


def _notification_job_id(notification: dict[str, Any]) -> str | None:
    payload = notification.get("payload")
    job_id = payload.get("job_id") if isinstance(payload, dict) else None
    return job_id if isinstance(job_id, str) and job_id else None


def _approval_custom_id(decision: str, approval_ref: str) -> str:
    return f"{_APPROVAL_CUSTOM_ID_PREFIX}{decision}:{approval_ref}"


def _job_refresh_custom_id(job_id: str) -> str:
    return f"{_JOB_REFRESH_CUSTOM_ID_PREFIX}{job_id}"


def _notification_ack_custom_id(notification_id: str) -> str:
    return f"{_NOTIFICATION_ACK_CUSTOM_ID_PREFIX}{notification_id}"


def _attention_ack_custom_id(attention_item_id: str) -> str:
    return f"{_ATTENTION_ACK_CUSTOM_ID_PREFIX}{attention_item_id}"


def _attention_snooze_custom_id(attention_item_id: str) -> str:
    return f"{_ATTENTION_SNOOZE_CUSTOM_ID_PREFIX}{attention_item_id}"


def _attention_resolve_custom_id(attention_item_id: str) -> str:
    return f"{_ATTENTION_RESOLVE_CUSTOM_ID_PREFIX}{attention_item_id}"


def _attention_refresh_custom_id(attention_item_id: str) -> str:
    return f"{_ATTENTION_REFRESH_CUSTOM_ID_PREFIX}{attention_item_id}"


def _is_ariel_custom_id(custom_id: str) -> bool:
    return custom_id.startswith(
        (
            _APPROVAL_CUSTOM_ID_PREFIX,
            _JOB_REFRESH_CUSTOM_ID_PREFIX,
            _NOTIFICATION_ACK_CUSTOM_ID_PREFIX,
            _ATTENTION_ACK_CUSTOM_ID_PREFIX,
            _ATTENTION_SNOOZE_CUSTOM_ID_PREFIX,
            _ATTENTION_RESOLVE_CUSTOM_ID_PREFIX,
            _ATTENTION_REFRESH_CUSTOM_ID_PREFIX,
        )
    )


def _discord_context_for_message(
    message: discord.Message,
    *,
    bot_user_id: int | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "guild_id": message.guild.id if message.guild is not None else None,
        "channel_id": message.channel.id,
        "message_id": message.id,
        "author_id": message.author.id,
        "mentioned_bot": (
            bot_user_id is not None
            and any(mention.id == bot_user_id for mention in message.mentions)
        ),
    }
    guild_name = getattr(message.guild, "name", None)
    if isinstance(guild_name, str):
        context["guild_name"] = guild_name
    channel_name = getattr(message.channel, "name", None)
    if isinstance(channel_name, str):
        context["channel_name"] = channel_name
    channel_type = getattr(message.channel, "type", None)
    if channel_type is not None:
        context["channel_type"] = str(channel_type)
    jump_url = getattr(message, "jump_url", None)
    if isinstance(jump_url, str):
        context["message_url"] = jump_url
    if message.reference is not None and message.reference.message_id is not None:
        context["reply_to_message_id"] = message.reference.message_id
    parent_channel_id = getattr(message.channel, "parent_id", None)
    if parent_channel_id is not None:
        context["thread_id"] = message.channel.id
        context["parent_channel_id"] = parent_channel_id
        if isinstance(channel_name, str):
            context["thread_name"] = channel_name
        parent = getattr(message.channel, "parent", None)
        parent_name = getattr(parent, "name", None)
        if isinstance(parent_name, str):
            context["parent_channel_name"] = parent_name
    attachments = getattr(message, "attachments", None) or []
    if attachments:
        attachment_context: list[dict[str, Any]] = []
        for attachment in attachments:
            source_attachment_id = getattr(attachment, "id", None)
            filename = getattr(attachment, "filename", None)
            download_url = getattr(attachment, "url", None)
            if (
                not isinstance(source_attachment_id, int)
                or not isinstance(filename, str)
                or not isinstance(download_url, str)
            ):
                continue
            attachment_context.append(
                {
                    "source": "discord",
                    "source_attachment_id": source_attachment_id,
                    "filename": filename,
                    "content_type": getattr(attachment, "content_type", None),
                    "size_bytes": getattr(attachment, "size", None),
                    "attachment_ref": f"discord:{source_attachment_id}",
                    "download_url": download_url,
                }
            )
        if attachment_context:
            context["attachments"] = attachment_context
    return context


async def _edit_with_approval_decision(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    approval_ref: str,
    decision: str,
) -> None:
    try:
        content = await asyncio.to_thread(
            decide_approval,
            ariel_base_url=ariel_base_url,
            approval_ref=approval_ref,
            decision=decision,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_with_job_refresh(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    job_id: str,
    allowed_user_id: int | None,
) -> None:
    try:
        content = await asyncio.to_thread(
            refresh_job,
            ariel_base_url=ariel_base_url,
            job_id=job_id,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=ArielActionView(
            ariel_base_url=ariel_base_url,
            job_id=job_id,
            allowed_user_id=allowed_user_id,
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_with_notification_ack(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    notification_id: str,
    allowed_user_id: int | None,
) -> None:
    try:
        content, job_id = await asyncio.to_thread(
            ack_notification,
            ariel_base_url=ariel_base_url,
            notification_id=notification_id,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
        job_id = None
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
        job_id = None
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=(
            ArielActionView(
                ariel_base_url=ariel_base_url,
                job_id=job_id,
                allowed_user_id=allowed_user_id,
            )
            if job_id is not None
            else None
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_with_attention_ack(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    attention_item_id: str,
) -> None:
    try:
        content = await asyncio.to_thread(
            ack_attention_item,
            ariel_base_url=ariel_base_url,
            attention_item_id=attention_item_id,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_with_attention_snooze(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    attention_item_id: str,
) -> None:
    try:
        content = await asyncio.to_thread(
            snooze_attention_item,
            ariel_base_url=ariel_base_url,
            attention_item_id=attention_item_id,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_with_attention_resolve(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    attention_item_id: str,
) -> None:
    try:
        content = await asyncio.to_thread(
            resolve_attention_item,
            ariel_base_url=ariel_base_url,
            attention_item_id=attention_item_id,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_with_attention_refresh(
    *,
    interaction: discord.Interaction,
    ariel_base_url: str,
    attention_item_id: str,
    allowed_user_id: int | None,
) -> None:
    try:
        content = await asyncio.to_thread(
            refresh_attention_item,
            ariel_base_url=ariel_base_url,
            attention_item_id=attention_item_id,
        )
    except ArielDiscordError as exc:
        content = f"Ariel request failed: {exc}"
    except httpx.HTTPError:
        content = "Ariel request failed: could not reach the local Ariel API."
    await interaction.response.edit_message(
        content=format_discord_message(content),
        view=ArielActionView(
            ariel_base_url=ariel_base_url,
            attention_item_id=attention_item_id,
            allowed_user_id=allowed_user_id,
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


class ArielActionView(discord.ui.View):
    def __init__(
        self,
        *,
        ariel_base_url: str,
        approval_refs: list[str] | None = None,
        job_id: str | None = None,
        notification_id: str | None = None,
        attention_item_id: str | None = None,
        allowed_user_id: int | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.ariel_base_url = ariel_base_url
        self.allowed_user_id = allowed_user_id
        for approval_ref in approval_refs or []:
            approve_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=_approval_custom_id("approve", approval_ref),
            )
            self.add_item(approve_button)

            deny_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=_approval_custom_id("deny", approval_ref),
            )
            self.add_item(deny_button)

        if job_id is not None:
            refresh_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Refresh job",
                style=discord.ButtonStyle.secondary,
                custom_id=_job_refresh_custom_id(job_id),
            )
            self.add_item(refresh_button)

        if notification_id is not None:
            ack_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Acknowledge",
                style=discord.ButtonStyle.primary,
                custom_id=_notification_ack_custom_id(notification_id),
            )
            self.add_item(ack_button)

        if attention_item_id is not None:
            attention_ack_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Acknowledge",
                style=discord.ButtonStyle.primary,
                custom_id=_attention_ack_custom_id(attention_item_id),
            )
            self.add_item(attention_ack_button)

            snooze_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Snooze 24h",
                style=discord.ButtonStyle.secondary,
                custom_id=_attention_snooze_custom_id(attention_item_id),
            )
            self.add_item(snooze_button)

            resolve_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Resolve",
                style=discord.ButtonStyle.success,
                custom_id=_attention_resolve_custom_id(attention_item_id),
            )
            self.add_item(resolve_button)

            attention_refresh_button: discord.ui.Button[ArielActionView] = discord.ui.Button(
                label="Refresh",
                style=discord.ButtonStyle.secondary,
                custom_id=_attention_refresh_custom_id(attention_item_id),
            )
            self.add_item(attention_refresh_button)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = AppSettings()
    discord_bot_token = settings.discord_bot_token
    if discord_bot_token is None:
        raise DiscordBotConfigError("missing Discord configuration: ARIEL_DISCORD_BOT_TOKEN")
    bot = configured_discord_bot(settings)
    bot.run(discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
