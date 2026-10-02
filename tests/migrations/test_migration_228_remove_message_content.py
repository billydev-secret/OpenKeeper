"""Migration 228: stored message text is erased, not just made unreachable.

On 2026-09-13 the bot stopped keeping any copy of what members write
(docs/plans/remove-message-storage.md). The migration drops messages.content,
the attachment and embed tables, six config rows and the cached payloads that
quoted text, and turns on secure_delete so the bytes it frees are overwritten.

The last property is the one a schema check cannot see: without secure_delete
the dropped text survives in the file's free space, readable with ``strings``.
So the test plants a sentinel, runs the migration, and reads the raw file.

The seed is one row per table on purpose. Many rows split b-tree pages, and a
split leaves stale copies of the sentinel in free space *before* the migration
runs. secure_delete cannot reach space that was already free, which is the
production caveat the migration header documents and the manual VACUUM
removes. This test pins what the migration itself guarantees: nothing it frees
keeps the text.
"""

from __future__ import annotations

import gc
import pathlib
import sqlite3

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 111111111111111111
#: Long and unlike anything SQLite writes, so a hit can only be our text.
SENTINEL = "sentinel-" + "q7Zx" * 40
DEAD = (
    "message_storage_level",
    "message_retention_enabled",
    "ai_prompt_review",
    "ai_prompt_scan",
    "ai_prompt_query_user",
    "ai_prompt_query_channel",
)
TEXT_CACHE = ("sentiment_feed", "deep:sentiment_feed", "sentiment_outliers")
KEPT_CACHE = ("sentiment", "gini")


def _pre_228(db) -> None:
    """Migrate fully, then put back the shape and data 228 exists to remove."""
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.execute("ALTER TABLE messages ADD COLUMN content TEXT")
        conn.execute(
            "CREATE TABLE message_attachments (message_id INTEGER NOT NULL, "
            "url TEXT NOT NULL, PRIMARY KEY (message_id, url))"
        )
        conn.execute(
            "CREATE TABLE message_embeds (message_id INTEGER NOT NULL, "
            "embed_index INTEGER NOT NULL, title TEXT, description TEXT, "
            "url TEXT, author_name TEXT, footer_text TEXT, fields_json TEXT, "
            "PRIMARY KEY (message_id, embed_index))"
        )
        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id, "
            "ts, sentiment, media_kind, content) VALUES (1, ?, 10, 50, 1000, 0.5, "
            "'media', ?)",
            (GUILD, f"{SENTINEL}-message"),
        )
        conn.execute(
            "INSERT INTO message_attachments VALUES (1, ?)", (f"https://x/{SENTINEL}",)
        )
        conn.execute(
            "INSERT INTO message_embeds (message_id, embed_index, description) "
            "VALUES (1, 0, ?)",
            (f"{SENTINEL}-embed",),
        )
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(GUILD, key, "1") for key in DEAD]
            + [(GUILD, "mod_channel_id", "222222222222222222")],
        )
        conn.executemany(
            "INSERT INTO health_metrics_cache (guild_id, metric_key, payload_json, "
            "computed_at) VALUES (?, ?, ?, 0)",
            [(GUILD, key, f'{{"content": "{SENTINEL}"}}') for key in TEXT_CACHE]
            + [(GUILD, key, "{}") for key in KEPT_CACHE],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '228%'")


def _sentinel_hits(db) -> dict[str, int]:
    """Sentinel occurrences per file, once every page is in the main file.

    The runner's ``with sqlite3.connect()`` commits but does not close, so
    collect it first; then leaving WAL mode checkpoints every frame into the
    main file and deletes the WAL. If a connection were still open the mode
    change would silently stay 'wal', so that is asserted rather than assumed.
    """
    gc.collect()
    conn = sqlite3.connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    finally:
        conn.close()
    assert mode == "delete"
    needle = SENTINEL.encode()
    return {
        path.name: path.read_bytes().count(needle)
        for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-journal"))
        if path.exists()
    }


def test_the_planted_text_is_in_the_file_before_the_migration(tmp_path):
    """Guards the next test against passing vacuously."""
    db = tmp_path / "t.db"
    _pre_228(db)
    assert _sentinel_hits(db)["t.db"] >= 6


def test_the_text_is_gone_from_the_file_bytes(tmp_path):
    db = tmp_path / "t.db"
    _pre_228(db)
    migrations.apply_migrations_sync(db)
    hits = _sentinel_hits(db)
    assert sum(hits.values()) == 0, hits


def test_without_secure_delete_the_text_would_survive(tmp_path, monkeypatch):
    """The PRAGMA is load-bearing, and the byte test above can see it.

    Some SQLite builds (Fedora's, which production runs) default secure_delete
    on; the Windows test runner's does not. Forcing it off shows the dropped
    text left in free space on every build, so the test above passing is the
    migration's doing and not the build's.
    """
    real_read_text = pathlib.Path.read_text

    def without_secure_delete(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if self.name == "228_remove_message_content.sql":
            assert "PRAGMA secure_delete = ON;" in text
            text = text.replace("PRAGMA secure_delete = ON;", "PRAGMA secure_delete = OFF;")
        return text

    db = tmp_path / "t.db"
    _pre_228(db)
    monkeypatch.setattr(pathlib.Path, "read_text", without_secure_delete)
    migrations.apply_migrations_sync(db)
    assert sum(_sentinel_hits(db).values()) > 0


def test_schema_loses_the_text_and_keeps_the_metadata(tmp_path):
    db = tmp_path / "t.db"
    _pre_228(db)
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        row = conn.execute("SELECT ts, sentiment, media_kind FROM messages").fetchall()
    assert "content" not in cols
    assert {"sentiment", "media_kind", "deleted_at", "reply_to_id"} <= cols
    assert tables & {"message_attachments", "message_embeds"} == set()
    assert {"message_mentions", "message_reactions"} <= tables
    assert [tuple(r) for r in row] == [(1000, 0.5, "media")]


def test_dead_keys_and_text_caches_go_and_neighbours_stay(tmp_path):
    db = tmp_path / "t.db"
    _pre_228(db)
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        keys = {r[0] for r in conn.execute("SELECT key FROM config WHERE guild_id = ?", (GUILD,))}
        cache = {r[0] for r in conn.execute("SELECT metric_key FROM health_metrics_cache")}
    assert keys & set(DEAD) == set()
    assert "mod_channel_id" in keys
    assert cache == set(KEPT_CACHE)
