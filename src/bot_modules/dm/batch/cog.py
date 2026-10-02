"""Batched DMs — the send loop and message-delete retraction.

The logic lives in ``bot_modules.dm.batch``; this only runs it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from bot_modules.dm.batch.service import flush_due
from bot_modules.dm.batch.store import retract_refs

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.dm_batch")


class DMBatchCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        self._flush_loop.start()

    async def cog_unload(self) -> None:
        self._flush_loop.cancel()

    @tasks.loop(seconds=30)
    async def _flush_loop(self) -> None:
        try:
            await flush_due(self.bot)
        except Exception:
            log.exception("dm_batch: flush failed")

    @_flush_loop.before_loop
    async def _before_flush(self) -> None:
        await self.bot.wait_until_ready()

    def _retract(self, guild_id: int, message_ids: list[int]) -> None:
        with self.bot.ctx.open_db() as conn:
            retract_refs(conn, guild_id, message_ids)

    @commands.Cog.listener("on_raw_message_delete")
    async def _on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id:
            await asyncio.to_thread(self._retract, payload.guild_id, [payload.message_id])

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def _on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if payload.guild_id:
            await asyncio.to_thread(self._retract, payload.guild_id, list(payload.message_ids))


async def setup(bot: Bot) -> None:
    await bot.add_cog(DMBatchCog(bot))
