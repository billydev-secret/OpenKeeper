"""Tests for quickdraw's Components V2 persistent view."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.duels.quickdraw.views import QuickdrawLayoutView
from tests.fakes import FakeUser, fake_interaction


def _control_button(view: QuickdrawLayoutView) -> discord.ui.Button:
    btn = next(iter(view._control_row.children))
    assert isinstance(btn, discord.ui.Button)
    return btn


# ── QuickdrawLayoutView ──────────────────────────────────────────────────────

async def test_fire_button_callback_fires():
    on_fire = AsyncMock()
    view = QuickdrawLayoutView(game_id=5, on_fire=on_fire)

    interaction = fake_interaction(user=FakeUser(id=10))
    btn = _control_button(view)
    await btn.callback(interaction)

    on_fire.assert_awaited_once_with(interaction, 5)


def test_fire_button_custom_id_encodes_game_id():
    view = QuickdrawLayoutView(game_id=42, on_fire=AsyncMock())
    assert _control_button(view).custom_id == "fire:42"


def test_fire_button_label():
    view = QuickdrawLayoutView(game_id=1, on_fire=AsyncMock())
    assert "Fire" in _control_button(view).label


def test_fire_button_style_is_danger():
    view = QuickdrawLayoutView(game_id=1, on_fire=AsyncMock())
    assert _control_button(view).style == discord.ButtonStyle.danger


def test_different_game_ids_have_different_custom_ids():
    view1 = QuickdrawLayoutView(game_id=1, on_fire=AsyncMock())
    view2 = QuickdrawLayoutView(game_id=2, on_fire=AsyncMock())
    assert _control_button(view1).custom_id != _control_button(view2).custom_id


def test_render_replaces_content_and_keeps_the_control_row_last():
    view = QuickdrawLayoutView(game_id=7, on_fire=AsyncMock())
    view.render([discord.ui.TextDisplay("## first"), discord.ui.TextDisplay("second")])
    assert [type(c) for c in view.children] == [
        discord.ui.TextDisplay, discord.ui.TextDisplay, discord.ui.ActionRow,
    ]
    assert view.children[-1] is view._control_row


def test_render_called_twice_does_not_duplicate_the_control_row():
    view = QuickdrawLayoutView(game_id=7, on_fire=AsyncMock())
    view.render([discord.ui.TextDisplay("one")])
    view.render([discord.ui.TextDisplay("two")])
    rows = [c for c in view.children if isinstance(c, discord.ui.ActionRow)]
    assert len(rows) == 1


@pytest.mark.parametrize("answered", [False, True])
async def test_on_error_is_logged_and_answered_once(caplog, answered):
    """A failing Fire press used to die in discord.py's default handler: no
    game context in the log, and the player got "interaction failed"."""
    view = QuickdrawLayoutView(game_id=9, on_fire=AsyncMock())
    interaction = MagicMock()
    interaction.response.is_done.return_value = answered
    interaction.response.send_message = AsyncMock()

    with caplog.at_level(logging.ERROR, logger="dungeonkeeper.quickdraw"):
        await view.on_error(interaction, RuntimeError("boom"), _control_button(view))

    assert "game 9" in caplog.text
    if answered:
        interaction.response.send_message.assert_not_awaited()
    else:
        interaction.response.send_message.assert_awaited_once_with(
            "Something went wrong.", ephemeral=True
        )
