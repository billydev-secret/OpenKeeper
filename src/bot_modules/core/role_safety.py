"""Shared guards for features that hand out roles on a member's own click.

Any self-service role grant (role menus, announcement buttons) faces the same
question: is it safe to let an unprivileged member take this role by pressing a
button? The answer lives here so there is exactly one list of dangerous
permissions to keep current — role menus grew the original copy (spec §3.2).

Two call sites with different stakes:

* **Write path** (a dashboard route saving a config) — reject early with a
  message naming the role, so misconfiguration is nearly impossible.
* **Click path** (a button callback, possibly on a message posted months ago) —
  re-check and fail closed. A role that was harmless when it was configured can
  be granted ``administrator`` later, and a posted announcement is never
  revisited; without this the stale button would still hand it out.

The same check guards every grant whose role comes from **config** — a dial,
a grant definition, a shop item — even when a mod or the bot itself is the one
granting: on 2026-09-03 a promotion-review dial pointed at STAFF and a routine
approval handed it out. :func:`grant_block_reason` is that check plus the log
line an admin needs; ``tests/contracts/test_role_grant_safety_contract.py`` fails when a
new ``add_roles`` site reaches neither function nor its allowlist.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger("dungeonkeeper.role_safety")

# Permission bits that make a role dangerous to hand out via self-service.
_DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mention_everyone",
)


def is_dangerous(role: discord.Role) -> bool:
    """True if the role carries any permission we won't self-serve by default."""
    perms = role.permissions
    return any(getattr(perms, name, False) for name in _DANGEROUS_PERMS)


def role_block_reason(
    role: discord.Role | None,
    bot_member: discord.Member | None,
    *,
    allow_elevated: bool = False,
) -> str | None:
    """Why this role can't be self-assigned, or None if it can.

    The string is written to be shown to an admin on the dashboard; callers on
    the click path log it rather than surfacing it to the member (it names
    server configuration a member has no business seeing).

    ``allow_elevated`` is the role-menus per-option override — announcements
    deliberately never pass it, so an elevated role can't ride a public post.
    ``bot_member`` of None skips only the hierarchy check (the bot isn't in the
    guild yet, or we're off-gateway); every other guard still applies.
    """
    if role is None:
        return "that role doesn't exist (anymore)"
    if role.is_default():
        return f"“{role.name}” is the default role and can't be assigned"
    if role.managed:
        return f"“{role.name}” is managed by an integration and can't be self-assigned"
    if bot_member is not None and role >= bot_member.top_role:
        return f"“{role.name}” is above my highest role — I can't grant it"
    if is_dangerous(role) and not allow_elevated:
        return f"“{role.name}” carries elevated permissions"
    return None


def grant_block_reason(
    role: discord.Role, guild: discord.Guild | None, *, feature: str
) -> str | None:
    """:func:`role_block_reason` for a config-sourced grant, logged when refused.

    For call sites with nobody to tell but the log (an automatic grant, or a
    member who shouldn't see the server's permission layout). A caller with a
    mod to answer can still show the returned reason.

    ``guild`` is optional because the body has always reached it through
    ``getattr``: an interaction's ``guild`` is ``Guild | None``, and a missing
    one simply yields no bot member, which :func:`role_block_reason` already
    treats as "cannot tell — allow, the grant-time check still stands".
    """
    reason = role_block_reason(role, getattr(guild, "me", None))
    if reason is not None:
        log.warning(
            "%s: refused to grant role %s in guild %s: %s",
            feature, role.id, getattr(guild, "id", None), reason,
        )
    return reason
