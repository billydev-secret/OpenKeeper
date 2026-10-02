"""Guess cache housekeeping — getting members' originals off the disk.

A Guess submission writes the full-size original to ``guess_cache/orig/``
(:func:`guess_orig_dir` — beside the DB, under ``var/`` once the runtime data
moves there) and records the path on the round. Three things end that file's life, and all
three live here:

* :func:`rollback_unposted_round` — the round's channel post raised. The row
  and the file are both already durable at that point, so without this the
  submission becomes an open round that never posted and a photo the member
  never sees again (bug #227; prod rounds 21-23, 355, 379, 408 and 414).
* :func:`age_out_originals` — the round is open and old. Solved rounds delete
  their original at reveal time; this is the cut for the ones nobody solves.
* :func:`sweep_orphan_originals` — the file is on disk and *no* round points
  at it. Both of the above read ``guess_rounds`` to find work, so neither can
  see a file whose row was deleted out of band, or one written in the instant
  before the path was recorded. This one reads the directory instead.
* :func:`clear_member_media` — the member's rounds are about to be erased or
  anonymised. Unlike the three above this one also reaches Discord: it takes
  down the round's crop post as well as the original, because Billy's rule for
  every Guess erasure (2026-09-27) is *"Deleting the message should clear
  everything from guess."* It runs before the rows go, from the member's own
  My Data button, the dashboard's Erase, and the departure auto-anonymise.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.core.paths import data_dir
from bot_modules.games.guess.repo import (
    clear_round_original_path,
    get_round,
    insert_audit_event,
    list_all_recorded_originals,
    list_cached_originals_for_user,
    list_member_round_posts,
    list_stale_open_originals,
    flag_user_open_rounds_optout,
    soft_delete_round,
    withdraw_guess_consent,
)

log = logging.getLogger("dungeonkeeper.guess")

# How long an unsolved round's original submission may sit in the on-disk
# cache before the age-out sweep deletes the file (the round itself, and the
# crop already posted to Discord, are untouched). Solved rounds delete their
# original at solve time; this covers the rounds nobody ever solves. Cut from
# 90 to 7 days on 2026-09-15 with the retention statement's four-beat rewrite:
# a full-size private photo nobody has guessed in a week is not being played.
ORIGINAL_MAX_AGE_DAYS = 7

# How recently written a file must be for the orphan sweep to leave it alone.
# A submit writes the original a moment before the round records the path, so
# without this the sweep could delete the photo of a round being posted right
# now. An hour is far longer than that window and far shorter than the age-out.
ORPHAN_GRACE_SECONDS = 3600


def guess_orig_dir(db_path: Path) -> Path:
    """Where a round's full-size original is kept until the reveal deletes it.

    Resolved against the DB's parent like every other runtime data dir, so
    with production's relative ``dungeonkeeper.db`` the path a round records
    (``guess_cache/orig/<id>.jpg``) is relative too, and it is the same
    directory the orphan sweep reads. Until 2026-09-30 this was a
    cwd-relative ``Path("guess_cache")`` in the cog.
    """
    return data_dir("guess_cache", base=db_path.parent) / "orig"


def _unlink(path: Path) -> bool:
    """Best-effort delete. A file we cannot remove is logged, never raised —
    every caller here is cleaning up after something that already went wrong.

    Returns whether the file is gone (or was already gone) afterwards, so a
    caller that records the outcome — the rollback's audit event — doesn't
    have to claim success it didn't get.
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        log.warning("guess: could not delete cached original %s", path)
        return False


def rollback_unposted_round(
    db_path: Path, round_id: int, *, guild_id: int, actor_id: int
) -> None:
    """Undo a round whose channel post never landed.

    Deletes the cached original, clears its path, soft-deletes the round and
    records a ``post_failed`` audit entry. Soft rather than hard: round ids
    stay unique, and the failure is visible in the audit log afterwards —
    the four prod rounds that vanished this way left no trace at all, which
    is why nobody could explain them.
    """
    original_path = ""
    with open_db(db_path) as conn:
        row = get_round(conn, round_id)
        if row is not None:
            original_path = row.original_path or ""
        if original_path:
            clear_round_original_path(conn, round_id)
        soft_delete_round(conn, round_id)

    # Whether there was ever a file to remove, and — separately — whether the
    # removal actually succeeded. A disk error leaves the audit trail saying
    # so rather than claiming a delete that didn't happen (the DB reference is
    # cleared regardless; the orphan sweep will pick the leftover file up
    # later since nothing points at it any more).
    original_deleted = _unlink(Path(original_path)) if original_path else False

    try:
        with open_db(db_path) as conn:
            insert_audit_event(
                conn,
                guild_id=guild_id,
                actor_id=actor_id,
                action="post_failed",
                round_id=round_id,
                details={
                    "had_original": bool(original_path),
                    "original_deleted": original_deleted,
                },
            )
    except Exception:
        log.exception("guess: audit write failed for post_failed round=%s", round_id)


def age_out_originals(db_path: Path, *, now: float | None = None) -> int:
    """Delete cached originals for open rounds older than the age-out window.

    Returns the number of files cleared. Missing files (e.g. pre-rename
    ``veil_cache`` paths) still get their DB reference cleared — path-missing
    means already-clean."""
    cutoff = (now if now is not None else time.time()) - ORIGINAL_MAX_AGE_DAYS * 86400
    cleared = 0
    with open_db(db_path) as conn:
        for round_id, original_path in list_stale_open_originals(conn, older_than=cutoff):
            _unlink(Path(original_path))
            clear_round_original_path(conn, round_id)
            cleared += 1
    return cleared


def sweep_orphan_originals(
    db_path: Path, orig_dir: Path, *, now: float | None = None
) -> int:
    """Delete cached originals no round records a path to. Returns the count.

    A file counts as referenced if any round — open, solved, soft-deleted,
    any guild — holds its path. Matching is on the resolved path **and** on
    the bare filename: recorded paths are relative (``guess_cache/orig/443.jpg``,
    or ``var/guess_cache/orig/443.jpg`` after the move)
    and resolve against the process cwd, so if the sweep is ever pointed at a
    directory reached by another route, the name match is what stops it
    deleting live rounds' photos. Filenames are ``<round_id><ext>``, so that
    is an id match in practice.
    """
    now = now if now is not None else time.time()
    try:
        entries = [p for p in orig_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    if not entries:
        return 0

    with open_db(db_path) as conn:
        recorded = list_all_recorded_originals(conn)
    referenced = {str(Path(p).resolve()) for p in recorded}
    referenced_names = {Path(p).name for p in recorded}

    swept = 0
    for entry in entries:
        if str(entry.resolve()) in referenced or entry.name in referenced_names:
            continue
        try:
            if now - entry.stat().st_mtime < ORPHAN_GRACE_SECONDS:
                continue
        except OSError:
            continue
        _unlink(entry)
        swept += 1
    return swept


class MediaNotCleared(RuntimeError):
    """A member's Guess post or original could not be removed.

    Raised rather than logged: every caller is about to tell the member, or an
    admin, that their Guess data is gone — and the photo still sitting on disk,
    or its crop still up in the channel, would make that false. The caller
    stops before the rows go, so a retry finds everything it needs.
    """


#: ``(guild_id, channel_id, message_id) -> None``: delete one round's post in
#: Discord. A post that is already gone, or a channel the bot cannot see any
#: more, is success; anything else raises. The Discord half lives with the
#: caller (``PrivacyCog.delete_guess_post``) so this module stays testable.
DeletePost = Callable[[int, int, int], Awaitable[None]]


def delete_member_originals(db_path: Path, user_id: int, *, guild_id: int | None) -> int:
    """Unlink the member's cached Guess Who originals and clear the paths.

    A file already gone counts as cleared. A file that exists but cannot be
    removed raises :class:`MediaNotCleared`.

    **Each path is cleared in its own transaction, right after its own unlink
    succeeds**, and none of them share the caller's. An unlink cannot be rolled
    back, so a path clear that can be is a row pointing at a file that is gone:
    with two originals, the first deleted and the second refusing, one rollback
    used to restore the first file's path and the parked job left it there.

    Moved here from ``departure_erasure_service`` (as ``delete_guess_originals``)
    on 2026-09-27, when the member's own button became its third caller.
    """
    with open_db(db_path) as conn:
        rounds = list_cached_originals_for_user(conn, user_id, guild_id=guild_id)
    cleared = 0
    for round_id, original_path in rounds:
        try:
            Path(original_path).unlink(missing_ok=True)
        except OSError as exc:
            raise MediaNotCleared(f"could not delete a Guess Who original: {exc}") from exc
        with open_db(db_path) as conn:
            clear_round_original_path(conn, round_id)
        cleared += 1
    return cleared


async def clear_member_media(
    db_path: Path,
    user_id: int,
    *,
    guild_id: int | None,
    delete_post: DeletePost,
) -> None:
    """Take the member's rounds out of Discord and off the disk.

    Every round they submitted or are the answer to — the set
    ``guess_repo.forget_member`` deletes — loses its channel post and its
    cached original. *guild_id* narrows to one guild; ``None`` is every guild,
    for the all-guild anonymise.

    Posts first, then files; the rows are the caller's, afterwards. A failure
    raises and leaves the rows, so the posts and paths still to do are still
    findable on a retry.
    """

    def _posts() -> list[tuple[int, int, int]]:
        with open_db(db_path) as conn:
            return list_member_round_posts(conn, user_id, guild_id=guild_id)

    for post_guild_id, channel_id, message_id in await asyncio.to_thread(_posts):
        await delete_post(post_guild_id, channel_id, message_id)
    await asyncio.to_thread(delete_member_originals, db_path, user_id, guild_id=guild_id)


def withdraw_departed_member(
    db_path: Path, *, guild_id: int, user_id: int, when: float
) -> tuple[int, int]:
    """A member left the server: close out what they leave in Guess Who.

    The same two steps as losing the Guess role — their open rounds are
    flagged ``answer_optout`` (so nobody burns guesses on a round that can no
    longer be won) and their consent row is stamped withdrawn — plus the one
    thing a role loss leaves to the age-out: the cached originals go now,
    because no consent holder is left in the guild. The crop already posted
    stays; the departure auto-anonymise, if it runs, takes the post and the
    rows later and finds nothing left to unlink.

    Returns ``(rounds_flagged, originals_deleted)``. An original that cannot be
    removed is logged and left for the age-out and the orphan sweep, so one
    stuck file never stops the flag and the withdrawal.
    """
    with open_db(db_path) as conn:
        flagged = flag_user_open_rounds_optout(
            conn, guild_id=guild_id, user_id=user_id
        )
        withdraw_guess_consent(
            conn, guild_id=guild_id, user_id=user_id, withdrawn_at=when
        )
    try:
        deleted = delete_member_originals(db_path, user_id, guild_id=guild_id)
    except MediaNotCleared:
        log.warning("guess: a departed member's original could not be deleted")
        deleted = 0
    return flagged, deleted
