"""Cross-cutting contract for the "Spin the Compliment" Components V2 cards
(Components V2 wave 3b — games). Per-builder behavior and the cog's
send/redraw flow live in ``tests/games/test_games_compliment_logic.py``; this file
is the guide's contract-file checklist run once over every card this feature
ships: paradigm shape, budget, no ``Container``, a glyph-led title, and every
member named through ``name_fn`` rather than a raw mention.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.games.compliment.layouts import (
    build_lobby_layout,
    build_pairings_layout,
    build_wrap_recap_layout,
    format_pairing_line,
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


def _name_fn(uid: int) -> str:
    return f"User{uid}"


def _lobby_empty():
    return build_lobby_layout("Alice", [])


def _lobby_full():
    return build_lobby_layout("Alice", ["Bob", "Carol"], start_at=1_700_000_000)


def _pairings():
    return build_pairings_layout({1: 2, 2: 3, 3: 1}, name_fn=_name_fn, ends_at=1_600)


def _wrap_recap():
    return build_wrap_recap_layout(5, 6, payout_line="🪙 +20 to winners")


ALL_CARDS = [
    pytest.param(_lobby_empty, id="lobby-empty"),
    pytest.param(_lobby_full, id="lobby-full"),
    pytest.param(_pairings, id="pairings"),
    pytest.param(_wrap_recap, id="wrap-recap"),
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
        pytest.param("## 💛 Spin the Compliment", False, id="glyph-led-title"),
        pytest.param("### Spin the Compliment", True, id="section-heading-as-title"),
        pytest.param("## Spin the Compliment", True, id="no-glyph"),
        pytest.param("Spin the Compliment", True, id="no-heading"),
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
    """Every member named here goes through ``name_fn`` (pairings) or is
    escaped plain text (lobby) — never a raw ``<@id>``. The ping itself
    lives in a separate plain ``content`` message the cog sends, not in
    any card."""
    assert "<@" not in _all_text(build())


# ── Paradigm shape ────────────────────────────────────────────────────


def test_pairings_and_wrap_recap_are_paradigm_7_receipts():
    """A receipt card carries no interactive controls of its own — the
    cog appends the lobby's buttons / the recap's Spin Again row outside
    the builder."""
    for build in (_pairings, _wrap_recap):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


def test_lobby_is_pure_content_for_paradigm_8_live_board():
    """The Join/Leave/Close & Generate/Help row lives in ``ComplimentView``,
    not in the pure builder — the whole card is rebuilt and re-sent on
    every pool change, same shape as the auction sticky card."""
    for build in (_lobby_empty, _lobby_full):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


# ── Card-specific spot checks ────────────────────────────────────────


def test_lobby_empty_pool_shows_a_dash():
    text = _all_text(_lobby_empty())
    assert "Pool (0)" in text
    assert "—" in text


def test_lobby_lists_participants_and_the_countdown():
    text = _all_text(_lobby_full())
    assert "Bob" in text and "Carol" in text
    assert "<t:1700000000:R>" in text


def test_lobby_omits_the_countdown_when_none_was_set():
    assert "Starting" not in _all_text(_lobby_empty())


def test_lobby_escapes_markdown_in_the_host_and_participant_names():
    """A Text Display renders markdown where an embed field never did —
    member-set display names must not inject formatting."""
    text = _all_text(build_lobby_layout("*Evil*", ["_Bob_"]))
    assert "*Evil*" not in text
    assert "\\*Evil\\*" in text
    assert "\\_Bob\\_" in text


def test_pairings_lists_every_giver_receiver_line():
    text = _all_text(_pairings())
    for line in ("User1 → User2", "User2 → User3", "User3 → User1"):
        assert line in text


def test_pairings_marks_delivered_givers():
    text = _all_text(
        build_pairings_layout({1: 2, 2: 3}, name_fn=_name_fn, delivered={2})
    )
    lines = [ln for ln in text.splitlines() if "→" in ln]
    assert lines[0] == "User1 → User2"
    assert lines[1] == "✅ User2 → User3"


def test_pairings_shows_the_wrap_countdown_and_how_to_deliver():
    text = _all_text(_pairings())
    assert "<t:1600:R>" in text
    assert "@mention" in text


def test_pairings_defaults_to_a_mention_for_an_unwired_caller():
    """The default keeps an un-wired caller rendering a mention rather than
    crashing — the cog always passes a real ``name_fn``."""
    text = _all_text(build_pairings_layout({1: 2, 2: 1}))
    assert "<@1> → <@2>" in text


def test_format_pairing_line_uses_arrow():
    assert format_pairing_line("<@1>", "<@2>") == "<@1> → <@2>"


def test_format_pairing_line_prefixes_a_check_when_delivered():
    assert format_pairing_line("A", "B", delivered=True) == "✅ A → B"


def test_wrap_recap_shows_the_count_and_the_payout_line():
    text = _all_text(_wrap_recap())
    assert "Wrap-Up" in text
    assert "5" in text and "6" in text
    assert text.splitlines()[-1] == "-# 🪙 +20 to winners"


def test_wrap_recap_omits_the_payout_line_when_nothing_paid():
    text = _all_text(build_wrap_recap_layout(0, 0))
    assert not any(line.startswith("-#") for line in text.splitlines())


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_card_fits_the_budget():
    """The pairings card scales with the pool — a large pool is the
    fullest shape this feature builds. Measured, not assumed."""
    pairings = {i: (i % 20) + 1 for i in range(1, 21)}
    view = _view(
        build_pairings_layout(pairings, name_fn=_name_fn, ends_at=1_600)
    )
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS
