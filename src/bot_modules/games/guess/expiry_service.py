"""Guess Who round lifetime — the rounds nobody solved.

A crop that is too hard is a dead end for everyone: guessers burn their cap and
the card keeps a live Guess button that leads nowhere. With
``guess_round_lifetime_days`` set, an open round older than that resolves
unsolved: the round is stamped ``expired_at``, its card is **deleted** from the
channel and its cached original is deleted from disk. Nothing is revealed:
members consented to a reveal only when a round is solved.

It is **not** a solve. ``solved_at`` and ``solver_id`` stay NULL, because the
top-posters query counts any row with ``solved_at`` set as a solve — stamping it
would put every timed-out round on a public leaderboard (commit ``6edc1b55``) —
and nobody is credited as a guesser.

This module is the database half plus the clean-up decision: which rounds are
due, claiming them, and :func:`clear_expired_round` (delete the card, delete the
file, stamp ``expiry_cleared_at``). Claiming happens **first**, in one write
transaction with a per-round race guard, so a correct guess landing at the same
moment either wins (the claim then skips the round) or loses (the guess is told
the round has ended), never both. A card that could not be deleted stays
uncleared and the next daily sweep retries it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import discord

from bot_modules.core.db_utils import open_db_immediate
from bot_modules.games.guess.models import GuessRound
from bot_modules.games.guess.repo import (
    _row_to_round,
    list_expirable_rounds,
    list_uncleared_expired_rounds,
    mark_expiry_cleared,
    mark_round_expired,
    set_round_original_path,
)

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


def expiry_cutoff(lifetime_days: int, *, now: float | None = None) -> float | None:
    """Rounds created before this have outlived the dial; ``None`` when the
    dial is 0 (never expire) or nonsensical."""
    if lifetime_days <= 0:
        return None
    return (now if now is not None else time.time()) - lifetime_days * SECONDS_PER_DAY


def claim_expired_rounds(
    db_path: Path, *, guild_id: int, lifetime_days: int, now: float | None = None
) -> list[GuessRound]:
    """Mark every due round in the guild expired and return the ones this call won.

    A round already solved, deleted, flagged ``answer_optout`` or expired is
    never returned; the opt-out case is already closed by the member's own
    withdrawal.
    """
    when = now if now is not None else time.time()
    cutoff = expiry_cutoff(lifetime_days, now=when)
    if cutoff is None:
        return []
    claimed: list[GuessRound] = []
    # IMMEDIATE: the list is read and then written, and a solve committing in
    # between would otherwise fail the UPDATE on a stale snapshot and throw
    # away every claim in the pass.
    with open_db_immediate(db_path) as conn:
        for row in list_expirable_rounds(conn, guild_id=guild_id, older_than=cutoff):
            if mark_round_expired(conn, row["id"], when=when):
                claimed.append(_row_to_round(row))
    return claimed


def pending_expired_rounds(db_path: Path, *, guild_id: int) -> list[GuessRound]:
    """Expired rounds whose card is not deleted yet (fresh claims and retries)."""
    with open_db_immediate(db_path) as conn:
        return [
            _row_to_round(r) for r in list_uncleared_expired_rounds(conn, guild_id=guild_id)
        ]


async def clear_expired_round(
    db_path: Path,
    round_row: GuessRound,
    delete_message: Callable[[], Awaitable[None]] | None,
    *,
    now: float | None = None,
) -> bool:
    """Delete the round's cached original and card; True once it is cleared.

    The original always goes. The card counts as done when *delete_message*
    succeeds, raises ``NotFound`` (already gone) or is ``None`` (the channel is
    gone). Any other Discord error leaves the round uncleared so the next sweep
    retries; the original is already gone by then.
    """
    if round_row.original_path:
        await asyncio.to_thread(Path(round_row.original_path).unlink, missing_ok=True)
        with open_db_immediate(db_path) as conn:
            set_round_original_path(conn, round_row.id, "")
    if delete_message is not None:
        try:
            await delete_message()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("guess: round %d expired card could not be deleted", round_row.id)
            return False
    with open_db_immediate(db_path) as conn:
        mark_expiry_cleared(conn, round_row.id, when=now if now is not None else time.time())
    return True
