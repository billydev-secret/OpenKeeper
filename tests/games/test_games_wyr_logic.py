"""Tests for the extracted Would-You-Rather pure-logic module and its cog.

Covers ``bot_modules/games/wyr/logic.py`` (question parser, vote toggle,
show-my-vote) and the cog glue that needs no Discord. The Components V2
cards are covered in ``test_games_wyr_layouts.py``.
"""

from __future__ import annotations

import pytest

from bot_modules.games.wyr.logic import (
    parse_question_input,
    record_show_vote,
    shown_on_side,
    toggle_vote,
)


# ── parse_question_input ─────────────────────────────────────────────


def test_parse_question_input_splits_simple_pair():
    assert parse_question_input("fly | be invisible") == ("fly", "be invisible")


def test_parse_question_input_strips_whitespace_around_options():
    assert parse_question_input("  swim   |   fly  ") == ("swim", "fly")


def test_parse_question_input_returns_none_for_empty_string():
    assert parse_question_input("") is None


def test_parse_question_input_returns_none_for_whitespace_only():
    assert parse_question_input("   \n\t  ") is None


def test_parse_question_input_returns_none_when_no_pipe():
    assert parse_question_input("just one option") is None


def test_parse_question_input_returns_none_when_left_half_empty():
    assert parse_question_input("  | something") is None


def test_parse_question_input_returns_none_when_right_half_empty():
    assert parse_question_input("something |  ") is None


def test_parse_question_input_returns_none_when_both_halves_empty():
    assert parse_question_input(" | ") is None


def test_parse_question_input_uses_only_first_pipe():
    """Extra pipes in option B are preserved as-is — split(|, 1)."""
    assert parse_question_input("a | b | c") == ("a", "b | c")


# ── toggle_vote ──────────────────────────────────────────────────────


def test_toggle_vote_a_records_fresh_vote_returns_false():
    votes_a: list[int] = []
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, user_id=42, choice="a")
    assert changed is False
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_b_records_fresh_vote_returns_false():
    votes_a: list[int] = []
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, user_id=42, choice="b")
    assert changed is False
    assert votes_a == []
    assert votes_b == [42]


def test_toggle_vote_switching_from_b_to_a_returns_true():
    votes_a: list[int] = []
    votes_b: list[int] = [42]
    changed = toggle_vote(votes_a, votes_b, 42, "a")
    assert changed is True
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_switching_from_a_to_b_returns_true():
    votes_a: list[int] = [42]
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, 42, "b")
    assert changed is True
    assert votes_a == []
    assert votes_b == [42]


def test_toggle_vote_re_pressing_same_side_is_idempotent():
    """A user already on side A pressing A again is a no-op (no duplicate
    in the list and changed=False)."""
    votes_a: list[int] = [42]
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, 42, "a")
    assert changed is False
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_preserves_other_voters():
    """Switching one user must not disturb anyone else's vote."""
    votes_a: list[int] = [1, 2]
    votes_b: list[int] = [3, 4]
    toggle_vote(votes_a, votes_b, 3, "a")
    assert votes_a == [1, 2, 3]
    assert votes_b == [4]


def test_toggle_vote_raises_on_invalid_choice():
    with pytest.raises(ValueError):
        toggle_vote([], [], 1, "c")


# ── record_show_vote / shown_on_side ─────────────────────────────────


@pytest.mark.parametrize(
    "votes_a, votes_b, shown, uid, expected, shown_after",
    [
        pytest.param([1], [], [], 1, "shown", [1], id="voter-a-shows"),
        pytest.param([], [2], [], 2, "shown", [2], id="voter-b-shows"),
        pytest.param([1], [], [], 9, "not_voted", [], id="no-vote-nothing-to-show"),
        pytest.param([1], [], [1], 1, "already", [1], id="re-press-is-idempotent"),
    ],
)
def test_record_show_vote(votes_a, votes_b, shown, uid, expected, shown_after):
    """vote-games-61: showing is the voter's own act, and needs a vote."""
    assert record_show_vote(votes_a, votes_b, shown, uid) == expected
    assert shown == shown_after


def test_shown_on_side_follows_the_voter_across_a_switch():
    votes_a, votes_b, shown = [1, 2], [3], []
    record_show_vote(votes_a, votes_b, shown, 1)
    assert shown_on_side(votes_a, shown) == [1] and shown_on_side(votes_b, shown) == []
    toggle_vote(votes_a, votes_b, 1, "b")
    assert shown_on_side(votes_a, shown) == [] and shown_on_side(votes_b, shown) == [1]


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402

import discord  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.games.wyr.cog as wyr_cog  # noqa: E402
from bot_modules.games.utils.game_manager import (  # noqa: E402
    create_game,
    end_game,
    get_active_game_by_id,
    get_game_payload,
)
from bot_modules.games.utils.round_pacing import ADVANCE_DENIED, RoundPacing  # noqa: E402
from bot_modules.games.db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


def _channel(guild=None):
    return SimpleNamespace(
        id=100, name="games", guild=guild,
        send=AsyncMock(return_value=SimpleNamespace(id=555, edit=AsyncMock())),
    )


def _message():
    return SimpleNamespace(id=555, edit=AsyncMock())


def _ids(view) -> list[str]:
    return [i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)]


def _view_text(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _interaction(uid: int, *, message=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}", roles=[]),
        guild=None, guild_id=None,
        channel=SimpleNamespace(id=100, name="games", guild=None),
        message=message or _message(),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


async def test_run_round_empty_bank_opens_a_waiting_round(monkeypatch, sync_db_path):
    """vote-games-50: the bank had nothing, so the round waits for a posed
    question with only Pose / End / Help live — the game does not end."""
    monkeypatch.setattr(wyr_cog, "get_wyr_question", AsyncMock(return_value=None))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "wyr", payload={"rounds": {}})
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    await cog._run_round(None, gid, 1, "Host", 1, channel)

    view = bot.active_views[gid]
    assert view.waiting is True
    assert _ids(view) == ["wyr_htp", "wyr_pose"]
    assert await get_active_game_by_id(bot.games_db, gid) is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["q"] == ""
    assert channel.send.await_args.kwargs["view"] is view
    assert "Pose Question" in _view_text(view)

    # A posed question opens the round in place.
    await view.begin_round("fly", "swim", _message())
    assert view.waiting is False
    assert _ids(view) == ["wyr_htp", "wyr_a", "wyr_b", "wyr_show_vote", "wyr_pose"]
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["q"] == "fly OR swim"
    assert payload["rounds"]["1"]["opened_at"]


async def test_votes_are_persisted_on_every_press(sync_db_path):
    """vote-games-58: a restart mid-round used to rebuild an empty tally under
    a full bar, because votes reached the payload only on Next."""
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr", payload={"rounds": {"1": {"a": [], "b": [], "q": "x OR y"}}},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=_channel(),
        option_a="x", option_b="y",
    )
    await view.vote_a(_interaction(7))  # type: ignore[arg-type]
    await view.vote_b(_interaction(8))  # type: ignore[arg-type]
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"] == {"a": [7], "b": [8], "shown": [], "q": "x OR y"}


async def test_show_my_vote_is_the_voters_own_and_survives_a_restart(sync_db_path):
    """vote-games-61: any voter can name themselves — no host gate — and
    the choice rides with the votes, so a rebuilt view still shows it."""
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr", payload={"rounds": {"1": {"a": [], "b": [], "q": "x OR y"}}},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=_channel(),
        option_a="x", option_b="y",
    )
    # No vote yet: refused, nothing shown.
    stranger = _interaction(9)
    await view.show_my_vote(stranger)  # type: ignore[arg-type]
    assert "Vote" in stranger.response.send_message.await_args.args[0]
    assert view.shown == []

    await view.vote_a(_interaction(7))  # type: ignore[arg-type]
    shower = _interaction(7)
    await view.show_my_vote(shower)  # type: ignore[arg-type]
    # Acked, then redrawn through the board seam (redraw_board) rather than
    # edited straight off the interaction — with the sticky board dial on,
    # the message this press came from may already have been reposted.
    assert shower.response.defer.await_args is not None
    assert shower.message.edit.await_args is not None
    assert shower.message.edit.await_args.kwargs["view"] is view
    assert "User 7" in _view_text(view)
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["shown"] == [7]

    row = await get_active_game_by_id(bot.games_db, gid)
    assert row is not None
    rebuilt_bot = _SpyBot(sync_db_path)
    rebuilt_bot.add_view = lambda *a, **k: None  # type: ignore[attr-defined]
    rebuilt = wyr_cog.WYRCog(rebuilt_bot)  # type: ignore[arg-type]
    assert await rebuilt.recover_game(row, payload, _channel(), _message())
    assert rebuilt_bot.active_views[gid].shown == [7]


async def test_end_game_posts_the_recap_and_pays_every_voter(monkeypatch, sync_db_path):
    """vote-games-52: the host's 🏁 End Game ends through the paying path with
    the recap (most divisive question + total votes)."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {"rounds": {
        "1": {"a": [1, 2], "b": [3], "q": "A OR B"},
        "2": {"a": [], "b": [], "q": "C OR D"},
    }}
    gid = await create_game(bot.games_db, 100, 1, "wyr", payload=payload)
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=2, channel=channel,
        option_a="C", option_b="D",
    )
    bot.active_views[gid] = view
    view.votes_a, view.votes_b = [4], [1]
    message = _message()

    assert view.finish_callback is not None
    await view.finish_callback(message, "ended")

    assert view._closed is True
    message.edit.assert_awaited()  # the board becomes the round's record
    assert _ids(view) == [], "a closed round carries no controls"
    recap = _view_text(channel.send.await_args.kwargs["view"])
    assert "Game Over" in recap
    assert "Total votes: **5**" in recap
    assert "C OR D" in recap  # 1–1 beats 2–1
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3, 4]
    assert call.kwargs["bot"] is bot
    assert call.kwargs["reason"] == "ended"
    assert call.kwargs["round_count"] == 2
    assert gid not in bot.active_views


async def test_expired_at_next_ends_with_the_recap_and_pays(monkeypatch, sync_db_path):
    """vote-games-59: pressing Next on a >24h game used to archive a bare,
    guild-0 row and pay nobody."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    monkeypatch.setattr(wyr_cog, "is_game_expired", AsyncMock(return_value=True))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr", payload={"rounds": {"1": {"a": [], "b": [], "q": "x OR y"}}},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=channel,
        option_a="x", option_b="y",
    )
    bot.active_views[gid] = view
    view.votes_a = [5, 6]

    await view.advance_callback(_message())

    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [5, 6]
    assert call.kwargs["bot"] is bot
    assert call.kwargs["reason"] == "expired"
    # The last round's votes were written before the archive.
    assert call.kwargs["payload"]["rounds"]["1"]["a"] == [5, 6]
    assert channel.send.await_count == 1  # the recap, and no new round


async def test_round_cap_ends_with_the_recap(monkeypatch, sync_db_path):
    """discovery-3: the configured last round posts the recap instead of
    opening another round."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr",
        payload={"rounds": {"1": {"a": [], "b": [], "q": "a OR b"}, "2": {"a": [], "b": [], "q": "c OR d"}}, "max_rounds": 2},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=2, channel=channel,
        option_a="c", option_b="d", pacing=RoundPacing(max_rounds=2),
    )
    bot.active_views[gid] = view
    view.votes_a = [9]

    await view.advance_callback(_message())

    call = spy.await_args
    assert call is not None and call.kwargs["reason"] == "round_cap"
    assert "last round" in _view_text(channel.send.await_args.kwargs["view"])


async def test_launch_stores_pacing_and_the_scheduled_flag(monkeypatch, sync_db_path):
    """The slash/schedule option wins over the dashboard default; a launch with
    no host at the keyboard is marked scheduled (platform-23)."""
    monkeypatch.setattr(wyr_cog, "get_wyr_question", AsyncMock(return_value=("x", "y")))
    bot = _SpyBot(sync_db_path)
    await bot.games_db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES (?, ?, 1, ?)",
        (9001, "wyr", '{"round_seconds": 45, "max_rounds": 4}'),
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]

    gid = await cog.launch(channel=_channel(), host_id=5, host_name="H", guild_id=9001, options={})
    assert gid is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert (payload["round_seconds"], payload["max_rounds"], payload["scheduled"]) == (45, 4, False)
    bot.active_views[gid].pacing.cancel_timer()
    await end_game(bot.games_db, gid)

    gid = await cog.launch(
        channel=_channel(), host_id=0, host_name="Today's feature", guild_id=9001,
        options={"round_seconds": 0, "max_rounds": 0, "scheduled": True},
    )
    assert gid is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert (payload["round_seconds"], payload["max_rounds"], payload["scheduled"]) == (0, 0, True)
    assert bot.active_views[gid].pacing.timer_task is None  # host-paced: no timer


async def test_scheduled_round_lets_a_voter_press_next_after_the_window(sync_db_path):
    """platform-23: a scheduled game whose creator isn't there cannot stall."""
    bot = _SpyBot(sync_db_path)
    advance = AsyncMock()
    view = wyr_cog.WYRRoundView(
        "g", 1, "x", "y", 1, True, bot.games_db, bot, "Host", advance,
        pacing=RoundPacing(round_seconds=0, scheduled=True, opened_at=1.0),
    )
    view.votes_a = [7]
    # A non-voter is told to vote first.
    interaction = _interaction(8)
    await view.press_next(interaction)  # type: ignore[arg-type]
    assert "Vote first" in interaction.response.send_message.await_args.args[0]
    advance.assert_not_awaited()
    # A voter, once the window has elapsed, advances the round.
    interaction = _interaction(7)
    await view.press_next(interaction)  # type: ignore[arg-type]
    interaction.response.defer.assert_awaited_once()
    advance.assert_awaited_once()
    # A scheduled game keeps Next on the card, where that voter can reach it.
    assert "wyr_next" in _ids(view)

    # A hosted game keeps Next to the host / mods / Game Host role.
    hosted = wyr_cog.WYRRoundView(
        "g", 1, "x", "y", 1, True, bot.games_db, bot, "Host", AsyncMock(),
        pacing=RoundPacing(round_seconds=0, scheduled=False, opened_at=1.0),
    )
    hosted.votes_a = [7]
    interaction = _interaction(7)
    await hosted.press_next(interaction)  # type: ignore[arg-type]
    assert interaction.response.send_message.await_args.args[0] == ADVANCE_DENIED
    # ...and keeps it off the card: it lives on the host panel.
    assert "wyr_next" not in _ids(hosted)


async def test_slash_entry_refuses_a_busy_channel_through_the_shared_guard(sync_db_path):
    """platform-18: the slash entry runs the one launch guard (busy channel,
    empty bank, ...) instead of its own two checks."""
    bot = _SpyBot(sync_db_path)
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new")
    cog.launch = launch  # type: ignore[method-assign]
    await bot.games_db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (100, 9001),
    )
    await create_game(bot.games_db, 100, 5, "nhie", state="playing", payload={}, guild_id=9001)
    interaction = _interaction(1)
    interaction.guild_id = 9001
    interaction.channel_id = 100

    await wyr_cog.WYRCog.wyr.callback(cog, interaction)  # type: ignore[attr-defined]

    launch.assert_not_awaited()
    text = interaction.response.send_message.await_args.args[0]
    assert "already a game running" in text and "Never Have I Ever" in text
