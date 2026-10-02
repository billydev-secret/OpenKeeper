"""breaks.service: the order a break happens in, rollback, return, rejoin.

A bug here is silent, permanent role loss, so the round trip is asserted end
to end against the real table.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.breaks import service
from bot_modules.breaks.store import (
    BREAK_CHANNEL_KEY,
    BREAK_ROLE_KEY,
    SPICY_LITE_ROLE_KEY,
    get_break,
)
from bot_modules.core.app_context import AppContext
from tests.db_template import migrated_db

G = 100
EVERYONE = 1
BREAK, LITE, SPICY, DENIZEN, OTHER, BOOSTER, HIGH = 900, 901, 800, 700, 701, 600, 999
NEW_LITE = 902
BOT_TOP = 950


def _role(rid: int, *, managed: bool = False, position: int = 10) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id, r.managed, r.position, r.name = rid, managed, position, f"r{rid}"
    r.is_default.return_value = rid == EVERYONE
    r.permissions = discord.Permissions(administrator=rid == HIGH)
    r.__ge__ = lambda _self, other: r.position >= other.position
    return r


class Guild:
    def __init__(self):
        self.roles = {
            rid: _role(rid, managed=(rid == BOOSTER), position=(1000 if rid == HIGH else 10))
            for rid in (EVERYONE, BREAK, LITE, NEW_LITE, SPICY, DENIZEN, OTHER, BOOSTER, HIGH)
        }
        self.g = MagicMock(spec=discord.Guild)
        self.g.id = G
        self.g.default_role = self.roles[EVERYONE]
        self.g.roles = list(self.roles.values())
        self.g.get_role = MagicMock(side_effect=lambda rid: self.roles.get(rid))
        me = MagicMock()
        me.top_role = _role(0, position=BOT_TOP)
        self.g.me = me


def _member(guild: Guild, role_ids, *, uid: int = 5, fail=None) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id, m.bot, m.guild = uid, False, guild.g
    m.display_name = m.name = f"u{uid}"
    perms = MagicMock()
    perms.administrator = perms.manage_guild = False
    m.guild_permissions = perms
    m.roles = [guild.roles[EVERYONE]] + [guild.roles[r] for r in role_ids]

    async def _add(*roles, reason=None):
        if fail == "add":
            raise discord.HTTPException(MagicMock(status=500), "boom")
        for r in roles:
            if r not in m.roles:
                m.roles.append(r)

    async def _remove(*roles, reason=None):
        if fail == "remove":
            raise discord.HTTPException(MagicMock(status=500), "boom")
        for r in roles:
            if r in m.roles:
                m.roles.remove(r)

    m.add_roles = AsyncMock(side_effect=_add)
    m.remove_roles = AsyncMock(side_effect=_remove)
    return m


@pytest.fixture
def ctx(tmp_path):
    migrated_db(tmp_path / "b.db")
    c = AppContext(
        bot=MagicMock(), log=logging.getLogger("t"), db_path=tmp_path / "b.db",
        guild_id=G, debug=True,
    )
    c.set_config_value(BREAK_ROLE_KEY, str(BREAK), G)
    c.set_config_value(BREAK_CHANNEL_KEY, "42", G)
    c.set_config_value(SPICY_LITE_ROLE_KEY, str(LITE), G)
    c.set_config_value("promotion_review_grant_role_id", str(SPICY), G)
    return c


def _ids(member) -> set[int]:
    return {r.id for r in member.roles} - {EVERYONE}


def _row(ctx, uid=5):
    with ctx.open_db() as conn:
        return get_break(conn, G, uid)


async def test_round_trip_swaps_spicy_for_lite_and_gives_everything_back(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN, SPICY, OTHER, BOOSTER])

    started = await service.start_break(ctx, m)
    assert started.ok
    assert _ids(m) == {BREAK, LITE, BOOSTER}  # integration role untouched
    assert sorted(_row(ctx).stored_roles) == sorted([DENIZEN, SPICY, OTHER])

    ended = await service.end_break(ctx, m)
    assert ended.ok
    assert _ids(m) == {DENIZEN, SPICY, OTHER, BOOSTER}
    assert _row(ctx) is None


async def test_the_row_is_written_before_any_role_comes_off(ctx, monkeypatch):
    guild = Guild()
    m = _member(guild, [DENIZEN])
    seen = []

    async def _add(*roles, reason=None):
        seen.append(_row(ctx) is not None)
        m.roles.extend(roles)

    m.add_roles = AsyncMock(side_effect=_add)
    await service.start_break(ctx, m)
    assert seen == [True]


@pytest.mark.parametrize("fail", ["add", "remove"])
async def test_a_failed_strip_rolls_everything_back(ctx, fail):
    guild = Guild()
    m = _member(guild, [DENIZEN, SPICY], fail=fail)
    result = await service.start_break(ctx, m)
    assert not result.ok
    # add failing means nothing changed; remove failing makes the rollback's
    # own remove fail too, so the row stays for recovery.
    if fail == "add":
        assert _row(ctx) is None
    else:
        assert _row(ctx) is not None


async def test_a_second_press_is_refused_and_keeps_the_first_snapshot(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN])
    assert (await service.start_break(ctx, m)).ok
    again = await service.start_break(ctx, m)
    assert not again.ok and "already on a break" in again.message
    assert _row(ctx).stored_roles == [DENIZEN]


async def test_dark_until_configured(ctx):
    ctx.set_config_value(BREAK_CHANNEL_KEY, "0", G)
    guild = Guild()
    m = _member(guild, [DENIZEN])
    result = await service.start_break(ctx, m)
    assert not result.ok
    assert _ids(m) == {DENIZEN}
    assert _row(ctx) is None


async def test_a_role_above_the_bot_refuses_before_anything_changes(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN, HIGH])
    result = await service.start_break(ctx, m)
    assert not result.ok and "above mine" in result.message
    assert _ids(m) == {DENIZEN, HIGH}


async def test_staff_are_refused(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN])
    m.guild_permissions.manage_guild = True
    assert not (await service.start_break(ctx, m)).ok


async def test_return_reports_a_role_deleted_during_the_break(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN, OTHER])
    await service.start_break(ctx, m)
    del guild.roles[OTHER]
    guild.g.roles = list(guild.roles.values())
    ended = await service.end_break(ctx, m)
    assert ended.ok and "1 role(s)" in ended.message
    assert _ids(m) == {DENIZEN}


async def test_a_failed_return_keeps_the_break_saved(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN])
    await service.start_break(ctx, m)
    m.add_roles = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
    ended = await service.end_break(ctx, m)
    assert not ended.ok
    assert _row(ctx) is not None


async def test_im_back_with_no_row_still_takes_a_stray_break_role_off(ctx):
    guild = Guild()
    m = _member(guild, [BREAK])
    ended = await service.end_break(ctx, m)
    assert not ended.ok
    assert _ids(m) == set()


async def test_rejoin_reapplies_the_hold_roles_only(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN, SPICY])
    await service.start_break(ctx, m)
    rejoined = _member(guild, [])
    assert await service.reapply_on_join(ctx, rejoined) is True
    assert _ids(rejoined) == {BREAK, LITE}
    assert await service.reapply_on_join(ctx, _member(guild, [], uid=6)) is False


def _make_admin(guild: Guild, rid: int) -> None:
    guild.roles[rid].permissions = discord.Permissions(administrator=True)


@pytest.mark.parametrize("unsafe", [BREAK, LITE], ids=["break-role", "lite-role"])
async def test_a_hold_role_dial_on_an_elevated_role_refuses_the_break(ctx, unsafe):
    guild = Guild()
    _make_admin(guild, unsafe)
    m = _member(guild, [DENIZEN, SPICY])
    result = await service.start_break(ctx, m)
    assert not result.ok
    assert result.message == service.logic.REFUSAL_MESSAGES["unsafe_role"]
    assert _ids(m) == {DENIZEN, SPICY}
    assert _row(ctx) is None


async def test_rejoin_skips_a_hold_role_that_became_elevated(ctx):
    guild = Guild()
    await service.start_break(ctx, _member(guild, [DENIZEN, SPICY]))
    _make_admin(guild, LITE)
    rejoined = _member(guild, [])
    assert await service.reapply_on_join(ctx, rejoined) is True
    assert _ids(rejoined) == {BREAK}


async def test_a_mod_can_end_an_absent_members_break(ctx):
    guild = Guild()
    await service.start_break(ctx, _member(guild, [DENIZEN]))
    assert await service.end_break_absent(ctx, G, 5, actor_id=2) is True
    assert await service.end_break_absent(ctx, G, 5, actor_id=2) is False


# ── Set up break channel ─────────────────────────────────────────────


def _chan(cid: int, *, fail: bool = False) -> MagicMock:
    c = MagicMock(spec=discord.TextChannel)
    c.id, c.name = cid, f"c{cid}"
    c.set_permissions = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=403), "no") if fail else None
    )
    c.send = AsyncMock()
    return c


@pytest.fixture
def dark_ctx(tmp_path):
    migrated_db(tmp_path / "s.db")
    return AppContext(
        bot=MagicMock(), log=logging.getLogger("t"), db_path=tmp_path / "s.db",
        guild_id=G, debug=True,
    )


def _setup_guild(channels):
    guild = Guild()
    guild.g.channels = channels
    return guild


async def test_setup_grants_the_channel_hides_the_rest_and_switches_breaks_on(dark_ctx, monkeypatch):
    brk = _chan(42)
    others = [_chan(43), _chan(44)]
    guild = _setup_guild([brk, *others])
    role = guild.roles[BREAK]
    monkeypatch.setattr(service, "ensure_break_role", AsyncMock(return_value=role))
    view = discord.ui.LayoutView(timeout=None)

    ok, note = await service.setup_break_channel(dark_ctx, guild.g, brk, return_panel=view)

    assert (ok, note) == (True, "")
    brk.set_permissions.assert_awaited_once_with(
        role, view_channel=True, send_messages=True, read_message_history=True
    )
    brk.send.assert_awaited_once_with(view=view)
    for c in others:
        c.set_permissions.assert_awaited_once_with(role, view_channel=False, send_messages=False)
    with dark_ctx.open_db() as conn:
        assert service.read_break_config(dark_ctx, conn, G).break_channel_id == 42


async def _still_dark(ctx) -> bool:
    with ctx.open_db() as conn:
        return not service.read_break_config(ctx, conn, G).enabled


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("no_role", id="no-manage-roles-or-none"),
        pytest.param("grant", id="channel-grant-refused"),
        pytest.param("post", id="panel-post-refused"),
    ],
)
async def test_a_failed_setup_leaves_breaks_off(dark_ctx, monkeypatch, failure):
    brk = _chan(42, fail=(failure == "grant"))
    if failure == "post":
        brk.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "no"))
    guild = _setup_guild([brk])
    role = None if failure == "no_role" else guild.roles[BREAK]
    monkeypatch.setattr(service, "ensure_break_role", AsyncMock(return_value=role))

    ok, note = await service.setup_break_channel(
        dark_ctx, guild.g, brk, return_panel=discord.ui.LayoutView(timeout=None)
    )
    assert ok is False and note
    assert await _still_dark(dark_ctx)


async def test_setup_refuses_a_non_text_channel(dark_ctx):
    voice = MagicMock(spec=discord.VoiceChannel)
    ok, note = await service.setup_break_channel(
        dark_ctx, Guild().g, voice, return_panel=discord.ui.LayoutView(timeout=None)
    )
    assert not ok and "text channel" in note


async def test_a_channel_that_could_not_be_hidden_is_reported_not_fatal(dark_ctx, monkeypatch):
    brk = _chan(42)
    guild = _setup_guild([brk, _chan(43, fail=True), _chan(44)])
    dark_ctx.set_config_value(BREAK_ROLE_KEY, str(BREAK), G)  # what ensure_break_role stores
    monkeypatch.setattr(service, "ensure_break_role", AsyncMock(return_value=guild.roles[BREAK]))
    ok, note = await service.setup_break_channel(
        dark_ctx, guild.g, brk, return_panel=discord.ui.LayoutView(timeout=None)
    )
    assert ok and "1 channel" in note
    assert not await _still_dark(dark_ctx)


async def test_a_mod_end_is_audited_as_the_mod(ctx):
    guild = Guild()
    m = _member(guild, [DENIZEN])
    await service.start_break(ctx, m)
    assert (await service.end_break(ctx, m, actor_id=2)).ok
    with ctx.open_db() as conn:
        row = conn.execute(
            "SELECT actor_id FROM audit_log WHERE action = 'break_end'"
        ).fetchone()
    assert row["actor_id"] == 2


# ── what a break pauses and a return resumes ──────────────────────


@pytest.fixture
def hooks(monkeypatch):
    """Record the side-effect calls instead of running them."""
    import bot_modules.economy.perk_actions as perks
    import bot_modules.economy.rentals_service as rentals

    calls: list[tuple] = []
    state = {"pause_fails": False, "perks": [], "gate": {}}

    def _paused(conn, guild_id, user_id, paused, *, gate_ok=None, now=None):
        calls.append(("paused", user_id, paused, gate_ok))
        if state["pause_fails"]:
            raise RuntimeError("db down")
        return 1

    async def _gate(bot, guild_id, perk):
        return state["gate"].get(perk, True)

    async def _apply(bot, db_path, guild_id, user_id):
        calls.append(("apply_perks", user_id))
        return True

    monkeypatch.setattr(rentals, "set_beneficiary_rentals_paused", _paused)
    monkeypatch.setattr(
        rentals, "list_member_rentals", lambda conn, g, u: state["perks"]
    )
    monkeypatch.setattr(perks, "feature_gate_ok", _gate)
    monkeypatch.setattr(perks, "apply_role_perks", _apply)
    return calls, state


@pytest.mark.parametrize("pause_fails", [False, True], ids=["clean", "pause_fails"])
async def test_start_pauses_rentals_and_ends_pen_pals(ctx, hooks, pause_fails):
    calls, state = hooks
    state["pause_fails"] = pause_fails
    m = _member(Guild(), [DENIZEN])

    assert (await service.start_break(ctx, m)).ok
    # A failed pause is logged, never allowed to skip the pen-pal step —
    # which is an event Pen Pals listens for, not a call into its cog.
    assert ("paused", 5, True, None) in calls
    ctx.bot.dispatch.assert_any_call("member_break_start", G, 5)


async def test_end_resumes_gate_ok_rentals_and_reprojects_perks(ctx, hooks):
    calls, state = hooks
    state["perks"] = [
        {"perk": "color", "beneficiary_id": 5},
        {"perk": "gift", "beneficiary_id": 77},  # someone else's: not ours to resume
    ]
    state["gate"] = {"color": False}
    m = _member(Guild(), [DENIZEN])
    assert (await service.start_break(ctx, m)).ok
    calls.clear()

    assert (await service.end_break(ctx, m)).ok
    assert calls == [("paused", 5, False, {"color": False}), ("apply_perks", 5)]


async def test_end_without_perks_skips_the_reprojection(ctx, hooks):
    calls, _ = hooks
    m = _member(Guild(), [DENIZEN])
    assert (await service.start_break(ctx, m)).ok
    calls.clear()

    assert (await service.end_break(ctx, m)).ok
    assert calls == [("paused", 5, False, {})]


# ── a dial moving under an open break, and strays with no row ─────


async def test_a_lite_dial_moved_mid_break_still_comes_off_on_return(ctx):
    """The row records the Spicy Lite it actually gave; the end of the break
    takes that off even though the dial now names another role."""
    m = _member(Guild(), [DENIZEN, SPICY])
    assert (await service.start_break(ctx, m)).ok
    assert _row(ctx).lite_role_id == LITE and _row(ctx).break_role_id == BREAK
    ctx.set_config_value(SPICY_LITE_ROLE_KEY, str(NEW_LITE), G)

    assert (await service.end_break(ctx, m)).ok
    assert _ids(m) == {DENIZEN, SPICY}


async def test_a_retired_hold_role_with_no_row_is_still_cleared(ctx):
    """An erased row plus a re-pointed dial: the stray old Spicy Lite is
    still recognised as a break role and taken off."""
    from bot_modules.breaks.store import retire_hold_role

    with ctx.open_db() as conn:
        retire_hold_role(conn, G, LITE, NEW_LITE)
    ctx.set_config_value(SPICY_LITE_ROLE_KEY, str(NEW_LITE), G)
    m = _member(Guild(), [DENIZEN, LITE])

    assert not (await service.end_break(ctx, m)).ok
    assert _ids(m) == {DENIZEN}


async def test_a_break_waits_for_a_jail_already_changing_the_member(ctx):
    """Anything already holding the member's role lock (a jail mid-strip)
    finishes before a break touches a single role."""
    import asyncio

    from bot_modules.breaks.locks import member_role_lock

    m = _member(Guild(), [DENIZEN])
    lock = member_role_lock(G, 5)
    await lock.acquire()
    task = asyncio.create_task(service.start_break(ctx, m))
    await asyncio.sleep(0.05)
    assert not m.add_roles.await_count and _row(ctx) is None
    lock.release()
    assert (await task).ok
