"""Migration 227: Rules Watch's three tables and three config keys stay gone.

Rules Watch was removed in full on 2026-09-13 — the monitor, the report menu,
both dashboard panels and the guard prompt all went in the same commit as this
migration. What it left in the database had no reader afterwards: the flagged
event store, its moderator labels, the withdrawal ledger, and three config
rows (the on/off switch, the alert channel and the guard's editable prompt).

Deleting the config rows rather than zeroing them is the point, so that is what
these assert — a 0 left behind still reads as a value somebody set.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 111111111111111111
TABLES = ("rules_events", "rules_labels", "rules_ledger")
DEAD = (
    "rules_watch_enabled",
    "rules_watch_channel_id",
    "ai_prompt_rules_watch",
)


def _tables(db_path) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def _keys(db_path, guild_id=GUILD) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT key FROM config WHERE guild_id = ?", (guild_id,)
            )
        }


def test_full_chain_drops_the_three_tables(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    assert _tables(db) & set(TABLES) == set()


def test_the_dead_keys_are_gone_and_a_neighbour_survives(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(GUILD, key, "1") for key in DEAD]
            + [(GUILD, "mod_channel_id", "222222222222222222")],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '227%'")
    migrations.apply_migrations_sync(db)

    keys = _keys(db)
    assert keys & set(DEAD) == set()
    assert "mod_channel_id" in keys
