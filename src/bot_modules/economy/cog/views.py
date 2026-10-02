"""The economy cog's views, modals and panel buttons."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from typing import TYPE_CHECKING, Any, cast

import discord

from bot_modules.core.layout import ExpiringLayoutView, card
from bot_modules.core.layout import bind as bind_layout
from bot_modules.core.utils import ExpiringView, MemberScoped
from bot_modules.docs.help_card import load_doc_body
from bot_modules.economy.bank_panel_layout import (
    build_economy_panel_layout,
)
from bot_modules.economy.guide import (
    GUIDE_TOPIC_CUSTOM_ID,
    HOW_IT_WORKS_CUSTOM_ID,
    NOTIFY_CUSTOM_ID,
    show_guide,
    show_guide_topic,
    toggle_notify,
)
from bot_modules.economy.layouts import (
    PAY_CANCEL_ID,
    PAY_CONFIRM_ID,
    build_pay_confirm_layout,
)
from bot_modules.economy.leaderboard import (
    LeaderboardData,
)
from bot_modules.economy.perks import (
    NO_CONFIG_PERKS,
    PERK_LABELS,
    perk_on_sale,
    perk_price,
)
from bot_modules.economy.shop import (
    SECTION_COSMETICS,
    SECTION_GAMES,
    SECTION_SERVER,
    SECTION_SPECIALS,
    SECTION_YOURS,
    store_page,
)
from bot_modules.economy.shop_layouts import (
    NEXT_PAGE_ID as SHOP_NEXT_PAGE_ID,
    POST_RENT_CUSTOMISE_ID,
    PREV_PAGE_ID as SHOP_PREV_PAGE_ID,
    REFUND_BACK_ID,
    REFUND_CONFIRM_ID,
    REFUND_ROW_ID_BASE,
    SHOP_HELP_CUSTOM_ID,
    SHOP_HELP_DOC_KEY,
    SHOP_PANEL_OPEN_CUSTOM_ID,
    SHOP_SECTION_CUSTOM_IDS,
    STORE_ROW_ID_BASE,
    ShopRow,
    build_post_rent_layout,
    build_refund_confirm_layout,
    build_refund_picker_layout,
    build_role_notice_layout,
    build_shop_chooser_layout,
    build_shop_help_layout,
    build_shop_panel_layout,
    build_shop_section_layout,
    build_store_layout,
    shop_section_rows,
)
from bot_modules.economy.shop_items import ItemView
from bot_modules.economy import shop_items_service as shop_items_svc
from bot_modules.economy.perk_actions import (
    apply_role_perks,
)
from bot_modules.economy.quest_views import (
    QUEST_BOARD_CUSTOM_ID,
    WALLET_CUSTOM_ID,
    open_my_quests,
    open_wallet,
)
from bot_modules.core.branding_service import PanelArt
from bot_modules.economy.bounty_service import (
    MAX_DESC_LEN,
    MAX_TITLE_LEN,
)
from bot_modules.economy.pin_service import (
    MAX_PIN_LEN,
    MIN_PIN_LEN,
)
from bot_modules.economy.theme_service import (
    MAX_BLURB_LEN,
    MAX_TITLE_LEN as MAX_THEME_TITLE_LEN,
    MIN_BLURB_LEN,
    MIN_TITLE_LEN as MIN_THEME_TITLE_LEN,
)
from bot_modules.economy.color_palette import (
    PALETTE_PAGE_SIZE,
    ShowroomPage,
)
from bot_modules.economy.rentals_service import (
    rent_perk,
)
from bot_modules.economy.service import (
    EconSettings,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

from bot_modules.economy.cog.common import (
    _MAX_ROLE_NAME_LEN,
)

if TYPE_CHECKING:
    from bot_modules.economy.economy_cog import EconomyCog

log = logging.getLogger("dungeonkeeper.economy")


class _MemberScopedView(MemberScoped, ExpiringView):
    """A view usable only by the member it was opened for.

    Shared base for the shop-adjacent pickers (``_ShopView``,
    ``_IconCatalogView``, ``_RefundPickerView``) and for the confirm gates,
    which were each carrying an identical ``interaction_check``. Subclasses
    differ only in what the refusal says and how long they live, so those are
    a class attribute and a constructor argument; the check itself is
    :class:`MemberScoped`'s.
    """

    SCOPE_DENIAL = "❌ Open your own shop with /bank shop."

    def __init__(self, user_id: int, *, timeout: float | None = 120) -> None:
        super().__init__(timeout=timeout)
        self.scope_user_id = user_id


class _GiftConfirmView(_MemberScopedView):
    """Ephemeral Confirm/Cancel gate for gifting a perk the friend already has.

    The rental would stack silently (their role already shows the perk), so
    the double-spend has to be an explicit choice, mirroring _PayConfirmView.
    """

    SCOPE_DENIAL = "❌ This confirmation isn't yours."

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        gifter: discord.Member,
        member: discord.Member,
        perk: str,
    ) -> None:
        super().__init__(gifter.id, timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.gifter = gifter
        self.member = member
        self.perk = perk

    @discord.ui.button(label="Gift Anyway", style=discord.ButtonStyle.success)
    async def _confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await self.cog.finalize_gift(
            interaction, self.settings, self.guild, self.gifter, self.member,
            self.perk, via_confirm=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Gift cancelled.", view=None
        )


class _RaffleBuyModal(discord.ui.Modal, title="Weekly Raffle Tickets"):
    quantity = discord.ui.TextInput(
        label="How many tickets?", min_length=1, max_length=3, placeholder="1"
    )

    def __init__(self, cog: EconomyCog, settings: EconSettings) -> None:
        super().__init__()
        self.cog = cog
        self.settings = settings

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_buy_raffle_tickets(
            interaction, self.settings, str(self.quantity.value)
        )


class _EmojiCancelView(_MemberScopedView):
    """Cancel button on the bare /bank emoji status reply (pending only)."""

    SCOPE_DENIAL = "❌ This isn't your submission."

    def __init__(self, cog: EconomyCog, submission_id: int, user_id: int) -> None:
        super().__init__(user_id, timeout=60)
        self.cog = cog
        self.submission_id = submission_id

    @discord.ui.button(label="Cancel & Refund", style=discord.ButtonStyle.danger)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await self.cog.do_cancel_emoji(interaction, self.submission_id)


class _RoleNameModal(discord.ui.Modal, title="Set Your Custom Name"):
    text = discord.ui.TextInput(
        label="Name",
        min_length=1,
        max_length=_MAX_ROLE_NAME_LEN,
        placeholder="Becomes your server nickname + your role name",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_name(interaction, str(self.text.value))


class _RoleColorModal(discord.ui.Modal, title="Custom Role Color"):
    hex_value = discord.ui.TextInput(
        label="Hex color", min_length=3, max_length=9, placeholder="#7B2FF7"
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_color(interaction, str(self.hex_value.value))


class _RoleGradientModal(discord.ui.Modal, title="Gradient Role"):
    hex1 = discord.ui.TextInput(
        label="First hex color", min_length=3, max_length=9, placeholder="#7B2FF7"
    )
    hex2 = discord.ui.TextInput(
        label="Second hex color", min_length=3, max_length=9, placeholder="#2FF7B2"
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_gradient(
            interaction, str(self.hex1.value), str(self.hex2.value)
        )


class _RoleIconModal(discord.ui.Modal, title="Role Icon"):
    emoji = discord.ui.TextInput(
        label="Server emoji",
        min_length=1,
        max_length=100,
        placeholder=":emoji_name: — a custom emoji from this server",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_icon_emoji(interaction, str(self.emoji.value))


class _PinSubmitModal(discord.ui.Modal, title="Pin a Message"):
    """The paragraph a member pays to pin; a mod reviews it before it goes up."""

    text: discord.ui.TextInput = discord.ui.TextInput(
        label="Your message",
        style=discord.TextStyle.paragraph,
        min_length=MIN_PIN_LEN,
        max_length=MAX_PIN_LEN,
        placeholder="Keep it short and fun — a mod approves it before it pins.",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_pin_submit(interaction, str(self.text.value))


class _ThemeSubmitModal(discord.ui.Modal, title="Buy a Themed Day"):
    """A theme name plus what people should do with it; a mod reviews both."""

    theme_title: discord.ui.TextInput = discord.ui.TextInput(
        label="Theme name",
        min_length=MIN_THEME_TITLE_LEN,
        max_length=MAX_THEME_TITLE_LEN,
        placeholder="e.g. Cursed Cooking",
    )
    blurb: discord.ui.TextInput = discord.ui.TextInput(
        label="What should people post?",
        style=discord.TextStyle.paragraph,
        min_length=MIN_BLURB_LEN,
        max_length=MAX_BLURB_LEN,
        placeholder="A line or two — a bare name doesn't tell anyone what to do.",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_theme_submit(
            interaction, str(self.theme_title.value), str(self.blurb.value)
        )


class _BountyPostModal(discord.ui.Modal, title="Post a Bounty"):
    """A freeform task plus the poster's opening stake into the pot."""

    b_title: discord.ui.TextInput = discord.ui.TextInput(
        label="Bounty",
        max_length=MAX_TITLE_LEN,
        placeholder="What needs doing? e.g. 'Draw our mascot as a knight'",
    )
    description: discord.ui.TextInput = discord.ui.TextInput(
        label="Details (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=MAX_DESC_LEN,
        placeholder="Any rules, deadline, or what 'done' looks like.",
    )
    stake: discord.ui.TextInput = discord.ui.TextInput(
        label="Your opening stake",
        max_length=12,
        placeholder="Coins you put in to start the pot",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_bounty_post(
            interaction,
            str(self.b_title.value),
            str(self.description.value or ""),
            str(self.stake.value),
        )


# Which modal customises which perk; a gifted perk uses the same modal as a
# self-rented one (entitlements are beneficiary-based, so the rows match).
_CFG_MODALS = {
    "role_name": _RoleNameModal,
    "role_color": _RoleColorModal,
    "role_gradient": _RoleGradientModal,
    "role_icon": _RoleIconModal,
}


# Discord caps a select at 25 options; the last slot is the bring-your-own
# Custom entry, so a larger catalog shows its first 24 (by sort order) and
# tells the member the list was trimmed.
_ICON_SELECT_LIMIT = 24


class _IconCatalogSelect(discord.ui.Select):
    """A picker of curated role icons; choosing one rents or switches to it.

    The final option is always **Custom** — the bring-your-own icon at the
    flat ``price_role_icon`` — so stocking a catalog never takes the classic
    upload-your-own rental off the shelf.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        icons: list[dict],
    ) -> None:
        options = [
            discord.SelectOption(
                label=str(icon["name"])[:100],
                value=str(icon["id"]),
                description=(
                    f"{settings.currency_emoji} {int(icon['price']):,} / week"
                )[:100],
            )
            for icon in icons[:_ICON_SELECT_LIMIT]
        ]
        options.append(
            discord.SelectOption(
                label="Custom — Upload Your Own",
                value="custom",
                emoji="🎨",
                description=(
                    f"{settings.currency_emoji} "
                    f"{settings.price_role_icon:,} / week — any image or emoji"
                )[:100],
            )
        )
        super().__init__(
            placeholder="Choose a role icon…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.settings = settings
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "custom":
            await self.cog.pick_custom_icon(interaction, self.settings, self.guild)
            return
        await self.cog.pick_catalog_icon(
            interaction, self.guild, int(self.values[0])
        )


class _IconCatalogView(_MemberScopedView):
    """Ephemeral catalog picker, scoped to the member who opened the shop."""

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        icons: list[dict],
    ) -> None:
        super().__init__(user_id)
        self.add_item(_IconCatalogSelect(cog, settings, guild, icons))


def _palette_files(page: ShowroomPage) -> list[discord.File]:
    """Wrap a built gallery page's swatch bytes for one send.

    Fresh objects every time: a ``discord.File``'s buffer is consumed on send,
    so the page holds bytes and each send (or page flip) gets its own files.
    """
    return [
        discord.File(io.BytesIO(data), filename=name)
        for name, data in page.attachments
    ]


class _ColorPaletteSelect(discord.ui.Select):
    """A picker of curated palette colours; choosing one rents or switches to it.

    Unlike the icon picker there is no bring-your-own slot: picking your own two
    colours is a different product (``role_gradient``), which the shop lists on
    its own row. The options are exactly the colours shown on the current
    gallery page, so the names in the menu and the swatches above it can never
    disagree — and a palette larger than a select's 25 slots pages instead of
    being silently trimmed.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        colors: list[dict],
    ) -> None:
        super().__init__(
            placeholder="Choose a color…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=str(c["name"])[:100],
                    value=str(c["id"]),
                    description=(
                        f"#{c['hex1']} → #{c['hex2']} · "
                        f"{settings.currency_emoji} {int(c['price']):,} / week"
                    )[:100],
                )
                for c in colors
            ],
        )
        self.cog = cog
        self.settings = settings
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.pick_catalog_color(
            interaction, self.guild, int(self.values[0])
        )


class _ColorPaletteView(_MemberScopedView):
    """The shop's swatch gallery: a page of colours you can see, and pick.

    Scoped to the member who opened the shop, and longer-lived than the other
    pickers because browsing art is slower than reading a list.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        colors: list[dict],
        *,
        page: int = 0,
        page_count: int = 1,
    ) -> None:
        super().__init__(user_id, timeout=300)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.colors = colors
        self.page = page
        self.page_count = page_count
        self.rebuild()

    def rebuild(self) -> None:
        """Re-lay the select and the pager for whatever page is showing now."""
        self.clear_items()
        start = self.page * PALETTE_PAGE_SIZE
        shown = self.colors[start : start + PALETTE_PAGE_SIZE]
        self.add_item(_ColorPaletteSelect(self.cog, self.settings, self.guild, shown))
        if self.page_count <= 1:
            return
        prev = discord.ui.Button(
            label="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0
        )
        prev.callback = self._flip(self.page - 1)
        counter = discord.ui.Button(
            label=f"{self.page + 1} / {self.page_count}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        nxt = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.page_count - 1,
        )
        nxt.callback = self._flip(self.page + 1)
        for item in (prev, counter, nxt):
            self.add_item(item)

    def _flip(self, page: int):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.show_palette_page(interaction, self, page)

        return _cb


def _item_bought_text(
    settings: EconSettings,
    outcome: shop_items_svc.PurchaseOutcome,
    *,
    granted: bool,
) -> str:
    """What the buyer is told, which depends on what actually happened.

    A staff-fulfilled order says so plainly and does not promise a time: the
    honest answer is that a human has to do it, and inventing an ETA is how a
    member decides the bot is broken an hour later.
    """
    paid = f"{settings.currency_emoji} **{outcome.price:,}**"
    head = f"✅ Bought **{outcome.item_name}** for {paid}"
    if outcome.state == "pending":
        return (
            f"{head}.\n"
            "The staff have it on their list — you'll get it once someone's "
            "picked it up. If they can't, you get your "
            f"{settings.currency_plural} back."
        )
    if not granted:
        return (
            f"{head}, but I couldn't hand you the role — a mod will need to "
            "sort that out. Your purchase is recorded."
        )
    if outcome.state == "live":
        return f"{head}/week. It renews weekly — cancel any time from the shop."
    return f"{head}. Enjoy!"


_card = card  # core.layout.card; the cogs import it under this name


def _bind_shop_nav(
    view: discord.ui.LayoutView,
    cog: EconomyCog,
    page: int,
    nav: list[str],
    section: str,
) -> None:
    """Wire the shared ◀ / ▶ pair to a page turn, on any shop page view.

    Shared by ``_StoreView`` and ``_ShopView``. The arrows walk one
    section's own book (``shop.section_book``) and never cross into the
    next shelf, so the turn carries the section it started in.
    """
    for item_id, delta in ((SHOP_PREV_PAGE_ID, -1), (SHOP_NEXT_PAGE_ID, 1)):
        button = view.find_item(item_id)
        if button is None:
            continue

        async def _cb(interaction: discord.Interaction, target: int = page + delta) -> None:
            await cog.turn_shop_page(
                interaction, target, section=section, replacing=view
            )

        button.callback = _cb  # type: ignore[method-assign]


class _MemberScopedLayoutView(MemberScoped, ExpiringLayoutView):
    """The ``LayoutView`` sibling of ``_MemberScopedView``.

    Shared base for the shop-adjacent pickers (``_ShopView``,
    ``_StoreView``, ``_RefundPickerView``) and the confirm gate, which were
    each carrying an identical ``interaction_check``. Subclasses differ only
    in what the refusal says and how long they live, so those are a class
    attribute and a constructor argument; the check itself is
    :class:`MemberScoped`'s.
    """

    SCOPE_DENIAL = "❌ Open your own shop with /bank shop."

    def __init__(self, user_id: int, *, timeout: float | None = 120) -> None:
        super().__init__(timeout=timeout)
        self.scope_user_id = user_id


class _PayConfirmView(_MemberScopedLayoutView):
    """Ephemeral Confirm/Cancel gate for a transfer over the threshold.

    **Paradigm 5 — decision card.** Cancel deletes the card silently (guide's
    pattern for a decision that resolves to "never mind" — see
    ``dm_perms_cog.RemoveConfirmView``) rather than editing in a "cancelled"
    notice: there is nothing left to confirm once the answer is no, so a
    card announcing that fact would outlive its own relevance.
    """

    SCOPE_DENIAL = "❌ This confirmation isn't yours."

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        sender: discord.Member,
        recipient: discord.Member,
        amount: int,
        memo: str | None = None,
        public: bool = False,
    ) -> None:
        super().__init__(sender.id, timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.memo = memo
        self.public = public
        for item in build_pay_confirm_layout(
            settings,
            amount=amount,
            recipient_name=discord.utils.escape_markdown(recipient.display_name),
            memo=memo,
            public=public,
        ):
            self.add_item(item)
        confirm = self.find_item(PAY_CONFIRM_ID)
        if confirm is not None:
            confirm.callback = self._on_confirm  # type: ignore[method-assign]
        cancel = self.find_item(PAY_CANCEL_ID)
        if cancel is not None:
            cancel.callback = self._on_cancel  # type: ignore[method-assign]

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.cog.finalize_pay(
            interaction, self.settings, self.guild, self.sender, self.recipient,
            self.amount, memo=self.memo, via_confirm=True, public=self.public,
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


class _StoreNoteModal(discord.ui.Modal):
    """The one free-text field an item can ask its buyer for.

    Only opened for items whose admin ticked "ask for a note" — the name to
    engrave, the colour they want — so a plain purchase never grows a form.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        item: ItemView,
    ) -> None:
        super().__init__(title=f"Buy {item.name}"[:45])
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.item = item
        self.note: discord.ui.TextInput = discord.ui.TextInput(
            label="Anything the staff should know?",
            placeholder="e.g. the name to put on it",
            required=False,
            max_length=shop_items_svc.NOTE_MAX_LEN,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_buy_item(
            interaction, self.settings, self.guild, self.item,
            note=str(self.note.value or ""),
        )


class _StoreView(_MemberScopedLayoutView):
    """One store page of the shop. **Paradigm 6 — list with per-row actions.**

    Scoped to the member who opened it, like every other shop picker. The
    page lives on the view rather than in a custom_id because the whole
    thing is ephemeral and dies with its timeout — nothing here outlives a
    restart, so there is no persistent id to encode state into.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        items: list[ItemView],
        owned_item_ids: set[int],
        balance: int,
        store_index: int = 0,
        *,
        page: int = 0,
        nav: list[str] | None = None,
    ) -> None:
        super().__init__(user_id)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.owned_item_ids = owned_item_ids
        page_items = store_page(items, store_index)
        self.page_items = page_items
        for item in build_store_layout(
            settings, page_items, owned_item_ids=owned_item_ids,
            balance=balance, page=page, nav=nav or [],
        ):
            self.add_item(item)
        for index, product in enumerate(page_items):
            button = self.find_item(STORE_ROW_ID_BASE + index)
            if button is not None and not getattr(button, "disabled", False):
                button.callback = self._make_buy_callback(product)  # type: ignore[method-assign]
        _bind_shop_nav(self, cog, page, nav or [], SECTION_SPECIALS)

    def _make_buy_callback(self, item: ItemView):
        async def _cb(interaction: discord.Interaction) -> None:
            if item.ask_note:
                await interaction.response.send_modal(
                    _StoreNoteModal(self.cog, self.settings, self.guild, item)
                )
                return
            await self.cog.do_buy_item(interaction, self.settings, self.guild, item)

        return _cb


class _RefundPickerView(_MemberScopedLayoutView):
    """The member's cancellable rentals + held shield. **Paradigm 6.**"""

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        rentals: list[dict],
        shield_price: int,
    ) -> None:
        super().__init__(user_id)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.rentals = rentals
        self.shield_price = shield_price
        for item in build_refund_picker_layout(settings, rentals, shield_price):
            self.add_item(item)
        for index, target in enumerate(self._targets()):
            button = self.find_item(REFUND_ROW_ID_BASE + index)
            if button is not None:
                button.callback = self._make_pick_callback(target)

    def _targets(self) -> list[str]:
        out = [f"rental:{int(r['id'])}" for r in self.rentals]
        if self.shield_price > 0:
            out.append("shield")
        return out

    def _make_pick_callback(self, target: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.show_refund_confirm(
                interaction, self.settings, self.guild, target, replacing=self,
            )

        return _cb


class _RefundConfirmView(_MemberScopedLayoutView):
    """Ephemeral Confirm/Back gate before a refund actually runs.
    **Paradigm 10 — confirm.**

    Cancel is silent (guide § Words): Back deletes this ephemeral message
    rather than editing it to a "Nothing changed" card — the panel behind it
    was never touched, so that IS the answer.
    """

    SCOPE_DENIAL = "❌ This confirmation isn't yours."

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        target: str,
        title: str,
        refund_text: str,
    ) -> None:
        super().__init__(user_id, timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.target = target
        for item in build_refund_confirm_layout(settings, title, refund_text):
            self.add_item(item)
        confirm = self.find_item(REFUND_CONFIRM_ID)
        if confirm is not None:
            confirm.callback = self._on_confirm
        back = self.find_item(REFUND_BACK_ID)
        if back is not None:
            back.callback = self._on_back

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.cog.finalize_refund(
            interaction, self.settings, self.guild, self.target
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


class _ShopView(_MemberScopedLayoutView):
    """The controls for one non-Specials section of the shop.
    **Paradigm 6 — list with per-row actions.**

    One row per self-perk on 🎨 Role Cosmetics: Rent when unowned, a
    customise modal when owned. Feature-gated rows are disabled either way,
    and ``owned`` is the viewer's beneficiary-based entitlements, so a
    gifted perk shows its customise button exactly like a self-rented one.

    ``section`` decides which rows appear at all — a page must not carry a
    control for a row its own card does not show. ↩️ Cancel & Refund lives
    on 🧾 Your Rentals only (``SECTION_YOURS``), the one card that gathers
    every rental whichever shelf it came from, so filing it there hides it
    from nobody. ``shop_section_rows`` decides the row list; this class only
    binds behaviour to what that function already decided.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        gated: set[str],
        owned: set[str],
        has_catalog: bool = False,
        has_palette: bool = False,
        shields_held: int = 0,
        refundable: list[dict] | None = None,
        shield_price: int = 0,
        balance: int = 0,
        page: int = 0,
        nav: list[str] | None = None,
        section: str = SECTION_COSMETICS,
        icon_catalog: tuple[int, int, int] | None = None,
        color_catalog: tuple[int, int, int] | None = None,
    ) -> None:
        super().__init__(user_id)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.refundable = refundable or []
        self.shield_price = shield_price
        rows = shop_section_rows(
            settings, section, gated=gated, owned=owned,
            icon_catalog=icon_catalog if has_catalog else None,
            color_catalog=color_catalog, has_palette=has_palette,
            shields_held=shields_held,
            refundable=bool(self.refundable) or shield_price > 0,
        )
        for item in build_shop_section_layout(
            settings, section, rows, balance=balance, page=page, nav=nav or [],
        ):
            self.add_item(item)
        for row in rows:
            if row.disabled:
                continue
            button = self.find_item(row.row_id)
            if button is not None:
                button.callback = self._make_callback(row)
        _bind_shop_nav(self, cog, page, nav or [], section)

    def _make_callback(self, row: ShopRow):
        kind = row.kind
        perk = row.perk

        async def _cb(interaction: discord.Interaction) -> None:
            if kind == "rent" or kind == "voice":
                await self.cog.do_rent(
                    interaction, self.settings, self.guild,
                    perk or "voice_style",
                )
            elif kind == "cfg":
                await self.cog.open_customise_modal(interaction, perk)
            elif kind == "colors":
                await self.cog.open_color_palette(
                    interaction, self.settings, self.guild
                )
            elif kind == "icons":
                await self.cog.open_icon_catalog(
                    interaction, self.settings, self.guild
                )
            elif kind == "pin":
                await interaction.response.send_modal(_PinSubmitModal(self.cog))
            elif kind == "theme":
                await interaction.response.send_modal(_ThemeSubmitModal(self.cog))
            elif kind == "shield":
                await self.cog.do_buy_shield(interaction, self.settings, self.guild)
            elif kind == "raffle":
                await interaction.response.send_modal(
                    _RaffleBuyModal(self.cog, self.settings)
                )
            elif kind == "refund":
                await self.cog.open_refund_picker(
                    interaction, self.settings, self.guild, self.refundable,
                    self.shield_price,
                )

        return _cb


class _PostRentView(ExpiringLayoutView):
    """A fresh rental's confirmation. **Paradigm 7 — receipt**, with an
    optional Customise accessory for a perk that has settings to make.

    ``body`` overrides the standard "Rented/Unlocked" receipt copy — the
    icon picker's own switch confirmations ("Switched to a custom icon…")
    reuse this view for its Customise accessory without being a rental
    receipt themselves.
    """

    def __init__(
        self,
        cog: EconomyCog,
        perk: str,
        *,
        comped: bool = False,
        body: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.perk = perk
        customise = perk not in NO_CONFIG_PERKS and perk != "voice_style"
        items = (
            build_role_notice_layout(body, perk=perk, customise=customise)
            if body is not None
            else build_post_rent_layout(perk, comped=comped, customise=customise)
        )
        for item in items:
            self.add_item(item)
        if customise:
            button = self.find_item(POST_RENT_CUSTOMISE_ID)
            if button is not None:
                button.callback = self._on_customise

    async def _on_customise(self, interaction: discord.Interaction) -> None:
        await self.cog.open_customise_modal(interaction, self.perk)


async def _rent_perk_flow(
    interaction: discord.Interaction,
    cog: EconomyCog,
    settings: EconSettings,
    guild: discord.Guild,
    perk: str,
) -> None:
    """Rent a self-perk from a shop button, then project the role.

    Every reply is ephemeral to the clicker, and a successful rent carries
    the perk's customise button (when it has one) so styling happens
    without leaving the message.
    """
    ctx = cog.bot.ctx
    user_id = interaction.user.id
    if not perk_on_sale(settings, perk):
        # BEFORE the comp check below, which skips rent_perk entirely and so
        # would sail straight past its "not for sale" guard — a comped mod
        # clicking a stale button would otherwise be handed a perk the server
        # had switched off. The button is normally gone; a shop card left
        # open in someone's client, and the icon/palette pickers' own entry
        # points, are what make this reachable.
        await interaction.response.send_message(
            "❌ This server has stopped selling that perk.", ephemeral=True
        )
        return
    # A comped member is already entitled, so there is nothing to buy — take
    # the money-free path rather than opening a rental they'd then be billed
    # for. The shop hides rent buttons for perks it shows as owned, but the
    # icon picker's Custom entry lands here directly, so the guard belongs in
    # the shared flow rather than at each button.
    comped = settings.mod_perk_comp and ctx.is_mod(interaction)

    def _rent() -> None:
        with ctx.open_db() as conn:
            rent_perk(conn, settings, guild.id, user_id, perk, now=time.time())

    try:
        if not comped:
            await asyncio.to_thread(_rent)
    except ValueError as exc:
        msg = str(exc)
        if "insufficient" in msg:
            text = await cog._short_funds_text(
                settings, guild.id, user_id, perk_price(settings, perk)
            )
        elif "already rented" in msg:
            text = "❌ You're already renting that perk."
        elif "not for sale" in msg:
            # Reachable despite the button being hidden: a stale shop card
            # someone left open, or the icon/colour picker's own entry points.
            text = "❌ This server has stopped selling that perk."
        else:
            text = "❌ That perk isn't available."
        await interaction.response.send_message(text, ephemeral=True)
        return

    if perk == "voice_style":
        # No personal role to project and no customise modal — the perk's
        # controls ARE Voice Control's rename/limit, live again from now on.
        view = _PostRentView(cog, perk, comped=comped)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()
        return
    await apply_role_perks(cog.bot, ctx.db_path, guild.id, user_id)
    view = _PostRentView(cog, perk, comped=comped)
    await interaction.response.send_message(view=view, ephemeral=True)
    view.message = await interaction.original_response()


async def _open_shop_from_panel(
    interaction: discord.Interaction, section: str | None = None
) -> None:
    """Route a persistent panel click to the clicker's own shop.

    ``section`` is the Browse (or Manage) the member pressed; ``None`` is a
    button from a panel posted before the split by section — the single
    Open Shop, or a legacy per-perk rent button — and opens the section
    chooser, the nearest thing to what it used to open. The cog is resolved
    at click time — the panel outlives reloads, so nothing rendered at post
    time is trusted at click time.
    """
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ The shop only works in a server.", ephemeral=True
        )
        return
    bot = cast("Bot", interaction.client)
    cog = cast("EconomyCog | None", bot.get_cog("EconomyCog"))
    if cog is None:  # cog unloaded mid-flight; the panel button outlives it
        await interaction.response.send_message(
            "❌ The shop isn't available right now.", ephemeral=True
        )
        return
    if section is None:
        await cog.open_personal_shop(interaction)
    else:
        await cog.open_shop_section(interaction, section)


async def _open_shop_help(interaction: discord.Interaction) -> None:
    """The guild's shop FAQ doc, read now, as an ephemeral card.

    Needs no cog: the doc is read straight from the ``docs`` table, so the
    Help keeps answering even while the economy cog is mid-reload.
    """
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ The shop only works in a server.", ephemeral=True
        )
        return
    bot = cast("Bot", interaction.client)
    body = await asyncio.to_thread(
        load_doc_body, bot.ctx.db_path, interaction.guild.id, SHOP_HELP_DOC_KEY
    )
    view = discord.ui.LayoutView()
    for item in build_shop_help_layout(body):
        view.add_item(item)
    # An admin-written doc may carry a role or @everyone mention; the card
    # is an answer to one tap, never a ping.
    await interaction.response.send_message(
        view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


def _bind_section_buttons(view: discord.ui.LayoutView, sections: list[str]) -> None:
    """Route each Browse / Manage on a chooser to its section's book, and
    its Help to the shop FAQ."""
    bind_layout(view, SHOP_HELP_CUSTOM_ID, _open_shop_help)
    for section in [*sections, SECTION_YOURS]:

        async def _cb(interaction: discord.Interaction, section: str = section) -> None:
            await _open_shop_from_panel(interaction, section)

        bind_layout(view, SHOP_SECTION_CUSTOM_IDS[section], _cb)


#: Every shelf a panel can list — what the ``cog_load`` registration view
#: carries, so a Browse on any panel still routes after a restart.
_ALL_SHELVES = [SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES]


async def _pick_guide_topic(interaction: discord.Interaction) -> None:
    """The panel's **Learn more about…** dropdown.

    Opens the guide card at the picked topic, then schedules the panel's
    reset: a persistent select keeps showing the member's pick until the
    message is edited, and Discord sends nothing for picking the same
    option again (the casino's **Pick a table…** pays the same way).
    """
    await show_guide_topic(interaction)
    bot = cast("Bot", interaction.client)
    cog = cast("EconomyCog | None", bot.get_cog("EconomyCog"))
    if cog is not None and interaction.guild_id is not None:
        cog.schedule_panel_reset(interaction.guild_id)


class EconomyPanelView(discord.ui.LayoutView):
    """The channel economy panel: the live board in the hub's frame, **Help**
    beside the title, the How it works dropdown and the member's buttons at
    the foot.

    Carries no per-message state — every id is static, and the buttons' are
    the ones the embed panel carried — so it is re-registered in
    ``cog_load`` rather than being a DynamicItem. ``items=None`` is that
    registration instance: an empty board, which still carries every id.
    A panel posted before the How it works block (2026-09-28) has no
    dropdown, and its Help still answers.
    """

    def __init__(self, items: list[discord.ui.Item[Any]] | None = None) -> None:
        super().__init__(timeout=None)
        if items is None:
            items = build_economy_panel_layout(
                EconSettings(), LeaderboardData([], [], []),
                lambda uid: f"User {uid}", now_ts=0.0,
            )
        for item in items:
            self.add_item(item)
        bind_layout(self, HOW_IT_WORKS_CUSTOM_ID, show_guide)
        bind_layout(self, GUIDE_TOPIC_CUSTOM_ID, _pick_guide_topic)
        bind_layout(self, QUEST_BOARD_CUSTOM_ID, open_my_quests)
        bind_layout(self, WALLET_CUSTOM_ID, open_wallet)
        bind_layout(self, NOTIFY_CUSTOM_ID, toggle_notify)


class ShopPanelView(discord.ui.LayoutView):
    """The channel shop panel: each section's blurb with a **Browse**
    beside it, then **Your Rentals → Manage**, **Help** beside the title.

    Carries no per-message state, so it's a static-custom_id view (like the
    economy panel's own buttons) re-registered in ``cog_load`` rather than a
    DynamicItem. Each button opens the clicker's own copy of that one
    section as an ephemeral reply. ``sections=None`` is the registration
    instance: it lists every shelf, so whichever subset a posted panel
    shows, each id is routed — including a panel from the split by section,
    whose Browse per shelf carried the same ids.
    """

    def __init__(
        self,
        sections: list[str] | None = None,
        *,
        art: PanelArt | None = None,
    ) -> None:
        super().__init__(timeout=None)
        art = art or PanelArt(banner=None, footer=None)
        if sections is None:
            sections = list(_ALL_SHELVES)
        for item in build_shop_panel_layout(
            sections,
            banner_url=art.banner,
            footer_url=art.footer,
            title_in_banner=art.title_in_banner,
        ):
            self.add_item(item)
        _bind_section_buttons(self, sections)


class _LegacyShopOpenView(discord.ui.LayoutView):
    """Routes the single **Open Shop** / **Browse** (``econ_shop_open``).

    Never posted — registered in ``cog_load`` only. The pre-split panel
    carried it, and so did the stock list's foot row for a day
    (2026-09-26); either keeps answering until its sticky repaints it. It
    opens the section chooser.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.ActionRow(discord.ui.Button(
            label="🛍️ Browse", custom_id=SHOP_PANEL_OPEN_CUSTOM_ID,
        )))
        bind_layout(self, SHOP_PANEL_OPEN_CUSTOM_ID, _open_shop_from_panel)


class _ShopChooserView(_MemberScopedLayoutView):
    """`/bank shop` (and a pre-split panel's buttons): the panel's section
    rows on the member's own ephemeral card. **Paradigm 1's rows, bare.**

    Its buttons share the channel panel's ids and are bound here too, so the
    card answers from its own view rather than leaning on the ``cog_load``
    registration.
    """

    def __init__(self, user_id: int, sections: list[str]) -> None:
        super().__init__(user_id)
        for item in build_shop_chooser_layout(sections):
            self.add_item(item)
        _bind_section_buttons(self, sections)


class ShopRentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_shop_panel:(?P<perk>[a-z_]+)"),
):
    """Legacy per-perk panel button; now a launcher for the personal shop.

    Panels posted before the single Open Shop button carried one of these
    per perk. They stay registered so stale panels keep working across
    restarts, but every click now opens the clicker's personal shop menu
    (where renting that perk is one more click) instead of renting directly
    — one menu, no drift. A `/bank post-shop` refresh replaces them.
    """

    def __init__(
        self,
        perk: str,
        *,
        label: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or f"Rent {PERK_LABELS.get(perk, perk)}",
                style=style,
                disabled=disabled,
                custom_id=f"econ_shop_panel:{perk}",
            )
        )
        self.perk = perk

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ShopRentButton:
        return cls(str(match["perk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open_shop_from_panel(interaction)
