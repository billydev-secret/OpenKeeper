"""Discord views for Flash Themes — the paid themed day's approval card.

Components V2 since 2026-09-28: both cards this feature sends — the
mod-review card and the public live announcement (which is also the pinned
message) — are ``LayoutView``s built from ``bot_modules/economy/theme_layouts.py``,
not embeds (``docs/plans/components_v2_migration.md`` wave 3a). The review
card reuses ``economy/approval_layouts.build_review_card_layout``, the shared
shape ``quest_layouts.build_signoff_card_layout`` established. The live
announcement is the hub banner's one documented exception to "channel-resident
hub panels only" (Ben, 2026-09-28) — see ``theme_layouts``'s module docstring.

Same shape as ``pin_views``: a **persistent** Approve/Decline pair built from
``discord.ui.DynamicItem`` subclasses whose ``custom_id`` embeds the submission
id (``econ_theme_sub:approve:<id>`` / ``econ_theme_sub:deny:<id>``), so a click
still routes after a restart once the cog re-registers the classes.

The one real difference from the pin's card is that **Approve posts nothing**.
A pin goes up the moment a mod says yes; a theme joins a queue and runs when
the channel is next free, so approving is a pure database move and the
announcement is the loop's job (:func:`announce_theme`). That keeps the
heaviest failure mode — a Discord post that doesn't land — off the mod's
button and onto a sweep that simply tries again next hour.

Every handler is fail-safe: a service error becomes an ephemeral note, never a
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
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import discord

from bot_modules.core.card_art import read_card_art
from bot_modules.core.db_utils import open_db
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.theme_layouts import (
    ANNOUNCEMENT_BANNER_TITLE,
    build_theme_live_layout,
    build_theme_review_layout,
)
from bot_modules.economy.view_helpers import (
    card_name_fn,
    edit_review_card_layout,
    edit_stored_card_layout,
    refresh_review_card_layout,
    refresh_todo_board,
    review_surface,
)
from bot_modules.core.branding_service import resolve_panel_art_conn
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)
from bot_modules.economy.theme_service import (
    approve as approve_theme,
    deny,
    get_submission,
    go_live,
    queue_depth,
    theme_window_seconds,
)
from bot_modules.shared.name_resolver import (
    NameFn,
    build_name_fn,
    mention,
)
from bot_modules.panels.title_service import titled_banner_url

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import Bot
    from bot_modules.core.branding_service import PanelArt

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review themed days."


def _hours_text(settings: EconSettings) -> str:
    hours = int(theme_window_seconds(settings) // 3600)
    return "24 hours" if hours == 24 else f"{hours} hours"


async def unpin_and_delete(
    bot: discord.Client, channel_id: int, message_id: int
) -> None:
    """Best-effort: unpin and delete a finished theme's announcement.

    Shared with the loop's expiry sweep. A missing channel/message (already
    gone, or the bot lost access) is not an error — the DB row is already
    retired. Deleting rather than merely unpinning matches Pin of the Day, and
    is what keeps an erased member's name from surviving in a stale
    announcement long after their data went.
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
        await message.unpin(reason="Flash theme ended")
    except discord.HTTPException:
        log.debug("econ theme: failed to unpin %s", message_id, exc_info=True)
    try:
        await message.delete()
    except discord.HTTPException:
        log.debug("econ theme: failed to delete %s", message_id, exc_info=True)


# ── persistent approval buttons ───────────────────────────────────────────────


class ThemeApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_theme_sub:approve:(?P<sid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the submission id."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_theme_sub:approve:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ThemeApproveButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.submission_id, approve=True, deny_reason=None
        )


class _DenyReasonModal(discord.ui.Modal, title="Decline This Theme"):
    """Reason is optional but encouraged — the member gets it in a DM."""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Why? (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Too close to last week's, needs rewording…",
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


class ThemeDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_theme_sub:deny:(?P<sid>\d+)"),
):
    """Persistent Decline button; opens the reason modal before resolving."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Decline",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_theme_sub:deny:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ThemeDenyButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _DenyReasonModal(self.submission_id, interaction.message)
        )


def _review_buttons_row(submission_id: int) -> "discord.ui.ActionRow[Any]":
    """The persistent Approve/Decline pair, built fresh for one card. See
    ``quest_views._signoff_buttons_row`` for why the ``# type: ignore``s below
    are a known-safe pyright gap, not a real mistyping.
    """
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(ThemeApproveButton(submission_id))  # type: ignore[reportArgumentType]
    row.add_item(ThemeDenyButton(submission_id))  # type: ignore[reportArgumentType]
    return row


def _theme_card_view(
    settings: EconSettings,
    row,
    *,
    name_fn: NameFn = mention,
    art: PanelArt | None = None,
) -> discord.ui.LayoutView:
    """The one artifact a theme card is: text plus its Approve/Decline pair,
    baked in together — buttons only while the row is still pending. This
    replaces the embed era's separate ``ThemeReviewView`` class: a
    Components V2 card is one item, not a message body and a view attached
    to it separately.
    """
    submission_id = int(row["id"])
    state = str(row["state"])
    buttons = _review_buttons_row(submission_id) if state == "pending" else None
    items = build_theme_review_layout(
        settings,
        sponsor_id=int(row["user_id"]),
        name_fn=name_fn,
        title=str(row["title"]),
        blurb=str(row["blurb"]),
        price=int(row["price"]),
        state=state,
        resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
        deny_reason=str(row["deny_reason"] or ""),
        refunded=row["refunded_at"] is not None,
        expires_at=row["expires_at"],
        buttons=buttons,
        art=art,
    )
    view: discord.ui.LayoutView = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def render_theme_review_card(
    accent,
    settings: EconSettings,
    row,
    *,
    name_fn: NameFn = mention,
    art: PanelArt | None = None,
) -> discord.ui.LayoutView:
    """``(accent, settings, row)`` — the call shape ``approval_views.py``'s
    per-product registry and the dashboard's resolver use for every
    product's card now that both (theme, pin) build a
    Components V2 ``LayoutView``, so every surface can call any of them
    uniformly. ``accent`` is unused: a V2 card carries no colour (guide §
    No container, no accent bar); it stays a parameter only so the two
    products keep one signature. ``art`` is for the approvals-channel copy
    only (``view_helpers.surface_art``).
    """
    return _theme_card_view(settings, row, name_fn=name_fn, art=art)


def _card_layout(
    settings: EconSettings, row, name_fn: NameFn = mention, *, art: PanelArt | None = None
) -> discord.ui.LayoutView:
    """Positional-args adapter matching the ``build_layout(settings, row,
    name_fn, art=)`` shape ``view_helpers``'s edit/refresh partials call."""
    return _theme_card_view(settings, row, name_fn=name_fn, art=art)


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ theme")
_refresh_board = partial(refresh_todo_board, log_label="econ theme")


_edit_card = partial(
    edit_review_card_layout, build_layout=_card_layout, log_label="econ theme"
)
_edit_stored = partial(
    edit_stored_card_layout, build_layout=_card_layout, log_label="econ theme"
)
_refresh_card = partial(
    refresh_review_card_layout,
    read_row=get_submission,
    build_layout=_card_layout,
    log_label="econ theme",
)


async def _handle_resolution(
    interaction: discord.Interaction,
    submission_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Gate, resolve, edit the card, DM. Never raises."""
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
        log.debug("econ theme: failed to defer resolution", exc_info=True)

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
        log.exception("econ theme: failed to load submission %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't load that — try again.")
        return
    if loaded is None:
        await _safe_ephemeral(interaction, "❌ That theme no longer exists.")
        return
    settings, row = loaded

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    # One resolver for every repaint below: the buyer, and the mod acting now.
    name_fn = await card_name_fn(ctx, guild, row, member.id)
    if str(row["state"]) != "pending":  # type: ignore[index]
        await _refresh_card(card, ctx, settings, submission_id, name_fn=name_fn)
        await _safe_ephemeral(interaction, f"Already {row['state']}.")  # type: ignore[index]
        return

    def _resolve():
        with ctx.open_db() as conn:
            if approve:
                fresh = approve_theme(conn, submission_id, resolver_id=member.id)
                return fresh, queue_depth(conn, guild.id)
            fresh = deny(
                conn, submission_id, resolver_id=member.id,
                deny_reason=deny_reason or "",
            )
            return fresh, 0

    try:
        fresh, depth = await asyncio.to_thread(_resolve)
    except ValueError as exc:
        await _refresh_card(card, ctx, settings, submission_id, name_fn=name_fn)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ theme: failed to resolve %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't resolve that — try again.")
        return

    await _edit_card(card, settings, fresh, name_fn=name_fn, ctx=ctx)
    # If they resolved this from the board's ephemeral, the card sitting in
    # the approvals channel is still offering the decision — close it too.
    await _edit_stored(bot, card, settings, fresh, name_fn=name_fn)
    await _dm_sponsor(bot, ctx.db_path, guild, settings, fresh)
    await _refresh_board(bot, guild.id)
    if approve:
        ahead = max(0, depth - 1)
        when = "It runs next time the channel is free." if not ahead else (
            f"{ahead} ahead of it in the queue."
        )
        await _safe_ephemeral(interaction, f"Approved and queued. {when}")
    else:
        await _safe_ephemeral(interaction, "Declined and refunded.")


def theme_resolution_dm_text(settings: EconSettings, row) -> str:
    """The member-facing receipt for a resolved submission."""
    unit = settings.currency_plural or "coins"
    state = str(row["state"])
    if state == "approved":
        return (
            f"🎨 Your theme **{row['title']}** was approved — it runs the next "
            "time the channel is free. You'll hear from me when it goes up."
        )
    if state == "live":
        return (
            f"🎨 Your theme **{row['title']}** is live now, announced and pinned "
            f"for {_hours_text(settings)}. Enjoy the day!"
        )
    # Only `refunded_at` knows whether money actually went back: `expired` is
    # reached by a request nobody reviewed (refunded) AND by a theme that ran
    # or was ended early (not refunded). Branching on the state alone told a
    # member their coins were returned when a mod ended their running theme.
    if row["refunded_at"] is None:
        return (
            f"🎨 Your themed day **{row['title']}** has ended. Thanks for "
            "setting the tone — no refund, you got your day."
        )
    reason = discord.utils.escape_markdown(str(row["deny_reason"] or ""))
    tail = f"\n**Why:** {reason}" if reason else ""
    return (
        f"🚫 Your themed day wasn't accepted, and your {int(row['price'])} {unit} "
        f"have been refunded.\n> {row['title']}{tail}"
    )


async def _dm_sponsor(
    bot: discord.Client,
    db_path: Path,
    guild: discord.Guild,
    settings: EconSettings,
    row,
) -> None:
    """Tell the member what happened. Best-effort; a closed DM is not an error.

    Takes a ``db_path`` rather than an AppContext because the loop announcing a
    queued theme has only the former.
    """
    if not int(row["user_id"]):
        return  # detached by an erasure — there is nobody left to tell
    try:
        await notify_member(
            bot, db_path, guild.id, int(row["user_id"]),
            content=theme_resolution_dm_text(settings, row),
        )
    except Exception:  # noqa: BLE001 — best-effort receipt DM after a resolution that already moved money; notify_member's own catch doesn't cover every failure mode of the underlying send
        log.debug("econ theme: failed to DM member", exc_info=True)


def _read_announcement_art(db_path: Path, guild_id: int) -> tuple[str | None, str | None]:
    """The guild's hub banner/footer for the live announcement, blocking.

    Drawing :data:`theme_layouts.ANNOUNCEMENT_BANNER_TITLE` into the banner
    (``panel_title_service.titled_banner_url``) is Pillow work — this, like
    that call, must run off the event loop. Unconfigured art (the default)
    returns ``(None, None)``, which ``build_theme_live_layout`` turns into no
    galleries at all: a guild that has never uploaded hub art sees no change
    (ship dark). A guild that already has one sees it on the announcement the
    next restart on, whether or not the title actually drew — a failed draw
    (an unreadable upload, a Pillow error) falls back to the plain banner
    art rather than losing the gallery outright.
    """
    with open_db(db_path) as conn:
        art = resolve_panel_art_conn(conn, guild_id)
    if not art.banner:
        return None, art.footer
    banner_url = titled_banner_url(art.banner, ANNOUNCEMENT_BANNER_TITLE) or art.banner
    return banner_url, art.footer


async def announce_theme(
    bot: discord.Client,
    db_path: Path,
    guild: discord.Guild,
    settings: EconSettings,
    row,
) -> bool:
    """Post + pin one queued theme's announcement, then flip it live.

    Called by the hourly loop when the theme channel is free. Returns True if
    the theme actually went live.

    The Discord post happens BEFORE the state move, matching Pin of the Day: a
    post that fails leaves the row ``approved``, so the sweep simply tries
    again next hour and nobody is charged for a day that never ran. If the
    state move then fails the row was resolved out from under us (withdrawn,
    and already refunded), so the orphan announcement is taken straight back
    down.
    """
    channel = guild.get_channel(int(settings.theme_channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("econ theme: no usable theme channel in guild %d", guild.id)
        return False

    # Public and pinned for the whole window, so the buyer is resolved to a
    # name. A user_id of 0 means an erasure detached the theme from whoever
    # bought it; the builder renders "a member" and nothing is looked up.
    name_fn = await build_name_fn(
        guild=guild, db_path=db_path, guild_id=guild.id,
        user_ids=[int(row["user_id"] or 0)],
    )
    banner_url, footer_url = await asyncio.to_thread(
        _read_announcement_art, db_path, guild.id
    )
    # Computed once, before the post, and handed to `go_live` below as its
    # own `now` — so the countdown printed on the card and the row's stored
    # `expires_at` agree exactly rather than being two separate reads of the
    # clock a few awaits apart.
    live_now = time.time()
    expires_at = live_now + theme_window_seconds(settings)
    items = build_theme_live_layout(
        settings,
        sponsor_id=int(row["user_id"]),
        name_fn=name_fn,
        title=str(row["title"]),
        blurb=str(row["blurb"]),
        expires_at=expires_at,
        banner_url=banner_url,
        footer_url=footer_url,
    )
    view: discord.ui.LayoutView = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    try:
        posted = await channel.send(view=view)
    except discord.HTTPException:
        log.warning(
            "econ theme: couldn't post in the theme channel for guild %d", guild.id
        )
        return False
    try:
        await posted.pin(reason="Flash theme")
    except discord.HTTPException:
        # The card is already in the channel but could not be pinned (no Manage
        # Messages, or the channel is at Discord's 50-pin limit). Take it back
        # down: the row stays `approved`, so leaving it would post a fresh
        # unpinned copy every hour for as long as the condition lasts.
        log.warning(
            "econ theme: couldn't pin in the theme channel for guild %d", guild.id
        )
        try:
            await posted.delete()
        except discord.HTTPException:
            log.debug("econ theme: failed to clean up unpinned card", exc_info=True)
        return False

    def _golive():
        with open_db(db_path) as conn:
            return go_live(
                conn, int(row["id"]),
                theme_channel_id=channel.id,
                theme_message_id=posted.id,
                window_seconds=theme_window_seconds(settings),
                now=live_now,
            )

    try:
        fresh = await asyncio.to_thread(_golive)
    except ValueError:
        await unpin_and_delete(bot, channel.id, posted.id)
        return False
    except Exception:
        log.exception("econ theme: go_live failed for %s", row["id"])
        await unpin_and_delete(bot, channel.id, posted.id)
        return False

    await _restamp_card(bot, guild, db_path, settings, fresh)
    await _dm_sponsor(bot, db_path, guild, settings, fresh)
    return True


async def _restamp_card(
    bot: discord.Client,
    guild: discord.Guild,
    db_path: Path,
    settings: EconSettings,
    row,
) -> None:
    """Re-render a legacy bank-channel card once a queued theme goes live.

    Nothing has recorded those ids since the review moved to the todo board
    (2026-08-29), so this is a no-op for every new submission and exists for
    the cards posted before it. The loop holds no card object — only the ids
    on the row — so it fetches before editing. Entirely best-effort: the theme
    is already running and the member already told, so a lost card is
    cosmetic.
    """
    channel_id, message_id = int(row["card_channel_id"]), int(row["card_message_id"])
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    # Without a resolver the restamp fell back to <@id> for sponsor and approver.
    name_fn = await build_name_fn(
        guild=guild,
        db_path=db_path,
        guild_id=guild.id,
        user_ids=[int(row["user_id"] or 0), int(row["resolver_id"] or 0)],
    )
    try:
        art = await read_card_art(getattr(bot, "ctx", None), guild)
        card = await channel.fetch_message(message_id)
        await card.edit(
            content=None, embed=None, attachments=[],
            view=_theme_card_view(settings, row, name_fn=name_fn, art=art),
        )
    except discord.HTTPException:
        log.debug("econ theme: failed to restamp card %s", message_id, exc_info=True)
