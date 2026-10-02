"""Personal-role perks: custom emoji, name, color, gradient and icon."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    parse_hex_color,
)
from bot_modules.economy.color_palette import (
    showroom_page,
)
from bot_modules.economy.color_catalog_service import (
    color_ints,
    get_catalog_color,
)
from bot_modules.economy.icon_catalog_service import (
    get_catalog_icon,
)
from bot_modules.economy.rentals_service import (
    get_live_preset_rental,
    get_live_role_icon_rental,
    rent_perk,
    set_rental_catalog_color,
    set_rental_catalog_icon,
    upsert_personal_role,
)
from bot_modules.economy import emoji_service as emoji_svc
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
)
from bot_modules.voice.master.service import (
    name_is_blocked,
)

if TYPE_CHECKING:
    pass

from bot_modules.economy.cog.base import (
    EconomyBase,
)
from bot_modules.economy.cog.common import (
    _CATALOG_LOCKED_MSG,
    _MAX_ICON_BYTES,
    _MAX_ROLE_NAME_LEN,
    _NICK_FAILED,
    _NICK_FORBIDDEN,
    _NO_ROLE_ICONS_MSG,
    _custom_name_confirmation,
    _icon_store_path,
    _resolve_guild_emoji,
)
from bot_modules.economy.cog.views import (
    _CFG_MODALS,
    _ColorPaletteView,
    _EmojiCancelView,
    _ICON_SELECT_LIMIT,
    _IconCatalogView,
    _PostRentView,
    _palette_files,
    _rent_perk_flow,
)

log = logging.getLogger("dungeonkeeper.economy")


class PerksMixin(EconomyBase):
    """Personal-role perks: custom emoji, name, color, gradient and icon.

    Mixed into ``EconomyCog`` (economy_cog.py), which was split by area in
    standards audit row 4.29. Moved verbatim: the attributes it reads
    (``self.bot``, the StickyPanels, the caches) are set in the cog's
    ``__init__``.
    """

    #: The line above the swatches, on the first page and on every flip.
    _PALETTE_INTRO = (
        "Pick a color to rent — each is billed weekly, and you can switch any time:"
    )

    async def _emoji_status(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        member: discord.Member,
    ) -> None:
        """Bare /bank emoji: show the member's in-flight sponsorship, if any."""

        def _load():
            with self.bot.ctx.open_db() as conn:
                return emoji_svc.open_submission(conn, guild.id, member.id)

        row = await asyncio.to_thread(_load)
        if row is None:
            await interaction.response.send_message(
                "Sponsor a custom emoji with `/bank emoji image: name:` — "
                f"{settings.currency_emoji} {settings.price_emoji:,}/week "
                f"(animated {settings.price_emoji_animated:,}), first week "
                "held at submit, mod-approved.",
                ephemeral=True,
            )
            return
        state = str(row["state"])
        if state == "pending":
            view = _EmojiCancelView(self._cog, int(row["id"]), member.id)
            await interaction.response.send_message(
                f"Your :{row['name']}: is waiting for a mod. Cancel to get "
                "your escrow back:",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return
        blurb = (
            "being set up" if state == "approved"
            else "live — it renews weekly from your wallet"
        )
        await interaction.response.send_message(
            f"Your sponsored :{row['name']}: is {blurb}.", ephemeral=True
        )


    async def do_cancel_emoji(
        self, interaction: discord.Interaction, submission_id: int
    ) -> None:
        def _cancel():
            with self.bot.ctx.open_db() as conn:
                return emoji_svc.cancel_submission(
                    conn, submission_id, user_id=interaction.user.id
                )

        try:
            row = await asyncio.to_thread(_cancel)
        except ValueError as exc:
            await interaction.response.edit_message(content=str(exc), view=None)
            return
        await interaction.response.edit_message(
            content=(
                f"Cancelled :{row['name']}: — your "
                f"{int(row['price']):,} escrow is back in your wallet."
            ),
            view=None,
        )


    async def open_customise_modal(
        self, interaction: discord.Interaction, perk: str
    ) -> None:
        await interaction.response.send_modal(_CFG_MODALS[perk](self._cog))


    async def open_icon_catalog(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """Show the curated icon picker (rent a new icon or switch the rented one)."""
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return
        icons = await asyncio.to_thread(self._load_catalog, guild.id)
        if not icons:
            await interaction.response.send_message(
                "❌ No rentable icons are set up here yet.", ephemeral=True
            )
            return
        note = ""
        if len(icons) > _ICON_SELECT_LIMIT:
            note = f"\n_Showing the first {_ICON_SELECT_LIMIT} icons._"
        view = _IconCatalogView(self._cog, settings, guild, interaction.user.id, icons)
        await interaction.response.send_message(
            f"Pick a role icon to rent — each is billed weekly:{note}",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


    async def open_color_palette(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """Open the swatch showroom: the gradients themselves, and a picker.

        This is the whole showroom now — it used to be a channel of permanent
        image posts, because a select menu can name a colour but not show it.
        The art rides along as attachments on an ephemeral message instead, so
        the gradients are seen where they are bought and no server has to spend
        a channel on them.
        """
        if not await self._palette_gate_ok(interaction, guild.id):
            return
        colors = await asyncio.to_thread(self._load_palette, guild.id)
        if not colors:
            await interaction.response.send_message(
                "❌ No rentable colors are set up here yet.", ephemeral=True
            )
            return
        # File reads and gradient rendering — off the event loop, like the load.
        page = await asyncio.to_thread(
            showroom_page, colors, 0, currency_emoji=settings.currency_emoji
        )
        view = _ColorPaletteView(
            self._cog, settings, guild, interaction.user.id, colors,
            page=page.page, page_count=page.page_count,
        )
        await interaction.response.send_message(
            self._PALETTE_INTRO,
            embeds=page.embeds,
            files=_palette_files(page),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


    async def show_palette_page(
        self,
        interaction: discord.Interaction,
        view: _ColorPaletteView,
        page: int,
    ) -> None:
        """Flip the open gallery to another page, in place.

        The swatches are re-uploaded rather than kept around: a ``discord.File``
        is spent when it is sent, and holding ten images per page in memory for
        every open shop costs more than reading them again.
        """
        built = await asyncio.to_thread(
            showroom_page, view.colors, page,
            currency_emoji=view.settings.currency_emoji,
        )
        view.page = built.page
        view.page_count = built.page_count
        view.rebuild()
        await interaction.response.edit_message(
            content=self._PALETTE_INTRO,
            embeds=built.embeds,
            attachments=_palette_files(built),
            view=view,
        )


    async def pick_catalog_color(
        self, interaction: discord.Interaction, guild: discord.Guild, color_id: int
    ) -> None:
        """Rent the chosen palette colour, or switch a live rental to it.

        A fresh rental charges the colour's price upfront; switching an existing
        rental only re-tags it (no charge) so the newly chosen colour's price
        takes effect at the next weekly renewal — the same deal as the icon
        catalog. A staff comp wears the colour with no rental row at all.
        """
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, dict | None, int | None]:
            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                row = get_catalog_color(conn, guild.id, color_id)
                pair = color_ints(row) if row is not None else None
                color = (
                    {
                        "name": str(row["name"]),
                        "price": int(row["price"]) or settings.price_role_preset,
                        "pair": pair,
                    }
                    if row is not None and int(row["enabled"]) and pair is not None
                    else None
                )
                existing = get_live_preset_rental(conn, guild.id, user_id)
                existing_id = int(existing["id"]) if existing is not None else None
            return settings, color, existing_id

        settings, color, existing_id = await asyncio.to_thread(_load)
        if await self._refuse_disabled(interaction, settings):
            return
        if color is None:
            await interaction.response.send_message(
                "❌ That color isn't available anymore — open the shop again.",
                ephemeral=True,
            )
            return
        if not await self._palette_gate_ok(interaction, guild.id):
            return

        primary, secondary = color["pair"]
        colors_patch = {"color": primary, "color2": secondary}
        comped = settings.mod_perk_comp and self.bot.ctx.is_mod(interaction)
        if existing_id is None and comped:
            # The comp entitles role_preset, and a palette colour IS that perk at
            # a per-colour price — charging staff here would make the curated
            # colours the one thing the comp didn't cover. No rental row: a comp
            # never creates one.
            def _set_comped_color() -> None:
                with self.bot.ctx.open_db() as conn:
                    upsert_personal_role(conn, guild.id, user_id, colors_patch)

            await asyncio.to_thread(_set_comped_color)
            verb = "Set"
        elif existing_id is None:
            # New rental: rent_perk + colour set ride ONE transaction, so a
            # failed upfront debit rolls the whole thing back (the ValueError
            # must propagate out of the `with` block — never caught inside it).
            def _rent() -> None:
                with self.bot.ctx.open_db() as conn:
                    rent_perk(
                        conn, settings, guild.id, user_id, "role_preset",
                        catalog_color_id=color_id, now=time.time(),
                    )
                    upsert_personal_role(conn, guild.id, user_id, colors_patch)

            try:
                await asyncio.to_thread(_rent)
            except ValueError as exc:
                msg = str(exc)
                if "insufficient" in msg:
                    text = await self._short_funds_text(
                        settings, guild.id, user_id, int(color["price"])
                    )
                elif "already rented" in msg:
                    text = "❌ You're already renting a palette color."
                else:
                    text = "❌ That color isn't available."
                await interaction.response.send_message(text, ephemeral=True)
                return
            verb = "Rented"
        else:
            def _switch() -> None:
                with self.bot.ctx.open_db() as conn:
                    set_rental_catalog_color(conn, guild.id, existing_id, color_id)
                    upsert_personal_role(conn, guild.id, user_id, colors_patch)

            await asyncio.to_thread(_switch)
            verb = "Switched to"

        # Defer before apply_role_perks — its rate-limited role edits can exceed
        # the 3s budget, and the rent/switch is already committed.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await apply_role_perks(self.bot, self.bot.ctx.db_path, guild.id, user_id)
        tail = (
            "" if ok else " (I couldn't update your role right now — try again shortly.)"
        )
        # "On the house" only where nothing is actually being billed. A comped
        # mod who was already paying for a palette rental still pays it — the
        # comp never cancels a rental on someone's behalf — so quoting them a
        # free switch would be wrong. (The icon picker has the same shape at
        # `pick_catalog_icon`; it predates this and is left alone here.)
        price_note = (
            " (on the house)"
            if comped and existing_id is None
            else f" ({settings.currency_emoji} {color['price']:,}/week)"
        )
        await interaction.edit_original_response(
            content=f"{verb} **{color['name']}**{price_note}.{tail}",
        )


    async def pick_catalog_icon(
        self, interaction: discord.Interaction, guild: discord.Guild, icon_id: int
    ) -> None:
        """Rent the chosen catalog icon, or switch a live rental to it.

        A fresh rental charges the icon's price upfront; switching an existing
        rental only re-tags it (no charge) so the newly chosen icon's price
        takes effect at the next weekly renewal.
        """
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, dict | None, int | None]:
            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                row = get_catalog_icon(conn, guild.id, icon_id)
                icon = (
                    {
                        "name": row["name"],
                        "price": int(row["price"]),
                        "image_path": str(row["image_path"]),
                    }
                    if row is not None and int(row["enabled"])
                    else None
                )
                existing = get_live_role_icon_rental(conn, guild.id, user_id)
                existing_id = int(existing["id"]) if existing is not None else None
            return settings, icon, existing_id

        settings, icon, existing_id = await asyncio.to_thread(_load)
        if await self._refuse_disabled(interaction, settings):
            return
        if icon is None:
            await interaction.response.send_message(
                "❌ That icon isn't available anymore — open the shop again.",
                ephemeral=True,
            )
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return

        comped = settings.mod_perk_comp and self.bot.ctx.is_mod(interaction)
        if existing_id is None and comped:
            # The comp entitles role_icon, and a catalog icon IS that perk at a
            # per-icon price — so charging staff here would have made the
            # curated icons the one thing the comp didn't cover. No rental row:
            # a comp never creates one, and without it this branch stays the
            # path every time they change icon.
            def _set_comped_icon() -> None:
                with self.bot.ctx.open_db() as conn:
                    upsert_personal_role(
                        conn, guild.id, user_id, {"icon_path": icon["image_path"]}
                    )

            await asyncio.to_thread(_set_comped_icon)
            verb = "Set"
        elif existing_id is None:
            # New rental: rent_perk + icon set ride ONE transaction, so a failed
            # upfront debit rolls the whole thing back (the ValueError must
            # propagate out of the `with` block — never caught inside it).
            def _rent() -> None:
                with self.bot.ctx.open_db() as conn:
                    rent_perk(
                        conn, settings, guild.id, user_id, "role_icon",
                        catalog_icon_id=icon_id, now=time.time(),
                    )
                    upsert_personal_role(
                        conn, guild.id, user_id, {"icon_path": icon["image_path"]}
                    )

            try:
                await asyncio.to_thread(_rent)
            except ValueError as exc:
                msg = str(exc)
                if "insufficient" in msg:
                    text = await self._short_funds_text(
                        settings, guild.id, user_id, int(icon["price"])
                    )
                elif "already rented" in msg:
                    text = "❌ You're already renting a role icon."
                else:
                    text = "❌ That icon isn't available."
                await interaction.response.send_message(text, ephemeral=True)
                return
            verb = "Rented"
        else:
            def _switch() -> None:
                with self.bot.ctx.open_db() as conn:
                    set_rental_catalog_icon(conn, guild.id, existing_id, icon_id)
                    upsert_personal_role(
                        conn, guild.id, user_id, {"icon_path": icon["image_path"]}
                    )

            await asyncio.to_thread(_switch)
            verb = "Switched to"

        # Defer before apply_role_perks — its rate-limited role edits can exceed
        # the 3s budget, and the rent/switch is already committed.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await apply_role_perks(self.bot, self.bot.ctx.db_path, guild.id, user_id)
        tail = (
            "" if ok else " (I couldn't update your role right now — try again shortly.)"
        )
        # A comped icon has no weekly price to quote — saying one would imply a
        # charge that never happens.
        price_note = (
            " (on the house)"
            if comped
            else f" ({settings.currency_emoji} {icon['price']:,}/week)"
        )
        await interaction.edit_original_response(
            content=f"{verb} the **{icon['name']}** icon{price_note}.{tail}",
        )


    async def pick_custom_icon(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """The picker's Custom entry: rent the flat-price bring-your-own icon,
        or switch a live catalog rental back to it.

        Switching re-tags the rental (no charge; the flat price bills from the
        next renewal, mirroring an icon-to-icon switch) and clears the
        projected image — the catalog art belongs to the catalog price, so the
        member starts from a blank icon and uploads their own.
        """
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, dict | None]:
            with self.bot.ctx.open_db() as conn:
                fresh = load_econ_settings(conn, guild.id)
                row = get_live_role_icon_rental(conn, guild.id, user_id)
                existing = (
                    {
                        "id": int(row["id"]),
                        "catalog_icon_id": row["catalog_icon_id"],
                    }
                    if row is not None
                    else None
                )
            return fresh, existing

        settings, existing = await asyncio.to_thread(_load)
        if await self._refuse_disabled(interaction, settings):
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return

        if existing is None:
            # Fresh rental — the shared rent flow already handles the charge,
            # the error copy, and the upload note + customise button.
            await _rent_perk_flow(interaction, self._cog, settings, guild, "role_icon")
            return
        if existing["catalog_icon_id"] is None:
            view = _PostRentView(
                self._cog, "role_icon",
                body=(
                    "You already have the custom icon — set an emoji "
                    "below, or upload an image with `/bank role icon`."
                ),
            )
            await interaction.response.send_message(view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return

        def _switch() -> None:
            with self.bot.ctx.open_db() as conn:
                set_rental_catalog_icon(conn, guild.id, existing["id"], None)
                upsert_personal_role(conn, guild.id, user_id, {"icon_path": ""})

        await asyncio.to_thread(_switch)
        await apply_role_perks(self.bot, self.bot.ctx.db_path, guild.id, user_id)
        view = _PostRentView(
            self._cog, "role_icon",
            body=(
                f"Switched to a **custom icon** ({settings.currency_emoji} "
                f"{settings.price_role_icon:,}/week from your next "
                "renewal). Set an emoji below, or upload an image with "
                "`/bank role icon`."
            ),
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


    async def set_role_name(self, interaction: discord.Interaction, text: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_name"):
            return
        text = text.strip()
        if not text or len(text) > _MAX_ROLE_NAME_LEN:
            await interaction.response.send_message(
                f"❌ Role names must be 1–{_MAX_ROLE_NAME_LEN} characters.", ephemeral=True
            )
            return
        patterns = await asyncio.to_thread(self._name_blocklist, guild.id)
        if name_is_blocked(text, patterns):
            await interaction.response.send_message(
                "❌ That name isn't allowed here.", ephemeral=True
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"name": text}
        )
        nick_ok, nick_reason = await self._apply_custom_nick(interaction.user, text)
        await self._apply_and_confirm(
            interaction,
            guild.id,
            user_id,
            _custom_name_confirmation(text, nick_ok=nick_ok, nick_reason=nick_reason),
        )


    async def _apply_custom_nick(
        self, member: discord.User | discord.Member, text: str
    ) -> tuple[bool, str]:
        """Set ``member``'s server nickname to their custom name (best-effort).

        Returns ``(ok, reason)``. The role rename stands regardless; the
        nickname can fail when the bot lacks Manage Nicknames, the member
        outranks the bot, or the member is the guild owner (a Discord limit).
        """
        if not isinstance(member, discord.Member):
            return False, ""
        try:
            await member.edit(nick=text, reason="Economy custom-name perk")
        except discord.Forbidden:
            return False, _NICK_FORBIDDEN
        except discord.HTTPException:
            return False, _NICK_FAILED
        return True, ""


    async def set_role_color(self, interaction: discord.Interaction, hex: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_color"):
            return
        value = parse_hex_color(hex)
        if value is None:
            await interaction.response.send_message(
                "❌ Give a color as a hex code like `#7B2FF7`.", ephemeral=True
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"color": value}
        )
        await self._apply_and_confirm(
            interaction, guild.id, user_id, f"Your role color is now `#{value:06X}`."
        )


    async def set_role_gradient(
        self, interaction: discord.Interaction, hex1: str, hex2: str
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_gradient"):
            return
        if not await feature_gate_ok(self.bot, guild.id, "role_gradient"):
            await interaction.response.send_message(
                "❌ This server doesn't support gradient roles right now.", ephemeral=True
            )
            return
        v1, v2 = parse_hex_color(hex1), parse_hex_color(hex2)
        if v1 is None or v2 is None:
            await interaction.response.send_message(
                "❌ Give both colors as hex codes like `#7B2FF7`.", ephemeral=True
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"color": v1, "color2": v2}
        )
        await self._apply_and_confirm(
            interaction, guild.id, user_id,
            f"Your gradient is now `#{v1:06X}` → `#{v2:06X}`.",
        )


    async def set_role_icon_emoji(
        self, interaction: discord.Interaction, raw: str
    ) -> None:
        """Set the role icon from one of the server's custom emojis (modal path)."""
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
        emoji = _resolve_guild_emoji(guild, raw)
        if emoji is None:
            await interaction.response.send_message(
                "❌ That doesn't match a custom emoji on this server — type its "
                "name like `:party_parrot:`. For an image icon, upload one "
                "with `/bank role icon`.",
                ephemeral=True,
            )
            return
        if emoji.animated:
            await interaction.response.send_message(
                "❌ Animated emojis can't be role icons — pick a static one.",
                ephemeral=True,
            )
            return
        try:
            data = await emoji.read()
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I couldn't fetch that emoji's image — try again shortly.",
                ephemeral=True,
            )
            return
        if len(data) > _MAX_ICON_BYTES:
            await interaction.response.send_message(
                "❌ That emoji's image is too big — 256KB max.", ephemeral=True
            )
            return
        await self._store_role_icon(interaction, guild.id, user_id, data)


    async def _role_icon_gate_ok(
        self, interaction: discord.Interaction, guild_id: int
    ) -> bool:
        """Whether role icons are available here; sends the refusal if not."""
        if await feature_gate_ok(self.bot, guild_id, "role_icon"):
            return True
        await interaction.response.send_message(_NO_ROLE_ICONS_MSG, ephemeral=True)
        return False


    async def _palette_gate_ok(
        self, interaction: discord.Interaction, guild_id: int
    ) -> bool:
        """Whether palette colours can render here; sends the refusal if not.

        A palette colour is a two-colour fade, so it needs the same
        ENHANCED_ROLE_COLORS support as the free-form gradient.
        """
        if await feature_gate_ok(self.bot, guild_id, "role_preset"):
            return True
        await interaction.response.send_message(
            "❌ This server doesn't support gradient roles right now.", ephemeral=True
        )
        return False


    async def _store_role_icon(
        self, interaction: discord.Interaction, guild_id: int, user_id: int, data: bytes
    ) -> None:
        """Persist role-icon bytes and re-apply the role.

        Shared tail of the two acquisition paths — the emoji modal and the
        `/bank role icon` upload — which differ only in where ``data`` and its
        size refusal come from.
        """
        def _write() -> None:
            # _icon_store_path mkdirs, so it belongs on the worker thread too.
            path = _icon_store_path(self.bot.ctx.db_path, guild_id, user_id)
            path.write_bytes(data)
            self._upsert_role(guild_id, user_id, {"icon_path": str(path)})

        await asyncio.to_thread(_write)
        await self._apply_and_confirm(
            interaction, guild_id, user_id, "Your role icon is set."
        )
