"""Tests for ``bot_modules/games/mlt/layouts.py`` — the Components V2 cards
that replaced ``games_mlt/embeds.py`` — and the host panel that took Start,
Next and End Game off the cards.

The frame the three round games share is pinned in
``test_round_layouts.py``; what is here is Most Likely To's own: the lobby,
the dropdown round, the crowned results, the standings. Budget is checked
at the worst case the game can produce: 25 players (``MAX_PLAYERS``, the
dropdown's own ceiling) of markdown-heavy names.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.mlt.cog as mlt_cog
from bot_modules.core.layout import card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.utils.round_pacing import CONTROL_DENIED, RoundPacing
from bot_modules.games.mlt import layouts
from bot_modules.games.mlt.layouts import (
    HELP_ID,
    HOST_END_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    JOIN_ID,
    LEAVE_ID,
    LOBBY_HELP_ID,
    NEXT_ID,
    POSE_ID,
    VOTE_SELECT_ID,
    build_final_standings_layout,
    build_host_panel_layout,
    build_lobby_layout,
    build_results_layout,
    build_round_layout,
)
from bot_modules.games.mlt.logic import MAX_PLAYERS
from bot_modules.shared.name_resolver import mention

NASTY_NAME = "*" * 32


def _named(uid: int) -> str:
    return f"Member{uid}"


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str]:
    return [
        i.custom_id for i in card(items).walk_children() if getattr(i, "custom_id", None)
    ]


# ── the lobby ────────────────────────────────────────────────────────────────


def test_lobby_names_the_game_host_and_roster_and_carries_nothing_host_only():
    items = build_lobby_layout("Alice", ["Bob", "Carol"], start_at=1_700_000_000)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Most Likely To\nHosted by **Alice**")
    assert "<t:1700000000:R>" in text
    assert "### Players (2)\nBob, Carol" in text
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [LOBBY_HELP_ID, JOIN_ID, LEAVE_ID]


def test_empty_and_closed_lobbies():
    assert "Nobody yet" in _text(build_lobby_layout("Alice", []))
    assert _ids(build_lobby_layout("Alice", ["Bob"], closed=True)) == [LOBBY_HELP_ID]


def test_lobby_roster_escapes_then_fits_whole_names():
    """A 32-char markdown name escapes to 64; 25 of them pass 1,600, past
    the old field cap. Whole names are dropped and counted, never cut."""
    text = _text(build_lobby_layout("Alice", ["**bold**", *[NASTY_NAME] * MAX_PLAYERS]))
    assert "\\*\\*bold\\*\\*" in text
    assert "…and" in text and "more" in text
    roster = text.split("Players (26)\n", 1)[1]
    assert len(roster) <= layouts.ROSTER_BUDGET


def test_lobby_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        "Alice", [], header_url="https://x/h.webp", footer_url="https://x/f.webp",
        title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)


# ── the round card ───────────────────────────────────────────────────────────

OPTIONS = [("Ada", "1"), ("Bo", "2"), ("Cy", "3")]


def test_open_round_is_the_prompt_the_dropdown_and_pose():
    items = build_round_layout("win a staring contest", 2, 0, OPTIONS)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Most Likely To…\nRound **2**")
    assert "Most likely to… **win a staring contest**" in text
    assert _ids(items) == [HELP_ID, VOTE_SELECT_ID, POSE_ID]
    select = next(i for i in card(items).walk_children() if isinstance(i, discord.ui.Select))
    assert [o.label for o in select.options] == ["Ada", "Bo", "Cy"]
    assert "votes" not in text, "the tally is secret while the round is open"


def test_dropdown_never_exceeds_discords_25_options():
    items = build_round_layout("x", 1, 0, [(f"P{i}", str(i)) for i in range(40)])
    select = next(i for i in card(items).walk_children() if isinstance(i, discord.ui.Select))
    assert len(select.options) == 25


def test_scheduled_round_keeps_next_on_the_card():
    assert _ids(build_round_layout("x", 1, 0, OPTIONS, scheduled=True))[-1] == NEXT_ID


def test_waiting_round_offers_only_pose_and_help():
    items = build_round_layout("", 1, 0, OPTIONS, waiting=True, scheduled=True)
    assert _ids(items) == [HELP_ID, POSE_ID]
    assert "Pose Prompt" in _text(items)


@pytest.mark.parametrize("count, said", [(1, "**1** vote cast"), (4, "**4** votes cast")])
def test_closed_round_says_how_many_voted_and_drops_every_control(count, said):
    items = build_round_layout("x", 3, count, OPTIONS, closed=True, scheduled=True)
    assert _ids(items) == []
    assert "Round Over" in _text(items) and said in _text(items)


def test_prompt_is_escaped_then_fitted():
    text = _text(build_round_layout("*sneaky* " + "*" * 2000, 1, 0, OPTIONS))
    assert "\\*sneaky\\*" in text
    assert len(text) < 1500


def test_queued_prompts_are_counted_in_the_subtext():
    assert "-# 2 prompts queued" in _text(build_round_layout("x", 1, 0, OPTIONS, queued=2))


# ── results, standings ───────────────────────────────────────────────────────


def test_results_crown_the_decided_winners_and_sort_by_votes():
    text = _text(build_results_layout(
        "win", 3, {1: 1, 2: 3, 3: 3}, name_fn=_named, winners=[2],
    ))
    assert text.startswith(f"## {layouts.ICON} Most Likely To — Round 3 Results\nMost likely to… **win**")
    lines = text.split("\n")[-3:]
    assert lines[0] == "👑 **Member2** — 3 votes"
    assert lines[1] == "**Member3** — 3 votes"
    assert lines[2] == "**Member1** — 1 vote"


def test_results_without_winners_crown_the_top_count_and_nobody_at_zero():
    tied = _text(build_results_layout("x", 1, {1: 2, 2: 2, 3: 0}, name_fn=_named)).split("\n", 1)[1]
    assert tied.count("👑") == 2
    assert "👑" not in _text(build_results_layout("x", 1, {1: 0}, name_fn=_named)).split("\n", 1)[1]
    assert _text(build_results_layout("x", 1, {})).endswith("No votes cast.")


def test_names_go_through_name_fn_and_a_miss_reads_user_id():
    text = _text(build_results_layout("x", 1, {7: 1}, name_fn=mention))
    assert "User 7" in text and "<@" not in text
    standings = _text(build_final_standings_layout({"7": 2}, name_fn=mention))
    assert "User 7" in standings and "<@" not in standings


def test_standings_rank_by_crowns_with_ties_at_the_top_crowned():
    text = _text(build_final_standings_layout({"1": 2, "2": 2, "3": 1}, name_fn=_named))
    assert text.startswith(f"## {layouts.ICON} Most Likely To — Game Over")
    assert "### Final Standings\n👑 **Member1** — 2 crowns\n👑 **Member2** — 2 crowns\n3. **Member3** — 1 crown" in text


def test_standings_say_the_last_round_only_at_the_cap_and_carry_the_payout():
    capped = _text(build_final_standings_layout({}, reason="round_cap", payout_line="🪙 +5"))
    assert "That's the last round!" in capped
    assert "No crowns were awarded this game." in capped
    assert capped.endswith("-# 🪙 +5")
    assert "last round" not in _text(build_final_standings_layout({}, reason="ended"))


def test_every_mlt_render_site_passes_a_resolver():
    """``name_fn`` defaults to ``mention`` so an un-wired caller still renders;
    that is exactly why the wiring needs its own guard."""
    needs = {
        name
        for name, fn in inspect.getmembers(layouts, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
        and fn.__module__ == layouts.__name__
        and not name.startswith("_")
    }
    assert needs == {"build_results_layout", "build_final_standings_layout"}
    source = pathlib.Path(inspect.getfile(mlt_cog)).read_text(encoding="utf-8")
    missed = [
        f"games_mlt_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


# ── every card: budget at the worst case, grey buttons ───────────────────────


def _every_card() -> dict[str, list[discord.ui.Item[Any]]]:
    names = [NASTY_NAME] * MAX_PLAYERS
    ids = list(range(1, MAX_PLAYERS + 1))
    nasty = lambda uid: "\\*" * 32  # noqa: E731
    return {
        "lobby": build_lobby_layout(NASTY_NAME, names, 1, header_url="https://x/h", footer_url="https://x/f"),
        "round": build_round_layout("~" * 2000, 50, 0, [(n, str(u)) for u, n in zip(ids, names)], advance_at=1, queued=15, scheduled=True),
        "round-closed": build_round_layout("~" * 2000, 50, 25, [], closed=True),
        "panel": build_host_panel_layout(),
        "results": build_results_layout("~" * 2000, 50, {uid: 1 for uid in ids}, name_fn=nasty, winners=ids),
        "standings": build_final_standings_layout({str(uid): uid for uid in ids}, name_fn=nasty, reason="round_cap", payout_line="🪙 +5 to everyone who played"),
    }


CARDS = _every_card()
every_card = pytest.mark.parametrize("name", list(CARDS))


@every_card
def test_every_card_fits_the_budget_at_its_worst_case(name):
    check_budget(card(CARDS[name]))


@every_card
def test_every_button_is_grey(name):
    buttons = [i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.Button)]
    assert {b.style for b in buttons} <= {discord.ButtonStyle.secondary}


def test_host_panel_is_start_next_and_end_on_their_own_ids():
    assert _ids(build_host_panel_layout()) == [HOST_START_ID, HOST_NEXT_ID, HOST_END_ID]
    assert {HOST_START_ID, HOST_NEXT_ID, HOST_END_ID}.isdisjoint({"mlt_start", "mlt_next", "mlt_end"})


# ── the host panel: one panel for the whole game ─────────────────────────────


def _bot():
    return SimpleNamespace(active_views={}, games_db=None)


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}", roles=[]),
        guild=None, guild_id=None, channel=SimpleNamespace(id=100, name="games"),
        message=SimpleNamespace(id=999, edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def _panel(bot):
    return host_panel.GameHostPanel(
        bot, "gid", build_host_panel_layout(),
        {HOST_START_ID: "start_game", HOST_NEXT_ID: "press_next", HOST_END_ID: "press_end"},
        view_type=(mlt_cog.MLTJoinView, mlt_cog.MLTVoteView),
    )


def _press(panel, custom_id):
    return next(i for i in panel.walk_children() if getattr(i, "custom_id", None) == custom_id).callback


def test_mlt_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["mlt"] == "MLTCog"


async def test_next_and_end_in_the_lobby_say_the_game_has_not_started():
    bot = _bot()
    bot.active_views["gid"] = mlt_cog.MLTJoinView("gid", 1, None, bot, None, players=[1, 2, 3])
    for custom_id in (HOST_NEXT_ID, HOST_END_ID):
        interaction = _interaction()
        await _press(_panel(bot), custom_id)(interaction)
        assert _said(interaction) == mlt_cog.NOT_STARTED_REPLY


async def test_start_once_the_game_is_running_says_so():
    bot = _bot()
    bot.active_views["gid"] = mlt_cog.MLTVoteView(
        "gid", 1, "x", 1, [1, 2], None, bot, "Host", None, AsyncMock(),
    )
    interaction = _interaction()
    await _press(_panel(bot), HOST_START_ID)(interaction)
    assert _said(interaction) == mlt_cog.ALREADY_STARTED_REPLY


async def test_games_host_opens_in_either_phase_and_refuses_a_stranger():
    bot = _bot()
    cog = mlt_cog.MLTCog(bot)  # type: ignore[arg-type]
    bot.active_views["gid"] = mlt_cog.MLTJoinView("gid", 1, None, bot, cog)
    host = _interaction()
    await cog.open_host_panel(host, "gid")  # type: ignore[arg-type]
    assert isinstance(host.response.send_message.await_args.kwargs["view"], host_panel.GameHostPanel)

    stranger = _interaction(2)
    await cog.open_host_panel(stranger, "gid")  # type: ignore[arg-type]
    assert _said(stranger) == CONTROL_DENIED

    bot.active_views["gid"] = mlt_cog.MLTVoteView("gid", 1, "x", 1, [1], None, bot, "Host", None, AsyncMock())
    again = _interaction()
    await cog.open_host_panel(again, "gid")  # type: ignore[arg-type]
    assert "view" in again.response.send_message.await_args.kwargs


async def test_start_from_the_panel_closes_the_lobby_and_runs_once(monkeypatch):
    monkeypatch.setattr(mlt_cog, "get_game_payload", AsyncMock(return_value={"players": [1, 2, 3]}))
    monkeypatch.setattr(mlt_cog, "update_game_state", AsyncMock())
    bot = _bot()
    run = AsyncMock()
    cog = SimpleNamespace(_run_round=run)
    view = mlt_cog.MLTJoinView("gid", 1, None, bot, cog, players=[1, 2, 3])
    view._msg = SimpleNamespace(edit=AsyncMock(), channel="chan", guild=None)
    bot.active_views["gid"] = view

    await view.start_game(_interaction())  # type: ignore[arg-type]
    await view.start_game(_interaction())  # type: ignore[arg-type]

    run.assert_awaited_once()
    assert run.await_args.kwargs["players"] == [1, 2, 3]
    assert run.await_args.kwargs["channel"] == "chan"
    edited = view._msg.edit.await_args.kwargs
    assert edited["view"] is view and edited["embed"] is None
    assert _ids(list(view.children)) == [LOBBY_HELP_ID]


async def test_panel_next_advances_the_board_not_the_panel_message():
    bot = _bot()
    view = mlt_cog.MLTVoteView(
        "gid", 1, "x", 1, [1, 2], None, bot, "Host", None, AsyncMock(),
        pacing=RoundPacing(opened_at=1.0),
    )
    view.message = SimpleNamespace(id=555)
    interaction = _interaction()
    await view.press_next(interaction)  # type: ignore[arg-type]
    view.advance_callback.assert_awaited_once_with(view.message)


@pytest.mark.parametrize(
    "game_host, starts",
    [
        pytest.param(True, True, id="game-host-role-starts"),
        pytest.param(False, False, id="plain-member-refused"),
    ],
)
async def test_start_takes_the_same_gate_the_panel_opened_on(monkeypatch, game_host, starts):
    """A Game Host could open the host panel (``may_control``) and then had
    Start refused by a narrower host-or-mod check (ship review, 2026-10-01)."""
    from bot_modules.games.utils import round_pacing

    monkeypatch.setattr(round_pacing, "has_game_host_role", AsyncMock(return_value=game_host))
    monkeypatch.setattr(mlt_cog, "get_game_payload", AsyncMock(return_value={"players": [1, 2, 3]}))
    monkeypatch.setattr(mlt_cog, "update_game_state", AsyncMock())
    bot = _bot()
    run = AsyncMock()
    view = mlt_cog.MLTJoinView("gid", 1, None, bot, SimpleNamespace(_run_round=run), players=[1, 2, 3])
    view._msg = SimpleNamespace(edit=AsyncMock(), channel="chan", guild=None)
    bot.active_views["gid"] = view

    press = _interaction(uid=7)  # not the host, not a mod
    await view.start_game(press)  # type: ignore[arg-type]

    if starts:
        run.assert_awaited_once()
    else:
        run.assert_not_awaited()
        assert _said(press) == CONTROL_DENIED
