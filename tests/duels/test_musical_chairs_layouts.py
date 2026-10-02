"""Cross-cutting contract for the Musical Chairs Components V2 cards
(Components V2 wave 3b — games). Cog-runtime wiring lives in
``tests/duels/test_musical_chairs_cog.py``; the control-row view lives in
``tests/duels/test_musical_chairs_views.py``. This file is the guide's
contract-file checklist run once over every card this feature ships:
paradigm shape, budget, no ``Container``, a glyph-led title, escaped member
text, and no re-naming of the winner/loser on the result card.
"""

from __future__ import annotations

import time

import discord
import pytest

from bot_modules.duels.musical_chairs.layouts import build_result_layout, build_round_layout
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


def _name_fn(uid: int) -> str:
    return f"User{uid}"


def _round_music():
    return build_round_layout(
        phase="MUSIC", round_no=2, chairs=3, alive=[1, 2, 3, 4],
        stakes="🏷️ Loser surrenders their nickname for 24 hours.", name_fn=_name_fn,
    )


def _round_scramble():
    return build_round_layout(
        phase="SCRAMBLE", round_no=2, chairs=2, alive=[1, 2, 3],
        stakes="🏷️ Loser surrenders their nickname for 24 hours.", name_fn=_name_fn,
    )


def _result_imposed():
    return build_result_layout(
        players=4, stakes="🏷️ Loser surrenders their nickname for 24 hours.",
        nick_stake=True, imposed_nick="Chair Thief", self_apply_nick=None,
        nick_span="24 hours", naming_deadline=1_700_001_800,
    )


def _result_self_apply():
    return build_result_layout(
        players=4, stakes="🏷️ Loser surrenders their nickname for 24 hours.",
        nick_stake=True, imposed_nick=None, self_apply_nick="Chair Thief",
        nick_span="24 hours", naming_deadline=1_700_001_800,
    )


def _result_awaiting():
    return build_result_layout(
        players=4, stakes="🏷️ Loser surrenders their nickname for 24 hours.",
        nick_stake=True, imposed_nick=None, self_apply_nick=None,
        nick_span="24 hours", naming_deadline=1_700_001_800,
    )


def _result_stakes_only():
    return build_result_layout(
        players=4, stakes="💰 🪙 **50** coins each — winner takes 🪙 **200** coins.",
        nick_stake=False, imposed_nick=None, self_apply_nick=None,
        nick_span="24 hours", naming_deadline=1_700_001_800,
    )


ALL_CARDS = [
    pytest.param(_round_music, id="round-music"),
    pytest.param(_round_scramble, id="round-scramble"),
    pytest.param(_result_imposed, id="result-imposed"),
    pytest.param(_result_self_apply, id="result-self-apply"),
    pytest.param(_result_awaiting, id="result-awaiting"),
    pytest.param(_result_stakes_only, id="result-stakes-only"),
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


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_carries_interactive_controls(build):
    """Paradigm 9 (round) and paradigm 7 (result) both carry no controls of
    their own — the cog's persistent SitView / BaseGame's result buttons
    supply them outside the builder."""
    assert not any(
        isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
        for item in _view(build()).walk_children()
    )


# ── Mentions: the result card never re-names the winner/loser ──────────────


@pytest.mark.parametrize(
    "build", [_result_imposed, _result_self_apply, _result_awaiting, _result_stakes_only]
)
def test_result_card_never_renames_a_player(build):
    """BaseGame._result_layout's ping line already named both players; this
    card must describe the outcome without a second name for either one —
    no resolved display name and no raw mention appears here."""
    text = _all_text(build())
    assert "<@" not in text


def test_round_card_names_players_through_name_fn_not_a_raw_mention():
    text = _all_text(_round_music())
    assert "User1" in text and "<@1>" not in text


def test_round_card_defaults_to_a_mention_for_an_unwired_caller():
    items = build_round_layout(phase="MUSIC", round_no=1, chairs=2, alive=[9], stakes="s")
    text = _all_text(items)
    assert "<@9>" in text


# ── Paradigm-specific spot checks ───────────────────────────────────────


def test_scramble_card_says_sit_now_and_names_the_chair_count():
    text = _all_text(_round_scramble())
    assert "press 🪑 Sit now" in text
    assert "**2**" in text


def test_music_card_warns_against_sitting_early():
    text = _all_text(_round_music())
    assert "don't sit yet" in text


def test_round_card_with_nobody_alive_shows_a_dash():
    text = _all_text(
        build_round_layout(phase="MUSIC", round_no=1, chairs=0, alive=[], stakes="s")
    )
    assert "—" in text


def test_result_card_escapes_a_member_typed_imposed_nickname():
    text = _all_text(build_result_layout(
        players=2, stakes="s", nick_stake=True, imposed_nick="*Evil*",
        self_apply_nick=None, nick_span="24 hours", naming_deadline=1_700_001_800,
    ))
    assert "*Evil*" not in text
    assert "\\*Evil\\*" in text


def test_result_card_escapes_a_member_typed_self_apply_nickname():
    text = _all_text(build_result_layout(
        players=2, stakes="s", nick_stake=True, imposed_nick=None,
        self_apply_nick="_Sneaky_", nick_span="24 hours", naming_deadline=1_700_001_800,
    ))
    assert "_Sneaky_" not in text
    assert "\\_Sneaky\\_" in text


def test_result_card_self_apply_explains_the_owner_workaround():
    text = _all_text(_result_self_apply())
    assert "won't let me rename the server owner" in text
    assert "Chair Thief" in text


def test_result_card_awaiting_nickname_counts_down_not_states():
    """Guide § Words: count down, don't state — a static "within 30 minutes"
    goes stale the moment it's read; a live <t:...:R> never does."""
    text = _all_text(_result_awaiting())
    assert "<t:1700001800:R>" in text


def test_result_card_stakes_only_has_no_nickname_block():
    text = _all_text(_result_stakes_only())
    assert "🏷️" not in text
    assert "⏳" not in text


def test_result_card_states_the_configured_sentence_span():
    """The nickname duration always comes from the caller-computed
    ``nick_span`` (BaseGame._hours_span, dial-aware) — never a literal."""
    text = _all_text(build_result_layout(
        players=2, stakes="s", nick_stake=True, imposed_nick="X",
        self_apply_nick=None, nick_span="48 hours", naming_deadline=1_700_001_800,
    ))
    assert "48 hours" in text
    assert "24 hours" not in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_card_fits_the_budget():
    """A round card scales with the roster — a large game is the fullest
    shape this feature builds. Measured, not assumed."""
    alive = list(range(1, 21))
    view = _view(build_round_layout(
        phase="MUSIC", round_no=9, chairs=19, alive=alive,
        stakes="🏷️ Loser surrenders their nickname for 24 hours.", name_fn=_name_fn,
    ))
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS


def test_naming_deadline_is_a_real_future_timestamp_shape():
    """A light sanity check on the sample data every other test in this file
    reuses: a plausible unix timestamp, not an accidental duration/seconds
    value passed where a deadline was expected."""
    assert 1_700_001_800 > time.time() - 86400 * 3650  # not decades in the past
