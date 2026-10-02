"""To do list — DB helpers extracted from todo_cog for testability.

Covers the task rows themselves plus the sticky board's placement. Recurring
task definitions live in ``todo_recurring_service``.

Every function takes ``now_ts`` explicitly where it needs the clock, so
time-dependent behavior stays deterministic in tests.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from bot_modules.core.db_utils import get_config_value, parse_bool

TASK_MAX_LEN = 500

#: Ceiling on a single list query — the dashboard paginates visually, not by
#: query, and the board renders far fewer than this.
LIST_LIMIT = 200

#: The two lists sharing the ``todos`` table (migration 225). Every function
#: below that reads or writes rows or the board takes ``list_key`` and defaults
#: it to the todo list, so the callers that predate the bug list — the shop,
#: the chore reset, the sticky registry — keep seeing exactly what they saw.
LIST_TODO = "todo"
LIST_BUG = "bug"
LISTS = (LIST_TODO, LIST_BUG)

#: The two change-card watermarks, named as the columns they live in. A plan
#: advances exactly one of them, and carries the name as data — so the name is
#: a literal rather than a bare ``str``, and a column that does not exist is a
#: type error rather than a silent no-op UPDATE.
CursorColumn = Literal["change_cursor_at", "digest_at"]


def _check_list(list_key: str) -> str:
    """Refuse an unknown list rather than silently reading an empty one."""
    if list_key not in LISTS:
        raise ValueError(f"unknown todo list: {list_key!r}")
    return list_key


_TODO_COLS = (
    "id, guild_id, added_by, task, description, source_message_url,"
    " created_at, completed_at, completed_by, recurring_id, missed_at,"
    " purchase_id, list"
)


def create_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    added_by: int,
    task: str,
    *,
    description: str | None = None,
    source_message_url: str | None = None,
    recurring_id: int | None = None,
    purchase_id: int | None = None,
    screenshots: Sequence[Mapping[str, str]] = (),
    now_ts: float | None = None,
    list_key: str = LIST_TODO,
) -> int:
    """Insert a new to do and return its ID.

    ``purchase_id`` marks a row a custom-shop-item order spawned, the way
    ``recurring_id`` marks one a recurring definition spawned. The task text of
    such a row names the item and never the buyer — see
    ``economy/shop_items.todo_task_text``.

    Chores and orders are todo-list work only; a bug row carrying either would
    be listed on a board with no Chores section and no way to settle an order.

    ``screenshots`` is a bug's uploads as ``{"url", "ext"}`` pairs (see
    ``screenshot_entries``), held only until the bug's change card posts them.
    """
    _check_list(list_key)
    if list_key != LIST_TODO and (recurring_id or purchase_id):
        raise ValueError("chores and shop orders belong on the todo list")
    if screenshots and list_key != LIST_BUG:
        raise ValueError("screenshots belong on the bug list")
    cur = conn.execute(
        "INSERT INTO todos"
        " (guild_id, added_by, task, description, source_message_url,"
        "  recurring_id, purchase_id, created_at, list, attachment_urls)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            added_by,
            task,
            description,
            source_message_url,
            recurring_id,
            purchase_id,
            time.time() if now_ts is None else now_ts,
            list_key,
            json.dumps(list(screenshots)) if screenshots else None,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_todos(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    status: str | None = None,
    limit: int = LIST_LIMIT,
    list_key: str = LIST_TODO,
) -> list[sqlite3.Row]:
    """Todos for ``guild_id``, newest first.

    ``status`` filters to ``pending`` or ``completed``; anything else (including
    None) returns both.
    """
    where = "guild_id = ? AND list = ?"
    params: list = [guild_id, _check_list(list_key)]
    if status == "pending":
        where += f" AND {_OPEN}"
    elif status == "completed":
        where += " AND completed_at IS NOT NULL"
    return conn.execute(
        f"SELECT {_TODO_COLS} FROM todos WHERE {where}"
        f" ORDER BY created_at DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()


#: The only columns the board and its Complete picker actually read. Selecting
#: the full row would haul up to 200 × (500-char task + 1000-char description)
#: out of SQLite every refresh to render fifteen short lines.
#: ``added_by`` rides along for the change cards, which say who added a task.
#: The board's own sections do not print it — a task line there is the work, not
#: its author — so this is one extra column on a query that already runs, rather
#: than a second query for one id. ``attachment_urls`` rides along for the same
#: reason: the bug card posts a new bug's screenshots, and it is NULL everywhere
#: else.
_BOARD_COLS = (
    "t.id, t.task, t.description, t.created_at, t.recurring_id, t.purchase_id,"
    " t.added_by, t.attachment_urls"
)

#: A purchase-spawned row names the item, never the buyer (see
#: ``economy/shop_items.todo_task_text`` for why). The buyer is joined in here
#: instead, so the board can say who an order is for while the stored text
#: stays free of them — and so an erased buyer's row simply renders as unknown,
#: because the purchase row is purged and the join finds nothing.
_BUYER_JOIN = (
    " LEFT JOIN econ_shop_purchases p ON p.id = t.purchase_id"
)

#: A row is outstanding only while it is neither ticked nor written off. The
#: ``missed_at`` half is what stops a chore the daily reset closed yesterday
#: from lingering on the all-todos board forever.
_OPEN = "completed_at IS NULL AND missed_at IS NULL"
#: The same predicate, qualified for the buyer-join query above.
_OPEN_T = "t.completed_at IS NULL AND t.missed_at IS NULL"


#: Chore-spawned rows are excluded from the board's Tasks section because the
#: Chores section above it already shows them, with richer state (done / missed
#: / streak) than a plain task line can carry. Listing them twice on one board
#: was the first thing that looked wrong when the two boards merged.
_NOT_A_CHORE = "recurring_id IS NULL"


def pending_todos(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    limit: int = LIST_LIMIT,
    exclude_chores: bool = False,
    since_ts: float | None = None,
    list_key: str = LIST_TODO,
) -> list[sqlite3.Row]:
    """Outstanding todos, **oldest first** — the board's reading order, so the
    task that has been waiting longest sits at the top.

    ``exclude_chores`` drops rows a recurring definition spawned; the board
    passes it, the dashboard does not (its one list is the whole list).

    ``since_ts`` narrows to rows created after a moment, which is what the
    change cards ask for: "what has arrived and is *still* waiting". Combining
    the age filter with the outstanding predicate rather than querying the
    table raw is deliberate — an item added and ticked inside one polling
    window is then never announced, and a card saying a task needs doing after
    somebody did it is worse than no card.
    """
    where = "t.list = ? AND " + _OPEN_T + (
        f" AND t.{_NOT_A_CHORE}" if exclude_chores else ""
    )
    params: list[object] = [guild_id, _check_list(list_key)]
    if since_ts is not None:
        where += " AND t.created_at > ?"
        params.append(float(since_ts))
    params.append(int(limit))
    return conn.execute(
        f"SELECT {_BOARD_COLS}, p.user_id AS buyer_id FROM todos t"
        f"{_BUYER_JOIN}"
        f" WHERE t.guild_id = ? AND {where}"
        f" ORDER BY t.created_at ASC LIMIT ?",
        tuple(params),
    ).fetchall()


def pending_count(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    exclude_chores: bool = False,
    list_key: str = LIST_TODO,
) -> int:
    """How many todos are outstanding, uncapped by any list limit."""
    where = _OPEN + (f" AND {_NOT_A_CHORE}" if exclude_chores else "")
    return conn.execute(
        f"SELECT COUNT(*) FROM todos WHERE guild_id = ? AND list = ? AND {where}",
        (guild_id, _check_list(list_key)),
    ).fetchone()[0]


def complete_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    guild_id: int,
    completed_by: int,
    *,
    now_ts: float | None = None,
    list_key: str = LIST_TODO,
) -> bool:
    """Mark a todo complete. Returns False when it's missing or already closed.

    Scoped to ``list_key`` as well as the guild, so a Complete press on one
    board can never close an id that belongs to the other list — the ids share
    one sequence, and a stale picker or a hand-typed dashboard URL is all it
    would take.

    The ``completed_at IS NULL`` guard makes this idempotent under a race
    between the board button and the dashboard. The ``missed_at IS NULL`` half
    refuses a row the daily reset already wrote off: yesterday's chore is
    finished business, and letting a late tick credit it would both invent a
    completion that never happened and corrupt the streak either side of it.

    A row a custom-shop-item order spawned also DELIVERS that order here,
    releasing the escrowed coins to the server (and opening the rental, for a
    weekly item). It hangs off the guarded UPDATE's rowcount rather than
    guarding itself, so it inherits that exactly-once property — and it lives
    in here rather than at the two call sites so neither the board button nor
    the dashboard can tick an order off without settling it.
    """
    now = time.time() if now_ts is None else now_ts
    cur = conn.execute(
        f"UPDATE todos SET completed_at = ?, completed_by = ?"
        f" WHERE id = ? AND guild_id = ? AND list = ? AND {_OPEN}",
        (now, completed_by, todo_id, guild_id, _check_list(list_key)),
    )
    if cur.rowcount == 0:
        return False
    _settle_purchase(conn, todo_id, completed_by, now)
    return True


def _settle_purchase(
    conn: sqlite3.Connection, todo_id: int, completed_by: int, now: float
) -> None:
    """Deliver the shop order behind a just-completed todo, if there is one.

    Deferred import: the economy imports the todo board, not the other way
    round, and this keeps that true for every caller who never sells anything.
    """
    row = conn.execute(
        "SELECT purchase_id FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()
    if row is None or not row["purchase_id"]:
        return
    from bot_modules.economy.shop_items_service import (  # noqa: PLC0415
        fulfil_for_todo,
    )

    fulfil_for_todo(conn, todo_id, completed_by, now=now)


def mark_missed(
    conn: sqlite3.Connection, todo_id: int, *, now_ts: float
) -> bool:
    """Close an outstanding row *without* crediting it. Returns False if already closed.

    The daily reset's other half: the next occurrence of a recurring chore can
    only spawn once the previous instance stops being outstanding, and this is
    how it stops without pretending anyone did it.
    """
    cur = conn.execute(
        f"UPDATE todos SET missed_at = ? WHERE id = ? AND {_OPEN}",
        (now_ts, todo_id),
    )
    return cur.rowcount > 0


# ── Board placement ─────────────────────────────────────────────


@dataclass(frozen=True)
class TodoBoard:
    """Where the guild's sticky board lives. Zeroes mean unposted.

    The two cursors are the change cards' state (migration 224), and ``None``
    on both is the meaningful default: it says "nothing has been announced
    yet", which the planner reads as "bank the clock and stay quiet" rather
    than "announce the backlog". A zero would be an epoch timestamp and mean
    the opposite, so these are nullable rather than defaulted.
    """

    guild_id: int
    channel_id: int = 0
    message_id: int = 0
    updated_at: float = 0.0
    #: High-water mark of what the change cards have announced.
    change_cursor_at: float | None = None
    #: When the once-a-day recurring-chore digest last posted.
    digest_at: float | None = None

    @property
    def posted(self) -> bool:
        return bool(self.channel_id and self.message_id)


def get_board(
    conn: sqlite3.Connection, guild_id: int, *, list_key: str = LIST_TODO
) -> TodoBoard:
    row = conn.execute(
        "SELECT channel_id, message_id, updated_at, change_cursor_at, digest_at"
        " FROM todo_board WHERE guild_id = ? AND list = ?",
        (guild_id, _check_list(list_key)),
    ).fetchone()
    if row is None:
        return TodoBoard(guild_id=guild_id)
    return TodoBoard(
        guild_id=guild_id,
        channel_id=row["channel_id"] or 0,
        message_id=row["message_id"] or 0,
        updated_at=row["updated_at"] or 0.0,
        # Left as None when unset rather than coerced: see TodoBoard.
        change_cursor_at=row["change_cursor_at"],
        digest_at=row["digest_at"],
    )


def save_board(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    *,
    now_ts: float | None = None,
    list_key: str = LIST_TODO,
) -> None:
    conn.execute(
        "INSERT INTO todo_board"
        " (guild_id, list, channel_id, message_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(guild_id, list) DO UPDATE SET"
        "   channel_id = excluded.channel_id,"
        "   message_id = excluded.message_id,"
        "   updated_at = excluded.updated_at",
        (
            guild_id,
            _check_list(list_key),
            channel_id,
            message_id,
            time.time() if now_ts is None else now_ts,
        ),
    )


def clear_board(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now_ts: float | None = None,
    list_key: str = LIST_TODO,
) -> None:
    """Forget the placement without dropping the row, so a later post reuses it."""
    save_board(conn, guild_id, 0, 0, now_ts=now_ts, list_key=list_key)


def save_change_cursors(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    change_cursor_at: float | None = None,
    digest_at: float | None = None,
    list_key: str = LIST_TODO,
) -> None:
    """Advance either change-card cursor, leaving the other and the ids alone.

    Separate from ``save_board`` on purpose. That function owns *placement* and
    is called by the sticky machinery on every repost; folding a cursor write
    into it would mean a routine repost could silently move the announcement
    watermark past items nobody had announced. Only the columns actually passed
    are written, so advancing the daily digest can never disturb the arrivals
    cursor a few milliseconds later in the same tick.

    Does nothing when the guild has no board row: a guild with no board is never
    polled, so there is no cursor to keep, and an INSERT here would create a
    placement row for a board that was never posted.
    """
    sets: list[str] = []
    params: list[object] = []
    if change_cursor_at is not None:
        sets.append("change_cursor_at = ?")
        params.append(float(change_cursor_at))
    if digest_at is not None:
        sets.append("digest_at = ?")
        params.append(float(digest_at))
    if not sets:
        return
    params.extend((guild_id, _check_list(list_key)))
    conn.execute(
        f"UPDATE todo_board SET {', '.join(sets)} WHERE guild_id = ? AND list = ?",
        tuple(params),
    )


def save_cursor_column(
    conn: sqlite3.Connection,
    guild_id: int,
    column: CursorColumn,
    value: float,
    *,
    list_key: str = LIST_TODO,
) -> None:
    """Advance the one cursor a plan names, by name.

    ``deliver_cards`` holds the column as data — the plan says which watermark
    it moves — and this is the single place that turns that name back into a
    keyword. Spelled out rather than ``**{column: value}``, because the dict
    form would just as happily land on ``list_key`` and no type checker can
    tell the difference.
    """
    if column == "digest_at":
        save_change_cursors(conn, guild_id, digest_at=value, list_key=list_key)
    else:
        save_change_cursors(conn, guild_id, change_cursor_at=value, list_key=list_key)


def reset_change_cursors(
    conn: sqlite3.Connection, guild_id: int, *, list_key: str = LIST_TODO
) -> None:
    """Null both cursors on one list's board, re-arming the silent first tick.

    What switching a board's change cards back on does, so a guild that ran
    them months ago is not greeted with everything that arrived while they were
    off. Per list: switching the bug cards on must not wipe the todo board's
    watermark, and vice versa.
    """
    conn.execute(
        "UPDATE todo_board SET change_cursor_at = NULL, digest_at = NULL"
        " WHERE guild_id = ? AND list = ?",
        (guild_id, _check_list(list_key)),
    )


def chore_changes_since(
    conn: sqlite3.Connection, guild_id: int, since_ts: float
) -> tuple[int, int]:
    """``(spawned, missed)`` recurring-chore instances since ``since_ts``.

    Counts, not rows, because that is all the daily digest says. The two halves
    are independent: a day where the chore went undone both writes yesterday's
    instance off as missed *and* spawns a fresh one, so a single row can be
    counted in one half on one day and the other half the next.

    Deliberately counts by ``created_at``/``missed_at`` rather than reading the
    chore scoreboard, so the digest reports the window since the last digest
    rather than whatever state the board happens to show at noon.
    """
    row = conn.execute(
        "SELECT"
        "  COALESCE(SUM(created_at > ?), 0) AS spawned,"
        "  COALESCE(SUM(missed_at IS NOT NULL AND missed_at > ?), 0) AS missed"
        " FROM todos WHERE guild_id = ? AND recurring_id IS NOT NULL",
        (float(since_ts), float(since_ts), guild_id),
    ).fetchone()
    if row is None:
        return 0, 0
    return int(row["spawned"] or 0), int(row["missed"] or 0)


#: Per-guild dial for the board's change cards. Absent means off, which is how
#: the feature ships: no row exists anywhere until an admin ticks the box on the
#: Todo panel, so a merge and a restart change nothing.
CHANGE_CARDS_KEY = "todo_change_cards"
#: The bug board's own switch — separate, so turning cards on for one board
#: never starts the bot posting above the other. Absent means off, likewise.
BUG_CHANGE_CARDS_KEY = "bug_change_cards"
_CHANGE_CARDS_KEYS = {LIST_TODO: CHANGE_CARDS_KEY, LIST_BUG: BUG_CHANGE_CARDS_KEY}


def change_cards_key(list_key: str = LIST_TODO) -> str:
    """The config key holding one list's change-card switch."""
    return _CHANGE_CARDS_KEYS[_check_list(list_key)]


def change_cards_enabled(
    conn: sqlite3.Connection, guild_id: int, *, list_key: str = LIST_TODO
) -> bool:
    """Whether this guild posts a card when one list's board changes.

    Read with ``allow_legacy_fallback=False``: this dial makes the bot post
    messages into a channel, and inheriting that from the legacy ``guild_id=0``
    row would switch it on in guilds nobody configured. Every other guild-scoped
    posting decision on this board is strictly per-guild for the same reason.

    Truthiness is ``parse_bool``'s and nobody else's. Its allow-list (``1``,
    ``true``, ``yes``, ``on``) is what every other dial in the repo means by on,
    and dials here do get set by hand straight into the prod ``config`` table —
    a deny-list would read ``off`` and ``no`` as **on** and start the bot posting
    into a mod channel on the strength of a word that plainly says otherwise.
    """
    return parse_bool(
        get_config_value(
            conn,
            change_cards_key(list_key),
            "0",
            guild_id,
            allow_legacy_fallback=False,
        )
    )


def guilds_with_change_cards(
    conn: sqlite3.Connection, *, list_key: str = LIST_TODO
) -> list[int]:
    """Guild ids with the change-card dial on — the poll's work list.

    One query per tick, and in a server where nobody has turned the feature on
    it returns nothing and the poll stops there. That is what keeps the loop's
    "no pointless reads per minute, forever" promise intact while the feature is
    dark: the cost of shipping this is a single indexed config lookup.

    Filtered in Python through ``parse_bool`` rather than by a SQL value list,
    so this and ``change_cards_enabled`` cannot come to different conclusions
    about the same stored string — the loop polling a guild whose own panel
    reads "off" is exactly the config bug two readers of one key produce.
    """
    rows = conn.execute(
        "SELECT guild_id, value FROM config WHERE key = ?",
        (change_cards_key(list_key),),
    ).fetchall()
    return [
        int(r["guild_id"])
        for r in rows
        if int(r["guild_id"]) and parse_bool(r["value"])
    ]


def guilds_with_board(
    conn: sqlite3.Connection, *, list_key: str = LIST_TODO
) -> list[int]:
    """Guild ids with a live board — the refresh loop's work list."""
    rows = conn.execute(
        "SELECT guild_id FROM todo_board"
        " WHERE list = ? AND channel_id != 0 AND message_id != 0",
        (_check_list(list_key),),
    ).fetchall()
    return [r["guild_id"] for r in rows]


# ── Who may file a bug ──────────────────────────────────────────


#: Per-guild dial on the Bug List panel: who may add to the bug list with
#: ``/bug`` or the bug board's Add button. Absent means moderators only, which is
#: exactly ``/todo``'s gate — so the command ships dark, and opening a public
#: write surface is an admin's deliberate act rather than a side effect of a
#: restart. Closing a bug is moderator-only whatever this says.
BUG_FILING_KEY = "bug_filing"
FILING_MODS = "mods"
FILING_EVERYONE = "everyone"
FILING_MODES = (FILING_MODS, FILING_EVERYONE)

#: How long a non-moderator waits between filings when the list is open to
#: everyone. Long enough that one member cannot bury the mod queue, short
#: enough that two real bugs noticed in one session both get filed.
BUG_COOLDOWN_SECONDS = 300


def bug_filing_mode(conn: sqlite3.Connection, guild_id: int) -> str:
    """``FILING_EVERYONE`` only when stored as exactly that; else moderators.

    Strictly per-guild (no legacy ``guild_id=0`` fallback) for the same reason
    as the change-card dial: this opens a write surface to every member, and
    inheriting that from a row nobody set for this guild would be the wrong
    direction to fail in. Anything unrecognised reads as the closed state.
    """
    value = get_config_value(
        conn, BUG_FILING_KEY, FILING_MODS, guild_id, allow_legacy_fallback=False
    )
    return (
        FILING_EVERYONE
        if str(value or "").strip().lower() == FILING_EVERYONE
        else FILING_MODS
    )


def last_bug_filed_at(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> float | None:
    """When this member last filed a bug, or None — the cooldown's only input.

    Derived from the rows themselves rather than a separate timestamp store, so
    there is nothing extra to erase: an erasure anonymises ``added_by`` and the
    member's cooldown history goes with it.
    """
    row = conn.execute(
        "SELECT MAX(created_at) FROM todos"
        " WHERE guild_id = ? AND list = ? AND added_by = ?",
        (guild_id, LIST_BUG, user_id),
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def bug_filing_refusal(
    *,
    is_mod: bool,
    mode: str,
    last_filed_at: float | None,
    now: float,
    cooldown_seconds: int = BUG_COOLDOWN_SECONDS,
) -> str | None:
    """The message refusing this filing, or None if it may go ahead.

    Moderators are never refused: they can already add to the todo list, and a
    cooldown on the people triaging the queue would only get in their way.
    """
    if is_mod:
        return None
    if mode != FILING_EVERYONE:
        return "❌ Only moderators can file bugs."
    if last_filed_at is not None:
        remaining = cooldown_seconds - (now - last_filed_at)
        if remaining > 0:
            minutes = max(1, -(-int(remaining) // 60))
            unit = "minute" if minutes == 1 else "minutes"
            return (
                "❌ You filed a bug a moment ago — you can file another in"
                f" {minutes} {unit}."
            )
    return None


# ── Bug screenshots ─────────────────────────────────────────────
#
# A member can attach images to a bug, and they are posted on the bug board's
# next change card (bug list row 244). The bot keeps no copy: the upload links
# sit on the row only until that card posts, and the card itself is the one
# place an image lives — until the bug closes, when the bug loop edits it off.

#: How many images one filing may carry — the modal's upload cap.
SCREENSHOT_MAX = 3
#: Per image. Screenshots are far smaller; this stops a photo dump.
SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024
#: The backstop for upload links no card ever took: the switch turned off, or
#: the board unposted, between a filing and the next tick. Discord's own links
#: expire within a day, so holding one past this could only ever fail.
SCREENSHOT_HOLD_SECONDS = 3600


def bug_screenshots_open(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Whether a bug filed now could carry screenshots.

    Only where there is a card to put them on: the bug board is posted and its
    change-card switch is on. Anywhere else an upload would sit on the row
    until the backstop cleared it, having gone nowhere — so it is not offered.
    """
    return bool(
        get_board(conn, guild_id, list_key=LIST_BUG).channel_id
        and change_cards_enabled(conn, guild_id, list_key=LIST_BUG)
    )


def screenshot_refusal(uploads: Sequence[tuple[str | None, int]]) -> str | None:
    """Why these uploads (``(content_type, size)`` each) can't go on a card."""
    if len(uploads) > SCREENSHOT_MAX:
        return f"❌ Attach at most {SCREENSHOT_MAX} screenshots."
    for content_type, size in uploads:
        if not str(content_type or "").startswith("image/"):
            return "❌ Screenshots must be images."
        if size > SCREENSHOT_MAX_BYTES:
            mb = SCREENSHOT_MAX_BYTES // (1024 * 1024)
            return f"❌ Each screenshot must be under {mb} MB."
    return None


def screenshot_entries(
    uploads: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    """``(url, filename)`` uploads as the ``{"url", "ext"}`` pairs a row holds.

    Only the extension of the member's filename is kept: the card renames every
    image ``bug-<id>-<n>.<ext>``, so the original name — which can say anything
    — never reaches the channel.
    """
    entries = []
    for url, filename in uploads:
        _, dot, ext = str(filename).rpartition(".")
        ext = ext.lower() if dot and ext.isalnum() and len(ext) <= 5 else "png"
        entries.append({"url": str(url), "ext": ext})
    return entries


def settle_bug_screenshots(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    upto_ts: float,
    posted_bug_ids: Sequence[int] = (),
    channel_id: int = 0,
    message_id: int = 0,
) -> None:
    """After a bug-card tick moved its cursor: drop every held upload link it
    covered, and remember the card the posted ones landed on.

    Cleared for the whole window, not only the bugs whose images posted — a
    boot pass, a give-up or an over-full card covers bugs whose images went
    nowhere, and the window is closed for them now too.
    """
    if posted_bug_ids and message_id:
        conn.executemany(
            "UPDATE todos SET card_channel_id = ?, card_message_id = ?"
            " WHERE id = ? AND guild_id = ? AND list = ?",
            [
                (channel_id, message_id, int(b), guild_id, LIST_BUG)
                for b in posted_bug_ids
            ],
        )
    conn.execute(
        "UPDATE todos SET attachment_urls = NULL"
        " WHERE guild_id = ? AND list = ? AND attachment_urls IS NOT NULL"
        " AND created_at <= ?",
        (guild_id, LIST_BUG, float(upto_ts)),
    )


def expire_bug_screenshots(conn: sqlite3.Connection, *, now: float) -> int:
    """Drop upload links no card took within ``SCREENSHOT_HOLD_SECONDS``."""
    return conn.execute(
        "UPDATE todos SET attachment_urls = NULL"
        " WHERE attachment_urls IS NOT NULL AND created_at < ?",
        (float(now) - SCREENSHOT_HOLD_SECONDS,),
    ).rowcount


def bug_cards_to_strip(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Bugs whose images are still on a card but shouldn't be, every guild.

    That is a bug that has **closed**, and one whose filer was **erased**
    (``added_by`` blanked to 0 by the purge): the purge cannot reach Discord,
    so the loop takes the images down within a minute of it instead.
    """
    return conn.execute(
        "SELECT id, guild_id, card_channel_id, card_message_id FROM todos"
        " WHERE card_message_id IS NOT NULL AND list = ?"
        " AND (completed_at IS NOT NULL OR missed_at IS NOT NULL OR added_by = 0)",
        (LIST_BUG,),
    ).fetchall()


def forget_bug_cards(conn: sqlite3.Connection, bug_ids: Sequence[int]) -> None:
    """The images are off the card (or the card is gone): stop tracking it."""
    conn.executemany(
        "UPDATE todos SET card_channel_id = NULL, card_message_id = NULL"
        " WHERE id = ?",
        [(int(b),) for b in bug_ids],
    )


#: What is lost while the board is unposted. It is the only Discord surface
#: that can tick anything off, so with it gone a mod can still add tasks with
#: /todo and then find nowhere to complete them. This went unsaid for three
#: days in prod and was discovered by failing to tick anything off, so the
#: dashboard says it on the card and again before removing the board.
UNPOSTED_COST = (
    "nothing can be ticked off from Discord while it is unposted — the board"
    " is the only surface with a Complete button"
)
