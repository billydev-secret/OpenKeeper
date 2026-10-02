"""Shared self-service role guards (core.role_safety).

These decide whether an unprivileged member may take a role by clicking a
button, so every branch is a safety gate and gets its own case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from bot_modules.core.role_safety import grant_block_reason, is_dangerous, role_block_reason


@dataclass(order=True)
class _Role:
    position: int
    id: int = field(compare=False)
    name: str = field(default="Role", compare=False)
    managed: bool = field(default=False, compare=False)
    default: bool = field(default=False, compare=False)
    perms: dict = field(default_factory=dict, compare=False)

    def __post_init__(self):
        self.permissions = SimpleNamespace(**self.perms)

    def is_default(self):
        return self.default


def _bot(position=100):
    return SimpleNamespace(top_role=_Role(position=position, id=1, name="DK"))


# ── is_dangerous ─────────────────────────────────────────────────────────────

def test_plain_role_is_not_dangerous():
    assert is_dangerous(_Role(position=5, id=2)) is False


def test_every_listed_permission_flags_the_role():
    for perm in (
        "administrator", "manage_guild", "manage_roles", "manage_channels",
        "manage_messages", "manage_webhooks", "kick_members", "ban_members",
        "moderate_members", "mention_everyone",
    ):
        role = _Role(position=5, id=2, perms={perm: True})
        assert is_dangerous(role) is True, f"{perm} should be dangerous"


def test_permission_set_to_false_is_not_dangerous():
    assert is_dangerous(_Role(position=5, id=2, perms={"administrator": False})) is False


# ── role_block_reason ────────────────────────────────────────────────────────

def test_ordinary_role_below_the_bot_is_allowed():
    assert role_block_reason(_Role(position=5, id=2), _bot()) is None


def test_missing_role_is_blocked():
    assert "doesn't exist" in (role_block_reason(None, _bot()) or "")


def test_default_role_is_blocked():
    role = _Role(position=0, id=2, name="@everyone", default=True)
    assert "default role" in (role_block_reason(role, _bot()) or "")


def test_managed_role_is_blocked():
    role = _Role(position=5, id=2, name="Booster", managed=True)
    assert "managed by an integration" in (role_block_reason(role, _bot()) or "")


def test_role_above_the_bot_is_blocked():
    role = _Role(position=200, id=2, name="Owner")
    assert "above my highest role" in (role_block_reason(role, _bot()) or "")


def test_role_equal_to_the_bots_top_role_is_blocked():
    # `>=`, not `>`: Discord can't grant a role at the bot's own height.
    role = _Role(position=100, id=2, name="Peer")
    assert "above my highest role" in (role_block_reason(role, _bot()) or "")


def test_dangerous_role_is_blocked_by_default():
    role = _Role(position=5, id=2, name="Mod", perms={"ban_members": True})
    assert "elevated permissions" in (role_block_reason(role, _bot()) or "")


def test_elevated_override_permits_a_dangerous_role():
    role = _Role(position=5, id=2, name="Mod", perms={"ban_members": True})
    assert role_block_reason(role, _bot(), allow_elevated=True) is None


def test_elevated_override_does_not_bypass_hierarchy():
    # The override is about permissions, never about what Discord lets us do.
    role = _Role(position=200, id=2, name="Owner", perms={"administrator": True})
    assert "above my highest role" in (
        role_block_reason(role, _bot(), allow_elevated=True) or ""
    )


def test_no_bot_member_skips_only_the_hierarchy_check():
    # Off-gateway we can't compare positions, but perms still disqualify.
    high = _Role(position=200, id=2, name="Owner")
    assert role_block_reason(high, None) is None
    dangerous = _Role(position=5, id=3, name="Mod", perms={"administrator": True})
    assert "elevated permissions" in (role_block_reason(dangerous, None) or "")


# ── grant_block_reason: the config-sourced grant wrapper ────────────────────

def _guild(position=100):
    return SimpleNamespace(id=42, me=_bot(position))


@pytest.mark.parametrize(
    ("role", "needle"),
    [
        pytest.param(_Role(position=5, id=2, name="STAFF", perms={"manage_messages": True}), "elevated", id="elevated"),
        pytest.param(_Role(position=150, id=2, name="Owner"), "above my highest role", id="above-bot"),
    ],
)
def test_grant_block_reason_refuses_and_logs(caplog, role, needle):
    with caplog.at_level(logging.WARNING, logger="dungeonkeeper.role_safety"):
        reason = grant_block_reason(role, _guild(), feature="Level reward role")
    assert needle in (reason or "")
    # The admin reading the log needs the feature, the role and the guild.
    assert "Level reward role" in caplog.text
    assert "role 2" in caplog.text and "guild 42" in caplog.text


def test_grant_block_reason_allows_a_plain_role_silently(caplog):
    with caplog.at_level(logging.WARNING, logger="dungeonkeeper.role_safety"):
        assert grant_block_reason(_Role(position=5, id=2), _guild(), feature="x") is None
    assert caplog.text == ""


def test_grant_block_reason_never_honours_elevated():
    # No override parameter exists: a config-sourced grant can't opt in.
    role = _Role(position=5, id=2, perms={"administrator": True})
    assert grant_block_reason(role, _guild(), feature="x") is not None
