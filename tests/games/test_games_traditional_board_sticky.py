"""Truth or Dare's room board: sticky only while the room is filling up.

Every other game on the Live Game Boards dial keeps its board sticky for as
long as that board is live, and the shared contract for that lives in
``test_games_board_sticky_adopters.py`` (Truth or Dare has a row there too).
What is only true here is the **window**: the room's one message is pinned to
the bottom of the channel while people are picking categories, and the first
question asked lets it settle where it is. From then on the question cards are
the thread of the game, and a board that went on jumping past them would bury
them one at a time.

So this file pins the two halves of that window plus the one thing the window
forced: because a repost draws the board from ``core.sticky`` with no
interaction and nothing to await, every render input has to be cached on the
view — including which of the two cards (the joining lobby card or the board
proper) the room's message is currently showing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.games.traditional.cog as traditional_cog
from bot_modules.games.traditional.cog import (
    AskQuestionModal,
    TraditionalCog,
    TraditionalHostView,
)
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.board_sticky_service import (
    build_board_content,
    set_board_sticky_enabled,
)
from bot_modules.games.db import GamesDb

GUILD = 4242
CHAN = 778
HOST = 1
LABEL = "traditional board"


# ── fakes ───────────────────────────────────────────────────────────────────


class _Member(SimpleNamespace):
    pass


def _member(uid: int, name: str) -> _Member:
    return _Member(id=uid, display_name=name, mention=f"<@{uid}>")


class _Guild:
    """Enough guild for the accent resolver and for ``StickyPanel.refresh``,
    which resolves the board's channel through the guild rather than through
    whatever ``place()`` was handed."""

    def __init__(self, members, channel=None):
        self.id = GUILD
        self.icon = None
        self.me = None  # branding falls back to the default accent
        self._members = {m.id: m for m in members}
        self._channel = channel

    def get_member(self, uid):
        return self._members.get(uid)

    def get_channel_or_thread(self, _channel_id):
        return self._channel


class _Response:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[tuple] = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


def _message(msg_id: int) -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.id = msg_id
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    return m


def _channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHAN
    channel.name = "games"
    channel.last_message_id = None
    # ``_do_close`` falls back to this when it is handed no channel; these
    # tests always hand it one, and a None keeps the recap's payout footer
    # (which wants a real guild) out of the way.
    channel.guild = None
    channel.is_nsfw = MagicMock(return_value=False)
    channel._next_id = 1000

    async def _send(*args, **kwargs):
        channel._next_id += 1
        return _message(channel._next_id)

    channel.send = AsyncMock(side_effect=_send)
    # Memoised per id, so a test can ask what was done to the message at a
    # given id without caring how many times it was looked up.
    partials: dict[int, MagicMock] = {}
    channel.get_partial_message = MagicMock(
        side_effect=lambda msg_id: partials.setdefault(msg_id, _message(msg_id))
    )
    return channel


def _cog(sync_db_path) -> TraditionalCog:
    ctx = MagicMock()
    ctx.db_path = sync_db_path
    ctx.open_db = lambda: open_db(sync_db_path)
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path),
        active_views={},
        ctx=ctx,
        get_guild=lambda gid: None,
    )
    return TraditionalCog(bot)  # type: ignore[arg-type]


async def _room(cog, channel, guild, *, dial: bool, prefs=None):
    """Open a room exactly the way ``TraditionalCog.launch`` does."""
    if dial:
        with cog.bot.ctx.open_db() as conn:
            set_board_sticky_enabled(conn, True, GUILD)
    payload: dict = {"prefs": prefs or {}, "participants": []}
    game_id = await create_game(
        cog.db, CHAN, HOST, "traditional",
        state="joining", payload=payload, guild_id=GUILD,
    )
    view = TraditionalHostView(
        game_id, HOST, cog.db, cog.bot, boards=cog.live_boards,
    )
    view.guild = guild
    view._host_name = "Host"
    cog.bot.active_views[game_id] = view
    view._message = await cog.live_boards.open(game_id, view, channel, guild)
    return view


def _interaction(guild, channel, user):
    return SimpleNamespace(
        user=user, guild=guild, guild_id=GUILD, channel=channel,
        response=_Response(),
        # Where the "everyone has been asked" line lands once the last pair
        # closes — ephemeral to the presser since 2026-09-20, so these rooms
        # reach it whenever their one player runs out of categories.
        followup=SimpleNamespace(send=AsyncMock()),
    )


# ── the sticky half: while the room is filling up ──────────────────────────


@pytest.mark.asyncio
async def test_a_repost_before_anyone_joins_puts_the_joining_card_back(sync_db_path):
    """The room's one message is the *lobby* card until the first render of
    the board proper. Both carry the line telling a newcomer to pick a
    category; what the lobby lacks is the asked count, which on a fresh
    room could only ever read zero."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host")], channel)
    view = await _room(cog, channel, guild, dial=True)

    card = build_board_content(LABEL, view).layout
    texts = [c.content for c in card.walk_children() if hasattr(c, "content")]

    assert any("Tap Join on what you're up for" in t for t in texts)
    assert not any("asked" in t for t in texts)
    assert view._panel is cog.live_boards[view.game_id]


@pytest.mark.asyncio
async def test_once_the_board_has_been_drawn_a_repost_draws_the_board(sync_db_path):
    """A press turns the message into the board proper, and from then on a
    repost has to render that — from the cached payload, since ``build`` runs
    with no interaction and no guild handed to it."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    view = await _room(cog, channel, guild, dial=True)

    await view.refresh_board(guild, {"prefs": {"2": ["sfw_truth"]}, "participants": [2]})
    card = build_board_content(LABEL, view).layout
    texts = [c.content for c in card.walk_children() if hasattr(c, "content")]

    assert any("**1** playing" in t and "asked" in t for t in texts)
    # Resolved, never a raw `<@id>`: a mention resolves by the reading
    # client's own cache, so it would render as a bare number to anyone
    # whose client has not seen that user.
    assert any("SFW Truth" in t and "Bee" in t for t in texts)


# ── the settling half: the first question takes it off the dial ────────────


@pytest.mark.asyncio
async def test_the_first_question_unsticks_the_board_where_the_repost_left_it(
    sync_db_path,
):
    """The board comes off the dial but stays alive: it still renders, and
    every later edit has to aim at wherever the resticks left it rather than
    at the id the room opened with."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, dial=True, prefs={"2": ["sfw_truth"]})
    opened = view._message

    # Chat buried it once before the host got round to asking.
    moved = await cog.live_boards[view.game_id].place(guild, channel)
    assert moved is not None and moved.id != opened.id
    panel = cog.live_boards[view.game_id]
    panel.schedule_restick(guild.id)
    task = panel._restick_tasks[guild.id]

    modal = AskQuestionModal(
        view.game_id, cog.db, channel, HOST, cog.bot,
        target_id="2", target_name="Bee", category="sfw_truth",
    )
    modal.question._value = "Best concert you've been to?"
    await modal.on_submit(_interaction(guild, channel, guild.get_member(1)))

    assert view.game_id not in cog.live_boards
    assert view._panel is None
    # Not a retire: the room plays on and the board still has to draw.
    assert view._board_retired is False
    assert build_board_content(LABEL, view).layout is not None
    # The final reference is the live board, not the message it opened with.
    assert view._message.id == moved.id
    channel.get_partial_message(moved.id).edit.assert_awaited()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_picking_a_category_leaves_the_board_sticky(sync_db_path):
    """Only a *question* unsticks. The presses that fill the room up are the
    whole reason for the window, so a category toggle redraws the board and
    leaves it on the dial."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, dial=True)

    await view.sfw_truth(_interaction(guild, channel, guild.get_member(2)))

    assert view._panel is cog.live_boards[view.game_id]
    assert view._board_retired is False
    card = build_board_content(LABEL, view).layout
    texts = [c.content for c in card.walk_children() if hasattr(c, "content")]
    # Resolved, never a raw `<@id>`: a mention resolves by the reading
    # client's own cache, so it would render as a bare number to anyone
    # whose client has not seen that user.
    assert any("SFW Truth" in t and "Bee" in t for t in texts)


@pytest.mark.asyncio
async def test_only_the_first_question_unsticks_the_board(sync_db_path):
    """The transition is read inside the same lock that writes it, so it is
    the 0 -> 1 step that unsticks — not every question after it."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(
        cog, channel, guild, dial=True, prefs={"2": ["sfw_truth", "sfw_dare"]},
    )
    unsticks: list[str] = []
    real_unstick = cog.live_boards.unstick

    def _counted(board_view, chan=None):
        unsticks.append(board_view.game_id)
        return real_unstick(board_view, chan)

    cog.live_boards.unstick = _counted  # type: ignore[method-assign]

    for category, text in (("sfw_truth", "First?"), ("sfw_dare", "Second?")):
        modal = AskQuestionModal(
            view.game_id, cog.db, channel, HOST, cog.bot,
            target_id="2", target_name="Bee", category=category,
        )
        modal.question._value = text
        await modal.on_submit(_interaction(guild, channel, guild.get_member(1)))

    assert unsticks == [view.game_id]


# ── the room closing: an ordinary retire ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_closed_room_refuses_to_render_its_board(sync_db_path, monkeypatch):
    """Closing the room is the ordinary retire, and it happens *before* the
    disabled final frame goes out — which is why that frame is painted on
    retire's return value rather than on the view's own reference."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    # The payout half of the close has its own tests; what matters here is
    # that the board is stopped on the way through.
    monkeypatch.setattr(traditional_cog, "end_game", AsyncMock())
    view = await _room(cog, channel, guild, dial=True)
    moved = await cog.live_boards[view.game_id].place(guild, channel)
    assert moved is not None
    panel = cog.live_boards[view.game_id]
    panel.schedule_restick(guild.id)
    task = panel._restick_tasks[guild.id]

    await view._do_close(None, game_msg=view._message, channel=channel)

    assert view._board_retired is True
    assert view.game_id not in cog.live_boards
    with pytest.raises(RuntimeError):
        build_board_content(LABEL, view)
    # The disabled frame landed on the board's real id, not on the opening one.
    channel.get_partial_message(moved.id).edit.assert_awaited_once()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_the_idle_close_stops_the_board_too(sync_db_path, monkeypatch):
    """The quiet-window close reaches ``_do_close`` with no interaction and
    with the channel taken off ``self._message`` — which, once the board is
    a ``PartialMessage`` handed back by the unstick, is still the right
    channel."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host")], channel)
    monkeypatch.setattr(traditional_cog, "end_game", AsyncMock())
    view = await _room(cog, channel, guild, dial=True)

    await view._do_close(
        None, game_msg=view._message, channel=channel,
        note="Closed after 30 quiet minutes.", reason="idle",
    )

    assert view._board_retired is True
    assert len(cog.live_boards) == 0
