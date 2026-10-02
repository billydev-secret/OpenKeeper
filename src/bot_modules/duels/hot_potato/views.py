"""Hot Potato game view: a single PASS button (Components V2)."""
from __future__ import annotations

import logging
from typing import Any, Callable

import discord

from bot_modules.duels.views import FramedCard

log = logging.getLogger("dungeonkeeper.hot_potato")


class PassLayoutView(FramedCard, discord.ui.LayoutView):
    """The persistent (``timeout=None``) control row for an active Hot
    Potato game — one PASS button, the same ``custom_id`` the prior
    embed-arm ``PassView`` used (``pass:{game_id}``), so a card already in
    flight across this conversion re-attaches to the same handler.

    Implements ``duels.views.DuelLayoutView``: :meth:`render` resets the
    card to the turn content ``BaseGame`` builds on every edit, re-adding
    this control row last so a re-render can never duplicate or lose the
    button.
    """

    def __init__(self, game_id: int, on_pass: Callable) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        btn: discord.ui.Button[Any] = discord.ui.Button(
            label="🥔 PASS",
            style=discord.ButtonStyle.primary,
            custom_id=f"pass:{game_id}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await on_pass(interaction, game_id)

        btn.callback = _cb
        self._control_row = discord.ui.ActionRow(btn)
        self.add_item(self._control_row)


    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
        /,
    ) -> None:
        log.exception("PassLayoutView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
