from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ArielDiscordReply:
    content: str
    view: discord.ui.View | None = None


def format_discord_message(message: str) -> str:
    normalized = message.strip() or "(empty Ariel response)"
    if len(normalized) <= 1900:
        return normalized
    return f"{normalized[:1886].rstrip()}\n[truncated]"


def ask_ariel(
    *,
    ariel_base_url: str,
    prompt: str,
    discord_message_id: int,
) -> str:
    return ask_ariel_reply(
        ariel_base_url=ariel_base_url,
        prompt=prompt,
        discord_message_id=discord_message_id,
    ).content


def ask_ariel_reply(
    *,
    ariel_base_url: str,
    prompt: str,
    discord_message_id: int,
    allowed_user_id: int | None = None,
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

        message_response = client.post(
            f"{ariel_base_url}/v1/sessions/{session_id}/message",
            headers={"Idempotency-Key": f"discord-message-{discord_message_id}"},
            json={"message": prompt},
        )
        message_payload = _json_response_payload(message_response)
        if message_response.status_code >= 400 or message_payload.get("ok") is not True:
            raise ArielDiscordError(_safe_ariel_error_message(message_payload))

        return _message_reply_for_discord(
            payload=message_payload,
            ariel_base_url=ariel_base_url,
            allowed_user_id=allowed_user_id,
        )


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
                name="ariel",
                description="Ask Ariel through the local API.",
                callback=self._slash_ariel,
            )
        )

    async def setup_hook(self) -> None:
        self.tree.copy_global_to(guild=discord.Object(id=self.ariel_guild_id))
        await self.tree.sync(guild=discord.Object(id=self.ariel_guild_id))

    async def _slash_ariel(self, interaction: discord.Interaction, prompt: str) -> None:
        if not self._interaction_is_allowed(interaction):
            await interaction.response.send_message(
                "This Ariel bot is limited to the configured Discord user and channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(thinking=True)
        reply = await self._ask_ariel_for_discord(
            prompt=prompt,
            discord_message_id=interaction.id,
        )
        if reply.view is None:
            await interaction.followup.send(
                format_discord_message(reply.content),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                format_discord_message(reply.content),
                view=reply.view,
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
                "This Ariel action is limited to the configured Discord user and channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._handle_custom_id_interaction(interaction, custom_id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id != self.ariel_user_id:
            return
        if message.type != discord.MessageType.default:
            return

        prompt = message.content.strip()
        if not prompt:
            return

        guild_id = message.guild.id if message.guild is not None else None
        should_answer = guild_id is None or (
            guild_id == self.ariel_guild_id and message.channel.id == self.ariel_channel_id
        )

        bot_user_id = self.user.id if self.user is not None else None
        mentioned_ariel = bot_user_id is not None and any(
            mention.id == bot_user_id for mention in message.mentions
        )
        if mentioned_ariel:
            should_answer = True
            prompt = (
                prompt.replace(f"<@{bot_user_id}>", "")
                .replace(f"<@!{bot_user_id}>", "")
                .strip()
            )
            if not prompt:
                return

        if not should_answer and bot_user_id is not None and message.reference is not None:
            referenced_message = message.reference.resolved
            referenced_author = getattr(referenced_message, "author", None)
            if getattr(referenced_author, "id", None) == bot_user_id:
                should_answer = True
            elif message.reference.message_id is not None and hasattr(
                message.channel, "fetch_message"
            ):
                try:
                    fetched_message = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                except discord.HTTPException:
                    fetched_message = None
                if fetched_message is not None:
                    should_answer = fetched_message.author.id == bot_user_id

        if not should_answer:
            return

        reply = await self._ask_ariel_for_discord(
            prompt=prompt,
            discord_message_id=message.id,
        )

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

    async def _ask_ariel_for_discord(
        self,
        *,
        prompt: str,
        discord_message_id: int,
    ) -> ArielDiscordReply:
        try:
            return await asyncio.to_thread(
                ask_ariel_reply,
                ariel_base_url=self.ariel_base_url,
                prompt=prompt,
                discord_message_id=discord_message_id,
                allowed_user_id=self.ariel_user_id,
            )
        except ArielDiscordError as exc:
            return ArielDiscordReply(content=f"Ariel request failed: {exc}")
        except httpx.HTTPError:
            return ArielDiscordReply(
                content="Ariel request failed: could not reach the local Ariel API."
            )

    def _interaction_is_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ariel_user_id:
            return False
        guild_id = interaction.guild.id if interaction.guild is not None else None
        if guild_id is None:
            return True
        return guild_id == self.ariel_guild_id and interaction.channel_id == self.ariel_channel_id

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


def _is_ariel_custom_id(custom_id: str) -> bool:
    return custom_id.startswith(
        (
            _APPROVAL_CUSTOM_ID_PREFIX,
            _JOB_REFRESH_CUSTOM_ID_PREFIX,
            _NOTIFICATION_ACK_CUSTOM_ID_PREFIX,
        )
    )


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


class ArielActionView(discord.ui.View):
    def __init__(
        self,
        *,
        ariel_base_url: str,
        approval_refs: list[str] | None = None,
        job_id: str | None = None,
        notification_id: str | None = None,
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
