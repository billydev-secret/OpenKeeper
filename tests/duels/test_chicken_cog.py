"""Cog-runtime tests for Chicken: crash resolution, bail flow, wipeout."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.duels.chicken import db as chdb
from bot_modules.duels.chicken.cog import ChickenCog
from bot_modules.core.db_utils import open_db
from bot_modules.economy.service import get_balance, save_econ_settings
from bot_modules.games.db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeGuild, fake_interaction

GUILD = 9001
CH = 100


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
async def cog(db: GamesDb) -> ChickenCog:
    return ChickenCog(FakeBot(db))  # type: ignore[arg-type]


async def _climbing(db, *, alive, bail_log=None, roster=None, stakes=None, crash_at=15.0):
    roster = roster or alive
    gid = await chdb.create_lobby(db, GUILD, CH, roster[0], stakes)
    now = time.time()
    await chdb.set_game_state(
        db, gid, "ACTIVE",
        phase="CLIMBING",
        roster=json.dumps(roster),
        alive=json.dumps(alive),
        bail_log=json.dumps(bail_log or []),
        climb_started_at=now - 5.0,
        climb_duration=25.0,
        crash_at=crash_at,
    )
    return await chdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_cog_exposes_group(db):
    cog = ChickenCog(FakeBot(db))  # type: ignore[arg-type]
    assert "chicken" in {c.name for c in cog.get_app_commands()}


def test_build_view_has_bail_button(db):
    view = ChickenCog(FakeBot(db)).build_game_view(9)  # type: ignore[arg-type]
    ids = [getattr(c, "custom_id", None) for c in view.walk_children()]
    assert "chicken_bail:9" in ids


# ── _crash resolution ──────────────────────────────────────────────────────────

async def test_crash_with_bailer_nicks_one_crasher(cog, db):
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    await cog._crash(game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 3          # bravest bailer
    assert g.loser_id in (1, 2)      # one crasher, drawn at random (duels-party-123)


async def test_crash_total_wipeout_no_nick(cog, db):
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    await cog._crash(game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED_NO_NICK"
    assert g.winner_id is None
    assert g.loser_id is None


async def test_a_real_crash_firing_is_not_cancelled_by_its_own_resolution(cog, db):
    # _crash runs inside the crash-timer task, and on_game_resolved's
    # _cancel_timers pops that same task along with the ticker. A cancel of
    # the running task lands on its next await (the prod-only Mahjong bug),
    # so only a real firing reproduces it.
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    task = asyncio.create_task(cog._run_crash_timer(game.id, 0.01))
    cog._add_timer(game.id, task)
    await asyncio.wait({task}, timeout=2)
    assert (await chdb.get_game(db, game.id)).state == "RESOLVED_NO_NICK"
    assert not task.cancelled()


async def test_crash_sets_group_cooldowns(cog, db):
    from bot_modules.duels import db as duels_db
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    await cog._crash(game.id)
    for uid in (1, 2, 3):
        assert await duels_db.check_group_cooldown(db, GUILD, "chicken", uid, 48) is not None


# ── economy payouts (Stage 1 faucet) ─────────────────────────────────────────


async def test_crash_pays_winner_and_losers(db, sync_db_path):
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 75.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)  # winner = 3 (bravest bailer)
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 3) == 25  # participation + win
        assert get_balance(conn, GUILD, 1) == 5   # participation only
        assert get_balance(conn, GUILD, 2) == 5


async def test_crash_no_payout_when_disabled(db, sync_db_path):
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)  # total wipeout, economy disabled
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 0


async def test_wipeout_pays_participation_only(db, sync_db_path):
    """Total wipeout (winner=None) still reaches the payout: everyone gets
    participation, nobody gets the win bonus."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._crash(game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED_NO_NICK"
    assert g.winner_id is None
    with open_db(sync_db_path) as conn:
        for uid in (1, 2, 3):
            assert get_balance(conn, GUILD, uid) == 5


async def test_expire_active_abandons_without_payout(db, sync_db_path):
    """An abandoned game terminalizes with zero economy effect — the most
    important branch for the wager refund path (stage 4b) to hook later."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = ChickenCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    game = await _climbing(db, alive=[1, 2], bail_log=[], roster=[1, 2, 3])
    assert game is not None
    await cog._expire_active(game)
    g = await chdb.get_game(db, game.id)
    assert g.state == "ABANDONED"
    with open_db(sync_db_path) as conn:
        for uid in (1, 2, 3):
            assert get_balance(conn, GUILD, uid) == 0


# ── _on_bail flow ──────────────────────────────────────────────────────────────

async def test_bail_removes_player_and_continues(cog, db):
    game = await _climbing(db, alive=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2
    await cog._on_bail(interaction, game.id)
    g = await chdb.get_game(db, game.id)
    assert g.alive == [1, 3]
    assert len(g.bail_log) == 1
    assert g.bail_log[0]["player_id"] == 2
    assert g.state == "ACTIVE"
    interaction.edit_original_response.assert_awaited()


async def test_bail_last_holder_everyone_blinked(cog, db):
    game = await _climbing(db, alive=[1], bail_log=[
        {"player_id": 2, "bail_ts": time.time(), "meter_pct": 40.0}
    ], roster=[1, 2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await cog._on_bail(interaction, game.id)
    g = await chdb.get_game(db, game.id)
    assert g.state == "RESOLVED_NO_NICK"
    assert g.winner_id == 1          # last to bail wins, no nick
    assert g.loser_id is None


async def test_bail_rejects_already_bailed(cog, db):
    game = await _climbing(db, alive=[1, 3], bail_log=[
        {"player_id": 2, "bail_ts": time.time(), "meter_pct": 30.0}
    ], roster=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2  # already bailed → not in alive
    await cog._on_bail(interaction, game.id)
    g = await chdb.get_game(db, game.id)
    assert g.alive == [1, 3]
    interaction.followup.send.assert_awaited()


# ── lobby start (custom-stakes mode skips nick preflight) ──────────────────────

async def test_lobby_start_begins_climb(cog, db):
    gid = await chdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await chdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]), message_id=42)
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    try:
        await cog._handle_lobby_start(interaction, gid)
        g = await chdb.get_game(db, gid)
        assert g.state == "ACTIVE"
        assert g.phase == "CLIMBING"
        assert g.alive == [1, 2]
        assert g.climb_started_at is not None
    finally:
        cog._cancel_timers(gid)


# ── hidden crash point (duels-party-113) ───────────────────────────────────────

@pytest.mark.parametrize(
    ("min_climb", "max_climb"),
    [
        pytest.param(10.0, 25.0, id="defaults"),
        pytest.param(8.0, 12.0, id="tight-range"),
        pytest.param(20.0, 20.0, id="fixed"),
    ],
)
async def test_start_rolls_a_hidden_crash_inside_the_dials(cog, db, min_climb, max_climb):
    """The crash is rolled in [min_climb, max_climb] and the meter is drawn
    over max_climb, so the bar can blow partway up — the old fixed, public
    climb_duration let a table count the crash out."""
    await chdb.upsert_config(db, GUILD, min_climb=min_climb, max_climb=max_climb)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, None)
    await chdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]), alive="[1, 2]")
    game = await chdb.get_game(db, gid)
    try:
        await cog.on_game_start(game)
        g = await chdb.get_game(db, gid)
        assert g.climb_duration == pytest.approx(max_climb)
        assert g.crash_at is not None
        assert min_climb <= g.crash_at <= max_climb
    finally:
        cog._cancel_timers(gid)


async def test_start_never_inverts_a_ceiling_below_the_floor(cog, db):
    await chdb.upsert_config(db, GUILD, min_climb=30.0, max_climb=12.0)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, None)
    await chdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]), alive="[1, 2]")
    game = await chdb.get_game(db, gid)
    try:
        await cog.on_game_start(game)
        g = await chdb.get_game(db, gid)
        assert g.crash_at == pytest.approx(30.0)
        assert g.climb_duration == pytest.approx(30.0)
    finally:
        cog._cancel_timers(gid)


async def test_resume_schedules_the_crash_from_crash_at_not_the_meter(cog, db, monkeypatch):
    """After a restart the crash timer must fire at the rolled crash point,
    while the ticker keeps drawing the bar to its ceiling."""
    scheduled: list[tuple[float, float]] = []
    monkeypatch.setattr(
        cog, "_schedule", lambda gid, crash_in, total: scheduled.append((crash_in, total))
    )
    game = await _climbing(db, alive=[1, 2], crash_at=15.0)  # started 5s ago, meter 25s
    await cog.on_game_resume(game)
    (crash_in, total), = scheduled
    assert crash_in == pytest.approx(10.0, abs=0.5)
    assert total == pytest.approx(20.0, abs=0.5)


async def test_resume_of_a_pre_migration_row_crashes_at_the_meter_end(cog, db, monkeypatch):
    scheduled: list[tuple[float, float]] = []
    monkeypatch.setattr(
        cog, "_schedule", lambda gid, crash_in, total: scheduled.append((crash_in, total))
    )
    game = await _climbing(db, alive=[1, 2], crash_at=None)
    await cog.on_game_resume(game)
    (crash_in, total), = scheduled
    assert crash_in == pytest.approx(20.0, abs=0.5)
    assert total == pytest.approx(20.0, abs=0.5)


def _card_text(items) -> str:
    return "\n".join(getattr(i, "content", "") for i in items)


async def test_live_card_does_not_promise_a_crash_at_100(cog, db):
    game = await _climbing(db, alive=[1, 2])
    text = _card_text(cog.render_game_state(game, FakeGuild()))
    assert "crash at 100%" not in text
    assert "hidden" in text


async def test_crash_card_names_the_meter_reading_and_the_draw(cog, db):
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 40.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3], crash_at=15.0)
    game.winner_id, game.loser_id = 3, 2
    text = _card_text(cog.render_result_state(game, FakeGuild()))
    assert "Crash at 60%!" in text
    assert "drawn at random" in text


async def test_crash_card_never_names_the_pinged_winner_or_loser(cog, db):
    """The base's result-card ping line already names both — this card's
    own items must not repeat either name (guide § Mentions)."""
    bail = [{"player_id": 3, "bail_ts": time.time(), "meter_pct": 40.0}]
    game = await _climbing(db, alive=[1, 2], bail_log=bail, roster=[1, 2, 3], crash_at=15.0)
    game.winner_id, game.loser_id = 3, 2
    text = _card_text(cog.render_result_state(game, FakeGuild()))
    assert "<@3>" not in text
    assert "<@2>" not in text


async def test_wipeout_card_says_the_pot_is_refunded(cog, db):
    """A total wipeout used to end on 'No winner, no nicknames' with the
    refund only visible as a separate wager line — say it on the card."""
    game = await _climbing(db, alive=[1, 2, 3], bail_log=[], roster=[1, 2, 3], crash_at=15.0)
    text = _card_text(cog.render_result_state(game, FakeGuild()))
    assert "Total Wipeout!" in text
    assert "everyone crashed at 60%" in text
    assert "pot is refunded" in text


async def test_everyone_blinked_card_names_the_winner_once(cog, db):
    """The winner is named exactly once — via ``winner_ref`` — and excluded
    from their own ranking-list row."""
    game = await _climbing(
        db,
        alive=[],
        bail_log=[
            {"player_id": 1, "bail_ts": time.time(), "meter_pct": 80.0},
            {"player_id": 2, "bail_ts": time.time(), "meter_pct": 50.0},
        ],
        roster=[1, 2],
    )
    game.winner_id = 1
    game.loser_id = None
    items = cog.render_result_state(game, FakeGuild())
    text = _card_text(items)
    assert "Everyone Blinked!" in text
    assert text.count("<@1>") == 1
    assert "<@2>" in text  # the other bailer still appears, in the ranking
