"""The Take a Break cards: ids, labels and colours the guide holds them to."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.breaks.layouts import (
    BREAK_PANEL_TITLE,
    END_CUSTOM_ID,
    RETURN_PANEL_TITLE,
    START_CUSTOM_ID,
    build_break_panel_layout,
    build_reply_layout,
    build_return_panel_layout,
)
from bot_modules.core.layout import (
    HUB_BANNER_ALT,
    HUB_FOOTER_ALT,
    MAX_ACCESSORY_LABEL,
    text_columns,
)


def _walk(items):
    for item in items:
        yield item
        yield from _walk(getattr(item, "children", []) or [])
        accessory = getattr(item, "accessory", None)
        if accessory is not None:
            yield accessory


def _buttons(items):
    return [i for i in _walk(items) if isinstance(i, discord.ui.Button)]


def _first_text(items) -> str:
    return next(i for i in _walk(items) if isinstance(i, discord.ui.TextDisplay)).content


@pytest.mark.parametrize(
    ("builder", "custom_id"),
    [
        pytest.param(build_break_panel_layout, START_CUSTOM_ID, id="take-a-break"),
        pytest.param(build_return_panel_layout, END_CUSTOM_ID, id="im-back"),
    ],
)
def test_each_panel_has_one_grey_persistent_button_that_fits_beside_its_row(builder, custom_id):
    items = builder()
    (button,) = _buttons(items)
    assert button.custom_id == custom_id  # frozen: live panels carry it
    assert button.style is discord.ButtonStyle.secondary
    assert text_columns(button.label) <= MAX_ACCESSORY_LABEL
    assert _first_text(items).startswith("## 💤 ")


@pytest.mark.parametrize(
    ("ok", "starting", "title"),
    [
        pytest.param(True, True, "## 💤 On a Break", id="started"),
        pytest.param(True, False, "## 👋 Welcome Back", id="ended"),
        pytest.param(False, True, "## 💤 Take a Break", id="refused"),
    ],
)
def test_the_reply_card_leads_with_a_title_and_marks_a_refusal(ok, starting, title):
    text = _first_text(build_reply_layout(ok=ok, starting=starting, message="m"))
    assert text.startswith(title)
    assert ("❌" in text) is (not ok)


# ── the panel art ────────────────────────────────────────────────────────

BANNER = "https://dash.example/static/panel-banners/" + "a" * 32 + ".webp"
FOOTER = "https://dash.example/static/panel-banners/" + "b" * 32 + ".webp"

#: Both cards live in a channel, so both take the guild's banner and footer
#: (guide § Media). The ephemeral reply card never does.
PANELS = [
    pytest.param(build_break_panel_layout, BREAK_PANEL_TITLE, id="take-a-break"),
    pytest.param(build_return_panel_layout, RETURN_PANEL_TITLE, id="im-back"),
]


def _galleries(items):
    return [i for i in _walk(items) if isinstance(i, discord.ui.MediaGallery)]


def _texts(items) -> str:
    return "\n".join(i.content for i in _walk(items) if isinstance(i, discord.ui.TextDisplay))


@pytest.mark.parametrize(("builder", "title"), PANELS)
def test_a_guild_with_no_art_gets_the_card_it_had(builder, title):
    """Ships dark: nothing is spliced until someone uploads a banner."""
    assert _galleries(builder()) == []
    assert _galleries(builder(banner_url=None, footer_url="")) == []


@pytest.mark.parametrize(("builder", "title"), PANELS)
def test_the_banner_tops_the_card_and_the_footer_closes_it(builder, title):
    items = builder(banner_url=BANNER, footer_url=FOOTER)
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    galleries = _galleries(items)
    assert [g.items[0].media.url for g in galleries] == [BANNER, FOOTER]
    assert [g.items[0].description for g in galleries] == [HUB_BANNER_ALT, HUB_FOOTER_ALT]


@pytest.mark.parametrize(("builder", "title"), PANELS)
def test_a_title_drawn_into_the_banner_replaces_the_heading(builder, title):
    """With the name in the art the ``##`` line would say it twice, so it
    goes and the alt text carries the name instead."""
    items = builder(banner_url=BANNER, footer_url=FOOTER, title_in_banner=True)
    assert items[0].items[0].description == title
    assert "## " not in _texts(items)


@pytest.mark.parametrize(("builder", "title"), PANELS)
@pytest.mark.parametrize(
    ("banner_url", "title_in_banner"),
    [
        pytest.param(None, True, id="no-banner-keeps-heading"),
        pytest.param(BANNER, False, id="switch-off-keeps-heading"),
    ],
)
def test_the_heading_stays_unless_the_banner_carries_it(
    builder, title, banner_url, title_in_banner
):
    items = builder(banner_url=banner_url, title_in_banner=title_in_banner)
    assert f"## 💤 {title}" in _texts(items)


def test_the_cog_views_bind_both_buttons():
    """A renamed custom_id would raise here, at construction — not on a
    member's press after the next restart."""
    from bot_modules.breaks.cog import BreakPanelView, BreakReturnView

    start = BreakPanelView(MagicMock())
    end = BreakReturnView(MagicMock())
    assert _buttons(start.children)[0].callback == start.start
    assert _buttons(end.children)[0].callback == end.end
    assert start.timeout is None and end.timeout is None


@pytest.mark.parametrize(
    ("builder", "row_title"),
    [
        pytest.param(build_break_panel_layout, "**Start a Break**", id="take-a-break"),
        pytest.param(build_return_panel_layout, "**I'm Back**", id="im-back"),
    ],
)
def test_row_titles_are_title_case(builder, row_title):
    texts = [i.content for i in _walk(builder()) if isinstance(i, discord.ui.TextDisplay)]
    assert any(t.startswith(row_title + "\n") for t in texts)
