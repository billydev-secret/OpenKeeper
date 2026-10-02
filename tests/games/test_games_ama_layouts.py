"""Tests for ``bot_modules/games/ama/layouts.py`` — the Components V2 cards
that replaced ``games_ama/embeds.py`` — and the host panel that took Skip,
New Hot Seat and End AMA off the board.

The builders are pure, so every card is asserted on its text with no
Discord mocks. Budget is checked at the worst case the game can produce:
the queue and the panel have no cap, questions run to 500 characters and
answers to 1,000, all of it markdown that doubles under escaping.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.ama.cog as ama_cog
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.ama import layouts
from bot_modules.games.ama.layouts import (
    APPROVE_ID,
    ASK_ID,
    HELP_ID,
    HOST_END_ID,
    HOST_NEW_SEAT_ID,
    HOST_SKIP_ID,
    PASS_ID,
    REJECT_ID,
    REPLY_ID,
    STATE_ENDED,
    STATE_EXPIRED,
    STATE_PASSED,
    VOLUNTEER_ID,
    build_answered_layout,
    build_asker_dm_layout,
    build_board_layout,
    build_host_panel_layout,
    host_panel_handlers,
    build_question_layout,
    build_recap_layout,
    build_screened_dm_layout,
    question_mentions,
)
from bot_modules.games.ama.logic import AMA_FORMAT_HOT_SEAT, AMA_FORMAT_PANEL, compute_recap_stats
from bot_modules.shared.name_resolver import GuildNames, mention

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


def _board(**overrides):
    kwargs: dict[str, Any] = dict(host_name="Ada", mode="unfiltered", game_format=AMA_FORMAT_HOT_SEAT, name_fn=mention)
    kwargs.update(overrides)
    return build_board_layout(**kwargs)


# ── the board ────────────────────────────────────────────────────────────────


def test_lobby_asks_for_a_hot_seat_and_carries_nothing_host_only():
    """Ben, 2026-09-30: Skip, New Hot Seat and End AMA live on the host panel."""
    items = _board()
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Anonymous AMA\nWho's taking the hot seat?")
    assert "Host: **Ada** · Mode: **Unfiltered** · Hot seat: **—**" in text
    assert _ids(items) == [HELP_ID, VOLUNTEER_ID, ASK_ID]
    assert f"-# {host_panel.HOST_HINT}" in text
    assert "Progress" not in text, "a fresh game can only ever read zero"


@pytest.mark.parametrize("mode, label", [("screened", "Screened"), ("unfiltered", "Unfiltered")])
def test_board_title_cases_the_mode(mode, label):
    """social-prompt-44: the live embeds printed the raw enum."""
    assert f"Mode: **{label}**" in _text(_board(mode=mode))
    assert f"Mode: **{label}**" in _text(build_recap_layout(mode, {}))


def test_hot_seat_board_names_the_seat_and_counts_the_turn_against_the_dial():
    text = _text(_board(hot_seat_name="*Bob*", questions_this_turn=1, per_turn=6))
    assert "Ask **\\*Bob\\*** anything — questions are anonymous.\n**5** questions left this turn." in text
    assert "Hot seat: **\\*Bob\\***" in text


def test_queue_is_listed_in_order_only_when_someone_is_waiting():
    named = _text(_board(hot_seat_name="Bob", queue=[1, 2], name_fn=lambda u: f"Member{u}"))
    assert "### 📋 Queue (2)\nMember1 → Member2" in named
    assert "Queue" not in _text(_board(hot_seat_name="Bob"))


def test_panel_board_prompts_to_volunteer_then_lists_the_roster():
    empty = _text(_board(game_format=AMA_FORMAT_PANEL))
    assert "Tap **🙋 Volunteer** to join the panel" in empty
    assert "### 🙋 Panel\n-# Nobody yet" in empty
    full = _text(_board(game_format=AMA_FORMAT_PANEL, panel=[7, 8], name_fn={7: "Ann", 8: "Bo"}.get))
    assert "Ask **anyone on the panel** anything" in full
    assert "### 🙋 Panel (2)\n• Ann\n• Bo" in full


def test_an_uncached_member_reads_user_id():
    assert "User 7" in _text(_board(hot_seat_name="Bob", queue=[7]))


def test_progress_bar_shows_with_a_payload_and_drops_a_failed_question():
    payload = {"questions": [{"status": "answered"}, {"status": "failed"}], "total_answered": 1, "total_passed": 0}
    text = _text(_board(hot_seat_name="Bob", payload=payload))
    assert "### 📊 Progress\nQuestions: **1** · Answered: **1** · Passed: **0**" in text
    assert "100%" in text


def test_closed_board_keeps_the_numbers_and_drops_every_control():
    items = _board(hot_seat_name="Bob", payload={"questions": []}, closed=True)
    assert _ids(items) == []
    assert "Progress" in _text(items) and "-# " not in _text(items)


def test_rosters_are_fitted_by_whole_names_at_a_whole_channel():
    names = lambda uid: NASTY  # noqa: E731
    queue = _text(_board(hot_seat_name=NASTY, queue=list(range(300)), name_fn=names))
    panel = _text(_board(game_format=AMA_FORMAT_PANEL, panel=list(range(300)), name_fn=names))
    for text in (queue, panel):
        assert "…and" in text and "more" in text
    check_budget(card(_board(hot_seat_name=NASTY, queue=list(range(300)), name_fn=names, payload={"questions": []})))
    check_budget(card(_board(game_format=AMA_FORMAT_PANEL, panel=list(range(300)), name_fn=names, payload={"questions": []})))


# ── the host panel ───────────────────────────────────────────────────────────


def test_host_panel_has_seat_controls_only_in_hot_seat_format():
    assert _ids(build_host_panel_layout(AMA_FORMAT_HOT_SEAT)) == [HOST_SKIP_ID, HOST_NEW_SEAT_ID, HOST_END_ID]
    assert _ids(build_host_panel_layout(AMA_FORMAT_PANEL)) == [HOST_END_ID]
    assert {HOST_SKIP_ID, HOST_NEW_SEAT_ID, HOST_END_ID}.isdisjoint({"ama_skip", "ama_new_hs", "ama_end"})
    assert host_panel.HOST_COMMAND in _text(build_host_panel_layout(AMA_FORMAT_PANEL))


# ── the question card ────────────────────────────────────────────────────────


def test_question_card_pings_a_cached_seat_once_and_names_an_uncached_one():
    pinged = build_question_layout("Why?", target_id=42)
    assert _text(pinged).startswith(f"## {layouts.ICON} Question\nFor <@42>\n> Why?")
    assert _text(pinged).count("42") == 1
    assert _ids(pinged) == [REPLY_ID, PASS_ID]
    named = _text(build_question_layout("Why?", target_name="*Bob*"))
    assert "For **\\*Bob\\***" in named and "<@" not in named


def test_question_mentions_allow_list_exactly_the_target():
    assert [u.id for u in question_mentions(42).users] == [42]
    assert not question_mentions(None).users


def test_question_card_escapes_and_quotes_every_line():
    text = _text(build_question_layout("*one*\ntwo", target_id=1))
    assert "> \\*one\\*\n> two" in text


@pytest.mark.parametrize("state", [STATE_PASSED, STATE_ENDED, STATE_EXPIRED])
def test_a_retired_question_card_keeps_its_question_and_loses_its_buttons(state):
    items = build_question_layout("Why?", target_id=42, state=state)
    assert _ids(items) == []
    assert _text(items).endswith(f"-# {state}") and "> Why?" in _text(items)


def test_answered_card_quotes_both_and_signs_the_answerer():
    text = _text(build_answered_layout("*Q*", "_A_", "*Ada*"))
    assert text.startswith(f"## {layouts.ICON} Question + Answer\n**Q:**\n> \\*Q\\*\n**A:**\n> \\_A\\_")
    assert text.endswith("-# — \\*Ada\\*")


# ── the DMs ──────────────────────────────────────────────────────────────────


def test_screened_dm_is_a_decision_card_that_edits_into_a_receipt():
    items = build_screened_dm_layout("the actual question", "Bob", "<#42>")
    text = _text(items)
    assert "For **Bob** in <#42>:\n> the actual question" in text
    assert _ids(items) == [APPROVE_ID, REJECT_ID]
    assert "These buttons don't expire" in text
    receipt = build_screened_dm_layout("the actual question", "Bob", "<#42>", receipt="✅ Question approved.")
    assert _ids(receipt) == []
    assert _text(receipt).endswith("✅ Question approved.") and "> the actual question" in _text(receipt)


def test_screened_dm_buttons_are_the_decision_pair():
    buttons = [i for i in card(build_screened_dm_layout("q", "Bob", "<#1>")).walk_children() if isinstance(i, discord.ui.Button)]
    assert [b.style for b in buttons] == [discord.ButtonStyle.success, discord.ButtonStyle.danger]


@pytest.mark.parametrize(
    "count, head, label",
    [
        pytest.param(1, "Your anonymous question in **<#42>** got a reply!", "Jump to the answer", id="one"),
        pytest.param(3, "3 of your anonymous questions in **<#42>** got replies!", "Jump to the first answer", id="batched"),
    ],
)
def test_asker_dm_names_the_room_and_links_the_answer(count, head, label):
    text = _text(build_asker_dm_layout("<#42>", answer_url="https://x/y", answer_count=count))
    assert head in text and f"[{label}](https://x/y)" in text
    assert "Jump" not in _text(build_asker_dm_layout("<#42>"))


# ── the recap ────────────────────────────────────────────────────────────────


def test_recap_renders_every_stat_and_thanks_once():
    stats = {"total_q": 4, "total_answered": 3, "total_passed": 1, "rotations": 2, "unique_askers": 3}
    text = _text(build_recap_layout("screened", stats, payout_line="🪙 +5 to everyone"))
    assert text.startswith(f"## {layouts.ICON} Anonymous AMA — Game Over\nThanks for playing!")
    assert text.count("Thanks") == 1, "the embed thanked twice (filler review, 2026-09-03)"
    assert "**4** questions asked by **3** people\n**3** answered · **1** passed" in text
    assert "75%" in text
    assert "Hot seat rotations: **2** · 🎙️ Mode: **Screened**" in text
    assert text.endswith("-# 🪙 +5 to everyone")


def test_recap_handles_zero_questions():
    text = _text(build_recap_layout("unfiltered", compute_recap_stats({})))
    assert "**0** questions asked by **0** people" in text and "0%" in text


# ── every card: budget at the worst case, glyph heading ──────────────────────


CARDS = {
    "board": _board(host_name=NASTY, hot_seat_name=NASTY, queue=list(range(300)), name_fn=lambda u: NASTY, payload={"questions": [{"status": "answered"}] * 3}),
    "board-panel": _board(host_name=NASTY, game_format=AMA_FORMAT_PANEL, panel=list(range(300)), name_fn=lambda u: NASTY, payload={"questions": []}),
    "panel": build_host_panel_layout(AMA_FORMAT_HOT_SEAT),
    "question": build_question_layout("_" * 500, target_name=NASTY),
    "question-retired": build_question_layout("_" * 500, target_name=NASTY, state=STATE_EXPIRED),
    "answered": build_answered_layout("_" * 500, "_" * 1000, NASTY),
    "screened-dm": build_screened_dm_layout("_" * 500, NASTY, "<#1>"),
    "asker-dm": build_asker_dm_layout("<#1>", answer_url="https://x/" + "y" * 100, answer_count=9),
    "recap": build_recap_layout("screened", {"total_q": 999, "total_answered": 500, "total_passed": 499, "rotations": 99, "unique_askers": 400}, payout_line="🪙 +20 to winners • +5 to everyone who played"),
}
every_card = pytest.mark.parametrize("name", list(CARDS))


@every_card
def test_every_card_fits_the_budget_at_its_worst_case(name):
    view = card(CARDS[name])
    check_budget(view)
    # A DM card grows by three components and a line under branding.
    assert view.content_length() <= MAX_CHARS - 100


@every_card
def test_every_card_leads_with_a_glyph_led_heading(name):
    first = next(i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert first.content.startswith("## ")


@every_card
def test_buttons_are_grey_except_the_decision_pair(name):
    buttons = [i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.Button)]
    assert {b.style for b in buttons if b.custom_id not in (APPROVE_ID, REJECT_ID)} <= {discord.ButtonStyle.secondary}


# ── the host panel: Skip, New Hot Seat and End AMA, off the board ────────────


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}"),
        guild=None, guild_id=None, channel=SimpleNamespace(id=100, name="games"),
        message=None,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_ama_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["ama"] == "AMACog"


async def test_games_host_sends_the_panel_and_refuses_a_stranger():
    bot = SimpleNamespace(active_views={}, games_db=None)
    view = ama_cog.AMAView("gid", 1, "unfiltered", None, bot, host_name="Ada")
    bot.active_views["gid"] = view
    cog = ama_cog.AMACog(bot)  # type: ignore[arg-type]

    host = _interaction(1)
    await cog.open_host_panel(host, "gid")  # type: ignore[arg-type]
    panel = host.response.send_message.await_args.kwargs["view"]
    assert isinstance(panel, host_panel.GameHostPanel)
    assert [i.custom_id for i in panel.walk_children() if getattr(i, "custom_id", None)] == [
        HOST_SKIP_ID, HOST_NEW_SEAT_ID, HOST_END_ID,
    ]

    stranger = _interaction(2)
    await cog.open_host_panel(stranger, "gid")  # type: ignore[arg-type]
    assert _said(stranger) == host_panel.NOT_HOST_REPLY

    view._closed = True
    late = _interaction(1)
    await cog.open_host_panel(late, "gid")  # type: ignore[arg-type]
    assert _said(late) == host_panel.GAME_ENDED_REPLY


async def test_seat_controls_answer_plainly_in_panel_format():
    bot = SimpleNamespace(active_views={}, games_db=None)
    view = ama_cog.AMAView("gid", 1, "unfiltered", None, bot, game_format=AMA_FORMAT_PANEL, host_name="Ada")
    for press in (view.skip_hot_seat, view.new_hot_seat):
        interaction = _interaction(1)
        await press(interaction)  # type: ignore[arg-type]
        assert "no hot seat" in _said(interaction)


def test_board_view_binds_its_public_buttons_and_rebuilds_from_state():
    bot = SimpleNamespace(active_views={}, games_db=None)
    view = ama_cog.AMAView("gid", 1, "unfiltered", None, bot, host_name="Ada")
    ids = [i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)]
    assert ids == [HELP_ID, VOLUNTEER_ID, ASK_ID]
    view._hot_seat_name = "Bob"
    view.queue = [7]
    view._rebuild({"questions": [], "total_answered": 0, "total_passed": 0})
    text = "\n".join(i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert "Ask **Bob** anything" in text and "User 7" in text and "Progress" in text


def test_question_view_renders_its_retired_states_from_state():
    guild = SimpleNamespace(get_member=lambda uid: SimpleNamespace(id=uid) if uid == 42 else None)
    view = ama_cog.QuestionView("gid", 42, None, 0, 3, None, "Why?", guild=guild)
    text = lambda: "\n".join(i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay))  # noqa: E731
    assert "For <@42>" in text()
    view._render(state=STATE_PASSED)
    assert text().endswith(f"-# {STATE_PASSED}")
    assert not [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]
    gone = ama_cog.QuestionView("gid", 7, None, 0, 3, None, "Why?", guild=guild)
    assert "For **User 7**" in "\n".join(i.content for i in gone.walk_children() if isinstance(i, discord.ui.TextDisplay))


@pytest.mark.parametrize("game_format", [AMA_FORMAT_HOT_SEAT, AMA_FORMAT_PANEL])
def test_host_panel_handlers_match_the_buttons_drawn(game_format):
    """``GameHostPanel`` binds every handler id and raises on one the card
    lacks — an open-panel AMA's ``/games host`` crashed on exactly that."""
    assert set(host_panel_handlers(game_format)) == set(_ids(build_host_panel_layout(game_format)))


def test_a_volunteer_only_known_users_knows_is_named_and_a_true_miss_reads_user_id():
    """The board's resolver is a ``build_name_fn`` one: a queued member the
    live cache misses reads their ``known_users`` name (the cache-only
    ``member_name`` printed ``User <id>``), and only a true miss falls back."""
    names = GuildNames(guild=None, table_names={5: "Gone_Girl"})
    text = _text(_board(queue=[5, 6], name_fn=names))
    assert "Gone\\_Girl" in text and "User 6" in text and "<@6>" not in text


@pytest.mark.parametrize(
    ("fails", "reattached"),
    [
        pytest.param(False, False, id="good-edit-needs-nothing"),
        # A swallowed 5xx/429: the rebuild had already replaced the buttons and
        # discord.py's store only learns the new ones from a successful edit,
        # so presses stopped routing until the next good one.
        pytest.param(True, True, id="failed-edit-reregisters-the-board"),
    ],
)
async def test_refresh_status_keeps_the_boards_buttons_routable_when_the_edit_fails(
    monkeypatch, fails, reattached,
):
    from unittest.mock import AsyncMock, Mock  # noqa: PLC0415

    monkeypatch.setattr(ama_cog, "get_game_payload", AsyncMock(return_value={"questions": []}))
    bot = SimpleNamespace(active_views={}, games_db=None, add_view=Mock())
    view = ama_cog.AMAView("gid", 1, "unfiltered", None, bot, host_name="Ada")
    edit = AsyncMock(
        side_effect=discord.HTTPException(SimpleNamespace(status=503, reason="down"), "down") if fails else None,
    )
    view._game_msg = SimpleNamespace(guild=None, id=77, edit=edit)  # type: ignore[assignment]

    await view.refresh_status(SimpleNamespace(guild=None))

    assert bot.add_view.called is reattached
    if reattached:
        bot.add_view.assert_called_once_with(view, message_id=77)
