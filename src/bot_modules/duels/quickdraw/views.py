"""Quickdraw's persistent game view: a single FIRE button.

``QuickdrawLayoutView`` implements ``duels.views.DuelLayoutView`` — the
contract every wave 3b mini-game's ``build_game_view`` returns
(``docs/components_v2_style_guide.md`` § Code conventions). ``render``
clears the message down to nothing and re-adds the turn card's content
followed by this view's own control row, last, every time. The button
keeps its exact ``fire:{game_id}`` custom id and danger style from the
pre-V2 ``FireView`` this replaces — only the surrounding shape changed.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import discord

from bot_modules.duels.views import FramedCard

log = logging.getLogger("dungeonkeeper.quickdraw")


class QuickdrawLayoutView(FramedCard, discord.ui.LayoutView):
    def __init__(self, game_id: int, on_fire: Callable) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        btn: discord.ui.Button[Any] = discord.ui.Button(
            label="🔫 Fire",
            style=discord.ButtonStyle.danger,
            custom_id=f"fire:{game_id}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await on_fire(interaction, game_id)

        btn.callback = _cb  # type: ignore[method-assign]
        self._control_row = discord.ui.ActionRow(btn)
        self.add_item(self._control_row)


    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("QuickdrawLayoutView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
