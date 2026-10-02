"""Tests for releasing and re-applying jails when the subject isn't present.

A jailed member who leaves keeps their ``active`` row — ``check_jail_rejoin``
re-applies the hold if they return — so every release path has to cope with a
:class:`discord.User` that has no roles to restore, and every rejoin path has to
cope with a jail channel that vanished while they were away.

Discord objects are mocked, matching ``test_jail_apply.py``: these functions
exist to orchestrate Discord calls, so the assertions are about which calls were
made and what landed in the DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.moderation.jail.jail_commands import (
    _do_unjail,
    check_jail_rejoin,
    jail_expiry_loop,
    jail_rejoin_reconcile_sweep,
    resolve_release_target,
    retire_departed_jail_channel,
)
from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from bot_modules.moderation.service import create_jail, get_active_jail
from tests.db_template import migrated_db


JAILED_ROLE_ID = 5000


# ── Fixtures ────────────────────────────────────────────────────────


def _make_ctx(db_path, *, guild_id: int = 100) -> AppContext:
    migrated_db(db_path)
    ctx = AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=guild_id,
        debug=True,
    )
    with open_db(db_path) as conn:
        _db_set(conn, "jailed_role_id", str(JAILED_ROLE_ID), guild_id=guild_id)
    return ctx


# Role mocks are interned by identity: production code does membership tests
# like ``jailed_role in member.roles``, and two MagicMocks with the same id
# would compare unequal, silently defeating the check under test.
_ROLE_CACHE: dict[tuple[int, bool, bool], MagicMock] = {}


def _role(role_id: int, *, managed: bool = False, default: bool = False) -> MagicMock:
    key = (role_id, managed, default)
    cached = _ROLE_CACHE.get(key)
    if cached is not None:
        return cached
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.name = f"role{role_id}"
    r.managed = managed
    r.is_default = MagicMock(return_value=default)
    _ROLE_CACHE[key] = r
    return r


def _member(
    member_id: int,
    *,
    role_ids: tuple[int, ...] = (),
    managed_role_ids: tuple[int, ...] = (),
    joined_at: datetime | None = None,
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = False
    m.name = f"u{member_id}"
    m.display_name = m.name
    m.mention = f"<@{member_id}>"
    m.roles = [_role(rid) for rid in role_ids] + [
        _role(rid, managed=True) for rid in managed_role_ids
    ]
    # The reconcile sweep keys off this. Defaults to just *after* now, so a
    # member built before a jail is seeded still reads as having rejoined after
    # it — the fresh-rejoiner case. Tests that need a long-standing member
    # (hand-released, never left) pass an explicit past ``joined_at``.
    m.joined_at = (
        joined_at if joined_at is not None else _now_dt() + timedelta(minutes=1)
    )
    m.remove_roles = AsyncMock()
    m.add_roles = AsyncMock()
    m.edit = AsyncMock()
    m.send = AsyncMock()
    return m


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _user(user_id: int) -> MagicMock:
    """A departed member: resolvable as a User, with no Member-only methods."""
    u = MagicMock(spec=discord.User)
    u.id = user_id
    u.bot = False
    u.name = f"u{user_id}"
    u.display_name = u.name
    u.mention = f"<@{user_id}>"
    u.send = AsyncMock()
    return u


def _guild(*, guild_id: int = 100, members: list | None = None) -> MagicMock:
    members = members or []
    by_id = {m.id: m for m in members}
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    g.name = "Test Guild"
    g.members = members
    g.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    default_role = _role(0, default=True)
    g.default_role = default_role
    jailed = _role(JAILED_ROLE_ID)
    g.roles = [default_role, jailed, _role(700), _role(701)]
    by_role = {r.id: r for r in g.roles}
    g.get_role = MagicMock(side_effect=lambda rid: by_role.get(int(rid)))
    g.channels = []
    g.get_channel = MagicMock(return_value=None)
    g.create_text_channel = AsyncMock()
    # Default: not on the ban list, i.e. they genuinely left or were kicked.
    g.fetch_ban = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "not banned")
    )
    g.me = _member(99)
    return g


def _text_channel(channel_id: int, guild: MagicMock | None = None) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.name = f"jail-{channel_id}"
    # generate_transcript reads channel.guild.id, so a released channel needs
    # its guild wired up.
    ch.guild = guild if guild is not None else _guild()
    ch.send = AsyncMock()
    ch.delete = AsyncMock()
    ch.set_permissions = AsyncMock()
    ch.history = MagicMock()
    return ch


def _seed_jail(
    ctx: AppContext,
    *,
    user_id: int,
    channel_id: int = 0,
    stored_roles: tuple[int, ...] = (700, 701),
    duration_seconds: int | None = None,
) -> int:
    with open_db(ctx.db_path) as conn:
        return create_jail(
            conn,
            guild_id=ctx.guild_id,
            user_id=user_id,
            moderator_id=1,
            reason="test",
            stored_roles=list(stored_roles),
            channel_id=channel_id,
            duration_seconds=duration_seconds,
        )


def _jail_row(ctx: AppContext, jail_id: int) -> dict:
    with open_db(ctx.db_path) as conn:
        return dict(
            conn.execute("SELECT * FROM jails WHERE id = ?", (jail_id,)).fetchone()
        )


def _audit_rows(ctx: AppContext, action: str) -> list[dict]:
    with open_db(ctx.db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit_log WHERE guild_id = ? AND action = ?"
                " ORDER BY id",
                (ctx.guild_id, action),
            ).fetchall()
        ]


# ── _do_unjail: departed subject ────────────────────────────────────


@pytest.mark.asyncio
async def test_unjail_releases_a_user_who_left_the_server(tmp_path):
    """The whole point: a User (not Member) can be released.

    Before ``_do_unjail`` grew an absent branch this raised ``AttributeError``
    on ``remove_roles`` — a departed member was unreleasable from Discord.
    """
    ctx = _make_ctx(tmp_path / "left.db")
    jail_channel = _text_channel(6000)
    guild = _guild(guild_id=ctx.guild_id)
    guild.get_channel = MagicMock(
        side_effect=lambda cid: jail_channel if cid == 6000 else None
    )
    jail_id = _seed_jail(ctx, user_id=42, channel_id=6000)
    target = _user(42)

    msg = await _do_unjail(ctx, guild, target, reason="time served")

    row = _jail_row(ctx, jail_id)
    assert row["status"] == "released"
    assert row["released_at"] is not None
    assert row["release_reason"] == "time served"
    # The jail channel is cleaned up exactly as it is for a present member —
    # the old DB-only shortcut left it orphaned in the category.
    jail_channel.delete.assert_awaited_once()
    # No Member-only role work was attempted.
    assert not hasattr(target, "remove_roles") or not target.remove_roles.called
    assert "no longer in the server" in msg
    # The mod is told the stored roles are gone for good.
    assert "2 stored role(s) were not restored" in msg


@pytest.mark.asyncio
async def test_unjail_of_departed_user_records_audit_note(tmp_path):
    ctx = _make_ctx(tmp_path / "leftaudit.db")
    guild = _guild(guild_id=ctx.guild_id)
    jail_id = _seed_jail(ctx, user_id=42)

    await _do_unjail(ctx, guild, _user(42), reason="closing out")

    rows = _audit_rows(ctx, "jail_release")
    assert len(rows) == 1
    extra = json.loads(rows[0]["extra"])
    assert extra["note"] == "user_left_guild"
    assert extra["jail_id"] == jail_id


@pytest.mark.asyncio
async def test_unjail_of_departed_user_without_channel_still_releases(tmp_path):
    """channel_id 0 (Manage Channels was missing at jail time) is releasable."""
    ctx = _make_ctx(tmp_path / "nochan.db")
    guild = _guild(guild_id=ctx.guild_id)
    jail_id = _seed_jail(ctx, user_id=42, channel_id=0)

    msg = await _do_unjail(ctx, guild, _user(42))

    assert _jail_row(ctx, jail_id)["status"] == "released"
    assert msg.startswith("✅")


@pytest.mark.asyncio
async def test_unjail_of_departed_user_with_no_stored_roles_omits_warning(tmp_path):
    ctx = _make_ctx(tmp_path / "noroles.db")
    guild = _guild(guild_id=ctx.guild_id)
    _seed_jail(ctx, user_id=42, stored_roles=())

    msg = await _do_unjail(ctx, guild, _user(42))

    assert "stored role" not in msg


# ── _do_unjail: present member (regression) ─────────────────────────


@pytest.mark.asyncio
async def test_unjail_still_restores_roles_for_a_present_member(tmp_path):
    ctx = _make_ctx(tmp_path / "present.db")
    member = _member(42, role_ids=(JAILED_ROLE_ID,))
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    jail_id = _seed_jail(ctx, user_id=42, stored_roles=(700, 701))

    msg = await _do_unjail(ctx, guild, member, reason="appeal granted")

    member.remove_roles.assert_awaited_once()
    member.add_roles.assert_awaited_once()
    restored = {r.id for r in member.add_roles.call_args.args}
    assert restored == {700, 701}
    assert _jail_row(ctx, jail_id)["status"] == "released"
    assert "released from jail" in msg
    # A present release carries no departed-user note.
    extra = json.loads(_audit_rows(ctx, "jail_release")[0]["extra"])
    assert "note" not in extra


@pytest.mark.asyncio
async def test_unjail_reports_not_jailed_when_no_active_row(tmp_path):
    ctx = _make_ctx(tmp_path / "nojail.db")
    guild = _guild(guild_id=ctx.guild_id)

    msg = await _do_unjail(ctx, guild, _user(42))

    assert "not currently jailed" in msg


# ── _do_unjail escapes the mod-typed release reason ──────────────────


def _guild_with_audit(ctx, *, members: list | None = None, log_channel_id: int = 9000):
    """``_guild`` wired with a resolvable mod-log channel, so the audit
    embed ``_do_unjail`` posts through ``_post_audit`` can be captured."""
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "log_channel_id", str(log_channel_id), guild_id=ctx.guild_id)
    guild = _guild(guild_id=ctx.guild_id, members=members)
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    guild.get_channel = MagicMock(
        side_effect=lambda cid: log_channel if cid == log_channel_id else None
    )
    return guild, log_channel


@pytest.mark.asyncio
async def test_unjail_reason_escaped_in_dm_and_audit_but_raw_in_write_backs(tmp_path):
    """The ``/unjail`` reason — an unbounded slash-command string, unlike the
    modal-bound reasons elsewhere in this module, since the dashboard route
    and the expiry loop share this same function — renders in the release DM
    and the mod-log audit embed, both of which need escaping. Discord's own
    audit-log ``reason=`` kwarg and the DB's structured audit-trail column
    are write-backs, never render surfaces, and must keep the raw text.
    """
    ctx = _make_ctx(tmp_path / "reason.db")
    member = _member(42, role_ids=(JAILED_ROLE_ID,))
    guild, log_channel = _guild_with_audit(ctx, members=[member])
    jail_id = _seed_jail(ctx, user_id=42, stored_roles=(700, 701))

    reason = "**forged** _reason_"
    await _do_unjail(ctx, guild, member, reason=reason)

    # DM embed description.
    dm_embed = member.send.await_args.kwargs["embed"]
    assert "\\*\\*forged\\*\\* \\_reason\\_" in dm_embed.description
    assert "**forged** _reason_" not in dm_embed.description

    # Mod-log audit embed description.
    audit_embed = log_channel.send.await_args.kwargs["embed"]
    assert "\\*\\*forged\\*\\* \\_reason\\_" in audit_embed.description
    assert "**forged** _reason_" not in audit_embed.description

    # Discord's own audit-log kwarg is a write-back, never escaped.
    assert member.remove_roles.await_args.kwargs["reason"] == f"Unjailed: {reason}"
    assert member.add_roles.await_args.kwargs["reason"] == f"Unjailed: {reason}"

    # The DB's structured audit-trail column and the jail row stay raw too.
    extra = json.loads(_audit_rows(ctx, "jail_release")[0]["extra"])
    assert extra["reason"] == reason
    assert _jail_row(ctx, jail_id)["release_reason"] == reason


@pytest.mark.asyncio
async def test_unjail_of_departed_user_reason_escaped_in_dm_and_audit(tmp_path):
    """Same escaping, on the no-``Member`` release path — a departed user
    still gets the DM and audit embeds, just no role restoration."""
    ctx = _make_ctx(tmp_path / "reason_left.db")
    guild, log_channel = _guild_with_audit(ctx)
    _seed_jail(ctx, user_id=42, stored_roles=())
    target = _user(42)

    reason = "~~struck~~ text"
    await _do_unjail(ctx, guild, target, reason=reason)

    dm_embed = target.send.await_args.kwargs["embed"]
    assert "\\~\\~struck\\~\\~ text" in dm_embed.description
    assert "~~struck~~ text" not in dm_embed.description

    audit_embed = log_channel.send.await_args.kwargs["embed"]
    assert "\\~\\~struck\\~\\~ text" in audit_embed.description
    assert "~~struck~~ text" not in audit_embed.description

    extra = json.loads(_audit_rows(ctx, "jail_release")[0]["extra"])
    assert extra["reason"] == reason


# ── resolve_release_target ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_release_target_prefers_the_guild_member(tmp_path):
    member = _member(42)
    guild = _guild(members=[member])
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=_user(42))
    bot.fetch_user = AsyncMock()

    assert await resolve_release_target(bot, guild, 42) is member
    bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_release_target_falls_back_to_cached_user(tmp_path):
    guild = _guild()
    cached = _user(42)
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=cached)
    bot.fetch_user = AsyncMock()

    assert await resolve_release_target(bot, guild, 42) is cached
    bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_release_target_fetches_when_uncached(tmp_path):
    guild = _guild()
    fetched = _user(42)
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=fetched)

    assert await resolve_release_target(bot, guild, 42) is fetched


@pytest.mark.asyncio
async def test_resolve_release_target_returns_none_for_unknown_account(tmp_path):
    guild = _guild()
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown user")
    )

    assert await resolve_release_target(bot, guild, 42) is None


@pytest.mark.asyncio
async def test_resolve_release_target_propagates_transient_api_errors(tmp_path):
    """A 5xx must not read as "this account doesn't exist".

    ``NotFound`` subclasses ``HTTPException``, so catching the parent would
    make a Discord blip look like a deleted account — and callers close the
    hold out on ``None``, skipping the transcript and orphaning the channel.
    """
    guild = _guild()
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(
        side_effect=discord.DiscordServerError(MagicMock(status=503), "upstream")
    )

    with pytest.raises(discord.HTTPException):
        await resolve_release_target(bot, guild, 42)


# ── check_jail_rejoin: missing channel ──────────────────────────────


@pytest.mark.asyncio
async def test_rejoin_recreates_a_deleted_jail_channel(tmp_path):
    """Without this the returning member sees an empty server and no notice."""
    ctx = _make_ctx(tmp_path / "rejoin.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    jail_id = _seed_jail(ctx, user_id=42, channel_id=6000)
    guild.get_channel = MagicMock(return_value=None)  # channel was deleted
    new_channel = _text_channel(6100)
    guild.create_text_channel = AsyncMock(return_value=new_channel)

    assert await check_jail_rejoin(ctx, member) is True

    guild.create_text_channel.assert_awaited_once()
    # The new id is written back, so the next release cleans up the right room.
    assert _jail_row(ctx, jail_id)["channel_id"] == 6100
    new_channel.set_permissions.assert_awaited_once()
    new_channel.send.assert_awaited_once()
    member.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejoin_creates_channel_when_original_jail_had_none(tmp_path):
    """channel_id 0 means Manage Channels was missing at jail time — retry now."""
    ctx = _make_ctx(tmp_path / "rejoin0.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    jail_id = _seed_jail(ctx, user_id=42, channel_id=0)
    new_channel = _text_channel(6200)
    guild.create_text_channel = AsyncMock(return_value=new_channel)

    assert await check_jail_rejoin(ctx, member) is True

    assert _jail_row(ctx, jail_id)["channel_id"] == 6200


@pytest.mark.asyncio
async def test_rejoin_survives_channel_creation_forbidden(tmp_path):
    """Still re-jails, still reports True — the hold matters more than the room."""
    ctx = _make_ctx(tmp_path / "rejoinforbidden.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    jail_id = _seed_jail(ctx, user_id=42, channel_id=6000)
    guild.create_text_channel = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no perms")
    )

    assert await check_jail_rejoin(ctx, member) is True

    member.add_roles.assert_awaited_once()
    assert _jail_row(ctx, jail_id)["status"] == "active"


@pytest.mark.asyncio
async def test_rejoin_uses_existing_channel_when_present(tmp_path):
    ctx = _make_ctx(tmp_path / "rejoinok.db")
    member = _member(42)
    existing = _text_channel(6000)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    guild.get_channel = MagicMock(
        side_effect=lambda cid: existing if cid == 6000 else None
    )
    _seed_jail(ctx, user_id=42, channel_id=6000)

    assert await check_jail_rejoin(ctx, member) is True

    guild.create_text_channel.assert_not_awaited()
    existing.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejoin_survives_channel_creation_http_error(tmp_path):
    """A guild at the channel cap raises plain HTTPException, not Forbidden.

    check_jail_rejoin is the first statement of on_member_join, so anything
    that escapes here aborts the member's whole join pipeline.
    """
    ctx = _make_ctx(tmp_path / "rejoincap.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42, channel_id=6000)
    guild.create_text_channel = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=400), "max channels")
    )

    assert await check_jail_rejoin(ctx, member) is True


@pytest.mark.asyncio
async def test_rejoin_preserves_integration_managed_roles(tmp_path):
    """Nitro Booster is re-granted on rejoin; stripping it wholesale 403s."""
    ctx = _make_ctx(tmp_path / "rejoinmanaged.db")
    member = _member(42, role_ids=(700,), managed_role_ids=(900,))
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42, channel_id=0)
    guild.create_text_channel = AsyncMock(return_value=_text_channel(6400))

    assert await check_jail_rejoin(ctx, member) is True

    stripped = {r.id for r in member.remove_roles.call_args.args}
    assert stripped == {700}  # the managed role and @everyone are left alone
    member.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejoin_reports_failure_instead_of_claiming_success(tmp_path):
    """A 403 on the role apply must not post "jail has been re-applied"."""
    ctx = _make_ctx(tmp_path / "rejoinrolefail.db")
    member = _member(42, role_ids=(700,))
    existing = _text_channel(6000)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    guild.get_channel = MagicMock(
        side_effect=lambda cid: existing if cid == 6000 else None
    )
    _seed_jail(ctx, user_id=42, channel_id=6000)
    member.remove_roles = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "role hierarchy")
    )

    # Still counts as jailed for the join pipeline — the hold is active in the
    # DB, so a welcome card plus auto-roles would compound the problem.
    assert await check_jail_rejoin(ctx, member) is True
    # ...but it must not have announced a successful re-jail in the channel.
    existing.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejoin_returns_false_without_an_active_jail(tmp_path):
    ctx = _make_ctx(tmp_path / "rejoinnone.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild

    assert await check_jail_rejoin(ctx, member) is False
    member.add_roles.assert_not_awaited()


# ── jail_rejoin_reconcile_sweep ─────────────────────────────────────


def _sweep_bot(guild) -> MagicMock:
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


@pytest.mark.asyncio
async def test_reconcile_reapplies_hold_for_a_roleless_rejoiner(tmp_path):
    """The offline-rejoin case: on_member_join never fired for them."""
    ctx = _make_ctx(tmp_path / "reconcile.db")
    member = _member(42)  # back with @everyone only, no Jailed role
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42, channel_id=0)
    guild.create_text_channel = AsyncMock(return_value=_text_channel(6300))

    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    member.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_leaves_a_member_holding_other_roles_alone(tmp_path):
    """A hand-released member must not have their roles wiped at boot."""
    ctx = _make_ctx(tmp_path / "reconcilekeep.db")
    member = _member(
        42, role_ids=(700, 701), joined_at=_now_dt() - timedelta(days=30)
    )
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42)

    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    member.add_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_skips_a_roleless_member_who_never_rejoined(tmp_path):
    """The case a role-count heuristic gets exactly backwards.

    Jailing strips every non-managed role, so a member released by hand — mod
    removes @Jailed and nothing else — holds no roles at all, and looks
    identical to a fresh rejoiner. Only the join timestamp separates them.
    """
    ctx = _make_ctx(tmp_path / "reconcilehandreleased.db")
    member = _member(42, joined_at=_now_dt() - timedelta(days=30))
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42)  # created just now, well after they joined

    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_skips_holds_that_already_expired(tmp_path):
    """Re-applying one only to release it 60s later is pure churn."""
    ctx = _make_ctx(tmp_path / "reconcileexpired.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42, duration_seconds=-5)

    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    member.add_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_continues_past_a_failing_member(tmp_path):
    """One bad member must not abandon everyone after them in the list.

    An escaping exception crashes the startup task; the resilient runner then
    restarts it to fail on the same member until it gives up for good.
    """
    ctx = _make_ctx(tmp_path / "reconcileresilient.db")
    broken = _member(42)
    ok = _member(43)
    guild = _guild(guild_id=ctx.guild_id, members=[broken, ok])
    broken.guild = guild
    ok.guild = guild
    broken.remove_roles = AsyncMock(side_effect=RuntimeError("boom"))
    broken.add_roles = AsyncMock(side_effect=RuntimeError("boom"))
    _seed_jail(ctx, user_id=42, channel_id=0)
    _seed_jail(ctx, user_id=43, channel_id=0)
    guild.create_text_channel = AsyncMock(return_value=_text_channel(6500))

    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    ok.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_skips_members_still_wearing_the_jailed_role(tmp_path):
    ctx = _make_ctx(tmp_path / "reconcileintact.db")
    member = _member(42, role_ids=(JAILED_ROLE_ID,))
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild
    _seed_jail(ctx, user_id=42)

    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    member.add_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_ignores_jails_for_absent_members(tmp_path):
    ctx = _make_ctx(tmp_path / "reconcileabsent.db")
    guild = _guild(guild_id=ctx.guild_id, members=[])
    _seed_jail(ctx, user_id=42)

    # No member to act on and no crash — the rejoin listener covers their return.
    await jail_rejoin_reconcile_sweep(_sweep_bot(guild), ctx)

    assert _jail_row(ctx, 1)["status"] == "active"


# ── jail_expiry_loop: departed member ───────────────────────────────


@pytest.mark.asyncio
async def test_expiry_loop_releases_departed_member_with_audit(tmp_path, monkeypatch):
    """The loop used to close these rows with no audit row and no cleanup."""
    ctx = _make_ctx(tmp_path / "expiry.db")
    jail_channel = _text_channel(6000)
    guild = _guild(guild_id=ctx.guild_id, members=[])
    guild.get_channel = MagicMock(
        side_effect=lambda cid: jail_channel if cid == 6000 else None
    )
    jail_id = _seed_jail(ctx, user_id=42, channel_id=6000, duration_seconds=-5)

    bot = _sweep_bot(guild)
    bot.is_closed = MagicMock(side_effect=[False, True])
    bot.get_user = MagicMock(return_value=_user(42))
    bot.fetch_user = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await jail_expiry_loop(bot, ctx)

    assert _jail_row(ctx, jail_id)["status"] == "released"
    jail_channel.delete.assert_awaited_once()
    rows = _audit_rows(ctx, "jail_release")
    assert len(rows) == 1
    assert json.loads(rows[0]["extra"])["note"] == "user_left_guild"


# ── on_member_remove listener (cog wiring) ──────────────────────────


def _transcript_count(ctx: AppContext, jail_id: int) -> int:
    with open_db(ctx.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE record_type = 'jail'"
            " AND record_id = ?",
            (jail_id,),
        ).fetchone()[0]


def _departure_setup(tmp_path, name, *, present: bool = False):
    """A jailed member who has left (absent from the cache) and their channel."""
    ctx = _make_ctx(tmp_path / name)
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member] if present else [])
    jail_channel = _text_channel(6000, guild)
    guild.get_channel = MagicMock(
        side_effect=lambda cid: jail_channel if cid == 6000 else None
    )
    member.guild = guild
    jail_id = _seed_jail(ctx, user_id=42, channel_id=6000)
    with open_db(ctx.db_path) as conn:
        jail = get_active_jail(conn, ctx.guild_id, 42)
    return ctx, guild, member, jail_channel, jail_id, jail


# ── retire_departed_jail_channel ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("banned", "stage"), [(False, "member left"), (True, "member banned")]
)
async def test_departure_archives_then_deletes_the_jail_channel(
    tmp_path, banned, stage
):
    """Leaves, kicks and bans all lose the channel — but only after it's saved.

    Reverting to the old keep-the-channel behaviour fails here. It is safe to
    delete because check_jail_rejoin rebuilds a missing channel on rejoin.
    """
    ctx, guild, member, jail_channel, jail_id, jail = _departure_setup(
        tmp_path, f"retire{banned}.db"
    )
    saved_before_delete: list[int] = []
    jail_channel.delete = AsyncMock(
        side_effect=lambda **_: saved_before_delete.append(
            _transcript_count(ctx, jail_id)
        )
    )

    deleted = await retire_departed_jail_channel(
        ctx, guild, member, jail, banned=banned, note="🚪 left"
    )

    assert deleted is True
    assert saved_before_delete == [1]
    # The departure note lands before the archive, so it is in the transcript.
    jail_channel.send.assert_awaited_once()
    with open_db(ctx.db_path) as conn:
        (content,) = conn.execute(
            "SELECT content FROM transcripts WHERE record_id = ?", (jail_id,)
        ).fetchone()
    assert json.loads(content)["transcript_stage"] == stage
    # The hold itself is untouched — a rejoin re-applies it.
    assert _jail_row(ctx, jail_id)["status"] == "active"


@pytest.mark.asyncio
async def test_departure_keeps_the_channel_when_the_transcript_fails(tmp_path):
    """Never destroy an appeal we failed to archive."""
    ctx, guild, member, jail_channel, jail_id, jail = _departure_setup(
        tmp_path, "retire_nosave.db"
    )
    jail_channel.history = MagicMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom")
    )
    posted = AsyncMock()
    import bot_modules.moderation.jail.jail_commands as jc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jc, "_post_audit", posted)
        deleted = await retire_departed_jail_channel(
            ctx, guild, member, jail, banned=False, note="🚪 left"
        )

    assert deleted is False
    jail_channel.delete.assert_not_awaited()
    assert posted.await_args.args[2].title == "⚠️ Jail Channel Kept"


@pytest.mark.asyncio
async def test_departure_still_deletes_when_only_the_transcript_post_fails(tmp_path):
    """Stored is archived: a failed post to the transcript channel isn't a reason
    to keep the room, and mods are pointed at the dashboard copy instead of being
    told (falsely) that nothing was saved."""
    ctx, guild, member, jail_channel, jail_id, jail = _departure_setup(
        tmp_path, "retire_nopost.db"
    )
    posted = AsyncMock()
    import bot_modules.moderation.jail.jail_commands as jc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jc, "_post_audit", posted)
        mp.setattr(
            jc,
            "_post_transcript",
            AsyncMock(side_effect=discord.HTTPException(MagicMock(status=413), "too big")),
        )
        deleted = await retire_departed_jail_channel(
            ctx, guild, member, jail, banned=False, note="🚪 left"
        )

    assert deleted is True
    assert _transcript_count(ctx, jail_id) == 1
    jail_channel.delete.assert_awaited_once()
    assert posted.await_args.args[2].title == "⚠️ Jail Transcript Not Posted"


@pytest.mark.asyncio
async def test_departure_keeps_the_channel_if_they_rejoined_meanwhile(tmp_path):
    """A fast rejoin reuses this channel — deleting it would strand them."""
    ctx, guild, member, jail_channel, jail_id, jail = _departure_setup(
        tmp_path, "retire_back.db", present=True
    )

    deleted = await retire_departed_jail_channel(
        ctx, guild, member, jail, banned=False, note="🚪 left"
    )

    assert deleted is False
    assert _transcript_count(ctx, jail_id) == 1
    jail_channel.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_departure_reports_a_failed_channel_delete(tmp_path):
    ctx, guild, member, jail_channel, jail_id, jail = _departure_setup(
        tmp_path, "retire_nodelete.db"
    )
    jail_channel.delete = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no Manage Channels")
    )
    posted = AsyncMock()
    import bot_modules.moderation.jail.jail_commands as jc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jc, "_post_audit", posted)
        deleted = await retire_departed_jail_channel(
            ctx, guild, member, jail, banned=False, note="🚪 left"
        )

    assert deleted is False
    assert posted.await_args.args[2].title == "⚠️ Jail Channel Not Deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", [0, 6999], ids=["never-created", "gone"])
async def test_departure_with_no_channel_does_nothing(tmp_path, channel_id):
    ctx, guild, member, jail_channel, jail_id, jail = _departure_setup(
        tmp_path, f"retire_none{channel_id}.db"
    )
    jail = {**jail, "channel_id": channel_id}

    assert (
        await retire_departed_jail_channel(
            ctx, guild, member, jail, banned=False, note="🚪 left"
        )
        is False
    )
    assert _transcript_count(ctx, jail_id) == 0


@pytest.mark.asyncio
async def test_leaving_while_jailed_is_recorded_and_retires_the_channel(tmp_path):
    """Cog wiring: the listener archives + deletes and records that it did."""
    from bot_modules.moderation.jail.cog import JailCog

    ctx, guild, member, jail_channel, jail_id, _ = _departure_setup(
        tmp_path, "left_event.db"
    )

    _bot = MagicMock()
    _bot.ctx = ctx
    cog = JailCog(_bot)
    await cog._note_jailed_member_left(member)

    rows = _audit_rows(ctx, "jail_member_left")
    assert len(rows) == 1
    extra = json.loads(rows[0]["extra"])
    assert extra["jail_id"] == jail_id
    assert extra["channel_deleted"] is True
    jail_channel.delete.assert_awaited_once()
    assert _jail_row(ctx, jail_id)["status"] == "active"


@pytest.mark.asyncio
async def test_banning_a_jailed_member_is_not_reported_as_leaving(tmp_path):
    """on_member_remove fires for bans too — don't promise a re-jail."""
    from bot_modules.moderation.jail.cog import JailCog

    ctx = _make_ctx(tmp_path / "banned.db")
    jail_channel = _text_channel(6000)
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    guild.get_channel = MagicMock(
        side_effect=lambda cid: jail_channel if cid == 6000 else None
    )
    guild.fetch_ban = AsyncMock(return_value=MagicMock())  # they are banned
    member.guild = guild
    _seed_jail(ctx, user_id=42, channel_id=6000)

    _bot = MagicMock()
    _bot.ctx = ctx
    cog = JailCog(_bot)
    await cog._note_jailed_member_left(member)

    assert json.loads(_audit_rows(ctx, "jail_member_left")[0]["extra"])["banned"] is True
    note = jail_channel.send.call_args.args[0]
    assert "banned" in note.lower()
    assert "rejoining re-applies" not in note


@pytest.mark.asyncio
async def test_leaving_falls_back_to_neutral_wording_without_ban_perms(tmp_path):
    from bot_modules.moderation.jail.cog import JailCog

    ctx = _make_ctx(tmp_path / "bannoperm.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    guild.fetch_ban = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no Ban Members")
    )
    member.guild = guild
    _seed_jail(ctx, user_id=42)

    _bot = MagicMock()
    _bot.ctx = ctx
    cog = JailCog(_bot)
    await cog._note_jailed_member_left(member)

    assert json.loads(_audit_rows(ctx, "jail_member_left")[0]["extra"])["banned"] is False


@pytest.mark.asyncio
async def test_leaving_without_an_active_jail_records_nothing(tmp_path):
    from bot_modules.moderation.jail.cog import JailCog

    ctx = _make_ctx(tmp_path / "left_nojail.db")
    member = _member(42)
    guild = _guild(guild_id=ctx.guild_id, members=[member])
    member.guild = guild

    _bot = MagicMock()
    _bot.ctx = ctx
    cog = JailCog(_bot)
    await cog._note_jailed_member_left(member)

    assert _audit_rows(ctx, "jail_member_left") == []


@pytest.mark.asyncio
async def test_expiry_loop_closes_row_for_an_unresolvable_account(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path / "expirygone.db")
    guild = _guild(guild_id=ctx.guild_id, members=[])
    jail_id = _seed_jail(ctx, user_id=42, duration_seconds=-5)

    bot = _sweep_bot(guild)
    bot.is_closed = MagicMock(side_effect=[False, True])
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone")
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await jail_expiry_loop(bot, ctx)

    assert _jail_row(ctx, jail_id)["status"] == "released"
    extra = json.loads(_audit_rows(ctx, "jail_release")[0]["extra"])
    assert extra["note"] == "user_unresolvable"
