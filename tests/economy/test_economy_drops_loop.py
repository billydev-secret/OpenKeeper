"""The coin-drop poster gives up on a channel it can't post in.

A drop channel the bot has lost Send Messages in used to cost a drop row, a
doomed request and a traceback every minute, for good. The DB and Discord
are stubbed at the loop's own seams: this is about the loop's decisions.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.background import FailureStreak
from bot_modules.economy import drops_loop as loop
from bot_modules.economy.service import EconSettings

GID, CID, BOT_ID, HUMAN = 100, 200, 999, 1
NOW = 1_800_000_000.0
SETTINGS = replace(
    EconSettings(),
    enabled=True,
    drops_channel_id=CID,
    drops_per_day=4,
    drops_min_coins=5,
    drops_max_coins=10,
)


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeChannel:
    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self) -> None:
        self.id = CID
        self.name = "general"
        self.send = AsyncMock(return_value=SimpleNamespace(id=777))
        self.history_fails = False

    def history(self, limit: int = 1):
        if self.history_fails:
            raise _forbidden()

        async def _aiter():
            yield SimpleNamespace(author=SimpleNamespace(id=HUMAN))

        return _aiter()


def _forbidden() -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "no")


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(
        loop,
        "_failures",
        FailureStreak(
            "coin drop",
            threshold=loop.MAX_SEND_FAILURES,
            retry_after=loop.PAUSED_PROBE_SECONDS,
            clock=clock,
        ),
    )
    return clock


@pytest.fixture
def world(monkeypatch, clock):
    """A guild whose faucet is live and whose next drop is always due."""
    channel = FakeChannel()
    channels = {CID: channel}
    guild = SimpleNamespace(id=GID, get_channel=channels.get)
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    created: list[int] = []
    discarded: list[int] = []

    def _create(*_a, **_k) -> int:
        created.append(len(created) + 1)
        return created[-1]

    monkeypatch.setattr(loop, "_guild_state_sync", lambda *_a: (SETTINGS, False))
    monkeypatch.setattr(loop, "_create_sync", _create)
    monkeypatch.setattr(loop, "_discard_sync", lambda _db, drop_id: discarded.append(drop_id))
    monkeypatch.setattr(loop, "_set_message_sync", lambda *_a: None)
    monkeypatch.setattr(loop, "channel_is_busy", AsyncMock(return_value=False))
    monkeypatch.setattr(loop, "_next_due", {})

    async def tick() -> None:
        loop._next_due[GID] = 0.0  # due now, every time
        await loop._consider_guild(bot, "db", guild, NOW)  # type: ignore[arg-type]

    return SimpleNamespace(
        channel=channel, channels=channels, tick=tick, created=created,
        discarded=discarded,
    )


@pytest.mark.parametrize("broken", ["send", "history"])
async def test_a_channel_that_keeps_failing_is_paused_then_probed(world, clock, broken):
    if broken == "send":
        world.channel.send = AsyncMock(side_effect=_forbidden())
    else:
        world.channel.history_fails = True
    for _ in range(loop.MAX_SEND_FAILURES):
        await world.tick()
    assert loop._failures.failing() == {(GID, CID)}
    rows_so_far = len(world.created)

    # Paused: no drop row, no request.
    world.channel.send = AsyncMock(return_value=SimpleNamespace(id=777))
    world.channel.history_fails = False
    await world.tick()
    world.channel.send.assert_not_awaited()
    assert len(world.created) == rows_so_far

    # An hour on, the probe posts a drop and ends the pause.
    clock.now += loop.PAUSED_PROBE_SECONDS
    await world.tick()
    world.channel.send.assert_awaited_once()
    assert loop._failures.failing() == set()


async def test_a_failed_send_discards_its_drop_row(world):
    world.channel.send = AsyncMock(side_effect=_forbidden())
    await world.tick()
    assert world.discarded == world.created == [1]


async def test_picking_another_channel_starts_clean(world, monkeypatch):
    """The pause belongs to the channel that failed: an admin who moves drops
    elsewhere gets them straight away, not after the hourly probe."""
    world.channel.send = AsyncMock(side_effect=_forbidden())
    for _ in range(loop.MAX_SEND_FAILURES):
        await world.tick()
    other = FakeChannel()
    other.id = CID + 1
    world.channels[other.id] = other
    moved = replace(SETTINGS, drops_channel_id=other.id)
    monkeypatch.setattr(loop, "_guild_state_sync", lambda *_a: (moved, False))
    await world.tick()
    other.send.assert_awaited_once()
