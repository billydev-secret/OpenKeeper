"""Tests for ``bot_modules/games/price/layouts.py`` — the Components V2
cards that replaced ``games_price/embeds.py`` — and the host panel that
took every host-only control off the boards.

Budget is checked at the worst case the game can produce: anyone in the
channel may join or price, so a 300-name roster and a 300-line ladder of
markdown names are the shapes to check, with a 500-character scenario
doubled by escaping.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.price.cog as price_cog
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.price import layouts
from bot_modules.games.price.layouts import (
    HELP_ID,
    HOST_ADD_ROUNDS_ID,
    HOST_CANCEL_ID,
    HOST_DRAW_ID,
    HOST_END_ID,
    HOST_SKIP_ID,
    HOST_START_ID,
    HOST_WRITE_ID,
    JOIN_ID,
    LEAVE_ID,
    LOBBY_HELP_ID,
    REASONABLE_ID,
    RUN_AGAIN_ID,
    SUBMIT_ID,
    SUBMIT_TIMER_ITEM_ID,
    UNHINGED_ID,
    VOTE_TIMER_ITEM_ID,
    WRITE_ID,
    build_host_panel_layout,
    build_lobby_layout,
    build_notice_layout,
    build_recap_layout,
    build_reveal_layout,
    build_round_results_layout,
    build_scenario_layout,
    build_scenario_wait_layout,
    build_start_layout,
    build_vote_layout,
)

NASTY = "*" * 32


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


def test_lobby_lists_the_roster_source_and_countdown_and_nothing_host_only():
    items = build_lobby_layout("Ada", ["Bo", "*Cy*"], 5, "players", start_at=1_700_000_000, min_players=2)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Name Your Price\nHosted by **Ada**")
    assert "Rounds: **5** · Scenarios: anyone can write one — first in wins" in text
    assert "⏰ Starting <t:1700000000:R> — on its own, once 2 have joined" in text
    assert "### Players (2)\nBo\n\\*Cy\\*" in text
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [LOBBY_HELP_ID, JOIN_ID, LEAVE_ID]


def test_lobby_without_countdown_or_players():
    text = _text(build_lobby_layout("Ada", [], 3, "bank"))
    assert "Starting" not in text
    assert "### Players (0)\n-# Nobody yet" in text
    assert "the question bank" in text


def test_closed_lobby_drops_join_and_leave():
    assert _ids(build_lobby_layout("Ada", ["Bo"], 3, "bank", closed=True)) == [LOBBY_HELP_ID]


def test_lobby_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        "Ada", [], 3, "bank", header_url="https://x/h.webp", footer_url="https://x/f.webp",
        title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)


def test_host_panel_carries_every_host_control_with_a_red_cancel():
    items = build_host_panel_layout()
    assert _ids(items) == [
        HOST_START_ID, HOST_CANCEL_ID, HOST_WRITE_ID, HOST_DRAW_ID,
        HOST_SKIP_ID, HOST_ADD_ROUNDS_ID, HOST_END_ID,
    ]
    buttons = {b.custom_id: b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)}
    assert buttons[HOST_CANCEL_ID].style == discord.ButtonStyle.danger
    assert {b.style for cid, b in buttons.items() if cid != HOST_CANCEL_ID} == {discord.ButtonStyle.secondary}
    assert set(buttons).isdisjoint({"price_start", "price_cancel", "price_skip", "price_add_rounds", "price_end", "price_draw_bank"})


# ── between the lobby and a price ────────────────────────────────────────────


def test_start_frame_names_the_host_and_round():
    text = _text(build_start_layout("Ada", 1, 5))
    assert "Hosted by **Ada** · Round 1/5" in text and "Starting up" in text


@pytest.mark.parametrize(
    "source, who, on_card",
    [
        pytest.param("host", "The host", False, id="host-writes-from-the-panel"),
        pytest.param("players", "Anyone in the room", True, id="players-write-from-the-card"),
    ],
)
def test_scenario_wait_says_who_writes_and_where(source, who, on_card):
    items = build_scenario_wait_layout(2, 5, source)
    text = _text(items)
    assert f"✍️ {who} is writing this round's scenario" in text
    assert (_ids(items) == [WRITE_ID]) is on_card
    assert (host_panel.HOST_COMMAND in text) is not on_card


# ── the submission board ─────────────────────────────────────────────────────


def test_board_renders_the_scenario_countdown_and_count():
    items = build_scenario_layout("Eat a bug", 2, 5, 1_700_000_000, 3, 4, roster_submitted=2)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Name Your Price — Round 2/5")
    timer = card(items).find_item(SUBMIT_TIMER_ITEM_ID)
    assert isinstance(timer, discord.ui.TextDisplay) and "<t:1700000000:R>" in timer.content
    assert '### "Eat a bug"' in text
    assert "💵 Submitted: **3** (2/4 who joined" in text
    assert "The host can press **Skip**" in text
    assert _ids(items) == [HELP_ID, SUBMIT_ID]
    assert f"-# {host_panel.HOST_HINT}" in text


def test_board_count_without_a_roster_and_the_closed_frame():
    assert "💵 Submitted: **1**/3 — the round closes once everyone has answered" in _text(
        build_scenario_layout("x", 1, 1, 1, 1, 3)
    )
    closed = build_scenario_layout("x", 1, 1, 1, 2, closed=True)
    assert _ids(closed) == []
    assert "<t:" not in _text(closed) and "Skip" not in _text(closed) and "**2**" in _text(closed)


def test_scenario_is_escaped_then_fitted():
    text = _text(build_scenario_layout("**bold** " + "*" * 2000, 1, 1, 1, 0))
    assert "\\*\\*bold\\*\\*" in text and len(text) < 1500


# ── the reveal, the vote, the results ────────────────────────────────────────


def test_reveal_ladder_and_stats():
    text = _text(build_reveal_layout("scen", 1, 3, [("*Ann*", 0), ("Bo", 500), ("Cy", 10_000_000)]))
    assert text.startswith(f"## {layouts.ICON} Reveal — Round 1/3\n\"scen\"")
    assert "### 💵 Price Ladder" in text
    assert "— **\\*Ann\\***" in text and "— **Bo**" in text
    assert "### 📊 Stats\nSpread:" in text and "Median:" in text and "Average:" in text


def test_reveal_omits_stats_for_an_empty_ladder():
    text = _text(build_reveal_layout("scen", 1, 3, []))
    assert "Price Ladder\n—" in text and "Stats" not in text


def test_vote_card_has_the_countdown_and_two_dropdowns_capped_at_25():
    items = build_vote_layout("scen", 1, 3, 1_700_000_000, [(f"P{i} — $1", str(i)) for i in range(40)])
    timer = card(items).find_item(VOTE_TIMER_ITEM_ID)
    assert isinstance(timer, discord.ui.TextDisplay)
    selects = [i for i in card(items).walk_children() if isinstance(i, discord.ui.Select)]
    assert [s.custom_id for s in selects] == [REASONABLE_ID, UNHINGED_ID]
    assert all(len(s.options) == 25 for s in selects)
    assert "Most Reasonable" in _text(items) and "Most Unhinged" in _text(items)


@pytest.mark.parametrize("votes, word", [(1, "1 vote"), (3, "3 votes")])
def test_round_results_name_winners_prices_and_votes(votes, word):
    text = _text(build_round_results_layout(2, 5, ["Ann"], 500, votes, ["Bo", "Cy"], 9_000_000, 2))
    assert text.startswith(f"## {layouts.ICON} Round 2 Results")
    assert f"🎯 **Most Reasonable**\n**Ann** ($500) — {word}" in text
    assert "🤯 **Most Unhinged**\n**Bo & Cy** ($9.0M) — 2 votes" in text
    assert "**Nobody**" in _text(build_round_results_layout(1, 1, [], 0, 0, [], 0, 0))


# ── the recap ────────────────────────────────────────────────────────────────


def test_recap_summary_awards_highlight_payout_and_run_again():
    items = build_recap_layout(
        3, 4,
        {"a": ("🏆 Cheapest", ["*Ann*"], "$5"), "b": ("💸 Priciest", [], "—"), "c": ("🎯 Sensible", ["Bo", "Cy"], "2 wins")},
        "Round 2 had the widest spread — $5 to $9,000,000", payout_line="🪙 +5 to everyone",
    )
    text = _text(items)
    assert "🎮 Rounds played: **3** · 👥 Players: **4**" in text
    assert "### 🏆 Awards\n🏆 Cheapest **\\*Ann\\*** — $5\n🎯 Sensible **Bo & Cy** — 2 wins" in text
    assert "Priciest" not in text, "an award with nobody on it is skipped"
    assert "### 💡 Highlight\nRound 2 had the widest spread" in text
    assert "-# 🪙 +5 to everyone" in text
    assert _ids(items) == [RUN_AGAIN_ID]


def test_recap_without_awards_or_highlight_is_the_summary_alone():
    text = _text(build_recap_layout(1, 2, {"a": ("x", [], "")}, None))
    assert "Awards" not in text and "Highlight" not in text


def test_notices():
    assert _text(build_notice_layout(layouts.CANCELLED_TEXT)).endswith(layouts.CANCELLED_TEXT)
    assert _text(build_notice_layout(layouts.PICKING_UP_TEXT)).startswith(f"## {layouts.ICON} Name Your Price")


# ── every card: budget at the worst case, glyph heading ──────────────────────


CARDS = {
    "lobby": build_lobby_layout(NASTY, [NASTY] * 300, 20, "players", start_at=1, header_url="https://x/h", footer_url="https://x/f"),
    "panel": build_host_panel_layout(),
    "start": build_start_layout(NASTY, 1, 20),
    "wait": build_scenario_wait_layout(20, 20, "players"),
    "board": build_scenario_layout("~" * 500, 20, 20, 1, 300, 300, roster_submitted=300),
    "reveal": build_reveal_layout("~" * 500, 20, 20, [(NASTY, 10**12)] * 300),
    "vote": build_vote_layout("~" * 500, 20, 20, 1, [(NASTY + " — $1,000,000,000,000", str(i)) for i in range(300)]),
    "results": build_round_results_layout(20, 20, [NASTY] * 50, 10**12, 99, [NASTY] * 50, 10**12, 99),
    "recap": build_recap_layout(20, 300, {k: (k, [NASTY] * 50, "~" * 40) for k in ("a", "b", "c", "d", "e")}, "~" * 200, payout_line="🪙 +20 to winners • +5 to everyone who played"),
    "notice": build_notice_layout(layouts.PICKING_UP_TEXT),
}
every_card = pytest.mark.parametrize("name", list(CARDS))


@every_card
def test_every_card_fits_the_budget_at_its_worst_case(name):
    view = card(CARDS[name])
    check_budget(view)
    assert view.content_length() <= MAX_CHARS


@every_card
def test_every_card_leads_with_a_glyph_led_heading(name):
    first = next(i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert first.content.startswith(f"## {layouts.ICON} ")


# ── the host panel: one panel for the whole game ─────────────────────────────


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}"),
        guild=None, guild_id=None, channel=SimpleNamespace(id=100, name="games"),
        message=None,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_price_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["price"] == "PriceCog"


async def test_games_host_opens_in_any_phase_and_refuses_a_stranger():
    bot = SimpleNamespace(active_views={}, games_db=None)
    cog = price_cog.PriceCog(bot)  # type: ignore[arg-type]
    for view in (
        price_cog.PriceLobbyView("gid", 1, None, bot, cog),
        price_cog.ScenarioPromptView(price_cog.HostScenarioModal(), 1, 1, 3, open_to_all=False),
        price_cog.PriceVoteView("gid", 1, "Host", "x", [1, 2], {1: 5, 2: 6}, 1, 3, 20, None, None, bot),
    ):
        bot.active_views["gid"] = view
        host = _interaction(1)
        await cog.open_host_panel(host, "gid")  # type: ignore[arg-type]
        assert isinstance(host.response.send_message.await_args.kwargs["view"], host_panel.GameHostPanel)
        stranger = _interaction(2)
        await cog.open_host_panel(stranger, "gid")  # type: ignore[arg-type]
        assert _said(stranger) == host_panel.NOT_HOST_REPLY
    bot.active_views.clear()
    late = _interaction(1)
    await cog.open_host_panel(late, "gid")  # type: ignore[arg-type]
    assert _said(late) == host_panel.GAME_ENDED_REPLY


@pytest.mark.parametrize(
    "phase, press, expected",
    [
        pytest.param("lobby", "skip_round", price_cog.NO_ROUND_REPLY, id="skip-in-the-lobby"),
        pytest.param("lobby", "write_scenario", price_cog.NO_SCENARIO_WAIT_REPLY, id="write-in-the-lobby"),
        pytest.param("lobby", "end_game_button", price_cog.NOT_STARTED_REPLY, id="end-in-the-lobby"),
        pytest.param("wait", "start_game", price_cog.ALREADY_STARTED_REPLY, id="start-during-the-wait"),
        pytest.param("wait", "cancel_lobby", price_cog.ALREADY_STARTED_REPLY, id="cancel-during-the-wait"),
        pytest.param("wait", "skip_round", price_cog.NO_ROUND_REPLY, id="skip-during-the-wait"),
        pytest.param("vote", "draw_bank", price_cog.NO_SCENARIO_WAIT_REPLY, id="draw-during-the-vote"),
        pytest.param("vote", "skip_round", price_cog.NO_ROUND_REPLY, id="skip-during-the-vote"),
        # Not "hasn't started yet": the game is under way in both phases.
        pytest.param("wait", "add_rounds", price_cog.SCENARIO_WAIT_ADD_REPLY, id="add-rounds-during-the-wait"),
        pytest.param("wait", "end_game_button", price_cog.SCENARIO_WAIT_END_REPLY, id="end-during-the-wait"),
        pytest.param("vote", "end_game_button", price_cog.VOTE_END_REPLY, id="end-during-the-vote"),
    ],
)
async def test_a_panel_press_out_of_its_phase_answers_plainly(phase, press, expected):
    bot = SimpleNamespace(active_views={}, games_db=None)
    views = {
        "lobby": price_cog.PriceLobbyView("gid", 1, None, bot, None),
        "wait": price_cog.ScenarioPromptView(price_cog.HostScenarioModal(), 1, 1, 3, open_to_all=False),
        "vote": price_cog.PriceVoteView("gid", 1, "Host", "x", [1, 2], {1: 5, 2: 6}, 1, 3, 20, None, None, bot),
    }
    interaction = _interaction(1)
    await getattr(views[phase], press)(interaction)
    assert _said(interaction) == expected


async def test_scenario_wait_takes_the_hosts_write_and_draw_from_the_panel():
    modal = price_cog.HostScenarioModal()
    view = price_cog.ScenarioPromptView(modal, 1, 1, 3, open_to_all=False)
    assert _ids(list(view.children)) == [], "the host's Write Scenario is on the panel, not the card"
    stranger = _interaction(2)
    await view.write_scenario(stranger)
    assert "Only the host or a mod" in _said(stranger)
    host = _interaction(1)
    await view.write_scenario(host)
    host.response.send_modal.assert_awaited_once_with(modal)
    draw = _interaction(1)
    await view.draw_bank(draw)
    assert modal.resolved and await modal.wait_for_result(timeout=0.01) is None


def test_open_to_all_wait_keeps_write_on_the_card():
    view = price_cog.ScenarioPromptView(price_cog.HostScenarioModal(), 1, 1, 3, open_to_all=True)
    assert [i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)] == [WRITE_ID]


async def test_vote_refuses_a_self_vote_and_closes_when_everyone_has_voted():
    bot = SimpleNamespace(active_views={}, games_db=None)
    view = price_cog.PriceVoteView("gid", 1, "Host", "x", [1, 2], {1: 5, 2: 6}, 1, 3, 20, None, None, bot)
    view._timer = SimpleNamespace(skip=AsyncMock())  # type: ignore[assignment]
    skipped = []
    view.skip_timer = lambda: skipped.append(True)  # type: ignore[method-assign]
    own = _interaction(1)
    own.data = {"values": ["1"]}
    await view._vote_reasonable(own)
    assert _said(own) == "❌ You can't vote for yourself!"
    for uid, target in ((1, 2), (2, 1)):
        for press in (view._vote_reasonable, view._vote_unhinged):
            i = _interaction(uid)
            i.data = {"values": [str(target)]}
            await press(i)
    assert view.reasonable_votes == {1: 2, 2: 1} and view.unhinged_votes == {1: 2, 2: 1}
    assert skipped


@pytest.mark.parametrize(
    ("closed", "confirms"),
    [
        pytest.param(False, True, id="taking-prices-asks-to-confirm"),
        # The few seconds of reveal: the round is closed, ``end_early``
        # would return without a word after the host had confirmed.
        pytest.param(True, False, id="reveal-says-the-round-is-over"),
    ],
)
async def test_end_game_during_the_reveal_never_shows_a_confirm_that_does_nothing(closed, confirms):
    bot = SimpleNamespace(active_views={}, games_db=None)
    view = price_cog.PriceGameView(
        game_id="gid", host_id=1, host_name="Host", scenario="x", round_num=1, total_rounds=3,
        timer_secs=30, db=None, bot=bot, cog=price_cog.PriceCog(bot),  # type: ignore[arg-type]
        expected_ids={1, 2}, settings={"rounds": 3},
    )
    view._closed = closed
    interaction = _interaction(1)

    await view.end_game_button(interaction)  # type: ignore[arg-type]

    sent = interaction.response.send_message.await_args
    assert ("view" in sent.kwargs) is confirms
    if not confirms:
        assert sent.args[0] == price_cog.ROUND_OVER_REPLY
