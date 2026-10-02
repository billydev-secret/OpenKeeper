"""Tests for the extracted Anonymous AMA pure-logic module and its cog.

Covers ``bot_modules/games/ama/logic.py`` (ISO timestamps, question
status transitions, retention, recap stats, bottom-bar formatting) and the
cog glue that needs no Discord. The Components V2 cards are covered in
``test_games_ama_layouts.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.games.ama.logic import (
    AMA_FORMAT_HOT_SEAT,
    AMA_FORMAT_PANEL,
    RESOLVED_QUESTION_STATUSES,
    UNANSWERED_QUESTION_RETENTION,
    add_question,
    bottom_bar_label,
    bottom_bar_text,
    build_question_entry,
    compute_recap_stats,
    hot_seat_seconds_remaining,
    is_panel_target,
    is_resolved_status,
    mark_question_answered,
    mark_question_approved,
    mark_question_expired,
    mark_question_failed,
    mark_question_passed,
    mark_question_pending_dm,
    mark_question_rejected,
    mode_label,
    normalize_format,
    open_question_cards,
    panel_bottom_bar_label,
    parse_iso_ts,
    question_count,
    pending_screened_dms,
    recompute_totals,
    remaining_questions_text,
    resolve_questions_per_turn,
    should_expire,
    toggle_panel_member,
    unique_asker_count,
    utcnow_iso,
)


# ── utcnow_iso / parse_iso_ts ────────────────────────────────────────


def test_utcnow_iso_with_injected_clock_is_deterministic():
    fixed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert utcnow_iso(fixed) == "2024-01-02T03:04:05+00:00"


def test_utcnow_iso_default_round_trips_through_parse():
    s = utcnow_iso()
    parsed = parse_iso_ts(s)
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_iso_ts_handles_none_and_empty():
    assert parse_iso_ts(None) is None
    assert parse_iso_ts("") is None


def test_parse_iso_ts_handles_unparseable():
    assert parse_iso_ts("not a date") is None


def test_parse_iso_ts_coerces_naive_to_utc():
    naive = "2024-05-01T12:00:00"
    parsed = parse_iso_ts(naive)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_parse_iso_ts_converts_offset_to_utc():
    parsed = parse_iso_ts("2024-05-01T12:00:00+05:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.hour == 7  # 12 - 5


# ── is_resolved_status ───────────────────────────────────────────────


@pytest.mark.parametrize("status", ["answered", "passed", "rejected", "expired"])
def test_is_resolved_status_recognizes_terminal_states(status):
    assert is_resolved_status(status) is True


@pytest.mark.parametrize("status", ["pending", "approved", "", None, "unknown"])
def test_is_resolved_status_rejects_non_terminal(status):
    assert is_resolved_status(status) is False


def test_is_resolved_status_is_case_insensitive():
    assert is_resolved_status("ANSWERED") is True
    assert is_resolved_status("Passed") is True


def test_resolved_question_statuses_constant_matches_set():
    assert RESOLVED_QUESTION_STATUSES == {
        "answered", "passed", "rejected", "expired", "failed",
    }


# ── should_expire ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("asked_delta", "retention", "expected"),
    [
        pytest.param(None, None, False, id="asked-at-none"),
        pytest.param(timedelta(days=8), None, True, id="after-default-retention"),
        pytest.param(timedelta(days=6), None, False, id="within-default-retention"),
        pytest.param(
            timedelta(hours=2), timedelta(hours=1), True, id="custom-retention-expired"
        ),
        pytest.param(
            timedelta(hours=2),
            timedelta(hours=3),
            False,
            id="custom-retention-not-expired",
        ),
        # The cog used ``(now - asked) > retention`` (strict greater);
        # equal should NOT expire.
        pytest.param(
            UNANSWERED_QUESTION_RETENTION,
            None,
            False,
            id="exact-boundary-not-expired",
        ),
    ],
)
def test_should_expire(
    asked_delta: timedelta | None, retention: timedelta | None, expected: bool
):
    """``asked_delta=None`` means asked_at is None (never asked)."""
    now = datetime(2024, 5, 10, tzinfo=timezone.utc)
    asked = None if asked_delta is None else now - asked_delta
    kwargs = {} if retention is None else {"retention": retention}
    assert should_expire(asked, now, **kwargs) is expected


# ── build_question_entry / add_question ──────────────────────────────


def test_build_question_entry_minimum_fields():
    entry = build_question_entry(
        asker_id=42, text="hi", hot_seat_id=99, now_iso="2024-01-01T00:00:00+00:00"
    )
    assert entry == {
        "asker_id": 42,
        "text": "hi",
        "status": "pending",
        "asked_at": "2024-01-01T00:00:00+00:00",
        "hot_seat_id": 99,
    }


def test_build_question_entry_takes_an_explicit_status():
    entry = build_question_entry(
        asker_id=4, text="x", hot_seat_id=99, status="approved",
        now_iso="2024-01-01T00:00:00+00:00",
    )
    assert entry["status"] == "approved"


def test_build_question_entry_default_timestamp_uses_now():
    entry = build_question_entry(asker_id=1, text="x", hot_seat_id=2)
    # Round-trips through parse_iso_ts as a valid timestamp
    assert parse_iso_ts(entry["asked_at"]) is not None


def test_build_question_entry_has_no_source_tag():
    """social-prompt-48: the retired AI idle-question path tagged entries
    with ``source``; a player question never carries one."""
    entry = build_question_entry(
        asker_id=1, text="x", hot_seat_id=2, now_iso="2024-01-01T00:00:00+00:00"
    )
    assert "source" not in entry
    import bot_modules.games.ama.logic as ama_logic

    assert not hasattr(ama_logic, "first_content_line")
    assert not hasattr(ama_logic, "mark_question_message")


def test_add_question_appends_and_returns_index():
    payload: dict = {}
    e1 = build_question_entry(1, "q1", 99, now_iso="t1")
    idx1 = add_question(payload, e1)
    assert idx1 == 0
    assert payload["questions"][0] is e1
    assert payload["total_questions"] == 1

    e2 = build_question_entry(2, "q2", 99, now_iso="t2")
    idx2 = add_question(payload, e2)
    assert idx2 == 1
    assert payload["total_questions"] == 2


# ── mark_question_approved ───────────────────────────────────────────


def test_mark_question_approved_sets_status_and_message_id():
    payload = {
        "questions": [
            {
                "asker_id": 1,
                "text": "q",
                "status": "pending",
                "asked_at": "t",
                "hot_seat_id": 99,
            }
        ]
    }
    mark_question_approved(payload, 0, message_id=12345)
    q = payload["questions"][0]
    assert q["status"] == "approved"
    assert q["question_message_id"] == 12345
    # asked_at and hot_seat_id untouched
    assert q["asked_at"] == "t"
    assert q["hot_seat_id"] == 99


def test_mark_question_approved_setdefaults_asked_at_and_hot_seat():
    """Screened-approve path preserves existing asked_at / hot_seat_id."""
    payload = {
        "questions": [
            {
                "asker_id": 1,
                "text": "q",
                "status": "pending",
                "asked_at": "original",
                "hot_seat_id": 11,
            }
        ]
    }
    mark_question_approved(
        payload, 0, message_id=1, hot_seat_id=999, now_iso="new"
    )
    q = payload["questions"][0]
    assert q["asked_at"] == "original"  # setdefault left it alone
    assert q["hot_seat_id"] == 11  # setdefault left it alone


def test_mark_question_approved_fills_missing_asked_at_and_hot_seat():
    payload = {"questions": [{"asker_id": 1, "text": "q", "status": "pending"}]}
    mark_question_approved(
        payload, 0, message_id=42, hot_seat_id=999, now_iso="new"
    )
    q = payload["questions"][0]
    assert q["asked_at"] == "new"
    assert q["hot_seat_id"] == 999
    assert q["question_message_id"] == 42


def test_mark_question_approved_out_of_range_is_noop():
    payload = {"questions": []}
    mark_question_approved(payload, 0, message_id=42)
    assert payload["questions"] == []





def test_mark_question_answered_sets_status_and_timestamp():
    payload = {
        "questions": [
            {"status": "approved"},
            {"status": "approved"},
        ]
    }
    mark_question_answered(payload, 1, message_id=42, now_iso="2024-05-01")
    q = payload["questions"][1]
    assert q["status"] == "answered"
    assert q["answered_at"] == "2024-05-01"
    assert q["question_message_id"] == 42
    assert payload["total_answered"] == 1


def test_mark_question_answered_setdefaults_message_id():
    payload = {
        "questions": [
            {"status": "approved", "question_message_id": 999}
        ]
    }
    mark_question_answered(payload, 0, message_id=42, now_iso="t")
    # setdefault preserves existing
    assert payload["questions"][0]["question_message_id"] == 999


def test_mark_question_answered_recomputes_total_across_questions():
    payload = {
        "questions": [
            {"status": "answered"},
            {"status": "approved"},
            {"status": "answered"},
            {"status": "passed"},
        ]
    }
    mark_question_answered(payload, 1, now_iso="t")
    assert payload["total_answered"] == 3


def test_mark_question_answered_out_of_range_still_recomputes_totals():
    payload = {"questions": [{"status": "answered"}]}
    mark_question_answered(payload, 99, now_iso="t")
    # Out of range: status not flipped, but totals refreshed
    assert payload["total_answered"] == 1


# ── mark_question_passed ─────────────────────────────────────────────


def test_mark_question_passed_sets_fields_and_increments_total():
    payload = {"questions": [{"status": "approved"}], "total_passed": 0}
    mark_question_passed(payload, 0, message_id=1, now_iso="t")
    q = payload["questions"][0]
    assert q["status"] == "passed"
    assert q["passed_at"] == "t"
    assert q["question_message_id"] == 1
    assert payload["total_passed"] == 1


def test_mark_question_passed_increments_existing_total():
    payload = {"questions": [{"status": "approved"}], "total_passed": 4}
    mark_question_passed(payload, 0, now_iso="t")
    assert payload["total_passed"] == 5


def test_mark_question_passed_increments_when_total_missing():
    payload = {"questions": [{"status": "approved"}]}
    mark_question_passed(payload, 0, now_iso="t")
    assert payload["total_passed"] == 1


def test_mark_question_passed_setdefaults_message_id():
    payload = {
        "questions": [{"status": "approved", "question_message_id": 999}]
    }
    mark_question_passed(payload, 0, message_id=1, now_iso="t")
    assert payload["questions"][0]["question_message_id"] == 999


# ── mark_question_rejected ───────────────────────────────────────────


def test_mark_question_rejected_only_sets_status():
    payload = {"questions": [{"asker_id": 1, "text": "q", "status": "pending"}]}
    mark_question_rejected(payload, 0)
    q = payload["questions"][0]
    assert q["status"] == "rejected"
    # No timestamp or message id added
    assert "rejected_at" not in q
    assert "question_message_id" not in q


def test_mark_question_rejected_out_of_range_is_noop():
    payload = {"questions": []}
    mark_question_rejected(payload, 0)
    assert payload["questions"] == []


# ── mark_question_expired ────────────────────────────────────────────


def test_mark_question_expired_sets_status_and_timestamp():
    q = {"status": "approved"}
    mark_question_expired(q, now_iso="2024-05-01")
    assert q["status"] == "expired"
    assert q["expired_at"] == "2024-05-01"


def test_mark_question_expired_default_timestamp():
    q = {"status": "approved"}
    mark_question_expired(q)
    assert q["status"] == "expired"
    assert parse_iso_ts(q["expired_at"]) is not None


# ── recompute_totals ─────────────────────────────────────────────────


def test_recompute_totals_counts_only_answered():
    payload = {
        "questions": [
            {"status": "answered"},
            {"status": "approved"},
            {"status": "answered"},
            {"status": "passed"},
            {"status": "rejected"},
        ]
    }
    recompute_totals(payload)
    assert payload["total_answered"] == 2


def test_recompute_totals_empty_payload():
    payload: dict = {}
    recompute_totals(payload)
    assert payload["total_answered"] == 0


def test_recompute_totals_no_answered():
    payload = {"questions": [{"status": "passed"}]}
    recompute_totals(payload)
    assert payload["total_answered"] == 0


# ── unique_asker_count / compute_recap_stats ─────────────────────────


def test_unique_asker_count_excludes_a_zero_id():
    qs = [
        {"asker_id": 0},  # AI
        {"asker_id": 1},
        {"asker_id": 1},  # dup
        {"asker_id": 2},
    ]
    assert unique_asker_count(qs) == 2


def test_unique_asker_count_handles_missing_asker_id():
    qs = [{"text": "no asker"}, {"asker_id": 5}]
    assert unique_asker_count(qs) == 1


def test_compute_recap_stats_pulls_all_fields():
    payload = {
        "questions": [
            {"asker_id": 1, "status": "answered"},
            {"asker_id": 2, "status": "passed"},
            {"asker_id": 0, "status": "answered"},
        ],
        "total_answered": 2,
        "total_passed": 1,
        "hot_seat_rotations": 3,
    }
    stats = compute_recap_stats(payload)
    assert stats == {
        "total_q": 3,
        "total_answered": 2,
        "total_passed": 1,
        "rotations": 3,
        "unique_askers": 2,
    }


def test_compute_recap_stats_empty_payload():
    stats = compute_recap_stats({})
    assert stats == {
        "total_q": 0,
        "total_answered": 0,
        "total_passed": 0,
        "rotations": 0,
        "unique_askers": 0,
    }


# ── bottom_bar_label ─────────────────────────────────────────────────


def test_bottom_bar_label_no_hot_seat_no_queue():
    assert bottom_bar_label(None, 0) == "🎙️ AMA"


def test_bottom_bar_label_with_hot_seat_no_queue():
    assert bottom_bar_label("Alice", 0) == "🎙️ AMA: @Alice"


def test_bottom_bar_label_with_hot_seat_and_queue():
    label = bottom_bar_label("Alice", 3)
    assert label == "🎙️ AMA: @Alice  •  📋 3 in queue"


def test_bottom_bar_label_no_hot_seat_ignores_queue():
    """When there's no hot seat the bar should be the bare label even
    if the queue is populated (matches the cog's existing precedence)."""
    assert bottom_bar_label(None, 5) == "🎙️ AMA"


def test_bottom_bar_label_escapes_hot_seat_markdown():
    """The bar is a message ``content=`` send, so a hot-seat display name of
    ``**Moderator**`` must not render bold — same escaping build_main_embed
    already applies on the embed side (sibling asymmetry, misfix sweep)."""
    label = bottom_bar_label("**Moderator**", 0)
    assert label == "🎙️ AMA: @\\*\\*Moderator\\*\\*"
    assert "**Moderator**" not in label


# ── bottom_bar_text ──────────────────────────────────────────────────


def test_bottom_bar_text_picks_the_line_for_the_format():
    """One dispatcher for the bar's line, now that one ``build`` draws it."""
    assert bottom_bar_text(
        AMA_FORMAT_HOT_SEAT, hot_seat_name="Alice", queue_len=3
    ) == bottom_bar_label("Alice", 3)
    assert bottom_bar_text(
        AMA_FORMAT_PANEL, panel_len=2
    ) == panel_bottom_bar_label(2)


def test_bottom_bar_text_starts_a_hot_seat_game_the_way_launch_used_to():
    """Launch hard-coded "🎙️ AMA" as the bar's first line. Keeping that
    string right was never anything but luck — it is what
    ``bottom_bar_label(None, 0)`` returns — so this pins the equivalence
    the migration relies on rather than the literal."""
    assert bottom_bar_text(AMA_FORMAT_HOT_SEAT) == "🎙️ AMA"


def test_bottom_bar_text_defaults_an_unknown_format_to_hot_seat():
    """``normalize_format`` decides, so a payload written before the panel
    format existed still renders a bar rather than a panel count of zero."""
    assert bottom_bar_text("", hot_seat_name="Alice") == bottom_bar_label("Alice", 0)


# ── remaining_questions_text ─────────────────────────────────────────


@pytest.mark.parametrize(
    "asked,expected",
    [
        (0, "**4** questions left this turn."),
        (1, "**3** questions left this turn."),
        (2, "**2** questions left this turn."),
        (3, "**1** question left this turn."),
        (4, "**0** questions left this turn."),
    ],
)
def test_remaining_questions_text_pluralisation(asked, expected):
    assert remaining_questions_text(asked) == expected


def test_remaining_questions_text_never_negative():
    # A seat that answered more than the dial asked (the dial was lowered
    # mid-game, or a leftover card from an earlier seat) shows 0, not -1.
    assert remaining_questions_text(5, per_turn=4) == "**0** questions left this turn."


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, 4, id="unset-is-default"),
        pytest.param("", 4, id="blank-is-default"),
        pytest.param("abc", 4, id="garbage-is-default"),
        pytest.param(0, 1, id="zero-clamps-to-one"),
        pytest.param(6, 6, id="in-range"),
        pytest.param("7", 7, id="string-number"),
        pytest.param(999, 20, id="ceiling"),
    ],
)
def test_resolve_questions_per_turn(value, expected):
    assert resolve_questions_per_turn(value) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param("unfiltered", "Unfiltered", id="unfiltered"),
        pytest.param("screened", "Screened", id="screened"),
        pytest.param("SCREENED", "Screened", id="upper-enum"),
        pytest.param(None, "Unfiltered", id="missing"),
    ],
)
def test_mode_label_is_title_case(mode, expected):
    assert mode_label(mode) == expected


# ── hot-seat timer re-arm (social-prompt-42) ─────────────────────────


@pytest.mark.parametrize(
    ("started_ago_s", "expected"),
    [
        pytest.param(None, 3600, id="unknown-start-gets-the-full-hour"),
        pytest.param(600, 3000, id="ten-minutes-in"),
        pytest.param(3600, 5, id="hour-elapsed-rotates-promptly"),
        pytest.param(90000, 5, id="long-overdue-still-floors"),
        pytest.param(-120, 3600, id="clock-skew-caps-at-the-hour"),
    ],
)
def test_hot_seat_seconds_remaining(started_ago_s, expected):
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    started = None if started_ago_s is None else (now - timedelta(seconds=started_ago_s)).isoformat()
    assert hot_seat_seconds_remaining(started, now) == expected


# ── the live buttons a close or a restart must find again ────────────


def test_open_question_cards_lists_unresolved_posted_questions():
    qs = [
        {"status": "approved", "question_message_id": 11},
        {"status": "answered", "question_message_id": 12},
        {"status": "pending"},                       # screened, never posted
        {"status": "approved", "message_id": 14},     # legacy key
        {"status": "passed", "question_message_id": 15},
    ]
    assert open_question_cards(qs) == [(0, 11), (3, 14)]


def test_pending_screened_dms_lists_only_pending_with_a_dm_id():
    payload = {"questions": [
        {"status": "pending"},
        {"status": "pending"},
        {"status": "approved"},
    ]}
    mark_question_pending_dm(payload, 1, 555)
    mark_question_pending_dm(payload, 2, 556)  # approved already — ignored
    mark_question_pending_dm(payload, 9, 557)  # out of range — no-op
    assert payload["questions"][1]["host_dm_message_id"] == 555
    assert pending_screened_dms(payload["questions"]) == [(1, 555)]


def test_remaining_questions_text_custom_per_turn():
    assert remaining_questions_text(0, per_turn=2) == "**2** questions left this turn."


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.games.ama.cog as ama_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.games.db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_do_close_pays_askers_and_hot_seats(monkeypatch, sync_db_path):
    """Participants = human askers (AI sentinel 0 excluded) plus the hot-seat
    occupants who answered."""
    spy = AsyncMock()
    monkeypatch.setattr(ama_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {"questions": [
        {"asker_id": 5, "hot_seat_id": 9},
        {"asker_id": 0, "hot_seat_id": 9},   # AI-asked, answered by hot seat 9
        {"asker_id": 7, "hot_seat_id": 5},   # asker 5 later took the hot seat
        {"asker_id": 5, "hot_seat_id": 9},
    ]}
    gid = await create_game(bot.games_db, 100, 1, "ama", payload=payload)
    view = ama_cog.AMAView(gid, 1, "public", bot.games_db, bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())
    await view._do_close(channel)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [5, 7, 9]  # askers {5,7} ∪ hot seats {9,5}
    assert call.kwargs["bot"] is bot
    # social-prompt-40: the recorded count is the roster the recap path pays,
    # so it agrees with what the 24h sweep (game_roster._ama) would record —
    # it used to be the asker count alone (2 here) on this path and 3 on the
    # sweep's, for the same session.
    assert call.kwargs["player_count"] == 3


# ── normalize_format ─────────────────────────────────────────────────


def test_normalize_format_recognizes_panel():
    assert normalize_format(AMA_FORMAT_PANEL) == AMA_FORMAT_PANEL


@pytest.mark.parametrize("value", [None, "", "hot_seat", "bogus", "HOT_SEAT", 0])
def test_normalize_format_defaults_to_hot_seat(value):
    # Old payloads have no format key and bad input shouldn't panic —
    # everything that isn't the explicit panel sentinel is hot seat.
    assert normalize_format(value) == AMA_FORMAT_HOT_SEAT


# ── toggle_panel_member / is_panel_target ────────────────────────────


def test_toggle_panel_member_adds_then_removes_preserving_order():
    panel: list[int] = []
    assert toggle_panel_member(panel, 10) is True   # joined
    assert toggle_panel_member(panel, 20) is True
    assert panel == [10, 20]                          # join order preserved
    assert toggle_panel_member(panel, 10) is False  # left
    assert panel == [20]


def test_is_panel_target_reflects_membership():
    panel = [1, 2, 3]
    assert is_panel_target(panel, 2) is True
    assert is_panel_target(panel, 99) is False
    assert is_panel_target([], 2) is False


# ── panel_bottom_bar_label ───────────────────────────────────────────


def test_panel_bottom_bar_label_empty_panel():
    assert panel_bottom_bar_label(0) == "🎙️ AMA Panel"


def test_panel_bottom_bar_label_singular_vs_plural():
    assert "1 answering question" in panel_bottom_bar_label(1)
    assert panel_bottom_bar_label(1).rstrip().endswith("question")
    assert "3 answering questions" in panel_bottom_bar_label(3)


# ── a question whose card never reached the channel ──────────────────────────

def test_mark_question_failed_does_not_shift_the_other_indices():
    """The reason a failed question is marked and not removed.

    Every live question card carries its own index in its buttons. Deleting
    entry 1 would silently re-point entry 2's card at entry 1's text — so the
    list must never shrink, only gain terminal states.
    """
    payload: dict = {}
    for text in ("first", "second", "third"):
        add_question(payload, build_question_entry(asker_id=7, text=text, hot_seat_id=9))

    mark_question_failed(payload, 1)

    assert [q["text"] for q in payload["questions"]] == ["first", "second", "third"]
    assert payload["questions"][1]["status"] == "failed"
    assert payload["questions"][0]["status"] == "pending"
    assert payload["questions"][2]["status"] == "pending"


def test_a_failed_question_is_terminal():
    """It has no card to answer and is too young to prune, so if it were not
    terminal nothing would ever move it off the open list."""
    assert is_resolved_status("failed")
    assert "failed" in RESOLVED_QUESTION_STATUSES


def test_mark_question_failed_ignores_an_index_past_the_end():
    payload: dict = {"questions": []}
    mark_question_failed(payload, 3)  # must not raise
    assert payload["questions"] == []


def test_question_count_excludes_a_failed_question():
    """It is the progress bar's denominator, so a question nobody can see
    would otherwise sit in the total for the life of the game."""
    questions = [
        {"status": "answered"},
        {"status": "failed"},
        {"status": "pending"},
    ]
    assert question_count(questions) == 2


def test_question_count_still_counts_a_rejected_question():
    """Deliberate, and the distinction the helper exists to draw: a rejected
    question really was asked and really was acted on, the same as a passed
    one. A failed entry is different in kind — nobody ever asked anything."""
    assert question_count([{"status": "rejected"}, {"status": "passed"}]) == 2
