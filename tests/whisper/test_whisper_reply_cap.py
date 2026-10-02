"""Cog-level: one-reply-per-whisper enforcement."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.whisper.models import Whisper
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")

SENDER, TARGET, OTHER = 1001, 2001, 9999


def _w() -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state="pending", solved=False, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )


def _make_button():
    from bot_modules.whisper.cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperReplyButton(bot, 42)


# ── Button-level gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_button_blocks_when_already_replied():
    """A second reply attempt errors at button click without opening the modal."""
    button = _make_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=1):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()
    args = interaction.response.send_message.call_args.args
    assert "already" in args[0].lower() or "limited" in args[0].lower()


@pytest.mark.asyncio
async def test_reply_button_allows_first_reply():
    button = _make_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_button_blocks_outsider_even_with_zero_replies():
    """Non-participant gets the participant error, not the cap error."""
    button = _make_button()
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()
    args = interaction.response.send_message.call_args.args
    assert "recipient" in args[0].lower() or "sender" in args[0].lower()


# ── Modal-level race protection ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_modal_recheck_blocks_second_submission():
    """If a reply lands between modal open and submit, on_submit still rejects."""
    from bot_modules.whisper.cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "second reply"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=1), \
         patch("bot_modules.whisper.cog._do_insert_reply") as ins:
        await modal.on_submit(interaction)

    ins.assert_not_called()
    args = interaction.response.send_message.call_args.args
    assert "already" in args[0].lower() or "limited" in args[0].lower()


# ── Reply DM carries no Reply button ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_dm_carries_only_report():
    """After the one allowed reply, the inbound DM card offers Report and
    nothing else — the chain ends here."""
    import discord

    from bot_modules.whisper.cog import WhisperReplyModal
    from bot_modules.whisper.models import WhisperConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "first reply"  # type: ignore[attr-defined]
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    cfg = WhisperConfig(guild_id=9001, role_id=1, channel_id=2, log_channel_id=0)

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99), \
         patch("bot_modules.whisper.cog._load_config", return_value=cfg), \
         patch("bot_modules.whisper.cog.send_branded_dm", AsyncMock()) as dm:
        await modal.on_submit(interaction)

    dm.assert_awaited_once()
    layout = dm.call_args.kwargs["layout"]
    ids = [
        b.custom_id for b in layout.walk_children() if isinstance(b, discord.ui.Button)
    ]
    assert ids == ["whisper:report_reply:99"]
