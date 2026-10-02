"""The guild-scoped anonymise in privacy/service.py.

``anonymize_user_guild_data`` is the departure worker's answer for a member
who left one server but is still in another. Its whole contract is a boundary:
everything of the left guild's that names the member is rewritten exactly as
the all-guild path would, and **nothing outside that guild moves by a byte**.
Both halves are computed here rather than trusted to the narrowing code.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.privacy.guild_purge_service import (
    CHANNEL_TABLES,
    CHILD_TABLES,
    child_where,
)
from bot_modules.privacy.service import (
    ANONYMIZE_KEEP_TABLES,
    anonymize_user_data,
    anonymize_user_guild_data,
    next_surrogate_id,
    plan_guild_anonymization,
)
from tests.db_template import migrated_db

LEFT = 1469491362444480666
STAYED = 1358999999999999997
LEFT_CHANNELS = (500, 501)
STAYED_CHANNEL = 900
SUBJECT = 900000000000000001
OTHER = 900000000000000002
SUBJECT_NAME = "saltandmarble"
SURROGATE = 1


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "anon_guild.db"
    migrated_db(path)
    with open_db(path) as conn:
        _seed(conn)
        conn.commit()
    return path


def _seed(conn: sqlite3.Connection) -> None:
    """The subject active in both guilds, with a row in each kind of table the
    scope has to route: guild-keyed, child, channel-keyed, global and KEEP."""
    for guild in (LEFT, STAYED):
        conn.executemany(
            "INSERT INTO known_users (guild_id, user_id, username, display_name, "
            "updated_at, is_bot, current_member) VALUES (?, ?, ?, ?, 1000.0, 0, 1)",
            [
                (guild, SUBJECT, SUBJECT_NAME, "Lark"),
                (guild, OTHER, "someone_else", "Someone"),
            ],
        )

    conn.executemany(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts, "
        "sentiment) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, LEFT, 500, SUBJECT, 1000, 0.5),
            (2, LEFT, 501, SUBJECT, 1001, 0.25),
            (3, LEFT, 500, OTHER, 1002, 0.75),
            (11, STAYED, STAYED_CHANNEL, SUBJECT, 1003, -0.5),
            (12, STAYED, STAYED_CHANNEL, OTHER, 1004, 0.0),
        ],
    )
    # Child table: rows reach their guild only through `messages`.
    conn.executemany(
        "INSERT INTO message_mentions (message_id, user_id) VALUES (?, ?)",
        [(3, SUBJECT), (12, SUBJECT)],
    )
    conn.executemany(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, "
        "channel_id) VALUES (?, ?, 'message', ?, ?, ?)",
        [
            (LEFT, SUBJECT, 5.5, 1000.0, 500),
            (LEFT, SUBJECT, 7.25, 1001.0, 501),
            (LEFT, OTHER, 3.0, 1002.0, 500),
            (STAYED, SUBJECT, 9.0, 1003.0, STAYED_CHANNEL),
        ],
    )

    # A whisper in each guild from OTHER naming the subject by username, each
    # with a reply the subject wrote (a child table's free text).
    for guild in (LEFT, STAYED):
        cur = conn.execute(
            "INSERT INTO whispers (guild_id, sender_id, target_id, message, "
            "created_at, state, solved, exposed, guesses_left) "
            "VALUES (?, ?, ?, ?, 1000.0, 'open', 0, 0, 3)",
            (guild, OTHER, SUBJECT, f"hello {SUBJECT_NAME}"),
        )
        conn.execute(
            "INSERT INTO whisper_replies (whisper_id, from_user_id, to_user_id, "
            "content, created_at) VALUES (?, ?, ?, 'my own reply', 1001.0)",
            (cur.lastrowid, SUBJECT, OTHER),
        )

    # KEEP table, in the left guild.
    conn.execute(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, "
        "created_at, revoked, revoke_reason) VALUES (?, ?, ?, 'spam', 1000.0, 0, '')",
        (LEFT, SUBJECT, OTHER),
    )
    # Channel-keyed table, one channel in each guild.
    conn.executemany(
        "INSERT INTO legitlibs_channel_config (channel_id, max_tier, set_by) "
        "VALUES (?, 1, ?)",
        [(500, SUBJECT), (STAYED_CHANNEL, SUBJECT)],
    )
    # Bot-wide table: belongs to no guild.
    conn.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text, "
        "added_by, tags) VALUES ('wyr', 'misc', ?, ?, '')",
        (f"asked by {SUBJECT_NAME}", SUBJECT),
    )


def _anonymize(db_path, guild_id=LEFT, *, channel_ids=LEFT_CHANNELS, surrogate_id=SURROGATE):
    with open_db(db_path) as conn:
        report = anonymize_user_guild_data(
            conn, SUBJECT, guild_id, surrogate_id=surrogate_id, channel_ids=channel_ids
        )
        conn.commit()
    return report


def _guild_rows(conn, guild_id: int, channels: tuple[int, ...]) -> dict[str, list]:
    """Every row belonging to *guild_id*, by the same three routes the purge uses."""
    out: dict[str, list] = {}
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        if "guild_id" in cols:
            where, params = "guild_id = ?", [guild_id]
        elif table in CHILD_TABLES:
            where, params = child_where(table, guild_id)
        elif table in CHANNEL_TABLES:
            marks = ", ".join("?" for _ in channels)
            where, params = f"{CHANNEL_TABLES[table]} IN ({marks})", list(channels)
        else:
            continue
        rows = conn.execute(f'SELECT * FROM "{table}" WHERE {where}', params).fetchall()
        out[table] = sorted((tuple(r) for r in rows), key=repr)
    return out


def _names_subject(value) -> bool:
    if value == SUBJECT:
        return True
    return isinstance(value, str) and (str(SUBJECT) in value or SUBJECT_NAME in value)


# ── The boundary ─────────────────────────────────────────────────────────────


def test_the_other_guilds_rows_do_not_move_by_a_byte(db):
    with open_db(db) as conn:
        before = _guild_rows(conn, STAYED, (STAYED_CHANNEL,))
    _anonymize(db)
    with open_db(db) as conn:
        after = _guild_rows(conn, STAYED, (STAYED_CHANNEL,))
    assert after == before
    # The seed actually put the subject there, so the equality is not vacuous.
    assert any(_names_subject(v) for rows in before.values() for r in rows for v in r)


def test_the_left_guild_names_the_member_only_in_the_keep_tables(db):
    _anonymize(db)
    with open_db(db) as conn:
        rows = _guild_rows(conn, LEFT, LEFT_CHANNELS)
    leaks = sorted(
        table
        for table, table_rows in rows.items()
        if table not in ANONYMIZE_KEEP_TABLES
        and any(_names_subject(v) for r in table_rows for v in r)
    )
    assert leaks == []
    assert rows["warnings"][0][2] == SUBJECT


def test_a_child_table_is_rewritten_only_under_the_left_guilds_parents(db):
    _anonymize(db)
    with open_db(db) as conn:
        mentions = dict(
            conn.execute("SELECT message_id, user_id FROM message_mentions").fetchall()
        )
        replies = conn.execute(
            "SELECT w.guild_id, r.from_user_id, r.content FROM whisper_replies r "
            "JOIN whispers w ON w.id = r.whisper_id ORDER BY w.guild_id"
        ).fetchall()
    assert mentions == {3: SURROGATE, 12: SUBJECT}
    assert {tuple(r) for r in replies} == {
        (LEFT, SURROGATE, ""),
        (STAYED, SUBJECT, "my own reply"),
    }


@pytest.mark.parametrize(
    "channel_ids, expected, reported",
    [
        pytest.param(LEFT_CHANNELS, {500: SURROGATE, STAYED_CHANNEL: SUBJECT}, False, id="channels-given"),
        pytest.param((), {500: SUBJECT, STAYED_CHANNEL: SUBJECT}, True, id="no-channels"),
    ],
)
def test_channel_keyed_tables_follow_the_channels_given(db, channel_ids, expected, reported):
    report = _anonymize(db, channel_ids=channel_ids)
    with open_db(db) as conn:
        got = dict(
            conn.execute("SELECT channel_id, set_by FROM legitlibs_channel_config").fetchall()
        )
    assert got == expected
    assert ("legitlibs_channel_config.set_by" in report.out_of_scope) is reported


def test_a_bot_wide_table_is_left_alone_and_reported(db):
    report = _anonymize(db)
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT added_by, question_text FROM games_question_bank "
            "WHERE category = 'misc' AND game_type = 'wyr' AND tags = ''"
        ).fetchone()
    assert tuple(row) == (SUBJECT, f"asked by {SUBJECT_NAME}")
    assert report.out_of_scope["games_question_bank.added_by"] == 1
    assert report.out_of_scope["games_question_bank.question_text"] == 1
    # Reported, not counted as done.
    assert "games_question_bank.added_by" not in report.id_rewrites


def test_left_guild_metrics_are_unchanged(db):
    probes = (
        "SELECT COUNT(*) FROM messages WHERE guild_id = ? GROUP BY author_id ORDER BY 1",
        "SELECT ROUND(SUM(amount), 6) FROM xp_events WHERE guild_id = ? "
        "GROUP BY user_id ORDER BY 1",
        "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id = ?",
    )

    def measure():
        with open_db(db) as conn:
            return [conn.execute(sql, (LEFT,)).fetchall() for sql in probes]

    before = measure()
    _anonymize(db)
    assert measure() == before


# ── Roster and surrogate registry ────────────────────────────────────────────


def test_only_the_left_guilds_roster_row_is_relabelled(db):
    _anonymize(db)
    with open_db(db) as conn:
        rows = {
            r["guild_id"]: (r["user_id"], r["username"], r["display_name"], r["current_member"])
            for r in conn.execute(
                "SELECT * FROM known_users WHERE user_id IN (?, ?)", (SUBJECT, SURROGATE)
            )
        }
    assert rows == {
        LEFT: (SURROGATE, f"cleared_user_{SURROGATE}", f"Cleared user {SURROGATE}", 0),
        STAYED: (SUBJECT, SUBJECT_NAME, "Lark", 1),
    }


def test_no_roster_row_in_the_left_guild_still_registers_the_surrogate(db):
    with open_db(db) as conn:
        conn.execute(
            "DELETE FROM known_users WHERE guild_id = ? AND user_id = ?", (LEFT, SUBJECT)
        )
        conn.commit()
    report = _anonymize(db)
    assert report.id_rewrites["known_users.registered"] == 1
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT guild_id FROM known_users WHERE user_id = ?", (SURROGATE,)
        ).fetchall()[0][0] == LEFT
        assert next_surrogate_id(conn) == SURROGATE + 1


def test_leaving_both_guilds_in_turn_gives_two_distinct_surrogates(db):
    for guild, channels in ((LEFT, LEFT_CHANNELS), (STAYED, (STAYED_CHANNEL,))):
        with open_db(db) as conn:
            surrogate = next_surrogate_id(conn)
            anonymize_user_guild_data(
                conn, SUBJECT, guild, surrogate_id=surrogate, channel_ids=channels
            )
            conn.commit()
    with open_db(db) as conn:
        authors = dict(
            conn.execute(
                "SELECT guild_id, author_id FROM messages WHERE message_id IN (1, 11)"
            ).fetchall()
        )
    assert authors == {LEFT: 1, STAYED: 2}


def test_the_all_guild_run_reports_no_scope(db):
    with open_db(db) as conn:
        report = anonymize_user_data(conn, SUBJECT, surrogate_id=SURROGATE)
    assert report.scope_guild_id is None
    assert report.out_of_scope == {}


# ── Dry run and guards ───────────────────────────────────────────────────────


def test_the_plan_matches_what_the_apply_does(db):
    with open_db(db) as conn:
        planned = plan_guild_anonymization(
            conn, SUBJECT, LEFT, channel_ids=LEFT_CHANNELS, surrogate_id=SURROGATE
        )
        conn.rollback()
    applied = _anonymize(db)
    assert planned.scope_guild_id == LEFT == applied.scope_guild_id
    assert planned.text_cleared == applied.text_cleared
    assert planned.text_rewrites == applied.text_rewrites
    assert planned.out_of_scope == applied.out_of_scope
    assert planned.id_rewrites == {
        k: v for k, v in applied.id_rewrites.items() if k != "known_users.label"
    }


@pytest.mark.parametrize(
    "user_id, surrogate_id, expected",
    [
        pytest.param(SURROGATE, 2, "below the snowflake floor", id="already-a-surrogate"),
        pytest.param(SUBJECT, 0, "out of range", id="surrogate-zero"),
    ],
)
def test_invalid_arguments_are_refused(db, user_id, surrogate_id, expected):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match=expected):
            anonymize_user_guild_data(conn, user_id, LEFT, surrogate_id=surrogate_id)


def test_a_surrogate_taken_in_another_guild_is_refused(db):
    _anonymize(db, guild_id=STAYED, channel_ids=(STAYED_CHANNEL,))
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="already in use"):
            anonymize_user_guild_data(conn, SUBJECT, LEFT, surrogate_id=SURROGATE)
