"""Migration 251: Chat Revive's on/off becomes a mode, and prod doesn't move.

``enabled = 1`` must come out as ``post`` and ``enabled = 0`` as ``off`` — the
feature ships dark, so every existing server keeps doing exactly what it did
(docs/specs/chat_revive_spec.md § What a lull does). The old column is dropped so
nothing can read a boolean that disagrees with the mode.
"""

from __future__ import annotations

import sqlite3

import migrations
from bot_modules.chat_revive.service import get_guild_config
from bot_modules.core.db_utils import open_db


def test_enabled_becomes_post_and_disabled_becomes_off(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        # Rebuild the pre-251 shape: `enabled`, no `mode`.
        conn.execute("ALTER TABLE revive_guild_config DROP COLUMN mode")
        conn.execute(
            "ALTER TABLE revive_guild_config"
            " ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0"
        )
        conn.executemany(
            "INSERT INTO revive_guild_config (guild_id, enabled, daily_budget)"
            " VALUES (?, ?, ?)",
            [(1, 1, 2), (2, 0, 3)],
        )
        conn.execute(
            "INSERT INTO revive_events (id, guild_id, channel_id, trigger_kind,"
            " pinged, local_day, created_at, success) VALUES (41, 1, 5, 'manual', 1,"
            " '2026-09-29', 100.0, 1)"
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '251%'")
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(revive_guild_config)")}
        on, off = get_guild_config(conn, 1), get_guild_config(conn, 2)
        kept = tuple(conn.execute(
            "SELECT id, trigger_kind, pinged, success FROM revive_events"
        ).fetchone())
        # The rebuilt ledger takes a staff lull ping, and ids carry on after
        # the copied ones (they are quest occurrence keys).
        conn.execute(
            "INSERT INTO revive_events (guild_id, channel_id, trigger_kind,"
            " local_day, created_at) VALUES (1, 5, 'staff', '2026-09-30', 200.0)"
        )
        new_id = conn.execute("SELECT MAX(id) FROM revive_events").fetchone()[0]
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(revive_events)")}
    assert "enabled" not in cols
    assert (on.mode, on.daily_budget) == ("post", 2)
    assert off.mode == "off"
    assert kept == (41, "manual", 1, 1)
    assert new_id == 42
    assert {"idx_revive_events_guild_ts", "idx_revive_events_channel_ts"} <= indexes


def test_a_new_row_defaults_to_off(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO revive_guild_config (guild_id) VALUES (7)")
    assert conn.execute(
        "SELECT mode FROM revive_guild_config WHERE guild_id = 7"
    ).fetchone() == ("off",)
    conn.close()
