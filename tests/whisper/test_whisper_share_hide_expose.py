"""Cog-level: share / delete flows.

(The file keeps its historical name; the Expose button it once covered was
dead code and was deleted in the 2026-09 games review.)

Since the Components V2 conversion (2026-09-30) the DM is redrawn from the
whisper row after every press (``_redraw_dm``), and Delete opens a confirm
card (``WhisperDeleteConfirmView``) rather than deleting on one tap.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import Whisper, WhisperConfig, WhisperState
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")



SENDER, TARGET = 1001, 2001
FEED, LOG = 8001, 8002
DM_MSG_ID = 99999


def _w(
    *,
    state: WhisperState = "pending",
    solved: bool = False,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state=state, solved=solved, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=DM_MSG_ID,
        deleted_at=deleted_at,
    )


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=7001, channel_id=FEED, log_channel_id=LOG)


def _bot():
    """A bot whose guild has no icon, so the DM redraw stamps a name only."""
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    guild = MagicMock()
    guild.name = "Guild"
    guild.icon = None
    bot.get_guild = MagicMock(return_value=guild)
    return bot


def _make_share_button():
    from bot_modules.whisper.cog import WhisperShareButton
    return WhisperShareButton(_bot(), 42)


def _make_delete_button():
    from bot_modules.whisper.cog import WhisperDeleteButton
    return WhisperDeleteButton(_bot(), 42)


def _feed_channel():
    feed_channel = MagicMock(spec=discord.TextChannel)
    old_msg = MagicMock()
    old_msg.delete = AsyncMock()
    feed_channel.fetch_message = AsyncMock(return_value=old_msg)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=77777))
    return feed_channel, old_msg


def _dm_message(msg_id: int = DM_MSG_ID) -> MagicMock:
    dm_msg = MagicMock()
    dm_msg.id = msg_id
    dm_msg.edit = AsyncMock()
    return dm_msg


def _fetched_dm(bot, dm_msg: MagicMock) -> None:
    """Route ``_redraw_dm``'s fetch path (no DM message in hand) to ``dm_msg``."""
    target = FakeMember(id=TARGET)
    target.create_dm.return_value.fetch_message = AsyncMock(return_value=dm_msg)
    bot.get_user = MagicMock(return_value=target)


def _card_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _button_ids(view: discord.ui.LayoutView) -> list[str | None]:
    return [b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)]


# ── Share ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_target_pending_deletes_old_and_posts_new_to_feed():
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.message = None  # no DM in hand: the redraw fetches it
    feed_channel, old_msg = _feed_channel()
    button.bot.get_guild.return_value.get_channel = MagicMock(return_value=feed_channel)
    _fetched_dm(button.bot, _dm_message())

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_update_state") as upd, \
         patch("bot_modules.whisper.cog._do_set_message_ids") as set_ids:
        await button.callback(interaction)

    upd.assert_called_once_with(":memory:", 42, "shared")
    old_msg.delete.assert_awaited_once()
    feed_channel.send.assert_awaited_once()
    sent = feed_channel.send.call_args.kwargs
    assert sent["allowed_mentions"].users is False
    text = _card_text(sent["view"])
    assert "Fresh Whisper Was Shared" in text
    assert "> hi" in text
    set_ids.assert_called_once_with(
        ":memory:", 42, channel_msg_id=77777, dm_msg_id=DM_MSG_ID
    )
    # Past the 3 s window: deferred, then the confirmation is a followup.
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_share_non_pending_rejected():
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(state="shared")), \
         patch("bot_modules.whisper.cog._do_update_state") as upd:
        await button.callback(interaction)

    upd.assert_not_called()
    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_share_edits_dm_view_when_invoked_from_dm():
    """Share clicked from the DM (interaction.message.id == dm_msg_id) redraws
    that message from the now-shared row: Share and Delete gone, Guess and
    Reply left — and, being an embed-era message possibly, with content and
    embed cleared explicitly so it becomes a V2 card."""
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    feed_channel, _ = _feed_channel()
    button.bot.get_guild.return_value.get_channel = MagicMock(return_value=feed_channel)
    dm_msg = _dm_message()
    interaction.message = dm_msg

    with patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(), _w(state="shared")]), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_update_state"), \
         patch("bot_modules.whisper.cog._do_set_message_ids"):
        await button.callback(interaction)

    dm_msg.edit.assert_awaited_once()
    edit_kwargs = dm_msg.edit.call_args.kwargs
    assert edit_kwargs["content"] is None
    assert edit_kwargs["embed"] is None
    assert edit_kwargs["attachments"] == []
    assert _button_ids(edit_kwargs["view"]) == ["whisper:guess:42", "whisper:reply:42"]


@pytest.mark.asyncio
async def test_share_from_another_message_redraws_the_dm_not_that_message():
    """Share pressed from a message that isn't the DM (interaction.message.id
    != dm_msg_id) must NOT edit that message — the DM is fetched by its
    stored id and redrawn instead."""
    button = _make_share_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    feed_channel, _ = _feed_channel()
    button.bot.get_guild.return_value.get_channel = MagicMock(return_value=feed_channel)
    other_msg = _dm_message(111111)  # not the DM
    interaction.message = other_msg
    dm_msg = _dm_message()
    _fetched_dm(button.bot, dm_msg)

    with patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(), _w(state="shared")]), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_update_state"), \
         patch("bot_modules.whisper.cog._do_set_message_ids"):
        await button.callback(interaction)

    other_msg.edit.assert_not_called()
    dm_msg.edit.assert_awaited_once()


# ── Delete ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_target_opens_confirm_without_deleting():
    """Nothing destructive is one tap: the button opens the confirm card and
    deletes nothing itself."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.message = None

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await button.callback(interaction)

    sd.assert_not_called()
    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    assert isinstance(view, WhisperDeleteConfirmView)
    assert "Delete Whisper #42?" in _card_text(view)
    assert view.message is interaction.original_response.return_value


@pytest.mark.asyncio
async def test_delete_non_target_rejected():
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=SENDER))  # sender, not target
    interaction.response.send_message = AsyncMock()
    interaction.message = None

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await button.callback(interaction)

    sd.assert_not_called()
    assert "view" not in interaction.response.send_message.call_args.kwargs


@pytest.mark.asyncio
async def test_delete_already_deleted_rejected():
    button = _make_delete_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    interaction.message = None

    with patch("bot_modules.whisper.cog._do_load_whisper",
               return_value=_w(deleted_at=1234.0)), \
         patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await button.callback(interaction)

    sd.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_yes_soft_deletes_and_clears_the_dm_buttons():
    """Yes on the confirm soft-deletes, turns the confirm into a receipt, and
    redraws the DM it came from with every button gone (terminal)."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    dm_msg = _dm_message()
    view = WhisperDeleteConfirmView(_bot(), 42, invoker_id=TARGET, dm_message=dm_msg)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(), _w(deleted_at=1234.0)]), \
         patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await view._on_yes(interaction)

    sd.assert_called_once_with(":memory:", 42)
    receipt = interaction.response.edit_message.call_args.kwargs["view"]
    assert "Whisper #42 Deleted" in _card_text(receipt)
    dm_msg.edit.assert_awaited_once()
    assert _button_ids(dm_msg.edit.call_args.kwargs["view"]) == []
    assert view.is_finished()


@pytest.mark.asyncio
async def test_confirm_yes_rechecks_the_row_before_deleting():
    """A whisper deleted between the press and the Yes is refused, not
    deleted twice."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    view = WhisperDeleteConfirmView(_bot(), 42, invoker_id=TARGET, dm_message=_dm_message())
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper",
               return_value=_w(deleted_at=1234.0)), \
         patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await view._on_yes(interaction)

    sd.assert_not_called()
    assert "already deleted" in _card_text(
        interaction.response.edit_message.call_args.kwargs["view"]
    ).lower()


@pytest.mark.asyncio
async def test_confirm_cancel_is_silent_and_deletes_nothing():
    """Cancel deletes the confirm itself and leaves the DM exactly as it was."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    dm_msg = _dm_message()
    view = WhisperDeleteConfirmView(_bot(), 42, invoker_id=TARGET, dm_message=dm_msg)
    interaction = fake_interaction(user=FakeMember(id=TARGET))

    with patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await view._on_cancel(interaction)

    sd.assert_not_called()
    interaction.response.defer.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once()
    dm_msg.edit.assert_not_called()
    assert view.is_finished()


@pytest.mark.asyncio
async def test_confirm_from_another_message_redraws_the_dm_not_that_message():
    """A confirm opened from a message that isn't the DM must NOT edit that
    message; the DM is fetched by its stored id and redrawn."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    bot = _bot()
    other_msg = _dm_message(111111)
    dm_msg = _dm_message()
    _fetched_dm(bot, dm_msg)
    view = WhisperDeleteConfirmView(bot, 42, invoker_id=TARGET, dm_message=other_msg)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(), _w(deleted_at=1234.0)]), \
         patch("bot_modules.whisper.cog._do_soft_delete"):
        await view._on_yes(interaction)

    other_msg.edit.assert_not_called()
    dm_msg.edit.assert_awaited_once()
