"""The card-strip art for a guild, read once when a public card is built.

``core.layout.card_strips`` turns it into components; this is the read, shared
by every card that wears the thin strips (Ben, 2026-09-30), so each sender
doesn't open its own connection to ``branding_config``.
"""

from __future__ import annotations

import asyncio
import logging

from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn

log = logging.getLogger("dungeonkeeper.card_art")

NO_ART = PanelArt(None, None)


async def read_card_art(ctx, guild) -> PanelArt:
    """The guild's panel art for a public card, or :data:`NO_ART` outside a
    guild. ``ctx`` is the app context (``bot.ctx``); ``guild`` is anything
    with an ``id``.

    **Never raises.** The strips are decoration read on the way to a post
    that has already been decided — a bounty, a refund notice, a staff
    review — so a failed read falls back to the plain card, never a lost
    one.

    **The same art in an age-gated channel, unspoilered**, as a lobby's:
    spoilers are for member-submitted media, and this is the server's own
    decoration (Ben, 2026-09-28). Read off the loop; a missing table reads
    as no art (``get_branding_conn``).
    """
    if ctx is None or guild is None:
        return NO_ART

    def _read() -> PanelArt:
        with ctx.open_db() as conn:
            return resolve_panel_art_conn(conn, guild.id)

    try:
        return await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001 — decoration must never cost the post it frames (docstring)
        log.debug("card art: read failed for guild %s", guild.id, exc_info=True)
        return NO_ART
