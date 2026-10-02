"""Chat Revive monitor loop — silence detection, posting, follow-up measuring.

Registered as a startup task factory (see ``__main__.py``); one ~2-minute tick
sweeps the follow-up measurements, then evaluates every enabled channel through
the same ``evaluate()`` brain the ``/revive check`` preview uses. Every SQLite
touch runs in ``asyncio.to_thread`` — nothing here blocks the event loop.

Zero-embarrassment posting: immediately before sending, the channel's newest
message is re-fetched — if it's the bot's own, or newer than the silence the
decision was based on, the revive is aborted. Combined with the in-flight
guard (shared with ``/revive fire``) this defeats ingest-lag and tick races.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot_modules.chat_revive.actions import send_revive
from bot_modules.chat_revive.logic import FLOURISHES, TRIGGER_STAFF, should_ping
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.question_source import channel_allows_nsfw
from bot_modules.core.background import FailureStreak, run_forever
from bot_modules.shared import feature_roles as fr
from bot_modules.moderation.settings import staff_ping_service
from bot_modules.chat_revive.service import (
    ChannelConfig,
    Evaluation,
    Question,
    evaluate,
    list_enabled_channels,
    measure_due_events,
    pick_question,
    record_event,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.chat_revive")

TICK_SECONDS = 120.0

# Channels with a revive send in progress (event-loop-confined, so a plain
# set is race-free). Shared by the loop and /revive fire.
_in_flight: set[int] = set()


# (guild_id, channel_id) → consecutive failures to read or post in a revive
# channel. Without it a channel the bot can no longer post in was retried every
# tick for as long as it stayed quiet — and an unpostable channel stays quiet.
# Probed hourly, so fixing the permissions heals it without a restart.
MAX_SEND_FAILURES = 5
PAUSED_PROBE_SECONDS = 3600.0
_failures: FailureStreak[tuple[int, int]] = FailureStreak(
    "chat revive",
    threshold=MAX_SEND_FAILURES,
    logger=log,
    retry_after=PAUSED_PROBE_SECONDS,
    paused_note="revives there are paused, with one attempt an hour",
)


def note_delivered(guild_id: int, channel_id: int) -> None:
    """A revive landed outside the loop (the dashboard's Fire): the channel
    works, so a pause on it is over."""
    _failures.succeeded((guild_id, channel_id))


class ReviveInFlight(Exception):
    """Another revive send is mid-flight for this channel."""


@contextlib.asynccontextmanager
async def send_guard(channel_id: int) -> AsyncIterator[None]:
    if channel_id in _in_flight:
        raise ReviveInFlight(str(channel_id))
    _in_flight.add(channel_id)
    try:
        yield
    finally:
        _in_flight.discard(channel_id)


# ── sync wrappers (run via asyncio.to_thread; also used by the cog) ──


def evaluate_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    *,
    now_ts: float,
    busy: bool,
    slowmode_delay: int,
) -> Evaluation:
    with open_db(db_path) as conn:
        return evaluate(
            conn,
            guild_id,
            channel_id,
            now_ts=now_ts,
            busy=busy,
            slowmode_delay=slowmode_delay,
        )


def pick_sync(
    db_path: Path,
    guild_id: int,
    *,
    categories: tuple[str, ...],
    allow_nsfw: bool,
    now_ts: float,
) -> Question | None:
    with open_db(db_path) as conn:
        return pick_question(
            conn, guild_id, categories=categories, allow_nsfw=allow_nsfw, now_ts=now_ts
        )


def record_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    *,
    question_id: int | None,
    message_id: int | None,
    trigger_kind: str,
    pinged: bool,
    now_ts: float,
    offset_hours: float,
) -> None:
    with open_db(db_path) as conn:
        record_event(
            conn,
            guild_id,
            channel_id,
            question_id=question_id,
            message_id=message_id,
            trigger_kind=trigger_kind,
            pinged=pinged,
            now_ts=now_ts,
            offset_hours=offset_hours,
        )


def _measure_sync(db_path: Path, now_ts: float) -> int:
    with open_db(db_path) as conn:
        return measure_due_events(conn, now_ts)


def _enabled_channels_sync(db_path: Path) -> list[ChannelConfig]:
    with open_db(db_path) as conn:
        return list_enabled_channels(conn)


# ── the monitor ───────────────────────────────────────────────────────


async def _lull_still_stands(
    bot: Bot, channel: discord.TextChannel, ev: Evaluation
) -> bool:
    """Re-check the channel's newest message right before sending.

    The decision was made from the ingest ledger, which can lag Discord by a
    beat. If the newest live message is the bot's own, or newer than the
    silence we judged, the lull is gone — stay silent. Raises
    ``discord.HTTPException`` when the history can't be read; the caller
    counts that against the channel.
    """
    newest = [m async for m in channel.history(limit=1)]
    if not newest:
        return True
    msg = newest[0]
    bot_user = getattr(bot, "user", None)
    if bot_user is not None and msg.author.id == bot_user.id:
        return False  # never talk after ourselves
    last_seen = ev.inputs.last_human_ts or 0.0
    return msg.created_at.timestamp() <= last_seen + 1.0


async def _staff_ping(
    bot: Bot,
    guild: discord.Guild,
    text: str,
    *,
    lane: staff_ping_service.Lane | None = None,
) -> bool:
    """Mention the moderators who opted into @Staff · Chat Revive
    (docs/specs/staff_pings_spec.md). False — nothing posted — when nobody has."""
    ctx = getattr(bot, "ctx", None)
    if ctx is None:
        return False
    return await staff_ping_service.post_staff_ping(
        ctx, guild, fr.STAFF_REVIVE_PING, text, lane=lane
    )


async def _unfilled(bot: Bot, guild: discord.Guild, reason: str, channel_id: int) -> None:
    """A confirmed lull went unfilled: tell the moderators why."""
    await _staff_ping(bot, guild, staff_ping_service.revive_stuck_text(reason, channel_id))


async def _hand_lull_to_staff(
    bot: Bot,
    db_path: Path,
    guild: discord.Guild,
    channel: discord.TextChannel,
    ev: Evaluation,
    now_ts: float,
) -> bool:
    """Ping-moderators mode: tell @Staff · Chat Revive about a confirmed lull
    instead of posting in it. Recorded only when the ping went out, so the
    rest period, gap and daily budget pace these pings the way they pace
    posts; a ping with nowhere to go leaves the lull to be checked again."""
    key = (guild.id, channel.id)
    try:
        still_quiet = await _lull_still_stands(bot, channel, ev)
    except discord.HTTPException:
        # Unreadable history pauses the channel here as it does when posting.
        _failures.failed(key, where=f"#{channel.name} (guild {guild.id})")
        return False
    _failures.succeeded(key)
    if not still_quiet:
        return False
    # The revive gates pace this, on a lane of its own (REVIVE_LULL_LANE).
    pinged = await _staff_ping(
        bot, guild, staff_ping_service.revive_lull_text(channel.id),
        lane=staff_ping_service.REVIVE_LULL_LANE,
    )
    if not pinged:
        return False
    await asyncio.to_thread(
        record_sync,
        db_path,
        guild.id,
        channel.id,
        question_id=None,
        message_id=None,
        trigger_kind=TRIGGER_STAFF,
        pinged=False,
        now_ts=now_ts,
        offset_hours=ev.offset_hours,
    )
    log.info("revive lull in #%s handed to staff (guild %s)", channel.name, guild.id)
    return True


async def consider_channel(
    bot: Bot, db_path: Path, cfg: ChannelConfig, now_ts: float
) -> bool:
    """Evaluate one enabled channel; act if a genuine lull passes every gate —
    post a question, or in ping-moderators mode ping the staff role.

    Returns True only when a revive or staff ping was sent and recorded.
    """
    guild = bot.get_guild(cfg.guild_id)
    channel = guild.get_channel(cfg.channel_id) if guild else None
    if guild is None or not isinstance(channel, discord.TextChannel):
        return False
    key = (cfg.guild_id, cfg.channel_id)
    if _failures.paused(key):
        return False
    # Busy-check (active game/round) disabled for now — decide purely from
    # channel silence history.
    ev = await asyncio.to_thread(
        evaluate_sync,
        db_path,
        cfg.guild_id,
        cfg.channel_id,
        now_ts=now_ts,
        busy=False,
        slowmode_delay=channel.slowmode_delay or 0,
    )
    if not ev.verdict.fire:
        return False
    if not ev.guild_cfg.posts:
        return await _hand_lull_to_staff(bot, db_path, guild, channel, ev, now_ts)
    question = await asyncio.to_thread(
        pick_sync,
        db_path,
        cfg.guild_id,
        categories=cfg.categories,
        allow_nsfw=channel_allows_nsfw(channel),
        now_ts=now_ts,
    )
    if question is None:
        log.info(
            "revive lull in #%s but no eligible question (guild %s)",
            channel.name,
            cfg.guild_id,
        )
        # The verdict came from the ingest ledger, which can lag; confirm the
        # lull against Discord before paging staff about it.
        try:
            still_quiet = await _lull_still_stands(bot, channel, ev)
        except discord.HTTPException:
            still_quiet = False
        if still_quiet:
            await _unfilled(bot, guild, staff_ping_service.REVIVE_NO_QUESTION, channel.id)
        return False
    role_id = cfg.role_id_override or ev.guild_cfg.role_id
    ping = bool(
        cfg.ping_enabled
        and role_id
        and should_ping(
            ev.freq.last_ping_ts,
            now_ts,
            ev.freq.pings_today,
            max_per_day=ev.guild_cfg.ping_max_per_day,
            cooldown_seconds=ev.guild_cfg.ping_cooldown_minutes * 60,
        )
    )
    flourish = random.choice(FLOURISHES) if ev.guild_cfg.flourish_enabled else None
    try:
        async with send_guard(channel.id):
            if not await _lull_still_stands(bot, channel, ev):
                return False
            msg = await send_revive(
                channel,
                question_text=question.text,
                role_id=role_id if ping else None,
                flourish=flourish,
            )
    except ReviveInFlight:
        return False
    except discord.HTTPException:
        _failures.failed(key, where=f"#{channel.name} (guild {cfg.guild_id})")
        await _unfilled(bot, guild, staff_ping_service.REVIVE_SEND_FAILED, channel.id)
        return False
    _failures.succeeded(key)
    await asyncio.to_thread(
        record_sync,
        db_path,
        cfg.guild_id,
        cfg.channel_id,
        question_id=question.id,
        message_id=msg.id,
        trigger_kind="auto",
        pinged=ping,
        now_ts=now_ts,
        offset_hours=ev.offset_hours,
    )
    log.info(
        "revived #%s (guild %s): %r%s",
        channel.name,
        cfg.guild_id,
        question.text[:60],
        " +ping" if ping else "",
    )
    return True


async def run_tick(bot: Bot, db_path: Path, now_ts: float) -> None:
    """One tick: measure closed 30-minute windows, then sweep the channels."""
    try:
        await asyncio.to_thread(_measure_sync, db_path, now_ts)
    except Exception:
        log.exception("revive measurement sweep failed")
    try:
        channels = await asyncio.to_thread(_enabled_channels_sync, db_path)
    except Exception:
        log.exception("revive channel listing failed")
        return
    for cfg in channels:
        try:
            await consider_channel(bot, db_path, cfg, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "revive tick failed for channel %s (guild %s)",
                cfg.channel_id,
                cfg.guild_id,
            )


async def chat_revive_loop(bot: Bot, db_path: Path) -> None:
    await run_forever(
        bot,
        tick=lambda: run_tick(bot, db_path, time.time()),
        interval=TICK_SECONDS,
        label="chat revive",
        logger=log,
    )
