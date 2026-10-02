"""Discord UI views specific to Pressure Cooker."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import discord

from bot_modules.core import meters
from bot_modules.duels.views import FramedCard

log = logging.getLogger("dungeonkeeper.pressure")


def gauge_bar(gauge: int, width: int = 20) -> str:
    """Render a text progress bar. e.g. ``▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱ 42/100``

    Code-spanned: the gauge is repainted on every pump, and a bare ``▰▱`` run
    changes pixel length as it fills even though the character count is fixed.
    """
    clamped = max(0, min(gauge, 100))
    return meters.mono(f"{meters.fill(clamped, 100, width)} {gauge}/100")


class GameView(FramedCard, discord.ui.LayoutView):
    """The turn card's control row — persistent (timeout=None), custom_id
    encodes game_id. Components V2 twin of the old ``discord.ui.View``
    (``docs/plans/components_v2_migration.md`` wave 3b): implements
    ``duels.views.DuelLayoutView`` (``render``), same shape as
    ``PassLayoutView`` in ``tests/duels/test_duels_render_hooks.py``. The PUMP
    button lives in its own ``ActionRow`` — a bare ``Button`` cannot sit at
    a V2 message's top level — built once in ``__init__`` and re-added last
    by every :meth:`render`, so a re-render can never duplicate or lose it.
    """

    def __init__(
        self,
        game_id: int,
        on_pump: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_pump = on_pump

        pump_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Pump",
            style=discord.ButtonStyle.danger,
            emoji="💨",
            custom_id=f"pump:{game_id}",
        )
        pump_btn.callback = self._pump_callback
        self._control_row = discord.ui.ActionRow(pump_btn)
        self.add_item(self._control_row)

    async def _pump_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s pumped (game %d)", interaction.user.display_name, self.game_id)
        await self._on_pump(interaction, self.game_id)


    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("GameView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
