"""``bot_modules/duels/chicken/layouts.py`` — the Components V2 cards.

Pure builders, tested the same way as ``dm_perms``'s and ``casino``'s:
build one, walk the items, assert on the text. Three things beyond the
strings, each with its own section: the **budget** on the fullest real
card, the **paradigm shapes**, and **name_fn** use (plus the one deliberate
exception to "never a raw mention" — see below).
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.duels.chicken.layouts import (
    awaiting_nick_section,
    build_crash_result_layout,
    build_everyone_blinked_layout,
    build_round_layout,
    build_total_wipeout_layout,
    meter_bar,
    nick_applied_section,
    nick_self_apply_section,
)

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731


def _game(
    *,
    alive=(1, 2),
    bail_log=(),
    stakes_text=None,
    climb_started_at=0.0,
    climb_duration=100.0,
):
    return SimpleNamespace(
        alive=list(alive),
        bail_log=list(bail_log),
        stakes_text=stakes_text,
        climb_started_at=climb_started_at,
        climb_duration=climb_duration,
    )


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


#: Every builder, with arguments that exercise its optional branches.
ALL_CARDS = [
    pytest.param(
        lambda: build_round_layout(
            _game(bail_log=[{"player_id": 2, "meter_pct": 30.0}]),
            pct=45.0, name_fn=NAME_FN,
        ),
        id="round-live",
    ),
    pytest.param(
        lambda: build_round_layout(
            _game(alive=[], stakes_text="loser buys the next round"),
            pct=80.0, name_fn=NAME_FN,
        ),
        id="round-custom-stakes",
    ),
    pytest.param(
        lambda: build_crash_result_layout(
            blew_at_pct=62.0, n_alive=2, bravest_pct=75.0,
            stakes="Loser surrenders their nickname for 24 hours.",
            nick_section=None,
        ),
        id="crash-awaiting-nothing",
    ),
    pytest.param(
        lambda: build_crash_result_layout(
            blew_at_pct=62.0, n_alive=1, bravest_pct=75.0,
            stakes="Loser surrenders their nickname for 24 hours.",
            nick_section=awaiting_nick_section(1_750_100_000.0, "24 hours"),
        ),
        id="crash-awaiting-nick",
    ),
    pytest.param(
        lambda: build_crash_result_layout(
            blew_at_pct=100.0, n_alive=3, bravest_pct=None,
            stakes="Loser surrenders their nickname for 24 hours.",
            nick_section=nick_applied_section("Chicken Little", "24 hours"),
        ),
        id="crash-nick-applied",
    ),
    pytest.param(
        lambda: build_crash_result_layout(
            blew_at_pct=50.0, n_alive=2, bravest_pct=10.0,
            stakes="Loser surrenders their nickname for 24 hours.",
            nick_section=nick_self_apply_section("The Boss", "24 hours"),
        ),
        id="crash-self-apply",
    ),
    pytest.param(
        lambda: build_everyone_blinked_layout(
            winner_ref="<@11>", ranking=[("N22", 50.0), ("N33", 20.0)],
        ),
        id="everyone-blinked-ranked",
    ),
    pytest.param(
        lambda: build_everyone_blinked_layout(winner_ref="N11", ranking=[]),
        id="everyone-blinked-solo",
    ),
    pytest.param(
        lambda: build_total_wipeout_layout(blew_at_pct=88.0),
        id="total-wipeout",
    ),
]


# ── Every card renders, fits, and stays bare ────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


def _lead_heading(items) -> str:
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    """``## {glyph} Title``, guide § Anatomy — the sweep dm_perms established."""
    heading = _lead_heading(build())
    assert heading.startswith("## "), f"lead with a `## Title` line: {heading!r}"
    assert len(heading) > 3 and not heading[3].isascii(), (
        f"lead the title with the feature's glyph: {heading!r}"
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container) for item in _view(build()).walk_children()
    )


# One glyph per outcome (guide § Words): ✅/❌/➖ vocabulary plus Chicken's own
# — 🐔 the live board, 💥 a crash (loser or wipeout), 😵 an in-body callout.
_OUTCOME_GLYPHS = {"🐔", "💥"}


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_heading_glyph_is_in_the_outcome_vocabulary(build):
    glyph = _lead_heading(build())[3]
    assert glyph in _OUTCOME_GLYPHS, f"a new outcome glyph: {glyph!r}"


# ── name_fn / no raw mentions, with the one documented exception ────────


@pytest.mark.parametrize(
    "build",
    [c for c in ALL_CARDS if c.id != "everyone-blinked-ranked" and c.id != "everyone-blinked-solo"],
)
def test_no_card_writes_a_raw_mention(build):
    """Every card except the cosmetic winner card never carries a ``<@id>``
    — members are named through ``name_fn`` (round card) or not at all (the
    crash-result and wipeout cards, which never re-name the pinged winner
    or loser — see :func:`build_crash_result_layout`'s docstring)."""
    assert "<@" not in _all_text(build())


def test_round_card_names_holders_and_bailers_through_name_fn():
    text = _all_text(
        build_round_layout(
            _game(alive=[7], bail_log=[{"player_id": 8, "meter_pct": 33.0}]),
            pct=50.0, name_fn=NAME_FN,
        )
    )
    assert "N7" in text
    assert "N8" in text


def test_everyone_blinked_winner_ref_is_the_one_and_only_mention():
    """``winner_ref`` may legitimately be a real ``<@id>`` — the one
    deliberate ping in Chicken, computed by the cog (present member only)
    and handed down as a plain string; see ``ChickenCog.render_result_state``."""
    text = _all_text(build_everyone_blinked_layout(winner_ref="<@11>", ranking=[]))
    assert text.count("<@11>") == 1


def test_everyone_blinked_degrades_to_a_name_when_the_winner_left():
    """A departed winner is named, not pinged — no stray mention either way."""
    text = _all_text(build_everyone_blinked_layout(winner_ref="N11", ranking=[]))
    assert "<@" not in text
    assert "N11" in text


def test_everyone_blinked_ranking_excludes_the_winners_own_row():
    """The caller filters the winner out of ``ranking`` before calling this
    builder — assert the builder doesn't reintroduce them by rendering
    whatever it's handed, so a caller regression would show up as a name
    appearing twice."""
    text = _all_text(
        build_everyone_blinked_layout(winner_ref="<@11>", ranking=[("N22", 40.0)])
    )
    assert text.count("N22") == 1
    assert "N11" not in text


# ── Copy: count down, don't state ────────────────────────────────────────


def test_awaiting_nickname_counts_down_not_states():
    text = awaiting_nick_section(1_750_100_000.0, "24 hours")
    assert "<t:1750100000:R>" in text
    assert "within" not in text.lower()


# ── Escaping: member-typed free text in a TextDisplay ────────────────────


def test_round_card_escapes_stakes_text():
    text = _all_text(
        build_round_layout(_game(stakes_text="*loser* buys drinks"), pct=1.0, name_fn=NAME_FN)
    )
    assert "*loser*" not in text
    assert r"\*loser\*" in text


def test_nick_applied_escapes_the_nickname():
    text = nick_applied_section("*Weasel*", "24 hours")
    assert "*Weasel*" not in text
    assert r"\*Weasel\*" in text


def test_nick_self_apply_escapes_the_nickname():
    text = nick_self_apply_section("*Weasel*", "24 hours")
    assert "*Weasel*" not in text
    assert r"\*Weasel\*" in text


# ── The live climb card ───────────────────────────────────────────────────


def test_live_card_never_promises_a_crash_at_100():
    """The crash point is hidden — the meter fills to 100% of the *drawn*
    span, not to the (unshown) crash point."""
    text = _all_text(build_round_layout(_game(), pct=100.0, name_fn=NAME_FN))
    assert "crash at 100" not in text.lower()
    assert "hidden" in text


def test_meter_bar_holds_one_length_as_it_fills():
    assert len(meter_bar(0.0)) == len(meter_bar(50.0)) == len(meter_bar(100.0))


def test_round_card_falls_back_to_the_default_stakes_line():
    text = _all_text(build_round_layout(_game(stakes_text=None), pct=1.0, name_fn=NAME_FN))
    assert "surrenders their nickname" in text
