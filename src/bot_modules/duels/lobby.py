"""LobbyView — shared join/start lobby for N-player (BaseGame) games.

Components V2, implementing :class:`bot_modules.duels.views.DuelLayoutView`:
persistent (timeout=None); expiry is handled by BaseGame's sweep loop, not
the view timeout. Button custom_ids embed the game_id so they survive a bot
restart and are rebuilt in BaseGame.cog_load. :meth:`render` lays the
lobby's own content (``duels.layouts.build_lobby_layout``) then re-adds the
Join/Leave/Start/Cancel row last, same pattern as every wave-3b mini-game's
own control-row view (e.g. ``duels/chicken/views.py``).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import discord

from bot_modules.duels.views import FramedCard

log = logging.getLogger("dungeonkeeper.duels")

_Handler = Callable[[discord.Interaction, int], Awaitable[None]]


class LobbyView(FramedCard, discord.ui.LayoutView):
    """Join lobby for multiplayer games. `✋ Join` / `🚪 Leave` for anyone,
    `▶️ Start` / `🚫 Cancel` gated to the host inside the handlers."""

    def __init__(
        self,
        game_id: int,
        host_id: int,
        on_join: _Handler,
        on_leave: _Handler,
        on_start: _Handler,
        on_cancel: _Handler,
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self._on_join = on_join
        self._on_leave = on_leave
        self._on_start = on_start
        self._on_cancel = on_cancel

        join_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Join", style=discord.ButtonStyle.success, emoji="✋",
            custom_id=f"lobby_join:{game_id}",
        )
        join_btn.callback = self._join_cb

        leave_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Leave", style=discord.ButtonStyle.secondary, emoji="🚪",
            custom_id=f"lobby_leave:{game_id}",
        )
        leave_btn.callback = self._leave_cb

        start_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Start", style=discord.ButtonStyle.primary, emoji="▶️",
            custom_id=f"lobby_start:{game_id}",
        )
        start_btn.callback = self._start_cb

        cancel_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, emoji="🚫",
            custom_id=f"lobby_cancel:{game_id}",
        )
        cancel_btn.callback = self._cancel_cb

        self._control_row = discord.ui.ActionRow(join_btn, leave_btn, start_btn, cancel_btn)
        self.add_item(self._control_row)

    async def _join_cb(self, interaction: discord.Interaction) -> None:
        await self._on_join(interaction, self.game_id)

    async def _leave_cb(self, interaction: discord.Interaction) -> None:
        await self._on_leave(interaction, self.game_id)

    async def _start_cb(self, interaction: discord.Interaction) -> None:
        await self._on_start(interaction, self.game_id)

    async def _cancel_cb(self, interaction: discord.Interaction) -> None:
        await self._on_cancel(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("LobbyView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
