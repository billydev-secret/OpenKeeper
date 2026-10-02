"""Tests for the extracted DM-permission pure-logic modules.

Covers ``bot_modules/dm/perms/logic.py`` (validation, formatting, decision)
(the cards themselves moved to ``layouts.py`` with the Components V2
conversion, and are covered by ``test_dm_perms_layouts.py``). Same
pressure-cooker pattern as the starboard/emoji-stealer refactors: the cog file
keeps the Discord glue, this file proves the helpers behave without any
real Discord client.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.dm.perms.logic import (
    audit_line_accepted,
    audit_line_asked,
    audit_line_denied,
    audit_line_expired,
    audit_line_revoked,
    clamp_reason,
    classify_dm_request,
    discard_consent_pair,
    display_name_for,
    pick_dm_roles_to_remove,
    safe_field_text,
)


# ── safe_field_text ──────────────────────────────────────────────────


def test_safe_field_text_returns_dash_for_empty_string():
    assert safe_field_text("") == "—"


def test_safe_field_text_returns_dash_for_none():
    assert safe_field_text(None) == "—"


def test_safe_field_text_escapes_markdown():
    # ``escape_markdown`` backslash-escapes special characters so a
    # crafted reason can't impersonate bot formatting.
    out = safe_field_text("**bold**")
    assert "**" not in out or "\\*" in out


def test_safe_field_text_passes_plain_text_through():
    assert safe_field_text("hello world") == "hello world"


# ── clamp_reason ─────────────────────────────────────────────────────


def test_clamp_reason_passes_short_text_through():
    assert clamp_reason("hi", max_len=10) == "hi"


def test_clamp_reason_passes_text_at_exact_limit_unchanged():
    text = "x" * 10
    assert clamp_reason(text, max_len=10) == text


def test_clamp_reason_truncates_with_ellipsis():
    text = "x" * 20
    out = clamp_reason(text, max_len=10)
    assert len(out) == 10
    assert out.endswith("…")
    # max_len - 1 chars of original + ellipsis
    assert out == "x" * 9 + "…"


def test_clamp_reason_handles_empty_string():
    assert clamp_reason("", max_len=5) == ""


# ── classify_dm_request ──────────────────────────────────────────────


def _classify_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build classify_dm_request kwargs with passing defaults (returns None)."""
    base: dict[str, Any] = dict(
        target_in_guild=True,
        is_self=False,
        target_is_bot=False,
        target_mode="ask",
        is_mutual=False,
        has_pending=False,
        target_display_name="Bob",
    )
    base.update(overrides)
    return base


def test_classify_dm_request_returns_none_when_all_checks_pass():
    assert classify_dm_request(**_classify_kwargs()) is None


def test_classify_dm_request_rejects_user_not_in_guild():
    out = classify_dm_request(**_classify_kwargs(target_in_guild=False))
    assert out is not None
    assert "couldn't check" in out.lower() or "may not be in this server" in out


def test_classify_dm_request_rejects_self_request():
    out = classify_dm_request(**_classify_kwargs(is_self=True))
    assert out is not None
    assert "yourself" in out.lower()


def test_classify_dm_request_rejects_bot_targets():
    out = classify_dm_request(**_classify_kwargs(target_is_bot=True))
    assert out is not None
    assert "bot" in out.lower()


def test_classify_dm_request_rejects_closed_target():
    out = classify_dm_request(**_classify_kwargs(target_mode="closed"))
    assert out is not None
    assert "Bob" in out
    assert "isn't accepting" in out.lower() or "not accepting" in out.lower()


def test_classify_dm_request_short_circuits_open_target_without_request():
    out = classify_dm_request(**_classify_kwargs(target_mode="open"))
    assert out is not None
    assert "open" in out.lower()
    assert "no request needed" in out.lower() or "just message" in out.lower()


def test_classify_dm_request_blocks_when_already_mutual():
    out = classify_dm_request(**_classify_kwargs(is_mutual=True))
    assert out is not None
    assert "already" in out.lower() or "connection" in out.lower()


def test_classify_dm_request_blocks_when_pending_request_exists():
    out = classify_dm_request(**_classify_kwargs(has_pending=True))
    assert out is not None
    assert "pending" in out.lower()


def test_classify_dm_request_self_request_takes_priority_over_bot_target():
    """Priority: not-in-guild > self > bot > mode > mutual > pending."""
    # If both is_self and target_is_bot are set, is_self wins.
    out = classify_dm_request(**_classify_kwargs(is_self=True, target_is_bot=True))
    assert out is not None
    assert "yourself" in out.lower()


def test_classify_dm_request_not_in_guild_takes_priority_over_self():
    out = classify_dm_request(**_classify_kwargs(target_in_guild=False, is_self=True))
    assert out is not None
    assert "may not be in this server" in out.lower()


# ── pick_dm_roles_to_remove ──────────────────────────────────────────


def _role(name: str, position: int):
    r = MagicMock(spec=discord.Role)
    r.name = name
    r.position = position
    return r


def test_pick_dm_roles_to_remove_returns_empty_when_no_roles():
    assert pick_dm_roles_to_remove([]) == []


def test_pick_dm_roles_to_remove_returns_empty_when_single_role():
    assert pick_dm_roles_to_remove([_role("DMs: Ask", 1)]) == []


def test_pick_dm_roles_to_remove_keeps_highest_position():
    """With no ``keep`` to go on, the highest ``position`` survives.

    This is the fallback for duplicates the bot did not create. The rationale
    once recorded here — "the new role is bumped above the old" — was simply
    untrue: Discord does not move a role's position when it is assigned, so
    position never encoded recency, and trusting it reverted real choices.
    """
    low = _role("DMs: Ask", 1)
    mid = _role("DMs: Closed", 5)
    high = _role("DMs: Open", 10)
    out = pick_dm_roles_to_remove([low, mid, high])
    # high must NOT be in to-remove
    assert high not in out
    # low and mid must be removed
    assert low in out
    assert mid in out
    assert len(out) == 2


def test_pick_dm_roles_to_remove_handles_two_role_case():
    a = _role("DMs: Ask", 1)
    b = _role("DMs: Open", 2)
    out = pick_dm_roles_to_remove([a, b])
    assert out == [a]


def test_pick_dm_roles_to_remove_keeps_the_just_chosen_role():
    """The member's new choice survives even when it sits lowest.

    The 2026-08-18 defect: a member on Ask pressed Closed, briefly held both,
    and the dedup kept Ask because it sat higher in the role list — silently
    reverting the choice while the panel confirmed it. ``keep`` is the role
    that arrived in this member-update, so it must win regardless of position.
    """
    chosen = _role("DM CLOSED", 1)
    previous = _role("ASK TO DM", 9)
    out = pick_dm_roles_to_remove([previous, chosen], keep=chosen)
    assert out == [previous]


def test_pick_dm_roles_to_remove_falls_back_to_position_without_keep():
    chosen = _role("DM CLOSED", 1)
    previous = _role("ASK TO DM", 9)
    assert pick_dm_roles_to_remove([previous, chosen]) == [chosen]


def test_pick_dm_roles_to_remove_ignores_a_keep_the_member_does_not_hold():
    """A stale ``keep`` degrades to the position rule rather than stripping all."""
    a = _role("DMs: Ask", 1)
    b = _role("DMs: Open", 2)
    stranger = _role("DMs: Closed", 7)
    out = pick_dm_roles_to_remove([a, b], keep=stranger)
    assert out == [a]


def test_pick_dm_roles_to_remove_accepts_iterable():
    a = _role("DMs: Ask", 1)
    b = _role("DMs: Open", 2)
    # tuple input — function should not assume list semantics
    out = pick_dm_roles_to_remove((a, b))
    assert out == [a]


# ── audit-line builders ──────────────────────────────────────────────


def test_audit_line_asked_uses_arrow_and_type():
    line = audit_line_asked("Alice", "Bob", "Direct Message")
    assert "Alice" in line
    assert "Bob" in line
    assert "➝" in line
    assert "Direct Message" in line


def test_audit_line_accepted_uses_double_arrow():
    """Accepted is a two-way relationship — uses ↔ not ➝."""
    line = audit_line_accepted("Alice", "Bob", "Friend Request")
    assert "↔" in line
    assert "Friend Request" in line
    assert "accepted" in line.lower()


def test_audit_line_denied_uses_one_way_arrow():
    line = audit_line_denied("Alice", "Bob", "Direct Message")
    assert "➝" in line
    assert "denied" in line.lower()


def test_audit_line_expired_uses_one_way_arrow():
    line = audit_line_expired("Alice", "Bob", "Direct Message")
    assert "➝" in line
    assert "expired" in line.lower()


def test_audit_line_revoked_includes_actor():
    line = audit_line_revoked("Alice", "Bob", "Carol")
    assert "↔" in line
    assert "Alice" in line
    assert "Bob" in line
    assert "Carol" in line
    assert "revoked" in line.lower()


def test_audit_line_revoked_when_actor_is_one_of_the_pair():
    """Self-revoke (common case) still surfaces the actor name explicitly."""
    line = audit_line_revoked("Alice", "Bob", "Alice")
    # Alice appears at least once — actor disambiguation may dedupe or not,
    # but at minimum the line must mention "(by Alice)".
    assert "by Alice" in line


# ── display_name_for ─────────────────────────────────────────────────


def test_display_name_for_uses_member_display_name():
    member = MagicMock(spec=discord.Member)
    member.display_name = "Cool Name"
    assert display_name_for(member, 42) == "Cool Name"


def test_display_name_for_falls_back_to_id_when_member_is_none():
    assert display_name_for(None, 42) == "42"


# ── discard_consent_pair ─────────────────────────────────────────────
#
# A connection is cached under whichever ordering created it, so revoke has to
# try both. Before this was extracted, the cog open-coded the two discards and
# the boolean; the panel's revoke button and any future caller now share it.


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param((1, 2), id="stored-as-requester-target"),
        pytest.param((2, 1), id="stored-as-target-requester"),
    ],
)
def test_discard_consent_pair_removes_either_ordering(stored):
    pairs = {stored}
    assert discard_consent_pair(pairs, 1, 2) is True
    assert pairs == set()


def test_discard_consent_pair_removes_both_orderings_when_duplicated():
    """A pair written under both orderings must not survive a revoke."""
    pairs = {(1, 2), (2, 1)}
    assert discard_consent_pair(pairs, 1, 2) is True
    assert pairs == set()


def test_discard_consent_pair_reports_absent_pair():
    """False is what makes the caller say "no connection" instead of "done"."""
    pairs = {(3, 4)}
    assert discard_consent_pair(pairs, 1, 2) is False
    assert pairs == {(3, 4)}


def test_discard_consent_pair_leaves_other_pairs_alone():
    pairs = {(1, 2), (3, 4), (5, 6)}
    assert discard_consent_pair(pairs, 3, 4) is True
    assert pairs == {(1, 2), (5, 6)}


def test_discard_consent_pair_handles_empty_cache():
    pairs: set[tuple[int, int]] = set()
    assert discard_consent_pair(pairs, 1, 2) is False
