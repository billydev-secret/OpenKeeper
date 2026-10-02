"""A timer helper that cancels a stored task never cancels the task running it.

A firing timer often reaches its own cancel helper — a game resolved from the
timer calls ``on_game_resolved``, which cancels "the game's timer", which is
the task doing the resolving. Cancelling the running task schedules
``CancelledError`` at its next ``await`` and the timer body's own
``except CancelledError`` swallows it, so everything after that await silently
never happens. Mahjong's phase timers wedged a whole live-QA session this way
(2026-08-23), and the 2026-09-14 standards recheck found the same shape in
five more game cogs. Tests that call the resolve path from the test task
cancel a *different* task and cannot see it; only a real firing can.

So every helper named like a timer cancel/arm/start that calls ``.cancel()``
must consult ``asyncio.current_task()`` in the same function. The per-cog
tests beside each game prove the behavior; this file stops the shape coming
back in a helper nobody has written yet. It scans the whole tree, so the
scoped pre-commit gate cannot map a cog edit to it — CI runs it on push.
"""

from __future__ import annotations

import ast
import re
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src" / "bot_modules"

TIMER_HELPER = re.compile(r"(cancel|arm|start)\w*timer")


def _unguarded_helpers(source: str) -> tuple[list[str], list[str]]:
    """(every matching helper that calls ``.cancel()``, the unguarded ones)."""
    seen: list[str] = []
    unguarded: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not TIMER_HELPER.search(node.name):
            continue
        cancels = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "cancel"
            and not n.args
            for n in ast.walk(node)
        )
        if not cancels:
            continue
        seen.append(f"{node.name}:{node.lineno}")
        guarded = any(
            (isinstance(n, ast.Attribute) and n.attr == "current_task")
            or (isinstance(n, ast.Name) and n.id == "current_task")
            for n in ast.walk(node)
        )
        if not guarded:
            unguarded.append(f"{node.name}:{node.lineno}")
    return seen, unguarded


def test_every_timer_cancel_helper_skips_the_running_task():
    seen: list[str] = []
    missed: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        found, bad = _unguarded_helpers(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC)
        seen += [f"{rel} {h}" for h in found]
        missed += [f"{rel} {h}" for h in bad]
    # Not vacuous: the five game cogs, round pacing, Mahjong and Voice
    # Control's two grace timers were all matched when this landed.
    assert len(seen) >= 9, seen
    assert not missed, (
        "timer helpers that can cancel the task running them — skip it with "
        "`task is not asyncio.current_task()`:\n" + "\n".join(missed)
    )


def test_the_scan_flags_an_unguarded_helper_and_passes_a_guarded_one():
    bad = (
        "def _cancel_timer(self, gid):\n"
        "    task = self._timers.pop(gid, None)\n"
        "    if task and not task.done():\n"
        "        task.cancel()\n"
    )
    good = bad.replace(
        "if task and not task.done():",
        "if task and task is not asyncio.current_task() and not task.done():",
    )
    assert _unguarded_helpers(bad)[1] == ["_cancel_timer:1"]
    assert _unguarded_helpers(good) == (["_cancel_timer:1"], [])
