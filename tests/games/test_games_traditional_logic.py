"""Tests for the extracted Truth-or-Dare pure-logic module.

Covers ``bot_modules/games/traditional/logic.py`` (toggle, record,
selection algorithm, recap summarization). Mirrors the pressure_cooker
pattern: the cog file stays thin; this module proves the extracted pieces
work without spinning up Discord. The Components V2 cards
(``games/traditional/layouts.py``) have their own file,
``test_games_traditional_layouts.py``.
"""

from __future__ import annotations

import inspect
import random

import pytest

from bot_modules.games.utils.question_source import get_traditional_question
from bot_modules.games.traditional.logic import (
    BAR_TEXT,
    CAT_LABELS,
    CATEGORIES,
    IDLE_MINUTES_DEFAULT,
    all_asked,
    all_asked_notice,
    asked_counts_by_user,
    asked_key,
    available_targets,
    category_allowed,
    clamp_idle_minutes,
    filter_nsfw_prefs,
    idle_close_notice,
    idle_seconds_left,
    parse_asked_key,
    question_pool_size,
    record_asked,
    select_next_question_target,
    summarize_asked_by_category,
    toggle_pref,
    touch_activity,
)


# ── toggle_pref ──────────────────────────────────────────────────────


def test_toggle_pref_adds_user_and_category_on_first_press():
    payload: dict = {}
    action = toggle_pref(payload, user_id=42, category="sfw_truth")
    assert action == "added"
    assert payload["participants"] == [42]
    assert payload["prefs"] == {"42": ["sfw_truth"]}


def test_toggle_pref_removes_existing_category():
    payload = {"participants": [42], "prefs": {"42": ["sfw_truth", "sfw_dare"]}}
    action = toggle_pref(payload, 42, "sfw_truth")
    assert action == "removed"
    assert payload["prefs"]["42"] == ["sfw_dare"]
    # User still has another preference, so still in participants
    assert payload["participants"] == [42]


def test_toggle_pref_drops_participant_when_last_preference_removed():
    """Once a player's prefs go empty they're cleanly removed from the lobby."""
    payload = {"participants": [42], "prefs": {"42": ["sfw_truth"]}}
    action = toggle_pref(payload, 42, "sfw_truth")
    assert action == "removed"
    assert payload["participants"] == []
    assert "42" not in payload["prefs"]


def test_toggle_pref_appends_second_category_for_same_user():
    payload: dict = {}
    toggle_pref(payload, 42, "sfw_truth")
    action = toggle_pref(payload, 42, "nsfw_dare")
    assert action == "added"
    assert payload["prefs"]["42"] == ["sfw_truth", "nsfw_dare"]
    # User listed only once in participants even after multiple prefs
    assert payload["participants"] == [42]


def test_toggle_pref_handles_multiple_users_independently():
    payload: dict = {}
    toggle_pref(payload, 1, "sfw_truth")
    toggle_pref(payload, 2, "nsfw_dare")
    assert payload["participants"] == [1, 2]
    assert payload["prefs"] == {"1": ["sfw_truth"], "2": ["nsfw_dare"]}


def test_toggle_pref_single_choice_replaces_existing_pick():
    """In single-choice mode a new pick swaps out the old one (radio-style)."""
    payload: dict = {}
    toggle_pref(payload, 42, "sfw_truth", single_choice=True)
    action = toggle_pref(payload, 42, "nsfw_dare", single_choice=True)
    assert action == "switched"
    assert payload["prefs"]["42"] == ["nsfw_dare"]
    # Still a single participant entry after the swap
    assert payload["participants"] == [42]


def test_toggle_pref_single_choice_first_pick_is_a_plain_add():
    """A player's first pick in single-choice mode is an ordinary add, not a swap."""
    payload: dict = {}
    action = toggle_pref(payload, 42, "sfw_dare", single_choice=True)
    assert action == "added"
    assert payload["prefs"]["42"] == ["sfw_dare"]


def test_toggle_pref_single_choice_deselects_to_empty():
    """Tapping the already-selected category in single-choice mode clears it."""
    payload: dict = {}
    toggle_pref(payload, 42, "sfw_truth", single_choice=True)
    action = toggle_pref(payload, 42, "sfw_truth", single_choice=True)
    assert action == "removed"
    assert payload["participants"] == []
    assert "42" not in payload["prefs"]


# ── record_asked ─────────────────────────────────────────────────────


def test_record_asked_writes_under_composite_key():
    payload: dict = {}
    record_asked(payload, target_id="42", category="sfw_truth", question="Q?")
    assert payload["asked"] == {"42:sfw_truth": "Q?"}


def test_record_asked_preserves_other_entries():
    payload = {"asked": {"1:sfw_truth": "Old"}}
    record_asked(payload, "2", "nsfw_dare", "New")
    assert payload["asked"] == {"1:sfw_truth": "Old", "2:nsfw_dare": "New"}


def test_record_asked_overwrites_same_pair():
    """A second question for the same (user, category) overwrites — this
    mirrors how the cog uses the key as a dedup guard."""
    payload: dict = {}
    record_asked(payload, "42", "sfw_truth", "First")
    record_asked(payload, "42", "sfw_truth", "Second")
    assert payload["asked"]["42:sfw_truth"] == "Second"


# ── one sweep, no second round (passes removed 2026-09-20) ───────────


@pytest.mark.parametrize(
    ("uid", "cat", "expected"),
    [
        pytest.param("42", "sfw_truth", "42:sfw_truth", id="plain-pair"),
        pytest.param("7", "nsfw_dare", "7:nsfw_dare", id="nsfw-pair"),
    ],
)
def test_asked_key_round_trips(uid, cat, expected):
    assert asked_key(uid, cat) == expected
    assert parse_asked_key(expected) == (uid, cat)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("42:sfw_truth:2", ("42", "sfw_truth"), id="pass-2-key"),
        pytest.param("7:nsfw_dare:10", ("7", "nsfw_dare"), id="two-digit-pass"),
    ],
)
def test_a_legacy_pass_key_still_reads_as_its_pair(key, expected):
    """A ``uid:cat:n`` key is a leftover from the second pass, removed
    2026-09-20. Nothing writes one now, but a game was live in prod sitting
    on pass 2 when it went, so one press before the restart could still have
    left a key of this shape. Read naively the trailing number becomes the
    category and the recap grows a category called "2"."""
    assert parse_asked_key(key) == expected


def test_a_legacy_pass_key_counts_under_its_real_category():
    asked = {"1:sfw_truth": "q", "1:sfw_truth:2": "q", "2:nsfw_dare:2": "q"}
    assert summarize_asked_by_category(asked)["sfw_truth"] == 2
    assert summarize_asked_by_category(asked)["nsfw_dare"] == 1
    assert "2" not in summarize_asked_by_category(asked)
    assert asked_counts_by_user(asked) == {"1": 2, "2": 1}


def test_record_asked_never_reopens_a_pair():
    """The payload's old ``pass`` key is inert: a pair asked once stays asked
    however the payload got there."""
    payload = {"pass": 2, "asked": {"42:sfw_truth": "First"}}
    record_asked(payload, "42", "sfw_truth", "Second")
    assert payload["asked"] == {"42:sfw_truth": "Second"}


def test_a_finished_room_stays_finished():
    """The game that was live in prod when passes went: everyone asked, and
    the payload still carrying ``pass: 2``. Nobody becomes askable again."""
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare"]}
    asked = {"1:sfw_truth": "q", "2:sfw_dare": "q"}
    assert available_targets(prefs, asked) == []
    assert select_next_question_target(prefs, asked) is None
    assert all_asked(prefs, asked) is True


@pytest.mark.parametrize(
    ("prefs", "asked", "expected"),
    [
        pytest.param({}, {}, False, id="empty-room-is-never-finished"),
        pytest.param({"1": []}, {}, False, id="no-prefs-is-never-finished"),
        pytest.param({"1": ["sfw_truth"]}, {}, False, id="nothing-asked"),
        pytest.param({"1": ["sfw_truth"]}, {"1:sfw_truth": "q"}, True, id="everyone-asked"),
        pytest.param(
            {"1": ["sfw_truth", "sfw_dare"]}, {"1:sfw_truth": "q"}, False,
            id="one-of-two-categories-asked",
        ),
        pytest.param(
            {"1": ["sfw_truth", "sfw_dare"]},
            {"1:sfw_truth": "q", "1:sfw_dare": "q"}, True,
            id="both-categories-asked",
        ),
    ],
)
def test_all_asked(prefs, asked, expected):
    assert all_asked(prefs, asked) is expected


def test_a_player_is_asked_once_per_category_they_picked():
    """Billy's rule, 2026-09-20: two ticks means two questions, and then the
    player is done — not one question, and not a fresh round."""
    prefs = {"1": ["sfw_truth", "nsfw_dare"]}
    assert len(available_targets(prefs, {})) == 2
    asked = {"1:sfw_truth": "q"}
    assert available_targets(prefs, asked) == [("1", "nsfw_dare")]
    asked["1:nsfw_dare"] = "q"
    assert available_targets(prefs, asked) == []


def test_progress_counts_the_whole_game():
    prefs = {"1": ["sfw_truth", "sfw_dare"]}
    asked = {"1:sfw_truth": "q"}
    assert question_pool_size(prefs, asked) == 2


def test_all_asked_notice_points_at_end_game_and_offers_no_rerun():
    text = all_asked_notice()
    assert "End Game" in text
    assert "Ask Question" not in text
    assert "pass" not in text.lower() and "again" not in text.lower()


def test_the_bar_line_names_the_game_and_the_two_things_it_does():
    assert "Truth or Dare" in BAR_TEXT
    assert "pick" in BAR_TEXT and "room" in BAR_TEXT


# ── idle close (trivia-tail-84: 18 of 19 games were left to the 24h sweep)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, IDLE_MINUTES_DEFAULT, id="unset-is-the-default"),
        pytest.param("", IDLE_MINUTES_DEFAULT, id="blank-is-the-default"),
        pytest.param("x", IDLE_MINUTES_DEFAULT, id="junk-is-the-default"),
        pytest.param(0, 0, id="zero-is-off"),
        pytest.param("15", 15, id="string-minutes"),
        pytest.param(-5, 0, id="negative-clamps-to-off"),
        pytest.param(99999, 24 * 60, id="clamps-to-a-day"),
    ],
)
def test_clamp_idle_minutes(raw, expected):
    assert clamp_idle_minutes(raw) == expected


def test_an_unconfigured_room_waits_an_hour():
    """Billy's call, 2026-09-20: 20 minutes closed rooms that were only
    between questions. No guild has ever set the dial, so this constant is
    the wait every Truth or Dare actually gets."""
    assert IDLE_MINUTES_DEFAULT == 60


def test_idle_seconds_left_is_none_when_the_dial_is_off():
    assert idle_seconds_left({"idle_minutes": 0, "last_activity": 0}, now=1000) is None
    assert idle_seconds_left({"last_activity": 0}, now=1000) is None


def test_idle_seconds_left_counts_from_the_last_activity():
    payload: dict = {"idle_minutes": 20}
    touch_activity(payload, now=1000)
    assert payload["last_activity"] == 1000
    assert idle_seconds_left(payload, now=1000) == 20 * 60
    assert idle_seconds_left(payload, now=1000 + 19 * 60) == 60
    assert idle_seconds_left(payload, now=1000 + 25 * 60) == 0


def test_idle_seconds_left_with_no_activity_stamp_starts_now():
    assert idle_seconds_left({"idle_minutes": 1}, now=5000) == 60


def test_idle_close_notice_says_how_quiet():
    assert "20 quiet minutes" in idle_close_notice(20)
    assert "1 quiet minute." in idle_close_notice(1)


# ── available_targets ────────────────────────────────────────────────


def test_available_targets_returns_all_pairs_when_nothing_asked():
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare", "nsfw_truth"]}
    assert available_targets(prefs, {}) == [
        ("1", "sfw_truth"),
        ("2", "sfw_dare"),
        ("2", "nsfw_truth"),
    ]


def test_available_targets_filters_out_already_asked_pairs():
    prefs = {"1": ["sfw_truth", "sfw_dare"]}
    asked = {"1:sfw_truth": "Q"}
    assert available_targets(prefs, asked) == [("1", "sfw_dare")]


def test_available_targets_returns_empty_when_everything_asked():
    prefs = {"1": ["sfw_truth"]}
    asked = {"1:sfw_truth": "Q"}
    assert available_targets(prefs, asked) == []


# ── asked_counts_by_user ─────────────────────────────────────────────


def test_asked_counts_by_user_groups_by_user_id():
    asked = {
        "1:sfw_truth": "a",
        "1:sfw_dare": "b",
        "2:nsfw_dare": "c",
    }
    assert asked_counts_by_user(asked) == {"1": 2, "2": 1}


def test_asked_counts_by_user_empty():
    assert asked_counts_by_user({}) == {}


# ── select_next_question_target ──────────────────────────────────────


def test_select_next_question_target_returns_none_when_no_prefs():
    assert select_next_question_target({}, {}) is None


def test_select_next_question_target_returns_none_when_all_asked():
    prefs = {"1": ["sfw_truth"]}
    asked = {"1:sfw_truth": "Q"}
    assert select_next_question_target(prefs, asked) is None


def test_select_next_question_target_prefers_least_asked_user():
    """User 1 already has 2 questions; user 2 has 0. The picker MUST
    pick user 2 even though user 1 still has pending categories."""
    prefs = {
        "1": ["sfw_truth", "nsfw_truth"],
        "2": ["sfw_dare"],
    }
    asked = {"1:sfw_dare": "Q", "1:nsfw_dare": "Q"}
    rng = random.Random(0)
    result = select_next_question_target(prefs, asked, rng=rng)
    assert result == ("2", "sfw_dare")


def test_select_next_question_target_picks_uniformly_among_tied_users():
    """When two users are tied for least-asked, the rng decides — pin
    the seed so the test is deterministic but the algorithm is still
    exercised."""
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare"]}
    rng = random.Random(0)
    result = select_next_question_target(prefs, {}, rng=rng)
    assert result in {("1", "sfw_truth"), ("2", "sfw_dare")}


def test_select_next_question_target_uses_module_random_when_rng_omitted():
    """Default path: passing no rng falls back to the module-level random.
    We only assert it returns SOMETHING valid — the actual choice is
    random, but it must be one of the available pairs."""
    prefs = {"1": ["sfw_truth"]}
    result = select_next_question_target(prefs, {})
    assert result == ("1", "sfw_truth")


def test_select_next_question_target_recounts_after_each_call():
    """Picker is stateless; the cog keeps calling it and the asked-dict
    grows. Simulate two consecutive picks."""
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare"]}
    payload: dict = {}
    rng = random.Random(1)

    first = select_next_question_target(prefs, payload.get("asked", {}), rng=rng)
    assert first is not None
    uid, cat = first
    record_asked(payload, uid, cat, "Q")

    second = select_next_question_target(prefs, payload["asked"], rng=rng)
    assert second is not None
    # The second pick must NOT be the same player (other player has 0 asked)
    second_uid, _ = second
    assert second_uid != uid


def test_select_next_question_target_never_picks_an_excluded_uid_even_when_least_asked():
    """The no-contact gate: an excluded player is dropped BEFORE the
    least-asked filter, so being the least-asked player in the room can
    never pull them back in. User 2 has been asked nothing and would win on
    the weighting alone; excluded, the pick falls to user 1 every time."""
    prefs = {"1": ["sfw_truth", "sfw_dare"], "2": ["sfw_dare"]}
    asked = {"1:nsfw_truth": "Q", "1:nsfw_dare": "Q"}
    for seed in range(20):
        result = select_next_question_target(
            prefs, asked, rng=random.Random(seed), excluded={2}
        )
        assert result is not None
        assert result[0] == "1"


@pytest.mark.parametrize("excluded", [{"2"}, {2}], ids=["str-ids", "int-ids"])
def test_select_next_question_target_excluded_accepts_int_or_str_ids(excluded):
    """``no_contact_partners`` hands back ints; the payload keys are strs.
    Either spelling must exclude."""
    prefs = {"2": ["sfw_dare"]}
    assert select_next_question_target(prefs, {}, excluded=excluded) is None


def test_select_next_question_target_returns_none_when_only_excluded_remain():
    """With every open pair belonging to an excluded player the picker
    reports exhaustion — the cog then sends its ordinary "all asked" reply,
    which is exactly what makes the refusal indistinguishable."""
    prefs = {"1": ["sfw_truth"], "2": ["sfw_dare"]}
    asked = {"1:sfw_truth": "Q"}
    assert select_next_question_target(prefs, asked, excluded={2}) is None


def test_select_next_question_target_empty_excluded_changes_nothing():
    prefs = {"1": ["sfw_truth"]}
    assert select_next_question_target(prefs, {}, excluded=set()) == ("1", "sfw_truth")


# ── summarize_asked_by_category ──────────────────────────────────────


def test_summarize_asked_by_category_zero_when_nothing_asked():
    result = summarize_asked_by_category({})
    assert result == {cat: 0 for cat in CATEGORIES}


def test_summarize_asked_by_category_counts_each_category():
    asked = {
        "1:sfw_truth": "a",
        "2:sfw_truth": "b",
        "3:nsfw_dare": "c",
    }
    result = summarize_asked_by_category(asked)
    assert result["sfw_truth"] == 2
    assert result["nsfw_dare"] == 1
    assert result["sfw_dare"] == 0
    assert result["nsfw_truth"] == 0


def test_summarize_asked_by_category_tolerates_unknown_categories():
    """Stale payloads may carry categories that no longer exist in the
    cog's CATEGORIES list. They should still be counted, not crash."""
    asked = {"1:legacy_cat": "old question"}
    result = summarize_asked_by_category(asked)
    assert result["legacy_cat"] == 1
    # Known categories are still there at zero
    assert result["sfw_truth"] == 0


# ── question_pool_size ───────────────────────────────────────────────


def test_question_pool_size_sums_pref_combos():
    prefs = {"1": ["sfw_truth", "sfw_dare"], "2": ["nsfw_dare"]}
    assert question_pool_size(prefs, {}) == 3


def test_question_pool_size_empty():
    assert question_pool_size({}, {}) == 0


def test_question_pool_size_counts_asked_combos_dropped_from_prefs():
    """A category asked then removed from a player's prefs still counts,
    so the denominator never drops below the number already asked."""
    prefs = {"1": ["sfw_truth"]}  # player dropped sfw_dare after being asked
    asked = {"1:sfw_truth": "Q", "1:sfw_dare": "Q"}
    assert question_pool_size(prefs, asked) == 2


# ── CATEGORIES / CAT_LABELS sanity ──────────────────────────────────


def test_categories_and_labels_stay_aligned():
    """Every category constant has a friendly label and vice versa."""
    assert set(CATEGORIES) == set(CAT_LABELS.keys())


@pytest.mark.parametrize("cat", CATEGORIES)
def test_each_category_has_a_human_label(cat):
    assert CAT_LABELS[cat]


# ── get_traditional_question (bank getter) ───────────────────────────


class _FakeDB:
    """Minimal async db exposing fetchall/execute for the getter."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.served: list[int] = []

    async def fetchall(self, query, params=()):
        return list(self._rows)

    async def execute(self, query, params=()):
        (qid,) = params
        self.served.append(qid)


_next_qid = iter(range(1, 10_000))


def _row(text, category, last_served_at=None):
    import json
    return (next(_next_qid), text, json.dumps([category]), last_served_at)


async def test_get_traditional_question_exact_category_match():
    db = _FakeDB([_row("truth Q", "sfw_truth"), _row("dare Q", "sfw_dare")])
    got = await get_traditional_question(db, "sfw_truth")
    assert got == "truth Q"


async def test_get_traditional_question_nsfw_is_a_distinct_category():
    # An sfw category must never serve an nsfw-tagged question.
    db = _FakeDB([_row("spicy", "nsfw_truth")])
    assert await get_traditional_question(db, "sfw_truth") is None
    assert await get_traditional_question(db, "nsfw_truth") == "spicy"


async def test_get_traditional_question_excludes_used():
    db = _FakeDB([_row("a", "sfw_dare"), _row("b", "sfw_dare")])
    got = await get_traditional_question(db, "sfw_dare", exclude=["a"])
    assert got == "b"


async def test_get_traditional_question_none_when_empty():
    assert await get_traditional_question(_FakeDB([]), "sfw_truth") is None


async def test_get_traditional_question_unknown_category_returns_none():
    db = _FakeDB([_row("a", "sfw_truth")])
    assert await get_traditional_question(db, "bogus") is None


# ── round-robin: least-recently-served wins over pure randomness ─────


async def test_get_traditional_question_prefers_never_served_row():
    """A never-served row (last_served_at is None) beats a previously-served
    row every time, regardless of random tiebreaks — a small bank shouldn't
    repeat a question while an untouched one is still available."""
    db = _FakeDB([
        _row("served already", "sfw_truth", last_served_at="2026-07-01 00:00:00"),
        _row("never served", "sfw_truth", last_served_at=None),
    ])
    for _ in range(25):
        assert await get_traditional_question(db, "sfw_truth") == "never served"


async def test_get_traditional_question_marks_the_served_row():
    """Serving a question records it as served (round-robin state) so the
    next draw across a separate game session won't repeat it immediately."""
    db = _FakeDB([_row("only one", "sfw_truth")])
    qid = db._rows[0][0]
    got = await get_traditional_question(db, "sfw_truth")
    assert got == "only one"
    assert db.served == [qid]


# ── NSFW gating ─────────────────────────────────────────────────────
# NSFW Truth/Dare shipped with no age gate at all: the four NSFW buttons were
# selectable in any channel and the bank round served from them. The gate is
# on the *preference*, so every serve path inherits it.

def test_nsfw_category_refused_in_sfw_channel():
    assert category_allowed("nsfw_truth", False) is False
    assert category_allowed("nsfw_dare", False) is False


def test_sfw_categories_always_allowed():
    for cat in ("sfw_truth", "sfw_dare"):
        assert category_allowed(cat, False) is True
        assert category_allowed(cat, True) is True


def test_nsfw_category_allowed_in_age_restricted_channel():
    assert category_allowed("nsfw_truth", True) is True
    assert category_allowed("nsfw_dare", True) is True


def test_filter_drops_nsfw_prefs_in_sfw_channel():
    prefs = {"1": ["sfw_truth", "nsfw_dare"], "2": ["nsfw_truth"]}
    assert filter_nsfw_prefs(prefs, False) == {"1": ["sfw_truth"], "2": []}


def test_filter_is_a_no_op_in_age_restricted_channel():
    prefs = {"1": ["sfw_truth", "nsfw_dare"]}
    assert filter_nsfw_prefs(prefs, True) == prefs


def test_filtered_prefs_never_offer_an_nsfw_target():
    # The gate holding end-to-end: a player opted into NSFW only, in a channel
    # that is not age-restricted, is never picked rather than served NSFW.
    prefs = filter_nsfw_prefs({"1": ["nsfw_truth", "nsfw_dare"]}, False)
    assert available_targets(prefs, {}) == []
    assert select_next_question_target(prefs, {}) is None


def test_host_view_exposes_an_end_game_button_bound_to_do_close():
    """The one wiring assertion this fix needs at the cog layer.

    ``_do_close`` is the only Truth-or-Dare call site that passes ``bot=`` and
    ``player_ids=`` to ``end_game``, i.e. the only one that pays the room. It
    shipped with no button reaching it, so no game ever paid. Guard the wiring,
    not the payout behaviour — that lives in test_games_expiry_service.py.

    The persistent id and its label are the layout builder's job now
    (``test_games_traditional_layouts.py`` covers the card shape); what
    belongs here is that the method the id gets bound to is still the one
    that closes the room through the confirm popup and the host/mod gate.
    """
    from bot_modules.games.traditional.cog import TraditionalHostView
    from bot_modules.games.traditional import layouts

    (end_button,) = [
        b for b in layouts.host_controls_row().children if b.custom_id == layouts.END_ID
    ]
    assert end_button.label == "End Game"

    source = inspect.getsource(TraditionalHostView.end_game_button)
    assert "_do_close" in source
    assert "is_host_or_mod" in source  # host/mod gate, not open to the room
    assert "ConfirmCloseView" in source  # same confirm popup every other game uses


# ── Ask Question wiring: the no-contact list reaches the picker ──────
#
# The picker's ``excluded`` parameter is only a gate if the button actually
# passes the presser's no-contact partners into it. One wiring test proves
# the glue; the selection rule itself is covered above.

from types import SimpleNamespace  # noqa: E402

import bot_modules.games.traditional.cog as traditional_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.games.db import GamesDb  # noqa: E402
from bot_modules.no_contact.service import add_pair  # noqa: E402


class _AskResponse:
    def __init__(self) -> None:
        self.messages: list[tuple] = []
        self.modals: list = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)


class _AskFollowup:
    """Where an ephemeral reply to the presser lands after a defer."""

    def __init__(self) -> None:
        self.messages: list[tuple] = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class _AskGuild:
    def __init__(self, members: dict[int, str]) -> None:
        self.id = 77
        self._members = {
            uid: SimpleNamespace(id=uid, display_name=name, mention=f"<@{uid}>")
            for uid, name in members.items()
        }

    def get_member(self, uid):
        return self._members.get(uid)


async def _ask_view(sync_db_path, prefs, asked, *, pair=None):
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path),
        active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    if pair is not None:
        add_pair(sync_db_path, 77, pair[0], pair[1], created_by=99)
    payload = {
        "participants": [int(u) for u in prefs],
        "prefs": prefs,
        "asked": asked,
    }
    game_id = await create_game(bot.games_db, 4242, 1, "traditional", payload=payload)
    view = traditional_cog.TraditionalHostView(game_id, 1, bot.games_db, bot)
    guild = _AskGuild({1: "Host", 2: "Bee", 3: "Cat"})
    interaction = SimpleNamespace(
        user=guild.get_member(1),
        guild=guild,
        guild_id=77,
        channel=SimpleNamespace(id=4242, name="games", guild=guild),
        response=_AskResponse(),
        followup=_AskFollowup(),
    )
    return view, interaction


async def test_ask_question_skips_the_pressers_no_contact_partner(monkeypatch, sync_db_path):
    """Host 1 and player 2 are a no-contact pair; player 2 is the least-asked
    (never asked) player. Ask Question must seat player 3 instead — the
    question modal is addressed to them, never to the blocked partner."""
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view(
        sync_db_path,
        prefs={"2": ["sfw_truth"], "3": ["sfw_dare"]},
        asked={"3:sfw_truth": "Q"},
        pair=(1, 2),
    )
    await view.ask_question(interaction)  # type: ignore[arg-type]
    assert interaction.response.messages == []
    assert [m.target_id for m in interaction.response.modals] == ["3"]


async def test_ask_question_with_only_the_blocked_partner_open_uses_the_ordinary_exhausted_reply(
    monkeypatch, sync_db_path
):
    """The non-disclosure pin, and the reason there is only one such string.

    Two rooms, both of which leave Ask Question with nobody to seat. In the
    first every pair really has been asked; in the second one pair is open
    and the presser is blocked with its player. The two replies must be
    **byte-identical**, because any wording that fits one case better than
    the other is what tells the presser which room they are in — see
    ``docs/specs/no_contact_spec.md``. Branching on ``all_asked()`` to word the
    genuine case "properly" is the tempting cleanup this test exists to fail.
    """
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)

    genuine, gen_interaction = await _ask_view(
        sync_db_path, prefs={"2": ["sfw_truth"]}, asked={"2:sfw_truth": "Q"},
    )
    await genuine.ask_question(gen_interaction)  # type: ignore[arg-type]
    (gen_content, gen_kwargs), = gen_interaction.response.messages

    blocked, blk_interaction = await _ask_view(
        sync_db_path, prefs={"2": ["sfw_truth"]}, asked={}, pair=(1, 2),
    )
    await blocked.ask_question(blk_interaction)  # type: ignore[arg-type]
    assert blk_interaction.response.modals == []
    (blk_content, blk_kwargs), = blk_interaction.response.messages

    assert blk_content == gen_content == all_asked_notice()
    assert blk_kwargs.get("ephemeral") is gen_kwargs.get("ephemeral") is True


# ── Ask Question draws from the bank and honours the age-gate ──────


class _AskChannel(SimpleNamespace):
    def __init__(self, *, nsfw: bool = False, **kw):
        super().__init__(**kw)
        self._nsfw = nsfw
        self.sent: list[tuple] = []

    def is_nsfw(self) -> bool:
        return self._nsfw

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=1)


async def _ask_view_in(sync_db_path, prefs, asked, *, nsfw: bool, extra: dict | None = None):
    view, interaction = await _ask_view(sync_db_path, prefs, asked)
    interaction.channel = _AskChannel(nsfw=nsfw, id=4242, name="games", guild=interaction.guild)
    if extra:
        def _apply(p):
            p.update(extra)
        await modify_payload(view.db, view.game_id, _apply)
    return view, interaction


from bot_modules.games.utils.game_manager import get_game_payload, modify_payload  # noqa: E402


async def test_ask_question_pre_fills_the_modal_from_the_bank(monkeypatch, sync_db_path):
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(sync_db_path, {"2": ["sfw_truth"]}, {}, nsfw=False)
    await view.db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text, tags)"
        " VALUES ('traditional', 'sfw', 'Bank Q?', '[\"sfw_truth\"]')"
    )
    await view.ask_question(interaction)  # type: ignore[arg-type]
    (modal,) = interaction.response.modals
    assert modal.bank_default == "Bank Q?"
    assert modal.question.default == "Bank Q?"


async def test_write_my_own_opens_an_empty_box(monkeypatch, sync_db_path):
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(sync_db_path, {"2": ["sfw_truth"]}, {}, nsfw=False)
    await view.db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text, tags)"
        " VALUES ('traditional', 'sfw', 'Bank Q?', '[\"sfw_truth\"]')"
    )
    await view.write_own(interaction)  # type: ignore[arg-type]
    (modal,) = interaction.response.modals
    assert modal.bank_default is None
    assert modal.question.default is None


async def test_an_uncached_player_is_named_user_id_never_a_bare_number(monkeypatch, sync_db_path):
    """Player 9 opted in and then left the member cache: the modal (and the
    question card's "For …" line it feeds) names them ``User 9``, the name
    resolver's fallback, not a bare ``9``."""
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(sync_db_path, {"9": ["sfw_truth"]}, {}, nsfw=False)
    await view.write_own(interaction)  # type: ignore[arg-type]
    (modal,) = interaction.response.modals
    assert modal.target_name == "User 9"


async def test_ask_question_in_a_channel_that_lost_its_age_gate_serves_no_nsfw(monkeypatch, sync_db_path):
    """safety-sweep-10 / trivia-tail-96: an NSFW-only player in a channel that
    is no longer age-restricted is not asked at all — the presser sees the
    empty-room reply, never an 'NSFW Truth for X' box."""
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(sync_db_path, {"2": ["nsfw_truth"]}, {}, nsfw=False)
    await view.ask_question(interaction)  # type: ignore[arg-type]
    assert interaction.response.modals == []
    (content, _), = interaction.response.messages
    assert content == traditional_cog.NO_PLAYERS_REPLY


async def test_ask_question_in_an_age_gated_channel_still_serves_nsfw(monkeypatch, sync_db_path):
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(sync_db_path, {"2": ["nsfw_truth"]}, {}, nsfw=True)
    await view.ask_question(interaction)  # type: ignore[arg-type]
    (modal,) = interaction.response.modals
    assert modal.cat == "nsfw_truth"


async def test_ask_question_after_the_last_pair_opens_no_box(monkeypatch, sync_db_path):
    """There is no second round (passes removed 2026-09-20). Once every
    (player, category) pair is asked the button stops handing out questions
    instead of reopening the room, and the payload's ``pass`` key — which a
    game live in prod still carried — does not bring anyone back."""
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(
        sync_db_path, {"2": ["sfw_truth"]}, {"2:sfw_truth": "q"}, nsfw=False,
        extra={"pass": 2},
    )
    await view.ask_question(interaction)  # type: ignore[arg-type]
    assert interaction.response.modals == []
    (content, kwargs), = interaction.response.messages
    assert content == all_asked_notice()
    assert kwargs.get("ephemeral") is True
    assert (await get_game_payload(view.db, view.game_id))["asked"] == {"2:sfw_truth": "q"}


async def test_modal_submit_records_under_lock_and_tells_only_the_presser(monkeypatch, sync_db_path):
    """The record goes through modify_payload (a toggle landing while the box
    was open survives), a bank question sent unchanged counts as a bank ask,
    and the "everyone has been asked" line goes to the presser alone — it was
    a channel post until 2026-09-20, and the room does not need it."""
    monkeypatch.setattr(traditional_cog, "is_host_or_mod", lambda *_: True)
    view, interaction = await _ask_view_in(sync_db_path, {"2": ["sfw_truth"]}, {}, nsfw=False)
    channel = interaction.channel
    modal = traditional_cog.AskQuestionModal(
        view.game_id, view.db, channel, 1, view.bot,
        target_id="2", target_name="Bee", category="sfw_truth", bank_default="Bank Q?",
    )
    # A concurrent toggle between opening the box and sending it.
    def _join(p):
        toggle_pref(p, 3, "sfw_dare")
    await modify_payload(view.db, view.game_id, _join)

    modal.question._value = "Bank Q?"
    responded: list[str] = []
    interaction.response.defer = lambda: _record(responded)  # type: ignore[attr-defined]
    await modal.on_submit(interaction)  # type: ignore[arg-type]

    payload = await get_game_payload(view.db, view.game_id)
    assert payload["asked"] == {"2:sfw_truth": "Bank Q?"}
    assert payload["prefs"]["3"] == ["sfw_dare"]  # the toggle was kept
    assert payload["bank_asked"] == 1 and payload["bank_used"] == ["Bank Q?"]
    assert payload["last_activity"] > 0
    # Player 3 joined mid-box, so pairs remain: nothing said to anyone yet.
    assert interaction.followup.messages == []

    # Ask player 3 too — that is the last pair, and only the presser hears it.
    modal2 = traditional_cog.AskQuestionModal(
        view.game_id, view.db, channel, 1, view.bot,
        target_id="3", target_name="Cat", category="sfw_dare",
    )
    modal2.question._value = "Own Q"
    await modal2.on_submit(interaction)  # type: ignore[arg-type]
    payload = await get_game_payload(view.db, view.game_id)
    assert payload["bank_asked"] == 1  # a written question is not a bank ask
    (content, kwargs), = interaction.followup.messages
    assert content == all_asked_notice()
    assert kwargs.get("ephemeral") is True
    # and nothing about it went to the channel.
    assert not any(c and "asked" in c for c, _ in channel.sent)


async def _record(bucket: list[str]) -> None:
    bucket.append("deferred")


async def test_launch_stores_the_idle_dial_and_arms_the_close(sync_db_path):
    from tests.fakes import FakeChannel

    class _Msg(SimpleNamespace):
        pass

    class _Chan(FakeChannel):
        async def send(self, *a, **kw):
            return _Msg(id=99, channel=self)

    bot = SimpleNamespace(games_db=GamesDb(sync_db_path), active_views={}, ctx=SimpleNamespace(db_path=sync_db_path))
    await bot.games_db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES (77, 'traditional', 1, ?)",
        ('{"idle_minutes": 7}',),
    )
    cog = traditional_cog.TraditionalCog(bot)  # type: ignore[arg-type]
    game_id = await cog.launch(channel=_Chan(id=4242), host_id=1, host_name="Host", guild_id=77, options={})
    assert game_id
    payload = await get_game_payload(bot.games_db, game_id)
    assert payload["idle_minutes"] == 7 and payload["last_activity"] > 0
    view = bot.active_views[game_id]
    try:
        assert view._idle_task is not None and not view._idle_task.done()
    finally:
        view._idle_task.cancel()


async def test_idle_fire_posts_the_recap_pays_and_archives(monkeypatch, sync_db_path):
    """The window ran out with nothing pressed: the room sees the notice and
    the recap, and the game ends through the paying path with reason=idle."""
    spy = AsyncMock()
    monkeypatch.setattr(traditional_cog, "end_game", spy)
    view, interaction = await _ask_view_in(
        sync_db_path, {"2": ["sfw_truth"]}, {"2:sfw_truth": "q"}, nsfw=False,
        extra={"idle_minutes": 1, "last_activity": 1},
    )
    channel = interaction.channel
    channel.guild = None  # no payout footer lookup against a fake guild
    edits: list = []

    async def _edit(**kw):
        edits.append(kw)

    view._message = SimpleNamespace(edit=_edit, channel=channel)
    await view._idle_fire()

    assert view._closed and view.is_finished()
    (_, kwargs), = [s for s in channel.sent if "view" in s[1]]
    recap = kwargs["view"]
    texts = [c.content for c in recap.walk_children() if hasattr(c, "content")]
    assert any(idle_close_notice(1) in t for t in texts)
    call = spy.await_args
    assert call is not None and call.kwargs["player_ids"] == [2] and call.kwargs["reason"] == "idle"
    assert view.game_id not in view.bot.active_views


async def test_idle_fire_re_arms_when_activity_landed_meanwhile(monkeypatch, sync_db_path):
    spy = AsyncMock()
    monkeypatch.setattr(traditional_cog, "end_game", spy)
    view, interaction = await _ask_view_in(
        sync_db_path, {"2": ["sfw_truth"]}, {}, nsfw=False,
        extra={"idle_minutes": 60, "last_activity": int(time.time())},
    )
    view._message = SimpleNamespace(edit=AsyncMock(), channel=interaction.channel)
    await view._idle_fire()
    try:
        assert not view._closed
        spy.assert_not_awaited()
        assert view._idle_task is not None
    finally:
        if view._idle_task:
            view._idle_task.cancel()


import time  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402
