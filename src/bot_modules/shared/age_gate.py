"""Discord's own age gate, read at both levels.

The one place the bot asks whether a channel is age-gated. It lived in the
NSFW image classifier until that was removed (docs/specs/post_monitoring_spec.md);
everything that gates on age restriction still reads it from here.
"""
from __future__ import annotations

import logging

log = logging.getLogger("dungeonkeeper.age_gate")


def is_age_gated_channel(channel: object, *, default: bool = False) -> bool:
    """Whether Discord itself age-gates this channel.

    **A category's flag counts.** ``TextChannel.is_nsfw()`` reports only the
    channel's own flag, and Discord does not cascade an age-restricted
    category onto the channels already inside it — so a channel sitting in a
    category full of age-gated siblings reads as SFW here unless someone
    ticked it individually. That gap deleted a member's upload in
    ``themes-and-challenges`` on 2026-09-01: the channel was the one in its
    category without the flag. Reading the category too closes it, and reads
    **Discord's own age gate at both levels** — there is no bot-side "treat
    this as NSFW" anywhere, which is the rule in CLAUDE.md.

    Threads delegate to their parent, and channel types without the concept
    (DMs, group DMs) are False.

    *default* is what to return when the age gate can't be established at all
    — a partial object, a thread whose parent isn't cached. ``False`` is the
    safe direction for gates that permit something in NSFW channels (no tip
    offered on a channel we can't read); a caller excluding NSFW channels from
    something wants ``True``, and :func:`advisor_context.can_view` passes it.
    """
    checker = getattr(channel, "is_nsfw", None)
    if not callable(checker):
        return default
    try:
        if bool(checker()):
            return True
    except Exception:  # noqa: BLE001 - a partial channel object must not raise here
        # Fail-safe, but not silent: an age gate that cannot be read is worth
        # a line whichever caller hit it.
        log.debug("age gate: is_nsfw() raised on %r", channel, exc_info=True)
        return default
    try:
        # Thread.category raises ClientException when the parent isn't cached,
        # so this walk stays inside a try rather than beside one.
        category = getattr(channel, "category", None)
    except Exception:  # noqa: BLE001 — a raising category lookup must fall back to the caller's default, never break the NSFW gate
        log.debug("age gate: category lookup raised on %r", channel, exc_info=True)
        return default
    if category is None:
        return False
    parent_checker = getattr(category, "is_nsfw", None)
    if not callable(parent_checker):
        return False
    try:
        return bool(parent_checker())
    except Exception:  # noqa: BLE001 — a raising category flag must fall back to the caller's default, never break the NSFW gate
        log.debug("age gate: category is_nsfw() raised on %r", channel, exc_info=True)
        return default
