"""Components V2 view for Chicken — a single BAIL button (all players hold
at start), in its own control row per ``duels.views.DuelLayoutView``.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

from bot_modules.duels.views import FramedCard

log = logging.getLogger("dungeonkeeper.chicken")


class ChickenView(FramedCard, discord.ui.LayoutView):
    """Implements :class:`bot_modules.duels.views.DuelLayoutView`: persistent
    (``timeout=None``), the Bail button built once in ``__init__`` and kept
    in ``self._control_row``, re-added last on every :meth:`render`."""

    def __init__(
        self,
        game_id: int,
        on_press: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_press = on_press

        btn: discord.ui.Button = discord.ui.Button(
            label="Bail",
            style=discord.ButtonStyle.danger,
            emoji="🐔",
            custom_id=f"chicken_bail:{game_id}",
        )
        btn.callback = self._press_cb
        self._control_row = discord.ui.ActionRow(btn)
        self.add_item(self._control_row)


    async def _press_cb(self, interaction: discord.Interaction) -> None:
        await self._on_press(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ChickenView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
