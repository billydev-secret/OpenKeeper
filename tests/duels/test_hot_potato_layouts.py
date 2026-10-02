"""Cross-cutting contract for the Hot Potato (duel) Components V2 cards
(Components V2 wave 3b — games). Cog-runtime behavior (``_explode``,
``handle_interaction``) lives in ``tests/duels/test_hot_potato_cog.py``; this
file is the guide's contract-file checklist run once over every card this
feature ships: paradigm shape, budget, no ``Container``, a glyph-led title,
and every member named through ``name_fn`` rather than a raw mention.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.duels.hot_potato.game import HotPotatoGame
from bot_modules.duels.hot_potato.layouts import (
    build_hot_potato_card,
    build_hot_potato_result,
)
from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.duels.filters import validate_stakes


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


def _name_fn(uid: int) -> str:
    return f"User{uid}"


def _game(**overrides) -> HotPotatoGame:
    base: dict = dict(
        id=1, guild_id=9001, channel_id=100, challenger_id=101, target_id=102,
        state="ACTIVE", holder_id=101, winner_id=None, loser_id=None,
        stakes_text=None, nick_stake=False,
        pass_log=[{"holder_id": 101, "received_at": 0.0, "passed_at": None}],
    )
    base.update(overrides)
    return HotPotatoGame(**base)


def _turn_holding():
    return build_hot_potato_card(_game(), _name_fn)


def _turn_boom():
    return build_hot_potato_card(_game(winner_id=102, loser_id=101), _name_fn)


def _turn_custom_stakes():
    return build_hot_potato_card(_game(stakes_text="Loser buys pizza"), _name_fn)


def _result_minimal():
    return build_hot_potato_result(3, "Loser surrenders their nickname for 24 hours.")


def _result_with_style():
    return build_hot_potato_result(
        5, "Loser surrenders their nickname for 24 hours.",
        style_lines=["**🏆 Winner**: +30 pts — now has 174 style points"],
    )


def _result_nick_applied():
    return build_hot_potato_result(
        2, "🏷️ Loser surrenders their nickname for 24 hours.",
        nick_heading="🏷️ Nickname Applied",
        nick_body="**The Loser** is now known as **Spudhead** for 24 hours.",
    )


def _result_fullest():
    return build_hot_potato_result(
        20, "🏷️ Loser surrenders their nickname for 24 hours.",
        style_lines=[
            "**🏆 Winner**: +30 pts — now has 174 style points",
            "**💀 Loser**: +10 pts — now has 40 style points",
        ],
        nick_heading="🏷️ Nickname Applied",
        nick_body="**The Loser** is now known as **Spudhead** for 24 hours.",
    )


ALL_CARDS = [
    pytest.param(_turn_holding, id="turn-holding"),
    pytest.param(_turn_boom, id="turn-boom"),
    pytest.param(_turn_custom_stakes, id="turn-custom-stakes"),
    pytest.param(_result_minimal, id="result-minimal"),
    pytest.param(_result_with_style, id="result-with-style"),
    pytest.param(_result_nick_applied, id="result-nick-applied"),
    pytest.param(_result_fullest, id="result-fullest"),
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
        pytest.param("## 🥔 Hot Potato", False, id="glyph-led-title"),
        pytest.param("### Hot Potato", True, id="section-heading-as-title"),
        pytest.param("## Hot Potato", True, id="no-glyph"),
        pytest.param("Hot Potato", True, id="no-heading"),
    ],
)
def test_the_heading_sweep_can_actually_see_a_violation(heading, faulty):
    """The sweep above is born green; this is what proves it can go red."""
    assert bool(_heading_faults(heading)) is faulty


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_at_mention(build):
    """Every member named on the turn card goes through ``name_fn``; the
    result card names nobody at all — the base's ``_result_layout`` composes
    the ``<@winner> <@loser>`` ping line separately, ahead of these items,
    so a raw mention here would be a duplicate (guide § Mentions)."""
    assert "<@" not in _all_text(build())


# ── Paradigm shape ────────────────────────────────────────────────────


def test_turn_card_is_paradigm_9_pure_content():
    """Prompt only — the base's ``PassLayoutView`` owns the PASS button in
    its own control row, never the mini-game's builder."""
    for build in (_turn_holding, _turn_boom, _turn_custom_stakes):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


def test_result_card_feeds_paradigm_5_content_only():
    """No controls of its own — ``BaseGame._result_layout`` appends the
    Name the Loser / Run It Back button row outside this builder."""
    for build in (_result_minimal, _result_with_style, _result_nick_applied):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


# ── Card-specific spot checks ────────────────────────────────────────


def test_turn_card_holding_names_both_players_and_the_holder():
    text = _all_text(_turn_holding())
    assert "User101" in text and "User102" in text
    assert "holding the potato" in text


def test_turn_card_boom_shows_explosion_not_the_holding_line():
    text = _all_text(_turn_boom())
    assert "BOOM" in text
    assert "exploded" in text
    assert "holding the potato" not in text


def test_turn_card_defaults_stakes_when_none_set():
    assert "Loser surrenders their nickname." in _all_text(_turn_holding())


def test_turn_card_renders_pre_escaped_member_stakes_text_without_doubling():
    """``stakes_text`` reaches the game row already markdown-escaped by
    ``duels.filters.validate_stakes`` at challenge time (the same contract
    ``base_duel.py``'s other stakes_text readers follow) — the builder must
    read it as-is, not escape it a second time (that would turn a single
    backslash into a visible double backslash on the card)."""
    result = validate_stakes("*Loser* buys pizza")
    assert result.ok
    text = _all_text(
        build_hot_potato_card(_game(stakes_text=result.value), _name_fn)
    )
    assert "\\*Loser\\*" in text
    assert "\\\\*Loser\\\\*" not in text
    assert "*Loser*" not in text


def test_turn_card_shows_the_pass_count():
    game = _game(
        pass_log=[
            {"holder_id": 101, "received_at": 0.0, "passed_at": 1.0},
            {"holder_id": 102, "received_at": 1.0, "passed_at": None},
        ]
    )
    text = _all_text(build_hot_potato_card(game, _name_fn))
    assert "Passes: 1" in text


def test_result_card_names_nobody_only_role_words():
    """The ping line above already names both players — this content must
    describe the outcome without re-naming either (guide § Mentions)."""
    text = _all_text(_result_with_style())
    assert "🏆 Winner" in text
    assert "User101" not in text and "User102" not in text


def test_result_card_shows_passes_and_stakes():
    text = _all_text(_result_minimal())
    assert "Passes: 3" in text
    assert "24 hours" in text


def test_result_omits_style_section_when_no_points():
    assert "Style Points" not in _all_text(_result_minimal())


def test_result_includes_style_section_when_points_given():
    text = _all_text(_result_with_style())
    assert "✨ Style Points" in text
    assert "now has 174 style points" in text


def test_result_omits_nick_section_when_none_given():
    text = _all_text(_result_minimal())
    assert "Nickname" not in text


def test_result_nick_applied_section_present():
    text = _all_text(_result_nick_applied())
    assert "🏷️ Nickname Applied" in text
    assert "Spudhead" in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_cards_fit_the_budget():
    for build in (_turn_holding, _turn_boom, _result_fullest):
        view = _view(build())
        check_budget(view)
        components, _chars = layout_budget(view)
        assert components <= MAX_COMPONENTS
