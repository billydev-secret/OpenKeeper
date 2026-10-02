"""Tests for ``bot_modules/games/ttl/layouts.py`` — the Components V2 cards
that replaced ``games_ttl/embeds.py`` — and the host panel that took Start
Guessing and Next off the boards.

Budget is checked at the worst case the game can produce: a 300-name
roster and two 300-name voter lists of markdown names, 200-character
statements doubled by escaping, and a long prompt.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.ttl.cog as ttl_cog
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.ttl import layouts
from bot_modules.games.ttl.layouts import (
    HELP_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    JOIN_ID,
    SUBMIT_ID,
    VOTE_IDS,
    build_guess_layout,
    build_host_panel_layout,
    build_lobby_layout,
    build_recap_layout,
    build_reveal_layout,
    winner_ids,
)

NASTY = "*" * 32
STATS = {
    "best_liar": ["1"], "most_fooled_count": 3,
    "most_honest": ["2"], "least_fooled_count": 0,
    "best_guesser": ["9", "1"], "max_correct": 4,
}


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


def test_lobby_shows_the_prompt_the_roster_and_nothing_host_only():
    items = build_lobby_layout("best *worst* date", ["Bo", "*Cy*"])
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Two Truths and a Lie\n**Prompt:** best \\*worst\\* date\n")
    assert layouts.LOBBY_DESCRIPTION in text
    assert "### Players (2)\nBo, \\*Cy\\*" in text
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [HELP_ID, SUBMIT_ID]


def test_lobby_without_a_prompt_or_players():
    text = _text(build_lobby_layout(None, []))
    assert "Prompt" not in text and "### Players (0)\n-# Nobody yet" in text


def test_closed_lobby_drops_submit_and_points_at_join_edit():
    items = build_lobby_layout(None, ["Bo"], closed=True)
    assert _ids(items) == []  # stopped view: a Help here could only fail (ship review, 2026-10-01)
    assert layouts.GUESSING_UNDERWAY in _text(items)
    assert host_panel.HOST_HINT not in _text(items)


def test_lobby_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        None, [], header_url="https://x/h.webp", footer_url="https://x/f.webp", title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)


def test_host_panel_carries_start_and_next_all_grey():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_START_ID, HOST_NEXT_ID]
    buttons = [b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    assert {b.custom_id for b in buttons}.isdisjoint({"ttl_start", "ttl_next"})
    assert host_panel.HOST_COMMAND in _text(items)


# ── the guess card ───────────────────────────────────────────────────────────


def test_guess_card_shows_the_subject_prompt_statements_meters_and_votes():
    items = build_guess_layout("_Alice_", ["*s1*", "s2", "s3"], {1: 0, 2: 0, 3: 1}, prompt="Monday trauma?")
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Guess the Lie — \\_Alice\\_'s Turn\n**Prompt:** Monday trauma?")
    assert '1️⃣' in text and '"\\*s1\\*"' in text
    assert "67% (2)" in text and "33% (1)" in text and "0% (0)" in text
    assert _ids(items) == [HELP_ID, *VOTE_IDS, JOIN_ID]
    assert f"-# {host_panel.HOST_HINT}" in text


def test_guess_card_without_a_prompt_has_no_prompt_line():
    assert "Prompt" not in _text(build_guess_layout("A", ["a", "b", "c"], {}))


@pytest.mark.parametrize(
    ("advance_at", "closed", "shown"),
    [
        pytest.param(1_700_000_000, False, True, id="timed-open"),
        pytest.param(1_700_000_000, True, False, id="timed-closed"),
        pytest.param(None, False, False, id="host-paced"),
    ],
)
def test_guess_card_shows_the_countdown_only_while_timed_and_open(advance_at, closed, shown):
    text = _text(build_guess_layout("A", ["a", "b", "c"], {}, closed=closed, advance_at=advance_at))
    assert ("⏱️ Auto-advances <t:1700000000:R>" in text) is shown


def test_closed_guess_card_is_the_reveal_frame_with_no_controls():
    items = build_guess_layout("_Alice_", ["a", "b", "c"], {1: 2}, closed=True)
    assert _ids(items) == []
    assert _text(items).startswith(f"## {layouts.ICON} Reveal — \\_Alice\\_")


def test_every_vote_button_is_grey():
    buttons = [b for b in card(build_guess_layout("A", ["a", "b", "c"], {})).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    assert [b.label for b in buttons if b.custom_id in VOTE_IDS] == list(layouts.NUM_GLYPHS)


# ── the reveal ───────────────────────────────────────────────────────────────


def test_reveal_names_the_lie_and_escapes_the_voters():
    text = _text(build_reveal_layout("_Subj_", ["t1", "t2", "LIE!"], 2, ["**Mod**", "Bo"], ["Cy"]))
    assert text.startswith(f"## {layouts.ICON} Reveal — \\_Subj\\_\nThe lie was 3️⃣: \"LIE!\" ✅")
    assert "### 🎯 Correct (2)\n\\*\\*Mod\\*\\*, Bo" in text
    assert "### ❌ Fooled (1)\nCy" in text


def test_reveal_with_no_voters_shows_a_dash():
    text = _text(build_reveal_layout("S", ["a", "b", "c"], 0, [], []))
    assert "### 🎯 Correct (0)\n—" in text and "### ❌ Fooled (0)\n—" in text


def test_reveal_drops_overflow_voters_whole():
    text = _text(build_reveal_layout("S", ["a", "b", "c"], 0, [f"v{i}" + "_" * 30 for i in range(300)], []))
    assert "more" in text and "\\_\\_\\_" in text
    assert not any(line.endswith("\\") for line in text.splitlines())


# ── the recap ────────────────────────────────────────────────────────────────


def test_winner_ids_are_every_winner_once_in_card_order():
    assert winner_ids(STATS) == [1, 2, 9]
    assert winner_ids({"best_liar": ["x"], "best_guesser": ["3"]}) == [3]


def test_recap_pings_members_names_the_rest_and_never_both():
    name_fn = {1: "Alice", 2: "Bob"}.get
    items = build_recap_layout(
        STATS, name_fn=lambda uid: name_fn(uid) or f"<@{uid}>", pingable=[1],
        payout_line="🪙 +5 to everyone",
    )
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Two Truths and a Lie — Game Over")
    assert "🤥 **Best Liar** — <@1> (3 fooled)" in text
    assert "🪞 **Open Book** — Bob (fooled no one)" in text
    # A winner nobody can name any more reads User <id>, never a bare number
    # or a <@id> the reader's client may not resolve.
    assert "🎯 **Best Guesser** — User 9, <@1> (4 correct)" in text
    assert "<@9>" not in text and "<@2>" not in text
    assert text.endswith("-# 🪙 +5 to everyone")
    assert _ids(items) == []


def test_recap_open_book_counts_the_fooled():
    text = _text(build_recap_layout({**STATS, "least_fooled_count": 1}, name_fn=lambda u: f"U{u}"))
    assert "🪞 **Open Book** — U2 (fooled only 1)" in text


def test_recap_with_no_awards_says_so():
    text = _text(build_recap_layout({}, name_fn=str))
    assert "no awards this time" in text and "Best Liar" not in text


# ── every card: budget at the worst case, glyph heading ──────────────────────


CARDS = {
    "lobby": build_lobby_layout("~" * 1000, [NASTY] * 300, header_url="https://x/h", footer_url="https://x/f"),
    "panel": build_host_panel_layout(),
    "guess": build_guess_layout(NASTY, ["~" * 200] * 3, {i: i % 3 for i in range(50)}, prompt="~" * 1000, advance_at=1),
    "reveal": build_reveal_layout(NASTY, ["~" * 200] * 3, 1, [NASTY] * 300, [NASTY] * 300),
    "recap": build_recap_layout(
        {"best_liar": [str(i) for i in range(50)], "most_fooled_count": 9,
         "most_honest": [str(i) for i in range(50)], "least_fooled_count": 1,
         "best_guesser": [str(i) for i in range(50)], "max_correct": 9},
        name_fn=lambda uid: NASTY, payout_line="🪙 +20 to winners • +5 to everyone who played",
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


def _guess_view(bot):
    return ttl_cog.TTLGuessView("gid", 1, 9, ["a", "b", "c"], 1, None, bot, "Host", AsyncMock(), subject_name="Subj")


def test_two_truths_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["ttl"] == "TTLCog"


async def test_games_host_opens_in_either_phase_and_refuses_a_stranger():
    bot = _bot()
    cog = ttl_cog.TTLCog(bot)  # type: ignore[arg-type]
    for view in (ttl_cog.TTLSubmitView("gid", 1, None, bot, cog), _guess_view(bot)):
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
        pytest.param("lobby", "next_round", ttl_cog.NOT_GUESSING_REPLY, id="next-in-the-lobby"),
        pytest.param("guess", "start_guessing", ttl_cog.ALREADY_GUESSING_REPLY, id="start-during-guessing"),
    ],
)
async def test_a_panel_press_out_of_its_phase_answers_plainly(phase, press, expected):
    bot = _bot()
    views = {"lobby": ttl_cog.TTLSubmitView("gid", 1, None, bot, None), "guess": _guess_view(bot)}
    interaction = _interaction(1)
    await getattr(views[phase], press)(interaction)
    assert _said(interaction) == expected


async def test_next_from_the_panel_advances_the_board_not_the_panel():
    view = _guess_view(_bot())
    view.message = SimpleNamespace(id=5)
    stranger = _interaction(2)
    await view.next_round(stranger)
    assert _said(stranger).startswith("❌")
    host = _interaction(1)
    await view.next_round(host)
    view.advance_callback.assert_awaited_once_with(view.message)


async def test_a_submission_landing_redraws_the_lobby_roster_from_the_payload():
    view = ttl_cog.TTLSubmitView("gid", 1, None, _bot(), None)
    view.message = SimpleNamespace(edit=AsyncMock())
    await view.show_players(["Bo", "Cy"])
    assert "### Players (2)\nBo, Cy" in _text(list(view.children))
    kwargs = view.message.edit.await_args.kwargs
    assert kwargs["view"] is view and kwargs["embed"] is None and kwargs["content"] is None


async def test_a_closed_lobby_refuses_a_stale_submit_press():
    view = ttl_cog.TTLSubmitView("gid", 1, None, _bot(), None)
    view._closed = True
    interaction = _interaction(3)
    await view.submit(interaction)
    assert _said(interaction) == ttl_cog.ALREADY_GUESSING_REPLY
    interaction.response.send_modal.assert_not_awaited()
