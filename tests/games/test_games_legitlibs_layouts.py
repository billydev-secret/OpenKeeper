"""Tests for ``games/legitlibs/layouts.py`` — the Components V2 cards
that replaced the embed builders in ``rendering.py`` — the ``LibsCard``
view the phases share, and the host panel that took Start and Cancel off
the cards.

Budget is checked at the worst case a round can produce: Classic's
default ``player_max`` is 99, a blank may take a thousand characters and a
template may hold 25 of them, so a story that outgrows a card and a
volunteer roster that outgrows a line are the shapes to check.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.games.legitlibs import layouts, views
from bot_modules.games.legitlibs.layouts import (
    ANOTHER_ID,
    CLASSIC_HELP_ID,
    CLASSIC_SUBMIT_ID,
    FILL_HELP_ID,
    HOST_CANCEL_ID,
    HOST_START_ID,
    JOIN_ID,
    LEAVE_ID,
    LOBBY_HELP_ID,
    RESCUE_SUBMIT_ID,
    SUBMIT_ID,
    VOLUNTEER_ID,
    build_cancelled_layout,
    build_cast_layout,
    build_classic_fill_layout,
    build_classic_reveal_layout,
    build_host_panel_layout,
    build_lobby_layout,
    build_no_submissions_layout,
    build_quiplash_fill_layout,
    build_rescue_claim_layout,
    build_rescue_fill_layout,
    build_reveal_layout,
    fit_names,
)
from bot_modules.games.legitlibs.rendering import render_filled_body
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str]:
    return [
        i.custom_id for i in card(items).walk_children() if getattr(i, "custom_id", None)
    ]


def _escaped(raw: str) -> str:
    return discord.utils.escape_markdown(raw)


# ── the lobby ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "count, said",
    [pytest.param(1, "**1** joined (need 3)", id="short"), pytest.param(3, "**3** joined ✓", id="enough")],
)
def test_lobby_names_the_template_heat_mode_and_the_count_against_the_floor(count, said):
    items = build_lobby_layout("Ada", "A Night Out", 2, "quiplash", count, 3)
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} LegitLibs — Quiplash\nHosted by **Ada**")
    assert '**"A Night Out"**' in text
    assert "Heat: **🌶️🌶️ Spicy** · Mode: **Quiplash**" in text
    assert said in text


def test_lobby_carries_join_leave_help_and_nothing_host_only():
    """Ben, 2026-09-30: Start and Cancel live on the host panel."""
    items = build_lobby_layout("Ada", "T", 1, "classic", 1, 2)
    assert _ids(items) == [LOBBY_HELP_ID, JOIN_ID, LEAVE_ID]
    assert f"-# {host_panel.HOST_HINT}" in _text(items)
    assert _ids(build_lobby_layout("Ada", "T", 1, "classic", 1, 2, closed=True)) == []  # stopped view: a Help here could only fail (ship review, 2026-10-01)


def test_lobby_escapes_and_fits_the_admin_written_title():
    text = _text(build_lobby_layout("*Ada*", "*sneaky* " + "~" * 500, 1, "classic", 1, 2))
    assert "\\*Ada\\*" in text and "\\*sneaky\\*" in text
    assert len(text) < 900


def test_lobby_wears_the_strip_and_drops_its_heading_when_titled():
    items = build_lobby_layout(
        "Ada", "T", 1, "classic", 1, 2,
        header_url="https://x/h.webp", footer_url="https://x/f.webp", title_in_header=True,
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert f"## {layouts.ICON}" not in _text(items)


def test_host_panel_is_start_and_a_red_cancel_on_their_own_ids():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_START_ID, HOST_CANCEL_ID]
    buttons = [i for i in card(items).walk_children() if isinstance(i, discord.ui.Button)]
    assert buttons[0].style == discord.ButtonStyle.secondary
    assert buttons[1].style == discord.ButtonStyle.danger, "Cancel is destructive"
    assert {HOST_START_ID, HOST_CANCEL_ID}.isdisjoint({"ml_start", "ml_cancel_lobby", "ml_cancel_fill"})


# ── the fill cards ───────────────────────────────────────────────────────────


def test_quiplash_fill_shows_the_redacted_story_counter_and_countdown():
    items = build_quiplash_fill_layout("T", 3, 4, 1, 1_700_000_000, "I once **`[1]`** a **`[2]`**.")
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} LegitLibs — Fill Phase\nPress **📝 Submit Fills**")
    assert "Submitted: **1 / 4** · ⏳ <t:1700000000:R>" in text
    assert "I once **`[1]`** a **`[2]`**." in text
    assert _ids(items) == [FILL_HELP_ID, SUBMIT_ID]
    assert f"-# {host_panel.HOST_HINT}" in text


def test_closed_fill_card_keeps_the_count_and_drops_the_countdown_and_controls():
    items = build_quiplash_fill_layout("T", 3, 4, 4, 1_700_000_000, "body", closed=True)
    assert _ids(items) == []
    text = _text(items)
    assert "Submitted: **4 / 4**" in text and "<t:" not in text and "-# " not in text


def test_classic_fill_and_rescue_fill_take_their_own_ids():
    assert _ids(build_classic_fill_layout("T", 1, 3, 1, 1)) == [CLASSIC_HELP_ID, CLASSIC_SUBMIT_ID]
    assert "Players done: **1 / 3**" in _text(build_classic_fill_layout("T", 1, 3, 1, 1))
    assert _ids(build_rescue_fill_layout("T", 1, 0, 2, 1)) == [RESCUE_SUBMIT_ID]
    assert "Rescuers done: **0 / 2**" in _text(build_rescue_fill_layout("T", 1, 0, 2, 1))


@pytest.mark.parametrize("unfilled, said", [(1, "1 blank still"), (3, "3 blanks still")])
def test_rescue_claim_counts_the_unfilled_blanks_and_lists_volunteers(unfilled, said):
    items = build_rescue_claim_layout("T", 4, unfilled, ["Ada", "Bo"], 1)
    text = _text(items)
    assert said in text
    assert "### Volunteers (2)\nAda, Bo" in text
    assert _ids(items) == [VOLUNTEER_ID]
    assert "Volunteers (0)\n—" in _text(build_rescue_claim_layout("T", 4, 1, [], 1))


# ── the volunteer roster: fitted by whole escaped names ──────────────────────
#
# classic.py escapes each display name before handing the list over; that
# escape can double a name, and the embed's field used to be filled with a
# raw character slice that could land inside a ``\X`` escape pair. The
# default template player_max of 99 makes a long roster a live path.


def test_roster_fits_the_budget_with_many_escaped_names():
    names = [_escaped("_" * 32) for _ in range(99)]
    assert len(fit_names(names)) <= layouts.NAMES_BUDGET


def test_roster_never_ends_on_a_dangling_backslash():
    raw = "a" * 1019 + "_" * 50
    value = fit_names([_escaped(raw)], 1024)
    assert not value.rstrip("…").endswith("\\")
    assert value == "_…and 1 more_"


def test_roster_names_are_really_escaped_not_just_truncated():
    value = fit_names([_escaped("**bold**")])
    assert "**bold**" not in value and "bold" in value


def test_roster_drops_whole_names_never_a_partial_one():
    names = [_escaped(f"vol_{i:02d}_{'*' * 20}") for i in range(40)]
    value = fit_names(names)
    roster = next(line for line in value.split("\n") if not line.startswith("_…and "))
    shown = roster.split(", ")
    assert shown
    for name in shown:
        assert name in names, f"entry was not one whole original name: {name!r}"
    assert "more" in value


# ── the reveals ──────────────────────────────────────────────────────────────


def test_quiplash_reveal_is_anonymous_unless_attributed():
    anon = build_reveal_layout("T", 2, "I once **ate** a **shoe**.", 2, 5)
    text = _text(anon)
    assert text.startswith(f"## {layouts.ICON} Submission 2 of 5\nI once **ate** a **shoe**.")
    assert text.endswith("-# 🌶️🌶️ T")
    assert _ids(anon) == []
    named = _text(build_reveal_layout("T", 2, "body", 1, 1, by="*Ada*"))
    assert named.endswith("-# 🌶️🌶️ T • by \\*Ada\\*")


def test_cast_and_no_submissions_and_cancelled_notices():
    assert _text(build_cast_layout(["**#1** — Ada", "**#2** — Bo"])).startswith(
        f"## {layouts.ICON} Who Wrote What\n**#1** — Ada\n**#2** — Bo"
    )
    assert "Nobody filled in the blanks in time." in _text(build_no_submissions_layout("T"))
    assert _text(build_cancelled_layout()).startswith(f"## {layouts.ICON} LegitLibs — Cancelled")


def test_classic_reveal_names_contributors_in_subtext():
    text = _text(build_classic_reveal_layout("T", 4, "body", ["Ada", "*Bo*"]))
    assert text.startswith(f"## {layouts.ICON} T\nbody")
    assert text.endswith("-# Classic • 💀 • Contributors: Ada, \\*Bo\\*")


@pytest.mark.parametrize("builder", [build_reveal_layout, build_classic_reveal_layout])
def test_another_one_rides_only_when_asked(builder):
    kwargs = {"submission_num": 1, "total_submissions": 1} if builder is build_reveal_layout else {"contributor_names": []}
    assert _ids(builder("T", 1, "b", **kwargs)) == []
    assert _ids(builder("T", 1, "b", another=True, **kwargs)) == [ANOTHER_ID]


def test_an_overlong_story_is_cut_at_the_card_never_inside_an_escape():
    """25 thousand-character markdown fills escape past 50,000 characters;
    Quiplash never truncated at all, and Classic cut at Discord's 4,096 —
    over a card's 4,000 either way."""
    body = " ".join("{b%d}" % i for i in range(25))
    fills = {f"b{i}": "_" * 1000 for i in range(25)}
    story = render_filled_body(body, [], fills)
    for items in (
        build_reveal_layout("~" * 300, 1, story, 1, 1, by="*" * 32, another=True),
        build_classic_reveal_layout("~" * 300, 1, story, [_escaped("*" * 32)] * 99, another=True),
    ):
        check_budget(card(items))
        text = _text(items)
        assert "…" in text
        assert not text.split("\n")[1].rstrip("…").endswith("\\")


# ── every card: budget, glyph heading, grey buttons ──────────────────────────


CARDS = {
    "lobby": build_lobby_layout("*" * 32, "~" * 500, 4, "classic", 99, 2, header_url="https://x/h", footer_url="https://x/f"),
    "quiplash-fill": build_quiplash_fill_layout("~" * 500, 4, 12, 3, 1, "x" * 3000),
    "classic-fill": build_classic_fill_layout("~" * 500, 4, 99, 3, 1),
    "rescue-claim": build_rescue_claim_layout("~" * 500, 4, 25, [_escaped("_" * 32)] * 99, 1),
    "rescue-fill": build_rescue_fill_layout("~" * 500, 4, 3, 99, 1),
    "panel": build_host_panel_layout(),
    "cast": build_cast_layout([f"**#{i}** — " + _escaped("*" * 32) for i in range(1, 13)], another=True),
    "no-submissions": build_no_submissions_layout("~" * 500, another=True),
    "cancelled": build_cancelled_layout(),
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


@every_card
def test_buttons_are_grey_except_the_red_cancel(name):
    buttons = [i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.Button)]
    assert {b.style for b in buttons if b.custom_id != HOST_CANCEL_ID} <= {discord.ButtonStyle.secondary}


# ── LibsCard: the one view the phases share ──────────────────────────────────


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}"),
        guild=None, channel=SimpleNamespace(id=100, name="games"),
        message=SimpleNamespace(id=999, edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_legitlibs_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["legitlibs"] == "LegitLibsCog"


async def test_libs_card_binds_what_it_shows_and_rebinds_on_render():
    join = AsyncMock()
    view = views.LibsCard("gid", 1, build_lobby_layout("Ada", "T", 1, "classic", 1, 2), {JOIN_ID: join})
    button = next(i for i in view.walk_children() if getattr(i, "custom_id", None) == JOIN_ID)
    assert button.callback is join
    view.render(build_lobby_layout("Ada", "T", 1, "classic", 2, 2))
    button = next(i for i in view.walk_children() if getattr(i, "custom_id", None) == JOIN_ID)
    assert button.callback is join
    view.render(build_lobby_layout("Ada", "T", 1, "classic", 2, 2, closed=True))
    assert JOIN_ID not in [getattr(i, "custom_id", None) for i in view.walk_children()]


async def test_panel_actions_keep_their_own_gate_and_answer_out_of_phase():
    start, cancel = AsyncMock(), AsyncMock()
    lobby = views.LibsCard("gid", 1, [], {}, actions={"start": start, "cancel": cancel})

    stranger = _interaction(2)
    await lobby.start_game(stranger)  # type: ignore[arg-type]
    assert "Only the host or a mod" in _said(stranger)
    start.assert_not_awaited()

    host = _interaction(1)
    await lobby.start_game(host)  # type: ignore[arg-type]
    start.assert_awaited_once_with(host)

    confirm = _interaction(1)
    await lobby.cancel_round(confirm)  # type: ignore[arg-type]
    assert "Cancel this round?" in _said(confirm)
    assert isinstance(confirm.response.send_message.await_args.kwargs["view"], views._CancelConfirmView)
    cancel.assert_not_awaited()  # nothing destructive is one tap

    fill = views.LibsCard("gid", 1, [], {}, actions={"cancel": cancel})
    late = _interaction(1)
    await fill.start_game(late)  # type: ignore[arg-type]
    assert _said(late) == views.ALREADY_STARTED_REPLY

    reveal = views.LibsCard("gid", 1, [], {})
    over = _interaction(1)
    await reveal.cancel_round(over)  # type: ignore[arg-type]
    assert _said(over) == views.NOTHING_TO_CANCEL_REPLY


async def test_a_closed_phase_can_no_longer_cancel_the_round():
    """The fill card closing used to only ``stop()`` it, leaving Cancel wired
    while the view sat in ``active_views`` — the host panel could cancel
    mid-reveal and wipe the room's payout (ship review, 2026-10-01)."""
    cancel = AsyncMock()
    fill = views.LibsCard("gid", 1, [], {}, actions={"start": AsyncMock(), "cancel": cancel})
    fill.close_phase()
    assert fill.is_finished()

    press = _interaction(1)
    await fill.cancel_round(press)  # type: ignore[arg-type]
    assert _said(press) == views.NOTHING_TO_CANCEL_REPLY
    late = _interaction(1)
    await fill.start_game(late)  # type: ignore[arg-type]
    assert _said(late) == views.ALREADY_STARTED_REPLY
    cancel.assert_not_awaited()


async def test_a_confirm_answered_after_the_phase_closed_cancels_nothing():
    """"Yes, Cancel Round" opened during the fill and pressed during the
    reveal must not reach the cancel action."""
    cancel = AsyncMock()
    fill = views.LibsCard("gid", 1, [], {}, actions={"cancel": cancel})
    opener = _interaction(1)
    await fill.cancel_round(opener)  # type: ignore[arg-type]
    confirm_view = opener.response.send_message.await_args.kwargs["view"]

    fill.close_phase()
    yes = _interaction(1)
    yes.response.edit_message = AsyncMock()
    yes.followup = SimpleNamespace(send=AsyncMock())
    await confirm_view.confirm.callback(yes)

    cancel.assert_not_awaited()
    yes.followup.send.assert_awaited_once_with(views.NOTHING_TO_CANCEL_REPLY, ephemeral=True)


async def test_a_confirm_while_the_phase_is_open_still_cancels():
    cancel = AsyncMock()
    fill = views.LibsCard("gid", 1, [], {}, actions={"cancel": cancel})
    opener = _interaction(1)
    await fill.cancel_round(opener)  # type: ignore[arg-type]
    yes = _interaction(1)
    yes.response.edit_message = AsyncMock()
    await opener.response.send_message.await_args.kwargs["view"].confirm.callback(yes)
    cancel.assert_awaited_once_with(yes)
