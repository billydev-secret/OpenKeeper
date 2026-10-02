"""Helpers every economy mixin and the cog itself lean on."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, cast

import discord

from bot_modules.economy.cog.common import (
    _DISABLED_MSG,
    _ShopContext,
)
from bot_modules.core.layout import card
from bot_modules.core.role_safety import grant_block_reason
from bot_modules.economy.perks import (
    FEATURE_GATED,
    PERK_REFUSAL as _PERK_REFUSAL,
    PERK_REFUSAL_FALLBACK as _PERK_REFUSAL_FALLBACK,
)
from bot_modules.economy import shop_items_service as shop_items_svc
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
)
from bot_modules.economy.color_catalog_service import (
    catalog_price_range as color_price_range,
)
from bot_modules.economy.color_catalog_service import (
    list_catalog as list_color_catalog,
)
from bot_modules.economy.icon_catalog_service import (
    catalog_price_range,
    list_catalog,
)
from bot_modules.economy.rentals_service import (
    effective_entitlements,
    entitlements,
    get_live_role_icon_rental,
    list_refundable_rentals,
    upsert_personal_role,
)
from bot_modules.economy.rentals import comp_entitlements
from bot_modules.economy.service import (
    EconSettings,
    get_balance,
    get_streak_shield_status,
    load_econ_settings,
    save_econ_settings,
)
from bot_modules.voice.master.service import (
    list_name_blocklist,
)

if TYPE_CHECKING:
    from bot_modules.economy.economy_cog import EconomyCog
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.economy")


class EconomyBase:
    """Helpers every economy mixin and the cog itself lean on.

    Mixed into ``EconomyCog`` (economy_cog.py), which was split by area in
    standards audit row 4.29. Moved verbatim: the attributes it reads
    (``self.bot``, the StickyPanels, the caches) are set in the cog's
    ``__init__``.
    """

    bot: Bot

    @property
    def _cog(self) -> EconomyCog:
        """This object, typed as the cog it always is at runtime.

        The views take an ``EconomyCog``; a mixin method hands them
        ``self._cog`` rather than ``self`` so the type checker sees the whole
        cog, including methods another mixin provides.
        """
        return cast("EconomyCog", self)

    async def _repaint_todo_board(self, guild_id: int) -> None:
        """Tell the mods' board a new order just landed on it.

        Every other path that adds a todo repaints — ``/todo``, the board's own
        Add button, the dashboard — and the 60s loop is NOT a backstop here: it
        only repaints guilds where a recurring chore spawned or was written
        off. Without this a bought order stayed invisible in Discord until
        somebody happened to chat in the board's channel. Best effort: the
        purchase is already committed, so a Discord hiccup must not surface as
        a failed buy.
        """
        cog = self.bot.get_cog("TodoCog")
        refresh = getattr(cog, "refresh_board", None)
        if refresh is None:
            return
        try:
            await refresh(guild_id)
        except Exception:  # noqa: BLE001 — pragma: no cover - defensive: the order is already placed; a board repaint failure is a warning, not an error
            log.warning(
                "Economy: failed to repaint the todo board for %s after a "
                "shop-item order.", guild_id,
            )


    async def _grant_item_role(
        self, guild: discord.Guild, user_id: int, role_id: int
    ) -> bool:
        """Give the buyer the role their item grants. False if it didn't land.

        Best effort by design: a deleted role, a role above the bot, or a
        member who left between paying and this call are all staff problems,
        not reasons to fail a completed purchase.
        """
        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        if member is None or role is None:
            return False
        if role in member.roles:
            return True
        # An item row naming an elevated role must not sell staff access.
        if grant_block_reason(role, guild, feature="Economy shop item") is not None:
            return False
        try:
            await member.add_roles(role, reason="Economy: shop item purchased")
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "Economy: failed to grant shop-item role %s to %s in %s.",
                role_id, user_id, guild.id,
            )
            return False
        return True


    async def _apply_and_confirm(
        self, interaction: discord.Interaction, guild_id: int, user_id: int, msg: str
    ) -> None:
        # Defer first: apply_role_perks makes several REST calls (incl. the
        # rate-limited edit_role_positions), which a 429 can push past the 3s
        # interaction budget — leaving the member with "This interaction failed"
        # even though the perk saved.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await apply_role_perks(self.bot, self.bot.ctx.db_path, guild_id, user_id)
        if ok:
            await interaction.edit_original_response(content=msg)
        else:
            await interaction.edit_original_response(
                content=(
                    "Saved — but I couldn't update your role right now. "
                    "Try again shortly."
                ),
            )


    async def _refuse_disabled(
        self, interaction: discord.Interaction, settings: EconSettings
    ) -> bool:
        """Send the economy-off refusal; True when it fired and the caller stops.

        Every member-facing economy surface opens with this gate. Commands that
        already hold ``settings`` from a compound load (the shop's role context,
        the quest board, the wallet) call this directly; the ones that would
        otherwise load settings for the gate alone go through
        ``_settings_or_refuse``.
        """
        if settings.enabled:
            return False
        await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
        return True


    async def _settings_or_refuse(
        self, interaction: discord.Interaction, guild_id: int
    ) -> EconSettings | None:
        """Settings for an enabled economy, else send the refusal and give None."""
        settings = await asyncio.to_thread(self._load_settings, guild_id)
        return None if await self._refuse_disabled(interaction, settings) else settings


    async def _require_perk(
        self, interaction: discord.Interaction, guild_id: int, perk: str
    ) -> bool:
        """Whether the member may drive ``perk``'s role-studio setter.

        The economy-off gate and the not-rented refusal in one place: every
        setter opened with the same pair, differing only in the perk name and
        its refusal line.
        """
        settings, ent = await asyncio.to_thread(
            self._load_role_ctx,
            guild_id,
            interaction.user.id,
            self.bot.ctx.is_mod(interaction),
        )
        if await self._refuse_disabled(interaction, settings):
            return False
        if perk not in ent:
            # .get, not [perk]: the table covers four of the five SELF_PERKS,
            # so a setter added for the fifth must degrade to a polite refusal,
            # not a KeyError and "This interaction failed."
            await interaction.response.send_message(
                _PERK_REFUSAL.get(perk, _PERK_REFUSAL_FALLBACK), ephemeral=True
            )
            return False
        return True


    async def _gated_perks(self, guild_id: int) -> set[str]:
        """The feature-gated perks this guild currently can't offer."""
        return {
            perk
            for perk in FEATURE_GATED
            if not await feature_gate_ok(self.bot, guild_id, perk)
        }


    async def _defer(
        self, interaction: discord.Interaction, *, via_confirm: bool
    ) -> None:
        """Ack the interaction before slow role-apply REST calls.

        A component press (``via_confirm``) defers as a message update — no
        loading spinner, so the confirm view's own message is edited in place by
        the follow-up ``edit_original_response``. A fresh slash/modal submit
        defers with the thinking indicator.
        """
        if via_confirm:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)


    async def _reply(
        self, interaction: discord.Interaction, text: str, *, via_confirm: bool
    ) -> None:
        if via_confirm:
            await interaction.response.edit_message(content=text, embed=None, view=None)
        else:
            await interaction.response.send_message(text, ephemeral=True)


    async def _reply_embed(
        self, interaction: discord.Interaction, embed: discord.Embed, *, via_confirm: bool
    ) -> None:
        if via_confirm:
            await interaction.response.edit_message(
                content=None, embed=embed, view=None
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


    async def _reply_layout(
        self,
        interaction: discord.Interaction,
        items: list[discord.ui.Item[Any]],
        *,
        via_confirm: bool,
    ) -> None:
        """Answer a command with a Components V2 card, from either entry point.

        ``via_confirm`` mirrors ``_reply``: a confirm-gated payment resolves
        by editing its own ephemeral confirm card in place — a V2 edit has no
        ``content``/``embed`` to null out, just the old view swapped for the
        new one — while the direct (under-threshold) path sends a fresh
        ephemeral reply.
        """
        view = card(items)
        if via_confirm:
            await interaction.response.edit_message(view=view)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)


    def _balance(self, guild_id: int, user_id: int) -> int:
        with self.bot.ctx.open_db() as conn:
            return get_balance(conn, guild_id, user_id)


    async def _short_funds_text(
        self, settings: EconSettings, guild_id: int, user_id: int, price: int
    ) -> str:
        """The shared refusal for a purchase the member can't afford.

        Reads the balance back so the member sees the gap, not just the price.
        Every "insufficient" handler in the cog renders this one sentence — a
        reword belongs here, not in four places.
        """
        bal = await asyncio.to_thread(self._balance, guild_id, user_id)
        return (
            f"❌ You need {settings.currency_emoji} {price:,} "
            f"but only have {bal:,}."
        )


    def _shop_context(
        self, guild_id: int, user_id: int, is_staff: bool = False
    ) -> _ShopContext:
        """Read the whole shop in one go.

        The header (settings, entitlements, balance), the catalog row's price
        span, and the refund/shield controls' state were three separate thread
        hops, each opening — and PRAGMA-ing — its own connection for a page
        that renders once. ``list_refundable_rentals`` already excludes
        sponsored-emoji rentals (a different self-service surface, ``/bank
        emoji``) and admin-force-cancelled ones.

        A disabled economy stops after the settings row: the caller refuses
        before rendering anything, so reading the rest would be paying five
        queries to say "the economy is off".
        """
        with self.bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return _ShopContext(
                    settings, set(), set(), 0, None, None, [], 0, 0, [], set()
                )
            # Both halves here rather than through ``effective_entitlements``:
            # settings are already in hand, and the shop is the one surface
            # that needs to tell a comped row from a rented one.
            rented = entitlements(conn, guild_id, user_id)
            ent = comp_entitlements(
                rented, is_staff=is_staff, comp_enabled=settings.mod_perk_comp
            )
            comped = ent - rented
            balance = get_balance(conn, guild_id, user_id)
            icon_range = catalog_price_range(conn, guild_id)
            color_range = color_price_range(
                conn, guild_id, settings.price_role_preset
            )
            rentals = [dict(r) for r in list_refundable_rentals(conn, guild_id, user_id)]
            shields_held, shield_price = get_streak_shield_status(
                conn, guild_id, user_id, settings
            )
            now = time.time()
            items = shop_items_svc.shop_items_for(
                conn, guild_id, now=now, user_id=user_id
            )
            owned_items = {
                i.item_id
                for i in items
                if shop_items_svc.owned_count(conn, guild_id, user_id, i.item_id)
            }
        return _ShopContext(
            settings, ent, comped, balance, icon_range, color_range, rentals,
            shields_held, shield_price, items, owned_items,
        )


    def _load_role_ctx(
        self, guild_id: int, user_id: int, is_staff: bool = False
    ) -> tuple[EconSettings, set[str]]:
        """Settings and the member's entitlements, for the role-studio gates.

        No balance: the shop header is the only surface that shows one, and it
        reads the whole page through ``_shop_context``.
        """
        with self.bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            ent = effective_entitlements(
                conn, guild_id, user_id, is_staff=is_staff
            )
        return settings, ent


    def _name_blocklist(self, guild_id: int) -> list[str]:
        with self.bot.ctx.open_db() as conn:
            return list_name_blocklist(conn, guild_id)


    def _upsert_role(
        self, guild_id: int, user_id: int, values: dict[str, object]
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            upsert_personal_role(conn, guild_id, user_id, values)


    def _load_catalog(self, guild_id: int) -> list[dict]:
        """Enabled catalog icons a member may rent, as plain dicts for the view."""
        with self.bot.ctx.open_db() as conn:
            return [
                {"id": int(r["id"]), "name": r["name"], "price": int(r["price"])}
                for r in list_catalog(conn, guild_id, enabled_only=True)
            ]


    def _load_palette(self, guild_id: int) -> list[dict]:
        """Rentable palette colours, as plain dicts for the gallery.

        ``price`` resolves the per-colour override against the flat perk price
        here, so the select's descriptions quote what the member will actually be
        charged rather than a raw 0. ``key`` and ``image_path`` are what the
        swatch gallery draws from — the art if it is on disk, the two hexes if
        it is not.
        """
        with self.bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            return [
                {
                    "id": int(r["id"]),
                    "key": r["key"],
                    "name": r["name"],
                    "hex1": r["hex1"],
                    "hex2": r["hex2"],
                    "image_path": r["image_path"],
                    "price": int(r["price"]) or settings.price_role_preset,
                }
                for r in list_color_catalog(conn, guild_id, rentable_only=True)
            ]


    def _icon_price_range(self, guild_id: int) -> tuple[int, int, int] | None:
        """(min, max, count) over enabled icons, or None with no catalog set up."""
        with self.bot.ctx.open_db() as conn:
            return catalog_price_range(conn, guild_id)


    def _catalog_locked(self, guild_id: int, user_id: int) -> bool:
        """Whether this member's live icon rental is tied to a catalog icon.

        A catalog rental's image IS the icon being paid for, so the
        bring-your-own upload paths are blocked until the member switches to
        the flat-price Custom entry in the shop picker. A custom rental (or no
        rental — the entitlement check upstream handles that) uploads freely,
        catalog or not.
        """
        with self.bot.ctx.open_db() as conn:
            row = get_live_role_icon_rental(conn, guild_id, user_id)
        return row is not None and row["catalog_icon_id"] is not None


    async def _require_economy_enabled(self, guild_id: int) -> None:
        """Raise with a reason the admin can act on when the economy is off.

        The panel route maps ValueError to a 400 carrying the message; returning
        None instead would surface as "Discord rejected the post", pointing at
        the wrong thing entirely.
        """
        settings = await asyncio.to_thread(self._load_settings, guild_id)
        if not settings.enabled:
            raise ValueError(
                "The economy is disabled for this server — turn it on under "
                "Economy → Settings before posting its panels."
            )


    def _load_settings(self, guild_id: int) -> EconSettings:
        with self.bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)


    def _save_settings(self, guild_id: int, values: dict[str, object]) -> None:
        with self.bot.ctx.open_db() as conn:
            save_econ_settings(conn, guild_id, values)
