"""Cog-level: WhisperInboxView callback coverage + share-side-effects.

Covers the async event-handler branches of ``WhisperInboxView`` (the row
openers, _on_back, _on_prev, _on_next, _on_filter, _on_clear_filter,
_on_share, _on_delete, _on_guess, _on_reply, _on_report, after_delete) and
the helper ``_share_side_effects``. What each card says is pinned by the
builder tests in tests/whisper/test_whisper_layouts.py; this file is about what a
press changes.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.layouts import INBOX_BACK_ID, INBOX_OPEN_ID_TEMPLATE
from bot_modules.whisper.models import (
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperConfig,
)
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")



SENDER = 1001
TARGET = 2001
OTHER = 9999
GUILD_ID = 9001


def _w(**overrides) -> Whisper:
    defaults = dict(
        id=1,
        guild_id=GUILD_ID,
        sender_id=SENDER,
        target_id=TARGET,
        message="hi",
        created_at=time.time(),
        state=STATE_PENDING,
        solved=False,
        exposed=False,
        guesses_left=3,
        channel_msg_id=88888,
        dm_msg_id=99999,
    )
    defaults.update(overrides)
    return Whisper(**defaults)  # type: ignore[arg-type]


def _bot():
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return bot


def _view(whispers: list[Whisper], *, mode: str = "received"):
    from bot_modules.whisper.cog import WhisperInboxView
    return WhisperInboxView(
        _bot(), whispers, invoker_id=TARGET, mode=mode,
    )


def _ids(view) -> list[str]:
    return [b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)]


def _opened(view, whisper_id: int):
    """A view with ``whisper_id`` opened, the way an Open press leaves it."""
    view._open_id = whisper_id
    view.render()
    return view


# ── interaction_check ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interaction_check_blocks_other_user():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_message = AsyncMock()
    assert (await view.interaction_check(interaction)) is False
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_interaction_check_allows_invoker():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    assert (await view.interaction_check(interaction)) is True


# ── open / back / nav callbacks ───────────────────────────────────────


@pytest.mark.asyncio
async def test_open_swaps_to_the_whisper_and_back_returns_to_the_list():
    ws = [_w(id=1), _w(id=2), _w(id=3)]
    view = _view(ws)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._opener(2)(interaction)
    assert view._open_id == 2
    assert INBOX_BACK_ID in _ids(view)
    assert INBOX_OPEN_ID_TEMPLATE.format(whisper_id=2) not in _ids(view)
    interaction.response.edit_message.assert_awaited_once_with(view=view)

    await view._on_back(interaction)
    assert view._open_id is None
    assert INBOX_OPEN_ID_TEMPLATE.format(whisper_id=2) in _ids(view)


@pytest.mark.asyncio
async def test_back_keeps_the_page_and_filter_you_left():
    ws = [_w(id=i, message="match" if i % 2 else "other") for i in range(1, 40)]
    view = _view(ws)
    view.apply_filter("match")
    view._page = 1
    view.render()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._opener(23)(interaction)
    await view._on_back(interaction)
    assert view._page == 1
    assert view._filter_query == "match"
    assert all("match" in w.message for w in view._display)


@pytest.mark.asyncio
async def test_on_prev_decrements_page():
    ws = [_w(id=i) for i in range(1, 40)]
    view = _view(ws)
    view._page = 1
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_prev(interaction)
    assert view._page == 0


@pytest.mark.asyncio
async def test_on_prev_clamps_at_zero():
    ws = [_w(id=i) for i in range(1, 40)]
    view = _view(ws)
    view._page = 0
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_prev(interaction)
    assert view._page == 0


@pytest.mark.asyncio
async def test_on_next_increments_page():
    ws = [_w(id=i) for i in range(1, 40)]
    view = _view(ws)
    view._page = 0
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_next(interaction)
    assert view._page == 1


@pytest.mark.asyncio
async def test_on_filter_opens_modal():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_filter(interaction)
    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_clear_filter_resets_display_and_closes_the_open_pane():
    ws = [_w(id=1, message="foo"), _w(id=2, message="bar")]
    view = _view(ws)
    view.apply_filter("foo")
    assert [w.id for w in view._display] == [1]
    view._open_id = 1
    view._page = 3
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_clear_filter(interaction)
    assert view._filter_query == ""
    assert view._display == ws
    assert view._open_id is None
    assert view._page == 0


# ── action callbacks ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_share_blocks_invalid_transition():
    view = _opened(_view([_w(state=STATE_SHARED)]), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_share(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "already" in msg


@pytest.mark.asyncio
async def test_on_share_happy_path_updates_state():
    view = _opened(_view([_w()]), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))

    with patch("bot_modules.whisper.cog._do_update_state") as upd, \
         patch("bot_modules.whisper.cog._share_side_effects", new=AsyncMock()), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(state=STATE_SHARED)), \
         patch("bot_modules.whisper.cog._redraw_dm", new=AsyncMock()) as redraw:
        await view._on_share(interaction)

    upd.assert_called_once()
    assert view._all[0].state == STATE_SHARED
    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once_with(view=view)
    # The DM loses its Share and Delete too, from the reloaded row.
    redraw.assert_awaited_once()
    assert redraw.call_args.args[1].state == STATE_SHARED


@pytest.mark.asyncio
async def test_on_share_nothing_open_short_circuits():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_share(interaction)
    interaction.response.send_message.assert_not_called()
    interaction.response.defer.assert_not_called()


@pytest.mark.asyncio
async def test_on_delete_opens_the_confirm_card_without_deleting():
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    ws = [_w(id=1), _w(id=2)]
    view = _opened(_view(ws), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))

    with patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await view._on_delete(interaction)

    sd.assert_not_called()
    assert [w.id for w in view._all] == [1, 2]
    confirm = interaction.response.send_message.call_args.kwargs["view"]
    assert isinstance(confirm, WhisperDeleteConfirmView)
    assert confirm.whisper_id == 1


@pytest.mark.asyncio
async def test_on_delete_blocks_invalid_transition():
    view = _opened(_view([_w(deleted_at=5.0)]), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_delete(interaction)
    msg = interaction.response.send_message.call_args.args[0].lower()
    assert "already" in msg


@pytest.mark.asyncio
async def test_after_delete_drops_the_row_and_redraws_through_the_handle():
    ws = [_w(id=1), _w(id=2)]
    view = _opened(_view(ws), 1)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    await view.after_delete(1)
    assert [w.id for w in view._all] == [2]
    assert view._open_id is None
    view.message.edit.assert_awaited_once_with(view=view)


@pytest.mark.asyncio
async def test_after_delete_without_a_handle_only_updates_state():
    view = _opened(_view([_w(id=1)]), 1)
    await view.after_delete(1)
    assert view._all == []


@pytest.mark.asyncio
async def test_on_delete_nothing_open_short_circuits():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_delete(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_guess_nothing_open_short_circuits():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_guess(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_reply_nothing_open_short_circuits():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_reply(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_reply_opens_modal_when_allowed():
    view = _opened(_view([_w()]), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    with patch("bot_modules.whisper.cog._do_count_replies", return_value=0):
        await view._on_reply(interaction)
    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_reply_blocks_when_cap_hit():
    view = _opened(_view([_w()]), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    with patch("bot_modules.whisper.cog._do_count_replies", return_value=1):
        await view._on_reply(interaction)
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_on_report_nothing_open_short_circuits():
    view = _view([_w()])
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_report(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_report_blocks_non_target():
    view = _opened(_view([_w()]), 1)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    # interaction_check normally blocks but we're calling _on_report directly
    await view._on_report(interaction)
    interaction.response.send_modal.assert_not_called()


@pytest.mark.asyncio
async def test_on_report_opens_modal_for_target():
    view = _opened(_view([_w()]), 1)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    await view._on_report(interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── _share_side_effects ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_side_effects_no_guild_returns_early():
    from bot_modules.whisper.cog import _share_side_effects
    bot = _bot()
    bot.get_guild.return_value = None
    await _share_side_effects(bot, _w())
    # No DB call since we returned early
    bot.get_guild.assert_called_once()


@pytest.mark.asyncio
async def test_share_side_effects_no_feed_channel_returns_early():
    from bot_modules.whisper.cog import _share_side_effects
    bot = _bot()
    guild = MagicMock()
    guild.get_channel.return_value = None  # no feed channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)
    with patch("bot_modules.whisper.cog._load_config", return_value=cfg):
        await _share_side_effects(bot, _w())


@pytest.mark.asyncio
async def test_share_side_effects_happy_path_posts_new_message():
    from bot_modules.whisper.cog import _share_side_effects
    bot = _bot()
    feed_channel = MagicMock(spec=discord.TextChannel)
    old_msg = MagicMock()
    old_msg.delete = AsyncMock()
    feed_channel.fetch_message = AsyncMock(return_value=old_msg)
    new_msg = MagicMock(id=77777)
    feed_channel.send = AsyncMock(return_value=new_msg)
    guild = MagicMock()
    guild.get_channel.return_value = feed_channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)

    with patch("bot_modules.whisper.cog._load_config", return_value=cfg), \
         patch("bot_modules.whisper.cog._do_set_message_ids") as set_ids:
        jump = await _share_side_effects(bot, _w(channel_msg_id=12345))

    feed_channel.send.assert_awaited_once()
    mentions = feed_channel.send.call_args.kwargs["allowed_mentions"]
    assert (mentions.users, mentions.roles, mentions.everyone) == (False, False, False)
    set_ids.assert_called_once()
    # The Share press's confirmation links to the new post.
    assert jump == new_msg.jump_url


@pytest.mark.asyncio
async def test_share_side_effects_swallows_delete_http_error():
    from bot_modules.whisper.cog import _share_side_effects
    bot = _bot()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.fetch_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom"),
    )
    new_msg = MagicMock(id=77777)
    feed_channel.send = AsyncMock(return_value=new_msg)
    guild = MagicMock()
    guild.get_channel.return_value = feed_channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)

    with patch("bot_modules.whisper.cog._load_config", return_value=cfg), \
         patch("bot_modules.whisper.cog._do_set_message_ids"):
        await _share_side_effects(bot, _w(channel_msg_id=12345))

    # send still tried after the delete error
    feed_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_share_side_effects_swallows_send_http_error():
    from bot_modules.whisper.cog import _share_side_effects
    bot = _bot()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom"),
    )
    guild = MagicMock()
    guild.get_channel.return_value = feed_channel
    bot.get_guild.return_value = guild
    cfg = WhisperConfig(guild_id=GUILD_ID, role_id=1, channel_id=2)

    with patch("bot_modules.whisper.cog._load_config", return_value=cfg), \
         patch("bot_modules.whisper.cog._do_set_message_ids") as set_ids:
        # no channel_msg_id this time, so no old-msg delete attempt
        jump = await _share_side_effects(bot, _w(channel_msg_id=0))

    set_ids.assert_not_called()
    assert jump is None
