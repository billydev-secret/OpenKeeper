"""Discord views for Pin of the Day — the paid pin's mod-approval card.

Components V2 since 2026-09-28: both cards this feature sends — the
mod-review card and the public "📌 Pinned by a Member" card — are
``LayoutView``s built from ``bot_modules/economy/pin_layouts.py``, not embeds
(``docs/plans/components_v2_migration.md`` wave 3a). The review card reuses
``economy/approval_layouts.build_review_card_layout``, the shared shape
``quest_layouts.build_signoff_card_layout`` established.

A **persistent** Approve/Decline pair built from
``discord.ui.DynamicItem`` subclasses whose ``custom_id`` embeds the submission
id (``econ_pin_sub:approve:<id>`` / ``econ_pin_sub:deny:<id>``), so a click still
routes after a restart once the cog re-registers the classes.

Approve is heavier than the sponsor's: it posts the live "📌 Pinned by @X" card
to the pin channel and pins it, then flips the row to ``live`` (superseding any
prior live pin, which it unpins). The Discord post happens BEFORE the DB move, so
a failed post leaves the row pending and refundable — the member is never charged
for a pin nobody saw. Decline opens a reason modal and refunds.

Every handler is fail-safe — a service error becomes an ephemeral note, never a
dead button.

The card is reviewed in two places, and the buttons below serve both: a post in
the economy's staff-only ``approvals_channel_id`` (``approval_views.
post_approval_card``, dark until that dial is set), and the ephemeral detail
behind the todo board's 🧾 Approvals button. Resolving on either closes both —
the board reads the queue live, and the channel card is repainted through the
ids stored on the row (``view_helpers.edit_stored_card_layout``). Between
2026-08-29 and 2026-09-02 the board was the only surface; before that, cards
went to ``bank_channel_id``, which in the main guild members can read. Those
legacy cards are still the *embed* messages they were created as; the
resolution repaint converts one to a card in place, the way the style guide's
"edit converting embed to card" case says (``content=None, embed=None,
attachments=[]``, then ``view=``) — see ``view_helpers.edit_review_card_layout``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Sequence, cast
from functools import partial

import discord

from bot_modules.core.card_art import read_card_art
from bot_modules.economy.pin_layouts import build_pin_live_layout, build_pin_review_layout
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.pin_service import (
    PIN_LIFETIME_SECONDS,
    deny,
    get_submission,
    go_live,
    refund_failed_golive,
)
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)
from bot_modules.shared.name_resolver import (
    NameFn,
    mention,
)
from bot_modules.core.utils import jump_url
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy.view_helpers import (
    card_name_fn,
    edit_review_card_layout,
    edit_stored_card_layout,
    refresh_review_card_layout,
    refresh_todo_board,
    review_surface,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot
    from bot_modules.core.branding_service import PanelArt

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review pinned messages."


async def unpin_and_delete(
    bot: discord.Client, channel_id: int, message_id: int
) -> None:
    """Best-effort: unpin and delete a live pin's Discord message.

    Shared with the loop's expiry sweep. A missing channel/message (already gone,
    or the bot lost access) is not an error — the DB row is already retired.
    """
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.HTTPException:
        return
    try:
        await message.unpin(reason="Pin of the Day expired")
    except discord.HTTPException:
        log.debug("econ pin: failed to unpin %s", message_id, exc_info=True)
    try:
        await message.delete()
    except discord.HTTPException:
        log.debug("econ pin: failed to delete %s", message_id, exc_info=True)


# ── persistent approval buttons ───────────────────────────────────────────────


class PinApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_pin_sub:approve:(?P<sid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the submission id."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_pin_sub:approve:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PinApproveButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.submission_id, approve=True, deny_reason=None
        )


class _DenyReasonModal(discord.ui.Modal, title="Decline This Pin"):
    """Reason is optional but encouraged — the member gets it in a DM."""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Why? (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Off-topic, ad, needs rewording…",
    )

    def __init__(self, submission_id: int, card: discord.Message | None) -> None:
        super().__init__()
        self.submission_id = submission_id
        self.card = card

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction,
            self.submission_id,
            approve=False,
            deny_reason=str(self.reason.value or ""),
            card_message=self.card,
        )


class PinDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_pin_sub:deny:(?P<sid>\d+)"),
):
    """Persistent Decline button; opens the reason modal before resolving."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Decline",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_pin_sub:deny:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PinDenyButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _DenyReasonModal(self.submission_id, interaction.message)
        )


def _card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send."""
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _review_buttons_row(submission_id: int) -> "discord.ui.ActionRow[Any]":
    """The persistent Approve/Decline pair, built fresh for one card.

    ``DynamicItem`` is declared ``Item[View | LayoutView]`` in the discord.py
    stubs (it can attach to either), but ``ActionRow`` wants an ``Item``
    parameterized to its own concrete view type — a union isn't a subtype of
    either half, so pyright flags a real runtime-safe call as a type error
    (same note as ``quest_views._signoff_buttons_row``). The two buttons are
    still verified in ``tests/economy/test_pin_layouts.py`` to render inside a
    ``LayoutView`` and keep their exact ``custom_id``.
    """
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(PinApproveButton(submission_id))  # type: ignore[reportArgumentType]
    row.add_item(PinDenyButton(submission_id))  # type: ignore[reportArgumentType]
    return row


def build_pin_review_view(
    settings: EconSettings,
    row,
    *,
    name_fn: NameFn = mention,
    art: PanelArt | None = None,
) -> discord.ui.LayoutView:
    """The reviewed card for one submission — buttoned while pending, bare
    once resolved.

    One build per surface: the board's ephemeral detail and the resolution
    repaint each get their own instance, so a view never belongs to two
    messages. Called directly by ``approval_views.py``'s registry (posting
    and the board's picker); the resolution flow below goes through
    ``_card_layout``, the row-based adapter ``view_helpers``'s partials call.
    """
    state = str(row["state"])
    submission_id = int(row["id"])
    buttons = _review_buttons_row(submission_id) if state == "pending" else None
    return _card(
        build_pin_review_layout(
            settings,
            sponsor_id=int(row["user_id"]),
            name_fn=name_fn,
            message=str(row["message"]),
            price=int(row["price"]),
            state=state,
            resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
            deny_reason=str(row["deny_reason"] or ""),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            refunded=row["refunded_at"] is not None,
            buttons=buttons,
            art=art,
        )
    )


def _card_layout(
    settings: EconSettings, row, name_fn: NameFn = mention, *, art: PanelArt | None = None
) -> discord.ui.LayoutView:
    """Positional-args adapter matching the ``build_layout(settings, row,
    name_fn, art=)`` shape ``view_helpers``'s edit/refresh partials call."""
    return build_pin_review_view(settings, row, name_fn=name_fn, art=art)


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ pin")
_refresh_board = partial(refresh_todo_board, log_label="econ pin")


async def _handle_resolution(
    interaction: discord.Interaction,
    submission_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Gate, resolve, (for approve: post+pin), edit the card, DM. Never raises."""
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    # An ephemeral card means the todo board's Approvals flow; see
    # ``view_helpers.review_surface``. Everything downstream repaints the
    # same way either way.
    card = review_surface(
        interaction,
        card_message if card_message is not None else interaction.message,
    )

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("econ pin: failed to defer resolution", exc_info=True)

    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return

    def _load() -> tuple[EconSettings, object] | None:
        with ctx.open_db() as conn:
            row = get_submission(conn, submission_id)
            if row is None:
                return None
            return load_econ_settings(conn, guild.id), row

    try:
        loaded = await asyncio.to_thread(_load)
    except Exception:
        log.exception("econ pin: failed to load submission %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't load that — try again.")
        return
    if loaded is None:
        await _safe_ephemeral(interaction, "❌ That submission no longer exists.")
        return
    settings, row = loaded

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    # One resolver for every repaint below: the requester, and the mod acting now.
    name_fn = await card_name_fn(ctx, guild, row, member.id)
    if str(row["state"]) != "pending":  # type: ignore[index]
        await _refresh_card(card, ctx, settings, submission_id, name_fn=name_fn)
        await _safe_ephemeral(interaction, f"Already {row['state']}.")  # type: ignore[index]
        return

    if not approve:
        await _do_deny(interaction, ctx, guild, settings, submission_id,
                       card, deny_reason or "", member.id, name_fn)
    else:
        await _do_approve(interaction, bot, ctx, guild, settings,
                          submission_id, row, card, member.id, name_fn)
    # Whatever it resolved to, it is off the queue the board renders. Both
    # branches above swallow their own failures, so this always runs.
    await _refresh_board(bot, guild.id)


async def _do_deny(
    interaction, ctx, guild: discord.Guild, settings, submission_id, card,
    deny_reason: str, resolver_id: int, name_fn: NameFn = mention,
) -> None:
    def _resolve():
        with ctx.open_db() as conn:
            return deny(
                conn, submission_id, resolver_id=resolver_id, deny_reason=deny_reason
            )

    try:
        fresh = await asyncio.to_thread(_resolve)
    except ValueError as exc:
        await _refresh_card(card, ctx, settings, submission_id, name_fn=name_fn)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ pin: failed to deny %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't resolve that — try again.")
        return
    await _edit_card(card, settings, fresh, name_fn=name_fn, ctx=ctx)
    # A resolution from the board's ephemeral leaves the approvals-channel
    # card still offering the decision — close that one too.
    await _edit_stored(interaction.client, card, settings, fresh, name_fn=name_fn)
    await _dm_sponsor(interaction.client, ctx, guild, settings, fresh)
    await _safe_ephemeral(interaction, "Declined and refunded.")


async def _do_approve(
    interaction, bot, ctx, guild: discord.Guild, settings, submission_id,
    row, card, resolver_id: int, name_fn: NameFn = mention,
) -> None:
    """Post + pin the live card, then flip the row to live (superseding any prior).

    The Discord post is first: if it fails, the row stays pending and we refund,
    so the member is never charged for a pin that never showed. The expiry is
    computed once, here, and threaded into both the posted card's countdown
    and ``go_live``'s ``now`` — so the card's "Expires <t:…:R>" and the row
    it describes agree to the second.
    """
    channel = guild.get_channel(int(settings.pin_channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        await _refund_and_report(
            interaction, ctx, guild, settings, row, card,
            "❌ No pin channel is set here — refunded. Set one on the dashboard.",
            name_fn=name_fn,
        )
        return

    # The pinned card is public and long-lived, so the buyer must be named
    # rather than left as a <@id> that renders as a bare number to anyone
    # whose client hasn't cached them.
    art = await read_card_art(ctx, guild)
    now = time.time()
    live_view = _card(
        build_pin_live_layout(
            sponsor_id=int(row["user_id"]),
            name_fn=name_fn,
            message=str(row["message"]),
            expires_at=int(now + PIN_LIFETIME_SECONDS),
            art=art,
        )
    )
    try:
        posted = await channel.send(view=live_view)
        await posted.pin(reason="Pin of the Day")
    except discord.HTTPException:
        log.warning("econ pin: failed to post/pin for %s", submission_id)
        await _refund_and_report(
            interaction, ctx, guild, settings, row, card,
            "❌ Couldn't post or pin in the pin channel (permissions?) — refunded.",
            name_fn=name_fn,
        )
        return

    def _golive():
        with ctx.open_db() as conn:
            return go_live(
                conn, submission_id, resolver_id=resolver_id,
                pin_channel_id=channel.id, pin_message_id=posted.id, now=now,
            )

    try:
        result = await asyncio.to_thread(_golive)
    except ValueError as exc:
        # Raced (declined/resolved between load and now) — undo the orphan pin.
        await unpin_and_delete(bot, channel.id, posted.id)
        await _refresh_card(card, ctx, settings, submission_id, name_fn=name_fn)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ pin: go_live failed for %s", submission_id)
        await unpin_and_delete(bot, channel.id, posted.id)
        await _safe_ephemeral(interaction, "❌ Couldn't finalise that — try again.")
        return

    # Retire the pin this one replaced (best-effort Discord cleanup).
    if result.superseded is not None:
        await unpin_and_delete(
            bot,
            int(result.superseded["pin_channel_id"]),
            int(result.superseded["pin_message_id"]),
        )

    await _edit_card(card, settings, result.live, name_fn=name_fn, ctx=ctx)
    await _edit_stored(bot, card, settings, result.live, name_fn=name_fn)
    await _dm_sponsor(bot, ctx, guild, settings, result.live)
    await _safe_ephemeral(interaction, "Approved — pinned for 24 hours.")


async def _refund_and_report(
    interaction, ctx, guild: discord.Guild, settings, row, card, msg: str,
    *, name_fn: NameFn | None = None,
) -> None:
    """Refund a pin that couldn't be posted and tell the mod.

    ``name_fn`` is the resolver the caller already built for this row; only a
    caller without one pays for a second prefetch.
    """
    def _refund():
        with ctx.open_db() as conn:
            refund_failed_golive(conn, row)
            return get_submission(conn, int(row["id"]))

    try:
        fresh = await asyncio.to_thread(_refund)
    except Exception:
        log.exception("econ pin: refund-on-post-failure errored for %s", row["id"])
        fresh = None
    if fresh is not None:
        if name_fn is None:
            name_fn = await card_name_fn(ctx, guild, row)
        await _edit_card(card, settings, fresh, name_fn=name_fn, ctx=ctx)
        await _edit_stored(
            interaction.client, card, settings, fresh, name_fn=name_fn
        )
        await _dm_sponsor(interaction.client, ctx, guild, settings, fresh)
    await _safe_ephemeral(interaction, msg)


_edit_card = partial(
    edit_review_card_layout, build_layout=_card_layout, log_label="econ pin"
)
_edit_stored = partial(
    edit_stored_card_layout, build_layout=_card_layout, log_label="econ pin"
)


_refresh_card = partial(
    refresh_review_card_layout,
    read_row=get_submission,
    build_layout=_card_layout,
    log_label="econ pin",
)


def pin_resolution_dm_text(settings: EconSettings, row, *, guild_id: int) -> str:
    """The member-facing receipt for a resolved submission.

    An approved pin has a live message by the time this runs, so the receipt
    links it — "enjoy the spotlight" is a poor invitation if they then have to
    go hunting for their own pin. A denial has no message to link: it never
    got posted.

    The link goes *before* the quoted text, never appended to it. Pin text is
    validated for length alone and keeps its newlines, so a member whose pin
    ends mid-code-fence would otherwise swallow the URL into a code block and
    render it unclickable.
    """
    unit = settings.currency_plural or "coins"
    if str(row["state"]) == "live":
        channel_id = int(row["pin_channel_id"] or 0)
        message_id = int(row["pin_message_id"] or 0)
        where = (
            f"\n{jump_url(guild_id, channel_id, message_id)}"
            if channel_id and message_id
            else ""
        )
        return (
            "📌 Your message is pinned for the next 24 hours — enjoy the "
            f"spotlight!{where}\n"
            f"> {row['message']}"
        )
    reason = discord.utils.escape_markdown(str(row["deny_reason"] or ""))
    tail = f"\n**Why:** {reason}" if reason else ""
    return (
        f"🚫 Your pin wasn't accepted, and your {int(row['price'])} {unit} have "
        f"been refunded.\n> {row['message']}{tail}"
    )


async def _dm_sponsor(
    bot: Bot, ctx: AppContext, guild: discord.Guild, settings: EconSettings, row
) -> None:
    """Tell the member what happened. Best-effort; a closed DM is not an error."""
    text = pin_resolution_dm_text(settings, row, guild_id=guild.id)
    try:
        await notify_member(bot, ctx.db_path, guild.id, int(row["user_id"]), content=text)
    except Exception:  # noqa: BLE001 — best-effort receipt DM after a resolution that already moved money; notify_member's own catch doesn't cover every failure mode of the underlying send
        log.debug("econ pin: failed to DM member", exc_info=True)
