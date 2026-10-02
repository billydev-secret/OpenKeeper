"""Coin Drops loop — posts random drops; a persistent Claim button settles them.

Registered as a startup task factory (see ``__main__.py``). Each ~1-minute
tick expires overdue drops, then walks the guilds: a guild whose faucet is
live (``drops_configured``) gets a jittered next-drop time, and when it comes
due the pouch is posted — unless the guild already holds an open pouch, the
channel is mid-game, or nobody has spoken since our own last message (a drop
should land in conversation, not echo into the void; the due time simply
stands until someone talks).

Claims are a :class:`DropClaimButton` press — a ``DynamicItem`` whose
``custom_id`` carries the drop id, so it needs no state and survives
restarts (registered in ``__main__.py``). The DB's conditional UPDATE
(`try_claim_drop`) arbitrates who actually wins; losers get an ephemeral
"too slow".

Components V2 since ``docs/plans/components_v2_migration.md`` wave 3a: every
card this loop sends is a bare ``LayoutView`` built by
``economy_drops_layouts.py``, not an embed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.chat_revive.actions import channel_is_busy
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.core.db_utils import open_db
from bot_modules.core.background import FailureStreak, run_forever
from bot_modules.core.layout import card
from bot_modules.economy.drops_layouts import (
    build_claimed_layout,
    build_drop_layout,
    build_expired_layout,
)
from bot_modules.economy.drops_service import (
    create_drop,
    discard_drop,
    drops_configured,
    expire_due_drops,
    has_open_drop,
    next_drop_delay,
    roll_amount,
    set_drop_message,
    try_claim_drop,
)
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
)
from bot_modules.shared.name_resolver import build_name_fn

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.economy")

TICK_SECONDS = 60.0

# guild_id → wall-clock ts of the next scheduled drop. Deliberately
# in-memory: a restart just re-jitters each guild's next drop.
_next_due: dict[int, float] = {}
_rng = random.Random()

# (guild_id, channel_id) → consecutive failures to read or post in the drop
# channel. A channel the bot has lost Send Messages in used to cost a drop row,
# a doomed request and a traceback every tick, for good. Keyed by channel so
# picking another one starts clean; probed hourly so fixing the permissions
# heals it without a restart.
MAX_SEND_FAILURES = 5
PAUSED_PROBE_SECONDS = 3600.0
_failures: FailureStreak[tuple[int, int]] = FailureStreak(
    "coin drop",
    threshold=MAX_SEND_FAILURES,
    logger=log,
    retry_after=PAUSED_PROBE_SECONDS,
    paused_note="drops there are paused, with one attempt an hour",
)


# ── the claim button ──────────────────────────────────────────────────


class DropClaimButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"econ_drop:claim:(?P<drop_id>\d+)",
):
    """Persistent first-wins Claim button; ``custom_id`` carries the drop id.

    Stateless by design — everything needed at click time is the id plus a
    fresh settings read, so a restart between post and claim costs nothing.
    The conditional UPDATE in ``try_claim_drop`` is the race arbiter; the
    winning click also edits the drop message, which removes the button.
    """

    def __init__(self, drop_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Claim",
                emoji="🪙",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_drop:claim:{drop_id}",
            )
        )
        self.drop_id = drop_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> DropClaimButton:
        return cls(int(match["drop_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return
        bot = cast("Bot", interaction.client)
        db_path = bot.ctx.db_path
        booster = member.premium_since is not None

        def _claim() -> tuple[EconSettings, int | None, PanelArt]:
            with open_db(db_path) as conn:
                settings = load_econ_settings(conn, guild.id)
                art = resolve_panel_art_conn(conn, guild.id)
                if not settings.enabled:
                    return settings, None, art
                credited = try_claim_drop(
                    conn,
                    settings,
                    self.drop_id,
                    guild.id,
                    member.id,
                    now_ts=time.time(),
                    booster=booster,
                )
                return settings, credited, art

        settings, credited, art = await asyncio.to_thread(_claim)
        if credited is None:
            await interaction.response.send_message(
                "Too slow — this pouch is already gone!", ephemeral=True
            )
            return
        name_fn = await build_name_fn(
            guild=guild, db_path=db_path, guild_id=guild.id, user_ids=[member.id]
        )
        await interaction.response.edit_message(
            content=None,
            embed=None,
            attachments=[],
            view=card(
                build_claimed_layout(
                    settings=settings,
                    credited=credited,
                    claimant_id=member.id,
                    name_fn=name_fn,
                    art=art,
                )
            ),
        )


# ── sync wrappers (run via asyncio.to_thread) ─────────────────────────


def _expire_sync(db_path: Path, now_ts: float) -> list[dict]:
    with open_db(db_path) as conn:
        return [dict(row) for row in expire_due_drops(conn, now_ts)]


def _guild_state_sync(db_path: Path, guild_id: int) -> tuple[EconSettings, bool]:
    with open_db(db_path) as conn:
        return load_econ_settings(conn, guild_id), has_open_drop(conn, guild_id)


def _art_sync(db_path: Path, guild_id: int) -> PanelArt:
    with open_db(db_path) as conn:
        return resolve_panel_art_conn(conn, guild_id)


def _create_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    amount: int,
    *,
    now_ts: float,
    expire_minutes: int,
) -> int:
    with open_db(db_path) as conn:
        return create_drop(
            conn,
            guild_id,
            channel_id,
            amount,
            now_ts=now_ts,
            expire_minutes=expire_minutes,
        )


def _set_message_sync(db_path: Path, drop_id: int, message_id: int) -> None:
    with open_db(db_path) as conn:
        set_drop_message(conn, drop_id, message_id)


def _discard_sync(db_path: Path, drop_id: int) -> None:
    with open_db(db_path) as conn:
        discard_drop(conn, drop_id)


# ── the drop scheduler ────────────────────────────────────────────────


async def _someone_spoke_since_our_last(channel: discord.TextChannel, bot: Bot) -> bool:
    """A drop should land in conversation — if the newest message is our own
    (a previous drop, a leaderboard refresh…), hold until a human talks.

    Raises ``discord.HTTPException`` when the history can't be read; the
    caller counts that against the channel."""
    newest = [m async for m in channel.history(limit=1)]
    if not newest:
        return True
    bot_user = getattr(bot, "user", None)
    return bot_user is None or newest[0].author.id != bot_user.id


async def _consider_guild(
    bot: Bot, db_path: Path, guild: discord.Guild, now_ts: float
) -> None:
    settings, pouch_out = await asyncio.to_thread(
        _guild_state_sync, db_path, guild.id
    )
    if not drops_configured(settings):
        _next_due.pop(guild.id, None)
        return
    due = _next_due.get(guild.id)
    if due is None:
        _next_due[guild.id] = now_ts + next_drop_delay(settings, _rng)
        return
    if now_ts < due:
        return
    channel = guild.get_channel(settings.drops_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return  # misconfigured; the due time stands until fixed
    key = (guild.id, channel.id)
    if _failures.paused(key):
        return
    where = f"#{channel.name} (guild {guild.id})"
    # Never stack pouches: an open drop blocks the next one (its claim or
    # expiry frees the slot).
    if pouch_out:
        return
    if await channel_is_busy(bot, channel.id):
        return  # mid-game; retry next tick
    try:
        spoke = await _someone_spoke_since_our_last(channel, bot)
    except discord.HTTPException:
        _failures.failed(key, where=where)
        return
    if not spoke:
        return
    amount = roll_amount(settings, _rng)
    expires_at = now_ts + max(1, settings.drops_expire_minutes) * 60.0
    # The row comes first — the Claim button's custom_id needs the id.
    art = await asyncio.to_thread(_art_sync, db_path, guild.id)
    drop_id = await asyncio.to_thread(
        _create_sync,
        db_path,
        guild.id,
        channel.id,
        amount,
        now_ts=now_ts,
        expire_minutes=settings.drops_expire_minutes,
    )
    try:
        msg = await channel.send(
            view=card(
                build_drop_layout(
                    settings=settings,
                    amount=amount,
                    expires_at=expires_at,
                    claim_button=DropClaimButton(drop_id),
                    art=art,
                )
            )
        )
    except discord.HTTPException:
        _failures.failed(key, where=where)
        await asyncio.to_thread(_discard_sync, db_path, drop_id)
        return
    _failures.succeeded(key)
    await asyncio.to_thread(_set_message_sync, db_path, drop_id, msg.id)
    _next_due[guild.id] = now_ts + next_drop_delay(settings, _rng)
    log.info(
        "dropped %s coins in #%s (guild %s, drop %s)",
        amount,
        channel.name,
        guild.id,
        drop_id,
    )


async def _sweep_expired(bot: Bot, db_path: Path, now_ts: float) -> None:
    rows = await asyncio.to_thread(_expire_sync, db_path, now_ts)
    art_by_guild: dict[int, PanelArt] = {}
    for row in rows:
        message_id = int(row["message_id"])
        if message_id == 0:
            continue  # send never completed; nothing to edit
        guild = bot.get_guild(int(row["guild_id"]))
        if guild is None:
            continue
        channel = guild.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            continue
        if guild.id not in art_by_guild:
            art_by_guild[guild.id] = await asyncio.to_thread(_art_sync, db_path, guild.id)
        art = art_by_guild[guild.id]
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(
                content=None,
                embed=None,
                attachments=[],
                view=card(build_expired_layout(art)),
            )
        except discord.HTTPException:
            pass  # drop message deleted or unreachable — the row is settled


async def run_tick(bot: Bot, db_path: Path, now_ts: float) -> None:
    """One tick: expire overdue drops, then give each guild its chance."""
    try:
        await _sweep_expired(bot, db_path, now_ts)
    except Exception:
        log.exception("drop expiry sweep failed")
    for guild in bot.guilds:
        try:
            await _consider_guild(bot, db_path, guild, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("drop tick failed for guild %s", guild.id)


async def economy_drops_loop(bot: Bot, db_path: Path) -> None:
    await run_forever(
        bot,
        tick=lambda: run_tick(bot, db_path, time.time()),
        interval=TICK_SECONDS,
        label="coin drop",
        logger=log,
    )
