"""Fantasies & Dealbreakers' per-entry ``advance`` closure.

Each entry in a round gets its own ``FantasiesVoteView`` and its own
``advance`` callback, handed to that view as ``advance_callback`` — a
``discord.ui.View`` with ``timeout=None``, so the callback stays reachable
for as long as the message's buttons are (a stale click on a board whose
disabling edit silently 404'd, say), not just for the one call the round
loop's own timer/Next/vote-complete path makes.

Before the loop bound ``view`` as a default argument, ``advance`` read
``view`` as a free variable off the enclosing ``for`` loop — so a second,
stale call through *entry 1's* callback, made after the loop had already
opened entry 2, resolved ``view`` to entry 2's brand-new view: it would
mark entry 2 closed, persist a result blending entry 1's text with
whatever entry 2's live vote tallies happened to be, and wake the round
loop's wait for entry 2 before anyone had voted on it. This pins the fix:
a stale call through an old entry's callback must be a no-op against the
*next* entry's still-open view.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_modules.games.fantasies.cog import FantasiesCog, FantasiesVoteView
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.db import GamesDb

GUILD = 4242
CHAN = 900
HOST = 1
ENTRY_1_AUTHOR = 101
ENTRY_2_AUTHOR = 102


def _channel():
    async def _send(*_args, **_kwargs):
        return SimpleNamespace(edit=AsyncMock())

    return SimpleNamespace(id=CHAN, name="games", guild=None, send=AsyncMock(side_effect=_send))


async def _wait_until(predicate, *, tries=300, interval=0.02) -> None:
    """Poll for *predicate*, giving the background round-loop task real
    wall-clock time to reach it — including past its ``asyncio.sleep(1)``
    between entries."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


async def test_stale_advance_from_a_prior_entry_leaves_the_next_entry_alone(
    sync_db_path, monkeypatch
):
    # Skip the submissions window entirely — entries are already in the
    # payload below, and this bug lives in the per-entry vote loop that
    # runs after submissions close.
    monkeypatch.setattr(
        "bot_modules.games.fantasies.cog.SubmitRoundView.wait",
        AsyncMock(return_value=None),
    )

    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path),
        active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = FantasiesCog(bot)  # type: ignore[arg-type]

    payload = {
        "rounds": {
            "1": {
                "entries": [
                    {"user_id": ENTRY_1_AUTHOR, "text": "Entry one", "category": "Fantasy"},
                    {"user_id": ENTRY_2_AUTHOR, "text": "Entry two", "category": "Fantasy"},
                ],
            },
        },
        "results": [],
        "round_seconds": 0,  # host-paced: no timer task racing the test
    }
    game_id = await create_game(
        cog.db, CHAN, HOST, "fantasies", state="playing", guild_id=GUILD, payload=payload,
    )
    # Anything truthy: _run_round bails out early if the game_id has been
    # removed from active_views (an end-game race), and the real value
    # (a FantasiesMainView) is irrelevant to this test.
    bot.active_views[game_id] = SimpleNamespace()
    channel = _channel()

    task = asyncio.create_task(
        cog._run_round(game_id, HOST, "Host", 1, channel, main_view=None)
    )
    try:
        await _wait_until(
            lambda: isinstance(bot.active_views.get(game_id), FantasiesVoteView)
        )
        view1 = bot.active_views[game_id]
        advance1 = view1.advance_callback
        assert view1.entry_text == "Entry one"

        # Entry 1's real close — the timer, Next, or a complete vote.
        await advance1(view1.message)

        await _wait_until(
            lambda: isinstance(bot.active_views.get(game_id), FantasiesVoteView)
            and bot.active_views[game_id] is not view1
        )
        view2 = bot.active_views[game_id]
        assert view2.entry_text == "Entry two"
        assert view2._closed is False

        # A stale second call through entry 1's own callback: e.g. a Next
        # click on a board whose disabling edit silently failed and so
        # stayed clickable.
        await advance1(SimpleNamespace(edit=AsyncMock()))

        # Entry 2 must be untouched: still open, still the live view.
        assert view2._closed is False
        assert bot.active_views[game_id] is view2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
