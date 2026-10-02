"""One lock per member for everything that snapshots their roles.

Starting a break, ending one, jailing and marking inactive each read a saved
role set, change live roles over several REST calls, then write rows. Two of
them interleaving — "I'm back" landing while a jail strips — leaves the
snapshot and the live roles telling different stories, with no error. Each
takes this lock around its read → roles → write stretch, so they queue.

In-process is enough: the dashboard runs on the bot's own event loop.
Weak values, so a member nobody is touching costs nothing.
"""

from __future__ import annotations

import asyncio
import weakref

_locks: weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock] = weakref.WeakValueDictionary()


def member_role_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = (guild_id, user_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock
