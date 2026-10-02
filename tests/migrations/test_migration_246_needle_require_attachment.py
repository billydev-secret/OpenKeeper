"""Migration 246: Auto-Thread can require an upload on every post.

The column arrives at 0 on every channel that already exists, so the restart
that applies it changes nothing: no post is removed until an admin ticks the
box. A migration that defaulted the dial on, or touched another setting, fails
here.
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


def test_existing_channels_get_the_dial_off_and_keep_their_settings(tmp_path, monkeypatch):
    db = tmp_path / "m246.db"
    _apply_before(db, monkeypatch, "246")
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(needle_channels)")}
    assert "require_attachment" not in cols
    conn.execute(
        "INSERT INTO needle_channels (guild_id, channel_id, include_bots, notify_op_on_reply) "
        "VALUES (1, 2, 1, 1)"
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT require_attachment, include_bots, notify_op_on_reply FROM needle_channels"
    ).fetchall()
    conn.execute("INSERT INTO needle_channels (guild_id, channel_id) VALUES (1, 3)")
    fresh = conn.execute(
        "SELECT require_attachment FROM needle_channels WHERE channel_id = 3"
    ).fetchone()
    conn.close()
    assert row == [(0, 1, 1)]
    assert fresh == (0,)


def test_applying_twice_is_harmless(tmp_path):
    db = tmp_path / "m246-twice.db"
    migrations.apply_migrations_sync(db)
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(needle_channels)")]
    conn.close()
    assert cols.count("require_attachment") == 1
