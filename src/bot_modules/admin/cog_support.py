"""Support server link command.

`/games support` was the same thing under a games-shaped name and was removed
2026-07-28 — support is bot-wide, not games-specific, so a member looking for
help shouldn't have to know it lives under `/games`. This command absorbed that
one's card rendering, which is why it reaches into `games_help.layouts`: the
builder is shared, not games-only, and lives there for historical reasons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.games.help.layouts import build_support_layout

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot


class SupportCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(
        name="support",
        description="Get a link to the support Discord server.",
    )
    async def support(self, interaction: discord.Interaction) -> None:
        view = discord.ui.LayoutView(timeout=None)
        for item in build_support_layout():
            view.add_item(item)
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(SupportCog(bot))
