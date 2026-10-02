"""Server Features — one hub for a member's own tools (bug-list row 259).

The public panel lists eight areas in four groups; each button answers with
an ephemeral card for the member who pressed it. Six of the eight hand off
to the flow that already owns the feature — the ticket modal, the Ask modal,
DM Settings, the Take a Break panel, the ``/info`` card, the My Data panel —
the way ``/info``'s buttons do (``member_info.views``: "reuse is the safety
property"). The two
cards built here are the ones no feature had: **Roles & Pings**
(``server_features.logic``/``service``) and **No Contact**, which replaced the
``/nocontact`` commands.

No slash command: an admin posts the hub from the dashboard's Server Features
page (``panel_registry``), and nothing happens until they do. The hub view is
registered once at load, so every posted copy keeps working across restarts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.ext import commands

from bot_modules.core.db_utils import open_db
from bot_modules.core.layout import ExpiringLayoutView, bind, disable_all
from bot_modules.core.sticky import PanelContent
from bot_modules.server_features import logic, service
from bot_modules.server_features.layouts import (
    SERVER_FEATURES_PANEL_TITLE,
    ADD_ID,
    ASK_CUSTOM_ID,
    ASK_TO_DM_CUSTOM_ID,
    BREAK_CUSTOM_ID,
    CONFIRM_CANCEL_ID,
    CONFIRM_YES_ID,
    DMS_CUSTOM_ID,
    DONE_ID,
    INFO_CUSTOM_ID,
    MENU_SELECT_ID_BASE,
    MY_DATA_CUSTOM_ID,
    NEXT_PAGE_ID,
    NO_CONTACT_CUSTOM_ID,
    PING_BUTTON_ID_BASE,
    PREV_PAGE_ID,
    REMOVE_BUTTON_ID_BASE,
    REPLY_DMS_BUTTON_ID,
    ROLES_CUSTOM_ID,
    TICKET_CUSTOM_ID,
    build_hub_layout,
    build_no_contact_answer_layout,
    build_no_contact_layout,
    build_remove_confirm_layout,
    build_roles_layout,
    list_page,
)
from bot_modules.no_contact import service as no_contact_service
from bot_modules.core.branding_service import (
    PanelArt,
    resolve_assistant_name_conn,
    resolve_panel_art_conn,
)
from bot_modules.shared.name_resolver import NameFn, build_name_fn
from bot_modules.panels.title_service import with_title
from bot_modules.no_contact.logic import (
    ADDED_TEXT,
    REMOVED_TEXT,
    OwnEntry,
    add_refusal,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.server_features")

CARD_TIMEOUT_SECONDS = 600
CONFIRM_TIMEOUT_SECONDS = 120

_UNAVAILABLE = "❌ That isn't available right now — try again in a minute."
_SERVER_ONLY = "❌ Use this in the server."


def _card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """A control-free view for one send: a receipt with no buttons. Never
    give it a button carrying a persistent id — the message's own view is
    consulted before the persistent ones, so the press would land on this
    view's no-op callback (``open_break``)."""
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


async def _say(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _bind_id(
    view: discord.ui.LayoutView,
    item_id: int,
    callback: Callable[[discord.Interaction], Awaitable[None]],
) -> None:
    item = view.find_item(item_id)
    if item is not None:
        item.callback = callback  # type: ignore[attr-defined]


async def _close(view: ExpiringLayoutView, interaction: discord.Interaction) -> None:
    """Done: grey the card out rather than leave live controls."""
    disable_all(view)
    await interaction.response.edit_message(view=view)
    view.stop()


# ═════════════════════════════════════════════════════════════════════
# The hub
# ═════════════════════════════════════════════════════════════════════


class ServerFeaturesHubView(discord.ui.LayoutView):
    """The public panel. **Paradigm 1 — hub panel**, persistent.

    Each handler finds the owning cog with ``bot.get_cog`` at press time and
    answers "not available" when it isn't loaded, rather than binding to a
    cog object at construction that a reload could replace.
    """

    def __init__(
        self,
        cog: ServerFeaturesCog,
        *,
        assistant_name: str = "Poppy",
        art: Optional[PanelArt] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        art = art or PanelArt(banner=None, footer=None)
        for item in build_hub_layout(
            assistant_name=assistant_name,
            banner_url=art.banner,
            footer_url=art.footer,
            title_in_banner=art.title_in_banner,
        ):
            self.add_item(item)
        bind(self, TICKET_CUSTOM_ID, self.open_ticket)
        bind(self, ASK_CUSTOM_ID, self.open_ask)
        bind(self, ROLES_CUSTOM_ID, self.open_roles)
        bind(self, ASK_TO_DM_CUSTOM_ID, self.open_ask_to_dm)
        bind(self, DMS_CUSTOM_ID, self.open_dms)
        bind(self, BREAK_CUSTOM_ID, self.open_break)
        bind(self, NO_CONTACT_CUSTOM_ID, self.open_no_contact)
        bind(self, MY_DATA_CUSTOM_ID, self.open_my_data)
        bind(self, INFO_CUSTOM_ID, self.open_info)

    @property
    def bot(self) -> Bot:
        return self.cog.bot

    async def open_ticket(self, interaction: discord.Interaction) -> None:
        # The ticket modal is the whole flow — it reads the ticket config and
        # answers "not set up" itself on submit, exactly as the ticket panel's
        # own button does.
        if self.bot.get_cog("JailCog") is None:
            await _say(interaction, _UNAVAILABLE)
            return
        from bot_modules.moderation.jail.jail_commands import _TicketOpenModal  # noqa: PLC0415

        await interaction.response.send_modal(_TicketOpenModal())

    async def open_ask(self, interaction: discord.Interaction) -> None:
        # The Ask panel's two steps, verbatim: the shared cooldown and daily
        # cap (a billed model call per ask), then the question modal.
        if self.bot.get_cog("AdvisorCog") is None:
            await _say(interaction, _UNAVAILABLE)
            return
        from bot_modules.advisor.cog import (  # noqa: PLC0415
            _AskModal,
            _ask_blocked,
        )

        if await _ask_blocked(interaction, interaction.user.id):
            return
        await interaction.response.send_modal(_AskModal(history=None))

    async def open_roles(self, interaction: discord.Interaction) -> None:
        await self.cog.open_roles(interaction)

    async def open_ask_to_dm(self, interaction: discord.Interaction) -> None:
        # The DM Requests panel's Open Form button: the one-step request
        # modal, which runs every pre-check itself at submit.
        cog = self.bot.get_cog("DmPermsCog")
        if not isinstance(interaction.user, discord.Member):
            await _say(interaction, _SERVER_ONLY)
            return
        if cog is None:
            await _say(interaction, _UNAVAILABLE)
            return
        from bot_modules.dm.perms.cog import DmRequestModal  # noqa: PLC0415

        await interaction.response.send_modal(DmRequestModal(cog))  # type: ignore[arg-type]

    async def open_dms(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("DmPermsCog")
        member = interaction.user
        if not isinstance(member, discord.Member):
            await _say(interaction, _SERVER_ONLY)
            return
        if cog is None:
            await _say(interaction, _UNAVAILABLE)
            return
        from bot_modules.dm.perms.cog import open_dm_settings  # noqa: PLC0415

        await open_dm_settings(cog, interaction, member)  # type: ignore[arg-type]

    async def open_break(self, interaction: discord.Interaction) -> None:
        # The Take a Break panel itself, ephemerally, as BreaksCog's own bound
        # view so this row adds a confirm step and no break code. It must be
        # the bound view and not a control-free card: discord.py answers a
        # press from the view stored for the pressed message before it looks
        # at the persistent views, so a card whose Start button carried the
        # persistent id with no callback swallowed the press silently (a
        # member saw "didn't respond in time" on 2026-09-30).
        breaks = self.bot.get_cog("BreaksCog")
        if breaks is None:
            await _say(interaction, _UNAVAILABLE)
            return
        from bot_modules.breaks.cog import BreakPanelView  # noqa: PLC0415

        await interaction.response.send_message(
            view=BreakPanelView(breaks), ephemeral=True  # type: ignore[arg-type]
        )

    async def open_no_contact(self, interaction: discord.Interaction) -> None:
        await self.cog.open_no_contact(interaction)

    async def open_my_data(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("PrivacyCog")
        if cog is None:
            await _say(interaction, _UNAVAILABLE)
            return
        await cog.open_panel(interaction)  # type: ignore[attr-defined]

    async def open_info(self, interaction: discord.Interaction) -> None:
        # The /info card itself, through the same entry point the command
        # uses — one card, two doors.
        cog = self.bot.get_cog("MemberInfoCog")
        if cog is None:
            await _say(interaction, _UNAVAILABLE)
            return
        await cog.open_panel(interaction)  # type: ignore[attr-defined]


# ═════════════════════════════════════════════════════════════════════
# Roles & Pings
# ═════════════════════════════════════════════════════════════════════


class RolesCardView(ExpiringLayoutView):
    """The member's own roles card. **Paradigm 2 — settings panel.**

    Every press re-reads the card before acting (``service.read_card``), so
    it is judged against the server as it is now — an option an admin took
    out of Customize, or a jail a moderator applied, since the card opened.
    """

    def __init__(self, cog: ServerFeaturesCog, member: discord.Member, read: service.CardRead) -> None:
        super().__init__(timeout=CARD_TIMEOUT_SECONDS)
        self.cog = cog
        self.member = member
        self._render(read)

    def _render(self, read: service.CardRead, *, note: str = "") -> None:
        self.clear_items()
        self._card = read.card
        for item in build_roles_layout(read.card, held=read.held, note=note):
            self.add_item(item)
        _bind_id(self, DONE_ID, self._on_done)
        for index, row in enumerate(read.card.pings):
            _bind_id(self, PING_BUTTON_ID_BASE + index, self._ping_callback(row.key))
        for index, menu in enumerate(read.card.menus):
            _bind_id(self, MENU_SELECT_ID_BASE + index, self._menu_callback(index, menu.prompt_id))
        _bind_id(self, REPLY_DMS_BUTTON_ID, self._on_reply_dms)

    async def _on_done(self, interaction: discord.Interaction) -> None:
        await _close(self, interaction)

    def _ping_callback(self, key: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            await self._change(interaction, lambda card: logic.ping_change(card, key))

        return _callback

    def _menu_callback(
        self, index: int, prompt_id: int
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            select = self.find_item(MENU_SELECT_ID_BASE + index)
            values = [str(v) for v in (getattr(select, "values", None) or [])]

            def plan(card: logic.RoleCard) -> tuple[set[int], set[int]] | None:
                menu = next((m for m in card.menus if m.prompt_id == prompt_id), None)
                if menu is None:
                    return None
                return logic.plan_menu_change(
                    menu, logic.chosen_indices(menu, values), self._role_ids()
                )

            await self._change(interaction, plan)

        return _callback

    async def _on_reply_dms(self, interaction: discord.Interaction) -> None:
        # Not a role: a row in needle_reply_optins. Judged against a fresh
        # card like every other press, so a hold or a dial switched off since
        # the card opened is honoured.
        if isinstance(interaction.user, discord.Member):
            self.member = interaction.user
        await interaction.response.defer()
        ctx = self.cog.bot.ctx
        read = await service.read_card(ctx, self.member)
        want = None if read.held else logic.reply_dms_change(read.card)
        if want is None:
            note = "" if read.held else "That option isn't offered any more — the card has been refreshed."
            self._render(read, note=note)
            await interaction.edit_original_response(view=self)
            return
        await service.set_reply_dms(ctx, self.member.guild.id, self.member.id, on=want)
        self._render(await service.read_card(ctx, self.member), note="Saved.")
        await interaction.edit_original_response(view=self)

    def _role_ids(self) -> set[int]:
        return {r.id for r in self.member.roles}

    async def _change(
        self,
        interaction: discord.Interaction,
        plan: Callable[[logic.RoleCard], tuple[set[int], set[int]] | None],
    ) -> None:
        # The interaction's member is the gateway's freshest copy; the one
        # captured at open predates any change made on this card.
        if isinstance(interaction.user, discord.Member):
            self.member = interaction.user
        # Up to two REST calls plus an onboarding read: defer first.
        await interaction.response.defer()
        read = await service.read_card(self.cog.bot.ctx, self.member)
        if read.held:
            self._render(read)
            await interaction.edit_original_response(view=self)
            return
        change = plan(read.card)
        if change is None:
            self._render(read, note="That option isn't offered any more — the card has been refreshed.")
            await interaction.edit_original_response(view=self)
            return
        add, remove = change
        note = "Saved."
        roles = self._role_ids()
        if add or remove:
            add_ok, remove_ok = await service.apply_change(self.member, add, remove)
            # Applied independently: a remove Discord refuses must not erase
            # the record of an add that landed, or the redraw would claim
            # nothing happened when half the change did.
            roles = logic.with_change(
                roles, add if add_ok else set(), remove if remove_ok else set()
            )
            if not (add_ok and remove_ok):
                note = "❌ Discord wouldn't make that change — ask a moderator."
        read = await service.read_card(self.cog.bot.ctx, self.member, role_ids=roles)
        self._render(read, note=note)
        await interaction.edit_original_response(view=self)


# ═════════════════════════════════════════════════════════════════════
# No Contact
# ═════════════════════════════════════════════════════════════════════


class NoContactAddModal(discord.ui.Modal, title="Add to Your No-Contact List"):
    """Who, and an optional reason. **Paradigm 4 — request form.**"""

    def __init__(self, card: NoContactCardView) -> None:
        super().__init__()
        self.card = card
        self.who: discord.ui.UserSelect[Any] = discord.ui.UserSelect(
            placeholder="Pick a member…", min_values=1, max_values=1, required=True
        )
        self.add_item(discord.ui.Label(text="Who?", component=self.who))
        self.reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
            style=discord.TextStyle.short, required=False, max_length=300
        )
        self.add_item(
            discord.ui.Label(
                text="Reason (optional)",
                description="Only moderators see this — never the person you add.",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = list(self.who.values)
        guild = interaction.guild
        if not values or guild is None:
            await interaction.response.send_message(
                view=_card(build_no_contact_answer_layout("❌ Pick someone to add.")),
                ephemeral=True,
            )
            return
        target = values[0]
        refusal = add_refusal(
            actor_id=interaction.user.id, target_id=target.id, target_is_bot=target.bot
        )
        if refusal is not None:
            await interaction.response.send_message(
                view=_card(build_no_contact_answer_layout(refusal)), ephemeral=True
            )
            return
        await asyncio.to_thread(
            no_contact_service.member_add,
            self.card.cog.bot.ctx.db_path,
            guild.id,
            interaction.user.id,
            target.id,
            reason=str(self.reason.value or ""),
        )
        name_fn = await _names(self.card.cog.bot, guild, [target.id])
        name = discord.utils.escape_markdown(name_fn(target.id))
        # The same words whether or not an entry already existed — see
        # ``ADDED_TEXT``.
        await interaction.response.send_message(
            view=_card(build_no_contact_answer_layout(ADDED_TEXT.format(name=name))),
            ephemeral=True,
        )
        await self.card.reload(note=f"Added {name}.")


class NoContactCardView(ExpiringLayoutView):
    """The member's own no-contact list. **Paradigm 2 + 6**, paged at ten."""

    def __init__(
        self,
        cog: ServerFeaturesCog,
        member: discord.Member,
        entries: Sequence[OwnEntry],
        name_fn: NameFn,
    ) -> None:
        super().__init__(timeout=CARD_TIMEOUT_SECONDS)
        self.cog = cog
        self.member = member
        self._entries = list(entries)
        self.name_fn = name_fn
        self._page = 0
        self._note = ""
        self._render()

    def _render(self) -> None:
        self.clear_items()
        self._page, _count, rows = list_page(self._entries, self._page)
        for item in build_no_contact_layout(
            self._entries, name_fn=self.name_fn, page=self._page, note=self._note
        ):
            self.add_item(item)
        _bind_id(self, DONE_ID, self._on_done)
        _bind_id(self, ADD_ID, self._on_add)
        _bind_id(self, PREV_PAGE_ID, self._on_prev)
        _bind_id(self, NEXT_PAGE_ID, self._on_next)
        for index, entry in enumerate(rows):
            if entry.removable:
                _bind_id(self, REMOVE_BUTTON_ID_BASE + index, self._remove_callback(entry))

    def _remove_callback(self, entry: OwnEntry) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            confirm = NoContactRemoveConfirmView(self, entry.other_id)
            await interaction.response.send_message(view=confirm, ephemeral=True)
            confirm.message = await interaction.original_response()

        return _callback

    async def _on_done(self, interaction: discord.Interaction) -> None:
        await _close(self, interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        self._page += step
        self._note = ""
        self._render()
        await interaction.response.edit_message(view=self)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NoContactAddModal(self))

    async def reload(self, *, note: str) -> None:
        """Re-read the list and redraw the stored message — called from the
        modal's or the confirm card's interaction, not this card's."""
        self._entries, self.name_fn = await _load_entries(self.cog.bot, self.member)
        self._note = note
        self._render()
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.debug("server features: no-contact card could not be redrawn")


class NoContactRemoveConfirmView(ExpiringLayoutView):
    """"Yes, Remove" / Cancel in front of one entry. **Paradigm 10.**"""

    def __init__(self, card: NoContactCardView, other_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.card = card
        self.other_id = other_id
        for item in build_remove_confirm_layout(user_id=other_id, name_fn=card.name_fn):
            self.add_item(item)
        _bind_id(self, CONFIRM_YES_ID, self._on_yes)
        _bind_id(self, CONFIRM_CANCEL_ID, self._on_cancel)

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        self.stop()
        guild = self.card.member.guild
        refusal = await asyncio.to_thread(
            no_contact_service.member_remove,
            self.card.cog.bot.ctx.db_path,
            guild.id,
            self.card.member.id,
            self.other_id,
        )
        name = discord.utils.escape_markdown(self.card.name_fn(self.other_id))
        message = refusal if refusal is not None else REMOVED_TEXT.format(name=name)
        await interaction.response.edit_message(
            view=_card(build_no_contact_answer_layout(message))
        )
        await self.card.reload(note="" if refusal else f"Removed {name}.")

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        """Cancel deletes the confirm; the unchanged card behind it is the answer."""
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


async def _names(bot: Bot, guild: discord.Guild, user_ids: Sequence[int]) -> NameFn:
    return await build_name_fn(
        guild=guild, db_path=bot.ctx.db_path, guild_id=guild.id, user_ids=list(user_ids)
    )


async def _load_entries(bot: Bot, member: discord.Member) -> tuple[list[OwnEntry], NameFn]:
    entries = await asyncio.to_thread(
        no_contact_service.member_entries, bot.ctx.db_path, member.guild.id, member.id
    )
    return entries, await _names(bot, member.guild, [e.other_id for e in entries])


# ═════════════════════════════════════════════════════════════════════
# The cog
# ═════════════════════════════════════════════════════════════════════


class ServerFeaturesCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(ServerFeaturesHubView(self))

    async def open_roles(self, interaction: discord.Interaction) -> None:
        """The Roles & Pings card, as the reply to ``interaction``."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            await _say(interaction, _SERVER_ONLY)
            return
        # Reading Customize is a REST call: defer so a slow one can't lose the
        # interaction.
        await interaction.response.defer(ephemeral=True, thinking=True)
        read = await service.read_card(self.bot.ctx, member)
        view = RolesCardView(self, member, read)
        view.message = await interaction.followup.send(view=view, ephemeral=True, wait=True)

    async def open_no_contact(self, interaction: discord.Interaction) -> None:
        """The No Contact card, as the reply to ``interaction``. Also opened by
        ``/info``'s no-contact button (``member_info.views``)."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            await _say(interaction, _SERVER_ONLY)
            return
        entries, name_fn = await _load_entries(self.bot, member)
        view = NoContactCardView(self, member, entries, name_fn)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    def _hub_settings(self, guild_id: int) -> tuple[str, PanelArt]:
        """The guild's name for its assistant, and its hub banner and footer.
        Blocking — the titled banner is drawn here on first use."""
        with open_db(self.bot.ctx.db_path) as conn:
            name = resolve_assistant_name_conn(conn, guild_id)
            art = resolve_panel_art_conn(conn, guild_id)
        # After the connection closes: drawing a new title reads every frame.
        return name, with_title(art, SERVER_FEATURES_PANEL_TITLE)

    async def build_server_features_panel(self, guild: discord.Guild) -> PanelContent:
        """The hub as it should look now — what a post sends and what the
        panel refresher edits a recorded copy to (``panel_registry.render``)."""
        assistant_name, art = await asyncio.to_thread(self._hub_settings, guild.id)
        return PanelContent(
            layout=ServerFeaturesHubView(self, assistant_name=assistant_name, art=art)
        )

    async def post_server_features_panel(
        self, guild: discord.Guild, channel
    ) -> discord.Message | None:
        """Post the hub into ``channel``. Entry point for the dashboard's panel
        poster (``panel_registry``). The route records the post and moves that
        copy on a second post into the same channel; a copy elsewhere keeps
        working — the buttons carry no state."""
        try:
            content = await self.build_server_features_panel(guild)
            return await channel.send(**content.send_kwargs())
        except discord.HTTPException:
            log.exception("server features: couldn't post the hub in guild %s", guild.id)
            return None


async def setup(bot: Bot) -> None:
    await bot.add_cog(ServerFeaturesCog(bot))
