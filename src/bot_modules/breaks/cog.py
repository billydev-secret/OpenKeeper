"""Take a Break — the two persistent panels and nothing else.

A member presses **💤 Start** on the "Take a Break" panel an admin posted,
and ``breaks.service`` saves their roles, strips them and puts on the break
role; **👋 Back** on the panel in the break channel gives everything back.
No slash commands: the config (role, channel, Spicy Lite) is on the
dashboard's Take a Break page, and so is a mod's End break.

Both views are registered once at load with ``bot.add_view`` — a persistent
view dispatches on ``custom_id`` alone, so every copy of either panel keeps
working across restarts.

Both panels live in a channel, so both carry the guild's panel art; the copy
registered at load passes none, since only the copy actually posted is ever
rendered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from bot_modules.breaks import service
from bot_modules.breaks.layouts import (
    BREAK_PANEL_TITLE,
    END_CUSTOM_ID,
    RETURN_PANEL_TITLE,
    START_CUSTOM_ID,
    build_break_panel_layout,
    build_reply_layout,
    build_return_panel_layout,
)
from bot_modules.core.layout import bind
from bot_modules.core.sticky import PanelContent
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.panels.title_service import with_title

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.breaks")


def _card(items: list[discord.ui.Item]) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


async def _answer(
    interaction: discord.Interaction, result: service.BreakResult, *, starting: bool
) -> None:
    await interaction.followup.send(
        view=_card(build_reply_layout(ok=result.ok, starting=starting, message=result.message)),
        ephemeral=True,
    )


class BreakPanelView(discord.ui.LayoutView):
    """The public "Take a Break" card. **Paradigm 1 — hub panel**, persistent.

    ``art`` is the guild's panel banner and footer. The instance registered at
    cog load passes none — a persistent view dispatches on ``custom_id``
    alone, so the art only matters on the copy that is actually posted.
    """

    def __init__(self, cog: BreaksCog, *, art: Optional[PanelArt] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        art = art or PanelArt(banner=None, footer=None)
        for item in build_break_panel_layout(
            banner_url=art.banner,
            footer_url=art.footer,
            title_in_banner=art.title_in_banner,
        ):
            self.add_item(item)
        bind(self, START_CUSTOM_ID, self.start)

    async def start(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
            return
        # One role call per role: a member with twenty roles outlasts the
        # three-second window, so defer first.
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await service.start_break(self.cog.bot.ctx, member)
        await _answer(interaction, result, starting=True)


class BreakReturnView(discord.ui.LayoutView):
    """The "I'm back" card in the break channel. Persistent, and art-carrying
    on the copy ``setup_break_channel`` posts."""

    def __init__(self, cog: BreaksCog, *, art: Optional[PanelArt] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        art = art or PanelArt(banner=None, footer=None)
        for item in build_return_panel_layout(
            banner_url=art.banner,
            footer_url=art.footer,
            title_in_banner=art.title_in_banner,
        ):
            self.add_item(item)
        bind(self, END_CUSTOM_ID, self.end)

    async def end(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Use this in the server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await service.end_break(self.cog.bot.ctx, member)
        await _answer(interaction, result, starting=False)


class BreaksCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(BreakPanelView(self))
        self.bot.add_view(BreakReturnView(self))

    async def _read_art(self, guild: discord.Guild, title: str) -> PanelArt:
        """The guild's panel art, with ``title`` drawn into the banner when the
        guild asked for that. Off the event loop: the first build of a new
        title draws every frame of the banner."""

        def _read() -> PanelArt:
            with self.bot.ctx.open_db() as conn:
                art = resolve_panel_art_conn(conn, guild.id)
            # Draws the titled banner on first use — off the loop, and after
            # the connection is closed.
            return with_title(art, title)

        return await asyncio.to_thread(_read)

    async def return_panel_view(self, guild: discord.Guild) -> discord.ui.LayoutView:
        """A bound "I'm back" view for ``service.setup_break_channel`` to post."""
        art = await self._read_art(guild, RETURN_PANEL_TITLE)
        return BreakReturnView(self, art=art)

    async def build_break_panel(self, guild: discord.Guild) -> PanelContent:
        """The panel as it should look now — what a post sends and what the
        panel refresher edits a recorded copy to (``panel_registry.render``),
        so a new banner reaches a posted panel on the next refresh."""
        art = await self._read_art(guild, BREAK_PANEL_TITLE)
        return PanelContent(layout=BreakPanelView(self, art=art))

    async def post_break_panel(self, guild: discord.Guild, channel) -> discord.Message | None:
        """Post the "Take a Break" panel into ``channel``.

        Entry point for the dashboard's panel poster (``panel_registry``). The
        route records the post and moves that copy on a second post into the
        same channel; a copy elsewhere keeps working — the button carries no
        state.
        """
        try:
            content = await self.build_break_panel(guild)
            return await channel.send(**content.send_kwargs())
        except discord.HTTPException:
            log.exception("breaks: couldn't post the break panel in guild %s", guild.id)
            return None


async def setup(bot: Bot) -> None:
    await bot.add_cog(BreaksCog(bot))
