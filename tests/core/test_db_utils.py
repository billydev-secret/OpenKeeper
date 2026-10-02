from __future__ import annotations

import ast
import sqlite3

import pytest

from bot_modules.core.db_utils import (
    TEST_PROFILE_ENV,
    fixed_offset_tz,
    get_config_id_set,
    get_config_int,
    get_config_value,
    _optimize_and_close,
    open_db,
    open_db_immediate,
    parse_bool,
)
from tests.db_template import migrated_db
from tests.paths import REPO_ROOT


# ── parse_bool ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"))
def test_truthy_strings(value):
    assert parse_bool(value) is True


@pytest.mark.parametrize("value", ("0", "false", "False", "no", "off", "random", ""))
def test_falsy_strings(value):
    assert parse_bool(value) is False


def test_none_returns_default_false():
    assert parse_bool(None) is False


def test_none_returns_explicit_default():
    assert parse_bool(None, default=True) is True
    assert parse_bool(None, default=False) is False


def test_strips_whitespace():
    assert parse_bool("  true  ") is True
    assert parse_bool("  false  ") is False


# ── config DB ─────────────────────────────────────────────────────────

@pytest.fixture
def config_db(tmp_path):
    return migrated_db(tmp_path / "test.db")


def test_get_config_value_missing_key_returns_default(config_db):
    with open_db(config_db) as conn:
        assert get_config_value(conn, "missing", "fallback") == "fallback"


def test_get_config_value_stored_key(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config (key, value) VALUES ('mykey', 'myval')")
        assert get_config_value(conn, "mykey", "fallback") == "myval"


def test_get_config_value_overrides_default(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config (key, value) VALUES ('guild_id', '12345')")
        assert get_config_value(conn, "guild_id", "0") == "12345"


def test_get_config_id_set_empty_bucket(config_db):
    with open_db(config_db) as conn:
        assert get_config_id_set(conn, "no_such_bucket") == set()


def test_get_config_id_set_returns_correct_ids(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('roles', 10)")
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('roles', 20)")
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('other', 99)")
        assert get_config_id_set(conn, "roles") == {10, 20}


def test_get_config_id_set_scoped_to_bucket(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('a', 1)")
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('b', 2)")
        assert get_config_id_set(conn, "a") == {1}
        assert get_config_id_set(conn, "b") == {2}


# ── legacy-fallback behavior ─────────────────────────────────────────
# guild_id=0 stores legacy (single-guild) config. Non-zero guild reads
# normally fall back to it, but per-guild callers can opt out so an
# unconfigured non-home guild gets real defaults instead of inheriting
# the home guild's settings.

def test_get_config_value_falls_back_to_legacy_for_non_home_guild(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '999')"
        )
        # No guild-100 row → falls back to legacy
        assert get_config_value(conn, "mod_channel_id", "0", guild_id=100) == "999"


def test_get_config_value_strict_mode_skips_legacy_fallback(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '999')"
        )
        result = get_config_value(
            conn, "mod_channel_id", "0", guild_id=100, allow_legacy_fallback=False
        )
        assert result == "0"


def test_get_config_value_strict_mode_still_returns_guild_specific(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '111')"
        )
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (100, 'mod_channel_id', '222')"
        )
        # Strict mode does NOT block the guild-specific read, only the legacy fallback
        result = get_config_value(
            conn, "mod_channel_id", "0", guild_id=100, allow_legacy_fallback=False
        )
        assert result == "222"


def test_get_config_value_legacy_lookup_ignores_strict_flag(config_db):
    """guild_id=0 is the legacy bucket itself; the flag has no effect there."""
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '999')"
        )
        assert (
            get_config_value(
                conn, "mod_channel_id", "0", guild_id=0, allow_legacy_fallback=False
            )
            == "999"
        )


def test_get_config_id_set_falls_back_to_legacy_for_non_home_guild(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 10)"
        )
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 20)"
        )
        assert get_config_id_set(conn, "roles", guild_id=100) == {10, 20}


def test_get_config_id_set_strict_mode_skips_legacy_fallback(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 10)"
        )
        result = get_config_id_set(
            conn, "roles", guild_id=100, allow_legacy_fallback=False
        )
        assert result == set()


def test_get_config_id_set_strict_mode_still_returns_guild_specific(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 10)"
        )
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (100, 'roles', 77)"
        )
        result = get_config_id_set(
            conn, "roles", guild_id=100, allow_legacy_fallback=False
        )
        assert result == {77}


# ── open_db_immediate's hand-driven transaction ───────────────────────
#
# open_db_immediate opens with isolation_level=None and issues BEGIN
# IMMEDIATE / COMMIT itself. A conn.commit() inside the block therefore
# ends the transaction early and leaves the exit-time COMMIT with nothing
# to commit — sqlite3 raises "cannot commit - no transaction is active",
# and the handler's ROLLBACK raises on top, masking it. The write has
# already landed by then, so the caller sees a half-finished operation:
# survivor's first live join charged and enrolled the member, then threw
# before refreshing the panel, echoing, or granting the role (2026-08-18).

def test_open_db_immediate_commits_on_clean_exit(tmp_path):
    db = tmp_path / "t.db"
    with open_db_immediate(db) as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    with open_db(db) as conn:
        assert [r["v"] for r in conn.execute("SELECT v FROM t")] == [1]


def test_open_db_immediate_rolls_back_on_error(tmp_path):
    db = tmp_path / "t.db"
    with open_db_immediate(db) as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(ValueError):
        with open_db_immediate(db) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("boom")
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_inner_commit_breaks_open_db_immediate(tmp_path):
    """The failure mode the contract test below exists to prevent."""
    db = tmp_path / "t.db"
    with pytest.raises(sqlite3.OperationalError):
        with open_db_immediate(db) as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.commit()


def test_no_source_commits_inside_open_db_immediate():
    """No `with open_db_immediate(...)` block in src/ may commit itself.

    Static because the breakage only surfaces at runtime, deep inside a
    view callback, where the write has already succeeded and only the
    follow-up work is lost.
    """
    src = REPO_ROOT / "src"
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "open_db_immediate" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            names = {
                item.context_expr.func.id
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
            }
            if "open_db_immediate" not in names:
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "commit"
                ):
                    offenders.append(
                        f"{path.relative_to(src)}:{inner.lineno}"
                    )
    assert not offenders, (
        "commit() inside an open_db_immediate block (the context manager "
        f"already commits): {', '.join(offenders)}"
    )


# ── PRAGMA optimize on close ──────────────────────────────────────────
#
# SQLite only re-plans against the statistics in sqlite_stat1, and nothing
# here ran ANALYZE on a schedule — so a database that grew from empty to
# ~800 MB kept being queried with plans chosen for its early shape. Both
# helpers now run `PRAGMA optimize` immediately before closing, which is
# SQLite's own prescription: a no-op unless a table this connection touched
# has drifted far enough to be worth re-analysing.

class _RecordingConnection(sqlite3.Connection):
    """A real connection that remembers every statement executed on it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.statements: list[str] = []

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        self.statements.append(sql)
        return super().execute(sql, *args, **kwargs)


@pytest.fixture
def prod_profile(monkeypatch):
    """Open connections the way the bot does: the suite's SQLite profile off.

    ``tests/conftest.py`` turns the profile on for every test; the tests
    below are about what production connections do, so they turn it off.
    """
    monkeypatch.delenv(TEST_PROFILE_ENV, raising=False)


@pytest.fixture
def recorded(monkeypatch):
    """Make both helpers open ``_RecordingConnection``s, and collect them."""
    opened: list[_RecordingConnection] = []
    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        conn = real_connect(*args, factory=_RecordingConnection, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    return opened


def test_open_db_optimizes_before_closing(tmp_path, recorded, prod_profile):
    with open_db(tmp_path / "t.db") as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
    (conn,) = recorded
    assert conn.statements[-1] == "PRAGMA optimize"


def test_open_db_immediate_optimizes_before_closing(tmp_path, recorded, prod_profile):
    with open_db_immediate(tmp_path / "t.db") as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
    (conn,) = recorded
    assert conn.statements[-1] == "PRAGMA optimize"


def test_optimize_runs_even_when_the_block_raised(tmp_path, recorded, prod_profile):
    """The rollback path closes too, so it optimizes too."""
    with pytest.raises(ValueError):
        with open_db_immediate(tmp_path / "t.db") as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            raise ValueError("boom")
    (conn,) = recorded
    assert conn.statements[-1] == "PRAGMA optimize"


def test_a_failing_optimize_neither_raises_nor_leaks_the_connection(prod_profile):
    """The guard that makes this safe to put in a ``finally``.

    ANALYZE takes the write lock, so a contended database is precisely when
    optimize fails — and it must not turn a committed unit of work into an
    exception, nor skip the close and strand a WAL read mark.
    """
    closed: list[bool] = []

    class _Conn:
        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            closed.append(True)

    _optimize_and_close(_Conn())  # type: ignore[arg-type]
    assert closed == [True]


def test_a_failing_optimize_does_not_mask_the_original_error(tmp_path, monkeypatch, prod_profile):
    """A block that raised still surfaces *its* exception, not the pragma's."""

    class _RefusesToOptimize(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if sql == "PRAGMA optimize":
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args, **kwargs)

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *a, **kw: real_connect(*a, factory=_RefusesToOptimize, **kw),
    )

    with pytest.raises(ValueError, match="boom"):
        with open_db_immediate(tmp_path / "t.db") as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            raise ValueError("boom")


# ── the test-only SQLite profile ──────────────────────────────────────
# tests/conftest.py sets DK_SQLITE_TEST_PROFILE=1 for the whole suite. The
# rule that matters is the inverse of "never ship an unenforced toggle": the
# profile must be impossible to reach from a bot process, and the default path
# must be byte-for-byte what it was.


@pytest.mark.parametrize("helper", [open_db, open_db_immediate])
def test_prod_connections_are_wal_with_normal_sync_and_optimize(tmp_path, recorded, prod_profile, helper):
    with helper(tmp_path / "t.db") as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    (conn,) = recorded
    assert conn.statements[-1] == "PRAGMA optimize"


@pytest.mark.parametrize("helper", [open_db, open_db_immediate])
def test_the_profile_drops_fsync_and_optimize_but_keeps_wal(tmp_path, recorded, helper):
    with helper(tmp_path / "t.db") as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 0  # OFF
    (conn,) = recorded
    assert "PRAGMA optimize" not in conn.statements


def test_the_flag_alone_is_not_enough_outside_pytest(tmp_path, recorded, monkeypatch):
    """A shell that exported the flag and then started the bot gets prod behaviour."""
    monkeypatch.setenv(TEST_PROFILE_ENV, "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with open_db(tmp_path / "t.db") as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    (conn,) = recorded
    assert conn.statements[-1] == "PRAGMA optimize"


def test_any_other_flag_value_is_prod(tmp_path, recorded, monkeypatch):
    monkeypatch.setenv(TEST_PROFILE_ENV, "true")
    with open_db(tmp_path / "t.db") as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    (conn,) = recorded
    assert conn.statements[-1] == "PRAGMA optimize"


# ── fixed_offset_tz ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "offset_hours, expected_hours",
    [
        (0.0, 0.0),
        (-7.0, -7.0),       # the main guild's actual dial
        (5.5, 5.5),         # India — fractional offsets are real
        (14.0, 14.0),       # Kiribati, the real-world maximum
        (-12.0, -12.0),
    ],
)
def test_legitimate_offsets_pass_through(offset_hours, expected_hours):
    assert fixed_offset_tz(offset_hours).utcoffset(None).total_seconds() == (
        expected_hours * 3600
    )


@pytest.mark.parametrize("offset_hours", [24.0, 99.0, 1e6, -24.0, -99.0, -1e6])
def test_out_of_range_offsets_clamp_instead_of_raising(offset_hours):
    """``timezone()`` raises at or past ±24h; nothing validates the stored dial.

    ``get_tz_offset_hours`` returns any float the config table parses, so an
    admin typo of 99 would otherwise raise inside whichever scheduler read it
    next — a crash in a shared loop rather than one feature's wrong time.
    """
    tz = fixed_offset_tz(offset_hours)
    seconds = tz.utcoffset(None).total_seconds()
    assert abs(seconds) < 24 * 3600
    assert (seconds > 0) == (offset_hours > 0)


@pytest.mark.parametrize(
    "stored, legacy, strict, expected",
    [
        pytest.param("42", None, False, 42, id="stored"),
        pytest.param(None, None, False, 0, id="unset"),
        pytest.param("", None, False, 0, id="blank"),
        pytest.param("abc", None, False, 0, id="not-a-number"),
        pytest.param(None, "999", False, 999, id="legacy-fallback"),
        pytest.param(None, "999", True, 0, id="strict-skips-legacy"),
    ],
)
def test_get_config_int(config_db, stored, legacy, strict, expected):
    with open_db(config_db) as conn:
        if stored is not None:
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (100, 'k', ?)", (stored,)
            )
        if legacy is not None:
            conn.execute("INSERT INTO config (guild_id, key, value) VALUES (0, 'k', ?)", (legacy,))
        got = get_config_int(conn, "k", 100, allow_legacy_fallback=not strict)
    assert got == expected
