"""Age-based retention for the archive and the behavioural stores.

Built by the 2026-09-05 retention & disclosure review. That review's finding
was not that anything overruns a stated period — every bounded claim in
``docs/data_register.md`` is enforced and honoured — but that 44 of the
register's 78 rows said "indefinite", and almost none of those recorded a
decision. They recorded a default nobody had revisited.

**The sweep ships disabled**, behind a per-guild switch — see the config key
below. It is on in no guild. That is not the original intent — the first draft shipped
it enabled, on the argument that a rule nobody switches on is not a rule — but
the period below rests on a premise that turned out to be wrong, and a sweep
that deletes on a wrong number is worse than one that waits:

    The first draft said "no report reads further back than 90 days"
    (``contributors_service.WINDOW_DAYS``). That was the longest *aggregate*
    consumer, not the longest consumer: the Connection Graph's replay read
    ``user_interactions_log`` over 210 days, so a 180-day pass would have
    blanked its earliest weeks.

That premise went with the social graph on 2026-09-13
(``docs/plans/remove-social-graph.md``): the replay, its log, and the other two
edge stores were dropped by migration 229. ``BEHAVIOURAL_RETENTION_DAYS`` is
still **provisional** in the sense that nobody has re-decided it since, and the
dial still ships off. Re-decide it against ``ping_events`` — the only table
left — before enabling it.

* **Message content** had its own 365-day arm here until 2026-09-13, when the
  bot stopped storing message text at all
  (``docs/plans/remove-message-storage.md``). There is nothing left to redact.

* **Behavioural stores — 180 days, provisional.** ``ping_events``: who pinged
  which role, when. ``reaction_log``, ``user_interactions_log`` and
  ``voice_follow_log`` used to sit here too; they were dropped outright rather
  than aged out. No rollup to fall back on, so a deletion here is a real
  loss.

``member_events`` is deliberately **not** in that list. It is the join/leave
record, and ``reports_data.get_greeter_log_sessions`` reads it from whatever
``since_ts`` the caller asks for, with no retention floor. A period on that
table would silently cut any report whose range reaches past it. The table is 1,497 rows. It stays.
"""

from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger(__name__)

#: Behavioural rows older than this are deleted outright. Chosen as double the
#: longest *aggregate* consumer (90 days); provisional — see the module
#: docstring.
BEHAVIOURAL_RETENTION_DAYS = 180

#: Per-guild switch. Absent means **off**.
BEHAVIOURAL_RETENTION_CONFIG_KEY = "behavioural_retention_enabled"

#: ``(table, timestamp column)``. Every one of these carries ``guild_id``, so
#: the sweep is per-guild and a guild that opts out is genuinely untouched.
#: Adding a row here needs a matching ``docs/data_register.md`` update and a
#: line in ``manual.html`` §Your Data & Privacy — the register row is the
#: record of processing, and an undisclosed sweep is as much a defect as an
#: undisclosed store.
BEHAVIOURAL_TABLES: tuple[tuple[str, str], ...] = (
    ("ping_events", "ts"),
)

#: Rows deleted per statement. Nibbling keeps the write lock short
#: enough that message ingest does not stall behind it. That only holds
#: because each chunk is committed — see :func:`sweep_behavioural`; chunking
#: inside one open transaction would split the statements but not the lock.
DELETE_CHUNK = 20_000


def _switch(conn: sqlite3.Connection, key: str, guild_id: int) -> bool:
    row = conn.execute(
        "SELECT value FROM config WHERE key = ? AND guild_id = ?",
        (key, guild_id),
    ).fetchone()
    return bool(row) and str(row[0]).strip() in ("1", "true", "True", "on", "yes")


def behavioural_retention_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True only where a guild has switched the behavioural sweep on.

    Absent means off, and it should stay off: ``BEHAVIOURAL_RETENTION_DAYS`` is
    provisional — see the module docstring.
    """
    return _switch(conn, BEHAVIOURAL_RETENTION_CONFIG_KEY, guild_id)


def behavioural_cutoff(*, older_than_days: int, now: float | None = None) -> float:
    """Unix timestamp before which behavioural rows are past their period."""
    return (time.time() if now is None else now) - older_than_days * 86400


def prunable_behavioural_count(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    older_than_days: int = BEHAVIOURAL_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """How many rows each behavioural table would lose, without deleting any.

    A read-only counterpart to :func:`sweep_behavioural`, so the size of a
    pass can be measured — from a console or an ops script — without waiting
    for the sweep's own log line. Nothing on the dashboard calls it yet.
    """
    cutoff = behavioural_cutoff(older_than_days=older_than_days, now=now)
    counts: dict[str, int] = {}
    for table, ts_col in BEHAVIOURAL_TABLES:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE guild_id = ? AND {ts_col} < ?",
            (guild_id, cutoff),
        ).fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


def sweep_behavioural(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    older_than_days: int = BEHAVIOURAL_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """Delete behavioural rows past their period. Returns per-table counts.

    Honours the guild's off switch — a disabled guild returns all-zero rather
    than raising, because "nothing to do" is the ordinary case here, not an
    error worth a caller's attention.

    **Commits between chunks.** SQLite's write lock is held for a whole
    transaction, so ``DELETE_CHUNK`` only bounds the stall if each chunk is
    released; without the commit the caller's transaction would pin the lock
    for the entire drain. The trade is that a mid-sweep failure leaves the
    earlier chunks deleted — which is the right way round for retention: the
    work is idempotent and the next pass simply resumes.
    """
    if not behavioural_retention_enabled(conn, guild_id):
        return {table: 0 for table, _ in BEHAVIOURAL_TABLES}

    cutoff = behavioural_cutoff(older_than_days=older_than_days, now=now)
    deleted: dict[str, int] = {}
    for table, ts_col in BEHAVIOURAL_TABLES:
        total = 0
        while True:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN ("
                f"  SELECT rowid FROM {table}"
                f"  WHERE guild_id = ? AND {ts_col} < ? LIMIT {DELETE_CHUNK}"
                f")",
                (guild_id, cutoff),
            )
            n = max(cur.rowcount, 0)
            total += n
            # Release the write lock between chunks — see the docstring.
            conn.commit()
            if n < DELETE_CHUNK:
                break
        deleted[table] = total
    return deleted


def run_retention(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    behavioural_days: int = BEHAVIOURAL_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """Run the sweep for one guild. Returns ``{table: rows_deleted}``.

    The shape is the same whether or not the guild switched it on, so a caller
    reading a table key never has to know how a guild is configured.
    """
    return sweep_behavioural(
        conn, guild_id, older_than_days=behavioural_days, now=now
    )
