"""The departure auto-anonymise: privacy/departure_erasure_service.py.

What must hold, and is tested here rather than trusted:

- nothing is queued or run while the dial is off, for a bot, for a member who
  is jailed or on a live ticket, or for an id that is already a surrogate;
- a failed or partial message deletion anonymises nothing and leaves the row,
  counted, for a retry — and parks it after the last attempt;
- a member who rejoins, or is jailed while waiting, is dropped untouched;
- a finished job leaves no row, no real id in the audit entry, and no cached
  Guess Who original;
- all-guild scope only when the member left the primary guild and is in no
  other guild the bot shares.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.privacy import departure_erasure_service as dep
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
NOW = 1_000_000.0


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "departure.db"
    migrated_db(path)
    with open_db(path) as conn:
        for guild in (PRIMARY, SECOND):
            set_config_value(conn, dep.DIAL_KEY, "1", guild)
            for user, name in ((SUBJECT, "saltandmarble"), (OTHER, "someone_else")):
                conn.execute(
                    "INSERT INTO known_users (guild_id, user_id, username, display_name, "
                    "updated_at, is_bot, current_member) VALUES (?, ?, ?, ?, 1000.0, 0, 0)",
                    (guild, user, name, name),
                )
    return path


def _jail(conn, guild=PRIMARY, status="active"):
    conn.execute(
        "INSERT INTO jails (guild_id, user_id, moderator_id, created_at, status) "
        "VALUES (?, ?, ?, 1000.0, ?)",
        (guild, SUBJECT, OTHER, status),
    )


def _ticket(conn, *, opener, status="open"):
    cur = conn.execute(
        "INSERT INTO tickets (guild_id, user_id, channel_id, description, "
        "source_message_url, status, escalated, created_at, close_reason) "
        "VALUES (?, ?, 500, '', '', ?, 0, 1000.0, '')",
        (PRIMARY, opener, status),
    )
    return cur.lastrowid


def _participant(conn, ticket_id, *, removed=False):
    conn.execute(
        "INSERT INTO ticket_participants (ticket_id, user_id, added_by, added_at, removed_at) "
        "VALUES (?, ?, ?, 1000.0, ?)",
        (ticket_id, SUBJECT, OTHER, 2000.0 if removed else None),
    )


# ── hold_reason ─────────────────────────────────────────────────────────────


def _dial_off(conn):
    set_config_value(conn, dep.DIAL_KEY, "0", PRIMARY)


def _jailed(conn):
    _jail(conn)


def _released(conn):
    _jail(conn, status="released")


def _jailed_elsewhere(conn):
    _jail(conn, guild=SECOND)


def _opened_ticket(conn):
    _ticket(conn, opener=SUBJECT)


def _opened_closed_ticket(conn):
    _ticket(conn, opener=SUBJECT, status="closed")


def _opened_deleted_ticket(conn):
    _ticket(conn, opener=SUBJECT, status="deleted")


def _joined_ticket(conn):
    _participant(conn, _ticket(conn, opener=OTHER))


def _removed_from_ticket(conn):
    _participant(conn, _ticket(conn, opener=OTHER), removed=True)


@pytest.mark.parametrize(
    ("setup", "user_id", "is_bot", "expected"),
    [
        pytest.param(None, SUBJECT, False, None, id="happy"),
        pytest.param(None, SUBJECT, True, dep.HOLD_BOT, id="bot"),
        pytest.param(None, 7, False, dep.HOLD_NOT_A_MEMBER, id="already-a-surrogate"),
        pytest.param(_dial_off, SUBJECT, False, dep.HOLD_DIAL_OFF, id="dial-off"),
        pytest.param(_jailed, SUBJECT, False, dep.HOLD_JAILED, id="jailed"),
        pytest.param(_released, SUBJECT, False, None, id="released-jail"),
        pytest.param(_jailed_elsewhere, SUBJECT, False, None, id="jailed-in-other-guild"),
        pytest.param(_opened_ticket, SUBJECT, False, dep.HOLD_TICKET, id="opened-ticket"),
        pytest.param(_opened_closed_ticket, SUBJECT, False, dep.HOLD_TICKET, id="closed-ticket"),
        pytest.param(_opened_deleted_ticket, SUBJECT, False, None, id="archived-ticket"),
        pytest.param(_joined_ticket, SUBJECT, False, dep.HOLD_TICKET, id="joined-ticket"),
        pytest.param(_removed_from_ticket, SUBJECT, False, None, id="removed-from-ticket"),
    ],
)
def test_hold_reason(db, setup, user_id, is_bot, expected):
    with open_db(db) as conn:
        if setup:
            setup(conn)
        assert dep.hold_reason(conn, PRIMARY, user_id, is_bot=is_bot) == expected


def test_the_dial_defaults_off(tmp_path):
    path = migrated_db(tmp_path / "fresh.db")
    with open_db(path) as conn:
        assert dep.hold_reason(conn, PRIMARY, SUBJECT) == dep.HOLD_DIAL_OFF


# ── the queue ───────────────────────────────────────────────────────────────


def test_a_job_waits_out_the_settle_delay(db):
    with open_db(db) as conn:
        dep.enqueue(conn, PRIMARY, SUBJECT, now=NOW)
        assert dep.next_job(conn, now=NOW + dep.SETTLE_SECONDS - 1) is None
        job = dep.next_job(conn, now=NOW + dep.SETTLE_SECONDS)
    assert job == dep.Job(PRIMARY, SUBJECT, NOW, 0)


def test_leaving_again_resets_the_row(db):
    with open_db(db) as conn:
        dep.enqueue(conn, PRIMARY, SUBJECT, now=NOW)
        job = dep.next_job(conn, now=NOW + dep.SETTLE_SECONDS)
        for _ in range(dep.MAX_ATTEMPTS):
            dep.record_failure(conn, job, "boom", now=NOW)
            job = dep.Job(job.guild_id, job.user_id, job.left_at, job.attempts + 1)
        dep.enqueue(conn, PRIMARY, SUBJECT, now=NOW + 10)
        [row] = dep.list_jobs(conn, PRIMARY)
    assert (row["state"], row["attempts"], row["last_error"]) == ("pending", 0, "")
    assert row["next_at"] == NOW + 10 + dep.SETTLE_SECONDS


def test_failures_back_off_then_park(db):
    with open_db(db) as conn:
        dep.enqueue(conn, PRIMARY, SUBJECT, now=NOW)
        job = dep.Job(PRIMARY, SUBJECT, NOW, 0)
        states = []
        for attempt in range(dep.MAX_ATTEMPTS):
            states.append(dep.record_failure(conn, job, "x" * 500, now=NOW))
            [row] = dep.list_jobs(conn, PRIMARY)
            assert row["attempts"] == attempt + 1
            assert row["next_at"] == NOW + dep.RETRY_BASE_SECONDS * 2**attempt
            assert len(row["last_error"]) == 300
            job = dep.Job(PRIMARY, SUBJECT, NOW, attempt + 1)
        # A parked row is never picked again by itself.
        assert dep.next_job(conn, now=NOW * 100) is None
        assert dep.retry_job(conn, PRIMARY, SUBJECT, now=NOW) is True
        assert dep.next_job(conn, now=NOW) == dep.Job(PRIMARY, SUBJECT, NOW, 0)
        # Retry only revives a parked row.
        assert dep.retry_job(conn, PRIMARY, SUBJECT, now=NOW) is False
    assert states == ["pending"] * (dep.MAX_ATTEMPTS - 1) + ["failed"]


def test_cancel_reports_whether_there_was_a_row(db):
    with open_db(db) as conn:
        assert dep.cancel(conn, PRIMARY, SUBJECT) is False
        dep.enqueue(conn, PRIMARY, SUBJECT, now=NOW)
        assert dep.cancel(conn, PRIMARY, SUBJECT) is True
        assert dep.list_jobs(conn, PRIMARY) == []


def test_switching_off_clears_only_that_guilds_queue(db):
    with open_db(db) as conn:
        dep.enqueue(conn, PRIMARY, SUBJECT, now=NOW)
        dep.enqueue(conn, PRIMARY, OTHER, now=NOW)
        dep.enqueue(conn, SECOND, SUBJECT, now=NOW)
        assert dep.clear_queue(conn, PRIMARY) == 2
        assert dep.list_jobs(conn, PRIMARY) == []
        assert len(dep.list_jobs(conn, SECOND)) == 1


@pytest.mark.parametrize(
    ("guild", "elsewhere", "expected"),
    [
        (PRIMARY, False, dep.SCOPE_ALL),
        (PRIMARY, True, dep.SCOPE_GUILD),
        (SECOND, False, dep.SCOPE_GUILD),
        (SECOND, True, dep.SCOPE_GUILD),
    ],
)
def test_choose_scope(guild, elsewhere, expected):
    assert dep.choose_scope(guild, primary_guild_id=PRIMARY, present_elsewhere=elsewhere) == expected


# ── process_job ─────────────────────────────────────────────────────────────


class FakeDiscord:
    def __init__(
        self, *, members=(), failed=0, raises=None, on_delete=None, is_member_raises=None
    ):
        self.members = set(members)  # (guild_id, user_id)
        self.failed = failed
        self.raises = raises
        self.on_delete = on_delete
        self.is_member_raises = is_member_raises
        self.deleted: list[tuple[int, int]] = []
        self.posts_deleted: list[tuple[int, int, int]] = []

    async def delete_messages(self, guild_id, user_id):
        self.deleted.append((guild_id, user_id))
        if self.on_delete:
            self.on_delete()
        if self.raises:
            raise self.raises
        return self.failed

    async def is_member(self, guild_id, user_id):
        if self.is_member_raises:
            raise self.is_member_raises
        return (guild_id, user_id) in self.members

    async def delete_guess_post(self, guild_id, channel_id, message_id):
        self.posts_deleted.append((guild_id, channel_id, message_id))

    def hooks(self):
        return dep.DepartureHooks(
            delete_messages=self.delete_messages,
            is_member=self.is_member,
            guild_ids=lambda: [PRIMARY, SECOND],
            channel_ids=lambda guild_id: [500],
            delete_guess_post=self.delete_guess_post,
        )


def _queued(db, guild=PRIMARY):
    with open_db(db) as conn:
        dep.enqueue(conn, guild, SUBJECT, now=NOW)
    return dep.Job(guild, SUBJECT, NOW, 0)


async def _run(db, job, fake):
    return await dep.process_job(
        db, job, fake.hooks(), primary_guild_id=PRIMARY, actor_id=BOT_ID, clock=lambda: NOW
    )


def _ids_in(db, guild):
    with open_db(db) as conn:
        return {
            r[0]
            for r in conn.execute("SELECT user_id FROM known_users WHERE guild_id = ?", (guild,))
        }


async def test_happy_path_all_guilds(db, tmp_path):
    photo = tmp_path / "orig.jpg"
    photo.write_bytes(b"jpeg")
    with open_db(db) as conn:
        rid = insert_round(conn, guild_id=SECOND, submitter_id=SUBJECT, answer_id=SUBJECT, difficulty="easy", candidate_count=1)
        set_round_original_path(conn, rid, str(photo))
        update_round_message(conn, rid, message_id=77, crop_url="", crop_path="")
        conn.execute("UPDATE guess_rounds SET channel_id = 66 WHERE id = ?", (rid,))
    job = _queued(db)
    fake = FakeDiscord()

    outcome = await _run(db, job, fake)

    assert (outcome.status, outcome.detail) == ("done", dep.SCOPE_ALL)
    assert fake.deleted == [(PRIMARY, SUBJECT)]
    assert SUBJECT not in _ids_in(db, PRIMARY) | _ids_in(db, SECOND)
    assert not photo.exists()
    # All-guild scope reaches the round's post in the other guild too.
    assert fake.posts_deleted == [(SECOND, 66, 77)]
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM departure_erasure_queue").fetchone()[0] == 0
        # Deleted, not rewritten to the surrogate (2026-09-27): with its photo
        # and post gone, a kept round would be a hole, not a play record.
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds WHERE id = ?", (rid,)).fetchone()[0] == 0
        [audit] = conn.execute(
            "SELECT actor_id, target_id, extra FROM audit_log WHERE action = ?", (dep.AUDIT_ACTION,)
        ).fetchall()
    assert audit[0] == BOT_ID and audit[1] is None
    assert str(SUBJECT) not in audit[2]
    assert json.loads(audit[2])["scope"] == dep.SCOPE_ALL


async def test_an_open_break_is_deleted_not_rewritten_to_the_surrogate(db):
    """Anonymisation wins over a break: the saved roles go with the member
    rather than surviving under a surrogate id."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO break_holds (guild_id, user_id, stored_roles, started_at) "
            "VALUES (?, ?, '[1]', 0)",
            (PRIMARY, SUBJECT),
        )
    outcome = await _run(db, _queued(db), FakeDiscord())
    assert outcome.status == "done"
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM break_holds").fetchone()[0] == 0


async def test_still_in_another_guild_scopes_to_the_one_left(db, tmp_path):
    kept = tmp_path / "kept.jpg"
    kept.write_bytes(b"jpeg")
    with open_db(db) as conn:
        rid = insert_round(conn, guild_id=SECOND, submitter_id=SUBJECT, answer_id=SUBJECT, difficulty="easy", candidate_count=1)
        set_round_original_path(conn, rid, str(kept))
    job = _queued(db)

    outcome = await _run(db, job, FakeDiscord(members={(SECOND, SUBJECT)}))

    assert (outcome.status, outcome.detail) == ("done", dep.SCOPE_GUILD)
    assert SUBJECT not in _ids_in(db, PRIMARY)
    assert SUBJECT in _ids_in(db, SECOND)
    assert kept.exists()  # a photo in a guild they are still in stays


async def test_leaving_a_second_guild_never_goes_bot_wide(db):
    outcome = await _run(db, _queued(db, SECOND), FakeDiscord())
    assert (outcome.status, outcome.detail) == ("done", dep.SCOPE_GUILD)
    assert SUBJECT in _ids_in(db, PRIMARY)
    assert SUBJECT not in _ids_in(db, SECOND)


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param(FakeDiscord(failed=2), id="some-messages-undeleted"),
        pytest.param(FakeDiscord(raises=RuntimeError("rate limited")), id="deletion-raised"),
    ],
)
async def test_a_failed_deletion_anonymises_nothing(db, fake):
    outcome = await _run(db, _queued(db), fake)

    assert outcome.status == "retry"
    assert SUBJECT in _ids_in(db, PRIMARY)
    with open_db(db) as conn:
        [row] = dep.list_jobs(conn, PRIMARY)
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = ?", (dep.AUDIT_ACTION,)).fetchone()[0] == 0
    assert row["attempts"] == 1 and row["last_error"]
    assert row["next_at"] == NOW + dep.RETRY_BASE_SECONDS


async def test_a_presence_check_error_before_deletion_still_backs_off(db):
    """The very first ``_why_not`` (before any message is touched) must be
    covered by the same failure handling as the rest of the job.

    It used to sit outside ``process_job``'s ``try``, so a Discord error
    surfacing there (not just NotFound) propagated straight out of the
    function instead of landing on the row: no ``last_error``, no bumped
    ``attempts``, and next_at left unmoved — the exact same job would be
    picked up again on the very next tick forever, hammering the API with no
    backoff and no way to ever park as ``failed``.
    """
    fake = FakeDiscord(is_member_raises=RuntimeError("Discord 500"))

    outcome = await _run(db, _queued(db), fake)

    assert outcome.status == "retry"
    assert fake.deleted == []  # never got past the presence check
    assert SUBJECT in _ids_in(db, PRIMARY)
    with open_db(db) as conn:
        [row] = dep.list_jobs(conn, PRIMARY)
    assert row["attempts"] == 1 and "Discord 500" in row["last_error"]
    assert row["next_at"] == NOW + dep.RETRY_BASE_SECONDS


async def test_the_last_failed_attempt_parks_the_row(db):
    _queued(db)
    job = dep.Job(PRIMARY, SUBJECT, NOW, dep.MAX_ATTEMPTS - 1)
    outcome = await _run(db, job, FakeDiscord(failed=1))
    assert outcome.status == "failed"
    with open_db(db) as conn:
        assert dep.list_jobs(conn, PRIMARY)[0]["state"] == dep.STATE_FAILED


async def test_a_rejoined_member_is_dropped_before_any_deletion(db):
    fake = FakeDiscord(members={(PRIMARY, SUBJECT)})
    outcome = await _run(db, _queued(db), fake)
    assert (outcome.status, outcome.detail) == ("dropped", dep.DROP_REJOINED)
    assert fake.deleted == []
    assert SUBJECT in _ids_in(db, PRIMARY)
    with open_db(db) as conn:
        assert dep.list_jobs(conn, PRIMARY) == []


def _jail_now(db):
    def _do():
        with open_db(db) as conn:
            _jail(conn)
    return _do


def _dial_off_now(db):
    def _do():
        with open_db(db) as conn:
            _dial_off(conn)
    return _do


@pytest.mark.parametrize(
    ("make_side_effect", "reason"),
    [
        pytest.param(_jail_now, dep.HOLD_JAILED, id="jailed-during-the-scan"),
        pytest.param(_dial_off_now, dep.HOLD_DIAL_OFF, id="dial-turned-off-during-the-scan"),
    ],
)
async def test_a_change_during_the_scan_drops_the_job(db, make_side_effect, reason):
    outcome = await _run(db, _queued(db), FakeDiscord(on_delete=make_side_effect(db)))
    assert (outcome.status, outcome.detail) == ("dropped", reason)
    assert SUBJECT in _ids_in(db, PRIMARY)
    with open_db(db) as conn:
        assert dep.list_jobs(conn, PRIMARY) == []


async def test_a_member_jailed_while_queued_is_kept(db):
    job = _queued(db)
    with open_db(db) as conn:
        _jail(conn)
    fake = FakeDiscord()
    outcome = await _run(db, job, fake)
    assert (outcome.status, outcome.detail) == ("dropped", dep.HOLD_JAILED)
    assert fake.deleted == []


async def test_a_row_that_is_gone_does_nothing(db):
    fake = FakeDiscord()
    outcome = await _run(db, dep.Job(PRIMARY, SUBJECT, NOW, 0), fake)
    assert (outcome.status, outcome.detail) == ("dropped", dep.DROP_ROW_GONE)
    assert fake.deleted == []


async def test_an_undeletable_guess_original_retries_instead(db, tmp_path, monkeypatch):
    photo = tmp_path / "stuck.jpg"
    photo.write_bytes(b"jpeg")
    with open_db(db) as conn:
        rid = insert_round(conn, guild_id=PRIMARY, submitter_id=SUBJECT, answer_id=SUBJECT, difficulty="easy", candidate_count=1)
        set_round_original_path(conn, rid, str(photo))

    def _refuse(self, missing_ok=False):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", _refuse)
    outcome = await _run(db, _queued(db), FakeDiscord())
    assert outcome.status == "retry"
    assert SUBJECT in _ids_in(db, PRIMARY)
    with open_db(db) as conn:
        assert dep.list_jobs(conn, PRIMARY)[0]["attempts"] == 1


async def test_a_later_unlink_failure_keeps_the_earlier_path_cleared(db, tmp_path, monkeypatch):
    """A stored path never names a file that is already gone.

    The two deletions used to share the anonymise transaction, so the second
    one refusing rolled back the first one's cleared path — and the file it
    named had already been unlinked.
    """
    photos = []
    with open_db(db) as conn:
        for name in ("one.jpg", "two.jpg"):
            photo = tmp_path / name
            photo.write_bytes(b"jpeg")
            photos.append(photo)
            rid = insert_round(
                conn, guild_id=PRIMARY, submitter_id=SUBJECT, answer_id=SUBJECT,
                difficulty="easy", candidate_count=1,
            )
            set_round_original_path(conn, rid, str(photo))

    real_unlink = Path.unlink
    seen: list[str] = []

    def _refuse_the_second(self, missing_ok=False):
        seen.append(self.name)
        if len(seen) > 1:
            raise PermissionError("read-only")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _refuse_the_second)
    outcome = await _run(db, _queued(db), FakeDiscord())

    assert outcome.status == "retry"
    assert SUBJECT in _ids_in(db, PRIMARY)  # the anonymise never ran
    assert len([p for p in photos if not p.exists()]) == 1
    with open_db(db) as conn:
        stored = [r[0] for r in conn.execute("SELECT original_path FROM guess_rounds")]
    # Whichever one went, its row was cleared and stayed cleared: the only path
    # still stored names a file that is still on disk.
    remaining = [p for p in stored if p]
    assert len(remaining) == 1 and Path(remaining[0]).exists()
