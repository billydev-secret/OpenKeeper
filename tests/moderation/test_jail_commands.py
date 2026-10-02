"""Tests for bot_modules.moderation.jail.jail_commands helper functions.

Focused on the prelaunch P0 fix: ``_get_mod_role_ids``/``_get_admin_role_ids``
take an explicit ``guild_id``, and ``_is_admin`` looks up roles
via ``ctx.guild_config(member.guild.id)`` rather than the home-guild flat
fields on ``AppContext``. Without this scoping, a 2nd guild would silently
inherit the home guild's mod/admin role list.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.moderation.jail.jail_commands as jc

from bot_modules.moderation.jail.jail_commands import (
    _get_admin_role_ids,
    _get_mod_role_ids,
    _is_admin,
)
from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from tests.db_template import migrated_db


def _make_ctx(db_path, guild_id: int = 10) -> AppContext:
    migrated_db(db_path)
    return AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=guild_id,
        debug=True,
    )


def _member(role_ids: list[int], *, manage_guild=False, administrator=False, guild_id=10):
    m = MagicMock()
    m.roles = [MagicMock(id=rid) for rid in role_ids]
    m.guild_permissions = MagicMock(manage_guild=manage_guild, administrator=administrator)
    m.guild = MagicMock()
    m.guild.id = guild_id
    return m


# ── _get_mod_role_ids / _get_admin_role_ids ──────────────────────────


def test_get_mod_role_ids_scoped_to_guild(tmp_path):
    """_get_mod_role_ids must use the guild_id arg, not the home guild flats."""
    ctx = _make_ctx(tmp_path / "jc1.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100,101", guild_id=10)
        _db_set(conn, "mod_role_ids", "200,201", guild_id=20)

    assert _get_mod_role_ids(ctx, 10) == {100, 101}
    assert _get_mod_role_ids(ctx, 20) == {200, 201}


def test_get_admin_role_ids_scoped_to_guild(tmp_path):
    ctx = _make_ctx(tmp_path / "jc2.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "admin_role_ids", "500", guild_id=10)
        _db_set(conn, "admin_role_ids", "600,601", guild_id=20)

    assert _get_admin_role_ids(ctx, 10) == {500}
    assert _get_admin_role_ids(ctx, 20) == {600, 601}


def test_get_mod_role_ids_returns_empty_for_unconfigured_non_home_guild(tmp_path):
    """Unconfigured 2nd guild must NOT inherit home-guild roles via legacy fallback."""
    ctx = _make_ctx(tmp_path / "jc3.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100", guild_id=0)  # legacy
        _db_set(conn, "mod_role_ids", "111", guild_id=10)  # home

    assert _get_mod_role_ids(ctx, 20) == set()


# ── _is_admin ─────────────────────────────────────────────────────────
#
# The mod check it once sat beside is ``AppContext.member_is_mod`` now; its
# cases live in tests/core/test_app_context.py.


def test_is_admin_true_for_administrator_short_circuit(tmp_path):
    ctx = _make_ctx(tmp_path / "jc9.db", guild_id=10)
    member = _member(role_ids=[], administrator=True, guild_id=10)
    assert _is_admin(member, ctx) is True


def test_is_admin_false_for_mod_role_only(tmp_path):
    """Mod role does NOT grant admin in _is_admin (the inverse direction does grant
    mod via member_is_mod, but member_is_admin requires admin_role_ids)."""
    ctx = _make_ctx(tmp_path / "jc10.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=10)

    member = _member(role_ids=[777], guild_id=10)
    assert _is_admin(member, ctx) is False


def test_is_admin_true_for_configured_admin_role(tmp_path):
    ctx = _make_ctx(tmp_path / "jc11.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "admin_role_ids", "999", guild_id=10)

    member = _member(role_ids=[999], guild_id=10)
    assert _is_admin(member, ctx) is True


# ── jail keys are read per guild through GuildConfig ─────────────────


def test_jail_keys_are_guild_scoped(tmp_path):
    ctx = _make_ctx(tmp_path / "jc12.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5", guild_id=10)
        _db_set(conn, "jailed_role_id", "7", guild_id=20)

    assert ctx.guild_config(10).jailed_role_id == 5
    assert ctx.guild_config(20).jailed_role_id == 7


def test_unset_jail_key_reads_zero(tmp_path):
    ctx = _make_ctx(tmp_path / "jc13.db", guild_id=10)
    assert ctx.guild_config(10).ticket_category_id == 0


# ── warning threshold read honors the guild-scoped row ───────────────
#
# The dashboard writes ``warning_threshold`` per-guild; the jail cog's
# threshold-alert read must pass ``guild_id`` or it silently reads the
# legacy ``guild_id=0`` row and the hard-coded "3" default always wins.


def test_read_warning_threshold_uses_guild_scoped_row(tmp_path):
    from bot_modules.moderation.jail.cog import _read_warning_threshold

    ctx = _make_ctx(tmp_path / "jc14.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "warning_threshold", "3", guild_id=0)  # legacy
        _db_set(conn, "warning_threshold", "5", guild_id=10)

    # The configured guild row must win over the legacy default of 3.
    assert _read_warning_threshold(ctx, 10) == 5


def test_read_warning_threshold_defaults_to_three(tmp_path):
    from bot_modules.moderation.jail.cog import _read_warning_threshold

    ctx = _make_ctx(tmp_path / "jc15.db", guild_id=10)
    assert _read_warning_threshold(ctx, 10) == 3


# ── /modinfo's embed-title user label is escaped ──────────────────────
#
# ``str(user)`` is the account's global username (+ legacy #discriminator)
# and lands straight in build_modinfo_embed's TITLE. A migrated username is
# charset-restricted, but a pre-migration legacy one can still carry
# Discord's older, broader charset.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            "[Click here](https://evil.example)#0",
            "\\[Click here](https://evil.example)#0",
            id="masked-link-legacy-username",
        ),
        pytest.param(
            "__spooky__",
            "\\_\\_spooky\\_\\_",
            id="underscore-emphasis",
        ),
        pytest.param("plainname#0", "plainname#0", id="plain-username-untouched"),
    ],
)
def test_modinfo_user_label_is_escaped(raw, expected):
    from bot_modules.moderation.jail.cog import _modinfo_user_label

    user = MagicMock(spec=discord.Member)
    user.__str__ = MagicMock(return_value=raw)

    assert _modinfo_user_label(user) == expected


# ── policy vote timeout sweeps every guild ───────────────────────────
#
# ``/policy open`` works on any server and each server sets its own voting
# deadline on its own dashboard, so a sweep that only ever looked at the home
# guild left proposals elsewhere stuck in 'voting' forever.


async def test_policy_vote_sweep_resolves_on_every_guild(tmp_path, monkeypatch):
    import time

    from bot_modules.moderation.jail import jail_commands as jc
    from bot_modules.moderation.service import (
        create_policy_ticket,
        start_policy_vote,
    )

    ctx = _make_ctx(tmp_path / "jc16.db", guild_id=10)
    pids: dict[int, int] = {}
    with open_db(ctx.db_path) as conn:
        for gid in (10, 20):
            pid = create_policy_ticket(
                conn, guild_id=gid, creator_id=1, channel_id=5,
                title="t", description="d",
            )
            start_policy_vote(conn, pid, vote_text="v")
            conn.execute(
                "UPDATE policy_tickets SET vote_started_at = ? WHERE id = ?",
                (time.time() - 10 * 24 * 3600, pid),
            )
            pids[gid] = pid

    resolved: list[tuple[int, int]] = []

    async def _fake_resolve(bot, ctx_, guild, policy):
        resolved.append((guild.id, policy["id"]))

    monkeypatch.setattr(jc, "_resolve_expired_policy", _fake_resolve)

    bot = MagicMock()
    bot.guilds = [MagicMock(id=10), MagicMock(id=20)]

    await jc.sweep_expired_policy_votes(bot, ctx)

    assert sorted(resolved) == [(10, pids[10]), (20, pids[20])]


async def test_policy_vote_sweep_honors_a_per_guild_deadline_of_zero(
    tmp_path, monkeypatch
):
    """A guild that sets the deadline to 0 turns auto-resolution off — for
    itself only."""
    import time

    from bot_modules.moderation.jail import jail_commands as jc
    from bot_modules.moderation.service import (
        create_policy_ticket,
        start_policy_vote,
    )

    ctx = _make_ctx(tmp_path / "jc17.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "policy_vote_timeout_hours", "0", guild_id=20)
        for gid in (10, 20):
            pid = create_policy_ticket(
                conn, guild_id=gid, creator_id=1, channel_id=5,
                title="t", description="d",
            )
            start_policy_vote(conn, pid, vote_text="v")
            conn.execute(
                "UPDATE policy_tickets SET vote_started_at = ? WHERE id = ?",
                (time.time() - 10 * 24 * 3600, pid),
            )

    resolved: list[int] = []

    async def _fake_resolve(bot, ctx_, guild, policy):
        resolved.append(guild.id)

    monkeypatch.setattr(jc, "_resolve_expired_policy", _fake_resolve)

    bot = MagicMock()
    bot.guilds = [MagicMock(id=10), MagicMock(id=20)]

    await jc.sweep_expired_policy_votes(bot, ctx)

    assert resolved == [10]


# ── _post_audit: record cards, and pings that actually reach someone ──────


def _audit_channel_ctx(tmp_path):
    """A ctx with a mod-log channel, plus the guild/channel mocks it resolves."""
    ctx = _make_ctx(str(tmp_path / "audit.db"))
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "log_channel_id", "555", guild_id=10)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.id = 10
    guild.get_channel.return_value = channel
    return ctx, guild, channel


@pytest.mark.asyncio
async def test_post_audit_stamps_a_record_card(tmp_path):
    """Audit cards are the mod-log's scrollback, so they carry a timestamp.

    Stamped here rather than in each of the ~20 builders — which is how 17 of
    them came to be missing one. → embed_style_guide.md § Timestamps
    """
    ctx, guild, channel = _audit_channel_ctx(tmp_path)
    embed = discord.Embed(title="🔒 Member Jailed")
    assert embed.timestamp is None

    await jc._post_audit(ctx, guild, embed)

    sent = channel.send.await_args.kwargs["embed"]
    assert sent.timestamp is not None


@pytest.mark.asyncio
async def test_post_audit_keeps_a_timestamp_the_builder_already_set(tmp_path):
    ctx, guild, channel = _audit_channel_ctx(tmp_path)
    stamped = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    embed = discord.Embed(title="📩 Ticket Opened", timestamp=stamped)

    await jc._post_audit(ctx, guild, embed)

    assert channel.send.await_args.kwargs["embed"].timestamp == stamped


@pytest.mark.asyncio
async def test_post_audit_defaults_to_pinging_nobody(tmp_path):
    ctx, guild, channel = _audit_channel_ctx(tmp_path)

    await jc._post_audit(ctx, guild, discord.Embed(title="🔓 Member Released"))

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["allowed_mentions"].roles is False


@pytest.mark.asyncio
async def test_post_audit_allow_lists_exactly_the_roles_it_pings(tmp_path):
    """The warning-threshold alert used to put its pings in the embed, where a
    mention notifies nobody. They ride in ``content=`` now, and the send has to
    allow-list them or Discord suppresses them anyway."""
    ctx, guild, channel = _audit_channel_ctx(tmp_path)

    await jc._post_audit(
        ctx,
        guild,
        discord.Embed(title="🚨 Warning Threshold Reached"),
        content="<@&100> <@&200>",
        ping_role_ids=[100, 200],
    )

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&100> <@&200>"
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [100, 200]
    # AllowedMentions' unset fields default to *permitted*, so naming only
    # `roles` would widen every audit card that pings from "pings nobody" to
    # "pings whoever the text happens to name".
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False


# ── finalize_policy_vote escapes the admin-typed title/vote-text columns ──
#
# Every render site below reads the same policy_tickets.title/.vote_text
# columns the ``/policy ballot`` modal and ``/policy open`` command write —
# admin/mod-typed free text, not the bot's own words. Markdown reaching any
# of these unescaped lets a title of e.g. ``**forged**`` bold text the bot
# never composed into a public channel message, a public embed field, or the
# mod-log audit trail.


@pytest.mark.asyncio
async def test_finalize_policy_vote_adopted_escapes_every_render_site(
    tmp_path, monkeypatch
):
    from bot_modules.moderation.service import create_policy_ticket, start_policy_vote

    ctx, guild, channel = _audit_channel_ctx(tmp_path)
    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=1, channel_id=556,
            title="**forged title**", description="d",
        )
        start_policy_vote(conn, pid, vote_text="__forged__ vote text")

    channel.send = AsyncMock()
    channel.delete = AsyncMock()
    # The transcript path (history read, markdown render, DM) is unrelated
    # to what's under test here and heavy to mock faithfully — isolated out,
    # same as ``test_vote_button_finalizes_...`` does for the whole finalizer.
    monkeypatch.setattr(jc, "_collect_and_post_transcript", AsyncMock())

    ok = await jc.finalize_policy_vote(
        ctx, guild, pid, "adopted",
        channel=channel,
        yes_ids=[1], no_ids=[], abstain_ids=[],
        actor_id=1, timed_out=False,
    )
    assert ok is True
    assert channel.send.await_count == 3

    # 1. The public "Policy adopted!" channel announcement (message content).
    announcement = channel.send.await_args_list[0].args[0]
    assert '"\\*\\*forged title\\*\\*"' in announcement
    assert "\\_\\_forged\\_\\_ vote text" in announcement
    assert "**forged title**" not in announcement

    # 2. The "Adopted Policies" embed — used to be hand-rolled and raw here;
    # now the same shared builder jail_cog's manual `/policy close` uses.
    adopted_embed = channel.send.await_args_list[1].kwargs["embed"]
    assert adopted_embed.fields[0].name == "\\*\\*forged title\\*\\*"
    assert adopted_embed.fields[0].value == "\\_\\_forged\\_\\_ vote text"

    # 3. The mod-log "Policy Adopted" audit embed.
    audit_embed = channel.send.await_args_list[2].kwargs["embed"]
    assert "\\*\\*forged title\\*\\*" in audit_embed.description
    assert "\\_\\_forged\\_\\_ vote text" in audit_embed.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "timed_out"),
    [
        pytest.param("rejected_no_quorum", True, id="timed-out-no-quorum"),
        pytest.param("rejected", False, id="rejected"),
    ],
)
async def test_finalize_policy_vote_non_adopted_escapes_vote_text(
    tmp_path, monkeypatch, outcome, timed_out
):
    from bot_modules.moderation.service import create_policy_ticket, start_policy_vote

    ctx, guild, channel = _audit_channel_ctx(tmp_path)
    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=1, channel_id=556,
            title="t", description="d",
        )
        start_policy_vote(conn, pid, vote_text="~~struck~~ text")

    channel.send = AsyncMock()
    channel.delete = AsyncMock()
    monkeypatch.setattr(jc, "_collect_and_post_transcript", AsyncMock())

    ok = await jc.finalize_policy_vote(
        ctx, guild, pid, outcome,
        channel=channel,
        yes_ids=[], no_ids=[], abstain_ids=[],
        actor_id=1, timed_out=timed_out,
    )
    assert ok is True
    assert channel.send.await_count == 2

    announcement = channel.send.await_args_list[0].args[0]
    assert "\\~\\~struck\\~\\~ text" in announcement
    assert "~~struck~~ text" not in announcement

    audit_embed = channel.send.await_args_list[1].kwargs["embed"]
    assert "\\~\\~struck\\~\\~ text" in audit_embed.description


# ── The finalizer acts on the policy's own channel, never the press's ─────
#
# ``finalize_policy_vote`` archives and then DELETES the channel it is handed.
# The sweeper resolved it from ``policy["channel_id"]``; the button handler
# passed ``interaction.channel``. Those were the same channel for as long as a
# vote button could only exist inside the proposal channel — the moment one
# lives anywhere else (a community ballot thread, a message a mod copied), the
# button path deletes the wrong channel. Both paths resolve the recorded
# channel now.


def _voting_policy(ctx, *, guild_id: int, channel_id: int) -> int:
    from bot_modules.moderation.service import create_policy_ticket, start_policy_vote

    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=guild_id, creator_id=1, channel_id=channel_id,
            title="t", description="d",
        )
        start_policy_vote(conn, pid, vote_text="v")
    return pid


def _vote_interaction(ctx, *, guild_id: int, policy_channel_id: int, press_channel_id: int):
    """A press arriving from a channel that is NOT the policy's own channel."""
    voter = MagicMock(spec=discord.Member)
    voter.id = 42
    voter.bot = False
    voter.roles = []
    voter.guild_permissions = MagicMock(manage_guild=True, administrator=True)
    # Real strings: the tally embed resolves ids to names and escapes markdown,
    # and re.sub cannot take a MagicMock. Nothing here asserts on the name — it
    # just has to be a name.
    voter.display_name = "Voter"
    voter.name = "voter"

    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.members = [voter]
    # A bare MagicMock answers get_member() for EVERY id, so the tally embed's
    # name resolver would "find" a mock member for every voter and then try to
    # markdown-escape a MagicMock. Behave like a real cache: the voter is
    # present, nobody else is.
    guild.get_member.side_effect = lambda uid: voter if uid == voter.id else None
    voter.guild = guild

    policy_channel = MagicMock(spec=discord.TextChannel)
    policy_channel.id = policy_channel_id
    guild.get_channel.return_value = policy_channel

    press_channel = MagicMock(spec=discord.TextChannel)
    press_channel.id = press_channel_id

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = MagicMock()
    interaction.client.ctx = ctx
    interaction.user = voter
    interaction.guild = guild
    interaction.channel = press_channel
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction, policy_channel, press_channel


@pytest.mark.asyncio
async def test_vote_button_finalizes_against_the_policys_own_channel(
    tmp_path, monkeypatch
):
    ctx = _make_ctx(tmp_path / "jc18.db", guild_id=10)
    policy_id = _voting_policy(ctx, guild_id=10, channel_id=777)
    interaction, policy_channel, press_channel = _vote_interaction(
        ctx, guild_id=10, policy_channel_id=777, press_channel_id=999
    )

    handed: list[object] = []

    async def _fake_finalize(ctx_, guild, pid, outcome, *, channel, **kw):
        handed.append(channel)
        return True

    monkeypatch.setattr(jc, "finalize_policy_vote", _fake_finalize)

    await jc._handle_policy_vote(interaction, policy_id, "yes")

    assert handed, "the sole eligible voter voting yes must finalize the vote"
    assert handed[0] is policy_channel
    assert handed[0] is not press_channel


@pytest.mark.asyncio
async def test_vote_button_hands_no_channel_when_the_policys_own_is_gone(
    tmp_path, monkeypatch
):
    """A deleted proposal channel must finalize with ``channel=None`` — never
    fall back to whichever channel the press came from."""
    ctx = _make_ctx(tmp_path / "jc19.db", guild_id=10)
    policy_id = _voting_policy(ctx, guild_id=10, channel_id=777)
    interaction, _policy_channel, _press = _vote_interaction(
        ctx, guild_id=10, policy_channel_id=777, press_channel_id=999
    )
    interaction.guild.get_channel.return_value = None

    handed: list[object] = []

    async def _fake_finalize(ctx_, guild, pid, outcome, *, channel, **kw):
        handed.append(channel)
        return True

    monkeypatch.setattr(jc, "finalize_policy_vote", _fake_finalize)

    await jc._handle_policy_vote(interaction, policy_id, "yes")

    assert handed == [None]


# ── One roster rule, one implementation ───────────────────────────────────


def test_guild_eligible_voters_matches_the_shared_helper(tmp_path):
    """The three sites that used to rebuild the roster inline now share
    ``jail.logic.eligible_voters`` — bots out, administrators in, configured
    mod/admin role holders in, everyone else out."""
    from bot_modules.moderation.jail.logic import eligible_voters

    ctx = _make_ctx(tmp_path / "jc20.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100", guild_id=10)
        _db_set(conn, "admin_role_ids", "200", guild_id=10)

    def _m(uid, *, roles=(), admin=False, bot=False):
        m = MagicMock(spec=discord.Member)
        m.id = uid
        m.bot = bot
        m.roles = [MagicMock(id=r) for r in roles]
        m.guild_permissions = MagicMock(administrator=admin)
        return m

    members = [
        _m(1, admin=True),             # administrator
        _m(2, roles=[100]),            # mod role
        _m(3, roles=[200]),            # admin role
        _m(4, roles=[999]),            # unrelated role
        _m(5, roles=[100], bot=True),  # a bot holding a mod role
    ]
    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.members = members

    assert jc.guild_eligible_voters(ctx, guild) == {1, 2, 3}
    assert jc.guild_eligible_voters(ctx, guild) == eligible_voters(
        [
            {
                "user_id": m.id,
                "is_bot": m.bot,
                "role_ids": [r.id for r in m.roles],
                "is_administrator": m.guild_permissions.administrator,
            }
            for m in members
        ],
        {100},
        {200},
    )


# ── Community ballots ─────────────────────────────────────────────────


def _open_ballot_row(ctx, *, closes_at: float = 0.0, guild_id: int = 10) -> int:
    from bot_modules.moderation.jail import policy_ballot_service as pbs
    from bot_modules.moderation.service import create_policy_ticket

    with open_db(ctx.db_path) as conn:
        policy_id = create_policy_ticket(
            conn, guild_id=guild_id, creator_id=1, channel_id=0,
            title="q", description="",
        )
        return pbs.open_ballot(
            conn, guild_id=guild_id, policy_id=policy_id, channel_id=500,
            question="Quiet hours?", opened_by=1, closes_at=closes_at,
        )


def _presser(ctx, *, can_view: bool = True, is_bot: bool = False, admin: bool = False):
    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.bot = is_bot
    member.roles = []
    member.guild_permissions = MagicMock(manage_guild=admin, administrator=admin)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.members = [member]
    # No member cache: the name resolver falls through to `known_users` and
    # then to a mention, which is what the accent/name paths under test want.
    guild.get_member = MagicMock(return_value=None)
    guild.icon = None
    member.guild = guild

    channel = MagicMock(spec=discord.Thread)
    channel.id = 600
    channel.permissions_for = MagicMock(
        return_value=MagicMock(view_channel=can_view)
    )

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = MagicMock()
    interaction.client.ctx = ctx
    interaction.user = member
    interaction.guild = guild
    interaction.channel = channel
    interaction.channel_id = channel.id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_a_ballot_press_records_the_vote(tmp_path):
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b1.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    interaction = _presser(ctx)

    await jc._handle_ballot_vote(interaction, ballot_id, "yes")

    with open_db(ctx.db_path) as conn:
        assert pbs.tally_ballot(conn, ballot_id)["yes"] == [42]
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_a_presser_who_cannot_see_the_thread_is_refused(tmp_path):
    """Visibility *is* the electorate, so it is re-read from Discord on every
    press: a member who lost the role that let them into the channel stops
    being able to vote from that moment."""
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b2.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    interaction = _presser(ctx, can_view=False)

    await jc._handle_ballot_vote(interaction, ballot_id, "yes")

    with open_db(ctx.db_path) as conn:
        assert pbs.get_ballot_votes(conn, ballot_id) == []
    assert "❌" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_press_on_a_closed_ballot_is_refused(tmp_path):
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b3.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.close_ballot(conn, ballot_id, closed_by=1)
    interaction = _presser(ctx)

    await jc._handle_ballot_vote(interaction, ballot_id, "yes")

    with open_db(ctx.db_path) as conn:
        assert pbs.get_ballot_votes(conn, ballot_id) == []


@pytest.mark.asyncio
async def test_the_close_button_refuses_a_non_moderator(tmp_path):
    """The Close button sits on a card everyone in the thread can see, so it
    cannot rely on being invisible to members — it refuses on press."""
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b4.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    interaction = _presser(ctx, admin=False)

    await jc.PolicyBallotCloseButton(ballot_id).callback(interaction)

    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None and pbs.is_open(ballot)
    assert "❌" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_the_close_button_freezes_the_result_for_a_moderator(tmp_path):
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b5.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=1, choice="yes"
        )
    interaction = _presser(ctx, admin=True)

    await jc.PolicyBallotCloseButton(ballot_id).callback(interaction)

    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None
        assert ballot["outcome"] == pbs.OUTCOME_PASSED
        assert ballot["closed_by"] == 42
        # A ballot is recorded as a policy ticket, so closing it must close
        # that ticket too — otherwise a decided ballot sits in the dashboard's
        # live queue forever.
        ticket = conn.execute(
            "SELECT status FROM policy_tickets WHERE id = ?", (ballot["policy_id"],)
        ).fetchone()
        assert ticket["status"] == "closed"


@pytest.mark.asyncio
async def test_a_ballot_never_writes_a_policy_row(tmp_path):
    """A passed ballot is *recorded*, not enacted — Billy's decision. Nothing
    reaches `policies`, the table `/policy list` reads."""
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b6.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=1, choice="yes"
        )
    interaction = _presser(ctx, admin=True)

    await jc.PolicyBallotCloseButton(ballot_id).callback(interaction)

    with open_db(ctx.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_the_ballot_card_is_rendered_with_resolved_names(tmp_path):
    """The render-site guard: a ``<@id>`` in an embed read by ordinary members
    degrades to a bare number for anyone who has not seen that member."""
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b7.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=7, choice="yes"
        )
        ballot = pbs.get_ballot(conn, ballot_id)
        tally = pbs.tally_ballot(conn, ballot_id)
    assert ballot is not None

    named = MagicMock(spec=discord.Member)
    named.display_name = "Ada"
    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.get_member = MagicMock(return_value=named)

    embed = await jc._render_ballot_embed(ctx, guild, ballot, tally)

    yes_field = next(f for f in embed.fields if f.name.startswith("✅"))
    assert "Ada" in (yes_field.value or "")
    assert "<@7>" not in (yes_field.value or "")


@pytest.mark.asyncio
async def test_the_sweep_closes_ballots_past_their_deadline(tmp_path, monkeypatch):
    """Ballots ride the existing 60-second policy sweep rather than getting a
    second loop over every guild."""
    ctx = _make_ctx(tmp_path / "b8.db", guild_id=10)
    due = _open_ballot_row(ctx, closes_at=100.0)
    not_due = _open_ballot_row(ctx, closes_at=9_999_999_999.0)
    no_deadline = _open_ballot_row(ctx, closes_at=0.0)

    closed: list[int] = []

    async def _fake_finalize(ctx_, guild, ballot_id, *, closed_by, cancelled=False):
        closed.append(ballot_id)
        return None

    monkeypatch.setattr(jc, "finalize_ballot", _fake_finalize)
    monkeypatch.setattr(jc, "_policy_vote_timeout_pass", AsyncMock())

    bot = MagicMock()
    bot.guilds = [MagicMock(id=10)]
    await jc.sweep_expired_policy_votes(bot, ctx)

    assert closed == [due]
    assert not_due not in closed
    assert no_deadline not in closed


@pytest.mark.asyncio
async def test_policy_ballot_command_refuses_a_non_admin_at_runtime(tmp_path):
    """`/policy`'s ``default_permissions`` decorators are inert — discord.py
    only emits ``default_member_permissions`` for top-level commands and the
    group carries none — so every member already sees ``/policy ballot`` in
    the picker. The runtime check is the only gate there is."""
    from bot_modules.moderation.jail.cog import JailCog

    ctx = _make_ctx(tmp_path / "b9.db", guild_id=10)
    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=False)
    interaction.channel = MagicMock(spec=discord.TextChannel)

    await cog.policy_ballot_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_not_awaited()
    assert "❌" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_policy_ballot_command_refuses_outside_a_text_channel(tmp_path):
    """A ballot opens a thread, and a thread cannot hold one."""
    from bot_modules.moderation.jail.cog import JailCog

    ctx = _make_ctx(tmp_path / "b10.db", guild_id=10)
    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)  # channel is a Thread

    await cog.policy_ballot_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_ballot_command_opens_the_modal_for_an_admin(tmp_path):
    from bot_modules.moderation.jail.cog import JailCog

    ctx = _make_ctx(tmp_path / "b11.db", guild_id=10)
    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)
    interaction.channel = MagicMock(spec=discord.TextChannel)

    await cog.policy_ballot_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_awaited()


@pytest.mark.asyncio
async def test_policy_close_in_a_ballot_thread_cancels_the_ballot(tmp_path):
    """A ballot's own ticket wears status 'ballot', so the ordinary proposal
    lookup can never reach it — `/policy close` finds it by thread instead and
    records the votes cast without claiming a result."""
    from bot_modules.moderation.jail.cog import JailCog
    from bot_modules.moderation.jail import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b12.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.attach_ballot_message(conn, ballot_id, thread_id=600, message_id=601)
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=1, choice="yes"
        )

    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)
    interaction.guild.get_channel_or_thread = MagicMock(return_value=None)

    await cog.policy_close_cmd.callback(cog, interaction, None)

    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None
        assert ballot["outcome"] == pbs.OUTCOME_CANCELLED
        # The counts are still frozen: the record says what the room had said.
        assert pbs.frozen_counts(ballot) == (1, 0, 0)


@pytest.mark.asyncio
async def test_policy_close_escapes_the_title_in_the_mod_log_audit_embed(tmp_path):
    """policy_close_cmd builds a *second*, literal audit embed a few lines
    after calling build_policy_close_embed (which already escapes the same
    title) — both renders of the title must agree, or the mod log gets the
    raw masked link the channel-posted embed already guards against.
    """
    from bot_modules.moderation.jail.cog import JailCog
    from bot_modules.moderation.service import create_policy_ticket

    ctx = _make_ctx(tmp_path / "b14.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        create_policy_ticket(
            conn, guild_id=10, creator_id=1, channel_id=600,
            title="[Click here](https://evil.example)", description="",
        )

    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    # channel is a discord.Thread (see _presser), so this skips the
    # TextChannel branch (channel-posted embed + transcript + delete) and
    # goes straight to the standalone audit_embed under test.
    interaction = _presser(ctx, admin=True)
    interaction.guild.get_channel_or_thread = MagicMock(return_value=None)

    posted: list[discord.Embed] = []

    async def _capture(_ctx, _guild, embed):
        posted.append(embed)

    import bot_modules.moderation.jail.cog as jail_cog_mod

    original = jail_cog_mod._post_audit
    jail_cog_mod._post_audit = _capture
    try:
        await cog.policy_close_cmd.callback(cog, interaction, None)
    finally:
        jail_cog_mod._post_audit = original

    assert posted, "the mod log never saw the close"
    description = posted[0].description or ""
    assert "\\[Click here](https://evil.example)" in description
    # escape_markdown only backslashes the *leading* `[`, so a naive
    # "raw not in description" check would pass even unescaped — assert the
    # exact bold-wrapped raw title is absent instead.
    assert "**[Click here](https://evil.example)**" not in description


@pytest.mark.asyncio
async def test_policy_vote_cannot_be_started_inside_a_ballot_thread(tmp_path):
    """The mod team's unanimity vote archives and deletes the channel it runs
    in when it resolves. A ballot thread must never be reachable by it."""
    from bot_modules.moderation.jail.cog import JailCog
    from bot_modules.moderation.jail import policy_ballot_service as pbs
    from bot_modules.moderation.service import get_policy_ticket_by_channel

    ctx = _make_ctx(tmp_path / "b13.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None
        conn.execute(
            "UPDATE policy_tickets SET channel_id = ?, status = ? WHERE id = ?",
            (600, pbs.TICKET_STATUS_BALLOT, ballot["policy_id"]),
        )
        assert get_policy_ticket_by_channel(conn, 600) is None

    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)

    await cog.policy_vote_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_not_awaited()


# ── Policy visibility toggle ──────────────────────────────────────────


def test_every_jail_dynamic_item_is_registered_at_cog_load():
    """A DynamicItem never handed to ``add_dynamic_items`` renders fine and
    then silently fails to route on click, forever, with no error anywhere.

    This is glue, not behaviour, which is why it earns one assertion: the
    visibility toggle sits on a proposal card that outlives restarts, so an
    unregistered button would look correct and do nothing the next day.
    """
    import inspect

    from bot_modules.moderation.jail import cog as jail_cog

    defined = {
        name
        for name, obj in inspect.getmembers(jc, inspect.isclass)
        if issubclass(obj, discord.ui.DynamicItem)
        and obj is not discord.ui.DynamicItem
        and obj.__module__ == jc.__name__
    }
    source = inspect.getsource(jail_cog.JailCog.cog_load)
    missing = sorted(name for name in defined if name not in source)
    assert not missing, (
        "jail DynamicItems missing from add_dynamic_items (their buttons "
        f"would render but never route): {missing}"
    )


def test_visibility_button_template_matches_only_its_own_ids():
    """The three policy button families share a card lifetime and a prefix
    space; a template that also matched ``policy_vote:`` would route a vote
    press into a permission change."""
    template = jc.PolicyVisibilityButton.__discord_ui_compiled_template__
    assert template.fullmatch("policy_visibility:12")
    assert template.fullmatch("policy_vote:yes:12") is None
    assert template.fullmatch("policy_ballot:yes:12") is None
    assert template.fullmatch("policy_visibility:") is None


def test_visibility_button_renders_the_action_not_the_state():
    mods = jc.PolicyVisibilityButton(7, "mods")
    public = jc.PolicyVisibilityButton(7, "public")
    assert mods.item.label == "Open to Members"
    assert public.item.label == "Make Mods-Only"
    # Same custom id either way: the id carries the policy, never the state,
    # so a card that sat through a restart cannot act on a stale reading.
    assert mods.item.custom_id == public.item.custom_id == "policy_visibility:7"


@pytest.mark.asyncio
async def test_visibility_button_refuses_a_non_admin(tmp_path):
    """The card is visible to members the moment the channel is open, so the
    gate has to be a runtime check on press — it cannot lean on being
    hidden."""
    ctx = _make_ctx(tmp_path / "t.db")
    bot = MagicMock()
    bot.ctx = ctx

    member = MagicMock(spec=discord.Member)
    member.roles = []
    member.guild_permissions = discord.Permissions.none()
    member.id = 42
    member.guild = MagicMock(spec=discord.Guild)
    member.guild.id = 10

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.user = member
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    await jc.PolicyVisibilityButton(1, "mods").callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.await_args.args[0]
    assert "Only admins" in msg
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def _visibility_interaction(ctx, *, channel_id: int = 556):
    """A component interaction on a proposal card, with the bits ``_apply``
    actually touches."""
    bot = MagicMock()
    bot.ctx = ctx

    member = MagicMock(spec=discord.Member)
    member.id = 99
    member.mention = "<@99>"
    member.guild = MagicMock(spec=discord.Guild)
    member.guild.id = 10

    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.default_role = MagicMock(spec=discord.Role)
    guild.get_member = MagicMock(return_value=None)
    guild.get_channel = MagicMock(return_value=None)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.mention = f"<#{channel_id}>"
    channel.set_permissions = AsyncMock()
    channel.send = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.user = member
    interaction.guild = guild
    interaction.channel = channel
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction, channel


@pytest.mark.asyncio
async def test_opening_a_proposal_grants_history_and_posting(tmp_path):
    """Opening is a real permission change, and the three grants are the
    decision: members read the backlog (that is what the confirm warns
    about) and may post, because the reason to open a proposal is member
    input rather than an audience."""
    from bot_modules.moderation.service import create_policy_ticket, get_policy_ticket

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=7, channel_id=556,
            title="T", description="D",
        )
        policy = get_policy_ticket(conn, pid)

    interaction, channel = _visibility_interaction(ctx)
    await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")

    kwargs = channel.set_permissions.await_args.kwargs
    assert kwargs["view_channel"] is True
    assert kwargs["read_message_history"] is True
    assert kwargs["send_messages"] is True
    assert channel.set_permissions.await_args.args[0] is interaction.guild.default_role

    with open_db(ctx.db_path) as conn:
        assert get_policy_ticket(conn, pid)["visibility"] == "public"

    # Said in the channel, not just to the presser: the audience changed
    # under everyone already in the room.
    channel.send.assert_awaited_once()
    assert "opened this proposal to all members" in channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_closing_a_proposal_denies_view_explicitly(tmp_path):
    """Back to an explicit deny, not a cleared overwrite.

    Clearing it would leave the channel inheriting whatever the ticket
    category says, which is not what `/policy open` created and could leave
    it visible.
    """
    from bot_modules.moderation.service import (
        create_policy_ticket,
        get_policy_ticket,
        set_policy_visibility,
    )

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=7, channel_id=556,
            title="T", description="D",
        )
        set_policy_visibility(conn, pid, visibility="public")
        policy = get_policy_ticket(conn, pid)

    interaction, channel = _visibility_interaction(ctx)
    await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "mods")

    kwargs = channel.set_permissions.await_args.kwargs
    assert kwargs["view_channel"] is False
    assert kwargs["read_message_history"] is None
    assert kwargs["send_messages"] is None

    with open_db(ctx.db_path) as conn:
        assert get_policy_ticket(conn, pid)["visibility"] == "mods"


@pytest.mark.asyncio
async def test_a_forbidden_permission_edit_does_not_record_a_change(tmp_path):
    """If Discord refuses the overwrite, the row must not claim it happened —
    the card would then show "open to members" over a private channel, and
    the next press would try to close a channel nobody opened."""
    from bot_modules.moderation.service import create_policy_ticket, get_policy_ticket

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=7, channel_id=556,
            title="T", description="D",
        )
        policy = get_policy_ticket(conn, pid)

    interaction, channel = _visibility_interaction(ctx)
    channel.set_permissions = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "nope")
    )

    await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")

    with open_db(ctx.db_path) as conn:
        assert get_policy_ticket(conn, pid)["visibility"] == "mods"
    channel.send.assert_not_awaited()
    assert "Manage Roles" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_a_non_forbidden_permission_edit_failure_does_not_record_a_change(
    tmp_path,
):
    """A deleted-mid-press channel (or any other Discord hiccup) raises
    ``discord.HTTPException`` rather than ``discord.Forbidden``. That must
    not fall through uncaught and leave the deferred interaction hanging
    with no followup — and, same as the Forbidden case, must not persist a
    visibility change that never actually landed."""
    from bot_modules.moderation.service import create_policy_ticket, get_policy_ticket

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=7, channel_id=556,
            title="T", description="D",
        )
        policy = get_policy_ticket(conn, pid)

    interaction, channel = _visibility_interaction(ctx)
    channel.set_permissions = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown channel")
    )

    await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")

    with open_db(ctx.db_path) as conn:
        assert get_policy_ticket(conn, pid)["visibility"] == "mods"
    channel.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert "went wrong" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("total,expected", [
    (0, (0, False)),
    (1, (1, False)),
    (jc.POLICY_EXPOSURE_COUNT_CAP - 1, (jc.POLICY_EXPOSURE_COUNT_CAP - 1, False)),
    # Exactly the cap is a real total, not a floor: "500 messages", no "+".
    (jc.POLICY_EXPOSURE_COUNT_CAP, (jc.POLICY_EXPOSURE_COUNT_CAP, False)),
    # One past it is the first genuinely capped case.
    (jc.POLICY_EXPOSURE_COUNT_CAP + 1, (jc.POLICY_EXPOSURE_COUNT_CAP, True)),
])
async def test_backlog_count_reports_the_cap_as_a_floor_only_when_it_is_one(
    total, expected
):
    """Off-by-one here is a wrong number in a safety prompt: a channel of
    exactly the cap must not be announced as "500+"."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 556

    def _history(limit=None):
        async def _gen():
            for i in range(min(total, limit or total)):
                yield MagicMock()
        return _gen()

    channel.history = _history
    assert await jc._count_channel_messages(channel) == expected


@pytest.mark.asyncio
async def test_an_unreadable_backlog_counts_as_unknown_not_as_zero():
    """``discord.Forbidden`` IS a ``discord.HTTPException``, so a bot without
    Read Message History in the policy channel lands in the same handler as
    a transient API error.

    Returning the running tally there would render "0 messages readable by
    every member" — the most reassuring sentence the confirm can produce,
    printed precisely when the backlog is unmeasured. Unknown must stay
    unknown all the way to the prompt.
    """
    from bot_modules.moderation.jail.logic import policy_exposure_warning

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 556

    def _history(limit=None):
        async def _gen():
            raise discord.Forbidden(MagicMock(status=403), "no history")
            yield  # pragma: no cover - unreachable, makes this a generator
        return _gen()

    channel.history = _history
    count, capped = await jc._count_channel_messages(channel)
    assert count is None
    assert capped is False
    assert "0 messages" not in policy_exposure_warning(count, capped=capped)


@pytest.mark.asyncio
async def test_a_backlog_that_fails_partway_discards_the_partial_tally():
    """A count that dies after 37 of 4,000 messages must not report "37
    messages" — an understatement is the dangerous direction here."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 556

    def _history(limit=None):
        async def _gen():
            for _ in range(37):
                yield MagicMock()
            raise discord.HTTPException(MagicMock(status=500), "boom")
        return _gen()

    channel.history = _history
    assert await jc._count_channel_messages(channel) == (None, False)


# ── Visibility: the confirm gate, and the jailed deny ────────────────


class _EmptyHistory:
    """Stand-in for ``channel.history(...)`` — an empty async iterator."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _admin_visibility_press(ctx, *, channel_id: int = 556):
    """``_visibility_interaction`` wired for a real button press by an admin:
    a deferrable response, an admin presser, and a countable backlog."""
    interaction, channel = _visibility_interaction(ctx, channel_id=channel_id)
    interaction.user.guild_permissions = discord.Permissions(administrator=True)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    channel.history = MagicMock(return_value=_EmptyHistory())
    return interaction, channel


def _seed_policy(ctx, *, visibility: str = "mods", channel_id: int = 556):
    from bot_modules.moderation.service import (
        create_policy_ticket,
        get_policy_ticket,
        set_policy_visibility,
    )

    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=7, channel_id=channel_id,
            title="T", description="D",
        )
        if visibility != "mods":
            set_policy_visibility(conn, pid, visibility=visibility)
        return pid, get_policy_ticket(conn, pid)


@pytest.mark.asyncio
async def test_declining_the_confirm_opens_nothing(tmp_path, monkeypatch):
    """The confirm is this feature's safety gate, so it gets a test that
    proves it *denies*.

    Cancel and timeout are deliberately the same outcome — ``_ConfirmView``
    only sets ``result`` on a Confirm press — so a prompt nobody answers
    fails closed. Patching ``View.wait`` to return without a press is
    exactly that case: the channel must stay private and the row must still
    read ``mods``, because everything the warning measures (the backlog a
    member would be handed) is unrecoverable once it's wrong.
    """
    from bot_modules.moderation.service import get_policy_ticket

    ctx = _make_ctx(tmp_path / "t.db")
    pid, _ = _seed_policy(ctx)
    interaction, channel = _admin_visibility_press(ctx)
    monkeypatch.setattr(discord.ui.View, "wait", AsyncMock(return_value=None))

    await jc.PolicyVisibilityButton(pid, "mods").callback(interaction)

    # The warning was shown...
    interaction.followup.send.assert_awaited_once()
    assert "readable by every member" in interaction.followup.send.await_args.args[0]
    # ...and nothing followed it.
    channel.set_permissions.assert_not_awaited()
    channel.send.assert_not_awaited()
    with open_db(ctx.db_path) as conn:
        assert get_policy_ticket(conn, pid)["visibility"] == "mods"


@pytest.mark.asyncio
async def test_opening_re_denies_the_jailed_role_first(tmp_path):
    """`manual.html` and the spec both promise jailed members never see a
    policy channel, open or not. That rested on the create-time listener,
    which is best-effort by design — so the press that grants
    ``@everyone view_channel`` re-stamps the deny itself, and stamps it
    *before* the allow so there is no window where the channel is public
    without it.
    """
    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "777", guild_id=10)
    pid, policy = _seed_policy(ctx)

    interaction, channel = _admin_visibility_press(ctx)
    jail_role = MagicMock(spec=discord.Role)
    jail_role.id = 777
    interaction.guild.get_role = MagicMock(return_value=jail_role)
    # No overwrite for the jail role yet: jailed members inherit @everyone.
    channel.overwrites_for = MagicMock(
        return_value=discord.PermissionOverwrite(view_channel=None)
    )

    await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")

    calls = channel.set_permissions.await_args_list
    assert calls[0].args[0] is jail_role
    assert calls[0].kwargs["view_channel"] is False
    assert calls[1].args[0] is interaction.guild.default_role
    assert calls[1].kwargs["view_channel"] is True


@pytest.mark.asyncio
async def test_a_channel_we_cannot_shut_jailed_members_out_of_stays_private(
    tmp_path,
):
    """If the deny can't be written, the promise can't be kept — so don't
    open. Failing the other way would publish the channel to jailed members
    while the admin guide says it can't happen, which is the worst of the
    two errors: silent, and only visible to the people it exposes it to.
    """
    from bot_modules.moderation.service import get_policy_ticket

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "777", guild_id=10)
    pid, policy = _seed_policy(ctx)

    interaction, channel = _admin_visibility_press(ctx)
    jail_role = MagicMock(spec=discord.Role)
    jail_role.id = 777
    interaction.guild.get_role = MagicMock(return_value=jail_role)
    channel.overwrites_for = MagicMock(
        return_value=discord.PermissionOverwrite(view_channel=None)
    )
    channel.set_permissions = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no manage roles")
    )

    await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")

    with open_db(ctx.db_path) as conn:
        assert get_policy_ticket(conn, pid)["visibility"] == "mods"
    channel.send.assert_not_awaited()
    assert "jailed members" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_the_visibility_notice_and_audit_card_name_nobody_with_an_id(
    tmp_path,
):
    """The branch that took `<@id>` out of the vote rosters doesn't get to
    put one back in the card next door. The in-channel notice is message
    content (where a mention resolves server-side) but pings nobody, and the
    mod-log embed carries a resolved name — an embed mention is resolved by
    the *reading* client's cache and renders as bare digits to anyone who
    hasn't seen that member.
    """
    ctx = _make_ctx(tmp_path / "t.db")
    pid, policy = _seed_policy(ctx)
    interaction, channel = _admin_visibility_press(ctx)

    # The resolver's documented last resort *is* `<@id>`, so give it someone
    # to find — otherwise this test would pass on the fallback and prove
    # nothing about the wiring.
    def _member_named(uid):
        m = MagicMock(spec=discord.Member)
        m.display_name = {7: "Proposer Seven", 99: "Mod Nine"}.get(uid)
        return m if m.display_name else None

    interaction.guild.get_member = MagicMock(side_effect=_member_named)

    posted: list[discord.Embed] = []

    async def _capture(_ctx, _guild, embed):
        posted.append(embed)

    import bot_modules.moderation.jail.jail_commands as _jc

    original = _jc._post_audit
    _jc._post_audit = _capture
    try:
        await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")
    finally:
        _jc._post_audit = original

    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert posted, "the mod log never saw the change"
    description = posted[0].description or ""
    assert "Mod Nine" in description
    assert "<@" not in description


@pytest.mark.asyncio
async def test_visibility_change_audit_embed_escapes_the_policy_title(tmp_path):
    """The mod-log "Policy Visibility Changed" embed quotes the admin-typed
    title verbatim into its description — unescaped markdown there would
    let a title like ``**forged**`` bold text the audit trail never wrote.
    """
    ctx = _make_ctx(tmp_path / "t.db")
    from bot_modules.moderation.service import (
        create_policy_ticket,
        get_policy_ticket,
    )

    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=10, creator_id=7, channel_id=556,
            title="**forged** _title_", description="D",
        )
        policy = get_policy_ticket(conn, pid)

    interaction, channel = _admin_visibility_press(ctx)

    posted: list[discord.Embed] = []

    async def _capture(_ctx, _guild, embed):
        posted.append(embed)

    import bot_modules.moderation.jail.jail_commands as _jc

    original = _jc._post_audit
    _jc._post_audit = _capture
    try:
        await jc.PolicyVisibilityButton(pid)._apply(interaction, ctx, policy, "public")
    finally:
        _jc._post_audit = original

    assert posted, "the mod log never saw the change"
    description = posted[0].description or ""
    assert "\\*\\*forged\\*\\* \\_title\\_" in description
    assert "**forged** _title_" not in description


def _ballot_row(**overrides) -> dict:
    row = {
        "id": 1,
        "guild_id": 10,
        "policy_id": 1,
        "channel_id": 0,
        "thread_id": 0,
        "message_id": 0,
        "question": "q",
        "opened_by": 1,
        "opened_at": 0.0,
        "closes_at": 0.0,
        "closed_at": 0.0,
        "closed_by": 1,
        "yes_count": 1,
        "no_count": 0,
        "abstain_count": 0,
        "outcome": jc.OUTCOME_PASSED,
    }
    row.update(overrides)
    return row


def test_ballot_result_embed_escapes_the_mod_typed_question():
    """The public ballot thread and the mod-log audit copy both render
    this description — a raw ``__question__`` would italicize text the
    ``_PolicyBallotModal`` author never asked the bot to style."""
    closed = _ballot_row(question="__is this__ ok?")

    embed = jc._ballot_result_embed(closed)

    assert "\\_\\_is this\\_\\_ ok?" in (embed.description or "")
    assert "__is this__ ok?" not in (embed.description or "")


@pytest.mark.parametrize(
    ("outcome", "title"),
    [
        pytest.param(jc.OUTCOME_PASSED, "✅ Ballot Passed", id="passed"),
        pytest.param(jc.OUTCOME_CANCELLED, "🚫 Ballot Cancelled", id="cancelled"),
        pytest.param("failed", "❌ Ballot Failed", id="failed"),
    ],
)
def test_ballot_result_embed_titles_every_outcome(outcome, title):
    closed = _ballot_row(outcome=outcome)
    assert jc._ballot_result_embed(closed).title == title


# ── _TicketCloseModal.on_submit escapes the mod-typed close reason ───────


def _ticket_close_interaction(ctx, *, channel_id: int = 700):
    """A modal submission on a ticket channel, wired for a real close by a
    moderator with a DM-able creator on the other end."""
    bot = MagicMock()
    bot.ctx = ctx

    mod = MagicMock(spec=discord.Member)
    mod.id = 42
    mod.mention = "<@42>"
    mod.display_name = "Mod Fortytwo"
    mod.guild_permissions = discord.Permissions(administrator=True)

    creator = MagicMock(spec=discord.Member)
    creator.id = 7
    creator.mention = "<@7>"
    creator.display_name = "Creator Seven"
    creator.send = AsyncMock()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.name = "Test Guild"
    guild.icon = None
    guild.get_member = MagicMock(return_value=creator)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    channel.set_permissions = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.user = mod
    interaction.guild = guild
    interaction.channel = channel
    interaction.channel_id = channel_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction, channel, creator


@pytest.mark.asyncio
async def test_ticket_close_reason_escaped_at_every_render_site(tmp_path):
    """The close reason (``_TicketCloseModal``, max_length=500) is mod-typed
    free text rendered at three sites — the channel close notice, the
    creator's DM, and the mod-log audit embed — mirroring the sibling
    ``/ticket close`` command's ``ticket_close_cmd`` escaping. The DB writes
    (``close_ticket``, ``write_audit``) must still see the raw text.
    """
    from bot_modules.moderation.service import create_ticket, get_ticket_by_channel

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        create_ticket(
            conn, guild_id=10, user_id=7, channel_id=700, description="d",
        )

    interaction, channel, creator = _ticket_close_interaction(ctx)
    modal = jc._TicketCloseModal(ticket_id=1)
    modal.reason._value = "**forged** reason"

    posted: list[discord.Embed] = []

    async def _capture(_ctx, _guild, embed):
        posted.append(embed)

    original = jc._post_audit
    jc._post_audit = _capture
    try:
        await modal.on_submit(interaction)
    finally:
        jc._post_audit = original

    # 1. Channel close notice (message content).
    close_msg = channel.send.await_args.args[0]
    assert "\\*\\*forged\\*\\* reason" in close_msg
    assert "**forged** reason" not in close_msg

    # 2. Creator's DM embed description.
    dm_embed = creator.send.await_args.kwargs["embed"]
    assert "\\*\\*forged\\*\\* reason" in dm_embed.description
    assert "**forged** reason" not in dm_embed.description

    # 3. Mod-log audit embed description.
    assert posted, "the mod log never saw the close"
    assert "\\*\\*forged\\*\\* reason" in posted[0].description
    assert "**forged** reason" not in posted[0].description

    # The DB writes are a write-back and a structured audit-trail column —
    # never escaped, per the sweep's rule against double-escaping storage.
    with open_db(ctx.db_path) as conn:
        assert get_ticket_by_channel(conn, 700)["close_reason"] == "**forged** reason"
        row = conn.execute(
            "SELECT extra FROM audit_log WHERE action = 'ticket_close'"
        ).fetchone()
        assert "**forged** reason" in row["extra"]


@pytest.mark.asyncio
async def test_ticket_close_with_no_reason_adds_no_reason_line(tmp_path):
    """An empty reason must not print a stray ``**Reason:**`` line anywhere —
    covers the falsy-``reason`` branch the escaping sites above all guard."""
    from bot_modules.moderation.service import create_ticket

    ctx = _make_ctx(tmp_path / "t.db")
    with open_db(ctx.db_path) as conn:
        create_ticket(
            conn, guild_id=10, user_id=7, channel_id=700, description="d",
        )

    interaction, channel, creator = _ticket_close_interaction(ctx)
    modal = jc._TicketCloseModal(ticket_id=1)
    # reason._value left at its default (None) — same as submitting blank.

    posted: list[discord.Embed] = []

    async def _capture(_ctx, _guild, embed):
        posted.append(embed)

    original = jc._post_audit
    jc._post_audit = _capture
    try:
        await modal.on_submit(interaction)
    finally:
        jc._post_audit = original

    assert "Reason" not in channel.send.await_args.args[0]
    assert "Reason" not in creator.send.await_args.kwargs["embed"].description
    assert "Reason" not in posted[0].description


# ── bug #270: every embed DM through ``_dm_user`` names the server ────


def _unbranded_dm_user_calls() -> list[str]:
    """``_dm_user(embed=...)`` calls in the jail package missing ``guild=``
    or ``db_path=``.

    ``_dm_user`` brands only when the caller hands it the guild, and picks
    the accent only with ``db_path`` too. ``test_dm_branding_contract`` cannot
    see a caller that forgot: the send and the brander both live inside
    ``_dm_user``, so the wrapper reads as compliant whatever its callers pass.
    A file-only DM (the transcript) carries no embed and is exempt.
    """
    import ast

    from tests.paths import REPO_ROOT

    offenders: list[str] = []
    jail = REPO_ROOT / "src" / "bot_modules" / "moderation" / "jail"
    assert jail.is_dir(), "the jail package moved; an rglob over nothing passes empty"
    for path in sorted(jail.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "_dm_user":
                continue
            passed = {kw.arg for kw in node.keywords}
            if "embed" in passed and not {"guild", "db_path"} <= passed:
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def test_every_embed_dm_user_call_is_branded():
    assert not _unbranded_dm_user_calls(), (
        "_dm_user(embed=...) without guild= and db_path= sends an unbranded "
        f"DM: {_unbranded_dm_user_calls()}"
    )
