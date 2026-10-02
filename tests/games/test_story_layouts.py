"""Builder tests for the Story Builder Components V2 cards.

Mirrors the pilot's test shape (``docs/components_v2_style_guide.md``
§ Verifying a conversion): render every builder, check the whole-message
budget on the fullest real card, confirm each leads with its title, and
confirm every rendered name goes through the injected ``name_fn`` rather
than a raw id.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.story.layouts import (
    build_lobby_layout,
    build_story_board_layout,
    build_story_recap_layout,
    build_turn_prompt_layout,
)
from bot_modules.games.story.logic import (
    ATTRIBUTION_CARD_BUDGET,
    STORY_TEXT_CARD_BUDGET,
    assemble_story_text,
    build_attribution_lines,
    trim_attribution_lines,
)
from bot_modules.core.layout import check_budget


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _content(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _name_fn(uid: int) -> str:
    return f"Writer{uid}"


# ── build_lobby_layout — paradigm 8 ─────────────────────────────────────


def test_lobby_layout_leads_with_the_glyph_led_game_title():
    view = _view(build_lobby_layout("Host Name", "blind", 10, [], _name_fn))
    content = _content(view)
    assert content.startswith(f"## {GAME_ICONS['story']} {GAME_NAMES['story']}")


def test_lobby_layout_renders_players_through_name_fn():
    view = _view(build_lobby_layout("Host Name", "blind", 10, [111, 222], _name_fn))
    content = _content(view)
    assert "Writer111" in content and "Writer222" in content
    assert "<@111>" not in content and "<@222>" not in content


def test_lobby_layout_shows_an_em_dash_with_no_players():
    view = _view(build_lobby_layout("Host Name", "blind", 10, [], _name_fn))
    assert "Writers (0)" in _content(view)
    assert "—" in _content(view)


def test_lobby_layout_omits_the_countdown_when_none_was_set():
    view = _view(build_lobby_layout("Host Name", "blind", 10, [], _name_fn, start_at=None))
    assert "Starting" not in _content(view)


def test_lobby_layout_renders_the_start_countdown():
    view = _view(build_lobby_layout("Host Name", "blind", 10, [], _name_fn, start_at=1234567890))
    assert "<t:1234567890:R>" in _content(view)


def test_lobby_layout_carries_the_persistent_button_ids():
    view = _view(build_lobby_layout("Host Name", "blind", 10, [], _name_fn))
    ids = {getattr(i, "custom_id", None) for i in view.walk_children()}
    assert {"story_join", "story_leave", "story_start", "story_htp"} <= ids


def test_lobby_layout_builder_adds_no_callbacks():
    for item in build_lobby_layout("Host Name", "blind", 10, [], _name_fn):
        if isinstance(item, discord.ui.ActionRow):
            for button in item.children:
                assert "callback" not in button.__dict__


def test_lobby_layout_escapes_markdown_in_names():
    def hostile_name_fn(uid: int) -> str:
        return "**Bold** Name"

    view = _view(build_lobby_layout("**Bold** Host", "blind", 10, [1], hostile_name_fn))
    content = _content(view)
    assert content.count("\\*\\*Bold\\*\\* Name") == 1  # the Writers list entry
    assert "\\*\\*Bold\\*\\* Host" in content  # the Host line


def test_lobby_layout_stays_in_budget_with_a_full_roster():
    # 30 writers is the max_sentences ceiling, not a player cap — Story has
    # no player cap, so this pins a generously large but plausible roster.
    view = _view(build_lobby_layout(
        "A Very Long Hostname That Takes Up Space", "full", 30,
        list(range(1, 41)), _name_fn, start_at=1234567890,
    ))
    check_budget(view)


# ── build_story_board_layout — paradigm 8 ───────────────────────────────


def test_story_board_layout_leads_with_the_glyph_led_title():
    view = _view(build_story_board_layout(0, 10, 1, [1, 2, 3], _name_fn))
    assert _content(view).startswith(f"## {GAME_ICONS['story']} Story in Progress")


def test_story_board_layout_renders_names_through_name_fn():
    view = _view(build_story_board_layout(0, 10, 1, [1, 2], _name_fn))
    content = _content(view)
    assert "Writer1" in content and "Writer2" in content
    assert "<@1>" not in content and "<@2>" not in content


def test_story_board_layout_marks_the_current_writer_in_turn_order():
    view = _view(build_story_board_layout(0, 10, 2, [1, 2, 3], _name_fn))
    content = _content(view)
    assert "▸ Writer2" in content
    assert "▸ Writer1" not in content and "▸ Writer3" not in content


def test_story_board_layout_shows_sentence_progress():
    view = _view(build_story_board_layout(4, 10, 1, [1], _name_fn))
    assert "5/10" in _content(view)


def test_story_board_layout_escapes_markdown_in_names():
    def hostile_name_fn(uid: int) -> str:
        return "**Bold** Name"

    view = _view(build_story_board_layout(0, 10, 1, [1], hostile_name_fn))
    content = _content(view)
    assert "\\*\\*Bold\\*\\* Name" in content


def test_story_board_layout_stays_in_budget_with_the_max_rotation():
    # 30 sentences is the max game length; the rotation itself can carry
    # every writer who ever joined, dropped ones included until the lap
    # that drops them completes — a generous upper bound, not a real cap.
    view = _view(build_story_board_layout(29, 30, 15, list(range(1, 31)), _name_fn))
    check_budget(view)


# ── build_turn_prompt_layout — paradigm 9 ───────────────────────────────


def test_turn_prompt_layout_names_the_current_writer():
    view = _view(build_turn_prompt_layout(7, _name_fn))
    assert "Writer7" in _content(view)
    assert "<@7>" not in _content(view)


def test_turn_prompt_layout_carries_the_frozen_button_ids_in_one_row():
    items = build_turn_prompt_layout(7, _name_fn)
    rows = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    assert len(rows) == 1
    ids = {b.custom_id for b in rows[0].children}
    assert ids == {"story_write", "story_skip", "story_leave_turn"}


def test_turn_prompt_layout_builder_adds_no_callbacks():
    """Guide § Code conventions: a pure builder returns components, never
    behaviour — the cog wires callbacks after ``add_item``.

    A builder never sets ``callback`` on the ``Button`` instances it
    returns — that would show up as an instance-level override of the
    class default. The cog's own ``_bind`` is what does that, once per
    ``_rebuild`` (this sweep exists on the cog side too, as
    ``test_embed_builder_purity_contract.py``; this is a narrower, in-file
    check on this specific card)."""
    for item in build_turn_prompt_layout(7, _name_fn):
        if isinstance(item, discord.ui.ActionRow):
            for button in item.children:
                assert "callback" not in button.__dict__


# ── build_story_recap_layout — paradigm 7 ───────────────────────────────


def _long_story(n_sentences: int = 30, sentence_len: int = 480) -> list[dict]:
    return [
        {"author_id": (i % 5) + 1 if i else None, "text": "X" * sentence_len}
        for i in range(n_sentences)
    ]


def test_story_recap_layout_leads_with_the_glyph_led_title():
    view = _view(build_story_recap_layout("A tale.", 2, 1, []))
    assert _content(view).startswith(f"## {GAME_ICONS['story']} The Complete Story")


def test_story_recap_layout_shows_the_writer_and_sentence_counts():
    view = _view(build_story_recap_layout("A tale.", 4, 12, []))
    assert "4 writers | 12 sentences" in _content(view)


def test_story_recap_layout_omits_attribution_section_when_empty():
    view = _view(build_story_recap_layout("A tale.", 1, 1, []))
    assert "Who Wrote What" not in _content(view)


def test_story_recap_layout_notes_omitted_attribution_lines():
    view = _view(build_story_recap_layout("A tale.", 2, 2, ["**A:** *hi*"], omitted=5))
    assert "+5 more not shown" in _content(view)


def test_story_recap_layout_omits_the_note_when_nothing_was_trimmed():
    view = _view(build_story_recap_layout("A tale.", 2, 2, ["**A:** *hi*"], omitted=0))
    assert "more not shown" not in _content(view)


def test_story_recap_layout_renders_the_payout_footer_as_a_footer_line():
    view = _view(build_story_recap_layout(
        "A tale.", 2, 2, [], footer_line="🪙 +20 to winners • +5 to everyone who played",
    ))
    assert "-# 🪙 +20 to winners" in _content(view)


def test_story_recap_layout_omits_the_footer_when_payouts_are_off():
    view = _view(build_story_recap_layout("A tale.", 2, 2, [], footer_line=None))
    assert "-#" not in _content(view)


def test_story_recap_layout_stays_in_budget_on_the_fullest_real_story():
    """The fullest card a member actually sees: a 30-sentence story at the
    modal's 500-char max, trimmed by the same logic the cog runs
    (``assemble_story_text`` / ``trim_attribution_lines``) before it ever
    reaches the builder — not an isolated piece measured on its own
    (2026-09-21 wave-3a review: the fullest *combined* card is what must
    stay under budget, not each section in isolation)."""
    sentences = _long_story()
    story_text = assemble_story_text(sentences, max_len=STORY_TEXT_CARD_BUDGET)
    lines = build_attribution_lines(sentences, _name_fn)
    attribution_lines, omitted = trim_attribution_lines(lines, ATTRIBUTION_CARD_BUDGET)

    view = _view(build_story_recap_layout(
        story_text=story_text,
        player_count=5,
        sentence_count=len(sentences),
        attribution_lines=attribution_lines,
        omitted=omitted,
        footer_line="🪙 +20 to winners • +5 to everyone who played",
    ))
    check_budget(view)


# ── logic.trim_attribution_lines ────────────────────────────────────────


def test_trim_attribution_lines_keeps_everything_under_budget():
    lines = ["short one", "short two", "short three"]
    kept, omitted = trim_attribution_lines(lines, max_len=1000)
    assert kept == lines
    assert omitted == 0


def test_trim_attribution_lines_drops_whole_lines_past_the_budget():
    lines = ["a" * 100, "b" * 100, "c" * 100]
    kept, omitted = trim_attribution_lines(lines, max_len=150)
    assert kept == ["a" * 100]
    assert omitted == 2


def test_trim_attribution_lines_never_cuts_a_line_mid_string():
    lines = ["a" * 100, "b" * 100]
    kept, _omitted = trim_attribution_lines(lines, max_len=150)
    for line in kept:
        assert line in lines  # never a partial/truncated variant


@pytest.mark.parametrize(
    "lines, max_len, expected_kept, expected_omitted",
    [
        pytest.param([], 100, [], 0, id="empty"),
        pytest.param(["x"], 0, [], 1, id="budget-too-small-for-one-line"),
    ],
)
def test_trim_attribution_lines_edge_cases(lines, max_len, expected_kept, expected_omitted):
    kept, omitted = trim_attribution_lines(lines, max_len=max_len)
    assert kept == expected_kept
    assert omitted == expected_omitted
