"""Tests for the age-based retention sweep (2026-09-05 review).

The unit under test is ``retention_service``. One property matters more than
the row counts and is asserted repeatedly: **the window is a floor, not a
suggestion** — nothing inside it is ever touched.

There used to be a second, message arm that nulled stored text after 365 days.
It went on 2026-09-13 with message storage itself
(docs/plans/remove-message-storage.md): with no text written, there was
nothing left for it to redact.
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from bot_modules.core.db_utils import set_config_value
from bot_modules.privacy import retention_service
from bot_modules.shared.message_store import init_message_tables

NOW = 2_000_000_000.0
DAY = 86400.0
GUILD = 1
OTHER_GUILD = 2


def _conn(*, enabled: bool = True) -> sqlite3.Connection:
    """A schema plus, by default, retention switched ON for ``GUILD``.

    The dial ships off, so a sweep test that forgot to enable it would pass
    vacuously — "nothing was deleted" is trivially true when nothing runs.
    Enabling here by default makes the vacuous case impossible to write by
    accident; the two tests that are *about* the default pass ``enabled=False``.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_message_tables(conn)
    conn.execute(
        """
        CREATE TABLE config (
            guild_id INTEGER NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
        """
    )
    # ping_events already exists with its real schema when init_message_tables
    # owns it. Anything it does not create is stubbed minimally: the sweep
    # reads guild_id and the timestamp and nothing else.
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"guild_id INTEGER NOT NULL, user_id INTEGER, "
            f"{ts_col} REAL NOT NULL)"
        )
    if enabled:
        set_config_value(
            conn, retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY, "1", GUILD
        )
    return conn


#: Distinct values for id-ish columns, so a real primary key does not silently
#: collapse several inserted rows into one and make a sweep look more effective
#: than it was.
_seq = itertools.count(1)


def _behav(conn, table, ts_col, *, age_days: float, guild_id: int = GUILD) -> None:
    """Insert one row, filling whatever the real schema demands.

    A table may be created by ``init_message_tables`` with its production
    schema and several NOT NULL columns, or be a stub. Rather than keep a
    per-table insert list in step with both, fill every NOT NULL
    column that has no default with a unique integer — the sweep only ever
    reads ``guild_id`` and the timestamp, so the rest just has to be valid.
    """
    n = next(_seq)
    values = {"guild_id": guild_id, ts_col: NOW - age_days * DAY}
    for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
        name = col["name"]
        if name in values:
            continue
        if col["notnull"] and col["dflt_value"] is None:
            values[name] = n
    cols = ", ".join(values)
    marks = ", ".join("?" * len(values))
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
    )


# ── the switch ────────────────────────────────────────────────────────


def test_absent_config_row_means_retention_is_off():
    """Ships dark: nothing sweeps until a guild opts in."""
    conn = _conn(enabled=False)
    assert retention_service.behavioural_retention_enabled(conn, GUILD) is False


@pytest.mark.parametrize("stored,expected", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("0", False), ("", False), ("no", False),
])
def test_switch_reads_truthy_values_as_enabled(stored, expected):
    conn = _conn(enabled=False)
    set_config_value(
        conn, retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY, stored, GUILD
    )
    assert retention_service.behavioural_retention_enabled(conn, GUILD) is expected


def test_member_events_is_not_swept():
    """The join/leave reports read member_events with no window at all.

    ``reports_data`` pulls every join and leave stamp unwindowed, so sweeping
    this table would silently truncate those reports to the retention period.
    It must stay out of the list however the periods are re-decided.
    """
    swept = {table for table, _ in retention_service.BEHAVIOURAL_TABLES}
    assert "member_events" not in swept


def test_disabled_guild_keeps_everything():
    conn = _conn(enabled=False)
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=900)

    result = retention_service.run_retention(conn, GUILD, now=NOW)

    # Same keys as an enabled pass, all zero — a caller reading result[table]
    # must not KeyError only for the guilds that opted out.
    assert result == {table: 0 for table, _ in retention_service.BEHAVIOURAL_TABLES}
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_one_guild_opting_out_does_not_shield_another():
    conn = _conn()
    set_config_value(
        conn, retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY, "0", OTHER_GUILD
    )
    table, ts_col = retention_service.BEHAVIOURAL_TABLES[0]
    _behav(conn, table, ts_col, age_days=900, guild_id=GUILD)
    _behav(conn, table, ts_col, age_days=900, guild_id=OTHER_GUILD)

    retention_service.run_retention(conn, GUILD, now=NOW)
    retention_service.run_retention(conn, OTHER_GUILD, now=NOW)

    rows = conn.execute(f"SELECT guild_id FROM {table}").fetchall()
    assert [r["guild_id"] for r in rows] == [OTHER_GUILD]


def test_run_retention_has_no_message_arm():
    """The message arm was removed with message storage; a pass reports only
    the behavioural tables, so no caller can mistake it for a text sweep."""
    conn = _conn()
    result = retention_service.run_retention(conn, GUILD, now=NOW)
    assert set(result) == {table for table, _ in retention_service.BEHAVIOURAL_TABLES}
    assert not hasattr(retention_service, "MESSAGE_RETENTION_CONFIG_KEY")


# ── the behavioural arm: deletion ─────────────────────────────────────


@pytest.mark.parametrize(
    "table,ts_col", retention_service.BEHAVIOURAL_TABLES
)
def test_each_behavioural_table_is_swept(table, ts_col):
    conn = _conn()
    _behav(conn, table, ts_col, age_days=200)
    _behav(conn, table, ts_col, age_days=10)

    deleted = retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert deleted[table] == 1
    remaining = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert remaining == 1, "the row inside the window must survive"


@pytest.mark.parametrize("age_days", [0, 90, 179, 179.9])
def test_behavioural_row_inside_the_window_is_untouched(age_days):
    conn = _conn()
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=age_days)

    deleted = retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert not any(deleted.values())
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_behavioural_sweep_is_scoped_to_its_guild():
    conn = _conn()
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=200, guild_id=GUILD)
        _behav(conn, table, ts_col, age_days=200, guild_id=OTHER_GUILD)

    retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        rows = conn.execute(
            f"SELECT guild_id FROM {table}"
        ).fetchall()
        assert [r["guild_id"] for r in rows] == [OTHER_GUILD]


def test_sweep_deletes_past_a_single_chunk(monkeypatch):
    """The chunked delete loop must drain, not stop at one chunk."""
    monkeypatch.setattr(retention_service, "DELETE_CHUNK", 10)
    conn = _conn()
    table, ts_col = retention_service.BEHAVIOURAL_TABLES[0]
    for _ in range(25):
        _behav(conn, table, ts_col, age_days=200)

    deleted = retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert deleted[table] == 25
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# ── counting without deleting ─────────────────────────────────────────


def test_prunable_count_reports_without_deleting():
    conn = _conn()
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=200)

    counts = retention_service.prunable_behavioural_count(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert all(n == 1 for n in counts.values())
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


