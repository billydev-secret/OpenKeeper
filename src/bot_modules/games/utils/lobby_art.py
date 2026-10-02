"""The game-lobby art for a channel, read once when a lobby is built.

``core.layout.lobby_art`` turns it into components; this is the read, shared
by every game whose lobby is a Components V2 card, so each cog doesn't open
its own connection to ``branding_config`` or make its own NSFW call.
"""

from __future__ import annotations

import asyncio

from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.panels.title_service import with_lobby_title

NO_ART = PanelArt(None, None)


async def read_lobby_art(bot, channel, title: str) -> PanelArt:
    """The guild's panel art for a lobby in ``channel``, or :data:`NO_ART`
    outside a guild, with ``title`` (the game's name) drawn into the header
    between its leaves when there is one (``with_lobby_title``).

    **The same art in an age-gated channel, unspoilered.** Spoilers are for
    member-submitted media (Ben, 2026-09-28: "we can use design elements
    without need spoilers. Spoilers are for user submitted nudity"); this is
    the server's own decoration, uploaded by an admin. Until then a lobby
    there stayed plain, which left Risky Rolls — played only in an
    age-gated channel — the one game that could never wear it.
    Read and drawn off the loop; a missing table reads as no art
    (``get_branding_conn``).
    """
    return await read_guild_lobby_art(bot, getattr(channel, "guild", None), title)


async def read_guild_lobby_art(bot, guild, title: str) -> PanelArt:
    """:func:`read_lobby_art` for a caller that holds a guild rather than a
    channel — a channel hub's sticky panel builds from its guild (Guess Who's
    prompt, 2026-09-30). :data:`NO_ART` when ``guild`` is ``None``."""
    if guild is None:
        return NO_ART

    def _read() -> PanelArt:
        with bot.ctx.open_db() as conn:
            art = resolve_panel_art_conn(conn, guild.id)
        # After the connection closes: a first draw reads every frame.
        return with_lobby_title(art, title)

    return await asyncio.to_thread(_read)

