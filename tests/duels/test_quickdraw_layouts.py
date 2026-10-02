"""Contract for the Quickdraw Components V2 cards (wave 3b). Per-hook
delegation, escaping and the no-renaming-the-winner/loser rule live in
``tests/duels/test_quickdraw_cog.py``; this file is the guide's contract-file
checklist run once over every card this feature's pure builders can
produce: paradigm shape, budget, no ``Container`` and a glyph-led title.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.duels.quickdraw.layouts import (
    build_result_items_layout,
    build_turn_card_layout,
    build_void_layout,
)
from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget


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


def _waiting():
    return build_turn_card_layout("WAITING", "Ada", "Bo", "Loser surrenders their nickname.")


def _draw():
    return build_turn_card_layout("DRAW", "Ada", "Bo", "Loser surrenders their nickname.")


def _winner_fired():
    # Renders identically to DRAW — the opponent must stay blind.
    return build_turn_card_layout(
        "WINNER_FIRED", "Ada", "Bo", "Loser surrenders their nickname."
    )


def _void():
    return build_void_layout("Ada", "Bo")


def _result_false_start():
    return build_result_items_layout(
        clean_draw=False, stakes_text="Loser surrenders their nickname."
    )


def _result_clean_draw_both_times():
    return build_result_items_layout(
        clean_draw=True, stakes_text="Loser surrenders their nickname.",
        winner_reaction=0.187, loser_reaction=0.239,
    )


def _result_clean_draw_winner_only():
    return build_result_items_layout(
        clean_draw=True, stakes_text="Loser surrenders their nickname.",
        winner_reaction=0.187, loser_reaction=None,
    )


def _result_with_nick_section():
    return build_result_items_layout(
        clean_draw=True, stakes_text="Loser surrenders their nickname.",
        winner_reaction=0.187, loser_reaction=0.239,
        nick_heading="🏷️ Nickname Applied",
        nick_body="**Bo** is now known as **Sir Slowpoke** for 24 hours.",
    )


ALL_CARDS = [
    pytest.param(_waiting, id="waiting"),
    pytest.param(_draw, id="draw"),
    pytest.param(_winner_fired, id="winner-fired"),
    pytest.param(_void, id="void"),
    pytest.param(_result_false_start, id="result-false-start"),
    pytest.param(_result_clean_draw_both_times, id="result-clean-draw-both-times"),
    pytest.param(_result_clean_draw_winner_only, id="result-clean-draw-winner-only"),
    pytest.param(_result_with_nick_section, id="result-with-nick-section"),
]


# ── Every card renders, fits, and stays bare ─────────────────────────────


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


def _heading_faults(heading: str) -> list[str]:
    """What is wrong with a card's lead line, per guide § Anatomy."""
    if not heading.startswith("## "):
        return [f"a card-level heading is a `## Title` line: {heading!r}"]
    if len(heading) < 4 or heading[3].isascii():
        return [f"lead the title with the feature's glyph: {heading!r}"]
    return []


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize(
    ("heading", "faulty"),
    [
        pytest.param("## 🤠 Quickdraw", False, id="glyph-led-title"),
        pytest.param("### Quickdraw", True, id="section-heading-as-title"),
        pytest.param("## Quickdraw", True, id="no-glyph"),
        pytest.param("Quickdraw", True, id="no-heading"),
    ],
)
def test_the_heading_sweep_can_actually_see_a_violation(heading, faulty):
    """The sweep above is born green; this is what proves it can go red."""
    assert bool(_heading_faults(heading)) is faulty


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container) for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_builder_adds_its_own_controls(build):
    """Every card here is content-only: the turn card's persistent Fire
    button lives in ``QuickdrawLayoutView``'s own control row, appended by
    ``render()`` after these items, never inside a pure builder — and the
    result items land inside the base's ``ResultLayoutView`` the same way."""
    assert not any(
        isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
        for item in _view(build()).walk_children()
    )


# ── Paradigm shape ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "build", [_waiting, _draw, _winner_fired]
)
def test_turn_card_states_are_paradigm_9_game_turn_cards(build):
    """Content only — see ``test_no_builder_adds_its_own_controls`` above
    for why no ``ActionRow`` lives here even though this is the turn card."""
    assert _view(build()).children


@pytest.mark.parametrize("build", [_void])
def test_void_is_a_paradigm_7_receipt(build):
    text = _all_text(build())
    assert "Nobody Drew" in text
    assert "voided" in text.lower()


# ── Card-specific spot checks ────────────────────────────────────────


def test_draw_and_winner_fired_render_identically():
    """The opponent must stay blind to a winner who already fired — their
    press has to be a genuine reaction, not a race against knowledge."""
    assert _all_text(_draw()) == _all_text(_winner_fired())


def test_waiting_names_both_players_and_shows_the_stakes():
    text = _all_text(_waiting())
    assert "Ada" in text and "Bo" in text
    assert "Loser surrenders their nickname." in text


def test_void_names_both_players():
    text = _all_text(_void())
    assert "Ada" in text and "Bo" in text


def test_result_false_start_says_what_happened_without_a_name():
    text = _all_text(_result_false_start())
    assert "False Start" in text
    assert "Ada" not in text and "Bo" not in text


def test_result_clean_draw_shows_both_reaction_times_and_the_margin():
    text = _all_text(_result_clean_draw_both_times())
    assert "0.187s" in text
    assert "0.239s" in text
    assert "0.052s" in text  # margin


def test_result_clean_draw_winner_only_shows_didnt_draw():
    text = _all_text(_result_clean_draw_winner_only())
    assert "0.187s" in text
    assert "didn't draw" in text


def test_result_items_never_carry_a_raw_mention():
    """The ping line is the base's job (``BaseGame._result_layout``) — a
    mini-game's own items never duplicate it."""
    for build in (
        _result_false_start, _result_clean_draw_both_times, _result_clean_draw_winner_only,
    ):
        assert "<@" not in _all_text(build())


def test_result_includes_the_stakes_line():
    assert "Loser surrenders their nickname." in _all_text(_result_false_start())


def test_result_nick_section_is_appended_when_given():
    text = _all_text(_result_with_nick_section())
    assert "Nickname Applied" in text
    assert "Sir Slowpoke" in text


def test_result_omits_the_nick_section_when_not_given():
    text = _all_text(_result_clean_draw_both_times())
    assert "Nickname" not in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_real_card_fits_the_budget():
    """The result card with a nickname section attached is the fullest
    shape this feature builds — measured, not assumed."""
    view = _view(_result_with_nick_section())
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS
