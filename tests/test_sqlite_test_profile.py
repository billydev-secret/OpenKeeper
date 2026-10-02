"""The suite-wide SQLite profile that ``tests/conftest.py`` installs.

Every connection the suite opens runs with ``synchronous=OFF``: the helpers in
``db_utils`` through their own flag, everything else through the wrapped
``sqlite3.connect``. These tests pin the wrapper's reach and its edges; the
helpers' prod path is proven in ``tests/core/test_db_utils.py``.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from bot_modules.core.db_utils import TEST_PROFILE_ENV, open_db
from migrations import apply_migrations_sync


def test_the_flag_is_set_for_every_test():
    assert os.environ.get(TEST_PROFILE_ENV) == "1"


def test_a_raw_connection_skips_fsync(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 0
    finally:
        conn.close()


async def test_the_aiosqlite_fixture_inherits_it(temp_db):
    """aiosqlite reaches ``sqlite3.connect`` through the module attribute, so
    the wrapper covers it without the fixture setting anything itself."""
    cur = await temp_db.execute("PRAGMA synchronous")
    assert (await cur.fetchone())[0] == 0


def test_the_helpers_keep_wal_under_the_profile(sync_db_path):
    """Lock semantics must match the bot's: readers never block writers."""
    with open_db(sync_db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 0


def test_journal_mode_is_left_alone_for_raw_connections(tmp_path):
    """A test that sets WAL, or reads the mode back, sees the real thing —
    ``test_db_backup``, ``test_preflight_scripts`` and the migration-228
    byte test all depend on it."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()


def test_the_migration_runner_builds_a_real_schema(tmp_path):
    db = tmp_path / "fresh.db"
    apply_migrations_sync(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] > 0
    finally:
        conn.close()


def test_a_read_only_uri_connection_still_opens(tmp_path):
    db = tmp_path / "ro.db"
    sqlite3.connect(db).close()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_a_file_that_is_not_a_database_fails_where_the_test_expects(tmp_path):
    """The wrapper's own pragma must not turn a deliberate bad open into a
    setup error: the test's first statement is where the failure belongs."""
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database, not at all" * 64)
    conn = sqlite3.connect(bad)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("SELECT 1")
    conn.close()


def test_a_connection_factory_is_passed_through(tmp_path):
    class _Conn(sqlite3.Connection):
        pass

    conn = sqlite3.connect(tmp_path / "t.db", factory=_Conn)
    assert isinstance(conn, _Conn)
    conn.close()
