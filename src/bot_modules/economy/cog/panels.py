"""The four channel-bottom sticky panels and their stored ids."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.economy.cog.base import (
    EconomyBase,
)
from bot_modules.economy.cog.views import (
    EconomyPanelView,
    ShopPanelView,
)
from bot_modules.core.db_utils import parse_bool
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.economy.bank_panel_layout import (
    build_economy_panel_layout,
    economy_panel_title,
)
from bot_modules.economy.leaderboard import (
    collect_leaderboard_data,
)
from bot_modules.economy.shop_layouts import (
    SHOP_PANEL_TITLE,
)
from bot_modules.economy.shop import shop_sections
from bot_modules.economy.shop_items import ItemView
from bot_modules.economy import shop_items_service as shop_items_svc
from bot_modules.economy.bounty_views import (
    build_bounty_hub_panel,
)
from bot_modules.economy.auction_views import (
    build_auction_panel,
)
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.panels.title_service import with_title
from bot_modules.economy.auction_service import (
    attach_card_to_latest,
    card_ids,
)
from bot_modules.panels.sticky_registry import bot_chasing_resident
from bot_modules.economy.bounty_service import (
    bounty_enabled,
)
from bot_modules.economy.color_catalog_service import (
    catalog_price_range as color_price_range,
)
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    save_econ_settings,
)
from bot_modules.shared.message_store import get_known_users_bulk

if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.economy")


class PanelsMixin(EconomyBase):
    """The four channel-bottom sticky panels and their stored ids.

    Mixed into ``EconomyCog`` (economy_cog.py), which was split by area in
    standards audit row 4.29. Moved verbatim: the attributes it reads
    (``self.bot``, the StickyPanels, the caches) are set in the cog's
    ``__init__``.
    """

    # Set in EconomyCog.__init__.
    economy_panel: StickyPanel
    shop_panel: StickyPanel
    auction_panel: StickyPanel
    bounty_panel: StickyPanel
    _panel_resets: dict[int, asyncio.Task[None]]

    _PANEL_FIELDS = {
        "panel": ("guide_channel_id", "guide_message_id"),
        "shop": ("shop_channel_id", "shop_message_id"),
    }

    #: Which config key names the channel each permanent panel lives in. The
    #: auction card is deliberately absent — see _publish_panel_guilds. The
    #: economy panel's key still reads ``guide`` for the reason _PANEL_FIELDS
    #: gives: it is the surviving message's own id, not a stale name.
    _PANEL_CHANNEL_KEYS = {
        "panel": "econ_guide_channel_id",
        "shop": "econ_shop_channel_id",
        "bounty": "econ_bounty_channel_id",
    }

    #: Picks of the panel's How it works dropdown coalesce into one reset.
    PANEL_RESET_SECONDS = 8.0

    def schedule_panel_reset(self, guild_id: int) -> None:
        """Redraw the economy panel in place a few seconds from now.

        The How it works dropdown is persistent, so a member's pick stays
        showing on their screen until the message is edited, and re-picking
        it sends nothing. An in-place edit (not a restick — an edit never
        moves the panel) puts the placeholder back. A burst of picks shares
        one edit.
        """
        if guild_id in self._panel_resets:
            return
        self._panel_resets[guild_id] = asyncio.create_task(
            self._reset_panel(guild_id)
        )

    async def _reset_panel(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(self.PANEL_RESET_SECONDS)
            # False: a deleted economy panel is how staff retire it, the
            # same reading the live-board loop gives a 404.
            await self.economy_panel.refresh(guild_id, repost_if_missing=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("economy panel reset failed for guild %s", guild_id)
        finally:
            self._panel_resets.pop(guild_id, None)

    async def post_economy_panel(self, guild, channel):
        """Place the economy panel. Entry point for the dashboard's panel poster
        (``panel_registry``); replaced /bank post-guide on 2026-07-28, and
        absorbed /bank post-leaderboard's successor on 2026-08-18.

        Edits in place when the panel is already this channel's, so a re-brand
        refresh doesn't hop it to the bottom. Returns None only when Discord
        refused the post.

        The disabled-economy check lives here rather than in the route because
        it is a domain rule, not an access rule: posting a currency panel for a
        currency that doesn't exist would be wrong however you got here. It
        *raises* rather than returning None so the admin is told to turn the
        economy on — a bare None reads as "Discord rejected it" and sends them
        to check bot permissions instead.
        """
        await self._require_economy_enabled(guild.id)
        return await self.economy_panel.place_or_refresh(guild, channel)


    def _panel_ids(self, guild_id: int, kind: str) -> tuple[int, int]:
        chan_field, msg_field = self._PANEL_FIELDS[kind]
        with self.bot.ctx.open_db() as conn:
            s = load_econ_settings(conn, guild_id)
        if not s.enabled:
            return 0, 0
        return getattr(s, chan_field), getattr(s, msg_field)


    def _save_panel_ids(
        self, guild_id: int, kind: str, channel_id: int, message_id: int
    ) -> None:
        chan_field, msg_field = self._PANEL_FIELDS[kind]
        with self.bot.ctx.open_db() as conn:
            save_econ_settings(
                conn, guild_id, {chan_field: channel_id, msg_field: message_id}
            )


    def _bounty_panel_ids(self, guild_id: int) -> tuple[int, int]:
        """Where the hub is, or (0, 0) for "unposted" (the restick no-ops).

        (0, 0) in three cases: the economy is off; no board channel is set (the
        same condition ``bounty_enabled`` gates the whole feature on); or the
        hub was posted in a channel that is no longer the board.

        That last one is why the channel is stored rather than assumed. An
        admin can repoint ``bounty_channel_id`` at any time from the dashboard,
        and that save doesn't touch the panel ids. Pairing the old message id
        with the new channel would have the restick edit-404, post a *second*
        hub, and fail to delete the first — leaving two live hubs, the stale
        one listing bounties whose cards now post elsewhere.
        """
        with self.bot.ctx.open_db() as conn:
            s = load_econ_settings(conn, guild_id)
        if not s.enabled or not bounty_enabled(s):
            return 0, 0
        if int(s.bounty_panel_channel_id) != int(s.bounty_channel_id):
            return 0, 0
        return int(s.bounty_panel_channel_id), int(s.bounty_panel_message_id)


    def _save_bounty_panel_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        """Persist where the hub landed — its own fields, never the board's.

        Writing ``bounty_channel_id`` here would let a mis-posted hub silently
        *redefine* which channel the board is, moving every future bounty card
        with it; ``post_bounty_panel`` refuses any channel but the board, so
        these two agree on every normal path and diverge only after a repoint.
        """
        with self.bot.ctx.open_db() as conn:
            save_econ_settings(
                conn,
                guild_id,
                {
                    "bounty_panel_channel_id": channel_id,
                    "bounty_panel_message_id": message_id,
                },
            )


    async def _drop_stale_bounty_hub(self, guild: discord.Guild) -> None:
        """Delete a hub left behind in a channel that is no longer the board.

        Its buttons are static custom_ids, so an orphaned hub keeps working —
        Post a bounty on it would file a card into the *new* board channel.
        Called when the panel is (re)posted, which is the moment an admin who
        just repointed the board is here to see it cleaned up. Best-effort:
        the ids are cleared either way so this can't be retried forever.
        """
        def _read() -> tuple[int, int, int]:
            with self.bot.ctx.open_db() as conn:
                s = load_econ_settings(conn, guild.id)
                return (
                    int(s.bounty_panel_channel_id),
                    int(s.bounty_panel_message_id),
                    int(s.bounty_channel_id),
                )

        old_channel, old_message, board = await asyncio.to_thread(_read)
        if not old_message or old_channel == board:
            return
        channel = guild.get_channel(old_channel)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.get_partial_message(old_message).delete()
            except discord.HTTPException:
                log.debug("econ bounty: stale hub already gone", exc_info=True)
        await asyncio.to_thread(self._save_bounty_panel_ids, guild.id, 0, 0)


    async def _build_bounty_panel(self, guild: discord.Guild) -> PanelContent:
        return PanelContent(layout=await build_bounty_hub_panel(self.bot, guild))


    async def post_bounty_panel(self, guild, channel=None):
        """Place the Bounty Board hub. Entry point for the dashboard's panel
        poster (``panel_registry``); the board's only entry point since
        ``/bounty`` was deleted.

        Takes its destination from ``bounty_channel_id`` rather than from the
        caller, because that was always the only answer: the hub's ids are
        looked up *through* that setting, so a hub anywhere else would be
        adopted by the restick as though it were in the board channel and then
        edited into the wrong place. This used to be enforced by refusing every
        other channel, which left the dashboard drawing a picker beside the
        Bounty Board Channel setting whose every other answer was an error —
        the duplication that prompted the 2026-08-29 audit. ``channel`` is
        accepted and ignored so the route can keep one uniform call shape (see
        ``_OWN_CHANNEL_METHODS``).
        """
        await self._require_economy_enabled(guild.id)
        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not bounty_enabled(settings):
            raise ValueError(
                "No bounty board channel is set — pick one under Economy → "
                "Settings before posting the board panel."
            )
        channel = guild.get_channel(int(settings.bounty_channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError(
                "The bounty board channel "
                f"(<#{int(settings.bounty_channel_id)}>) isn't a text channel "
                "in this server any more — pick a new one under Economy → "
                "Settings."
            )
        # Two panels that both chase bot posts can't share a channel: there is
        # one bottom slot, they take it from each other on every trigger, and
        # whichever lost is buried. Before core.sticky learned to ignore another
        # panel's placement (2026-08-06) this configuration re-posted forever.
        # Refusing mirrors what _sticky_check already does for an auction card.
        rival = await asyncio.to_thread(
            self._bot_chasing_rival, guild.id, int(channel.id)
        )
        if rival is not None:
            raise ValueError(
                f"<#{int(channel.id)}> already has {rival} stuck to its bottom, "
                "and both panels follow the bot's own posts — they would take "
                "the bottom slot from each other and one would always be "
                "buried. Give the bounty board its own channel."
            )
        # If the board was repointed, the hub still sitting in the old channel
        # has live buttons. Clear it out before placing the new one.
        await self._drop_stale_bounty_hub(guild)
        return await self.bounty_panel.place_or_refresh(guild, channel)


    def _bot_chasing_rival(self, guild_id: int, channel_id: int) -> str | None:
        with self.bot.ctx.open_db() as conn:
            return bot_chasing_resident(
                conn, guild_id, channel_id, excluding="economy-bounty"
            )


    async def refresh_bounty_hub_panel(self, guild: discord.Guild) -> None:
        """Repaint the hub after a pot or the open list changed.

        A no-op until the hub has actually been posted: ``place_or_refresh``
        would otherwise *create* one, so a guild that never set the board up
        would sprout a panel the first time somebody chipped in.
        """
        channel_id, message_id = await asyncio.to_thread(
            self._bounty_panel_ids, guild.id
        )
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self.bounty_panel.place_or_refresh(guild, channel)


    async def _build_economy_panel(self, guild: discord.Guild) -> PanelContent:
        now_ts = time.time()

        def _collect():
            # Settings ride the connection the board data already needs; this
            # runs on every sticky repost, so a second connect+PRAGMA for one
            # settings row was the most-repeated waste in the cog.
            #
            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                data = collect_leaderboard_data(conn, guild.id, now_ts)
                known = get_known_users_bulk(
                    conn, guild.id,
                    [uid for uid, _ in data.top_earners]
                    + ([data.last_winner_id] if data.last_winner_id else [])
                    + [uid for uid, _ in filter(None, (
                        data.casino_biggest, data.casino_luckiest))],
                )
                art = resolve_panel_art_conn(conn, guild.id)
            # Draws the titled banner on first use, after the connection closes.
            return settings, data, known, with_title(art, economy_panel_title(settings))

        settings, data, known, art = await asyncio.to_thread(_collect)

        def _name(uid: int) -> str:
            # Deliberately NOT resolve_name_from, which every other embed here
            # uses. build_leaderboard_embed renders this name inside an inline
            # `code span` so the columns align (see leaderboard.py's _pad), and
            # Discord renders no markdown in one — so the resolver's escaping
            # would print literal backslashes in the cell, and its `<@id>` last
            # resort would show as a bare "<@123>" rather than resolving.
            # The code span is what makes the name safe here; guarding the
            # backtick that could close it early is leaderboard.py's job.
            member = guild.get_member(uid)
            if member:
                return member.display_name
            return known.get(uid) or f"User {uid}"

        return PanelContent(layout=EconomyPanelView(build_economy_panel_layout(
            settings, data, _name, now_ts=now_ts,
            banner_url=art.banner,
            footer_url=art.footer,
            title_in_banner=art.title_in_banner,
        )))


    async def _build_shop_panel(self, guild: discord.Guild) -> PanelContent:
        """The channel shop panel: a row per stocked section, with its blurb.

        Every route to the panel — ``/bank post-shop`` and the sticky repost
        alike — arrives through here, so the two can't render different
        panels. No ``user_id``: the panel is member-agnostic and names no
        prices — the listing one tap away is correct for whoever opens it.
        """
        def _read() -> tuple[EconSettings, list[ItemView], bool, PanelArt]:
            # One connection for all of it, like _build_economy_panel — this
            # runs on every debounced sticky repost. The palette's range is
            # read only to know whether the palette row exists at all.
            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                items = shop_items_svc.shop_items_for(conn, guild.id, now=time.time())
                has_palette = color_price_range(
                    conn, guild.id, settings.price_role_preset
                ) is not None
                art = resolve_panel_art_conn(conn, guild.id)
            # Draws the titled banner on first use, after the connection closes.
            return settings, items, has_palette, with_title(art, SHOP_PANEL_TITLE)

        settings, items, has_palette, art = await asyncio.to_thread(_read)
        # Read for nobody (no owned set), so it is the same for every reader.
        sections = shop_sections(settings, items=items, has_palette=has_palette)
        return PanelContent(layout=ShopPanelView(sections, art=art))


    @commands.Cog.listener()
    async def on_shop_stock_change(self, guild_id: int) -> None:
        """The dashboard changed what the shop sells (a switch, a Special,
        the palette) — redraw the shop panel's list in place rather than
        leaving it stale until the channel next talks."""
        await self.shop_panel.refresh(guild_id)


    def _auction_card_ids(self, guild_id: int) -> tuple[int, int]:
        with self.bot.ctx.open_db() as conn:
            if not load_econ_settings(conn, guild_id).enabled:
                return 0, 0
            return card_ids(conn, guild_id)


    def _save_auction_card_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            attach_card_to_latest(conn, guild_id, channel_id, message_id)


    async def _build_auction_panel(self, guild: discord.Guild) -> PanelContent:
        content = await build_auction_panel(self.bot, guild)
        if content is None:
            # The auction finished between this restick being armed and the
            # placement reaching build. Raising is the hard stop that keeps
            # place() from posting a card for an auction that is over — build
            # runs before send, so nothing is posted. The close paths call
            # _release_panel the moment the state flips, which normally makes
            # the restick return earlier than this; getting here is the narrow
            # residual race, and core logs it at error level.
            raise RuntimeError(f"auction finished before its card was rebuilt "
                               f"(guild {guild.id})")
        return content


    @commands.Cog.listener("on_message")
    async def _restick_panels(self, message: discord.Message) -> None:
        """Keep all four panels at the bottom of their channels.

        One listener for four panels: each ignores activity outside its own
        channel, so a message usually arms at most one repost. The auction
        card is the exception — it can share a channel with the economy or
        shop panel, in which case both re-stick and one ends up second. ``/bank auction start`` warns the mod before that happens, and
        refuses outright for the two channels whose resident panel chases bot
        posts (the casino hub and the bounty board) because there the card
        would never surface at all — see ``_sticky_check`` in
        ``economy/auction_views.py``.

        The bounty hub is the one panel here with ``restick_on_bot`` on, so it
        follows its own board's new cards down instead of being stranded above
        them. Its re-renders are edits, which fire no MESSAGE_CREATE, so only a
        genuinely new card moves it — see the panel's declaration.
        """
        for panel in self._panels:
            await panel.on_message(message)


    @property
    def _panels(self) -> tuple[StickyPanel, ...]:
        return (
            self.economy_panel,
            self.shop_panel,
            self.auction_panel,
            self.bounty_panel,
        )


    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_panel_channels(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear panel ids whose channel just stopped existing.

        Nothing breaks without it — a dead channel id resolves to None and the
        restick returns early — but the ids outlive the channel, so the dashboard
        goes on reporting a panel that cannot be there.
        """
        for panel in self._panels:
            await panel.on_channel_delete(channel)


    async def post_shop_panel(self, guild, channel):
        """Place the perk-shop panel. See post_economy_panel."""
        await self._require_economy_enabled(guild.id)
        return await self.shop_panel.place_or_refresh(guild, channel)


    def _panel_guilds(self) -> dict[str, set[int]]:
        """Guilds whose economy is on and which have each panel's channel set.

        One query for all three rather than three ``load_econ_settings`` calls:
        this exists to keep work *off* the message path, so paying five
        settings loads to save five settings loads would be silly.
        """
        keys = ("econ_enabled", *self._PANEL_CHANNEL_KEYS.values())
        with self.bot.ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT guild_id, key, value FROM config WHERE key IN "
                f"({','.join('?' * len(keys))})",
                keys,
            ).fetchall()
        stored: dict[int, dict[str, str]] = {}
        for row in rows:
            stored.setdefault(int(row["guild_id"]), {})[str(row["key"])] = str(
                row["value"]
            )
        out: dict[str, set[int]] = {kind: set() for kind in self._PANEL_CHANNEL_KEYS}
        for guild_id, values in stored.items():
            if not parse_bool(values.get("econ_enabled", ""), False):
                continue
            for kind, key in self._PANEL_CHANNEL_KEYS.items():
                if (values.get(key) or "0") not in ("", "0"):
                    out[kind].add(guild_id)
        return out


    async def _publish_panel_guilds(self) -> None:
        """Give the three permanent panels their known-guilds fast path.

        Without this ``_known`` stays None and every message in every guild
        costs a cached id lookup per panel — four sequential ones, since
        ``_restick_panels`` forwards to each in turn — plus four separate
        ``load_econ_settings`` reads whenever the TTL lapses. Every other
        migrated cog published this from the start; this one never did (found
        2026-08-06).

        The **auction card is excluded on purpose.** It is the one panel that
        comes and goes, it is posted directly rather than through ``place`` (so
        nothing calls ``_remember`` to add its guild), and ``auction_views``
        calls ``forget`` right after posting — which would *discard* the guild
        from a published set and leave the card un-sticky. Its TTL cache is the
        gate, and ``forget`` is what keeps it honest.
        """
        by_kind = await asyncio.to_thread(self._panel_guilds)
        self.economy_panel.set_known_guilds(by_kind["panel"])
        self.shop_panel.set_known_guilds(by_kind["shop"])
        self.bounty_panel.set_known_guilds(by_kind["bounty"])
