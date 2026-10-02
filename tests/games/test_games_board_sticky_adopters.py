"""One contract, every game on the Live Game Boards dial.

The dial (``games_board_sticky_enabled``, Games Global Config → Live Game
Boards) is per-guild, off by default, and shared by every adopting game —
there is deliberately no per-game override. The machinery underneath is
``core.sticky.StickyPanel``, covered generically in
``tests/core/test_core_sticky.py``, and the two founding adopters have their own
files (``test_games_rushmore_board_sticky.py``,
``test_games_price_board_sticky.py``) for the site-specific shapes.

What this file pins is the part that has to hold identically at **every**
board site, and that a new adopter gets wrong silently:

* dial off ⇒ no ``StickyPanel`` is ever constructed, and the board goes out
  exactly the way the game posted it before it wired up here;
* dial on ⇒ the board is placed, the panel is registered under the game id,
  and the board's id lands in whichever location store that adopter uses;
* a board that *moves* removes the message it took over (after the
  replacement is safely up); a board that is **freshly posted** per
  round/take/entry leaves the closed one alone — deleting it would eat the
  game's own history the moment a guild ticked the dial on;
* retire refuses to render again, cancels pending resticks, drops the cached
  ids, and hands back wherever the board actually ended up;
* the dial is read **once**, when the board is first posted, so an admin
  flipping it mid-game never disturbs a board already running.

Rather than a per-cog copy of all that, each board site is one row in
``SITES``. A new adopter adds a row.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.games.clapback.cog import ClapbackCog
from bot_modules.games.fantasies.cog import FantasiesCog
from bot_modules.games.hottakes.cog import HotTakesCog
from bot_modules.games.mlt.cog import MLTCog
from bot_modules.games.nhie.cog import NHIECog
from bot_modules.games.price.cog import PriceCog
from bot_modules.games.rushmore.cog import RushmoreCog
from bot_modules.games.story.cog import StoryCog
from bot_modules.games.traditional.cog import TraditionalCog
from bot_modules.games.ttl.cog import TTLCog
from bot_modules.games.wyr.cog import WYRCog
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games import board_sticky_service as svc
from bot_modules.games.board_sticky_service import (
    board_location,
    build_board_content,
    set_board_sticky_enabled,
)
from bot_modules.games.db import GamesDb

GUILD = 4242
CHAN = 778
HOST = 1
PRIOR = 91  # the message already on screen before the board opens


# ── the adopters ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Site:
    """One board-posting site on the dial.

    *anchored* boards take over a message the caller hands them and so
    *move*; the rest post a fresh board per round/take/entry and leave the
    one that just closed on screen. *tracked* boards keep their ids in the
    live-game row; Fantasies' cannot, because that row names the host's
    control panel (see ``TransientBoardLocation``).
    """

    cog_cls: Any
    game_type: str
    suffix: str
    anchored: bool = False
    tracked: bool = True


SITES = {
    # Mt. Rushmore Draft: the lobby card becomes the draft board, one per game.
    "rushmore-draft": Site(RushmoreCog, "rushmore", "", anchored=True),
    # Name Your Price: one board carried from round to round.
    "price-round": Site(PriceCog, "price", " round 2", anchored=True),
    # The timed round games: a fresh board per round…
    "nhie-round": Site(NHIECog, "nhie", " round 2"),
    "wyr-round": Site(WYRCog, "wyr", " round 2"),
    "mlt-round": Site(MLTCog, "mlt", " round 2"),
    # …per take…
    "hottakes-take": Site(HotTakesCog, "hottakes", " take 2"),
    # …per subject…
    "ttl-round": Site(TTLCog, "ttl", " round 2"),
    # …and Clapback's two, one after the other inside a single round.
    "clapback-submit": Site(ClapbackCog, "clapback", " round 2"),
    "clapback-vote": Site(ClapbackCog, "clapback", " round 2 matchup 1"),
    # Truth or Dare's room board, posted once at launch — unanchored because
    # the lobby card *is* that board's first frame rather than a message it
    # takes over. Its own file covers the part that is only true here: it
    # comes off the dial when the first question is asked, without retiring.
    "traditional-room": Site(TraditionalCog, "traditional", ""),
    # …and Fantasies' two plus Story's turn card, all untracked: their game
    # row names something else (a host control panel, a lobby) and their
    # boards are gone by the time anything would read it back.
    "fantasies-submissions": Site(
        FantasiesCog, "fantasies", " round 2 submissions", tracked=False
    ),
    "fantasies-entry": Site(FantasiesCog, "fantasies", " entry 1", tracked=False),
    "story-turn": Site(StoryCog, "story", " turn 2", tracked=False),
}

sites = pytest.mark.parametrize("site", SITES.values(), ids=list(SITES))


class _FakeBoardView:
    """The whole ``LiveBoardView`` protocol and nothing else.

    Each adopter's real view is exercised in its own logic tests; what the
    sticky wiring touches is these five attributes, so the contract is
    stated against them rather than against seven constructors.
    """

    def __init__(self, game_id: str, guild=None):
        self.game_id = game_id
        self.guild = guild
        self._panel = None
        self._board_retired = False
        self.render_count = 0

    def _build_embed(self) -> discord.Embed:
        self.render_count += 1
        return discord.Embed(title=f"board {self.game_id} #{self.render_count}")


# ── fixtures ────────────────────────────────────────────────────────────────


def _cog(site: Site, sync_db_path):
    ctx = MagicMock()
    ctx.db_path = sync_db_path
    ctx.open_db = lambda: open_db(sync_db_path)
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path),
        active_views={},
        ctx=ctx,
        # StickyPanel.refresh/_delayed_restick resolve the guild through the
        # bot, not through whatever place() was handed.
        get_guild=lambda gid: None,
    )
    return site.cog_cls(bot)


def _message(msg_id: int) -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.id = msg_id
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    return m


def _channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHAN
    channel.last_message_id = None
    channel._next_id = 1000

    async def _send(**kwargs):
        channel._next_id += 1
        return _message(channel._next_id)

    channel.send = AsyncMock(side_effect=_send)
    # Both the replace-the-old-panel delete and retire's return value go
    # through this. Memoised per id, so a test can ask what was done to the
    # message at a given id without caring how many times it was looked up.
    partials: dict[int, MagicMock] = {}
    channel.get_partial_message = MagicMock(
        side_effect=lambda msg_id: partials.setdefault(msg_id, _message(msg_id))
    )
    return channel


def _guild(channel=None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    if channel is not None:
        guild.get_channel_or_thread = MagicMock(return_value=channel)
    return guild


async def _seed(cog, site: Site) -> str:
    """A live game row naming the message already on screen — the lobby card
    for an anchored site, the previous round's closed board (or, for
    Fantasies, the host's control panel) for the rest."""
    return await create_game(
        cog.db, CHAN, HOST, site.game_type,
        message_id=PRIOR, state="playing", guild_id=GUILD,
    )


def _dial_on(cog) -> None:
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)


async def _open(cog, site: Site, view, channel, guild, anchor=None):
    """Post the board the way that site's call site does."""
    return await cog.live_boards.open(
        view.game_id, view, channel, guild,
        anchor if site.anchored else None,
        suffix=site.suffix,
    )


def _stored(cog, site: Site, game_id: str) -> tuple[int, int]:
    """Where that adopter's location store says the board is."""
    if site.tracked:
        return board_location(cog.bot, game_id)
    return cog.live_boards._location.load(game_id)


# ── dial off: nothing is constructed, nothing changes ──────────────────────


@sites
@pytest.mark.asyncio
async def test_dial_off_posts_the_board_the_old_way(site: Site, sync_db_path):
    cog = _cog(site, sync_db_path)
    game_id = await _seed(cog, site)
    view = _FakeBoardView(game_id, guild=None)
    channel, anchor = _channel(), _message(PRIOR)

    result = await _open(cog, site, view, channel, _guild(channel), anchor)

    if site.anchored:
        # The board *is* that message — edited in place, never re-posted.
        anchor.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        assert result is anchor
    else:
        channel.send.assert_awaited_once()
        anchor.edit.assert_not_awaited()
        assert result.id == 1001
    anchor.delete.assert_not_awaited()
    # No panel exists, so every lifecycle forward this cog makes is a no-op.
    assert view._panel is None
    assert len(cog.live_boards) == 0
    assert game_id not in cog.live_boards


@sites
@pytest.mark.asyncio
async def test_dial_off_leaves_an_untracked_game_row_alone(site: Site, sync_db_path):
    """A freshly posted board re-points the game row it *is* — that is the
    write the adopter used to make itself. Fantasies' row names the host's
    control panel instead, and must survive its boards untouched, dial or no
    dial: ``recover_game`` re-registers the control view against whatever id
    is in there after a restart."""
    cog = _cog(site, sync_db_path)
    game_id = await _seed(cog, site)
    view = _FakeBoardView(game_id, guild=None)
    channel = _channel()

    result = await _open(cog, site, view, channel, _guild(channel), _message(PRIOR))

    channel_id, message_id = board_location(cog.bot, game_id)
    assert channel_id == CHAN
    if site.tracked:
        assert message_id == result.id
    else:
        assert message_id == PRIOR


@sites
@pytest.mark.asyncio
async def test_dial_on_but_no_guild_falls_through(site: Site, sync_db_path):
    """``guild`` comes from ``getattr(channel, "guild", None)``, so a games
    channel always has one — but ``StickyPanel.place`` dereferences
    ``guild.id`` immediately, so a ``None`` must never reach it even with the
    guild-0 fallback row switched on."""
    cog = _cog(site, sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, 0)
    game_id = await _seed(cog, site)
    view = _FakeBoardView(game_id)
    channel = _channel()

    await _open(cog, site, view, channel, None, _message(PRIOR))

    assert view._panel is None
    assert len(cog.live_boards) == 0


# ── dial on: placed, registered, and the ids land in the right store ───────


@sites
@pytest.mark.asyncio
async def test_dial_on_sticks_the_board_and_records_where_it_went(
    site: Site, sync_db_path
):
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    view = _FakeBoardView(game_id, guild=guild)

    result = await _open(cog, site, view, channel, guild, _message(PRIOR))

    channel.send.assert_awaited_once()
    assert cog.live_boards[game_id] is view._panel
    assert _stored(cog, site, game_id) == (CHAN, result.id)


@sites
@pytest.mark.asyncio
async def test_a_fresh_board_never_deletes_the_one_that_just_closed(
    site: Site, sync_db_path
):
    """``place()`` removes whatever its ``load_ids`` names, so a board that
    is posted fresh per round has to start with *no* prior board of its own
    — otherwise ticking the dial on would silently delete the round that just
    ended, which is that round's only record. A board that genuinely moves
    (Rushmore's lobby card, Price's carried board) does replace its
    predecessor — post-before-delete, so a failed placement loses nothing."""
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    view = _FakeBoardView(game_id, guild=guild)

    await _open(cog, site, view, channel, guild, _message(PRIOR))

    replaced = [
        call.args[0] for call in channel.get_partial_message.call_args_list
    ]
    if site.anchored:
        assert PRIOR in replaced
        channel.get_partial_message(PRIOR).delete.assert_awaited_once()
    else:
        assert PRIOR not in replaced
        assert channel.get_partial_message(PRIOR).delete.await_count == 0


@sites
@pytest.mark.asyncio
async def test_a_repost_follows_the_board_to_its_new_id(site: Site, sync_db_path):
    """The debounce is core.sticky's; what has to hold here is that the
    adopter's own location store tracks the repost, so everything reading it
    (the next edit, retire's return value) points at the live board."""
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = _FakeBoardView(game_id, guild=guild)

    first = await _open(cog, site, view, channel, guild, _message(PRIOR))
    second = await cog.live_boards[game_id].place(guild, channel)

    assert second is not None and second.id != first.id
    assert _stored(cog, site, game_id) == (CHAN, second.id)


@sites
@pytest.mark.asyncio
async def test_placement_failure_falls_back_and_keeps_the_old_message(
    site: Site, sync_db_path
):
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "no perms"))
    guild = _guild(channel)
    view = _FakeBoardView(game_id, guild=guild)
    anchor = _message(PRIOR)

    if site.anchored:
        result = await _open(cog, site, view, channel, guild, anchor)
        # place() never got past posting the replacement, so it never
        # reached the delete — and the game still has a board.
        assert result is anchor
        anchor.edit.assert_awaited_once()
        anchor.delete.assert_not_awaited()
    else:
        # Nothing to fall back onto: the fresh send is the only board there
        # was, and its failure is the caller's to handle (the round loops
        # wrap theirs in retry_transient / their own try-except).
        with pytest.raises(discord.HTTPException):
            await _open(cog, site, view, channel, guild, anchor)
    assert view._panel is None
    assert game_id not in cog.live_boards


# ── the dial is read once, when the board is first posted ──────────────────


@sites
@pytest.mark.asyncio
async def test_the_dial_is_read_exactly_once_per_board(
    site: Site, sync_db_path, monkeypatch
):
    """An admin flipping the dial mid-game must never disturb a board already
    running, and a board must never re-read the dial on the hot path — every
    message in the channel goes through the restick forwarder."""
    reads: list[int] = []
    real = svc.get_board_sticky_enabled

    def _counted(conn, guild_id=0):
        reads.append(guild_id)
        return real(conn, guild_id)

    monkeypatch.setattr(svc, "get_board_sticky_enabled", _counted)
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = _FakeBoardView(game_id, guild=guild)

    await _open(cog, site, view, channel, guild, _message(PRIOR))
    panel = cog.live_boards[game_id]
    await panel.place(guild, channel)
    await panel.refresh(guild.id)
    await cog.live_boards.on_message(MagicMock(spec=discord.Message))

    assert reads == [GUILD]


# ── retire: refuses, cancels, forgets, and says where the board is ─────────


@sites
@pytest.mark.asyncio
async def test_retire_refuses_to_render_again(site: Site, sync_db_path):
    """``build`` **raises** once retired rather than returning ``None``:
    ``StickyPanel._place_locked`` does not check its build result."""
    cog = _cog(site, sync_db_path)
    game_id = await _seed(cog, site)
    view = _FakeBoardView(game_id)

    assert cog.live_boards.retire(view) is None  # dial off: nothing to stop
    assert view._board_retired is True
    with pytest.raises(RuntimeError):
        build_board_content("board", view)


@sites
@pytest.mark.asyncio
async def test_retire_cancels_pending_resticks_and_drops_cached_ids(
    site: Site, sync_db_path
):
    """Cancelling the armed restick is not enough on its own: an
    ``on_message`` already in flight can still read the panel's cached ids
    and arm another one, which then reaches the refusal above and is logged
    as an ERROR traceback for an ordinary round ending."""
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    view = _FakeBoardView(game_id, guild=guild)
    await _open(cog, site, view, channel, guild, _message(PRIOR))
    panel = cog.live_boards[game_id]
    await panel._cached_ids(guild.id)
    assert guild.id in panel._ref
    panel.schedule_restick(guild.id)
    task = panel._restick_tasks[guild.id]

    cog.live_boards.retire(view, channel)

    assert view._board_retired is True
    assert game_id not in cog.live_boards
    assert guild.id not in panel._ref
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@sites
@pytest.mark.asyncio
async def test_retire_hands_back_wherever_the_board_ended_up(
    site: Site, sync_db_path
):
    """Every close path paints its final frame on this return value, not on
    the message it opened the board with — a restick may have moved the board
    since, leaving that local reference pointing at something already
    deleted."""
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = _FakeBoardView(game_id, guild=guild)
    await _open(cog, site, view, channel, guild, _message(PRIOR))
    moved = await cog.live_boards[game_id].place(guild, channel)
    assert moved is not None

    target = cog.live_boards.retire(view, channel)

    assert target is not None and target.id == moved.id


@sites
@pytest.mark.asyncio
async def test_an_untracked_board_leaves_nothing_behind_in_its_store(
    site: Site, sync_db_path
):
    """The in-memory store is the only thing holding an untracked board's
    ids, and a cog outlives every game it runs — so retire has to clear the
    game's entry rather than grow the dict one round at a time."""
    if site.tracked:
        pytest.skip("tracked boards live in the game row, which /games end clears")
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    view = _FakeBoardView(game_id, guild=guild)
    await _open(cog, site, view, channel, guild, None)

    cog.live_boards.retire(view, channel)

    assert cog.live_boards._location.load(game_id) == (0, 0)


@sites
@pytest.mark.asyncio
async def test_cog_unload_leaves_no_board_armed(site: Site, sync_db_path):
    cog = _cog(site, sync_db_path)
    _dial_on(cog)
    game_id = await _seed(cog, site)
    channel = _channel()
    guild = _guild(channel)
    view = _FakeBoardView(game_id, guild=guild)
    await _open(cog, site, view, channel, guild, _message(PRIOR))
    panel = cog.live_boards[game_id]
    panel.schedule_restick(guild.id)
    task = panel._restick_tasks[guild.id]

    await cog.cog_unload()

    assert len(cog.live_boards) == 0
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
