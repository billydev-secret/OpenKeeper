"""Migration-only schema parity: the schema built from migrations ALONE must
match what prod has via the in-code init_* helpers.

Three columns/indexes historically existed only in init_* helpers (a second,
unmigrated schema source), so a fresh install / disaster-recovery DB built from
migrations was missing them:

* ``auto_delete_rules.media_only`` (init_auto_delete_tables)
* ``idx_interactions_log_dedup`` partial UNIQUE (init_interaction_tables — no
  prod callers at all)
* every ``xp_events`` index (init_xp_tables — no src callers)

Migration 115 folds them in. The ``_pre115`` tests assert they are absent
before 115 (so the migration is doing real work / would fail before the fix);
the full-migration tests assert they are present after.
"""

from __future__ import annotations

import sqlite3

import migrations

GUILD = 42


def _apply_all(db_path) -> None:
    migrations.apply_migrations_sync(db_path)


def _apply_before(db_path, monkeypatch, number: str) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files",
        lambda: [f for f in real if f.name < number],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _apply_before_115(db_path, monkeypatch) -> None:
    _apply_before(db_path, monkeypatch, "115")


#: Migration 229 drops the edge stores, so the interaction-log tests below stop
#: one short of it: they pin what 115 did to a table that later went away.
def _apply_before_229(db_path, monkeypatch) -> None:
    _apply_before(db_path, monkeypatch, "229")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


# ── media_only ────────────────────────────────────────────────────────


def test_pre115_auto_delete_rules_missing_media_only(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert "media_only" not in _columns(conn, "auto_delete_rules")


def test_auto_delete_rules_has_media_only(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        assert "media_only" in _columns(conn, "auto_delete_rules")


# ── interaction dedup index ─────────────────────────────────────────────


def test_pre115_interactions_log_missing_dedup_index(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert "idx_interactions_log_dedup" not in _indexes(conn, "user_interactions_log")


def test_interactions_log_dedup_index_enforces_uniqueness(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_229(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert "idx_interactions_log_dedup" in _indexes(conn, "user_interactions_log")
        conn.execute(
            "INSERT OR IGNORE INTO user_interactions_log "
            "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?, ?, ?, ?, ?)",
            (GUILD, 1, 2, 100, 999),
        )
        # Same (guild, message_id, from, to) — the partial UNIQUE index must
        # make the second insert a no-op.
        dup = conn.execute(
            "INSERT OR IGNORE INTO user_interactions_log "
            "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?, ?, ?, ?, ?)",
            (GUILD, 1, 2, 100, 999),
        )
        assert dup.rowcount == 0


def test_migration_115_dedups_log_and_recomputes_aggregate(tmp_path, monkeypatch):
    """A prod DB that ran backfill twice (no dedup index) accumulated duplicate
    log rows and an inflated aggregate. 115 must purge the dups and rebuild
    user_interactions from the deduped log."""
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        # Two backfill passes recorded the same message twice (no index to stop it).
        for _ in range(2):
            conn.execute(
                "INSERT INTO user_interactions_log "
                "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?, ?, ?, ?, ?)",
                (GUILD, 1, 2, 100, 999),
            )
        conn.execute(
            "INSERT INTO user_interactions (guild_id, from_user_id, to_user_id, weight) "
            "VALUES (?, ?, ?, ?)",
            (GUILD, 1, 2, 2),  # inflated: counted twice
        )
        conn.commit()

    # Apply the remaining migrations up to 229 (including 115).
    _apply_before_229(db, monkeypatch)

    with sqlite3.connect(db) as conn:
        log_rows = conn.execute(
            "SELECT COUNT(*) FROM user_interactions_log WHERE message_id = 999"
        ).fetchone()[0]
        assert log_rows == 1  # duplicate purged
        weight = conn.execute(
            "SELECT weight FROM user_interactions WHERE guild_id=? AND from_user_id=1 AND to_user_id=2",
            (GUILD,),
        ).fetchone()[0]
        assert weight == 1  # recomputed from the deduped log


# ── migration 229: the social graph's edge stores are gone ─────────────


_SOCIAL_GRAPH_TABLES = (
    "user_interactions",
    "user_interactions_log",
    "voice_follow",
    "voice_follow_log",
    "reaction_log",
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_pre229_social_graph_tables_exist(tmp_path, monkeypatch):
    """So the drop below is doing real work on a database that has them."""
    db = tmp_path / "t.db"
    _apply_before_229(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert set(_SOCIAL_GRAPH_TABLES) <= _tables(conn)


def test_migration_229_drops_the_social_graph_tables(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        assert not set(_SOCIAL_GRAPH_TABLES) & _tables(conn)
        # Per-message emoji counts carry no member id and stay.
        assert "message_reactions" in _tables(conn)


# ── xp_events index ─────────────────────────────────────────────────────


def test_pre115_xp_events_has_no_index(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _apply_before_115(db, monkeypatch)
    with sqlite3.connect(db) as conn:
        assert _indexes(conn, "xp_events") == set()


def test_xp_events_is_indexed(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        idx = _indexes(conn, "xp_events")
        assert "idx_xp_events_lookup" in idx
        assert idx  # get_xp_leaderboard no longer full-scans


def test_member_xp_and_role_events_indexed(tmp_path):
    db = tmp_path / "t.db"
    _apply_all(db)
    with sqlite3.connect(db) as conn:
        assert "idx_member_xp_leaderboard" in _indexes(conn, "member_xp")
        assert "idx_role_events_lookup" in _indexes(conn, "role_events")
        assert "idx_member_activity_guild_ts" in _indexes(conn, "member_activity")


def test_migrations_apply_cleanly_twice(tmp_path):
    """Re-applying migrations (schema_version already populated) is a no-op and
    must not error — proves 115's ALTER/DELETE/INSERT are safely idempotent."""
    db = tmp_path / "t.db"
    _apply_all(db)
    _apply_all(db)  # would raise on duplicate column / index if not idempotent


# Numbers that two files already shared before anyone was watching. Every one
# of them is recorded in production's ``schema_version`` under its full
# filename, so they cannot be renumbered now — a renamed file reads as never
# applied and runs a second time. They are grandfathered, not endorsed.
KNOWN_DUPLICATE_NUMBERS = frozenset(
    {
        "012", "019", "077", "080", "085", "089", "095", "099", "100",
        "114", "115", "127", "128", "134", "136", "141", "200", "201",
    }
)


def test_no_new_migration_number_is_used_twice():
    """Two files sharing a number is a merge artefact, not a numbering style.

    The chain keys ``schema_version`` on the whole filename and orders by it, so
    duplicates do apply — which is why eighteen of them accumulated unnoticed.
    What a duplicate costs is the ordering guarantee: the two run in alphabetical
    order of their *names*, so whichever must go second is only there by luck, and
    the day their statements touch the same table the loser silently clobbers the
    other. Parallel worktrees pick the same next number constantly
    (design_guide.md, Part 1), so the collision is the normal case and this is the
    thing that notices it. The historical set is frozen above rather than fixed:
    renaming an applied migration makes it run again.
    """
    from collections import defaultdict

    by_number = defaultdict(list)
    for path in migrations._migration_files():
        by_number[path.name[:3]].append(path.name)
    clashes = {
        n: sorted(v)
        for n, v in by_number.items()
        if len(v) > 1 and n not in KNOWN_DUPLICATE_NUMBERS
    }
    assert not clashes, (
        f"migration numbers used more than once: {clashes} — take the next free "
        "number instead; re-check it against main before merging"
    )


def test_the_grandfathered_duplicates_are_still_all_duplicates():
    """The frozen set shrinks only by deletion, never by a renumber.

    If a number in it stops being shared, someone renamed a migration that
    production has already applied under its old filename — which makes it run a
    second time at the next boot.
    """
    from collections import Counter

    counts = Counter(p.name[:3] for p in migrations._migration_files())
    stale = sorted(n for n in KNOWN_DUPLICATE_NUMBERS if counts[n] == 1)
    assert not stale, (
        f"no longer duplicated: {stale} — if a file was renumbered, production "
        "will re-apply it under the new name"
    )
