"""Cog-level: the launcher's persistent buttons (``WhisperLauncherView``).

Send opens the send form through the cog's gate, Inbox and Sent open the
member's ephemeral inbox, and Help (new with the 2026-09-30 Components V2
conversion) reads the guild's ``whisper`` doc at click time. The card
itself is ``whisper/layouts.py``, tested in ``test_whisper_layouts.py``.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import Whisper, WhisperConfig, WhisperState
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")

ROLE = 7001


def _make_cog_and_view():
    from bot_modules.whisper.cog import WhisperCog, WhisperLauncherView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    bot.get_cog = MagicMock(return_value=cog)
    return cog, WhisperLauncherView(bot)


def _w(state: WhisperState = "pending") -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=1001, target_id=2001, message="x",
        created_at=time.time(), state=state, solved=False, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=8001, log_channel_id=8002)


def _card_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


@pytest.mark.asyncio
async def test_send_whisper_button_opens_the_send_form():
    """Clicking Send opens the one-modal send form for a role holder."""
    from bot_modules.whisper.cog import WhisperSendModal

    cog, view = _make_cog_and_view()
    sender = FakeMember(id=1001)
    role = MagicMock()
    role.id = ROLE
    sender.roles = [role]
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await view._on_send_click(interaction)

    interaction.response.send_modal.assert_awaited_once()
    assert isinstance(interaction.response.send_modal.call_args.args[0], WhisperSendModal)


@pytest.mark.asyncio
async def test_send_whisper_button_rejects_without_role():
    """User must hold the whisper role to open the form."""
    cog, view = _make_cog_and_view()
    sender = FakeMember(id=1001)
    sender.roles = []  # no whisper role
    role = MagicMock()
    role.id = ROLE
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await view._on_send_click(interaction)

    interaction.response.send_modal.assert_not_awaited()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "optin" in args[0].lower() or "role" in args[0].lower()


@pytest.mark.asyncio
async def test_check_whispers_lists_pending_and_shared():
    from bot_modules.whisper.cog import WhisperInboxView

    cog, view = _make_cog_and_view()
    interaction = fake_interaction(user=FakeMember(id=2001))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.original_response = AsyncMock(return_value=MagicMock())

    with patch(
        "bot_modules.whisper.cog._do_list_received_in_states",
        return_value=[_w("pending"), _w("shared")],
    ) as listed:
        await view._on_check_click(interaction)

    assert listed.call_args.kwargs["states"] == ["pending", "shared"]
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    inbox = kwargs["view"]
    assert isinstance(inbox, WhisperInboxView)
    assert inbox._mode == "received"
    # The stored handle is what lets the card grey out on timeout.
    assert inbox.message is interaction.original_response.return_value


@pytest.mark.asyncio
async def test_my_sent_button_opens_the_sent_list():
    from bot_modules.whisper.cog import WhisperInboxView

    cog, view = _make_cog_and_view()
    interaction = fake_interaction(user=FakeMember(id=1001))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.original_response = AsyncMock(return_value=MagicMock())

    with patch("bot_modules.whisper.cog._do_list_sent", return_value=[_w()]):
        await view._on_check_sent_click(interaction)

    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    assert isinstance(kwargs["view"], WhisperInboxView)
    assert kwargs["view"]._mode == "sent"


@pytest.mark.asyncio
async def test_help_button_reads_the_guild_doc_and_never_pings():
    """Help answers with the ``whisper`` doc read at click time; an
    admin-written doc may carry a role mention, so the card can't ping."""
    from bot_modules.whisper.layouts import HELP_DOC_KEY

    cog, view = _make_cog_and_view()
    interaction = fake_interaction(user=FakeMember(id=1001))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    with patch(
        "bot_modules.whisper.cog.load_doc_body", return_value="# Whisper FAQ\nSend one."
    ) as loaded:
        await view._on_help_click(interaction)

    loaded.assert_called_once_with(":memory:", 9001, HELP_DOC_KEY)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    assert kwargs["allowed_mentions"].roles is False
    assert "Send one." in _card_text(kwargs["view"])


@pytest.mark.asyncio
async def test_help_button_with_no_doc_written_says_so():
    cog, view = _make_cog_and_view()
    interaction = fake_interaction(user=FakeMember(id=1001))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    with patch("bot_modules.whisper.cog.load_doc_body", return_value=None):
        await view._on_help_click(interaction)

    text = _card_text(interaction.response.send_message.call_args.kwargs["view"])
    assert "Nobody has written" in text


@pytest.mark.asyncio
async def test_launcher_buttons_answer_when_the_cog_is_unloaded():
    """The registered bare view outlives a cog reload; a press then gets a
    plain error rather than an AttributeError."""
    from bot_modules.whisper.cog import WhisperLauncherView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.get_cog = MagicMock(return_value=None)
    view = WhisperLauncherView(bot)
    interaction = fake_interaction(user=FakeMember(id=1001))

    await view._on_send_click(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "isn't loaded" in args[0]


@pytest.mark.asyncio
async def test_view_registered_on_cog_load():
    """Persistent view must be added via bot.add_view at cog load so buttons survive restart."""
    from bot_modules.whisper.cog import WhisperCog, WhisperLauncherView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    bot.guilds = []
    cog = WhisperCog(bot)
    with patch("bot_modules.whisper.cog._do_launcher_guilds", return_value=set()):
        await cog.cog_load()
    bot.add_view.assert_called_once()
    registered = bot.add_view.call_args.args[0]
    assert isinstance(registered, WhisperLauncherView)
    assert registered.timeout is None


@pytest.mark.asyncio
async def test_dynamic_buttons_registered_on_cog_load():
    """Per-whisper Guess/Share/Delete buttons must register as dynamic items so they survive bot restart."""
    from bot_modules.whisper.cog import (
        WhisperCog,
        WhisperDeleteButton,
        WhisperGuessButton,
        WhisperShareButton,
    )
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    bot.guilds = []
    cog = WhisperCog(bot)
    with patch("bot_modules.whisper.cog._do_launcher_guilds", return_value=set()):
        await cog.cog_load()
    bot.add_dynamic_items.assert_called_once()
    args = bot.add_dynamic_items.call_args.args
    assert WhisperGuessButton in args
    assert WhisperShareButton in args
    assert WhisperDeleteButton in args
    # Expose was dead code (never attached to a view since 1396fb5e) and is
    # gone; its custom_id must not be re-registered.
    assert not any("Expose" in getattr(a, "__name__", "") for a in args)
