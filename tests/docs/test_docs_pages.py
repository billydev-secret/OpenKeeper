"""A dashboard doc read into the pages of a docs-pane card (``docs.pages``).

The pane opens a doc privately and turns its pages, so the rules pinned here
are the ones a reader relies on: nothing is dropped, pages fill in order,
every page fits one Components V2 message *with its controls*, and a fence
is never left open.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import MAX_CHARS, check_budget, layout_budget
from bot_modules.docs.help_card import HELP_TEXT_BUDGET
from bot_modules.docs.pages import (
    EMPTY_DOC_TEXT,
    PAGE_BODY_ITEMS,
    build_doc_page_items,
    page_caption,
    plan_doc_pages,
)
from bot_modules.docs.render import FENCE_RE, atomic_blocks

FALLBACK = "Server Guide"


def _plan(body):
    return plan_doc_pages(body, fallback_title=FALLBACK)


def _para(n: int, size: int = 400) -> str:
    """A paragraph of ``size`` characters that names itself."""
    head = f"Paragraph {n}. "
    return head + "x" * (size - len(head))


def _card(doc, index: int) -> discord.ui.LayoutView:
    """A page as the pane sends it: body, caption, arrows, a full select."""
    view = discord.ui.LayoutView(timeout=None)
    for item in build_doc_page_items(doc, index):
        view.add_item(item)
    view.add_item(discord.ui.TextDisplay(page_caption(index, len(doc))))
    view.add_item(
        discord.ui.ActionRow(
            discord.ui.Button(label="◀"), discord.ui.Button(label="▶")
        )
    )
    select = discord.ui.Select(
        options=[
            discord.SelectOption(label=f"Doc {i}", value=f"doc-{i}")
            for i in range(25)
        ]
    )
    view.add_item(discord.ui.ActionRow(select))
    return view


def _all_text(doc) -> list[str]:
    """Every block the pages carry, in order."""
    return [
        block
        for page in doc.pages
        for section in page
        for block in atomic_blocks(section.text)
    ]


# ── one page ────────────────────────────────────────────────────────────


def test_a_short_doc_is_one_page_titled_by_its_heading():
    doc = _plan("# Rules\n\nBe kind.\n\nNo spam.")
    assert doc.title == "Rules"
    assert len(doc) == 1
    texts = [
        i.content
        for i in build_doc_page_items(doc, 0)
        if isinstance(i, discord.ui.TextDisplay)
    ]
    assert texts == ["## Rules\nBe kind.\n\nNo spam."]


def test_a_doc_without_a_heading_takes_the_fallback_title():
    assert _plan("Just text.").title == FALLBACK


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param("# Only a heading\n", id="heading-only"),
        pytest.param("---\n\n---\n", id="only-breaks"),
    ],
)
def test_an_empty_doc_is_one_page_saying_so(body):
    doc = _plan(body)
    assert len(doc) == 1
    assert [s.text for s in doc.pages[0]] == [EMPTY_DOC_TEXT]


def test_sections_sharing_a_page_are_divided_by_a_drawn_separator():
    doc = _plan("# T\n\nOne.\n---\nTwo.")
    items = build_doc_page_items(doc, 0)
    kinds = [type(i) for i in items]
    assert kinds == [
        discord.ui.TextDisplay,
        discord.ui.Separator,
        discord.ui.TextDisplay,
    ]
    separator, second = items[1], items[2]
    assert isinstance(separator, discord.ui.Separator) and separator.visible
    assert isinstance(second, discord.ui.TextDisplay) and second.content == "Two."


# ── auto-fill, nothing lost ─────────────────────────────────────────────


def _etiquette_shape() -> str:
    """Prod's etiquette guide is 4,832 characters in five sections."""
    sections = [
        "\n\n".join(_para(10 * s + p, 300) for p in range(3)) for s in range(5)
    ]
    return "# Etiquette\n\n" + "\n---\n".join(sections)


def test_the_etiquette_guide_shape_fills_two_pages():
    body = _etiquette_shape()
    assert len(body) > MAX_CHARS
    assert len(_plan(body)) == 2


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_etiquette_shape(), id="five-sections"),
        pytest.param(
            "# Long\n\n" + "\n\n".join(_para(i) for i in range(40)),
            id="one-long-section",
        ),
        pytest.param(
            "\n---\n".join(f"Section {i}." for i in range(60)),
            id="many-tiny-sections",
        ),
    ],
)
def test_every_block_lands_on_a_page_in_order(body):
    doc = _plan(body)
    _, rest = body.split("\n", 1) if body.startswith("#") else ("", body)
    expected = [b for raw in rest.split("\n---\n") for b in atomic_blocks(raw)]
    assert _all_text(doc) == expected


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_etiquette_shape(), id="five-sections"),
        pytest.param(
            "# Long\n\n" + "\n\n".join(_para(i) for i in range(40)),
            id="one-long-section",
        ),
        pytest.param(
            "\n---\n".join(
                f"Section {i}.\n\n![a](https://x.test/{i}.png)" for i in range(30)
            ),
            id="many-sections-with-images",
        ),
        pytest.param("# Wall\n\n" + "line\n" * 3000, id="one-huge-paragraph"),
        pytest.param(
            "# " + "T" * 200 + "\n\n" + "\n\n".join(_para(i) for i in range(20)),
            id="longest-title",
        ),
    ],
)
def test_every_page_fits_one_card_with_its_controls(body):
    doc = _plan(body)
    for index in range(len(doc)):
        check_budget(_card(doc, index))


def test_a_page_fills_before_the_next_opens():
    """Auto-fill: a page is only closed by the budget, never by a ``---``."""
    doc = _plan("\n---\n".join(_para(i, 100) for i in range(10)))
    assert len(doc) == 1
    assert len(doc.pages[0]) == 10


def test_a_section_runs_on_across_a_page_turn():
    doc = _plan("# T\n\n" + "\n\n".join(_para(i) for i in range(15)))
    assert len(doc) == 2
    assert doc.pages[1][0].text.startswith("Paragraph ")
    chars = [sum(len(s.text) for s in page) for page in doc.pages]
    assert chars[0] > HELP_TEXT_BUDGET - 500  # filled, not split early


def test_many_small_sections_page_on_components_not_text():
    doc = _plan("\n---\n".join(f"S{i}" for i in range(40)))
    assert len(doc) == 3
    for index in range(len(doc)):
        components, _ = layout_budget(_card(doc, index))
        assert components <= 40
    body_items = [
        sum(1 for _ in build_doc_page_items(doc, i)) for i in range(len(doc))
    ]
    assert max(body_items) <= PAGE_BODY_ITEMS + 1  # + the title's own line


# ── oversized blocks ────────────────────────────────────────────────────


def test_an_oversized_paragraph_splits_at_line_breaks_keeping_every_line():
    lines = [f"line {i:04d}" for i in range(800)]
    doc = _plan("# Wall\n\n" + "\n".join(lines))
    assert len(doc) > 1
    kept = [line for page in doc.pages for s in page for line in s.text.split("\n")]
    assert kept == lines


def test_an_oversized_fence_is_refenced_on_every_piece():
    code = [f"print({i})" for i in range(700)]
    doc = _plan("# Code\n\n```py\n" + "\n".join(code) + "\n```")
    assert len(doc) > 1
    kept: list[str] = []
    for page in doc.pages:
        for section in page:
            lines = section.text.split("\n")
            assert lines[0] == "```py"
            assert lines[-1] == "```"
            assert FENCE_RE.match(section.text)
            kept.extend(lines[1:-1])
    assert kept == code


def test_an_unclosed_oversized_fence_is_closed_on_every_piece():
    doc = _plan("```\n" + "\n".join(f"row {i}" for i in range(900)))
    for page in doc.pages:
        for section in page:
            assert section.text.startswith("```\n")
            assert section.text.endswith("\n```")


# ── images ──────────────────────────────────────────────────────────────


def test_an_image_rides_its_sections_first_piece():
    body = "# T\n\n![banner](https://x.test/b.png)\n\n" + "\n\n".join(
        _para(i) for i in range(15)
    )
    doc = _plan(body)
    assert len(doc) == 2
    assert doc.pages[0][0].image_url == "https://x.test/b.png"
    assert doc.pages[1][0].image_url is None
    assert "![" not in "".join(s.text for p in doc.pages for s in p)


def test_an_image_only_first_section_still_carries_the_title():
    doc = _plan("![banner](https://x.test/b.png)\n---\nText.")
    items = build_doc_page_items(doc, 0)
    assert isinstance(items[0], discord.ui.TextDisplay)
    assert items[0].content == f"## {FALLBACK}"
    assert isinstance(items[1], discord.ui.MediaGallery)


def test_page_caption_is_one_based():
    assert page_caption(0, 3) == "-# Page 1 of 3"
    assert page_caption(2, 3) == "-# Page 3 of 3"


@pytest.mark.parametrize("opener_len", [3790, 3850, 4100])
def test_a_fence_with_a_huge_opening_line_still_pages_and_ends(opener_len):
    """An opening line nearly as long as a page left zero or negative room to
    re-fence each piece, and hard_split looped forever (ship review,
    2026-10-01). It now falls back to a plain split that fits the budget."""
    from bot_modules.docs.pages import _fit_block

    block = "```" + "x" * opener_len + "\n" + "\n".join(["line"] * 400) + "\n```"
    pieces = _fit_block(block, HELP_TEXT_BUDGET)
    assert pieces and all(len(p) <= HELP_TEXT_BUDGET for p in pieces)
    assert "".join(pieces).replace("\n", "").count("line") == 400


@pytest.mark.parametrize("limit", [0, -1, -50])
def test_hard_split_refuses_a_limit_that_cannot_make_progress(limit):
    from bot_modules.docs.render import hard_split

    with pytest.raises(ValueError):
        hard_split("abc\ndef", limit)
