"""Migration 240: posted_panels, absorbing ticket_panels.

The ticket panels already posted are the only posted-once panels anything
recorded, so their rows are what lets a restart refresh them without a
re-post. The ids are snowflakes past 2^53, so a lossy copy would show; the
guild-0 row stands in for prod's stray from before per-guild config.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 1469491362444480666
CHANNEL = 1531999999999999638
MESSAGE = 1539999999999999847


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "panels.db"
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files", lambda: [f for f in real if f.name < "240"]
    )
    migrations.apply_migrations_sync(path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)
    return path


def test_ticket_panels_carry_over_exactly_and_the_old_table_goes(db):
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO ticket_panels (guild_id, channel_id, message_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            [(GUILD, CHANNEL, MESSAGE, 12.5), (0, 5, 6, 1.0)],
        )
    migrations.apply_migrations_sync(db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT guild_id, panel_key, channel_id, message_id, options_json,"
            " posted_at FROM posted_panels"
        ).fetchall()
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert rows == [(GUILD, "ticket-panel", CHANNEL, MESSAGE, "{}", 12.5)]
    assert "ticket_panels" not in tables


def test_one_row_per_panel_per_channel(db):
    migrations.apply_migrations_sync(db)
    with sqlite3.connect(db) as conn:
        insert = (
            "INSERT INTO posted_panels (guild_id, panel_key, channel_id, message_id,"
            " posted_at) VALUES (?, ?, ?, ?, 0)"
        )
        conn.execute(insert, (GUILD, "ask-panel", CHANNEL, 1))
        conn.execute(insert, (GUILD, "ticket-panel", CHANNEL, 2))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (GUILD, "ask-panel", CHANNEL, 3))
