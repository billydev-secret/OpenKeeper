"""Tests for hot_potato_group/views.py (PassGroupLayoutView — Components V2,
wave 3b). The Pass button lives inside a nested ``ActionRow`` (the view's own
frozen control row, re-added last on every ``render()``), not as a flat
``view.children`` entry — walk the tree rather than assume a flat list."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.duels.hot_potato_group.views import PassGroupLayoutView


def _buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button]:
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def test_pass_view_has_single_pass_button():
    view = PassGroupLayoutView(42, AsyncMock())
    buttons = _buttons(view)
    assert len(buttons) == 1
    assert buttons[0].custom_id == "hpg_pass:42"
    assert buttons[0].emoji is not None and buttons[0].emoji.name == "🤲"


def test_pass_view_is_persistent():
    view = PassGroupLayoutView(1, AsyncMock())
    assert view.timeout is None


def test_pass_view_carries_its_control_row_before_any_render_call():
    """A cog_load re-attach registers this view with no content (see
    ``BaseGame._reattach_result_view``'s reasoning for the same shape on the
    result card) — the Pass button must already be live, not wait for the
    first ``render()``."""
    view = PassGroupLayoutView(7, AsyncMock())
    assert len(_buttons(view)) == 1


def test_pass_view_render_places_items_before_the_control_row():
    view = PassGroupLayoutView(5, AsyncMock())
    items = [discord.ui.TextDisplay("🥔 held by 123")]
    view.render(items)
    assert list(view.children) == [items[0], view._control_row]
    assert len(_buttons(view)) == 1


def test_pass_view_render_replaces_previous_items_not_appends():
    view = PassGroupLayoutView(5, AsyncMock())
    view.render([discord.ui.TextDisplay("first")])
    view.render([discord.ui.TextDisplay("second")])
    texts = [c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)]
    assert texts == ["second"]
    assert len(_buttons(view)) == 1


async def test_pass_button_callback_invokes_handler():
    handler = AsyncMock()
    view = PassGroupLayoutView(99, handler)
    button = _buttons(view)[0]
    interaction = object()
    await button.callback(interaction)  # type: ignore[arg-type]
    handler.assert_awaited_once_with(interaction, 99)
