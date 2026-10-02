"""Stopped deliveries and loops, as Config Problems card rows.

The bot stops doing two kinds of thing on its own, and both used to be visible
only as a log line in a log wiped every boot:

* a delivery paused by its ``FailureStreak`` — a sticky panel, coin drops or
  a chat revive that failed enough times in one channel to stop trying;
* a background loop the restart supervisor gave up on after too many crashes.

This turns ``core/background``'s records of both into rows in the shape of the
channel-health route's ``system`` list (``code``, ``severity``, ``title``,
``message`` — what ``tiles/config-problems.js`` renders), so the card needs no
change to show them. Pure: the route hands in the records and a channel namer.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from bot_modules.core.background import GaveUp, Stall


def _sentence(note: str) -> str:
    return f"{note[:1].upper()}{note[1:]}." if note else ""


def stall_problem(stall: Stall, channel_name: Callable[[int], str | None]) -> dict[str, str]:
    """One paused delivery.

    A probed pause (``retrying``) heals itself once the channel works again,
    so it warns; one that waits for proof stays broken until someone acts.
    """
    name = channel_name(stall.channel_id) if stall.channel_id else None
    where = f"#{name}" if name else "its channel"
    label = f"{stall.label[:1].upper()}{stall.label[1:]}"
    message = (
        f"The last {stall.failures} tries to post in {where} all failed, so "
        f"the bot stopped trying. {_sentence(stall.paused_note)} Check that "
        "the bot can still see and send messages there."
    )
    return {
        "code": f"delivery_paused:{stall.label}:{stall.channel_id or 0}",
        "severity": "warning" if stall.retrying else "error",
        "title": f"{label} can't post in {where}",
        "message": " ".join(message.split()),
    }


def gave_up_problem(record: GaveUp) -> dict[str, str]:
    """One background loop the supervisor stopped restarting."""
    at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(record.at))
    return {
        "code": f"loop_stopped:{record.name}",
        "severity": "error",
        "title": f"Background job {record.name} has stopped",
        "message": (
            f"It crashed {record.crashes} times and the bot stopped restarting "
            f"it at {at}. It stays stopped until the bot restarts. Last error: "
            f"{record.error}"
        ),
    }


def delivery_problems(
    gave_up: Iterable[GaveUp],
    stalls: Iterable[Stall],
    channel_name: Callable[[int], str | None],
) -> list[dict[str, str]]:
    """Every stopped loop, then every paused delivery, as card rows."""
    return [gave_up_problem(g) for g in gave_up] + [
        stall_problem(s, channel_name) for s in stalls
    ]
