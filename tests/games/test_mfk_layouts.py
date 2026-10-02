"""Contract + behavior tests for the MFK Components V2 cards
(``bot_modules/games/mfk/layouts.py`` — wave 3b). Mirrors
``tests/games/test_games_core_layouts.py``'s checklist: paradigm shape, budget,
no ``Container``, a glyph-led title, and every member named through
``name_fn`` rather than a raw mention.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.games.mfk.layouts import (
    CLOSE_ASSIGN_CUSTOM_ID,
    HOW_TO_PLAY_CUSTOM_ID,
    JOIN_CUSTOM_ID,
    build_assignments_layout,
    build_lobby_action_row,
    build_lobby_layout,
    format_assignment_value,
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


def _named(uid: int) -> str:
    return f"Member{uid}"


def _lobby_empty():
    return build_lobby_layout("Alice", [])


def _lobby_full():
    return build_lobby_layout(
        "Alice", ["Bob", "Carol", "Dan"], labels=["Cruise", "Wedding", "Vacation"],
        start_at=1_700_000_000,
    )


def _assignments_two():
    return build_assignments_layout({1: [2, 3, 4], 2: [1, 3, 4]}, name_fn=_named)


def _assignments_empty():
    return build_assignments_layout({}, labels=["Cruise", "Wedding", "Vacation"])


ALL_CARDS = [
    pytest.param(_lobby_empty, id="lobby-empty"),
    pytest.param(_lobby_full, id="lobby-full"),
    pytest.param(_assignments_two, id="assignments-two-players"),
    pytest.param(_assignments_empty, id="assignments-empty"),
]

# The assignments reveal is the one deliberate ping this feature sends (no
# other card names anyone with a raw mention): a V2 send has no content= to
# carry it, so each player's own line opens with a raw <@id> instead.
# Excluded from the "no raw mention" sweep below; covered by its own test.
# (Not a tests/contracts/test_embed_mention_guard.py ledger entry — that AST sweep's
# sinks match TextDisplay(/Section( by name and this module routes every
# TextDisplay through a local ``_text()`` wrapper, so it never sees this
# site; a KNOWN_EMBED_MENTIONS row for a site the scanner can't find would
# fail that file's own not-stale check.)
NO_PING_CARDS = [
    pytest.param(_lobby_empty, id="lobby-empty"),
    pytest.param(_lobby_full, id="lobby-full"),
    pytest.param(_assignments_empty, id="assignments-empty"),
]


# ── Contract sweep ───────────────────────────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    heading = next(
        item.content
        for item in _view(build()).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]
    assert heading.startswith("## ")
    assert len(heading) > 3 and not heading[3].isascii()


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container) for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", NO_PING_CARDS)
def test_no_card_writes_a_raw_at_mention(build):
    """Every member *named* in a TextDisplay goes through ``name_fn`` — a raw
    ``<@id>`` renders as digits to anyone with no cache hit for that id.
    The assignments reveal is the one deliberate exception (its own test,
    below) — that ping is the point of the card."""
    assert "<@" not in _all_text(build())


def test_assignments_pings_each_player_once_in_their_own_line():
    """The reveal's one deliberate ping: content= doesn't exist on a V2
    send, so each player's own line opens with a raw mention instead —
    their drawn targets are still named through ``name_fn``, never pinged."""
    text = _all_text(build_assignments_layout({1: [2, 3, 4], 2: [1, 3, 4]}, name_fn=_named))
    assert "<@1>" in text and "<@2>" in text


# ── Paradigm shape ───────────────────────────────────────────────────


def test_lobby_is_paradigm_8_live_board_with_its_three_frozen_buttons():
    view = _view(_lobby_full())
    row = next(item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow))
    ids = [b.custom_id for b in row.children]
    assert ids == [JOIN_CUSTOM_ID, CLOSE_ASSIGN_CUSTOM_ID, HOW_TO_PLAY_CUSTOM_ID]


def test_assignments_is_paradigm_7_receipt_with_no_controls():
    for build in (_assignments_two, _assignments_empty):
        assert not any(
            isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
            for item in _view(build()).walk_children()
        )


def test_build_lobby_action_row_matches_the_frozen_custom_ids():
    row = build_lobby_action_row()
    assert [b.custom_id for b in row.children] == [
        JOIN_CUSTOM_ID,
        CLOSE_ASSIGN_CUSTOM_ID,
        HOW_TO_PLAY_CUSTOM_ID,
    ]


# ── Card-specific spot checks (moved from the pre-conversion embed suite) ──


def test_lobby_default_labels_in_title():
    text = _all_text(build_lobby_layout("Alice", []))
    assert "Marry" in text and "Fornicate" in text and "Kiss" in text


def test_lobby_custom_labels_in_title():
    text = _all_text(
        build_lobby_layout("Alice", [], labels=["Cruise", "Wedding", "Vacation"])
    )
    assert "Cruise" in text and "Wedding" in text and "Vacation" in text


def test_lobby_empty_pool_shows_dash():
    text = _all_text(build_lobby_layout("Alice", []))
    assert "Pool (0)" in text
    assert "—" in text


def test_lobby_lists_participants():
    text = _all_text(build_lobby_layout("Alice", ["Bob", "Carol", "Dan"]))
    assert "Pool (3)" in text
    assert "Bob, Carol, Dan" in text


def test_lobby_categories_bolds_each_label():
    text = _all_text(build_lobby_layout("Alice", [], labels=["A", "B", "C"]))
    assert "**A**" in text and "**B**" in text and "**C**" in text


def test_lobby_names_the_host():
    text = _all_text(build_lobby_layout("Alice", []))
    assert "**Alice**" in text


def test_lobby_renders_the_start_countdown():
    # start_at advertises a start time as a live Discord relative timestamp;
    # the host still presses the button.
    text = _all_text(build_lobby_layout("Alice", [], start_at=1_700_000_000))
    assert "<t:1700000000:R>" in text


def test_lobby_omits_the_countdown_when_none_was_set():
    text = _all_text(build_lobby_layout("Alice", []))
    assert "Starting" not in text


def test_lobby_escapes_markdown_in_host_and_participant_names():
    """A member-set display name is untrusted, unescaped text — a
    TextDisplay renders markdown, unlike the embed field it replaced."""
    text = _all_text(build_lobby_layout("*Alice*", ["_Bob_"]))
    assert "*Alice*" not in text
    assert "\\*Alice\\*" in text
    assert "\\_Bob\\_" in text


def test_format_assignment_value_bolds_each_name():
    result = format_assignment_value(["X", "Y", "Z"])
    assert "**X**" in result and "**Y**" in result and "**Z**" in result


def test_format_assignment_value_separates_with_middle_dot():
    assert " · " in format_assignment_value(["X", "Y", "Z"])


def test_assignments_one_line_per_player():
    text = _all_text(build_assignments_layout({1: [2, 3, 4], 2: [1, 3, 4]}, name_fn=_named))
    assert "Member1" in text and "Member2" in text


def test_assignments_title_mentions_three_names():
    text = _all_text(build_assignments_layout({}))
    assert "Your Three Names" in text


def test_assignments_cta_lists_categories():
    text = _all_text(
        build_assignments_layout({}, labels=["Cruise", "Wedding", "Vacation"])
    )
    assert "Cruise" in text and "Wedding" in text and "Vacation" in text


def test_assignments_custom_labels_in_title():
    text = _all_text(build_assignments_layout({}, labels=["A", "B", "C"]))
    assert "A, B, C" in text


def test_assignments_line_includes_bold_target_names():
    text = _all_text(build_assignments_layout({1: [2, 3, 4]}, name_fn=_named))
    assert "**Member2**" in text and "**Member3**" in text and "**Member4**" in text


def test_assignments_defaults_to_a_mention_for_an_unwired_caller():
    text = _all_text(build_assignments_layout({99: [1, 2, 3]}))
    assert "<@99>" in text and "**<@1>**" in text


def test_assignments_payout_line_renders_as_subtext():
    text = _all_text(
        build_assignments_layout({}, payout_line="🪙 +20 to winners")
    )
    assert "-# 🪙 +20 to winners" in text


def test_assignments_omits_payout_subtext_when_none():
    text = _all_text(build_assignments_layout({}))
    assert "-#" not in text


def test_assignments_caps_at_twenty_five_rows():
    """A stale/oversized roster must never overflow the card — the lobby
    caps joins at MAX_PARTICIPANTS (25) already; this is the builder's own
    defensive slice."""
    assignments = {i: [i + 1, i + 2, i + 3] for i in range(30)}
    view = _view(build_assignments_layout(assignments, name_fn=_named))
    text = _all_text(build_assignments_layout(assignments, name_fn=_named))
    assert text.count("Member") <= 25 * 3  # up to 3 resolved target names/line
    check_budget(view)


# ── Budget ────────────────────────────────────────────────────────────


def test_fullest_assignments_card_fits_the_budget():
    """25 players, long display names, default labels, plus a payout line —
    the worst case the lobby's own MAX_PARTICIPANTS cap allows."""
    assignments = {i: [(i + 1) % 25, (i + 2) % 25, (i + 3) % 25] for i in range(25)}

    def _long_name(uid: int) -> str:
        return f"{'A' * 28}#{uid:02d}"

    view = _view(
        build_assignments_layout(
            assignments,
            name_fn=_long_name,
            payout_line="🪙 +20 to winners • +5 to everyone who played",
        )
    )
    check_budget(view)


def test_fullest_lobby_card_fits_the_budget():
    participants = [f"{'B' * 28}#{i:02d}" for i in range(25)]
    view = _view(
        build_lobby_layout(
            "H" * 32, participants, labels=["Cruise", "Wedding", "Vacation"],
            start_at=1_700_000_000,
        )
    )
    check_budget(view)


# ── Worst cases: member-authored text that escaping doubles ──────────


def _markdown_name(uid: int) -> str:
    """What ``build_name_fn`` hands back for a 32-char all-``*`` name."""
    return discord.utils.escape_markdown("*" * 32)


def test_markdown_heavy_assignments_drop_whole_rows_to_fit():
    """Three 64-char escaped names a row, 25 rows, ran to ~5,800 characters
    and Discord refused the reveal outright."""
    assignments = {i: [(i + 1) % 25, (i + 2) % 25, (i + 3) % 25] for i in range(25)}
    items = build_assignments_layout(
        assignments, labels=["*" * 2000] * 3, name_fn=_markdown_name,
        payout_line="🪙 +20 to winners",
    )
    check_budget(_view(items))
    text = _all_text(items)
    assert "more_" in text  # the dropped rows are counted, not silently lost
    assert text.endswith("-# 🪙 +20 to winners")


def test_markdown_heavy_pool_fits_escaped_and_counts_the_rest():
    items = build_lobby_layout("Alice", ["*" * 32] * 25)
    check_budget(_view(items))
    text = _all_text(items)
    assert "\\*" in text  # escaped, not merely truncated
    assert "more)" in text


def test_pool_budget_never_leaves_a_dangling_backslash():
    from bot_modules.games.mfk.layouts import _fit_pool

    assert not _fit_pool(["\\" * 32] * 25, limit=100).endswith("\\")
    assert _fit_pool([]) == "—"
    assert _fit_pool(["Bob"]) == "Bob"
    huge = _fit_pool(["*" * 4000], limit=1024)
    assert len(huge) <= 1024 and not huge.rstrip("…").endswith("\\")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda labels: build_lobby_layout("Alice", [], labels=labels), id="lobby"),
        pytest.param(lambda labels: build_assignments_layout({}, labels=labels), id="assignments"),
    ],
)
def test_a_hosts_categories_are_escaped_and_bounded_on_every_card(build):
    """``/mfk options:`` bounds the count of categories, not their length, and
    each card quotes them twice; three 2,000-char categories overflowed the
    card before a single player joined."""
    items = build(["*" * 2000] * 3)
    check_budget(_view(items))
    heading = _all_text(items).splitlines()[0]
    assert "\\*" in heading and "***" not in heading
    typed = _all_text(build(["Ex_wife", "B", "C"]))
    assert "Ex\\_wife" in typed
