"""The per-game bar plumbing both AMA and Truth or Dare place their strip with.

``tests/games/test_games_ama_bottom_bar.py`` covers this shape through AMA, which is
where it grew. What that file cannot see is the thing the extraction added:
the payload key is a *parameter* now, so two games can keep their bars in the
same row without knowing about each other's key. These pin the module's own
contract — the key is honoured, a missing game reports nowhere, and a payload
the module cannot parse is left alone rather than raising out of the worker
thread ``StickyPanel`` calls the id hooks on.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot_modules.games.bottom_bar_service import (
    BAR_TARGETS,
    bar_location,
    make_bar_panel,
    retire_bar,
    set_bar_location,
)

GAME = "game-1"
CHANNEL = 77
AMA_KEY = "bottom_message_id"
TOD_KEY = "bar_message_id"


def _bot(payload: dict | str | None = None, *, game_id: str = GAME) -> MagicMock:
    """A bot whose ``ctx.open_db()`` hands out one live games table.

    The same connection every time, so what ``set_bar_location`` wrote is what
    ``bar_location`` reads back. ``check_same_thread`` because ``StickyPanel``
    runs its id hooks through ``asyncio.to_thread``.
    """
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
            " VALUES (?, 'traditional', ?, ?)",
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
    return json.loads(row["payload"] or "{}")


# ── the key is the caller's ──────────────────────────────────────────


@pytest.mark.parametrize("key", [AMA_KEY, TOD_KEY])
def test_a_bar_round_trips_under_whichever_key_its_game_uses(key):
    bot = _bot({})
    set_bar_location(bot, GAME, 555, key)
    assert _payload_of(bot)[key] == 555
    assert bar_location(bot, GAME, key) == (CHANNEL, 555)


def test_two_keys_in_one_payload_do_not_see_each_other():
    """The reason the key is a parameter rather than a constant: a game that
    grew a second strip, or a payload carrying a leftover key from a bar that
    was renamed, must not have one read as the other."""
    bot = _bot({})
    set_bar_location(bot, GAME, 111, AMA_KEY)
    set_bar_location(bot, GAME, 222, TOD_KEY)
    assert bar_location(bot, GAME, AMA_KEY) == (CHANNEL, 111)
    assert bar_location(bot, GAME, TOD_KEY) == (CHANNEL, 222)


def test_an_unset_key_reads_as_no_bar_yet():
    bot = _bot({AMA_KEY: 999})
    assert bar_location(bot, GAME, TOD_KEY) == (CHANNEL, 0)


# ── a game that is gone reports nowhere ──────────────────────────────


def test_an_ended_game_has_no_bar_anywhere():
    """No row is what makes every late re-stick a no-op — a panel handed
    ``(0, 0)`` has nothing to delete and nowhere to post."""
    bot = _bot(None)
    assert bar_location(bot, GAME, TOD_KEY) == (0, 0)


def test_writing_to_an_ended_game_is_a_no_op_not_an_error():
    bot = _bot(None)
    set_bar_location(bot, GAME, 555, TOD_KEY)  # must not raise
    assert bar_location(bot, GAME, TOD_KEY) == (0, 0)


# ── the narrow write ─────────────────────────────────────────────────


def test_the_write_keeps_everything_else_in_the_payload():
    """One ``json_set``, not a read-modify-write: the async side of the
    payload is a Python read-modify-write on its own connection, and the
    narrower this is, the less of it there is to clobber."""
    bot = _bot({"asked": {"1:sfw_truth": "Q"}, "prefs": {"1": ["sfw_truth"]}})
    set_bar_location(bot, GAME, 555, TOD_KEY)
    payload = _payload_of(bot)
    assert payload["asked"] == {"1:sfw_truth": "Q"}
    assert payload["prefs"] == {"1": ["sfw_truth"]}
    assert payload[TOD_KEY] == 555


@pytest.mark.parametrize(
    "stored", [pytest.param("", id="empty"), pytest.param(None, id="null")],
)
def test_an_empty_payload_gets_a_fresh_object_rather_than_null(stored):
    bot = _bot(stored if stored is not None else "")
    bot.conn.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?", (stored, GAME)
    )
    set_bar_location(bot, GAME, 555, TOD_KEY)
    assert _payload_of(bot) == {TOD_KEY: 555}


def test_an_unparseable_payload_is_left_alone():
    """``json_valid`` guards the update: a payload we cannot parse keeps its
    bytes instead of raising out of the worker thread the hooks run on."""
    bot = _bot("{not json")
    set_bar_location(bot, GAME, 555, TOD_KEY)  # must not raise
    row = bot.conn.execute(
        "SELECT payload FROM games_active_games WHERE game_id = ?", (GAME,)
    ).fetchone()
    assert row["payload"] == "{not json"
    assert bar_location(bot, GAME, TOD_KEY) == (CHANNEL, 0)


def test_a_falsy_id_clears_the_stored_one():
    """How taking a bar down clears the id — the next place posts fresh
    instead of trying to edit a message that is gone."""
    bot = _bot({TOD_KEY: 555})
    set_bar_location(bot, GAME, 0, TOD_KEY)
    assert _payload_of(bot)[TOD_KEY] is None
    assert bar_location(bot, GAME, TOD_KEY) == (CHANNEL, 0)


# ── the panel the module builds ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_panel_is_named_per_game_and_follows_the_bot():
    """``restick_on_bot`` because in these games the bot is what buries the
    bar — every question card is ours. The name is per game instance, not per
    game type: it is the only place a log says which live room is stuck."""
    bot = _bot({})

    async def _build(_guild):
        raise AssertionError("not built in this test")

    panel = make_bar_panel(
        bot, GAME, name=f"traditional bar {GAME}", id_key=TOD_KEY, build=_build
    )
    assert panel.name == f"traditional bar {GAME}"
    assert panel._restick_on_bot is True
    assert panel._target_types == BAR_TARGETS


@pytest.mark.asyncio
async def test_the_panels_hooks_ignore_the_guild_they_are_handed():
    """A bar is keyed per game, never per guild: two rooms in one server each
    follow their own channel, so the guild id the shared ``StickyPanel``
    passes is not part of the lookup."""
    bot = _bot({})

    async def _build(_guild):
        raise AssertionError("not built in this test")

    panel = make_bar_panel(bot, GAME, name="bar", id_key=TOD_KEY, build=_build)
    panel._save_ids(1234, 4321, 555)
    assert _payload_of(bot)[TOD_KEY] == 555
    assert panel._load_ids(9999) == (CHANNEL, 555)


@pytest.mark.asyncio
async def test_make_bar_panel_hands_a_layout_shaped_build_straight_through():
    """This module never constructs a ``PanelContent`` itself — *build* is
    the caller's own function — so a Components V2 bar (wave 3b) that
    returns ``PanelContent(layout=...)`` reaches ``StickyPanel`` unchanged;
    nothing here has to branch on the shape."""
    import discord

    from bot_modules.core.sticky import PanelContent

    bot = _bot({})
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay("## Bar"))

    async def _build(_guild):
        return PanelContent(layout=view)

    panel = make_bar_panel(bot, GAME, name="bar", id_key=TOD_KEY, build=_build)

    content = await panel._build(SimpleNamespace(id=1))

    assert content.layout is view
    assert content.embed is None
    assert content.content is None


# ── retire_bar: the shared teardown both games' end paths call ────────


class _Bars:
    """A stand-in for ``LivePanels``: hand back a panel once, then nothing."""

    def __init__(self, panel=None) -> None:
        self._panel = panel
        self.released: list[str] = []

    def release(self, game_id: str):
        self.released.append(game_id)
        panel, self._panel = self._panel, None
        return panel


class _Panel:
    def __init__(self, *, boom: bool = False) -> None:
        self.unposted: list[object] = []
        self._boom = boom

    async def unpost(self, guild):
        self.unposted.append(guild)
        if self._boom:
            raise RuntimeError("Discord said no")


@pytest.mark.asyncio
async def test_retire_bar_deletes_the_strip_through_the_panels_own_teardown():
    panel = _Panel()
    bars = _Bars(panel)
    guild = SimpleNamespace(id=77)
    await retire_bar(bars, GAME, guild, label="traditional")
    assert bars.released == [GAME]
    assert panel.unposted == [guild]


@pytest.mark.asyncio
async def test_retire_bar_on_a_game_with_no_bar_is_a_no_op():
    """Every end path calls this, including the ones for rooms that never got
    far enough to grow a bar. A second call must be harmless too."""
    bars = _Bars(None)
    await retire_bar(bars, GAME, SimpleNamespace(id=77), label="ama")
    assert bars.released == [GAME]


@pytest.mark.asyncio
async def test_retire_bar_releases_before_it_gives_up_on_a_missing_guild():
    """The channel is gone, so there is no message to delete — but the release
    still has to happen, because that is what cancels the re-sticks already
    queued behind this call. Releasing *after* the guild check would leave a
    panel in the holder trying to repost into a dead channel."""
    panel = _Panel()
    bars = _Bars(panel)
    await retire_bar(bars, GAME, None, label="traditional")
    assert bars.released == [GAME]
    assert panel.unposted == []


@pytest.mark.asyncio
async def test_a_delete_that_will_not_go_is_logged_not_raised(caplog):
    """A bar the bot cannot delete is a row of dead buttons; the same
    exception escaping would take the whole end-of-game path down with it,
    so the room would never pay out either. The log line names the game."""
    bars = _Bars(_Panel(boom=True))
    with caplog.at_level("WARNING"):
        await retire_bar(bars, GAME, SimpleNamespace(id=77), label="traditional")
    assert GAME in caplog.text and "traditional" in caplog.text
