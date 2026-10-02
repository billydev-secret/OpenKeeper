"""Scheduled games: auto-launch party games at set times.

Three layers live here:
  * Pure time math — ``compute_next_run`` (unit-tested, no I/O).
  * Sync CRUD over a sqlite3 connection — used by the web route via ``run_query``.
  * The async polling loop ``scheduled_games_loop`` — registered as a bot startup task.

Wall-clock fields (``time_of_day`` minutes, ``recur_days``, ``start_date``) are the
source of truth; ``next_run_at`` is a derived UTC-epoch cache the loop polls. The
guild's fixed ``tz_offset_hours`` defines local time (no DST — matches the rest of
the bot, see ``db_utils.get_tz_offset_hours``).

A due slot is also skipped while the **feature rotation** has its target channel
hidden. Announcing is deliberately downstream of a successful launch, so that
one check both stops a game posting into a room nobody can open and stops the
role ping that would have advertised it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta

import discord

from bot_modules.core.db_utils import fixed_offset_tz, open_db
from bot_modules.core.utils import jump_url, resolve_bot_channel
from bot_modules.feature_rotation.store import is_hidden_by_rotation
from bot_modules.games.constants import (
    GAME_NAMES,
    LOBBY_GAME_TYPES,
    SCHEDULABLE_GAME_TYPES,
    SCHEDULE_BASE_GAME_TYPE,
)
from bot_modules.games.utils.game_manager import (
    check_game_enabled,
    get_active_game,
    get_active_game_by_id,
    resolve_name,
)
from bot_modules.ping_tracker import service as ping_tracker_service
from bot_modules.games.start_ping_service import (
    SCHEDULED_AUTO_START_MINUTES,
    auto_starter_for,
    claim_game_lobby_ping,
    mark_start_ping_sent,
    resolve_start_epoch,
    send_start_ping,
)

log = logging.getLogger(__name__)

# How long after a schedule's slot we keep retrying past a busy channel before
# giving up on that slot. A one-time row is marked missed; a recurring row
# rolls to its next slot. Before 2026-09-04 only one-time rows retried — a
# recurring row rolled the moment the channel was busy, so a member-opened
# round overlapping the slot by a minute cost the whole day (platform-24:
# prod's daily Risky Rolls was skipped on 09-02 for a round that closed 21
# minutes later).
GIVEUP_GRACE_SECONDS = 2 * 3600

VALID_RECURRENCE = ("once", "daily", "weekly")


# ── Announcement copy ───────────────────────────────────────────────────────

def build_launch_announcement(
    game_type: str,
    *,
    role_id: int | None,
    guild_id: int,
    channel_id: int,
    message_id: int | None,
) -> str:
    """The "it's starting" post, pointing at the board rather than the room.

    Built after the launcher has returned, which is the only reason a
    ``message_id`` exists to link at — see ``_process_due``. Landing someone in
    the channel and leaving them to find the game among whatever else is there
    was the complaint this answers (todo #97).

    ``message_id`` is None for the games that keep their state in memory and
    never register a ``games_active_games`` row (risky_roll). Those keep the
    bare line: a link needs a message, and half a URL is worse than none.
    """
    label = GAME_NAMES.get(game_type) or game_type.replace("_", " ").title()
    prefix = f"<@&{role_id}> " if role_id else ""
    line = f"{prefix}🎮 **{label}** is starting now!"
    if message_id is None:
        return line
    return f"{line}\n{jump_url(guild_id, channel_id, message_id)}"


async def _record_launch_ping(
    games_db,
    *,
    message_id: int,
    guild_id: int,
    channel_id: int,
    author_id: int,
    role_id: int,
    game_id: str,
    ts: float,
) -> None:
    """Log the launch announcement's role ping against the game it launched."""

    def _write() -> None:
        with open_db(games_db.db_path) as conn:
            ping_tracker_service.record_game_start_ping(
                conn,
                message_id=message_id,
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                role_ids=[role_id],
                game_id=game_id,
                ts=ts,
            )

    await asyncio.to_thread(_write)


# ── Pure time math ──────────────────────────────────────────────────────────

def _local_to_epoch(local_naive: datetime, offset_hours: float) -> float:
    """Convert a guild-local datetime to a UTC epoch (local = UTC + offset).

    ``local_naive``'s year/month/day/hour/... fields are read as guild-local
    wall-clock time — the guild's fixed offset (never UTC; this repo has no
    per-guild IANA zone) is what says what absolute instant they name, so it
    is stamped on as an explicit ``timezone`` here rather than subtracted by
    hand. ``offset_hours`` is authoritative: any tzinfo already on the input
    (callers that build it via ``_epoch_to_local`` pass one through) is
    replaced with the equivalent one, not trusted blindly.
    """
    return local_naive.replace(tzinfo=fixed_offset_tz(offset_hours)).timestamp()


def _epoch_to_local(epoch: float, offset_hours: float) -> datetime:
    """Convert a UTC epoch to a guild-local datetime, tagged with the guild's fixed offset.

    Aware rather than naive, but every caller in this codebase (here and in
    ``todo_recurring_service``/``todo.change_logic``, which borrow this pair
    so the day boundary can't disagree with the scheduler's) only ever reads
    ``.date()`` off the result, and that reads the same wall-clock fields
    either way.
    """
    return datetime.fromtimestamp(epoch, tz=fixed_offset_tz(offset_hours))


def compute_next_run(
    *,
    now_utc: float,
    offset_hours: float,
    recurrence: str,
    time_of_day_min: int,
    recur_days: list[int] | None = None,
    start_date: str | None = None,
    after: float | None = None,
) -> float | None:
    """Return the next fire time as a UTC epoch.

    For 'daily'/'weekly' the result is strictly greater than ``max(now_utc, after)``,
    so a row that fired (or was skipped) advances past many missed occurrences to the
    next future slot — at-most-once on recovery. For 'once' it returns the single
    slot's epoch (which may be in the past, i.e. fire-late); the loop never advances a
    one-time row. Returns None for weekly with no selected days, or a malformed once.
    """
    thr = now_utc if after is None else max(now_utc, after)
    tod = timedelta(minutes=int(time_of_day_min))
    # The wall-clock slot below is a human-entered local time (a schedule's
    # ``time_of_day``/``start_date``), never UTC — this guild's own fixed
    # offset is the only timezone in scope for it, so that's what tags it.
    tz = fixed_offset_tz(offset_hours)

    if recurrence == "once":
        if not start_date:
            return None
        d = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
        slot = d + tod
        return _local_to_epoch(slot, offset_hours)

    if recurrence == "daily":
        local_thr = _epoch_to_local(thr, offset_hours)
        base = local_thr.date()
        slot = datetime(base.year, base.month, base.day, tzinfo=tz) + tod
        if _local_to_epoch(slot, offset_hours) <= thr:
            slot = slot + timedelta(days=1)
        return _local_to_epoch(slot, offset_hours)

    if recurrence == "weekly":
        if not recur_days:
            return None
        days = {int(x) for x in recur_days}
        base = _epoch_to_local(thr, offset_hours).date()
        for i in range(8):
            d = base + timedelta(days=i)
            if d.weekday() in days:
                slot = datetime(d.year, d.month, d.day, tzinfo=tz) + tod
                epoch = _local_to_epoch(slot, offset_hours)
                if epoch > thr:
                    return epoch
        return None

    return None


# ── Offset readers ──────────────────────────────────────────────────────────

def _parse_offset(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def get_offset_hours_async(games_db, guild_id: int) -> float:
    """Async mirror of db_utils.get_tz_offset_hours, over the GamesDb wrapper."""
    row = await games_db.fetchone(
        "SELECT value FROM config WHERE guild_id = ? AND key = 'tz_offset_hours'",
        (guild_id,),
    )
    if row is None and guild_id != 0:
        row = await games_db.fetchone(
            "SELECT value FROM config WHERE guild_id = 0 AND key = 'tz_offset_hours'"
        )
    if row is None:
        return 0.0
    return _parse_offset(row[0])


# ── Sync CRUD (web route, via run_query) ────────────────────────────────────

_INSERT_COLS = (
    "guild_id", "channel_id", "game_type", "options", "created_by", "created_at",
    "time_of_day", "recurrence", "recur_days", "start_date", "next_run_at",
    "giveup_at", "announce", "announce_role_id",
)

_UPDATABLE_COLS = {
    "channel_id", "game_type", "options", "time_of_day", "recurrence", "recur_days",
    "start_date", "next_run_at", "giveup_at", "announce", "announce_role_id", "status",
}


def create_scheduled(conn: sqlite3.Connection, **fields) -> int:
    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    cols = ", ".join(_INSERT_COLS)
    cur = conn.execute(
        f"INSERT INTO games_scheduled ({cols}, status) VALUES ({placeholders}, 'active')",
        tuple(fields[c] for c in _INSERT_COLS),
    )
    return int(cur.lastrowid or 0)


def list_scheduled(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM games_scheduled WHERE guild_id = ? "
        "ORDER BY (status='active') DESC, next_run_at ASC",
        (guild_id,),
    ).fetchall()


def get_scheduled(conn: sqlite3.Connection, sched_id: int, guild_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM games_scheduled WHERE id = ? AND guild_id = ?",
        (sched_id, guild_id),
    ).fetchone()


def update_scheduled(conn: sqlite3.Connection, sched_id: int, guild_id: int, fields: dict) -> None:
    cols = [c for c in fields if c in _UPDATABLE_COLS]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    params = [fields[c] for c in cols] + [sched_id, guild_id]
    conn.execute(
        f"UPDATE games_scheduled SET {assignments} WHERE id = ? AND guild_id = ?",
        params,
    )


def delete_scheduled(conn: sqlite3.Connection, sched_id: int, guild_id: int) -> None:
    conn.execute(
        "DELETE FROM games_scheduled WHERE id = ? AND guild_id = ?", (sched_id, guild_id)
    )


def set_status(conn: sqlite3.Connection, sched_id: int, guild_id: int, status: str) -> None:
    conn.execute(
        "UPDATE games_scheduled SET status = ? WHERE id = ? AND guild_id = ?",
        (status, sched_id, guild_id),
    )


# ── Async polling loop ──────────────────────────────────────────────────────

# The loop's writes land only on the row as the poll read it. Between that
# SELECT and each write there are awaits (channel fetch, enabled check,
# rotation read), and a dashboard pause, edit or delete made in the gap has to
# win — not be overwritten, and not be launched over.
_UNCHANGED = "id=? AND status='active' AND next_run_at=?"


def _unchanged(row) -> tuple:
    return (row["id"], row["next_run_at"])


async def _advance_or_finish(games_db, row, now: float, last_status: str,
                             offset: float, recur_days) -> bool:
    """Recurring → roll next_run_at past now; once → mark done. Record last_run/status.

    Returns False when the row changed since the poll read it, in which case
    nothing was written and the caller must not act on it.
    """
    if row["recurrence"] == "once":
        nxt = None
    else:
        nxt = compute_next_run(
            now_utc=now, offset_hours=offset, recurrence=row["recurrence"],
            time_of_day_min=row["time_of_day"], recur_days=recur_days,
            start_date=row["start_date"], after=now,
        )
    if nxt is None:
        cur = await games_db.execute(
            "UPDATE games_scheduled SET status='done', next_run_at=NULL, "
            f"last_run_at=?, last_status=? WHERE {_UNCHANGED}",
            (now, last_status, *_unchanged(row)),
        )
    else:
        cur = await games_db.execute(
            "UPDATE games_scheduled SET next_run_at=?, last_run_at=?, last_status=? "
            f"WHERE {_UNCHANGED}",
            (nxt, now, last_status, *_unchanged(row)),
        )
    return cur.rowcount > 0



#: Only the claim writes this, and only for the moment a launch is in flight.
#: It is what lets :func:`_unclaim` recognise its own claim and nobody else's.
_LAUNCHING = "launching"

#: The row must still be exactly as the claim left it. ``last_run_at`` pins it
#: to *this* claim rather than some earlier one, so a row that was paused,
#: edited or relaunched in between is left alone instead of resurrected.
_STILL_CLAIMED = (
    "id=? AND status='done' AND next_run_at IS NULL "
    f"AND last_status='{_LAUNCHING}' AND last_run_at=?"
)


async def _unclaim_once(games_db, row, now: float, last_status: str) -> bool:
    """Give a one-time row back its slot after a launch that never happened.

    Step 4 claims the row *before* awaiting the launcher, so a crash mid-launch
    cannot double-fire on restart. That is the right trade for a crash, where
    nobody is left to tidy up — but it also meant a launch that failed and
    *said so* consumed the booking anyway. For a recurring row that costs one
    slot and the next one comes; for a one-time row it is the whole occurrence,
    gone with nothing in the channel and nothing on the schedule.

    Restoring is safe precisely because it only ever runs on a failure we
    caught. A crash still leaves the row claimed, so the double-fire the claim
    exists to prevent is still prevented. And a retry cannot double-post either:
    step 3's busy-check sees any board the failed launcher did manage to leave
    behind and stands down.

    Deliberately not done for recurring rows — see
    ``test_launcher_returning_none_records_error``. There, rolling to the next
    slot *is* the recovery, and re-arming the same slot would retry every 60s
    for two hours over a game that runs again tomorrow anyway.
    """
    if row["recurrence"] != "once":
        return False
    cur = await games_db.execute(
        f"UPDATE games_scheduled SET status='active', next_run_at=?, last_status=? "
        f"WHERE {_STILL_CLAIMED}",
        (row["next_run_at"], last_status, row["id"], now),
    )
    if cur.rowcount > 0:
        log.info(
            "Scheduled game %s: launch failed, slot restored — the poll retries "
            "until giveup_at", row["id"],
        )
        return True
    return False


async def _rotation_hidden(bot, guild_id: int, channel_id: int) -> bool:
    """Is the feature rotation currently hiding this channel? Never raises.

    Best-effort on purpose: the rotation is an optional feature and a database
    hiccup reading it must not stop a scheduled game launching. Failing open
    keeps today's behaviour, which is the safe direction — the cost of a wrong
    ``False`` is one game posted into a hidden room, the cost of a wrong
    ``True`` is a game that silently never runs.
    """
    ctx = getattr(bot, "ctx", None)
    if ctx is None:
        return False

    def _read() -> bool:
        with ctx.open_db() as conn:
            return is_hidden_by_rotation(conn, guild_id, channel_id)

    try:
        return await asyncio.to_thread(_read)
    except Exception:
        log.exception("Scheduled games: rotation visibility check failed")
        return False


async def _process_due(bot, games_db, row, now: float) -> None:
    sched_id = row["id"]
    guild_id = row["guild_id"]
    channel_id = row["channel_id"]
    game_type = row["game_type"]
    offset = await get_offset_hours_async(games_db, guild_id)
    recur_days = json.loads(row["recur_days"]) if row["recur_days"] else None

    # 0. Lateness guard (mirrors announcements' MAX_LATE_SECONDS). If the bot was
    #    offline through a slot, firing now would ping "starting now!" for a game
    #    whose window closed hours ago. A one-time row uses its giveup_at deadline
    #    (skip + mark missed); a recurring row skips this stale slot and rolls to
    #    the next one rather than launching late.
    if row["recurrence"] == "once":
        deadline = row["giveup_at"]
        too_late = deadline is not None and now >= deadline
    else:
        too_late = (now - row["next_run_at"]) > GIVEUP_GRACE_SECONDS
    if too_late:
        if row["recurrence"] == "once":
            await games_db.execute(
                "UPDATE games_scheduled SET status='done', next_run_at=NULL, "
                f"last_run_at=?, last_status='skipped_giveup' WHERE {_UNCHANGED}",
                (now, *_unchanged(row)),
            )
        else:
            # A recurring row that spent this slot retrying past a busy
            # channel rolls as 'skipped_active', so the panel blames the room
            # and not an outage. Only a retry recorded *within* this slot
            # counts — yesterday's status must not relabel today's downtime.
            retried_this_slot = (
                row["last_status"] == "skipped_active"
                and row["last_run_at"] is not None
                and row["last_run_at"] >= row["next_run_at"]
            )
            status = "skipped_active" if retried_this_slot else "skipped_late"
            await _advance_or_finish(games_db, row, now, status, offset, recur_days)
        return

    # 1. Resolve the target channel.
    channel = await resolve_bot_channel(bot, channel_id)
    if channel is None:
        log.warning("Scheduled game %s: channel %s unreachable", sched_id, channel_id)
        await _advance_or_finish(games_db, row, now, "error", offset, recur_days)
        return

    # 2. Re-check the game is still enabled (scheduler bypasses the slash guards).
    #    Display variants (e.g. ffa_banner) honor their base game's toggle.
    enable_type: str = SCHEDULE_BASE_GAME_TYPE.get(game_type) or str(game_type)
    if not await check_game_enabled(games_db, enable_type, guild_id):
        await _advance_or_finish(games_db, row, now, "skipped_disabled", offset, recur_days)
        return

    # 2.5 Skip if the feature rotation currently has this room hidden. Launching
    #    into a channel members can't open would post a game nobody can reach —
    #    and because the announcement below only fires after a successful
    #    launch, skipping here also stops the role ping about it, which is the
    #    "notification that opens nothing" papercut the rotation design flagged.
    #    Reads the observed hidden state, so a guild with the rotation off, or a
    #    channel outside its pool, is never gated.
    if await _rotation_hidden(bot, guild_id, channel_id):
        if row["recurrence"] == "once":
            # Stay due — the room reopens on its next featured day, and the
            # lateness guard above is what eventually gives up.
            await games_db.execute(
                f"UPDATE games_scheduled SET last_status='skipped_hidden' WHERE {_UNCHANGED}",
                _unchanged(row),
            )
            return
        await _advance_or_finish(games_db, row, now, "skipped_hidden", offset, recur_days)
        return

    # 3. Skip if the channel already has an active game. Most games register in the
    #    games_active_games table; some (e.g. risky_roll) track rounds in-memory and
    #    register an optional busy-check so we can see them too — otherwise we'd ping
    #    "starting now!" and then fail to launch a duplicate. Let the running game ride.
    busy = await get_active_game(games_db, channel_id) is not None
    if not busy:
        busy_check = getattr(bot, "game_busy_checks", {}).get(game_type)
        if busy_check is not None:
            try:
                busy = bool(await busy_check(channel_id))
            except Exception:
                log.exception("Scheduled game %s: busy-check for %s raised", sched_id, game_type)
    if busy:
        # Stay due — the 60s poll is the retry. A one-time row retries until
        # giveup_at, a recurring one until the slot is GIVEUP_GRACE_SECONDS
        # old; the lateness guard above is what gives up either way. The
        # retry is recorded as a run (last_run_at) so that guard can tell a
        # slot spent waiting on the room from one the bot slept through.
        await games_db.execute(
            "UPDATE games_scheduled SET last_status='skipped_active', last_run_at=? "
            f"WHERE {_UNCHANGED}",
            (now, *_unchanged(row)),
        )
        return

    launcher = bot.game_launchers.get(game_type) if hasattr(bot, "game_launchers") else None
    if launcher is None:
        log.error("Scheduled game %s: no launcher for game_type=%s", sched_id, game_type)
        await _advance_or_finish(games_db, row, now, "error", offset, recur_days)
        return

    # 4. Claim before launch: advance/finish state BEFORE awaiting the launch so a crash
    #    mid-launch can't double-fire on restart. A lost claim means the row was
    #    paused, rescheduled or deleted after the poll read it: don't launch.
    if not await _advance_or_finish(games_db, row, now, _LAUNCHING, offset, recur_days):
        log.info("Scheduled game %s: changed since the poll read it; not launching", sched_id)
        return

    guild = bot.get_guild(guild_id)
    host_name = resolve_name(guild, row["created_by"]) if guild else "Scheduled Game"
    try:
        options = json.loads(row["options"] or "{}")
    except (json.JSONDecodeError, TypeError):
        options = {}

    # ``scheduled`` tells the launcher nobody is at the keyboard: the round
    # games unlock Next for any voter after the round timer so an absent
    # schedule creator cannot stall the board on round 1 (platform-23).
    options = {**options, "scheduled": True}

    # A lobby game that can start itself (clapback-8) is given a countdown
    # when the schedule names none, so a scheduled row actually produces a
    # played game rather than a lobby waiting on a press nobody will make.
    auto_starts = game_type in LOBBY_GAME_TYPES and auto_starter_for(bot, game_type) is not None
    if auto_starts and resolve_start_epoch(options) is None:
        options["start_in"] = SCHEDULED_AUTO_START_MINUTES

    try:
        gid = await launcher(
            channel=channel,
            host_id=row["created_by"],
            host_name=host_name,
            guild_id=guild_id,
            options=options,
        )
    except Exception:
        log.exception("Scheduled game %s: launcher %s raised", sched_id, game_type)
        if not await _unclaim_once(games_db, row, now, "error"):
            await games_db.execute(
                "UPDATE games_scheduled SET last_status='error' WHERE id=?", (sched_id,)
            )
        return

    # Launchers return None on failure (e.g. missing send perms, caught internally),
    # so a falsy result is a real failure — don't mislabel it as launched.
    if gid:
        # last_launched_at is the one column that says when this schedule last
        # produced a game — last_status alone read 'skipped' for days with
        # nothing to show how long that had been going on (platform-24).
        await games_db.execute(
            "UPDATE games_scheduled SET last_status='launched', last_launched_at=? WHERE id=?",
            (now, sched_id),
        )
        log.info("Scheduled game %s launched: %s in channel %s", sched_id, game_type, channel_id)

        # Announce *after* the launch, not before. The board has to exist
        # before there is anything to link at, and announcing first also meant
        # a launch that then failed had already pinged a role about a game
        # nobody would find (todo #97). Every DB-backed launcher writes
        # message_id before returning, so the row is readable by now.
        # The schedule's own announcement stands in for the platform's Game
        # Night ping, and claims it before the send so the start-ping sweep
        # can't slip a second line in between. The claim can be *lost*: the
        # sweep runs on its own 15s clock and may have pinged this lobby in
        # the moments between the board appearing and this line — in which
        # case the room has already been called and the schedule stays quiet
        # rather than pinging twice for one game opening.
        if row["announce"] and await claim_game_lobby_ping(games_db, gid, no_row_wins=True):
            board = await get_active_game_by_id(games_db, gid)
            message_id = board["message_id"] if board else None
            role_id = row["announce_role_id"]
            try:
                announcement = await channel.send(
                    build_launch_announcement(
                        game_type,
                        role_id=role_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        message_id=int(message_id) if message_id else None,
                    ),
                    # Allow-list the one role the schedule names; never let the
                    # rendered text decide who gets pinged (embed_style_guide).
                    allowed_mentions=(
                        discord.AllowedMentions(roles=[discord.Object(id=int(role_id))])
                        if role_id
                        else discord.AllowedMentions.none()
                    ),
                    # The board is one message up in this same channel — a link
                    # preview of it would just be the game twice.
                    suppress_embeds=True,
                )
            except Exception:  # noqa: BLE001 — announce is best-effort after a launch that already succeeded; must not fail the scheduled launch
                log.warning("Scheduled game %s: announce failed in channel %s", sched_id, channel_id)
            else:
                # Tie the ping to the game it announced. The generic ingest
                # path will also see this message, but all it can tell is "a
                # bot pinged a role" — only here do we know *which game*, and
                # that link is what lets the Ping Response report say how many
                # people actually joined rather than only how many talked.
                # Never allowed to break a launch that already succeeded.
                if role_id:
                    try:
                        await _record_launch_ping(
                            games_db,
                            message_id=announcement.id,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            author_id=announcement.author.id,
                            role_id=int(role_id),
                            game_id=gid,
                            ts=announcement.created_at.timestamp(),
                        )
                    except Exception:  # noqa: BLE001 — never allowed to break a launch that already succeeded (see comment above)
                        log.warning(
                            "Scheduled game %s: ping tracking failed", sched_id,
                            exc_info=True,
                        )

        # A lobby game posts its lobby and waits for a human to press start —
        # nobody would otherwise know that's pending. Nudge the person who
        # scheduled it, right after the lobby so the nudge sits next to the
        # button. Lobby-less games self-run, so they get nothing — and so does
        # a lobby game that starts itself at its countdown: the sweep nudges
        # only if the roster is short when the moment comes.
        if game_type in LOBBY_GAME_TYPES and not auto_starts:
            # Record it against the launched game before sending: a schedule
            # whose stored options carry `start_in` also stamps a start_epoch,
            # which the poll loop would otherwise nudge for a second time.
            await mark_start_ping_sent(games_db, gid)
            await send_start_ping(channel, game_type, row["created_by"])
    elif not await _unclaim_once(games_db, row, now, "error"):
        await games_db.execute(
            "UPDATE games_scheduled SET last_status='error' WHERE id=?", (sched_id,)
        )
        log.warning("Scheduled game %s: launcher %s returned no game (no perms or channel busy)", sched_id, game_type)


async def scheduled_games_loop(bot) -> None:
    """Poll every 60s and fire due schedules. Registered as a bot startup task."""
    await bot.wait_until_ready()
    games_db = bot.games_db

    # Coverage check: every schedulable game must have a registered launcher.
    launchers = getattr(bot, "game_launchers", {})
    missing = [g for g in SCHEDULABLE_GAME_TYPES if g not in launchers]
    if missing:
        log.error(
            "Scheduled games: %d schedulable game(s) have no launcher and will fail "
            "to auto-launch: %s", len(missing), ", ".join(missing),
        )


    while not bot.is_closed():
        try:
            now = time.time()
            rows = await games_db.fetchall(
                "SELECT * FROM games_scheduled "
                "WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= ?",
                (now,),
            )
            for row in rows:
                try:
                    await _process_due(bot, games_db, row, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("scheduled game %s failed to process", row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled_games_loop iteration error")
        await asyncio.sleep(60)
