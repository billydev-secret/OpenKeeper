"""The one database read behind a cloud: which channels were busiest.

The words themselves never come from here. The bot stores no message text, so
every cloud reads recent history straight off Discord (capped at
:data:`logic.LIVE_FETCH_MAX`, storing nothing). What the database still has is
*when* each message happened, and that is enough to pick which channels an
"everywhere" cloud should read first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence


def recent_channel_ids(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_ids: Sequence[int],
    limit: int,
) -> list[int]:
    """Pick the ``limit`` most recently active of ``channel_ids``.

    Bounds the live-fetch fan-out when a moderator asks for every channel at
    once: without it, "everywhere" in a large guild is one Discord round trip
    per channel. Ranking uses ``messages`` timestamps, which record *that* a
    message happened without what it said.

    Falls back to the caller's own order for channels the table has never
    seen, so a brand-new channel is not silently unreachable.

    Every row counts, bot-authored and deleted alike: this ranks rooms by when
    they last saw traffic, and dropping rows would rank a busy room as dead.
    """
    if not channel_ids or limit <= 0:
        return []

    ph = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"SELECT channel_id, MAX(ts) FROM messages "
        f"WHERE guild_id = ? AND channel_id IN ({ph}) "
        f"GROUP BY channel_id ORDER BY MAX(ts) DESC LIMIT ?",
        [guild_id, *channel_ids, limit],
    ).fetchall()
    ranked = [int(r[0]) for r in rows]

    if len(ranked) < limit:
        seen = set(ranked)
        for cid in channel_ids:
            if len(ranked) >= limit:
                break
            if cid not in seen:
                ranked.append(int(cid))
                seen.add(cid)
    return ranked
