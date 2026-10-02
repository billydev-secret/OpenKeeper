"""Migration 248: Greeting Watch's DM list is deleted, not left behind.

The unanswered-greeting alert became the Staff · Greetings ping (2026-09-30),
so nothing reads the two notify keys. They hold member ids; a key left in the
table would be both a setting nobody enforces and ids kept past their use.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666

RETIRED = ("greeting_watch_notify_user_ids", "greeting_watch_notify_user_id")
KEPT = (
    "greeting_watch_enabled",
    "greeting_watch_channel_ids",
    "greeting_watch_window_minutes",
    "greeting_watch_extra_words",
)


def test_the_notify_keys_go_and_the_rest_of_greeting_watch_stays(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(g, key, "1384999999999999911") for g in (0, GUILD) for key in RETIRED + KEPT],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '248%'")
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        left = {row[0] for row in conn.execute("SELECT key FROM config")}
    assert left & set(RETIRED) == set()
    assert set(KEPT) <= left
