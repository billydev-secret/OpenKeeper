"""Tests for musical_chairs/views.py (SitView) — the DuelLayoutView the cog's
render hooks feed (``docs/specs/dk_pvp_games_suite_spec.md`` "The render-hooks
seam"). Model: ``PassLayoutView`` in ``tests/duels/test_duels_render_hooks.py``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.duels.musical_chairs.views import SitView


def test_sit_view_single_button():
    view = SitView(5, AsyncMock())
    assert isinstance(view, discord.ui.LayoutView)
    assert len(view._control_row.children) == 1
    btn = view._control_row.children[0]
    assert isinstance(btn, discord.ui.Button)
    assert btn.custom_id == "mc_sit:5"


def test_sit_view_persistent():
    assert SitView(1, AsyncMock()).timeout is None


def test_sit_view_render_keeps_control_row_last():
    view = SitView(1, AsyncMock())
    view.render([discord.ui.TextDisplay("round card")])
    children = list(view.children)
    assert isinstance(children[0], discord.ui.TextDisplay)
    assert children[0].content == "round card"
    assert children[-1] is view._control_row


def test_sit_view_render_replaces_prior_content():
    view = SitView(1, AsyncMock())
    view.render([discord.ui.TextDisplay("round 1")])
    view.render([discord.ui.TextDisplay("round 2")])
    texts = [c.content for c in view.children if isinstance(c, discord.ui.TextDisplay)]
    assert texts == ["round 2"]
    assert list(view.children)[-1] is view._control_row


async def test_sit_button_callback_invokes_handler():
    handler = AsyncMock()
    view = SitView(3, handler)
    button = view._control_row.children[0]
    interaction = object()
    await button.callback(interaction)  # type: ignore[arg-type]
    handler.assert_awaited_once_with(interaction, 3)
