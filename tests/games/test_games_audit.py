"""The anonymous-games audit mirror names the submitter for moderators."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.games.utils.audit import build_audit_notice_layout, send_audit_log


def _text_of(items) -> str:
    return "\n".join(
        item.content for item in items if isinstance(item, discord.ui.TextDisplay)
    )


def test_audit_notice_names_the_submitter_with_id_never_a_mention():
    """A mod whose client hasn't cached the submitter would read a bare
    number — a raw id stays deliberately, but never as ``<@id>``."""
    user = SimpleNamespace(id=42, display_name="Sneaky_One", mention="<@42>")
    text = _text_of(
        build_audit_notice_layout(game_type="wyr", user=user, content="hi")
    )
    assert "Sneaky\\_One (`42`)" in text
    assert "<@42>" not in text


def test_audit_notice_has_title_and_game():
    user = SimpleNamespace(id=1, display_name="A", mention="<@1>")
    text = _text_of(
        build_audit_notice_layout(game_type="wyr", user=user, content="hi")
    )
    assert text.startswith("## 🚩")
    assert "Anonymous Submission" in text
    assert "**Game:** WYR" in text


def test_audit_notice_quotes_and_escapes_content():
    user = SimpleNamespace(id=1, display_name="A", mention="<@1>")
    text = _text_of(
        build_audit_notice_layout(
            game_type="wyr", user=user, content="a *sneaky* line"
        )
    )
    assert '"a \\*sneaky\\* line"' in text


async def test_send_audit_log_sends_a_layout_view_card():
    """The channel mirror wraps the builder's items in a plain LayoutView —
    no accent color and no Container, per the Components V2 card rules."""
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = lambda cid: channel
    db = MagicMock()
    db.fetchone = AsyncMock(return_value=(123,))
    user = SimpleNamespace(id=42, display_name="Sneaky_One", mention="<@42>")

    await send_audit_log(
        bot, db, SimpleNamespace(id=1), game_type="wyr", user=user, content="hi"
    )

    view = channel.send.call_args.kwargs["view"]
    assert isinstance(view, discord.ui.LayoutView)
    text = "\n".join(
        item.content for item in view.children if isinstance(item, discord.ui.TextDisplay)
    )
    assert "Sneaky\\_One (`42`)" in text


async def test_send_audit_log_skips_when_no_channel_configured():
    bot = MagicMock()
    db = MagicMock()
    db.fetchone = AsyncMock(return_value=None)
    user = SimpleNamespace(id=1, display_name="A", mention="<@1>")

    await send_audit_log(
        bot, db, SimpleNamespace(id=1), game_type="wyr", user=user, content="hi"
    )
    # No channel lookup means no send attempt — nothing to assert on a mock
    # that was never touched beyond the guard, but the call above must not
    # raise (bot.get_channel is unset on the bare MagicMock).
