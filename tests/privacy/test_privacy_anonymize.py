"""Tests for the anonymisation path in privacy/service.py.

The headline is :func:`test_activity_metrics_are_identical_before_and_after`.
Everything this feature claims rests on one sentence — "every count, sum, rate
and distribution is mathematically unchanged" — and a docstring is not
enforcement, so that claim is computed here rather than asserted in prose.

The rest guard the two ways this can fail *silently*: the real id surviving
somewhere the report says it was rewritten, and a deliberate asymmetry being
tidied away by a later cleanup pass.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.privacy import service as privacy_service
from bot_modules.privacy.service import (
    ANONYMIZE_FREE_TEXT_COLUMNS,
    ANONYMIZE_JSON_TEXT_KEYS,
    ANONYMIZE_KEEP_TABLES,
    SURROGATE_ID_CEILING,
    AnonymizeReport,
    _anon_execute,
    anonymize_missed_text,
    anonymize_user_data,
    next_surrogate_id,
    plan_anonymization,
)
from tests.db_template import migrated_db

GUILD = 1469491362444480666
#: Snowflake-shaped: the floor guard rejects anything below 10^16.
SUBJECT = 900000000000000001
OTHER = 900000000000000002
SUBJECT_NAME = "saltandmarble"
SUBJECT_DISPLAY = "Lark"
SURROGATE = 1


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "anon.db"
    migrated_db(path)
    with open_db(path) as conn:
        _seed(conn)
        conn.commit()
    return path


#: Receives the other member's whisper that names the subject. Message text
#: used to carry that case, until migration 228 dropped messages.content.
BYSTANDER = 900000000000000004


def _seed(conn: sqlite3.Connection) -> None:
    """A miniature of the production shape this was measured against.

    Both members are active, the subject's text mentions them by id, username
    and display name, other members' text mentions them too, and every
    preserved category has a row so the KEEP list is exercised rather than
    assumed.
    """
    for uid, name, display in (
        (SUBJECT, SUBJECT_NAME, SUBJECT_DISPLAY),
        (OTHER, "someone_else", "Someone"),
    ):
        conn.execute(
            "INSERT INTO known_users "
            "(guild_id, user_id, username, display_name, updated_at, is_bot, "
            "current_member) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (GUILD, uid, name, display, 1000.0),
        )

    # Message rows: three of the subject's own and two of somebody else's.
    # Metadata only — the bot keeps no message text.
    conn.executemany(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, "
        "ts, sentiment) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, GUILD, 500, SUBJECT, 1000.0, 0.5),
            (2, GUILD, 500, SUBJECT, 1001.0, 0.25),
            (3, GUILD, 501, SUBJECT, 1002.0, -0.5),
            (4, GUILD, 500, OTHER, 1003.0, 0.75),
            (5, GUILD, 501, OTHER, 1004.0, 0.0),
        ],
    )
    conn.executemany(
        "INSERT INTO message_mentions (message_id, user_id) VALUES (?, ?)",
        [(4, SUBJECT), (1, OTHER)],
    )

    conn.executemany(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, "
        "channel_id) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (GUILD, SUBJECT, "message", 5.5, 1000.0, 500),
            (GUILD, SUBJECT, "message", 7.25, 1001.0, 500),
            (GUILD, SUBJECT, "voice", 12.0, 1002.0, 600),
            (GUILD, OTHER, "message", 3.0, 1003.0, 500),
        ],
    )
    conn.executemany(
        "INSERT INTO xp_daily (guild_id, user_id, source, channel_id, day, xp, "
        "events, first_at, last_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (GUILD, SUBJECT, "message", 500, "2026-09-01", 12.75, 2, 1000.0, 1001.0),
            (GUILD, OTHER, "message", 500, "2026-09-01", 3.0, 1, 1003.0, 1003.0),
        ],
    )
    conn.executemany(
        "INSERT INTO member_xp (guild_id, user_id, total_xp, level, "
        "last_message_at, last_message_norm) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (GUILD, SUBJECT, 24.75, 3, 1002.0, "and a third"),
            (GUILD, OTHER, 3.0, 1, 1004.0, "unrelated chatter"),
        ],
    )

    conn.executemany(
        "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, actor_id, "
        "meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (GUILD, SUBJECT, 100, "grant", None, None, 1000.0),
            (GUILD, SUBJECT, -30, "spend", SUBJECT, None, 1001.0),
            (GUILD, OTHER, 50, "grant", None, f'{{"from": {SUBJECT}}}', 1002.0),
        ],
    )

    # One whisper the subject sent (their words) and one they received
    # (somebody else's words, which must survive).
    conn.executemany(
        "INSERT INTO whispers (guild_id, sender_id, target_id, message, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        [
            (GUILD, SUBJECT, OTHER, "a secret I wrote", 1000.0),
            (GUILD, OTHER, SUBJECT, "a secret someone wrote to me", 1001.0),
            # Somebody else's words naming the subject in all three ways.
            (
                GUILD,
                OTHER,
                BYSTANDER,
                f"hey <@{SUBJECT}> — {SUBJECT_NAME} is {SUBJECT_DISPLAY}",
                1003.0,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO bio_answers (user_id, guild_id, slot, question_id, "
        "question_text, answer) VALUES (?, ?, 1, 1, 'where from?', 'Yorkshire')",
        (SUBJECT, GUILD),
    )
    conn.execute(
        "INSERT INTO audit_log (guild_id, action, actor_id, target_id, extra, "
        "created_at) VALUES (?, 'warn', ?, ?, '{}', ?)",
        (GUILD, OTHER, SUBJECT, 1000.0),
    )

    # The preserved categories — one row each, so KEEP is tested, not assumed.
    conn.execute(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, "
        "created_at) VALUES (?, ?, ?, 'a reason', ?)",
        (GUILD, SUBJECT, OTHER, 1000.0),
    )
    conn.execute(
        "INSERT INTO tickets (guild_id, user_id, channel_id, description, "
        "source_message_url, status, escalated, created_at, close_reason) "
        "VALUES (?, ?, 500, 'what happened to me', '', 'open', 0, ?, '')",
        (GUILD, SUBJECT, 1000.0),
    )
    conn.execute(
        "INSERT INTO no_contact_pairs (guild_id, user_low, user_high, "
        "protected_user_id, created_by, reason) VALUES (?, ?, ?, ?, ?, '')",
        (GUILD, min(SUBJECT, OTHER), max(SUBJECT, OTHER), OTHER, OTHER),
    )
    conn.execute(
        "INSERT INTO dm_audit_log (guild_id, actor_id, user_a_id, user_b_id, "
        "action, timestamp, notes) VALUES (?, ?, ?, ?, 'grant', ?, ?)",
        (GUILD, OTHER, SUBJECT, OTHER, 1000.0, f"about {SUBJECT_NAME}"),
    )

    # Surfaces the first production run left behind, reproduced at
    # the shape they were found in on 2026-09-11.
    #
    # An audit entry carrying the room name the subject typed, and a sibling
    # entry with no name key at all — the second proves the sweep lifts one
    # key rather than emptying the column.
    conn.executemany(
        "INSERT INTO audit_log (guild_id, action, actor_id, target_id, extra, "
        "created_at) VALUES (?, ?, ?, NULL, ?, ?)",
        [
            (GUILD, "vm_channel_create", SUBJECT, '{"channel_id": 500, "name": "Lark\'s Room"}', 1001.0),
            (GUILD, "vm_channel_delete", SUBJECT, '{"channel_id": 500, "reason": "external_delete"}', 1002.0),
        ],
    )

    # A QA note the subject typed, beside the verdict and payout that stay.
    conn.execute(
        "INSERT INTO qa_tests (guild_id, entry_key, title, body_md, created_at, "
        "updated_at) VALUES (?, 'k', 'a test', 'body', '2026-01-01', '2026-01-01')",
        (GUILD,),
    )
    conn.executemany(
        "INSERT INTO qa_verdicts (test_id, guild_id, user_id, verdict, note, "
        "paid_amount, created_at, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')",
        [
            (GUILD, SUBJECT, "blocked", "I cannot kidnap emojis sowwee", 50),
            (GUILD, OTHER, "pass", "someone else's note", 10),
        ],
    )


def _anonymize(db_path, *, user_id=SUBJECT, surrogate_id=SURROGATE):
    with open_db(db_path) as conn:
        report = anonymize_user_data(conn, user_id, surrogate_id=surrogate_id)
        conn.commit()
    return report


def test_a_table_name_that_is_not_a_plain_identifier_is_skipped(db, caplog):
    """The schema walk refuses a name it could not safely quote, and carries on.

    Before ``sql_identifier`` guarded the reflected names, a double quote in
    one table name broke the text sweep's statement and aborted the whole
    anonymisation.
    """
    with open_db(db) as conn:
        conn.execute('CREATE TABLE "odd""name" (user_id INTEGER, note TEXT)')
        conn.execute('INSERT INTO "odd""name" VALUES (?, ?)', (SUBJECT, "left"))
        conn.commit()

    with caplog.at_level(logging.WARNING):
        report = _anonymize(db)

    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE user_id = ?", (SUBJECT,)
        ).fetchone()[0] == 0
    assert "not a plain SQL identifier" in caplog.text
    # The report is what an operator reads before telling the subject their id
    # is gone, so the untouched table has to be on it and not only in the log.
    assert any(s.startswith('odd"name') for s in report.skipped), report.skipped


def test_the_dry_run_names_the_refused_table_too(db):
    """plan_anonymization and the apply share their report, and a dry run that
    omitted the hole would be a rehearsal of the wrong operation."""
    with open_db(db) as conn:
        conn.execute('CREATE TABLE "odd""name" (user_id INTEGER, note TEXT)')
        conn.commit()

    with open_db(db) as conn:
        report = plan_anonymization(conn, SUBJECT, surrogate_id=SURROGATE)

    assert any(s.startswith('odd"name') for s in report.skipped), report.skipped


# ── The whole claim of the feature ───────────────────────────────────────────

#: Every aggregate DK actually reports, in the shapes its panels use: totals,
#: distinct-member counts, per-channel histograms, and the *distribution* of
#: per-member values.
#:
#: The per-member probes select the aggregate WITHOUT its key on purpose. The
#: subject's key is supposed to change — that is the feature — while the
#: multiset of per-member values must not move at all. A probe selecting
#: `user_id, SUM(...)` reports a false failure on every run; it did, on the
#: first production rehearsal, showing the same 23,325.85 in the same rank
#: under a new key.
METRIC_PROBES = (
    ("messages, total", "SELECT COUNT(*) FROM messages"),
    ("messages, distinct authors", "SELECT COUNT(DISTINCT author_id) FROM messages"),
    (
        "messages, per-channel histogram",
        "SELECT channel_id, COUNT(*) FROM messages GROUP BY channel_id "
        "ORDER BY channel_id",
    ),
    (
        "messages, summed sentiment",
        "SELECT ROUND(SUM(sentiment), 6) FROM messages",
    ),
    (
        "messages, per-author count distribution",
        "SELECT COUNT(*) FROM messages GROUP BY author_id ORDER BY 1 DESC",
    ),
    ("xp_events, rows", "SELECT COUNT(*) FROM xp_events"),
    ("xp_events, summed amount", "SELECT ROUND(SUM(amount), 6) FROM xp_events"),
    ("xp_events, distinct members", "SELECT COUNT(DISTINCT user_id) FROM xp_events"),
    (
        "xp_events, per-member total distribution",
        "SELECT ROUND(SUM(amount), 6) FROM xp_events GROUP BY user_id "
        "ORDER BY 1 DESC",
    ),
    (
        "xp_events, per-source distribution",
        "SELECT source, ROUND(SUM(amount), 6) FROM xp_events GROUP BY source "
        "ORDER BY source",
    ),
    ("xp_daily, summed", "SELECT ROUND(SUM(xp), 6) FROM xp_daily"),
    ("member_xp, summed total_xp", "SELECT ROUND(SUM(total_xp), 6) FROM member_xp"),
    ("econ_ledger, net float", "SELECT SUM(amount) FROM econ_ledger"),
    (
        "econ_ledger, per-member balance distribution",
        "SELECT SUM(amount) FROM econ_ledger GROUP BY user_id ORDER BY 1 DESC",
    ),
    ("econ_ledger, distinct members", "SELECT COUNT(DISTINCT user_id) FROM econ_ledger"),
    ("message_mentions, rows", "SELECT COUNT(*) FROM message_mentions"),
    ("known_users, roster size", "SELECT COUNT(*) FROM known_users"),
)


def _metrics(db_path) -> dict[str, list]:
    with open_db(db_path) as conn:
        out = {}
        for label, sql in METRIC_PROBES:
            rows = conn.execute(sql).fetchall()
            # A probe that measures nothing compares equal to itself and would
            # pass vacuously. One did, in the operator script: xp_daily was
            # summed on a column that does not exist.
            assert rows and rows[0][0] is not None, f"probe measured nothing: {label}"
            out[label] = rows
    return out


def test_activity_metrics_are_identical_before_and_after(db):
    """THE claim: the statistics survive the rewrite untouched.

    If this fails, the feature has no reason to exist — the purge is simpler
    and more honest. Compute the battery, anonymise, recompute, compare.
    """
    before = _metrics(db)
    _anonymize(db)
    after = _metrics(db)

    moved = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not moved, "anonymisation moved an aggregate:\n" + "\n".join(
        f"  {k}\n    before: {b}\n    after:  {a}" for k, (b, a) in moved.items()
    )


def test_the_subjects_own_numbers_move_intact_to_the_surrogate(db):
    """Not merely "the totals match" — the member's own row is still one row.

    A rewrite that merged the subject into another member would keep every
    global total identical while destroying the per-member truth.
    """
    with open_db(db) as conn:
        before = conn.execute(
            "SELECT ROUND(SUM(amount), 6), COUNT(*) FROM xp_events WHERE user_id = ?",
            (SUBJECT,),
        ).fetchone()
    _anonymize(db)
    with open_db(db) as conn:
        after = conn.execute(
            "SELECT ROUND(SUM(amount), 6), COUNT(*) FROM xp_events WHERE user_id = ?",
            (SURROGATE,),
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM xp_events WHERE user_id = ?", (SUBJECT,)
        ).fetchone()[0] == 0
    assert tuple(after) == tuple(before)


# ── Where the real id may and may not survive ────────────────────────────────


def _tables(conn) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns_holding(conn, value) -> dict[str, int]:
    """Every ``table.column`` where *value* appears, by id or inside text."""
    found: dict[str, int] = {}
    for table in _tables(conn):
        for row in conn.execute(f'PRAGMA table_info("{table}")'):
            col = row[1]
            for sql, params in (
                (f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = ?', (value,)),
                (
                    f'SELECT COUNT(*) FROM "{table}" WHERE instr("{col}", ?) > 0',
                    (str(value),),
                ),
            ):
                try:
                    n = conn.execute(sql, params).fetchone()[0]
                except sqlite3.Error:
                    continue
                if n:
                    found[f"{table}.{col}"] = n
                    break
    return found


def test_the_real_id_survives_nowhere_outside_the_keep_tables(db):
    """The whole-database sweep. Anything else is a leak the report denied."""
    _anonymize(db)
    with open_db(db) as conn:
        leaked = {
            label: n
            for label, n in _columns_holding(conn, SUBJECT).items()
            if label.split(".")[0] not in ANONYMIZE_KEEP_TABLES
        }
    assert not leaked, f"the real id survived outside the KEEP tables: {leaked}"


def test_the_keep_tables_still_hold_the_real_id(db):
    """The other direction — the preserved categories must NOT be rewritten.

    A sanction, a DM-consent record and a no-contact order all exist in order
    to name a person; rewritten they say "someone did X" and the no-contact
    gate silently stops matching. See ANONYMIZE_KEEP_TABLES for the per-table
    reasoning.
    """
    _anonymize(db)
    with open_db(db) as conn:
        for table, column in (
            ("warnings", "user_id"),
            ("tickets", "user_id"),
            ("no_contact_pairs", "user_low"),
            ("dm_audit_log", "user_a_id"),
        ):
            held = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (SUBJECT,)
            ).fetchone()[0]
            assert held == 1, f"{table}.{column} lost the real id"
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (SURROGATE,)
                ).fetchone()[0]
                == 0
            )
        # The complaint text on a kept ticket stays: evidence that recorded
        # nothing would be worse than no evidence.
        assert (
            conn.execute("SELECT description FROM tickets").fetchone()[0]
            == "what happened to me"
        )


def test_no_contact_pair_still_matches_the_returning_account(db):
    """The safety case, stated as behaviour rather than as a row count.

    This is the reason no_contact_pairs is preserved: the gate refuses in a
    way the blocked party cannot distinguish from an ordinary outcome, so a
    pair that stopped matching would fail silently and for ever.
    """
    from bot_modules.no_contact.service import is_no_contact_conn

    _anonymize(db)
    with open_db(db) as conn:
        assert is_no_contact_conn(conn, GUILD, SUBJECT, OTHER)
        assert is_no_contact_conn(conn, GUILD, OTHER, SUBJECT)


# ── Names and free text ──────────────────────────────────────────────────────


def test_the_members_message_rows_and_their_metrics_remain(db):
    """Only the id changes. There is no message text left to clear — migration
    228 dropped the column — so whispers carry the own-text case below."""
    _anonymize(db)
    with open_db(db) as conn:
        rows = conn.execute(
            "SELECT ts, channel_id, sentiment FROM messages "
            "WHERE author_id = ? ORDER BY message_id",
            (SURROGATE,),
        ).fetchall()
    assert len(rows) == 3
    # The metadata every metric is computed from is untouched.
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (1000.0, 500, 0.5),
        (1001.0, 500, 0.25),
        (1002.0, 501, -0.5),
    ]


def test_text_written_by_other_people_is_kept(db):
    """`whispers` is keyed on sender_id, not target_id, and that is the rule.

    The 94 whispers the subject sent are theirs to lose; the 59 they received
    are somebody else's words.
    """
    _anonymize(db)
    with open_db(db) as conn:
        sent, received = conn.execute(
            "SELECT "
            " (SELECT message FROM whispers WHERE sender_id = ?), "
            " (SELECT message FROM whispers WHERE target_id = ?)",
            (SURROGATE, SURROGATE),
        ).fetchone()
    # Blanked rather than NULLed: whispers.message is NOT NULL, and the sweep
    # picks the empty string per column from the schema so a clear can never
    # fail a constraint and take the whole anonymisation down with it.
    assert sent == ""
    assert received == "a secret someone wrote to me"


def test_the_id_and_username_are_swept_from_other_members_text(db):
    _anonymize(db)
    with open_db(db) as conn:
        content = conn.execute(
            "SELECT message FROM whispers WHERE target_id = ?", (BYSTANDER,)
        ).fetchone()[0]
        meta = conn.execute(
            "SELECT meta FROM econ_ledger WHERE meta IS NOT NULL"
        ).fetchone()[0]
    assert str(SUBJECT) not in content
    assert SUBJECT_NAME not in content
    assert f"<@{SURROGATE}>" in content
    assert f"cleared_user_{SURROGATE}" in content
    # And inside JSON, which is where a `%id%` needle would have been the only
    # other option.
    assert meta == f'{{"from": {SURROGATE}}}'


#: A live member whose handle contains the subject's. Discord handles are
#: unique but not prefix-free, which is the whole point of this pair.
THIRD = 900000000000000003
THIRD_NAME = f"{SUBJECT_NAME}_2"


def _seed_colliding_handle(db_path) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, username, "
            "display_name, updated_at, is_bot, current_member) "
            "VALUES (?, ?, ?, ?, ?, 0, 1)",
            (GUILD, THIRD, THIRD_NAME, "Still Here", 1000.0),
        )
        conn.executemany(
            "INSERT INTO whispers (guild_id, sender_id, target_id, message, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (GUILD, THIRD, BYSTANDER, f"@{THIRD_NAME} waves at @{SUBJECT_NAME}", 2000.0),
                (GUILD, THIRD, BYSTANDER, f"only @{THIRD_NAME} here", 2001.0),
            ],
        )
        conn.commit()


def test_a_live_members_handle_is_not_corrupted_by_the_username_sweep(db):
    """Anonymising one member must never rewrite another member's record.

    The username sweep is a plain substring REPLACE and Discord handles are
    unique but **not prefix-free**. Before the shield in `_username_shields`,
    anonymising `saltandmarble` rewrote a *current* member's
    `saltandmarble_2` to `cleared_user_1_2` — in their own `known_users` row,
    and in every message that had ever mentioned them.
    """
    _seed_colliding_handle(db)
    _anonymize(db)
    with open_db(db) as conn:
        handle = conn.execute(
            "SELECT username FROM known_users WHERE user_id = ?", (THIRD,)
        ).fetchone()[0]
        both, other_only = (
            r[0]
            for r in conn.execute(
                "SELECT message FROM whispers WHERE sender_id = ? "
                "ORDER BY created_at",
                (THIRD,),
            ).fetchall()
        )
    assert handle == THIRD_NAME
    # The subject's handle goes; the live member's survives in the same string.
    assert both == f"@{THIRD_NAME} waves at @cleared_user_{SURROGATE}"
    assert other_only == f"only @{THIRD_NAME} here"


def test_the_count_ignores_a_row_that_only_names_the_live_member(db):
    """The shield must be in the count too, or the dry run overstates itself.

    A row holding *only* the other member's handle matches a naive
    `instr(col, needle)`, and SQLite reports every row its WHERE matched as
    updated even when REPLACE left it byte-for-byte identical — so comparing
    plan to apply cannot catch this. Adding such a row must not move a single
    number in the plan.
    """
    _seed_colliding_handle(db)
    with open_db(db) as conn:
        baseline = plan_anonymization(conn, SUBJECT, surrogate_id=SURROGATE)
        conn.execute(
            "INSERT INTO whispers (guild_id, sender_id, target_id, message, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (GUILD, THIRD, BYSTANDER, f"and @{THIRD_NAME} again", 2002.0),
        )
        widened = plan_anonymization(conn, SUBJECT, surrogate_id=SURROGATE)
        conn.rollback()
    assert widened.text_rewrites == baseline.text_rewrites


def test_the_display_name_is_deliberately_not_swept(db):
    """A deliberate anomaly — it reads as a miss and is not one.

    A username is a globally unique handle with no spaces; a display name is
    free-form and routinely a common word. Sweeping "Lark" across production
    would have rewritten 246 question-bank routing tags, an attachment URL,
    other members' channel and role names and an ordinary phrase that happened to contain the same word. If a
    cleanup pass adds display names to the needle list, this fails.
    """
    _anonymize(db)
    with open_db(db) as conn:
        content = conn.execute(
            "SELECT message FROM whispers WHERE target_id = ?", (BYSTANDER,)
        ).fetchone()[0]
    assert SUBJECT_DISPLAY in content


def test_the_roster_row_is_relabelled_rather_than_deleted(db):
    """Deleting it would make every embed render a bare `<@1>`.

    `name_resolver.resolve_name_from` falls back to `mention(user_id)`, and
    `is_bot` must stay 0 or `core/bot_exclusion` drops the surrogate from the
    very metrics this function preserves.
    """
    _anonymize(db)
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT username, display_name, is_bot, current_member "
            "FROM known_users WHERE user_id = ?",
            (SURROGATE,),
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM known_users WHERE user_id = ?", (SUBJECT,)
        ).fetchone()[0] == 0
    assert tuple(row) == (f"cleared_user_{SURROGATE}", f"Cleared user {SURROGATE}", 0, 1 - 1)


# ── Guards ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user_id, surrogate_id, expected",
    [
        pytest.param(SUBJECT, 0, "out of range", id="surrogate-zero"),
        pytest.param(SUBJECT, -1, "out of range", id="surrogate-negative"),
        pytest.param(
            SUBJECT, SURROGATE_ID_CEILING, "out of range", id="surrogate-at-ceiling"
        ),
        pytest.param(SUBJECT, OTHER, "out of range", id="surrogate-is-a-snowflake"),
        pytest.param(SURROGATE, 2, "below the snowflake floor", id="already-a-surrogate"),
        pytest.param(
            10**16 - 1, 2, "below the snowflake floor", id="just-under-the-floor"
        ),
    ],
)
def test_invalid_arguments_are_refused(db, user_id, surrogate_id, expected):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match=expected):
            anonymize_user_data(conn, user_id, surrogate_id=surrogate_id)


def test_a_surrogate_already_in_use_is_refused(db):
    """Reusing one merges two anonymised members and corrupts both silently."""
    _anonymize(db)
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="already in use"):
            anonymize_user_data(conn, OTHER, surrogate_id=SURROGATE)


def test_next_surrogate_id_starts_at_one_and_then_advances(db):
    with open_db(db) as conn:
        assert next_surrogate_id(conn) == 1
    _anonymize(db)
    with open_db(db) as conn:
        assert next_surrogate_id(conn) == 2


def test_a_constraint_failure_propagates_and_nothing_is_half_rewritten(db):
    """Deliberately stricter than the purge, which swallows every sqlite error.

    A swallowed statement here leaves the real id in place while the report
    claims the sweep succeeded — the feature failing silently into the exact
    state it exists to prevent. Two wallets, one per id, make the id sweep
    violate econ_wallets' primary key.
    """
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                (GUILD, SUBJECT, 70, 1000.0, 1000.0),
                (GUILD, SURROGATE, 5, 1000.0, 1000.0),
            ],
        )
        conn.commit()

    with open_db(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            anonymize_user_data(conn, SUBJECT, surrogate_id=SURROGATE)
        conn.rollback()

    # The caller owns the transaction, so the rollback is total: the member is
    # untouched rather than half-anonymised.
    with open_db(db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM xp_events WHERE user_id = ?", (SUBJECT,)
            ).fetchone()[0]
            == 3
        )
        assert (
            conn.execute(
                "SELECT message FROM whispers WHERE sender_id = ?", (SUBJECT,)
            ).fetchone()[0]
            == "a secret I wrote"
        )


def test_schema_drift_is_tolerated_but_a_real_error_is_not(db, caplog):
    """The two halves of _anon_execute's contract, side by side."""
    caplog.set_level(logging.WARNING)
    with open_db(db) as conn:
        assert (
            _anon_execute(
                conn, "UPDATE no_such_table SET x = ?", (1,), table="no_such_table"
            )
            is None
        )
        assert (
            _anon_execute(
                conn,
                "UPDATE known_users SET no_such_column = ?",
                (1,),
                table="known_users",
            )
            is None
        )
        with pytest.raises(sqlite3.Error):
            _anon_execute(conn, "UPDATE known_users SET", (), table="known_users")


def test_a_member_with_no_roster_row_still_registers_the_surrogate(db):
    """Without a roster row the surrogate used to go unregistered.

    `next_surrogate_id` reads `known_users`, so the next anonymisation was
    handed the same number and two departed members merged into one. The run
    now writes the `Cleared user N` row itself.
    """
    with open_db(db) as conn:
        conn.execute("DELETE FROM known_users WHERE user_id = ?", (SUBJECT,))
        conn.commit()
    with open_db(db) as conn:
        report = anonymize_user_data(
            conn, SUBJECT, surrogate_id=SURROGATE, roster_guild_id=GUILD
        )
        conn.commit()
    assert report.id_rewrites["known_users.registered"] == 1
    assert not report.skipped
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT guild_id, username, display_name, is_bot, current_member "
            "FROM known_users WHERE user_id = ?",
            (SURROGATE,),
        ).fetchone()
        assert tuple(row) == (
            GUILD, f"cleared_user_{SURROGATE}", f"Cleared user {SURROGATE}", 0, 0
        )
        assert next_surrogate_id(conn) == SURROGATE + 1


# ── The dry run must agree with the run ──────────────────────────────────────


def test_the_plan_matches_what_the_apply_actually_does(db):
    """A rehearsal that can drift from the thing it rehearses is worse than none.

    The operator reads the dry run and presses the button on the strength of
    it, so the two reports are built from one operation list and must agree
    row for row.
    """
    with open_db(db) as conn:
        planned = plan_anonymization(conn, SUBJECT, surrogate_id=SURROGATE)
    applied = _anonymize(db)

    assert planned.text_cleared == applied.text_cleared
    assert planned.text_rewrites == applied.text_rewrites
    # The apply adds one row the plan cannot: relabelling the roster row, which
    # is only reachable once the id sweep has moved it to the surrogate.
    assert planned.id_rewrites == {
        k: v for k, v in applied.id_rewrites.items() if k != "known_users.label"
    }
    assert applied.id_rewrites["known_users.label"] == 1
    assert planned.username == SUBJECT_NAME == applied.username
    assert planned.total_rows + 1 == applied.total_rows


def test_plan_anonymization_writes_nothing(db):
    """It is run against the live production database, mode=ro."""
    before = _metrics(db)
    with open_db(db) as conn:
        plan_anonymization(conn, SUBJECT, surrogate_id=SURROGATE)
        conn.rollback()
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE author_id = ?", (SUBJECT,)
        ).fetchone()[0] == 3
    assert _metrics(db) == before


def test_runs_clean_against_the_migrated_schema(db, caplog):
    """No statement may reference a table or column the real schema lacks.

    The mirror of test_purge_runs_clean_against_the_migrated_schema. Drift is
    tolerated at runtime, so without this the sweep could quietly skip half the
    database and still report success.
    """
    caplog.set_level(logging.WARNING)
    report = _anonymize(db)
    drift = [
        r.getMessage()
        for r in caplog.records
        if "Anonymise: skipping" in r.getMessage()
    ]
    assert not drift, "anonymise hit schema drift against the real schema:\n  " + "\n  ".join(drift)
    assert not report.skipped


def test_the_report_totals_what_it_lists(db):
    report = _anonymize(db)
    assert isinstance(report, AnonymizeReport)
    assert report.total_rows == (
        sum(report.id_rewrites.values())
        + sum(report.text_rewrites.values())
        + sum(report.text_cleared.values())
    )
    assert report.kept == sorted(ANONYMIZE_KEEP_TABLES)


# ── Deliberate anomalies: these fail if a cleanup pass makes things uniform ──


def test_the_keep_list_is_pinned():
    """A deliberate asymmetry, and the test is what a stranger's tidy-up runs.

    Each of these was decided table by table on 2026-09-11, and each removal
    breaks something that does not announce itself: a rewritten warning stops
    driving escalation, a rewritten consent record destroys the *other* party's
    defence, and a rewritten no-contact pair stops matching while the gate goes
    on refusing indistinguishably. Adding a table is just as load-bearing —
    econ_ledger is absent on purpose, because a balance is a SUM over user_id
    and moves to the surrogate intact.
    """
    assert ANONYMIZE_KEEP_TABLES == frozenset(
        {
            "dm_audit_log",
            "dm_consent_pairs",
            "jails",
            "no_contact_events",
            "no_contact_pairs",
            "policy_tickets",
            "ticket_participants",
            "tickets",
            "warnings",
        }
    )
    # Preserved by the purge, deliberately rewritten here.
    for rewritten in ("econ_ledger", "audit_log"):
        assert rewritten not in ANONYMIZE_KEEP_TABLES


@pytest.mark.parametrize(
    "table, column",
    [
        pytest.param("tickets", "description", id="kept-as-moderation-evidence"),
        pytest.param("music_playlist_tracks", "title", id="third-party-song-metadata"),
        pytest.param("music_playlist_tracks", "artist", id="third-party-song-artist"),
    ],
)
def test_third_party_text_stays_out_of_the_free_text_sweep(table, column):
    """Each of these is text a naive "clear every text column on their rows"
    sweep would take, and each would destroy somebody else's content or the
    evidence a kept row exists to be."""
    assert (table, column) not in {(t, c) for t, c, _ in ANONYMIZE_FREE_TEXT_COLUMNS}


def test_every_free_text_column_names_the_author_not_merely_the_member(db):
    """The third element must be the column naming who WROTE the text.

    Keying `whispers` on target_id instead of sender_id would clear the
    messages other people sent to the subject.
    """
    with open_db(db) as conn:
        for table, col, subject_col in ANONYMIZE_FREE_TEXT_COLUMNS:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            assert col in cols, f"{table}.{col} does not exist"
            assert subject_col in cols, f"{table}.{subject_col} does not exist"


# ── Surfaces the first production run missed ─────────────────────────────────
#
# Each was found by sweeping the live database after the 2026-09-11 apply, not
# by reading the code — the id rewrite had reached all three tables, so the
# report said "done" while the member's words sat beside the surrogate. These
# tests are what stops that combination recurring.


def test_a_json_blob_loses_only_the_members_own_key(db):
    """`json_remove`, not a column clear: 108 rows of telemetry for 38 names."""
    _anonymize(db)
    with open_db(db) as conn:
        rows = dict(
            conn.execute(
                "SELECT action, extra FROM audit_log WHERE actor_id = ?",
                (SURROGATE,),
            ).fetchall()
        )

    created = json.loads(rows["vm_channel_create"])
    assert "name" not in created, "the room name they typed survived"
    assert created["channel_id"] == 500, "the telemetry went with it"

    # A row of the same table with no name key is not touched at all.
    assert json.loads(rows["vm_channel_delete"]) == {
        "channel_id": 500,
        "reason": "external_delete",
    }


def test_the_qa_note_is_cleared_but_the_verdict_and_payout_stay(db):
    _anonymize(db)
    with open_db(db) as conn:
        mine = conn.execute(
            "SELECT verdict, note, paid_amount FROM qa_verdicts WHERE user_id = ?",
            (SURROGATE,),
        ).fetchone()
        theirs = conn.execute(
            "SELECT note FROM qa_verdicts WHERE user_id = ?", (OTHER,)
        ).fetchone()

    assert mine[1] is None, "the note they typed survived"
    assert (mine[0], mine[2]) == ("blocked", 50), "the QA record moved intact"
    assert theirs[0] == "someone else's note", "somebody else's note was taken"


def test_the_json_sweep_is_pinned():
    """It is deliberately NOT in `ANONYMIZE_FREE_TEXT_COLUMNS`.

    That list is keyed on the column naming the author, which is the guard that
    keeps other people's words out of the sweep. The JSON sweep does not fit it
    — it edits a key inside a blob — so folding it in is a silent behaviour
    change, not a tidy-up.
    """
    assert ANONYMIZE_JSON_TEXT_KEYS == (("audit_log", "extra", "$.name"),)
    flat = {(t, c) for t, c, _ in ANONYMIZE_FREE_TEXT_COLUMNS}
    assert ("audit_log", "extra") not in flat


@pytest.mark.parametrize(
    "table, column",
    [
        pytest.param("xp_events", "source", id="xp-events-source"),
        pytest.param("xp_daily", "source", id="xp-daily-source"),
        pytest.param("xp_daily", "day", id="xp-daily-day"),
        pytest.param("econ_ledger", "kind", id="econ-ledger-kind"),
    ],
)
def test_activity_labels_are_never_cleared(table, column):
    """Billy's call on 2026-09-11: keep the XP text.

    These columns are declared TEXT and sit on the subject's rows, so a sweep
    written as "every text column on a row naming this member" takes them — and
    they are not words anybody wrote. They are the enum and the date that make
    the row countable: clearing `xp_events.source` would delete the difference
    between a message and a voice minute across 48,646 rows and break the very
    statistics this whole path exists to keep.
    """
    assert (table, column) not in {(t, c) for t, c, _ in ANONYMIZE_FREE_TEXT_COLUMNS}
    assert (table, column) not in {(t, c) for t, c, _ in ANONYMIZE_JSON_TEXT_KEYS}


# ── Remediating a surrogate that was anonymised by older code ────────────────


def _remediate(db_path, surrogate_id=SURROGATE, *, dry_run=False):
    with open_db(db_path) as conn:
        report = anonymize_missed_text(conn, surrogate_id, dry_run=dry_run)
        conn.commit()
    return report


def test_remediation_clears_text_left_behind_by_an_earlier_run(db):
    """The production case: the id was rewritten, the words were not.

    Reproduced by anonymising and then writing the missed text back onto the
    surrogate's rows, which is exactly the state the 2026-09-11 run left — the
    member already carried surrogate 1 while their room names and QA note sat
    beside it.
    """
    _anonymize(db)
    with open_db(db) as conn:
        conn.execute(
            "UPDATE audit_log SET extra = ? WHERE action = 'vm_channel_create'",
            (json.dumps({"channel_id": 500, "name": "Lark's Room"}),),
        )
        conn.execute(
            "UPDATE qa_verdicts SET note = ? WHERE user_id = ?",
            ("a note the old code missed", SURROGATE),
        )
        conn.commit()

    before = _metrics(db)
    report = _remediate(db)
    assert report.total_rows == 2, report.text_cleared

    with open_db(db) as conn:
        assert "name" not in json.loads(
            conn.execute(
                "SELECT extra FROM audit_log WHERE action = 'vm_channel_create'"
            ).fetchone()[0]
        )
        assert conn.execute(
            "SELECT note FROM qa_verdicts WHERE user_id = ?", (SURROGATE,)
        ).fetchone()[0] is None

    # And the whole point: a remediation moves no number whatsoever.
    assert _metrics(db) == before


def test_remediation_is_idempotent(db):
    """A second pass finds nothing, so re-running it is never destructive."""
    _anonymize(db)
    first = _remediate(db)
    assert _remediate(db).total_rows == 0
    assert first.total_rows == 0, "the full anonymisation already did this work"


def test_remediation_dry_run_writes_nothing(db):
    _anonymize(db)
    with open_db(db) as conn:
        conn.execute(
            "UPDATE qa_verdicts SET note = 'still here' WHERE user_id = ?",
            (SURROGATE,),
        )
        conn.commit()

    assert _remediate(db, dry_run=True).total_rows == 1
    with open_db(db) as conn:
        assert (
            conn.execute(
                "SELECT note FROM qa_verdicts WHERE user_id = ?", (SURROGATE,)
            ).fetchone()[0]
            == "still here"
        )


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param(SUBJECT, id="a-real-account"),
        pytest.param(0, id="zero"),
        pytest.param(SURROGATE_ID_CEILING, id="at-the-ceiling"),
        pytest.param(-1, id="a-mahjong-house-seat"),
    ],
)
def test_remediation_refuses_anything_that_is_not_a_surrogate(db, subject):
    """The inverse guard to `_validate_surrogate`, and the more dangerous one.

    Pointed at a live member this would clear their words while leaving them
    fully identified by id and username everywhere — worse than doing nothing,
    because it destroys data without delivering any privacy.
    """
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="not a surrogate"):
            anonymize_missed_text(conn, subject)


def test_a_dry_run_does_not_claim_it_cleared_anything(db, caplog):
    """The preview and the apply log the same counts, so the verb is the only
    thing separating "this is what would happen" from "this happened" in the
    operator's log. Saying "cleared" for a dry run invites someone to believe
    a remediation already ran and skip the real one."""
    _anonymize(db)
    with open_db(db) as conn:
        conn.execute(
            "UPDATE qa_verdicts SET note = ? WHERE user_id = ?",
            ("a note the old code missed", SURROGATE),
        )
        conn.commit()

    with caplog.at_level(logging.INFO):
        _remediate(db, dry_run=True)
    assert "would clear" in caplog.text
    assert "cleared" not in caplog.text.replace("would clear", "")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _remediate(db)
    assert "cleared" in caplog.text


def test_two_sweeps_sharing_a_label_are_summed_not_overwritten(db, monkeypatch):
    """A table whose text is reached by more than one clear reports one label
    twice. Assigning keeps only the last, so the operator is told a smaller
    number than the run will touch, and the dry run they approved understates
    the apply. plan_anonymization sums; this must agree with it."""
    _anonymize(db)
    with open_db(db) as conn:
        conn.execute(
            "UPDATE qa_verdicts SET note = ? WHERE user_id = ?",
            ("a note the old code missed", SURROGATE),
        )
        conn.commit()

    baseline = _remediate(db, dry_run=True)
    bucket_name, label, count = next(
        (b, lbl, n)
        for b in ("text_rewrites", "text_cleared")
        for lbl, n in getattr(baseline, b).items()
        if n
    )

    real = privacy_service._anon_text_clear_operations

    def duplicated(conn, user_id):
        ops = real(conn, user_id)
        return ops + [op for op in ops if op[1] == label]

    monkeypatch.setattr(privacy_service, "_anon_text_clear_operations", duplicated)
    doubled = _remediate(db, dry_run=True)

    assert getattr(doubled, bucket_name)[label] == count * 2, (
        "a label produced by two operations must accumulate, not overwrite"
    )
