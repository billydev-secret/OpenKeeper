"""Cog-level: age-lock end-to-end + DM copy framing."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import Whisper, WhisperConfig
from bot_modules.whisper.service import LOCK_DURATION_SECONDS
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")




SENDER, TARGET = 1001, 2001
ROLE = 7001


def _w(*, created_at: float | None = None, **overrides) -> Whisper:
    defaults = dict(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time() if created_at is None else created_at,
        state="pending", solved=False, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )
    defaults.update(overrides)
    return Whisper(**defaults)  # type: ignore[arg-type]


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=8001, log_channel_id=8002)


def _texts(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


# ── Guess picker rejects when whisper is age-locked ──────────────────────────


@pytest.mark.asyncio
async def test_guess_pick_rejects_locked_whisper():
    """Even if a stale ephemeral lingers, a pick on a locked whisper is
    refused — and the refusal is a card, since the picker is a V2 message
    with no content to edit."""
    from bot_modules.whisper.cog import WhisperGuessListView
    from bot_modules.whisper.layouts import GUESS_PICK_ID
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    members = [FakeMember(id=4001, display_name="suspect")]
    locked = _w(created_at=time.time() - LOCK_DURATION_SECONDS - 100)
    view = WhisperGuessListView(bot, locked, members, invoker_id=TARGET)  # type: ignore[arg-type]
    select = next(i for i in view.walk_children() if getattr(i, "custom_id", None) == GUESS_PICK_ID)
    select._values = [str(SENDER)]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=locked), \
         patch("bot_modules.whisper.cog._do_record_guess", return_value=True) as rec:
        await view._on_pick(interaction)

    rec.assert_not_called()
    interaction.response.edit_message.assert_awaited_once()
    content = _texts(interaction.response.edit_message.call_args.kwargs["view"])
    assert "locked" in content.lower() or "too old" in content.lower()


# ── Inbox surfaces "Locked" and omits Guess ──────────────────────────────────


def test_inbox_locked_whisper_status_says_locked():
    from bot_modules.whisper.cog import WhisperInboxView
    from bot_modules.whisper.layouts import INBOX_ACTION_IDS
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    locked = _w(created_at=time.time() - LOCK_DURATION_SECONDS - 100)
    view = WhisperInboxView(bot, [locked], invoker_id=TARGET, mode="received")
    assert "Locked" in _texts(view)
    view._open_id = 42
    view.render()
    assert "too old" in _texts(view).lower()
    ids = [b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)]
    assert INBOX_ACTION_IDS["guess"] not in ids


# ── DM copy reframe ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_dm_message_frames_game_with_guess_count():
    """The DM frames the whisper as a guessing game and is branded with the
    origin server (the attribution line ``send_branded_dm`` stamps)."""
    from bot_modules.whisper.cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)

    import discord as d
    role = MagicMock()
    role.id = ROLE
    sender = FakeMember(id=SENDER)
    sender.roles = [role]
    target = MagicMock()
    target.id = TARGET
    target.roles = [role]
    target.send = AsyncMock(return_value=MagicMock(id=12345))
    target.is_timed_out = MagicMock(return_value=False)

    feed_channel = MagicMock(spec=d.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=67890))

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.user = sender
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test Server"
    interaction.guild.icon = None
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_whisper", return_value=42), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(message="my secret")), \
         patch("bot_modules.whisper.cog._do_set_message_ids"):
        await cog._send_impl(interaction, target=target, message="my secret")

    target.send.assert_awaited_once()
    dm_body = _texts(target.send.call_args.kwargs["view"])
    assert "Whisper" in dm_body
    assert "guess" in dm_body.lower()
    assert "> my secret" in dm_body
    assert "3 guesses left" in dm_body
    # Branded: the origin server is attributed on the card.
    assert "-# From Test Server" in dm_body
