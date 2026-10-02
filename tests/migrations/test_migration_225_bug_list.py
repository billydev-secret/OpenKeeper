"""Migration 225: todos gain a list column, todo_board is keyed by (guild, list).

The rebuild is the dangerous half. It drops and recreates the table that holds
the live board's placement, so a column left out of the copy either unposts the
board (ids) or silently resets the change-card watermark (cursors). The ids are
production's live board, as pinned by migration 180's test — snowflakes past
2^53, so a lossy copy would show.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 1469491362444480666
OTHER_GUILD = 1476999999999999998
PROD_CHANNEL = 1531999999999999638
PROD_MESSAGE = 1539999999999999847


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "bugs.db"
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "225"],
    )
    migrations.apply_migrations_sync(path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)
    return path


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_the_live_board_survives_byte_exact_with_its_cursors(db):
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO todo_board (guild_id, channel_id, message_id, updated_at,"
            " change_cursor_at, digest_at) VALUES (?, ?, ?, 5.5, 100.25, NULL)",
            (GUILD, PROD_CHANNEL, PROD_MESSAGE),
        )
        conn.execute(
            "INSERT INTO todo_board (guild_id, channel_id, message_id, updated_at)"
            " VALUES (?, 0, 0, 0)",
            (OTHER_GUILD,),
        )
        conn.commit()
    migrations.apply_migrations_sync(db)
    with _connect(db) as conn:
        rows = {
            r["guild_id"]: dict(r)
            for r in conn.execute("SELECT * FROM todo_board")
        }
    assert rows[GUILD] == {
        "guild_id": GUILD,
        "list": "todo",
        "channel_id": PROD_CHANNEL,
        "message_id": PROD_MESSAGE,
        "updated_at": 5.5,
        "change_cursor_at": 100.25,
        "digest_at": None,
    }
    assert rows[OTHER_GUILD]["list"] == "todo"


def test_existing_todos_all_land_on_the_todo_list(db):
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO todos (guild_id, added_by, task, created_at)"
            " VALUES (?, 1, 'old task', 0)",
            (GUILD,),
        )
        conn.commit()
    migrations.apply_migrations_sync(db)
    with _connect(db) as conn:
        assert [r["list"] for r in conn.execute("SELECT list FROM todos")] == [
            "todo"
        ]


def test_a_guild_can_hold_one_board_per_list_and_no_more(db):
    migrations.apply_migrations_sync(db)
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO todo_board (guild_id, list, channel_id, message_id)"
            " VALUES (?, 'todo', 1, 2)",
            (GUILD,),
        )
        conn.execute(
            "INSERT INTO todo_board (guild_id, list, channel_id, message_id)"
            " VALUES (?, 'bug', 3, 4)",
            (GUILD,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO todo_board (guild_id, list, channel_id, message_id)"
                " VALUES (?, 'bug', 5, 6)",
                (GUILD,),
            )
