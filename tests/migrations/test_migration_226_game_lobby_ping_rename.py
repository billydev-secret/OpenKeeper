"""Migration 226: the Game Lobby ping's stored names move to game_lobby_*.

The hazard is the opt-out. Prod's only row is ``game_night_ping_role_id = '0'``
— an admin's "(none)", which the sweep resolves to "configured, ping nobody".
If the row were left under the old key the new key would read as never set,
and the next lobby would provision a fresh role and start pinging it. The
first test is the one that bites if the key rename is ever dropped from the
migration while the code keeps reading the new name.
"""

from __future__ import annotations

import json

import migrations
from bot_modules.core.db_utils import open_db
from bot_modules.shared.feature_roles import GAME_LOBBY_PING

GUILD = 1469491362444480666
OTHER_GUILD = 1476999999999999998
ROLE = 1546999999999999829


def _rearm(conn) -> None:
    conn.execute("DELETE FROM schema_version WHERE migration LIKE '226%'")
    conn.commit()


def _config(db_path, guild_id: int) -> dict[str, str]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM config WHERE guild_id = ? AND key LIKE 'game_%ping_role_id'",
            (guild_id,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_the_none_decision_carries_across_to_the_new_key(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, 'game_night_ping_role_id', '0')",
            (GUILD,),
        )
        _rearm(conn)
    migrations.apply_migrations_sync(db)

    assert GAME_LOBBY_PING.key == "game_lobby_ping_role_id"
    assert _config(db, GUILD) == {"game_lobby_ping_role_id": "0"}


def test_a_chosen_role_and_other_guilds_carry_across(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, 'game_night_ping_role_id', ?)",
            [(GUILD, str(ROLE)), (OTHER_GUILD, "0")],
        )
        _rearm(conn)
    migrations.apply_migrations_sync(db)

    assert _config(db, GUILD) == {"game_lobby_ping_role_id": str(ROLE)}
    assert _config(db, OTHER_GUILD) == {"game_lobby_ping_role_id": "0"}


def test_the_carried_decision_wins_over_a_row_already_under_the_new_key(tmp_path):
    """OR REPLACE: the stored opt-out is the real decision; nothing could have
    written the new key before this migration's code shipped."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(GUILD, "game_night_ping_role_id", "0"), (GUILD, "game_lobby_ping_role_id", str(ROLE))],
        )
        _rearm(conn)
    migrations.apply_migrations_sync(db)

    assert _config(db, GUILD) == {"game_lobby_ping_role_id": "0"}


def test_the_bot_managed_role_record_follows_the_key(tmp_path):
    """The roster page addresses provenance by the registry key; a stale row
    would show the role the bot made as someone else's."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO bot_managed_roles (guild_id, role_key, role_id, origin, recorded_at)"
            " VALUES (?, 'game_night_ping_role_id', ?, 'created', 1788749490.0)",
            (GUILD, ROLE),
        )
        _rearm(conn)
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        rows = conn.execute(
            "SELECT role_key, role_id, origin FROM bot_managed_roles WHERE guild_id = ?", (GUILD,)
        ).fetchall()
    assert [tuple(r) for r in rows] == [("game_lobby_ping_role_id", ROLE, "created")]


def _insert_active(conn, game_id: str, payload: str) -> None:
    conn.execute(
        "INSERT INTO games_active_games (game_id, game_type, channel_id, host_id, payload)"
        " VALUES (?, 'mlt', 1, 2, ?)",
        (game_id, payload),
    )


def test_a_lobby_open_across_the_restart_keeps_its_once_only_flag(tmp_path):
    """Dropping the flag instead of renaming it would ping that lobby twice.
    The value stays JSON true (json_extract alone would hand back 1)."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        _insert_active(conn, "pinged", json.dumps({"game_night_pinged": True, "players": [5]}))
        _insert_active(conn, "fresh", json.dumps({"players": [6]}))
        _insert_active(conn, "broken", "not json")
        _rearm(conn)
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        got = dict(conn.execute("SELECT game_id, payload FROM games_active_games").fetchall())
    assert json.loads(got["pinged"]) == {"game_lobby_pinged": True, "players": [5]}
    assert json.loads(got["fresh"]) == {"players": [6]}
    assert got["broken"] == "not json"
