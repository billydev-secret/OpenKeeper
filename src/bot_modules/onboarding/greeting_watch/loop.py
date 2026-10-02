"""Greeting Watch monitor loop.

Registered as a startup task factory (see ``__main__.py``); one ~1-minute tick
finds greetings whose unanswered-window has closed and, for any where the
greeter was never acknowledged, posts the Staff · Greetings ping
(``docs/specs/staff_pings_spec.md``). Every SQLite touch runs in
``asyncio.to_thread`` so nothing here blocks the event loop.

Config (``greeting_watch_*`` keys) is read straight from the DB each tick, so an
admin toggling it on the dashboard takes effect on the next sweep without a
restart or cache invalidation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from bot_modules.core.db_utils import get_config_value, open_db, parse_bool
from bot_modules.core.utils import jump_url
from bot_modules.moderation.settings.staff_ping_service import (
    greeting_unanswered_text,
    post_staff_ping,
)
from bot_modules.shared import feature_roles as fr
from bot_modules.shared.name_resolver import build_name_fn
from bot_modules.core.background import run_forever
from bot_modules.onboarding.greeting_watch.service import (
    PendingGreeting,
    guilds_with_pending,
    is_acknowledged,
    list_due_greetings,
    mark_resolved,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.greeting_watch")

TICK_SECONDS = 60.0
DEFAULT_WINDOW_MINUTES = 10


# ── sync wrappers (run via asyncio.to_thread) ──────────────────────────


def _load_settings(db_path: Path, guild_id: int) -> tuple[bool, int]:
    """Return (enabled, window_minutes) for a guild."""
    with open_db(db_path) as conn:
        enabled = parse_bool(
            get_config_value(conn, "greeting_watch_enabled", "false", guild_id)
        )
        try:
            window = max(
                1,
                int(
                    get_config_value(
                        conn,
                        "greeting_watch_window_minutes",
                        str(DEFAULT_WINDOW_MINUTES),
                        guild_id,
                    )
                ),
            )
        except (TypeError, ValueError):
            window = DEFAULT_WINDOW_MINUTES
    return enabled, window


def _guilds_with_pending_sync(db_path: Path) -> list[int]:
    with open_db(db_path) as conn:
        return guilds_with_pending(conn)


def _due_sync(db_path: Path, guild_id: int, cutoff_ts: int) -> list[PendingGreeting]:
    with open_db(db_path) as conn:
        return list_due_greetings(conn, guild_id, cutoff_ts)


def _acked_sync(db_path: Path, guild_id: int, message_id: int) -> bool:
    with open_db(db_path) as conn:
        return is_acknowledged(conn, guild_id, message_id)


def _resolve_sync(
    db_path: Path, guild_id: int, message_ids: list[int], outcome: str, now_ts: int
) -> None:
    with open_db(db_path) as conn:
        for mid in message_ids:
            mark_resolved(conn, guild_id, mid, outcome, now_ts)


# ── the monitor ────────────────────────────────────────────────────────


async def _notify(
    bot: Bot,
    db_path: Path,
    guild_id: int,
    g: PendingGreeting,
    window_minutes: int,
) -> bool:
    """Ping staff about one unanswered greeting. True when the ping posted;
    False covers every reason it can't (no staff channel, nobody opted in)."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    # The greeter may have left by the time the window closes; known_users
    # still names them, where a <@id> would show digits.
    name_fn = await build_name_fn(
        guild=guild, db_path=db_path, guild_id=guild_id, user_ids=[g.author_id]
    )
    text = greeting_unanswered_text(
        name_fn(g.author_id),
        g.channel_id,
        window_minutes,
        jump_url(guild_id, g.channel_id, g.message_id),
    )
    return await post_staff_ping(bot.ctx, guild, fr.STAFF_GREETING_PING, text)


async def _process_guild(
    bot: Bot, db_path: Path, guild_id: int, now_ts: float
) -> None:
    enabled, window_minutes = await asyncio.to_thread(
        _load_settings, db_path, guild_id
    )
    window_seconds = window_minutes * 60
    cutoff = int(now_ts - window_seconds)
    due = await asyncio.to_thread(_due_sync, db_path, guild_id, cutoff)
    if not due:
        return

    # Turned off mid-window: retire the stragglers quietly so they don't
    # linger unresolved forever.
    if not enabled:
        await asyncio.to_thread(
            _resolve_sync,
            db_path,
            guild_id,
            [g.message_id for g in due],
            "skipped",
            int(now_ts),
        )
        return

    for g in due:
        # Ingest stamped the row when someone answered the greeter in the
        # channel (a reply, an @mention, a hello back, a reaction). `due` is
        # one snapshot taken before this loop started posting, so re-check
        # fresh right before deciding to notify — an answer to an earlier row
        # in this same batch can land while we're still working through it.
        acked = g.acknowledged or await asyncio.to_thread(
            _acked_sync, db_path, guild_id, g.message_id
        )
        if acked:
            await asyncio.to_thread(
                _resolve_sync,
                db_path,
                guild_id,
                [g.message_id],
                "acknowledged",
                int(now_ts),
            )
            continue
        await _notify(bot, db_path, guild_id, g, window_minutes)
        # Resolve whether or not the ping posted — the verdict is true either
        # way, and a guild with no moderator opted in would otherwise wedge
        # the row into re-alerting every tick.
        await asyncio.to_thread(
            _resolve_sync,
            db_path,
            guild_id,
            [g.message_id],
            "unanswered",
            int(now_ts),
        )


async def run_tick(bot: Bot, db_path: Path, now_ts: float) -> None:
    try:
        guild_ids = await asyncio.to_thread(_guilds_with_pending_sync, db_path)
    except Exception:
        log.exception("greeting watch: pending-guild scan failed")
        return
    for guild_id in guild_ids:
        try:
            await _process_guild(bot, db_path, guild_id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("greeting watch tick failed for guild %s", guild_id)
    # Retention: verdicted rows have no ongoing use; sweep the old ones.
    try:
        await asyncio.to_thread(_gc_resolved, db_path)
    except Exception:
        log.exception("greeting watch: resolved-row GC failed")


def _gc_resolved(db_path: Path) -> None:
    from bot_modules.core.db_utils import open_db
    from bot_modules.onboarding.greeting_watch.service import gc_resolved_watches

    with open_db(db_path) as conn:
        gc_resolved_watches(conn)


async def greeting_watch_loop(bot: Bot, db_path: Path) -> None:
    await run_forever(
        bot,
        tick=lambda: run_tick(bot, db_path, time.time()),
        interval=TICK_SECONDS,
        label="greeting watch",
        logger=log,
    )
