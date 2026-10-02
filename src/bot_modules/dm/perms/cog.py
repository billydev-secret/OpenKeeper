"""DM permission cog — ported from accord_bot (dm_perms_bot).

Components V2 since 2026-09-20: every card this feature sends is a
``LayoutView`` built by ``bot_modules/dm/perms/layouts.py``, not an embed.
The V2 flag is per message and one-way, so a feature converts whole — a
settings panel next to an embed receipt would look like two different bots
(``docs/plans/components_v2_migration.md`` stage 2).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.ext import commands

from bot_modules.breaks.store import is_on_break
from bot_modules.core.layout import ExpiringLayoutView, card, disable_all
from bot_modules.core.layout import bind as _bind
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.core.utils import safe_ephemeral
from bot_modules.dm.perms.layouts import (
    CONFIRM_CANCEL_ID,
    CONFIRM_REMOVE_ID,
    DM_CONSENT_ACCEPT_CUSTOM_ID,
    DM_CONSENT_DENY_CUSTOM_ID,
    DM_CONSENT_DENY_REPLY_CUSTOM_ID,
    DM_HELP_PANEL_CUSTOM_ID,
    DM_PANEL_TITLE,
    DM_REQUEST_PANEL_CUSTOM_ID,
    DM_SETTINGS_PANEL_CUSTOM_ID,
    DONE_BUTTON_ID,
    MODE_SELECT_ID,
    NEXT_PAGE_ID,
    PREV_PAGE_ID,
    REMOVE_BUTTON_ID_BASE,
    build_acceptance_layout,
    build_audit_notice_layout,
    build_consent_action_row,
    build_denial_layout_for_requester,
    build_denial_layout_for_view,
    build_expired_layout,
    build_guild_unavailable_layout,
    build_help_layout,
    build_panel_layout,
    build_remove_confirm_layout,
    build_remove_done_layout,
    build_remove_missing_layout,
    build_request_dm_layout,
    build_request_sent_layout,
    build_revoked_layout,
    build_settings_layout,
    build_stale_request_layout,
    connection_page,
    request_type_options,
)
from bot_modules.docs.help_card import load_doc_body
from bot_modules.dm.perms.logic import (
    audit_line_accepted,
    audit_line_asked,
    audit_line_denied,
    audit_line_expired,
    audit_line_revoked,
    clamp_reason,
    classify_dm_request,
    discard_consent_pair,
    display_name_for,
    pick_dm_roles_to_remove,
)
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.core.card_art import read_card_art
from bot_modules.panels.title_service import with_title
from bot_modules.shared.dm_branding import (
    brand_dm_layout,
    guild_display_name,
    guild_icon_url,
    send_branded_dm,
)
from bot_modules.dm.perms.service import (
    add_consent_pair,
    count_pending_for_requester,
    expire_stale_pending_requests,
    get_consent_pair_meta,
    HELP_DOC_KEY,
    get_request_limits,
    init_db,
    is_dm_mode_role,
    load_audit_channels,
    load_consent_pairs,
    load_dm_mode_roles,
    load_member_connections,
    load_panel_settings,
    load_request_by_message_id,
    load_requests,
    normalize_request_type,
    remove_consent_pair,
    remove_request,
    request_expiry_label,
    request_type_label,
    resolve_mode,
    set_member_dm_mode,
    set_panel_settings,
    upsert_request,
    write_audit_log,
)
from bot_modules.shared.name_resolver import NameFn, build_name_fn
from bot_modules.no_contact.service import is_no_contact

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

# How long a request waits and how many a member may have open are per-guild
# dials on the DM Permissions panel (dm_perms_service.get_request_limits).
EXPIRY_SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly
MAX_REASON_LENGTH = 250  # the request modal's own cap on the reason field
SETTINGS_TIMEOUT_SECONDS = 300
CONFIRM_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

#: ``core.layout.card``. DM branding mutates the card it is handed, so every
#: surface still gets its own build.
_card = card


async def _names(
    bot: Bot, guild: Optional[discord.Guild], *user_ids: int
) -> NameFn:
    """A resolver for the members a card is about.

    A ``<@id>`` inside a card renders from the *reading* client's cache, so
    it degrades to a bare number for anyone who hasn't seen that member —
    and these cards are read in DMs, where that is the common case.
    """
    db_path = bot.ctx.db_path
    guild_id = guild.id if guild is not None else 0
    return await build_name_fn(
        guild=guild,
        db_path=db_path,
        guild_id=guild_id,
        user_ids=[uid for uid in user_ids if uid],
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class AskConsentView(discord.ui.LayoutView):
    """The consent DM's decision card — persistent across restarts.

    A single instance is registered with ``bot.add_view()`` at cog load.
    All Accept/Decline clicks across the bot route to this instance, which
    looks up the underlying request from the DB by ``interaction.message.id``.
    This keeps in-flight requests usable across bot restarts, so the
    registered instance carries only the button row: its card is never sent,
    and inventing placeholder request data for it would be a card that could
    one day be posted by mistake.
    """

    def __init__(
        self, cog: DmPermsCog, *, card: Optional[Sequence[discord.ui.Item[Any]]] = None
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        for item in card if card is not None else [build_consent_action_row()]:
            self.add_item(item)
        _bind(self, DM_CONSENT_ACCEPT_CUSTOM_ID, self._on_accept)
        _bind(self, DM_CONSENT_DENY_CUSTOM_ID, self._on_deny)
        _bind(self, DM_CONSENT_DENY_REPLY_CUSTOM_ID, self._on_deny_reply)

    async def _on_accept(self, interaction: discord.Interaction) -> None:
        await self._handle_click(interaction, accepted=True)

    async def _on_deny(self, interaction: discord.Interaction) -> None:
        await self._handle_click(interaction, accepted=False)

    async def _on_deny_reply(self, interaction: discord.Interaction) -> None:
        """Validate the click, then open a modal for the decliner's note.

        Resolution/validation here must not consume the interaction response
        (``send_modal`` needs an un-responded interaction), so it only does DB
        reads before either erroring out or sending the modal.
        """
        message = interaction.message
        if message is None:
            await interaction.response.send_message(
                "❌ Couldn't find the request for this button.", ephemeral=True
            )
            return

        record = load_request_by_message_id(self.cog.bot.ctx.db_path, message.id)
        if record is None:
            await _replace_card(interaction, build_stale_request_layout())
            return

        if interaction.user.id != record["target_id"]:
            await interaction.response.send_message(
                "❌ This request isn't for you.", ephemeral=True
            )
            return

        await interaction.response.send_modal(DmDenyReplyModal(self, message))

    async def _handle_click(
        self, interaction: discord.Interaction, *, accepted: bool
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.response.send_message(
                "❌ Couldn't find the request for this button.", ephemeral=True
            )
            return

        record = load_request_by_message_id(self.cog.bot.ctx.db_path, message.id)
        if record is None:
            await _replace_card(interaction, build_stale_request_layout())
            return

        target_id = record["target_id"]
        requester_id = record["requester_id"]
        guild_id = record["guild_id"]
        req_type = record["request_type"]
        reason = record["reason"]

        if interaction.user.id != target_id:
            await interaction.response.send_message(
                "❌ This request isn't for you.", ephemeral=True
            )
            return

        guild = self.cog.bot.get_guild(guild_id)
        if guild is None:
            # Bot was removed from the guild after the request was sent.
            self.cog._drop_request_from_memory(guild_id, requester_id, target_id)
            remove_request(self.cog.bot.ctx.db_path, guild_id, requester_id, target_id)
            await _replace_card(interaction, build_guild_unavailable_layout())
            return

        requester = guild.get_member(requester_id)
        target = guild.get_member(target_id)

        if accepted:
            await self._handle_accept(
                interaction,
                guild=guild,
                requester=requester,
                target=target,
                requester_id=requester_id,
                target_id=target_id,
                req_type=req_type,
                reason=reason,
                source_msg_id=message.id,
                source_channel_id=getattr(message.channel, "id", None),
            )
        else:
            await self._handle_deny(
                interaction,
                guild=guild,
                requester=requester,
                target=target,
                requester_id=requester_id,
                target_id=target_id,
                req_type=req_type,
                reason=reason,
            )

    async def _handle_accept(
        self,
        interaction: discord.Interaction,
        *,
        guild: discord.Guild,
        requester: Optional[discord.Member],
        target: Optional[discord.Member],
        requester_id: int,
        target_id: int,
        req_type: str,
        reason: str,
        source_msg_id: Optional[int],
        source_channel_id: Optional[int],
    ) -> None:
        if requester is None or target is None:
            await interaction.response.send_message(
                "❌ Couldn't find one or both users in this server.", ephemeral=True
            )
            return

        self.cog.consent_pairs.setdefault(guild.id, set())
        self.cog.consent_pairs[guild.id].add((requester_id, target_id))
        self.cog.consent_pairs[guild.id].add((target_id, requester_id))

        add_consent_pair(
            self.cog.bot.ctx.db_path, guild.id, requester_id, target_id,
            rel_type=req_type, reason=reason,
            source_msg_id=source_msg_id, source_channel_id=source_channel_id,
        )
        self.cog._drop_request_from_memory(guild.id, requester_id, target_id)
        remove_request(self.cog.bot.ctx.db_path, guild.id, requester_id, target_id)

        type_label = request_type_label(req_type)
        name_fn = await _names(self.cog.bot, guild, requester_id, target_id)

        def accepted_card(viewer_id: int) -> discord.ui.LayoutView:
            # One build per surface: a view belongs to one message, DM
            # branding mutates the card it stamps, and the card names the
            # *other* party — so each recipient gets their own.
            return _card(build_acceptance_layout(
                viewer_id=viewer_id,
                requester_id=requester_id,
                target_id=target_id,
                request_type=req_type,
                name_fn=name_fn,
            ))

        # The decision card being replaced sits in the target's DMs.
        await _replace_card_view(interaction, accepted_card(target_id), guild=guild)
        await _dm(
            requester, self.cog.bot.ctx.db_path, guild,
            layout=accepted_card(requester_id),
        )
        # Their own Accept's receipt: through a break (dm_branding).
        await _dm(
            target, self.cog.bot.ctx.db_path, guild, layout=accepted_card(target_id),
            bypass_break=True,
        )

        write_audit_log(
            self.cog.bot.ctx.db_path, guild.id, "request_accepted",
            actor_id=target_id, user_a_id=requester_id, user_b_id=target_id,
            notes=f"type={req_type}",
        )
        await self.cog._post_audit(
            guild,
            audit_line_accepted(requester.display_name, target.display_name, type_label),
        )

    async def _handle_deny(
        self,
        interaction: discord.Interaction,
        *,
        guild: discord.Guild,
        requester: Optional[discord.Member],
        target: Optional[discord.Member],
        requester_id: int,
        target_id: int,
        req_type: str,
        reason: str,
    ) -> None:
        name_fn = await _names(self.cog.bot, guild, requester_id)

        # The card edit goes first, but it never raises (``_replace_card``
        # swallows a vanished or unreachable message), so the decline below is
        # recorded whatever became of the card: the press *is* the member's
        # answer, and a card that could not be repainted is a stale one — the
        # next press on it gets the No Longer Active card. The Decline + Note
        # modal is the path that bails on a failed edit, because there the
        # member has typed something they would otherwise lose.
        await _replace_card(
            interaction,
            build_denial_layout_for_view(
                requester_id=requester_id, request_type=req_type, name_fn=name_fn
            ),
            guild=guild,
        )

        await self._finalize_deny(
            guild=guild,
            requester=requester,
            target=target,
            requester_id=requester_id,
            target_id=target_id,
            req_type=req_type,
            reason=reason,
        )

    async def _finalize_deny(
        self,
        *,
        guild: discord.Guild,
        requester: Optional[discord.Member],
        target: Optional[discord.Member],
        requester_id: int,
        target_id: int,
        req_type: str,
        reason: str,
        reply: str = "",
    ) -> None:
        """Drop the request, notify the requester, and audit the denial.

        Shared by the plain Decline button and the "Decline + Note" modal;
        the caller is responsible for having already updated the target's own
        DM message. ``reply`` is the decliner's optional note.
        """
        type_label = request_type_label(req_type)

        self.cog._drop_request_from_memory(guild.id, requester_id, target_id)
        remove_request(self.cog.bot.ctx.db_path, guild.id, requester_id, target_id)

        if requester:
            name_fn = await _names(self.cog.bot, guild, target_id)
            await _dm(
                requester,
                self.cog.bot.ctx.db_path,
                guild,
                layout=_card(build_denial_layout_for_requester(
                    target_id=target_id,
                    guild_name=guild.name,
                    request_type=req_type,
                    reply=reply,
                    name_fn=name_fn,
                )),
            )

        write_audit_log(
            self.cog.bot.ctx.db_path, guild.id, "request_denied",
            actor_id=target_id, user_a_id=requester_id, user_b_id=target_id,
            notes=f"type={req_type}" + ("; replied" if reply else ""),
        )
        requester_name = display_name_for(requester, requester_id)
        target_name = display_name_for(target, target_id)
        await self.cog._post_audit(
            guild,
            audit_line_denied(requester_name, target_name, type_label),
        )


class DmRequestModal(discord.ui.Modal, title="DM Request"):
    """The whole request, in one form. **Paradigm 4 — request form.**

    Was pick-a-person → pick-a-type → a reason modal → submit, across two
    messages and four presses. None of those steps depended on the answer to
    the one before it, which is exactly the flow the guide collapses into a
    single modal (§ Controls): a user picker, a radio for DM-or-friend with a
    line on each, an optional reason, Send. Every pre-check now runs at
    submit and answers with the same ❌ line the stepped flow gave earlier.
    """

    def __init__(self, cog: DmPermsCog) -> None:
        super().__init__()
        self.cog = cog
        self.who: discord.ui.UserSelect[Any] = discord.ui.UserSelect(
            placeholder="Pick a member…", min_values=1, max_values=1, required=True
        )
        self.add_item(discord.ui.Label(text="Who?", component=self.who))
        self.kind = discord.ui.RadioGroup(
            required=True, options=request_type_options()
        )
        self.add_item(
            discord.ui.Label(text="Request Type", component=self.kind)
        )
        self.reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=False,
            max_length=MAX_REASON_LENGTH,
            placeholder="Why you'd like to connect…",
        )
        self.add_item(
            discord.ui.Label(
                text="Reason (optional)",
                description="They see this, so keep it friendly.",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = list(self.who.values)
        if not values:
            await interaction.response.send_message(
                "❌ Pick someone to send the request to.", ephemeral=True
            )
            return
        target: discord.Member | discord.User = values[0]
        # A modal's resolved user may arrive as a plain User; the mode roles
        # the pre-checks read only exist on the Member, so prefer the guild's
        # own copy when it has one.
        if interaction.guild is not None and not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id) or target
        await self.cog._submit_dm_request(
            interaction,
            target,
            self.kind.value or "dm",
            str(self.reason.value or "").strip(),
        )


class DmDenyReplyModal(discord.ui.Modal, title="Decline + Note"):
    """Lets the target decline a request while sending a short note back.

    **Paradigm 4 — request form**, opened mid-decision: one field, submitted
    at once, from the decision card's third button rather than from a hub.
    The modal carries the button's own name, so the press and the form it
    opens read as one thing.

    Carries the source DM message so the bot can edit it directly on submit
    (a version-independent alternative to editing via the modal-submit
    interaction), and re-resolves the request by that message id so a
    request answered in the meantime resolves as stale.
    """

    reply: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Your note",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=MAX_REASON_LENGTH,
        placeholder="They'll see this with the decline…",
    )

    def __init__(self, view: AskConsentView, message: discord.Message) -> None:
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reply = str(self.reply.value or "").strip()
        view = self._view

        record = load_request_by_message_id(view.cog.bot.ctx.db_path, self._message.id)
        if record is None:
            await interaction.response.send_message(
                "❌ That request is no longer pending.", ephemeral=True
            )
            return

        guild = view.cog.bot.get_guild(record["guild_id"])
        if guild is None:
            await interaction.response.send_message(
                "❌ That server is no longer available.", ephemeral=True
            )
            return

        requester_id = record["requester_id"]
        target_id = record["target_id"]
        req_type = record["request_type"]
        reason = record["reason"]
        requester = guild.get_member(requester_id)
        target = guild.get_member(target_id)

        name_fn = await _names(view.cog.bot, guild, requester_id)
        declined = _brand(
            _card(build_denial_layout_for_view(
                requester_id=requester_id,
                request_type=req_type,
                reply=reply,
                name_fn=name_fn,
            )),
            guild,
        )
        # Edit the bot's own DM first: on failure, bail before the DB delete so
        # the request stays pending and the note the member typed is not lost.
        # (The plain Decline button does not bail — see ``_handle_deny``.)
        try:
            await self._message.edit(
                content=None, embed=None, attachments=[], view=declined
            )
        except discord.HTTPException:
            log.warning("dm_perms: failed to edit source DM on deny-with-reply")
            await interaction.response.send_message(
                "❌ Something went wrong updating the request — nothing was changed, "
                "please try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Declined — your reply was sent to them.", ephemeral=True
        )

        await view._finalize_deny(
            guild=guild,
            requester=requester,
            target=target,
            requester_id=requester_id,
            target_id=target_id,
            req_type=req_type,
            reason=reason,
            reply=reply,
        )


class DmSettingsView(ExpiringLayoutView):
    """The member's own DM panel. **Paradigm 2 — settings panel.**

    Replaced ``/dm_help``, ``/dm_set_mode``, ``/dm_status`` and ``/dm_revoke``
    (2026-07-28): four top-level commands for one feature, which CLAUDE.md's
    "prefer one ephemeral panel over a sprawl of subcommands" rule calls out
    directly. Reached from the "Settings" button on the public request panel.

    Ephemeral and short-lived, so unlike ``DmRequestPanelView`` it holds its
    state in memory rather than recovering it from a custom_id. ``_mode`` is
    that state and is never re-read from roles while the panel is open:
    ``set_member_dm_mode`` goes out over REST and discord.py does not write
    the result back into the cached member, so a re-read would keep returning
    the *old* mode until a MEMBER_UPDATE arrives over the gateway — the panel
    would contradict its own "changed to…" line.
    """

    def __init__(
        self,
        cog: DmPermsCog,
        member: discord.Member,
        *,
        mode: str,
        connections: Sequence[dict[str, Any]],
        name_fn: NameFn,
    ) -> None:
        super().__init__(timeout=SETTINGS_TIMEOUT_SECONDS)
        self.cog = cog
        self.member = member
        self._mode = mode
        self._connections = list(connections)
        self._name_fn = name_fn
        self._changed = False
        self._note = ""
        self._page = 0
        self._render()

    # ── Rendering ────────────────────────────────────────────────────────

    def _render(self) -> None:
        """Rebuild the card and re-attach the callbacks the builder omits."""
        self.clear_items()
        # The same clamp the builder applies, so a page whose last row was
        # just removed binds its buttons to the rows the card actually shows.
        self._page, _page_count, rows = connection_page(self._connections, self._page)
        for item in build_settings_layout(
            mode=self._mode,
            connections=self._connections,
            name_fn=self._name_fn,
            changed=self._changed,
            note=self._note,
            page=self._page,
        ):
            self.add_item(item)

        select = self.find_item(MODE_SELECT_ID)
        if select is not None:
            select.callback = self._on_mode  # type: ignore[method-assign]
        done = self.find_item(DONE_BUTTON_ID)
        if done is not None:
            done.callback = self._on_done  # type: ignore[method-assign]
        for item_id, turn in ((PREV_PAGE_ID, self._on_prev), (NEXT_PAGE_ID, self._on_next)):
            arrow = self.find_item(item_id)
            if arrow is not None:
                arrow.callback = turn  # type: ignore[method-assign]
        for index, connection in enumerate(rows):
            button = self.find_item(REMOVE_BUTTON_ID_BASE + index)
            if button is not None:
                button.callback = self._remove_callback(connection)  # type: ignore[method-assign]

    def _remove_callback(
        self, connection: dict[str, Any]
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        """Bind one row's Remove to that row's member.

        The row index is the only thing the card carries, so the closure —
        not a custom_id holding a user id — is what keeps a press pointed at
        the member whose name is beside it.
        """
        async def _callback(interaction: discord.Interaction) -> None:
            await self._open_remove_confirm(interaction, connection)

        return _callback

    async def _redraw(self, interaction: discord.Interaction) -> None:
        """Redraw in place. Falls back to editing the original response when
        the interaction has already been deferred or answered."""
        self._render()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
            return
        await interaction.response.edit_message(view=self)

    async def reload(self, *, note: str) -> None:
        """Re-read the connections and redraw the stored message.

        Called after a removal, which happens on a *different* interaction
        (the confirm card's), so there is no response left to edit here.
        """
        self._connections = await asyncio.to_thread(
            load_member_connections,
            self.cog.bot.ctx.db_path,
            self.member.guild.id,
            self.member.id,
        )
        self._changed = False
        self._note = note
        self._render()
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.debug("dm_perms: settings panel could not be redrawn")

    # ── The mode dial ────────────────────────────────────────────────────

    async def _on_mode(self, interaction: discord.Interaction) -> None:
        select = self.find_item(MODE_SELECT_ID)
        values = list(getattr(select, "values", []) or [])
        mode = str(values[0]) if values else self._mode
        # Re-seat the cached member on every press. ``self.member`` is captured
        # when the panel opens and discord.py never writes REST results back
        # into it, so a *second* mode change in one sitting computed its
        # removals from a role set that predated the first — leaving the old
        # mode's role in place, which then raced the dedup listener and could
        # revert the choice the member had just made. The interaction carries a
        # member resolved by the gateway at click time, which is as fresh as
        # this process can get without a REST fetch per press.
        if isinstance(interaction.user, discord.Member):
            self.member = interaction.user
        # Defer first: set_member_dm_mode is up to five REST calls (three
        # ensure-role, a remove, an add) before the redraw could otherwise
        # answer, and Discord's 3-second window closes under load — the roles
        # would move while the member saw "This interaction failed".
        await interaction.response.defer()
        try:
            await set_member_dm_mode(
                self.member, mode, self.cog._mode_roles_for(self.member.guild.id)
            )
        except discord.Forbidden:
            await safe_ephemeral(
                interaction,
                "❌ I don't have permission to manage roles here.",
                log_label="dm_perms mode",
            )
            return
        self._mode = mode
        # A mode change left no trace anywhere before this: dm_audit_log only
        # held requests, so "did the bot set this, or did something else take
        # it away?" could only be answered by trawling role_events.
        await asyncio.to_thread(
            write_audit_log,
            self.cog.bot.ctx.db_path,
            self.member.guild.id,
            "mode_set",
            actor_id=self.member.id,
            user_a_id=self.member.id,
            notes=f"mode={mode}",
        )
        # The status line itself becomes the answer ("Changed to …") for
        # this one redraw; a removal's note, if one was showing, has been
        # answered by the press that replaced it.
        self._changed = True
        self._note = ""
        await self._redraw(interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._turn_page(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._turn_page(interaction, 1)

    async def _turn_page(self, interaction: discord.Interaction, step: int) -> None:
        """A page turn's answer is the page, so the "Changed to …" status a
        mode change left behind, or a removal's note, ages off here and the
        page line shows."""
        self._page += step
        self._changed = False
        self._note = ""
        await self._redraw(interaction)

    async def _on_done(self, interaction: discord.Interaction) -> None:
        """Close the panel: grey it out rather than leave live controls."""
        disable_all(self)
        await interaction.response.edit_message(view=self)
        self.stop()

    # ── Connections ──────────────────────────────────────────────────────

    async def _open_remove_confirm(
        self, interaction: discord.Interaction, connection: dict[str, Any]
    ) -> None:
        """Gate a Remove behind its own card. **Paradigm 10 — confirm.**"""
        confirm = RemoveConfirmView(self, connection)
        await interaction.response.send_message(view=confirm, ephemeral=True)
        confirm.message = await interaction.original_response()


class RemoveConfirmView(ExpiringLayoutView):
    """The "Yes, Remove" / Cancel gate in front of one connection.

    Nothing destructive is one tap (guide § Controls), and a red button
    beside a row on a phone is one mis-scroll from a tap. Answers on its own
    ephemeral message, then tells the panel behind it to redraw.
    """

    def __init__(self, panel: DmSettingsView, connection: dict[str, Any]) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.panel = panel
        self.connection = connection
        self.user_id = int(connection["user_id"])
        for item in build_remove_confirm_layout(
            user_id=self.user_id,
            request_type=connection.get("type"),
            name_fn=panel._name_fn,
        ):
            self.add_item(item)
        remove = self.find_item(CONFIRM_REMOVE_ID)
        if remove is not None:
            remove.callback = self._on_yes  # type: ignore[method-assign]
        cancel = self.find_item(CONFIRM_CANCEL_ID)
        if cancel is not None:
            cancel.callback = self._on_cancel  # type: ignore[method-assign]

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        # Defer first: revoke_connection does several DB reads plus a
        # fetch_message, a message edit, and two DM sends before we could
        # otherwise answer. That runs well past Discord's 3s initial-response
        # window, and a late edit raises NotFound — the revoke would succeed
        # while the member saw "This interaction failed".
        await interaction.response.defer()
        name = self.panel._name_fn(self.user_id)
        removed = await self.panel.cog.revoke_connection(
            self.panel.member.guild, self.panel.member, self.user_id
        )
        self.stop()
        name_fn = self.panel._name_fn
        answer = (
            build_remove_done_layout(user_id=self.user_id, name_fn=name_fn)
            if removed
            else build_remove_missing_layout(user_id=self.user_id, name_fn=name_fn)
        )
        await interaction.edit_original_response(view=_card(answer))
        await self.panel.reload(
            note=f"Removed your connection with {name}."
            if removed
            else f"You and {name} were no longer connected."
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        """Cancel closes the confirm rather than answering it.

        A "Nothing Changed" card was one more thing to read for a press
        whose whole point was that nothing happened; the panel behind this
        card, still showing the row, is the answer. Deleting an ephemeral
        message is best-effort — it may already have been dismissed.
        """
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


async def open_dm_settings(
    cog: "DmPermsCog", interaction: discord.Interaction, member: discord.Member
) -> None:
    """Send the member their own DM-settings panel.

    Shared by the DM request panel's Settings button and the ``/info`` panel,
    so both land on the identical view — the DM mode dial has three states and
    exactly one place that renders them (CLAUDE.md: collapse controls).
    """
    guild = member.guild
    connections = await asyncio.to_thread(
        load_member_connections, cog.bot.ctx.db_path, guild.id, member.id
    )
    name_fn = await _names(
        cog.bot, guild, *[int(row["user_id"]) for row in connections]
    )
    view = DmSettingsView(
        cog,
        member,
        mode=resolve_mode(member, cog._mode_roles_for(guild.id)),
        connections=connections,
        name_fn=name_fn,
    )
    await interaction.response.send_message(view=view, ephemeral=True)
    view.message = await interaction.original_response()


class DmRequestPanelView(discord.ui.LayoutView):
    """The public request panel. **Paradigm 1 — hub panel**, persistent.

    ``art`` is the guild's hub banner and footer. The instance registered at
    cog load passes none — a persistent view dispatches on ``custom_id``
    alone, so it only matters on the copy the sticky actually posts.
    """

    def __init__(self, cog: DmPermsCog, *, art: Optional[PanelArt] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        art = art or PanelArt(banner=None, footer=None)
        for item in build_panel_layout(
            banner_url=art.banner,
            footer_url=art.footer,
            title_in_banner=art.title_in_banner,
        ):
            self.add_item(item)
        _bind(self, DM_HELP_PANEL_CUSTOM_ID, self.open_help)
        _bind(self, DM_REQUEST_PANEL_CUSTOM_ID, self.open_request)
        _bind(self, DM_SETTINGS_PANEL_CUSTOM_ID, self.open_settings)

    async def open_help(self, interaction: discord.Interaction) -> None:
        """The guild's DM FAQ doc, read now, as an ephemeral card."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ Use this in the server.", ephemeral=True
            )
            return
        body = await asyncio.to_thread(
            load_doc_body, self.cog.bot.ctx.db_path, guild.id, HELP_DOC_KEY
        )
        # An admin-written doc may carry a role or @everyone mention; the
        # card is an answer to one tap, never a ping.
        await interaction.response.send_message(
            view=_card(build_help_layout(body)),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def open_request(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DmRequestModal(self.cog))

    async def open_settings(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "❌ Use this in the server.", ephemeral=True
            )
            return
        await open_dm_settings(self.cog, interaction, member)


# ---------------------------------------------------------------------------
# Module-level send helpers
# ---------------------------------------------------------------------------

def _brand(
    view: discord.ui.LayoutView, guild: Optional[discord.Guild]
) -> discord.ui.LayoutView:
    """Stamp a DM-bound card with the server it came from.

    Every card in this feature lands in a DM, and a DM has no guild of its
    own — so the fresh copies go out through ``send_branded_dm``, which adds
    the guild icon and a ``-# From <guild>`` line. A card that *replaces* one
    in place is the same card on the same surface and needs the same stamp:
    without it a decliner's kept receipt names no server while the
    requester's copy of that very event does, and the attribution the
    original request DM carried disappears on the edit that answers it.

    ``guild`` is ``None`` exactly where there is no server left to name — a
    request whose row is gone, or a guild the bot has been removed from — and
    then the card goes out unbranded rather than half-branded.
    """
    if guild is None:
        return view
    return brand_dm_layout(
        view,
        guild_name=guild_display_name(guild),
        guild_icon_url=guild_icon_url(guild),
    )


async def _replace_card(
    interaction: discord.Interaction,
    items: Sequence[discord.ui.Item[Any]],
    *,
    guild: Optional[discord.Guild] = None,
) -> None:
    """Swap the clicked card for ``items``, swallowing a vanished message."""
    await _replace_card_view(interaction, _card(items), guild=guild)


async def _replace_card_view(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    *,
    guild: Optional[discord.Guild] = None,
) -> None:
    """Edit the clicked message into ``view``, branded for ``guild``.

    ``content``/``embed``/``attachments`` are nulled explicitly because a
    consent DM sent before this feature converted is still an embed message,
    and an edit that only sets ``view`` would leave the old embed sitting
    above the new card (the flag flips on this very edit, one-way).
    """
    try:
        await interaction.response.edit_message(
            content=None, embed=None, attachments=[], view=_brand(view, guild)
        )
    except (discord.NotFound, discord.HTTPException):
        pass


async def _dm(
    user: discord.abc.Messageable,
    db_path: Path,
    guild: Optional[discord.Guild],
    **kwargs: Any,
) -> Optional[discord.Message]:
    """DM a consent card branded for ``guild``.

    Positional ``db_path``/``guild`` keep the call sites terse. A V2 card has
    no footer, so ``brand_dm_layout`` puts the server name in the card's
    ``-#`` subtext and, where the card opens with a bare text display, the
    guild icon in a thumbnail beside it.
    """
    return await send_branded_dm(user, db_path=db_path, guild=guild, **kwargs)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class DmPermsCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.consent_pairs: dict[int, set[tuple[int, int]]] = {}
        self.dm_requests: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
        self.panel_settings: dict[int, dict[str, Optional[int]]] = {}
        # Per-guild mode→role-id overrides ({"open"/"ask"/"closed": id}).
        # Loaded at cog_load; the web config route pokes this cache on save.
        self.mode_role_ids: dict[int, dict[str, int]] = {}
        self.panel = StickyPanel(
            "dm perms",
            bot,
            load_ids=self._panel_ids,
            save_ids=self._save_panel_ids,
            build=self._build_panel,
            refresh_on_update=True,
        )
        self._expiry_task: Optional[asyncio.Task[None]] = None
        self._autopost_task: Optional[asyncio.Task[None]] = None
        super().__init__()

    async def cog_load(self) -> None:
        def _load_all() -> dict[str, Any]:
            init_db(self.bot.ctx.db_path)
            return {
                "consent_pairs": load_consent_pairs(self.bot.ctx.db_path),
                "dm_requests": load_requests(self.bot.ctx.db_path),
                "panel_settings": load_panel_settings(self.bot.ctx.db_path),
                "mode_role_ids": load_dm_mode_roles(self.bot.ctx.db_path),
            }

        loaded = await asyncio.to_thread(_load_all)
        self.consent_pairs = loaded["consent_pairs"]
        self.dm_requests = loaded["dm_requests"]
        self.panel_settings = loaded["panel_settings"]
        self.mode_role_ids = loaded["mode_role_ids"]
        self._publish_panel_guilds()

        # Persistent views: clicks on DM consent buttons across ALL DMs route
        # to this single instance, which recovers per-request state from the DB.
        self.bot.add_view(DmRequestPanelView(self))
        self.bot.add_view(AskConsentView(self))

        # The expiry loop sweeps stale 24h+ pending requests. Its first
        # iteration runs once the bot is ready, which handles any requests
        # that aged out while the bot was offline.
        self._expiry_task = asyncio.create_task(self._expiry_loop())
        self._autopost_task = asyncio.create_task(self._autopost_panels())

    async def _autopost_panels(self) -> None:
        """Make sure every configured guild's request panel exists on boot.

        The panel is the *only* route to DM settings now that the /dm_* commands
        are gone, so it can't depend on an admin remembering to press "post
        panel" on the dashboard. ``place_or_refresh`` edits in place when the
        panel is already the configured channel's, so this re-runs safely on
        every restart instead of stacking duplicates.

        A guild with no configured panel channel is skipped — there is nowhere
        to put it, and inventing a channel is not this method's call.
        """
        await self.bot.wait_until_ready()
        for guild_id, settings in list(self.panel_settings.items()):
            channel_id = (settings or {}).get("panel_channel_id")
            if not channel_id:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                log.warning(
                    "dm_perms: panel channel %s for guild %s is missing or not "
                    "a text channel — skipping autopost",
                    channel_id, guild_id,
                )
                continue
            try:
                await self.panel.place_or_refresh(guild, channel)
            except Exception:
                # Deliberately broad, and per-guild: the loop body also reaches
                # sqlite (panel-id save), so a DB hiccup on one guild must not
                # abort the bootstrap for every guild after it. This runs in a
                # bare task nobody awaits, so an escaping exception would only
                # ever surface as "exception was never retrieved" — and the
                # panel is the only route to DM settings.
                log.exception(
                    "dm_perms: failed to autopost the panel in guild %s", guild_id
                )

    async def cog_unload(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            self._expiry_task = None
        if self._autopost_task is not None:
            self._autopost_task.cancel()
            self._autopost_task = None
        self.panel.cancel_all()

    # ── Background tasks ─────────────────────────────────────────────────────

    async def _expiry_loop(self) -> None:
        """Periodic sweep that expires pending requests past their window."""
        await self.bot.wait_until_ready()
        try:
            while not self.bot.is_closed():
                try:
                    await self._expire_stale_now()
                except Exception:
                    log.exception("DM request expiry sweep failed")
                await asyncio.sleep(EXPIRY_SWEEP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _expire_stale_now(self) -> None:
        expired = await asyncio.to_thread(
            expire_stale_pending_requests,
            self.bot.ctx.db_path,
        )
        for row in expired:
            gid = row["guild_id"]
            self._drop_request_from_memory(gid, row["requester_id"], row["target_id"])
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            requester = guild.get_member(row["requester_id"])
            target = guild.get_member(row["target_id"])
            requester_name = display_name_for(requester, row["requester_id"])
            target_name = display_name_for(target, row["target_id"])
            req_type = row["request_type"]
            type_label = request_type_label(req_type)
            write_audit_log(
                self.bot.ctx.db_path, gid, "request_expired",
                user_a_id=row["requester_id"], user_b_id=row["target_id"],
                notes=f"type={req_type}",
            )
            await self._post_audit(
                guild,
                audit_line_expired(requester_name, target_name, type_label),
            )
            if requester:
                # The sweep hands back each guild's window with the row: a
                # get_request_limits() here would open SQLite synchronously on
                # the event loop, once per expired request.
                name_fn = await _names(self.bot, guild, row["target_id"])
                await _dm(
                    requester,
                    self.bot.ctx.db_path,
                    guild,
                    layout=_card(build_expired_layout(
                        target_id=row["target_id"],
                        guild_name=guild.name,
                        request_type=req_type,
                        expiry_label=request_expiry_label(row["expiry_hours"]),
                        name_fn=name_fn,
                    )),
                )

    # ── State helpers ────────────────────────────────────────────────────────

    def _drop_request_from_memory(
        self, guild_id: int, requester_id: int, target_id: int
    ) -> None:
        guild_reqs = self.dm_requests.get(guild_id)
        if guild_reqs is not None:
            guild_reqs.pop((requester_id, target_id), None)

    def _audit_channel_for(self, guild_id: int) -> Optional[int]:
        """Read fresh from DB so changes via the web UI take effect immediately."""
        # Reuses load_audit_channels — small dict for all guilds; fine for the
        # expected scale and avoids a stale cache after web-side edits.
        channels = load_audit_channels(self.bot.ctx.db_path)
        ch = channels.get(guild_id)
        return int(ch) if ch else None

    async def _post_audit(self, guild: discord.Guild, message: str) -> None:
        """Fan one audit line out to the guild's mod-visible feed, if set.

        The send lives here rather than in the service: its card comes from
        ``dm/perms/layouts.py``, which reads the service's own request-type
        vocabulary, and a service that imported the layouts back would close
        that loop.
        """
        audit_channel_id = self._audit_channel_for(guild.id)
        if not audit_channel_id:
            return
        channel = guild.get_channel(audit_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        view = _card(build_audit_notice_layout(
            message=message, timestamp=int(time.time()),
            art=await read_card_art(self.bot.ctx, guild),
        ))
        try:
            await channel.send(view=view)
        except discord.HTTPException:
            pass

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _is_mutual(
        self, guild_id: int, a: int, b: int, *, check_no_contact: bool = True
    ) -> bool:
        # A no-contact pair reads as unconnected everywhere consent is checked,
        # even when a consent row still exists. The row is SUPPRESSED, not
        # deleted: it keeps the provenance of when and why they were connected,
        # which is exactly what a mod reviewing the case wants, while granting
        # none of the access it used to.
        #
        # Note this is the one direction where the in-memory cache is safe to
        # bypass — the DB read below can only ever turn a True into a False.
        #
        # ``check_no_contact=False`` is for the one caller that has already
        # made this exact query and folds the result in itself; it must never
        # be passed by a caller that hasn't.
        if check_no_contact and is_no_contact(self.bot.ctx.db_path, guild_id, a, b):
            return False
        pairs = self.consent_pairs.get(guild_id, set())
        return (a, b) in pairs and (b, a) in pairs

    def _has_pending_request(self, guild_id: int, a: int, b: int) -> bool:
        return (a, b) in self.dm_requests.get(guild_id, {})

    def _mode_roles_for(self, guild_id: int) -> dict[str, int]:
        """The guild's configured mode→role-id overrides (empty dict if none)."""
        return self.mode_role_ids.get(guild_id, {})

    def _precheck_dm_request(self, guild: discord.Guild, requester: discord.Member, target: discord.Member | discord.User) -> Optional[str]:
        # ``classify_dm_request`` takes primitives, not discord objects, so it
        # remains testable without spinning up Discord. The cog observes the
        # facts here and the classifier picks the right message.
        target_in_guild = isinstance(target, discord.Member)
        target_mode = (
            resolve_mode(target, self._mode_roles_for(guild.id))
            if isinstance(target, discord.Member)
            else ""
        )
        # Suppressing the consent pair alone would leave the request path OPEN
        # — "not connected" is precisely the state that invites a new request.
        # Present the pair as though her DMs were closed instead. That reuses
        # an existing refusal verbatim, and it is the right kind of lie: it
        # describes a general setting of hers rather than anything about him,
        # so it reads the same whether or not he is the reason.
        no_contact = is_no_contact(self.bot.ctx.db_path, guild.id, requester.id, target.id)
        if target_in_guild and no_contact:
            target_mode = "closed"
        # A member on a break gets no bot DMs, so the consent card could never
        # reach them; the same closed-DMs refusal says so without saying why.
        if target_in_guild and is_on_break(self.bot.ctx.db_path, guild.id, target.id):
            target_mode = "closed"
        return classify_dm_request(
            target_in_guild=target_in_guild,
            is_self=target.id == requester.id,
            target_is_bot=target.bot,
            target_mode=target_mode,
            # Reuse the lookup above rather than letting ``_is_mutual`` run the
            # identical query a second time.
            is_mutual=(
                not no_contact
                and self._is_mutual(guild.id, requester.id, target.id, check_no_contact=False)
            ),
            has_pending=self._has_pending_request(guild.id, requester.id, target.id),
            # classify_dm_request drops this straight into the refusal text
            # it returns (message content), so it's escaped here like every
            # sibling audit_line_*/build_*_embed name in this feature.
            target_display_name=discord.utils.escape_markdown(
                getattr(target, "display_name", str(target.id))
            ),
        )

    async def _submit_dm_request(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
        request_type: str,
        reason: str,
    ) -> None:
        assert interaction.guild and interaction.user
        guild = interaction.guild
        requester = interaction.user
        req_type = normalize_request_type(request_type or "dm")
        # The modal already caps the reason; this clamp is defence-in-depth for
        # any caller that reaches here another way.
        reason_clean = clamp_reason(reason, MAX_REASON_LENGTH)

        error = self._precheck_dm_request(guild, requester, user)  # type: ignore[arg-type]
        if error:
            if interaction.response.is_done():
                await interaction.followup.send(error, ephemeral=True)
            else:
                await interaction.response.send_message(error, ephemeral=True)
            return

        # Per-requester rate limit: cap concurrent pending requests so a single
        # user can't spam DM prompts to dozens of targets at once. Both the cap
        # and the expiry window are this guild's own dashboard dials.
        limits = get_request_limits(self.bot.ctx.db_path, guild.id)
        max_pending = limits["max_pending"]
        pending_count = count_pending_for_requester(
            self.bot.ctx.db_path, guild.id, requester.id
        )
        if pending_count >= max_pending:
            limit_msg = (
                f"❌ You already have {pending_count} pending DM requests. "
                f"Wait for some to be answered or expire (max {max_pending})."
            )
            if interaction.response.is_done():
                await interaction.followup.send(limit_msg, ephemeral=True)
            else:
                await interaction.response.send_message(limit_msg, ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        type_label = request_type_label(req_type)
        name_fn = await _names(self.bot, guild, requester.id, user.id)
        # Both receipts render this as a counting <t:…:R>; the expiry sweep
        # works from the row's own created_at, so a few seconds of drift
        # between the two is invisible.
        expires_at = int(time.time()) + limits["expiry_hours"] * 3600

        message = await _dm(
            user,
            self.bot.ctx.db_path,
            guild,
            layout=AskConsentView(self, card=build_request_dm_layout(
                requester_id=requester.id,
                requester_avatar_url=requester.display_avatar.url,
                guild_name=guild.name,
                request_type=req_type,
                reason=reason_clean,
                expires_at=expires_at,
                name_fn=name_fn,
            )),
        )
        if message is None:
            await interaction.followup.send(
                "❌ I couldn't DM that user — they may have DMs disabled.", ephemeral=True
            )
            return

        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        self.dm_requests.setdefault(guild.id, {})
        self.dm_requests[guild.id][(requester.id, user.id)] = {
            "request_type": req_type, "reason": reason_clean,
            "message_id": message.id, "channel_id": None,
            "created_at": now_ts, "status": "pending",
        }
        upsert_request(
            self.bot.ctx.db_path, guild.id, requester.id, user.id,
            req_type, reason_clean, message.id, None,
        )

        await _dm(
            requester,
            self.bot.ctx.db_path,
            guild,
            layout=_card(build_request_sent_layout(
                target_id=user.id,
                guild_name=guild.name,
                request_type=req_type,
                reason=reason_clean,
                expires_at=expires_at,
                name_fn=name_fn,
            )),
            bypass_break=True,  # a receipt for their own request
        )

        write_audit_log(
            self.bot.ctx.db_path, guild.id, "request_asked",
            actor_id=requester.id, user_a_id=requester.id, user_b_id=user.id,
            notes=f"type={req_type}",
        )
        await self._post_audit(
            guild,
            audit_line_asked(requester.display_name, user.display_name, type_label),
        )
        # audit_line_asked escapes its own arguments (see dm/perms/logic.py),
        # so the names go in raw above. This line interpolates directly, so it
        # escapes here — the target picked their own name, the requester reads it.
        await interaction.followup.send(
            f"📨 Request sent to {discord.utils.escape_markdown(user.display_name)} "
            "via DM!",
            ephemeral=True,
        )

    # ── request panel (core.sticky) ──────────────────────────────────────

    def _panel_ids(self, guild_id: int) -> tuple[int, int]:
        settings = self.panel_settings.get(guild_id) or {}
        return (
            int(settings.get("panel_channel_id") or 0),
            int(settings.get("panel_message_id") or 0),
        )

    def _save_panel_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.panel_settings[guild_id] = {
            "panel_channel_id": channel_id or None,
            "panel_message_id": message_id or None,
        }
        set_panel_settings(self.bot.ctx.db_path, guild_id, channel_id, message_id)
        self._publish_panel_guilds()

    def _publish_panel_guilds(self) -> None:
        """Keep the listener's fast path in sync with the in-memory settings, so
        a guild with no panel costs a set lookup rather than a DB read."""
        self.panel.set_known_guilds(
            {
                gid
                for gid, s in self.panel_settings.items()
                if s and s.get("panel_channel_id")
            }
        )

    async def _build_panel(self, guild: discord.Guild) -> PanelContent:
        def _read_art() -> PanelArt:
            with self.bot.ctx.open_db() as conn:
                art = resolve_panel_art_conn(conn, guild.id)
            # Draws the titled banner on first use — off the loop, and after
            # the connection is closed.
            return with_title(art, DM_PANEL_TITLE)

        art = await asyncio.to_thread(_read_art)
        return PanelContent(layout=DmRequestPanelView(self, art=art))

    async def post_panel(
        self, guild: discord.Guild, panel_channel_id: int
    ) -> Optional[int]:
        """Post the request panel (or refresh it in place if it's already
        there). Returns the live message id, or None if the channel is
        unusable. Public: the dashboard route calls this."""
        channel = guild.get_channel(panel_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        message = await self.panel.place_or_refresh(guild, channel)
        return message.id if message else None

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_panel_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear the request panel's ids if its channel was deleted, so the
        dashboard stops reporting a panel that cannot exist."""
        await self.panel.on_channel_delete(channel)

    @commands.Cog.listener("on_message")
    async def _on_message_panel_bump(self, message: discord.Message) -> None:
        """Keep the panel at the bottom of its channel.

        Trailing-edge: a burst of chat settles into one repost once the channel
        falls quiet, rather than the panel jumping on the first message of every
        burst (the old leading-edge cooldown).
        """
        await self.panel.on_message(message)

    @commands.Cog.listener("on_member_update")
    async def _on_member_update_dm_roles(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Enforce one DM-mode role, keeping the one that just arrived.

        ``before`` is the whole point: the role added *in this update* is the
        member's new choice, and stripping anything else is the dedup doing its
        job. Ignoring it and keeping the highest-positioned role instead undid
        real choices — see ``pick_dm_roles_to_remove``.
        """
        dm_roles = [
            r for r in after.roles
            if is_dm_mode_role(r, self._mode_roles_for(after.guild.id))
        ]
        before_ids = {r.id for r in before.roles}
        just_added = next((r for r in dm_roles if r.id not in before_ids), None)
        to_remove = pick_dm_roles_to_remove(dm_roles, keep=just_added)
        if not to_remove:
            return
        try:
            await after.remove_roles(*to_remove, reason="DM role dedup")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Could not dedup DM roles for member %s in guild %s: %s",
                after.id, after.guild.id, exc,
            )

    # ── Member self-service ──────────────────────────────────────────────────
    #
    # There are no /dm_* slash commands. /dm_help, /dm_set_mode, /dm_status and
    # /dm_revoke collapsed into the ephemeral DmSettingsView on 2026-07-28,
    # reached from the request panel's "Settings" button. This method is the
    # shared teardown the panel's per-connection Remove calls.

    async def revoke_connection(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target_id: int,
    ) -> bool:
        """Tear down the consent pair between ``actor`` and ``target_id``.

        Returns False when there was nothing to remove, so the caller can say
        "you have no connection with them" rather than claiming success. On a
        real removal this also retires the original request message, DMs both
        parties, and writes both the DB audit row and the mod-feed line.

        Takes an id rather than a member object: the panel lists connections
        from the consent table, and the other party may have left the guild
        since — which is exactly when a member most wants the row gone.
        """
        guild_id = guild.id
        pair_set = self.consent_pairs.get(guild_id, set())
        meta = get_consent_pair_meta(self.bot.ctx.db_path, guild_id, actor.id, target_id)
        db_removed = remove_consent_pair(
            self.bot.ctx.db_path, guild_id, actor.id, target_id
        )
        in_memory_removed = discard_consent_pair(pair_set, actor.id, target_id)

        if not (db_removed or in_memory_removed):
            return False

        name_fn = await _names(self.bot, guild, actor.id, target_id)

        def revoked_card(viewer_id: Optional[int]) -> discord.ui.LayoutView:
            # One build per surface: the card names the party the viewer
            # is not, and a channel card (no viewer) names both.
            return _card(build_revoked_layout(
                viewer_id=viewer_id,
                actor_id=actor.id,
                other_id=target_id,
                name_fn=name_fn,
            ))

        if meta and meta.get("source_msg_id") and meta.get("source_channel_id"):
            # The source is the decision DM the target pressed Accept on, so
            # its channel is a DM channel: guild.get_channel never resolves
            # one, and this edit used to be unreachable. By id, no fetch.
            source = self.bot.get_partial_messageable(
                meta["source_channel_id"]
            ).get_partial_message(meta["source_msg_id"])
            try:
                await source.edit(
                    content=None, embed=None, attachments=[],
                    view=_brand(revoked_card(None), guild),
                )
            except discord.HTTPException:
                pass

        await _dm(
            actor, self.bot.ctx.db_path, guild, layout=revoked_card(actor.id),
            bypass_break=True,  # a receipt for their own revoke
        )
        other = guild.get_member(target_id) or self.bot.get_user(target_id)
        if other is None:
            # Neither cache has them — likely because they left every guild
            # the bot shares with them. A DM works regardless of shared-guild
            # status once we have the id, so fetch rather than silently
            # dropping the notice both the confirm card and this feature's
            # docs promise ("Both of you get a DM about it.").
            try:
                other = await self.bot.fetch_user(target_id)
            except (discord.NotFound, discord.HTTPException):
                other = None
        if other is not None:
            await _dm(
                other, self.bot.ctx.db_path, guild, layout=revoked_card(target_id)
            )

        write_audit_log(
            self.bot.ctx.db_path, guild_id, "relationship_revoked",
            actor_id=actor.id, user_a_id=actor.id, user_b_id=target_id,
        )
        await self._post_audit(
            guild,
            audit_line_revoked(
                actor.display_name, name_fn(target_id), actor.display_name
            ),
        )
        return True


async def setup(bot: Bot) -> None:
    await bot.add_cog(DmPermsCog(bot))
