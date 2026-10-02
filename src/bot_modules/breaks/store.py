"""Database access for self-service breaks (``break_holds``, migration 237).

A row existing **is** the break: it holds the roles to give back, and "I'm
back" deletes it. Nothing about a finished break is kept here — the audit log
records start and end.

Thin synchronous helpers; async callers wrap them in ``asyncio.to_thread``.
``is_on_break_conn`` is the one question every other feature asks (DM mute,
Grant Audit, the inactive sweep, the Guess and XP listeners), so it stays a
single indexed point lookup.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from bot_modules.breaks.logic import absorb_snapshot
from bot_modules.core.db_utils import get_config_int, get_config_value, open_db, set_config_value

#: Config keys (guild-scoped, no legacy fallback — this feature is new).
BREAK_ROLE_KEY = "break_role_id"
BREAK_CHANNEL_KEY = "break_channel_id"
SPICY_LITE_ROLE_KEY = "break_spicy_lite_role_id"
#: JSON list of role ids either dial named before it was re-pointed. A member
#: can still wear an old hold role with no row to say so (an erased row, a
#: hand-added role), and every "is this a break role?" question has to know it.
RETIRED_ROLES_KEY = "break_retired_role_ids"


@dataclass(frozen=True)
class BreakRow:
    guild_id: int
    user_id: int
    stored_roles: list[int]
    swapped_spicy: bool
    started_at: float
    #: The hold roles this break actually put on; 0 = none.
    break_role_id: int = 0
    lite_role_id: int = 0

    @property
    def granted_role_ids(self) -> frozenset[int]:
        return frozenset(r for r in (self.break_role_id, self.lite_role_id) if r > 0)


def _row(r: sqlite3.Row) -> BreakRow:
    return BreakRow(
        guild_id=int(r["guild_id"]),
        user_id=int(r["user_id"]),
        stored_roles=[int(x) for x in json.loads(r["stored_roles"] or "[]")],
        swapped_spicy=bool(r["swapped_spicy"]),
        started_at=float(r["started_at"]),
        break_role_id=int(r["break_role_id"] or 0),
        lite_role_id=int(r["lite_role_id"] or 0),
    )


def create_break(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    stored_roles: list[int],
    swapped_spicy: bool,
    break_role_id: int = 0,
    lite_role_id: int = 0,
) -> None:
    """Open a break. Raises ``sqlite3.IntegrityError`` if one is already open —
    a double press must never overwrite the first snapshot.

    ``break_role_id`` / ``lite_role_id`` record the hold roles actually put on,
    so the end of the break can take them off even if a dial moved since."""
    conn.execute(
        "INSERT INTO break_holds "
        "(guild_id, user_id, stored_roles, swapped_spicy, started_at, "
        "break_role_id, lite_role_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id, user_id, json.dumps(stored_roles), int(swapped_spicy),
            time.time(), break_role_id, lite_role_id,
        ),
    )


def get_break(conn: sqlite3.Connection, guild_id: int, user_id: int) -> BreakRow | None:
    r = conn.execute(
        "SELECT * FROM break_holds WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return _row(r) if r else None


def is_on_break_conn(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM break_holds WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        is not None
    )


def is_on_break(db_path: Path, guild_id: int, user_id: int) -> bool:
    """Path-taking form for callers that hold a ``db_path`` rather than a conn."""
    with open_db(db_path) as conn:
        return is_on_break_conn(conn, guild_id, user_id)


def on_break_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT user_id FROM break_holds WHERE guild_id = ?", (guild_id,)
    ).fetchall()
    return {int(r["user_id"]) for r in rows}


def list_breaks(conn: sqlite3.Connection, guild_id: int) -> list[BreakRow]:
    rows = conn.execute(
        "SELECT * FROM break_holds WHERE guild_id = ? ORDER BY started_at",
        (guild_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def delete_break(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    """Close a break. False if there was none (a second "I'm back" press)."""
    cur = conn.execute(
        "DELETE FROM break_holds WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return cur.rowcount > 0


def read_break_role_ids(conn: sqlite3.Connection, guild_id: int) -> tuple[int, int]:
    """``(break_role_id, spicy_lite_role_id)``; 0 = not configured.

    Both are hold roles for every surface that must not report a member as
    stripped (Grant Audit) — someone can wear either live with no row.
    """
    return (
        get_config_int(conn, BREAK_ROLE_KEY, guild_id, allow_legacy_fallback=False),
        get_config_int(conn, SPICY_LITE_ROLE_KEY, guild_id, allow_legacy_fallback=False),
    )


def read_retired_role_ids(conn: sqlite3.Connection, guild_id: int) -> frozenset[int]:
    raw = get_config_value(conn, RETIRED_ROLES_KEY, "[]", guild_id, allow_legacy_fallback=False)
    try:
        return frozenset(int(x) for x in json.loads(raw or "[]") if int(x) > 0)
    except (ValueError, TypeError):
        return frozenset()


def retire_hold_role(
    conn: sqlite3.Connection, guild_id: int, old_role_id: int, new_role_id: int
) -> None:
    """Remember a hold role a dial is moving off, so it still counts as one.

    Called by every writer that re-points ``break_role_id`` or the Spicy Lite
    dial. A no-op when nothing moved or the old value was unset.
    """
    if old_role_id <= 0 or old_role_id == new_role_id:
        return
    retired = read_retired_role_ids(conn, guild_id) | {old_role_id}
    retired -= {new_role_id}
    set_config_value(conn, RETIRED_ROLES_KEY, json.dumps(sorted(retired)), guild_id)


def retire_on_dial_change(
    conn: sqlite3.Connection, guild_id: int, key: str, new_role_id: int
) -> None:
    """The hook for a generic role-dial writer (Bot-Managed Roles): if ``key``
    is one of the break dials, retire what it named before ``new_role_id``
    replaces it. Call **before** the write. Any other key is a no-op."""
    if key in (BREAK_ROLE_KEY, SPICY_LITE_ROLE_KEY):
        retire_hold_role(conn, guild_id, get_config_int(conn, key, guild_id, allow_legacy_fallback=False), new_role_id)


def all_hold_role_ids(conn: sqlite3.Connection, guild_id: int) -> frozenset[int]:
    """Every role that means "on a break": both dials now, plus any they
    named before. What a surface filters on when there may be no row."""
    current = frozenset(r for r in read_break_role_ids(conn, guild_id) if r > 0)
    return current | read_retired_role_ids(conn, guild_id)


def read_break_channel_id(conn: sqlite3.Connection, guild_id: int) -> int:
    return get_config_int(conn, BREAK_CHANNEL_KEY, guild_id, allow_legacy_fallback=False)


def absorb_break(
    conn: sqlite3.Connection, guild_id: int, user_id: int, snapshot: list[int]
) -> list[int]:
    """A jail/inactive snapshot with any open break folded in and closed.

    Mod action wins: the new hold takes the break's saved roles, so releasing
    the member gives back their pre-break roles rather than the break role.
    Call it inside the transaction that writes the hold row — it deletes the
    break row, so the saved roles are never in neither.

    Hold roles are filtered out **with or without** a row: a member can wear
    the break or Spicy Lite role with none (an erased row, a hand-added role),
    and a release must never hand one back.
    """
    brk = get_break(conn, guild_id, user_id)
    hold_ids = all_hold_role_ids(conn, guild_id)
    if brk is None:
        return [r for r in snapshot if r not in hold_ids]
    merged = absorb_snapshot(brk.stored_roles, snapshot, hold_ids | brk.granted_role_ids)
    delete_break(conn, guild_id, user_id)
    return merged
