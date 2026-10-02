"""Mod Settings — the posted staff-ping panel and the step-down strip.

Each rule in ``mod_settings.logic``'s docstring is a row here: the panel hands
out roles, so a passing test is the enforcement of "only moderators, only
pings that can fire, never an unsafe role, nothing under a hold".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.core.layout import check_budget
from bot_modules.moderation.settings import logic, service
from bot_modules.moderation.settings.layouts import (
    PING_CUSTOM_ID_PREFIX,
    build_mod_settings_hub_layout,
    ping_custom_id,
)
from bot_modules.shared import feature_roles as fr
from bot_modules.xp.promotion_review_service import CHANNEL_KEY as REVIEW_CHANNEL_KEY

GUILD = 42
USER = 100
PROMO = fr.PROMOTION_REVIEW_PING


@pytest.fixture(autouse=True)
def _fresh_press_memory(monkeypatch):
    """A press's remembered result is module state; no test may see another's."""
    monkeypatch.setattr(service, "_recent", {})
    monkeypatch.setattr(service, "_press_locks", {})


# ── registry ─────────────────────────────────────────────────────────────


def test_the_panel_offers_exactly_the_staff_pings():
    assert logic.STAFF_PINGS == fr.STAFF_ROLES
    assert PROMO in logic.STAFF_PINGS
    for entry in logic.STAFF_PINGS:
        assert entry.staff and entry.opt_in and entry.assigns, entry.key
        assert entry.spec.permissions == discord.Permissions.none(), entry.key


# ── build_card ───────────────────────────────────────────────────────────


def _build(role_id, *, delivered=(PROMO.key,), grantable=(7,), held=()):
    return logic.build_card(
        [(PROMO, role_id)],
        delivered=set(delivered),
        grantable=set(grantable),
        member_role_ids=set(held),
    )


@pytest.mark.parametrize(
    "role_id, kwargs, listed, on",
    [
        pytest.param(7, {}, True, False, id="made-off"),
        pytest.param(7, {"held": (7,)}, True, True, id="made-on"),
        pytest.param(0, {}, True, False, id="not-made-yet-listed-off"),
        pytest.param(7, {"delivered": ()}, False, False, id="no-channel-not-listed"),
        pytest.param(7, {"grantable": ()}, False, False, id="unsafe-role-not-listed"),
        pytest.param(0, {"delivered": ()}, False, False, id="unmade-and-no-channel"),
    ],
)
def test_build_card(role_id, kwargs, listed, on):
    card = _build(role_id, **kwargs)
    assert bool(card.rows) is listed
    if listed:
        assert card.rows[0].held is on
        assert card.rows[0].label == PROMO.spec.name


def test_every_row_is_one_unheld_row_per_staff_ping():
    """What the view registered at load answers: a press on any posted copy
    must reach a handler, whichever pings that copy happened to draw."""
    rows = logic.every_row()
    assert [r.key for r in rows] == [e.key for e in logic.STAFF_PINGS]
    assert not any(r.held for r in rows)


@pytest.mark.parametrize(
    "on, word", [pytest.param(True, "**on**", id="on"), pytest.param(False, "**off**", id="off")]
)
def test_toggled_text_says_where_the_press_left_them(on, word):
    row = _build(7).rows[0]
    text = logic.toggled_text(row, on=on)
    assert row.label in text and word in text


@pytest.mark.parametrize(
    "recent, expected",
    [
        pytest.param({}, {1, 7}, id="nothing-recent"),
        pytest.param({7: (False, 95.0)}, {1}, id="turned-off-recently"),
        pytest.param({9: (True, 95.0)}, {1, 7, 9}, id="turned-on-recently"),
        pytest.param({7: (False, 10.0)}, {1, 7}, id="stale-press-cache-wins"),
    ],
)
def test_roles_after_recent(recent, expected):
    assert logic.roles_after_recent({1, 7}, recent, now=100.0, window=30.0) == expected


def test_row_for_finds_a_listed_ping_and_refuses_a_dropped_one():
    card = _build(7)
    assert logic.row_for(card, PROMO.key) is card.rows[0]
    assert logic.row_for(_build(7, delivered=()), PROMO.key) is None


# ── the step-down strip ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "was_mod, is_mod, was_held, is_held, expected",
    [
        pytest.param(True, False, False, False, {7}, id="stepped-down-strips"),
        pytest.param(True, True, False, False, set(), id="still-a-mod"),
        pytest.param(False, False, False, False, set(), id="never-a-mod-hand-given-role-kept"),
        pytest.param(False, True, False, False, set(), id="promoted"),
        pytest.param(True, False, False, True, set(), id="a-hold-took-the-mod-role"),
        pytest.param(False, False, True, True, set(), id="still-under-the-hold"),
        pytest.param(False, False, True, False, {7}, id="out-of-a-hold-no-longer-a-mod"),
        pytest.param(False, True, True, False, set(), id="out-of-a-hold-still-a-mod"),
    ],
)
def test_roles_to_strip(was_mod, is_mod, was_held, is_held, expected):
    got = logic.roles_to_strip(
        was_mod=was_mod, is_mod=is_mod, was_held=was_held, is_held=is_held,
        member_role_ids={7, 8}, staff_role_ids=[7, 0, 9],
    )
    assert got == expected


# ── service: readings ────────────────────────────────────────────────────


def test_readings_offer_promotion_reviews_only_with_a_review_channel(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert PROMO.key not in service.read_readings(conn, GUILD, USER).delivered
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        readings = service.read_readings(conn, GUILD, USER)
    assert PROMO.key in readings.delivered
    assert (PROMO, 0) in readings.stored


def test_readings_drop_a_ping_an_admin_set_to_none(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, PROMO.key, "0", GUILD)
        readings = service.read_readings(conn, GUILD, USER)
    assert all(entry is not PROMO for entry, _ in readings.stored)


def test_readings_for_the_guild_alone_see_no_jail(sync_db_path):
    """The posted panel reads with no member: a jailed moderator's row must
    not make the guild's read say "jailed"."""
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO jails (guild_id, user_id, moderator_id, created_at) VALUES (?,?,?,0)",
            (GUILD, USER, 1),
        )
        assert service.read_readings(conn, GUILD).jailed is False


def test_readings_see_a_hold(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, fr.BREAK_ROLE.key, "30", GUILD)
        conn.execute(
            "INSERT INTO jails (guild_id, user_id, moderator_id, created_at) VALUES (?,?,?,0)",
            (GUILD, USER, 1),
        )
        readings = service.read_readings(conn, GUILD, USER)
    assert 30 in readings.hold_ids and readings.jailed is True


# ── service: read_card ───────────────────────────────────────────────────


def _role(rid, *, managed=False, perms=0):
    role = MagicMock(spec=discord.Role)
    role.id, role.name, role.managed = rid, "r", managed
    role.is_default.return_value = False
    role.permissions = discord.Permissions(perms)
    role.position = 1
    return role


def _ctx(path):
    return SimpleNamespace(open_db=lambda: open_db(path))


def _member(roles, guild_roles):
    by_id = {r.id: r for r in guild_roles}
    guild = SimpleNamespace(id=GUILD, me=None, roles=guild_roles, get_role=by_id.get)
    return SimpleNamespace(
        id=USER, guild=guild, roles=roles,
        add_roles=AsyncMock(), remove_roles=AsyncMock(),
    )


async def test_read_card_lists_a_made_role_and_its_state(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, PROMO.key, "7", GUILD)
    role = _role(7)
    read = await service.read_card(_ctx(sync_db_path), _member([role], [role]))
    assert read.held is False
    assert [(r.role_id, r.held) for r in read.card.rows] == [(7, True)]


async def test_read_card_treats_a_deleted_role_as_not_made(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, PROMO.key, "7", GUILD)
    read = await service.read_card(_ctx(sync_db_path), _member([], []))
    assert [r.role_id for r in read.card.rows] == [0]


async def test_read_card_hides_an_unsafe_role(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, PROMO.key, "7", GUILD)
    role = _role(7, perms=discord.Permissions(administrator=True).value)
    read = await service.read_card(_ctx(sync_db_path), _member([], [role]))
    assert read.card.empty


async def test_read_card_refuses_a_held_moderator(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, fr.BREAK_ROLE.key, "30", GUILD)
    brk = _role(30)
    read = await service.read_card(_ctx(sync_db_path), _member([brk], [brk]))
    assert read.held is True and read.card.empty


async def test_read_offered_lists_the_guilds_pings_with_no_state(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, PROMO.key, "7", GUILD)
    role = _role(7)
    guild = _member([role], [role]).guild
    card = await service.read_offered(_ctx(sync_db_path), guild)
    assert [(r.key, r.held) for r in card.rows] == [(PROMO.key, False)]


# ── service: a press on the panel ────────────────────────────────────────


def _read(*, held_role=False, held=False, offered=True):
    rows = _build(7, held=(7,) if held_role else ()).rows if offered else ()
    return service.CardRead(card=logic.ModCard(rows=rows), held=held)


@pytest.mark.parametrize(
    "read, on_ok, off_ok, text, stale, turned",
    [
        pytest.param(_read(), 7, True, "**on**", False, "on", id="off-turns-on"),
        pytest.param(_read(held_role=True), 7, True, "**off**", False, "off", id="on-turns-off"),
        pytest.param(_read(), None, True, logic.FAILED_TEXT, False, "on", id="grant-refused"),
        pytest.param(
            _read(held_role=True), 7, False, logic.FAILED_TEXT, False, "off", id="removal-refused"
        ),
        pytest.param(_read(held=True), 7, True, logic.HOLD_TEXT, False, None, id="under-a-hold"),
        pytest.param(_read(offered=False), 7, True, logic.GONE_TEXT, True, None, id="no-longer-offered"),
    ],
)
async def test_toggle(monkeypatch, read, on_ok, off_ok, text, stale, turned):
    turn_on = AsyncMock(return_value=on_ok)
    turn_off = AsyncMock(return_value=off_ok)
    monkeypatch.setattr(service, "read_card", AsyncMock(return_value=read))
    monkeypatch.setattr(service, "turn_on", turn_on)
    monkeypatch.setattr(service, "turn_off", turn_off)
    result = await service.toggle(SimpleNamespace(), _member([], []), PROMO.key)
    assert text in result.text
    assert result.stale is stale
    assert turn_on.await_count == (1 if turned == "on" else 0)
    assert turn_off.await_count == (1 if turned == "off" else 0)


async def test_a_double_tap_undoes_the_first_press(sync_db_path, monkeypatch):
    """Discord's echo of the role change lags the reply, so the cached roles
    still say Off on the second press. The press's own result must win, or a
    quick second tap answers "now on" twice. Run concurrently, the second
    press also waits for the first."""
    import asyncio

    with open_db(sync_db_path) as conn:
        set_config_value(conn, REVIEW_CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, PROMO.key, "7", GUILD)
    role = _role(7)
    member = _member([], [role])  # the cache never catches up
    monkeypatch.setattr(service, "turn_on", AsyncMock(return_value=7))
    monkeypatch.setattr(service, "turn_off", AsyncMock(return_value=True))
    ctx = _ctx(sync_db_path)
    first, second = await asyncio.gather(
        service.toggle(ctx, member, PROMO.key), service.toggle(ctx, member, PROMO.key)
    )
    assert "**on**" in first.text and "**off**" in second.text
    third = await service.toggle(ctx, member, PROMO.key)
    assert "**on**" in third.text


def test_the_redraw_event_reaches_the_cog():
    """The dashboard dispatches ``CONFIG_CHANGE_EVENT``; a renamed listener
    would leave every posted panel stale without failing anything."""
    from bot_modules.moderation.settings.cog import ModSettingsCog
    from bot_modules.moderation.settings import staff_ping_service

    names = {name for name, _ in ModSettingsCog.__cog_listeners__}
    assert f"on_{staff_ping_service.CONFIG_CHANGE_EVENT}" in names


@pytest.mark.parametrize("has_bot", [pytest.param(True, id="bot"), pytest.param(False, id="web-only")])
def test_announce_change(has_bot):
    from bot_modules.moderation.settings import staff_ping_service

    bot = MagicMock() if has_bot else None
    staff_ping_service.announce_change(bot, GUILD)
    if has_bot:
        bot.dispatch.assert_called_once_with(staff_ping_service.CONFIG_CHANGE_EVENT, GUILD)


# ── service: presses ─────────────────────────────────────────────────────


async def test_turn_on_makes_the_role_then_grants_it(monkeypatch):
    role = _role(7)
    ensure = AsyncMock(return_value=role)
    monkeypatch.setattr(service, "ensure_config_role", ensure)
    member = _member([], [role])
    assert await service.turn_on(SimpleNamespace(), member, PROMO) == 7
    assert ensure.await_args.kwargs["assigns"] is True
    member.add_roles.assert_awaited_once()


@pytest.mark.parametrize(
    "made",
    [
        pytest.param(None, id="dial-off-or-refused"),
        pytest.param(_role(7, managed=True), id="made-but-unsafe"),
    ],
)
async def test_turn_on_refuses(monkeypatch, made):
    monkeypatch.setattr(service, "ensure_config_role", AsyncMock(return_value=made))
    member = _member([], [])
    assert await service.turn_on(SimpleNamespace(), member, PROMO) is None
    member.add_roles.assert_not_awaited()


async def test_turn_on_reports_a_discord_refusal(monkeypatch):
    role = _role(7)
    monkeypatch.setattr(service, "ensure_config_role", AsyncMock(return_value=role))
    member = _member([], [role])
    member.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    assert await service.turn_on(SimpleNamespace(), member, PROMO) is None


@pytest.mark.parametrize(
    "exists, fails, ok",
    [
        pytest.param(True, False, True, id="removed"),
        pytest.param(False, False, True, id="already-gone"),
        pytest.param(True, True, False, id="refused"),
    ],
)
async def test_turn_off(exists, fails, ok):
    role = _role(7)
    member = _member([role], [role] if exists else [])
    if fails:
        member.remove_roles.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    assert await service.turn_off(member, 7) is ok


# ── service: strip on step-down ──────────────────────────────────────────


@pytest.mark.parametrize(
    "was, now, hold, stripped",
    [
        pytest.param(True, False, None, True, id="stepped-down"),
        pytest.param(True, True, None, False, id="still-mod"),
        pytest.param(False, False, None, False, id="never-mod"),
        pytest.param(True, False, "break", False, id="a-break-took-the-mod-role"),
        pytest.param(True, False, "jail", False, id="jailed-by-row-alone"),
        pytest.param(False, False, "released", True, id="released-no-longer-a-mod"),
    ],
)
async def test_strip_on_step_down(sync_db_path, was, now, hold, stripped):
    """A hold (jail, inactive, a break) may take the moderator role for a
    while; the opt-ins wait it out rather than being stripped."""
    break_role = _role(50)
    with open_db(sync_db_path) as conn:
        set_config_value(conn, PROMO.key, "7", GUILD)
        set_config_value(conn, fr.BREAK_ROLE.key, "50", GUILD)
        if hold == "jail":
            from bot_modules.moderation.service import create_jail

            create_jail(
                conn, guild_id=GUILD, user_id=USER, moderator_id=1, reason="r",
                stored_roles=[], channel_id=2, duration_seconds=None,
            )
    role = _role(7)
    before_roles = [role, break_role] if hold == "released" else [role]
    after_roles = [role, break_role] if hold == "break" else [role]
    before = _member(before_roles, [role, break_role])
    after = _member(after_roles, [role, break_role])
    ctx = SimpleNamespace(
        open_db=lambda: open_db(sync_db_path),
        member_is_mod=lambda m: was if m is before else now,
    )
    await service.strip_on_step_down(ctx, before, after)
    assert after.remove_roles.await_count == (1 if stripped else 0)


@pytest.mark.parametrize(
    "is_member, is_mod, admitted",
    [
        pytest.param(True, True, True, id="a-moderator"),
        pytest.param(True, False, False, id="not-a-moderator"),
        pytest.param(False, True, False, id="not-a-guild-member"),
    ],
)
def test_admitted_mod(is_member, is_mod, admitted):
    """The gate on every press."""
    user = MagicMock(spec=discord.Member if is_member else discord.User)
    ctx = SimpleNamespace(member_is_mod=lambda m: is_mod)
    assert (service.admitted_mod(ctx, user) is user) is admitted


def test_a_moderator_who_steps_down_is_refused_on_the_next_press():
    user = MagicMock(spec=discord.Member)
    mod = {"now": True}
    ctx = SimpleNamespace(member_is_mod=lambda m: mod["now"])
    assert service.admitted_mod(ctx, user) is user  # one press
    mod["now"] = False
    assert service.admitted_mod(ctx, user) is None  # the press after


# ── layouts ──────────────────────────────────────────────────────────────


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _buttons(items):
    return [i for i in _view(items).walk_children() if isinstance(i, discord.ui.Button)]


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(logic.every_row(), id="every-ping"),
        pytest.param(_build(7).rows, id="one-ping"),
        pytest.param((), id="none-set-up"),
    ],
)
def test_hub_draws_one_toggle_per_ping_and_fits(rows):
    items = build_mod_settings_hub_layout(rows)
    check_budget(_view(items))
    assert [b.custom_id for b in _buttons(items)] == [ping_custom_id(r.key) for r in rows]


def test_hub_with_nothing_set_up_says_so():
    items = build_mod_settings_hub_layout(())
    assert logic.EMPTY_TEXT in items[0].content


def test_hub_button_ids_are_frozen():
    """Every posted copy carries these ids; renaming the prefix orphans them."""
    assert PING_CUSTOM_ID_PREFIX == "mod_settings:ping:"
    assert ping_custom_id(PROMO.key) == f"mod_settings:ping:{PROMO.key}"
