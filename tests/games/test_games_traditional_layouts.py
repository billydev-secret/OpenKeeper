"""Tests for ``bot_modules/games/traditional/layouts.py`` — the Components V2
cards that replaced ``games_traditional/embeds.py``.

The builders are pure: they take dicts and primitives and return
``discord.ui.Item`` lists, so every card is asserted on its text with no
Discord mocks. Budget is checked against the worst case the feature can
actually produce — Truth or Dare carries no player cap, so a full-channel
room is the shape to check, not a token handful of players.
"""

from __future__ import annotations

import inspect
import pathlib
from typing import Any

import discord
import pytest

import bot_modules.games.traditional.cog as traditional_cog
from bot_modules.core.layout import card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.traditional import layouts
from bot_modules.games.traditional.layouts import (
    ASK_ID,
    END_ID,
    HELP_ID,
    NSFW_DARE_ID,
    NSFW_TRUTH_ID,
    SFW_DARE_ID,
    SFW_TRUTH_ID,
    WRITE_OWN_ID,
    build_host_panel_layout,
    build_question_layout,
    build_recap_layout,
    build_room_layout,
    question_mentions,
)
from bot_modules.games.traditional.logic import CATEGORIES


def _view(items: list[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in _view(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _button_ids(items: list[discord.ui.Item[Any]]) -> list[str | None]:
    return [
        b.custom_id for b in _view(items).walk_children()
        if isinstance(b, discord.ui.Button)
    ]


# ── the room board: join screen ─────────────────────────────────────────────


def test_join_screen_shows_the_prompt_and_no_questions_asked_field():
    """Finding 8 (2026-09-03 embed filler review): a fresh room's question
    count can only ever be zero, so the field is dropped entirely rather than
    printed as a proud zero."""
    text = _text(build_room_layout("Alice", {}, lobby=True))
    assert layouts.JOIN_HINT in text
    assert "**0** playing" in text
    assert "asked" not in text


def test_join_screen_single_choice_changes_the_prompt():
    text = _text(build_room_layout("Alice", {}, lobby=True, single_choice=True))
    assert layouts.JOIN_HINT_SINGLE in text
    assert layouts.JOIN_HINT not in text


@pytest.mark.parametrize("lobby", [True, False])
def test_board_carries_help_and_the_joins_and_nothing_host_only(lobby):
    """Ben, 2026-09-29: "move everything that's host only to an ephemeral
    message brought up by slash command". Help is for everyone and stays,
    beside the opening text; Ask / Write My Own / End Game are on the host
    panel. Reverting puts host-only buttons back in front of the room."""
    items = build_room_layout("Alice", {"prefs": {"1": ["sfw_truth"]}}, lobby=lobby)
    assert _button_ids(items) == [
        HELP_ID, SFW_TRUTH_ID, SFW_DARE_ID, NSFW_TRUTH_ID, NSFW_DARE_ID,
    ]
    intro = next(i for i in items if isinstance(i, discord.ui.Section))
    assert intro.accessory.custom_id == HELP_ID


@pytest.mark.parametrize("lobby", [True, False])
def test_board_points_the_host_at_their_controls(lobby):
    """A game started from /games help or the scheduler never hands the
    host their panel, and nothing host-only is on the board — this line is
    the only way such a host finds Ask Question and End Game."""
    text = _text(build_room_layout("Alice", {}, lobby=lobby))
    assert f"-# {layouts.HOST_HINT}" in text
    assert host_panel.HOST_COMMAND in layouts.HOST_HINT


def test_host_panel_carries_the_three_host_controls_all_grey():
    items = build_host_panel_layout()
    assert _button_ids(items) == [ASK_ID, WRITE_OWN_ID, END_ID]
    buttons = [b for b in _view(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert [b.label for b in buttons] == ["Ask Question", "Write My Own", "End Game"]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    assert host_panel.HOST_COMMAND in _text(items)


def test_host_panel_ids_are_not_the_boards_persistent_ids():
    """The panel is a short-lived ephemeral view; reusing the old board ids
    would let a stale pre-change board's buttons route into it."""
    assert {ASK_ID, WRITE_OWN_ID, END_ID}.isdisjoint({"tod_ask", "tod_write_own", "tod_end"})


def test_host_panel_stays_in_budget():
    check_budget(card(build_host_panel_layout()))


def test_every_button_is_grey():
    """Style guide § Buttons: grey unless the color means something. The
    embed-era red NSFW pair read as destructive and the blurple SFW pair
    ranked one over the other."""
    buttons = [
        b for b in _view(build_room_layout("Alice", {}, lobby=True)).walk_children()
        if isinstance(b, discord.ui.Button)
    ]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}


def test_each_category_is_its_own_row_with_join_beside_it():
    """Ben, 2026-09-29: "split up the categories and put a button by each
    one" — a Section per category, its glyph and name over who's in it,
    its toggle as the accessory."""
    payload = {"prefs": {"7": ["nsfw_truth"]}}
    items = build_room_layout("Alice", payload, lobby=False, names={"7": "Bee"})
    sections = [
        i for i in items
        if isinstance(i, discord.ui.Section) and i.accessory.custom_id != HELP_ID
    ]
    assert [s.accessory.custom_id for s in sections] == [
        SFW_TRUTH_ID, SFW_DARE_ID, NSFW_TRUTH_ID, NSFW_DARE_ID,
    ]
    assert {s.accessory.label for s in sections} == {layouts.JOIN_LABEL}
    texts = [s.children[0].content for s in sections]
    assert texts[2] == "💋 **NSFW Truth** · 1\nBee"
    assert texts[0] == "💭 **SFW Truth**\n-# Nobody yet"


def test_the_only_line_among_the_categories_splits_sfw_from_nsfw():
    """Design review 2026-09-29: the card's one real split is SFW against
    the 18+ pair, so that is the only line drawn among the rows — a line
    between every row drew each pair's halves as far apart as the pairs."""
    items = build_room_layout("Alice", {}, lobby=True)
    first_row = next(
        k for k, i in enumerate(items)
        if isinstance(i, discord.ui.Section) and i.accessory.custom_id == SFW_TRUTH_ID
    )
    shape = [
        i.accessory.custom_id if isinstance(i, discord.ui.Section) else "line"
        for i in items[first_row:]
        if isinstance(i, (discord.ui.Section, discord.ui.Separator))
    ]
    assert shape == [SFW_TRUTH_ID, SFW_DARE_ID, "line", NSFW_TRUTH_ID, NSFW_DARE_ID]
    split = next(i for i in items[first_row:] if isinstance(i, discord.ui.Separator))
    assert split.spacing is discord.SeparatorSpacing.large and split.visible


def test_help_row_keeps_to_short_lines_and_the_hint_goes_full_width():
    """A row beside a button keeps about 30 columns on a phone: every fact
    beside Help is its own short line, and the join hint sits under the
    row in subtext rather than wrapping ragged beside the button."""
    from bot_modules.core.layout import text_columns

    payload = {"participants": [1, 2], "prefs": {"1": ["sfw_truth"]}, "asked": {"1:sfw_truth": "q"}}
    items = build_room_layout("Ada Sample", payload, lobby=False, title_in_header=True, header_url="h")
    intro = next(i for i in items if isinstance(i, discord.ui.Section))
    lines = intro.children[0].content.split("\n")
    assert all(text_columns(line.replace("**", "")) <= 30 for line in lines)
    assert layouts.JOIN_HINT not in intro.children[0].content
    assert f"-# {layouts.JOIN_HINT}" in _text(items)


def test_join_label_fits_beside_its_row_on_a_phone():
    from bot_modules.core.layout import MAX_ACCESSORY_LABEL, text_columns

    assert text_columns(layouts.JOIN_LABEL) <= MAX_ACCESSORY_LABEL


def test_the_game_glyph_is_not_risky_rolls_dice():
    """Deliberate: ❓ replaced 🎲 in the V2 pass because Risky Rolls leads
    with the dice in the same age-gated channels."""
    from bot_modules.games.risky_roll.layouts import ICON as RISKY_ICON

    assert layouts.ICON == "❓"
    assert layouts.ICON != RISKY_ICON


# ── the room board: working board ───────────────────────────────────────────


def test_working_board_shows_host_participants_and_progress():
    payload = {
        "participants": [1, 2],
        "prefs": {"1": ["sfw_truth", "sfw_dare"], "2": ["nsfw_dare"]},
        "asked": {"1:sfw_truth": "Q"},
    }
    text = _text(build_room_layout("Alice", payload, lobby=False))
    assert "Hosted by **Alice**\n**2** playing\n**1 of 3** questions asked" in text
    # The join hint stays once play starts — people join mid-game, and it
    # is the only place the card says a second press drops you.
    assert layouts.JOIN_HINT in text


def test_working_board_drops_a_leftover_pass_key_from_the_progress_count():
    """A payload still carrying ``pass`` (removed 2026-09-20) must not double
    the denominator — the progress line counts the one sweep only."""
    payload = {
        "pass": 2,
        "participants": [1],
        "prefs": {"1": ["sfw_truth"]},
        "asked": {"1:sfw_truth": "q"},
    }
    text = _text(build_room_layout("Host", payload, lobby=False))
    assert "**1 of 1** questions asked" in text


def test_empty_category_reads_nobody_yet_not_a_bare_dash():
    """Finding 2: a placeholder is not an empty state."""
    text = _text(build_room_layout("Alice", {}, lobby=False))
    assert text.count("\n-# Nobody yet") == 4
    assert "—" not in text.split("\n", 1)[1]  # body text never borrows the heading dash


def test_populated_category_lists_resolved_names_not_ids():
    import re

    payload = {"prefs": {"7": ["sfw_truth"]}}
    text = _text(build_room_layout("Alice", payload, lobby=False, names={"7": "Bee"}))
    assert "Bee" in text
    assert not re.search(r"\b7\b", text)  # the bare uid appears nowhere


@pytest.mark.parametrize("names", [None, {"8": "Cy"}])
def test_uncached_member_reads_as_user_id_never_a_bare_number(names):
    """The name resolver's fallback, not the raw uid the embed printed."""
    payload = {"prefs": {"7": ["sfw_truth"]}}
    text = _text(build_room_layout("Alice", payload, lobby=False, names=names))
    assert "💭 **SFW Truth** · 1\nUser 7" in text


@pytest.mark.parametrize("lobby", [True, False])
def test_single_choice_hint_rides_on_both_states_once(lobby):
    """One sentence says the rule on the join screen and the working board
    alike — no separate ``-#`` line repeating it (finding 1)."""
    text = _text(build_room_layout("Alice", {}, lobby=lobby, single_choice=True))
    assert text.count(layouts.JOIN_HINT_SINGLE) == 1
    assert "One category each" not in text


# ── the room board: art ──────────────────────────────────────────────────────


def test_no_art_by_default():
    text = _text(build_room_layout("Alice", {}, lobby=True))
    assert f"{layouts.ICON} {layouts.TITLE}" in text
    view = _view(build_room_layout("Alice", {}, lobby=True))
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in view.walk_children())


def test_the_lobby_strip_draws_the_media_gallery_and_keeps_the_heading():
    items = build_room_layout("Alice", {}, lobby=True, header_url="https://x/h.png")
    view = _view(items)
    galleries = [i for i in view.walk_children() if isinstance(i, discord.ui.MediaGallery)]
    assert len(galleries) == 1
    assert f"## {layouts.ICON} {layouts.TITLE}" in _text(items)


def test_a_titled_strip_drops_the_heading_line():
    """The strip with the game's name drawn between its leaves replaces the
    ``##`` line, and carries the name as its alt text."""
    items = build_room_layout(
        "Alice", {}, lobby=True,
        header_url="https://x/h.png", title_in_header=True,
    )
    assert f"## {layouts.ICON} {layouts.TITLE}" not in _text(items)
    gallery = items[0]
    assert isinstance(gallery, discord.ui.MediaGallery)
    assert gallery.items[0].description == layouts.TITLE


def test_strip_and_footer_frame_the_board():
    """Ben, 2026-09-29: "switch truth or dare to the small header" — the
    board wears the lobby strip every other game does, not the tall hub
    banner it wore for a day. A footer without a strip draws nothing, as
    on every lobby."""
    items = build_room_layout(
        "Alice", {}, lobby=True,
        header_url="https://x/h.png", footer_url="https://x/f.png",
    )
    assert items[0].items[0].media.url == "https://x/h.png"  # type: ignore[attr-defined]
    assert items[-1].items[0].media.url == "https://x/f.png"  # type: ignore[attr-defined]
    footer_only = build_room_layout("Alice", {}, lobby=True, footer_url="https://x/f.png")
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in footer_only)


def test_the_board_never_wears_the_tall_hub_banner():
    """Reverting to the banner would put a hub's art on a lobby again."""
    src = inspect.getsource(layouts.build_room_layout)
    assert "hub_banner" not in src and "lobby_art(" in src


# ── the question card ────────────────────────────────────────────────────────


def test_question_card_pings_a_cached_target():
    text = _text(build_question_layout("sfw_truth", "Q?", target_id=42))
    assert "<@42>" in text


def test_question_card_names_an_uncached_target_without_pinging():
    text = _text(build_question_layout("sfw_truth", "Q?", target_name="Bee"))
    assert "Bee" in text
    assert "<@" not in text


def test_question_card_names_the_target_exactly_once():
    """Style guide § Mentions and names: never both a ping and a resolved
    name on the same card."""
    text = _text(build_question_layout("sfw_truth", "Q?", target_id=42, target_name="Bee"))
    assert text.count("For ") == 1


def test_question_card_uses_the_category_label_and_emoji():
    text = _text(build_question_layout("nsfw_dare", "Q?"))
    assert "NSFW Dare" in text
    assert layouts.CAT_EMOJI["nsfw_dare"] in text


def test_question_card_falls_back_on_an_unknown_category():
    text = _text(build_question_layout("legacy_cat", "Q?"))
    assert "Legacy Cat" in text


def test_question_card_preserves_markdown():
    text = _text(build_question_layout("sfw_truth", "*emphasised*"))
    assert "*emphasised*" in text


def test_question_card_keeps_multiline_inside_the_blockquote():
    text = _text(build_question_layout("sfw_truth", "line one\nline two"))
    assert "> line one" in text
    assert "> line two" in text


def test_question_card_has_no_controls():
    """Paradigm 7 — a receipt/notice, not a form: the host answers through
    the modal, not this card."""
    view = _view(build_question_layout("sfw_truth", "Q?"))
    assert not any(isinstance(i, discord.ui.Button) for i in view.walk_children())


# ── question_mentions: the allow-list ───────────────────────────────────────


def test_question_mentions_allows_only_the_target():
    mentions = question_mentions(42)
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    assert [u.id for u in mentions.users] == [42]


def test_question_mentions_allows_nobody_with_no_target():
    mentions = question_mentions(None)
    assert mentions.users == [] or not mentions.users


# ── the recap ────────────────────────────────────────────────────────────────


def test_recap_shows_totals():
    payload = {
        "participants": [1, 2, 3],
        "asked": {"1:sfw_truth": "Q", "2:sfw_dare": "Q"},
    }
    text = _text(build_recap_layout(payload))
    assert "**2** asked · **3** played" in text
    assert "Game Over" in text


def test_recap_only_shows_nonzero_category_breakdowns():
    payload = {
        "participants": [1, 2],
        "asked": {"1:sfw_truth": "Q", "2:sfw_truth": "Q"},
    }
    text = _text(build_recap_layout(payload))
    assert text.endswith("💭 **2**")
    for glyph in ("🔥", "💋", "😈"):
        assert glyph not in text


def test_recap_with_zero_asked():
    text = _text(build_recap_layout({"participants": [], "asked": {}}))
    assert text.endswith("**0** asked · **0** played")


def test_recap_carries_the_idle_note():
    text = _text(build_recap_layout({"participants": [], "asked": {}}, note="⌛ wrapped up"))
    assert "⌛ wrapped up" in text


def test_recap_names_the_bank_not_a_removed_button():
    """The 🎲 Bank Round button was removed 2026-09-20; the counter outlived
    it because an unedited Ask Question pre-fill feeds the same number."""
    text = _text(build_recap_layout(
        {"participants": [1], "asked": {"1:sfw_truth": "q"}, "bank_asked": 2}
    ))
    assert "**2** from the bank" in text
    assert "Round" not in text


def test_recap_omits_the_bank_line_when_nothing_came_from_it():
    text = _text(build_recap_layout({"participants": [], "asked": {}}))
    assert "from the bank" not in text


def test_recap_carries_the_payout_line_when_given():
    text = _text(build_recap_layout({"participants": [], "asked": {}}, payout_line="🪙 +5 to everyone"))
    assert "+5 to everyone" in text


def test_recap_omits_the_payout_line_when_none():
    text = _text(build_recap_layout({"participants": [], "asked": {}}, payout_line=None))
    assert "🪙" not in text


def test_recap_has_no_controls():
    view = _view(build_recap_layout({"participants": [], "asked": {}}))
    assert not any(isinstance(i, discord.ui.Button) for i in view.walk_children())


# ── budget: the worst case this feature can actually produce ───────────────


def test_room_board_stays_in_budget_at_a_full_channel_with_long_names_and_art():
    """Truth or Dare carries no player cap (unlike Compliment/MFK), so a
    full-channel room with long names in every category — plus the art and
    the single-choice line, every optional slot filled at once — is the real
    ceiling to check, not a token handful of players."""
    long_name = "A" * 32
    names = {str(uid): f"{long_name}{uid}" for uid in range(60)}
    prefs = {str(uid): list(CATEGORIES) for uid in range(60)}
    asked = {f"{uid}:{cat}": "Q" for uid in range(30) for cat in CATEGORIES}
    payload = {"participants": list(range(60)), "prefs": prefs, "asked": asked}
    items = build_room_layout(
        "A Very Long Host Display Name" * 2, payload, lobby=False,
        names=names, single_choice=True,
        header_url="https://x/h.png", footer_url="https://x/f.png",
    )
    check_budget(card(items))


def test_room_board_stays_in_budget_on_the_bare_join_screen():
    check_budget(card(build_room_layout("Host", {}, lobby=True)))


def test_question_card_stays_in_budget_with_the_longest_question():
    """500 is the modal's own ``max_length`` — the longest question this
    feature can ever produce."""
    items = build_question_layout("nsfw_dare", "Q" * 500, target_name="A Very Long Name")
    check_budget(card(items))


def test_recap_stays_in_budget_with_every_category_and_a_payout_line():
    payload = {
        "participants": list(range(60)),
        "asked": {f"{uid}:{cat}": "Q" for uid in range(60) for cat in CATEGORIES},
        "bank_asked": 40,
    }
    items = build_recap_layout(
        payload, note="⌛ Closed after 60 quiet minutes.",
        payout_line="🪙 +20 to winners • +5 to everyone who played",
    )
    check_budget(card(items))


# ── the cog never hand-rolls a mention ──────────────────────────────────────


def test_cog_never_hand_rolls_a_mention_string():
    """The one ``<@id>`` this feature ever renders is ``build_question_layout``'s
    own, built from a resolved member id the cog hands it — the cog itself
    must never format a ping directly, which is exactly the shape that would
    silently duplicate (and could drift from) the builder's one rule of
    naming a target exactly once."""
    src = pathlib.Path(inspect.getfile(traditional_cog)).read_text(encoding="utf-8")
    assert "<@" not in src


def test_lobby_and_recap_and_question_cards_are_layoutviews_with_no_container():
    """Sanity check alongside the repo-wide sweep (test_layout_no_container.py):
    every card this feature builds is a bare LayoutView."""
    for items in (
        build_room_layout("Host", {}, lobby=True),
        build_question_layout("sfw_truth", "Q?"),
        build_recap_layout({"participants": [], "asked": {}}),
    ):
        view = card(items)
        assert isinstance(view, discord.ui.LayoutView)
        assert not any(
            type(i).__name__ == "Container" for i in view.walk_children()
        )
