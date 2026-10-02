"""A take's own advance callback firing twice must not touch a later take.

``_run_voting`` used to build ``advance`` as a closure that read the loop's
``view`` variable by name rather than binding *its own* take's view. Under
normal play that's invisible — the take that opened the closure is always
the one that closes it before the loop can move on — but a **stale** second
call (a duplicate click racing the natural close, or a late re-delivered
interaction on an already-retired take's board) read whichever ``view`` the
loop held *at call time*: once the loop had moved on to the next take, that
was the *new* take's view, not the one the stale call actually belonged to.
The stale call would then prematurely close, mis-tally and re-edit the take
that was still genuinely in progress (B023).

This reproduces it at the cog's own loop rather than through a Discord
button mock, per CLAUDE.md's "reproduce the state that broke" — the bug is
in the loop's own closure wiring, so the loop is what has to run.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_modules.games.hottakes.cog import HotTakesCog
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.db import GamesDb

GUILD = 9001
CHAN = 500
HOST = 1

TAKES = [
    {"text": "take one", "user_id": 11},
    {"text": "take two", "user_id": 12},
]


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


async def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.01) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


async def test_a_stale_take_advance_does_not_corrupt_the_next_take(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = SimpleNamespace(
        games_db=db, active_views={}, ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = HotTakesCog(bot)  # type: ignore[arg-type]
    channel = _channel()

    gid = await create_game(
        db, CHAN, HOST, "hottakes", state="playing", guild_id=GUILD,
        payload={"takes": TAKES, "results": [], "round_seconds": 0},
    )
    # A placeholder so the loop's "did the game get cancelled" guard passes
    # before the first real view is built.
    bot.active_views[gid] = object()

    task = asyncio.create_task(cog._run_voting(
        interaction=None, game_id=gid, host_id=HOST, host_name="Host",
        channel=channel,
    ))
    try:
        await _wait_until(lambda: getattr(bot.active_views.get(gid), "take_num", None) == 1)
        view_one = bot.active_views[gid]
        stale_advance = view_one.advance_callback
        take_one_msg = channel.sent[-1]

        # Close take one for real.
        await stale_advance(take_one_msg)
        assert view_one._closed is True

        await _wait_until(lambda: getattr(bot.active_views.get(gid), "take_num", None) == 2)
        view_two = bot.active_views[gid]
        assert view_two is not view_one

        # A stale second call — take one's own callback firing again, as a
        # duplicate or late-delivered click would — must be a no-op now that
        # the game has moved on to take two.
        await stale_advance(take_one_msg)

        assert view_two._closed is False, "the stale call from take one closed take two"
        assert not view_two.pacing.advanced.is_set(), (
            "the stale call woke take two's timer/vote wait early"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
