"""Pure break logic — no Discord calls, no database.

Every decision a break makes lives here so the refusals, the snapshot and the
Spicy → Spicy Lite swap are pinned by plain unit tests. ``breaks.service`` does
the I/O around these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RefusalKind = Literal[
    "not_set_up",   # no break role / break channel configured — the feature is dark
    "bot",
    "staff",        # mod or admin: stripping staff deletes their personal perk role
    "on_break",     # already on one
    "jailed",
    "inactive",
    "unverified",   # removing Unverified fires the welcome/greeter ping
    "hierarchy",    # a role to strip is at or above the bot
    "unsafe_role",  # a hold-role dial names a role the bot won't hand out
]

REFUSAL_MESSAGES: dict[RefusalKind, str] = {
    "not_set_up": "Breaks aren't set up on this server yet.",
    "bot": "Bots can't take a break.",
    "staff": (
        "Moderators and admins can't use the break button — ask another admin "
        "to step your roles down instead."
    ),
    "on_break": "You're already on a break. Press **👋 Back** in the break channel to return.",
    "jailed": "You can't start a break while you're in a moderation hold.",
    "inactive": "You can't start a break from the inactive channel — open a ticket there instead.",
    "unverified": "You can take a break once you've finished joining the server.",
    "hierarchy": (
        "One of your roles sits above mine, so I couldn't give it back later. "
        "Ask an admin to check my role position."
    ),
    "unsafe_role": "Breaks aren't set up correctly here — ask an admin to check the break roles.",
}


@dataclass(frozen=True)
class BreakConfig:
    """The dials a break reads. 0 = not configured."""

    break_role_id: int
    break_channel_id: int
    spicy_role_id: int = 0
    spicy_lite_role_id: int = 0

    @property
    def enabled(self) -> bool:
        """Dark until an admin has run *Set up break channel*, which sets both."""
        return self.break_role_id > 0 and self.break_channel_id > 0

    @property
    def hold_role_ids(self) -> frozenset[int]:
        """The roles a break puts on — never snapshotted, never restored."""
        return frozenset(r for r in (self.break_role_id, self.spicy_lite_role_id) if r > 0)


@dataclass(frozen=True)
class MemberState:
    """What the refusal check needs to know about the presser."""

    is_bot: bool
    is_staff: bool
    on_break: bool
    jailed: bool
    inactive: bool
    role_ids: frozenset[int]
    #: Roles the break would strip that sit at or above the bot's top role.
    blocked_role_ids: frozenset[int] = frozenset()


def refusal(
    cfg: BreakConfig,
    member: MemberState,
    *,
    jailed_role_id: int = 0,
    inactive_role_id: int = 0,
    unverified_role_id: int = 0,
) -> RefusalKind | None:
    """Why this member can't start a break, or ``None`` if they can.

    Jail and inactive are judged on the DB row **or** the live role, the same
    two halves Grant Audit uses — a hand-added @Jailed with no row still means
    a mod is holding this member.
    """
    if not cfg.enabled:
        return "not_set_up"
    if member.is_bot:
        return "bot"
    if member.is_staff:
        return "staff"
    if member.on_break or (cfg.break_role_id in member.role_ids):
        return "on_break"
    if member.jailed or (jailed_role_id > 0 and jailed_role_id in member.role_ids):
        return "jailed"
    if member.inactive or (inactive_role_id > 0 and inactive_role_id in member.role_ids):
        return "inactive"
    if unverified_role_id > 0 and unverified_role_id in member.role_ids:
        return "unverified"
    if member.blocked_role_ids:
        return "hierarchy"
    return None


@dataclass(frozen=True)
class BreakPlan:
    """What starting a break does to one member."""

    #: Saved to ``break_holds`` and given back on return.
    stored_roles: list[int]
    #: The member's full role set for the duration (one ``edit(roles=…)``).
    during_role_ids: list[int]
    swapped_spicy: bool


def plan_break(
    cfg: BreakConfig,
    *,
    member_role_ids: list[int],
    managed_role_ids: set[int],
    default_role_id: int,
) -> BreakPlan:
    """Snapshot everything strippable, keep integration roles, add the hold roles.

    ``@everyone`` and managed roles are never snapshotted (the bot can neither
    strip nor restore them); managed roles stay on through the break. The break
    and Spicy Lite roles are never snapshotted either — restoring them would
    re-apply the break. Spicy Lite goes on only when Spicy was taken off *and*
    an admin picked a lite role.
    """
    stored = [
        rid
        for rid in member_role_ids
        if rid != default_role_id
        and rid not in managed_role_ids
        and rid not in cfg.hold_role_ids
    ]
    swapped = cfg.spicy_role_id > 0 and cfg.spicy_lite_role_id > 0 and cfg.spicy_role_id in stored
    during = [rid for rid in member_role_ids if rid in managed_role_ids]
    during.append(cfg.break_role_id)
    if swapped:
        during.append(cfg.spicy_lite_role_id)
    return BreakPlan(stored_roles=stored, during_role_ids=during, swapped_spicy=swapped)


def plan_return(
    hold_role_ids: frozenset[int],
    *,
    stored_roles: list[int],
    current_role_ids: list[int],
    available_role_ids: set[int],
    default_role_id: int,
) -> tuple[list[int], list[int]]:
    """``(role set after the break, stored roles that no longer exist)``.

    Anything the member picked up during the break (a mod hand-adding a role,
    an integration) is kept; only the hold roles come off. ``hold_role_ids``
    is every role that means "on a break" — the dials now, what they named
    before, and what this break was actually given (``breaks.store``). A role deleted while
    they were away cannot be given back and is reported, as Inactive does.
    """
    missing = [rid for rid in stored_roles if rid not in available_role_ids]
    after: list[int] = [
        rid
        for rid in current_role_ids
        if rid != default_role_id and rid not in hold_role_ids
    ]
    for rid in stored_roles:
        if rid in available_role_ids and rid not in after:
            after.append(rid)
    return after, missing


def absorb_snapshot(
    break_stored: list[int], hold_snapshot: list[int], hold_role_ids: frozenset[int]
) -> list[int]:
    """Merge an open break into a jail/inactive snapshot.

    A mod jailing (or setting inactive) someone on a break would otherwise
    snapshot just ``[break, lite]`` — and releasing them would hand the break
    back while their real roles stayed stranded in the break row. So the new
    hold takes the break's stored roles, plus anything else they hold now,
    minus the break's own roles.
    """
    merged = list(break_stored)
    for rid in hold_snapshot:
        if rid not in hold_role_ids and rid not in merged:
            merged.append(rid)
    return merged
