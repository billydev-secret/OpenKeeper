"""Migration 252: Auto-Thread threads archive after a per-channel duration.

The column arrives at 1440 (one day) on every channel that already exists,
which is exactly the value the cog used to hard-code, so the restart that
applies it changes nothing. A migration that defaulted to another duration,
or touched another setting, fails here.
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


def test_existing_channels_get_one_day_and_keep_their_settings(tmp_path, monkeypatch):
    db = tmp_path / "m252.db"
    _apply_before(db, monkeypatch, "252")
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(needle_channels)")}
    assert "auto_archive_minutes" not in cols
    conn.execute(
        "INSERT INTO needle_channels (guild_id, channel_id, include_bots, require_attachment) "
        "VALUES (1, 2, 1, 1)"
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT auto_archive_minutes, include_bots, require_attachment FROM needle_channels"
    ).fetchall()
    conn.execute("INSERT INTO needle_channels (guild_id, channel_id) VALUES (1, 3)")
    fresh = conn.execute(
        "SELECT auto_archive_minutes FROM needle_channels WHERE channel_id = 3"
    ).fetchone()
    conn.close()
    assert row == [(1440, 1, 1)]
    assert fresh == (1440,)


def test_applying_twice_is_harmless(tmp_path):
    db = tmp_path / "m252-twice.db"
    migrations.apply_migrations_sync(db)
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(needle_channels)")]
    conn.close()
    assert cols.count("auto_archive_minutes") == 1
