"""managed_roles_store — every bot-managed role's stored id, one read for all."""

from __future__ import annotations

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.shared import feature_roles as fr
from bot_modules.roles.bot_roles.managed_roles_store import read_stored_ids

GUILD = 42


def test_every_managed_role_has_an_entry(sync_db_path):
    with open_db(sync_db_path) as conn:
        stored = read_stored_ids(conn, GUILD)
    assert set(stored) == {e.key for e in fr.MANAGED_ROLES}
    assert all(v[0] == 0 for v in stored.values())


def test_a_config_dial_reads_as_this_guilds_own(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, fr.QOTD_PING.key, "7", GUILD)
        stored = read_stored_ids(conn, GUILD)
    assert stored[fr.QOTD_PING.key] == (7, True, False)


def test_a_none_dial_reads_as_opted_out(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, fr.WELCOME_PING.key, "0", GUILD)
        stored = read_stored_ids(conn, GUILD)
    assert stored[fr.WELCOME_PING.key][2] is True


def test_a_garbage_value_reads_as_unset(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, fr.RISKY_PING.key, "abc", GUILD)
        stored = read_stored_ids(conn, GUILD)
    assert stored[fr.RISKY_PING.key][0] == 0
