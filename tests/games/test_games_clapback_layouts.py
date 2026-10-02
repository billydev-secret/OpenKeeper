"""Tests for ``bot_modules/games/clapback/layouts.py`` — the Components V2
cards that replaced ``games_clapback/embeds.py``.

The builders are pure: they take dicts and primitives and return
``discord.ui.Item`` lists, so every card is asserted on its text with no
Discord mocks. Budget and the no-Container rule are checked per card.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.games.constants import GAME_ICONS
from bot_modules.games.clapback import layouts
from bot_modules.games.clapback.layouts import (
    build_lobby_layout,
    build_recap_layout,
    build_reveal_layout,
    build_scoreboard_layout,
    build_submit_layout,
    build_vote_layout,
)
from bot_modules.games.clapback.logic import THREE_PLAYER_NOTE

ICON = GAME_ICONS["clapback"]


def _named(uid: int) -> str:
    return f"User{uid}"


def _view(items: list[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in _view(items).walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str | None]:
    return [
        b.custom_id for b in _view(items).walk_children() if isinstance(b, discord.ui.Button)
    ]


def _tie() -> dict[str, Any]:
    return {"winner": None, "scores": {10: 50, 20: 50}, "clapback": False,
            "vote_counts": {10: 1, 20: 1}}


def _clapback(winner: int = 10) -> dict[str, Any]:
    loser = 20 if winner == 10 else 10
    return {"winner": winner, "scores": {winner: 125, loser: 0}, "clapback": True,
            "vote_counts": {winner: 3, loser: 0}}


def _regular() -> dict[str, Any]:
    return {"winner": 10, "scores": {10: 67, 20: 33}, "clapback": False,
            "vote_counts": {10: 2, 20: 1}}


def _reveal(result, *, anonymous=False, prompt=None, answers=None):
    return build_reveal_layout(
        result=result, answers=answers or {"10": "answer A", "20": "answer B"},
        player_a=10, player_b=20, anonymous=anonymous, name_fn=_named, prompt=prompt,
    )


_HISTORY = [{
    "round": 1,
    "matchups": [
        {"player_a": 10, "answer_a": "best*!", "votes_a": 3,
         "player_b": 20, "answer_b": "ok", "votes_b": 0, "clapback": True},
    ],
}]


def _recap(**payload_overrides):
    payload = {
        "scores": {"10": 250, "20": 100, "30": 50},
        "clapbacks": {"10": 2, "20": 0, "30": 0},
        "round_history": [], "players": [10, 20, 30],
    }
    payload.update(payload_overrides)
    return payload


# ── every card: budget, bare (no Container), glyph-led title ───────────────


_CARDS = [
    pytest.param(lambda: build_lobby_layout("Alice", {"rounds": 5}, list(range(1, 13)), _named,
                                            start_at=1, notice="⌛ timed out"), id="lobby"),
    pytest.param(lambda: build_submit_layout("p", 1, 5, "<t:1:R>", 0, 3, bye_player=7,
                                             name_fn=_named), id="submit"),
    pytest.param(lambda: build_vote_layout("a" * 500, "b" * 500, 1, 0, 2, "<t:1:R>",
                                           prompt="p" * 200), id="vote"),
    pytest.param(lambda: _reveal(_tie()), id="reveal-tie"),
    pytest.param(lambda: _reveal(_clapback()), id="reveal-clapback"),
    pytest.param(lambda: _reveal(_regular()), id="reveal-regular"),
    pytest.param(lambda: build_scoreboard_layout(
        {"scores": {str(i): i for i in range(1, 21)}}, 1, 5, bye_players=[30, 31],
        bye_award=40, name_fn=_named, with_next=True), id="scoreboard"),
    pytest.param(lambda: build_recap_layout(
        _recap(round_history=_HISTORY), {"anonymous": False}, _named,
        payout_line="💰 Winner paid 100 coins"), id="recap"),
]


@pytest.mark.parametrize("build", _CARDS)
def test_every_card_is_bare_titled_and_in_budget(build):
    items = build()
    view = _view(items)
    check_budget(view)
    assert not any(isinstance(i, discord.ui.Container) for i in view.walk_children())
    first = items[0]
    assert isinstance(first, discord.ui.TextDisplay)
    assert first.content.startswith(f"## {ICON} ")


def test_custom_ids_are_frozen():
    # Posted lobbies and restart recovery route by these; renaming one strands
    # every card already in a channel.
    assert _ids(build_lobby_layout("A", {"rounds": 1}, [])) == [
        "ql_join", "ql_leave", "ql_start", "ql_htp", "ql_cancel",
    ]
    assert _ids(build_submit_layout("p", 1, 1, "t", 0, 3)) == [
        "ql_submit", "ql_join_midgame", "ql_close_answers",
    ]
    assert _ids(build_vote_layout("a", "b", 1, 0, 1, "t")) == ["ql_vote_a", "ql_vote_b"]
    assert _ids(build_scoreboard_layout({"scores": {}}, 1, 2, with_next=True)) == ["ql_next"]
    assert _ids(build_recap_layout(_recap(), {}, _named)) == [
        "ql_replay", "ql_replay_shuffle",
    ]


# ── lobby ──────────────────────────────────────────────────────────────────


def test_lobby_countdown_promises_an_auto_start():
    # The countdown is not advertising: the sweep starts the game itself
    # (clapback-8), and the line says so — and names the floor it waits on.
    text = _text(build_lobby_layout("Alice", {"rounds": 5}, [], _named, start_at=1_700_000_000))
    assert "⏰ Starting <t:1700000000:R>" in text
    assert "on its own" in text and "3 have joined" in text


def test_lobby_without_a_countdown_has_no_starting_line():
    assert "⏰ Starting" not in _text(build_lobby_layout("Alice", {"rounds": 5}, [], _named))


def test_lobby_shows_host_rounds_and_empty_roster():
    text = _text(build_lobby_layout("Alice", {"rounds": 7}, [], _named))
    assert "Host: **Alice** · Rounds: **7**" in text
    assert "### Players (0)\n(nobody yet)" in text


@pytest.mark.parametrize(
    "players, shown, tail",
    [
        pytest.param([1, 2, 3], ["User1", "User2", "User3"], None, id="under-ten"),
        pytest.param(list(range(1, 13)), ["User1", "User10"], "(+2 more)", id="over-ten"),
    ],
)
def test_lobby_roster(players, shown, tail):
    text = _text(build_lobby_layout("Alice", {"rounds": 5}, players, _named))
    assert f"### Players ({len(players)})" in text
    for name in shown:
        assert name in text
    if tail:
        assert tail in text and "User11" not in text


@pytest.mark.parametrize(
    "players, shown",
    [
        pytest.param([1, 2, 3], True, id="three"),
        pytest.param([1, 2, 3, 4], False, id="four"),
        pytest.param([1, 2], False, id="two"),
    ],
)
def test_lobby_notes_the_three_player_floor(players, shown):
    # clapback-7: the lobby is honest about a three-player game.
    text = _text(build_lobby_layout("Host", {"rounds": 3}, players, _named))
    assert (THREE_PLAYER_NOTE in text) is shown


def test_lobby_notice_sits_above_the_buttons():
    # A V2 card has no content= line, so a timed-out or cancelled lobby says
    # so on the card itself, just above its (disabled) buttons.
    items = build_lobby_layout("A", {"rounds": 1}, [], notice="🛑 **Lobby cancelled**")
    assert isinstance(items[-2], discord.ui.TextDisplay)
    assert items[-2].content == "🛑 **Lobby cancelled**"
    assert isinstance(items[-1], discord.ui.ActionRow)


_HEADER = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"
_FOOTER = "https://dash.example/static/panel-banners/" + "f" * 32 + ".webp"


def _galleries(items) -> list[str]:
    return [i.items[0].media.url for i in items if isinstance(i, discord.ui.MediaGallery)]


@pytest.mark.parametrize(
    "header, footer",
    [
        pytest.param(None, None, id="no-art"),
        # Prod has a hub footer and no lobby header: its lobbies stay plain.
        pytest.param(None, _FOOTER, id="footer-alone"),
    ],
)
def test_lobby_without_a_header_draws_no_art(header, footer):
    plain = build_lobby_layout("A", {"rounds": 1}, [1, 2])
    items = build_lobby_layout("A", {"rounds": 1}, [1, 2], header_url=header, footer_url=footer)
    assert _galleries(items) == []
    assert [type(i) for i in items] == [type(i) for i in plain]


def test_lobby_header_tops_the_card_and_footer_closes_it():
    items = build_lobby_layout(
        "Alice", {"rounds": 5}, list(range(1, 13)), _named, start_at=1,
        notice="⌛ timed out", header_url=_HEADER, footer_url=_FOOTER,
    )
    check_budget(_view(items))
    assert _galleries(items[:1]) == [_HEADER]
    assert _text(items[1:2]).startswith(f"## {ICON} Clapback")
    assert isinstance(items[-2], discord.ui.ActionRow)
    assert _galleries(items[-1:]) == [_FOOTER]


def test_a_titled_header_replaces_the_heading():
    items = build_lobby_layout(
        "Alice", {"rounds": 5}, [1], _named,
        header_url=_HEADER, footer_url=_FOOTER, title_in_header=True,
    )
    check_budget(_view(items))
    text = _text(items)
    assert f"## {ICON}" not in text  # the header says "Clapback"; the card doesn't twice
    assert text.startswith("Join the battle of wits!")
    assert items[0].items[0].description == layouts.LOBBY_TITLE  # type: ignore[attr-defined]


def test_the_title_flag_without_a_header_keeps_the_heading():
    items = build_lobby_layout("A", {"rounds": 1}, [], title_in_header=True)
    assert _text(items).startswith(f"## {ICON} Clapback")


# ── submit ─────────────────────────────────────────────────────────────────


def test_submit_renders_prompt_timer_and_counts():
    text = _text(build_submit_layout("A weird prompt", 2, 5, "<t:123:R>", 1, 4))
    assert f"## {ICON} Clapback — Round 2/5" in text
    assert '**"A weird prompt"**' in text
    assert "<t:123:R> · Answers in: **1/4**" in text


def test_submit_names_the_benched_player():
    text = _text(build_submit_layout("p", 1, 3, "⏰ 60s", 0, 4, bye_player=7, name_fn=_named))
    assert "🪑 **User7** is sitting out" in text
    assert "<@7>" not in text


def test_submit_omits_the_bye_line_with_no_bye():
    assert "sitting out" not in _text(build_submit_layout("p", 1, 3, "⏰ 60s", 0, 4))


# ── vote ───────────────────────────────────────────────────────────────────


def test_vote_renders_both_answers_and_matchup_progress():
    items = build_vote_layout("ans A", "ans B", 1, 0, 2, "<t:99:R>", vote_count=4)
    text = _text(items)
    assert f"## {ICON} Head to Head — Round 1, Matchup 1/2" in text
    assert '🅰️ *"ans A"*' in text and '🅱️ *"ans B"*' in text
    assert "<t:99:R> · Votes: **4**" in text
    labels = [b.label for b in _view(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert all(label and "ans" in label for label in labels)


def test_submit_escapes_markdown_in_the_prompt():
    # A bank prompt is curator-written, not the bot's own copy: raw, a
    # ``||spoiler||`` would hide the round's whole question.
    text = _text(build_submit_layout("||surprise||", 1, 3, "<t:1:R>", 0, 2))
    assert "||surprise||" not in text
    assert "\\|\\|surprise\\|\\|" in text


def test_vote_escapes_markdown_in_answers_and_prompt():
    text = _text(build_vote_layout("**bold**", "_italic_", 1, 0, 1, "t", prompt="*p*"))
    assert "\\*\\*bold\\*\\*" in text
    assert "\\_italic\\_" in text
    assert '💬 **"\\*p\\*"**' in text


def test_vote_omits_the_prompt_when_absent():
    assert "💬" not in _text(build_vote_layout("a", "b", 1, 0, 1, "t"))


# ── reveal ─────────────────────────────────────────────────────────────────


def test_reveal_is_a_receipt_with_no_buttons():
    for result in (_tie(), _clapback(), _regular()):
        assert _ids(_reveal(result)) == []


def test_reveal_clapback_branch():
    text = _text(_reveal(_clapback(), answers={"10": "winning", "20": "losing"}))
    assert text.startswith(f"## {ICON} Clapback!")
    assert "⚡" not in text.splitlines()[0]
    assert "### 🏆 Winner" in text and "### 💀 Defeated" in text
    assert "by **User10** (+125 pts!)" in text
    assert "**User10** got a CLAPBACK! 🎉" in text


def test_reveal_clapback_winner_can_be_player_b():
    text = _text(_reveal(_clapback(winner=20), answers={"10": "loser side", "20": "winner side"}))
    winner = text.split("### 💀")[0]
    assert "winner side" in winner and "User20" in winner


def test_reveal_anonymous_hides_both_authors():
    text = _text(_reveal(_clapback(), anonymous=True))
    assert "User10" not in text and "User20" not in text
    assert "???" in text


def test_reveal_tie_branch():
    text = _text(_reveal(_tie()))
    assert f"## {ICON} Matchup Result — Tie!" in text
    assert text.count("🤝") == 2
    for bit in ("answer A", "answer B", "User10", "User20", "(+50 pts)"):
        assert bit in text


def test_reveal_regular_win_branch_with_prompt():
    text = _text(_reveal(_regular(), prompt="The worst superpower"))
    first = text.splitlines()[0]
    assert first == f"## {ICON} Matchup Result"
    assert "Clapback" not in first
    assert '💬 *"The worst superpower"*' in text
    assert "### 🏆 Winner" in text and "### 💀 Runner-up" in text
    assert "2 votes (67%)" in text and "1 votes (33%)" in text


def test_reveal_escapes_answers():
    text = _text(_reveal(_regular(), answers={"10": "*ha*", "20": "_b_"}))
    assert '*"\\*ha\\*"*' in text


# ── scoreboard ─────────────────────────────────────────────────────────────


def test_scoreboard_sorts_highest_first_with_names():
    text = _text(build_scoreboard_layout({"scores": {"1": 30, "2": 100, "3": 50}}, 2, 5,
                                         name_fn=_named))
    assert (
        "### 📊 Scoreboard\n🥇 **User2** — **100** pts\n🥈 **User3** — **50** pts\n"
        "🥉 **User1** — **30** pts"
    ) in text
    assert "<@" not in text


def test_scoreboard_default_resolver_keeps_a_mention():
    """An un-wired caller still renders (as a mention) rather than crashing;
    the AST test below is what forces the cog to wire a resolver."""
    text = _text(build_scoreboard_layout({"scores": {"1": 30}}, 2, 5, bye_players=[2]))
    assert "<@1>" in text and "<@2>" in text


def test_scoreboard_empty_scores_shows_placeholder():
    assert "### 📊 Scoreboard\nNo scores yet" in _text(build_scoreboard_layout({"scores": {}}, 1, 5))


@pytest.mark.parametrize(
    "byes, award, expect",
    [
        pytest.param(None, None, None, id="no-bye"),
        pytest.param([3], 62, "### 🪑 Bye\n**User3** sat this round out — **+62** pts (", id="one"),
        pytest.param([3], None, "**+50** pts", id="old-record-defaults-to-fifty"),
        pytest.param(3, None, "### 🪑 Bye\n", id="bare-id"),
        pytest.param([3, 4], 40, "### 🪑 Byes\n**User3**, **User4** sat this round out — **+40** pts each",
                     id="two"),
    ],
)
def test_scoreboard_bye_section(byes, award, expect):
    text = _text(build_scoreboard_layout({"scores": {"1": 10}}, 1, 5, bye_players=byes,
                                         bye_award=award, name_fn=_named))
    if expect is None:
        assert "🪑" not in text
    else:
        assert expect in text


@pytest.mark.parametrize(
    "round_num, expect",
    [
        pytest.param(2, "-# 3 round(s) remaining!", id="remaining"),
        pytest.param(5, "-# Final round complete!", id="final"),
    ],
)
def test_scoreboard_footer(round_num, expect):
    text = _text(build_scoreboard_layout({"scores": {"1": 10}}, round_num, 5))
    assert text.endswith(expect)


def test_scoreboard_next_row_only_when_asked():
    assert _ids(build_scoreboard_layout({"scores": {}}, 1, 5)) == []


def test_scoreboard_marks_a_leaver_below_the_standings():
    text = _text(build_scoreboard_layout(
        {"scores": {"1": 300, "2": 200, "3": 100}, "left": ["1"]}, 2, 5, name_fn=_named,
    ))
    lines = text.split("### 📊 Scoreboard\n")[1].splitlines()
    assert lines[0] == "🥇 **User2** — **200** pts"
    assert lines[1] == "🥈 **User3** — **100** pts"
    assert "User1" in lines[2] and "left" in lines[2]


# ── recap ──────────────────────────────────────────────────────────────────


def test_recap_with_no_scores_crowns_nobody():
    text = _text(build_recap_layout(
        {"scores": {}, "clapbacks": {}, "round_history": [], "players": []}, {}, _named,
    ))
    assert "🏆 Winner: **Nobody**" in text


def test_recap_crowns_the_highest_scorer_and_tallies_clapbacks():
    text = _text(build_recap_layout(_recap(), {"anonymous": False}, _named))
    assert "🏆 Winner: **User10** — **250** pts" in text
    assert "Rounds: **0** · Players: **3**" in text
    assert "🥇 **User10** — **250** pts (2 CLAPBACKS)" in text
    user20 = next(line for line in text.splitlines() if "User20" in line)
    assert "CLAPBACK" not in user20
    assert "⚡ Total Clapbacks: **2**" in text


def test_recap_singular_clapback_suffix():
    text = _text(build_recap_layout(
        _recap(scores={"10": 100}, clapbacks={"10": 1}, players=[10]), {}, _named,
    ))
    assert "(1 CLAPBACK)" in text and "CLAPBACKS" not in text


def test_recap_omits_total_clapbacks_when_zero():
    text = _text(build_recap_layout(_recap(clapbacks={"10": 0}), {}, _named))
    assert "Total Clapbacks" not in text


@pytest.mark.parametrize("anonymous", [False, True])
def test_recap_best_answer_escapes_and_honours_anonymous(anonymous):
    text = _text(build_recap_layout(_recap(round_history=_HISTORY), {"anonymous": anonymous},
                                    _named))
    best = text.split("### ⚡ Best Single Answer\n")[1].split("\n###")[0]
    # The old embed quoted the answer raw; a * in it broke the italics.
    assert '*"best\\*!"*' in best
    assert ("???" in best) is anonymous
    assert ("User10" in best) is not anonymous


def test_recap_includes_closest_matchup_when_qualifying():
    history = [{"round": 1, "matchups": [
        {"player_a": 10, "answer_a": "a", "votes_a": 1,
         "player_b": 20, "answer_b": "b", "votes_b": 1, "clapback": False},
    ]}]
    text = _text(build_recap_layout(_recap(round_history=history), {}, _named))
    assert "### 🤣 Closest Matchup" in text
    assert "1–1 in Round 1" in text


def test_recap_crowns_the_highest_remaining_player():
    """The leader left in round 4: the recap crowns whoever is still in and
    says the leaver's score is withdrawn, rather than naming a winner nobody
    is paid for."""
    text = _text(build_recap_layout(
        {"players": [2, 3], "left": ["1"], "scores": {"1": 300, "2": 200, "3": 100},
         "clapbacks": {}, "round_history": []},
        {"anonymous": False}, _named,
    ))
    assert "🏆 Winner: **User2** — **200** pts" in text
    board = text.split("### 📊 Final Scoreboard\n")[1]
    assert board.startswith("🥇 **User2** — **200** pts\n🥈 **User3** — **100** pts")
    assert "~~User1~~" in board and "withdrawn" in board
    assert "🥇 **User1**" not in board


@pytest.mark.parametrize("line", [None, "💰 Winner paid 100 coins"])
def test_recap_payout_line_is_subtext(line):
    text = _text(build_recap_layout(_recap(), {}, _named, payout_line=line))
    assert ("-# 💰 Winner paid 100 coins" in text) is bool(line)


# ── every render site in the cog passes a resolver ─────────────────────────


def test_every_clapback_render_site_passes_a_name_fn():
    """``build_lobby_layout``, ``build_submit_layout`` and
    ``build_scoreboard_layout`` default their ``name_fn`` to ``mention``, so a
    render site that forgets to pass one silently reintroduces
    digits-in-the-scoreboard and no builder test above would notice. This
    walks the cog and requires every call to a name-taking builder to hand
    one over."""
    from bot_modules.games.clapback import cog as games_clapback_cog

    needs = {
        name
        for name, fn in inspect.getmembers(layouts, inspect.isfunction)
        if name.startswith("build_") and "name_fn" in inspect.signature(fn).parameters
    }
    assert {"build_submit_layout", "build_scoreboard_layout", "build_lobby_layout"} <= needs
    # Explicit utf-8: the CI runner is Windows, where the default is cp1252.
    source = pathlib.Path(inspect.getfile(games_clapback_cog)).read_text(encoding="utf-8")
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in needs
    ]
    assert calls
    missed = [
        f"games_clapback_cog.py:{node.lineno} {node.func.id}()"  # type: ignore[attr-defined]
        for node in calls
        if not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)
