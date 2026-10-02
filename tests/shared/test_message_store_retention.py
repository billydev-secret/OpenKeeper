"""``store_message`` keeps metadata and derivations, never message text.

The bot stopped storing message text on 2026-09-13
(docs/plans/remove-message-storage.md). Before that, a per-guild storage level
decided whether text, attachment URLs and embeds were written. Now
``store_message`` has no parameter that could carry any of them; these tests
pin that, and pin what it does still record.
"""

from __future__ import annotations

import inspect
import sqlite3

from bot_modules.shared.message_store import init_message_tables, store_message


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_message_tables(conn)
    return conn


def _store(conn: sqlite3.Connection, *, sentiment: float | None = 0.5) -> None:
    store_message(
        conn,
        message_id=100,
        guild_id=1,
        channel_id=10,
        author_id=50,
        reply_to_id=None,
        ts=1_000_000,
        mention_ids=[77],
        sentiment=sentiment,
        emotion="joy" if sentiment is not None else None,
        media_kind="media",
    )


def _row(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM messages WHERE message_id = 100").fetchone()


def test_store_message_has_no_parameter_that_carries_text():
    params = set(inspect.signature(store_message).parameters)
    assert not params & {"content", "attachment_urls", "embeds", "retain_content"}


def test_store_message_keeps_metadata_and_derivations():
    conn = _conn()
    _store(conn)
    row = _row(conn)
    # The skeleton still rebuilds a Discord deep link.
    assert (row["guild_id"], row["channel_id"], row["author_id"]) == (1, 10, 50)
    assert row["sentiment"] == 0.5
    assert row["emotion"] == "joy"
    assert row["media_kind"] == "media"
    assert conn.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0] == 1


def test_the_schema_has_nowhere_to_put_text():
    """Migration 228 dropped the column and both tables; the ingest schema
    must not bring them back on a fresh database."""
    conn = _conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "content" not in cols
    assert tables & {"message_attachments", "message_embeds"} == set()


def test_storing_again_fills_a_missing_derivation_without_duplicating():
    conn = _conn()
    _store(conn, sentiment=None)
    _store(conn, sentiment=0.5)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert _row(conn)["sentiment"] == 0.5
