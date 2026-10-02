"""Message metadata archive.

Stores what the bot derives from a message at ingest — author, channel,
timestamps, reply reference, media_kind, sentiment, @mentions and reaction
counts. It keeps no message text, attachment URL or embed: migration 228
(2026-09-13) dropped the column and tables that held them.

All writes are idempotent — safe to call from both the live event handler
and the /interaction_scan backfill without creating duplicates.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from bot_modules.ping_tracker import service as ping_tracker_service

log = logging.getLogger(__name__)

# ── Media-kind classification ─────────────────────────────────────────
# A coarse per-message media classification, kept as metadata so media-based
# metrics keep working without storing the attachment or its URL.
# Derived from attachment file extensions only — no URL or content is stored.
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
# "media" = non-gif image/video (what the NSFW media metric counts).
MEDIA_KIND_MEDIA = "media"
MEDIA_KIND_GIF = "gif"
MEDIA_KIND_OTHER = "other"


def classify_media_kind(filenames: list[str]) -> str | None:
    """Reduce a message's attachment names to a single coarse media_kind.

    Precedence: ``media`` (non-gif image/video) > ``gif`` > ``other``; returns
    ``None`` when there are no attachments. ``filenames`` may be filenames or
    full URLs — only the extension (before any ``?`` query string) is inspected,
    so no URL or content is retained.
    """
    if not filenames:
        return None
    kinds: set[str] = set()
    for name in filenames:
        path = name.split("?", 1)[0].lower()
        dot = path.rfind(".")
        ext = path[dot:] if dot != -1 else ""
        if ext in _IMAGE_EXTENSIONS or ext in _VIDEO_EXTENSIONS:
            kinds.add(MEDIA_KIND_MEDIA)
        elif ext == ".gif":
            kinds.add(MEDIA_KIND_GIF)
        else:
            kinds.add(MEDIA_KIND_OTHER)
    if MEDIA_KIND_MEDIA in kinds:
        return MEDIA_KIND_MEDIA
    if MEDIA_KIND_GIF in kinds:
        return MEDIA_KIND_GIF
    return MEDIA_KIND_OTHER


def init_message_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id  INTEGER PRIMARY KEY,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            author_id   INTEGER NOT NULL,
            reply_to_id INTEGER,
            ts          INTEGER NOT NULL,
            sentiment   REAL,
            emotion     TEXT,
            media_kind  TEXT,
            deleted_at     INTEGER,
            deleted_source TEXT
        )
        """
    )
    # Migrate existing tables that lack the sentiment columns
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "sentiment" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN sentiment REAL")
    if "emotion" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN emotion TEXT")
    if "deleted_at" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN deleted_at INTEGER")
    if "deleted_source" not in _cols:
        conn.execute("ALTER TABLE messages ADD COLUMN deleted_source TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_guild_ts ON messages (guild_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_author "
        "ON messages (guild_id, author_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_channel_ts "
        "ON messages (guild_id, channel_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_sentiment "
        "ON messages (guild_id, sentiment)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_emotion "
        "ON messages (guild_id, emotion)"
    )
    # Partial: nearly every row is live, and only the deleted minority is ever
    # queried through this column. Keyed on ts rather than deleted_at so the
    # sort reads off the index — see 155_messages_deleted.sql.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_deleted "
        "ON messages (guild_id, ts) WHERE deleted_at IS NOT NULL"
    )
    # Newcomer Funnel's per-joiner loop seeks "this author's messages since
    # ts" and "who replied to one of this author's messages" — neither is
    # served by (guild_id, ts) or (guild_id, author_id) alone. See
    # 201_newcomer_funnel_indexes.sql for the measured before/after.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_author_ts "
        "ON messages (guild_id, author_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_reply_to "
        "ON messages (guild_id, reply_to_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_mentions (
            message_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mentions_user ON message_mentions (user_id)"
    )

    # Role pings. Deliberately a separate table rather than rows in
    # message_mentions: that table is keyed on user_id and a role has none,
    # which is exactly why role pings were invisible before this existed.
    # Schema owned by migration 198 — mirrored here like every other table in
    # this function so a fresh test database is complete.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ping_events (
            message_id  INTEGER PRIMARY KEY,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            author_id   INTEGER NOT NULL,
            role_ids    TEXT    NOT NULL DEFAULT '[]',
            everyone    INTEGER NOT NULL DEFAULT 0,
            source      TEXT    NOT NULL DEFAULT 'member',
            ref         TEXT,
            ts          REAL    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ping_events_guild_ts "
        "ON ping_events (guild_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ping_events_author "
        "ON ping_events (guild_id, author_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_reactions (
            message_id  INTEGER NOT NULL,
            emoji       TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (message_id, emoji)
        )
        """
    )


def init_known_users_table(conn: sqlite3.Connection) -> None:
    """Create the known_users lookup table for offline username resolution."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS known_users (
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            username        TEXT NOT NULL DEFAULT '',
            display_name    TEXT NOT NULL DEFAULT '',
            updated_at      REAL NOT NULL DEFAULT 0,
            is_bot          INTEGER NOT NULL DEFAULT 0,
            current_member  INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # Migrations for existing DBs
    for col, definition in (
        ("is_bot", "INTEGER NOT NULL DEFAULT 0"),
        ("current_member", "INTEGER NOT NULL DEFAULT 1"),
    ):
        try:
            conn.execute(f"ALTER TABLE known_users ADD COLUMN {col} {definition}")
        except Exception:
            log.exception("message_store: add column may already exist")


def upsert_known_user(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    username: str,
    display_name: str,
    ts: float,
    *,
    is_bot: bool = False,
    current_member: bool = True,
) -> None:
    """Insert or update a user's known name. Only updates name fields if ts is newer."""
    conn.execute(
        """
        INSERT INTO known_users (guild_id, user_id, username, display_name, updated_at, is_bot, current_member)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            username = CASE WHEN excluded.updated_at > known_users.updated_at THEN excluded.username ELSE known_users.username END,
            display_name = CASE WHEN excluded.updated_at > known_users.updated_at THEN excluded.display_name ELSE known_users.display_name END,
            updated_at = CASE WHEN excluded.updated_at > known_users.updated_at THEN excluded.updated_at ELSE known_users.updated_at END,
            is_bot = excluded.is_bot,
            current_member = excluded.current_member
        """,
        (guild_id, user_id, username, display_name, ts, int(is_bot), int(current_member)),
    )


def mark_member_left(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "UPDATE known_users SET current_member = 0 WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def init_member_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_events (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            event_type  TEXT NOT NULL,
            ts          REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id, event_type, ts)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_member_events_guild_ts "
        "ON member_events (guild_id, ts)"
    )


def record_member_event(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    event_type: str,
    ts: float,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO member_events (guild_id, user_id, event_type, ts)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, user_id, event_type, ts),
    )


def get_known_users_bulk(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: list[int],
) -> dict[int, str]:
    """Return {user_id: display_name} for a batch of users."""
    if not user_ids:
        return {}
    ph = ",".join("?" * len(user_ids))
    rows = conn.execute(
        f"SELECT user_id, display_name FROM known_users WHERE guild_id = ? AND user_id IN ({ph})",
        [guild_id, *user_ids],
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def get_known_user_names_bulk(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: list[int],
) -> dict[int, str]:
    """Return {user_id: best_known_name} for a batch, falling back to username.

    Unlike :func:`get_known_users_bulk` this keeps the ``username`` column in
    play, so a row recorded before the user had a guild nickname (blank
    ``display_name``) still yields something readable instead of dropping to a
    raw id. Rows whose name columns are both empty are omitted entirely, so a
    caller can treat "absent from the result" as "no name on file".
    """
    if not user_ids:
        return {}
    ph = ",".join("?" * len(user_ids))
    rows = conn.execute(
        f"SELECT user_id, display_name, username FROM known_users "
        f"WHERE guild_id = ? AND user_id IN ({ph})",
        [guild_id, *user_ids],
    ).fetchall()
    out: dict[int, str] = {}
    for r in rows:
        name = (r["display_name"] or "").strip() or (r["username"] or "").strip()
        if name:
            out[int(r["user_id"])] = name
    return out


def init_known_channels_table(conn: sqlite3.Connection) -> None:
    """Create the known_channels lookup table for offline channel name resolution.

    Also the registry of which ids are threads and what they hang off, which the
    channel analytics need to attribute thread activity to a parent channel.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS known_channels (
            guild_id        INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL,
            channel_name    TEXT NOT NULL DEFAULT '',
            updated_at      REAL NOT NULL DEFAULT 0,
            parent_id       INTEGER,
            is_thread       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )
    # Migrate tables created before the thread columns existed (163).
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(known_channels)").fetchall()}
    if "parent_id" not in _cols:
        conn.execute("ALTER TABLE known_channels ADD COLUMN parent_id INTEGER")
    if "is_thread" not in _cols:
        conn.execute(
            "ALTER TABLE known_channels ADD COLUMN is_thread INTEGER NOT NULL DEFAULT 0"
        )


def upsert_known_channel(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    ts: float,
    parent_id: int | None = None,
    is_thread: bool = False,
) -> None:
    """Insert or update a channel's known name. Only updates if ts is newer.

    *parent_id* and *is_thread* record whether this id is a thread and which
    channel it hangs off, which the channel analytics need to attribute a
    thread's activity to its parent instead of listing the thread as a channel
    of its own (see services/channel_rollup). Callers get both from discord.py's
    ``Thread.parent_id`` — an attribute no other channel type has, so a text
    channel can never be mistaken for a thread inside its category.

    A known parent is never unlearned: a caller that doesn't know one passes
    None and leaves whatever was already recorded in place. That matters because
    the backfill can name a parent the ingest path never sees again once the
    thread is archived.
    """
    conn.execute(
        """
        INSERT INTO known_channels
            (guild_id, channel_id, channel_name, updated_at, parent_id, is_thread)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET
            channel_name = excluded.channel_name,
            updated_at = excluded.updated_at,
            parent_id = COALESCE(excluded.parent_id, known_channels.parent_id),
            is_thread = excluded.is_thread
        WHERE excluded.updated_at > known_channels.updated_at
        """,
        (guild_id, channel_id, channel_name, ts, parent_id, 1 if is_thread else 0),
    )


def get_known_channels_bulk(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_ids: list[int],
) -> dict[int, str]:
    """Return {channel_id: channel_name} for a batch of channels."""
    if not channel_ids:
        return {}
    ph = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"SELECT channel_id, channel_name FROM known_channels WHERE guild_id = ? AND channel_id IN ({ph})",
        [guild_id, *channel_ids],
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def store_message(
    conn: sqlite3.Connection,
    message_id: int,
    guild_id: int,
    channel_id: int,
    author_id: int,
    reply_to_id: int | None,
    ts: int,
    mention_ids: list[int],
    sentiment: float | None = None,
    emotion: str | None = None,
    media_kind: str | None = None,
    role_mention_ids: Sequence[int] = (),
    mention_everyone: bool = False,
    ping_source: str = ping_tracker_service.SOURCE_MEMBER,
) -> None:
    """Store a message's metadata and derivations. Silently skips if already stored.

    No message text, attachment URL or embed is ever written: the bot stopped
    storing them on 2026-09-13 (``docs/plans/remove-message-storage.md``). The
    row keeps ids and timestamp (enough to rebuild a Discord deep link), the
    ingest-time derivations (``sentiment``/``emotion``/``media_kind``) and the
    @-mention edges.

    ``role_mention_ids`` / ``mention_everyone`` describe the role pings the
    message fired, recorded into ``ping_events``. They come from Discord's
    structured mention lists rather than the text.
    """
    conn.execute(
        """
        INSERT INTO messages
            (message_id, guild_id, channel_id, author_id, reply_to_id, ts, sentiment, emotion, media_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            reply_to_id = COALESCE(messages.reply_to_id, excluded.reply_to_id),
            sentiment = COALESCE(excluded.sentiment, messages.sentiment),
            emotion = COALESCE(excluded.emotion, messages.emotion),
            media_kind = COALESCE(excluded.media_kind, messages.media_kind)
        """,
        (
            message_id,
            guild_id,
            channel_id,
            author_id,
            reply_to_id,
            ts,
            sentiment,
            emotion,
            media_kind,
        ),
    )
    for user_id in mention_ids:
        conn.execute(
            "INSERT OR IGNORE INTO message_mentions (message_id, user_id) VALUES (?, ?)",
            (message_id, user_id),
        )
    ping_tracker_service.record_ping_event(
        conn,
        message_id=message_id,
        guild_id=guild_id,
        channel_id=channel_id,
        author_id=author_id,
        role_ids=role_mention_ids,
        everyone=mention_everyone,
        source=ping_source,
        ts=ts,
    )


def set_reaction_count(
    conn: sqlite3.Connection,
    message_id: int,
    emoji: str,
    count: int,
) -> None:
    """Set an absolute reaction count (used when backfilling from message history)."""
    if count <= 0:
        conn.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND emoji = ?",
            (message_id, emoji),
        )
    else:
        conn.execute(
            """
            INSERT INTO message_reactions (message_id, emoji, count) VALUES (?, ?, ?)
            ON CONFLICT(message_id, emoji) DO UPDATE SET count = excluded.count
            """,
            (message_id, emoji, count),
        )


def adjust_reaction_count(
    conn: sqlite3.Connection,
    message_id: int,
    emoji: str,
    delta: int,
) -> None:
    """Increment or decrement a reaction count for a live reaction event."""
    conn.execute(
        """
        INSERT INTO message_reactions (message_id, emoji, count)
        VALUES (?, ?, MAX(0, ?))
        ON CONFLICT(message_id, emoji) DO UPDATE SET count = MAX(0, count + ?)
        """,
        (message_id, emoji, delta, delta),
    )
    conn.execute(
        "DELETE FROM message_reactions WHERE message_id = ? AND emoji = ? AND count = 0",
        (message_id, emoji),
    )


# ── Deletion marking ──────────────────────────────────────────────────
#
# The archive is permanent: a Discord deletion flags the row, it does not
# remove it. The only hard erasure is ``privacy_service.purge_user_data`` — a
# subject's Art 17 request must actually erase, and a flag would not.

# A deletion we observed but cannot attribute — Discord's raw delete payload
# names no actor. This covers a member deleting their own message, a mod
# removing one, and a member's privacy-panel purge alike.
DELETE_SOURCE_DISCORD = "discord"
# Our own auto-delete sweep expiring a channel's messages on a timer. High
# volume and routine, which is exactly why it is worth telling apart.
DELETE_SOURCE_AUTO_DELETE = "auto_delete"

# Deliberately only two. A member's privacy-panel purge is *not* distinguished:
# ``privacy_cog._run_deletion`` is guaranteed — in a comment and in
# test_privacy_modes.test_no_mode_touches_the_database — never to open the DB,
# and stamping a source would break that guard. It would also mean the archive
# recorded that a member had exercised a privacy control, which is a new kind
# of data about them for a mod-visible surface. Their purge lands as
# ``discord`` like any other deletion.
DELETE_SOURCES = frozenset({DELETE_SOURCE_DISCORD, DELETE_SOURCE_AUTO_DELETE})


def mark_messages_deleted(
    conn: sqlite3.Connection,
    guild_id: int,
    message_ids: set[int] | list[int],
    source: str,
    ts: int,
) -> int:
    """Flag messages as deleted. Returns how many rows this call newly flagged.

    First writer wins: the ``deleted_at IS NULL`` guard makes the write both
    idempotent (a redelivered gateway event is a no-op) and non-clobbering.
    That guard is what makes attribution work at all — ``auto_delete_service``
    and the privacy purge stamp their ids *before* calling the Discord API, so
    the generic ``on_raw_message_delete`` that follows finds the row already
    flagged and leaves the specific source in place. Without it, every
    attributed deletion would be overwritten with ``discord`` moments later.

    Messages we have no row for are silently skipped — the guild may have been
    at storage level ``none``, or the message may predate the bot.
    """
    if not message_ids:
        return 0
    if source not in DELETE_SOURCES:
        raise ValueError(f"unknown delete source: {source!r}")
    ids = list(message_ids)
    ph = ",".join("?" * len(ids))
    cur = conn.execute(
        f"""
        UPDATE messages
           SET deleted_at = ?, deleted_source = ?
         WHERE message_id IN ({ph})
           AND guild_id = ?
           AND deleted_at IS NULL
        """,
        (ts, source, *ids, guild_id),
    )
    return max(cur.rowcount, 0)


def clear_deleted_flag(
    conn: sqlite3.Connection,
    guild_id: int,
    message_ids: set[int] | list[int],
    source: str,
) -> int:
    """Undo a claim for messages that were never actually deleted.

    The claim-then-delete order guarantees attribution but is optimistic: if
    Discord refuses the delete (lost permissions, a transient 5xx), the message
    is still there and a "deleted" badge would be a lie — one that also
    suppresses the deep link to a message a moderator could otherwise open.

    Scoped to ``source`` so a rollback can only ever clear our own optimistic
    claim. A row already flagged ``discord`` was deleted by someone else while
    we were working, and must survive this.
    """
    if not message_ids:
        return 0
    ids = list(message_ids)
    ph = ",".join("?" * len(ids))
    cur = conn.execute(
        f"""
        UPDATE messages
           SET deleted_at = NULL, deleted_source = NULL
         WHERE message_id IN ({ph})
           AND guild_id = ?
           AND deleted_source = ?
        """,
        (*ids, guild_id, source),
    )
    return max(cur.rowcount, 0)
