"""Channel panels that redraw themselves when the bot restarts or the art changes.

Ben, 2026-09-26: "can we update all panels on update". A panel that is already
in a channel keeps whatever the code that posted it drew until something
redraws it. Before this, a new layout, a wording fix or a new banner reached a
posted panel only when an admin pressed Post again or a sticky happened to
re-stick. See ``docs/plans/panel-refresh.md``.

This module is only the list. A panel joins it with ``register(name, fn)``,
where ``fn(guild)`` edits that guild's copies in place and returns how many
edits it issued (a bool is fine for a panel with one copy).
``services.panel_refresh_service`` runs every registered function, once per
boot and on every ``panel_art_change`` (``cogs.panel_refresh_cog`` fires
both). Two kinds join:

* a ``StickyPanel`` built with ``refresh_on_update=True``, which registers its
  own in-place refresh;
* the posted-once panels recorded in the ``posted_panels`` table, as one
  entry.

The Confessions launcher stays out on purpose: it re-sticks on every message
in its channel and draws nothing from art or settings, so a refresh would
only repeat what the next message already does.

Keyed by name, so a reloaded cog's new instance replaces the old one's entry
instead of leaving a dead one behind.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import discord

Refresher = Callable[[discord.Guild], Awaitable[int]]

_REFRESHERS: dict[str, Refresher] = {}


def register(name: str, fn: Refresher) -> None:
    """Add ``fn`` under ``name``, replacing any earlier entry of that name."""
    _REFRESHERS[name] = fn


def unregister(name: str, fn: Refresher | None = None) -> None:
    """Drop ``name``. With ``fn``, only if that is still the entry — so an old
    instance unloading after its replacement registered can't remove it."""
    if fn is None or _REFRESHERS.get(name) == fn:
        _REFRESHERS.pop(name, None)


def registered() -> list[tuple[str, Refresher]]:
    """Every entry, in registration order — a snapshot, safe to iterate while
    a cog reload registers or unregisters."""
    return list(_REFRESHERS.items())
