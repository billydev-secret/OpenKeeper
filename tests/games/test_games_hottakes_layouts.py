"""Tests for ``bot_modules/games/hottakes/layouts.py`` — the Components V2
cards that replaced ``games_hottakes/embeds.py`` — and the host panel that
took Start Voting, Cancel Game and Next Take off the boards.

Budget is checked at the worst case the game can produce: a 500-character
take of markdown (doubled by escaping) on the card, and three of them
quoted on the recap.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.hottakes.cog as hottakes_cog
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.hottakes import layouts
from bot_modules.games.hottakes.layouts import (
    HELP_ID,
    HOST_CANCEL_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    LOBBY_DESCRIPTION,
    SUBMIT_ID,
    VOTE_IDS,
    VOTE_SUBMIT_ID,
    build_host_panel_layout,
    build_lobby_layout,
    build_notice_layout,
    build_recap_layout,
    build_vote_layout,
)
from bot_modules.games.hottakes.logic import VOTE_LABELS

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


def test_lobby_says_how_to_play_counts_takes_and_carries_nothing_host_only():
    items = build_lobby_layout("*Ada*", 7, start_at=1_700_000_000)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Hot Takes\nHosted by **\\*Ada\\*** · 👁 Anonymous")
    assert LOBBY_DESCRIPTION in text
    assert "How to play" in text and "mods can still see who sent it" in text
    assert "📝 Submissions: **7** · ⏰ Starting <t:1700000000:R>" in text
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [HELP_ID, SUBMIT_ID]


def test_lobby_without_a_countdown():
    text = _text(build_lobby_layout("Ada"))
    assert "📝 Submissions: **0**" in text and "Starting" not in text


def test_closed_lobby_drops_submit_and_says_where_a_late_take_goes():
    items = build_lobby_layout("Ada", 3, closed=True)
    assert _ids(items) == []  # stopped view: a Help here could only fail (ship review, 2026-10-01)
    assert layouts.VOTING_UNDERWAY in _text(items)
    assert host_panel.HOST_HINT not in _text(items)


def test_lobby_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        "Ada", header_url="https://x/h.webp", footer_url="https://x/f.webp", title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)


def test_host_panel_carries_start_a_red_cancel_and_next():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_START_ID, HOST_CANCEL_ID, HOST_NEXT_ID]
    buttons = {b.custom_id: b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)}
    assert buttons[HOST_CANCEL_ID].style == discord.ButtonStyle.danger
    assert buttons[HOST_START_ID].style == buttons[HOST_NEXT_ID].style == discord.ButtonStyle.secondary
    assert set(buttons).isdisjoint({"ht_start", "ht_cancel", "ht_next"})
    assert host_panel.HOST_COMMAND in _text(items)


def test_cancelled_notice():
    assert _text(build_notice_layout(layouts.CANCELLED_TEXT)) == (
        f"## {layouts.ICON} Hot Takes\n{layouts.CANCELLED_TEXT}"
    )


# ── the take card ────────────────────────────────────────────────────────────


def test_take_card_shows_the_take_the_bars_and_the_votes_and_names_nobody():
    items = build_vote_layout("**bold** _italic_", 3, 5, {1: 4, 2: 0})
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Hot Take #3\nTake **3**/5 · 👁 Anonymous")
    assert "**\\*\\*bold\\*\\* \\_italic\\_**" in text
    for label in VOTE_LABELS:
        assert label in text
    assert "(1)" in text and "(0)" in text
    assert _ids(items) == [HELP_ID, *VOTE_IDS, VOTE_SUBMIT_ID]
    assert f"-# {host_panel.HOST_HINT}" in text
    assert "<@" not in text


def test_closed_take_is_a_record_with_no_controls():
    items = build_vote_layout("t", 2, 4, {1: 4}, closed=True)
    assert _ids(items) == []
    assert "Hot Take #2 — Round Over" in _text(items)


@pytest.mark.parametrize(
    ("advance_at", "closed", "shown"),
    [
        pytest.param(1_700_000_000, False, True, id="timed-open"),
        pytest.param(1_700_000_000, True, False, id="timed-closed"),
        pytest.param(None, False, False, id="host-paced"),
    ],
)
def test_take_card_shows_the_countdown_only_while_timed_and_open(advance_at, closed, shown):
    text = _text(build_vote_layout("take", 1, 2, {}, closed=closed, advance_at=advance_at))
    assert ("⏱️ Auto-advances <t:1700000000:R>" in text) is shown


def test_every_vote_button_is_grey():
    buttons = [b for b in card(build_vote_layout("t", 1, 1, {})).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    assert [b.label for b in buttons if b.custom_id in VOTE_IDS] == layouts.VOTE_GLYPHS


# ── the recap ────────────────────────────────────────────────────────────────


def test_recap_is_nothing_for_no_results():
    assert build_recap_layout([]) is None


def test_recap_with_one_take_omits_most_divisive():
    items = build_recap_layout([{"text": "only one", "avg": 3.456, "std": 0.0, "voters": [1, 2]}])
    assert items is not None
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Hot Takes — Game Over")
    assert "Takes: **1** · Voters: **2**" in text
    assert '### 🔥 Hottest Take\n"only one" (avg 3.5/5)' in text
    assert '### 🧊 Coldest Take\n"only one" (avg 3.5/5)' in text
    assert "Most Divisive" not in text


def test_recap_with_several_takes_names_the_divisive_one_escaped_and_dedupes_voters():
    # hottest/coldest (siblings, same results data) already escaped their
    # take text; most_divisive was left raw in the embed era — a submitted
    # take of ``||hidden||`` would spoiler-hide the whole recap field.
    items = build_recap_layout([
        {"text": "mild", "avg": 3.0, "std": 0.1, "voters": [1, 2]},
        {"text": "hot", "avg": 4.5, "std": 0.2, "voters": [2, 3]},
        {"text": "||divisive||", "avg": 3.5, "std": 2.5, "voters": [3]},
    ], payout_line="🪙 +5 to everyone")
    assert items is not None
    text = _text(items)
    assert '### 🔥 Hottest Take\n"hot"' in text and '### 🧊 Coldest Take\n"mild"' in text
    assert '### ⚡ Most Divisive\n"\\|\\|divisive\\|\\|"' in text
    assert "||divisive||" not in text
    assert "Takes: **3** · Voters: **3**" in text
    assert text.endswith("-# 🪙 +5 to everyone")
    assert _ids(items) == []


# ── every card: budget at the worst case, glyph heading ──────────────────────


CARDS = {
    "lobby": build_lobby_layout(NASTY, 999, start_at=1, header_url="https://x/h", footer_url="https://x/f"),
    "panel": build_host_panel_layout(),
    "vote": build_vote_layout("~" * 500, 99, 99, {i: i % 5 for i in range(50)}, advance_at=1),
    "recap": build_recap_layout(
        [{"text": "~" * 500, "avg": 3.0, "std": float(i), "voters": [i]} for i in range(3)],
        payout_line="🪙 +20 to winners • +5 to everyone who played",
    ),
    "notice": build_notice_layout(layouts.CANCELLED_TEXT),
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


def _bot():
    return SimpleNamespace(active_views={}, games_db=None)


def _vote_view(bot):
    return hottakes_cog.HotTakeVoteView("gid", 1, "take", 1, 2, None, bot, "Host", AsyncMock(), take_author_id=9)


def test_hot_takes_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["hottakes"] == "HotTakesCog"


async def test_games_host_opens_in_either_phase_and_refuses_a_stranger():
    bot = _bot()
    cog = hottakes_cog.HotTakesCog(bot)  # type: ignore[arg-type]
    for view in (hottakes_cog.HotTakesSubmitView("gid", 1, None, bot, cog), _vote_view(bot)):
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
        pytest.param("lobby", "next_take", hottakes_cog.NOT_VOTING_REPLY, id="next-in-the-lobby"),
        pytest.param("vote", "start_voting", hottakes_cog.VOTING_STARTED_REPLY, id="start-during-the-vote"),
        pytest.param("vote", "cancel_game", hottakes_cog.CANCEL_AFTER_START_REPLY, id="cancel-during-the-vote"),
    ],
)
async def test_a_panel_press_out_of_its_phase_answers_plainly(phase, press, expected):
    bot = _bot()
    views = {
        "lobby": hottakes_cog.HotTakesSubmitView("gid", 1, None, bot, None),
        "vote": _vote_view(bot),
    }
    interaction = _interaction(1)
    await getattr(views[phase], press)(interaction)
    assert _said(interaction) == expected


async def test_a_closed_lobby_refuses_submit_start_and_cancel():
    """Once voting opens the lobby's own card has no Submit button, but the
    persistent id could still arrive from a stale client; and the panel's
    Start / Cancel are told the phase has passed."""
    view = hottakes_cog.HotTakesSubmitView("gid", 1, None, _bot(), None)
    view._closed = True
    for press, expected in (
        (view.submit, hottakes_cog.VOTING_STARTED_REPLY),
        (view.start_voting, hottakes_cog.VOTING_STARTED_REPLY),
        (view.cancel_game, hottakes_cog.CANCEL_AFTER_START_REPLY),
    ):
        interaction = _interaction(1)
        await press(interaction)
        assert _said(interaction) == expected
        interaction.response.send_modal.assert_not_awaited()


async def test_a_take_landing_redraws_the_lobby_count_from_the_payload():
    view = hottakes_cog.HotTakesSubmitView("gid", 1, None, _bot(), None)
    view.message = SimpleNamespace(edit=AsyncMock())
    await view.show_count(4)
    assert "📝 Submissions: **4**" in _text(list(view.children))
    kwargs = view.message.edit.await_args.kwargs
    assert kwargs["view"] is view and kwargs["embed"] is None and kwargs["content"] is None
