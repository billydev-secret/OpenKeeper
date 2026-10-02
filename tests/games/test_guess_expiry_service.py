"""Guess Who round lifetime: which rounds expire, that an expiry is never a
solve, and that the card clean-up deletes, retries and reveals nothing.

The leaderboard assertion is the load-bearing one — commit 6edc1b55 recorded
that stamping ``solved_at`` on a timed-out round would count it as a solve in
the top-posters query. ``test_expired_round_is_not_a_solve`` fails if the
expiry is ever moved onto that column.
"""
from __future__ import annotations

from pathlib import Path

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.games.guess.expiry_service import (
    claim_expired_rounds,
    clear_expired_round,
    expiry_cutoff,
    pending_expired_rounds,
)
from bot_modules.games.guess.repo import (
    get_guess_config,
    get_round,
    get_top_guessers,
    get_top_posters,
    get_unsolved_round_ids,
    insert_round,
    list_stale_open_originals,
    mark_round_solved,
    set_guess_config_value,
    set_round_answer_optout,
    soft_delete_round,
)

GUILD = 9001
DAY = 86400
NOW = 100 * DAY


def _round(conn, *, age_days: float, message_id: int = 5, **kw) -> int:
    rid = insert_round(
        conn, guild_id=kw.pop("guild_id", GUILD), submitter_id=kw.pop("submitter", 10),
        answer_id=kw.pop("answer", 10), message_id=message_id,
    )
    conn.execute(
        "UPDATE guess_rounds SET created_at = ? WHERE id = ?",
        (NOW - age_days * DAY, rid),
    )
    return rid


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, None), (-3, None), (7, NOW - 7 * DAY)],
)
def test_expiry_cutoff(days, expected):
    assert expiry_cutoff(days, now=NOW) == expected


def test_dial_ships_dark(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        assert get_guess_config(conn, GUILD).round_lifetime_days == 0
        set_guess_config_value(conn, GUILD, "guess_round_lifetime_days", "7")
        assert get_guess_config(conn, GUILD).round_lifetime_days == 7


def test_dial_zero_expires_nothing(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = _round(conn, age_days=400)
    assert claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=0, now=NOW) == []
    with open_db(sync_db_path) as conn:
        assert get_round(conn, rid).expired_at is None


def test_only_old_open_rounds_in_this_guild_are_claimed(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        old = _round(conn, age_days=8)
        young = _round(conn, age_days=6)
        solved = _round(conn, age_days=20)
        mark_round_solved(conn, solved, solver_id=5, guesses_to_solve=1, unique_guessers_to_solve=1)
        deleted = _round(conn, age_days=20)
        soft_delete_round(conn, deleted)
        optout = _round(conn, age_days=20)
        set_round_answer_optout(conn, optout)
        unposted = _round(conn, age_days=20, message_id=0)
        elsewhere = _round(conn, age_days=20, guild_id=GUILD + 1)

    claimed = claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=7, now=NOW)

    assert [r.id for r in claimed] == [old]
    with open_db(sync_db_path) as conn:
        assert get_round(conn, old).expired_at == NOW
        for rid in (young, solved, deleted, optout, unposted, elsewhere):
            assert get_round(conn, rid).expired_at is None


def test_claim_is_idempotent(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _round(conn, age_days=8)
    assert len(claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=7, now=NOW)) == 1
    assert claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=7, now=NOW + 1) == []


def test_expired_round_is_not_a_solve(sync_db_path: Path):
    """No leaderboard credit for anyone: solved_at and solver_id stay NULL."""
    with open_db(sync_db_path) as conn:
        rid = _round(conn, age_days=8, submitter=10, answer=10)
        real = _round(conn, age_days=1, submitter=10, answer=10)
        mark_round_solved(conn, real, solver_id=77, guesses_to_solve=1, unique_guessers_to_solve=1)
    claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=7, now=NOW)
    with open_db(sync_db_path) as conn:
        row = get_round(conn, rid)
        assert row.solved_at is None and row.solver_id is None
        assert get_top_posters(conn, GUILD) == [(10, 2, 1)]  # posted 2, solved 1 (the real one)
        assert get_top_guessers(conn, GUILD) == [(77, 1)]


def test_expired_round_cannot_be_solved_afterwards_and_leaves_open_lists(sync_db_path: Path):
    """A correct guess racing the expiry loses, and once its card is cleared
    the round stops being re-registered as a live view at restart."""
    with open_db(sync_db_path) as conn:
        rid = _round(conn, age_days=8)
        assert get_unsolved_round_ids(conn) == [rid]
    claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=7, now=NOW)
    with open_db(sync_db_path) as conn:
        assert mark_round_solved(
            conn, rid, solver_id=5, guesses_to_solve=1, unique_guessers_to_solve=1
        ) == 0
        # Card not deleted yet: the button must keep answering "ended".
        assert get_unsolved_round_ids(conn) == [rid]
        conn.execute("UPDATE guess_rounds SET expiry_cleared_at = 1 WHERE id = ?", (rid,))
        assert get_unsolved_round_ids(conn) == []


def test_expired_round_with_a_surviving_original_still_ages_out(sync_db_path: Path):
    """If the clean-up fails after the claim the original is still on disk; the
    7-day age-out must keep seeing it or the photo is never deleted."""
    with open_db(sync_db_path) as conn:
        rid = _round(conn, age_days=8)
        conn.execute("UPDATE guess_rounds SET original_path = 'x/1.jpg' WHERE id = ?", (rid,))
    claim_expired_rounds(sync_db_path, guild_id=GUILD, lifetime_days=7, now=NOW)
    with open_db(sync_db_path) as conn:
        assert list_stale_open_originals(conn, older_than=NOW) == [(rid, "x/1.jpg")]


class _Resp:
    status = 500
    reason = "boom"


def _deleter(calls: list[int], exc: Exception | None = None):
    async def delete() -> None:
        calls.append(1)
        if exc is not None:
            raise exc
    return delete


def _expire_one(db: Path, *, path: str = "") -> int:
    with open_db(db) as conn:
        rid = _round(conn, age_days=8)
        if path:
            conn.execute("UPDATE guess_rounds SET original_path = ? WHERE id = ?", (path, rid))
    claim_expired_rounds(db, guild_id=GUILD, lifetime_days=7, now=NOW)
    return rid


@pytest.mark.asyncio
async def test_clear_deletes_card_and_original_and_stamps(sync_db_path: Path, tmp_path: Path):
    orig = tmp_path / "1.jpg"
    orig.write_bytes(b"x")
    rid = _expire_one(sync_db_path, path=str(orig))
    [row] = pending_expired_rounds(sync_db_path, guild_id=GUILD)
    calls: list[int] = []

    assert await clear_expired_round(sync_db_path, row, _deleter(calls), now=NOW + 5)

    assert calls == [1] and not orig.exists()
    with open_db(sync_db_path) as conn:
        r = get_round(conn, rid)
        assert r.expiry_cleared_at == NOW + 5 and r.original_path == ""
        assert r.solved_at is None and r.solver_id is None
        assert get_top_posters(conn, GUILD) == [(10, 1, 0)]
    assert pending_expired_rounds(sync_db_path, guild_id=GUILD) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "cleared"),
    [
        pytest.param(discord.NotFound(_Resp(), "gone"), True, id="not-found-counts-as-done"),
        pytest.param(discord.HTTPException(_Resp(), "boom"), False, id="discord-error-retries"),
    ],
)
async def test_failed_delete_is_retried_on_next_sweep(sync_db_path: Path, exc, cleared):
    orig_dir = sync_db_path.parent
    f = orig_dir / "o.jpg"
    f.write_bytes(b"x")
    _expire_one(sync_db_path, path=str(f))
    [row] = pending_expired_rounds(sync_db_path, guild_id=GUILD)

    assert await clear_expired_round(sync_db_path, row, _deleter([], exc)) is cleared

    assert not f.exists()  # the photo goes either way
    assert bool(pending_expired_rounds(sync_db_path, guild_id=GUILD)) is (not cleared)
    if not cleared:
        # Next sweep: still pending, and a working delete finishes it.
        [again] = pending_expired_rounds(sync_db_path, guild_id=GUILD)
        calls: list[int] = []
        assert await clear_expired_round(sync_db_path, again, _deleter(calls))
        assert calls == [1]
        assert pending_expired_rounds(sync_db_path, guild_id=GUILD) == []


@pytest.mark.asyncio
async def test_missing_channel_counts_as_cleared(sync_db_path: Path):
    _expire_one(sync_db_path)
    [row] = pending_expired_rounds(sync_db_path, guild_id=GUILD)
    assert await clear_expired_round(sync_db_path, row, None)
    assert pending_expired_rounds(sync_db_path, guild_id=GUILD) == []


def test_nothing_in_the_module_reveals_the_answer():
    """An expired round shows nothing: the reveal builder is gone."""
    from bot_modules.games.guess import layouts

    assert not hasattr(layouts, "build_expired_layout")
