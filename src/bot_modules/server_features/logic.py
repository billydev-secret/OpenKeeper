"""Roles & Pings — pure decisions for the Server Features panel's role card.

The card lets a member change two kinds of role in one place:

* **Bot pings** — the bot's own opt-in ping roles (``feature_roles``): Welcome,
  QOTD, Risky Rolls, Game Lobbies, Economy Notifications. One On/Off row each.
* **From Customize** — the questions in Discord's own Customize page (the
  guild's onboarding prompts), one dropdown per question, read live from
  Discord each time the card opens so it can't drift from what an admin set
  there.

And one switch that is not a role:

* **Bot DMs → Replies to Your Threads** — the Auto-Thread reply-DM opt-in
  (``needle.store``, migration 244). A table row, not a role, so nobody else
  can see who asked for DMs. Offered only while some auto-threaded channel in
  the guild has its reply-DM dial on: with every dial off it would be a switch
  that does nothing.

Everything here takes plain values and returns plain values; the Discord and
database half is ``server_features.service``.

**What the card will never hand out** — the rules below are the card's safety
story, and each is a row in ``tests/server_features/test_server_features_logic.py``:

* A role that fails ``core.role_safety.role_block_reason`` (managed, above the
  bot, or carrying moderation permissions). It is left off the card rather
  than shown and then refused, and a Customize option with any such role is
  dropped whole — granting half of an option would be a stranger outcome than
  not offering it.
* Any other role the bot manages for a feature of its own (``EXCLUDED``
  below): Guess Who and Voice Spectate are *gates* whose feature shows consent
  copy before granting them (``member_info.views`` — "reuse is the safety
  property"); the jail, inactive and break holds are moderation state; the DM
  mode roles belong to DM Settings; Survivor and Wellness to their features.
  Offering any of them here would be a second, ungated door, so they are
  excluded even when an admin also put them in Customize.
* Every staff job ping (``feature_roles.STAFF_ROLES``: Promotion Reviewers
  and the rest) — moderators opt into those on the Mod Settings panel.
* Nothing at all to a member under a hold (jailed, inactive, or on a break):
  those states *removed* their roles, and a role card that let them add some
  back would undo a moderator's decision.

The card never creates a role. A ping whose role was never made, or whose dial
an admin set to "(none)", simply isn't listed.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from bot_modules.shared import feature_roles as fr
from bot_modules.onboarding.welcome.onboarding_service import PromptView

#: The pings a member may toggle here, in display order.
MEMBER_PINGS: tuple[fr.FeatureRole, ...] = tuple(
    entry
    # CONFIG_ROLES already leaves out every staff ping (the Mod Settings panel
    # offers those). create_on_offer roles (Guess Who, Voice Spectate) are
    # feature gates with their own consent flow.
    for entry in fr.CONFIG_ROLES
    if not entry.create_on_offer
)

#: Every other bot-managed role: never toggled here, from either section.
EXCLUDED: tuple[fr.FeatureRole, ...] = tuple(
    entry for entry in fr.MANAGED_ROLES if entry not in MEMBER_PINGS
)

#: Discord's cap on a select's options.
MAX_MENU_OPTIONS = 25
#: Dropdowns the card draws before pointing at Customize for the rest. Three
#: components each (heading, row, select) against the 40-component ceiling,
#: after five ping rows and the card's own frame.
MAX_MENUS = 5

#: The Bot DMs section (separator, heading, one Section row) costs five, so a
#: card that shows it gives up one dropdown: 38 + 5 − 3 = 40.
MAX_MENUS_WITH_REPLY_DMS = MAX_MENUS - 1

#: The Bot DMs row's words. It is about Auto-Thread, so it says "threads".
REPLY_DMS_LABEL = "Replies to Your Threads"
REPLY_DMS_EMOJI = "🧵"
REPLY_DMS_BLURB = "Get a DM when someone replies to a thread you started."

HOLD_TEXT = (
    "❌ Your roles can't be changed from here right now. If you're on a break, "
    "press **👋 Back** in the break channel first."
)


@dataclass(frozen=True)
class PingRow:
    """One bot ping the member can switch on or off."""

    key: str
    role_id: int
    label: str
    emoji: str
    blurb: str
    held: bool


@dataclass(frozen=True)
class MenuOption:
    label: str
    description: str
    emoji: str
    role_ids: tuple[int, ...]
    selected: bool

    @property
    def value(self) -> str:
        """The dropdown value: the roles it hands out, not its position.

        A pick is judged against a card re-read at submit time, and an admin
        may have reordered Customize in between — an index would then point
        at a different option. The role set names the same thing on both
        reads. ``build_menus`` keeps it unique within a menu.
        """
        return ",".join(str(r) for r in sorted(self.role_ids))


@dataclass(frozen=True)
class RoleMenu:
    """One Customize question as a dropdown."""

    prompt_id: int
    title: str
    single_select: bool
    required: bool
    options: tuple[MenuOption, ...]

    @property
    def min_values(self) -> int:
        # Discord won't let a member clear a required question in Customize
        # either; the dropdown holds the same line.
        return 1 if self.required else 0

    @property
    def max_values(self) -> int:
        return 1 if self.single_select else len(self.options)


@dataclass(frozen=True)
class RoleCard:
    pings: tuple[PingRow, ...]
    menus: tuple[RoleMenu, ...]
    #: Customize questions left off for space — the card says so.
    menus_truncated: bool = False
    #: The member's Auto-Thread reply-DM opt-in; None when the guild offers no
    #: reply DMs, and the row isn't drawn.
    reply_dms: bool | None = None

    @property
    def empty(self) -> bool:
        return not self.pings and not self.menus and self.reply_dms is None


def build_menus(
    prompts: Iterable[PromptView],
    *,
    grantable: Collection[int],
    member_role_ids: Collection[int],
    limit: int = MAX_MENUS,
) -> tuple[list[RoleMenu], bool]:
    """Customize questions as dropdowns, and whether any were left off.

    An option survives only if it hands out at least one role and every role
    it hands out is in ``grantable`` (safe, and not ``EXCLUDED``). Channel-only
    options are Discord's to manage. A question left with no options is
    dropped; so is one that has more than :data:`MAX_MENU_OPTIONS`, cut to the
    first 25 rather than hidden, because a partial menu is still correct for
    every option it shows.
    """
    menus: list[RoleMenu] = []
    held = set(member_role_ids)
    for prompt in prompts:
        options: list[MenuOption] = []
        for option in prompt.options:
            if not option.role_ids or not all(r in grantable for r in option.role_ids):
                continue
            candidate = MenuOption(
                label=option.title[:100],
                description=option.description[:100],
                emoji=option.emoji,
                role_ids=tuple(option.role_ids),
                selected=all(r in held for r in option.role_ids),
            )
            # Two options handing out the same roles would share a value,
            # which Discord refuses; the second says nothing the first doesn't.
            if any(o.value == candidate.value for o in options):
                continue
            options.append(candidate)
        options = options[:MAX_MENU_OPTIONS]
        if not options:
            continue
        menus.append(
            RoleMenu(
                prompt_id=prompt.id,
                title=prompt.title,
                single_select=prompt.single_select,
                required=prompt.required,
                options=tuple(options),
            )
        )
    return menus[:limit], len(menus) > limit


def build_card(
    pings: Sequence[tuple[fr.FeatureRole, int]],
    prompts: Iterable[PromptView],
    *,
    grantable: Collection[int],
    member_role_ids: Collection[int],
    reply_dms: bool | None = None,
) -> RoleCard:
    """The whole card for one member.

    ``pings`` are the :data:`MEMBER_PINGS` whose role exists and whose dial
    isn't switched off, paired with the role id; the service resolves those.
    A ping whose role also appears in a Customize dropdown is listed once,
    under Customize — the admin put it there, so that is where it lives.
    """
    menus, truncated = build_menus(
        prompts,
        grantable=grantable,
        member_role_ids=member_role_ids,
        limit=MAX_MENUS if reply_dms is None else MAX_MENUS_WITH_REPLY_DMS,
    )
    in_menus = {r for menu in menus for option in menu.options for r in option.role_ids}
    held = set(member_role_ids)
    rows = tuple(
        PingRow(
            key=entry.key,
            role_id=role_id,
            label=entry.spec.name,
            emoji=entry.emoji,
            blurb=entry.blurb,
            held=role_id in held,
        )
        for entry, role_id in pings
        if role_id in grantable and role_id not in in_menus
    )
    return RoleCard(
        pings=rows, menus=tuple(menus), menus_truncated=truncated, reply_dms=reply_dms
    )


def reply_dms_row_state(*, offered: bool, opted_in: bool) -> bool | None:
    """The Bot DMs row's value: the member's opt-in, or None to leave it off.

    Shown where some channel offers reply DMs — and always to a member who is
    opted in, even after every dial went off, so their stored opt-in keeps an
    off switch on the card. Off and not offered is the only hidden case: that
    switch could only be turned on, to no effect.
    """
    if offered or opted_in:
        return opted_in
    return None


def reply_dms_change(card: RoleCard) -> bool | None:
    """The opt-in a press on the Bot DMs row asks for, or None if the fresh
    card no longer offers it (every channel's reply-DM dial was switched off)."""
    if card.reply_dms is None:
        return None
    return not card.reply_dms


def chosen_indices(menu: RoleMenu, values: Iterable[str]) -> list[int]:
    """The options a submitted pick names, by :attr:`MenuOption.value`.

    A value the fresh menu no longer offers is dropped: the admin took that
    option out of Customize (or made one of its roles unsafe) since the card
    was drawn, and a pick can't grant what the card would no longer show.
    """
    wanted = set(values)
    return [i for i, option in enumerate(menu.options) if option.value in wanted]


def plan_menu_change(
    menu: RoleMenu, chosen: Collection[int], member_role_ids: Collection[int]
) -> tuple[set[int], set[int]]:
    """``(add, remove)`` for a dropdown pick. ``chosen`` are option indexes.

    Roles of the chosen options are added; roles of the unchosen ones come
    off — except one a chosen option also hands out, which stays. Only roles
    this menu offers are ever touched, so a pick in one question can't remove
    a role that belongs to another.
    """
    held = set(member_role_ids)
    keep = {r for i in chosen for r in menu.options[i].role_ids}
    offered = {r for option in menu.options for r in option.role_ids}
    return keep - held, (offered - keep) & held


def ping_change(
    card: RoleCard, key: str
) -> tuple[set[int], set[int]] | None:
    """``(add, remove)`` for a press on one ping's toggle, or None if the fresh
    card no longer lists that ping (its dial was switched off, its role
    deleted or made unsafe, or it moved into a Customize dropdown)."""
    for row in card.pings:
        if row.key == key:
            return (set(), {row.role_id}) if row.held else ({row.role_id}, set())
    return None


def with_change(
    member_role_ids: Collection[int], add: Collection[int], remove: Collection[int]
) -> set[int]:
    """The member's roles once a change lands.

    The card redraws from this rather than from the cached member: a REST
    role change isn't written back into discord.py's member cache until the
    gateway echoes it, so a re-read straight after would show the old state.
    """
    return (set(member_role_ids) | set(add)) - set(remove)


def under_hold(
    member_role_ids: Collection[int], hold_role_ids: Iterable[int], *, jailed: bool
) -> bool:
    """Whether the member is jailed, inactive or on a break (see module docstring)."""
    if jailed:
        return True
    held = set(member_role_ids)
    return any(r > 0 and r in held for r in hold_role_ids)
