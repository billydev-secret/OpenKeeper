"""Server Features — Roles & Pings card: what it offers and what a press does.

Each exclusion in ``server_features.logic``'s docstring is a row here: the
card is a self-assign surface, so a passing test is the enforcement of "never
hand out a gate, a hold, or an unsafe role".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.needle.store import set_reply_optin
from bot_modules.server_features import logic, service
from bot_modules.shared import feature_roles as fr
from bot_modules.onboarding.welcome.onboarding_service import OptionView, PromptView

GUILD = 42
USER = 100


def _prompt(pid, *options, single=False, required=False, title="Pick"):
    return PromptView(
        id=pid, title=title, single_select=single, required=required,
        options=tuple(options),
    )


def _opt(title, *role_ids, channels=()):
    return OptionView(title=title, role_ids=tuple(role_ids), channel_ids=tuple(channels))


# ── which roles the card may offer ───────────────────────────────────────


def test_member_pings_are_the_opt_in_dials_minus_gates_and_staff():
    keys = {e.key for e in logic.MEMBER_PINGS}
    assert keys == {
        fr.WELCOME_PING.key, fr.QOTD_PING.key, fr.RISKY_PING.key,
        fr.GAME_LOBBY_PING.key, fr.ECONOMY_NOTIFY.key,
    }


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(fr.GUESS_ROLE, id="guess-gate"),
        pytest.param(fr.VOICE_SPECTATE_GATE, id="spectate-gate"),
        pytest.param(fr.PROMOTION_REVIEW_PING, id="staff-ping"),
        pytest.param(fr.JAILED_ROLE, id="jail-hold"),
        pytest.param(fr.INACTIVE_ROLE, id="inactive-hold"),
        pytest.param(fr.BREAK_ROLE, id="break-hold"),
        pytest.param(fr.DM_OPEN_ROLE, id="dm-mode"),
        pytest.param(fr.SURVIVOR_ROLE, id="survivor"),
        pytest.param(fr.WELLNESS_ROLE, id="wellness"),
    ],
)
def test_every_other_managed_role_is_excluded(entry):
    assert entry in logic.EXCLUDED
    assert entry not in logic.MEMBER_PINGS


# ── Customize dropdowns ──────────────────────────────────────────────────


def test_a_prompt_becomes_a_menu_with_the_members_picks_selected():
    menus, truncated = logic.build_menus(
        [_prompt(1, _opt("Red", 11), _opt("Blue", 12), single=True, required=True)],
        grantable={11, 12},
        member_role_ids={12},
    )
    assert not truncated
    [menu] = menus
    assert [o.selected for o in menu.options] == [False, True]
    assert (menu.min_values, menu.max_values) == (1, 1)


def test_a_multi_select_optional_menu_can_be_cleared():
    menus, _ = logic.build_menus(
        [_prompt(1, _opt("A", 1), _opt("B", 2), _opt("C", 3))],
        grantable={1, 2, 3}, member_role_ids=set(),
    )
    assert (menus[0].min_values, menus[0].max_values) == (0, 3)


@pytest.mark.parametrize(
    "option",
    [
        pytest.param(_opt("Channels only", channels=(5,)), id="no-roles"),
        pytest.param(_opt("Unsafe", 99), id="ungrantable"),
        pytest.param(_opt("Half unsafe", 11, 99), id="partly-ungrantable"),
    ],
)
def test_an_option_is_dropped_whole_unless_every_role_is_grantable(option):
    menus, _ = logic.build_menus(
        [_prompt(1, _opt("Fine", 11), option)], grantable={11}, member_role_ids=set()
    )
    assert [o.label for o in menus[0].options] == ["Fine"]


def test_a_prompt_left_with_no_options_is_not_drawn():
    menus, _ = logic.build_menus(
        [_prompt(1, _opt("Unsafe", 99))], grantable=set(), member_role_ids=set()
    )
    assert menus == []


def test_an_option_is_selected_only_when_the_member_has_all_its_roles():
    menus, _ = logic.build_menus(
        [_prompt(1, _opt("Both", 1, 2))], grantable={1, 2}, member_role_ids={1}
    )
    assert menus[0].options[0].selected is False


def test_options_cap_at_discords_twenty_five():
    opts = [_opt(f"R{i}", i) for i in range(1, 31)]
    menus, _ = logic.build_menus(
        [_prompt(1, *opts)], grantable=set(range(1, 31)), member_role_ids=set()
    )
    assert len(menus[0].options) == logic.MAX_MENU_OPTIONS


def test_menus_past_the_budget_are_cut_and_flagged():
    prompts = [_prompt(i, _opt("x", i)) for i in range(1, logic.MAX_MENUS + 3)]
    menus, truncated = logic.build_menus(
        prompts, grantable=set(range(1, 20)), member_role_ids=set()
    )
    assert len(menus) == logic.MAX_MENUS
    assert truncated


# ── the whole card ───────────────────────────────────────────────────────


def test_a_ping_is_listed_with_its_state():
    card = logic.build_card(
        [(fr.QOTD_PING, 7)], [], grantable={7}, member_role_ids={7}
    )
    [row] = card.pings
    assert (row.key, row.role_id, row.held) == (fr.QOTD_PING.key, 7, True)
    assert not card.empty


def test_an_ungrantable_ping_is_left_off():
    card = logic.build_card([(fr.QOTD_PING, 7)], [], grantable=set(), member_role_ids=set())
    assert card.empty


def test_a_ping_also_in_customize_shows_once_under_customize():
    card = logic.build_card(
        [(fr.QOTD_PING, 7), (fr.RISKY_PING, 8)],
        [_prompt(1, _opt("QOTD", 7))],
        grantable={7, 8},
        member_role_ids=set(),
    )
    assert [p.role_id for p in card.pings] == [8]
    assert card.menus[0].options[0].role_ids == (7,)


# ── what a dropdown pick changes ─────────────────────────────────────────


def _menu(*options):
    return logic.RoleMenu(
        prompt_id=1, title="t", single_select=False, required=False,
        options=tuple(
            logic.MenuOption(label=str(i), description="", emoji="", role_ids=r, selected=False)
            for i, r in enumerate(options)
        ),
    )


def test_a_pick_adds_the_chosen_and_removes_the_unchosen():
    add, remove = logic.plan_menu_change(_menu((1,), (2,), (3,)), [0], {2, 50})
    assert (add, remove) == ({1}, {2})


def test_a_role_shared_by_a_chosen_option_stays():
    add, remove = logic.plan_menu_change(_menu((1, 9), (2, 9)), [0], {2, 9})
    assert (add, remove) == ({1}, {2})


def test_a_pick_never_touches_roles_outside_the_menu():
    _, remove = logic.plan_menu_change(_menu((1,), (2,)), [], {1, 2, 50})
    assert remove == {1, 2}


# ── holds ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("roles", "jailed", "held"),
    [
        pytest.param({5}, False, False, id="free"),
        pytest.param({5, 30}, False, True, id="holds-a-hold-role"),
        pytest.param(set(), True, True, id="jailed-row-without-role"),
        pytest.param({0}, False, False, id="unset-hold-id-ignored"),
    ],
)
def test_under_hold(roles, jailed, held):
    assert logic.under_hold(roles, [0, 30], jailed=jailed) is held


# ── service: the database read ───────────────────────────────────────────


def test_readings_list_set_pings_and_exclude_managed_ids(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, fr.QOTD_PING.key, "7", GUILD)
        set_config_value(conn, fr.WELCOME_PING.key, "0", GUILD)  # "(none)"
        set_config_value(conn, fr.GUESS_ROLE.key, "70", GUILD)
        set_config_value(conn, fr.BREAK_ROLE.key, "30", GUILD)
        readings = service.read_role_readings(conn, GUILD, USER)
    assert readings.pings == ((fr.QOTD_PING, 7),)
    assert {70, 30} <= readings.excluded_ids
    assert 30 in readings.hold_ids
    assert 0 not in readings.excluded_ids
    assert readings.jailed is False


def test_readings_see_an_active_jail(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO jails (guild_id, user_id, moderator_id, created_at) VALUES (?,?,?,0)",
            (GUILD, USER, 1),
        )
        assert service.read_role_readings(conn, GUILD, USER).jailed is True


# ── service: which guild roles are grantable ─────────────────────────────


def _role(rid, name="r", *, managed=False, perms=0, position=1):
    role = MagicMock(spec=discord.Role)
    role.id, role.name, role.managed = rid, name, managed
    role.is_default.return_value = False
    role.permissions = discord.Permissions(perms)
    role.position = position
    role.__ge__ = lambda self, other: self.position >= other.position
    return role


def test_grantable_filters_excluded_named_and_unsafe_roles():
    roles = [
        _role(1),
        _role(2),  # excluded by id
        _role(3, "DMs: Open"),  # excluded by name — no stored id
        _role(4, managed=True),
        _role(5, perms=discord.Permissions(administrator=True).value),
    ]
    assert service.grantable_role_ids(roles, None, excluded_ids={2}) == {1}


async def test_apply_change_rechecks_safety_before_granting():
    safe, unsafe, gone = _role(1), _role(2, managed=True), None
    guild = SimpleNamespace(
        me=None, get_role={1: safe, 2: unsafe, 3: gone}.get
    )
    member = SimpleNamespace(
        id=USER, guild=guild, add_roles=AsyncMock(), remove_roles=AsyncMock()
    )
    assert await service.apply_change(member, {1, 2, 3}, {1}) == (True, True)
    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args == (safe,)
    member.remove_roles.assert_awaited_once()


async def test_apply_change_reports_a_refusal():
    member = SimpleNamespace(
        id=USER,
        guild=SimpleNamespace(me=None, get_role={1: _role(1)}.get),
        add_roles=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no")),
        remove_roles=AsyncMock(),
    )
    assert await service.apply_change(member, {1}, set()) == (False, True)


async def test_apply_change_reports_the_add_even_when_the_remove_fails():
    """A remove Discord refuses must not hide an add that already landed —
    the two are reported independently so the caller can reflect exactly
    what happened rather than rolling back to "nothing changed"."""
    member = SimpleNamespace(
        id=USER,
        guild=SimpleNamespace(me=None, get_role={1: _role(1), 2: _role(2)}.get),
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no")),
    )
    assert await service.apply_change(member, {1}, {2}) == (True, False)
    member.add_roles.assert_awaited_once()
    member.remove_roles.assert_awaited_once()


async def test_a_refused_add_skips_the_remove():
    """A Customize swap is add-A-remove-B: taking B after Discord refused A
    would leave the member with neither role."""
    member = SimpleNamespace(
        id=USER,
        guild=SimpleNamespace(me=None, get_role={1: _role(1), 2: _role(2)}.get),
        add_roles=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no")),
        remove_roles=AsyncMock(),
    )
    assert await service.apply_change(member, {1}, {2}) == (False, False)
    member.remove_roles.assert_not_awaited()


async def test_prompts_degrade_to_none_without_manage_server():
    guild = SimpleNamespace(
        id=GUILD,
        onboarding=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no")),
    )
    assert await service.read_prompts_or_none(guild) == ()


# ── select values and a pick judged against a fresh card ─────────────────


def test_an_option_value_is_its_role_set_not_its_position():
    menus, _ = logic.build_menus(
        [_prompt(1, _opt("Both", 2, 1))], grantable={1, 2}, member_role_ids=set()
    )
    assert menus[0].options[0].value == "1,2"


def test_a_duplicate_role_set_is_offered_once():
    menus, _ = logic.build_menus(
        [_prompt(1, _opt("A", 1), _opt("Also A", 1))], grantable={1}, member_role_ids=set()
    )
    assert [o.label for o in menus[0].options] == ["A"]


def test_a_pick_survives_a_reorder_and_drops_a_withdrawn_option():
    fresh, _ = logic.build_menus(
        [_prompt(1, _opt("C", 3), _opt("A", 1))], grantable={1, 3}, member_role_ids=set()
    )
    # "2" was on the card when it was drawn; the admin has since removed it.
    assert logic.chosen_indices(fresh[0], ["1", "2"]) == [1]


def _card_with(held: bool):
    return logic.build_card(
        [(fr.QOTD_PING, 7)], [], grantable={7}, member_role_ids={7} if held else set()
    )


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        pytest.param(False, ({7}, set()), id="off-turns-on"),
        pytest.param(True, (set(), {7}), id="on-turns-off"),
    ],
)
def test_ping_change_flips_the_ping(held, expected):
    assert logic.ping_change(_card_with(held), fr.QOTD_PING.key) == expected


def test_ping_change_refuses_a_ping_the_fresh_card_dropped():
    assert logic.ping_change(_card_with(False), fr.RISKY_PING.key) is None


# ── the Auto-Thread reply-DM row ─────────────────────────────────────────


def _card(reply_dms, prompts=()):
    return logic.build_card([], list(prompts), grantable=set(range(1, 20)),
                            member_role_ids=set(), reply_dms=reply_dms)


@pytest.mark.parametrize(
    ("reply_dms", "expected"),
    [
        pytest.param(False, True, id="off-turns-on"),
        pytest.param(True, False, id="on-turns-off"),
        # Every channel's dial went off since the card opened: no write.
        pytest.param(None, None, id="no-longer-offered"),
    ],
)
def test_reply_dms_change(reply_dms, expected):
    assert logic.reply_dms_change(_card(reply_dms)) is expected


def test_a_card_with_only_the_reply_dms_row_is_not_empty():
    assert _card(None).empty
    assert not _card(False).empty


@pytest.mark.parametrize(
    ("reply_dms", "menus"),
    [
        pytest.param(None, logic.MAX_MENUS, id="no-row-full-menus"),
        pytest.param(False, logic.MAX_MENUS_WITH_REPLY_DMS, id="row-costs-a-menu"),
    ],
)
def test_the_reply_dms_row_gives_up_one_dropdown(reply_dms, menus):
    prompts = [_prompt(i, _opt("x", i)) for i in range(1, logic.MAX_MENUS + 2)]
    card = _card(reply_dms, prompts)
    assert len(card.menus) == menus and card.menus_truncated


def _needle_channel(conn, *, dial: bool) -> None:
    conn.execute(
        "INSERT INTO needle_channels (guild_id, channel_id, notify_op_on_reply) VALUES (?, 5, ?)",
        (GUILD, int(dial)),
    )


@pytest.mark.parametrize(
    ("dial", "opted_in", "expected"),
    [
        pytest.param(None, False, None, id="no-auto-thread-channel"),
        # A switch that could only be turned on, to no effect, isn't offered.
        pytest.param(False, False, None, id="dial-off-hides-row"),
        # The bug: every dial went off and an opted-in member lost their off
        # switch while the row stayed stored. They keep the row.
        pytest.param(False, True, True, id="dial-off-opted-in-keeps-off-switch"),
        pytest.param(None, True, True, id="no-channel-opted-in-keeps-off-switch"),
        pytest.param(True, False, False, id="offered-off"),
        pytest.param(True, True, True, id="offered-on"),
    ],
)
def test_readings_offer_reply_dms_only_where_a_dial_is_on(sync_db_path, dial, opted_in, expected):
    with open_db(sync_db_path) as conn:
        if dial is not None:
            _needle_channel(conn, dial=dial)
        set_reply_optin(conn, guild_id=GUILD, user_id=USER, on=opted_in)
        assert service.read_role_readings(conn, GUILD, USER).reply_dms is expected


def test_with_change_is_what_the_redraw_shows():
    assert logic.with_change({1, 2}, {3}, {2}) == {1, 3}
