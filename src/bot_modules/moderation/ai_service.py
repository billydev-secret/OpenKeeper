"""AI-powered moderation helpers using a locally-hosted Ollama instance."""

from __future__ import annotations

import logging
from pathlib import Path

import discord

from bot_modules.shared import ollama_client
from bot_modules.shared.age_gate import is_age_gated_channel

log = logging.getLogger("dungeonkeeper.ai_mod")

_MAX_MSG_CHARS = 400

_WATCH_CHECK_SYSTEM = (
    "You are a Discord moderation assistant. Determine whether the message below violates any "
    "server rule.\n\n"
    "Rules:\n"
    "  Rule 1 — Adults only (21+): This is a membership age requirement — the server is for adults. "
    "Rule 1 is NEVER violated by the content of a message. Only flag Rule 1 if a member explicitly "
    "states they are under 21.\n"
    "  Rule 2 — No harassment, coercion, threats, demeaning behavior, slurs, or boundary violations. "
    "Compliments, flattery, expressions of attraction, flirting, playful teasing, emoji, and ordinary "
    "social interaction are NOT violations — even if they use words like 'sexy', 'hot', or 'gorgeous'. "
    "Only flag clear, explicit hostile intent: direct insults, threats, slurs, or sustained pressure "
    "on someone who has expressed they are not interested.\n"
    "  Rule 3 — Explicit content only in designated channels. This applies to explicit sexual text or "
    "images posted in non-designated channels. Ordinary conversation, emoji, compliments, and "
    "ambiguous phrasing are NOT violations. If the message header says the channel is NSFW-designated, "
    "Rule 3 cannot be violated. Evaluate the message text only — the sender's display name is not "
    "message content.\n"
    "  Rule 4 — No callouts or conflicts imported from other servers. Casually mentioning another "
    "server, referencing shared communities, or general chat about other places is NOT a violation. "
    "Only flag active drama, accusations, or beef being brought in from elsewhere.\n"
    "  Rule 5 — DMs are opt-in; do not DM members without their consent. Mentioning DMs, "
    "offering to DM, or discussing DM policies in public chat is NOT a violation.\n"
    "  Rule 6 — Settle disputes in tickets, not public chat. Ordinary venting, expressing a preference, "
    "or asking a question is NOT a violation. Only flag active public arguments or attempts to "
    "escalate a conflict in chat.\n\n"
    "Important: The vast majority of messages are completely normal and friendly. Your threshold "
    "must be HIGH — only flag messages where a reasonable moderator would take immediate action. "
    "If the message could plausibly be friendly, joking, or harmless, reply OK.\n\n"
    "Reply with exactly one of:\n"
    "  VIOLATION: <one-sentence reason citing the specific rule number>\n"
    "  OK\n\n"
    "No other output."
)


async def ai_check_watched_message(
    message: discord.Message,
    *,
    db_path: Path | None = None,
) -> tuple[bool, str]:
    """Check a single live message against server rules.

    Returns (is_violation, reason). Errors are raised to the caller.
    """
    from bot_modules.shared.ai_config import get_prompt_from_path

    guild_id = message.guild.id if message.guild else 0
    system = (
        get_prompt_from_path(db_path, "ai_prompt_watch_check", guild_id)
        if db_path
        else _WATCH_CHECK_SYSTEM
    )

    ts = message.created_at.strftime("%Y-%m-%d %H:%M") if message.created_at else "?"
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    is_nsfw = is_age_gated_channel(message.channel)
    nsfw_tag = " [NSFW-designated]" if is_nsfw else ""
    content = (message.content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
    prompt = f"[{ts}] #{channel_name}{nsfw_tag} | {message.author.display_name}: {content}"

    reply = await ollama_client.chat(
        system=system,
        user_content=prompt,
        max_tokens=256,
        temperature=0.0,
    )
    is_violation = reply.upper().startswith("VIOLATION")
    reason = reply[len("VIOLATION:"):].strip() if is_violation else ""
    return is_violation, reason

