"""Migration 244: Auto-Thread reply DMs become opt-in.

The per-thread mutes table goes, and the new opt-in table starts empty — the
quiet cutover: nobody is enrolled, so reply DMs stop for everyone until they
switch them on. A migration that carried a mute over, or enrolled anyone,
fails here.
"""

from __future__ import annotations

import sqlite3

import migrations


def _apply_before(db_path, monkeypatch, cutoff: str) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files", lambda: [f for f in real if f.name < cutoff]
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_mutes_are_dropped_and_nobody_is_opted_in(tmp_path, monkeypatch):
    db = tmp_path / "m244.db"
    _apply_before(db, monkeypatch, "244")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO needle_channels (guild_id, channel_id, notify_op_on_reply) VALUES (1, 2, 1)"
    )
    conn.execute(
        "INSERT INTO needle_reply_mutes (guild_id, channel_id, thread_id, user_id) "
        "VALUES (1, 2, 3, 4)"
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    conn = sqlite3.connect(db)
    tables = _tables(conn)
    optins = conn.execute("SELECT COUNT(*) FROM needle_reply_optins").fetchone()
    dial = conn.execute("SELECT notify_op_on_reply FROM needle_channels").fetchall()
    conn.close()
    assert "needle_reply_mutes" not in tables
    assert optins == (0,)
    # The admin's per-channel dial is untouched: it still gates the opt-in.
    assert dial == [(1,)]
