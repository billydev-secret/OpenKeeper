"""Tests for ``bot_modules/todo/layouts.py`` — the Components V2 cards that
replaced the todo board's, the bug board's and the change cards' embeds.

The builders are pure, so every card is asserted on its text and its controls
with no Discord mocks. What a row *says* is ``board_logic``'s and
``change_logic``'s, tested in their own files; this one holds the shape, the
budget at the worst board the feature can produce, and the two deliberate
anomalies: the boards wear no art, and a bug card names no filer.
"""

from __future__ import annotations

import inspect
from typing import Any

import discord
import pytest

from bot_modules.core.layout import MAX_ACCESSORY_LABEL, card, check_budget, layout_budget
from bot_modules.todo import layouts
from bot_modules.todo.board_logic import (
    EMPTY_BOARD,
    MAX_APPROVAL_ROWS,
    MAX_BOARD_ROWS,
    MAX_CONFESSION_ROWS,
    MAX_SIGNOFF_ROWS,
    TASK_HEADING,
    board_sections,
    render_board_footer,
    render_footer,
    render_rows,
)
from bot_modules.todo.change_logic import (
    MAX_CARD_IMAGES,
    MAX_CARD_LINES,
    bug_line,
    image_filename,
    is_bug_image,
    plan_arrival_card,
    BUG_TITLE_MANY,
    BUG_TITLE_ONE,
)
from bot_modules.todo.layouts import (
    BUG_ADD_ID,
    BUG_COMPLETE_ID,
    BUG_PICK_ID,
    TODO_ADD_ID,
    TODO_APPROVALS_ID,
    TODO_COMPLETE_ID,
    TODO_CONFESSIONS_ID,
    TODO_PICK_ID,
    TODO_SIGNOFFS_ID,
    build_bug_board_layout,
    build_change_card_layout,
    build_complete_picker_layout,
    build_completed_layout,
    build_todo_board_layout,
)
from bot_modules.todo.service import TASK_MAX_LEN


def _walk(items: list[discord.ui.Item[Any]], kind: type) -> list[Any]:
    return [i for i in card(items).walk_children() if isinstance(i, kind)]


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(i.content for i in _walk(items, discord.ui.TextDisplay))


def _button_ids(items: list[discord.ui.Item[Any]]) -> list[str | None]:
    return [b.custom_id for b in _walk(items, discord.ui.Button)]


def _todo_board(**kwargs) -> list[discord.ui.Item[Any]]:
    tasks = kwargs.pop("tasks", [{"id": 7, "task": "fix the quote bot"}])
    chores = kwargs.pop("chores", [])
    return build_todo_board_layout(
        board_sections(chores, tasks, **kwargs), render_board_footer(chores, len(tasks))
    )


def _bug_board(rows=({"id": 3, "task": "the shop is broken", "added_by": 42},)):
    return build_bug_board_layout(
        render_rows(rows), render_footer(len(rows), noun="bug")
    )


# ── the todo board ──────────────────────────────────────────────────────────


def test_the_todo_board_is_title_sections_counts_then_buttons():
    items = _todo_board(chores=[{"recurring_id": 1, "task": "Do a QOTD"}])
    texts = [i.content for i in _walk(items, discord.ui.TextDisplay)]
    assert texts[0] == "## 📋 Server Todo"
    # One Text Display per section, its heading a ### line over its rows.
    assert texts[1].startswith("### 🔁 Today's chores\n")
    assert texts[2] == f"### {TASK_HEADING}\n`#7` fix the quote bot"
    assert texts[3].startswith("-# ") and "1 task" in texts[3]
    # The buttons are the card's last item, so the counts read above them.
    assert isinstance(items[-1], discord.ui.ActionRow)


def test_an_empty_todo_board_says_so_once_and_keeps_its_buttons():
    items = build_todo_board_layout([], render_board_footer([], 0))
    assert _text(items).startswith(f"## 📋 Server Todo\n{EMPTY_BOARD}")
    assert "###" not in _text(items)
    assert len(_button_ids(items)) == 5


def test_the_todo_board_keeps_the_embed_era_button_ids():
    """The posted board converts in place on the boot repaint. A renamed id
    would leave every press on it answering "This interaction failed"."""
    assert _button_ids(_todo_board()) == [
        TODO_ADD_ID, TODO_COMPLETE_ID, TODO_SIGNOFFS_ID,
        TODO_APPROVALS_ID, TODO_CONFESSIONS_ID,
    ]
    assert (TODO_ADD_ID, TODO_COMPLETE_ID) == ("todo_board_add", "todo_board_complete")
    assert (TODO_SIGNOFFS_ID, TODO_APPROVALS_ID, TODO_CONFESSIONS_ID) == (
        "todo_board_signoffs", "todo_board_approvals", "todo_board_confessions",
    )


def test_the_todo_boards_buttons_fit_one_row():
    """Five is Discord's cap on a row; a sixth needs a second row."""
    [row] = [i for i in _todo_board() if isinstance(i, discord.ui.ActionRow)]
    assert len(row.children) == 5


# ── the bug board ───────────────────────────────────────────────────────────


def test_the_bug_board_is_title_rows_count_then_buttons():
    items = _bug_board()
    texts = [i.content for i in _walk(items, discord.ui.TextDisplay)]
    assert texts == [
        "## 🐛 Bug List",
        "`#3` the shop is broken",
        "-# 1 pending bug · updates automatically",
    ]
    assert _button_ids(items) == [BUG_ADD_ID, BUG_COMPLETE_ID]
    assert (BUG_ADD_ID, BUG_COMPLETE_ID) == ("bug_board_add", "bug_board_complete")


def test_the_bug_board_never_names_the_filer():
    """Deliberate (docs/specs/todo_spec.md § The bug list): a bug can be filed by
    any member, and nothing the bot posts about it carries their name or id."""
    text = _text(_bug_board())
    assert "42" not in text
    assert "<@" not in text


# ── both boards ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("build", [_todo_board, _bug_board], ids=["todo", "bug"])
def test_every_board_button_is_grey(build):
    """Style guide § Buttons: grey unless the colour means something. The
    embed-era boards had a blurple Add and a green Complete, which ranked two
    presses that aren't ranked."""
    assert {b.style for b in _walk(build(), discord.ui.Button)} == {
        discord.ButtonStyle.secondary
    }


@pytest.mark.parametrize("build", [_todo_board, _bug_board], ids=["todo", "bug"])
def test_the_boards_are_bare(build):
    """Deliberate (Ben, 2026-09-30): the two boards are staff worklists and
    wear no banner, lobby strip or footer art, against the guide's general
    rule for a newly converted channel hub. Fails if art is added."""
    assert not _walk(build(), discord.ui.MediaGallery)
    source = inspect.getsource(layouts)
    for helper in ("hub_banner(", "hub_footer(", "lobby_art("):
        assert helper not in source


# ── the Complete picker ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bugs", "pick_id", "title"),
    [
        pytest.param(False, TODO_PICK_ID, "## ✅ Complete Tasks", id="todo"),
        pytest.param(True, BUG_PICK_ID, "## ✅ Complete Bugs", id="bug"),
    ],
)
def test_the_picker_is_a_heading_over_a_multi_pick(bugs, pick_id, title):
    rows = [{"id": n, "task": f"thing {n}", "description": "why"} for n in (4, 9)]
    items = build_complete_picker_layout(rows, bugs=bugs)
    assert _text(items).startswith(title)
    [select] = _walk(items, discord.ui.Select)
    assert select.custom_id == pick_id
    assert [o.value for o in select.options] == ["4", "9"]
    assert select.options[0].label == "#4 thing 4"
    assert select.options[0].description == "why"
    # Several at once: that is what the one Complete button is for.
    assert (select.min_values, select.max_values) == (1, 2)


def test_the_picker_stops_at_discords_25_options():
    rows = [{"id": n, "task": "t" * TASK_MAX_LEN} for n in range(40)]
    items = build_complete_picker_layout(rows)
    [select] = _walk(items, discord.ui.Select)
    assert len(select.options) == 25
    assert select.max_values == 25
    check_budget(card(items))


@pytest.mark.parametrize(
    ("done", "bugs", "expected"),
    [
        pytest.param(1, False, "## ✅ Tasks Completed\nMarked **1** task complete.", id="one-task"),
        pytest.param(3, False, "## ✅ Tasks Completed\nMarked **3** tasks complete.", id="tasks"),
        pytest.param(1, True, "## ✅ Bugs Fixed\nMarked **1** bug fixed.", id="one-bug"),
        pytest.param(2, True, "## ✅ Bugs Fixed\nMarked **2** bugs fixed.", id="bugs"),
    ],
)
def test_the_picker_redraws_into_how_many_it_closed(done, bugs, expected):
    items = build_completed_layout(done, bugs=bugs)
    assert _text(items) == expected
    assert not _walk(items, discord.ui.Select)


@pytest.mark.parametrize(
    ("bugs", "title"),
    [
        pytest.param(False, "## ❌ Already Completed", id="todo"),
        pytest.param(True, "## ❌ Already Closed", id="bug"),
    ],
)
def test_a_pick_somebody_else_already_closed_says_nothing_changed(bugs, title):
    text = _text(build_completed_layout(0, bugs=bugs))
    assert text.startswith(title)
    assert "someone else" in text and "nothing changed" in text


# ── change cards ────────────────────────────────────────────────────────────


def test_a_change_card_is_its_title_over_its_lines_and_nothing_to_press():
    items = build_change_card_layout("📝 2 new items need a mod", "➕ a\n➕ b")
    assert _text(items) == "## 📝 2 new items need a mod\n➕ a\n➕ b"
    assert not _walk(items, discord.ui.Button)
    assert not _walk(items, discord.ui.MediaGallery)


def test_a_bug_card_references_each_screenshot_it_uploads():
    """A V2 message shows no attachment a component doesn't reference, and the
    filename in the reference is how closing the bug finds its pictures."""
    names = [image_filename(12, 1, "png"), image_filename(30, 1, "jpg")]
    items = build_change_card_layout(
        "🐛 2 new bugs", "🐛 a\n🐛 b", images=[(names[0], 12), (names[1], 30)]
    )
    [gallery] = _walk(items, discord.ui.MediaGallery)
    assert [i.media.url for i in gallery.items] == [f"attachment://{n}" for n in names]
    assert is_bug_image(gallery.items[0].media.url.rsplit("/", 1)[-1], 12)
    # Alt text names the bug, never a person.
    assert [i.description for i in gallery.items] == [
        "Screenshot for bug #12", "Screenshot for bug #30",
    ]
    assert not any(i.spoiler for i in gallery.items)


def test_screenshots_are_spoilered_in_an_age_gated_channel():
    """Style guide § Media: member-submitted media into a Discord age-gated
    channel is spoilered, on the media item."""
    items = build_change_card_layout(
        "🐛 1 new bug", "🐛 a", images=[("bug-1-1.png", 1)], spoiler=True
    )
    [gallery] = _walk(items, discord.ui.MediaGallery)
    assert all(i.spoiler for i in gallery.items)


# ── every card ──────────────────────────────────────────────────────────────


_EVERY_CARD = {
    "todo board": _todo_board(),
    "todo board, empty": build_todo_board_layout([], render_board_footer([], 0)),
    "bug board": _bug_board(),
    "bug board, empty": build_bug_board_layout(render_rows(()), render_footer(0, noun="bug")),
    "picker": build_complete_picker_layout([{"id": 1, "task": "a"}]),
    "bug picker": build_complete_picker_layout([{"id": 1, "task": "a"}], bugs=True),
    "completed": build_completed_layout(2),
    "already completed": build_completed_layout(0),
    "bugs fixed": build_completed_layout(2, bugs=True),
    "already closed": build_completed_layout(0, bugs=True),
    "change card": build_change_card_layout("📝 1 new item needs a mod", "➕ a"),
}


@pytest.mark.parametrize("name", sorted(_EVERY_CARD))
def test_every_card_leads_with_a_glyph_led_title(name):
    first = _EVERY_CARD[name][0]
    assert isinstance(first, discord.ui.TextDisplay)
    assert first.content.startswith("## ")
    assert not first.content[3].isascii(), first.content


@pytest.mark.parametrize("name", sorted(_EVERY_CARD))
def test_no_card_is_boxed_or_carries_a_mention(name):
    view = card(_EVERY_CARD[name])
    assert not [i for i in view.walk_children() if isinstance(i, discord.ui.Container)]
    assert "<@" not in _text(_EVERY_CARD[name])


def test_no_button_label_is_wider_than_a_phone_row():
    for name, items in _EVERY_CARD.items():
        for button in _walk(items, discord.ui.Button):
            assert len(button.label or "") <= MAX_ACCESSORY_LABEL + 2, (name, button.label)


# ── budget: the worst case this feature can actually produce ───────────────


def _loud(n: int) -> str:
    """A row at the service's length cap made of characters escaping doubles."""
    return f"{n}" + "_*" * (TASK_MAX_LEN // 2)


def _worst_board() -> tuple[list[tuple[str, str]], str]:
    """Every section at its row cap, every row ``TASK_MAX_LEN`` of markdown
    (so the clip-then-escape doubles it), long names, an overflow line in each
    — every optional slot filled at once."""
    name = "N" * 32
    signoffs = [
        {"id": n, "quest_title": _loud(n), "reward": 10**9, "claimant_name": name}
        for n in range(MAX_SIGNOFF_ROWS + 1)
    ]
    approvals = [
        {"id": n, "kind": "sponsor", "summary": _loud(n), "price": 10**9,
         "requester_name": name}
        for n in range(MAX_APPROVAL_ROWS + 1)
    ]
    confessions = [
        {"id": n, "content": _loud(n), "created_at": 1_700_000_000}
        for n in range(MAX_CONFESSION_ROWS + 1)
    ]
    chores = [
        {"recurring_id": n, "todo_id": n, "task": _loud(n), "streak": 999,
         "completed_at": 1_700_000_000, "completed_by_name": name}
        for n in range(50)
    ]
    tasks = [
        {"id": 10**6 + n, "task": _loud(n), "recurring_id": 1, "purchase_id": 1,
         "buyer_name": name}
        for n in range(MAX_BOARD_ROWS + 1)
    ]
    sections = board_sections(
        chores, tasks,
        signoff_rows=signoffs, signoff_total=999,
        approval_rows=approvals, approval_total=999,
        confession_rows=confessions, confession_total=999,
        task_total=999,
    )
    assert len(sections) == 5
    return sections, render_board_footer(chores, 999, 999, 999, 999)


def test_the_todo_board_stays_in_budget_with_every_section_full_of_markdown():
    """Fails before the trim: that board's text measured 4,140 characters
    against Discord's 4,000, which a 400 would have answered by painting
    nothing. Text is the ceiling that binds here — the board is five Text
    Displays however many rows it has."""
    sections, footer = _worst_board()
    untrimmed = [f"### {h}\n{b}" for h, b in sections]
    assert sum(map(len, untrimmed)) + len(f"## 📋 Server Todo-# {footer}") > 4000
    items = build_todo_board_layout(sections, footer)
    view = card(items)
    check_budget(view)
    components, chars = layout_budget(view)
    assert components <= 20
    assert chars <= layouts.BOARD_TEXT_BUDGET
    text = _text(items)
    # It says rows were lost, and every section is still on the board.
    assert layouts.TRIMMED_LINE in text
    assert text.count("### ") == 5
    assert all(body.strip() for body in text.split("### ")[1:])


def test_an_ordinary_full_board_is_not_trimmed():
    """The same full board in plain words: every row at the length cap, no
    markdown to double. Nothing is cut."""
    sections, footer = _worst_board()
    plain = [(h, b.replace("\\", "")) for h, b in sections]
    items = build_todo_board_layout(plain, footer)
    assert layouts.TRIMMED_LINE not in _text(items)
    assert [i.content.split("\n", 1)[1] for i in _walk(items, discord.ui.TextDisplay)[1:-1]] == [
        body for _, body in plain
    ]
    assert layout_budget(card(items))[1] < 3400


def test_the_bug_board_stays_in_budget_at_a_full_screen_of_markdown():
    rows = [{"id": 10**6 + n, "task": _loud(n)} for n in range(MAX_BOARD_ROWS + 1)]
    items = build_bug_board_layout(
        render_rows(rows, total=999), render_footer(999, noun="bug")
    )
    check_budget(card(items))


def test_a_bug_card_stays_in_budget_at_a_burst_with_every_image_slot_used():
    """More bugs than a card prints, each at the length cap, and the ten
    images one message may carry."""
    bugs = [{"id": 10**6 + n, "task": _loud(n)} for n in range(MAX_CARD_LINES + 5)]
    plan = plan_arrival_card(
        now=2.0, cursor_at=1.0, task_lines=[bug_line(b) for b in bugs],
        title_one=BUG_TITLE_ONE, title_many=BUG_TITLE_MANY,
    )
    assert plan.title and plan.body
    items = build_change_card_layout(
        plan.title, plan.body,
        images=[(image_filename(n, 1, "png"), n) for n in range(MAX_CARD_IMAGES)],
        spoiler=True,
    )
    check_budget(card(items))


@pytest.mark.parametrize(
    "sections",
    [
        pytest.param([("Tasks", "x" * 5000)], id="one-enormous-row"),
        pytest.param([("A", "y" * 3000), ("B", "z" * 3000)], id="two-single-row-sections"),
        pytest.param([("A", "\n".join(["row"] * 50) + "\n" + "w" * 4000)], id="rows-then-a-giant"),
    ],
)
def test_fit_sections_always_fits_even_when_rows_cannot_be_dropped(sections):
    """Row-trimming stops at one line per section, so a single giant row
    could still 400 the whole board (ship review, 2026-10-01)."""
    from bot_modules.todo import layouts

    room = 3500
    fitted = layouts._fit_sections(sections, room)
    assert sum(len(body) for _, body in fitted) <= room
    assert [h for h, _ in fitted] == [h for h, _ in sections]
    assert any(body.endswith(layouts.TRIMMED_LINE) for _, body in fitted)


def test_fit_sections_leaves_a_board_that_fits_untouched():
    from bot_modules.todo import layouts

    sections = [("Tasks", "one\ntwo"), ("Chores", "three")]
    assert layouts._fit_sections(sections, 3500) == sections
