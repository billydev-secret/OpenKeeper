from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from collections.abc import Generator, Iterable
from datetime import timedelta, timezone
from pathlib import Path
from typing import TypedDict

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sql_identifier(name: str) -> str:
    """Return ``name`` if it is a bare SQL identifier, else raise ``ValueError``.

    Table and column names cannot be bound as query parameters, so any helper
    that works across several tables has to format them into the statement.
    Callers pass literals today; a shared builder is precisely where that
    stops being self-evident, and the guard is cheaper than the alternative.
    """
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ValueError(f"not a valid SQL identifier: {name!r}")
    return name


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse string to boolean value."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


#: Set to "1" by ``tests/conftest.py``. Honoured only while pytest is running
#: a test (it exports ``PYTEST_CURRENT_TEST`` for the duration of each one), so
#: a shell that inherited the variable can never hand it to a running bot.
TEST_PROFILE_ENV = "DK_SQLITE_TEST_PROFILE"


def _test_profile() -> bool:
    """True inside the suite's SQLite profile: no fsync, no optimize-on-close.

    The suite opens tens of thousands of short-lived connections on throwaway
    template copies (``tests/db_template.py``). Every fsync and every ANALYZE
    there is pure cost — on the remote runner they saturated the SSD while
    the cores idled. Two conditions on purpose: the conftest sets the flag,
    pytest sets the other, and a bot process satisfies neither.
    ``tests/core/test_db_utils.py`` proves both halves.
    """
    return (
        os.environ.get(TEST_PROFILE_ENV) == "1"
        and os.environ.get("PYTEST_CURRENT_TEST") is not None
    )


def _apply_pragmas(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """The connection profile both helpers share (``test_db_utils_open_db_contract``)."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    # NORMAL survives a process crash and, in WAL, a power loss up to the last
    # checkpoint. OFF under tests: nothing in tmp needs to survive anything,
    # and WAL is kept so lock semantics match the bot's exactly.
    conn.execute("PRAGMA synchronous=OFF" if _test_profile() else "PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA mmap_size=268435456")


def _optimize_and_close(conn: sqlite3.Connection) -> None:
    """Run ``PRAGMA optimize``, then close — never letting either raise.

    SQLite's own advice is to run ``optimize`` on every connection just before
    closing it: it is a no-op unless a table this connection queried has drifted
    far enough from its ``sqlite_stat1`` row to be worth re-``ANALYZE``-ing, so
    the cost is nothing on almost every call and an occasional small ANALYZE on
    the rest. Without it the planner keeps working from whatever statistics the
    last manual ANALYZE left behind, which for a database that has grown from
    empty to ~800 MB means plans chosen for a table shape that no longer exists.

    Swallowing the error is the point of the helper. This runs in a ``finally``,
    after the caller's transaction has already committed, so anything raised
    here would replace a completed unit of work with an exception — or mask the
    exception that sent us into the ``finally`` in the first place. ANALYZE
    needs the write lock, so a busy database is exactly when it would fail, and
    a skipped optimization is not worth a single failed command. The close still
    happens regardless: a leaked connection would hold its WAL read mark open.

    Under the test profile the optimize is skipped outright: a template
    copy's statistics are never read by a later connection.
    """
    if _test_profile():
        conn.close()
        return
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:  # busy, read-only, mid-rollback — all fine to skip
        pass
    finally:
        conn.close()


@contextlib.contextmanager
def open_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection, yield it inside a transaction, then close.

    Uses deferred transactions so ``with open_db(...) as conn:`` commits
    on success and rolls back on exception.
    """
    conn = sqlite3.connect(db_path, timeout=30, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, busy_timeout_ms=30000)
    try:
        with conn:  # BEGIN … COMMIT/ROLLBACK
            yield conn
    finally:
        _optimize_and_close(conn)


@contextlib.contextmanager
def open_db_immediate(
    db_path: Path, *, busy_timeout_ms: int = 30000
) -> Generator[sqlite3.Connection, None, None]:
    """Like :func:`open_db`, but the transaction starts with ``BEGIN IMMEDIATE``.

    The write lock is taken up front — before any read — so a read-then-write
    sequence can't run on a stale WAL snapshot and hit ``SQLITE_BUSY_SNAPSHOT``.
    Concurrent writers instead queue on the lock (``busy_timeout_ms``) and each
    re-reads the latest committed state. Use this for read-validate-then-write
    money paths that must serialize (e.g. auction bids); plain ``open_db`` is
    fine for write-first or single-statement work. Callers that retry (e.g.
    ``place_bid_now``) should pass a *short* ``busy_timeout_ms`` so a contended
    lock fails fast and retries instead of blocking a worker for the full 30s.
    """
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, busy_timeout_ms=busy_timeout_ms)
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        _optimize_and_close(conn)


def get_config_value(
    conn: sqlite3.Connection,
    key: str,
    default: str,
    guild_id: int = 0,
    *,
    allow_legacy_fallback: bool = True,
) -> str:
    """Retrieve configuration value from config table.

    Tries guild-specific first, then (unless ``allow_legacy_fallback`` is False)
    falls back to ``guild_id=0`` (legacy). Pass ``allow_legacy_fallback=False``
    for non-home guilds so an unconfigured guild gets real defaults instead of
    silently inheriting the legacy/home ``guild_id=0`` values.
    """
    row = conn.execute(
        "SELECT value FROM config WHERE guild_id = ? AND key = ?", (guild_id, key)
    ).fetchone()
    if row:
        return row["value"]
    if guild_id != 0 and allow_legacy_fallback:
        # Fall back to legacy (guild_id=0) row
        row = conn.execute(
            "SELECT value FROM config WHERE guild_id = 0 AND key = ?", (key,)
        ).fetchone()
        if row:
            return row["value"]
    return default


def get_config_int(
    conn: sqlite3.Connection,
    key: str,
    guild_id: int = 0,
    *,
    allow_legacy_fallback: bool = True,
) -> int:
    """An integer config value (usually a channel or role id); 0 when unset,
    blank or not a number. The one copy of what four modules each wrote as a
    private ``_int_config``."""
    raw = get_config_value(
        conn, key, "0", guild_id, allow_legacy_fallback=allow_legacy_fallback
    )
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def get_tz_offset_hours(conn: sqlite3.Connection, guild_id: int = 0) -> float:
    """Return the guild's configured UTC offset (hours), defaulting to 0.0."""
    raw = get_config_value(conn, "tz_offset_hours", "0", guild_id)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


#: One minute short of a day — ``timezone`` refuses anything at or past ±24h,
#: and the real-world extreme is +14, so this only ever catches nonsense.
_MAX_TZ_OFFSET = timedelta(hours=24) - timedelta(minutes=1)


def fixed_offset_tz(offset_hours: float) -> timezone:
    """Return a guild's UTC offset as a ``timezone``, clamped to a legal range.

    This repo has no per-guild IANA zone: a guild's local time is the fixed
    offset in ``config.tz_offset_hours``, and nothing validates that value on
    the way in — ``get_tz_offset_hours`` above accepts any float it can parse.
    ``timezone()`` raises outside ±24h, so an absurd stored offset would take
    down whichever scheduler read it next, which is a worse failure than the
    wrong-but-quiet time the hand-rolled subtraction used to produce. Clamping
    keeps a bad dial contained to the feature whose dial it is.
    """
    delta = timedelta(hours=offset_hours)
    if delta > _MAX_TZ_OFFSET:
        delta = _MAX_TZ_OFFSET
    elif delta < -_MAX_TZ_OFFSET:
        delta = -_MAX_TZ_OFFSET
    return timezone(delta)


def set_config_value(
    conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0
) -> None:
    """Upsert a config value, scoped to the given guild."""
    conn.execute(
        """
        INSERT INTO config (guild_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value
        """,
        (guild_id, key, value),
    )


def delete_config_value(
    conn: sqlite3.Connection, key: str, guild_id: int = 0
) -> None:
    """Delete a single config key for the given guild."""
    conn.execute(
        "DELETE FROM config WHERE guild_id = ? AND key = ?", (guild_id, key)
    )


def add_config_id(
    conn: sqlite3.Connection, bucket: str, value: int, guild_id: int = 0
) -> None:
    """Insert an ID value into a bucket for the given guild."""
    conn.execute(
        "INSERT OR IGNORE INTO config_ids (guild_id, bucket, value) VALUES (?, ?, ?)",
        (guild_id, bucket, value),
    )


def remove_config_id(
    conn: sqlite3.Connection, bucket: str, value: int, guild_id: int = 0
) -> None:
    """Remove an ID value from a bucket for the given guild."""
    conn.execute(
        "DELETE FROM config_ids WHERE guild_id = ? AND bucket = ? AND value = ?",
        (guild_id, bucket, value),
    )


def clear_config_id_bucket(
    conn: sqlite3.Connection, bucket: str, guild_id: int = 0
) -> None:
    """Remove all ID values from a bucket for the given guild."""
    conn.execute(
        "DELETE FROM config_ids WHERE guild_id = ? AND bucket = ?",
        (guild_id, bucket),
    )


def replace_config_id_bucket(
    conn: sqlite3.Connection,
    bucket: str,
    values: Iterable[int | str],
    guild_id: int = 0,
) -> None:
    """Replace a bucket's entire contents with ``values``.

    The clear-then-insert pair this composes is what "the dashboard sent the
    full list for this bucket" always means; callers pass the raw strings a
    JSON body carries and the int coercion happens here.
    """
    clear_config_id_bucket(conn, bucket, guild_id)
    for value in values:
        add_config_id(conn, bucket, int(value), guild_id)


def get_config_id_set(
    conn: sqlite3.Connection,
    bucket: str,
    guild_id: int = 0,
    *,
    allow_legacy_fallback: bool = True,
) -> set[int]:
    """Retrieve set of integer IDs from config_ids table for given bucket.

    Tries guild-specific first, then (unless ``allow_legacy_fallback`` is False)
    falls back to ``guild_id=0`` (legacy). Pass ``allow_legacy_fallback=False``
    for non-home guilds so an unconfigured guild gets an empty set instead of
    silently inheriting the legacy/home ``guild_id=0`` values.
    """
    rows = conn.execute(
        "SELECT value FROM config_ids WHERE guild_id = ? AND bucket = ? ORDER BY value",
        (guild_id, bucket),
    ).fetchall()
    if rows:
        return {int(row["value"]) for row in rows}
    if guild_id != 0 and allow_legacy_fallback:
        rows = conn.execute(
            "SELECT value FROM config_ids WHERE guild_id = 0 AND bucket = ? ORDER BY value",
            (bucket,),
        ).fetchall()
    return {int(row["value"]) for row in rows}


# ---------------------------------------------------------------------------
# Grant roles
# ---------------------------------------------------------------------------


class GrantRoleConfig(TypedDict):
    grant_name: str
    label: str
    role_id: int
    log_channel_id: int
    announce_channel_id: int
    grant_message: str
    required_role_id: int


_DEFAULT_GRANT_ROLES: list[tuple[str, str]] = [
    ("denizen", "Denizen"),
    ("nsfw", "NSFW"),
    ("veteran", "Veteran"),
    ("kink", "Kink"),
    ("goldengirl", "Golden Girl"),
]


def init_grant_role_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grant_roles (
            guild_id            INTEGER NOT NULL,
            grant_name          TEXT NOT NULL,
            label               TEXT NOT NULL DEFAULT '',
            role_id             INTEGER NOT NULL DEFAULT 0,
            log_channel_id      INTEGER NOT NULL DEFAULT 0,
            announce_channel_id INTEGER NOT NULL DEFAULT 0,
            grant_message       TEXT NOT NULL DEFAULT '',
            required_role_id    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, grant_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grant_role_permissions (
            guild_id    INTEGER NOT NULL,
            grant_name  TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('user', 'role')),
            entity_id   INTEGER NOT NULL,
            PRIMARY KEY (guild_id, grant_name, entity_type, entity_id)
        )
        """
    )


def migrate_grant_roles(conn: sqlite3.Connection, guild_id: int) -> None:
    """One-time migration: copy legacy config keys into grant_roles table."""
    existing = conn.execute(
        "SELECT 1 FROM grant_roles WHERE guild_id = ? LIMIT 1", (guild_id,)
    ).fetchone()
    if existing:
        return

    for grant_name, label in _DEFAULT_GRANT_ROLES:
        role_id = int(get_config_value(conn, f"{grant_name}_role_id", "0") or 0)
        log_ch = int(get_config_value(conn, f"{grant_name}_log_channel_id", "0") or 0)
        ann_ch = int(
            get_config_value(conn, f"{grant_name}_announce_channel_id", "0") or 0
        )
        msg = get_config_value(conn, f"{grant_name}_grant_message", "")
        conn.execute(
            """
            INSERT OR IGNORE INTO grant_roles
                (guild_id, grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, grant_name, label, role_id, log_ch, ann_ch, msg),
        )

    # Migrate greeter role as a permission for all grant roles
    greeter_id = int(get_config_value(conn, "greeter_role_id", "0") or 0)
    if greeter_id > 0:
        for grant_name, _ in _DEFAULT_GRANT_ROLES:
            conn.execute(
                "INSERT OR IGNORE INTO grant_role_permissions (guild_id, grant_name, entity_type, entity_id) VALUES (?, ?, 'role', ?)",
                (guild_id, grant_name, greeter_id),
            )


def get_grant_roles(
    conn: sqlite3.Connection, guild_id: int
) -> dict[str, GrantRoleConfig]:
    rows = conn.execute(
        "SELECT grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message, required_role_id "
        "FROM grant_roles WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {
        row["grant_name"]: GrantRoleConfig(
            grant_name=row["grant_name"],
            label=row["label"],
            role_id=row["role_id"],
            log_channel_id=row["log_channel_id"],
            announce_channel_id=row["announce_channel_id"],
            grant_message=row["grant_message"],
            required_role_id=row["required_role_id"],
        )
        for row in rows
    }


def upsert_grant_role(
    conn: sqlite3.Connection,
    guild_id: int,
    grant_name: str,
    *,
    label: str,
    role_id: int,
    log_channel_id: int,
    announce_channel_id: int,
    grant_message: str,
    required_role_id: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO grant_roles
            (guild_id, grant_name, label, role_id, log_channel_id, announce_channel_id, grant_message, required_role_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, grant_name) DO UPDATE SET
            label=excluded.label, role_id=excluded.role_id,
            log_channel_id=excluded.log_channel_id,
            announce_channel_id=excluded.announce_channel_id,
            grant_message=excluded.grant_message,
            required_role_id=excluded.required_role_id
        """,
        (
            guild_id,
            grant_name,
            label,
            role_id,
            log_channel_id,
            announce_channel_id,
            grant_message,
            required_role_id,
        ),
    )


def delete_grant_role(conn: sqlite3.Connection, guild_id: int, grant_name: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM grant_roles WHERE guild_id = ? AND grant_name = ?",
        (guild_id, grant_name),
    )
    conn.execute(
        "DELETE FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ?",
        (guild_id, grant_name),
    )
    return cursor.rowcount > 0


def get_grant_permissions(
    conn: sqlite3.Connection,
    guild_id: int,
    grant_name: str,
) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT entity_type, entity_id FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ?",
        (guild_id, grant_name),
    ).fetchall()
    return [(row["entity_type"], row["entity_id"]) for row in rows]


def add_grant_permission(
    conn: sqlite3.Connection,
    guild_id: int,
    grant_name: str,
    entity_type: str,
    entity_id: int,
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO grant_role_permissions (guild_id, grant_name, entity_type, entity_id) VALUES (?, ?, ?, ?)",
        (guild_id, grant_name, entity_type, entity_id),
    )
    return (cur.rowcount or 0) > 0


def remove_grant_permission(
    conn: sqlite3.Connection,
    guild_id: int,
    grant_name: str,
    entity_type: str,
    entity_id: int,
) -> bool:
    cur = conn.execute(
        "DELETE FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ? AND entity_type = ? AND entity_id = ?",
        (guild_id, grant_name, entity_type, entity_id),
    )
    return (cur.rowcount or 0) > 0


def can_use_grant(
    conn: sqlite3.Connection,
    guild_id: int,
    grant_name: str,
    member_id: int,
    role_ids: list[int],
) -> bool:
    """Check if a member (by ID and their role IDs) has permission for a grant role."""
    # Check user permission
    row = conn.execute(
        "SELECT 1 FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ? AND entity_type = 'user' AND entity_id = ?",
        (guild_id, grant_name, member_id),
    ).fetchone()
    if row:
        return True
    # Check role permissions
    if role_ids:
        placeholders = ",".join("?" for _ in role_ids)
        row = conn.execute(
            f"SELECT 1 FROM grant_role_permissions WHERE guild_id = ? AND grant_name = ? AND entity_type = 'role' AND entity_id IN ({placeholders})",
            [guild_id, grant_name, *role_ids],
        ).fetchone()
        if row:
            return True
    return False
