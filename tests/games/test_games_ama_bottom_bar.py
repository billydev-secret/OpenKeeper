"""AMA's bottom bar, now placed by the shared ``StickyPanel``.

The bar used to run its own delete-and-re-post loop and keep its only handle
on the live view (``_bottom_msg``). What replaced it is a panel per AMA whose
id hooks read and write the bar's id where it already lived — the game
payload's ``bottom_message_id`` — with the channel taken off the game row,
since a bar cannot move to another channel.

Two things are pinned here because nothing else would notice them going:
the payload write is *narrow* (one ``json_set``, not a read-modify-write that
would clobber a concurrent async payload write wholesale), and the panel opts
into ``restick_on_bot`` — in an AMA it is the bot's own question cards and
rotation announcements that bury the bar, and the hand-rolled listener had no
author filter at all.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import discord
import pytest

from bot_modules.games.ama import bottom_bar_service as svc
from bot_modules.games.board_sticky_service import (
    board_sticky_enabled,
)
from bot_modules.games.ama.logic import AMA_FORMAT_HOT_SEAT, AMA_FORMAT_PANEL

GAME = "ama-1"
CHANNEL = 77
GUILD = 4242


def _bot(payload: dict | str | None = None, *, game_id: str = GAME) -> MagicMock:
    """A bot whose ``ctx.open_db()`` hands out one live games table.

    The same connection every time, not a fresh one: the point of most of
    these is that what ``save_ids`` wrote is what ``load_ids`` reads back.
    ``sqlite3.Connection.__exit__`` commits without closing, so handing the
    same object to every ``with`` is exactly how the real pool behaves here.
    """
    # check_same_thread because ``StickyPanel`` runs its id hooks through
    # asyncio.to_thread; the real open_db hands out a per-call connection and
    # never has one cross threads.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE games_active_games (game_id TEXT PRIMARY KEY, "
        "game_type TEXT, channel_id INTEGER, message_id INTEGER, payload TEXT)"
    )
    if payload is not None:
        stored = payload if isinstance(payload, str) else json.dumps(payload)
        conn.execute(
            "INSERT INTO games_active_games (game_id, game_type, channel_id, payload)"
            " VALUES (?, 'ama', ?, ?)",
            (game_id, CHANNEL, stored),
        )
    bot = MagicMock()
    bot.ctx = SimpleNamespace(open_db=lambda: conn)
    bot.conn = conn
    return bot


def _payload_of(bot: MagicMock, game_id: str = GAME) -> dict:
    row = bot.conn.execute(
        "SELECT payload FROM games_active_games WHERE game_id = ?", (game_id,)
    ).fetchone()
    return json.loads(row["payload"])


def _view(
    game_format: str = AMA_FORMAT_HOT_SEAT,
    *,
    hot_seat_name: str | None = None,
    queue: list[int] | None = None,
    panel: list[int] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        game_format=game_format,
        _hot_seat_name=hot_seat_name,
        queue=list(queue or []),
        panel=list(panel or []),
    )


# ── where the bar is ────────────────────────────────────────────────────


def test_the_bar_id_comes_from_the_payload_and_the_channel_from_the_row():
    bot = _bot({"bottom_message_id": 999, "questions": []})
    assert svc.bar_location(bot, GAME) == (CHANNEL, 999)


def test_a_game_with_no_bar_yet_reports_nowhere():
    """What a launch sees on its first placement: nothing to replace, so
    ``place()`` posts and deletes nothing."""
    bot = _bot({"questions": []})
    assert svc.bar_location(bot, GAME) == (CHANNEL, 0)


def test_a_game_that_is_over_reports_nowhere():
    bot = _bot(None)
    assert svc.bar_location(bot, GAME) == (0, 0)


def test_a_payload_that_will_not_parse_does_not_break_the_read():
    """``load_ids`` runs in a worker thread on every placement; raising there
    surfaces as a restick failure traceback, not as the bad row it is."""
    bot = _bot("{not json")
    assert svc.bar_location(bot, GAME) == (CHANNEL, 0)


# ── stamping the bar's new id ───────────────────────────────────────────


def test_stamping_the_bar_id_leaves_the_rest_of_the_payload_alone():
    """The reason this is one ``json_set`` and not a read-modify-write.

    Questions, the hot seat and the rotation count are all written from the
    async side on their own connection. A Python-side read-modify-write here
    would carry a whole stale payload back over the top of whatever landed
    while we were thinking about it.
    """
    bot = _bot({"questions": [{"text": "q"}], "hot_seat_id": 5})
    svc.set_bar_location(bot, GAME, CHANNEL, 321)

    assert _payload_of(bot) == {
        "questions": [{"text": "q"}], "hot_seat_id": 5, "bottom_message_id": 321,
    }


def test_the_new_id_is_what_the_next_placement_reads():
    bot = _bot({"questions": []})
    svc.set_bar_location(bot, GAME, CHANNEL, 321)
    assert svc.bar_location(bot, GAME) == (CHANNEL, 321)


def test_taking_the_bar_down_clears_the_stored_id():
    """``unpost`` saves ``(0, 0)``. Recovery must then read "no bar" rather
    than rebind a persistent view to a message that has been deleted."""
    bot = _bot({"bottom_message_id": 999})
    svc.set_bar_location(bot, GAME, 0, 0)

    assert svc.bar_location(bot, GAME) == (CHANNEL, 0)
    assert not _payload_of(bot).get("bottom_message_id")


def test_a_payload_that_will_not_parse_is_left_as_it_is():
    """Better a bar that stops being followed than a payload overwritten:
    the questions in there are the game."""
    bot = _bot("{not json")
    svc.set_bar_location(bot, GAME, CHANNEL, 321)

    row = bot.conn.execute(
        "SELECT payload FROM games_active_games WHERE game_id = ?", (GAME,)
    ).fetchone()
    assert row["payload"] == "{not json"


def test_a_game_that_is_over_is_not_resurrected():
    bot = _bot(None)
    svc.set_bar_location(bot, GAME, CHANNEL, 321)
    assert bot.conn.execute("SELECT count(*) FROM games_active_games").fetchone()[0] == 0


# ── what the bar looks like ─────────────────────────────────────────────


def test_the_bar_is_a_line_of_text_not_an_embed():
    """An embed would box and indent a strip meant to read as part of the
    channel — which is why ``PanelContent`` grew a text-only shape."""
    bottom_view = MagicMock(spec=discord.ui.View)
    content = svc.build_bar_content(_view(hot_seat_name="Robin"), bottom_view)

    assert content.embed is None
    assert content.content == "🎙️ AMA: @Robin"
    assert content.view is bottom_view


def test_the_panel_format_bar_counts_who_is_answering():
    content = svc.build_bar_content(
        _view(AMA_FORMAT_PANEL, panel=[1, 2]), MagicMock(spec=discord.ui.View)
    )
    assert content.content == "🎙️ AMA Panel — 2 answering questions"


def test_a_bar_whose_line_is_unchanged_costs_no_edit():
    """The bar is refreshed on every volunteer, every queue change and every
    rotation, and most of those leave the line identical. The signature *is*
    the line, so the panel's unchanged-content gate catches them."""
    view = _view(hot_seat_name="Robin")
    first = svc.build_bar_content(view, MagicMock(spec=discord.ui.View))
    again = svc.build_bar_content(view, MagicMock(spec=discord.ui.View))
    assert first.signature == again.signature == first.content

    view.queue.append(7)
    assert svc.build_bar_content(view, MagicMock(spec=discord.ui.View)).signature != (
        first.signature
    )


# ── the panel the cog holds ─────────────────────────────────────────────


def _panel(bot: MagicMock, view: SimpleNamespace | None = None):
    return svc.make_bar_panel(
        bot, GAME, view or _view(), MagicMock(spec=discord.ui.View)
    )


def test_the_panels_own_hooks_round_trip_through_the_payload():
    """End to end through the hooks ``StickyPanel`` actually calls, off-thread."""
    bot = _bot({"questions": []})
    panel = _panel(bot)

    assert asyncio.run(panel.ids(GUILD)) == (CHANNEL, 0)
    asyncio.run(asyncio.to_thread(panel._save_ids, GUILD, CHANNEL, 555))
    assert asyncio.run(panel.ids(GUILD)) == (CHANNEL, 555)


def test_a_panel_is_named_after_its_own_game():
    """Two AMAs can be live at once, and the failure ceiling's log line is
    the only place an admin sees which one the bot cannot post in."""
    assert _panel(_bot({})).name == f"ama bar {GAME}"


@pytest.mark.asyncio
async def test_the_bar_still_follows_the_chat_with_live_game_boards_off():
    """The deliberate anomaly: AMA's bar is **not** on the Live Game Boards
    dial, and this fails the moment somebody puts it there.

    Every other sticky board in the games system is behind
    ``games_board_sticky_enabled``, off by default, because each of those
    boards used to sit still and the dial preserves that. The bar never sat
    still — it has followed the bottom of an AMA's channel since long before
    the dial existed, and it is the only place the Ask a Question button
    lives, so a dial-off AMA whose bar stopped moving would be an AMA nobody
    can ask a question in. There is no previous behaviour for a dial to
    preserve here, so the migration onto ``StickyPanel`` is unconditional.
    See ``docs/specs/games_system_spec.md`` § Live game boards.
    """
    bot = _bot({"bottom_message_id": 999})
    bot.conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, "
        "key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    # The dial really is off for this guild — no row at all, which is the
    # default state every guild is in until an admin ticks the box.
    assert await board_sticky_enabled(bot, guild) is False

    # The bar is placed anyway…
    panel = _panel(bot)
    assert panel is not None

    # …and still moves when the conversation buries it.
    message = MagicMock(spec=discord.Message)
    message.id = 1000
    message.author = MagicMock(bot=False)
    message.guild = guild
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = CHANNEL

    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(message)

    sched.assert_called_once_with(GUILD)


@pytest.mark.asyncio
async def test_the_bar_follows_the_bots_own_messages():
    """In an AMA the bot is what buries the bar: every question card and
    every hot-seat announcement is ours. The listener this replaced had no
    author filter, so defaulting to member-only would quietly narrow it."""
    panel = _panel(_bot({"bottom_message_id": 999}))
    message = MagicMock(spec=discord.Message)
    message.id = 1000
    message.author = MagicMock(bot=True)
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = GUILD
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = CHANNEL

    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(message)

    sched.assert_called_once_with(GUILD)
