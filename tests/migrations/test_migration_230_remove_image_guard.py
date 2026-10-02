"""Migration 230: Image Guard's three tables, six dials and exempt bucket stay gone.

The image classifier was removed on 2026-09-15; only the spoiler gate remains,
and it reads a file's name and spoiler flag, never the picture. What the
classifier left in the database had no reader afterwards and described
members' uploads, so it is dropped rather than kept.

The spoiler channel list goes too: the gate now reads Discord's NSFW flag.
The neighbours matter as much as the deletions: other buckets share the
config_ids table, and legacy grant keys share the ``nsfw_`` prefix.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 111111111111111111
TABLES = ("nsfw_classifications", "nsfw_detections", "nsfw_blocks")
DEAD = (
    "nsfw_classifier_threshold",
    "nsfw_classifier_sfw_threshold",
    "nsfw_chest_label_floor",
    "nsfw_sfw_prevention_mode",
    "nsfw_sfw_prevention_log_channel_id",
    "nsfw_observe_age_gated",
)


def _tables(db_path) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_full_chain_drops_the_three_tables(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    assert _tables(db) & set(TABLES) == set()


def test_dead_rows_go_and_their_neighbours_survive(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(GUILD, key, "1") for key in DEAD]
            # Shares the prefix; not the classifier's.
            + [(GUILD, "nsfw_role_id", "222222222222222222")],
        )
        conn.executemany(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (?, ?, ?)",
            [
                (GUILD, "nsfw_prevention_exempt_channels", 10),
                (GUILD, "spoiler_required_channels", 20),
                (GUILD, "bypass_role_ids", 30),
            ],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '230%'")
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        keys = {r[0] for r in conn.execute("SELECT key FROM config WHERE guild_id = ?", (GUILD,))}
        buckets = {
            r[0]
            for r in conn.execute("SELECT bucket FROM config_ids WHERE guild_id = ?", (GUILD,))
        }
    assert keys & set(DEAD) == set()
    assert "nsfw_role_id" in keys
    assert buckets == {"bypass_role_ids"}


def test_it_drops_populated_tables(tmp_path):
    """A prod database still has the tables and their rows when 230 first runs."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.execute("CREATE TABLE nsfw_classifications (message_id INTEGER, marqo_score REAL)")
        conn.execute("CREATE TABLE nsfw_detections (message_id INTEGER, label TEXT)")
        conn.execute("CREATE TABLE nsfw_blocks (message_id INTEGER, author_id INTEGER)")
        conn.execute("INSERT INTO nsfw_classifications VALUES (1, 0.9)")
        conn.execute("INSERT INTO nsfw_detections VALUES (1, 'SEX_ACT')")
        conn.execute("INSERT INTO nsfw_blocks VALUES (1, 50)")
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '230%'")
    migrations.apply_migrations_sync(db)
    assert _tables(db) & set(TABLES) == set()
