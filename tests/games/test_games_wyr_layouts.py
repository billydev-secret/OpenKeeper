"""Tests for ``bot_modules/games/wyr/layouts.py`` — the Components V2 cards
that replaced ``games_wyr/embeds.py`` — and the host panel that took Next
and End Game off the card.

The frame the three round games share is pinned in
``test_round_layouts.py``; what is here is Would You Rather's own: the two
options over their bars, the opt-in named voters, the recap's numbers.
Budget is checked at the worst case the game can produce: no player cap
exists, so every voter in a full channel pressing Show My Vote is the shape
to check.
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

import bot_modules.games.wyr.cog as wyr_cog
from bot_modules.core.layout import card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.utils.round_pacing import CONTROL_DENIED, END_DENIED, RoundPacing
from bot_modules.games.wyr import layouts
from bot_modules.games.wyr.layouts import (
    HELP_ID,
    HOST_END_ID,
    HOST_NEXT_ID,
    NEXT_ID,
    POSE_ID,
    SHOW_VOTE_ID,
    VOTE_A_ID,
    VOTE_B_ID,
    build_host_panel_layout,
    build_recap_layout,
    build_round_layout,
)


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


# ── the round card ───────────────────────────────────────────────────────────


def test_round_card_shows_each_option_over_its_own_bar():
    text = _text(build_round_layout("fly", "swim", [1, 2, 3], [4], False, 3))
    assert text.startswith(f"## {layouts.ICON} Would You Rather\nRound **3**")
    a, b = text.index("🅰️ **fly**"), text.index("🅱️ **swim**")
    assert a < text.index("75% (3)") < b < text.index("25% (1)")


def test_round_card_escapes_markdown_in_options():
    text = _text(build_round_layout("**bold**", "_italic_", [], [], False, 1))
    assert "\\*\\*bold\\*\\*" in text
    assert "\\_italic\\_" in text


def test_open_round_carries_the_votes_pose_and_help_and_nothing_host_only():
    """Ben, 2026-09-30: everything host-only lives on the host panel.
    Reverting puts Next and End Game back in front of the room."""
    assert _ids(build_round_layout("fly", "swim", [], [], True, 1)) == [
        HELP_ID, VOTE_A_ID, VOTE_B_ID, SHOW_VOTE_ID, POSE_ID,
    ]


def test_scheduled_round_keeps_next_on_the_card():
    """platform-23: on a scheduled game any voter may press Next once it
    unlocks, so there it is not host-only and stays public."""
    ids = _ids(build_round_layout("fly", "swim", [], [], True, 1, scheduled=True))
    assert ids[-1] == NEXT_ID


def test_waiting_round_offers_only_pose_and_help():
    """vote-games-50: nothing to vote on, and nothing to advance past — not
    even on a scheduled game."""
    items = build_round_layout("", "", [], [], True, 1, waiting=True, scheduled=True)
    assert _ids(items) == [HELP_ID, POSE_ID]
    assert "Pose Question" in _text(items)


def test_closed_round_keeps_the_tally_and_drops_every_control():
    items = build_round_layout("fly", "swim", [1, 2], [3], True, 4, closed=True, scheduled=True)
    assert _ids(items) == []
    text = _text(items)
    assert "Round Over" in text
    assert "67% (2)" in text and "33% (1)" in text


def test_votes_are_anonymous_until_a_voter_shows_their_own():
    text = _text(build_round_layout("fly", "swim", [1, 2], [3], True, 1, name_fn=_named))
    assert "Member" not in text


def test_only_the_shown_voters_are_named_and_the_rest_are_counted():
    text = _text(build_round_layout(
        "fly", "swim", [1, 2, 3], [4], True, 1, name_fn=_named, shown=[1, 4],
    ))
    assert "Member1 +2 anonymous" in text
    assert "Member2" not in text and "Member3" not in text
    # A side with nobody hidden has no count.
    assert "Member4\n" in text + "\n" and "Member4 +" not in text


def test_a_shown_voter_the_resolver_cannot_name_reads_user_id():
    from bot_modules.shared.name_resolver import mention

    text = _text(build_round_layout("fly", "swim", [7], [], True, 1, name_fn=mention, shown=[7]))
    assert "User 7" in text and "<@7>" not in text


def test_closed_round_keeps_the_shown_names():
    text = _text(build_round_layout(
        "fly", "swim", [1], [2], True, 1, closed=True, name_fn=_named, shown=[1],
    ))
    assert "Member1" in text


def test_every_wyr_render_site_passes_a_resolver():
    """``name_fn`` defaults to ``mention`` so an un-wired caller still renders;
    the wiring therefore needs its own guard, or a render site that forgets
    the resolver silently brings the bare-number bug back."""
    needs = {
        name
        for name, fn in inspect.getmembers(layouts, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
        and fn.__module__ == layouts.__name__
        and not name.startswith("_")
    }
    assert needs == {"build_round_layout"}
    source = pathlib.Path(inspect.getfile(wyr_cog)).read_text(encoding="utf-8")
    missed = [
        f"games_wyr_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


@pytest.mark.parametrize("anonymous", [True, False])
def test_anonymity_note_says_how_to_opt_out(anonymous):
    """vote-games-61: "Anonymous" alone read as a promise nobody could act
    on, so the line names the button."""
    text = _text(build_round_layout("fly", "swim", [], [], anonymous, 1))
    assert (f"-# {layouts.ANON_NOTE}" in text) is anonymous
    assert "Show My Vote" in layouts.ANON_NOTE


def test_queued_questions_are_counted_in_the_subtext():
    assert "-# 2 questions queued" in _text(
        build_round_layout("fly", "swim", [], [], True, 1, queued=2)
    )
    assert "queued" not in _text(build_round_layout("fly", "swim", [], [], True, 1))


def test_round_card_fits_the_budget_with_a_whole_channel_showing_its_votes():
    """No player cap exists and every voter may press Show My Vote. The
    embed had no fit on this path; the card counts what it cannot name."""
    voters = list(range(1, 201))
    nasty = lambda uid: "\\*" * 32  # noqa: E731 — a 32-char markdown name, escaped
    items = build_round_layout(
        "~" * 2000, "_" * 2000, voters[:100], voters[100:], True, 50,
        name_fn=nasty, shown=voters, advance_at=1, queued=15, scheduled=True,
    )
    check_budget(card(items))
    assert "more" in _text(items)


# ── the host panel, the recap ────────────────────────────────────────────────


def test_host_panel_is_next_and_end_on_its_own_ids():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_NEXT_ID, HOST_END_ID]
    assert {HOST_NEXT_ID, HOST_END_ID}.isdisjoint({"wyr_next", "wyr_end"})
    assert _text(items).startswith(f"## {layouts.ICON} Host Controls")


ROUNDS = {
    "1": {"a": [1, 2], "b": [3], "q": "A OR B"},
    "2": {"a": [4], "b": [1], "q": "C OR D"},
    "3": {"a": [], "b": [], "q": ""},
}


def test_recap_counts_played_rounds_and_votes_and_names_the_closest_split():
    text = _text(build_recap_layout(ROUNDS))
    assert text.startswith(f"## {layouts.ICON} Would You Rather — Game Over")
    assert "Rounds played: **2**" in text  # the round left waiting never happened
    assert "Total votes: **5**" in text
    assert "### Most Divisive\nC OR D\n🅰️ 1 — 🅱️ 1" in text


def test_recap_with_no_votes_says_so():
    assert "No votes were cast." in _text(build_recap_layout({"1": {"a": [], "b": [], "q": "x OR y"}}))


@pytest.mark.parametrize(
    "reason, said", [pytest.param("round_cap", True, id="round-cap"), pytest.param("ended", False, id="host-ended")],
)
def test_recap_says_it_was_the_last_round_only_at_the_cap(reason, said):
    assert ("That's the last round" in _text(build_recap_layout(ROUNDS, reason=reason))) is said


def test_recap_carries_the_payout_line_as_subtext():
    assert "-# 🪙 +5 to everyone" in _text(build_recap_layout(ROUNDS, payout_line="🪙 +5 to everyone"))
    check_budget(card(build_recap_layout(
        {"1": {"a": [1], "b": [2], "q": "~" * 6000}}, reason="round_cap", payout_line="p",
    )))


# ── the host panel: Next and End Game, off the card ──────────────────────────


def _bot():
    return SimpleNamespace(active_views={}, games_db=None)


def _view(bot, **kwargs):
    view = wyr_cog.WYRRoundView(
        "gid", 1, "x", "y", 1, True, None, bot, "Host", AsyncMock(),
        pacing=RoundPacing(opened_at=1.0), finish_callback=AsyncMock(), **kwargs,
    )
    view.message = SimpleNamespace(id=555, edit=AsyncMock())
    bot.active_views["gid"] = view
    return view


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}", roles=[]),
        guild=None, guild_id=None, channel=SimpleNamespace(id=100, name="games"),
        message=SimpleNamespace(id=999, edit=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False,
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_wyr_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["wyr"] == "WYRCog"


async def test_games_host_sends_the_host_their_panel():
    bot = _bot()
    _view(bot)
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    interaction = _interaction()

    await cog.open_host_panel(interaction, "gid")  # type: ignore[arg-type]

    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["view"], host_panel.GameHostPanel)
    assert kwargs["ephemeral"] is True


@pytest.mark.parametrize(
    "uid, live, expected",
    [
        pytest.param(2, True, CONTROL_DENIED, id="not-the-host"),
        pytest.param(1, False, host_panel.GAME_ENDED_REPLY, id="game-ended"),
    ],
)
async def test_games_host_refusals(uid, live, expected):
    bot = _bot()
    _view(bot)
    if not live:
        bot.active_views.clear()
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    interaction = _interaction(uid)

    await cog.open_host_panel(interaction, "gid")  # type: ignore[arg-type]

    assert _said(interaction) == expected


async def test_panel_next_advances_the_board_not_the_panel_message():
    """A panel press arrives on the ephemeral panel; the round to advance
    is the board the view holds."""
    bot = _bot()
    view = _view(bot)
    interaction = _interaction()

    await view.press_next(interaction)  # type: ignore[arg-type]

    view.advance_callback.assert_awaited_once_with(view.message)
    interaction.response.defer.assert_awaited_once()


async def test_next_on_a_waiting_round_has_nothing_to_advance():
    bot = _bot()
    view = wyr_cog.WYRRoundView("gid", 1, "", "", 1, True, None, bot, "Host", AsyncMock())
    interaction = _interaction()

    await view.press_next(interaction)  # type: ignore[arg-type]

    assert "pose one first" in _said(interaction)
    view.advance_callback.assert_not_awaited()


async def test_end_game_keeps_its_own_gate_and_confirms_before_ending():
    bot = _bot()
    view = _view(bot)
    stranger = _interaction(2)
    await view.press_end(stranger)  # type: ignore[arg-type]
    assert _said(stranger) == END_DENIED

    host = _interaction(1)
    await view.press_end(host)  # type: ignore[arg-type]
    assert "Are you sure" in _said(host)
    view.finish_callback.assert_not_awaited()  # nothing destructive is one tap


async def test_a_queued_question_redraws_the_card_with_the_count(monkeypatch):
    monkeypatch.setattr(wyr_cog, "audit_anonymous", AsyncMock())
    bot = _bot()
    view = _view(bot)
    board = SimpleNamespace(id=555, edit=AsyncMock())
    modal = wyr_cog.PoseWYRModal(view, board)  # type: ignore[arg-type]
    modal.option_a._value = "fly"  # type: ignore[attr-defined]
    modal.option_b._value = "swim"  # type: ignore[attr-defined]

    await modal.on_submit(_interaction(7))  # type: ignore[arg-type]

    assert view.queued_questions == [("fly", "swim")]
    assert board.edit.await_args.kwargs["view"] is view
    text = "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )
    assert "-# 1 question queued" in text


async def test_recovery_redraws_a_board_that_is_still_an_embed(sync_db_path):
    """The conversion lands with a restart: a round in flight across it is
    an embed message whose old Next and End Game buttons the recovered view
    cannot answer, so recovery turns it into the card at once."""
    from bot_modules.games.utils.game_manager import create_game, get_active_game_by_id
    from bot_modules.games.db import GamesDb

    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path), add_view=lambda *a, **k: None,
    )
    payload = {"rounds": {"1": {"a": [7], "b": [], "q": "x OR y"}}}
    gid = await create_game(bot.games_db, 100, 1, "wyr", payload=payload)
    row = await get_active_game_by_id(bot.games_db, gid)
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, name="games", guild=None)

    embed_era = SimpleNamespace(id=1, embeds=[object()], edit=AsyncMock())
    assert await cog.recover_game(row, payload, channel, embed_era)
    kwargs = embed_era.edit.await_args.kwargs
    assert kwargs["embed"] is None and kwargs["content"] is None
    assert kwargs["view"] is bot.active_views[gid]

    already_a_card = SimpleNamespace(id=2, embeds=[], edit=AsyncMock())
    assert await cog.recover_game(row, payload, channel, already_a_card)
    already_a_card.edit.assert_not_awaited()
