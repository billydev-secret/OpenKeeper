"""Cog tests for the Watch List's DM relay.

``tests/moderation/test_watch_service.py`` covers the DB-backed add/remove/load helpers.
This file covers ``_notify_watchers``'s own message composition — the one
place a watched member's raw post reaches another member, and therefore the
one place the 2026-09-20 escaping sweep had to be told to stop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.moderation.watch.cog import WatchCog


def _message(*, content: str, attachments=(), author_id=7, author_name="watched"):
    author = MagicMock(spec=discord.Member)
    author.id = author_id
    author.bot = False
    author.display_name = author_name
    author.name = author_name

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    channel.name = "general"

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Test Guild"

    message = MagicMock(spec=discord.Message)
    message.author = author
    message.content = content
    message.attachments = list(attachments)
    message.channel = channel
    message.guild = guild
    message.jump_url = "https://discord.com/channels/1/2/3"
    return message


def _cog_with_watcher(watcher_id: int = 999, watched_id: int = 7):
    bot = MagicMock()
    bot.ctx = MagicMock()
    bot.ctx.watched_users = {watched_id: {watcher_id}}
    watcher = MagicMock(spec=discord.User)
    watcher.display_name = "Watcher"
    watcher.send = AsyncMock()
    bot.get_user = MagicMock(return_value=watcher)
    cog = WatchCog(bot)
    return cog, watcher


@pytest.mark.asyncio
async def test_the_relayed_body_is_the_members_text_verbatim():
    """A deliberate anomaly, and the test that fails if it's "cleaned up".

    Every other member-authored string the bot quotes is escaped. This one
    is not, because the whole point of a watch is to show a mod exactly what
    was typed (``docs/specs/watch_spec.md`` § Behavior) — a mod reading a sanitised
    copy is reading something nobody wrote, and for a moderation decision the
    difference between ``||slur||`` and ``\\|\\|slur\\|\\|`` is the decision.

    The relay is a DM to a single mod who asked for it, so the markdown it
    renders reaches nobody the member could not already reach in the channel
    the message was posted to.
    """
    raw_body = "**look at this** ||secret|| [link](https://evil.example)"
    message = _message(content=raw_body)
    cog, watcher = _cog_with_watcher()

    with patch("bot_modules.shared.ollama_client.is_available", return_value=False):
        await cog._notify_watchers(message)

    watcher.send.assert_awaited_once()
    sent = watcher.send.await_args.args[0]
    assert sent.startswith(raw_body), "the body must reach the mod byte for byte"
    assert "\\" not in sent[: len(raw_body)]


@pytest.mark.asyncio
async def test_the_attribution_escapes_the_watched_members_own_name():
    """The other half of the same line, and it goes the other way.

    The body is the member's text, quoted deliberately. The attribution is
    the *bot's* sentence about that member, and a display name holding
    markdown would reformat it — which is how a watched member could dress
    the bot's own "who and where" line up as something else entirely.
    """
    message = _message(content="hi", author_name="__spooky__")
    cog, watcher = _cog_with_watcher()

    with patch("bot_modules.shared.ollama_client.is_available", return_value=False):
        await cog._notify_watchers(message)

    sent = watcher.send.await_args.args[0]
    assert "\\_\\_spooky\\_\\_" in sent
    assert "(@__spooky__)" not in sent
