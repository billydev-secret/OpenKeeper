"""Mod Settings — the Discord and database half.

``logic`` decides; this gathers what it decides on and carries out a press.
Every press re-reads the moderator's card first (:func:`read_card`), so it is
judged against the server as it is now, not as the posted panel was drawn: a
channel an admin unset, a role made unsafe, or a moderator jailed since.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot_modules.core.role_provision import ensure_config_role
from bot_modules.core.role_safety import role_block_reason
from bot_modules.moderation.settings import logic
from bot_modules.server_features import logic as sf_logic
from bot_modules.server_features.service import HOLD_ROLES
from bot_modules.shared import feature_roles as fr
from bot_modules.moderation.settings import staff_ping_service
from bot_modules.roles.bot_roles.managed_roles_store import read_stored_ids
from bot_modules.moderation.service import get_active_jail

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.mod_settings")

_REASON = "Mod Settings: staff pings"

#: The panel's ``panel_registry`` key, which its ``posted_panels`` rows carry.
PANEL_KEY = "mod-settings"

#: Per (guild, member): role id -> (held after the press, when). See
#: ``logic.roles_after_recent``. In memory: a restart outlasts the lag.
_recent: dict[tuple[int, int], dict[int, tuple[bool, float]]] = {}
#: One press at a time per (guild, member), so two quick presses can't both
#: read the card before either has changed it.
_press_locks: dict[tuple[int, int], asyncio.Lock] = {}


@dataclass(frozen=True)
class Readings:
    #: ``(ping, stored role id)`` for each staff ping not switched off.
    stored: tuple[tuple[fr.FeatureRole, int], ...]
    delivered: frozenset[str]
    hold_ids: frozenset[int]
    jailed: bool


def read_readings(
    conn: sqlite3.Connection, guild_id: int, user_id: int | None = None
) -> Readings:
    """``user_id`` None reads the guild alone (the posted panel): nobody is
    jailed."""
    ids = read_stored_ids(conn, guild_id)
    stored = tuple(
        (entry, ids[entry.key][0])
        for entry in logic.STAFF_PINGS
        if not ids.get(entry.key, (0, False, False))[2]
    )
    return Readings(
        stored=stored,
        delivered=frozenset(staff_ping_service.delivered_keys(conn, guild_id)),
        hold_ids=frozenset(ids.get(e.key, (0, False, False))[0] for e in HOLD_ROLES) - {0},
        jailed=user_id is not None and get_active_jail(conn, guild_id, user_id) is not None,
    )


def admitted_mod(ctx: AppContext, user: discord.abc.User) -> discord.Member | None:
    """The member, when they may press the panel right now — checked on every
    press, so a moderator who steps down is refused on the next one. None
    means refuse with ``logic.NOT_A_MOD_TEXT``."""
    if isinstance(user, discord.Member) and ctx.member_is_mod(user):
        return user
    return None


@dataclass(frozen=True)
class CardRead:
    card: logic.ModCard
    held: bool


def _card(
    guild: discord.Guild, readings: Readings, member_roles: Collection[int]
) -> logic.ModCard:
    # A stored id whose role was deleted reads as "not made": the next press
    # recreates it through ensure_config_role, which says so in the mod log.
    stored = tuple(
        (entry, rid if guild.get_role(rid) is not None else 0)
        for entry, rid in readings.stored
    )
    grantable = {
        role.id for role in guild.roles if role_block_reason(role, guild.me) is None
    }
    return logic.build_card(
        stored,
        delivered=readings.delivered,
        grantable=grantable,
        member_role_ids=member_roles,
    )


async def read_offered(ctx: AppContext, guild: discord.Guild) -> logic.ModCard:
    """The pings the posted panel lists: the same rows any moderator's card
    would have, with nobody's state in them."""

    def _read() -> Readings:
        with ctx.open_db() as conn:
            return read_readings(conn, guild.id)

    return _card(guild, await asyncio.to_thread(_read), ())


async def read_card(ctx: AppContext, member: discord.Member) -> CardRead:
    """One moderator's card as the server stands now."""
    guild = member.guild

    def _read() -> Readings:
        with ctx.open_db() as conn:
            return read_readings(conn, guild.id, member.id)

    readings = await asyncio.to_thread(_read)
    member_roles = logic.roles_after_recent(
        (r.id for r in member.roles),
        _recent.get((guild.id, member.id), {}),
        now=time.monotonic(),
    )
    if sf_logic.under_hold(member_roles, readings.hold_ids, jailed=readings.jailed):
        return CardRead(card=logic.ModCard(rows=()), held=True)
    return CardRead(card=_card(guild, readings, member_roles), held=False)


@dataclass(frozen=True)
class Toggled:
    #: The private reply.
    text: str
    #: The press was on a ping the panel no longer offers: redraw it.
    stale: bool = False


async def toggle(ctx: AppContext, member: discord.Member, key: str) -> Toggled:
    """One press on the posted panel: switch ``key`` on for ``member``, or
    off when they already have it. The caller has admitted the moderator."""
    who = (member.guild.id, member.id)
    async with _press_locks.setdefault(who, asyncio.Lock()):
        return await _toggle(ctx, member, key, who)


def _remember(who: tuple[int, int], role_id: int, held: bool) -> None:
    _recent.setdefault(who, {})[role_id] = (held, time.monotonic())


async def _toggle(
    ctx: AppContext, member: discord.Member, key: str, who: tuple[int, int]
) -> Toggled:
    read = await read_card(ctx, member)
    if read.held:
        return Toggled(logic.HOLD_TEXT)
    row = logic.row_for(read.card, key)
    if row is None:
        return Toggled(logic.GONE_TEXT, stale=True)
    if row.held:
        if not await turn_off(member, row.role_id):
            return Toggled(logic.FAILED_TEXT)
        _remember(who, row.role_id, False)
        return Toggled(logic.toggled_text(row, on=False))
    added = await turn_on(ctx, member, fr.BY_KEY[key])
    if added is None:
        return Toggled(logic.FAILED_TEXT)
    _remember(who, added, True)
    return Toggled(logic.toggled_text(row, on=True))


async def turn_on(ctx: AppContext, member: discord.Member, entry: fr.FeatureRole) -> int | None:
    """Make or adopt the ping's role if need be, then give it. The role id,
    or None when Discord refused or the dial was switched off meanwhile."""
    guild = member.guild
    role = await ensure_config_role(
        ctx, guild, entry.key, entry.spec,
        feature=entry.feature,
        allow_legacy_fallback=entry.legacy_fallback,
        assigns=entry.assigns,
    )
    if role is None or role_block_reason(role, guild.me) is not None:
        return None
    try:
        await member.add_roles(role, reason=_REASON)
    except discord.HTTPException:
        log.warning("mod settings: role add failed for %s", member.id, exc_info=True)
        return None
    return role.id


async def turn_off(member: discord.Member, role_id: int) -> bool:
    role = member.guild.get_role(role_id)
    if role is None:
        return True
    try:
        await member.remove_roles(role, reason=_REASON)
    except discord.HTTPException:
        log.warning("mod settings: role remove failed for %s", member.id, exc_info=True)
        return False
    return True


async def strip_on_step_down(
    ctx: AppContext, before: discord.Member, after: discord.Member
) -> None:
    """Take every staff ping role off a member who just stopped being a
    moderator (``logic.roles_to_strip``); a hold is waited out."""
    is_mod = ctx.member_is_mod(after)
    if is_mod:
        return
    was_mod = ctx.member_is_mod(before)

    def _read() -> tuple[list[int], frozenset[int], bool]:
        with ctx.open_db() as conn:
            ids = read_stored_ids(conn, after.guild.id)
            jailed = get_active_jail(conn, after.guild.id, after.id) is not None
        staff = [ids.get(e.key, (0, False, False))[0] for e in logic.STAFF_PINGS]
        hold_ids = frozenset(ids.get(e.key, (0, False, False))[0] for e in HOLD_ROLES) - {0}
        return staff, hold_ids, jailed

    staff_ids, hold_ids, jailed = await asyncio.to_thread(_read)
    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}
    strip = logic.roles_to_strip(
        was_mod=was_mod,
        is_mod=is_mod,
        # The jail row may be written either side of the role change, so
        # "before" goes by the hold roles alone.
        was_held=sf_logic.under_hold(before_ids, hold_ids, jailed=False),
        is_held=sf_logic.under_hold(after_ids, hold_ids, jailed=jailed),
        member_role_ids=after_ids,
        staff_role_ids=staff_ids,
    )
    roles = [r for r in (after.guild.get_role(i) for i in strip) if r is not None]
    if not roles:
        return
    try:
        await after.remove_roles(*roles, reason="No longer a moderator: staff pings removed")
    except discord.HTTPException:
        log.warning("mod settings: staff ping strip failed for %s", after.id, exc_info=True)
