"""My Data → Delete everything: the departure recipe, run on a member who is still here.

Billy, 2026-09-27: *"can we add a … delete everything button?"* — decided as
"hard erase but anonymize usage data", which is exactly what the departure
auto-anonymise already does to a leaver. So this runs that recipe, on the
member's own press, with no dial ("it's an important privacy guard"):

1. refuse a member who is jailed or on a live ticket — their record is
   moderation business, and a mod has to be involved;
2. delete every Discord message they posted in this guild (``MODE_ALL``) —
   and stop, with nothing else touched, if any message would not go;
3. end an open break, giving their roles back, and stop if that fails —
   they are still here, and a deleted break row would strand them wearing
   the break role with no saved roles to return;
4. take down their Guess Who posts and unlink their cached originals;
5. in one write-locked transaction, delete their Guess records and anonymise
   the rest (``departure_erasure_service.anonymise_member``, the same core
   the departure job runs, so the two paths cannot drift).

**Live state is not protected, on purpose.** A leaver's escrowed wager, game
seat, owned voice room and Pen Pals pairing are refunded and closed by nine
leave listeners before the departure job runs; a member pressing this button
has had none of that. Billy's call (2026-09-27): *just erase* — whatever is
mid-flight moves to the surrogate with everything else. The confirm says so
in as many words (``privacy.logic.EVERYTHING_IN_PROGRESS``), and that sentence
is the only guard, so a test pins it.

**The receipt does not name them.** The audit row's actor is the bot and its
target is empty, as the departure job writes it: the member's own id would be
the one row the anonymisation left behind.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import open_db, open_db_immediate
from bot_modules.privacy.deletion_service import DeletionResult
from bot_modules.games.guess import cleanup_service as guess_cleanup_service
from bot_modules.privacy.departure_erasure_service import (
    SCOPE_ALL,
    anonymise_member,
    choose_scope,
    standing_hold,
)
from bot_modules.privacy.service import AnonymizeReport

log = logging.getLogger("dungeonkeeper.privacy.self_erasure")

AUDIT_ACTION = "privacy_self_anonymise"

STATUS_DONE = "done"
#: Jailed or on a live ticket; ``detail`` is the departure hold constant.
STATUS_HELD = "held"
#: Some messages would not delete; nothing past the message sweep ran.
STATUS_MESSAGES_LEFT = "messages_left"
#: The break could not be ended; nothing past the message sweep ran.
STATUS_BREAK_STUCK = "break_stuck"
#: Anything else that raised. Messages and Guess posts may already be gone.
STATUS_ERROR = "error"


@dataclass(frozen=True)
class SelfErasureHooks:
    """The Discord side of a run, injected so the orchestration is testable."""

    #: Delete the member's messages in this guild, reporting on their card.
    delete_messages: Callable[[], Awaitable[DeletionResult]]
    #: End an open break, giving their roles back. True if there was none.
    end_break: Callable[[], Awaitable[bool]]
    #: Whether they are in any other guild the bot shares. Raise if unknown.
    present_elsewhere: Callable[[], Awaitable[bool]]
    #: This guild's channel ids, for the channel-keyed tables.
    channel_ids: Callable[[], Sequence[int]]
    #: Delete one Guess round's post: ``(guild_id, channel_id, message_id)``.
    delete_guess_post: guess_cleanup_service.DeletePost


@dataclass(frozen=True)
class Outcome:
    status: str
    #: The scope for ``done``, the hold for ``held``, the error for ``error``.
    detail: str = ""
    messages: DeletionResult | None = None
    report: AnonymizeReport | None = None


async def erase_everything(
    db_path: Path,
    *,
    guild_id: int,
    user_id: int,
    primary_guild_id: int,
    actor_id: int,
    hooks: SelfErasureHooks,
) -> Outcome:
    """Run the whole press, or stop at the first step that did not complete."""
    messages: DeletionResult | None = None
    try:
        hold = await asyncio.to_thread(_hold, db_path, guild_id, user_id)
        if hold:
            return Outcome(STATUS_HELD, hold)
        messages = await hooks.delete_messages()
        if messages.failed:
            return Outcome(STATUS_MESSAGES_LEFT, messages=messages)
        # Asked only where the answer matters: choose_scope never picks all
        # guilds outside the primary one.
        present_elsewhere = (
            await hooks.present_elsewhere() if guild_id == primary_guild_id else True
        )
        scope = choose_scope(
            guild_id, primary_guild_id=primary_guild_id, present_elsewhere=present_elsewhere
        )
        channel_ids = tuple(hooks.channel_ids())
        # The scan is slow; a mod may have jailed them or opened a ticket on
        # them while it ran. Ask again with nothing irreversible past it done.
        hold = await asyncio.to_thread(_hold, db_path, guild_id, user_id)
        if hold:
            return Outcome(STATUS_HELD, hold, messages=messages)
        if not await hooks.end_break():
            return Outcome(STATUS_BREAK_STUCK, messages=messages)
        # Posts and files first, outside the transaction: neither a Discord
        # delete nor an unlink can be rolled back.
        await guess_cleanup_service.clear_member_media(
            db_path,
            user_id,
            guild_id=None if scope == SCOPE_ALL else guild_id,
            delete_post=hooks.delete_guess_post,
        )
        report = await asyncio.to_thread(
            _finish,
            db_path,
            guild_id=guild_id,
            user_id=user_id,
            scope=scope,
            channel_ids=channel_ids,
            actor_id=actor_id,
        )
    except Exception as exc:  # noqa: BLE001 - the member must never read "done" after a failure
        # Never the member id: the log would outlive the anonymisation.
        log.exception("Delete everything failed in guild %d", guild_id)
        return Outcome(STATUS_ERROR, f"{type(exc).__name__}: {exc}", messages=messages)
    if isinstance(report, str):
        return Outcome(STATUS_HELD, report, messages=messages)
    return Outcome(STATUS_DONE, scope, messages=messages, report=report)


def _hold(db_path: Path, guild_id: int, user_id: int) -> str | None:
    with open_db(db_path) as conn:
        return standing_hold(conn, guild_id, user_id)


def _finish(
    db_path: Path,
    *,
    guild_id: int,
    user_id: int,
    scope: str,
    channel_ids: Sequence[int],
    actor_id: int,
) -> AnonymizeReport | str:
    """The database half, under the write lock the surrogate allocation needs.

    The hold is checked once more under the lock, and returned if it caught one.
    """
    with open_db_immediate(db_path) as conn:
        hold = standing_hold(conn, guild_id, user_id)
        if hold:
            return hold
        return anonymise_member(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            scope=scope,
            channel_ids=channel_ids,
            actor_id=actor_id,
            action=AUDIT_ACTION,
        )
