"""The shared background-loop runner, ``spawn`` for one-shot tasks, and the
``FailureStreak`` that stops a delivery loop retrying a dead channel forever.

Four service loops (chat revive, coin drops, greeting watch, intake nudges)
were the same five lines. The lines are worth testing because getting them
wrong is *quiet*: a loop that lets an exception escape simply stops, and
nothing announces that coin drops died three days ago.
"""

from __future__ import annotations

import ast
import asyncio
import logging

import pytest

from bot_modules.core import background
from bot_modules.core.background import FailureStreak, run_forever, spawn
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"


class FakeBot:
    """Closes after ``ticks`` iterations so the loop terminates."""

    def __init__(self, ticks: int):
        self._left = ticks
        self.waited = False

    async def wait_until_ready(self) -> None:
        self.waited = True

    def is_closed(self) -> bool:
        if self._left <= 0:
            return True
        self._left -= 1
        return False


@pytest.fixture
def no_sleep(monkeypatch):
    """Record sleeps instead of taking them."""
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


@pytest.mark.asyncio
async def test_waits_for_the_gateway_before_the_first_tick(no_sleep):
    """The tick reads the member/channel cache, so it can't run cold."""
    order: list[str] = []
    bot = FakeBot(ticks=1)

    async def _ready():
        order.append("ready")

    bot.wait_until_ready = _ready  # type: ignore[method-assign]

    async def tick():
        order.append("tick")

    await run_forever(bot, tick=tick, interval=5, label="x")
    assert order == ["ready", "tick"]


@pytest.mark.asyncio
async def test_ticks_until_the_bot_closes(no_sleep):
    calls = []
    await run_forever(FakeBot(ticks=3), tick=lambda: _count(calls), interval=5, label="x")
    assert len(calls) == 3
    assert no_sleep == [5, 5, 5]


async def _count(calls):
    calls.append(1)


@pytest.mark.asyncio
async def test_a_failing_tick_does_not_end_the_loop(no_sleep, caplog):
    """The whole point. One bad tick must not stop coin drops for good."""
    calls = []

    async def tick():
        calls.append(1)
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        await run_forever(FakeBot(ticks=3), tick=tick, interval=5, label="coin drop")

    assert len(calls) == 3  # kept going
    assert "coin drop tick crashed" in caplog.text
    assert caplog.records[0].exc_info is not None  # with a traceback


@pytest.mark.asyncio
async def test_it_still_sleeps_after_a_failed_tick(no_sleep):
    """Otherwise a tick that throws instantly spins the event loop."""

    async def tick():
        raise RuntimeError("boom")

    await run_forever(FakeBot(ticks=2), tick=tick, interval=30, label="x")
    assert no_sleep == [30, 30]


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(no_sleep):
    """Shutdown cancels these tasks; eating it would hang the close."""

    async def tick():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_forever(FakeBot(ticks=3), tick=tick, interval=5, label="x")


@pytest.mark.asyncio
async def test_a_closed_bot_never_ticks(no_sleep):
    calls = []
    await run_forever(FakeBot(ticks=0), tick=lambda: _count(calls), interval=5, label="x")
    assert calls == []


@pytest.mark.asyncio
async def test_the_traceback_files_under_the_callers_logger(no_sleep, caplog):
    """Each loop passes its own logger so the crash reads as that service's,
    not as the scaffolding's."""

    async def tick():
        raise RuntimeError("boom")

    mine = logging.getLogger("bot_modules.onboarding.intake.loop")
    with caplog.at_level(logging.ERROR):
        await run_forever(
            FakeBot(ticks=1), tick=tick, interval=5, label="intake nudge", logger=mine
        )
    assert caplog.records[0].name == "bot_modules.onboarding.intake.loop"


# ── spawn ─────────────────────────────────────────────────────────────


async def _settle(task: asyncio.Task) -> None:
    """Let the task finish and its done-callbacks run."""
    await asyncio.wait([task])
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_spawned_crash_is_logged_as_it_happens(caplog):
    """Not whenever the garbage collector gets round to the task."""

    async def _boom():
        raise RuntimeError("boom")

    mine = logging.getLogger("dungeonkeeper.chicken")
    with caplog.at_level(logging.ERROR):
        await _settle(spawn(_boom(), label="chicken crash", logger=mine))

    (record,) = caplog.records
    assert record.name == "dungeonkeeper.chicken"
    assert record.getMessage() == "chicken crash crashed"
    assert record.exc_info and isinstance(record.exc_info[1], RuntimeError)


@pytest.mark.asyncio
async def test_a_spawned_task_is_held_until_it_finishes():
    """The loop keeps only a weak reference; ``spawn`` keeps the strong one."""
    gate = asyncio.Event()

    async def _wait():
        await gate.wait()

    task = spawn(_wait(), label="x")
    assert task in background._LIVE
    gate.set()
    await _settle(task)
    assert task not in background._LIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["returns", "cancelled"])
async def test_a_spawned_task_that_does_not_fail_logs_nothing(ending, caplog):
    """Cancellation is shutdown, not a crash."""

    async def _body():
        if ending == "cancelled":
            await asyncio.sleep(3600)

    with caplog.at_level(logging.ERROR):
        task = spawn(_body(), label="x")
        if ending == "cancelled":
            await asyncio.sleep(0)
            task.cancel()
        await _settle(task)
    assert caplog.records == []


def _discarded_tasks(sources: dict[str, str]) -> list[str]:
    """Every ``create_task``/``ensure_future`` whose task is thrown away."""
    found = []
    for path, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute | ast.Name)
                and getattr(node.value.func, "attr", getattr(node.value.func, "id", ""))
                in ("create_task", "ensure_future")
            ):
                found.append(f"{path}:{node.lineno}")
    return found


def test_no_task_is_started_and_forgotten():
    """A task nobody holds can be collected mid-run, and one nobody awaits
    fails in silence. Keep the task, or hand the coroutine to ``spawn``."""
    sources = {
        str(p.relative_to(SRC)): p.read_text(encoding="utf-8")
        for p in SRC.rglob("*.py")
    }
    assert _discarded_tasks(sources) == []


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("asyncio.create_task(f())", True),
        ("loop.create_task(f())", True),
        ("create_task(f())", True),
        ("asyncio.ensure_future(f())", True),
        ("task = asyncio.create_task(f())", False),
        ("self._tasks.add(asyncio.create_task(f()))", False),
        ("spawn(f(), label='x')", False),
    ],
)
def test_the_discarded_task_sweep(line, flagged):
    assert bool(_discarded_tasks({"m.py": line})) is flagged


# ── FailureStreak ─────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _streak(clock: Clock | None = None, **kw) -> FailureStreak[str]:
    kw.setdefault("threshold", 3)
    return FailureStreak("drops", clock=clock or Clock(), **kw)


def _fail(streak: FailureStreak[str], key: str, times: int = 1) -> None:
    """Fail the way a caller does: from inside the handler that caught it."""
    for _ in range(times):
        try:
            raise RuntimeError("403 Forbidden")
        except RuntimeError:
            streak.failed(key, where=f"#{key}")


@pytest.mark.parametrize(
    ("failures", "paused"),
    [(0, False), (2, False), (3, True), (7, True)],
)
def test_a_key_pauses_at_the_threshold(failures, paused):
    streak = _streak()
    _fail(streak, "general", failures)
    assert streak.paused("general") is paused
    assert (streak.failing() == {"general"}) is paused


def test_a_success_ends_the_streak():
    """Two failures, a success, two more — never five in a row."""
    streak = _streak()
    _fail(streak, "general", 2)
    streak.succeeded("general")
    _fail(streak, "general", 2)
    assert not streak.paused("general")


def test_keys_count_separately():
    streak = _streak()
    _fail(streak, "general", 3)
    assert streak.paused("general")
    assert not streak.paused("lounge")


def test_without_a_probe_it_stays_paused_until_reset():
    clock = Clock()
    streak = _streak(clock)
    _fail(streak, "general", 3)
    clock.now += 10 * 86400
    assert streak.paused("general")
    streak.reset("general")
    assert not streak.paused("general")
    assert streak.failing() == set()


def test_a_probe_gets_one_attempt_per_interval():
    """Fixing the permissions heals it, at one request an hour, not a tick."""
    clock = Clock()
    streak = _streak(clock, retry_after=3600)
    _fail(streak, "general", 3)
    clock.now += 3599
    assert streak.paused("general")
    clock.now += 1
    assert not streak.paused("general")  # the probe
    _fail(streak, "general")  # …failed
    assert streak.paused("general")  # so another hour
    assert streak.failing() == {"general"}
    clock.now += 3600
    assert not streak.paused("general")
    streak.succeeded("general")  # the probe worked
    assert streak.failing() == set()


def test_it_logs_each_failure_then_gives_up_once(caplog):
    """Warnings with a count and a traceback, one error at the threshold."""
    streak = _streak(paused_note="drops there are paused")
    with caplog.at_level(logging.WARNING):
        _fail(streak, "general", 4)
    levels = [r.levelname for r in caplog.records]
    assert levels == ["WARNING", "WARNING", "ERROR", "WARNING"]
    assert caplog.records[0].getMessage() == "drops: could not deliver to #general (failure 1/3)"
    assert caplog.records[2].getMessage() == (
        "drops: giving up on #general after 3 consecutive failures — drops there are paused"
    )
    assert all(r.exc_info for r in caplog.records)


# ── what the dashboard reads ─────────────────────────────────────────


def _fail_at(streak: FailureStreak, key, times: int, **kw) -> None:
    for _ in range(times):
        try:
            raise RuntimeError("403 Forbidden")
        except RuntimeError:
            streak.failed(key, where=str(key), **kw)


def test_stalled_deliveries_lists_paused_keys_for_one_guild():
    panel = FailureStreak("zz panel", threshold=2, clock=Clock())  # keyed by guild
    drops = FailureStreak("zz drops", threshold=2, retry_after=60, clock=Clock())
    _fail_at(panel, 7, 2, channel_id=70)
    _fail_at(drops, (7, 71), 2)
    _fail_at(drops, (7, 72), 1)  # below the threshold: still trying
    _fail_at(drops, (8, 80), 2)  # another guild
    stalls = [s for s in background.stalled_deliveries(7) if s.label.startswith("zz")]
    assert [(s.label, s.channel_id, s.failures, s.retrying) for s in stalls] == [
        ("zz drops", 71, 2, True),
        ("zz panel", 70, 2, False),
    ]


def test_a_key_that_names_no_guild_is_left_off():
    streak = _streak()
    _fail(streak, "general", 3)
    assert streak.failing() == {"general"}
    assert streak.stalls() == []


def test_the_streak_registry_does_not_keep_a_streak_alive():
    import gc

    # A logger nothing captures: a captured record's traceback would hold the
    # frame, and the frame the streak.
    quiet = logging.getLogger("zz.quiet")
    quiet.propagate = False
    streak = FailureStreak("zz gone", threshold=1, logger=quiet, clock=Clock())
    _fail_at(streak, 9, 1)
    assert any(s.label == "zz gone" for s in background.stalled_deliveries(9))
    del streak
    gc.collect()
    assert not any(s.label == "zz gone" for s in background.stalled_deliveries(9))
