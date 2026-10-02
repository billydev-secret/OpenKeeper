"""Mod Settings — pure decisions for the posted Mod Settings panel.

Billy, 2026-09-28: "a mod settings panel … opt in pings for Qotd time, chat
revive, bumping". These are **staff job pings** — "a job needs doing" — not
the member pings of the same names, which stay on Help & Settings → Roles &
Pings. Each is a permission-less role in ``feature_roles.STAFF_ROLES``; a
moderator presses its button on the panel to switch it on (or off again), and
the bot mentions it when the job comes up.
Spec: ``docs/specs/staff_pings_spec.md``.

**What the panel will never do** — each rule is a row in
``tests/moderation/test_mod_settings_logic.py``:

* Act for anyone who isn't a moderator (``AppContext.member_is_mod``).
* List a ping whose delivery isn't set up — its channel unset, so a press
  would switch on something that never fires (CLAUDE.md: never ship a toggle
  that isn't enforced).
* List a ping an admin set to "(none)" on the dashboard.
* Hand out a role that fails ``core.role_safety.role_block_reason``.
* Change anything for a moderator under a hold (jailed, inactive, on a break),
  exactly as the member card refuses.

The posted panel is the same for everyone who sees it, so it draws no state:
the private reply to a press says what the press did (:func:`toggled_text`).

A ping whose role doesn't exist yet is still listed: the first moderator who
turns it on makes it (or adopts one of that name). A trigger never makes one —
nobody would hold a role made at the moment it is pinged.

And one rule that runs without the panel: a member who **stops being a
moderator** loses every staff ping role (:func:`roles_to_strip`, Ben
2026-09-28), so an ex-mod isn't pinged about staff jobs. A hold (jail,
inactive, a break) that takes the moderator role away for a while is not a
step down: the opt-ins wait it out, and are stripped only if the member
comes out of the hold no longer a moderator.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from bot_modules.shared import feature_roles as fr

#: The pings the panel offers, in display order.
STAFF_PINGS: tuple[fr.FeatureRole, ...] = fr.STAFF_ROLES

NOT_A_MOD_TEXT = "❌ This panel is for moderators."
HOLD_TEXT = "❌ Your pings can't be changed from here right now."
EMPTY_TEXT = (
    "There are no staff pings to pick yet — an admin sets where each one "
    "posts on the dashboard."
)
GONE_TEXT = "That ping isn't offered any more — the panel has been refreshed."
FAILED_TEXT = "❌ Discord wouldn't make that change."


@dataclass(frozen=True)
class StaffRow:
    """One staff ping the moderator can switch on or off."""

    key: str
    #: 0 while the role hasn't been made; turning it on makes it.
    role_id: int
    label: str
    emoji: str
    blurb: str
    held: bool


@dataclass(frozen=True)
class ModCard:
    rows: tuple[StaffRow, ...]

    @property
    def empty(self) -> bool:
        return not self.rows


def build_card(
    stored: Sequence[tuple[fr.FeatureRole, int]],
    *,
    delivered: Collection[str],
    grantable: Collection[int],
    member_role_ids: Collection[int],
) -> ModCard:
    """The card for one moderator.

    ``stored`` is each staff ping the admin hasn't switched off, with its
    stored role id (0 when not made yet); ``delivered`` the keys whose channel
    is set. A made role must be ``grantable`` (safe, and still there).
    """
    held = set(member_role_ids)
    rows = tuple(
        StaffRow(
            key=entry.key,
            role_id=role_id,
            label=entry.spec.name,
            emoji=entry.emoji,
            blurb=entry.blurb,
            held=role_id > 0 and role_id in held,
        )
        for entry, role_id in stored
        if entry.key in delivered and (role_id == 0 or role_id in grantable)
    )
    return ModCard(rows=rows)


def every_row() -> tuple[StaffRow, ...]:
    """A row for every staff ping, set up or not — what the persistent view
    registered at load answers, so a press on any posted copy reaches it."""
    return tuple(
        StaffRow(
            key=entry.key, role_id=0, label=entry.spec.name,
            emoji=entry.emoji, blurb=entry.blurb, held=False,
        )
        for entry in STAFF_PINGS
    )


def toggled_text(row: StaffRow, *, on: bool) -> str:
    """The private answer to a press that worked. ``row`` is the state before
    it, so ``on`` is where the press left the moderator."""
    if on:
        return f"{row.emoji} **{row.label}** pings are now **on** for you."
    return f"{row.emoji} **{row.label}** pings are now **off** for you."


#: How long a press's own result outranks the cached roles. Discord's echo of
#: a role change lags the REST reply, so a quick second press judged on the
#: cache alone would repeat the first ("now on" twice) instead of undoing it.
RECENT_SECONDS = 30.0


def roles_after_recent(
    role_ids: Iterable[int],
    recent: Mapping[int, tuple[bool, float]],
    *,
    now: float,
    window: float = RECENT_SECONDS,
) -> set[int]:
    """The member's roles with their recent presses laid over the cache.

    ``recent`` maps a role id to (held after the press, when). An entry older
    than ``window`` is ignored: by then the cache has caught up, and a change
    made since in Discord itself must win.
    """
    roles = set(role_ids)
    for role_id, (held, at) in recent.items():
        if now - at > window:
            continue
        if held:
            roles.add(role_id)
        else:
            roles.discard(role_id)
    return roles


def row_for(card: ModCard, key: str) -> StaffRow | None:
    """The fresh card's row for a press, or None when it's no longer offered
    (its channel was unset, its dial switched off, its role made unsafe)."""
    return next((row for row in card.rows if row.key == key), None)


def roles_to_strip(
    *,
    was_mod: bool,
    is_mod: bool,
    member_role_ids: Iterable[int],
    staff_role_ids: Iterable[int],
    was_held: bool = False,
    is_held: bool = False,
) -> set[int]:
    """The staff ping roles to take off a member whose roles just changed.

    Only on the step down — a moderator (or a member under a hold) before,
    neither after. Nothing while a hold stands: it may be what took the
    moderator role, and it gives it back. A member who was never a moderator
    is left alone: an admin may have given one of these roles by hand, and
    that is theirs to undo.
    """
    if is_mod or is_held or not (was_mod or was_held):
        return set()
    staff = {r for r in staff_role_ids if r > 0}
    return staff & set(member_role_ids)
