"""Mod Settings — the posted panel where moderators pick their staff job pings.

No slash command: an admin posts the panel from the dashboard's Moderation &
Privacy page (``panel_registry``), and nothing happens until they do. Each
row's button switches that ping on for the moderator who pressed it, or off
if they already have it, and answers privately. The panel view is registered
once at load with every ping's button, so every posted copy keeps working
across restarts whichever rows it shows.

Thin: ``moderation.settings.logic`` decides, ``moderation.settings.service`` reads and
applies, ``moderation.settings.layouts`` draws. The same cog strips every staff ping
role from a member who stops being a moderator.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.core.layout import bind
from bot_modules.core.sticky import PanelContent
from bot_modules.moderation.settings import logic, service
from bot_modules.panels import refresh_service as panel_refresh_service
from bot_modules.moderation.settings import staff_ping_service
from bot_modules.moderation.settings.layouts import build_mod_settings_hub_layout, ping_custom_id

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.mod_settings")

_UNAVAILABLE = "❌ That isn't available right now — try again in a minute."


class ModSettingsHubView(discord.ui.LayoutView):
    """The posted panel. **Paradigm 1 — hub panel**, persistent.

    Each handler finds the cog with ``get_cog`` at press time and answers
    "not available" when it isn't loaded, rather than holding a cog object a
    reload could replace.
    """

    def __init__(self, rows: Sequence[logic.StaffRow]) -> None:
        super().__init__(timeout=None)
        for item in build_mod_settings_hub_layout(rows):
            self.add_item(item)
        for row in rows:
            bind(self, ping_custom_id(row.key), self._press(row.key))

    @staticmethod
    def _press(key: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            cog = interaction.client.get_cog("ModSettingsCog")  # type: ignore[attr-defined]
            if not isinstance(cog, ModSettingsCog):
                await interaction.response.send_message(_UNAVAILABLE, ephemeral=True)
                return
            await cog.press(interaction, key)

        return _callback


class ModSettingsCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(ModSettingsHubView(logic.every_row()))

    async def press(self, interaction: discord.Interaction, key: str) -> None:
        """A moderator pressed ``key``'s button: toggle it, answer privately."""
        member = service.admitted_mod(self.bot.ctx, interaction.user)
        if member is None:
            await interaction.response.send_message(logic.NOT_A_MOD_TEXT, ephemeral=True)
            return
        # Making a role is a REST call: defer so a slow one can't lose the
        # interaction.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await service.toggle(self.bot.ctx, member, key)
        except Exception:
            # A deferred interaction with no followup leaves "thinking…"
            # hanging forever: always answer.
            log.exception("mod settings: toggle %s failed in guild %s", key, member.guild.id)
            await interaction.followup.send(_UNAVAILABLE, ephemeral=True)
            return
        await interaction.followup.send(result.text, ephemeral=True)
        if result.stale:
            await self._redraw(member.guild)

    async def _redraw(self, guild: discord.Guild) -> None:
        await panel_refresh_service.refresh_panel_key(
            self.bot, self.bot.ctx.db_path, guild, service.PANEL_KEY
        )

    @commands.Cog.listener(f"on_{staff_ping_service.CONFIG_CHANGE_EVENT}")
    async def _on_config_change(self, guild_id: int) -> None:
        """A dashboard save may have changed which jobs are offered: redraw
        the posted panels so the change shows without a restart."""
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self._redraw(guild)

    async def build_mod_settings_panel(self, guild: discord.Guild) -> PanelContent:
        """The panel as it should look now — what a post sends and what the
        panel refresher edits a recorded copy to (``panel_registry.render``)."""
        card = await service.read_offered(self.bot.ctx, guild)
        return PanelContent(layout=ModSettingsHubView(card.rows))

    async def post_mod_settings_panel(
        self, guild: discord.Guild, channel
    ) -> discord.Message | None:
        """Post the panel into ``channel``. Entry point for the dashboard's
        panel poster (``panel_registry``). The route records the post and
        moves that copy on a second post into the same channel."""
        try:
            content = await self.build_mod_settings_panel(guild)
            return await channel.send(**content.send_kwargs())
        except discord.HTTPException:
            log.exception("mod settings: couldn't post the panel in guild %s", guild.id)
            return None

    @commands.Cog.listener("on_member_update")
    async def _strip_staff_pings(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        await service.strip_on_step_down(self.bot.ctx, before, after)


async def setup(bot: Bot) -> None:
    await bot.add_cog(ModSettingsCog(bot))
