"""Departure auto-anonymise: the queue, its guards, and the job that empties it.

Billy, 2026-09-14: *"when someone leaves I want to automatically run delete_me
and anonymize data for them"*. Decisions and their reasons are in
``docs/plans/departure-auto-anonymise.md``; ``docs/specs/privacy_spec.md`` is the
reference.

When a member leaves a guild whose ``departure_auto_anonymise`` dial is on, the
privacy cog's ``on_member_remove`` calls :func:`hold_reason` and, if nothing
holds them back, :func:`enqueue`. After :data:`SETTLE_SECONDS` the cog's worker
hands the row to :func:`process_job`, which

1. deletes every Discord message they posted in that guild (``MODE_ALL``);
2. takes down their Guess Who posts and deletes their cached originals;
3. deletes their Guess records, anonymises the rest, and deletes the queue
   row, in one transaction (:func:`anonymise_member`, which My Data's Delete
   everything runs too).

**Why a queue.** Nine other ``on_member_remove`` listeners look the leaver up by
real id — wager refunds credit the ledger, Pen Pals and Voice Master write rows
— and listener order is undefined. A rewrite inside the event would miss the
refunds and have the real id written back behind it.

**Why the row dies in the anonymise transaction.** It holds the real id. A row
that outlived the rewrite would be the surrogate↔member mapping the
anonymisation exists never to write.

**Failure is loud, never partial.** A deletion pass that raises or leaves any
message undeleted stops the job before anything is anonymised. The row stays,
with ``attempts`` and ``last_error``, and is retried with backoff until
:data:`MAX_ATTEMPTS`, when it parks as ``failed`` for an admin.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import get_config_value, open_db, open_db_immediate
from bot_modules.games.guess import (
    cleanup_service as guess_cleanup_service,
    repo as guess_repo,
)
from bot_modules.moderation.service import get_active_jail, has_live_ticket, write_audit
from bot_modules.breaks.store import delete_break
from bot_modules.privacy.service import (
    _REAL_MEMBER_ID_FLOOR,
    AnonymizeReport,
    anonymize_user_data,
    anonymize_user_guild_data,
    next_surrogate_id,
)

log = logging.getLogger("dungeonkeeper.privacy.departure")

DIAL_KEY = "departure_auto_anonymise"
AUDIT_ACTION = "privacy_auto_anonymise"

#: How long after the leave the job may start. Long enough for every other
#: leave listener to have run, and for an accidental leave to be undone.
SETTLE_SECONDS = 300.0
#: First retry after a failed run; doubles each time.
RETRY_BASE_SECONDS = 1800.0
#: Failed runs before the row parks as ``failed``.
MAX_ATTEMPTS = 5

SCOPE_ALL = "all"
SCOPE_GUILD = "guild"

STATE_PENDING = "pending"
STATE_FAILED = "failed"

# hold_reason() values.
HOLD_BOT = "bot"
HOLD_NOT_A_MEMBER = "not_a_member"
HOLD_DIAL_OFF = "dial_off"
HOLD_JAILED = "jailed"
HOLD_TICKET = "ticket"
# process_job() drop reasons beyond those.
DROP_ROW_GONE = "row_gone"
DROP_REJOINED = "rejoined"


class DepartureError(RuntimeError):
    """A job step that did not complete; the row stays for a retry."""


def is_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    return (
        get_config_value(conn, DIAL_KEY, "0", guild_id, allow_legacy_fallback=False)
        == "1"
    )


def hold_reason(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, is_bot: bool = False
) -> str | None:
    """Why this departure must not be processed, or ``None`` if it may.

    Checked when the member leaves and again when the job runs, so a member
    jailed, ticketed or switched off in between is still held back.

    - A member jailed or on a live ticket (opened, or still a participant, on
      one that is ``open`` or ``closed``) is **kept for good** — the release or
      archive does not re-queue them. Their record is moderation business.
    - An id below the snowflake floor is already a surrogate: someone
      anonymised this member from the dashboard while the job waited.
    """
    if is_bot:
        return HOLD_BOT
    if user_id < _REAL_MEMBER_ID_FLOOR:
        return HOLD_NOT_A_MEMBER
    if not is_enabled(conn, guild_id):
        return HOLD_DIAL_OFF
    return standing_hold(conn, guild_id, user_id)


def standing_hold(conn: sqlite3.Connection, guild_id: int, user_id: int) -> str | None:
    """The holds that are moderation business rather than the departure's own.

    Split out of :func:`hold_reason` so My Data's Delete everything
    (``self_erasure_service``) refuses a jailed or ticketed member without
    reading this job's dial, which says nothing about a member still here.
    """
    if get_active_jail(conn, guild_id, user_id):
        return HOLD_JAILED
    if has_live_ticket(conn, guild_id, user_id):
        return HOLD_TICKET
    return None


# ── The queue ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Job:
    guild_id: int
    user_id: int
    left_at: float
    attempts: int


def enqueue(conn: sqlite3.Connection, guild_id: int, user_id: int, *, now: float) -> None:
    """Queue a departure. Leaving again resets an existing row to a fresh start."""
    conn.execute(
        """
        INSERT INTO departure_erasure_queue
            (guild_id, user_id, left_at, next_at, state, attempts, last_error)
        VALUES (?, ?, ?, ?, 'pending', 0, '')
        ON CONFLICT (guild_id, user_id) DO UPDATE SET
            left_at = excluded.left_at, next_at = excluded.next_at,
            state = 'pending', attempts = 0, last_error = ''
        """,
        (guild_id, user_id, now, now + SETTLE_SECONDS),
    )


def cancel(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    """Drop a queued departure. True if there was one."""
    cur = conn.execute(
        "DELETE FROM departure_erasure_queue WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return cur.rowcount > 0


def clear_queue(conn: sqlite3.Connection, guild_id: int) -> int:
    """Drop every queued departure for a guild — the dial was turned off.

    The worker would drop them one by one at pickup anyway, but only once their
    time came; a row left waiting would be run by a later switch-on, which
    would anonymise someone who left while the server had said it would not.
    """
    cur = conn.execute("DELETE FROM departure_erasure_queue WHERE guild_id = ?", (guild_id,))
    return cur.rowcount


def next_job(conn: sqlite3.Connection, *, now: float) -> Job | None:
    """The pending row that has waited longest past its ``next_at``."""
    row = conn.execute(
        "SELECT guild_id, user_id, left_at, attempts FROM departure_erasure_queue "
        "WHERE state = 'pending' AND next_at <= ? ORDER BY next_at LIMIT 1",
        (now,),
    ).fetchone()
    if row is None:
        return None
    return Job(int(row[0]), int(row[1]), float(row[2]), int(row[3]))


def record_failure(
    conn: sqlite3.Connection, job: Job, error: str, *, now: float
) -> str:
    """Count a failed run and schedule the retry. Returns the row's new state.

    The error text is kept short: it is for an admin to read on the dashboard,
    not a traceback (that goes to the log).
    """
    attempts = job.attempts + 1
    state = STATE_FAILED if attempts >= MAX_ATTEMPTS else STATE_PENDING
    next_at = now + RETRY_BASE_SECONDS * 2 ** (attempts - 1)
    conn.execute(
        "UPDATE departure_erasure_queue SET attempts = ?, last_error = ?, "
        "state = ?, next_at = ? WHERE guild_id = ? AND user_id = ?",
        (attempts, error[:300], state, next_at, job.guild_id, job.user_id),
    )
    return state


def list_jobs(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    """This guild's queue for the dashboard, oldest leave first."""
    rows = conn.execute(
        "SELECT user_id, left_at, next_at, state, attempts, last_error "
        "FROM departure_erasure_queue WHERE guild_id = ? ORDER BY left_at",
        (guild_id,),
    ).fetchall()
    return [
        {
            "user_id": str(r[0]),
            "left_at": float(r[1]),
            "next_at": float(r[2]),
            "state": str(r[3]),
            "attempts": int(r[4]),
            "last_error": str(r[5]),
        }
        for r in rows
    ]


def retry_job(conn: sqlite3.Connection, guild_id: int, user_id: int, *, now: float) -> bool:
    """Put a parked ``failed`` row back in line with a fresh attempt count."""
    cur = conn.execute(
        "UPDATE departure_erasure_queue SET state = 'pending', attempts = 0, "
        "last_error = '', next_at = ? "
        "WHERE guild_id = ? AND user_id = ? AND state = 'failed'",
        (now, guild_id, user_id),
    )
    return cur.rowcount > 0


def choose_scope(guild_id: int, *, primary_guild_id: int, present_elsewhere: bool) -> str:
    """All guilds, or only the one they left.

    All guilds only when the member is in no other guild the bot shares — else
    the rewrite would anonymise them somewhere they still are — **and** they
    left the primary guild. An all-guild rewrite reaches every server's rows,
    and the dashboard already allows that from the primary guild only; a
    second server's dial must not be able to do what its admins cannot.
    """
    if guild_id == primary_guild_id and not present_elsewhere:
        return SCOPE_ALL
    return SCOPE_GUILD


# ── The job ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DepartureHooks:
    """The Discord side of a job, injected so the orchestration is testable."""

    #: Delete the member's messages in the guild; return how many could not be.
    delete_messages: Callable[[int, int], Awaitable[int]]
    #: Whether the member is in the guild now. Raise if that cannot be told.
    is_member: Callable[[int, int], Awaitable[bool]]
    #: Every guild the bot is in now.
    guild_ids: Callable[[], Iterable[int]]
    #: The guild's channel ids, for the channel-keyed tables.
    channel_ids: Callable[[int], Sequence[int]]
    #: Delete one Guess round's post: ``(guild_id, channel_id, message_id)``.
    delete_guess_post: guess_cleanup_service.DeletePost


@dataclass(frozen=True)
class Outcome:
    #: ``done``, ``dropped``, ``retry`` or ``failed``.
    status: str
    #: The scope for ``done``, the reason for ``dropped``, the error otherwise.
    detail: str = ""
    report: AnonymizeReport | None = None


async def process_job(
    db_path: Path,
    job: Job,
    hooks: DepartureHooks,
    *,
    primary_guild_id: int,
    actor_id: int,
    clock: Callable[[], float] = time.time,
) -> Outcome:
    """Run one departure to completion, or leave its row for a retry."""
    try:
        reason = await _why_not(db_path, job, hooks)
        if reason:
            await asyncio.to_thread(_drop, db_path, job)
            return Outcome("dropped", reason)
        failed = await hooks.delete_messages(job.guild_id, job.user_id)
        if failed:
            raise DepartureError(f"{failed} message(s) could not be deleted")
        present_elsewhere = False
        for guild_id in hooks.guild_ids():
            if guild_id != job.guild_id and await hooks.is_member(guild_id, job.user_id):
                present_elsewhere = True
                break
        scope = choose_scope(
            job.guild_id,
            primary_guild_id=primary_guild_id,
            present_elsewhere=present_elsewhere,
        )
        channel_ids = tuple(hooks.channel_ids(job.guild_id))
        # The message scan and the presence sweep are both slow and both talk to
        # Discord. Ask once more here, with nothing irreversible done yet: they
        # may have rejoined, or been jailed, while those ran.
        reason = await _why_not(db_path, job, hooks)
        if reason:
            await asyncio.to_thread(_drop, db_path, job)
            return Outcome("dropped", reason)
        # Guess posts and files first, and outside the transaction below:
        # neither a Discord delete nor an unlink can be rolled back, so they
        # must not sit inside something that can.
        await guess_cleanup_service.clear_member_media(
            db_path,
            job.user_id,
            guild_id=None if scope == SCOPE_ALL else job.guild_id,
            delete_post=hooks.delete_guess_post,
        )
        report = await asyncio.to_thread(
            _finish, db_path, job, scope=scope, channel_ids=channel_ids, actor_id=actor_id
        )
    except Exception as exc:  # noqa: BLE001 - every failure must land on the row
        log.exception("Departure auto-anonymise failed (guild %s)", job.guild_id)
        message = f"{type(exc).__name__}: {exc}"
        state = await asyncio.to_thread(_fail, db_path, job, message, clock())
        return Outcome("failed" if state == STATE_FAILED else "retry", message)
    if report is None:
        return Outcome("dropped", DROP_ROW_GONE)
    return Outcome("done", scope, report)


async def _why_not(db_path: Path, job: Job, hooks: DepartureHooks) -> str | None:
    def _check() -> str | None:
        with open_db(db_path) as conn:
            if not _row_pending(conn, job):
                return DROP_ROW_GONE
            return hold_reason(conn, job.guild_id, job.user_id)

    reason = await asyncio.to_thread(_check)
    if reason:
        return reason
    if await hooks.is_member(job.guild_id, job.user_id):
        return DROP_REJOINED
    return None


def _row_pending(conn: sqlite3.Connection, job: Job) -> bool:
    row = conn.execute(
        "SELECT state FROM departure_erasure_queue WHERE guild_id = ? AND user_id = ?",
        (job.guild_id, job.user_id),
    ).fetchone()
    return row is not None and row[0] == STATE_PENDING


def _drop(db_path: Path, job: Job) -> None:
    with open_db(db_path) as conn:
        cancel(conn, job.guild_id, job.user_id)


def _fail(db_path: Path, job: Job, message: str, now: float) -> str:
    with open_db(db_path) as conn:
        return record_failure(conn, job, message, now=now)


def _finish(
    db_path: Path,
    job: Job,
    *,
    scope: str,
    channel_ids: Sequence[int],
    actor_id: int,
) -> AnonymizeReport | None:
    """The database half, in one write-locked transaction.

    The member's cached photos are already gone by the time this runs — file
    deletions cannot join a transaction that may roll back, so they happen
    before the lock is taken.

    ``open_db_immediate`` as the dashboard route uses it: the surrogate is
    allocated under the write lock, so two anonymisations cannot both read the
    same number as free. The checks are repeated under the lock, because the
    row or the member's status may have changed since the async half looked.
    """
    with open_db_immediate(db_path) as conn:
        if not _row_pending(conn, job):
            return None
        if hold_reason(conn, job.guild_id, job.user_id):
            cancel(conn, job.guild_id, job.user_id)
            return None
        # Before the rewrite, not after: the id sweep would otherwise turn this
        # row into one naming the surrogate, and it must simply be gone.
        cancel(conn, job.guild_id, job.user_id)
        return anonymise_member(
            conn,
            guild_id=job.guild_id,
            user_id=job.user_id,
            scope=scope,
            channel_ids=channel_ids,
            actor_id=actor_id,
            action=AUDIT_ACTION,
        )


def anonymise_member(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    scope: str,
    channel_ids: Sequence[int],
    actor_id: int,
    action: str,
) -> AnonymizeReport:
    """The database half every anonymisation of a member runs, inside the
    caller's write-locked transaction (``open_db_immediate``, so the surrogate
    is allocated under the lock).

    Shared by this job and My Data's Delete everything
    (``self_erasure_service``), so a leaver and a member erasing themselves
    get the same thing. The caller has already taken down the member's Guess
    posts and photos — neither can be rolled back, so neither belongs in here.
    """
    # An open break goes with them (Billy, 2026-09-26: anonymisation wins
    # over a break surviving a long absence). Rewritten, it would leave a
    # surrogate listed as "on a break" holding a dead role list. A member
    # still here has had theirs ended, roles returned, before this runs.
    delete_break(conn, guild_id, user_id)
    # Guess records are deleted rather than rewritten (Billy, 2026-09-27:
    # "deleting the message should clear everything from guess"). Their photo
    # and post are already gone, and a round kept under a surrogate with no
    # picture is not a play record, it is a hole.
    guess_guilds = (
        guess_repo.member_guild_ids(conn, user_id) if scope == SCOPE_ALL else [guild_id]
    )
    for guess_guild in guess_guilds:
        guess_repo.forget_member(conn, guild_id=guess_guild, user_id=user_id)
    surrogate = next_surrogate_id(conn)
    if scope == SCOPE_ALL:
        report = anonymize_user_data(
            conn, user_id, surrogate_id=surrogate, roster_guild_id=guild_id
        )
    else:
        report = anonymize_user_guild_data(
            conn, user_id, guild_id, surrogate_id=surrogate, channel_ids=channel_ids
        )
    # As the dashboard's receipt: the surrogate is safe to record, the real id
    # is what the run removes.
    write_audit(
        conn,
        guild_id=guild_id,
        action=action,
        actor_id=actor_id,
        target_id=None,
        extra={
            "surrogate": report.surrogate_id,
            "scope": scope,
            "rows": report.total_rows,
            "tables": len(report.id_rewrites),
        },
    )
    return report
