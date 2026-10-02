"""Needle reply-DM opt-ins — one row per member who asked to be DMed when
someone replies to a thread they started.

Opt-IN since migration 244 (2026-09-28): no row, no DM. Turning it off deletes
the row, so Off stores nothing. Per-user data (``docs/data_register.md``):
purged on erasure, otherwise kept until the member turns it off.
"""

from __future__ import annotations

import sqlite3
import time


def ensure_optins_table(conn: sqlite3.Connection) -> None:
    """Mirror of migration 244, for callers that build the schema by hand."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS needle_reply_optins (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)


def set_reply_optin(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int, on: bool
) -> None:
    """Switch a member's reply DMs on or off. Either press twice is harmless."""
    if on:
        conn.execute(
            "INSERT OR IGNORE INTO needle_reply_optins (guild_id, user_id, created_at) "
            "VALUES (?, ?, ?)",
            (guild_id, user_id, int(time.time())),
        )
    else:
        conn.execute(
            "DELETE FROM needle_reply_optins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


def is_reply_opted_in(conn: sqlite3.Connection, *, guild_id: int, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM needle_reply_optins WHERE guild_id = ? AND user_id = ? LIMIT 1",
        (guild_id, user_id),
    ).fetchone()
    return row is not None


def reply_dms_offered(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Does any auto-threaded channel in the guild send reply DMs?

    The Roles & Pings row is shown only when this is true: with every
    channel's dial off the switch would do nothing, and a switch that isn't
    enforced must not be offered.
    """
    row = conn.execute(
        "SELECT 1 FROM needle_channels WHERE guild_id = ? AND notify_op_on_reply = 1 LIMIT 1",
        (guild_id,),
    ).fetchone()
    return row is not None
