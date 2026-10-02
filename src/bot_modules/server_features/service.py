"""Roles & Pings — the Discord and database half.

``logic`` decides; this module gathers what it decides on (the stored role
ids, the guild's Customize questions, the member's hold state) and carries out
a change. The card view in ``cogs.server_features_cog`` calls
:func:`read_card` to draw and re-reads it on every press, so a press is judged
against the server as it is *now* — an admin who removed an option from
Customize, or a moderator who jailed the member, since the card opened.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot_modules.core.role_safety import role_block_reason
from bot_modules.needle.store import is_reply_opted_in, reply_dms_offered, set_reply_optin
from bot_modules.server_features import logic
from bot_modules.shared import feature_roles as fr
from bot_modules.roles.bot_roles.managed_roles_store import read_stored_ids
from bot_modules.moderation.service import get_active_jail
from bot_modules.onboarding.welcome.onboarding_service import PromptView, read_prompts

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.server_features")

#: The holds that took a member's roles away (``logic.under_hold``).
HOLD_ROLES: tuple[fr.FeatureRole, ...] = (fr.JAILED_ROLE, fr.INACTIVE_ROLE, fr.BREAK_ROLE)


@dataclass(frozen=True)
class RoleReadings:
    """Everything the card needs from the database, in one pass."""

    #: ``(ping, role id)`` for each ping whose dial is set and not "(none)".
    pings: tuple[tuple[fr.FeatureRole, int], ...]
    #: Stored ids of every bot-managed role the card must never offer.
    excluded_ids: frozenset[int]
    hold_ids: frozenset[int]
    jailed: bool
    #: The Auto-Thread reply-DM opt-in, or None when the guild offers none.
    reply_dms: bool | None = None


def read_role_readings(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> RoleReadings:
    stored = read_stored_ids(conn, guild_id)
    pings = tuple(
        (entry, stored[entry.key][0])
        for entry in logic.MEMBER_PINGS
        if stored.get(entry.key, (0, False, False))[0] > 0
        and not stored[entry.key][2]
    )
    return RoleReadings(
        pings=pings,
        excluded_ids=frozenset(
            stored.get(entry.key, (0, False, False))[0] for entry in logic.EXCLUDED
        )
        - {0},
        hold_ids=frozenset(stored.get(entry.key, (0, False, False))[0] for entry in HOLD_ROLES)
        - {0},
        jailed=get_active_jail(conn, guild_id, user_id) is not None,
        reply_dms=logic.reply_dms_row_state(
            offered=reply_dms_offered(conn, guild_id),
            opted_in=is_reply_opted_in(conn, guild_id=guild_id, user_id=user_id),
        ),
    )


def grantable_role_ids(
    roles: Iterable[discord.Role],
    bot_member: discord.Member | None,
    *,
    excluded_ids: Collection[int],
) -> set[int]:
    """Roles the card may hand out: safe to self-assign, and not the bot's own.

    Excluded by stored id, and also **by name** for the bot-managed roles that
    don't store one reliably — the DM mode trio is found by name on every
    pass (``feature_roles._dm_mode``), so an id lookup alone would miss a
    "DMs: Open" an admin had also put into Customize.
    """
    excluded_names = {
        entry.spec.name for entry in logic.EXCLUDED if entry.source != fr.SOURCE_CONFIG
    }
    return {
        role.id
        for role in roles
        if role.id not in excluded_ids
        and role.name not in excluded_names
        and role_block_reason(role, bot_member) is None
    }


async def read_prompts_or_none(guild: discord.Guild) -> tuple[PromptView, ...]:
    """The guild's Customize questions, or none when Discord won't say.

    Reading onboarding needs Manage Server. Without it the card still offers
    the bot pings — one section missing is better than a card that fails.
    """
    try:
        onboarding = await guild.onboarding()
    except discord.HTTPException:
        log.info("server features: couldn't read onboarding in guild %s", guild.id)
        return ()
    return read_prompts(onboarding)


@dataclass(frozen=True)
class CardRead:
    card: logic.RoleCard
    #: The member is jailed, inactive or on a break: the card refuses.
    held: bool


async def read_card(
    ctx: AppContext,
    member: discord.Member,
    *,
    role_ids: Collection[int] | None = None,
) -> CardRead:
    """The card as the server stands now. ``role_ids`` overrides the member's
    cached roles for the redraw right after a change (``logic.with_change``).
    """
    guild = member.guild

    def _read() -> RoleReadings:
        with ctx.open_db() as conn:
            return read_role_readings(conn, guild.id, member.id)

    readings = await asyncio.to_thread(_read)
    member_roles = set(role_ids) if role_ids is not None else {r.id for r in member.roles}
    held = logic.under_hold(member_roles, readings.hold_ids, jailed=readings.jailed)
    if held:
        return CardRead(card=logic.RoleCard(pings=(), menus=()), held=True)
    grantable = grantable_role_ids(
        guild.roles, guild.me, excluded_ids=readings.excluded_ids
    )
    card = logic.build_card(
        readings.pings,
        await read_prompts_or_none(guild),
        grantable=grantable,
        member_role_ids=member_roles,
        reply_dms=readings.reply_dms,
    )
    return CardRead(card=card, held=False)


async def set_reply_dms(ctx: AppContext, guild_id: int, user_id: int, *, on: bool) -> None:
    """Write the member's Auto-Thread reply-DM opt-in (the Bot DMs row)."""

    def _write() -> None:
        with ctx.open_db() as conn:
            set_reply_optin(conn, guild_id=guild_id, user_id=user_id, on=on)

    await asyncio.to_thread(_write)


async def apply_change(
    member: discord.Member, add: Collection[int], remove: Collection[int]
) -> tuple[bool, bool]:
    """Add and remove roles, independently. ``(add_ok, remove_ok)``.

    Every id here came out of a freshly read card, so each has already passed
    the grantable check; it is asked again anyway, one line from the grant, so
    an admin who raised a role's permissions a second ago can't slip it
    through. A role deleted in the meantime is skipped.

    The two calls are tried separately so a failure on one side doesn't hide a
    success on the other: an add that lands followed by a remove Discord
    refuses must still be reported as an add, not silently swallowed into one
    "nothing happened" failure the caller would otherwise believe. A failed add
    skips the remove (``remove_ok`` False): a Customize swap is add-A-remove-B,
    and taking B after A was refused would leave the member with neither.
    """
    guild = member.guild
    to_add = [
        r
        for r in (guild.get_role(i) for i in add)
        if r is not None and role_block_reason(r, guild.me) is None
    ]
    to_remove = [r for r in (guild.get_role(i) for i in remove) if r is not None]
    add_ok = True
    if to_add:
        try:
            await member.add_roles(*to_add, reason="Help & Settings: Roles & Pings")
        except discord.HTTPException:
            log.warning("server features: role add failed for %s", member.id, exc_info=True)
            add_ok = False
    remove_ok = True
    if to_remove and not add_ok:
        remove_ok = False
    elif to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Help & Settings: Roles & Pings")
        except discord.HTTPException:
            log.warning("server features: role remove failed for %s", member.id, exc_info=True)
            remove_ok = False
    return add_ok, remove_ok
