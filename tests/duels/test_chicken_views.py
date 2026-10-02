"""Tests for chicken/views.py (ChickenView) — a Components V2 LayoutView."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.duels.chicken.views import ChickenView


def _buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button]:
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def test_chicken_view_single_bail_button():
    view = ChickenView(6, AsyncMock())
    buttons = _buttons(view)
    assert len(buttons) == 1
    assert buttons[0].custom_id == "chicken_bail:6"


def test_chicken_view_button_sits_in_an_action_row():
    view = ChickenView(6, AsyncMock())
    assert len(view.children) == 1
    assert isinstance(view.children[0], discord.ui.ActionRow)


def test_chicken_view_is_a_layout_view():
    assert isinstance(ChickenView(1, AsyncMock()), discord.ui.LayoutView)


def test_chicken_view_persistent():
    assert ChickenView(1, AsyncMock()).timeout is None


def test_chicken_view_render_replaces_content_and_keeps_control_row():
    view = ChickenView(1, AsyncMock())
    view.render([discord.ui.TextDisplay("hello")])
    assert isinstance(view.children[0], discord.ui.TextDisplay)
    assert isinstance(view.children[-1], discord.ui.ActionRow)
    assert len(_buttons(view)) == 1


async def test_bail_button_callback_invokes_handler():
    handler = AsyncMock()
    view = ChickenView(4, handler)
    button = _buttons(view)[0]
    interaction = object()
    await button.callback(interaction)  # type: ignore[arg-type]
    handler.assert_awaited_once_with(interaction, 4)
