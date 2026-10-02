"""Sending batched DMs: the handler registry and the flush.

A feature joins by registering one handler for its ``kind`` (normally in its
cog's ``cog_load``) and calling :func:`bot_modules.dm.batch.store.enqueue`
where it used to send. The handler receives a whole batch, re-checks whatever
its own switches say, and sends one DM. What every kind gets for free:

- the no-contact list is consulted again at send time (``take_due``), so a
  pair added while a row waited drops that actor's rows silently;
- a recipient who has left the server gets nothing;
- a deleted ``ref_id`` message is retracted before it is ever announced
  (``dm_batch_cog``);
- ``send_branded_dm`` still applies Take a Break and closed-DM handling.

A batch whose kind has no handler (its cog failed to load) is dropped, not
kept: a notification ten minutes late is already the promise's limit.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from bot_modules.dm.batch.logic import PendingDM, drop_blocked
from bot_modules.dm.batch.store import claim_due
from bot_modules.no_contact.service import is_no_contact_conn

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.dm_batch")

Handler = Callable[["Bot", discord.Guild, discord.Member, list[PendingDM]], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {}
#: Kinds whose ``actor_id`` is a member the DM *names* rather than one who
#: acted on the recipient (Whisper's guessed member). The column still makes
#: the row erasable with that member, but ``take_due`` leaves it alone: the
#: handler degrades the name itself, as the feature's own rules say.
_NO_ACTOR_RECHECK: set[str] = set()


def register_handler(kind: str, handler: Handler, *, recheck_actor: bool = True) -> None:
    _HANDLERS[kind] = handler
    if recheck_actor:
        _NO_ACTOR_RECHECK.discard(kind)
    else:
        _NO_ACTOR_RECHECK.add(kind)


def unregister_handler(kind: str) -> None:
    _HANDLERS.pop(kind, None)
    _NO_ACTOR_RECHECK.discard(kind)


def handler_for(kind: str) -> Handler | None:
    return _HANDLERS.get(kind)


def take_due(conn: sqlite3.Connection, *, now: int) -> list[list[PendingDM]]:
    """Claim the due batches and drop rows the no-contact list now forbids.

    A batch left empty by that filter is gone entirely — the recipient hears
    nothing, exactly as if those replies had never been made.
    """
    batches = claim_due(conn, now=now)
    pairs = {
        (r.guild_id, r.user_id, r.actor_id)
        for batch in batches
        for r in batch
        if r.actor_id and r.kind not in _NO_ACTOR_RECHECK
    }
    blocked = {p for p in pairs if is_no_contact_conn(conn, *p)}
    kept = [
        batch if batch[0].kind in _NO_ACTOR_RECHECK else drop_blocked(batch, blocked)
        for batch in batches
    ]
    return [batch for batch in kept if batch]


async def flush_due(bot: Bot, *, now: int | None = None) -> int:
    """Send every due batch. Returns how many batches reached a handler."""
    stamp = int(time.time()) if now is None else now

    def _take() -> list[list[PendingDM]]:
        with bot.ctx.open_db() as conn:
            return take_due(conn, now=stamp)

    batches = await asyncio.to_thread(_take)
    sent = 0
    for batch in batches:
        head = batch[0]
        handler = handler_for(head.kind)
        if handler is None:
            log.warning("dm_batch: no handler for kind %r; dropped %d row(s)", head.kind, len(batch))
            continue
        guild = bot.get_guild(head.guild_id)
        if guild is None:
            continue
        member = guild.get_member(head.user_id)
        if member is None:
            continue  # left the server: the news is about a place they're not in
        try:
            await handler(bot, guild, member, batch)
            sent += 1
        except Exception:
            log.exception("dm_batch: %s handler failed for guild %s", head.kind, head.guild_id)
    return sent
