"""A round's own advance callback firing twice must not touch a later round.

Same shape as Hot Takes' stale-advance bug (see
``tests/games/test_games_hottakes_stale_advance.py``): ``_run_guessing`` built
``advance`` as a closure reading the loop's ``view``/``advanced`` names
rather than binding *its own* round's view and Event. A stale second call —
a duplicate click, or a late-delivered interaction on a round that already
closed — read whichever ``view``/``advanced`` the loop held at call time.
Once the loop had moved to the next subject's round, a stale call would
prematurely close, mis-score and re-edit the round genuinely in progress,
and wake its vote-wait early (B023).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_modules.games.ttl.cog import TTLCog
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.db import GamesDb

GUILD = 9002
CHAN = 501
HOST = 1

SUBJECT_A = "201"
SUBJECT_B = "202"

SUBMISSIONS = {
    SUBJECT_A: {"statements": ["a1", "a2", "a3"], "lie": 0},
    SUBJECT_B: {"statements": ["b1", "b2", "b3"], "lie": 1},
}


def _channel():
    sent: list[SimpleNamespace] = []

    async def _send(*_a, **_kw):
        msg = SimpleNamespace(id=len(sent) + 1, edit=AsyncMock())
        sent.append(msg)
        return msg

    chan = SimpleNamespace(
        id=CHAN, name="games", guild=None, send=AsyncMock(side_effect=_send),
    )
    chan.sent = sent
    return chan


async def _wait_until(predicate, *, timeout: float = 4.0, interval: float = 0.01) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


async def test_a_stale_round_advance_does_not_corrupt_the_next_round(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = SimpleNamespace(
        games_db=db, active_views={}, ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = TTLCog(bot)  # type: ignore[arg-type]
    channel = _channel()

    gid = await create_game(
        db, CHAN, HOST, "ttl", state="guessing", guild_id=GUILD,
        payload={
            "submissions": SUBMISSIONS, "submission_count": 2, "submitter_names": {},
            "scores": {}, "prompt": None, "vote_timer": 0,
        },
    )
    # A placeholder so the loop's "did the game get cancelled" guard passes
    # before the first real view is built.
    bot.active_views[gid] = object()

    task = asyncio.create_task(cog._run_guessing(
        interaction=None, game_id=gid, host_id=HOST, host_name="Host",
        channel=channel,
    ))
    try:
        await _wait_until(
            lambda: getattr(bot.active_views.get(gid), "subject_id", None) == int(SUBJECT_A)
        )
        view_one = bot.active_views[gid]
        stale_advance = view_one.advance_callback
        round_one_msg = channel.sent[-1]

        # Close round one for real.
        await stale_advance(round_one_msg)
        assert view_one._closed is True

        await _wait_until(
            lambda: getattr(bot.active_views.get(gid), "subject_id", None) == int(SUBJECT_B)
        )
        view_two = bot.active_views[gid]
        assert view_two is not view_one

        # A stale second call — round one's own callback firing again, as a
        # duplicate or late-delivered click would — must be a no-op now that
        # the game has moved on to round two.
        await stale_advance(round_one_msg)

        assert view_two._closed is False, "the stale call from round one closed round two"
        assert not view_two._advanced_event.is_set(), (
            "the stale call woke round two's vote wait early"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
