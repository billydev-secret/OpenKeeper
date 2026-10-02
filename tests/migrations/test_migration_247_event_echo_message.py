"""Migration 247: Event Echo remembers the card it posted (bug #273).

Rows that exist at the restart keep everything they had and get NULL ids: a
card posted before the migration cannot be retracted, and must not look as
though it can.
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


def test_existing_rows_keep_their_values_and_get_no_card(tmp_path, monkeypatch):
    db = tmp_path / "m247.db"
    _apply_before(db, monkeypatch, "247")
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_echo_log)")}
    assert not {"echo_channel_id", "echo_message_id"} & cols
    conn.execute(
        "INSERT INTO event_echo_log (guild_id, source, echo_key, ref, echoed_at, suppressed) "
        "VALUES (1, 'party_game', 'mfk', 'g1', 100.0, 0)"
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT guild_id, source, echo_key, ref, echoed_at, suppressed, "
        "echo_channel_id, echo_message_id FROM event_echo_log"
    ).fetchall()
    conn.close()
    assert row == [(1, "party_game", "mfk", "g1", 100.0, 0, None, None)]


def test_applying_twice_is_harmless(tmp_path):
    db = tmp_path / "m247-twice.db"
    migrations.apply_migrations_sync(db)
    migrations.apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(event_echo_log)")]
    conn.close()
    assert cols.count("echo_channel_id") == 1
    assert cols.count("echo_message_id") == 1
