"""Tests for ``bot_modules/games/rushmore/layouts.py`` — the Components V2
cards that replaced ``games_rushmore/embeds.py`` — and the host panel that
took Start Draft off the lobby.

The builders are pure: primitives in, ``discord.ui.Item`` lists out, so
every card is asserted on its text with no Discord mocks. Budget is checked
at the worst case the game can really produce: 25 drafters (``MAX_PLAYERS``,
the dropdown's own ceiling) whose names, picks and topic are all markdown,
which is what doubles under escaping.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.rushmore.cog as rushmore_cog
from bot_modules.core.branding import BACKTICK_STAND_IN
from bot_modules.core.layout import MAX_CHARS, card, check_budget, layout_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.rushmore import layouts
from bot_modules.games.rushmore.layouts import (
    HELP_ID,
    JOIN_ID,
    LEAVE_ID,
    PICK_ID,
    RUN_AGAIN_ID,
    START_ID,
    VOTE_SELECT_ID,
    VOTE_TIMER_ITEM_ID,
    build_draft_layout,
    build_final_boards_pages,
    build_host_panel_layout,
    build_interrupted_layout,
    build_lobby_layout,
    build_recap_layout,
    build_vote_layout,
    build_winner_layout,
    render_draft_board,
)
from bot_modules.games.rushmore.logic import DRAFT_ROUNDS, MAX_PLAYERS, SKIPPED_MARKER

#: A 32-character display name of pure markdown — Discord's longest, and the
#: one escaping doubles.
NASTY_NAME = "*" * 32
NASTY_PICK = "_" * 100
NASTY_TOPIC = "~" * 300


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str]:
    return [
        i.custom_id for i in card(items).walk_children()
        if getattr(i, "custom_id", None)
    ]


def _buttons(items: list[discord.ui.Item[Any]]) -> list[discord.ui.Button]:
    return [i for i in card(items).walk_children() if isinstance(i, discord.ui.Button)]


def _full_game():
    players = [(uid, NASTY_NAME) for uid in range(1, MAX_PLAYERS + 1)]
    boards = {str(uid): [NASTY_PICK] * DRAFT_ROUNDS for uid, _ in players}
    return players, boards


# ── render_draft_board ───────────────────────────────────────────────


def test_render_draft_board_marks_active_player():
    players = [(1, "Alice"), (2, "Bob")]
    boards = {"1": ["A", None, None, None], "2": [None, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=1)
    assert "**Draft Board:**" in out
    # The active player's line has the target emoji marker
    lines = out.split("\n")
    alice_line = next(line for line in lines if "Alice" in line)
    bob_line = next(line for line in lines if "Bob" in line)
    assert "\U0001f3af" in alice_line
    assert "\U0001f3af" not in bob_line


def test_render_draft_board_truncates_long_names():
    players = [(1, "X" * 30)]
    boards = {"1": [None, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=None)
    # Name caps at 16 chars (with the last replaced by ".")
    assert "X" * 30 not in out
    assert "." in out


def test_render_draft_board_truncates_long_picks():
    players = [(1, "Alice")]
    boards = {"1": ["A" * 30, None, None, None]}
    out = render_draft_board(players, boards, 1)
    assert "…" in out
    # The full 30-char string is not rendered
    assert "A" * 30 not in out


def test_render_draft_board_shows_skipped_label():
    players = [(1, "Alice")]
    boards = {"1": [SKIPPED_MARKER, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=None)
    assert "*Skipped*" in out


def test_render_draft_board_shows_dash_for_empty_slots():
    players = [(1, "Alice")]
    boards = {"1": [None, None, None, None]}
    out = render_draft_board(players, boards, None)
    assert "—" in out


def test_render_draft_board_empty_players():
    out = render_draft_board([], {}, None)
    assert "**Draft Board:**" in out


def test_render_draft_board_uses_default_empty_board_for_missing_uid():
    """A player whose uid isn't in boards still renders 4 dash slots."""
    players = [(99, "Ghost")]
    out = render_draft_board(players, {}, active_player_id=None)
    # 4 empty slots = four "—" segments
    assert out.count("—") == 4


def test_render_draft_board_escapes_markdown_in_picks():
    # A drafted pick renders outside the name's code span, on the live
    # public board shown every turn — the same value other builders in
    # this module (final boards, winner, recap) already escape.
    players = [(1, "Alice")]
    boards = {"1": ["*bold*", None, None, None]}
    out = render_draft_board(players, boards, active_player_id=None)
    assert "*bold*" not in out
    assert "\\*bold\\*" in out


def test_render_draft_board_never_orphans_an_escape_at_the_cut():
    """The truncation branch cuts *escaped* text, so it can split a pair.

    ``escape_markdown`` writes every escape as exactly two characters, so a
    fixed-index slice can land between the backslash and the character it
    protects. The orphan then escapes the ellipsis we append, and a stray
    ``\\`` nobody typed appears on the public board. The modal behind this
    field allows 100 raw characters against an 18-character display budget,
    so the truncation branch fires on ordinary picks, not only adversarial
    ones \u2014 this input is 19 raw characters with an underscore at the cut.
    """
    pick = "a" * 14 + "_" + "bbbb"
    out = render_draft_board([(1, "n")], {"1": [pick]}, active_player_id=None)
    assert "\\\u2026" not in out, "a lone backslash reached the board"
    assert out.rstrip().endswith("\u2026")


def test_render_draft_board_neutralizes_backtick_in_name():
    """A backtick in a name must not close the row's code span early.

    Discord renders no markdown inside a `` `code span` ``, so the name
    is not escape_markdown'd — the vulnerability is the delimiter itself.
    A raw backtick in the name would end the span after it and put the
    picks that follow back into markdown-rendering territory.
    """
    players = [(1, "back`tick")]
    boards = {"1": [None, None, None, None]}
    out = render_draft_board(players, boards, active_player_id=None)
    lines = out.split("\n")
    name_line = next(line for line in lines if "back" in line)
    # Only the two span delimiters remain — the member's own backtick was
    # substituted, not doubled or escaped-with-a-backslash (which would
    # print a literal backslash in the monospace cell).
    assert name_line.count("`") == 2
    assert BACKTICK_STAND_IN in name_line
    assert "back`tick" not in name_line




# ── the lobby ────────────────────────────────────────────────────────────────


def test_lobby_names_the_game_the_host_and_the_roster():
    text = _text(build_lobby_layout("Alice", ["Bob", "Carol"], topic=None))
    assert "Mt. Rushmore Draft" in text
    assert "Hosted by **Alice**" in text
    assert "Players (2)" in text
    assert "Bob, Carol" in text


def test_empty_lobby_says_nobody_yet():
    text = _text(build_lobby_layout("Alice", []))
    assert "Players (0)" in text
    assert "Nobody yet" in text


def test_lobby_teases_a_topic_the_host_supplied():
    assert "Mt. Rushmore of **Snacks**" in _text(build_lobby_layout("Alice", [], topic="Snacks"))
    assert "Build your" not in _text(build_lobby_layout("Alice", [], topic=None))


@pytest.mark.parametrize(
    "mode, expected, absent",
    [
        pytest.param("snake", "when it's your turn", "everyone picks at once", id="snake"),
        pytest.param("blitz", "everyone picks at once", "when it's your turn", id="blitz"),
    ],
)
def test_lobby_how_to_follows_the_mode(mode, expected, absent):
    """The rules ride on the card — live games showed nobody presses Help."""
    text = _text(build_lobby_layout("Billy", [], "Snacks", mode=mode)).lower()
    assert "how it works" in text
    assert expected in text
    assert absent not in text


def test_lobby_carries_nothing_host_only():
    """Ben, 2026-09-30 (the Truth or Dare rule of 2026-09-29, extended):
    everything host-only lives on the host panel. Reverting puts Start Draft
    back in front of the room."""
    items = build_lobby_layout("Alice", ["Bob"])
    assert _ids(items) == [HELP_ID, JOIN_ID, LEAVE_ID]
    intro = next(i for i in items if isinstance(i, discord.ui.Section))
    assert intro.accessory.custom_id == HELP_ID


def test_lobby_points_the_host_at_their_controls():
    """A scheduled or Help-started game hands nobody a panel; this line is
    the only way such a host finds Start Draft."""
    assert f"-# {host_panel.HOST_HINT}" in _text(build_lobby_layout("Alice", []))


def test_closed_lobby_drops_join_and_leave():
    assert _ids(build_lobby_layout("Alice", ["Bob"], closed=True)) == [HELP_ID]


def test_lobby_renders_the_start_countdown_only_when_set():
    assert "<t:1700000000:R>" in _text(build_lobby_layout("Alice", [], start_at=1_700_000_000))
    assert "Starting" not in _text(build_lobby_layout("Alice", []))


def test_lobby_escapes_member_text():
    text = _text(build_lobby_layout("*Host*", ["**Bob**"], topic="*Topic*"))
    assert "\\*Host\\*" in text
    assert "\\*\\*Bob\\*\\*" in text
    assert "\\*Topic\\*" in text


def test_lobby_roster_drops_whole_names_and_counts_them():
    """A full adversarial lobby escapes to 1,648 characters; the roster
    keeps whole names and counts the rest — never a cut that could split an
    escape pair."""
    names = [NASTY_NAME] * MAX_PLAYERS
    text = _text(build_lobby_layout("Alice", names))
    roster = text.split("Players (25)\n", 1)[1]
    assert len(roster) <= layouts.ROSTER_BUDGET
    assert "more)" in roster
    shown = roster.split(" (+")[0].split(", ")
    assert all(name == "\\*" * 32 for name in shown)


def test_lobby_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        "Alice", [], header_url="https://x/h.webp", footer_url="https://x/f.webp",
        title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)
    # No header: no art at all, and the heading stays.
    plain = build_lobby_layout("Alice", [], footer_url="https://x/f.webp")
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in plain)
    assert f"## {layouts.ICON} {layouts.TITLE}" in _text(plain)


def test_host_panel_is_start_draft_on_its_own_id():
    items = build_host_panel_layout()
    assert _ids(items) == [START_ID]
    assert START_ID != "rushmore_start", "the panel is not the old lobby's persistent button"
    assert host_panel.HOST_COMMAND in _text(items)


# ── the draft board ──────────────────────────────────────────────────────────


def test_draft_board_renders_round_topic_and_the_picker():
    items = build_draft_layout(
        "Snacks", [(1, "Alice")], {"1": ["Chips", None, None, None]},
        active_player_id=1, active_player_name="Alice", round_num=2, deadline=1_700_000_000,
    )
    text = _text(items)
    assert "Mt. Rushmore of: Snacks" in text
    assert "Round 2/4" in text
    assert "<t:1700000000:R>" in text
    assert "**Alice**'s turn!" in text
    assert "Chips" in text
    assert _ids(items) == [PICK_ID]


def test_draft_board_has_no_picker_line_without_an_active_player():
    text = _text(build_draft_layout("T", [(1, "A")], {}, None, None, 1, 1_700_000_000))
    assert "turn!" not in text


def test_draft_board_deadline_is_whatever_the_caller_holds():
    """Regression (todo #208/#209): the builder used to recompute the
    deadline per render, so a blitz pick redrawing the board mid-round reset
    the visible countdown. It now only ever prints the value it is given."""
    kwargs = dict(
        topic="T", players=[(1, "A")], boards={}, active_player_id=None,
        active_player_name=None, round_num=1,
    )
    first = _text(build_draft_layout(**kwargs, deadline=111))
    again = _text(build_draft_layout(**kwargs, deadline=111))
    later = _text(build_draft_layout(**kwargs, deadline=222))
    assert first == again
    assert "<t:111:R>" in first and "<t:222:R>" in later


def test_closed_draft_board_drops_the_countdown_and_the_button():
    items = build_draft_layout("T", [(1, "A")], {}, None, None, 4, 111, closed=True)
    assert _ids(items) == []
    assert "<t:" not in _text(items)
    assert "Draft complete" in _text(items)


# ── final boards ─────────────────────────────────────────────────────────────


def test_final_boards_lists_every_players_four_lines():
    pages = build_final_boards_pages(
        "Topic", [(1, "Alice"), (2, "Bob")],
        {"1": ["A1", "A2", SKIPPED_MARKER, None], "2": ["B1", "B2", "B3", "B4"]},
    )
    assert len(pages) == 1
    text = _text(pages[0])
    assert "Final Boards" in text
    assert "**Alice**\n1. A1\n2. A2\n3. *Skipped*\n4. —" in text
    assert "**Bob**\n1. B1\n2. B2\n3. B3\n4. B4" in text


def test_final_boards_handles_a_missing_board():
    text = _text(build_final_boards_pages("Topic", [(1, "Alice")], {})[0])
    assert text.count("—") >= DRAFT_ROUNDS


def test_final_boards_with_nobody_says_so():
    assert "Nobody finished a board" in _text(build_final_boards_pages("Topic", [], {})[0])


def test_full_adversarial_final_boards_page_and_lose_nothing():
    """25 drafters of four 100-character markdown picks run past 20,000
    characters escaped. The embed had no cap at all; the cards page, every
    page inside the budget and every board on exactly one of them."""
    players, boards = _full_game()
    pages = build_final_boards_pages(NASTY_TOPIC, players, boards)
    assert len(pages) > 1
    for page in pages:
        check_budget(card(page))
    body = "\n".join(_text(p) for p in pages)
    assert body.count("\\*" * 32) == MAX_PLAYERS
    assert f"(1/{len(pages)})" in _text(pages[0])


# ── the vote, the winner ─────────────────────────────────────────────────────


def test_vote_card_says_spectators_may_vote_and_counts_down():
    items = build_vote_layout("Snacks", 1_700_000_000, [("Alice", "1"), ("Bob", "2")])
    text = _text(items)
    assert "Vote" in text and "Snacks" in text
    assert "Anyone in the channel can vote" in text
    timer = card(items).find_item(VOTE_TIMER_ITEM_ID)
    assert isinstance(timer, discord.ui.TextDisplay)
    assert "<t:1700000000:R>" in timer.content


def test_vote_dropdown_never_exceeds_discords_25_options():
    items = build_vote_layout("T", 1, [(f"P{i}", str(i)) for i in range(40)])
    select = next(i for i in card(items).walk_children() if isinstance(i, discord.ui.Select))
    assert select.custom_id == VOTE_SELECT_ID
    assert len(select.options) == 25


@pytest.mark.parametrize(
    "votes, label", [pytest.param(3, "3 votes", id="plural"), pytest.param(1, "1 vote", id="singular")],
)
def test_winner_card_names_the_winner_and_the_board(votes, label):
    text = _text(build_winner_layout(
        "Snacks", ["Alice"], votes, [["Chips", "Dip", SKIPPED_MARKER, None]],
        [("Alice", votes), ("Bob", 0)],
    ))
    assert f"\U0001f3c6 **Alice** wins! — {label}" in text
    assert "1. Chips\n2. Dip\n3. *Skipped*\n4. —" in text
    assert "### Full Results" in text
    assert "**Bob** — 0 votes" in text


def test_winner_card_stacks_tied_boards():
    text = _text(build_winner_layout(
        "T", ["Alice", "Bob"], 2, [["A1", "A2", "A3", "A4"], ["B1", "B2", "B3", "B4"]],
        [("Alice", 2), ("Bob", 2)],
    ))
    assert "**Alice & Bob** wins!" in text
    assert "A1" in text and "B1" in text


def test_winner_card_with_no_results_renders_a_dash():
    text = _text(build_winner_layout("T", ["Alice"], 0, [[]], []))
    assert text.rstrip().endswith("—")


# ── the recap ────────────────────────────────────────────────────────────────


def test_recap_summary_has_topic_players_duration_and_winner():
    text = _text(build_recap_layout(
        "Snacks", 4, 125.0, ["Alice"], 3, [["Chips", "Dip", "Salsa", "Nuts"]], {},
    ))
    assert "Game Over" in text
    assert "Topic: **Snacks**" in text
    assert "Players: **4**" in text
    assert "**2m 5s**" in text
    assert "**Alice** wins! — 3 votes" in text
    assert "1. Chips" in text


def test_recap_without_a_winner_says_so():
    """A game nobody voted in used to crown an empty name."""
    text = _text(build_recap_layout("T", 3, 10.0, [], 0, [], {}))
    assert "Nobody won this one." in text
    assert "wins!" not in text


def test_recap_renders_every_stat():
    stats = {
        "first_pick": {"pick": "Chips", "player": "Alice"},
        "skipped_count": 2, "skipped_names": ["Bob"],
        "fastest": {"pick": "Dip", "player": "Alice", "time": 1.5},
        "slowest": {"pick": "Nuts", "player": "Bob", "time": 40.0},
        "vote_split": 3,
    }
    text = _text(build_recap_layout("T", 3, 10.0, ["Alice"], 2, [[]], stats))
    assert "Draft Stats" in text
    assert "First Overall Pick: **Chips** (Alice, Round 1)" in text
    assert "Skipped Picks: **2** (Bob)" in text
    assert "Fastest Pick: **Dip** by Alice (1.5s)" in text
    assert "Slowest Pick: **Nuts** by Bob (40.0s)" in text
    assert "3-way split" in text


def test_recap_unanimous_overrides_the_vote_split():
    text = _text(build_recap_layout(
        "T", 3, 10.0, ["Alice"], 2, [[]], {"unanimous": True, "vote_split": 2},
    ))
    assert "Unanimous Vote: Yes — everyone voted for **Alice**" in text
    assert "split" not in text


def test_recap_omits_draft_stats_when_there_are_none():
    assert "Draft Stats" not in _text(build_recap_layout("T", 3, 10.0, ["A"], 1, [[]], {}))


def test_recap_carries_the_payout_line_and_run_again():
    items = build_recap_layout(
        "T", 3, 10.0, ["A"], 1, [[]], {}, payout_line="🪙 +20 to winners",
    )
    assert "-# 🪙 +20 to winners" in _text(items)
    assert _ids(items) == [RUN_AGAIN_ID]
    assert "-# " not in _text(build_recap_layout("T", 3, 10.0, ["A"], 1, [[]], {}))


# ── every card: budget, glyph-led heading, grey buttons ──────────────────────


def _every_card() -> dict[str, list[discord.ui.Item[Any]]]:
    players, boards = _full_game()
    names = [n for _, n in players]
    winner_boards = [boards[str(uid)] for uid, _ in players]
    stats = {
        "first_pick": {"pick": NASTY_PICK, "player": NASTY_NAME},
        "skipped_count": 100, "skipped_names": names,
        "fastest": {"pick": NASTY_PICK, "player": NASTY_NAME, "time": 1.0},
        "slowest": {"pick": NASTY_PICK, "player": NASTY_NAME, "time": 2.0},
        "unanimous": True,
    }
    cards = {
        "lobby": build_lobby_layout(
            NASTY_NAME, names, NASTY_TOPIC, mode="blitz", start_at=1,
            header_url="https://x/h.webp", footer_url="https://x/f.webp",
        ),
        "lobby-closed": build_lobby_layout(NASTY_NAME, names, NASTY_TOPIC, closed=True),
        "host-panel": build_host_panel_layout(),
        "draft": build_draft_layout(NASTY_TOPIC, players, boards, 1, NASTY_NAME, 4, 1),
        "draft-closed": build_draft_layout(NASTY_TOPIC, players, boards, None, None, 4, 1, closed=True),
        "vote": build_vote_layout(NASTY_TOPIC, 1, [(n, str(u)) for u, n in players]),
        # A 25-way tie: every drafter a winner, every board stacked.
        "winner": build_winner_layout(
            NASTY_TOPIC, names, 1, winner_boards, [(n, 1) for n in names],
        ),
        "recap": build_recap_layout(
            NASTY_TOPIC, MAX_PLAYERS, 3599.0, names, 1, winner_boards, stats,
            payout_line="🪙 +20 to winners • +5 to everyone who played",
        ),
        "interrupted": build_interrupted_layout(),
    }
    for number, page in enumerate(build_final_boards_pages(NASTY_TOPIC, players, boards)):
        cards[f"final-boards-{number}"] = page
    return cards


CARDS = _every_card()
every_card = pytest.mark.parametrize("name", list(CARDS))


@every_card
def test_every_card_fits_the_budget_at_its_worst_case(name):
    view = card(CARDS[name])
    check_budget(view)
    assert layout_budget(view)[1] <= MAX_CHARS


@every_card
def test_every_card_leads_with_a_glyph_led_heading(name):
    """Guide § Anatomy: every card, including a one-line notice. The lobby
    may hand its heading to a titled strip, so it is checked untitled."""
    first = next(
        i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.TextDisplay)
    )
    assert first.content.startswith(f"## {layouts.ICON} "), first.content[:40]


@every_card
def test_buttons_are_grey(name):
    """Guide § Anatomy: colour on a button is a meaning. Nothing in this game
    is a decision or a destructive press, so nothing is coloured — the embed
    era's green Join, blurple Start Draft and Run Again ranked buttons that
    aren't ranked."""
    assert {b.style for b in _buttons(CARDS[name])} <= {discord.ButtonStyle.secondary}


@every_card
def test_no_card_mentions_a_member(name):
    """Members are named, never ``<@id>``-mentioned, inside a card; the turn
    pings are separate plain messages with their own allow-list."""
    assert "<@" not in _text(CARDS[name])


# ── the host panel: Start Draft, off the lobby ───────────────────────────────


def _bot():
    return SimpleNamespace(active_views={}, games_db=None)


def _join_view(bot, *, players=(1, 2, 3)):
    view = rushmore_cog.RushmoreJoinView(
        "gid", 1, "Host", "Snacks", "host", None, bot, SimpleNamespace(),
        players=list(players),
    )
    bot.active_views["gid"] = view
    return view


def _panel(bot):
    return host_panel.GameHostPanel(
        bot, "gid", layouts.build_host_panel_layout(),
        {layouts.START_ID: "start_draft"},
        view_type=(
            rushmore_cog.RushmoreJoinView, rushmore_cog.RushmoreDraftView,
            rushmore_cog.RushmoreVoteView,
        ),
    )


async def _press_start(panel, interaction) -> None:
    button = next(i for i in panel.walk_children() if getattr(i, "custom_id", None) == layouts.START_ID)
    await button.callback(interaction)


def _interaction(user_id=1, *, done=False):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="U", roles=[]),
        guild=None, guild_id=1, channel=SimpleNamespace(id=5, name="games"),
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(),
            is_done=lambda: done,
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_rushmore_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["rushmore"] == "RushmoreCog"
    assert hasattr(rushmore_cog.RushmoreCog, "open_host_panel")


async def test_games_host_sends_the_panel_to_the_host():
    bot = _bot()
    _join_view(bot)
    cog = rushmore_cog.RushmoreCog(bot)  # type: ignore[arg-type]
    interaction = _interaction()

    await cog.open_host_panel(interaction, "gid")  # type: ignore[arg-type]

    panel = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(panel, host_panel.GameHostPanel)
    assert panel.game_id == "gid"
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert panel.timeout == host_panel.HOST_PANEL_TIMEOUT < 15 * 60


@pytest.mark.parametrize(
    "user_id, setup, expected",
    [
        pytest.param(2, "lobby", host_panel.NOT_HOST_REPLY, id="not-the-host"),
        pytest.param(1, "gone", host_panel.GAME_ENDED_REPLY, id="game-ended"),
        pytest.param(1, "started", rushmore_cog.DRAFT_RUNNING_REPLY, id="draft-under-way"),
        pytest.param(1, "drafting", rushmore_cog.DRAFT_RUNNING_REPLY, id="draft-view-live"),
        pytest.param(2, "voting", rushmore_cog.DRAFT_RUNNING_REPLY, id="vote-view-has-no-host"),
    ],
)
async def test_games_host_refusals(user_id, setup, expected):
    bot = _bot()
    view = _join_view(bot)
    if setup == "gone":
        bot.active_views.clear()
    elif setup == "started":
        view._closed = True
    elif setup == "drafting":
        bot.active_views["gid"] = SimpleNamespace(host_id=1)
    elif setup == "voting":
        bot.active_views["gid"] = rushmore_cog.RushmoreVoteView(
            "gid", "Topic", [1, 2], 1, None, None, None,
        )
    cog = rushmore_cog.RushmoreCog(bot)  # type: ignore[arg-type]
    interaction = _interaction(user_id)

    await cog.open_host_panel(interaction, "gid")  # type: ignore[arg-type]

    assert _said(interaction) == expected
    assert "view" not in interaction.response.send_message.await_args.kwargs


async def test_a_mod_gets_the_panel_too(monkeypatch):
    """The gate is ``is_host_or_mod`` — whoever it passes gets the panel."""
    monkeypatch.setattr(rushmore_cog, "is_host_or_mod", lambda interaction, host_id: True)
    bot = _bot()
    _join_view(bot)
    cog = rushmore_cog.RushmoreCog(bot)  # type: ignore[arg-type]
    interaction = _interaction(99)
    await cog.open_host_panel(interaction, "gid")  # type: ignore[arg-type]
    assert "view" in interaction.response.send_message.await_args.kwargs


async def test_panel_press_hands_off_to_the_lobbys_own_gate(monkeypatch):
    """The panel holds no authority: Start Draft re-checks host-or-mod."""
    bot = _bot()
    _join_view(bot)
    panel = _panel(bot)
    stranger = _interaction(2)

    await _press_start(panel, stranger)

    assert _said(stranger) == "❌ Only the host or a mod can start."


@pytest.mark.parametrize(
    "phase, expected",
    [
        pytest.param("closed-lobby", rushmore_cog.DRAFT_RUNNING_REPLY, id="draft-starting"),
        pytest.param("drafting", rushmore_cog.DRAFT_RUNNING_REPLY, id="draft-under-way"),
        pytest.param("voting", rushmore_cog.DRAFT_RUNNING_REPLY, id="vote-on"),
        pytest.param("gone", host_panel.GAME_ENDED_REPLY, id="game-over"),
    ],
)
async def test_a_stale_panel_press_never_acts_on_a_dead_lobby(phase, expected):
    bot = _bot()
    view = _join_view(bot)
    panel = _panel(bot)
    if phase == "closed-lobby":
        view._closed = True
    elif phase == "drafting":
        bot.active_views["gid"] = rushmore_cog.RushmoreDraftView(
            "gid", 1, "Host", "Topic", [1, 2, 3], 60, None, None, bot, None,
        )
    elif phase == "voting":
        bot.active_views["gid"] = rushmore_cog.RushmoreVoteView(
            "gid", "Topic", [1, 2], 1, None, None, None,
        )
    else:
        bot.active_views.clear()
    interaction = _interaction()

    await _press_start(panel, interaction)

    assert _said(interaction) == expected


async def test_start_draft_refuses_below_the_player_floor(monkeypatch):
    monkeypatch.setattr(
        rushmore_cog, "get_game_payload", AsyncMock(return_value={"settings": {"min_players": 3}}),
    )
    bot = _bot()
    view = _join_view(bot, players=(1, 2))
    interaction = _interaction()

    await view.start_draft(interaction)  # type: ignore[arg-type]

    assert "Need at least 3 players" in _said(interaction)
    assert view._closed is False


async def test_start_draft_closes_the_lobby_and_runs_once(monkeypatch):
    """Two panels can both reach Start Draft; the first owns the start."""
    monkeypatch.setattr(
        rushmore_cog, "get_game_payload", AsyncMock(return_value={"settings": {}}),
    )
    bot = _bot()
    start = AsyncMock()
    view = _join_view(bot)
    view.cog = SimpleNamespace(_start_draft=start, _get_settings=AsyncMock(return_value={}))
    view._msg = SimpleNamespace(edit=AsyncMock(), channel="chan", guild=None)

    await view.start_draft(_interaction())  # type: ignore[arg-type]
    await view.start_draft(_interaction())  # type: ignore[arg-type]

    start.assert_awaited_once()
    assert start.await_args.kwargs["players"] == [1, 2, 3]
    assert start.await_args.kwargs["channel"] == "chan"
    edited = view._msg.edit.await_args.kwargs
    assert edited["view"] is view and edited["embed"] is None and edited["content"] is None
    assert [
        i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)
    ] == [HELP_ID]


async def test_lobby_redraw_clears_an_embed_era_message(monkeypatch):
    """A lobby posted as an embed before this conversion survives a restart;
    its first Join has to clear the embed or Discord rejects the V2 edit."""
    monkeypatch.setattr(
        rushmore_cog, "modify_payload", AsyncMock(return_value={"players": [1, 2, 3, 7]}),
    )
    bot = _bot()
    view = _join_view(bot)
    interaction = _interaction(7)
    interaction.response.edit_message = AsyncMock()

    await view.join(interaction)  # type: ignore[arg-type]

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs == {"view": view, "content": None, "embed": None, "attachments": []}
    assert view.players == [1, 2, 3, 7]
    assert "Players (4)" in "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def test_an_uncached_drafter_reads_user_id_never_a_bare_number():
    """Guide § Mentions and names: the resolver's miss is ``User <id>``."""
    view = rushmore_cog.RushmoreDraftView(
        "gid", 1, "Host", "Topic", [7], 30, None, None, None, None, mode="snake",
    )
    text = "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )
    assert "User 7" in text
    assert "`7" not in text


def test_draft_view_is_a_live_board_layout():
    """What ``game_board_sticky_service`` needs of a V2 board."""
    view = rushmore_cog.RushmoreDraftView(
        "gid", 1, "Host", "Topic", [1, 2, 3], 30, None, None, None, None, mode="blitz",
    )
    assert isinstance(view, discord.ui.LayoutView)
    assert view._panel is None and view._board_retired is False
    assert [getattr(i, "custom_id", None) for i in view.walk_children()].count(PICK_ID) == 1
    view._closed = True
    view._rebuild()
    assert PICK_ID not in [getattr(i, "custom_id", None) for i in view.walk_children()]


async def test_vote_refuses_a_self_vote_and_counts_a_change():
    view = rushmore_cog.RushmoreVoteView("gid", "Topic", [1, 2], 1, None, None, None)
    own = _interaction(1)
    own.data = {"values": ["1"]}
    await view._on_vote(own)  # type: ignore[arg-type]
    assert _said(own) == "❌ You can't vote for yourself!"
    assert view.votes == {}

    first = _interaction(1)
    first.data = {"values": ["2"]}
    await view._on_vote(first)  # type: ignore[arg-type]
    again = _interaction(1)
    again.data = {"values": ["2"]}
    await view._on_vote(again)  # type: ignore[arg-type]
    assert view.votes == {1: 2}
    assert "(changed)" in _said(again)
    assert view.all_voted() is False


async def test_a_vote_press_after_the_tally_is_answered_plainly_and_not_counted():
    """The vote is closed *before* the winner card replaces the dropdown, but
    the view keeps listening until then: a press in that gap used to get
    "This interaction failed"."""
    view = rushmore_cog.RushmoreVoteView("gid", "Topic", [1, 2], 1, None, None, None)
    view._closed = True
    late = _interaction(1)
    late.data = {"values": ["2"]}
    await view._on_vote(late)  # type: ignore[arg-type]
    assert _said(late) == "The vote is over."
    assert view.votes == {}


def test_the_vote_view_stops_only_after_the_winner_card_is_up():
    import inspect  # noqa: PLC0415

    src = inspect.getsource(rushmore_cog.RushmoreCog)
    closed = src.index("vote_view._closed = True")
    edited = src.index("await vote_msg.edit(view=winner_card)")
    assert closed < edited < src.index("vote_view.stop()")
