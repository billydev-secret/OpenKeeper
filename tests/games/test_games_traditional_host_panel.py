"""Truth or Dare's host panel: the ephemeral card ``/games host`` opens.

Ben, 2026-09-29: "move everything that's host only to an ephemeral message
brought up by slash command". What is pinned here is the glue — who may
open the panel, that it goes out ephemerally, that a press hands off to the
room's own handler, and that a panel outliving its room says so instead of
acting. The handlers' own gates (host/mod, NSFW age check, no-contact) are
covered where they live, in ``test_games_traditional_logic.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot_modules.games.utils.host_panel import HOST_PANEL_TIMEOUT
from bot_modules.games.traditional.cog import (
    GAME_ENDED_REPLY,
    NOT_HOST_REPLY,
    TraditionalCog,
    TraditionalHostPanel,
    TraditionalHostView,
)
from bot_modules.games.traditional.layouts import ASK_ID, END_ID, WRITE_OWN_ID

HOST = 1
GAME = "g1"


def _bot():
    return SimpleNamespace(active_views={}, games_db=None, ctx=SimpleNamespace(db_path=":memory:"))


def _room(bot) -> TraditionalHostView:
    view = TraditionalHostView(GAME, HOST, None, bot)
    bot.active_views[GAME] = view
    return view


def _interaction(uid: int = HOST, *, done: bool = False):
    response = SimpleNamespace(send_message=AsyncMock(), is_done=lambda: done)
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name="U"),
        guild=None,
        response=response,
        followup=SimpleNamespace(send=AsyncMock(return_value="followup-msg")),
        original_response=AsyncMock(return_value="panel-msg"),
    )


def _cog(bot) -> TraditionalCog:
    cog = TraditionalCog.__new__(TraditionalCog)
    cog.bot = bot
    return cog


def _button(panel: discord.ui.LayoutView, custom_id: str) -> discord.ui.Button:
    return next(
        b for b in panel.walk_children()
        if isinstance(b, discord.ui.Button) and b.custom_id == custom_id
    )


async def test_host_gets_the_panel_ephemerally():
    bot = _bot()
    _room(bot)
    interaction = _interaction()

    await _cog(bot).open_host_panel(interaction, GAME)  # type: ignore[arg-type]

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    panel = kwargs["view"]
    assert isinstance(panel, TraditionalHostPanel)
    assert panel.message == "panel-msg"  # so the timeout can grey it out


async def test_a_member_who_is_not_host_or_mod_is_refused():
    bot = _bot()
    _room(bot)
    interaction = _interaction(uid=99)

    await _cog(bot).open_host_panel(interaction, GAME)  # type: ignore[arg-type]

    interaction.response.send_message.assert_awaited_once_with(NOT_HOST_REPLY, ephemeral=True)


async def test_a_closed_or_missing_room_opens_no_panel():
    bot = _bot()
    room = _room(bot)
    room._closed = True
    for game_id in (GAME, "gone"):
        interaction = _interaction()
        await _cog(bot).open_host_panel(interaction, game_id)  # type: ignore[arg-type]
        interaction.response.send_message.assert_awaited_once_with(GAME_ENDED_REPLY, ephemeral=True)


async def test_panel_on_an_answered_interaction_goes_out_as_a_followup():
    """The launch path: ``finish_launch_response`` already answered it."""
    from bot_modules.games.traditional.cog import send_host_panel

    bot = _bot()
    room = _room(bot)
    interaction = _interaction(done=True)

    await send_host_panel(interaction, room)  # type: ignore[arg-type]

    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


async def test_panel_buttons_hand_off_to_the_rooms_handlers(monkeypatch):
    bot = _bot()
    room = _room(bot)
    calls: list[str] = []
    for name in ("ask_question", "write_own", "end_game_button"):
        monkeypatch.setattr(room, name, AsyncMock(side_effect=lambda _i, n=name: calls.append(n)))
    panel = TraditionalHostPanel(room)

    for custom_id in (ASK_ID, WRITE_OWN_ID, END_ID):
        await _button(panel, custom_id).callback(_interaction())

    assert calls == ["ask_question", "write_own", "end_game_button"]


async def test_a_press_after_the_room_closed_says_so_and_does_nothing(monkeypatch):
    bot = _bot()
    room = _room(bot)
    ask = AsyncMock()
    monkeypatch.setattr(room, "ask_question", ask)
    panel = TraditionalHostPanel(room)
    room._closed = True
    interaction = _interaction()

    await _button(panel, ASK_ID).callback(interaction)

    ask.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(GAME_ENDED_REPLY, ephemeral=True)


def test_the_panel_times_out_while_discord_still_lets_it_grey_out():
    """An ephemeral message is editable through its interaction for 15
    minutes. A longer timeout fails its grey-out edit silently and leaves
    a live-looking panel whose every press fails."""
    assert 0 < HOST_PANEL_TIMEOUT < 15 * 60
    assert TraditionalHostPanel(_room(_bot())).timeout == HOST_PANEL_TIMEOUT


async def test_a_refusal_on_an_answered_interaction_goes_out_as_a_followup():
    """Help's Start Here reaches ``open_host_panel`` already deferred."""
    bot = _bot()
    interaction = _interaction(uid=99, done=True)

    await _cog(bot).open_host_panel(interaction, "gone")  # type: ignore[arg-type]

    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with(GAME_ENDED_REPLY, ephemeral=True)
