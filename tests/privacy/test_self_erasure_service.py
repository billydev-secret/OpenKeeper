"""My Data → Delete everything: privacy/self_erasure_service.py.

What must hold, and is tested here rather than trusted:

- a jailed or ticketed member is refused before anything is deleted, and
  again if the hold lands while the message scan runs;
- a message that would not delete stops the run with nothing anonymised;
- an open break is ended through the hook (roles back), and a break that
  won't end stops the run before any Guess post or row goes;
- a finished run leaves their Guess rounds, photo and post gone, their other
  rows on a surrogate, the moderation record on their real id, and an audit
  receipt that names neither them nor them as actor;
- all-guild scope only from the primary guild with no other shared guild.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.privacy.deletion_service import DeletionResult
from bot_modules.privacy.logic import MODE_ALL
from bot_modules.privacy import departure_erasure_service as dep
from bot_modules.privacy import self_erasure_service as svc
from bot_modules.games.guess.repo import (
    insert_round,
    set_round_original_path,
    update_round_message,
)
from tests.db_template import migrated_db

PRIMARY = 1469491362444480666
SECOND = 1358999999999999997
SUBJECT = 900000000000000001
OTHER = 900000000000000002
BOT_ID = 900000000000000099


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "self_erasure.db"
    migrated_db(path)
    with open_db(path) as conn:
        for guild in (PRIMARY, SECOND):
            for user, name in ((SUBJECT, "saltandmarble"), (OTHER, "someone_else")):
                conn.execute(
                    "INSERT INTO known_users (guild_id, user_id, username, display_name, "
                    "updated_at, is_bot, current_member) VALUES (?, ?, ?, ?, 1000.0, 0, 1)",
                    (guild, user, name, name),
                )
    return path


class FakeDiscord:
    def __init__(self, *, failed=0, elsewhere=False, break_ends=True, on_delete=None):
        self.failed = failed
        self.elsewhere = elsewhere
        self.break_ends = break_ends
        self.on_delete = on_delete
        self.calls: list[str] = []
        self.posts_deleted: list[tuple[int, int, int]] = []

    async def delete_messages(self):
        self.calls.append("messages")
        if self.on_delete:
            self.on_delete()
        return DeletionResult(MODE_ALL, found=3, deleted=3 - self.failed, failed=self.failed)

    async def end_break(self):
        self.calls.append("break")
        return self.break_ends

    async def present_elsewhere(self):
        self.calls.append("presence")
        return self.elsewhere

    async def delete_guess_post(self, guild_id, channel_id, message_id):
        self.posts_deleted.append((guild_id, channel_id, message_id))

    def hooks(self):
        return svc.SelfErasureHooks(
            delete_messages=self.delete_messages,
            end_break=self.end_break,
            present_elsewhere=self.present_elsewhere,
            channel_ids=lambda: [500],
            delete_guess_post=self.delete_guess_post,
        )


async def _run(db, fake, guild=PRIMARY):
    return await svc.erase_everything(
        db,
        guild_id=guild,
        user_id=SUBJECT,
        primary_guild_id=PRIMARY,
        actor_id=BOT_ID,
        hooks=fake.hooks(),
    )


def _ids_in(db, guild):
    with open_db(db) as conn:
        return {
            r[0]
            for r in conn.execute("SELECT user_id FROM known_users WHERE guild_id = ?", (guild,))
        }


def _posted_round(db, tmp_path, guild=PRIMARY):
    photo = tmp_path / f"orig-{guild}.jpg"
    photo.write_bytes(b"jpeg")
    with open_db(db) as conn:
        rid = insert_round(
            conn, guild_id=guild, submitter_id=SUBJECT, answer_id=SUBJECT,
            difficulty="easy", candidate_count=1,
        )
        set_round_original_path(conn, rid, str(photo))
        update_round_message(conn, rid, message_id=77, crop_url="", crop_path="")
        conn.execute("UPDATE guess_rounds SET channel_id = 66 WHERE id = ?", (rid,))
    return rid, photo


def _jail(conn, guild=PRIMARY):
    conn.execute(
        "INSERT INTO jails (guild_id, user_id, moderator_id, created_at, status) "
        "VALUES (?, ?, ?, 1000.0, 'active')",
        (guild, SUBJECT, OTHER),
    )


def _open_ticket(conn):
    conn.execute(
        "INSERT INTO tickets (guild_id, user_id, channel_id, description, "
        "source_message_url, status, escalated, created_at, close_reason) "
        "VALUES (?, ?, 500, '', '', 'open', 0, 1000.0, '')",
        (PRIMARY, SUBJECT),
    )


async def test_happy_path_deletes_guess_whole_and_anonymises_the_rest(db, tmp_path):
    rid, photo = _posted_round(db, tmp_path)
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (?, ?, ?, 'x', 1000.0)",
            (PRIMARY, SUBJECT, OTHER),
        )
    fake = FakeDiscord()

    outcome = await _run(db, fake)

    assert (outcome.status, outcome.detail) == (svc.STATUS_DONE, dep.SCOPE_ALL)
    assert fake.calls == ["messages", "presence", "break"]
    assert fake.posts_deleted == [(PRIMARY, 66, 77)]
    assert not photo.exists()
    assert SUBJECT not in _ids_in(db, PRIMARY) | _ids_in(db, SECOND)
    with open_db(db) as conn:
        # Deleted, not rewritten to the surrogate: "clear everything from guess".
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds WHERE id = ?", (rid,)).fetchone()[0] == 0
        # ANONYMIZE_KEEP_TABLES: the moderation record keeps the real id.
        assert conn.execute("SELECT user_id FROM warnings").fetchone()[0] == SUBJECT
        [audit] = conn.execute(
            "SELECT actor_id, target_id, extra FROM audit_log WHERE action = ?",
            (svc.AUDIT_ACTION,),
        ).fetchall()
    assert audit[0] == BOT_ID and audit[1] is None
    assert str(SUBJECT) not in audit[2]
    assert json.loads(audit[2])["scope"] == dep.SCOPE_ALL


@pytest.mark.parametrize(
    ("guild", "elsewhere", "scope", "asked"),
    [
        pytest.param(PRIMARY, False, dep.SCOPE_ALL, True, id="primary-only-member"),
        pytest.param(PRIMARY, True, dep.SCOPE_GUILD, True, id="primary-but-elsewhere-too"),
        pytest.param(SECOND, False, dep.SCOPE_GUILD, False, id="second-guild-never-all"),
    ],
)
async def test_scope(db, guild, elsewhere, scope, asked):
    fake = FakeDiscord(elsewhere=elsewhere)
    outcome = await _run(db, fake, guild=guild)
    assert (outcome.status, outcome.detail) == (svc.STATUS_DONE, scope)
    assert ("presence" in fake.calls) is asked
    if scope == dep.SCOPE_GUILD:
        other = SECOND if guild == PRIMARY else PRIMARY
        assert SUBJECT in _ids_in(db, other)
        assert SUBJECT not in _ids_in(db, guild)


@pytest.mark.parametrize(
    ("setup", "hold"),
    [
        pytest.param(_jail, dep.HOLD_JAILED, id="jailed"),
        pytest.param(_open_ticket, dep.HOLD_TICKET, id="ticket"),
    ],
)
async def test_a_moderation_hold_refuses_before_anything_is_deleted(db, setup, hold):
    with open_db(db) as conn:
        setup(conn)
    fake = FakeDiscord()
    outcome = await _run(db, fake)
    assert (outcome.status, outcome.detail) == (svc.STATUS_HELD, hold)
    assert fake.calls == []
    assert SUBJECT in _ids_in(db, PRIMARY)


async def test_a_jail_landing_during_the_scan_stops_the_run(db, tmp_path):
    rid, photo = _posted_round(db, tmp_path)

    def _jailed_mid_scan():
        with open_db(db) as conn:
            _jail(conn)

    fake = FakeDiscord(on_delete=_jailed_mid_scan)
    outcome = await _run(db, fake)
    assert (outcome.status, outcome.detail) == (svc.STATUS_HELD, dep.HOLD_JAILED)
    assert "break" not in fake.calls
    assert photo.exists() and fake.posts_deleted == []
    assert SUBJECT in _ids_in(db, PRIMARY)


async def test_a_jail_landing_while_guess_posts_come_down_still_holds(db, tmp_path):
    """The third check, under the write lock: posts and files are gone, rows are not."""
    rid, photo = _posted_round(db, tmp_path)
    fake = FakeDiscord()
    delete_post = fake.delete_guess_post

    async def _jailed_mid_cleanup(guild_id, channel_id, message_id):
        await delete_post(guild_id, channel_id, message_id)
        with open_db(db) as conn:
            _jail(conn)

    fake.delete_guess_post = _jailed_mid_cleanup
    outcome = await _run(db, fake)
    assert (outcome.status, outcome.detail) == (svc.STATUS_HELD, dep.HOLD_JAILED)
    assert fake.posts_deleted == [(PRIMARY, 66, 77)]
    assert SUBJECT in _ids_in(db, PRIMARY)
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds WHERE id = ?", (rid,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", (svc.AUDIT_ACTION,)
        ).fetchone()[0] == 0


async def test_a_message_that_would_not_delete_anonymises_nothing(db, tmp_path):
    rid, photo = _posted_round(db, tmp_path)
    fake = FakeDiscord(failed=1)
    outcome = await _run(db, fake)
    assert outcome.status == svc.STATUS_MESSAGES_LEFT
    assert outcome.messages is not None and outcome.messages.failed == 1
    assert fake.calls == ["messages"]
    assert photo.exists()
    assert SUBJECT in _ids_in(db, PRIMARY)


async def test_a_break_that_will_not_end_stops_before_guess(db, tmp_path):
    """A present member's break is ended, not deleted: a deleted row would
    leave them wearing the break role with nothing to restore."""
    rid, photo = _posted_round(db, tmp_path)
    fake = FakeDiscord(break_ends=False)
    outcome = await _run(db, fake)
    assert outcome.status == svc.STATUS_BREAK_STUCK
    assert photo.exists() and fake.posts_deleted == []
    assert SUBJECT in _ids_in(db, PRIMARY)


async def test_a_step_that_raises_reports_an_error_not_done(db):
    fake = FakeDiscord()

    async def _boom():
        raise RuntimeError("presence unknown")

    hooks = fake.hooks()
    hooks = svc.SelfErasureHooks(
        delete_messages=hooks.delete_messages,
        end_break=hooks.end_break,
        present_elsewhere=_boom,
        channel_ids=hooks.channel_ids,
        delete_guess_post=hooks.delete_guess_post,
    )
    outcome = await svc.erase_everything(
        db, guild_id=PRIMARY, user_id=SUBJECT, primary_guild_id=PRIMARY,
        actor_id=BOT_ID, hooks=hooks,
    )
    assert outcome.status == svc.STATUS_ERROR
    assert SUBJECT in _ids_in(db, PRIMARY)
