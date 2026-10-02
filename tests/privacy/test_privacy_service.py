"""Tests for privacy/service.py."""

from __future__ import annotations

import json
import logging

import pytest

from bot_modules.core.db_utils import open_db
from tests.db_template import migrated_db
from bot_modules.privacy.service import (
    SUBJECT_ID_COLUMNS,
    _MESSAGE_CHILD_TABLES,
    export_user_data,
    purge_user_data,
)
from bot_modules.analytics.usage_telemetry_service import (
    KIND_COMMAND,
    KIND_PANEL,
    record_event,
)
from tests.paths import REPO_ROOT

GUILD = 123
USER = 1001
OTHER_USER = 1002


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


def _insert_message(conn, guild_id, user_id, message_id=None):
    mid = message_id or (user_id * 1000 + guild_id)
    conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(message_id, guild_id, channel_id, author_id, ts) VALUES (?, ?, ?, ?, ?)",
        (mid, guild_id, 500, user_id, 1000.0),
    )
    return mid


def _insert_xp(conn, guild_id, user_id):
    conn.execute(
        "INSERT OR IGNORE INTO member_xp (guild_id, user_id, total_xp) VALUES (?, ?, ?)",
        (guild_id, user_id, 100),
    )


def _insert_known_user(conn, guild_id, user_id):
    conn.execute(
        "INSERT OR IGNORE INTO known_users (guild_id, user_id, username, display_name, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, f"user_{user_id}", f"User {user_id}", 1000.0),
    )


# ── message count return value ─────────────────────────────────────────


def test_returns_message_count(db):
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, USER, 2)
        count = purge_user_data(conn, GUILD, USER)
    assert count == 2


def test_returns_zero_for_user_with_no_messages(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        count = purge_user_data(conn, GUILD, USER)
    assert count == 0


# ── total rows erased (the Member Data panel's toast) ──────────────────


def test_counted_purge_reports_every_row_not_just_messages(db):
    from bot_modules.privacy.service import purge_user_data_counted

    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, USER, 2)
        _insert_xp(conn, GUILD, USER)
        result = purge_user_data_counted(conn, GUILD, USER)
    assert result.message_rows == 2
    # the two messages and the xp row, at the least
    assert result.total_rows >= result.message_rows + 1


def test_counted_purge_of_a_member_with_nothing_reports_zero(db):
    from bot_modules.privacy.service import purge_user_data_counted

    with open_db(db) as conn:
        result = purge_user_data_counted(conn, GUILD, USER)
    assert (result.message_rows, result.total_rows) == (0, 0)


def test_counted_purge_with_keep_messages_counts_the_rest(db):
    from bot_modules.privacy.service import purge_user_data_counted

    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_xp(conn, GUILD, USER)
        kept = purge_user_data_counted(conn, GUILD, USER, keep_messages=True)
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE author_id = ?", (USER,)
        ).fetchone()[0]
    assert left == 1, "keep_messages leaves the message rows alone"
    assert kept.total_rows >= 1  # the xp row went, the message did not
    assert kept.message_rows == 0, "kept messages are not reported as erased"


def test_counted_purge_does_not_count_another_members_rows(db):
    from bot_modules.privacy.service import purge_user_data_counted

    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        _insert_xp(conn, GUILD, USER + 1)
        mine = purge_user_data_counted(conn, GUILD, USER)
        theirs = purge_user_data_counted(conn, GUILD, USER + 1)
    assert mine.total_rows == theirs.total_rows


# ── messages deleted ──────────────────────────────────────────────────


def test_deletes_messages(db):
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert remaining == 0


def test_does_not_delete_other_users_messages(db):
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, OTHER_USER, 2)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (GUILD, OTHER_USER),
        ).fetchone()[0]
    assert remaining == 1


def test_does_not_delete_other_guilds_messages(db):
    other_guild = 999
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, other_guild, USER, 2)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (other_guild, USER),
        ).fetchone()[0]
    assert remaining == 1


# ── core tables deleted ───────────────────────────────────────────────


def test_deletes_member_xp(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert row == 0


def test_deletes_known_users(db):
    with open_db(db) as conn:
        _insert_known_user(conn, GUILD, USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM known_users WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert row == 0


def test_deletes_usage_events(db):
    """Usage telemetry is retained indefinitely, so this hard-erasure path is
    the only thing that ever clears it — if it drops out of the purge list the
    data becomes genuinely unreachable."""
    with open_db(db) as conn:
        record_event(conn, GUILD, KIND_COMMAND, "bank", USER)
        record_event(conn, GUILD, KIND_PANEL, "home", USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert row == 0


def test_does_not_delete_other_users_usage_events(db):
    with open_db(db) as conn:
        record_event(conn, GUILD, KIND_COMMAND, "bank", USER)
        record_event(conn, GUILD, KIND_COMMAND, "bank", OTHER_USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE guild_id = ? AND user_id = ?",
            (GUILD, OTHER_USER),
        ).fetchone()[0]
    assert row == 1


def test_idempotent_on_empty_db(db):
    with open_db(db) as conn:
        count = purge_user_data(conn, GUILD, USER)
    assert count == 0


# ── keep_messages=True ────────────────────────────────────────────────


def test_keep_messages_preserves_message_rows(db):
    """The Member Data panel's keep-messages option leaves the archive in place."""
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_message(conn, GUILD, USER, 2)
        count = purge_user_data(conn, GUILD, USER, keep_messages=True)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND author_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    # Returns the count for the summary even though nothing was deleted.
    assert count == 2
    assert remaining == 2


def test_keep_messages_still_clears_xp(db):
    """Other PII (XP, activity, profile) is purged even when messages are kept."""
    with open_db(db) as conn:
        _insert_message(conn, GUILD, USER, 1)
        _insert_xp(conn, GUILD, USER)
        _insert_known_user(conn, GUILD, USER)
        purge_user_data(conn, GUILD, USER, keep_messages=True)
        xp_remaining = conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
        known_remaining = conn.execute(
            "SELECT COUNT(*) FROM known_users WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert xp_remaining == 0
    assert known_remaining == 0


# ── heavy users must not break the erasure (SQLite variable cap) ───────


def test_purges_user_with_more_messages_than_sqlite_variable_cap(db):
    """A heavy poster has more message rows than SQLite's 32,766-variable cap.

    The old implementation inlined every message id into one ``IN (?,…)``,
    so a genuine legal-erasure run raised ``too many SQL variables`` for
    exactly the accounts most likely to file one. 2026-08 review, privacy A1.
    """
    n = 33_000
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts) "
            "VALUES (?, ?, 500, ?, 1000.0)",
            ((i + 1, GUILD, USER) for i in range(n)),
        )
        # Child rows on both ends of the id range so chunking is exercised.
        conn.executemany(
            "INSERT INTO message_mentions (message_id, user_id) VALUES (?, 7)",
            [(1,), (n,)],
        )
        count = purge_user_data(conn, GUILD, USER)
        msgs = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE author_id = ?", (USER,)
        ).fetchone()[0]
        children = conn.execute(
            "SELECT COUNT(*) FROM message_mentions"
        ).fetchone()[0]
    assert count == n
    assert msgs == 0
    assert children == 0


# ── tables added by the 2026-08 review (previously missed) ─────────────


@pytest.mark.parametrize(
    ("table", "insert_sql", "params", "where"),
    [
        pytest.param(
            "xp_reaction_awards",
            "INSERT INTO xp_reaction_awards (guild_id, message_id, user_id) VALUES (?, ?, ?)",
            (GUILD, 1, USER),
            "guild_id = ? AND user_id = ?",
            id="xp_reaction_awards",
        ),
        pytest.param(
            "member_birthdays",
            "INSERT INTO member_birthdays (guild_id, user_id, birth_month, birth_day, set_by, set_at) "
            "VALUES (?, ?, 6, 15, ?, 0)",
            (GUILD, USER, USER),
            "guild_id = ? AND user_id = ?",
            id="member_birthdays",
        ),
        pytest.param(
            "voice_master_profiles",
            "INSERT INTO voice_master_profiles (guild_id, user_id, saved_name, updated_at) VALUES (?, ?, 'room', 0)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="voice_master_profiles",
        ),
        pytest.param(
            "bios",
            "INSERT INTO bios (user_id, guild_id, message_id, channel_id) VALUES (?, ?, 1, 2)",
            (USER, GUILD),
            "guild_id = ? AND user_id = ?",
            id="bios",
        ),
        pytest.param(
            "bio_answers",
            "INSERT INTO bio_answers (user_id, guild_id, slot, question_id, question_text, answer) "
            "VALUES (?, ?, 0, 1, 'q', 'a')",
            (USER, GUILD),
            "guild_id = ? AND user_id = ?",
            id="bio_answers",
        ),
        pytest.param(
            "bio_field_values",
            "INSERT INTO bio_field_values (user_id, guild_id, field_id, field_label, value) "
            "VALUES (?, ?, 1, 'l', 'v')",
            (USER, GUILD),
            "guild_id = ? AND user_id = ?",
            id="bio_field_values",
        ),
        pytest.param(
            "pen_pals_pool_events",
            "INSERT INTO pen_pals_pool_events (guild_id, user_id, at, action, reason) "
            "VALUES (?, ?, 0, 'join', 'panel')",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="pen_pals_pool_events",
        ),
        pytest.param(
            "pen_pals_optouts",
            "INSERT INTO pen_pals_optouts (guild_id, user_id, at) VALUES (?, ?, 0)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="pen_pals_optouts",
        ),
        # A confession held by mod-approve mode. Deleted outright rather than
        # left to the fourteen-day sweep its sibling confession_threads relies on:
        # the row is queued *to be published*, and erasing a member then posting
        # what they wrote — because a mod reached the queue first — is not a
        # defensible outcome.
        pytest.param(
            "confession_pending",
            "INSERT INTO confession_pending (guild_id, author_id, content, created_at) "
            "VALUES (?, ?, 'a secret', 0)",
            (GUILD, USER),
            "guild_id = ? AND author_id = ?",
            id="confession_pending",
        ),
        # The member's anonymous alias in a confession thread or an FFA prompt
        # (anon-tail-69): the one row that ties a still-visible pseudonym back
        # to them. Shares the threads' fourteen-day sweep and goes at erasure too.
        pytest.param(
            "confession_emoji_assignments",
            "INSERT INTO confession_emoji_assignments "
            "(guild_id, root_message_id, user_id, emoji_index, name_index, created_at) "
            "VALUES (?, 4242, ?, 1, 1, 0)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="confession_emoji_assignments",
        ),
        # An Auto-Thread "Replies to Your Threads" opt-in (migration 244).
        pytest.param(
            "needle_reply_optins",
            "INSERT INTO needle_reply_optins (guild_id, user_id) VALUES (?, ?)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="needle_reply_optins",
        ),
        # A member's running Hot Potato style score (review H1, 2026-09-14).
        pytest.param(
            "hot_potato_style",
            "INSERT INTO hot_potato_style (guild_id, user_id, total_points) "
            "VALUES (?, ?, 3)",
            (GUILD, USER),
            "guild_id = ? AND user_id = ?",
            id="hot_potato_style",
        ),
    ],
)
def test_purges_review_added_simple_tables(db, table, insert_sql, params, where):
    with open_db(db) as conn:
        conn.execute(insert_sql, params)
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", (GUILD, USER)
        ).fetchone()[0]
    assert remaining == 0


@pytest.mark.parametrize(
    ("table", "col_a", "col_b"),
    [
        pytest.param("watched_users", "watched_user_id", "watcher_user_id", id="watched_users"),
        pytest.param("voice_master_trusted", "owner_id", "target_id", id="voice_master_trusted"),
        pytest.param("invite_edges", "inviter_id", "invitee_id", id="invite_edges"),
        pytest.param("dm_batch_pending", "user_id", "actor_id", id="dm_batch_pending"),
    ],
)
def test_purges_two_sided_tables_in_both_directions(db, table, col_a, col_b):
    """Pair tables are cleared whichever side the erased user is on."""
    extra = {
        "invite_edges": ", joined_at, invite_code) VALUES (?, ?, ?, 0, 'c')",
        "voice_master_trusted": ", added_at) VALUES (?, ?, ?, 0)",
        "watched_users": ") VALUES (?, ?, ?)",
        "dm_batch_pending": ", kind, created_at) VALUES (?, ?, ?, 'k', 0)",
    }[table]
    with open_db(db) as conn:
        conn.execute(
            f"INSERT INTO {table} (guild_id, {col_a}, {col_b}{extra}",
            (GUILD, USER, OTHER_USER),
        )
        conn.execute(
            f"INSERT INTO {table} (guild_id, {col_a}, {col_b}{extra}",
            (GUILD, OTHER_USER + 1, USER),
        )
        purge_user_data(conn, GUILD, USER)
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col_a} = ? OR {col_b} = ?",
            (USER, USER),
        ).fetchone()[0]
    assert remaining == 0


def test_purge_covers_economy_per_member_state(db):
    """purge_user_data delegates econ/casino per-member rows to econ_purge_user
    — the ledger itself is deliberately preserved (audit integrity)."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, updated_at) "
            "VALUES (?, ?, 100, 0, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at) "
            "VALUES (?, ?, 100, 'test', 0)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        wallets = conn.execute(
            "SELECT COUNT(*) FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT COUNT(*) FROM econ_ledger WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0]
    assert wallets == 0
    assert ledger == 1  # preserved on purpose


def test_purge_covers_music_playlist_member_rows(db):
    """purge_user_data delegates the music playlist's `added_by` rows to
    music_playlist_store.purge_member_rows — the generic user_id sweep can't
    reach them (register: docs/data_register.md). Reviewer references are
    nulled, other members' rows survive."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO music_playlist_tracks (guild_id, playlist_id, track_id, "
            "channel_id, message_id, added_by, added_at) VALUES (?, 'pl', 't1', 5, 6, ?, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO music_playlist_tracks (guild_id, playlist_id, track_id, "
            "channel_id, message_id, added_by, added_at) VALUES (?, 'pl', 't2', 5, 7, ?, 0)",
            (GUILD, OTHER_USER),
        )
        conn.execute(
            "INSERT INTO music_playlist_unmatched (guild_id, channel_id, message_id, "
            "source_url, added_by, created_at) VALUES (?, 5, 8, 'https://x/1', ?, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO music_playlist_unmatched (guild_id, channel_id, message_id, "
            "source_url, added_by, status, reviewed_by, reviewed_at, created_at) "
            "VALUES (?, 5, 9, 'https://x/2', ?, 'approved', ?, 0, 0)",
            (GUILD, OTHER_USER, USER),
        )
        purge_user_data(conn, GUILD, USER)
        mine = conn.execute(
            "SELECT (SELECT COUNT(*) FROM music_playlist_tracks WHERE added_by = ?) + "
            "(SELECT COUNT(*) FROM music_playlist_unmatched WHERE added_by = ?)",
            (USER, USER),
        ).fetchone()[0]
        others = conn.execute(
            "SELECT (SELECT COUNT(*) FROM music_playlist_tracks WHERE added_by = ?) + "
            "(SELECT COUNT(*) FROM music_playlist_unmatched WHERE added_by = ?)",
            (OTHER_USER, OTHER_USER),
        ).fetchone()[0]
        reviewer = conn.execute(
            "SELECT reviewed_by FROM music_playlist_unmatched WHERE added_by = ?",
            (OTHER_USER,),
        ).fetchone()[0]
    assert mine == 0
    assert others == 2  # other members' rows survive
    assert reviewer is None  # the erased member's reviewer reference is nulled


# ── subject access export (GDPR Art 15 / Art 20) ───────────────────────


def test_export_finds_rows_by_conventional_subject_column(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        _insert_known_user(conn, GUILD, USER)
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["member_xp"] == 1
    assert data["counts"]["known_users"] == 1
    assert data["tables"]["member_xp"]["rows"][0]["total_xp"] == 100


def test_export_excludes_other_users_and_other_guilds(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, USER)
        _insert_xp(conn, GUILD, OTHER_USER)
        _insert_xp(conn, GUILD + 1, USER)
        data = export_user_data(conn, GUILD, USER)
    rows = data["tables"]["member_xp"]["rows"]
    assert len(rows) == 1
    assert rows[0]["user_id"] == USER
    assert rows[0]["guild_id"] == GUILD


def test_export_is_empty_for_unknown_user(db):
    with open_db(db) as conn:
        _insert_xp(conn, GUILD, OTHER_USER)
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"] == {}
    assert data["tables"] == {}


def test_export_says_on_its_face_which_table_it_could_not_search(db):
    """A table the schema walk refuses is a hole in an Art 15 answer, so the
    export names it in `notes` rather than only in the log the operator does
    not read before replying to the subject."""
    with open_db(db) as conn:
        conn.execute('CREATE TABLE "odd""name" (guild_id INTEGER, user_id INTEGER)')
        conn.execute('INSERT INTO "odd""name" VALUES (?, ?)', (GUILD, USER))
        _insert_xp(conn, GUILD, USER)
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["member_xp"] == 1  # the rest of the answer survives
    assert 'odd"name' not in data["tables"]
    assert any(
        n.startswith('odd"name: not searched') for n in data["notes"]
    ), data["notes"]


def test_export_includes_ledger_the_purge_deliberately_preserves(db):
    """Art 17(3) retention is not an Art 15 exemption: rows the server keeps
    are still the subject's data and must still be disclosable."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at) "
            "VALUES (?, ?, 100, 'test', 0)",
            (GUILD, USER),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["econ_ledger"] == 1


def test_export_reaches_message_children_through_the_author(db):
    """message_sentiment has no subject column — it is only the subject's data
    by virtue of hanging off their message, so column discovery cannot find it
    and the message-id join must."""
    with open_db(db) as conn:
        mid = _insert_message(conn, GUILD, USER)
        conn.execute(
            "INSERT INTO message_sentiment "
            "(message_id, guild_id, channel_id, sentiment, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, GUILD, 500, 0.5, 1000.0),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["messages"] == 1
    assert data["counts"]["message_sentiment"] == 1


def test_export_message_children_chunk_past_the_variable_cap(db):
    """Same failure mode the purge had (A1): >32,766 ids inlined into one
    IN (...) raises. The heaviest posters are the likeliest requesters."""
    with open_db(db) as conn:
        for i in range(33_000):
            _insert_message(conn, GUILD, USER, message_id=9_000_000 + i)
        conn.commit()
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["messages"] == 33_000


def test_export_flags_tables_naming_a_second_member(db):
    """Art 15(4): the operator has to decide about the counterparty before
    disclosure, so the export must surface those tables rather than bury them."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO invite_edges (guild_id, inviter_id, invitee_id, joined_at) "
            "VALUES (?, ?, ?, 0)",
            (GUILD, USER, OTHER_USER),
        )
        data = export_user_data(conn, GUILD, USER)
    assert "invite_edges" in data["review_required"]


def test_export_finds_the_subject_on_either_side_of_a_pair_table(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO invite_edges (guild_id, inviter_id, invitee_id, joined_at) "
            "VALUES (?, ?, ?, 0)",
            (GUILD, OTHER_USER, USER),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["invite_edges"] == 1


def test_export_finds_a_member_on_a_grant_keeper_allow_list(db):
    """`grant_role_permissions` names the members allowed to hand out a grant
    in an `entity_id` column (entity_type 'user'). It was invisible to the
    export until `entity_id` was a recognised subject column — a keeper list is
    personal data about the keeper. (The legacy `give_role_permissions` table
    had the same shape and the same blind spot; migration 193 dropped it.)"""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO grant_role_permissions "
            "(guild_id, grant_name, entity_type, entity_id) VALUES (?, 'nsfw', 'user', ?)",
            (GUILD, USER),
        )
        # A role entry on the same list is not a member and must not be
        # mistaken for one.
        conn.execute(
            "INSERT INTO grant_role_permissions "
            "(guild_id, grant_name, entity_type, entity_id) VALUES (?, 'nsfw', 'role', ?)",
            (GUILD, 4242),
        )
        data = export_user_data(conn, GUILD, USER)
    assert data["counts"]["grant_role_permissions"] == 1


def test_export_covers_every_table_the_purge_deletes(db):
    """The load-bearing property: an access export that misses a table the
    erasure path knows about is an incomplete answer to a statutory request.
    Seeds a row in each purge-covered table, then asserts the export sees it.
    New tables joining the purge fail here until the export can reach them."""
    with open_db(db) as conn:
        purge_tables = _tables_touched_by_purge(conn)
        missing = []
        for table in sorted(purge_tables):
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if not (cols & SUBJECT_ID_COLUMNS) and table not in _MESSAGE_CHILD_TABLES:
                missing.append(table)
    assert not missing, (
        "purge-covered tables the export's column discovery cannot reach: "
        f"{missing} — add the column to SUBJECT_ID_COLUMNS or reach the table "
        "explicitly, as the message children are"
    )


def _tables_touched_by_purge(conn):
    """Every table name purge_user_data (and econ_purge_user) writes to, read
    off the source rather than restated — a second copy is how the two drift."""
    import re
    from pathlib import Path

    import bot_modules.privacy.service as ps
    import bot_modules.economy.service as es

    names = set()
    for mod in (ps, es):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        names |= set(re.findall(r"DELETE FROM (\w+)", src))
        names |= set(re.findall(r'DELETE FROM \{?"?(\w+)"?\}?', src))
    real = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    names |= set(ps._MESSAGE_CHILD_TABLES)
    names |= set(getattr(es, "_PURGE_USER_ID_TABLES", ()))
    return {n for n in names if n in real}


def test_purge_strips_from_user_chips_from_mention_award_rules(db):
    """A from_user chip names a member inside the conditions JSON — the
    "list-column blind spot": invisible to SUBJECT_ID_COLUMNS. Erasure must
    strip the chip; a rule left with no chips is deleted outright (an empty
    chip list is the fail-closed "matches nothing" state, and keeping a husk
    that exists only because of the erased member serves nobody).
    """
    from bot_modules.mention_awards.logic import Condition
    from bot_modules.mention_awards.store import create_rule, list_rules

    with open_db(db) as conn:
        # Rule A: from_user chip on the erased member + a text chip — chip
        # stripped, rule survives.
        a = create_rule(
            conn, GUILD, channel_id=42, amount=10,
            conditions=[
                Condition("from_user", str(USER)),
                Condition("contains_text", "your turn"),
            ],
        )
        # Rule B: ONLY a from_user chip on the erased member — rule deleted.
        create_rule(
            conn, GUILD, channel_id=43, amount=10,
            conditions=[Condition("from_user", str(USER))],
        )
        # Rule C: someone else's from_user chip — untouched.
        c = create_rule(
            conn, GUILD, channel_id=44, amount=10,
            conditions=[Condition("from_user", str(OTHER_USER))],
        )

        purge_user_data(conn, GUILD, USER)

        rows = {int(r["id"]): r for r in list_rules(conn, GUILD)}
        assert set(rows) == {a, c}
        assert str(USER) not in rows[a]["conditions"]
        assert "your turn" in rows[a]["conditions"]
        assert str(OTHER_USER) in rows[c]["conditions"]


def test_purge_anonymises_todos_rather_than_deleting_them(db):
    """A todos row is the team's work plus two ids naming a person.

    Deleting it to erase the ids would take real outstanding work off other
    people's list — a task someone else is part-way through vanishing because
    an unrelated member left. Clearing the ids erases everything identifying
    while the work stands (register: docs/data_register.md).
    """
    from bot_modules.todo.service import complete_todo, create_todo

    with open_db(db) as conn:
        mine = create_todo(conn, GUILD, USER, "Post the QOTD")
        theirs = create_todo(conn, GUILD, OTHER_USER, "Rotate the tunnel token")
        ticked = create_todo(conn, GUILD, OTHER_USER, "Check the mod queue")
        complete_todo(conn, ticked, GUILD, USER)

        purge_user_data(conn, GUILD, USER)

        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, task, added_by, completed_by FROM todos"
            ).fetchall()
        }

    # Nothing was removed — the work survives the erasure.
    assert set(rows) == {mine, theirs, ticked}
    assert rows[mine]["task"] == "Post the QOTD"
    # …but nothing still names the erased member.
    assert rows[mine]["added_by"] == 0
    assert rows[ticked]["completed_by"] is None
    # Another member's rows are untouched.
    assert rows[theirs]["added_by"] == OTHER_USER
    assert rows[ticked]["added_by"] == OTHER_USER


def test_purge_drops_a_bugs_held_screenshot_links_and_lets_the_card_be_stripped(db):
    """The member's uploads are theirs, not the team's work: the held links go,
    and the blanked filer is what puts a posted card on the strip list."""
    from bot_modules.todo.service import (
        LIST_BUG,
        bug_cards_to_strip,
        create_todo,
        settle_bug_screenshots,
    )

    shot = [{"url": "https://cdn.example/a.png", "ext": "png"}]
    with open_db(db) as conn:
        held = create_todo(
            conn, GUILD, USER, "shop broke", screenshots=shot, list_key=LIST_BUG
        )
        posted = create_todo(conn, GUILD, USER, "bank broke", list_key=LIST_BUG)
        settle_bug_screenshots(
            conn, GUILD, upto_ts=0, posted_bug_ids=[posted],
            channel_id=5, message_id=6,
        )
        theirs = create_todo(
            conn, GUILD, OTHER_USER, "games broke", screenshots=shot,
            list_key=LIST_BUG,
        )
        assert bug_cards_to_strip(conn) == []

        purge_user_data(conn, GUILD, USER)

        urls = dict(conn.execute("SELECT id, attachment_urls FROM todos").fetchall())
        strip = [r["id"] for r in bug_cards_to_strip(conn)]
    assert urls[held] is None
    assert urls[theirs] is not None
    assert strip == [posted]


def test_purge_of_todos_is_scoped_to_the_guild(db):
    from bot_modules.todo.service import create_todo

    with open_db(db) as conn:
        here = create_todo(conn, GUILD, USER, "Post the QOTD")
        elsewhere = create_todo(conn, 999, USER, "Their chore")
        purge_user_data(conn, GUILD, USER)
        rows = {
            r["id"]: r["added_by"]
            for r in conn.execute("SELECT id, added_by FROM todos").fetchall()
        }
    assert rows[here] == 0
    assert rows[elsewhere] == USER


def test_export_sees_a_task_the_member_completed(db):
    """`completed_by` had to join SUBJECT_ID_COLUMNS or an access request would
    show the tasks a member *added* and silently omit the ones they did."""
    from bot_modules.todo.service import complete_todo, create_todo

    assert "completed_by" in SUBJECT_ID_COLUMNS
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, OTHER_USER, "Check the mod queue")
        complete_todo(conn, todo_id, GUILD, USER)
        result = export_user_data(conn, GUILD, USER)

    tasks = result["tables"]["todos"]["rows"]
    assert [r["id"] for r in tasks] == [todo_id]


def test_purge_blanks_the_recurring_definition_so_it_stays_erased(db):
    """Regression: the anonymisation used to undo itself.

    `_spawn_one` stamps a definition's `created_by` onto every todos row it
    materialises. Scrubbing only `todos` left the id in `todo_recurring`, so
    the next scheduled fire wrote it straight back into `todos.added_by` — and
    every day after. The register's "nothing identifying survives" claim
    depends on this.
    """
    from bot_modules.todo.recurring_service import create_recurring, spawn_due

    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Post QOTD", recurrence="daily",
            time_of_day=0, created_by=USER, now_ts=0.0,
        )
        purge_user_data(conn, GUILD, USER)

        assert conn.execute(
            "SELECT created_by FROM todo_recurring"
        ).fetchone()["created_by"] == 0

        # The next fire must not resurrect the id.
        spawn_due(conn, now_ts=86_400.0, offset_hours_for=lambda _g: 0.0)
        added = [r["added_by"] for r in conn.execute("SELECT added_by FROM todos")]

    assert added and all(a == 0 for a in added)


def test_purge_of_definitions_is_scoped_to_the_guild(db):
    from bot_modules.todo.recurring_service import create_recurring

    with open_db(db) as conn:
        create_recurring(
            conn, GUILD, task="Mine", recurrence="daily",
            time_of_day=0, created_by=USER, now_ts=0.0,
        )
        create_recurring(
            conn, 999, task="Theirs", recurrence="daily",
            time_of_day=0, created_by=USER, now_ts=0.0,
        )
        purge_user_data(conn, GUILD, USER)
        rows = {
            r["task"]: r["created_by"]
            for r in conn.execute("SELECT task, created_by FROM todo_recurring")
        }
    assert rows["Mine"] == 0
    assert rows["Theirs"] == USER


# ── Games records (review H1, 2026-09-14) ────────────────────────────

_HISTORY_INSERT = (
    "INSERT INTO games_game_history "
    "(game_id, game_type, channel_id, host_id, payload, started_at, guild_id) "
    "VALUES (?, ?, 500, ?, ?, '2026-09-01 12:00:00', ?)"
)


@pytest.mark.parametrize(
    ("table", "insert_sql", "params"),
    [
        pytest.param(
            "hot_potato_games",
            "INSERT INTO hot_potato_games (guild_id, channel_id, challenger_id, "
            "target_id) VALUES (?, 500, ?, ?)",
            (GUILD, OTHER_USER, USER),
            id="hot_potato-seat",
        ),
        pytest.param(
            "hot_potato_games",
            "INSERT INTO hot_potato_games (guild_id, channel_id, challenger_id, "
            "target_id, pass_log) VALUES (?, 500, ?, ?, ?)",
            (GUILD, OTHER_USER, OTHER_USER + 1,
             json.dumps([{"holder_id": USER, "received_at": 1.0}])),
            id="hot_potato-pass-log",
        ),
        pytest.param(
            "pressure_games",
            "INSERT INTO pressure_games (guild_id, channel_id, challenger_id, "
            "target_id, state) VALUES (?, 500, ?, ?, 'RESOLVED')",
            (GUILD, USER, OTHER_USER),
            id="pressure-seat",
        ),
        pytest.param(
            "pressure_games",
            "INSERT INTO pressure_games (guild_id, channel_id, challenger_id, "
            "target_id, state, pumps_json) VALUES (?, 500, ?, ?, 'ACTIVE', ?)",
            (GUILD, OTHER_USER, OTHER_USER + 1,
             json.dumps([{"player_id": USER, "roll": 14}])),
            id="pressure-pumps",
        ),
        pytest.param(
            "quickdraw_games",
            "INSERT INTO quickdraw_games (guild_id, channel_id, challenger_id, "
            "target_id, winner_id) VALUES (?, 500, ?, ?, ?)",
            (GUILD, OTHER_USER, OTHER_USER + 1, USER),
            id="quickdraw-seat",
        ),
        pytest.param(
            "mc_games",
            "INSERT INTO mc_games (guild_id, channel_id, host_id, roster) "
            "VALUES (?, 500, ?, ?)",
            (GUILD, OTHER_USER, json.dumps([OTHER_USER, USER])),
            id="musical_chairs-roster",
        ),
        pytest.param(
            "chicken_games",
            "INSERT INTO chicken_games (guild_id, channel_id, host_id, "
            "elimination_order) VALUES (?, 500, ?, ?)",
            (GUILD, OTHER_USER, json.dumps([USER])),
            id="chicken-elimination-order",
        ),
        pytest.param(
            "games_active_games",
            "INSERT INTO games_active_games (game_id, channel_id, game_type, "
            "host_id, payload, guild_id) VALUES ('g1', 500, 'clapback', ?, ?, ?)",
            (OTHER_USER, json.dumps({"scores": {str(USER): 200}}), GUILD),
            id="active-payload-key",
        ),
        pytest.param(
            "games_game_history",
            _HISTORY_INSERT,
            ("g1", "ama", OTHER_USER,
             json.dumps({"questions": [{"asker_id": USER, "text": "mine"}]}),
             GUILD),
            id="history-ama-asker",
        ),
        # Archived before games_game_history carried a guild (migration 204).
        pytest.param(
            "games_game_history",
            _HISTORY_INSERT,
            ("g1", "photo", USER, "{}", 0),
            id="history-guild-zero-host",
        ),
    ],
)
def test_purge_deletes_a_game_record_naming_the_member(db, table, insert_sql, params):
    """A game record goes whole from any seat, list entry or payload key."""
    with open_db(db) as conn:
        conn.execute(insert_sql, params)
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_purge_leaves_game_records_that_do_not_name_the_member(db):
    """Digit-bounded, guild-scoped: 10011 is not 1001, and guild B keeps its own."""
    near_miss = json.dumps({"players": [int(f"{USER}1"), int(f"9{USER}")]})
    with open_db(db) as conn:
        conn.execute(_HISTORY_INSERT, ("near", "clapback", OTHER_USER, near_miss, GUILD))
        conn.execute(_HISTORY_INSERT, ("elsewhere", "photo", USER, "{}", GUILD + 1))
        conn.execute(
            "INSERT INTO mc_games (guild_id, channel_id, host_id, roster) "
            "VALUES (?, 500, ?, ?)",
            (GUILD + 1, OTHER_USER, json.dumps([USER])),
        )
        purge_user_data(conn, GUILD, USER)
        kept = sorted(r[0] for r in conn.execute("SELECT game_id FROM games_game_history"))
        assert kept == ["elsewhere", "near"]
        assert conn.execute("SELECT COUNT(*) FROM mc_games").fetchone()[0] == 1


def test_dead_pressure_cooker_member_tables_are_dropped(db):
    """Superseded by the duel_* tables and read by nothing (migration 232)."""
    with open_db(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"pressure_nicks", "pressure_cooldowns"}


def test_orphaned_quality_score_leaves_table_is_dropped(db):
    """Migration 234: the leave-of-absence store outlived both its writer and
    the quality score that read it, and was dropped with Contributors."""
    with open_db(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "quality_score_leaves" not in names


def test_dead_april_fools_tables_are_dropped_where_they_exist():
    """Migration 233. The prank's tables were created by application code, so a
    migrated schema never has them and the check above would pass vacuously;
    apply the migration to a database that does, as prod did until 2026-09-14."""
    import sqlite3

    sql = (
        REPO_ROOT
        / "src" / "migrations" / "233_drop_dead_april_fools_tables.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE foolsday_names (guild_id INTEGER, user_id INTEGER, original TEXT)")
    conn.execute("CREATE TABLE foolsday_exclusions (guild_id INTEGER, user_id INTEGER)")
    conn.execute("INSERT INTO foolsday_exclusions VALUES (1, 1001)")
    conn.executescript(sql)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"foolsday_names", "foolsday_exclusions"}
    conn.executescript(sql)  # IF EXISTS: a database that never ran the prank


# ── Risky Rolls (registered 2026-08-20) ──────────────────────────────


def _insert_risky_round(conn, guild_id, *, game_id, opener_id, rolls, **seats):
    conn.execute(
        "INSERT INTO risky_active_rounds "
        "(game_id, channel_id, guild_id, opener_id, is_open, highest_user, "
        " lowest_user, second_lowest_user, second_highest_user) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            game_id, 500, guild_id, opener_id,
            seats.get("highest_user"), seats.get("lowest_user"),
            seats.get("second_lowest_user"), seats.get("second_highest_user"),
        ),
    )
    for uid, roll in rolls.items():
        conn.execute(
            "INSERT INTO risky_round_rolls (game_id, user_id, roll) VALUES (?, ?, ?)",
            (game_id, uid, roll),
        )


def test_purge_clears_risky_round_the_member_rolled_in(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=OTHER_USER,
            rolls={USER: 40, OTHER_USER: 90},
            highest_user=OTHER_USER, lowest_user=USER,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 0
        # The rolls go with the round: this connection makes no foreign_keys
        # promise, so the cascade must not be what removes them.
        assert conn.execute("SELECT COUNT(*) FROM risky_round_rolls").fetchone()[0] == 0


def test_purge_clears_risky_round_the_member_only_opened(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=USER,
            rolls={OTHER_USER: 90, 1003: 10},
            highest_user=OTHER_USER, lowest_user=1003,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 0


@pytest.mark.parametrize("seat", ["second_lowest_user", "second_highest_user"])
def test_purge_clears_risky_round_naming_the_member_in_a_second_seat(db, seat):
    # The 100 and 1 rules seat a member who is neither winner nor loser.
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=OTHER_USER,
            rolls={OTHER_USER: 100, 1003: 1},
            highest_user=OTHER_USER, lowest_user=1003, **{seat: USER},
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 0


def test_purge_leaves_a_risky_round_the_member_had_no_part_in(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, GUILD, game_id="g1", opener_id=OTHER_USER,
            rolls={OTHER_USER: 90, 1003: 10},
            highest_user=OTHER_USER, lowest_user=1003,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM risky_round_rolls").fetchone()[0] == 2


def test_purge_leaves_another_guilds_risky_round(db):
    with open_db(db) as conn:
        _insert_risky_round(
            conn, 999, game_id="g1", opener_id=USER, rolls={USER: 40},
            highest_user=USER,
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_active_rounds").fetchone()[0] == 1


def _insert_pending_question(conn, guild_id, *, winner_id, **csv_columns):
    """A pending question whose CSV columns default to naming nobody.

    ``participant_user_ids`` is NOT NULL, so it carries a bystander id unless
    the caller is the one putting the subject there.
    """
    columns = {"participant_user_ids": "1003"} | csv_columns
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO risky_pending_questions "
        f"(game_id, channel_id, guild_id, winner_id, prompt_kind, {names}) "
        f"VALUES ('g1', 500, ?, ?, 'direct', {marks})",
        (guild_id, winner_id, *columns.values()),
    )


@pytest.mark.parametrize(
    "column", ["participant_user_ids", "lowest_tie_user_ids", "questioners_asked"]
)
def test_purge_clears_pending_question_naming_the_member_in_a_csv_column(db, column):
    with open_db(db) as conn:
        _insert_pending_question(
            conn, GUILD, winner_id=OTHER_USER, **{column: str(USER)}
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_pending_questions").fetchone()[0] == 0


def test_purge_does_not_match_a_csv_id_by_substring(db):
    # USER is 1001; a round naming 10010 and 10011 must survive a bare-LIKE bug.
    with open_db(db) as conn:
        _insert_pending_question(
            conn, GUILD, winner_id=OTHER_USER,
            participant_user_ids=f"{USER}0,{USER}1",
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_pending_questions").fetchone()[0] == 1


def test_purge_clears_posted_question_the_member_asked(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO risky_posted_questions "
            "(message_id, channel_id, guild_id, asker_id, allowed_replier_ids, question_text) "
            "VALUES (7, 500, ?, ?, ?, 'their own words')",
            (GUILD, USER, str(OTHER_USER)),
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_posted_questions").fetchone()[0] == 0


def test_purge_clears_posted_question_the_member_was_only_asked(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO risky_posted_questions "
            "(message_id, channel_id, guild_id, asker_id, allowed_replier_ids, question_text) "
            "VALUES (7, 500, ?, ?, ?, 'someone elses words')",
            (GUILD, OTHER_USER, f"{USER},1003"),
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risky_posted_questions").fetchone()[0] == 0



# ── Erasure statements must match the schema they run against ────────────────
#
# `purge_user_data` and `econ_purge_user` tolerate schema drift: a failing
# statement logs a warning and the sweep continues, so one table missing on an
# older deployment cannot abort an erasure midway. The cost of that tolerance
# is that a statement naming a column the table does not have fails on *every*
# erasure, silently, forever — which is exactly what happened to eight tables
# between their introduction and the 2026-09-02 GDPR review (five of them
# holding 108,687 production rows an erasure reported as cleared and never
# touched).
#
# This test is the standing guard: run the whole erasure against a fully
# migrated schema and require it to be silent. It fails on the day someone adds
# a table to a purge list without the columns that list's statement uses.

def test_purge_runs_clean_against_the_migrated_schema(db, caplog):
    """No purge statement may reference a column its table does not have.

    There is no allow-list: the one tolerated warning, `foolsday_exclusions`
    (an application-created table), went with migration 233."""
    caplog.set_level(logging.WARNING)

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    unexpected = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("dungeonkeeper.")
        and ("purge" in record.getMessage().lower())
    ]
    assert not unexpected, (
        "purge_user_data hit schema errors it silently swallowed:\n  "
        + "\n  ".join(unexpected)
    )


@pytest.mark.parametrize(
    "table, columns, values",
    [
        pytest.param(
            "econ_quest_progress",
            "(quest_id, user_id, period, current, target)",
            "(?, ?, 'all', 1, 5)",
            id="quest-progress",
        ),
        pytest.param(
            "econ_quest_progress_marks",
            "(quest_id, user_id, period, occurrence)",
            "(?, ?, 'all', 'x')",
            id="quest-progress-marks",
        ),
        pytest.param(
            "econ_community_contrib",
            "(quest_id, user_id, count)",
            "(?, ?, 3)",
            id="community-contrib",
        ),
        pytest.param(
            "econ_community_tier_payouts",
            "(quest_id, tier, user_id)",
            "(?, 1, ?)",
            id="community-tier-payouts",
        ),
    ],
)
def test_purge_clears_quest_tables_that_key_on_the_quest(db, table, columns, values):
    """These carry no guild_id and reach their guild through `econ_quests`."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_quests (id, guild_id, title, qtype, created_at) "
            "VALUES (1, ?, 'q', 'daily', 0)",
            (GUILD,),
        )
        conn.execute(
            f"INSERT INTO {table} {columns} VALUES {values}", (1, USER)
        )
        conn.execute(
            f"INSERT INTO {table} {columns} VALUES {values}", (1, OTHER_USER)
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        remaining = conn.execute(
            f"SELECT user_id FROM {table}"
        ).fetchall()
    assert [r[0] for r in remaining] == [OTHER_USER]


def test_purge_of_quest_rows_does_not_cross_guilds(db):
    """A parent-scoped delete must not erase the member in another guild."""
    other_guild = GUILD + 1
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_quests (id, guild_id, title, qtype, created_at) "
            "VALUES (1, ?, 'a', 'daily', 0)",
            (GUILD,),
        )
        conn.execute(
            "INSERT INTO econ_quests (id, guild_id, title, qtype, created_at) "
            "VALUES (2, ?, 'b', 'daily', 0)",
            (other_guild,),
        )
        for quest_id in (1, 2):
            conn.execute(
                "INSERT INTO econ_quest_progress "
                "(quest_id, user_id, period, current, target) "
                "VALUES (?, ?, 'all', 1, 5)",
                (quest_id, USER),
            )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        remaining = conn.execute(
            "SELECT quest_id FROM econ_quest_progress"
        ).fetchall()
    assert [r[0] for r in remaining] == [2]


def test_purge_clears_qotd_rewards_through_their_qotd(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_qotd (id, guild_id, channel_id, message_id, "
            "question, posted_by, local_day, created_at) "
            "VALUES (1, ?, 5, 6, 'q', 9, '2026-01-01', 0)",
            (GUILD,),
        )
        conn.execute(
            "INSERT INTO econ_qotd_rewards (qotd_id, user_id) VALUES (1, ?)",
            (USER,),
        )
        conn.execute(
            "INSERT INTO econ_qotd_rewards (qotd_id, user_id) VALUES (1, ?)",
            (OTHER_USER,),
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        remaining = conn.execute(
            "SELECT user_id FROM econ_qotd_rewards"
        ).fetchall()
    assert [r[0] for r in remaining] == [OTHER_USER]


def test_purge_clears_wellness_counter_children_before_their_parent(db):
    """They key on `cap_id` only — once the cap is gone nothing can find them."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO wellness_caps (id, guild_id, user_id, label, scope, "
            "window, cap_limit, created_at) "
            "VALUES (1, ?, ?, 'l', 'guild', 'day', 3, 0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO wellness_cap_counters (cap_id, window_start_epoch, "
            "count) VALUES (1, 0, 2)"
        )
        conn.execute(
            "INSERT INTO wellness_cap_overages (cap_id, window_start_epoch, "
            "overage_count) VALUES (1, 0, 1)"
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        for table in ("wellness_cap_counters", "wellness_cap_overages"):
            assert (
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            ), table


# ── The five decisions settled 2026-09-02 ────────────────────────────────────


def test_purge_clears_quote_rows_from_either_side(db):
    """A quote row names two members and is incoherent with one removed."""
    with open_db(db) as conn:
        for quoter, quoted in ((USER, OTHER_USER), (OTHER_USER, USER),
                               (OTHER_USER, 1003)):
            conn.execute(
                "INSERT INTO quote_audit_log (ts, guild_id, channel_id, "
                "quoter_id, quoted_user_id, quoted_message_id, "
                "posted_message_id, theme, font) "
                "VALUES (0, ?, 1, ?, ?, 2, 3, 't', 'f')",
                (GUILD, quoter, quoted),
            )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        rows = conn.execute(
            "SELECT quoter_id, quoted_user_id FROM quote_audit_log"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(OTHER_USER, 1003)]


@pytest.mark.parametrize(
    "table, subject_sql, actor_column, blanked",
    [
        pytest.param(
            "inactive_members",
            "INSERT INTO inactive_members (guild_id, user_id, moderator_id, "
            "created_at) VALUES (?, ?, ?, 0)",
            "moderator_id",
            0,
            id="inactive-members",
        ),
        pytest.param(
            "promotion_review_cards",
            "INSERT INTO promotion_review_cards (guild_id, user_id, "
            "channel_id, message_id, created_at, resolved_by) "
            "VALUES (?, ?, 1, 2, 0, ?)",
            "resolved_by",
            None,
            id="promotion-review-cards",
        ),
    ],
)
def test_purge_deletes_the_subjects_row_but_only_blanks_the_actors(
    db, table, subject_sql, actor_column, blanked
):
    """Erasing a mod must not delete an unrelated member's record."""
    with open_db(db) as conn:
        # The member as subject — this row is about them and goes.
        conn.execute(subject_sql, (GUILD, USER, 999))
        # The member as actor on someone else's row — the row stands, the id
        # is blanked, because the record belongs to OTHER_USER.
        conn.execute(subject_sql, (GUILD, OTHER_USER, USER))
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        rows = conn.execute(
            f"SELECT user_id, {actor_column} FROM {table}"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(OTHER_USER, blanked)]


def test_purge_takes_the_members_open_break_only(db):
    """A break is the member's own choice, not a sanction: erasure takes it.
    Another member's open break in the same guild is untouched."""
    with open_db(db) as conn:
        for uid in (USER, OTHER_USER):
            conn.execute(
                "INSERT INTO break_holds (guild_id, user_id, stored_roles, started_at) "
                "VALUES (?, ?, '[1]', 0)",
                (GUILD, uid),
            )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        rows = conn.execute("SELECT user_id FROM break_holds").fetchall()
    assert [r[0] for r in rows] == [OTHER_USER]


def test_purge_clears_qa_verdicts_but_only_unsigns_the_test(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO qa_tests (id, guild_id, entry_key, title, body_md, "
            "verified_by, created_at, updated_at) "
            "VALUES (1, ?, 'k', 't', 'b', ?, '', '')",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO qa_verdicts (test_id, guild_id, user_id, verdict, "
            "note, created_at, updated_at) "
            "VALUES (1, ?, ?, 'pass', 'my note', '', '')",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO qa_verdicts (test_id, guild_id, user_id, verdict, "
            "voided_by, created_at, updated_at) "
            "VALUES (1, ?, ?, 'fail', ?, '', '')",
            (GUILD, OTHER_USER, USER),
        )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        # The test itself survives — it is the project's work product.
        assert tuple(conn.execute(
            "SELECT COUNT(*), verified_by FROM qa_tests"
        ).fetchone()) == (1, None)
        # Their own verdict goes; the one they merely voided stands, unsigned.
        assert [
            tuple(r) for r in conn.execute(
                "SELECT user_id, voided_by FROM qa_verdicts"
            ).fetchall()
        ] == [(OTHER_USER, None)]


def test_purge_clears_the_members_own_blocks_but_not_blocks_against_them(db):
    """A block list withholds something — the other member's entry stands."""
    with open_db(db) as conn:
        for owner, target in ((USER, OTHER_USER), (OTHER_USER, USER)):
            conn.execute(
                "INSERT INTO voice_master_blocked (guild_id, owner_id, "
                "target_id, added_at) VALUES (?, ?, ?, 0)",
                (GUILD, owner, target),
            )
        conn.commit()

    with open_db(db) as conn:
        purge_user_data(conn, GUILD, USER)
        conn.commit()

    with open_db(db) as conn:
        rows = conn.execute(
            "SELECT owner_id, target_id FROM voice_master_blocked"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(OTHER_USER, USER)]


# ---------------------------------------------------------------------------
# Bug #222 — the rows that kept member data and named no Art 17(3) ground
# ---------------------------------------------------------------------------
#
# Each register row said "preserved" with an engineering reason in the cell
# (matchmaking memory, operational, authority grant). An engineering reason is
# not a lawful ground, so each was re-decided. The tests below are the
# decisions; the grounds are in docs/data_register.md and the rationale for
# each is in the comment beside its statement in privacy_service.py.


def test_purge_clears_a_grant_keeper_allow_list_entry(db):
    """A member named on *Who Can Hand This Out* loses the entry.

    An authority grant is not a record anyone relies on afterwards: erasing it
    revokes a keeper's ability to hand a role out, which is the same effect as
    an admin removing the row on the Role Grants panel.
    """
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO grant_role_permissions (guild_id, grant_name, entity_type, entity_id) "
            "VALUES (?, 'verified', 'user', ?)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM grant_role_permissions WHERE entity_id = ?", (USER,)
        ).fetchone()[0] == 0


def test_purge_leaves_a_role_entry_on_the_same_allow_list(db):
    """`entity_type = 'role'` rows name a role, not a member — and a role id
    can share the numeric space with a member id, so the purge must match on
    the type as well as the value."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO grant_role_permissions (guild_id, grant_name, entity_type, entity_id) "
            "VALUES (?, 'verified', 'role', ?)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM grant_role_permissions WHERE entity_type = 'role'"
        ).fetchone()[0] == 1


def test_purge_clears_the_confession_rate_limit_row(db):
    """`last_confess_at` is a timestamp saying this member posted an anonymous
    confession at that moment — deanonymising against a visible post, and the
    reason "operational, no content" was never a ground to keep it."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO confession_rate_limits "
            "(guild_id, author_id, last_confess_at, day_key, day_count) "
            "VALUES (?, ?, 1700000000, '2026-09-17', 2)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM confession_rate_limits WHERE guild_id = ? AND author_id = ?",
            (GUILD, USER),
        ).fetchone()[0] == 0


def test_purge_clears_the_whisper_store_through_the_shared_engine(db):
    """The whispers half of bug #222: the deletes existed but only
    ``/whisper forget-me`` could reach them, so an operator erasure left every
    whisper standing. Same function, both callers."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO whispers (id, guild_id, sender_id, target_id, message, created_at) "
            "VALUES (1, ?, ?, ?, 'mine', 1)",
            (GUILD, USER, OTHER_USER),
        )
        conn.execute(
            "INSERT INTO whispers (id, guild_id, sender_id, target_id, message, created_at) "
            "VALUES (2, ?, ?, ?, 'theirs', 1)",
            (GUILD, OTHER_USER, OTHER_USER + 1),
        )
        conn.execute(
            "INSERT INTO whisper_guesses (whisper_id, guessed_id, correct, created_at) "
            "VALUES (1, ?, 0, 1)",
            (OTHER_USER,),
        )
        conn.execute(
            "INSERT INTO whisper_replies (whisper_id, from_user_id, to_user_id, content, created_at) "
            "VALUES (1, ?, ?, 'r', 1)",
            (OTHER_USER, USER),
        )
        purge_user_data(conn, GUILD, USER)

        assert conn.execute("SELECT COUNT(*) FROM whispers").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM whispers").fetchone()[0] == 2
        # The children go even though this connection makes no promise about
        # PRAGMA foreign_keys, which is why forget_member deletes them itself.
        assert conn.execute("SELECT COUNT(*) FROM whisper_guesses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM whisper_replies").fetchone()[0] == 0


def test_purge_keeps_a_whisper_report_the_member_filed_on_someone_else(db):
    """Art 17(3)(e): the row is evidence about another member's message."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO whispers (id, guild_id, sender_id, target_id, message, created_at) "
            "VALUES (5, ?, ?, ?, 'theirs', 1)",
            (GUILD, OTHER_USER, OTHER_USER + 1),
        )
        conn.execute(
            "INSERT INTO whisper_reports (whisper_id, reporter_id, reason, created_at) "
            "VALUES (5, ?, 'rude', 1)",
            (USER,),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM whisper_reports WHERE reporter_id = ?", (USER,)
        ).fetchone()[0] == 1


def test_purge_takes_the_member_out_of_the_pen_pals_pool(db):
    """A live defect, not only a register one: the pool is "who is waiting to
    be matched right now", so an erased member left in it gets matched and a
    private channel opened for an account that is not supposed to exist."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO pen_pals_pool (guild_id, user_id, joined_at) VALUES (?, ?, 1)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM pen_pals_pool WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()[0] == 0


def _seed_pen_pal_session(conn, session_id, u1, u2, state="active"):
    conn.execute(
        "INSERT INTO pen_pals_sessions (session_id, guild_id, channel_id, user1_id, "
        "user2_id, started_at, expiry_at, next_question_at, state) "
        "VALUES (?, ?, 777, ?, ?, 1, 2, 3, ?)",
        (session_id, GUILD, u1, u2, state),
    )


def test_purge_closes_a_live_pen_pals_session_rather_than_stranding_it(db):
    """The row can't simply go: the orphan-channel sweep keys on a *closed*
    session, so deleting the row outright leaves the private channel standing
    forever with nothing left that knows to remove it."""
    with open_db(db) as conn:
        _seed_pen_pal_session(conn, "live-1", USER, OTHER_USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT state, close_reason FROM pen_pals_sessions WHERE session_id = 'live-1'"
        ).fetchone()
        assert row["state"] == "closed"
        assert row["close_reason"] == "purged"


@pytest.mark.parametrize("seat", ["user1_id", "user2_id"])
def test_purge_scrubs_the_member_out_of_pen_pals_sessions(db, seat):
    """Whichever seat they held, their id leaves the pairing record.

    The row itself survives because it is also the *other* member's no-repeat
    memory — Art 17(3)(e), applied to the one column rather than the row.
    """
    u1, u2 = (USER, OTHER_USER) if seat == "user1_id" else (OTHER_USER, USER)
    with open_db(db) as conn:
        _seed_pen_pal_session(conn, "past-1", u1, u2, state="closed")
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT user1_id, user2_id FROM pen_pals_sessions WHERE session_id = 'past-1'"
        ).fetchone()
        assert USER not in (row["user1_id"], row["user2_id"])
        assert OTHER_USER in (row["user1_id"], row["user2_id"])


def test_purge_clears_the_members_own_pen_pals_blocks(db):
    """Their own protective choice is a preference with no ground to outlive
    them — the `pen_pals_optouts` precedent."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO pen_pals_blocks (guild_id, user_id, blocked_user_id, source) "
            "VALUES (?, ?, ?, 'member')",
            (GUILD, USER, OTHER_USER),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM pen_pals_blocks WHERE user_id = ?", (USER,)
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("user_id", "blocked_user_id", "source", "why"),
    [
        pytest.param(OTHER_USER, USER, "member", "somebody blocked them", id="blocked-by-another"),
        pytest.param(OTHER_USER, USER, "admin", "an admin separation", id="admin-against-them"),
        pytest.param(USER, OTHER_USER, "admin", "an admin separation", id="admin-by-them"),
    ],
)
def test_purge_keeps_a_pen_pals_block_that_protects_someone_else(
    db, user_id, blocked_user_id, source, why
):
    """Art 17(3)(e), the `voice_master_blocked` precedent: a block another
    member or a moderator put in place is a protective record about *them*,
    and an erasure must not be a way to clear it."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO pen_pals_blocks (guild_id, user_id, blocked_user_id, source) "
            "VALUES (?, ?, ?, ?)",
            (GUILD, user_id, blocked_user_id, source),
        )
        purge_user_data(conn, GUILD, USER)
        assert conn.execute(
            "SELECT COUNT(*) FROM pen_pals_blocks WHERE source = ?", (source,)
        ).fetchone()[0] == 1, why


def test_purge_blanks_a_closed_mahjong_tables_state_snapshot(db):
    """The closed row can't be deleted — `mahjong_results` is preserved game
    history and points at it by foreign key — so the personal data inside it
    goes instead: the state JSON is a snapshot naming seat member ids, and
    nothing reads a closed table's state back."""
    state = '{"seats": [{"user_id": %d, "rack": ["1B"]}]}' % USER
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO mahjong_cards (id, guild_id, card_id, display_name, "
            "season, card_json, created_at) "
            "VALUES (1, ?, 'c', 'Card', 'meadow', '{}', 1)",
            (GUILD,),
        )
        conn.execute(
            "INSERT INTO mahjong_tables (id, guild_id, channel_id, mode, stake, "
            "card_row_id, host_id, status, state, created_at, updated_at) "
            "VALUES (1, ?, 5, 4, 10, 1, ?, 'closed', ?, 1, 1)",
            (GUILD, USER, state),
        )
        conn.execute(
            "INSERT INTO mahjong_seats (table_id, guild_id, user_id, seat_index, live) "
            "VALUES (1, ?, ?, 0, 0)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT state, host_id FROM mahjong_tables WHERE id = 1"
        ).fetchone()
        assert str(USER) not in row["state"]
        assert row["host_id"] == 0


def test_purge_leaves_a_closed_mahjong_table_the_member_never_sat_at(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO mahjong_cards (id, guild_id, card_id, display_name, "
            "season, card_json, created_at) "
            "VALUES (1, ?, 'c', 'Card', 'meadow', '{}', 1)",
            (GUILD,),
        )
        conn.execute(
            "INSERT INTO mahjong_tables (id, guild_id, channel_id, mode, stake, "
            "card_row_id, host_id, status, state, created_at, updated_at) "
            "VALUES (1, ?, 5, 4, 10, 1, ?, 'closed', '{\"seats\": []}', 1, 1)",
            (GUILD, OTHER_USER),
        )
        purge_user_data(conn, GUILD, USER)
        row = conn.execute(
            "SELECT state, host_id FROM mahjong_tables WHERE id = 1"
        ).fetchone()
        assert row["state"] == '{"seats": []}'
        assert row["host_id"] == OTHER_USER


# ── Guess Who and the welcome card (2026-09-20 decisions) ──────────────


def _insert_round(conn, round_id, *, submitter, answer=None, guild=GUILD, solver=None):
    conn.execute(
        "INSERT INTO guess_rounds (id, guild_id, submitter_id, answer_id, channel_id,"
        " message_id, created_at, solver_id, confession_text) VALUES (?,?,?,?,1,1,1.0,?,'')",
        (round_id, guild, submitter, answer if answer is not None else submitter, solver),
    )


def _insert_guess(conn, round_id, guesser, guessed):
    conn.execute(
        "INSERT INTO guess_guesses (round_id, guesser_id, guessed_user_id, correct,"
        " created_at) VALUES (?,?,?,0,1.0)",
        (round_id, guesser, guessed),
    )


def test_purge_takes_the_members_guess_round_whole(db):
    """Until 2026-09-20 no ``guess_*`` table but ``guess_consents`` was purged
    at all, so an erasure left the member's submitted photo and confession
    standing. The round goes whole, taking other players' guesses on it."""
    with open_db(db) as conn:
        _insert_round(conn, 1, submitter=USER)
        _insert_guess(conn, 1, OTHER_USER, USER)
        conn.execute(
            "INSERT INTO guess_audit_log (guild_id, ts, actor_id, action, round_id)"
            " VALUES (?, 1.0, ?, 'submit', 1)",
            (GUILD, USER),
        )
        purge_user_data(conn, GUILD, USER)

        assert conn.execute("SELECT COUNT(*) FROM guess_rounds").fetchone()[0] == 0
        # The children go with it: this connection makes no promise about
        # PRAGMA foreign_keys, so the cascade cannot be relied on.
        assert conn.execute("SELECT COUNT(*) FROM guess_guesses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM guess_audit_log").fetchone()[0] == 0


def test_purge_takes_a_round_whose_answer_is_the_member(db):
    """The photo is of them even when somebody else submitted it."""
    with open_db(db) as conn:
        _insert_round(conn, 1, submitter=OTHER_USER, answer=USER)
        purge_user_data(conn, GUILD, USER)
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds").fetchone()[0] == 0


def test_purge_leaves_another_members_round_standing(db):
    """Scope: the erasure is not a way to clear the rest of the game."""
    with open_db(db) as conn:
        _insert_round(conn, 1, submitter=OTHER_USER)
        _insert_guess(conn, 1, OTHER_USER + 1, OTHER_USER)
        purge_user_data(conn, GUILD, USER)
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM guess_guesses").fetchone()[0] == 1


def test_purge_clears_the_members_play_on_a_surviving_round(db):
    """Both directions: a guess they made, and a guess naming them.
    ``guess_guesses`` has no guild_id, so the scope comes from the round."""
    with open_db(db) as conn:
        _insert_round(conn, 1, submitter=OTHER_USER)
        _insert_guess(conn, 1, USER, OTHER_USER)
        _insert_guess(conn, 1, OTHER_USER + 1, USER)
        _insert_guess(conn, 1, OTHER_USER + 1, OTHER_USER)
        purge_user_data(conn, GUILD, USER)

        rows = conn.execute("SELECT guesser_id, guessed_user_id FROM guess_guesses").fetchall()
        assert [tuple(r) for r in rows] == [(OTHER_USER + 1, OTHER_USER)]


def test_purge_does_not_reach_another_guilds_guess_rows(db):
    with open_db(db) as conn:
        _insert_round(conn, 1, submitter=USER, guild=GUILD + 1)
        _insert_guess(conn, 1, USER, OTHER_USER)
        purge_user_data(conn, GUILD, USER)
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM guess_guesses").fetchone()[0] == 1


def test_purge_unnames_the_member_as_solver_without_deleting_the_round(db):
    """Art 17(3)(e) applied to the column: the round is another member's
    record, so it stands — but it stops naming the erased solver, and the
    leaderboard filters ``solver_id IS NOT NULL``."""
    with open_db(db) as conn:
        _insert_round(conn, 1, submitter=OTHER_USER, solver=USER)
        purge_user_data(conn, GUILD, USER)
        row = conn.execute("SELECT solver_id FROM guess_rounds WHERE id = 1").fetchone()
        assert row["solver_id"] is None


def test_purge_deletes_the_members_welcome_card_and_its_steps(db):
    """``intake_card_steps`` has no guild_id and reaches its card by id, so
    the ids are resolved before the card row goes."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO intake_cards (id, guild_id, user_id, channel_id, message_id,"
            " created_at) VALUES (1, ?, ?, 1, 1, 1.0)",
            (GUILD, USER),
        )
        conn.execute(
            "INSERT INTO intake_cards (id, guild_id, user_id, channel_id, message_id,"
            " created_at, resolved_by) VALUES (2, ?, ?, 1, 1, 1.0, ?)",
            (GUILD, OTHER_USER, USER),
        )
        for card_id in (1, 2):
            conn.execute(
                "INSERT INTO intake_card_steps (card_id, position, step_key, label,"
                " done_by) VALUES (?, 1, 'rules', 'Read the rules', ?)",
                (card_id, USER),
            )
        purge_user_data(conn, GUILD, USER)

        assert [r["id"] for r in conn.execute("SELECT id FROM intake_cards")] == [2]
        steps = conn.execute("SELECT card_id, done_by FROM intake_card_steps").fetchall()
        # Their own card's steps go; their greeter id on the OTHER member's
        # card stays — that is the other member's welcome record (Art 17(3)(e)).
        assert [tuple(r) for r in steps] == [(2, USER)]
