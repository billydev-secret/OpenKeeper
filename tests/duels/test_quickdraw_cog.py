"""Cog-runtime tests for Quickdraw: VOID, false start, and the payout branches.

These pin the resolution paths the economy seam funnels through: a voided
round pays nothing, every genuinely-played round pays participation + win.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest_asyncio

from bot_modules.duels.quickdraw import db as qdb
from bot_modules.duels.quickdraw.cog import QuickdrawDuel
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


def _econ_cog(db: GamesDb, db_path: Path, *, with_channel: bool = False) -> QuickdrawDuel:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, db_path, [P1, P2], with_channel=with_channel)
    return QuickdrawDuel(bot)  # type: ignore[arg-type]


async def _active_game(db: GamesDb, *, qd_state: str, **extra):
    gid = await qdb.create_game(db, GUILD, CH, P1, P2, None)
    await qdb.set_game_state(db, gid, "ACTIVE", qd_state=qd_state, **extra)
    return await qdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_build_view_has_fire_button(db, sync_db_path):
    view = _econ_cog(db, sync_db_path).build_game_view(9)
    ids = [getattr(c, "custom_id", None) for c in view.walk_children()]
    assert "fire:9" in ids


# ── render hooks: escaping and the no-renaming-the-winner/loser rule ────────────


def _text(items) -> str:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


async def test_render_game_state_returns_layout_items(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    guild = cog.bot.guild
    waiting = await _active_game(db, qd_state="WAITING")
    items = cog.render_game_state(waiting, guild)
    assert isinstance(items, list)
    assert all(isinstance(i, discord.ui.Item) for i in items)
    assert "## 🤠 Quickdraw" in _text(items)


async def test_render_game_state_escapes_a_markdown_display_name(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    guild = cog.bot.guild
    guild.members[P1].display_name = "*Evil*"
    waiting = await _active_game(db, qd_state="WAITING")
    text = _text(cog.render_game_state(waiting, guild))
    assert "*Evil*" not in text
    assert "\\*Evil\\*" in text


async def test_render_result_state_does_not_rename_the_winner_or_loser(db, sync_db_path):
    """The base composes a ``<@winner> <@loser>`` ping line ahead of these
    items (``BaseGame._result_layout``) — re-naming either here would
    violate the guide's "name a person once, never both" rule. Custom
    stakes text (rather than the default nickname stake) keeps the
    "Awaiting Nickname" call-to-action — which addresses the winner by
    name on purpose, a different case — out of this card."""
    cog = _econ_cog(db, sync_db_path)
    guild = cog.bot.guild
    gid = await qdb.create_game(db, GUILD, CH, P1, P2, "loser buys a round")
    now = time.time()
    await qdb.set_game_state(
        db, gid, "ACTIVE", qd_state="COMPLETE",
        winner_id=P1, loser_id=P2, fired_at=now, resolved_at=now,
    )
    game = await qdb.get_game(db, gid)
    text = _text(cog.render_result_state(game, guild))
    assert guild.members[P1].display_name not in text
    assert guild.members[P2].display_name not in text


async def test_render_result_state_escapes_the_nickname_and_original_name(db, sync_db_path):
    """Each escaped exactly once: the typed nick by the copy helper at render,
    the original name by BaseGame where it captures it (so it arrives here
    already escaped, as it does in play)."""
    cog = _econ_cog(db, sync_db_path)
    guild = cog.bot.guild
    game = await _active_game(
        db, qd_state="COMPLETE",
        winner_id=P1, loser_id=P2, fired_at=time.time(), resolved_at=time.time(),
    )
    text = _text(
        cog.render_result_state(
            game, guild, imposed_nick="_Clown_",
            original_name=discord.utils.escape_markdown("*Old*"),
        )
    )
    assert "_Clown_" not in text
    assert "\\_Clown\\_" in text
    assert "*Old*" not in text
    assert "\\*Old\\*" in text
    assert "\\\\" not in text  # never escaped twice


# ── VOID: nobody fired ─────────────────────────────────────────────────────────

async def test_void_when_nobody_fires_pays_nothing(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _active_game(db, qd_state="DRAW", fired_at=time.time())
    await cog._fire_void(game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "VOID"
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 0
        assert get_balance(conn, GUILD, P2) == 0


async def test_a_real_draw_window_firing_is_not_cancelled_by_its_own_void(db, sync_db_path):
    # The prod-only timer bug (Mahjong, 2026-08-23): _fire_void runs inside
    # the draw-window task and its tail _cancel_timer pops that same task.
    # Cancelling the running task lands CancelledError on its next await, so
    # only a real firing reproduces it — calling _fire_void from the test
    # task cancels nothing. Today no await follows, so the task just ends
    # cancelled; one added await would silently drop the rest of the void.
    cog = _econ_cog(db, sync_db_path)
    game = await _active_game(db, qd_state="DRAW", fired_at=time.time())
    task = asyncio.create_task(cog._run_drawwindow_timer(game.id, 0.01))
    cog._timers[game.id] = task
    await asyncio.wait({task}, timeout=2)
    assert (await qdb.get_game(db, game.id)).state == "VOID"
    assert not task.cancelled()


# ── False start: presser loses immediately ─────────────────────────────────────

async def test_false_start_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _active_game(db, qd_state="WAITING")
    interaction = fake_interaction()
    interaction.user.id = P2
    interaction.guild = cog.bot.guild
    interaction.followup.send.return_value = SimpleNamespace(id=901)
    await cog._handle_game_button(interaction, game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == P1
    assert g.loser_id == P2
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25   # participation + win
        assert get_balance(conn, GUILD, P2) == 5    # participation only


# ── Winner fired, opponent answers second ──────────────────────────────────────

async def test_opponent_second_fire_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    now = time.time()
    game = await _active_game(
        db, qd_state="WINNER_FIRED",
        winner_id=P1, loser_id=P2, fired_at=now - 1.0, resolved_at=now - 0.5,
    )
    interaction = fake_interaction()
    interaction.user.id = P2
    interaction.guild = cog.bot.guild
    interaction.followup.send.return_value = SimpleNamespace(id=902)
    await cog._handle_game_button(interaction, game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.loser_fired_at is not None
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25
        assert get_balance(conn, GUILD, P2) == 5


# ── Winner fired, opponent never answers (draw window expires) ─────────────────

async def test_winner_fired_void_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path, with_channel=True)
    edit = AsyncMock(return_value=True)
    cog._edit_card = edit  # type: ignore[method-assign]
    now = time.time()
    game = await _active_game(
        db, qd_state="WINNER_FIRED", message_id=640,
        winner_id=P1, loser_id=P2, fired_at=now - 6.0, resolved_at=now - 5.5,
    )
    await cog._fire_void(game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.qd_state == "COMPLETE"
    # The result is the duel's own card, edited — not a fresh post.
    assert edit.await_count == 1 and edit.await_args.args == (CH, 640)
    assert g.result_message_id == 640
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25
        assert get_balance(conn, GUILD, P2) == 5


async def test_winner_fired_void_channel_gone_still_resolves_and_pays(db, sync_db_path):
    """A vanished channel used to pay but leave the row ACTIVE for the sweep
    to abandon; now the round terminalizes so the economy seam sees it."""
    cog = _econ_cog(db, sync_db_path)  # no channel resolvable
    now = time.time()
    game = await _active_game(
        db, qd_state="WINNER_FIRED",
        winner_id=P1, loser_id=P2, fired_at=now - 6.0, resolved_at=now - 5.5,
    )
    await cog._fire_void(game.id)
    g = await qdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.result_message_id is None
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25
        assert get_balance(conn, GUILD, P2) == 5
