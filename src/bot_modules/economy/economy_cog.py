"""Economy — the ``/bank`` command surface (wallet view + mod grants).

Thin cog over ``bot_modules.economy.service``: it loads per-guild
``econ_`` settings on each interaction (cheap KV reads, no cache for stage 0),
resolves the branded currency naming, and renders the bank/wallet/leaderboard
Components V2 cards (the Perk Shop still renders embeds — its own conversion
is a separate wave). See docs/specs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.economy.cog.panels import PanelsMixin
from bot_modules.economy.cog.perks import PerksMixin
from bot_modules.economy.cog.quests import QuestsMixin
from bot_modules.economy.cog.shop import ShopMixin
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.core.paths import data_dir
from bot_modules.core.sticky import StickyPanel
from bot_modules.economy.view_helpers import (
    unit as _unit,
)
from bot_modules.economy.wallet import build_wallet_layout
from bot_modules.economy.layouts import (
    build_grant_receipt_layout,
    build_mute_toggle_layout,
)
from bot_modules.economy.transfers import build_payment_receipt_layout, pay_disclosure
from bot_modules.economy.notice_batch import NOTICE_BATCH_KIND, queue_notice, render_notices
from bot_modules.dm.batch.logic import PendingDM, unique_actors
from bot_modules.dm.batch.service import register_handler, unregister_handler
from bot_modules.economy.perks import (
    FEATURE_GATED,
    GIFTABLE_PERKS,
    NO_CONFIG_PERKS,
    PERK_LABELS,
    SELF_PERKS,
    perk_on_sale,
    perk_price,
    perks_on_sale,
)
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    revoke_role_perks,
)
from bot_modules.economy.quest_views import (
    QuestApproveButton,
    QuestDenyButton,
)
from bot_modules.economy.bounty_views import (
    BountyAwardButton,
    BountyCancelButton,
    BountyChipInButton,
    BountyHubChipButton,
    BountyHubPostButton,
)
from bot_modules.economy.auction_views import (
    AuctionBidButton,
    cancel_open_auction,
    end_open_auction,
    settle_and_announce,
    start_auction,
)
from bot_modules.no_contact.service import is_no_contact_conn
from bot_modules.economy.auction_service import (
    open_auction_guild_ids,
)
from bot_modules.economy.pin_views import (
    PinApproveButton,
    PinDenyButton,
)
from bot_modules.economy.theme_views import (
    ThemeApproveButton,
    ThemeDenyButton,
)
from bot_modules.economy.rentals_service import (
    cancel_all_for_member,
    effective_entitlements,
    list_member_rentals,
    rent_perk,
)
from bot_modules.economy import emoji_service as emoji_svc
from bot_modules.economy import wager_service as wager_svc
from bot_modules.economy import quests_service as quests_svc
from bot_modules.economy.service import (
    EconSettings,
    apply_credit,
    get_balance,
    get_ledger,
    get_notify_muted,
    get_streak_shields,
    load_econ_settings,
    notify_member,
    set_notify_muted,
    transfer_currency,
)
from bot_modules.shared.name_resolver import (
    build_name_fn,
)
from bot_modules.voice.master.service import (
    list_name_blocklist,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

from bot_modules.economy.cog.common import (
    _CATALOG_LOCKED_MSG,
    _EMOJI_CONTENT_TYPES,
    _MAX_ICON_BYTES,
    _MAX_MEMO_LEN,
    _PAY_CONFIRM_THRESHOLD,
    _can_grant,
    _can_post_publicly,
    _clean_memo,
)
from bot_modules.economy.cog.views import (
    EconomyPanelView,
    ShopPanelView,
    ShopRentButton,
    _GiftConfirmView,
    _LegacyShopOpenView,
    _PayConfirmView,
    _card,
)

log = logging.getLogger("dungeonkeeper.economy")















































































































async def _deliver_notice_batch(
    bot: Bot, guild: discord.Guild, member: discord.Member, rows: list[PendingDM]
) -> None:
    """One DM for every economy note a member collected over the window.

    ``dm_batch`` has already dropped notes from anyone now under a no-contact
    rule with them; ``notify_member`` still applies their mute preferences and
    the bank-channel fallback.
    """
    actors = unique_actors([r for r in rows if r.payload.get("named")])
    name_fn = await build_name_fn(
        guild=guild, db_path=bot.ctx.db_path, guild_id=guild.id, user_ids=actors,
    )
    text = render_notices(rows, name_fn)
    if text:
        await notify_member(bot, bot.ctx.db_path, guild.id, member.id, content=text)


class EconomyCog(
    ShopMixin, PerksMixin, QuestsMixin, PanelsMixin, commands.Cog
):
    bank = app_commands.Group(
        name="bank",
        description="Wallet and currency commands.",
        guild_only=True,
    )
    role = app_commands.Group(
        name="role",
        description="Personal role extras (customize your perks in /bank shop).",
        parent=bank,
    )
    auction = app_commands.Group(
        name="auction",
        description="Run a live auction (staff only).",
        parent=bank,
    )

    @auction.command(name="start", description="Start a live auction (staff only).")
    @app_commands.describe(
        title="What's being auctioned (short name)",
        prize="What the winner gets — you hand it over yourself",
        hours="How long the auction runs, in hours",
    )
    async def auction_start(
        self,
        interaction: discord.Interaction,
        title: str,
        prize: str,
        hours: app_commands.Range[float, 1, 168],
    ) -> None:
        await start_auction(
            interaction, title=title, prize=prize, duration_hours=float(hours)
        )

    @auction.command(name="cancel", description="Cancel the live auction and refund (staff only).")
    async def auction_cancel(self, interaction: discord.Interaction) -> None:
        await cancel_open_auction(interaction)

    @auction.command(name="end", description="Close the live auction now (staff only).")
    async def auction_end(self, interaction: discord.Interaction) -> None:
        await end_open_auction(interaction)

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        # guild_id → (monotonic expiry, trigger quests). TTL-refreshed in the
        # message listener; empty lists are cached too so guilds without
        # trigger quests cost one dict lookup per message.
        self._trigger_cache: dict[
            int, tuple[float, list[quests_svc.TriggerQuest]]
        ] = {}
        # Four channel-bottom panels, all on the shared machinery in
        # core.sticky (locks, debounce, id cache, post-before-delete). This cog
        # only says where each panel's ids live and what it should look like.
        #
        # There were five until 2026-08-18, when the static how-it-works guide
        # was folded into the live board: one panel, one message, with the
        # guide behind an ❓ button on it. It kept the *guide's* ids (see
        # _PANEL_FIELDS) and the *board's* content and refresh loops.
        # guild_id → the pending in-place redraw that resets the panel's How
        # it works dropdown after a pick (PanelsMixin.schedule_panel_reset).
        self._panel_resets: dict[int, asyncio.Task[None]] = {}
        self.economy_panel = StickyPanel(
            "econ panel", bot,
            load_ids=lambda gid: self._panel_ids(gid, "panel"),
            save_ids=lambda gid, cid, mid: self._save_panel_ids(
                gid, "panel", cid, mid
            ),
            build=self._build_economy_panel,
            refresh_on_update=True,
        )
        self.shop_panel = StickyPanel(
            "econ shop", bot,
            load_ids=lambda gid: self._panel_ids(gid, "shop"),
            save_ids=lambda gid, cid, mid: self._save_panel_ids(gid, "shop", cid, mid),
            build=self._build_shop_panel,
            refresh_on_update=True,
        )
        # The fourth panel is the odd one out: every other StickyPanel in the
        # bot is permanent and one-per-guild, but an auction ENDS. The
        # lifecycle lives entirely in these callbacks (see card_ids /
        # attach_card_to_latest) — card_ids goes (0, 0) at close and the
        # restick, which never creates a panel it didn't already find, simply
        # stops. No change to core.sticky was needed for that.
        #
        # restick_on_bot stays off (the default), simply because there is
        # nothing to chase: while an auction is open the bot posts NOTHING into
        # the channel — bid confirmations are ephemeral and outbid notices are
        # DMs. Turning it on would buy no behaviour, not court a flood; the
        # casino's 275-message storm was an unrecorded placement id, not the
        # flag (see the bounty hub below).
        self.auction_panel = StickyPanel(
            "econ auction", bot,
            load_ids=self._auction_card_ids,
            save_ids=self._save_auction_card_ids,
            build=self._build_auction_panel,
        )
        # The Bounty Board hub — the board's only entry point since /bounty was
        # deleted (2026-07-29). Unlike the three panels above it does not get
        # to pick its channel: it belongs in `bounty_channel_id`, the board it
        # fronts, so its ids come from there (see _bounty_panel_ids).
        #
        # restick_on_bot is ON, because this is the one sticky panel whose own
        # channel is where the bot posts: without it a burst of new bounty
        # cards strands the hub — the only way to post or chip in — above them
        # until a human happens to speak.
        #
        # This does not reintroduce the casino's 275-message flood. That flood
        # was never "restick_on_bot chases its own repost"; it was
        # schedule_restick() cancel-and-rearming the very task parked inside
        # place(), so the repost landed but its id was never recorded and the
        # at-the-bottom guard kept comparing against a dead message. Shielding
        # the placement fixed it (core.sticky.place, 2026-07-26), and three
        # layers now stop the self-chase: the cached-id skip in
        # should_restick(), the last_message_id pre-check in
        # _delayed_restick(), and only_if_buried re-deciding under the
        # placement lock.
        #
        # The re-render paths cannot arm a restick at all, which is what makes
        # this cheap: chip-in and award edit the card in place
        # (bounty_views._refresh_card) and the expiry sweep does too
        # (refresh_card_by_id), and a message edit fires no MESSAGE_CREATE. A
        # genuinely new card is the only trigger — precisely the event the hub
        # should be jumping over — so a burst of N cards costs one repost once
        # the channel goes quiet for the debounce.
        self.bounty_panel = StickyPanel(
            "econ bounty hub", bot,
            load_ids=self._bounty_panel_ids,
            save_ids=self._save_bounty_panel_ids,
            build=self._build_bounty_panel,
            restick_on_bot=True,
            refresh_on_update=True,
        )
        # Photo Challenge channel id, TTL-cached so the on_message listener
        # costs a dict lookup, not a DB read, for every message in the guild:
        # guild_id → (monotonic expiry, channel_id).
        self._photo_opts: dict[int, tuple[float, int]] = {}
        super().__init__()

    async def cog_unload(self) -> None:
        unregister_handler(NOTICE_BATCH_KIND)
        self._auction_settle_loop.cancel()
        for task in self._panel_resets.values():
            task.cancel()
        for panel in self._panels:
            panel.cancel_all()

    @tasks.loop(seconds=30)
    async def _auction_settle_loop(self) -> None:
        """Close auctions past their end and announce each — the timed-close path.

        One indexed read finds the (usually zero) guilds with a live auction, so
        idle guilds cost nothing; only those are then swept. Best-effort — a
        per-guild failure is logged, never fatal to the loop."""
        def _live() -> set[int]:
            with self.bot.ctx.open_db() as conn:
                return open_auction_guild_ids(conn)

        try:
            live = await asyncio.to_thread(_live)
        except Exception:
            log.exception("auction settle loop: failed to list live auctions")
            return
        for guild_id in live:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await settle_and_announce(self.bot, guild)
            except Exception:
                log.exception("auction settle loop failed for guild %s", guild_id)

    @_auction_settle_loop.before_loop
    async def _before_auction_settle(self) -> None:
        await self.bot.wait_until_ready()

    @bank.command(name="wallet", description="Check your balance and recent activity.")
    async def bank_wallet(self, interaction: discord.Interaction) -> None:
        await self.send_wallet_panel(interaction)

    async def send_wallet_panel(self, interaction: discord.Interaction) -> None:
        """The member's private balance + activity view.

        Shared by the ``/bank wallet`` command and the leaderboard panel's
        "Wallet" button, so both open the exact same ephemeral card (mirrors
        ``send_quests_panel``). A bare Components V2 card, like every
        ephemeral card: no banner, no accent (guide § No container).
        """
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        user_id = interaction.user.id

        def _load() -> tuple[
            EconSettings, int, list, list, int, sqlite3.Row | None
        ]:
            from bot_modules.casino.service import (  # noqa: PLC0415
                member_casino_stats,
            )

            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    # The caller refuses before rendering, so the other five
                    # reads would be thrown away (mirrors _shop_context).
                    return settings, 0, [], [], 0, None
                balance = get_balance(conn, guild_id, user_id)
                ledger = get_ledger(conn, guild_id, user_id, limit=10)
                rentals = list_member_rentals(conn, guild_id, user_id)
                shields = get_streak_shields(conn, guild_id, user_id)
                casino = member_casino_stats(conn, guild_id, user_id)
            return settings, balance, ledger, rentals, shields, casino

        settings, balance, ledger, rentals, shields, casino = (
            await asyncio.to_thread(_load)
        )

        if await self._refuse_disabled(interaction, settings):
            return

        # A gifted rental names its recipient, who may since have left.
        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=guild_id,
            user_ids=[int(r["beneficiary_id"]) for r in rentals],
        )
        items = build_wallet_layout(
            settings,
            balance=balance,
            ledger=ledger,
            rentals=rentals,
            shields=shields,
            casino=casino,
            viewer_id=user_id,
            name_fn=name_fn,
        )
        await interaction.response.send_message(view=_card(items), ephemeral=True)

    @bank.command(name="grant", description="Award currency to a member (staff only).")
    @app_commands.describe(
        member="Who to award",
        amount="How much to award (whole number)",
        reason="Why — recorded in the ledger",
    )
    async def bank_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild_id)
        if settings is None:
            return

        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to grant currency.", ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "❌ Bots don't have wallets.", ephemeral=True
            )
            return

        if amount < 1:
            await interaction.response.send_message(
                "❌ The amount must be at least 1.", ephemeral=True
            )
            return

        booster = member.premium_since is not None
        meta = {"reason": reason, "granted_by": actor.display_name}

        def _grant() -> tuple[int, PanelArt]:
            with self.bot.ctx.open_db() as conn:
                return apply_credit(
                    conn,
                    guild_id,
                    member.id,
                    amount,
                    "grant",
                    actor_id=actor.id,
                    meta=meta,
                    booster=booster,
                    multiplier=settings.booster_multiplier,
                ), resolve_panel_art_conn(conn, guild_id)

        credited, art = await asyncio.to_thread(_grant)

        items = build_grant_receipt_layout(
            settings,
            credited=credited,
            amount=amount,
            booster=booster,
            member_name=discord.utils.escape_markdown(member.display_name),
            reason=reason,
            actor_name=actor.display_name,
            art=art,
        )
        await interaction.response.send_message(view=_card(items))

    @bank.command(
        name="mute", description="Toggle economy DM notifications for yourself."
    )
    async def bank_mute(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        settings = await self._settings_or_refuse(interaction, guild_id)
        if settings is None:
            return

        def _toggle() -> bool:
            with self.bot.ctx.open_db() as conn:
                new_muted = not get_notify_muted(conn, guild_id, user_id)
                set_notify_muted(conn, guild_id, user_id, new_muted)
                return new_muted

        muted = await asyncio.to_thread(_toggle)

        items = build_mute_toggle_layout(muted=muted)
        await interaction.response.send_message(view=_card(items), ephemeral=True)

    # ── transfers ────────────────────────────────────────────────────────

    @bank.command(name="pay", description="Send currency to another member.")
    @app_commands.describe(
        member="Who to pay",
        amount="How much (whole number)",
        memo="Optional note — what's it for? (shown to them)",
        public=(
            "Post the receipt in this channel — your note is shown too "
            "(your balance stays private)"
        ),
    )
    async def bank_pay(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        memo: app_commands.Range[str, None, _MAX_MEMO_LEN] | None = None,
        public: bool = False,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        sender = interaction.user
        assert isinstance(sender, discord.Member)

        # Normalise once, here — every downstream site (ledger meta, embeds,
        # DM) takes the cleaned value and escapes only at render.
        memo = _clean_memo(memo)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not settings.transfers_enabled:
            await interaction.response.send_message(
                "❌ Transfers are turned off on this server.", ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message(
                "❌ Bots don't have wallets.", ephemeral=True
            )
            return
        if member.id == sender.id:
            await interaction.response.send_message(
                "❌ You can't pay yourself.", ephemeral=True
            )
            return
        if amount < 1:
            await interaction.response.send_message(
                "❌ The amount must be at least 1.", ephemeral=True
            )
            return

        if amount > _PAY_CONFIRM_THRESHOLD:
            # Say it before they press, not after: over the threshold the
            # receipt is a separate public post, and the memo/public note is
            # part of what gets published — both render on the confirm card
            # itself now (build_pay_confirm_layout), not composed here.
            view = _PayConfirmView(
                self, settings, guild, sender, member, amount, memo, public
            )
            await interaction.response.send_message(view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return

        await self.finalize_pay(
            interaction, settings, guild, sender, member, amount,
            memo=memo, via_confirm=False, public=public,
        )

    async def finalize_pay(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        sender: discord.Member,
        recipient: discord.Member,
        amount: int,
        *,
        memo: str | None = None,
        via_confirm: bool,
        public: bool = False,
    ) -> None:
        """Execute the transfer and report — shared by the direct and confirm paths.

        ``public`` is the sender's per-payment choice to post the receipt in
        the channel. It only ever reaches the *success* reply: every refusal
        below still goes out through ``_reply``, which is ephemeral-only, so
        "you don't have enough" can never be announced to a room.

        The public receipt is always a followup, on both paths. An ephemeral
        message cannot be promoted to a public one, so the confirm path — which
        resolves by editing its own ephemeral confirm in place — has to send
        the receipt separately; making the direct path do the same keeps one
        payment looking identical either side of the confirm threshold.
        """

        def _tx() -> tuple[int, bool, PanelArt]:
            with self.bot.ctx.open_db() as conn:
                # Asked on every payment, not just the public ones: the
                # recipient's DM is contact too, so the answer changes the
                # outcome of an ordinary private pay as well.
                blocked = is_no_contact_conn(
                    conn, guild.id, sender.id, recipient.id
                )
                transfer_currency(
                    conn, guild.id, sender.id, recipient.id, amount, memo=memo
                )
                return (
                    get_balance(conn, guild.id, sender.id), blocked,
                    resolve_panel_art_conn(conn, guild.id),
                )

        try:
            new_balance, blocked, art = await asyncio.to_thread(_tx)
        except ValueError as exc:
            if "insufficient" in str(exc):
                bal = await asyncio.to_thread(self._balance, guild.id, sender.id)
                text = (
                    f"You don't have enough — your balance is "
                    f"{settings.currency_emoji} {bal:,}."
                )
            else:
                text = "That payment isn't allowed."
            await self._reply(interaction, text, via_confirm=via_confirm)
            return

        safe_memo = discord.utils.escape_markdown(memo) if memo else None
        # Permissions are only worth resolving for a payment that asked to be
        # published; the ordinary private receipt never touches the channel.
        disclosure = pay_disclosure(
            public,
            blocked=blocked,
            can_post=public and _can_post_publicly(interaction),
        )
        show_publicly = disclosure.public_receipt
        items = build_payment_receipt_layout(
            settings,
            discord.utils.escape_markdown(sender.display_name),
            discord.utils.escape_markdown(recipient.display_name),
            amount,
            memo=memo,
            balance=None if show_publicly else new_balance,
            public=show_publicly,
            art=art,
        )
        if show_publicly:
            # The private half of a public payment is the balance the public
            # receipt deliberately omits — so the sender still sees it, and
            # nobody else does.
            await self._reply(
                interaction,
                f"{settings.currency_emoji} Sent. Your balance: {new_balance:,}.",
                via_confirm=via_confirm,
            )
            try:
                await interaction.followup.send(
                    view=_card(items), allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                # The money moved and the sender has their receipt; a failed
                # post stays quiet for the same reason the no-contact downgrade
                # does (see receipt_is_public).
                log.warning(
                    "pay receipt: couldn't post publicly in guild %s channel %s",
                    guild.id, getattr(interaction.channel, "id", None),
                )
        else:
            await self._reply_layout(interaction, items, via_confirm=via_confirm)

        # notify_member sends `content` with no allowed_mentions, and its
        # fallback posts into the public bank channel — so a memo has to be
        # mention-escaped here or it could ping the server.
        if not disclosure.notify_recipient:
            # A no-contact pair: the coins landed, and nothing authored by the
            # sender goes with them. See transfers.pay_disclosure for why the
            # transfer itself is not refused.
            return
        line = f"sent you {settings.currency_emoji} {amount:,} {_unit(settings, amount)}."
        if safe_memo:
            # The memo is already on both ledger rows; this copy lives only
            # until the batch goes out.
            line += f' — "{discord.utils.escape_mentions(safe_memo)}"'
        await self._queue_notice(guild.id, recipient.id, line, actor_id=sender.id)

    async def _queue_notice(
        self, guild_id: int, recipient_id: int, line: str, *, actor_id: int
    ) -> None:
        """Hold a "someone did X for you" note for the recipient's next batched
        DM (``economy.notice_batch``); the money has already moved."""

        def _q() -> None:
            with self.bot.ctx.open_db() as conn:
                queue_notice(
                    conn, guild_id=guild_id, recipient_id=recipient_id,
                    line=line, actor_id=actor_id,
                )

        try:
            await asyncio.to_thread(_q)
        except Exception:  # noqa: BLE001 — a courtesy note must not undo a finished transfer's reply
            log.warning("econ: failed to queue a note for %s", recipient_id, exc_info=True)

    # ── shop ─────────────────────────────────────────────────────────────

    @bank.command(name="shop", description="Browse and rent personal-role perks.")
    async def bank_shop(self, interaction: discord.Interaction) -> None:
        await self.open_personal_shop(interaction)

    @bank.command(name="store", description="Browse what this server is selling.")
    async def bank_store(self, interaction: discord.Interaction) -> None:
        await self.open_personal_store(interaction)










    # ── self-service refunds ─────────────────────────────────────────────




    # ── pin of the day ───────────────────────────────────────────────────


    # ── flash themes ─────────────────────────────────────────────────────


    # ── community bounty ─────────────────────────────────────────────────



    # ── emoji sponsorship ────────────────────────────────────────────────

    @bank.command(
        name="emoji",
        description="Sponsor a custom emoji — you pay weekly rent to keep it.",
    )
    @app_commands.describe(
        image="The emoji image (PNG/JPEG/WEBP, GIF for animated — max 256KB)",
        name="Its :name: — 2–32 letters, numbers, underscores",
    )
    async def bank_emoji(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment | None = None,
        name: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not emoji_svc.sponsoring_enabled(settings):
            await interaction.response.send_message(
                "❌ Emoji sponsorship isn't enabled here.", ephemeral=True
            )
            return

        if image is None or name is None:
            await self._emoji_status(interaction, settings, guild, member)
            return

        ctype = (image.content_type or "").split(";")[0].strip()
        if ctype not in _EMOJI_CONTENT_TYPES:
            await interaction.response.send_message(
                "❌ Emoji images are PNG, JPEG, WEBP, or GIF.", ephemeral=True
            )
            return
        if image.size > emoji_svc.MAX_IMAGE_BYTES:
            await interaction.response.send_message(
                "❌ Discord caps emoji images at 256KB — that one's too big.",
                ephemeral=True,
            )
            return
        animated = _EMOJI_CONTENT_TYPES[ctype]

        await interaction.response.defer(ephemeral=True)
        data = await image.read()

        # Live Discord slot math the service layer can't see: sponsors may
        # use the cap's worth of slots, but never the guild's LAST free slot
        # of that kind (static and animated count separately).
        same_kind = [e for e in guild.emojis if e.animated == animated]
        guild_has_room = len(same_kind) < max(0, guild.emoji_limit - 1)

        ext = "gif" if animated else "png"
        directory = data_dir("econ_emoji", base=self.bot.ctx.db_path.parent) / str(guild.id)
        path = directory / f"{int(time.time())}_{member.id}.{ext}"

        taken = {e.name for e in guild.emojis}

        def _submit():
            # The mkdir and the up-to-256KB write ride the worker thread with
            # the DB work rather than stalling the event loop — same order as
            # before (image on disk before the row that points at it), so the
            # unlink below still undoes a rejected submission.
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            with self.bot.ctx.open_db() as conn:
                for row in emoji_svc.list_submissions(conn, guild.id):
                    if row["state"] in ("pending", "approved", "live"):
                        taken.add(str(row["name"]))
                under_cap = (
                    emoji_svc.open_submission_count(conn, guild.id)
                    < max(0, settings.emoji_sponsor_slots)
                )
                return emoji_svc.submit_sponsorship(
                    conn, settings, guild.id, member.id,
                    name=name, image_path=str(path), animated=animated,
                    blocklist_patterns=list_name_blocklist(conn, guild.id),
                    taken_names=taken,
                    guild_slots_free=guild_has_room and under_cap,
                )

        try:
            outcome = await asyncio.to_thread(_submit)
        except ValueError as exc:
            path.unlink(missing_ok=True)
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        unit = _unit(settings, outcome.price)
        await interaction.followup.send(
            f"📨 Sent :{name.strip(':')}: to the mods for review — "
            f"{outcome.price} {unit} held (that covers week one). If it's "
            "turned down you get a full refund; once it's up it renews "
            "weekly from your wallet, and lapsing takes it down.",
            ephemeral=True,
        )



    # ── gift ─────────────────────────────────────────────────────────────

    async def _giftable_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """The perks THIS guild sells, for `/bank gift`.

        Autocomplete rather than a static ``@app_commands.choices`` list
        because the choices on a slash command are baked in at import and are
        the same in every guild, so a switched-off perk would keep offering
        itself in the picker and refuse only after someone picked it. The
        refusal in the command body stays regardless — autocomplete is a
        convenience, and Discord will happily deliver a hand-typed value that
        never appeared in it.
        """
        if interaction.guild is None:
            return []
        guild_id = interaction.guild.id

        def _settings() -> EconSettings:
            with self.bot.ctx.open_db() as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_settings)
        needle = current.casefold()
        return [
            app_commands.Choice(name=PERK_LABELS[p], value=p)
            for p in perks_on_sale(settings, GIFTABLE_PERKS)
            if needle in PERK_LABELS[p].casefold()
        ][:25]

    @bank.command(
        name="gift",
        description="Gift a friend a perk — you pay its weekly price.",
    )
    @app_commands.describe(member="Who to gift it to", perk="Which perk to gift")
    @app_commands.autocomplete(perk=_giftable_autocomplete)
    async def bank_gift(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        perk: str,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        gifter = interaction.user
        assert isinstance(gifter, discord.Member)
        perk_key = perk

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if perk_key not in GIFTABLE_PERKS:
            # Free-text now that the picker is autocomplete: a typo or a stale
            # client can deliver anything at all.
            await interaction.response.send_message(
                "❌ That isn't a perk you can gift. Pick one from the list.",
                ephemeral=True,
            )
            return
        if member.bot:
            await interaction.response.send_message(
                "❌ Bots can't wear perks.", ephemeral=True
            )
            return
        if member.id == gifter.id:
            await interaction.response.send_message(
                "❌ Rent your own perks with /bank shop.", ephemeral=True
            )
            return
        if not perk_on_sale(settings, perk_key):
            # Covers the voice lease's old price-0 check and every other perk
            # besides: a switched-off perk can't be bought for yourself OR for
            # a friend, or the gift route would be a hole straight through the
            # checkbox.
            #
            # Checked BEFORE the Discord feature gate below. Both refuse, but
            # only one is actionable: "needs a server feature" sends an admin
            # hunting a boost level for a perk they themselves unchecked.
            await interaction.response.send_message(
                f"❌ **{PERK_LABELS[perk_key]}** isn't for sale here right now.",
                ephemeral=True,
            )
            return
        if perk_key in FEATURE_GATED and not await feature_gate_ok(
            self.bot, guild.id, perk_key
        ):
            await interaction.response.send_message(
                "❌ That perk needs a server feature that isn't enabled here.",
                ephemeral=True,
            )
            return

        def _recipient_ent() -> set[str]:
            # The recipient's staff comp counts here — gifting a mod a perk
            # they already get free should hit the "already has it" confirm,
            # not silently take the gifter's coins.
            recipient_is_staff = self.bot.ctx.member_is_mod(member)
            with self.bot.ctx.open_db() as conn:
                return effective_entitlements(
                    conn, guild.id, member.id, is_staff=recipient_is_staff
                )

        if perk_key in await asyncio.to_thread(_recipient_ent):
            # Probably a mistake — the perk stacks silently (their role
            # already shows it), so make the double-spend an explicit choice.
            view = _GiftConfirmView(self, settings, guild, gifter, member, perk_key)
            await interaction.response.send_message(
                f"{discord.utils.escape_markdown(member.display_name)} already has "
                f"**{PERK_LABELS[perk_key]}**. Gift it anyway?",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return

        await self.finalize_gift(
            interaction, settings, guild, gifter, member, perk_key,
            via_confirm=False,
        )

    async def finalize_gift(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        gifter: discord.Member,
        member: discord.Member,
        perk: str,
        *,
        via_confirm: bool,
    ) -> None:
        """Charge the gifter and open the rental with the friend as beneficiary."""

        def _rent() -> bool:
            with self.bot.ctx.open_db() as conn:
                # Read inside the same connection as the rental, for the same
                # reason /bank pay does: the "who gifted you this" DM is
                # contact, so a no-contact pair gets the perk without the
                # announcement (transfers.pay_disclosure).
                blocked = is_no_contact_conn(conn, guild.id, gifter.id, member.id)
                rent_perk(
                    conn, settings, guild.id, gifter.id, perk,
                    beneficiary_id=member.id, now=time.time(),
                )
                return blocked

        label = PERK_LABELS[perk]
        try:
            blocked = await asyncio.to_thread(_rent)
        except ValueError as exc:
            msg = str(exc)
            if "insufficient" in msg:
                text = await self._short_funds_text(
                    settings, guild.id, gifter.id, perk_price(settings, perk)
                )
            elif "already rented" in msg:
                text = f"❌ You're already gifting them **{label}**."
            elif "not for sale" in msg:
                text = f"❌ This server has stopped selling **{label}**."
            else:
                text = "❌ That gift isn't available."
            await self._reply(interaction, text, via_confirm=via_confirm)
            return

        # Defer BEFORE the slow role-apply + notify REST calls (apply_role_perks
        # makes several requests incl. the rate-limited edit_role_positions) so a
        # single 429 can't push the first response past the 3s budget and leave
        # the charged gifter staring at "This interaction failed."
        await self._defer(interaction, via_confirm=via_confirm)
        if perk in SELF_PERKS:
            await apply_role_perks(self.bot, self.bot.ctx.db_path, guild.id, member.id)
        # `/bank role icon` (upload) hard-refuses catalog servers, so only hint it
        # off-catalog; on a catalog server the recipient picks from /bank shop.
        note = ""
        if perk == "role_icon" and (
            await asyncio.to_thread(self._icon_price_range, guild.id) is None
        ):
            note = " They can upload one with `/bank role icon`."
        if perk == "voice_style":
            gift_hint = "Renaming and sizing your voice channel are unlocked."
        elif perk in NO_CONFIG_PERKS:
            gift_hint = "It's already live on your personal role — no setup needed."
        else:
            gift_hint = "Set it up from /bank shop."
        if pay_disclosure(False, blocked=blocked, can_post=False).notify_recipient:
            await self._queue_notice(
                guild.id, member.id, f"gifted you **{label}**! {gift_hint}",
                actor_id=gifter.id,
            )
        await interaction.edit_original_response(
            content=(
                f"Gifted **{label}** to {member.mention}. They can set it from "
                f"`/bank shop`.{note}"
            ),
            embed=None,
            view=None,
        )

    # ── role studio ──────────────────────────────────────────────────────
    # Customisation is button + modal driven from /bank shop; the setters
    # below are shared by the modals (and re-check entitlements on submit,
    # since a rental can lapse between opening the shop and submitting).
















    @role.command(
        name="icon", description="Upload an image for your personal role's icon."
    )
    @app_commands.describe(
        image="An image up to 256KB (emoji icons: use /bank shop)"
    )
    async def role_icon(
        self, interaction: discord.Interaction, image: discord.Attachment
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_icon"):
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return
        if await asyncio.to_thread(self._catalog_locked, guild.id, user_id):
            await interaction.response.send_message(_CATALOG_LOCKED_MSG, ephemeral=True)
            return
        if image.size > _MAX_ICON_BYTES:
            await interaction.response.send_message(
                "❌ That image is too big — 256KB max.", ephemeral=True
            )
            return
        data = await image.read()
        await self._store_role_icon(interaction, guild.id, user_id, data)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Cancel a leaver's rentals, refund live wagers, re-project roles."""
        guild = member.guild
        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            return

        # Escrowed game stakes come back: the plan flagged a leaver's stake
        # stranding forever (their id stays in the roster but they can never
        # win). Refunding only their row leaves the rest of the pot intact
        # for the players still in the game.
        def _refund_wagers() -> int:
            with self.bot.ctx.open_db() as conn:
                total = 0
                for row in wager_svc.live_stakes_for_member(
                    conn, guild.id, member.id
                ):
                    # Mahjong escrow is NOT winner-takes-pot: a live hand
                    # settles zero-sum against every seat's held row, so
                    # yanking the leaver's row here would make the eventual
                    # settle_split refuse (players != escrow holders) and
                    # crash the winner's Mahjong. The mahjong cog folds the
                    # leaver fallow instead — their escrow stays held and
                    # pays out per the fallow rules (spec §1).
                    if str(row["game_type"]) == "mahjong":
                        continue
                    total += wager_svc.refund_player(
                        conn, str(row["game_type"]), int(row["game_id"]),
                        member.id,
                    )
                return total

        try:
            refunded = await asyncio.to_thread(_refund_wagers)
            if refunded:
                log.info(
                    "econ: refunded %d staked to leaver %s in %s",
                    refunded, member.id, guild.id,
                )
        except Exception:
            log.exception(
                "econ: wager refund failed for leaver %s in %s",
                member.id, guild.id,
            )

        def _cancel() -> list:
            with self.bot.ctx.open_db() as conn:
                return cancel_all_for_member(
                    conn, guild.id, member.id, now=time.time()
                )

        rows = await asyncio.to_thread(_cancel)
        # Re-project every distinct beneficiary whose entitlements just changed —
        # the leaver themselves (self-perks / received gifts) AND any friend whose
        # gifted color the leaver was funding.
        affected = {int(r["beneficiary_id"]) for r in rows}
        affected.add(member.id)
        for beneficiary_id in affected:
            try:
                await revoke_role_perks(
                    self.bot, self.bot.ctx.db_path, guild.id, beneficiary_id
                )
            except Exception:
                log.exception(
                    "econ: role cleanup failed for %s in %s", beneficiary_id, guild.id
                )

    # ── shared helpers ───────────────────────────────────────────────────






















    @bank.command(name="quests", description="View and claim the server's active quests.")
    async def bank_quests(self, interaction: discord.Interaction) -> None:
        await self.send_quests_panel(interaction)



    # ── trigger-word quest verification (spec §4.4) ───────────────────────




    # ── photo-post event quest (posting a photo in the Photo Challenge channel) ──






    # ── how-to guide panel ───────────────────────────────────────────────



    # ── channel-bottom panels ────────────────────────────────────────────
    #
    # All four run on core.sticky.StickyPanel. Each supplies only: where its
    # ids live, and what it should look like. A panel is treated as unposted
    # while the economy is disabled, so a disabled guild never re-sticks.
    #
    # _PANEL_FIELDS covers the two permanent panels; the auction card keeps
    # its ids elsewhere and supplies its own callbacks (see below).
    #
    # The merged economy panel reads the *guide's* pair, which looks backwards
    # for a panel rendering the leaderboard and isn't: the message those ids
    # name is the one that survived the merge, in the channel the panel now
    # calls home, so every guild was already pointing at the right message on
    # changeover day and no id had to be copied. The retired
    # ``leaderboard_*`` pair, and the boot-time one-shot that cleared it, were
    # deleted on 2026-08-29 once every guild had restarted past the merge.













    # ── the auction card's sticky callbacks ──────────────────────────────
    #
    # Unlike the three panels above, these ids live on the auction row rather
    # than in econ settings — the card belongs to one auction, not to the
    # guild — so they go through the service instead of _PANEL_FIELDS.







    # ── persistent shop panel ────────────────────────────────────────────


    async def cog_load(self) -> None:
        register_handler(NOTICE_BATCH_KIND, _deliver_notice_batch)
        # Re-register the persistent buttons so clicks on existing messages
        # still route after a restart — the custom_ids carry the state
        # (econ_claim:{approve,deny}:<id>, econ_shop_panel:<perk> on
        # pre-Open-Shop panels).
        self.bot.add_dynamic_items(
            QuestApproveButton,
            QuestDenyButton,
            ShopRentButton,
            PinApproveButton,
            PinDenyButton,
            ThemeApproveButton,
            ThemeDenyButton,
            BountyChipInButton,
            BountyAwardButton,
            BountyCancelButton,
            # The hub's two buttons carry no per-message state, but they are
            # dynamic items too so the whole bounty surface registers here.
            BountyHubPostButton,
            BountyHubChipButton,
            AuctionBidButton,
        )
        # The economy panel's four buttons and the shop panel's buttons carry
        # no per-message state, so they are plain static-custom_id views, not
        # dynamic items. EconomyPanelView is what re-registers the guide's two
        # buttons since the merge — registering a second view for them would
        # be two persistent views claiming one custom_id.
        self.bot.add_view(EconomyPanelView())
        self.bot.add_view(_LegacyShopOpenView())
        self.bot.add_view(ShopPanelView())
        await self._publish_panel_guilds()
        self._auction_settle_loop.start()

    # ── the listener's fast path ──────────────────────────────────────────








async def setup(bot: Bot) -> None:
    await bot.add_cog(EconomyCog(bot))
