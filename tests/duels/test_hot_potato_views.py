"""Tests for hot_potato views."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.duels.hot_potato.views import PassLayoutView
from tests.fakes import FakeUser, fake_interaction


def _button(view: PassLayoutView) -> discord.ui.Button:
    return next(c for c in view.walk_children() if isinstance(c, discord.ui.Button))


# ── PassLayoutView ───────────────────────────────────────────────────────────

async def test_pass_layout_view_callback_fires():
    on_pass = AsyncMock()
    view = PassLayoutView(game_id=5, on_pass=on_pass)

    interaction = fake_interaction(user=FakeUser(id=10))
    btn = _button(view)
    await btn.callback(interaction)

    on_pass.assert_awaited_once_with(interaction, 5)


def test_pass_layout_view_custom_id_encodes_game_id():
    view = PassLayoutView(game_id=42, on_pass=AsyncMock())
    assert _button(view).custom_id == "pass:42"


def test_pass_layout_view_label_contains_pass():
    view = PassLayoutView(game_id=1, on_pass=AsyncMock())
    assert "PASS" in _button(view).label


def test_pass_layout_view_style_is_primary():
    view = PassLayoutView(game_id=1, on_pass=AsyncMock())
    assert _button(view).style == discord.ButtonStyle.primary


def test_pass_layout_view_single_button():
    view = PassLayoutView(game_id=7, on_pass=AsyncMock())
    buttons = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1


async def test_pass_layout_view_different_game_ids_have_different_custom_ids():
    view1 = PassLayoutView(game_id=1, on_pass=AsyncMock())
    view2 = PassLayoutView(game_id=2, on_pass=AsyncMock())
    assert _button(view1).custom_id != _button(view2).custom_id


def test_pass_layout_view_render_resets_content_and_keeps_control_row_last():
    """render(items) must reset the card to *items* — no controls — then
    re-add the control row last, so a re-render never duplicates or loses
    the PASS button (duels.views.DuelLayoutView's contract)."""
    view = PassLayoutView(game_id=3, on_pass=AsyncMock())
    view.render([discord.ui.TextDisplay("## 🥔 Hot Potato\nfirst render")])
    view.render([discord.ui.TextDisplay("## 💥 BOOM!\nsecond render")])

    children = list(view.children)
    assert len(children) == 2
    assert isinstance(children[0], discord.ui.TextDisplay)
    assert "second render" in children[0].content
    assert children[-1] is view._control_row
    buttons = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 1


async def test_pass_layout_view_error_is_logged_and_answered_once(caplog):
    """A failing Pass press used to die in discord.py's default handler: no
    game context in the log, and the player got "interaction failed"."""
    view = PassLayoutView(game_id=9, on_pass=AsyncMock())
    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()

    with caplog.at_level(logging.ERROR, logger="dungeonkeeper.hot_potato"):
        await view.on_error(interaction, RuntimeError("boom"), _button(view))

    assert "game 9" in caplog.text
    interaction.response.send_message.assert_awaited_once_with(
        "Something went wrong.", ephemeral=True
    )


async def test_pass_layout_view_error_skips_reply_when_already_answered(caplog):
    view = PassLayoutView(game_id=9, on_pass=AsyncMock())
    interaction = MagicMock()
    interaction.response.is_done.return_value = True
    interaction.response.send_message = AsyncMock()

    with caplog.at_level(logging.ERROR, logger="dungeonkeeper.hot_potato"):
        await view.on_error(interaction, RuntimeError("boom"), _button(view))

    interaction.response.send_message.assert_not_awaited()
