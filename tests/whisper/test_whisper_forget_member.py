"""``whisper_repo.forget_member`` — the one whisper-erasure engine.

Lives at the service layer because it now has two callers: the member's own
``/mydata`` → Whisper history button, and ``privacy_service.purge_user_data``
for an operator erasure. Until 2026-09-17 these deletes were a private helper
inside ``whisper_cog``, reachable only from ``/whisper forget-me`` — so a full
operator erasure reached no whisper at all (bug #222, item 3). These tests
replace the cog-level ones that went with that command.

The scoping is the interesting part, and each rule here is a bug that was
actually filed: guild scope (an unscoped delete once wiped the member's reply
history in every *other* guild — 2026-08 review, whisper A1), and what
survives on Art 17(3)(e) rights of others (a report this member filed on
somebody else's whisper is about that member's message, not theirs).
"""

from __future__ import annotations

from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.whisper.repo import (
    count_replies,
    forget_member,
    get_whisper,
    insert_guess,
    insert_reply,
    insert_report,
    insert_whisper,
    list_guesses,
)

GUILD_ID = 9001
OTHER_GUILD = 8888
USER_A = 1001  # the member being forgotten
USER_B = 2001
USER_C = 3001


def _forget(db_path: Path, *, guild_id: int = GUILD_ID, user_id: int = USER_A) -> None:
    with open_db(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        forget_member(conn, guild_id=guild_id, user_id=user_id)


def test_deletes_whispers_sent_and_received(sync_db_path: Path):
    """Both directions go; another pair's whisper is untouched."""
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        sent = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="from A")
        received = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_B, target_id=USER_A, message="to A")
        unrelated = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_B, target_id=USER_C, message="B to C")

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, sent) is None
        assert get_whisper(conn, received) is None
        assert get_whisper(conn, unrelated) is not None


def test_does_not_touch_other_guilds(sync_db_path: Path):
    """Scoped to the guild the erasure is for — whisper A1, which this was."""
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        mine = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="here")
        elsewhere = insert_whisper(conn, guild_id=OTHER_GUILD, sender_id=USER_A, target_id=USER_B, message="there")

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, mine) is None
        assert get_whisper(conn, elsewhere) is not None


def test_guesses_replies_and_reports_cascade_off_a_deleted_whisper(sync_db_path: Path):
    """The child rows go with their parent, so nothing is stranded naming the member."""
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="from A")
        insert_guess(conn, whisper_id=wid, guessed_id=USER_C, correct=False)
        insert_reply(conn, whisper_id=wid, from_user_id=USER_B, to_user_id=USER_A, content="reply")
        insert_report(conn, whisper_id=wid, reporter_id=USER_B, reason="rude")

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, wid) is None
        assert list_guesses(conn, whisper_id=wid) == []
        assert count_replies(conn, wid) == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM whisper_reports WHERE whisper_id = ?", (wid,)
        ).fetchone()[0] == 0


def test_a_report_this_member_filed_on_someone_elses_whisper_survives(sync_db_path: Path):
    """Art 17(3)(e) rights of others, and the reason the whispers row is a
    *partial* purge rather than a clean one.

    The row is evidence about another member's message. Erasing it at the
    request of the person who reported it would take the report away from the
    member it was filed against, and from the mod who has to act on it.
    """
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        theirs = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_B, target_id=USER_C, message="B to C")
        insert_report(conn, whisper_id=theirs, reporter_id=USER_A, reason="filed by A")
        # Same for a guess naming A on a third party's whisper: someone else
        # guessed *at* them, on a message that is not theirs.
        insert_guess(conn, whisper_id=theirs, guessed_id=USER_A, correct=False)

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, theirs) is not None
        assert conn.execute(
            "SELECT reporter_id FROM whisper_reports WHERE whisper_id = ?", (theirs,)
        ).fetchone()["reporter_id"] == USER_A
        assert [g.guessed_id for g in list_guesses(conn, whisper_id=theirs)] == [USER_A]


def test_replies_the_member_wrote_on_another_pairs_whisper_go(sync_db_path: Path):
    """Their own words, on a whisper that itself survives."""
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        theirs = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_B, target_id=USER_C, message="B to C")
        insert_reply(conn, whisper_id=theirs, from_user_id=USER_A, to_user_id=USER_B, content="A wrote this")
        insert_reply(conn, whisper_id=theirs, from_user_id=USER_B, to_user_id=USER_C, content="not A's")

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, theirs) is not None
        remaining = conn.execute(
            "SELECT from_user_id FROM whisper_replies WHERE whisper_id = ?", (theirs,)
        ).fetchall()
        assert [r["from_user_id"] for r in remaining] == [USER_B]


def test_a_stranded_reply_is_swept_whatever_guild_it_came_from(sync_db_path: Path):
    """A reply whose parent whisper is already gone has no guild left to
    attribute it to, so it is deleted regardless of scope — the privacy-safe
    direction, and the one deliberate exception to the guild rule above."""
    with open_db(sync_db_path) as conn:
        # Foreign keys deliberately OFF for the insert only: the orphan this
        # tests for was created by a delete that ran *without* the cascade, so
        # the constraint refuses to reproduce it directly.
        conn.execute("PRAGMA foreign_keys = OFF")
        insert_reply(conn, whisper_id=424242, from_user_id=USER_A, to_user_id=USER_B, content="orphan")

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM whisper_replies WHERE from_user_id = ?", (USER_A,)
        ).fetchone()[0] == 0


def test_a_reply_the_member_wrote_in_another_guild_survives(sync_db_path: Path):
    """The whole of whisper A1, in one case: erasing in guild B must leave the
    member's reply history in guild A standing.

    The old cleanup deleted replies by from/to user id with no guild filter at
    all, and ``whisper_replies`` carries no guild column — its parent whisper
    is the only thing that attributes it — so the unscoped delete reached
    every guild the member had ever replied in.
    """
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        theirs_elsewhere = insert_whisper(
            conn, guild_id=OTHER_GUILD, sender_id=USER_B, target_id=USER_A, message="elsewhere"
        )
        insert_reply(
            conn, whisper_id=theirs_elsewhere, from_user_id=USER_A, to_user_id=USER_B, content="keep"
        )
        here = insert_whisper(
            conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="here"
        )
        insert_reply(conn, whisper_id=here, from_user_id=USER_B, to_user_id=USER_A, content="drop")

    _forget(sync_db_path)

    with open_db(sync_db_path) as conn:
        kept = conn.execute("SELECT content FROM whisper_replies ORDER BY id").fetchall()
        assert [r["content"] for r in kept] == ["keep"]
        assert get_whisper(conn, theirs_elsewhere) is not None
        assert get_whisper(conn, here) is None


def test_children_go_without_the_foreign_key_cascade(sync_db_path: Path):
    """The deletes must not need ``PRAGMA foreign_keys = ON``.

    The schema cascades guesses, replies and reports off a deleted whisper —
    but only when the pragma is set, and ``purge_user_data`` runs on the
    caller's connection, which makes no such promise (the pragma is a no-op
    inside a transaction, so the purge cannot set it either). Leaning on the
    cascade there would strand exactly the rows this function exists to
    remove, which is the shape of the bug that put it here.
    """
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="x")
        insert_guess(conn, whisper_id=wid, guessed_id=USER_C, correct=False)
        insert_report(conn, whisper_id=wid, reporter_id=USER_B, reason="rude")
        rid = insert_reply(conn, whisper_id=wid, from_user_id=USER_B, to_user_id=USER_A, content="r")
        conn.execute(
            "INSERT INTO whisper_reply_reports (reply_id, reporter_id, reason, created_at) "
            "VALUES (?, ?, 'rude', 1)",
            (rid, USER_B),
        )

    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        forget_member(conn, guild_id=GUILD_ID, user_id=USER_A)

    with open_db(sync_db_path) as conn:
        for table in (
            "whispers", "whisper_guesses", "whisper_reports",
            "whisper_replies", "whisper_reply_reports",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
