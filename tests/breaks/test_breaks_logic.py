"""Pure break logic: refusals, the snapshot, the Spicy → Spicy Lite swap,
the return, and a jail/inactive hold absorbing an open break."""

from __future__ import annotations

import pytest

from bot_modules.breaks.logic import (
    REFUSAL_MESSAGES,
    BreakConfig,
    MemberState,
    absorb_snapshot,
    plan_break,
    plan_return,
    refusal,
)

EVERYONE = 1
BREAK = 900
LITE = 901
SPICY = 800
DENIZEN = 700
JAILED = 950
INACTIVE = 951
UNVERIFIED = 952
BOOSTER = 600  # managed (an integration's)

CFG = BreakConfig(
    break_role_id=BREAK, break_channel_id=42, spicy_role_id=SPICY, spicy_lite_role_id=LITE
)


def _state(**kw) -> MemberState:
    base = dict(
        is_bot=False, is_staff=False, on_break=False, jailed=False, inactive=False,
        role_ids=frozenset({DENIZEN}),
    )
    base.update(kw)
    return MemberState(**base)


def _refuse(state: MemberState, cfg: BreakConfig = CFG):
    return refusal(
        cfg, state, jailed_role_id=JAILED, inactive_role_id=INACTIVE,
        unverified_role_id=UNVERIFIED,
    )


def test_an_ordinary_member_may_start_a_break():
    assert _refuse(_state()) is None


@pytest.mark.parametrize(
    "cfg",
    [
        pytest.param(BreakConfig(break_role_id=0, break_channel_id=42), id="no-role"),
        pytest.param(BreakConfig(break_role_id=BREAK, break_channel_id=0), id="no-channel"),
    ],
)
def test_the_feature_is_dark_until_both_role_and_channel_are_set(cfg):
    assert _refuse(_state(), cfg) == "not_set_up"


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        pytest.param(_state(is_bot=True), "bot", id="bot"),
        pytest.param(_state(is_staff=True), "staff", id="staff"),
        pytest.param(_state(on_break=True), "on_break", id="break-row"),
        pytest.param(_state(role_ids=frozenset({BREAK})), "on_break", id="break-role-live"),
        pytest.param(_state(jailed=True), "jailed", id="jail-row"),
        pytest.param(_state(role_ids=frozenset({JAILED})), "jailed", id="jailed-role-live"),
        pytest.param(_state(inactive=True), "inactive", id="inactive-row"),
        pytest.param(_state(role_ids=frozenset({INACTIVE})), "inactive", id="inactive-role-live"),
        pytest.param(_state(role_ids=frozenset({UNVERIFIED})), "unverified", id="unverified"),
        pytest.param(_state(blocked_role_ids=frozenset({DENIZEN})), "hierarchy", id="hierarchy"),
    ],
)
def test_every_refusal(state, kind):
    assert _refuse(state) == kind
    assert REFUSAL_MESSAGES[kind]


def test_plan_snapshots_everything_but_everyone_managed_and_hold_roles():
    plan = plan_break(
        CFG,
        member_role_ids=[EVERYONE, DENIZEN, SPICY, BOOSTER, 555],
        managed_role_ids={BOOSTER},
        default_role_id=EVERYONE,
    )
    assert plan.stored_roles == [DENIZEN, SPICY, 555]
    # Integration roles stay on; the break role and Spicy Lite go on.
    assert plan.during_role_ids == [BOOSTER, BREAK, LITE]
    assert plan.swapped_spicy is True


@pytest.mark.parametrize(
    ("cfg", "roles"),
    [
        pytest.param(CFG, [DENIZEN], id="member-had-no-spicy"),
        pytest.param(
            BreakConfig(break_role_id=BREAK, break_channel_id=42, spicy_role_id=SPICY),
            [DENIZEN, SPICY],
            id="no-lite-role-picked",
        ),
        pytest.param(
            BreakConfig(break_role_id=BREAK, break_channel_id=42, spicy_lite_role_id=LITE),
            [DENIZEN, SPICY],
            id="no-spicy-role-known",
        ),
    ],
)
def test_no_lite_swap_unless_spicy_was_taken_and_a_lite_role_is_picked(cfg, roles):
    plan = plan_break(cfg, member_role_ids=roles, managed_role_ids=set(), default_role_id=EVERYONE)
    assert plan.swapped_spicy is False
    assert LITE not in plan.during_role_ids
    assert plan.during_role_ids == [BREAK]


def test_hold_roles_are_never_snapshotted():
    """A stray Spicy Lite (hand-added) must not come back as a 'real' role."""
    plan = plan_break(
        CFG, member_role_ids=[DENIZEN, LITE], managed_role_ids=set(), default_role_id=EVERYONE
    )
    assert plan.stored_roles == [DENIZEN]


def test_return_restores_the_snapshot_and_drops_the_hold_roles():
    after, missing = plan_return(
        CFG.hold_role_ids,
        stored_roles=[DENIZEN, SPICY],
        current_role_ids=[EVERYONE, BOOSTER, BREAK, LITE],
        available_role_ids={EVERYONE, BOOSTER, BREAK, LITE, DENIZEN, SPICY},
        default_role_id=EVERYONE,
    )
    assert after == [BOOSTER, DENIZEN, SPICY]
    assert missing == []


def test_return_keeps_roles_gained_during_the_break_and_reports_deleted_ones():
    after, missing = plan_return(
        CFG.hold_role_ids,
        stored_roles=[DENIZEN, 555],
        current_role_ids=[BREAK, 777],
        available_role_ids={BREAK, DENIZEN, 777},
        default_role_id=EVERYONE,
    )
    assert after == [777, DENIZEN]
    assert missing == [555]


def test_absorb_merges_the_break_into_a_mod_hold_snapshot():
    """Jailing a break-taker snapshots [break, lite] alone; the release would
    hand the break back and strand the real roles. Absorbing fixes that."""
    merged = absorb_snapshot([DENIZEN, SPICY], [BREAK, LITE, 777], CFG.hold_role_ids)
    assert merged == [DENIZEN, SPICY, 777]
