"""Migration 256: ``guess_rounds.expired_at`` and ``expiry_cleared_at``, NULL on every existing round."""

from __future__ import annotations

import sqlite3

import migrations


def test_existing_rounds_arrive_unexpired(tmp_path, monkeypatch):
    db = tmp_path / "m256.db"
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files", lambda: [f for f in real if f.name < "256"]
    )
    migrations.apply_migrations_sync(db)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(guess_rounds)")}
    assert not {"expired_at", "expiry_cleared_at"} & cols
    conn.execute(
        "INSERT INTO guess_rounds (guild_id, submitter_id, answer_id, created_at)"
        " VALUES (1, 2, 2, 1.0)"
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(guess_rounds)")}
    assert {"expired_at", "expiry_cleared_at"} <= cols
    assert conn.execute(
        "SELECT expired_at, expiry_cleared_at FROM guess_rounds"
    ).fetchone() == (None, None)
