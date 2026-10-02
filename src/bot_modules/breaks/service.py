"""Starting, ending and re-applying a break — the Discord I/O around ``logic``.

Order on the way in, and why (Inactive and Jail both write their row *after*
stripping, so a crash in between loses the member's roles for good):

1. the ``break_holds`` row is written **first**;
2. the break role (and Spicy Lite) go on;
3. the snapshot roles come off.

Per-role ``add_roles`` / ``remove_roles`` rather than one ``edit(roles=…)``:
a member holding an integration-managed role (Nitro Booster, a bot role)
makes a wholesale role set 403 — the reason ``check_jail_rejoin`` does the
same. If step 2 or 3 fails, everything is put back and the row deleted.

Every ``on_member_update`` the strip fires sees the row already in place, so
listeners can ask ``is_on_break_conn`` and get the truth.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

import discord

from bot_modules.breaks import logic
from bot_modules.breaks.locks import member_role_lock
from bot_modules.breaks.logic import BreakConfig, MemberState
from bot_modules.breaks.store import (
    BREAK_CHANNEL_KEY,
    all_hold_role_ids,
    create_break,
    delete_break,
    get_break,
    read_break_channel_id,
    read_break_role_ids,
)
from bot_modules.core.app_context import AppContext
from bot_modules.core.role_safety import grant_block_reason
from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.inactive.store import get_active_inactive
from bot_modules.moderation.service import get_active_jail, write_audit
from bot_modules.xp.service import nsfw_grant_role_id

log = logging.getLogger("dungeonkeeper.breaks")


@dataclass(frozen=True)
class BreakResult:
    ok: bool
    message: str


def read_break_config(ctx: AppContext, conn: sqlite3.Connection, guild_id: int) -> BreakConfig:
    break_role_id, lite_role_id = read_break_role_ids(conn, guild_id)
    gcfg = ctx.guild_config(guild_id)
    return BreakConfig(
        break_role_id=break_role_id,
        break_channel_id=read_break_channel_id(conn, guild_id),
        spicy_role_id=nsfw_grant_role_id(gcfg.grant_roles, gcfg.promotion_review_grant_role_id),
        spicy_lite_role_id=lite_role_id,
    )


def _int_cfg(conn: sqlite3.Connection, key: str, guild_id: int) -> int:
    try:
        return int(get_config_value(conn, key, "0", guild_id) or "0")
    except ValueError:
        return 0


def _blocked_by_hierarchy(guild: discord.Guild, roles: list[discord.Role]) -> frozenset[int]:
    """Roles the bot can't take off — judged on the roles actually being
    stripped, never the member's top role (a managed role above the bot blocks
    nothing, as ``inactive.logic.build_sweep_preview`` notes)."""
    top = guild.me.top_role.position
    return frozenset(r.id for r in roles if r.position >= top)


async def start_break(ctx: AppContext, member: discord.Member) -> BreakResult:
    """Snapshot, strip and hold — queued behind any jail, inactive or other
    break change to the same member (``breaks.locks``)."""
    async with member_role_lock(member.guild.id, member.id):
        return await _start_break(ctx, member)


async def _start_break(ctx: AppContext, member: discord.Member) -> BreakResult:
    guild = member.guild
    guild_id, user_id = guild.id, member.id
    unmanaged = [r for r in member.roles if not r.managed and r.id != guild.default_role.id]

    def _read():
        with ctx.open_db() as conn:
            cfg = read_break_config(ctx, conn, guild_id)
            state = MemberState(
                is_bot=member.bot,
                is_staff=ctx.member_is_mod(member),
                on_break=get_break(conn, guild_id, user_id) is not None,
                jailed=get_active_jail(conn, guild_id, user_id) is not None,
                inactive=get_active_inactive(conn, guild_id, user_id) is not None,
                role_ids=frozenset(r.id for r in member.roles),
                blocked_role_ids=_blocked_by_hierarchy(
                    guild, [r for r in unmanaged if r.id not in cfg.hold_role_ids]
                ),
            )
            kind = logic.refusal(
                cfg,
                state,
                jailed_role_id=_int_cfg(conn, "jailed_role_id", guild_id),
                inactive_role_id=_int_cfg(conn, "inactive_role_id", guild_id),
                unverified_role_id=ctx.guild_config(guild_id).unverified_role_id,
            )
            return cfg, kind

    cfg, kind = await asyncio.to_thread(_read)
    if kind is not None:
        return BreakResult(False, logic.REFUSAL_MESSAGES[kind])

    break_role = guild.get_role(cfg.break_role_id)
    if break_role is None:
        return BreakResult(False, logic.REFUSAL_MESSAGES["not_set_up"])

    plan = logic.plan_break(
        cfg,
        member_role_ids=[r.id for r in member.roles],
        managed_role_ids={r.id for r in member.roles if r.managed},
        default_role_id=guild.default_role.id,
    )
    lite_role = guild.get_role(cfg.spicy_lite_role_id) if plan.swapped_spicy else None
    # Both hold roles come from dials: one pointed at a staff role would hand
    # it to every member who took a break.
    if any(
        grant_block_reason(r, guild, feature="break")
        for r in (break_role, lite_role)
        if r is not None
    ):
        return BreakResult(False, logic.REFUSAL_MESSAGES["unsafe_role"])

    def _persist() -> bool:
        try:
            with ctx.open_db() as conn:
                create_break(
                    conn,
                    guild_id=guild_id,
                    user_id=user_id,
                    stored_roles=plan.stored_roles,
                    swapped_spicy=lite_role is not None,
                    break_role_id=break_role.id,
                    lite_role_id=lite_role.id if lite_role is not None else 0,
                )
        except sqlite3.IntegrityError:
            return False
        return True

    if not await asyncio.to_thread(_persist):
        # A second press racing the first: the first snapshot stands.
        return BreakResult(False, logic.REFUSAL_MESSAGES["on_break"])

    to_add = [break_role] + ([lite_role] if lite_role is not None else [])
    stored = set(plan.stored_roles)
    to_strip = [r for r in member.roles if r.id in stored]
    reason = "Took a break"
    stripping = False
    try:
        await member.add_roles(*to_add, reason=reason)
        stripping = True
        if to_strip:
            await member.remove_roles(*to_strip, reason=reason)
    except discord.HTTPException:
        log.warning("break: start failed for %s; rolling back", member, exc_info=True)
        # Only what may have changed is put back: a failed add stripped nothing.
        await _roll_back(ctx, member, to_strip if stripping else [], to_add)
        return BreakResult(
            False,
            "Something went wrong and nothing was changed — your roles are as they "
            "were. Try again in a minute, or ask a moderator.",
        )

    def _audit() -> None:
        with ctx.open_db() as conn:
            write_audit(
                conn,
                guild_id=guild_id,
                action="break_start",
                actor_id=user_id,
                target_id=user_id,
                extra={"roles": len(plan.stored_roles), "spicy_lite": lite_role is not None},
            )

    await asyncio.to_thread(_audit)
    await _after_start(ctx, member)
    return BreakResult(
        True,
        "You're on a break. Your roles are saved and nothing has been deleted. "
        "Press **👋 Back** in the break channel whenever you're ready.",
    )


async def _roll_back(
    ctx: AppContext,
    member: discord.Member,
    stripped: list[discord.Role],
    added: list[discord.Role],
) -> None:
    """Best effort: put the member back exactly as they were, then drop the row.

    The row goes only once the roles are back — if the restore itself fails,
    the row is what lets "I'm back" (or a mod) finish the job.
    """
    try:
        if stripped:
            await member.add_roles(*stripped, reason="Break failed — restoring")
        await member.remove_roles(*added, reason="Break failed — restoring")
    except discord.HTTPException:
        log.exception("break: rollback failed for %s; row kept for recovery", member)
        return

    def _drop() -> None:
        with ctx.open_db() as conn:
            delete_break(conn, member.guild.id, member.id)

    await asyncio.to_thread(_drop)


async def end_break(
    ctx: AppContext, member: discord.Member, *, actor_id: int | None = None
) -> BreakResult:
    """Give the member their saved roles back and close the break.

    ``actor_id`` is the mod ending it from the dashboard; ``None`` means the
    member pressed "I'm back". Real roles go on **before** the break role comes off, so
    a partial failure never leaves them with neither.
    """
    async with member_role_lock(member.guild.id, member.id):
        return await _end_break(ctx, member, actor_id=actor_id)


async def _end_break(
    ctx: AppContext, member: discord.Member, *, actor_id: int | None
) -> BreakResult:
    guild = member.guild
    guild_id, user_id = guild.id, member.id

    def _read():
        with ctx.open_db() as conn:
            return (
                read_break_config(ctx, conn, guild_id),
                get_break(conn, guild_id, user_id),
                all_hold_role_ids(conn, guild_id),
            )

    cfg, row, hold_ids = await asyncio.to_thread(_read)
    if row is None:
        # No saved roles — but someone wearing a break role with no row (a
        # hand-added role, an erased row) still deserves a way out of it,
        # including one a dial has since moved off.
        stray = [r for r in member.roles if r.id in hold_ids]
        if stray:
            try:
                await member.remove_roles(*stray, reason="Break ended (no saved roles)")
            except discord.HTTPException:
                pass
        return BreakResult(False, "You're not on a break.")

    # What this break put on, what the dials name now, and what they named
    # before: an admin re-pointing Spicy Lite mid-break mustn't strand the old one.
    hold_ids = hold_ids | row.granted_role_ids
    after, missing = logic.plan_return(
        hold_ids,
        stored_roles=row.stored_roles,
        current_role_ids=[r.id for r in member.roles],
        available_role_ids={r.id for r in guild.roles},
        default_role_id=guild.default_role.id,
    )
    current = {r.id for r in member.roles}
    to_add = [r for rid in after if rid not in current and (r := guild.get_role(rid)) is not None]
    to_remove = [r for r in member.roles if r.id in hold_ids]
    reason = "Back from a break" if actor_id is None else f"Break ended by a moderator ({actor_id})"
    try:
        if to_add:
            await member.add_roles(*to_add, reason=reason)
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
    except discord.HTTPException:
        log.warning("break: end failed for %s", member, exc_info=True)
        return BreakResult(
            False,
            "I couldn't give all your roles back just now — nothing is lost, your "
            "break is still saved. Try again in a minute, or ask a moderator.",
        )

    def _close() -> None:
        with ctx.open_db() as conn:
            delete_break(conn, guild_id, user_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="break_end",
                actor_id=actor_id if actor_id is not None else user_id,
                target_id=user_id,
                extra={"missing_roles": len(missing), "by_mod": actor_id is not None},
            )

    await asyncio.to_thread(_close)
    await _after_end(ctx, member)
    note = f" {len(missing)} role(s) were deleted while you were away and couldn't come back." if missing else ""
    return BreakResult(True, f"Welcome back! Your roles are restored.{note}")


async def end_break_absent(
    ctx: AppContext, guild_id: int, user_id: int, *, actor_id: int
) -> bool:
    """A mod ends the break of someone no longer in the server: drop the row.

    There is nobody to give roles to; if they rejoin they arrive as a fresh
    member. False if they weren't on a break.
    """

    def _close() -> bool:
        with ctx.open_db() as conn:
            if not delete_break(conn, guild_id, user_id):
                return False
            write_audit(
                conn,
                guild_id=guild_id,
                action="break_end",
                actor_id=actor_id,
                target_id=user_id,
                extra={"absent": True, "by_mod": True},
            )
            return True

    return await asyncio.to_thread(_close)


async def reapply_on_join(ctx: AppContext, member: discord.Member) -> bool:
    """A member who left mid-break and came back gets the break again.

    True when a break was re-applied, so the join handler skips auto-roles and
    the intake card, as it does for a jailed rejoiner. Their saved roles are
    untouched; only the hold roles go back on.
    """
    guild = member.guild

    def _read():
        with ctx.open_db() as conn:
            return read_break_config(ctx, conn, guild.id), get_break(conn, guild.id, member.id)

    cfg, row = await asyncio.to_thread(_read)
    if row is None or member.bot:
        return False
    # The roles this break was given, falling back to today's dials for one
    # whose role has since been deleted.
    roles = [guild.get_role(row.break_role_id) or guild.get_role(cfg.break_role_id)]
    if row.swapped_spicy:
        roles.append(guild.get_role(row.lite_role_id) or guild.get_role(cfg.spicy_lite_role_id))
    present = [
        r for r in roles
        if r is not None and grant_block_reason(r, guild, feature="break") is None
    ]
    if present:
        try:
            await member.add_roles(*present, reason="Rejoined while on a break")
        except discord.HTTPException:
            log.warning("break: could not re-apply break to %s on rejoin", member)
    return True


# ── side effects that follow a break (stage 6 fills these in) ────────


async def _after_start(ctx: AppContext, member: discord.Member) -> None:
    """What else a break pauses: perk rental billing (decision 8) and a
    pen-pal session (decision 10). Each step is isolated — the break itself
    has already happened, and one failing mustn't skip the other.

    Pen Pals hears it as the ``member_break_start`` event rather than being
    called, so this service never reaches into a cog.
    """
    from bot_modules.economy.rentals_service import set_beneficiary_rentals_paused

    guild_id, user_id = member.guild.id, member.id

    def _pause() -> int:
        with ctx.open_db() as conn:
            return set_beneficiary_rentals_paused(conn, guild_id, user_id, True)

    try:
        await asyncio.to_thread(_pause)
    except Exception:
        log.exception("breaks: pausing rentals for %s failed", user_id)
    ctx.bot.dispatch("member_break_start", guild_id, user_id)


async def _after_end(ctx: AppContext, member: discord.Member) -> None:
    """What a return resumes: rental billing, then the personal perk role
    (re-projected, since a perk bought or edited mid-break can have made a
    role the snapshot never held)."""
    from bot_modules.economy.perk_actions import apply_role_perks, feature_gate_ok
    from bot_modules.economy.rentals_service import (
        list_member_rentals,
        set_beneficiary_rentals_paused,
    )

    guild_id, user_id = member.guild.id, member.id

    def _perks() -> set[str]:
        with ctx.open_db() as conn:
            return {
                str(r["perk"])
                for r in list_member_rentals(conn, guild_id, user_id)
                if int(r["beneficiary_id"]) == user_id
            }

    try:
        perks = await asyncio.to_thread(_perks)
        gate_ok = {p: await feature_gate_ok(ctx.bot, guild_id, p) for p in perks}

        def _resume() -> int:
            with ctx.open_db() as conn:
                return set_beneficiary_rentals_paused(
                    conn, guild_id, user_id, False, gate_ok=gate_ok
                )

        await asyncio.to_thread(_resume)
        if perks:
            await apply_role_perks(ctx.bot, ctx.db_path, guild_id, user_id)
    except Exception:
        log.exception("breaks: resuming rentals for %s failed", user_id)


# ── setup (the dashboard's "Set up break channel") ──────────────────


async def ensure_break_role(ctx: AppContext, guild: discord.Guild) -> discord.Role | None:
    """The guild's ``@On a Break`` role, created if missing; ``None`` without
    **Manage Roles**, or when an admin set the dial to "(none)". Through ``ensure_config_role`` for the same reason
    Inactive is: it is the one path that tells this guild's stored id from
    an inherited one."""
    from bot_modules.core.role_provision import ensure_config_role
    from bot_modules.shared.feature_roles import BREAK_ROLE

    return await ensure_config_role(
        ctx,
        guild,
        BREAK_ROLE.key,
        BREAK_ROLE.spec,
        feature=BREAK_ROLE.feature,
        allow_legacy_fallback=BREAK_ROLE.legacy_fallback,
        respect_opt_out=BREAK_ROLE.none_means_off,
        assigns=BREAK_ROLE.assigns,
    )


async def _lock_down(
    guild: discord.Guild, role: discord.Role, break_channel: discord.abc.GuildChannel
) -> int:
    """Hide every channel from the break role except the break channel.

    Per channel and explicit, never a category sync — category grants do not
    cascade. Runs on **every** setup press, not only when the role is made, so
    pressing it again after adding channels hides the new ones too, and a
    re-point hides the old break channel with no separate revoke step. Best
    effort per channel; returns how many couldn't be set. A Spicy Lite allow
    still wins where one exists — role allows beat role denies — which is the
    point of the swap.
    """
    failed = 0
    for channel in guild.channels:
        if channel.id == break_channel.id:
            continue
        try:
            await channel.set_permissions(role, view_channel=False, send_messages=False)
        except discord.HTTPException:
            failed += 1
    return failed


async def setup_break_channel(
    ctx: AppContext,
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    *,
    return_panel: discord.ui.LayoutView,
) -> tuple[bool, str]:
    """Point breaks at ``channel``: role, access, the "I'm back" panel, config.

    ``(ok, note)`` — ``note`` is the reason on failure, or a warning worth
    showing on success. Order follows ``setup_inactive_channel``: everything
    that can fail runs before the config write, so a failure leaves breaks as
    they were. Writing ``break_channel_id`` is what switches the feature on
    (``BreakConfig.enabled``), so it goes last. ``return_panel`` is the cog's
    bound view — a callback-less copy would shadow the persistent one on the
    message it was sent with.
    """
    if not isinstance(channel, discord.TextChannel):
        return False, "Pick a regular text channel."

    role = await ensure_break_role(ctx, guild)
    if role is None:
        return False, (
            "Couldn't get a break role: either I'm missing Manage Roles, or it was "
            "set to (none) on the Bot-Managed Roles page."
        )
    try:
        await channel.set_permissions(
            role, view_channel=True, send_messages=True, read_message_history=True
        )
    except discord.HTTPException:
        return False, (
            f"Couldn't give the break role access to #{channel.name} — "
            "check my channel permissions."
        )
    try:
        await channel.send(view=return_panel)
    except discord.HTTPException:
        return False, f"Couldn't post the On a Break panel in #{channel.name}."

    failed = await _lock_down(guild, role, channel)

    def _persist() -> None:
        with ctx.open_db() as conn:
            set_config_value(conn, BREAK_CHANNEL_KEY, str(channel.id), guild.id)

    await asyncio.to_thread(_persist)

    if failed:
        return True, (
            f"Couldn't hide {failed} channel(s) from the break role — check my "
            "permissions there, then press Set up again."
        )
    return True, ""
