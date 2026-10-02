"""Batched economy notes — "X sent you coins", gifts, bounty refunds.

These are notes about someone else's action with nothing to press, so they
wait on ``dm_batch`` and a member hears once per window, however many
arrived (decided 2026-09-28, ``docs/plans/dm-batching.md`` stage 2). One kind
covers all three, grouped per recipient, so a burst of pays and a refund
share one DM.

Each row carries its line already written, minus the name: the member who
acted is the row's ``actor_id`` and is named at send time, so an erasure or
a no-contact pair added during the window takes the row, and the name is
never stored. Delivery still goes through ``notify_member`` — mute
preferences, closed DMs and the bank-channel fallback are unchanged.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import discord

from bot_modules.dm.batch.logic import PendingDM
from bot_modules.dm.batch.store import enqueue

NOTICE_BATCH_KIND = "econ_notice"

#: Lines past this are summarised, keeping the DM well inside Discord's
#: 2,000-character message limit.
MAX_LINES = 12


def queue_notice(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    recipient_id: int,
    line: str,
    actor_id: int = 0,
    named: bool = True,
) -> None:
    """Hold one economy note for ``recipient_id``.

    ``line`` is the note after the actor's name (``"sent you 🪙 5 coins."``),
    already mention-escaped by the caller. ``named=False`` is a note that
    names nobody but still has an actor to key on — the bounty poster behind a
    refund, whose no-contact pair with the contributor suppresses the note.
    """
    enqueue(
        conn,
        kind=NOTICE_BATCH_KIND,
        guild_id=guild_id,
        user_id=recipient_id,
        actor_id=actor_id,
        payload={"line": line, "named": int(named)},
    )


def render_notices(rows: list[PendingDM], name_fn: Callable[[int], str]) -> str:
    """The DM body for one recipient's batch: a line per note, oldest first."""
    lines: list[str] = []
    for r in rows:
        line = str(r.payload.get("line") or "")
        if not line:
            continue
        if r.payload.get("named") and r.actor_id:
            name = discord.utils.escape_mentions(name_fn(r.actor_id))
            line = f"{name} {line}"
        lines.append(line)
    if len(lines) > MAX_LINES:
        extra = len(lines) - (MAX_LINES - 1)
        lines = lines[: MAX_LINES - 1] + [f"…and {extra} more."]
    return "\n".join(lines)
