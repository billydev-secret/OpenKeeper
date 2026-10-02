"""Shared helpers for the economy card/view modules (bounty, theme,
pin, auction, …).

These are trivial but were copy-pasted into every view module; centralizing
keeps the currency vocabulary and the "reply without blowing up" behavior in one
place so a change lands everywhere at once.

``edit_review_card_layout``/``edit_stored_card_layout``/
``refresh_review_card_layout`` re-render a paid-submission review card in
place through a caller's ``build_layout``, which returns a ready Components
V2 ``LayoutView``. Theme and pin both bind through this trio (theme's own
2026-09-28 conversion was the last caller of an earlier embed-flavored trio
of the same three mechanics, since removed).
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.shared.name_resolver import NameFn, mention

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt
    from bot_modules.economy.service import EconSettings

log = logging.getLogger("dungeonkeeper.economy")


def unit(settings: EconSettings, amount: int) -> str:
    """Currency name matching ``amount``'s grammatical number.

    Note the deliberate difference from ``coins`` below: this returns the
    configured plural verbatim, including an empty one. Callers that render
    the bare unit (the wallet header, quest rewards) have always shown
    whatever the guild configured; ``coins`` substitutes a literal fallback.
    Don't "unify" the two without deciding which behaviour a guild with an
    empty ``currency_plural`` should get.
    """
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def coins(settings: EconSettings, amount: int) -> str:
    """``🪙 **250** coins`` — the currency vocabulary every economy card uses."""
    unit = (
        settings.currency_name
        if abs(amount) == 1
        else (settings.currency_plural or "coins")
    )
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ view")


class EphemeralCard:
    """Stands in for a review card when the review happened on the todo board.

    The board's Approve/Decline buttons live under an *ephemeral* detail
    message. That message is real and arrives at a resolver exactly like a
    channel card would, but an ephemeral message cannot be edited through the
    channel-message endpoint — only through the interaction that owns it.
    Wrapping the interaction in the one method every card path calls keeps the
    resolution code identical either way, instead of sprouting a
    "which surface am I on?" branch at each of its half-dozen repaints.
    """

    __slots__ = ("_interaction",)

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction

    async def edit(self, **kwargs) -> None:
        await self._interaction.edit_original_response(**kwargs)


def review_surface(
    interaction: discord.Interaction, card: "discord.Message | None"
) -> "discord.Message | EphemeralCard | None":
    """The thing to repaint: the channel card, or the board's ephemeral detail.

    ``card`` is whatever the resolver was handed — ``interaction.message`` for
    a button, or the message the deny modal was opened from. An ephemeral one
    means the board flow; see :class:`EphemeralCard`.
    """
    if card is not None and getattr(card.flags, "ephemeral", False):
        return EphemeralCard(interaction)
    return card


async def surface_art(ctx, surface) -> PanelArt | None:
    """The card strips for a review card on ``surface``, or ``None``.

    Only the copy in the staff approvals channel — a real ``Message`` in a
    guild — wears them (Ben, 2026-09-30). The todo board's ephemeral detail
    (:class:`EphemeralCard`) is one mod's working surface and stays plain,
    as does a surface with no guild or a caller with no ``ctx`` to read with.
    """
    from bot_modules.core.card_art import read_card_art  # noqa: PLC0415

    if ctx is None or surface is None or isinstance(surface, EphemeralCard):
        return None
    guild = getattr(surface, "guild", None)
    if guild is None:
        return None
    return await read_card_art(ctx, guild)


async def refresh_todo_board(
    bot: "discord.Client", guild_id: int, *, log_label: str
) -> None:
    """Repaint the mods' todo board, where everything waiting on them is listed.

    Called on both edges — a request filed and a request resolved — because
    the board's own 60s loop only repaints guilds where a recurring chore
    spawned, so without this a paid request would sit invisible until
    something else happened to move the board. Best effort in the strongest
    sense: by the time this runs the money has already moved and the member
    has already been told, so a Discord hiccup must never surface as a failed
    submission or a failed refund.
    """
    # ``bot`` is annotated as the bare Client because an expiry sweep holds
    # one; only a commands.Bot carries cogs, which the runtime one always is.
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog("TodoCog") if get_cog is not None else None
    refresh = getattr(cog, "refresh_board", None)
    if refresh is None:
        return
    try:
        await refresh(guild_id)
    except Exception:  # noqa: BLE001 — pragma: no cover - defensive; the board's refresh internals aren't visible here and a Discord hiccup must never surface as a failed submission/refund (see docstring)
        log.warning(
            "%s: failed to repaint the todo board for %s.", log_label, guild_id
        )


async def card_name_fn(
    ctx, guild, row, *extra_ids: int, guild_id: int | None = None
) -> NameFn:
    """A resolver covering everyone a paid-request card names.

    That is the requester (``row['user_id']``) plus whichever mod resolved it,
    prefetched in one batch. Embed mentions are resolved by the *reading*
    client from its own cache, so a card that emits ``<@id>`` shows a bare
    number to any mod who has never seen that member — and a paid request is
    often the first time they meet them. A member who has since left is still
    named, from ``known_users``.

    Cheap to call: ``build_name_fn`` queries only for ids the live member
    cache misses, so the usual case costs no I/O at all.

    ``guild`` may be None — the dashboard resolves a submission for a guild the
    bot may not currently see — in which case pass ``guild_id`` so the
    ``known_users`` lookup still has a guild to scope to. With neither, every
    id falls through to ``<@id>``, which is the old behaviour and no worse.

    **Never raises.** This is called from repaint paths that run *after* the
    money has already moved — a resolution the dashboard has committed, a card
    the picker is about to render — and it is the only I/O those paths grew.
    A database hiccup here must degrade to ``<@id>`` (the pre-resolver
    rendering), not abandon the repaint, skip the member's DM, or turn a
    committed resolution into a 500.
    """
    from bot_modules.shared.name_resolver import (  # noqa: PLC0415
        build_name_fn,
    )

    def _column(key: str):
        # Kinds of paid request carry different columns; a missing one is None.
        try:
            return row[key] if row is not None else None
        except (IndexError, KeyError, TypeError):
            return None

    ids = [int(row["user_id"] or 0)] if row is not None else []
    resolver_id = _column("resolver_id")
    if resolver_id:
        ids.append(int(resolver_id))
    ids.extend(int(i) for i in extra_ids if i)
    gid = int(guild.id) if guild is not None else int(guild_id or 0)
    try:
        return await build_name_fn(
            guild=guild, db_path=ctx.db_path, guild_id=gid, user_ids=ids
        )
    except Exception:  # noqa: BLE001 — docstring guarantee "Never raises"; runs after money has already moved, must degrade to <@id> rather than abandon the repaint
        log.warning(
            "econ view: name resolution failed for guild %s; rendering mentions",
            gid, exc_info=True,
        )
        return mention


async def edit_review_card_layout(
    card: "discord.Message | EphemeralCard | None",
    settings: "EconSettings",
    row,
    *,
    build_layout,
    log_label: str,
    name_fn: NameFn = mention,
    ctx=None,
) -> None:
    """Re-render a paid-submission review card in place, best effort.

    ``build_layout`` stays a caller's argument on purpose. Pins and
    themes each render a different card from different
    columns, and that copy belongs with the product it speaks for — only the
    edit-and-swallow mechanics are shared. Losing the card is not worth
    raising over: the row is already resolved and the member has already
    been told.

    ``build_layout`` returns a ready ``discord.ui.LayoutView`` (buttons and
    all, or bare once resolved); the edit nulls out the message's embed slot
    the way every embed→layout conversion does (``content=None, embed=None,
    attachments=[]``), since a card resolved from a legacy message may still
    have started life as an embed.

    ``name_fn`` is handed to ``build_layout`` so the requester and the
    resolving mod render as names rather than ``<@id>``, which an embed or a
    layout alike leaves as a bare number for any reader whose client hasn't
    cached them. The default preserves the pre-resolver rendering for a
    caller that has no resolver to give.

    ``ctx`` lets the channel card keep its card strips through the repaint
    (:func:`surface_art`); the board's ephemeral never gets them.
    """
    if card is None:
        return
    art = await surface_art(ctx, card)
    try:
        await card.edit(
            content=None, embed=None, attachments=[],
            view=build_layout(settings, row, name_fn, art=art),
        )
    except discord.HTTPException:
        log.debug("%s: failed to edit card", log_label, exc_info=True)


async def edit_stored_card_layout(
    bot: "discord.Client",
    card: "discord.Message | EphemeralCard | None",
    settings: "EconSettings",
    row,
    *,
    build_layout,
    log_label: str,
    name_fn: NameFn = mention,
) -> None:
    """Repaint the request's card in the approvals channel, wherever it was resolved.

    The other half of "two surfaces, one ledger". A mod can resolve a paid
    request from the card itself, from the todo board's ephemeral Approvals
    detail, or from the dashboard — and the two they were *not* looking at
    must stop offering a decision that has already been made. The board is
    read live off the submission tables so it self-corrects; the channel card
    is a message, and only ``card_channel_id``/``card_message_id`` can find it
    again.

    Skipped when the caller already repainted that very message: ``card`` is
    the surface they were on, so a resolution from the channel card itself
    must not fetch and edit it a second time. An ephemeral surface never
    matches, which is exactly the case this exists for.

    Best effort throughout — the row is resolved and the member already told,
    so a deleted card or a lost permission is cosmetic, never an error.
    """
    from bot_modules.economy.approvals_service import (  # noqa: PLC0415
        card_location,
    )

    channel_id, message_id = card_location(row)
    if not channel_id or not message_id:
        return
    if int(getattr(card, "id", 0) or 0) == message_id:
        return
    try:
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        message = await channel.fetch_message(message_id)
        art = await surface_art(getattr(bot, "ctx", None), message)
        await message.edit(
            content=None, embed=None, attachments=[],
            view=build_layout(settings, row, name_fn, art=art),
        )
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        log.debug("%s: failed to edit the stored card", log_label, exc_info=True)
    except Exception:  # noqa: BLE001 — pragma: no cover - defensive fallback below the specific discord catch; the row is already resolved so the card is cosmetic, never an error
        log.warning("%s: unexpected error editing the stored card", log_label)


async def refresh_review_card_layout(
    card: "discord.Message | EphemeralCard | None",
    ctx,
    settings: "EconSettings",
    submission_id: int,
    *,
    read_row,
    build_layout,
    log_label: str,
    name_fn: NameFn = mention,
) -> None:
    """Reload a row that moved underneath its card and re-render it.

    The row can change without the card knowing: a mod resolves it from the
    dashboard, or two mods press buttons at once. Re-reading before the edit
    is what stops a card claiming "approved" after the row went the other
    way. A failed read leaves the card as it is rather than blanking it —
    stale beats wrong-and-confident.
    """
    if card is None:
        return

    def _read():
        with ctx.open_db() as conn:
            return read_row(conn, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001 — read_row is caller-supplied per-product; a failed read must leave the card as-is (stale beats wrong-and-confident), not raise
        log.debug("%s: failed to reload for refresh", log_label, exc_info=True)
        return
    if row is not None:
        await edit_review_card_layout(
            card, settings, row,
            build_layout=build_layout, log_label=log_label, name_fn=name_fn,
            ctx=ctx,
        )
