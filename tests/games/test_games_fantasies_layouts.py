"""Tests for ``bot_modules/games/fantasies/layouts.py`` — the Components V2
cards that replaced ``games_fantasies/embeds.py`` — and the host panel that
took Start Round, Close Submissions, Next and End Game off the boards.

Budget is checked at the worst case the game can produce: a 500-character
entry of markdown (doubled by escaping) on the card, and three of them
quoted on the recap.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.fantasies.cog as fan_cog
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.fantasies import layouts
from bot_modules.games.fantasies.layouts import (
    HELP_ID,
    HOST_CLOSE_ID,
    HOST_END_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    LOBBY_DESCRIPTION,
    NOPE_ID,
    SAME_ID,
    SUBMIT_DEALBREAKER_ID,
    SUBMIT_FANTASY_ID,
    build_host_panel_layout,
    build_lobby_layout,
    build_recap_layout,
    build_submit_layout,
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


# ── the panel ────────────────────────────────────────────────────────────────


def test_panel_says_how_to_play_counts_rounds_and_carries_nothing_host_only():
    items = build_lobby_layout("*Ada*", 2, start_at=1_700_000_000)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Fantasies & Dealbreakers\nHosted by **\\*Ada\\*** · 👁 Anonymous")
    assert LOBBY_DESCRIPTION in text
    assert "How to play" in text and "mods can still see who sent it" in text
    assert "🔁 Rounds played: **2** · ⏰ Starting <t:1700000000:R>" in text
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [HELP_ID]


def test_panel_without_a_countdown():
    text = _text(build_lobby_layout("Ada"))
    assert "🔁 Rounds played: **0**" in text and "Starting" not in text


def test_ended_panel_drops_help_and_the_countdown_and_says_so():
    items = build_lobby_layout("Ada", 3, start_at=1_700_000_000, ended=True)
    assert _ids(items) == []
    text = _text(items)
    assert layouts.ENDED_LINE in text and "Starting" not in text and host_panel.HOST_HINT not in text


def test_panel_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        "Ada", header_url="https://x/h.webp", footer_url="https://x/f.webp", title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)


def test_host_panel_carries_every_host_control_all_grey():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_START_ID, HOST_CLOSE_ID, HOST_NEXT_ID, HOST_END_ID]
    buttons = [b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    assert {b.custom_id for b in buttons}.isdisjoint({"fan_start_round", "fan_close_sub", "fan_next", "fan_end"})
    assert host_panel.HOST_COMMAND in _text(items)


# ── the submissions card ─────────────────────────────────────────────────────


def test_submissions_card_offers_both_categories_and_the_closed_frame_none():
    items = build_submit_layout(3)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Round 3\nSubmit your fantasy or dealbreaker anonymously!")
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [SUBMIT_FANTASY_ID, SUBMIT_DEALBREAKER_ID]
    labels = [b.label for b in card(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert labels == ["💖 Submit a Fantasy", "🚩 Submit a Dealbreaker"]
    closed = build_submit_layout(3, closed=True)
    assert _ids(closed) == []
    assert "✅ Submissions closed for Round 3!" in _text(closed)


# ── the entry card ───────────────────────────────────────────────────────────


def test_entry_card_shows_the_entry_the_bars_and_the_votes_and_names_nobody():
    items = build_vote_layout(
        entry_text="**bold** _italic_", entry_num=2, category="Dealbreaker",
        same_votes=[1, 2], nope_votes=[3], total_entries=4,
    )
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Dealbreaker #2\nEntry **2**/4 · 👁 Anonymous")
    assert "**\\*\\*bold\\*\\* \\_italic\\_**" in text
    assert "✅ Same\n" in text and "67% (2)" in text
    assert "❌ Not for me\n" in text and "33% (1)" in text
    assert _ids(items) == [HELP_ID, SAME_ID, NOPE_ID]
    assert f"-# {host_panel.HOST_HINT}" in text
    assert "<@" not in text


def test_entry_progress_without_a_total():
    text = _text(build_vote_layout(entry_text="e", entry_num=1, category="Fantasy", same_votes=[], nope_votes=[]))
    assert "\nEntry **1** · 👁 Anonymous" in text


def test_closed_entry_is_a_record_with_no_controls():
    items = build_vote_layout(
        entry_text="e", entry_num=1, category="Fantasy", same_votes=[1], nope_votes=[], closed=True,
    )
    assert _ids(items) == []
    assert "Fantasy #1 — Round Over" in _text(items)


@pytest.mark.parametrize(
    ("advance_at", "closed", "shown"),
    [
        pytest.param(1_700_000_000, False, True, id="timed-open"),
        pytest.param(1_700_000_000, True, False, id="timed-closed"),
        pytest.param(None, False, False, id="host-paced"),
    ],
)
def test_entry_card_shows_the_countdown_only_while_timed_and_open(advance_at, closed, shown):
    text = _text(build_vote_layout(
        entry_text="x", entry_num=1, category="Fantasy", same_votes=[], nope_votes=[],
        closed=closed, advance_at=advance_at,
    ))
    assert ("⏱️ Auto-advances <t:1700000000:R>" in text) is shown


def test_every_vote_button_is_grey():
    """The embed era's green Same and red Not for Me ranked a preference
    that carries no such meaning (guide § Anatomy)."""
    items = build_vote_layout(entry_text="e", entry_num=1, category="Fantasy", same_votes=[], nope_votes=[])
    buttons = [b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}


# ── the recap ────────────────────────────────────────────────────────────────


def test_recap_is_nothing_for_no_results():
    assert build_recap_layout([]) is None


def test_recap_names_the_three_picks_escaped_with_the_tallies_and_payout():
    items = build_recap_layout([
        {"text": "beach", "same_pct": 1.0, "voters": [1, 2], "category": "Fantasy"},
        {"text": "||snoring||", "same_pct": 0.5, "voters": [2, 3], "category": "Dealbreaker"},
        {"text": "*spiders*", "same_pct": 0.0, "voters": [3], "category": "Dealbreaker"},
    ], payout_line="🪙 +5 to everyone")
    assert items is not None
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Fantasies & Dealbreakers — Game Over")
    assert "Entries: **3** · Voters: **3**" in text
    assert '### 🌟 Most Universally Shared\n"beach" (100% Same)' in text
    assert '### ⚡ Most Polarizing\n"\\|\\|snoring\\|\\|"' in text
    assert '### 🏔️ Biggest Outlier\n"\\*spiders\\*" (0% Same)' in text
    assert "||snoring||" not in text
    assert text.endswith("-# 🪙 +5 to everyone")
    assert _ids(items) == []


# ── every card: budget at the worst case, glyph heading ──────────────────────


CARDS = {
    "panel": build_lobby_layout(NASTY, 999, start_at=1, header_url="https://x/h", footer_url="https://x/f"),
    "host": build_host_panel_layout(),
    "submit": build_submit_layout(99),
    "vote": build_vote_layout(
        entry_text="~" * 500, entry_num=99, category="Dealbreaker",
        same_votes=list(range(50)), nope_votes=list(range(50, 99)), total_entries=99, advance_at=1,
    ),
    "recap": build_recap_layout(
        [{"text": "~" * 500, "same_pct": p, "voters": [i], "category": "Fantasy"} for i, p in enumerate((1.0, 0.5, 0.0))],
        payout_line="🪙 +20 to winners • +5 to everyone who played",
    ),
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


def _vote_view(bot, main_view=None):
    return fan_cog.FantasiesVoteView(
        "gid", 1, "beach", 1, "Fantasy", None, bot, "Host", AsyncMock(),
        entry_author_id=9, main_view=main_view,
    )


def test_fantasies_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["fantasies"] == "FantasiesCog"


async def test_games_host_opens_in_either_phase_and_refuses_a_stranger():
    bot = _bot()
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    for view in (fan_cog.FantasiesMainView("gid", 1, None, bot, cog), _vote_view(bot)):
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
        pytest.param("panel", "close_submissions", fan_cog.NO_WINDOW_REPLY, id="close-between-rounds"),
        pytest.param("panel", "next_entry", fan_cog.NO_ENTRY_REPLY, id="next-between-rounds"),
        pytest.param("vote", "start_round", fan_cog.ROUND_RUNNING_REPLY, id="start-during-the-vote"),
        pytest.param("vote", "close_submissions", fan_cog.NO_WINDOW_REPLY, id="close-during-the-vote"),
        pytest.param("orphan-vote", "end_game_button", fan_cog.GAME_OVER_REPLY, id="end-on-a-vote-with-no-panel"),
    ],
)
async def test_a_panel_press_out_of_its_phase_answers_plainly(phase, press, expected):
    bot = _bot()
    main = fan_cog.FantasiesMainView("gid", 1, None, bot, None)
    views = {"panel": main, "vote": _vote_view(bot, main), "orphan-vote": _vote_view(bot)}
    interaction = _interaction(1)
    await getattr(views[phase], press)(interaction)
    assert _said(interaction) == expected


async def test_the_panel_routes_close_and_next_to_the_rounds_live_piece():
    bot = _bot()
    main = fan_cog.FantasiesMainView("gid", 1, None, bot, None)
    sub = fan_cog.SubmitRoundView("gid", 1, 1, None, bot)
    main._active_submit_view = sub
    press = _interaction(1)
    await main.close_submissions(press)
    assert sub._closed and sub.is_finished()
    press.response.defer.assert_awaited_once()

    vote = _vote_view(bot, main)
    vote.message = SimpleNamespace(id=5)
    main._active_vote_view = vote
    press = _interaction(1)
    await main.next_entry(press)
    vote.advance_callback.assert_awaited_once_with(vote.message)

    # A stranger reaching either through the panel is still refused by the
    # piece itself.
    sub2 = fan_cog.SubmitRoundView("gid", 1, 2, None, bot)
    main._active_submit_view = sub2
    stranger = _interaction(2)
    await main.close_submissions(stranger)
    assert _said(stranger).startswith("❌") and not sub2._closed


async def test_end_game_mid_vote_is_handed_to_the_panel_view():
    bot = _bot()
    main = fan_cog.FantasiesMainView("gid", 1, None, bot, None)
    vote = _vote_view(bot, main)
    press = _interaction(1)
    await vote.end_game_button(press)
    assert "Are you sure" in _said(press)


async def test_a_closed_window_refuses_a_stale_submit_press():
    view = fan_cog.SubmitRoundView("gid", 1, 1, None, _bot())
    view._closed = True
    interaction = _interaction(3)
    await view.submit_fantasy(interaction)
    assert "closed" in _said(interaction)
    interaction.response.send_modal.assert_not_awaited()


async def test_a_round_ending_redraws_the_panel_with_the_count():
    bot = _bot()
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    main = fan_cog.FantasiesMainView("gid", 1, None, bot, cog)
    main.round_num = 2
    main.message = SimpleNamespace(edit=AsyncMock())
    bot.active_views["gid"] = main
    await cog._restore_panel("gid", main)
    assert "🔁 Rounds played: **2**" in _text(list(main.children))
    kwargs = main.message.edit.await_args.kwargs
    assert kwargs["view"] is main and kwargs["embed"] is None
