"""``dm_batch_pending`` — notifications waiting to be sent as one DM.

Per-user data (``docs/data_register.md``): a row names the recipient
(``user_id``) and the member whose action it reports (``actor_id``). It lives
until its batch is sent — ``WINDOW_SECONDS`` plus any restart downtime — and
``purge_user_data`` deletes rows naming the member on either side.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from bot_modules.dm.batch.logic import (
    WINDOW_SECONDS,
    PendingDM,
    decode_payload,
    encode_payload,
    group_batches,
)


def ensure_table(conn: sqlite3.Connection) -> None:
    """Mirror of migration 243, for callers that build the schema by hand."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_batch_pending (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT    NOT NULL,
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            group_key  INTEGER NOT NULL DEFAULT 0,
            actor_id   INTEGER NOT NULL DEFAULT 0,
            ref_id     INTEGER NOT NULL DEFAULT 0,
            payload    TEXT    NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dm_batch_pending_batch "
        "ON dm_batch_pending (kind, guild_id, user_id, group_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dm_batch_pending_ref "
        "ON dm_batch_pending (guild_id, ref_id)"
    )


def enqueue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    guild_id: int,
    user_id: int,
    group_key: int = 0,
    actor_id: int = 0,
    ref_id: int = 0,
    payload: dict[str, Any] | None = None,
    now: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO dm_batch_pending "
        "(kind, guild_id, user_id, group_key, actor_id, ref_id, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind, guild_id, user_id, group_key, actor_id, ref_id,
            encode_payload(payload), int(time.time()) if now is None else now,
        ),
    )


def _row(r: sqlite3.Row | tuple[Any, ...]) -> PendingDM:
    return PendingDM(
        id=int(r[0]), kind=str(r[1]), guild_id=int(r[2]), user_id=int(r[3]),
        group_key=int(r[4]), actor_id=int(r[5]), ref_id=int(r[6]),
        payload=decode_payload(r[7]), created_at=int(r[8]),
    )


def claim_due(
    conn: sqlite3.Connection, *, now: int, window: int = WINDOW_SECONDS
) -> list[list[PendingDM]]:
    """Take every batch whose oldest row has waited ``window`` seconds.

    The rows are deleted as they are read: a batch is sent at most once, and a
    crash between claim and send loses that one DM rather than repeating it.
    A batch still inside its window is left whole, including rows that arrived
    after it opened.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.kind, p.guild_id, p.user_id, p.group_key, p.actor_id,
               p.ref_id, p.payload, p.created_at
        FROM dm_batch_pending p
        JOIN (
            SELECT kind, guild_id, user_id, group_key
            FROM dm_batch_pending
            GROUP BY kind, guild_id, user_id, group_key
            HAVING MIN(created_at) <= ?
        ) due USING (kind, guild_id, user_id, group_key)
        """,
        (now - window,),
    ).fetchall()
    if not rows:
        return []
    ids = [int(r[0]) for r in rows]
    conn.executemany("DELETE FROM dm_batch_pending WHERE id = ?", [(i,) for i in ids])
    return group_batches([_row(r) for r in rows])


def retract_refs(conn: sqlite3.Connection, guild_id: int, ref_ids: list[int]) -> int:
    """A deleted message takes its waiting notification with it."""
    ids = [i for i in ref_ids if i]
    if not ids:
        return 0
    cur = conn.executemany(
        "DELETE FROM dm_batch_pending WHERE guild_id = ? AND ref_id = ?",
        [(guild_id, i) for i in ids],
    )
    return cur.rowcount


def drop_group(
    conn: sqlite3.Connection, *, kind: str, guild_id: int, group_key: int
) -> int:
    """Everything waiting about one thing (a deleted thread), for every recipient."""
    cur = conn.execute(
        "DELETE FROM dm_batch_pending WHERE kind = ? AND guild_id = ? AND group_key = ?",
        (kind, guild_id, group_key),
    )
    return cur.rowcount


def pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM dm_batch_pending").fetchone()[0])
