"""Discord view for Hot Potato (group) — a single persistent Pass button.

Components V2 (wave 3b, ``docs/plans/components_v2_migration.md``):
``PassGroupLayoutView`` implements ``duels.views.DuelLayoutView`` —
``BaseGame`` builds it fresh through ``build_game_view`` and calls
``render(items)`` every time the card is (re)posted (accept, every
"continue" edit); it never builds the control row itself. Same frozen ``custom_id``
(``hpg_pass:{game_id}``), label, style and emoji as the pre-conversion
``discord.ui.View`` this replaces, so a restart re-registers the same
button under the same id and an in-flight game keeps working across the
upgrade. Mirrors the fixture in ``tests/duels/test_duels_render_hooks.py``.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import discord

from bot_modules.duels.views import FramedCard

log = logging.getLogger("dungeonkeeper.hot_potato_group")


class PassGroupLayoutView(FramedCard, discord.ui.LayoutView):
    """Persistent layout with one 🤲 Pass button. Holder/min-hold validation
    happens server-side in the cog's handle_interaction."""

    def __init__(
        self,
        game_id: int,
        on_press: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_press = on_press

        btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Pass",
            style=discord.ButtonStyle.danger,
            emoji="🤲",
            custom_id=f"hpg_pass:{game_id}",
        )
        btn.callback = self._press_cb
        # Built and added immediately, not just inside render(): a
        # cog_load re-attach registers this view with no content (see
        # BaseGame._reattach_result_view's reasoning for the same shape on
        # the result card), and it must still carry a live Pass button
        # until the next edit calls render().
        self._control_row = discord.ui.ActionRow(btn)
        self.add_item(self._control_row)


    async def _press_cb(self, interaction: discord.Interaction) -> None:
        await self._on_press(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("PassGroupLayoutView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
