"""No-contact list — the mention/reply watcher.

Configuration (where alerts go, which role is pinged) lives on the dashboard,
per the project's config-belongs-on-the-web rule. A member's own list — add,
remove, see — is the 🛡️ No Contact card on the Server Features hub
(``server_features``), which also opens from ``/info``. The ``/nocontact``
command group lived here until 2026-09-26 (bug-list row 259); its copy and its
add/remove rules moved to ``no_contact_logic`` / ``no_contact_service`` so the
card draws on the same implementation the command did.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.utils import get_guild_channel_or_thread, jump_url
from bot_modules.no_contact import service as no_contact_service
from bot_modules.no_contact.logic import (
    KIND_MENTION,
    alert_ping_prefix,
    alerts_for_message,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.no_contact")


class NoContactCog(commands.Cog):
    """The mention/reply watcher that alerts staff about a no-contact pair."""

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        super().__init__()

    # ── Mention / reply watcher ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Alert staff when one member of a pair mentions or replies to the other.

        Reads mentions off the live message rather than ``message_mentions``
        so the alert doesn't depend on ingest having already run, and falls
        back to the DB only to resolve a reply's author when Discord didn't
        hand us the referenced message.
        """
        if message.guild is None or message.author.bot:
            return

        # Order matters — this runs on every message in the server. Everything
        # before the first DB touch is in-memory, and the partners lookup (one
        # indexed read, empty for virtually every author) is what gates the
        # rest. Resolving the reply author can itself hit the DB, so it waits
        # until we know this author has a partner at all; otherwise every
        # reply in the guild would pay for a lookup whose answer is discarded.
        mentioned_ids = [u.id for u in message.mentions if not u.bot]
        if not mentioned_ids and message.reference is None:
            return

        partners = await asyncio.to_thread(
            no_contact_service.no_contact_partners,
            self.bot.ctx.db_path,
            message.guild.id,
            message.author.id,
        )
        if not partners:
            return

        alerts = alerts_for_message(
            author_id=message.author.id,
            mentioned_ids=mentioned_ids,
            reply_to_author_id=await self._reply_author_id(message),
            no_contact_partners=partners,
        )
        if not alerts:
            return

        # Per-guild constants, read once rather than once per alert.
        settings = await asyncio.to_thread(
            no_contact_service.get_settings, self.bot.ctx.db_path, message.guild.id
        )
        for alert in alerts:
            await asyncio.to_thread(
                no_contact_service.record_event,
                self.bot.ctx.db_path,
                message.guild.id,
                actor_id=alert.actor_id,
                target_id=alert.target_id,
                kind=alert.kind,
                channel_id=message.channel.id,
                message_id=message.id,
            )
        # The event rows above are the durable record and are written whether
        # or not a guild ever configured a channel; only the Discord ping is
        # optional, so bail out of all of it in one place.
        channel = (
            get_guild_channel_or_thread(message.guild, settings["alert_channel_id"])
            if settings["alert_channel_id"]
            else None
        )
        if channel is None:
            return
        accent = await safe_resolve_accent(self.bot.ctx, message.guild, log_label="no contact")
        for alert in alerts:
            await self._post_alert(message, alert, channel, settings, accent)

    async def _reply_author_id(self, message: discord.Message) -> Optional[int]:
        """Author of the message this one replies to, or None.

        A Discord reply with the ping switched off still lands in front of the
        person replied to and never appears in ``message_mentions``, so this
        is not a redundant second trigger — it is the one someone reaches for
        once they notice mentions are being watched.
        """
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            return None if resolved.author.bot else resolved.author.id
        if ref.message_id is None:
            return None

        def _lookup() -> Optional[int]:
            from bot_modules.core.db_utils import open_db  # noqa: PLC0415

            with open_db(self.bot.ctx.db_path) as conn:
                row = conn.execute(
                    "SELECT author_id FROM messages WHERE message_id = ?",
                    (ref.message_id,),
                ).fetchone()
            return int(row["author_id"]) if row else None

        return await asyncio.to_thread(_lookup)

    async def _post_alert(
        self,
        message: discord.Message,
        alert,
        channel,
        settings: dict[str, int],
        accent,
    ) -> None:
        """Post one alert to the configured staff channel. Best-effort.

        Destination, settings and accent are resolved once by the caller and
        passed in — they are per-guild constants, and a message mentioning
        several partners would otherwise re-read them per alert.
        """
        assert message.guild is not None
        actor = message.guild.get_member(alert.actor_id)
        target = message.guild.get_member(alert.target_id)
        actor_name = (
            discord.utils.escape_markdown(actor.display_name)
            if actor
            else f"User {alert.actor_id}"
        )
        target_name = (
            discord.utils.escape_markdown(target.display_name)
            if target
            else f"User {alert.target_id}"
        )
        verb = "mentioned" if alert.kind == KIND_MENTION else "replied to"

        embed = discord.Embed(
            title="No-Contact Alert",
            description=(
                f"**{actor_name}** {verb} **{target_name}**, "
                f"who they are under a no-contact rule with."
            ),
            color=accent,
        )
        # ``message.guild`` is non-None here, so this is always a guild
        # channel — but the union still includes the DM types, so reach for
        # the mention defensively rather than narrowing on every subclass.
        embed.add_field(
            name="Channel",
            value=getattr(message.channel, "mention", f"<#{message.channel.id}>"),
            inline=True,
        )
        link = jump_url(message.guild.id, message.channel.id, message.id)
        embed.add_field(name="Message", value=f"[Jump]({link})", inline=True)
        # No message text: content retention is off by default, so it usually
        # isn't stored, and widening retention server-wide to serve this
        # feature would cost every other member's privacy. The link is enough
        # for a moderator to read it in place.
        embed.set_footer(text="The member being protected is not notified.")

        try:
            await channel.send(
                content=alert_ping_prefix(settings["alert_role_id"]) or None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False
                ),
            )
        except discord.HTTPException:
            log.warning("Failed to post no-contact alert in guild %s", message.guild.id)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(NoContactCog(bot))
