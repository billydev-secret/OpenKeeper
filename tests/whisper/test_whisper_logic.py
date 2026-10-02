"""Tests for the whisper pure-logic module, ``bot_modules/whisper/logic.py``
(status pills, time formatting, fuzzy search, the button matrices). The
cards themselves are ``whisper/layouts.py``, tested in
``test_whisper_layouts.py``. Mirrors the starboard/jail pattern: the cog
file stays thin; this module proves the extracted helpers work without
spinning up Discord views.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bot_modules.whisper.models import (
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperConfig,
    WhisperState,
)
from bot_modules.whisper.logic import (
    check_send_cooldown,
    dm_action_buttons,
    filter_whispers_by_message,
    format_cooldown_message,
    format_hourly_cap_message,
    format_time_ago,
    fuzzy_score_members,
    guess_line,
    inbox_action_buttons,
    inbox_footer,
    member_picker_placeholder,
    preview,
    prune_recent_target_sends,
    recompute_inbox_after_delete,
    sender_feedback_wanted,
    status_pill,
)
from bot_modules.whisper.service import GuessOutcome


# ── Test fixtures ─────────────────────────────────────────────────────


def _whisper(
    *,
    id: int = 1,
    guild_id: int = 100,
    sender_id: int = 10,
    target_id: int = 20,
    message: str = "hello",
    created_at: float = 1_000.0,
    state: WhisperState = STATE_PENDING,
    solved: bool = False,
    exposed: bool = False,
    guesses_left: int = 3,
    channel_msg_id: int | None = None,
    dm_msg_id: int | None = None,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=id,
        guild_id=guild_id,
        sender_id=sender_id,
        target_id=target_id,
        message=message,
        created_at=created_at,
        state=state,
        solved=solved,
        exposed=exposed,
        guesses_left=guesses_left,
        channel_msg_id=channel_msg_id,
        dm_msg_id=dm_msg_id,
        deleted_at=deleted_at,
    )


@dataclass
class _FakeMember:
    """Minimal stand-in — only display_name is read by the helpers."""

    display_name: str


# ── format_time_ago ───────────────────────────────────────────────────


def test_format_time_ago_seconds():
    assert format_time_ago(1_000.0, now=1_030.0) == "30s ago"


def test_format_time_ago_minutes():
    assert format_time_ago(1_000.0, now=1_000.0 + 5 * 60) == "5m ago"


def test_format_time_ago_hours():
    assert format_time_ago(1_000.0, now=1_000.0 + 3 * 3600) == "3h ago"


def test_format_time_ago_one_day_singular():
    # Singular: 1 day → "1 day ago" (no plural s)
    assert format_time_ago(1_000.0, now=1_000.0 + 86400) == "1 day ago"


def test_format_time_ago_multiple_days_plural():
    assert format_time_ago(1_000.0, now=1_000.0 + 86400 * 4) == "4 days ago"


def test_format_time_ago_clamps_to_zero_for_future_inputs():
    # Negative delta should not produce negative output
    assert format_time_ago(2_000.0, now=1_000.0) == "0s ago"


# ── status_pill ───────────────────────────────────────────────────────


def test_status_pill_solved_beats_everything():
    # Solved outranks out-of-guesses: a solve consumed the last guess.
    w = _whisper(solved=True, guesses_left=0)
    assert status_pill(w, now=w.created_at + 1) == "Solved"


def test_status_pill_solved():
    w = _whisper(solved=True)
    assert status_pill(w, now=w.created_at + 1) == "Solved"


def test_status_pill_locked_after_30_days():
    w = _whisper()
    # 30 days + 1 second past creation
    assert status_pill(w, now=w.created_at + 30 * 86400 + 1) == "Locked"


def test_status_pill_no_guesses_when_exhausted_and_not_locked():
    w = _whisper(guesses_left=0)
    assert status_pill(w, now=w.created_at + 1) == "No guesses"


def test_status_pill_shared_for_pending_shared_state():
    w = _whisper(state=STATE_SHARED)
    assert status_pill(w, now=w.created_at + 1) == "Shared"


def test_status_pill_new_for_pending_fresh_whisper():
    w = _whisper(state=STATE_PENDING)
    assert status_pill(w, now=w.created_at + 1) == "New"


# ── preview ───────────────────────────────────────────────────────────


def test_preview_short_text_unchanged():
    assert preview("hi there") == "hi there"


def test_preview_collapses_newlines_to_spaces():
    assert preview("line1\nline2") == "line1 line2"


def test_preview_truncates_with_ellipsis():
    long = "a" * 100
    out = preview(long, n=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_preview_respects_custom_n():
    out = preview("0123456789abcdef", n=5)
    assert out == "0123…"


# ── fuzzy_score_members ────────────────────────────────────────────────


def test_fuzzy_exact_match_ranks_first():
    members = [
        _FakeMember(display_name="alice"),
        _FakeMember(display_name="bob"),
        _FakeMember(display_name="alicia"),
    ]
    result = fuzzy_score_members(members, "alice")
    assert result[0].display_name == "alice"


def test_fuzzy_prefix_beats_substring():
    members = [
        _FakeMember(display_name="malice"),  # substring
        _FakeMember(display_name="alibaba"),  # prefix
    ]
    result = fuzzy_score_members(members, "ali")
    assert [m.display_name for m in result] == ["alibaba", "malice"]


def test_fuzzy_case_insensitive():
    members = [_FakeMember(display_name="ALICE")]
    assert len(fuzzy_score_members(members, "alice")) == 1


def test_fuzzy_filters_out_non_matches():
    members = [
        _FakeMember(display_name="alice"),
        _FakeMember(display_name="zzz"),
    ]
    result = fuzzy_score_members(members, "alice")
    assert [m.display_name for m in result] == ["alice"]


def test_fuzzy_subsequence_match():
    # 'abc' is a subsequence of 'aXbXc'
    members = [_FakeMember(display_name="aXbXc")]
    result = fuzzy_score_members(members, "abc")
    assert len(result) == 1


def test_fuzzy_empty_query_matches_all_via_subsequence():
    # Empty query has all chars trivially in any name — score 1 for everyone
    members = [_FakeMember(display_name="alice"), _FakeMember(display_name="bob")]
    result = fuzzy_score_members(members, "")
    assert len(result) == 2


# ── filter_whispers_by_message ────────────────────────────────────────


def test_filter_whispers_substring_match():
    ws = [_whisper(id=1, message="hello world"), _whisper(id=2, message="goodbye")]
    out = filter_whispers_by_message(ws, "hello")
    assert [w.id for w in out] == [1]


def test_filter_whispers_case_insensitive():
    ws = [_whisper(id=1, message="HELLO")]
    assert filter_whispers_by_message(ws, "hello") == ws


def test_filter_whispers_returns_fresh_list():
    ws = [_whisper(id=1, message="hi")]
    out = filter_whispers_by_message(ws, "hi")
    assert out is not ws


def test_filter_whispers_no_match_empty():
    ws = [_whisper(id=1, message="hi")]
    assert filter_whispers_by_message(ws, "zzz") == []


# ── sender feedback (2026-09 review, rotation-rooms-159) ───────────────
#
# The sender used to hear nothing after sending: no signal on a wrong guess,
# on being unmasked, or on the target running out of guesses. Each guess now
# DMs them one line. Copy is pinned here so the three outcomes can't drift.
# The whisper's number is the digest card's heading, not part of the line.


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            GuessOutcome(correct=False, attempts_remaining=2, exhausted=False),
            "They guessed Alice. Wrong, 2 left.",
            id="wrong-plural",
        ),
        pytest.param(
            GuessOutcome(correct=False, attempts_remaining=1, exhausted=False),
            "They guessed Alice. Wrong, 1 left.",
            id="wrong-singular",
        ),
        pytest.param(
            GuessOutcome(correct=False, attempts_remaining=0, exhausted=True),
            "They guessed Alice. Wrong — they're out of guesses. You're safe.",
            id="exhausted",
        ),
        pytest.param(
            GuessOutcome(correct=True, attempts_remaining=2, exhausted=False),
            "They guessed you. They got you.",
            id="correct-never-echoes-the-label",
        ),
    ],
)
def test_guess_line(outcome, expected):
    assert guess_line("Alice", outcome) == expected


@pytest.mark.parametrize(
    ("dial", "sender_roles", "expected"),
    [
        pytest.param(True, {7001, 5}, True, id="on-and-in-pool"),
        pytest.param(False, {7001}, False, id="dial-off-ships-dark"),
        pytest.param(True, {5}, False, id="sender-opted-out"),
        pytest.param(True, set(), False, id="sender-left"),
    ],
)
def test_sender_feedback_wanted(dial, sender_roles, expected):
    """The dial ships OFF (prod whispers in flight must not start DMing until
    an admin flips it), and a sender who has since opted out of the pool has
    left the game — they don't get its DMs."""
    cfg = WhisperConfig(
        guild_id=9001, role_id=7001, channel_id=8001, sender_feedback=dial,
    )
    assert sender_feedback_wanted(cfg, sender_role_ids=sender_roles) is expected


# ── inbox_footer ──────────────────────────────────────────────────────


def test_inbox_footer_locked_first():
    w = _whisper()
    # well past lock duration
    out = inbox_footer(w, mode="received", now=w.created_at + 100 * 86400)
    assert "Locked" in out


def test_inbox_footer_solved():
    w = _whisper(solved=True)
    assert inbox_footer(w, mode="received", now=w.created_at + 1) == "Solved."


def test_inbox_footer_out_of_guesses():
    w = _whisper(guesses_left=0)
    out = inbox_footer(w, mode="received", now=w.created_at + 1)
    assert "Out of guesses" in out


def test_inbox_footer_received_mode_shows_left():
    w = _whisper(guesses_left=2)
    assert (
        inbox_footer(w, mode="received", now=w.created_at + 1)
        == "2 guesses left."
    )


def test_inbox_footer_sent_mode_says_for_the_target():
    w = _whisper(guesses_left=2)
    assert (
        inbox_footer(w, mode="sent", now=w.created_at + 1)
        == "2 guesses remain for the target."
    )


# ── check_send_cooldown ───────────────────────────────────────────────


def test_cooldown_none_when_no_prior_send():
    assert check_send_cooldown(None, now=100, cooldown_seconds=30) is None


def test_cooldown_zero_last_send_treated_as_none():
    # The cog used a ``.get(uid, 0)`` default; verify 0 is permissive.
    assert check_send_cooldown(0, now=100, cooldown_seconds=30) is None


def test_cooldown_returns_seconds_remaining():
    out = check_send_cooldown(100, now=110, cooldown_seconds=30)
    assert out == 20


def test_cooldown_exactly_at_boundary_clears():
    # 30s after a 30s cooldown started — boundary is the moment it clears.
    assert check_send_cooldown(100, now=130, cooldown_seconds=30) is None


def test_cooldown_returns_at_least_one_second_to_avoid_zero():
    # If we're 29.5 s in, int(0.5) would be 0; the helper bumps to 1 so the
    # user never sees "wait 0s".
    out = check_send_cooldown(100, now=129.5, cooldown_seconds=30)
    assert out == 1


# ── prune_recent_target_sends ─────────────────────────────────────────


def test_prune_keeps_recent_drops_old():
    # window=3600. now=3800. t=100→3700 ago (drop), t=300→3500 ago (keep),
    # t=3700→100 ago (keep).
    ts = [100.0, 300.0, 3700.0]
    out = prune_recent_target_sends(ts, now=3800.0)
    assert out == [300.0, 3700.0]


def test_prune_empty_input():
    assert prune_recent_target_sends([], now=1000.0) == []


def test_prune_custom_window():
    ts = [100.0, 250.0]
    out = prune_recent_target_sends(ts, now=300.0, window_seconds=60)
    assert out == [250.0]


def test_prune_boundary_dropped():
    # Exactly window_seconds old → dropped (strict <)
    ts = [100.0]
    assert prune_recent_target_sends(ts, now=160.0, window_seconds=60) == []


# ── format_cooldown / hourly_cap messages ─────────────────────────────


def test_cooldown_message_includes_seconds():
    assert "20s" in format_cooldown_message(20)


def test_hourly_cap_message_includes_cap():
    out = format_hourly_cap_message(5)
    assert "5 whispers" in out


# ── member_picker_placeholder ──────────────────────────────────────────


def test_member_picker_base_text_used_when_no_filter_single_page():
    out = member_picker_placeholder(
        filter_query="", display_count=5, page=0, page_count=1,
        base="Pick the sender…",
    )
    assert out == "Pick the sender…"


def test_member_picker_no_filter_multi_page_includes_index():
    out = member_picker_placeholder(
        filter_query="", display_count=30, page=0, page_count=2,
        base="Pick recipient…",
    )
    assert out == "Pick recipient… (1/2)"


def test_member_picker_filter_plural():
    out = member_picker_placeholder(
        filter_query="a", display_count=3, page=0, page_count=1,
        base="Pick the sender…",
    )
    assert "3 matches" in out


def test_member_picker_filter_singular():
    out = member_picker_placeholder(
        filter_query="a", display_count=1, page=0, page_count=1,
        base="Pick the sender…",
    )
    assert "1 match" in out and "1 matches" not in out


def test_member_picker_filter_multi_page_includes_index():
    out = member_picker_placeholder(
        filter_query="a", display_count=30, page=1, page_count=2,
        base="Pick recipient…",
    )
    assert "(2/2)" in out


# ── inbox_action_buttons ───────────────────────────────────────────────


def test_inbox_actions_received_fresh_whisper():
    w = _whisper(state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert actions == ["guess", "share", "reply", "report", "delete"]


def test_inbox_actions_received_solved_no_guess_button():
    w = _whisper(solved=True, state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert "guess" not in actions
    # still pending, so share is offered
    assert "share" in actions


def test_inbox_actions_received_no_guesses_left():
    w = _whisper(guesses_left=0)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert "guess" not in actions


def test_inbox_actions_received_locked_no_guess_button():
    w = _whisper()
    actions = inbox_action_buttons(
        w, mode="received", now=w.created_at + 100 * 86400,
    )
    assert "guess" not in actions


def test_inbox_actions_received_shared_no_share_button():
    w = _whisper(state=STATE_SHARED)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert "share" not in actions
    # reply / report / delete remain
    assert {"reply", "report", "delete"}.issubset(set(actions))


def test_inbox_actions_sent_mode_is_reply_only():
    """Only the recipient may delete, so Sent must not offer a Delete whose
    press can only refuse."""
    w = _whisper(state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="sent", now=w.created_at + 1)
    assert actions == ["reply"]


# ── dm_action_buttons ─────────────────────────────────────────────────
#
# The DM redraws from this after every press; it replaces the embed era's
# three view constructors (all four / without Guess / without Share+Delete).


@pytest.mark.parametrize(
    ("whisper", "expected"),
    [
        pytest.param(_whisper(), ["guess", "share", "reply", "delete"], id="fresh"),
        pytest.param(_whisper(solved=True), ["share", "reply", "delete"], id="solved-drops-guess"),
        pytest.param(_whisper(guesses_left=0), ["share", "reply", "delete"], id="exhausted-drops-guess"),
        pytest.param(_whisper(state=STATE_SHARED), ["guess", "reply"], id="shared-drops-share-and-delete"),
        pytest.param(_whisper(state=STATE_SHARED, solved=True), ["reply"], id="shared-and-solved"),
        pytest.param(_whisper(deleted_at=5.0), [], id="deleted-has-no-buttons"),
    ],
)
def test_dm_action_buttons(whisper, expected):
    assert dm_action_buttons(whisper, now=1_001.0) == expected


def test_dm_action_buttons_locked_drops_guess():
    w = _whisper(created_at=0.0)
    assert dm_action_buttons(w, now=31 * 86400.0) == ["share", "reply", "delete"]


# ── recompute_inbox_after_delete ───────────────────────────────────────


def test_delete_clears_state_when_inbox_emptied():
    w = _whisper(id=1)
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=[w],
        display_whispers=[w],
        deleted_id=1,
        page=0,
        page_size=25,
    )
    assert new_all == []
    assert new_display == []
    assert page == 0
    assert sel is None


def test_delete_keeps_page_and_picks_next_row():
    ws = [_whisper(id=i) for i in range(1, 4)]
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=ws,
        display_whispers=ws,
        deleted_id=2,
        page=0,
        page_size=25,
    )
    assert [w.id for w in new_all] == [1, 3]
    assert page == 0
    # After deleting #2, first row of (clamped) page is #1
    assert sel == 1


def test_delete_clamps_to_previous_page_when_last_row_on_page_gone():
    # Page size 2: page 0 = [1,2], page 1 = [3]. Delete #3 while on page 1.
    ws = [_whisper(id=i) for i in range(1, 4)]
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=ws,
        display_whispers=ws,
        deleted_id=3,
        page=1,
        page_size=2,
    )
    assert [w.id for w in new_display] == [1, 2]
    assert page == 0
    assert sel == 1


def test_delete_respects_display_subset_when_filter_active():
    all_ws = [_whisper(id=i) for i in range(1, 4)]
    # Filter shows only #2 and #3
    display = [all_ws[1], all_ws[2]]
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=all_ws,
        display_whispers=display,
        deleted_id=2,
        page=0,
        page_size=25,
    )
    assert [w.id for w in new_all] == [1, 3]
    assert [w.id for w in new_display] == [3]
    assert sel == 3


def test_inbox_actions_received_delete_is_last():
    w = _whisper(state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert actions[-1] == "delete"


def test_insert_whisper_seeds_the_configured_number_of_guesses(tmp_path):
    from bot_modules.core.db_utils import open_db
    from bot_modules.whisper.repo import (
        get_whisper,
        get_whisper_config,
        insert_whisper,
        set_whisper_config_value,
    )
    from tests.db_template import migrated_db

    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        # Default first: unchanged from the schema's three.
        assert get_whisper_config(conn, 111).guesses_per_whisper == 3
        wid = insert_whisper(
            conn, guild_id=111, sender_id=1, target_id=2, message="m",
        )
        assert get_whisper(conn, wid).guesses_left == 3

        set_whisper_config_value(conn, 111, "whisper_guesses_per_whisper", "5")
        cfg = get_whisper_config(conn, 111)
        assert cfg.guesses_per_whisper == 5
        wid2 = insert_whisper(
            conn, guild_id=111, sender_id=1, target_id=2, message="m",
            guesses_left=cfg.guesses_per_whisper,
        )
        assert get_whisper(conn, wid2).guesses_left == 5


# ── batched guess notes (dm_batch) ───────────────────────────────────


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(GuessOutcome(correct=True, attempts_remaining=2, exhausted=False), id="correct"),
        pytest.param(GuessOutcome(correct=False, attempts_remaining=1, exhausted=False), id="wrong"),
        pytest.param(GuessOutcome(correct=False, attempts_remaining=0, exhausted=True), id="exhausted"),
    ],
)
def test_guess_payload_round_trips(outcome):
    from bot_modules.whisper.logic import guess_payload, outcome_from_payload

    assert outcome_from_payload(guess_payload(outcome)) == outcome


