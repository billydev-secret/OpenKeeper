"""Truth or Dare's opt-in strip: the bar that follows the chat once play starts.

The room card is sticky only while people are picking categories
(``test_games_traditional_board_sticky.py``); the first question lets it
settle so the question cards can be read in order. That left the four category
buttons scrolling away with it, so a player who arrived mid-game had nothing to
press. The bar is the answer: one line of text and the same four buttons plus a
link back to the room, posted at the moment the card stops following and
following the chat from then on.

What is pinned here is the *window* and the *lifecycle* — the bar is not there
before the first question, there is exactly one of it however many questions
go out, it comes down with the room, and a restart adopts the one already on
screen instead of posting a second. The buttons themselves hold no state: each
delegates to the room card's own handler, and that handler's gates (the NSFW
age check, single-choice, the board redraw) are covered where it lives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.games.traditional.cog as traditional_cog
from bot_modules.games.traditional.cog import (
    AskQuestionModal,
    TraditionalBottomView,
    TraditionalCog,
    TraditionalHostView,
)
from bot_modules.core.db_utils import open_db
from bot_modules.core.sticky import StickyPanel
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.traditional.logic import BAR_ID_KEY, BAR_TEXT
from bot_modules.games.db import GamesDb

GUILD = 4242
CHAN = 778
HOST = 1


# ── fakes ───────────────────────────────────────────────────────────────────


def _member(uid: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=uid, display_name=name, mention=f"<@{uid}>")


class _Guild:
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
    m.jump_url = f"https://discord.com/channels/{GUILD}/{CHAN}/{msg_id}"
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    return m


def _channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHAN
    channel.name = "games"
    channel.last_message_id = None
    channel.guild = None
    channel.is_nsfw = MagicMock(return_value=False)
    channel._next_id = 1000
    channel.sent: list[dict] = []

    async def _send(*args, **kwargs):
        channel._next_id += 1
        channel.sent.append(kwargs | ({"content": args[0]} if args else {}))
        return _message(channel._next_id)

    channel.send = AsyncMock(side_effect=_send)
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
        add_view=MagicMock(),
    )
    return TraditionalCog(bot)  # type: ignore[arg-type]


async def _room(cog, channel, guild, *, prefs=None, payload_extra=None):
    """Open a room the way ``launch`` does, bar plumbing and all."""
    payload: dict = {"prefs": prefs or {}, "participants": []}
    payload.update(payload_extra or {})
    game_id = await create_game(
        cog.db, CHAN, HOST, "traditional",
        state="joining", payload=payload, guild_id=GUILD,
    )
    view = TraditionalHostView(
        game_id, HOST, cog.db, cog.bot,
        boards=cog.live_boards, bars=cog.live_bars,
    )
    view.guild = guild
    view._host_name = "Host"
    view._message = _message(900)
    cog.bot.active_views[game_id] = view
    return view


def _interaction(guild, channel, user):
    return SimpleNamespace(
        user=user, guild=guild, guild_id=GUILD, channel=channel,
        response=_Response(), followup=SimpleNamespace(send=AsyncMock()),
    )


async def _ask(cog, view, channel, guild, *, target="2", category="sfw_truth", text="Q?"):
    modal = AskQuestionModal(
        view.game_id, cog.db, channel, HOST, cog.bot,
        target_id=target, target_name="Bee", category=category,
    )
    modal.question._value = text
    await modal.on_submit(_interaction(guild, channel, guild.get_member(1)))


def _stored_bar_id(cog, game_id) -> int | None:
    with cog.bot.ctx.open_db() as conn:
        row = conn.execute(
            "SELECT payload FROM games_active_games WHERE game_id = ?", (game_id,)
        ).fetchone()
    return json.loads(row["payload"] or "{}").get(BAR_ID_KEY)


# ── the window: not before the first question ───────────────────────────────


@pytest.mark.asyncio
async def test_no_bar_while_the_room_is_still_filling_up(sync_db_path):
    """Billy's call: the strip appears once play starts, not at launch. While
    the room card is still following the chat the four buttons are already at
    the bottom of the channel, and a second copy of them would be noise."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    view = await _room(cog, channel, guild)

    await view.sfw_truth(_interaction(guild, channel, guild.get_member(2)))

    assert view.game_id not in cog.live_bars
    assert _stored_bar_id(cog, view.game_id) is None


@pytest.mark.asyncio
async def test_the_first_question_posts_the_bar(sync_db_path):
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth", "sfw_dare"]})

    await _ask(cog, view, channel, guild)

    assert view.game_id in cog.live_bars
    # The id is persisted, so a restart finds this bar instead of a second.
    assert _stored_bar_id(cog, view.game_id) is not None
    # The line itself went out, below the question card.
    assert any(kw.get("content") == BAR_TEXT for kw in channel.sent)
    # And the view is bound so its buttons survive a restart.
    bound = [c.args[0] for c in cog.bot.add_view.call_args_list]
    assert any(isinstance(v, TraditionalBottomView) for v in bound)


@pytest.mark.asyncio
async def test_a_second_question_does_not_post_a_second_bar(sync_db_path):
    """``open_bar`` is idempotent — one strip per room however long it runs."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth", "sfw_dare"]})

    await _ask(cog, view, channel, guild, category="sfw_truth", text="First?")
    first = _stored_bar_id(cog, view.game_id)
    await _ask(cog, view, channel, guild, category="sfw_dare", text="Second?")

    assert len(cog.live_bars) == 1
    assert _stored_bar_id(cog, view.game_id) == first
    assert len([kw for kw in channel.sent if kw.get("content") == BAR_TEXT]) == 1


@pytest.mark.asyncio
async def test_a_channel_the_bot_cannot_post_in_does_not_take_the_game_down(
    sync_db_path, monkeypatch
):
    """Best-effort: the question already went out, and the room card still
    carries the same four buttons. A failed placement drops the panel again so
    the listener stops watching for a bar that was never posted."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth"]})

    async def _boom(*_a, **_kw):
        raise discord.Forbidden(MagicMock(status=403), "no")

    monkeypatch.setattr(StickyPanel, "place", _boom)

    await _ask(cog, view, channel, guild)  # must not raise

    assert view.game_id not in cog.live_bars


# ── the room closing takes the bar with it ──────────────────────────────────


@pytest.mark.asyncio
async def test_closing_the_room_takes_the_bar_down(sync_db_path, monkeypatch):
    """A row of buttons under a finished game is worse than no row: it looks
    live. ``unpost`` deletes it under the placement lock and zeroes the stored
    id, so a re-stick already queued behind it cannot put it back."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    monkeypatch.setattr(traditional_cog, "end_game", AsyncMock())
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth"]})
    await _ask(cog, view, channel, guild)
    bar_id = _stored_bar_id(cog, view.game_id)
    assert bar_id is not None

    await view._do_close(None, game_msg=view._message, channel=channel)

    assert view.game_id not in cog.live_bars
    channel.get_partial_message(bar_id).delete.assert_awaited()


@pytest.mark.asyncio
async def test_the_idle_close_takes_the_bar_down_too(sync_db_path, monkeypatch):
    """The quiet window is now the ordinary ending for a room nobody ends by
    hand (the idle wait went to an hour on 2026-09-20), so it is the path the
    bar most often comes down on."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    monkeypatch.setattr(traditional_cog, "end_game", AsyncMock())
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth"]})
    await _ask(cog, view, channel, guild)

    await view._do_close(
        None, game_msg=view._message, channel=channel,
        note="Closed after 60 quiet minutes.", reason="idle",
    )

    assert len(cog.live_bars) == 0


@pytest.mark.asyncio
async def test_retiring_a_room_that_never_started_is_a_no_op(sync_db_path, monkeypatch):
    """A room closed before its first question has no bar, and the retire path
    runs anyway — it must not raise on the way past."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host")], channel)
    monkeypatch.setattr(traditional_cog, "end_game", AsyncMock())
    view = await _room(cog, channel, guild)

    await view._do_close(None, game_msg=view._message, channel=channel)

    assert len(cog.live_bars) == 0


# ── /games end and the 24h sweep reach the bar too ──────────────────────────
#
# Those two do not go through ``_do_close`` at all: they call
# ``force_end_active_game``, which pops the view and greys the room card but
# knows nothing about a second message this cog posted. Until 2026-09-20 that
# left a bar of four live buttons under a finished game, and its panel ran a
# SELECT per channel message for a row that no longer existed.


@pytest.mark.asyncio
async def test_a_mod_ending_the_game_takes_the_bar_down(sync_db_path):
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth"]})
    await _ask(cog, view, channel, guild)
    bar_id = _stored_bar_id(cog, view.game_id)
    assert bar_id is not None

    await cog.cleanup_ended_game(CHAN, view.game_id, channel=channel)

    assert view.game_id not in cog.live_bars
    channel.get_partial_message(bar_id).delete.assert_awaited()


@pytest.mark.asyncio
async def test_cleanup_drops_the_panel_even_with_no_view_to_ask(sync_db_path):
    """A reload, or a game whose view never came back. There is nobody to call
    ``retire_bar`` on, but the listener must still stop watching."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None
    view = await _room(cog, channel, guild, prefs={"2": ["sfw_truth"]})
    await _ask(cog, view, channel, guild)
    cog.bot.active_views.pop(view.game_id)

    await cog.cleanup_ended_game(CHAN, view.game_id, channel=channel)

    assert view.game_id not in cog.live_bars


@pytest.mark.asyncio
async def test_cleanup_on_a_room_with_no_bar_is_a_no_op(sync_db_path):
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host")], channel)
    view = await _room(cog, channel, guild)

    await cog.cleanup_ended_game(CHAN, view.game_id, channel=channel)  # must not raise

    assert len(cog.live_bars) == 0


@pytest.mark.asyncio
async def test_games_end_routes_truth_or_dare_through_the_cleanup_hook():
    """The wiring assertion: ``/games end`` looks the hook up in one table,
    and Truth or Dare is in it. Until 2026-09-20 the lookup was a bare
    ``if game_type == "ama"`` and no other cog could ever be reached."""
    from bot_modules.games.config.cog import PRE_TEARDOWN_COGS

    assert PRE_TEARDOWN_COGS["traditional"] == "TraditionalCog"
    assert PRE_TEARDOWN_COGS["ama"] == "AMACog"  # the original entry still wired


@pytest.mark.asyncio
async def test_a_button_on_an_orphaned_bar_refuses_instead_of_pretending(sync_db_path):
    """Belt and braces for a bar the cleanup could not delete — a channel the
    bot lost Manage Messages in. A press must not write a preference into a
    dead game and answer "added"."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    view = await _room(cog, channel, guild)
    view._closed = True
    bar = TraditionalBottomView(view, "https://example.invalid/msg")
    interaction = _interaction(guild, channel, guild.get_member(2))

    await bar.sfw_truth.callback(interaction)

    (content, kwargs), = interaction.response.messages
    assert "ended" in content
    assert kwargs.get("ephemeral") is True
    assert _stored_bar_id(cog, view.game_id) is None
    with cog.bot.ctx.open_db() as conn:
        row = conn.execute(
            "SELECT payload FROM games_active_games WHERE game_id = ?", (view.game_id,)
        ).fetchone()
    assert json.loads(row["payload"])["prefs"] == {}


# ── a restart adopts the bar already on screen ──────────────────────────────


@pytest.mark.asyncio
async def test_recovery_adopts_the_bar_the_restart_left_behind(sync_db_path):
    """The id is in the payload, so recovery re-binds the buttons and re-arms
    the follow without fetching or re-posting. A second strip under a running
    game would be the visible failure."""
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host"), _member(2, "Bee")], channel)
    channel.guild = guild
    game_id = await create_game(
        cog.db, CHAN, HOST, "traditional", state="active", guild_id=GUILD,
        payload={
            "prefs": {"2": ["sfw_truth"]}, "participants": [2],
            "asked": {"2:sfw_truth": "Q"}, BAR_ID_KEY: 5150,
        },
    )
    row = {"game_id": game_id, "host_id": HOST}
    payload = {
        "prefs": {"2": ["sfw_truth"]}, "participants": [2],
        "asked": {"2:sfw_truth": "Q"}, BAR_ID_KEY: 5150,
    }

    await cog.recover_game(row, payload, channel, _message(900))

    assert game_id in cog.live_bars
    bound = {
        c.kwargs.get("message_id"): c.args[0]
        for c in cog.bot.add_view.call_args_list
    }
    assert isinstance(bound.get(5150), TraditionalBottomView)
    # Adopted, not re-posted.
    assert channel.send.await_count == 0


@pytest.mark.asyncio
async def test_recovery_of_a_room_with_no_bar_yet_leaves_it_barless(sync_db_path):
    cog = _cog(sync_db_path)
    channel = _channel()
    guild = _Guild([_member(1, "Host")], channel)
    channel.guild = guild
    game_id = await create_game(
        cog.db, CHAN, HOST, "traditional", state="joining", guild_id=GUILD,
        payload={"prefs": {}, "participants": []},
    )

    await cog.recover_game(
        {"game_id": game_id, "host_id": HOST},
        {"prefs": {}, "participants": []},
        channel,
        _message(900),
    )

    assert game_id not in cog.live_bars


# ── the buttons themselves hold nothing ─────────────────────────────────────


def test_the_bar_carries_the_four_categories_and_a_way_back():
    """Five items, the link last: the four toggles are the point and the jump
    is the escape hatch for a room card that has scrolled out of reach."""
    view = TraditionalBottomView(MagicMock(), "https://example.invalid/msg")
    labels = [c.label for c in view.children]
    assert labels == [
        "SFW Truth", "SFW Dare", "NSFW Truth", "NSFW Dare", "Jump to Lobby",
    ]
    link = view.children[-1]
    assert link.style is discord.ButtonStyle.link
    assert link.url == "https://example.invalid/msg"
    # Persistent: a restart re-binds these by custom_id, and a timeout would
    # leave a live-looking strip nobody can press.
    assert view.timeout is None
    assert all(c.custom_id for c in view.children[:4])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attr", "category"),
    [
        pytest.param("sfw_truth", "sfw_truth", id="sfw-truth"),
        pytest.param("sfw_dare", "sfw_dare", id="sfw-dare"),
        pytest.param("nsfw_truth", "nsfw_truth", id="nsfw-truth"),
        pytest.param("nsfw_dare", "nsfw_dare", id="nsfw-dare"),
    ],
)
async def test_every_bar_button_is_the_room_cards_own_press(attr, category):
    """The strip holds no state and repeats no rule: one call through to the
    card's handler, so the age gate and the single-choice rule are the same
    ones, enforced in one place."""
    host_view = MagicMock()
    host_view._toggle_pref = AsyncMock()
    view = TraditionalBottomView(host_view, "https://example.invalid/msg")
    interaction = SimpleNamespace(
        user=_member(2, "Bee"), channel=SimpleNamespace(name="games"),
    )

    await getattr(view, attr).callback(interaction)

    host_view._toggle_pref.assert_awaited_once_with(interaction, category)
