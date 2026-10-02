"""Scaffolding for the bot's long-running background loops.

A service loop is the same five lines everywhere: wait for the gateway, then
tick until the bot closes, surviving anything a tick throws. Getting those
five lines wrong is quiet and expensive — a loop that lets an exception
escape simply stops, and nothing announces that coin drops or intake nudges
died three days ago.

A one-shot task handed to the loop and forgotten has the same failure twice
over: nothing holds it, so the loop's weak reference can let it be collected
mid-run, and nothing reads its result, so its traceback surfaces — if ever —
as an unlabelled "Task exception was never retrieved" whenever it is
collected. ``spawn`` is the forgettable version.

And a loop that posts into a channel on a schedule has a third: when the
channel stops accepting its messages it tries again every tick, forever, one
doomed request and one traceback at a time — and since ``log.txt`` is wiped
every boot, the warnings never even add up to evidence. ``FailureStreak`` is
the counter that stops it.

Both kinds of stopping are kept where the dashboard can read them — every
paused delivery (``stalled_deliveries``) and every supervised loop the restart
supervisor gave up on (``gave_up``) — because a warning in a log that is wiped
at the next boot is not something an admin will ever see. The Config Problems
card shows them (``services/delivery_health_logic``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, TypeVar

DEFAULT_LOG = logging.getLogger(__name__)

# Strong references to every spawned task until it finishes. The event loop
# keeps only a weak one, so a task nothing else holds can vanish mid-await.
_LIVE: set[asyncio.Task[Any]] = set()

K = TypeVar("K", bound=Hashable)


@dataclass(frozen=True)
class GaveUp:
    """A supervised background loop the restart supervisor stopped restarting."""

    name: str
    #: Wall-clock time of the give-up.
    at: float
    crashes: int
    #: The last exception, one line.
    error: str


@dataclass(frozen=True)
class Stall:
    """A delivery paused by its ``FailureStreak``."""

    label: str
    guild_id: int
    #: The channel the last failure was in, when the caller knows it.
    channel_id: int | None
    failures: int
    #: Whether it is probed on a timer (True) or waits for proof (False).
    retrying: bool
    paused_note: str


# name → the give-up, for this process's lifetime: a restart restarts the loop.
_GAVE_UP: dict[str, GaveUp] = {}

# Every live FailureStreak. Weak, because some belong to panels that come and
# go (a mahjong table's), and a registry must not keep them alive.
_STREAKS: weakref.WeakSet[FailureStreak[Any]] = weakref.WeakSet()


def record_give_up(name: str, *, crashes: int, error: BaseException) -> None:
    """The supervisor stopped restarting ``name``; keep it for the dashboard."""
    _GAVE_UP[name] = GaveUp(
        name=name,
        at=time.time(),
        crashes=crashes,
        error=f"{type(error).__name__}: {error}".splitlines()[0][:200],
    )


def gave_up() -> list[GaveUp]:
    """Every loop given up on since this process started, oldest first."""
    return sorted(_GAVE_UP.values(), key=lambda g: g.at)


def stalled_deliveries(guild_id: int) -> list[Stall]:
    """Every paused delivery into ``guild_id``, across every streak."""
    stalls = [s for streak in list(_STREAKS) for s in streak.stalls()]
    return sorted(
        (s for s in stalls if s.guild_id == guild_id),
        key=lambda s: (s.label, s.channel_id or 0),
    )


def _guild_of(key: Hashable) -> int | None:
    """The guild a streak key names: the key itself, or a pair's first element.

    Every delivery streak keys by a guild id or a ``(guild, channel)`` pair;
    a key that is neither names no guild and is left off the dashboard.
    """
    head = key[0] if isinstance(key, tuple) and key else key
    return head if isinstance(head, int) and not isinstance(head, bool) else None


async def run_forever(
    bot: Any,
    *,
    tick: Callable[[], Awaitable[Any]],
    interval: float,
    label: str,
    logger: logging.Logger | None = None,
) -> None:
    """Await ``tick()`` every ``interval`` seconds until the bot closes.

    The contract each caller was hand-rolling:

    * wait for the gateway before the first tick, so the tick can rely on
      the cache being warm;
    * re-raise ``CancelledError`` — shutdown cancels these tasks, and
      swallowing it would hang the close;
    * swallow everything else, logged with a traceback, because one bad
      tick must not end the loop for good;
    * sleep *after* the tick, including after a failed one, so a tick that
      throws instantly can't spin the event loop.

    ``logger`` defaults to this module's, but callers pass their own so the
    traceback still files under the service that owns the loop rather than
    under the scaffolding.
    """
    log = logger or DEFAULT_LOG
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s tick crashed", label)
        await asyncio.sleep(interval)


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    label: str,
    logger: logging.Logger | None = None,
) -> asyncio.Task[Any]:
    """Start ``coro`` as a task nobody needs to await, and still hear it fail.

    The task is held until it finishes and its exception, if any, is logged
    with a traceback under ``label`` the moment it happens. Cancellation is
    not a failure and logs nothing. The task is returned for a caller that
    wants it after all, but discarding it is the point.
    """
    task = asyncio.create_task(coro, name=label)
    _LIVE.add(task)
    task.add_done_callback(partial(_reap, logger or DEFAULT_LOG, label))
    return task


def _reap(log: logging.Logger, label: str, task: asyncio.Task[Any]) -> None:
    _LIVE.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("%s crashed", label, exc_info=exc)


class FailureStreak(Generic[K]):
    """Consecutive delivery failures per key, and when to stop trying.

    One instance per delivery — a sticky panel, the coin-drop poster, the
    revive poster — keyed by whatever identifies a destination (a guild id,
    a ``(guild, channel)`` pair). Call ``failed`` from inside the ``except``
    that caught the send, so the traceback is logged with it; ``succeeded``
    after a delivery that worked; and ask ``paused`` before trying at all.

    Below ``threshold`` each failure logs a warning with its count. At the
    threshold it logs one error and the key is paused. ``retry_after=None``
    keeps it paused until ``succeeded``/``reset`` or a restart — right for a
    caller with some other proof the channel is back, like a sticky panel's
    re-post from the dashboard. A number lets one attempt through that many
    seconds after the last failure, so a loop with no such signal heals once
    someone fixes the channel's permissions, at one request per interval
    instead of one per tick.

    Held in memory on purpose: a restart is a fresh start. The paused keys
    are ``failing()``, and every instance registers itself so
    ``stalled_deliveries`` can show them all on the dashboard; a key names its
    guild (``_guild_of``), and its channel is the second element of a pair or
    whatever ``failed(channel_id=)`` last reported.
    """

    def __init__(
        self,
        label: str,
        *,
        threshold: int,
        logger: logging.Logger | None = None,
        retry_after: float | None = None,
        paused_note: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.label = label
        self.threshold = threshold
        self._log = logger or DEFAULT_LOG
        self._retry_after = retry_after
        self._paused_note = paused_note
        self._clock = clock
        # key → (consecutive failures, clock reading at the last one,
        #        the channel it was in)
        self._streaks: dict[K, tuple[int, float, int | None]] = {}
        _STREAKS.add(self)

    def failed(self, key: K, *, where: str, channel_id: int | None = None) -> None:
        """Count a failure at ``key``; ``where`` names it for the log.

        ``channel_id`` is for a key that doesn't carry its channel (a sticky
        panel keys by guild), so the dashboard can still name it.
        """
        count = self._streaks.get(key, (0, 0.0, None))[0] + 1
        self._streaks[key] = (count, self._clock(), channel_id)
        if count < self.threshold:
            self._log.warning(
                "%s: could not deliver to %s (failure %d/%d)",
                self.label, where, count, self.threshold, exc_info=True,
            )
        elif count == self.threshold:
            self._log.error(
                "%s: giving up on %s after %d consecutive failures%s",
                self.label, where, count,
                f" — {self._paused_note}" if self._paused_note else "",
                exc_info=True,
            )
        else:
            self._log.warning(
                "%s: %s still failing (%d in a row)",
                self.label, where, count, exc_info=True,
            )

    def succeeded(self, key: K) -> None:
        """A delivery worked: the streak is over."""
        self._streaks.pop(key, None)

    reset = succeeded

    def paused(self, key: K) -> bool:
        """Should the caller skip ``key`` this time?"""
        count, last, _ = self._streaks.get(key, (0, 0.0, None))
        if count < self.threshold:
            return False
        if self._retry_after is None:
            return True
        return self._clock() - last < self._retry_after

    def failing(self) -> set[K]:
        """Every key that has reached the threshold, probing or not."""
        return {k for k, (count, _, _) in self._streaks.items() if count >= self.threshold}

    def stalls(self) -> list[Stall]:
        """``failing()``, described for the dashboard."""
        out = []
        for key, (count, _, channel_id) in self._streaks.items():
            guild_id = _guild_of(key)
            if count < self.threshold or guild_id is None:
                continue
            if channel_id is None and isinstance(key, tuple) and len(key) > 1:
                channel_id = int(key[1])
            out.append(Stall(
                label=self.label,
                guild_id=guild_id,
                channel_id=channel_id,
                failures=count,
                retrying=self._retry_after is not None,
                paused_note=self._paused_note,
            ))
        return out
