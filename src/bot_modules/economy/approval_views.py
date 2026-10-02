"""Paid-request review: the approvals-channel card and the board's button.

A themed day and a pin are two products a member pays for and a moderator
then approves (a sponsored question of the day was a third until 2026-09).
Each used to post its own Approve/Decline
card into the economy's ``bank_channel_id``, which in the main guild is
"how-it-works" — a member-facing explainer. That published an unreviewed
request, naming the member and quoting what they wrote, to the whole server
before anyone had looked at it. Pin of the Day was the worst of them: it
has no dashboard queue, so that public card was the *only* place it could be
reviewed at all.

There are now two review surfaces, and both are always live:

* **A card in ``approvals_channel_id``** (:func:`post_approval_card`) — a
  *dedicated, staff-only* dial, not the bank channel, and dark until it is
  set. This is where mods actually work.
* **The mods' todo board**, exactly as the quest sign-offs do
  (``quest_views.open_signoff_picker``). The board is one sticky message and
  Discord caps the components on a message, so Approve/Decline cannot hang off
  it once per request: the Approvals button opens an ephemeral pick-one select
  across every queue, and picking a request edits that ephemeral into the
  product's own review card. The backstop, and the only surface when the
  channel dial is unset.

**Two surfaces, one ledger.** ``card_channel_id``/``card_message_id`` on each
submissions table are what stop them disagreeing: a resolution anywhere closes
both — the board because its section reads the queue live, the card because
``view_helpers.edit_stored_card_layout`` repaints it from those ids. The
hourly expiry sweep closes cards through :func:`close_expired_card` for the
same reason.

Nothing here re-implements a product's review. The registry below maps a queue
key onto the one thing the picker needs from each product — build its card,
buttons baked in — and stops there; approving a pin still posts and pins,
approving a theme still only queues, and both still live with their product.

Every product (theme, pin) builds a bare Components V2 ``LayoutView``
now that theme converted in its 2026-09-28 wave — theme was the last of the
products still building an ``Embed`` plus a separate persistent
``discord.ui.View``. The three call sites below (open picker, post to
channel, close on expiry) used to branch on ``isinstance(rendered,
discord.ui.LayoutView)`` to tell the two shapes apart; with nothing left on
the embed side of that branch, they no longer need to.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, cast

import discord

from bot_modules.core.card_art import read_card_art
from bot_modules.core.utils import role_ping_kwargs
from bot_modules.shared.name_resolver import NameFn, mention
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy import pin_views, theme_views
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.view_helpers import card_name_fn, refresh_todo_board
from bot_modules.economy.approvals_service import (
    QUEUES_BY_KEY,
    card_location,
    get_approval_row,
    pending_approvals,
    set_approval_card,
)
from bot_modules.economy.service import EconSettings, load_econ_settings
from bot_modules.todo.board_logic import approval_label

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot
    from bot_modules.core.branding_service import PanelArt

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review paid requests."

#: Seconds the ephemeral picker stays usable. Matches the sign-off picker.
_PICKER_TIMEOUT = 180

_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ approvals")


#: One queue's card builder — every product now returns a bare Components V2
#: ``LayoutView`` with its buttons already baked in (state-dependent; see
#: ``theme_views._theme_card_view`` and ``pin_views.build_pin_review_view``), so there is nothing left to attach
#: separately — a call site just sends or edits with ``view=`` alone.
_Renderer = Callable[[EconSettings, Any], discord.ui.LayoutView]


def _renderers(
    name_fn: NameFn = mention, art: PanelArt | None = None
) -> dict[str, _Renderer]:
    """Queue key → how to build this row's card.

    A registry rather than a method on ``ApprovalQueue``: the descriptor lives
    in the service layer, which must not import Discord views, and what a
    request *looks* like is the product's business either way. Everything here
    is a thin adapter onto a builder that already existed for the card.

    ``name_fn`` resolves the requester (and the resolving mod) to a display
    name. It is threaded in rather than looked up inside because resolution
    needs an async prefetch and these adapters are called synchronously; the
    default keeps a caller that has none rendering exactly as before. A card
    in a *channel* must always pass a real one — see
    :func:`post_approval_card`.

    ``art`` is the guild's thin card strips, for a card in the approvals
    channel only; the board's ephemeral picker passes none.
    """

    def theme_layout(settings, row) -> discord.ui.LayoutView:
        return theme_views.render_theme_review_card(
            None, settings, row, name_fn=name_fn, art=art
        )

    def pin_layout(settings, row) -> discord.ui.LayoutView:
        return pin_views.build_pin_review_view(settings, row, name_fn=name_fn, art=art)

    return {
        "theme": theme_layout,
        "pin": pin_layout,
    }


#: Queue keys this build can draw a card for. Read off the registry so the two
#: can't drift, and checked before any work: a select value naming a product
#: this build doesn't know is a shrug, not a crash.
RENDERABLE_KINDS = frozenset(_renderers())


def option_text(row: dict[str, Any], settings: EconSettings) -> tuple[str, str]:
    """``(label, description)`` for one request's row in the select.

    The label names the member and the queue, because one select covers several
    products and "Alex" alone doesn't say what they bought. The description
    carries what they actually wrote and what it cost — the two things a mod
    weighs before opening it. Both are clipped to Discord's 100.
    """
    who = str(row.get("requester_name") or "").strip() or "someone"
    label = f"{who} — {approval_label(str(row['kind']))}"
    if len(label) > 100:
        label = label[:99] + "…"
    price = int(row.get("price") or 0)
    unit = settings.currency_name if abs(price) == 1 else (settings.currency_plural or "coins")
    summary = " ".join(str(row.get("summary") or "").split())
    desc = f"{price:,} {unit} · {summary}" if summary else f"{price:,} {unit}"
    if len(desc) > 100:
        desc = desc[:99] + "…"
    return label, desc


class ApprovalPickSelect(discord.ui.Select):
    """Ephemeral picker listing every paid request waiting on a moderator."""

    def __init__(self, rows: list[dict[str, Any]], settings: EconSettings) -> None:
        options = []
        for row in rows[:25]:  # Discord caps a select at 25 options
            label, desc = option_text(row, settings)
            # The value carries the queue as well as the id: the ids are
            # per-table, so theme #3 and pin #3 are different requests and an
            # id alone would open whichever product happened to be asked.
            options.append(
                discord.SelectOption(
                    label=label,
                    value=f"{row['kind']}:{row['id']}",
                    description=desc or None,
                )
            )
        super().__init__(
            placeholder="Pick a request to review…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        kind, _, raw_id = str(self.values[0]).partition(":")
        bot = cast("Bot", interaction.client)
        guild = interaction.guild
        if guild is None:
            await _safe_ephemeral(interaction, "❌ This only works in a server.")
            return
        if kind not in QUEUES_BY_KEY or kind not in RENDERABLE_KINDS:
            await _safe_ephemeral(interaction, "❌ That request no longer exists.")
            return
        submission_id = int(raw_id)

        def _load():
            with bot.ctx.open_db() as conn:
                row = get_approval_row(conn, kind, submission_id)
                if row is None:
                    return None
                return load_econ_settings(conn, guild.id), row

        try:
            loaded = await asyncio.to_thread(_load)
        except Exception:
            log.exception("econ approvals: failed to load %s %s", kind, submission_id)
            await _safe_ephemeral(
                interaction, "❌ Couldn't load that request — try again."
            )
            return
        if loaded is None:
            await _safe_ephemeral(interaction, "❌ That request no longer exists.")
            return
        settings, row = loaded

        renderer = _renderers(await card_name_fn(bot.ctx, guild, row))[kind]
        # The row's true state, so a request somebody resolved while this
        # picker was open renders as resolved, with no buttons, rather than
        # offering a decision already made — the builder itself is
        # state-dependent (see the ``_Renderer`` alias above).
        rendered = renderer(settings, row)
        try:
            await interaction.response.edit_message(
                content=None, embed=None, attachments=[], view=rendered
            )
        except discord.HTTPException:
            log.debug("econ approvals: failed to open request detail", exc_info=True)


async def open_approvals_picker(interaction: discord.Interaction) -> None:
    """The todo board's Approvals button: gate, then offer what's waiting.

    Gated on ``can_manage_economy`` rather than the board's own moderator
    check — every decision here moves real currency (a denial refunds it) — so
    it stays the economy's gate even though the button sits on the todo board.
    That is the same call the sign-off picker made, and in the main guild the
    two roles are the same one.
    """
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return

    def _load() -> tuple[EconSettings, list[dict[str, Any]]]:
        with bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild.id)
            rows = pending_approvals(conn, guild.id, limit=25)
        return settings, rows

    try:
        settings, rows = await asyncio.to_thread(_load)
    except Exception:
        log.exception("econ approvals: failed to load the pending requests")
        await _safe_ephemeral(
            interaction, "❌ Couldn't load the requests — try again."
        )
        return

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    if not rows:
        await _safe_ephemeral(interaction, "No paid requests are waiting. ✨")
        return

    for row in rows:
        requester = guild.get_member(int(row["user_id"]))
        row["requester_name"] = requester.display_name if requester else "someone"

    view = discord.ui.View(timeout=_PICKER_TIMEOUT)
    view.add_item(ApprovalPickSelect(rows, settings))
    await interaction.response.send_message(
        "Which request do you want to review?", view=view, ephemeral=True
    )


refresh_approvals_board = partial(refresh_todo_board, log_label="econ approvals")


# ---------------------------------------------------------------------------
# Posting a card into the review channel
# ---------------------------------------------------------------------------


async def post_approval_card(
    bot: Bot,
    guild: discord.Guild,
    settings: EconSettings,
    kind: str,
    submission_id: int,
) -> None:
    """Post one paid request's review card to the approvals channel, best effort.

    The channel surface the board replaced, done properly: a **dedicated,
    staff-only** ``approvals_channel_id`` rather than the member-facing bank
    channel these cards used to land in. Unset ⇒ this returns immediately and
    the board stays the only surface, which is how the dial ships dark.

    One poster for every product. The embed and the buttons come from
    the same ``_renderers()`` registry the board's picker uses, so a mod reads
    an identical card whichever surface they are on, and neither surface owns
    a copy of a product's review.

    The member has already paid and the pending row already exists by the time
    this runs, so **nothing here may raise back at them** — a missing channel,
    a forbidden send or a Discord hiccup leaves the request on the board and
    is logged, not surfaced. The card's location is recorded last: the ledger
    is what lets a resolution on one surface repaint the other, and a card
    with no recorded location simply behaves like one posted before the dial
    was set.
    """
    channel_id = int(getattr(settings, "approvals_channel_id", 0) or 0)
    if not channel_id:
        return
    if kind not in RENDERABLE_KINDS:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning(
            "econ approvals: review channel %s missing in guild %s",
            channel_id, guild.id,
        )
        return

    def _read():
        with bot.ctx.open_db() as conn:
            return get_approval_row(conn, kind, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:
        log.exception("econ approvals: failed to read %s %s", kind, submission_id)
        return
    if row is None:
        return

    renderer = _renderers(
        await card_name_fn(bot.ctx, guild, row), await read_card_art(bot.ctx, guild)
    )[kind]
    rendered = renderer(settings, row)
    # The people who can actually press Approve — ``can_manage_economy`` is
    # the gate on both surfaces — rather than a second role to keep in sync.
    ping = role_ping_kwargs([settings.manager_role_id])
    try:
        posted = await channel.send(view=rendered, **ping)
    except discord.HTTPException:
        log.warning(
            "econ approvals: failed to post the %s card for %s", kind, submission_id
        )
        return
    except Exception:
        log.exception("econ approvals: unexpected error posting %s %s", kind, submission_id)
        return

    def _record() -> None:
        with bot.ctx.open_db() as conn:
            set_approval_card(conn, kind, submission_id, channel.id, posted.id)

    try:
        await asyncio.to_thread(_record)
    except sqlite3.Error:
        log.debug("econ approvals: failed to record card ids", exc_info=True)


async def close_expired_card(
    bot: Bot,
    guild: discord.Guild,
    settings: EconSettings,
    kind: str,
    submission_id: int,
) -> None:
    """Repaint an expired request's card so it stops offering a decision.

    The hourly sweep refunds requests nobody reviewed in time, and until now
    nothing told their card. That was invisible while the cards lived only on
    the board — the board reads the queue live, so an expired row simply
    vanishes from it — but a *channel* card is a message: left alone it sits
    there indefinitely showing Approve and Decline for a request that has
    already been refunded. The buttons are safe (the ``state = from_state``
    guard rejects them) but they read as broken, and a mod pressing one has
    reasonably concluded the queue is lying to them.

    Best effort, and deliberately silent about a card that was never posted:
    every request submitted while the channel dial was unset has no location
    recorded, which is not an error.
    """
    if kind not in RENDERABLE_KINDS or not submission_id:
        return

    def _read():
        with bot.ctx.open_db() as conn:
            return get_approval_row(conn, kind, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except sqlite3.Error:
        log.debug("econ approvals: failed to read expired %s", submission_id, exc_info=True)
        return
    if row is None:
        return
    channel_id, message_id = card_location(row)
    if not channel_id or not message_id:
        return

    renderer = _renderers(
        await card_name_fn(bot.ctx, guild, row), await read_card_art(bot.ctx, guild)
    )[kind]
    # Row is already expired/resolved by the time this sweep runs, so the
    # builder itself renders with no buttons — nothing more to clear.
    rendered = renderer(settings, row)
    try:
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        message = await channel.fetch_message(message_id)
        await message.edit(content=None, embed=None, attachments=[], view=rendered)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        log.debug("econ approvals: failed to close expired card", exc_info=True)
    except Exception:  # noqa: BLE001 — pragma: no cover - defensive fallback below the specific discord catch; repainting a stale card must never raise
        log.warning("econ approvals: unexpected error closing expired card")
