"""The /grant prerequisite gate, and the announcement it posts.

The production shape: the Member grant requires the verification role, so a
newcomer who never verified can't be granted Member. The gate shipped
unreachable — ``/grant`` never passed ``required_role_id`` to the executor —
so these cases are the enforcement, not decoration.

The announcement rows at the bottom cover the other half of a grant: the
admin-authored template that goes to the announce channel as plain
content.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.roles.grant.role_grant_commands import _resolve_grant_message
from bot_modules.roles.grant.logic import (
    GATE_MISSING_PREREQUISITE,
    GATE_NO_PERMISSION,
    GATE_OK,
    GATE_PREREQUISITE_DELETED,
    grant_refusal,
    prerequisite_gate,
    prerequisites_for_role,
    role_prerequisite_verdict,
)

# The production shape: Member is gated behind verification, NSFW is a grant
# with no prerequisite, and one grant is parked on "(none)" (role_id 0).
_GRANTS = {
    "denizen": {"role_id": 100, "required_role_id": 900},
    "nsfw": {"role_id": 200, "required_role_id": 0},
    "kink": {"role_id": 0, "required_role_id": 0},
}


@pytest.mark.parametrize(
    ("required_role_id", "exists", "held", "is_admin", "expected"),
    [
        # No prerequisite configured — the default for every grant.
        pytest.param(0, False, False, False, GATE_OK, id="unconfigured"),
        pytest.param(0, False, False, True, GATE_OK, id="unconfigured-admin"),
        # The bug: verification absent, grant attempted.
        pytest.param(
            555, True, False, False, GATE_MISSING_PREREQUISITE, id="missing"
        ),
        pytest.param(555, True, True, False, GATE_OK, id="held"),
        # Admins override; moderators do not (is_mod isn't an input at all).
        pytest.param(555, True, False, True, GATE_OK, id="admin-bypasses-missing"),
        # Fails closed when the required role was deleted...
        pytest.param(
            555, False, False, False, GATE_PREREQUISITE_DELETED, id="deleted"
        ),
        # ...except for the admin who has to go fix the config.
        pytest.param(555, False, False, True, GATE_OK, id="admin-bypasses-deleted"),
        # A negative id is as unconfigured as 0 — guards against a hand-edited
        # DB row or a picker that writes -1 for "(none)".
        pytest.param(-1, False, False, False, GATE_OK, id="negative-id"),
    ],
)
def test_prerequisite_gate(required_role_id, exists, held, is_admin, expected):
    assert (
        prerequisite_gate(
            required_role_id=required_role_id,
            required_role_exists=exists,
            target_has_required=held,
            actor_is_admin=is_admin,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("grants", "role_id", "expected"),
    [
        # The bypass this closes: a role menu published for the gated role.
        pytest.param(_GRANTS, 100, (900,), id="gated-grant-role"),
        pytest.param(_GRANTS, 200, (), id="grant-role-without-prerequisite"),
        pytest.param(_GRANTS, 300, (), id="role-that-is-not-a-grant"),
        # "(none)" stores role_id 0; a menu can't hold role 0, but nothing
        # should ever match on it either.
        pytest.param(_GRANTS, 0, (), id="unset-grant-never-matches"),
        pytest.param({}, 100, (), id="no-grants-configured"),
        # Two grants on one role: both prerequisites have to be cleared.
        pytest.param(
            {
                "a": {"role_id": 100, "required_role_id": 900},
                "b": {"role_id": 100, "required_role_id": 800},
                "c": {"role_id": 100, "required_role_id": 900},
            },
            100,
            (800, 900),
            id="two-grants-one-role-deduped",
        ),
    ],
)
def test_prerequisites_for_role(grants, role_id, expected):
    assert prerequisites_for_role(grants, role_id) == expected


def test_missing_prerequisite_implies_the_role_exists():
    """The caller dereferences the role to name it in the refusal, so
    MISSING_PREREQUISITE must never be returned for a role that's gone."""
    assert (
        prerequisite_gate(
            required_role_id=555,
            required_role_exists=False,
            target_has_required=False,
            actor_is_admin=False,
        )
        != GATE_MISSING_PREREQUISITE
    )


# ── one role, handed out by id (the intake card's Give role) ────────────────
#
# The card's button put the role on with no look at the grant config, so a
# greeter could make a member of a newcomer who never verified. It is a third
# route to the role after /grant and the role menus, and it gets the same
# answers; these rows are the verdict the press acts on.

# Two grants pointing at one role, each with its own prerequisite.
_TWO_GATES = {
    "a": {"role_id": 100, "required_role_id": 900},
    "b": {"role_id": 100, "required_role_id": 800},
}


@pytest.mark.parametrize(
    ("grants", "role_id", "guild_roles", "held", "is_admin", "expected"),
    [
        # THE BUG: Give role pressed on a newcomer who hasn't verified.
        pytest.param(
            _GRANTS, 100, {100, 900}, set(), False,
            (GATE_MISSING_PREREQUISITE, 900),
            id="unverified-is-refused",
        ),
        pytest.param(_GRANTS, 100, {100, 900}, {900}, False, (GATE_OK, 0), id="verified"),
        # Admins override, as they do on /grant. Greeters and mods do not —
        # neither is an input here.
        pytest.param(
            _GRANTS, 100, {100, 900}, set(), True, (GATE_OK, 0),
            id="admin-bypasses-missing",
        ),
        # Fails closed once the prerequisite is deleted...
        pytest.param(
            _GRANTS, 100, {100}, set(), False,
            (GATE_PREREQUISITE_DELETED, 900),
            id="deleted-fails-closed",
        ),
        # ...even for a member a stale cache still lists it on.
        pytest.param(
            _GRANTS, 100, {100}, {900}, False,
            (GATE_PREREQUISITE_DELETED, 900),
            id="deleted-is-never-held",
        ),
        pytest.param(
            _GRANTS, 100, {100}, set(), True, (GATE_OK, 0),
            id="admin-bypasses-deleted",
        ),
        # Nothing to clear: no prerequisite, not a grant role, no grants at all.
        pytest.param(_GRANTS, 200, {200}, set(), False, (GATE_OK, 0), id="no-prerequisite"),
        pytest.param(_GRANTS, 300, {300}, set(), False, (GATE_OK, 0), id="not-a-grant-role"),
        pytest.param({}, 100, {100}, set(), False, (GATE_OK, 0), id="no-grants-configured"),
        # Two grants on one role: every prerequisite has to clear.
        pytest.param(
            _TWO_GATES, 100, {100, 800, 900}, {800, 900}, False, (GATE_OK, 0),
            id="two-gates-both-held",
        ),
        pytest.param(
            _TWO_GATES, 100, {100, 800, 900}, {800}, False,
            (GATE_MISSING_PREREQUISITE, 900),
            id="two-gates-one-held-names-the-other",
        ),
        pytest.param(
            _TWO_GATES, 100, {100, 800, 900}, set(), False,
            (GATE_MISSING_PREREQUISITE, 800),
            id="two-gates-none-held-names-the-first",
        ),
        # 800 sorts first and is merely missing; 900 is gone. Handing the
        # member 800 would not open the gate, so the broken one is reported.
        pytest.param(
            _TWO_GATES, 100, {100, 800}, set(), False,
            (GATE_PREREQUISITE_DELETED, 900),
            id="deleted-outranks-missing",
        ),
    ],
)
def test_role_prerequisite_verdict(grants, role_id, guild_roles, held, is_admin, expected):
    assert (
        role_prerequisite_verdict(
            grants,
            role_id,
            guild_role_ids=guild_roles,
            target_role_ids=held,
            actor_is_admin=is_admin,
        )
        == expected
    )


# ── which refusal /grant owes the caller (todo #176) ────────────────────────
#
# The reported bug: a greeter ran /grant on a newcomer who hadn't verified and
# got the bare "You don't have permission to use this command." The command
# checked the *actor's* allow-list before it ever looked at the member, so the
# one fact worth telling them — the member hasn't verified — was never reached.
# Billy's call: a refusal about the member outranks a refusal about the actor,
# accepting that anyone who can run /grant now learns a member's prerequisite
# state. These rows are that ordering; the strings live in the command.


@pytest.mark.parametrize(
    ("may_grant", "required_role_id", "exists", "held", "is_admin", "expected"),
    [
        # Nothing in the way.
        pytest.param(True, 0, False, False, False, GATE_OK, id="permitted-no-prereq"),
        pytest.param(True, 555, True, True, False, GATE_OK, id="permitted-prereq-held"),
        # Permitted actor, member not verified — unchanged by the reorder.
        pytest.param(
            True, 555, True, False, False, GATE_MISSING_PREREQUISITE,
            id="permitted-prereq-missing",
        ),
        # THE BUG. Not on the allow-list *and* the member hasn't verified: the
        # member's state is what the caller can act on, so it wins. Before the
        # fix this was GATE_NO_PERMISSION and the verification never surfaced.
        pytest.param(
            False, 555, True, False, False, GATE_MISSING_PREREQUISITE,
            id="unpermitted-prereq-missing-reports-the-member",
        ),
        # ...and a deleted prerequisite still outranks the actor gate, so a
        # broken gate is reported as broken rather than hidden behind it.
        pytest.param(
            False, 555, False, False, False, GATE_PREREQUISITE_DELETED,
            id="unpermitted-prereq-deleted-reports-the-config",
        ),
        # With nothing wrong on the member's side the actor gate is the honest
        # answer, so the allow-list keeps working exactly as before.
        pytest.param(
            False, 555, True, True, False, GATE_NO_PERMISSION,
            id="unpermitted-prereq-held",
        ),
        pytest.param(
            False, 0, False, False, False, GATE_NO_PERMISSION,
            id="unpermitted-no-prereq",
        ),
        # An admin passes both gates without being listed anywhere. Asserted on
        # the logic rather than inherited from can_use_grant_role's own admin
        # bypass, so the ordering can't quietly start depending on the caller.
        pytest.param(False, 555, True, False, True, GATE_OK, id="admin-bypasses-both"),
        pytest.param(False, 555, False, False, True, GATE_OK, id="admin-bypasses-deleted"),
    ],
)
def test_grant_refusal(may_grant, required_role_id, exists, held, is_admin, expected):
    assert (
        grant_refusal(
            actor_may_grant=may_grant,
            required_role_id=required_role_id,
            required_role_exists=exists,
            target_has_required=held,
            actor_is_admin=is_admin,
        )
        == expected
    )


# ── the announcement an admin wrote ────────────────────────────────────
#
# ``/grant``'s announce channel gets an admin-authored template rendered
# into plain message content, where every mention is live. The allow-list
# comes from the template text (``core.utils.template_pings``); the escape
# below is the second guard on the one substitution the member controls.


def _announce_actors(display_name: str = "Newbie"):
    member = MagicMock()
    member.mention = "<@500>"
    member.display_name = display_name
    role = MagicMock()
    role.mention = "<@&42>"
    role.name = "Denizen"
    interaction = MagicMock()
    interaction.user.mention = "<@9>"
    return member, role, interaction


def test_the_announcement_still_renders_every_placeholder():
    member, role, interaction = _announce_actors()

    assert _resolve_grant_message(
        "{member} ({member_name}) got {role} ({role_name}) from {actor}",
        member, role, None, interaction,
    ) == "<@500> (Newbie) got <@&42> (Denizen) from <@9>"


@pytest.mark.parametrize(
    "display_name",
    [
        pytest.param("@everyone", id="everyone"),
        pytest.param("@here", id="here"),
        pytest.param("<@&123456789012345678>", id="role-mention"),
    ],
)
def test_a_hostile_display_name_cannot_ping_through_the_announcement(display_name):
    """An admin's template may carry an ``@everyone``; a member's name may not.

    ``{member_name}`` splices in text the member picked, so a newcomer
    renaming themselves ``@everyone`` would otherwise ride the Mention
    Everyone permission the admin's own line needs — the same trap
    ``welcome_service.render_arrival_message`` escapes, escaped the same way.
    """
    member, role, interaction = _announce_actors(display_name)

    out = _resolve_grant_message(
        "@everyone {member_name} got {role}", member, role, None, interaction
    )

    assert out.startswith("@everyone ")  # the admin's own ping survives
    assert display_name not in out[len("@everyone "):]  # the member's does not
    # Neutralised, not deleted — the name still reads in the channel.
    assert discord.utils.escape_mentions(display_name) in out
