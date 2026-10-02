"""The /bank shop and store: browsing, buying, renting, refunds, submissions."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy import quests as quest_rules
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.view_helpers import (
    unit as _unit,
)
from bot_modules.economy.perks import (
    PERK_LABELS,
)
from bot_modules.economy.shop import (
    SECTION_SPECIALS,
    page_captions,
    section_book,
    shop_pages,
    shop_sections,
)
from bot_modules.economy.shop_layouts import (
    build_refund_done_layout,
    build_refund_missing_layout,
)
from bot_modules.economy.shop_items import ItemView, Refusal, refusal_text
from bot_modules.economy import shop_items_service as shop_items_svc
from bot_modules.economy.bounty_views import (
    post_bounty_card,
)
from bot_modules.economy.approval_views import (
    post_approval_card,
    refresh_approvals_board,
)
from bot_modules.economy.bounty_service import (
    bounty_enabled,
    create_bounty,
)
from bot_modules.economy.pin_service import (
    pin_enabled,
    submit_pin,
)
from bot_modules.economy.theme_service import (
    submit_theme,
    theme_enabled,
)
from bot_modules.economy.rentals_service import (
    RentalRefund,
    get_refundable_rental,
    refund_rental,
)
from bot_modules.economy.loop import revoke_perk_effect
from bot_modules.economy.rentals import prorated_refund
from bot_modules.economy import raffle_service as raffle_svc
from bot_modules.economy.service import (
    EconSettings,
    get_streak_shield_price,
    purchase_streak_shield,
    refund_streak_shield,
)

if TYPE_CHECKING:
    pass

from bot_modules.economy.cog.base import (
    EconomyBase,
)
from bot_modules.economy.cog.views import (
    _BountyPostModal,
    _RefundConfirmView,
    _RefundPickerView,
    _ShopChooserView,
    _ShopView,
    _StoreView,
    _card,
    _item_bought_text,
    _rent_perk_flow,
)

log = logging.getLogger("dungeonkeeper.economy")


class ShopMixin(EconomyBase):
    """The /bank shop and store: browsing, buying, renting, refunds, submissions.

    Mixed into ``EconomyCog`` (economy_cog.py), which was split by area in
    standards audit row 4.29. Moved verbatim: the attributes it reads
    (``self.bot``, the StickyPanels, the caches) are set in the cog's
    ``__init__``.
    """

    async def open_personal_shop(self, interaction: discord.Interaction) -> None:
        """`/bank shop` — the channel panel's section rows, privately.

        The same rows and ids as the panel, so whichever door a member uses
        they pick a shelf the same way; each button then opens that one
        section (``open_shop_section``).
        """
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        shop = await asyncio.to_thread(
            self._shop_context, guild_id, user_id, self.bot.ctx.is_mod(interaction)
        )
        if await self._refuse_disabled(interaction, shop.settings):
            return
        sections = shop_sections(shop.settings, items=shop.items)
        view = _ShopChooserView(user_id, sections)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


    async def open_shop_section(
        self, interaction: discord.Interaction, section: str
    ) -> None:
        """Serve one section of the member's shop, ephemeral to them.

        Every door into the shop lands on a section here — a panel Browse,
        a chooser row, `/bank store` — so every shop feature (the server's
        own items, rent, customise, refunds) is reached the same way.
        """
        await self._render_shop(interaction, 0, section=section, edit=False)


    async def open_personal_store(self, interaction: discord.Interaction) -> None:
        """`/bank store` — the shop opened straight on ✨ Specials.

        Where nothing is stocked it refuses by name instead of quietly
        serving another shelf, which would read as the command being wrong.
        """
        await self._render_shop(
            interaction, 0, section=SECTION_SPECIALS, edit=False, require_store=True
        )


    async def turn_shop_page(
        self,
        interaction: discord.Interaction,
        page: int,
        *,
        section: str,
        replacing: discord.ui.View | discord.ui.LayoutView | None = None,
    ) -> None:
        """Re-render the open section on another of its pages.

        Re-reads rather than paging a snapshot: an arrow tap can land minutes
        after the shop was opened, and showing a sold-out item as buyable is a
        refusal the member only discovers by trying to pay for it.

        ``replacing`` is the page view the arrow was pressed on. Each turn
        builds a new view for the same message, and discord.py keeps the old
        one's timeout timer running after it is swapped out: left alone, its
        ``on_timeout`` would edit a greyed-out copy of the old page back over
        the live one minutes later. So the old view is stopped here, right
        before the edit that supersedes it.
        """
        await self._render_shop(
            interaction, page, section=section, edit=True, replacing=replacing
        )


    async def _render_shop(
        self,
        interaction: discord.Interaction,
        page: int,
        *,
        section: str,
        edit: bool,
        require_store: bool = False,
        replacing: discord.ui.View | discord.ui.LayoutView | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id

        shop = await asyncio.to_thread(
            self._shop_context, guild.id, user_id, self.bot.ctx.is_mod(interaction)
        )
        settings = shop.settings
        if await self._refuse_disabled(interaction, settings):
            return
        if require_store and not shop.items:
            await interaction.response.send_message(
                "❌ This server hasn't put anything in the store yet.",
                ephemeral=True,
            )
            return

        has_palette = shop.color_range is not None or "role_preset" in shop.owned
        pages = shop_pages(
            settings, items=shop.items, owned=shop.owned, has_palette=has_palette
        )
        book = section_book(pages, section)
        if not book:
            # The section closed after the panel was posted (an admin switched
            # it off, or sold the last Special). Refuse by name — serving a
            # different shelf would read as the button being wrong.
            await interaction.response.send_message(
                "❌ That part of the shop is closed right now.", ephemeral=True
            )
            return
        # Clamped, not trusted: an admin can withdraw the last item while a
        # member sits on that page, and the book shrinks under them.
        page = max(0, min(page, len(book) - 1))
        section, index = book[page]
        nav = page_captions(book, len(shop.items))

        if section == SECTION_SPECIALS:
            view: discord.ui.LayoutView = _StoreView(
                self._cog, settings, guild, user_id, shop.items, shop.owned_item_ids,
                shop.balance, index, page=page, nav=nav,
            )
        else:
            gated = await self._gated_perks(guild.id)
            # No ``items``: the store is the Specials section with pages of its
            # own, so a preview here would be the same list twice in one book.
            view = _ShopView(
                self._cog, settings, guild, user_id, gated, shop.owned,
                shop.icon_range is not None,
                # A renter keeps their button even if the palette emptied out
                # under them — otherwise the only route to the perk they are
                # still paying for is the generic refund picker.
                has_palette=has_palette,
                shields_held=shop.shields_held, refundable=shop.refundable,
                shield_price=shop.shield_price, balance=shop.balance,
                page=page, nav=nav, section=section,
                icon_catalog=shop.icon_range, color_catalog=shop.color_range,
            )

        # Stopped before the edit, not after: the old view's timer can fire
        # during the edit's own round trip, and a stopped view never times out.
        if replacing is not None:
            replacing.stop()
        if edit:
            await interaction.response.edit_message(
                content=None, embed=None, view=view
            )
        else:
            await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


    async def do_buy_shield(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """Buy the one-shot streak shield (shared by the shop view and panel)."""
        user_id = interaction.user.id

        def _buy() -> int:
            with self.bot.ctx.open_db() as conn:
                return purchase_streak_shield(conn, settings, guild.id, user_id)

        try:
            price = await asyncio.to_thread(_buy)
        except ValueError as exc:
            msg = str(exc)
            if "insufficient" in msg:
                text = await self._short_funds_text(
                    settings, guild.id, user_id, settings.price_streak_shield
                )
            elif "already holding" in msg:
                text = (
                    "❌ You're already holding a 🛡️ shield — it burns "
                    "automatically if a missed day would break your streak."
                )
            elif "not for sale" in msg:
                text = "❌ This server has stopped selling streak shields."
            else:
                text = "❌ That isn't available right now."
            await interaction.response.send_message(text, ephemeral=True)
            return

        await interaction.response.send_message(
            f"🛡️ Streak shield ready ({settings.currency_emoji} {price:,}). "
            "If a gap would break your login streak, it burns automatically "
            "and the streak lives on.",
            ephemeral=True,
        )


    async def do_buy_raffle_tickets(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        raw_quantity: str,
    ) -> None:
        """Buy tickets for the current guild-local ISO week (modal submit)."""
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        try:
            quantity = int(raw_quantity.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Give a whole number of tickets.", ephemeral=True
            )
            return

        def _buy() -> raffle_svc.TicketPurchase:
            with self.bot.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild.id)
                week = quest_rules.iso_week_for(local_day_for(time.time(), offset))
                return raffle_svc.buy_tickets(
                    conn, settings, guild.id, user_id, week, quantity
                )

        try:
            purchase = await asyncio.to_thread(_buy)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎟️ {purchase.quantity} ticket(s) bought "
            f"({settings.currency_emoji} {purchase.price:,}) — you hold "
            f"{purchase.week_total} this week. Winner drawn at the week "
            "roll and announced by name; the prize is a free weekly perk "
            "payment.",
            ephemeral=True,
        )


    async def do_rent(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        perk: str,
    ) -> None:
        """Rent a self-perk from the ephemeral shop view."""
        await _rent_perk_flow(interaction, self._cog, settings, guild, perk)


    async def open_refund_picker(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        refundable: list[dict],
        shield_price: int,
    ) -> None:
        """Show the picker of what the member can cancel & refund."""
        view = _RefundPickerView(
            self._cog, settings, guild, interaction.user.id, refundable, shield_price
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


    async def show_refund_confirm(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        target: str,
        *,
        replacing: discord.ui.View | discord.ui.LayoutView | None = None,
    ) -> None:
        """Re-check the pick is still live and show its Confirm/Back gate.

        ``replacing`` is the picker the choice was made on. Every path below
        edits the picker's message — the Confirm/Back gate on success, a plain
        refusal otherwise — so the picker is stopped first; a replaced view
        keeps its timeout timer, and its ``on_timeout`` would otherwise edit
        the dead picker back over whatever replaced it.
        """
        user_id = interaction.user.id

        def _load() -> tuple[dict | None, int]:
            with self.bot.ctx.open_db() as conn:
                if target == "shield":
                    return None, get_streak_shield_price(
                        conn, guild.id, user_id, settings
                    )
                rental_id = int(target.split(":", 1)[1])
                row = get_refundable_rental(conn, guild.id, user_id, rental_id)
                return (dict(row) if row is not None else None), 0

        rental, shield_price = await asyncio.to_thread(_load)
        if replacing is not None:
            replacing.stop()
        if target == "shield":
            if shield_price <= 0:
                await interaction.response.edit_message(
                    content=None, embed=None,
                    view=_card(build_refund_missing_layout("🛡️ Streak Shield")),
                )
                return
            title = "🛡️ Streak Shield"
            refund_text = (
                f"You'll get back {settings.currency_emoji} **{shield_price:,}**."
            )
        else:
            if rental is None:
                await interaction.response.edit_message(
                    content=None, embed=None,
                    view=_card(build_refund_missing_layout("rental")),
                )
                return
            label = PERK_LABELS.get(str(rental["perk"]), str(rental["perk"]))
            title = label
            if rental["state"] == "active":
                preview = prorated_refund(
                    int(rental["price"]), float(rental["next_bill_at"]), time.time()
                )
                refund_text = (
                    f"You'll get back {settings.currency_emoji} **{preview:,}** "
                    "— the perk ends right now."
                )
            else:
                refund_text = (
                    "➖ You're already in the grace period, so there's no "
                    "refund — the perk ends right now."
                )
        view = _RefundConfirmView(
            self._cog, settings, guild, user_id, target, title, refund_text
        )
        await interaction.response.edit_message(content=None, embed=None, view=view)
        view.message = await interaction.original_response()


    async def finalize_refund(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        target: str,
    ) -> None:
        """Run the refund for real and strip the perk immediately."""
        user_id = interaction.user.id

        if target == "shield":

            def _refund_shield() -> int:
                with self.bot.ctx.open_db() as conn:
                    return refund_streak_shield(conn, guild.id, user_id, settings)

            try:
                amount = await asyncio.to_thread(_refund_shield)
            except ValueError:
                await interaction.response.edit_message(
                    content=None, embed=None,
                    view=_card(build_refund_missing_layout("🛡️ Streak Shield")),
                )
                return
            await interaction.response.edit_message(
                content=None, embed=None,
                view=_card(
                    build_refund_done_layout(settings, "🛡️ Streak Shield", amount)
                ),
            )
            return

        rental_id = int(target.split(":", 1)[1])

        def _refund_rental() -> RentalRefund:
            with self.bot.ctx.open_db() as conn:
                return refund_rental(
                    conn, guild.id, rental_id, requester_id=user_id
                )

        try:
            result = await asyncio.to_thread(_refund_rental)
        except ValueError:
            await interaction.response.edit_message(
                content=None, embed=None,
                view=_card(build_refund_missing_layout("rental")),
            )
            return

        # Best-effort past this point: the refund already committed (money
        # moved, rental terminal), so a Discord-side hiccup here must not
        # blow up the interaction — same guard the billing loop's own call to
        # this dispatcher uses for the same reason (economy_loop.py).
        try:
            await revoke_perk_effect(
                self.bot, self.bot.ctx.db_path, guild.id, result.perk, result.rental_id,
                result.beneficiary_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy shop refund: failed to revoke perk for beneficiary %s.",
                result.beneficiary_id,
            )
        label = PERK_LABELS.get(result.perk, result.perk)
        await interaction.response.edit_message(
            content=None, embed=None,
            view=_card(build_refund_done_layout(settings, label, result.refund)),
        )


    async def do_pin_submit(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        """Escrow the price, queue the pin, and post the mod-approval card."""
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not pin_enabled(settings):
            await interaction.response.send_message(
                "❌ Pinning a message isn't enabled here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _submit():
            with self.bot.ctx.open_db() as conn:
                return submit_pin(conn, settings, guild.id, member.id, message)

        try:
            outcome = await asyncio.to_thread(_submit)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Two review surfaces, one ledger: a card in the staff-only approvals
        # channel (dark until that dial is set), and the mods' todo board. Never
        # the bank channel — it names the member and quotes what they wrote, and
        # in the main guild that channel is a member-facing explainer. The money
        # is already taken and the row exists, so neither call may surface as an
        # error to the member; the board reads the queue live either way. Pin of
        # the Day has no dashboard queue at all, so these two are its only
        # review surfaces.
        await post_approval_card(self.bot, guild, settings, "pin", outcome.submission_id)
        await refresh_approvals_board(self.bot, guild.id)
        unit = _unit(settings, outcome.price)
        await interaction.followup.send(
            f"📨 Sent your message to the mods for review — {outcome.price} "
            f"{unit} held. If it's turned down you'll get a full refund; if it's "
            "approved it's pinned for 24 hours.",
            ephemeral=True,
        )


    async def do_theme_submit(
        self, interaction: discord.Interaction, title: str, blurb: str
    ) -> None:
        """Escrow the price, queue the theme, and put it on the mods' board."""
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not theme_enabled(settings):
            await interaction.response.send_message(
                "❌ Buying a themed day isn't enabled here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _submit():
            with self.bot.ctx.open_db() as conn:
                return submit_theme(conn, settings, guild.id, member.id, title, blurb)

        try:
            outcome = await asyncio.to_thread(_submit)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Approvals channel + todo board, never the bank channel — see the
        # pin above.
        await post_approval_card(self.bot, guild, settings, "theme", outcome.submission_id)
        await refresh_approvals_board(self.bot, guild.id)
        unit = _unit(settings, outcome.price)
        await interaction.followup.send(
            f"📨 Sent your theme to the mods for review — {outcome.price} {unit} "
            "held. If it's turned down you'll get a full refund; if it's "
            "approved it runs the next time the channel is free.",
            ephemeral=True,
        )


    async def open_bounty_post_modal(self, interaction: discord.Interaction) -> None:
        """Open the post-a-bounty modal for the hub panel's 🎯 button.

        Lives on the cog rather than in ``bounty_views`` because the modal
        submits back into :meth:`do_bounty_post`; the button just routes here.
        Replaced the ``/bounty`` command on 2026-07-29 — the hub is now the
        board's only entry point, per CLAUDE.md's "one panel over a sprawl of
        subcommands".
        """
        assert interaction.guild is not None
        guild = interaction.guild

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not bounty_enabled(settings):
            await interaction.response.send_message(
                "❌ Bounties aren't enabled here.", ephemeral=True
            )
            return
        # A modal must be the first response — the enable check runs before it.
        await interaction.response.send_modal(_BountyPostModal(self._cog))


    async def do_bounty_post(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        raw_stake: str,
    ) -> None:
        """Open a bounty from the modal: escrow the stake and post the board card."""
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled or not bounty_enabled(settings):
            await interaction.response.send_message(
                "❌ Bounties aren't enabled here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            stake = int(raw_stake.strip())
        except ValueError:
            await interaction.followup.send(
                "❌ Your stake has to be a whole number of coins.", ephemeral=True
            )
            return

        def _create():
            with self.bot.ctx.open_db() as conn:
                return create_bounty(
                    conn, settings, guild.id, member.id,
                    title=title, description=description, stake=stake,
                )

        try:
            outcome = await asyncio.to_thread(_create)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        await post_bounty_card(
            self.bot, self.bot.ctx, guild, settings, outcome.bounty_id
        )
        # The new bounty belongs on the hub's open list straight away.
        await self._cog.refresh_bounty_hub_panel(guild)
        await interaction.followup.send(
            f"🎯 Bounty posted with {outcome.stake:,} in the pot! Others can chip "
            "in from its card or the board panel, and a mod awards it when it's "
            "done.",
            ephemeral=True,
        )


    async def do_buy_item(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        item: ItemView,
        *,
        note: str = "",
    ) -> None:
        """Buy one custom shop item, then run its Discord side of the deal.

        The money and the row are the service's; this method owns only what
        Discord has to be told. A role grant that fails does NOT unwind the
        purchase — the member paid, the order exists, and a mod can hand the
        role over — so the failure is reported as a thing staff will fix rather
        than as a refusal that would contradict the debit already taken.
        """
        user_id = interaction.user.id

        def _buy() -> shop_items_svc.PurchaseOutcome:
            with self.bot.ctx.open_db() as conn:
                return shop_items_svc.purchase(
                    conn, settings, guild.id, user_id, item.item_id, note=note,
                )

        outcome = await asyncio.to_thread(_buy)
        if not outcome.ok:
            text = refusal_text(outcome.refusal)
            if outcome.refusal is Refusal.INSUFFICIENT:
                text = await self._short_funds_text(
                    settings, guild.id, user_id, item.price
                )
            await interaction.response.send_message(text, ephemeral=True)
            return

        granted = True
        if outcome.grant_role_id:
            granted = await self._grant_item_role(
                guild, user_id, int(outcome.grant_role_id)
            )
        if outcome.todo_id:
            await self._repaint_todo_board(guild.id)

        await interaction.response.send_message(
            _item_bought_text(settings, outcome, granted=granted), ephemeral=True
        )
