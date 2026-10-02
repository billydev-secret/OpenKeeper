"""``/wordcloud`` — a picture of what a channel has been talking about.

Moderator-only, and the reply is ephemeral. Read permission is the gate — a
moderator can cloud any channel they can read, and no channel they can't.

The bot stores no message text, so every cloud reads recent history straight
off Discord: at most the last ten minutes, storing nothing. The database only
supplies timestamps, to pick the busiest channels for an "everywhere" cloud.

Its two dials — the message cap and the default preset — live on the dashboard
(Channels & Messages → Word Cloud), per CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_config_value
from bot_modules.analytics.sentiment_service import score_text
from bot_modules.word_cloud import corpus, embeds, logic, presets, render
from bot_modules.word_cloud.embeds import FILENAME

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)



def _can_read(channel: object, member: discord.Member) -> bool:
    """Whether ``member`` may read ``channel``'s history.

    The single definition of this command's gate, used both for a named
    channel and for every candidate in the "everywhere" fan-out, so the two
    can never disagree about what "you can read it" means.

    A *private* thread needs more than the inherited answer:
    ``Thread.permissions_for`` only applies the parent channel's overwrites and
    knows nothing about who was invited, so a moderator never added to one
    still comes back "allowed". Discord's own rule is invitation or Manage
    Threads, and only the second half is knowable from the cache.
    """
    try:
        perms = channel.permissions_for(member)  # type: ignore[attr-defined]
    except discord.ClientException:
        # A thread whose parent isn't cached can't be permission-checked, and
        # one stale thread must not sink the whole command.
        return False
    if not (perms.read_messages and perms.read_message_history):
        return False
    if isinstance(channel, discord.Thread) and channel.is_private():
        return bool(perms.manage_threads)
    return True


def _readable_channel_ids(
    guild: discord.Guild, member: discord.Member
) -> list[int]:
    """Every channel in ``guild`` whose history ``member`` may read.

    Threads are included: a thread is its own messageable channel, so leaving
    them out would drop whole conversations.
    Archived threads are absent from ``guild.threads`` and so are never swept
    up by "everywhere" — running the command inside one still clouds it, since
    being in it is proof of access.
    """
    return [
        channel.id
        for channel in list(guild.text_channels) + list(guild.threads)
        if _can_read(channel, member)
    ]


class WordCloudCog(commands.Cog):
    """The ``/wordcloud`` command."""

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    # -- config -----------------------------------------------------------

    def _read_dials(self, guild_id: int) -> tuple[int, str]:
        """The message cap and the default preset.

        Run in a worker thread: this handler has already deferred, so there is
        no reason to make the event loop wait for sqlite.

        Both dials are read strictly, without the legacy ``guild_id=0``
        fallback: these keys are new, so a row at 0 could only be the home
        guild's, and a second guild inheriting someone else's cap and colour
        scheme is exactly the silent cross-guild bleed that flag exists to
        stop. The dashboard reads them the same way, so panel and command
        can't disagree.
        """
        with self.bot.ctx.open_db() as conn:
            raw_cap = get_config_value(
                conn,
                logic.CAP_KEY,
                str(logic.DEFAULT_CAP),
                guild_id,
                allow_legacy_fallback=False,
            )
            preset = get_config_value(
                conn,
                logic.PRESET_KEY,
                presets.DEFAULT_PRESET,
                guild_id,
                allow_legacy_fallback=False,
            )
        return logic.clamp_cap(raw_cap), preset

    # -- corpus -----------------------------------------------------------

    def _rank_channels(
        self, guild_id: int, channel_ids: list[int], limit: int
    ) -> list[int]:
        with self.bot.ctx.open_db() as conn:
            return corpus.recent_channel_ids(
                conn, guild_id=guild_id, channel_ids=channel_ids, limit=limit
            )

    async def _read_live(
        self,
        guild: discord.Guild,
        channel_ids: list[int],
        after: discord.abc.Snowflake | None,
        cap: int,
        author_id: int | None,
        since_ts: float,
    ) -> list[logic.Doc]:
        """Read recent history off Discord. Stores nothing.

        Bot authors are skipped, except when one was explicitly named —
        otherwise the cloud is the bot's own card copy. Messages Discord still
        serves are by definition not deleted.
        """
        docs: list[logic.Doc] = []
        for channel_id in channel_ids:
            if len(docs) >= cap:
                break
            channel = guild.get_channel_or_thread(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                # oldest_first is implied True whenever ``after`` is given, which
                # would hand back the *start* of the window on a channel busier
                # than the cap. apply_cap means "most recent", so say so
                # explicitly.
                async for message in channel.history(
                    limit=cap - len(docs), after=after, oldest_first=False
                ):
                    if message.created_at.timestamp() < since_ts:
                        continue
                    if author_id is not None:
                        if message.author.id != author_id:
                            continue
                    elif message.author.bot:
                        continue
                    if message.content:
                        docs.append(logic.Doc(text=message.content))
            except discord.HTTPException:
                # One unreadable channel must not sink the whole command.
                log.debug("word cloud: history failed for %s", channel_id, exc_info=True)
                continue
        return docs

    # -- command ----------------------------------------------------------

    @app_commands.command(
        name="wordcloud",
        description="Picture the words a channel has been using (moderators only).",
    )
    @app_commands.describe(
        window="How far back, like 5m. Up to 10 minutes, the default.",
        channel="Which channel. Defaults to this one.",
        member="Only this person's words.",
        everywhere="Use every channel you can read instead of just one.",
        preset="Visual style.",
        color="Colour words by the mood of their messages, or by the preset palette.",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name=p.label, value=p.key) for p in presets.PRESETS
        ],
        color=[
            app_commands.Choice(name="By mood", value="sentiment"),
            app_commands.Choice(name="By palette", value="palette"),
        ],
    )
    async def wordcloud(  # noqa: PLR0913 - each option is a distinct user choice
        self,
        interaction: discord.Interaction,
        window: str = "10m",
        channel: discord.TextChannel | None = None,
        member: discord.Member | None = None,
        everywhere: bool = False,
        preset: app_commands.Choice[str] | None = None,
        color: app_commands.Choice[str] | None = None,
    ) -> None:
        # Every gate lives in logic.plan_scope; this resolves the Discord
        # objects it needs and does what it is told.
        guild = interaction.guild
        invoker = interaction.user
        in_guild = guild is not None and isinstance(invoker, discord.Member)

        target = channel or interaction.channel if in_guild else None
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            target = None

        target_readable = False
        if target is not None and isinstance(invoker, discord.Member):
            target_readable = _can_read(target, invoker)

        scope = logic.plan_scope(
            is_mod=in_guild and self.bot.ctx.is_mod(interaction),
            in_guild=in_guild,
            everywhere=everywhere,
            everywhere_ids=(
                _readable_channel_ids(guild, invoker)
                if everywhere and guild is not None and isinstance(invoker, discord.Member)
                else ()
            ),
            target_id=target.id if target is not None else None,
            target_readable=target_readable,
            target_label=target.mention if target is not None else "",
            picked_channel=channel is not None,
        )
        if isinstance(scope, logic.Refusal):
            await interaction.response.send_message(scope.message, ephemeral=True)
            return

        try:
            span = logic.parse_window(window)
        except logic.WindowError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        assert guild is not None
        try:
            await self._run(interaction, guild, span, scope, member, preset, color)
        except Exception:
            log.exception("word cloud failed")
            await interaction.followup.send(
                "Something went wrong building that cloud.", ephemeral=True
            )

    async def _run(  # noqa: PLR0913 - mirrors the command's own options
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        span: timedelta,
        scope: logic.Scope,
        member: discord.Member | None,
        preset_choice: app_commands.Choice[str] | None,
        color_choice: app_commands.Choice[str] | None,
    ) -> None:
        cap, default_preset = await asyncio.to_thread(self._read_dials, guild.id)
        preset = presets.resolve_preset(
            preset_choice.value if preset_choice else default_preset
        )
        want_sentiment = (
            color_choice.value if color_choice else "sentiment"
        ) == "sentiment"

        author_id = member.id if member else None
        channel_ids = list(scope.channel_ids)
        notes: list[str] = [scope.note] if scope.note else []

        # The card is labelled with the span actually read, not the ask.
        span, clamped = logic.clamp_live_window(span)
        if clamped:
            notes.append(logic.live_clamp_note())
        if len(channel_ids) > logic.LIVE_CHANNEL_FANOUT:
            channel_ids = await asyncio.to_thread(
                self._rank_channels,
                guild.id,
                channel_ids,
                logic.LIVE_CHANNEL_FANOUT,
            )
            notes.append(
                f"Reading the {logic.LIVE_CHANNEL_FANOUT} busiest channels, "
                "to keep this quick."
            )
        since_ts = time.time() - span.total_seconds()
        # Ask Discord to start after this point rather than paging back
        # through the whole channel and filtering client-side.
        after = discord.Object(
            id=discord.utils.time_snowflake(discord.utils.utcnow() - span)
        )
        # One over the cap, so apply_cap can tell a window that merely fits
        # from one that was truncated.
        docs = await self._read_live(
            guild, channel_ids, after, cap + 1, author_id, since_ts
        )

        docs, capped = logic.apply_cap(docs, cap)
        if capped:
            notes.append(
                f"Capped at the most recent {cap:,} messages, so this covers "
                "less than the full window."
            )

        if want_sentiment:
            docs = await asyncio.to_thread(logic.score_docs, docs, score_text)
        stats = await asyncio.to_thread(logic.build_stats, docs)
        if not stats:
            await interaction.followup.send(
                logic.empty_message(
                    member.display_name if member else None, scope.label
                ),
                ephemeral=True,
            )
            return

        by_sentiment = want_sentiment and any(s.sentiment is not None for s in stats)
        if want_sentiment and not by_sentiment:
            notes.append("Nothing here could be mood-scored, so colours are the preset's.")

        png = await asyncio.to_thread(
            render.render_png, stats, preset, by_sentiment=by_sentiment
        )

        embed = embeds.build_cloud_embed(
            message_count=len(docs),
            member_name=member.display_name if member else None,
            scope_label=scope.label,
            span=span,
            source_label="live chat",
            by_sentiment=by_sentiment,
            notes=notes,
            color=await safe_resolve_accent(self.bot, guild),
        )
        await interaction.followup.send(
            embed=embed,
            file=discord.File(fp=io.BytesIO(png), filename=FILENAME),
            ephemeral=True,
        )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(WordCloudCog(bot))
