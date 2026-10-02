"""A dashboard doc read into an ephemeral help card (``docs.help_card``).

The hub panels' **Help** buttons all build here, so the reading rules — the
lifted heading, ``---`` breaks, images, the cut — are pinned once, not per
feature. A feature's own test pins only what it hands the builder.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.layout import check_budget
from bot_modules.docs.db import create_doc
from bot_modules.docs.help_card import (
    HELP_CUT_NOTE,
    HELP_MAX_SECTIONS,
    HELP_TEXT_BUDGET,
    HELP_TITLE_MAX,
    build_doc_help_layout,
    load_doc_body,
    plan_help_card,
)

FALLBACK = "Feature Help"
MISSING = "There's no FAQ here yet."


def _plan(body):
    return plan_help_card(body, fallback_title=FALLBACK, missing_text=MISSING)


def _build(body) -> list[discord.ui.Item]:
    return build_doc_help_layout(
        body, glyph="📬", fallback_title=FALLBACK, missing_text=MISSING
    )


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _texts(items) -> list[str]:
    return [i.content for i in items if isinstance(i, discord.ui.TextDisplay)]


# ── load_doc_body ───────────────────────────────────────────────────────


def _doc(db_path, guild_id: int, key: str, body: str) -> None:
    with open_db(db_path) as conn:
        create_doc(conn, guild_id, key, key, body, "", 1, 0.0)


def test_load_reads_the_asking_guilds_doc_by_key(sync_db_path):
    """Read as last saved on the dashboard, scoped to the asking guild."""
    _doc(sync_db_path, 1, "faq", "# DMs\nours")
    _doc(sync_db_path, 2, "faq", "# DMs\ntheirs")
    _doc(sync_db_path, 1, "other", "not this one")
    assert load_doc_body(sync_db_path, 1, "faq") == "# DMs\nours"


def test_load_is_none_without_the_doc(sync_db_path):
    _doc(sync_db_path, 1, "other", "not this one")
    assert load_doc_body(sync_db_path, 1, "faq") is None


# ── plan_help_card: title and the missing doc ──────────────────────────


@pytest.mark.parametrize(
    ("body", "title", "text_starts"),
    [
        pytest.param("# Doc title\nBody.", "Doc title", "Body.", id="heading-lifted"),
        pytest.param("## Smaller\nText.", "Smaller", "Text.", id="any-heading-level"),
        pytest.param("\n\n# Late\nText.", "Late", "Text.", id="leading-blank-lines"),
        pytest.param("Just prose.", FALLBACK, "Just prose.", id="headingless"),
        pytest.param(None, FALLBACK, MISSING, id="no-doc"),
        pytest.param("   \n ", FALLBACK, MISSING, id="blank-doc"),
        pytest.param("# Only a heading", "Only a heading", MISSING, id="heading-only"),
        pytest.param("# T\r\nWindows\r\nlines", "T", "Windows\nlines", id="crlf"),
    ],
)
def test_plan_title_and_first_text(body, title, text_starts):
    card = _plan(body)
    assert card.title == title
    assert card.sections[0].text.startswith(text_starts)


def test_plan_caps_an_overlong_heading():
    """The dashboard caps a doc's title field, not the heading line inside
    its body — so the card does, or a heading alone could blow the budget."""
    card = _plan("# " + "y" * 500 + "\nBody.")
    assert len(card.title) == HELP_TITLE_MAX


# ── sections and images ────────────────────────────────────────────────


def test_breaks_become_drawn_lines_between_sections():
    items = _build("# T\nOne.\n\n---\n\nTwo.\n---\nThree.")
    seps = [i for i in items if isinstance(i, discord.ui.Separator)]
    assert len(seps) == 2
    assert all(s.visible and s.spacing is discord.SeparatorSpacing.large for s in seps)
    assert _texts(items) == ["## 📬 T\nOne.", "Two.", "Three."]


def test_an_image_is_a_gallery_not_literal_markdown():
    items = _build("# T\nLook:\n![alt](https://example.com/a.png)\nafter")
    galleries = [i for i in items if isinstance(i, discord.ui.MediaGallery)]
    assert [g.items[0].media.url for g in galleries] == ["https://example.com/a.png"]
    assert "![" not in "\n".join(_texts(items))


def test_an_image_only_section_still_shows():
    items = _build("# T\nIntro.\n---\n![x](https://example.com/b.png)")
    assert any(isinstance(i, discord.ui.MediaGallery) for i in items)
    assert not _plan("# T\nIntro.\n---\n![x](https://example.com/b.png)").truncated


def test_a_heading_with_only_an_image_under_it_keeps_its_title():
    items = _build("# T\n![x](https://example.com/b.png)")
    assert _texts(items) == ["## 📬 T"]


def test_too_many_sections_are_cut_and_the_card_fits():
    body = "# T\n" + "\n---\n".join(
        f"Part {n}.\n![x](https://example.com/{n}.png)" for n in range(15)
    )
    card = _plan(body)
    assert len(card.sections) == HELP_MAX_SECTIONS
    assert card.truncated
    check_budget(_view(_build(body)))


# ── the cut ────────────────────────────────────────────────────────────


def test_a_long_doc_stops_at_a_block_and_says_so():
    """Doc bodies run to 40,000 characters; a message's text caps at 4,000."""
    body = "# FAQ\n" + "\n\n".join("x" * 99 for _ in range(100))
    items = _build(body)
    texts = _texts(items)
    assert texts[-1] == HELP_CUT_NOTE
    assert texts[0].endswith("x"), "cut between paragraphs, not mid-one"
    assert sum(len(t) for t in texts[:-1]) <= HELP_TEXT_BUDGET + len("## 📬 \n")
    check_budget(_view(items))


def test_one_giant_first_paragraph_is_split_not_dropped():
    card = _plan("# FAQ\n" + ("x" * 99 + "\n") * 100)
    assert card.truncated
    assert 0 < len(card.sections[0].text) <= HELP_TEXT_BUDGET


def test_the_cut_never_lands_inside_a_fenced_code_block():
    """Half a fence renders the rest of the card as code: a code block that
    doesn't fit is left out whole."""
    fence = "```\n" + "code line\n" * 50 + "```"
    body = "# FAQ\n" + "x" * 3_400 + "\n\n" + fence + "\n\nmore prose"
    card = _plan(body)
    assert card.truncated
    assert "```" not in card.sections[0].text


def test_an_overlong_heading_with_a_full_body_still_fits():
    body = "# " + "y" * 500 + "\n" + "z" * 5_000
    check_budget(_view(_build(body)))


def test_the_card_leads_with_one_title_and_no_second_heading():
    lines = "\n".join(_texts(_build("# Big\nText.\n\n## Sub\nMore."))).splitlines()
    assert lines[0] == "## 📬 Big"
    assert not any(line.startswith("# ") for line in lines)
