"""``/games end`` on a game that owns a recap ending.

Would You Rather, Never Have I Ever and Most Likely To end themselves with a
recap through the paying path (``end_with_recap``); ``/games end`` hands them
the close so the room sees that card rather than the red Force-Closed one
(vote-games-52 / discovery-3). Anything else — an unknown type, a cog that is
not loaded, an MLT lobby with nothing to recap — keeps the force-close path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.config.cog as mod
from bot_modules.games.config.cog import GamesConfigCog
from bot_modules.games.utils.game_manager import create_game, get_active_game_by_id
from bot_modules.games.db import GamesDb
from tests.fakes import FakeMessageableChannel

HOST = 1


def _cog(db_path, cogs: dict):
    bot = SimpleNamespace(
        games_db=GamesDb(db_path), active_views={}, ctx=SimpleNamespace(db_path=db_path),
        get_cog=lambda name: cogs.get(name),
    )
    return GamesConfigCog(bot)  # type: ignore[arg-type]


def _interaction(channel):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    # Answered once anything has been sent — what /games host's dispatcher
    # reads to know whether some game opened a panel.
    response.is_done = lambda: response.send_message.await_count > 0
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host", roles=[]),
        guild=None, guild_id=None, channel_id=channel.id, channel=channel,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _view_text(view: discord.ui.LayoutView) -> str:
    """Every TextDisplay in a card's component tree, however deeply
    nested, joined — the view-layer read of what used to be embed text."""
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


async def _confirm(interaction):
    """Press "Yes, End Game" on the confirmation the command sent."""
    view = interaction.response.send_message.await_args.kwargs["view"]
    confirm = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    await view.confirm.callback(confirm)


@pytest.mark.parametrize("game_type, cog_name", [("wyr", "WYRCog"), ("nhie", "NHIECog"), ("mlt", "MLTCog")])
async def test_games_end_hands_the_close_to_the_games_recap_ending(sync_db_path, game_type, cog_name):
    finisher = AsyncMock(return_value=True)
    cog = _cog(sync_db_path, {cog_name: SimpleNamespace(end_with_recap=finisher)})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, game_type, state="playing", payload={"rounds": {"1": {}}})
    interaction = _interaction(channel)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]
    await _confirm(interaction)

    finisher.assert_awaited_once_with(channel, gid)
    assert channel.sent == [], "no Force-Closed card when the game posted its own recap"


async def test_games_end_falls_back_to_force_close_when_there_is_nothing_to_recap(sync_db_path, monkeypatch):
    """An MLT lobby that never started answers False from its finisher."""
    finisher = AsyncMock(return_value=False)
    cog = _cog(sync_db_path, {"MLTCog": SimpleNamespace(end_with_recap=finisher)})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, "mlt", state="joining", payload={"rounds": {}, "players": []})
    force_end = AsyncMock()
    monkeypatch.setattr(mod, "force_end_active_game", force_end)
    interaction = _interaction(channel)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]
    await _confirm(interaction)

    finisher.assert_awaited_once_with(channel, gid)
    force_end.assert_awaited_once()
    assert channel.sent and "Force-Closed" in _view_text(channel.sent[-1]["view"])


async def test_games_end_force_closes_a_game_whose_cog_is_not_loaded(sync_db_path):
    cog = _cog(sync_db_path, {})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, "wyr", state="playing", payload={"rounds": {}})
    interaction = _interaction(channel)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]
    await _confirm(interaction)

    assert await get_active_game_by_id(cog.db, gid) is None
    assert channel.sent and "Force-Closed" in _view_text(channel.sent[-1]["view"])


# ── /games host: dispatch to the game's own host panel ──────────────────────


async def _opens_a_panel(interaction, *_):
    """What a real opener does: answer the press with its panel."""
    await interaction.response.send_message(view="panel", ephemeral=True)
    return True


async def test_games_host_opens_the_games_host_panel(sync_db_path):
    opener = AsyncMock(side_effect=_opens_a_panel)
    cog = _cog(sync_db_path, {"TraditionalCog": SimpleNamespace(open_host_panel=opener)})
    channel = FakeMessageableChannel(100)
    gid = await create_game(cog.db, channel.id, HOST, "traditional", state="joining", payload={})
    interaction = _interaction(channel)

    await GamesConfigCog.games_host.callback(cog, interaction)  # type: ignore[attr-defined]

    opener.assert_awaited_once_with(interaction, gid)
    interaction.response.send_message.assert_awaited_once_with(view="panel", ephemeral=True)


@pytest.mark.parametrize(
    "game_type, cogs, reply",
    [
        pytest.param(None, {}, mod.NO_ACTIVE_GAME_REPLY, id="no-game"),
        pytest.param("clapback", {"ClapbackCog": SimpleNamespace()}, mod.NO_HOST_PANEL_REPLY, id="game-without-a-panel"),
        pytest.param("traditional", {}, mod.NO_HOST_PANEL_REPLY, id="cog-not-loaded"),
    ],
)
async def test_games_host_refuses_quietly_without_a_panel_to_open(sync_db_path, game_type, cogs, reply):
    cog = _cog(sync_db_path, cogs)
    channel = FakeMessageableChannel(100)
    if game_type:
        await create_game(cog.db, channel.id, HOST, game_type, state="playing", payload={})
    interaction = _interaction(channel)

    await GamesConfigCog.games_host.callback(cog, interaction)  # type: ignore[attr-defined]

    interaction.response.send_message.assert_awaited_once_with(reply, ephemeral=True)


async def test_games_host_falls_through_to_the_cogs_that_track_their_own_games(sync_db_path):
    """Mahjong tables and Risky rounds are not ``games_active_games`` rows;
    with no shared row the channel is offered to their cogs."""
    seen = AsyncMock(side_effect=_opens_a_panel)
    cog = _cog(sync_db_path, {"MahjongCog": SimpleNamespace(open_channel_host_panel=seen)})
    interaction = _interaction(FakeMessageableChannel(100))

    await GamesConfigCog.games_host.callback(cog, interaction)  # type: ignore[attr-defined]

    seen.assert_awaited_once_with(interaction)
    interaction.response.send_message.assert_awaited_once_with(view="panel", ephemeral=True)
