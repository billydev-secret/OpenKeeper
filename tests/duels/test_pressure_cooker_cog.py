"""Cog-runtime tests for Pressure Cooker: the busted-pump resolution + payout.

The bust path is one of the economy seam's funnel points: the presser who
pushes the gauge over the ceiling loses, the opponent wins, both get paid.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import discord
import pytest_asyncio

from bot_modules.duels.pressure_cooker import db as pdb
from bot_modules.duels.pressure_cooker.cog import PressureCookerDuel
from bot_modules.core.db_utils import open_db
from bot_modules.economy.service import get_balance, save_econ_settings
from bot_modules.games.db import GamesDb
from tests.fakes import FakeEconGamesBot, fake_interaction

GUILD = 9001
CH = 100
P1, P2 = 1, 2


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _econ_cog(db: GamesDb, db_path: Path) -> PressureCookerDuel:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, db_path, [P1, P2])
    return PressureCookerDuel(bot)  # type: ignore[arg-type]


def _texts(view: discord.ui.LayoutView) -> list[str]:
    return [c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)]


async def test_bust_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    gid = await pdb.create_game(db, GUILD, CH, P1, P2, None)
    # Gauge at 99: any roll busts, so the press below always loses for P1.
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=P1, gauge=99, message_id=903)
    edit = AsyncMock(return_value=True)
    cog._edit_card = edit  # type: ignore[method-assign]
    interaction = fake_interaction()
    interaction.user.id = P1
    interaction.guild = cog.bot.guild
    await cog._handle_game_button(interaction, gid)
    g = await pdb.get_game(db, gid)
    assert g.state == "RESOLVED"
    assert g.winner_id == P2
    assert g.loser_id == P1
    assert g.result_message_id == 903

    # The BOOM result replaces the turn card in the same message (spec "One
    # card") — no greyed-out board first, no second post, no ping. The
    # names line leads; the card's own items never repeat either name.
    assert edit.await_count == 1 and edit.await_args.args == (CH, 903)
    interaction.followup.send.assert_not_called()
    result_texts = _texts(edit.await_args.kwargs["view"])
    assert result_texts[0] == f"🏆 **Winner:** U{P2}\n💀 **Loser:** U{P1}"
    assert any("💥 Boom." in t for t in result_texts)
    assert not any("<@" in t for t in result_texts)
    assert not any("Pump" in t for t in result_texts[1:])  # no display name leaks in

    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P2) == 25   # participation + win
        assert get_balance(conn, GUILD, P1) == 5    # participation only


async def test_out_of_turn_press_pays_nothing(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    gid = await pdb.create_game(db, GUILD, CH, P1, P2, None)
    await pdb.set_game_state(db, gid, "ACTIVE", active_player=P1, gauge=99)
    interaction = fake_interaction()
    interaction.user.id = P2  # not their turn
    interaction.guild = cog.bot.guild
    await cog._handle_game_button(interaction, gid)
    g = await pdb.get_game(db, gid)
    assert g.state == "ACTIVE"
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 0
        assert get_balance(conn, GUILD, P2) == 0


def test_game_card_names_players_and_turn_never_mentions(db, sync_db_path):
    """The Turn line held a <@id> (digits to an uncached reader) and absent
    players fell back to a bare id; every slot now goes through the resolver."""
    from bot_modules.duels.pressure_cooker.game import PressureGame

    cog = _econ_cog(db, sync_db_path)
    guild = cog.bot.guild
    guild.members[P1].display_name = "Pump_Er"
    game = PressureGame(
        id=1, guild_id=GUILD, channel_id=CH, message_id=None,
        challenger_id=P1, target_id=P2, state="ACTIVE", active_player=P1,
    )
    items = cog.render_game_state(game, guild)  # type: ignore[arg-type]

    text_items = [i.content for i in items if isinstance(i, discord.ui.TextDisplay)]
    assert any("**Pump\\_Er** vs **U2**" in t for t in text_items)
    assert any("▶️ **Turn:** Pump\\_Er" in t for t in text_items)
    assert not any("<@" in t for t in text_items)
