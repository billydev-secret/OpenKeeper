"""Redraw every posted channel panel once per boot and on every art change.

Glue only: the registry is ``core.panel_refresh``, the runner and the
posted-once panels' refresher are ``services.panel_refresh_service``. Plan:
``docs/plans/panel-refresh.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from discord.ext import commands

from bot_modules.core import panel_refresh
from bot_modules.panels.refresh_service import (
    POSTED_PANELS,
    posted_panels_refresher,
    refresh_all,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

#: How long after ``on_ready`` the boot pass waits. Cogs place and restore
#: their own panels as they come up; the refresh goes after them, so it edits
#: what they settled on rather than racing them for the same message.
BOOT_SETTLE_SECONDS = 60.0


class PanelRefreshCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._refresher = posted_panels_refresher(bot, bot.ctx.db_path)
        super().__init__()

    async def cog_load(self) -> None:
        panel_refresh.register(POSTED_PANELS, self._refresher)
        # A one-shot startup task, so a reconnect's second on_ready doesn't
        # run the pass again.
        self.bot.startup_task_factories.append(self._boot_refresh)

    async def cog_unload(self) -> None:
        panel_refresh.unregister(POSTED_PANELS, self._refresher)

    async def _boot_refresh(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(BOOT_SETTLE_SECONDS)
        await refresh_all(list(self.bot.guilds), reason="restart")

    @commands.Cog.listener()
    async def on_panel_art_change(self, guild_id: int) -> None:
        """The Branding page changed a banner, footer or the title setting —
        every panel in that guild redraws without waiting for its next repost."""
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await refresh_all([guild], reason="art change")


async def setup(bot: Bot) -> None:
    await bot.add_cog(PanelRefreshCog(bot))
