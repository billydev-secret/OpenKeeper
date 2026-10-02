"""Cog-runtime tests for Musical Chairs: the MUSIC→SCRAMBLE→ELIMINATE machine."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio

from bot_modules.duels.musical_chairs import db as mcdb
from bot_modules.duels.musical_chairs.cog import MusicalChairsCog
from bot_modules.duels.musical_chairs.views import SitView
from bot_modules.core.db_utils import open_db
from bot_modules.economy.service import get_balance, save_econ_settings
from bot_modules.games.db import GamesDb
from tests.fakes import (
    FakeEconGamesBot,
    FakeGuild,
    FakeMessageableChannel,
    fake_interaction,
)

GUILD = 9001
CH = 100


def _texts(items) -> list[str]:
    """Every TextDisplay's content in a rendered layout list, for substring
    assertions — the layout twin of reading an embed's description/fields."""
    return [c.content for c in items if isinstance(c, discord.ui.TextDisplay)]


class FakeBot:
    def __init__(self, db: GamesDb) -> None:
        self.games_db = db

    def add_view(self, *a, **k) -> None:
        pass

    def get_guild(self, gid):
        return None

    def get_channel(self, cid):
        return None


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest_asyncio.fixture
async def cog(db: GamesDb) -> MusicalChairsCog:
    return MusicalChairsCog(FakeBot(db))  # type: ignore[arg-type]


async def _game(
    db, *, phase, alive, seated=None, elim=None, stakes=None, reruns=0, message_id=None
):
    gid = await mcdb.create_lobby(db, GUILD, CH, alive[0], stakes)
    await mcdb.set_game_state(
        db, gid, "ACTIVE",
        phase=phase, round=1,
        chairs=max(0, len(alive) - 1),
        alive=json.dumps(alive),
        seated=json.dumps(seated or []),
        elimination_order=json.dumps(elim or []),
        reruns=reruns,
        message_id=message_id,
    )
    return await mcdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_cog_exposes_group(db):
    cog = MusicalChairsCog(FakeBot(db))  # type: ignore[arg-type]
    assert "musicalchairs" in {c.name for c in cog.get_app_commands()}


def test_build_view_has_sit_button(db):
    view = MusicalChairsCog(FakeBot(db)).build_game_view(8)  # type: ignore[arg-type]
    ids = {getattr(item, "custom_id", None) for item in view.walk_children()}
    assert "mc_sit:8" in ids


# ── render hooks wire the cog's DB state into the pure layout builders ─────────
# Builder behaviour itself (paradigm shape, escaping, budget) lives in
# tests/duels/test_musical_chairs_layouts.py; these are wiring checks only.

async def test_render_game_state_returns_layout_items_no_embed(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    rendered = cog.render_game_state(game, FakeGuild())
    assert isinstance(rendered, list)
    assert all(isinstance(i, discord.ui.Item) for i in rendered)
    assert not isinstance(rendered, discord.Embed)


async def test_render_result_state_returns_layout_items_no_embed(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[2])
    await cog._close_round_locked(game)
    g = await mcdb.get_game(db, game.id)
    rendered = cog.render_result_state(g, FakeGuild())
    assert isinstance(rendered, list)
    assert all(isinstance(i, discord.ui.Item) for i in rendered)


# ── _close_round_locked ────────────────────────────────────────────────────────

async def test_close_round_non_terminal_next_round(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2, 3])
    try:
        resolved = await cog._close_round_locked(game)
        assert resolved is False
        g = await mcdb.get_game(db, game.id)
        assert g.state == "ACTIVE"
        assert g.phase == "MUSIC"
        assert g.alive == [2, 3]
        assert g.elimination_order == [1]
        assert g.round == 2
    finally:
        cog._cancel_timer(game.id)


async def test_close_round_non_terminal_edits_panel_to_music_face(announce_cog, db):
    """The survive-the-round branch re-renders the panel in place through the
    layout arm — this is the commit's own new mechanism, not just the DB
    write ``test_close_round_non_terminal_next_round`` already covers."""
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2, 3], message_id=555)
    edits = AsyncMock()
    announce_cog._edit_message_silent = edits  # type: ignore[method-assign]
    try:
        resolved = await announce_cog._close_round_locked(game)
        assert resolved is False
    finally:
        announce_cog._cancel_timer(game.id)
    assert edits.await_args.kwargs.keys() == {"layout"}
    view = edits.await_args.kwargs["layout"]
    assert isinstance(view, SitView)
    assert any("don't sit yet" in t for t in _texts(view.children))


async def test_close_round_terminal_resolves(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[2])
    resolved = await cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 2
    assert g.loser_id == 1


async def test_close_round_no_show_multi_elim(cog, db):
    # 3 alive, only player 1 sat → 2 and 3 both out → 1 wins
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[1])
    resolved = await cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 1
    assert g.loser_id in (2, 3)  # last eliminated of the pair


# ── a round nobody sat in (duels-party-114) ────────────────────────────────────

@pytest.mark.parametrize(
    ("alive", "seated"),
    [
        pytest.param([1, 2], [], id="final-round"),
        pytest.param([1, 2, 3], [], id="early-round"),
    ],
)
async def test_close_round_with_no_sitter_reruns_instead_of_resolving(cog, db, alive, seated):
    """Two left, one chair, both miss the scramble: the later-listed player
    used to be declared winner AND runner-up and take the pot. The round
    re-runs — same players, fresh music, no terminal write."""
    game = await _game(db, phase="SCRAMBLE", alive=alive, seated=seated)
    try:
        resolved = await cog._close_round_locked(game)
        assert resolved is False
        g = await mcdb.get_game(db, game.id)
        assert g.state == "ACTIVE"
        assert g.phase == "MUSIC"
        assert g.alive == alive
        assert g.elimination_order == []
        assert g.seated == []
        assert g.reruns == 1
        assert g.winner_id is None and g.loser_id is None
        assert g.round == 1
    finally:
        cog._cancel_timer(game.id)


async def test_close_round_with_no_sitter_twice_voids_and_pays_nobody(db, sync_db_path):
    econ_cog = _econ_cog(db, sync_db_path, (1, 2))
    edits = AsyncMock()
    econ_cog._edit_message_silent = edits  # type: ignore[method-assign]
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[], reruns=1, message_id=555)
    resolved = await econ_cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, game.id)
    assert g.state == "VOID"
    assert g.winner_id is None and g.loser_id is None
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 0
        assert get_balance(conn, GUILD, 2) == 0
    # the called-off notice: the terminal control-free card, not the live panel
    assert edits.await_args.kwargs.keys() == {"layout"}
    notice = edits.await_args.kwargs["layout"]
    assert any("Called Off" in t for t in _texts(notice.children))


async def test_a_round_with_a_sitter_resets_the_rerun_count(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2, 3], reruns=1)
    try:
        resolved = await cog._close_round_locked(game)
        assert resolved is False
        g = await mcdb.get_game(db, game.id)
        assert g.reruns == 0
        assert g.alive == [2, 3]
    finally:
        cog._cancel_timer(game.id)


async def test_no_sitter_rerun_is_announced(announce_cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[], message_id=555)
    edits = AsyncMock()
    announce_cog._edit_message_silent = edits  # type: ignore[method-assign]
    try:
        await announce_cog._close_round_locked(game)
        assert any("Nobody sat" in t for t in announce_cog.bot.channel.texts)
    finally:
        announce_cog._cancel_timer(game.id)
    # _rerun_round's own panel edit: the re-armed MUSIC face through the
    # layout arm, not just the public announcement above.
    assert edits.await_args.kwargs.keys() == {"layout"}
    view = edits.await_args.kwargs["layout"]
    assert isinstance(view, SitView)
    assert any("don't sit yet" in t for t in _texts(view.children))


def _econ_cog(db, db_path, member_ids=(1, 2, 3)):
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    return MusicalChairsCog(FakeEconGamesBot(db, db_path, member_ids))  # type: ignore[arg-type]


async def test_close_round_terminal_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[2])
    await mcdb.set_game_state(db, game.id, "ACTIVE", roster=json.dumps([1, 2, 3]))
    game = await mcdb.get_game(db, game.id)
    resolved = await cog._close_round_locked(game)
    assert resolved is True
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 2) == 25   # winner: participation + win
        assert get_balance(conn, GUILD, 1) == 5    # participation only
        assert get_balance(conn, GUILD, 3) == 5    # eliminated earlier, still played


async def test_a_real_scramble_firing_is_not_cancelled_by_its_own_void(db, sync_db_path):
    # The scramble timer closes the round from inside its own task, and
    # _void_no_sitter → on_game_resolved → _cancel_timer pops that same
    # task. A cancel of the running task lands on its next await (the
    # prod-only Mahjong bug), so only a real firing reproduces it.
    cog = _econ_cog(db, sync_db_path, (1, 2))
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[], reruns=1)
    task = asyncio.create_task(cog._run_scramble_timer(game.id, 0.01))
    cog._timers[game.id] = task
    await asyncio.wait({task}, timeout=2)
    assert (await mcdb.get_game(db, game.id)).state == "VOID"
    assert not task.cancelled()


async def test_close_round_degenerate_no_winner_pays_nothing(db, sync_db_path):
    """The winner=None branch: nobody left to seat, nobody eliminated this
    round. The game is left ACTIVE for the sweep to abandon; no payout."""
    econ_cog = _econ_cog(db, sync_db_path)
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, None)
    await mcdb.set_game_state(
        db, gid, "ACTIVE",
        phase="SCRAMBLE", round=1, chairs=0,
        roster=json.dumps([1, 2, 3]),
        alive="[]", seated="[]", elimination_order="[]",
    )
    game = await mcdb.get_game(db, gid)
    resolved = await econ_cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, gid)
    assert g.state == "ACTIVE"  # no terminal write — the sweep abandons it
    await econ_cog._expire_active(g)
    g = await mcdb.get_game(db, gid)
    assert g.state == "ABANDONED"
    with open_db(sync_db_path) as conn:
        for uid in (1, 2, 3):
            assert get_balance(conn, GUILD, uid) == 0


# ── _on_sit: false start (MUSIC) ───────────────────────────────────────────────

async def test_on_sit_false_start_eliminates(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.alive == [2, 3]
    assert g.elimination_order == [1]
    interaction.followup.send.assert_awaited()  # "you sat too early"


async def test_on_sit_false_start_terminal(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 2
    assert g.loser_id == 1


# ── _on_sit: scramble seat claim ───────────────────────────────────────────────

async def test_on_sit_scramble_claims_seat(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.seated == [2]
    assert g.state == "ACTIVE"
    interaction.edit_original_response.assert_awaited()


async def test_on_sit_scramble_close_on_fill(cog, db):
    # 3 alive, 2 chairs; player 2 already seated, player 3 sits → seats full → close
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 3
    try:
        await cog._on_sit(interaction, game.id)
        g = await mcdb.get_game(db, game.id)
        assert g.alive == [2, 3]          # player 1 (never sat) is out
        assert g.elimination_order == [1]
        assert g.phase == "MUSIC"          # next round
    finally:
        cog._cancel_timer(game.id)


async def test_on_sit_rejects_double_sit(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2  # already seated
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.seated == [2]
    interaction.followup.send.assert_awaited()


async def test_on_sit_rejects_outsider(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 999
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.seated == []


# ── _start_scramble transition ─────────────────────────────────────────────────

async def test_start_scramble_flips_phase(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    try:
        await cog._start_scramble(game.id)
        g = await mcdb.get_game(db, game.id)
        assert g.phase == "SCRAMBLE"
        assert g.chairs == 2
        assert g.seated == []
    finally:
        cog._cancel_timer(game.id)


# ── lobby start (custom-stakes mode skips nick preflight) ──────────────────────

async def test_lobby_start_begins_music(cog, db):
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await mcdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2, 3]), message_id=42)
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    try:
        await cog._handle_lobby_start(interaction, gid)
        g = await mcdb.get_game(db, gid)
        assert g.state == "ACTIVE"
        assert g.phase == "MUSIC"
        assert g.alive == [1, 2, 3]
        assert g.chairs == 2
    finally:
        cog._cancel_timer(gid)


async def test_lobby_start_below_min_players_refused(cog, db):
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await mcdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]))  # min is 3
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    await cog._handle_lobby_start(interaction, gid)
    assert (await mcdb.get_game(db, gid)).state == "LOBBY"


# ── announcing every elimination, and why (game night 2026-08-21) ──────────────


class _AnnounceBot(FakeBot):
    """FakeBot with a resolvable guild and a real Messageable channel, so the
    public call-outs actually land somewhere a test can read."""

    def __init__(self, db: GamesDb, member_ids=(1, 2, 3, 4)) -> None:
        super().__init__(db)
        self.channel = FakeMessageableChannel(CH)
        self.guild = FakeGuild(
            id=GUILD,
            members={
                uid: SimpleNamespace(id=uid, display_name=f"U{uid}", mention=f"<@{uid}>")
                for uid in member_ids
            },
        )

    def get_guild(self, gid):
        return self.guild if gid == GUILD else None

    def get_channel(self, cid):
        return self.channel


@pytest_asyncio.fixture
async def announce_cog(db: GamesDb) -> MusicalChairsCog:
    return MusicalChairsCog(_AnnounceBot(db))  # type: ignore[arg-type]


async def test_false_start_is_announced_publicly(announce_cog, db):
    """An early sit used to be an ephemeral only, so the room watched the
    survivor count drop with no idea who had gone or why."""
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await announce_cog._on_sit(interaction, game.id)
    posts = announce_cog.bot.channel.texts
    assert any("U1" in t and "sat before the music stopped" in t for t in posts)
    assert any("2 left" in t for t in posts)


async def test_false_start_ephemeral_says_what_went_wrong(announce_cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await announce_cog._on_sit(interaction, game.id)
    (text,) = interaction.followup.send.await_args.args
    assert "music was still playing" in text


async def test_final_round_eliminations_are_announced(announce_cog, db):
    """The last round resolved straight into the result embed, so whoever
    missed the final chair was never named."""
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[2])
    await announce_cog._close_round_locked(game)
    assert any("U1" in t and "didn't find a chair" in t
               for t in announce_cog.bot.channel.texts)


async def test_terminal_false_start_still_announced(announce_cog, db):
    """Eliminating the second-to-last player ends the game — the call-out has
    to go out before the result embed, not be swallowed by it."""
    game = await _game(db, phase="MUSIC", alive=[1, 2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await announce_cog._on_sit(interaction, game.id)
    assert any("sat before the music stopped" in t
               for t in announce_cog.bot.channel.texts)


# ── the panel follows the conversation down (game night 2026-08-21) ───────────


async def test_scramble_reposts_panel_at_channel_bottom(announce_cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    await mcdb.set_game_state(db, game.id, "ACTIVE", message_id=555)
    try:
        await announce_cog._start_scramble(game.id)
    finally:
        announce_cog._cancel_timer(game.id)
    channel = announce_cog.bot.channel
    g = await mcdb.get_game(db, game.id)
    assert g.message_id != 555          # a brand-new message, at the bottom
    assert channel.deleted == [555]     # and the buried one is cleaned up
    posted = [s["view"] for s in channel.sent if s.get("view")]
    assert posted
    assert any("Sit" in t for t in _texts(posted[-1].children))


async def test_repost_keeps_old_panel_when_send_fails(announce_cog, db):
    """Post-before-delete: a channel the bot can no longer post in must keep
    its working panel rather than lose it to the move."""
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    await mcdb.set_game_state(db, game.id, "ACTIVE", message_id=555)
    channel = announce_cog.bot.channel

    async def _boom(*a, **k):
        raise discord.Forbidden(MagicMock(status=403), "no send")

    channel.send = _boom  # type: ignore[method-assign]
    moved = await announce_cog._repost_panel(game.id, announce_cog.bot.guild)
    assert moved is False
    assert channel.deleted == []
    g = await mcdb.get_game(db, game.id)
    assert g.message_id == 555


async def test_start_scramble_falls_back_to_edit_when_repost_fails(announce_cog, db):
    """When the move-to-bottom repost can't land (channel gone quiet on the
    bot), ``_start_scramble`` falls back to editing the old panel in place
    through the layout arm rather than leaving it stuck on the MUSIC face."""
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    await mcdb.set_game_state(db, game.id, "ACTIVE", message_id=555)
    channel = announce_cog.bot.channel

    async def _boom(*a, **k):
        raise discord.Forbidden(MagicMock(status=403), "no send")

    channel.send = _boom  # type: ignore[method-assign]
    edits = AsyncMock()
    announce_cog._edit_message_silent = edits  # type: ignore[method-assign]
    try:
        await announce_cog._start_scramble(game.id)
    finally:
        announce_cog._cancel_timer(game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.message_id == 555          # never moved — the repost never landed
    assert edits.await_args.kwargs.keys() == {"layout"}
    view = edits.await_args.kwargs["layout"]
    assert isinstance(view, SitView)
    assert any("Sit now" in t for t in _texts(view.children))


async def test_lobby_card_explains_the_rules(announce_cog, db):
    """Nobody could find the rules before the first round, so three separate
    players sat during the music without ever being told not to."""
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, None)
    await mcdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2, 3]))
    game = await mcdb.get_game(db, gid)
    items = await announce_cog._lobby_view(game, announce_cog.bot.guild, 3, 10)
    how = next(t for t in _texts(items) if "How to Play" in t)
    assert "don't" in how.lower() or "do nothing" in how.lower()
    assert "Last player seated wins" in how


async def test_music_phase_card_warns_against_sitting_early(announce_cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    rendered = announce_cog.render_game_state(game, announce_cog.bot.guild)
    assert any("don't sit yet" in t for t in _texts(rendered))


async def test_sit_survives_the_panel_being_reposted_mid_press(announce_cog, db):
    """A press already in flight when the round flips is answered against the
    old panel, which _repost_panel has since deleted. The seat still counts and
    the dead edit must not escape the button callback."""
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2
    interaction.edit_original_response = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "panel was moved")
    )

    await announce_cog._on_sit(interaction, game.id)

    g = await mcdb.get_game(db, game.id)
    assert g.seated == [2]      # the seat is recorded regardless
    assert g.state == "ACTIVE"
