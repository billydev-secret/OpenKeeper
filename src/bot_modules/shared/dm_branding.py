"""Per-guild branding for direct messages.

A DM has no guild context of its own. Discord gives the bot exactly one
username and one avatar globally, so a member who shares two servers with
us sees the identical sender either way — that ceiling is not something
code can move. What *can* carry the guild is the message body: the embed
accent, and an attribution line naming the server (with its icon).

Before this module every DM site solved that alone, and inconsistently:
``dm_perms`` named the guild in eight places but never passed an accent,
while the wellness DMs — a personal boundary tool, where "which server is
nudging me?" is the whole question — named no guild at all. Three
near-identical ``_try_dm`` helpers had accumulated alongside.

Two entry points, deliberately split:

* :func:`brand_dm_embed` is pure and synchronous. It stamps an accent and
  an attribution onto an embed and returns it. Callers that already own a
  delivery policy — ``economy_service.notify_member`` has mute prefs, an
  opt-in role gate, and a bank-channel fallback — brand with this and keep
  their own send path intact.
* :func:`send_branded_dm` is the common case: resolve the accent from the
  guild's branding config, brand, send, swallow a closed DM.

:func:`brand_dm_layout` is the pure half again for a Components V2 card,
which has no embed and no footer to stamp: the icon becomes a Section
accessory and the server name a ``-#`` subtext line.

Attribution defaults to the footer, not the author slot. Several DM embeds
already use the author line for something more informative than the server
name (``dm_perms`` puts the requesting member there), so claiming it by
default would cost more than it adds. Pass ``placement=ATTRIBUTION_AUTHOR``
where the guild genuinely is the most useful thing in the header.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal, Optional

import discord

from bot_modules.breaks.store import is_on_break
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.shared.embeds import DM_PRIMARY

log = logging.getLogger("dungeonkeeper.dm_branding")

ATTRIBUTION_FOOTER = "footer"
ATTRIBUTION_AUTHOR = "author"
ATTRIBUTION_NONE = "none"

Placement = Literal["footer", "author", "none"]

# Joins guild attribution to a footer the builder already set, matching the
# separator used elsewhere in the bot's embed footers.
_FOOTER_SEP = " • "


def brand_dm_embed(
    embed: discord.Embed,
    *,
    guild_name: Optional[str] = None,
    guild_icon_url: Optional[str] = None,
    color: Optional[discord.Color] = None,
    placement: Placement = ATTRIBUTION_FOOTER,
    keep_color: bool = False,
) -> discord.Embed:
    """Stamp ``embed`` with the guild accent and an attribution line.

    Pure: mutates and returns the embed passed in, performs no IO. ``color``
    falls back to :data:`DM_PRIMARY` so an unbranded guild keeps the look DM
    embeds have today rather than going Discord-default grey.

    ``keep_color`` leaves an already-set color alone and applies attribution
    only. Use it where the color carries meaning rather than branding — a
    release-from-hold notice is green because it is *good news*, and
    CLAUDE.md keeps semantic colors out of the accent's reach.

    With ``guild_name`` unset (the bot was kicked, or the caller has no
    guild in hand) the accent is still applied and attribution is skipped —
    a DM with no server name beats no DM at all.
    """
    if not (keep_color and embed.color is not None):
        embed.color = color if color is not None else discord.Color(DM_PRIMARY)

    if not guild_name or placement == ATTRIBUTION_NONE:
        return embed

    if placement == ATTRIBUTION_AUTHOR:
        embed.set_author(name=guild_name, icon_url=guild_icon_url)
        return embed

    # Footer: preserve whatever the builder already put there. Its text is
    # feature-specific ("DM relationships are logged…") and outranks ours,
    # so the server name trails it rather than replacing it.
    existing = (embed.footer.text or "").strip() if embed.footer else ""
    text = f"{existing}{_FOOTER_SEP}{guild_name}" if existing else guild_name
    embed.set_footer(text=text, icon_url=guild_icon_url)
    return embed


def guild_display_name(guild: Optional[discord.Guild]) -> Optional[str]:
    """The guild's name, or None when it has none to give.

    Same tolerance as :func:`guild_icon_url` — attribution is a nicety and
    must never be the reason a DM fails to send.
    """
    return getattr(guild, "name", None) if guild is not None else None


def guild_icon_url(guild: Optional[discord.Guild]) -> Optional[str]:
    """The guild's icon URL, or None for a guild with no icon set.

    Tolerates a guild object that does not carry an ``icon`` at all: the
    attribution text is the part that matters, and a missing icon must not
    be the reason a DM fails to send.
    """
    icon = getattr(guild, "icon", None) if guild is not None else None
    return icon.url if icon is not None else None


async def resolve_dm_accent(
    db_path: Optional[Path], guild: Optional[discord.Guild]
) -> discord.Color:
    """Accent for a DM sent on ``guild``'s behalf, or the DM default.

    Never raises. Resolving an accent can touch the database and, in avatar
    mode, fetch the bot avatar over HTTP — and this now sits on the path of
    DMs that matter a great deal more than their color does (a wellness
    friction notice is withheld unless its DM lands; whisper rolls back a
    stored row). A branding failure degrades to the default rather than
    costing the member the message.
    """
    # CancelledError derives from BaseException, so the shared helper's
    # ``except Exception`` lets a cancellation through untouched — which is
    # what the hand-rolled re-raise here used to spell out.
    return await safe_resolve_accent(
        db_path, guild, default=discord.Color(DM_PRIMARY), log_label="dm_branding"
    )


def brand_dm_layout(
    layout: discord.ui.LayoutView,
    *,
    guild_name: Optional[str] = None,
    guild_icon_url: Optional[str] = None,
) -> discord.ui.LayoutView:
    """Stamp guild attribution onto a Components V2 card bound for a DM.

    The V2 counterpart of :func:`brand_dm_embed`, and pure in the same way:
    it mutates the view it is handed and returns it, touching no database
    and sending nothing. A V2 message carries neither an embed nor a
    footer, so the two halves of the attribution move to components —
    the icon becomes a ``Thumbnail`` accessory and the server name a
    ``-#`` subtext line at the bottom of the card, which is where the
    footer used to read.

    The icon lands only when the card *opens* with a bare ``TextDisplay``:
    that gets wrapped in a ``Section`` whose accessory is the guild icon.
    A card that already opens with a ``Section`` is left alone, because a
    Section's accessory is mandatory and the builder already chose it —
    claiming it here would silently delete that button or thumbnail. Such
    a card still gets the name line, which is the half that carries the
    information.

    Budget note for the caller: the wrap costs two components (the Section
    and its Thumbnail) and the name line one, so a card built right up
    against the 40-component ceiling should leave three spare before it is
    handed to :func:`send_branded_dm`.
    """
    if guild_icon_url:
        children = list(layout.children)
        if children and isinstance(children[0], discord.ui.TextDisplay):
            wrapped = discord.ui.Section(
                children[0], accessory=discord.ui.Thumbnail(guild_icon_url)
            )
            layout.clear_items()
            layout.add_item(wrapped)
            for child in children[1:]:
                layout.add_item(child)
    if guild_name:
        layout.add_item(discord.ui.TextDisplay(f"-# From {guild_name}"))
    return layout


async def _recipient_on_break(
    user: discord.abc.Messageable, db_path: Optional[Path], guild: Optional[discord.Guild]
) -> bool:
    user_id = getattr(user, "id", None)
    guild_id = getattr(guild, "id", None)
    if db_path is None or not isinstance(guild_id, int) or not isinstance(user_id, int):
        return False
    try:
        return await asyncio.to_thread(is_on_break, db_path, guild_id, user_id)
    except sqlite3.Error:
        # The mute is a courtesy, not a safety gate: a DB fault delivers.
        log.warning("dm_branding: break check failed for %s; delivering", user_id)
        return False


async def send_branded_dm(
    user: discord.abc.Messageable,
    *,
    db_path: Optional[Path],
    guild: Optional[discord.Guild],
    embed: Optional[discord.Embed] = None,
    content: Optional[str] = None,
    layout: Optional[discord.ui.LayoutView] = None,
    placement: Placement = ATTRIBUTION_FOOTER,
    keep_color: bool = False,
    bypass_break: bool = False,
    **send_kwargs: Any,
) -> Optional[discord.Message]:
    """Brand ``embed`` for ``guild`` and DM it to ``user``.

    Returns the sent message, or None when the DM could not be delivered —
    a closed DM is an ordinary outcome here, not an error, so callers that
    only care whether it landed can test the result for None. Callers that
    must roll back on failure (whisper deletes its row) get the same signal.

    ``content`` is passed through untouched; branding only ever applies to
    the embed. Extra keyword arguments (``view``, ``allowed_mentions``,
    ``file``) reach ``user.send`` unchanged.

    ``layout`` is the Components V2 alternative: the card is the whole
    message, so it is exclusive with ``embed``, ``content`` and ``view``
    (Discord drops all three from a V2 message) and is branded by
    :func:`brand_dm_layout` instead. ``placement`` and ``keep_color`` have
    nothing to place or keep on a V2 card and are ignored for one; the
    accent is not resolved at all, which also spares a DM that carries no
    embed the database read and the avatar fetch behind it.

    A recipient **on a Take a Break** in ``guild`` is dropped and gets None,
    the same answer as a closed DM, so nothing can tell a break from one.
    Callers whose None path would ping or post publicly check
    :func:`bot_modules.breaks.store.is_on_break` up front instead.
    ``bypass_break=True`` is for the DMs that must still arrive: a receipt
    for the member's own action, a moderation notice, a staff-only DM.
    """
    if not bypass_break and await _recipient_on_break(user, db_path, guild):
        return None
    if layout is not None:
        if embed is not None or content:
            raise ValueError(
                "a layout DM carries no embed and no content — a V2 message "
                "has neither"
            )
        if "view" in send_kwargs:
            raise ValueError("a layout is its own view; don't pass view= too")
        brand_dm_layout(
            layout,
            guild_name=guild_display_name(guild),
            guild_icon_url=guild_icon_url(guild),
        )
        try:
            return await user.send(view=layout, **send_kwargs)
        except (discord.Forbidden, discord.HTTPException):
            return None

    if embed is not None:
        accent = await resolve_dm_accent(db_path, guild)
        brand_dm_embed(
            embed,
            guild_name=guild_display_name(guild),
            guild_icon_url=guild_icon_url(guild),
            color=accent,
            placement=placement,
            keep_color=keep_color,
        )

    kwargs: dict[str, Any] = dict(send_kwargs)
    if content:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed

    try:
        return await user.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return None
