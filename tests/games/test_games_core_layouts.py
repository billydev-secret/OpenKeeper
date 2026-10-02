"""Cross-cutting contract for the "Games core" Components V2 cards
(Components V2 wave 3b — shared board plumbing + status, help, session
recap, audit cards). Per-builder behavior lives in
``tests/games/test_games_config_logic.py``, ``tests/games/test_games_help_logic.py``,
``tests/games/test_games_session_logic.py`` and ``tests/games/test_games_audit.py``;
this file is the guide's contract-file checklist run once over every card
this group ships: paradigm shape, budget, no ``Container``, a glyph-led
title, and every member named through ``name_fn`` rather than a raw
mention.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.games.utils.audit import build_audit_notice_layout
from bot_modules.games.config.layouts import (
    build_force_end_layout,
    build_game_status_layout,
)
from bot_modules.games.help.layouts import (
    build_game_detail_layout,
    build_help_layout,
    build_support_layout,
)
from bot_modules.games.session.layouts import build_session_recap_layout


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


_ACTIVE_ROW = {
    "game_type": "wyr", "state": "running", "host_id": 42, "game_id": "g1",
}


def _status_active():
    return build_game_status_layout(_ACTIVE_ROW, name_fn=_name_fn)


def _status_idle():
    return build_game_status_layout(None, name_fn=_name_fn)


def _force_end():
    return build_force_end_layout("wyr")


def _help_overview():
    return build_help_layout()


def _game_detail():
    return build_game_detail_layout("wyr")


def _support():
    return build_support_layout()


def _session_recap():
    return build_session_recap_layout(
        game_count=3,
        player_ids=[1, 2, 3],
        duration_str="30m",
        highlights=["**🤔 Would You Rather**: round 1"],
        name_fn=_name_fn,
    )


def _audit_notice():
    user = SimpleNamespace(id=99, display_name="Sneaky_One", mention="<@99>")
    return build_audit_notice_layout(
        game_type="wyr", user=user, content="a submission", label="Flagged"
    )


ALL_CARDS = [
    pytest.param(_status_active, id="game-status-active"),
    pytest.param(_status_idle, id="game-status-idle"),
    pytest.param(_force_end, id="force-end"),
    pytest.param(_help_overview, id="help-overview"),
    pytest.param(_game_detail, id="game-detail"),
    pytest.param(_support, id="support"),
    pytest.param(_session_recap, id="session-recap"),
    pytest.param(_audit_notice, id="audit-notice"),
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
        pytest.param("## 🎲 Active Game", False, id="glyph-led-title"),
        pytest.param("### Active Game", True, id="section-heading-as-title"),
        pytest.param("## Active Game", True, id="no-glyph"),
        pytest.param("Active Game", True, id="no-heading"),
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
    """Every member named here goes through ``name_fn`` or a plain
    ``Name (`id`)`` pair (the audit notice, deliberately) — never a raw
    ``<@id>`` that renders as digits to a client with no cache hit."""
    assert "<@" not in _all_text(build())


# ── Paradigm shape ────────────────────────────────────────────────────


def test_status_and_force_end_and_audit_are_paradigm_7_receipts():
    """A receipt/notice card carries no interactive controls of its own."""
    for build in (_status_active, _status_idle, _force_end, _audit_notice, _session_recap):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


def test_help_and_detail_are_pure_content_for_paradigm_11_menu():
    """The picker Select and its buttons live in ``GameHelpView``, not in
    the pure builder — a menu-dropdown card's items are only what the view
    wraps in its own ``ActionRow``s alongside these."""
    for build in (_help_overview, _game_detail):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


# ── Card-specific spot checks ────────────────────────────────────────


def test_game_status_active_names_the_host_through_the_resolver():
    text = _all_text(_status_active())
    assert "User42" in text
    assert "`42`" in text


def test_game_status_idle_reads_differently_from_active():
    assert _all_text(_status_idle()) != _all_text(_status_active())


def test_force_end_names_the_game_type():
    assert "wyr" in _all_text(_force_end())


def test_help_overview_lists_the_picked_game():
    text = _all_text(_help_overview())
    assert "Would You Rather" in text


def test_game_detail_names_the_start_command():
    text = _all_text(_game_detail())
    assert "/games play wyr" in text


def test_support_card_includes_the_invite_url(monkeypatch):
    monkeypatch.setenv("SUPPORT_INVITE_URL", "https://discord.gg/example")
    assert "https://discord.gg/example" in _all_text(_support())


def test_session_recap_names_players_through_the_resolver():
    text = _all_text(_session_recap())
    assert "User1" in text and "User2" in text and "User3" in text


def test_audit_notice_keeps_the_raw_id_deliberately():
    """See ``docs/embed_style_guide.md`` "The sites that keep theirs" —
    fixed audience, name-plus-id, never a mention."""
    text = _all_text(_audit_notice())
    assert "Sneaky\\_One (`99`)" in text
    assert "<@99>" not in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_card_fits_the_budget():
    """The help overview (every game, every group, plus the extra-lines
    rooms) is the fullest card this group builds. Measured, not assumed."""
    view = _view(
        build_help_layout(extra_lines=[f"🏈 open room line {i}" for i in range(6)])
    )
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS
