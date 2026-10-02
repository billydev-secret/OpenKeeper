"""Migration 250: the sponsored-QOTD queue goes with ``/qotd``.

The queue table, ``econ_qotd.sponsor_user_id`` and two price dials had no
reader once the command that drained the queue was retired (2026-09-30). The
neighbours matter as much as the deletions: ``econ_qotd`` keeps every other
column and its rows, and ``econ_emoji_sponsor_expire_days`` shares the
``sponsor_expire_days`` tail but is a live Sponsored Emoji dial.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666
DEAD = ("econ_price_qotd_sponsor", "econ_qotd_sponsor_expire_days")
LIVE = ("econ_emoji_sponsor_expire_days", "econ_qotd_ping_role_id", "econ_reward_qotd")


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_full_chain_has_no_sponsor_storage(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        assert "econ_qotd_submissions" not in _tables(conn)
        assert "sponsor_user_id" not in _columns(conn, "econ_qotd")


def test_prod_shape_loses_the_sponsor_parts_and_keeps_the_rest(tmp_path):
    """Prod still has the table, the column and the dials when 250 first runs."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.execute(
            "CREATE TABLE econ_qotd_submissions (id INTEGER PRIMARY KEY, question TEXT)"
        )
        conn.execute("ALTER TABLE econ_qotd ADD COLUMN sponsor_user_id INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "INSERT INTO econ_qotd (guild_id, channel_id, message_id, question, posted_by,"
            " local_day, created_at) VALUES (?, 1, 2, 'Cats or dogs?', 3, '2026-09-30', 0)",
            (GUILD,),
        )
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(GUILD, key, "14") for key in DEAD + LIVE],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '250%'")
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        assert "econ_qotd_submissions" not in _tables(conn)
        assert "sponsor_user_id" not in _columns(conn, "econ_qotd")
        assert [r[0] for r in conn.execute("SELECT question FROM econ_qotd")] == ["Cats or dogs?"]
        keys = {r[0] for r in conn.execute("SELECT key FROM config WHERE guild_id = ?", (GUILD,))}
    assert keys & set(DEAD) == set()
    assert set(LIVE) <= keys
