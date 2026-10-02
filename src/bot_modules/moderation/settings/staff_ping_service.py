"""Staff job pings — where each one is delivered, and the one way to send one.

A staff ping (``feature_roles.STAFF_ROLES``) is only offered on
the Mod Settings panel while its delivery is configured — a switch for a ping that
can never fire is a preference nothing enforces. :func:`delivered_keys` is the
one answer to "is it configured", so the card and the triggers can't disagree.

Promotion Reviewers rides on the card its feature already posts, in the
promotion-reviews channel. Every other staff ping (:data:`CHANNEL_PINGS`) posts into one staff ping
channel (``staff_ping_channel_id``, set on the dashboard's Moderation page)
through :func:`post_staff_ping`. Spec: ``docs/specs/staff_pings_spec.md``.

**Ships dark.** With the channel unset nothing posts and the Mod Settings panel
offers none of these. A trigger never makes a role: with no role stored, the
stored one deleted, or nobody holding it, the ping is skipped. That last one
matters where a staff ping *replaces* an older notice (tickets): the older
notice still goes out until some moderator has actually opted in.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any, NamedTuple

import discord

from bot_modules.core.db_utils import (
    get_config_int,
    get_config_value,
    get_tz_offset_hours,
    parse_bool,
)
from bot_modules.economy.logic import QOTD_KIND, QOTD_KIND_SPICY, local_day_for, local_hour_ts
from bot_modules.core.utils import role_ping_kwargs
from bot_modules.shared import feature_roles as fr
from bot_modules.chat_revive.service import get_guild_config, list_enabled_channels
from bot_modules.moderation.service import ticket_notify_on_create_enabled
from bot_modules.xp.promotion_review_service import review_channel_id

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bot_modules.core.app_context import AppContext
    from bot_modules.todo.recurring_service import RecurringTask, SpawnResult

log = logging.getLogger("dungeonkeeper.staff_pings")

CHANNEL_KEY = "staff_ping_channel_id"

#: The staff pings delivered into the staff ping channel.
CHANNEL_PINGS: tuple[fr.FeatureRole, ...] = (
    fr.STAFF_TICKET_PING,
    fr.STAFF_CHORES_PING,
    fr.STAFF_REVIVE_PING,
    fr.STAFF_QOTD_PING,
    fr.STAFF_SPICY_QOTD_PING,
    fr.STAFF_GREETING_PING,
)

#: Minimum gap between two pings of one subject in one guild.
COOLDOWN_SECONDS: dict[str, float] = {
    # Every ticket is its own job; a cooldown would swallow the second one.
    fr.STAFF_TICKET_PING.key: 0,
    # One ping per scheduler tick already batches a guild's chores; chores
    # due at different hours are different jobs.
    fr.STAFF_CHORES_PING.key: 0,
    # A dead channel stays dead tick after tick; one nudge per few hours.
    fr.STAFF_REVIVE_PING.key: 3 * 60 * 60,
    # Once a day is enforced by _qotd_warned, not by a clock.
    fr.STAFF_QOTD_PING.key: 0,
    fr.STAFF_SPICY_QOTD_PING.key: 0,
    # Every unanswered hello is its own job, and Greeting Watch resolves a
    # greeting whether or not its ping posts.
    fr.STAFF_GREETING_PING.key: 0,
}

#: ``(guild_id, key) -> last post time``. In memory on purpose: a restart
#: forgetting it costs at most one extra ping. A :class:`Lane` keys its own
#: clock as ``key:lane``.
_last_post: dict[tuple[int, str], float] = {}


class Lane(NamedTuple):
    """A caller's own cooldown clock for a subject, kept apart from the
    subject's :data:`COOLDOWN_SECONDS` clock so neither holds the other back."""

    name: str
    cooldown: float


#: Chat Revive's ping-moderators mode: every confirmed lull, paced by the
#: revive dials (rest, gap, budget). The 3-hour subject cooldown would override
#: those, and sharing its clock would let a lull ping hold back a later
#: can't-fill ping. This lane is only a backstop: the ping posts before the
#: lull is recorded, so a failed record would otherwise re-ping every tick.
REVIVE_LULL_LANE = Lane("lull", 30 * 60)


def staff_channel_id(conn: sqlite3.Connection, guild_id: int) -> int:
    return get_config_int(conn, CHANNEL_KEY, guild_id)


def _has_active_chore(conn: sqlite3.Connection, guild_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM todo_recurring WHERE guild_id = ? AND status = 'active' LIMIT 1",
        (guild_id,),
    ).fetchone()
    return row is not None


def _revive_running(conn: sqlite3.Connection, guild_id: int) -> bool:
    return get_guild_config(conn, guild_id).enabled and bool(
        list_enabled_channels(conn, guild_id)
    )


#: Guild-local hour (0-23) by which a question of the day should be up.
#: Unset (the default) means the QOTD ping is off. One hour for both kinds:
#: the spicy QOTD is due when the regular one is.
QOTD_DEADLINE_KEY = "staff_ping_qotd_deadline_hour"

#: The economy dial naming the spicy QOTD role. Unset means the guild runs no
#: spicy QOTD, so there is nothing for its reminder to watch.
SPICY_ROLE_KEY = "econ_qotd_spicy_role_id"

#: Which staff ping reminds about which kind of question.
QOTD_PINGS: dict[str, fr.FeatureRole] = {
    QOTD_KIND: fr.STAFF_QOTD_PING,
    QOTD_KIND_SPICY: fr.STAFF_SPICY_QOTD_PING,
}


def parse_deadline_hour(raw: object) -> int | None:
    """A stored or submitted deadline hour, or None for unset/garbage."""
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


def qotd_deadline_hour(conn: sqlite3.Connection, guild_id: int) -> int | None:
    return parse_deadline_hour(get_config_value(conn, QOTD_DEADLINE_KEY, "", guild_id))


def _qotd_dial_set(conn: sqlite3.Connection, guild_id: int) -> bool:
    return qotd_deadline_hour(conn, guild_id) is not None


def _spicy_qotd_role(conn: sqlite3.Connection, guild_id: int) -> int:
    # Economy settings are guild-scoped only (load_econ_settings), so no
    # legacy guild-0 fallback here either.
    return get_config_int(conn, SPICY_ROLE_KEY, guild_id, allow_legacy_fallback=False)


def _spicy_qotd_dial_set(conn: sqlite3.Connection, guild_id: int) -> bool:
    """The deadline is set and the guild runs a spicy QOTD at all."""
    return _qotd_dial_set(conn, guild_id) and _spicy_qotd_role(conn, guild_id) > 0


def _greeting_watch_running(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Greeting Watch only records a hello in an enabled guild's watched channels."""
    return parse_bool(
        get_config_value(conn, "greeting_watch_enabled", "false", guild_id)
    ) and any(
        part.strip().isdigit()
        for part in get_config_value(conn, "greeting_watch_channel_ids", "", guild_id).split(",")
    )


#: Per subject, whether the job it pings about can come up at all. A ping
#: whose feature is off would be a switch that never fires.
_JOB_RUNNING = {
    # One switch for ticket notices: "Notify moderators when a ticket is
    # opened" governs the staff ping as well as the DMs it replaces.
    fr.STAFF_TICKET_PING.key: ticket_notify_on_create_enabled,
    fr.STAFF_CHORES_PING.key: _has_active_chore,
    fr.STAFF_REVIVE_PING.key: _revive_running,
    fr.STAFF_QOTD_PING.key: _qotd_dial_set,
    fr.STAFF_SPICY_QOTD_PING.key: _spicy_qotd_dial_set,
    fr.STAFF_GREETING_PING.key: _greeting_watch_running,
}


def _job_running(conn: sqlite3.Connection, guild_id: int, key: str) -> bool:
    check = _JOB_RUNNING.get(key)
    return check is None or check(conn, guild_id)


#: Dispatched after a dashboard save that can change :func:`delivered_keys`;
#: ``ModSettingsCog`` redraws its posted panels on it.
CONFIG_CHANGE_EVENT = "staff_pings_config_change"


def announce_change(bot: Any, guild_id: int) -> None:
    """Tell the bot which staff pings are offered may have changed, so the
    posted Mod Settings panel redraws without a restart. No bot (the web
    server alone, or a test's stand-in) is a no-op: a redraw is never worth
    failing the save that asked for it."""
    dispatch = getattr(bot, "dispatch", None)
    if dispatch is not None:
        dispatch(CONFIG_CHANGE_EVENT, guild_id)


def delivered_keys(conn: sqlite3.Connection, guild_id: int) -> set[str]:
    """Keys of the staff pings that can fire in this guild: their delivery
    channel is set and the feature that raises the job is running."""
    out: set[str] = set()
    if review_channel_id(conn, guild_id):
        out.add(fr.PROMOTION_REVIEW_PING.key)
    if staff_channel_id(conn, guild_id):
        out.update(
            entry.key for entry in CHANNEL_PINGS if _job_running(conn, guild_id, entry.key)
        )
    return out


def cooled_down(last: float | None, now: float, cooldown: float) -> bool:
    """Whether a subject may ping again (never pinged counts as cooled)."""
    return last is None or now - last >= cooldown


def ticket_opened_text(ticket_id: int, channel_id: int) -> str:
    """The staff ticket ping's words. No opener name and no description: the
    ticket channel has both, and this channel may be wider than the ticket."""
    return f"Ticket **#{ticket_id}** was opened: <#{channel_id}>."


#: Most chores one ping names; the rest are counted.
CHORES_LISTED = 10


def spawned_chore_names(
    due: Iterable[RecurringTask], spawned: Iterable[SpawnResult]
) -> dict[int, list[str]]:
    """Per guild, the chores a scheduler pass just put on the board.

    Only a fresh instance counts: a skip (the last one still open) is not a
    new job, and a write-off alone is a miss, not an arrival."""
    names = {task.id: task.task for task in due}
    out: dict[int, list[str]] = {}
    for result in spawned:
        name = names.get(result.recurring_id)
        if result.status == "spawned" and result.todo_id is not None and name:
            out.setdefault(result.guild_id, []).append(name)
    return out


def chores_due_text(names: list[str]) -> str:
    """The staff chores ping's words. Chore names are admin-typed, so they are
    escaped; the mention allow-list already stops them pinging anyone."""
    shown = [f"**{discord.utils.escape_markdown(n)}**" for n in names[:CHORES_LISTED]]
    extra = len(names) - len(shown)
    if extra > 0:
        shown.append(f"{extra} more")
    noun = "chore" if len(names) == 1 else "chores"
    return f"New {noun} on the todo board: {', '.join(shown)}."


REVIVE_NO_QUESTION = "no_question"
REVIVE_SEND_FAILED = "send_failed"


def revive_lull_text(channel_id: int) -> str:
    """The staff Chat Revive ping's words when Chat Revive is set to ping the
    moderators instead of posting: a confirmed lull, and nothing more."""
    return f"<#{channel_id}> has gone quiet — a good moment to start something."


def revive_stuck_text(reason: str, channel_id: int) -> str:
    """The staff Chat Revive ping's words: a lull Chat Revive confirmed but
    couldn't fill, and what a moderator can do about it."""
    if reason == REVIVE_NO_QUESTION:
        return (
            f"<#{channel_id}> has gone quiet, but Chat Revive has no question left "
            "it can use there. Add or re-enable questions on the dashboard, or "
            "start something yourself."
        )
    return (
        f"<#{channel_id}> has gone quiet, but Chat Revive couldn't post there. "
        "Check the bot's permissions in that channel, or start something yourself."
    )


def qotd_overdue(local_hour: int, deadline_hour: int | None, posted_today: bool) -> bool:
    """Whether today's question of the day is late: the deadline is set, has
    passed (guild-local), and nothing has been posted today."""
    return deadline_hour is not None and local_hour >= deadline_hour and not posted_today


def qotd_late_text(kind: str, due_ts: int) -> str:
    """The staff QOTD ping's words. The deadline is a ``<t:…:t>`` timestamp:
    "14:00" in server time reads wrong to every moderator elsewhere."""
    what = "spicy question of the day" if kind == QOTD_KIND_SPICY else "question of the day"
    return (
        f"No {what} has gone up yet today, and it was due by <t:{due_ts}:t>. "
        "Anyone free to post one?"
    )


def greeting_unanswered_text(
    greeter: str, channel_id: int, window_minutes: int, jump: str
) -> str:
    """The staff greeting ping's words. ``greeter`` is a resolved name, never
    a mention: the greeter may not be able to see this channel, or may have
    left by the time the window closes. It arrives markdown-escaped from
    ``build_name_fn``, so it is not escaped again here."""
    return (
        f"{greeter} said hello in <#{channel_id}> about {window_minutes} min ago "
        f"and nobody has answered. {jump}"
    )


#: ``(guild_id, kind) -> local day`` the QOTD ping last went out. In memory
#: like ``_last_post``: a restart after the deadline costs one repeat ping.
_qotd_warned: dict[tuple[int, str], str] = {}

#: After a QOTD ping that couldn't post (nobody opted in, channel unset), when
#: to look again: every tick until midnight would be two reads a minute for a
#: ping that has nowhere to go.
QOTD_RETRY_SECONDS = 15 * 60
_qotd_retry_at: dict[tuple[int, str], float] = {}


def overdue_qotds(
    conn: sqlite3.Connection, now_ts: float
) -> list[tuple[int, str, str, int]]:
    """``(guild_id, kind, local_day, deadline_ts)`` for each question of the
    day that is late and hasn't been pinged about today. The kinds are
    checked apart — a spicy question doesn't stand in for the regular one, or
    the other way round — and the spicy one only where its role is set."""
    rows = conn.execute(
        "SELECT guild_id, value FROM config WHERE key = ? AND guild_id != 0",
        (QOTD_DEADLINE_KEY,),
    ).fetchall()
    out: list[tuple[int, str, str, int]] = []
    for guild_id, raw in rows:
        deadline = parse_deadline_hour(raw)
        if deadline is None:
            continue
        offset = get_tz_offset_hours(conn, guild_id)
        day = local_day_for(now_ts, offset)
        local_hour = int(((now_ts + offset * 3600) // 3600) % 24)
        kinds = [QOTD_KIND]
        if _spicy_qotd_role(conn, guild_id) > 0:
            kinds.append(QOTD_KIND_SPICY)
        for kind in kinds:
            slot = (guild_id, kind)
            if _qotd_warned.get(slot) == day or _qotd_retry_at.get(slot, 0.0) > now_ts:
                continue
            posted = conn.execute(
                "SELECT 1 FROM econ_qotd"
                " WHERE guild_id = ? AND local_day = ? AND kind = ? LIMIT 1",
                (guild_id, day, kind),
            ).fetchone()
            if qotd_overdue(local_hour, deadline, posted is not None):
                out.append((guild_id, kind, day, local_hour_ts(day, offset, deadline)))
    return out


async def ping_overdue_qotd(ctx: AppContext, bot: discord.Client, now_ts: float) -> None:
    """The staff QOTD pings, once per guild-local day per kind. A day is
    marked only when the ping posts, so a moderator who opts in after the
    deadline still hears about today — within :data:`QOTD_RETRY_SECONDS`."""

    def _read() -> list[tuple[int, str, str, int]]:
        with ctx.open_db() as conn:
            return overdue_qotds(conn, now_ts)

    for guild_id, kind, day, due_ts in await asyncio.to_thread(_read):
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        slot = (guild_id, kind)
        if await post_staff_ping(ctx, guild, QOTD_PINGS[kind], qotd_late_text(kind, due_ts)):
            _qotd_warned[slot] = day
            _qotd_retry_at.pop(slot, None)
        else:
            _qotd_retry_at[slot] = now_ts + QOTD_RETRY_SECONDS


def _held(role: discord.Role | None) -> bool:
    """A role someone other than a bot holds. An unheld staff role pings
    nobody, so a ping that *replaces* an older one waits for this."""
    return role is not None and any(not m.bot for m in role.members)


def _targets(conn: sqlite3.Connection, guild_id: int, key: str) -> tuple[int, int]:
    """``(channel id, role id)`` for one staff ping, 0 for either when unset
    or when the job it pings about isn't running."""
    if not _job_running(conn, guild_id, key):
        return 0, 0
    return staff_channel_id(conn, guild_id), get_config_int(conn, key, guild_id)


async def post_staff_ping(
    ctx: AppContext,
    guild: discord.Guild,
    entry: fr.FeatureRole,
    text: str,
    *,
    now: float | None = None,
    lane: Lane | None = None,
) -> bool:
    """Mention ``entry``'s role in the staff ping channel with ``text``.

    True when it posted. Skipped — False — when the job isn't running, the
    channel or the role isn't set, either has been deleted, nobody holds the
    role, the subject pinged within its cooldown, or Discord refused. Never
    raises: a staff ping is a courtesy on top of the feature, which must carry
    on regardless (a caller that sits before its own bookkeeping, like a todo-board tick
    loop, would otherwise repeat its own work every tick).

    ``lane`` swaps the subject's :data:`COOLDOWN_SECONDS` clock for the
    caller's own (:data:`REVIVE_LULL_LANE`).
    """
    if entry not in CHANNEL_PINGS:
        raise ValueError(f"{entry.key} is not delivered to the staff ping channel")

    def _read() -> tuple[int, int]:
        with ctx.open_db() as conn:
            return _targets(conn, guild.id, entry.key)

    try:
        channel_id, role_id = await asyncio.to_thread(_read)
    except Exception:
        log.exception("staff ping %s: reading its setup failed in guild %s", entry.key, guild.id)
        return False
    if not channel_id or not _held(guild.get_role(role_id) if role_id else None):
        return False
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return False
    stamp = time.time() if now is None else now
    if lane is None:
        slot, gap = (guild.id, entry.key), COOLDOWN_SECONDS.get(entry.key, 0)
    else:
        slot, gap = (guild.id, f"{entry.key}:{lane.name}"), lane.cooldown
    if not cooled_down(_last_post.get(slot), stamp, gap):
        return False
    kwargs = role_ping_kwargs([role_id])
    kwargs["content"] = f"{kwargs['content']} {text}".strip()
    try:
        await channel.send(**kwargs)
    except discord.HTTPException:
        log.warning("staff ping %s failed in guild %s", entry.key, guild.id, exc_info=True)
        return False
    _last_post[slot] = stamp
    return True


async def ping_ticket_opened(
    ctx: AppContext, guild: discord.Guild, *, ticket_id: int, channel_id: int
) -> bool:
    """The staff ticket ping, from every path that opens a ticket. True when
    it posted — the caller's cue to skip the DMs it replaces."""
    return await post_staff_ping(
        ctx, guild, fr.STAFF_TICKET_PING, ticket_opened_text(ticket_id, channel_id)
    )
