from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord.ext import commands
import httpx

from .config import AppSettings


class DiscordBotConfigError(Exception):
    pass


class ArielDiscordError(Exception):
    pass


_APPROVAL_COMMAND_PATTERN = re.compile(
    r"^\s*(?P<decision>approve|deny)\s+(?P<approval_ref>apr_[a-z0-9]+)(?:\s+(?P<reason>.*?))?\s*$",
    re.IGNORECASE,
)


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

        return _format_message_response_for_discord(message_payload)


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

        approval_command = _parse_approval_command(prompt)

        try:
            if approval_command is None:
                assistant_message = await asyncio.to_thread(
                    ask_ariel,
                    ariel_base_url=self.ariel_base_url,
                    prompt=prompt,
                    discord_message_id=message.id,
                )
            else:
                assistant_message = await asyncio.to_thread(
                    decide_approval,
                    ariel_base_url=self.ariel_base_url,
                    approval_ref=approval_command["approval_ref"],
                    decision=approval_command["decision"],
                    reason=approval_command.get("reason"),
                )
        except ArielDiscordError as exc:
            assistant_message = f"Ariel request failed: {exc}"
        except httpx.HTTPError:
            assistant_message = "Ariel request failed: could not reach the local Ariel API."

        await message.reply(
            format_discord_message(assistant_message),
            mention_author=False,
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


def _parse_approval_command(prompt: str) -> dict[str, str] | None:
    match = _APPROVAL_COMMAND_PATTERN.match(prompt)
    if match is None:
        return None
    decision = match.group("decision").lower()
    result = {
        "decision": decision,
        "approval_ref": match.group("approval_ref"),
    }
    reason = match.group("reason")
    if reason is not None and reason.strip():
        result["reason"] = reason.strip()
    return result


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
    turn = payload.get("turn")
    lifecycle = turn.get("surface_action_lifecycle") if isinstance(turn, dict) else None
    if not isinstance(lifecycle, list):
        return []

    lines: list[str] = []
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
        capability_id = (
            proposal.get("capability_id")
            if isinstance(proposal, dict) and isinstance(proposal.get("capability_id"), str)
            else "action"
        )
        expires_at = approval.get("expires_at")
        suffix = f" expires_at={expires_at}" if isinstance(expires_at, str) else ""
        lines.append(
            f"Approval pending ({capability_id}): {approval_ref}{suffix}. "
            f"Reply `approve {approval_ref}` or `deny {approval_ref}`."
        )
    return lines


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
