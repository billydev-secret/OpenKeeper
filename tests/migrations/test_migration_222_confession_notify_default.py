"""Migrations 222 + 223: reply-notify dials.

222 turns every existing ``confession_config.notify_op_on_reply`` to 1. That
dial now sets the modal's starting yes/no, and both prod guilds had it at 0 —
without the UPDATE a restart would flip every confess form from "yes" to "no".
The new typed-reply dial must land off. 223 adds Needle's per-channel dial, off.
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


def test_existing_guilds_keep_a_yes_default_and_typed_dms_ship_off(tmp_path, monkeypatch):
    db = tmp_path / "m222.db"
    _apply_before(db, monkeypatch, "222")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO confession_config (guild_id, notify_op_on_reply) VALUES (?, ?)",
        [(1185999999999999222, 0), (1469491362444480666, 0), (3, 1)],
    )
    conn.execute(
        "INSERT INTO needle_channels (guild_id, channel_id) VALUES (1, 2)"
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT notify_op_on_reply, notify_op_on_typed_reply FROM confession_config ORDER BY guild_id"
    ).fetchall()
    needle = conn.execute("SELECT notify_op_on_reply FROM needle_channels").fetchall()
    conn.close()
    assert rows == [(1, 0), (1, 0), (1, 0)]
    assert needle == [(0,)]
