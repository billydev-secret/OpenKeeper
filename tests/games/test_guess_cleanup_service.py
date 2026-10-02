# tests/games/test_guess_cleanup_service.py
"""Tier 1 unit tests: the Guess cache housekeeping service.

Four jobs live here and all four end in a member's full-size photo leaving
the disk: the rollback of a round whose channel post never landed (bug #227),
the age-out of originals for rounds nobody solves, the orphan sweep for
files no round points at any more, and the member-scoped clear every Guess
erasure runs first — which also takes the round's post out of Discord.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.games.guess.cleanup_service import (
    ORPHAN_GRACE_SECONDS,
    MediaNotCleared,
    age_out_originals,
    clear_member_media,
    delete_member_originals,
    guess_orig_dir,
    rollback_unposted_round,
    sweep_orphan_originals,
    withdraw_departed_member,
)
from bot_modules.games.guess.repo import (
    get_guess_consent,
    get_round,
    insert_round,
    list_audit_events,
    mark_round_solved,
    record_guess_consent,
    set_round_original_path,
)

GUILD = 9001
USER_A = 1001


def _round(conn, **overrides) -> int:
    kwargs = dict(
        guild_id=GUILD,
        submitter_id=USER_A,
        answer_id=USER_A,
        difficulty="easy",
        candidate_count=1,
    )
    kwargs.update(overrides)
    return insert_round(conn, **kwargs)


# ── rollback_unposted_round ──────────────────────────────────────────────────

def test_rollback_deletes_original_soft_deletes_round_and_audits(
    sync_db_path: Path, tmp_path: Path
):
    """The bug #227 path: the row and the full-size original are both already
    durable when the channel post raises. Rolling back must leave neither."""
    orig = tmp_path / "77.jpg"
    orig.write_bytes(b"a-members-photo")
    with open_db(sync_db_path) as conn:
        round_id = _round(conn)
        set_round_original_path(conn, round_id, str(orig))

    rollback_unposted_round(
        sync_db_path, round_id, guild_id=GUILD, actor_id=USER_A
    )

    assert not orig.exists()
    with open_db(sync_db_path) as conn:
        row = get_round(conn, round_id)
        assert row is not None
        assert row.deleted_at is not None
        assert row.original_path == ""
        events = list_audit_events(conn, GUILD)
    assert [e.action for e in events] == ["post_failed"]
    assert events[0].round_id == round_id


def test_rollback_without_an_original_still_soft_deletes(sync_db_path: Path):
    """The confession path writes no file — the orphaned row is the whole
    problem there, and it still has to go."""
    with open_db(sync_db_path) as conn:
        round_id = _round(conn, round_type="confession", confession_text="hi")

    rollback_unposted_round(
        sync_db_path, round_id, guild_id=GUILD, actor_id=USER_A
    )

    with open_db(sync_db_path) as conn:
        row = get_round(conn, round_id)
    assert row is not None and row.deleted_at is not None


def test_rollback_tolerates_an_already_missing_file(sync_db_path: Path, tmp_path: Path):
    """A recorded path whose file never got written (the post raised between
    the two) must not stop the row being cleaned up."""
    with open_db(sync_db_path) as conn:
        round_id = _round(conn)
        set_round_original_path(conn, round_id, str(tmp_path / "never-written.jpg"))

    rollback_unposted_round(
        sync_db_path, round_id, guild_id=GUILD, actor_id=USER_A
    )

    with open_db(sync_db_path) as conn:
        row = get_round(conn, round_id)
    assert row is not None and row.deleted_at is not None and row.original_path == ""


# ── age_out_originals (moved here from the cog) ──────────────────────────────

def test_age_out_deletes_files_and_clears_paths(sync_db_path: Path, tmp_path: Path):
    """File gone + reference cleared; a missing file (pre-rename ``veil_cache``
    path) still gets its reference cleared."""
    real = tmp_path / "orig.jpg"
    real.write_bytes(b"x")
    with open_db(sync_db_path) as conn:
        r1 = _round(conn)
        set_round_original_path(conn, r1, str(real))
        r2 = _round(conn)
        set_round_original_path(conn, r2, str(tmp_path / "already-gone.jpg"))
        conn.execute("UPDATE guess_rounds SET created_at = 100.0 WHERE id IN (?, ?)", (r1, r2))
        young = _round(conn)
        set_round_original_path(conn, young, "guess_cache/orig/young.jpg")
        conn.execute(
            "UPDATE guess_rounds SET created_at = ? WHERE id = ?",
            (100.0 + 2 * 86400, young),
        )

    # Eight days after r1/r2, six after ``young``: the cut is 7 days, so the
    # old pair goes and the young one stays. A revert to 90 keeps all three.
    cleared = age_out_originals(sync_db_path, now=100.0 + 8 * 86400)

    assert cleared == 2
    assert not real.exists()
    with open_db(sync_db_path) as conn:
        paths = [
            r["original_path"]
            for r in conn.execute(
                "SELECT original_path FROM guess_rounds WHERE id IN (?, ?)", (r1, r2)
            )
        ]
        young_path = conn.execute(
            "SELECT original_path FROM guess_rounds WHERE id = ?", (young,)
        ).fetchone()["original_path"]
    assert paths == ["", ""]
    assert young_path == "guess_cache/orig/young.jpg"


# ── sweep_orphan_originals ───────────────────────────────────────────────────

def _age(path: Path, seconds: float, *, now: float) -> None:
    import os
    os.utime(path, (now - seconds, now - seconds))


@pytest.mark.parametrize("layout", ["flat", "var"])
def test_originals_dir_sits_beside_the_db(tmp_path: Path, layout: str):
    """Resolved against the DB's parent, not the cwd: ``guess_cache/orig`` there,
    or ``var/guess_cache/orig`` once ``var/`` exists (core.paths.data_dir). A
    relative DB path keeps the recorded path relative, as production records it."""
    expected = tmp_path / "guess_cache" / "orig"
    if layout == "var":
        (tmp_path / "var").mkdir()
        expected = tmp_path / "var" / "guess_cache" / "orig"
    assert guess_orig_dir(tmp_path / "bot.db") == expected
    assert not expected.exists()  # the submit path creates it, not the lookup


def test_a_relative_db_path_records_a_relative_original(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert guess_orig_dir(Path("dungeonkeeper.db")) == Path("guess_cache") / "orig"


def test_sweep_deletes_a_file_no_round_points_at(sync_db_path: Path, tmp_path: Path):
    """Prod rounds 355/379/414: the rows were deleted outside the bot, so the
    age-out — which reads the rows — could never find the photos they left."""
    now = 1_000_000.0
    orphan = tmp_path / "355.jpg"
    orphan.write_bytes(b"orphaned-photo")
    _age(orphan, ORPHAN_GRACE_SECONDS + 60, now=now)

    swept = sweep_orphan_originals(sync_db_path, tmp_path, now=now)

    assert swept == 1
    assert not orphan.exists()


def test_sweep_keeps_a_file_a_round_still_records(sync_db_path: Path, tmp_path: Path):
    now = 1_000_000.0
    kept = tmp_path / "500.jpg"
    kept.write_bytes(b"live-round-photo")
    _age(kept, ORPHAN_GRACE_SECONDS + 60, now=now)
    with open_db(sync_db_path) as conn:
        round_id = _round(conn)
        set_round_original_path(conn, round_id, str(kept))

    assert sweep_orphan_originals(sync_db_path, tmp_path, now=now) == 0
    assert kept.exists()


def test_sweep_keeps_a_file_still_inside_the_grace_window(
    sync_db_path: Path, tmp_path: Path
):
    """The window the sweep must not race: a submit writes the photo seconds
    before the round records its path. Freshly-written files are off limits."""
    now = 1_000_000.0
    mid_post = tmp_path / "600.jpg"
    mid_post.write_bytes(b"being-posted-right-now")
    _age(mid_post, 5, now=now)

    assert sweep_orphan_originals(sync_db_path, tmp_path, now=now) == 0
    assert mid_post.exists()


def test_sweep_matches_a_recorded_path_by_name_too(sync_db_path: Path, tmp_path: Path):
    """Recorded paths are relative (``guess_cache/orig/443.jpg``) and resolve
    against the process cwd. If that ever differs from the directory being
    swept, name matching is what stops the sweep deleting live photos."""
    now = 1_000_000.0
    kept = tmp_path / "443.jpg"
    kept.write_bytes(b"live-round-photo")
    _age(kept, ORPHAN_GRACE_SECONDS + 60, now=now)
    with open_db(sync_db_path) as conn:
        round_id = _round(conn)
        set_round_original_path(conn, round_id, "guess_cache/orig/443.jpg")

    assert sweep_orphan_originals(sync_db_path, tmp_path, now=now) == 0
    assert kept.exists()


def test_sweep_ignores_subdirectories_and_a_missing_dir(
    sync_db_path: Path, tmp_path: Path
):
    now = 1_000_000.0
    (tmp_path / "nested").mkdir()
    assert sweep_orphan_originals(sync_db_path, tmp_path, now=now) == 0
    assert (tmp_path / "nested").exists()
    assert sweep_orphan_originals(sync_db_path, tmp_path / "absent", now=now) == 0


# ── clear_member_media: every Guess erasure, before the rows go ────────────


def _posted(conn, *, photo: Path, guild=GUILD, channel=5, message=50, **overrides) -> int:
    rid = _round(conn, guild_id=guild, **overrides)
    set_round_original_path(conn, rid, str(photo))
    conn.execute(
        "UPDATE guess_rounds SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel, message, rid),
    )
    return rid


async def test_clear_member_media_takes_the_post_and_the_photo(
    sync_db_path: Path, tmp_path: Path
):
    """Billy, 2026-09-27: "Deleting the message should clear everything from
    guess" — the crop post in the channel as well as the file on disk."""
    photo = tmp_path / "1.jpg"
    photo.write_bytes(b"jpeg")
    elsewhere = tmp_path / "2.jpg"
    elsewhere.write_bytes(b"jpeg")
    with open_db(sync_db_path) as conn:
        rid = _posted(conn, photo=photo)
        _posted(conn, photo=elsewhere, guild=GUILD + 1, channel=6, message=60)
    deleted: list[tuple[int, int, int]] = []

    async def _delete(guild_id, channel_id, message_id):
        deleted.append((guild_id, channel_id, message_id))

    await clear_member_media(sync_db_path, USER_A, guild_id=GUILD, delete_post=_delete)

    assert deleted == [(GUILD, 5, 50)]
    assert not photo.exists()
    assert elsewhere.exists(), "one guild's erasure never reaches another's"
    with open_db(sync_db_path) as conn:
        round_ = get_round(conn, rid)
    # The row stays — it is the caller's to delete, after this.
    assert round_ is not None and round_.original_path == ""


async def test_a_post_that_will_not_go_leaves_the_photo_for_a_retry(
    sync_db_path: Path, tmp_path: Path
):
    """Posts first, and a failure stops everything after it: the file and its
    recorded path are still there, so a retry finds the whole round."""
    photo = tmp_path / "1.jpg"
    photo.write_bytes(b"jpeg")
    with open_db(sync_db_path) as conn:
        _posted(conn, photo=photo)

    async def _refuse(guild_id, channel_id, message_id):
        raise RuntimeError("discord is down")

    with pytest.raises(RuntimeError):
        await clear_member_media(sync_db_path, USER_A, guild_id=GUILD, delete_post=_refuse)
    assert photo.exists()


def test_an_undeletable_original_raises_rather_than_reporting_success(
    sync_db_path: Path, tmp_path: Path, monkeypatch
):
    photo = tmp_path / "1.jpg"
    photo.write_bytes(b"jpeg")
    with open_db(sync_db_path) as conn:
        _round_id = _posted(conn, photo=photo)

    def _refuse(self, missing_ok=False):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", _refuse)
    with pytest.raises(MediaNotCleared):
        delete_member_originals(sync_db_path, USER_A, guild_id=GUILD)
    with open_db(sync_db_path) as conn:
        assert get_round(conn, _round_id).original_path == str(photo)


# ── withdraw_departed_member ─────────────────────────────────────────────────

def test_departure_flags_open_rounds_withdraws_consent_and_deletes_originals(
    sync_db_path: Path, tmp_path: Path
):
    """A leaver's open round turns unsolvable, their consent is stamped
    withdrawn (not deleted) and the cached original goes. Another member's
    round, a solved one and another guild's are untouched."""
    mine = tmp_path / "1.jpg"
    other = tmp_path / "2.jpg"
    mine.write_bytes(b"leaver")
    other.write_bytes(b"stays")
    with open_db(sync_db_path) as conn:
        open_id = _round(conn)
        set_round_original_path(conn, open_id, str(mine))
        solved_id = _round(conn)
        mark_round_solved(
            conn, solved_id, solver_id=5, guesses_to_solve=1, unique_guessers_to_solve=1
        )
        elsewhere_id = _round(conn, guild_id=GUILD + 1)
        other_id = _round(conn, submitter_id=2002, answer_id=2002)
        set_round_original_path(conn, other_id, str(other))
        record_guess_consent(conn, guild_id=GUILD, user_id=USER_A, consented_at=1.0)

    flagged, deleted = withdraw_departed_member(
        sync_db_path, guild_id=GUILD, user_id=USER_A, when=50.0
    )

    assert (flagged, deleted) == (1, 1)
    assert not mine.exists() and other.exists()
    with open_db(sync_db_path) as conn:
        assert get_round(conn, open_id).answer_optout is True
        assert get_round(conn, solved_id).answer_optout is False
        assert get_round(conn, elsewhere_id).answer_optout is False
        assert get_round(conn, other_id).answer_optout is False
        consent = get_guess_consent(conn, guild_id=GUILD, user_id=USER_A)
        assert consent is not None and consent["withdrawn_at"] == 50.0


def test_departure_with_nothing_to_close_is_a_no_op(sync_db_path: Path):
    assert withdraw_departed_member(
        sync_db_path, guild_id=GUILD, user_id=USER_A, when=1.0
    ) == (0, 0)


def test_departure_still_flags_when_an_original_cannot_be_removed(
    sync_db_path: Path, tmp_path: Path, monkeypatch
):
    """One stuck file must not stop the flag or the withdrawal."""
    orig = tmp_path / "3.jpg"
    orig.write_bytes(b"x")
    with open_db(sync_db_path) as conn:
        round_id = _round(conn)
        set_round_original_path(conn, round_id, str(orig))

    def _boom(*_a, **_k):
        raise MediaNotCleared("nope")

    monkeypatch.setattr(
        "bot_modules.games.guess.cleanup_service.delete_member_originals", _boom
    )
    assert withdraw_departed_member(
        sync_db_path, guild_id=GUILD, user_id=USER_A, when=1.0
    ) == (1, 0)
    with open_db(sync_db_path) as conn:
        assert get_round(conn, round_id).answer_optout is True
