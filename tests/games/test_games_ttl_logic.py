"""Tests for the extracted Two Truths and a Lie pure-logic modules.

Covers ``bot_modules/games/ttl/logic.py`` (lie-index parsing,
submission storage, shuffle, vote tally, score updates, recap stats) and
the submission modal. The cards themselves are
``tests/games/test_games_ttl_layouts.py``. Mirrors the pressure_cooker / games_traditional
pattern: the cog file stays thin; this module proves the extracted
pieces work without spinning up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games.ttl.logic import (
    MIN_PLAYERS,
    roster_ids,
    add_submission,
    compute_recap_winners,
    is_lobby_row,
    mark_played,
    parse_lie_index,
    played_ids_from_payload,
    shuffle_statements,
    submission_locked,
    tally_votes,
    update_scores,
)


# ── parse_lie_index ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", 0), ("2", 1), ("3", 2),
        ("a", 0), ("b", 1), ("c", 2),
        ("A", 0), ("B", 1), ("C", 2),
        ("first", 0), ("second", 1), ("third", 2),
        ("one", 0), ("two", 1), ("three", 2),
        ("FIRST", 0), ("Second", 1),
        ("  1  ", 0),  # whitespace stripped
    ],
)
def test_parse_lie_index_accepts_all_known_forms(raw, expected):
    assert parse_lie_index(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "4", "x", "fourth", "1.0", " "])
def test_parse_lie_index_rejects_unknown_forms(raw):
    assert parse_lie_index(raw) is None


def test_parse_lie_index_none_input_returns_none():
    assert parse_lie_index(None) is None  # type: ignore[arg-type]


# ── add_submission ───────────────────────────────────────────────────


def test_add_submission_creates_submissions_and_names_dicts():
    payload: dict = {}
    add_submission(payload, 42, "Alice", ["s1", "s2", "s3"], 1)
    assert payload["submissions"] == {
        "42": {"statements": ["s1", "s2", "s3"], "lie": 1}
    }
    assert payload["submitter_names"] == {"42": "Alice"}
    assert payload["submission_count"] == 1


def test_add_submission_increments_count_for_multiple_players():
    payload: dict = {}
    add_submission(payload, 1, "Alice", ["a", "b", "c"], 0)
    add_submission(payload, 2, "Bob", ["d", "e", "f"], 2)
    assert payload["submission_count"] == 2
    assert set(payload["submissions"].keys()) == {"1", "2"}
    assert payload["submitter_names"] == {"1": "Alice", "2": "Bob"}


def test_add_submission_overwrites_existing_player():
    """Resubmission overwrites — preserves the cog's previous behavior
    where modal resubmit replaced the player's prior entry, and
    submission_count stays equal to len(submissions)."""
    payload: dict = {}
    add_submission(payload, 42, "Alice", ["a", "b", "c"], 0)
    add_submission(payload, 42, "Alice2", ["x", "y", "z"], 2)
    assert payload["submissions"]["42"] == {"statements": ["x", "y", "z"], "lie": 2}
    assert payload["submitter_names"]["42"] == "Alice2"
    assert payload["submission_count"] == 1


def test_add_submission_string_user_id_is_stringified():
    payload: dict = {}
    add_submission(payload, "42", "Alice", ["s1", "s2", "s3"], 0)
    assert "42" in payload["submissions"]


def test_add_submission_copies_statements_list():
    """The stored statements list must not alias the caller's list,
    otherwise later mutations would corrupt the payload."""
    payload: dict = {}
    stmts = ["s1", "s2", "s3"]
    add_submission(payload, 1, "Alice", stmts, 0)
    stmts.append("rogue")
    assert payload["submissions"]["1"]["statements"] == ["s1", "s2", "s3"]


# ── shuffle_statements ───────────────────────────────────────────────


def test_shuffle_statements_preserves_all_statements():
    rng = random.Random(0)
    statements = ["A", "B", "C"]
    new_stmts, _ = shuffle_statements(statements, 0, rng=rng)
    assert sorted(new_stmts) == sorted(statements)


def test_shuffle_statements_tracks_lie_index_through_shuffle():
    """The lie must remain identifiable after shuffling — whichever
    position holds the lie in new_stmts must equal the original lie."""
    rng = random.Random(0)
    statements = ["truth1", "truth2", "LIE"]
    new_stmts, new_lie = shuffle_statements(statements, 2, rng=rng)
    assert new_stmts[new_lie] == "LIE"


def test_shuffle_statements_uses_module_random_when_rng_omitted():
    statements = ["A", "B", "C"]
    new_stmts, new_lie = shuffle_statements(statements, 1)
    assert sorted(new_stmts) == ["A", "B", "C"]
    assert new_stmts[new_lie] == "B"


def test_shuffle_statements_with_deterministic_rng_is_reproducible():
    rng1 = random.Random(123)
    rng2 = random.Random(123)
    s1, l1 = shuffle_statements(["A", "B", "C"], 0, rng=rng1)
    s2, l2 = shuffle_statements(["A", "B", "C"], 0, rng=rng2)
    assert s1 == s2
    assert l1 == l2


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_splits_correctly():
    votes = {1: 2, 2: 0, 3: 2, 4: 1}
    correct, fooled = tally_votes(votes, lie_index=2)
    assert set(correct) == {1, 3}
    assert set(fooled) == {2, 4}


def test_tally_votes_empty_votes_returns_empty_lists():
    correct, fooled = tally_votes({}, lie_index=0)
    assert correct == []
    assert fooled == []


def test_tally_votes_all_correct():
    votes = {1: 1, 2: 1, 3: 1}
    correct, fooled = tally_votes(votes, lie_index=1)
    assert set(correct) == {1, 2, 3}
    assert fooled == []


def test_tally_votes_all_fooled():
    votes = {1: 0, 2: 0, 3: 0}
    correct, fooled = tally_votes(votes, lie_index=2)
    assert correct == []
    assert set(fooled) == {1, 2, 3}


# ── update_scores ────────────────────────────────────────────────────


def test_update_scores_creates_subject_entry_when_new():
    scores: dict = {}
    update_scores(scores, 42, correct_voters=[1, 2], fooled_voters=[3], total_voters=3)
    assert scores["42"] == {"fooled": 1, "correct_guesses": 0, "total_guessers": 3}


def test_update_scores_credits_each_correct_voter():
    scores: dict = {}
    update_scores(scores, 42, [1, 2], [3], 3)
    assert scores["1"]["correct_guesses"] == 1
    assert scores["2"]["correct_guesses"] == 1
    assert scores["1"]["fooled"] == 0


def test_update_scores_accumulates_across_rounds():
    scores: dict = {}
    update_scores(scores, 1, correct_voters=[2], fooled_voters=[3], total_voters=2)
    update_scores(scores, 1, correct_voters=[3], fooled_voters=[2], total_voters=2)
    assert scores["1"]["fooled"] == 2  # 1+1
    assert scores["1"]["total_guessers"] == 4  # 2+2
    # Voter 2 was correct once, fooled once
    assert scores["2"]["correct_guesses"] == 1
    # Voter 3 was correct once, fooled once
    assert scores["3"]["correct_guesses"] == 1


def test_update_scores_does_not_touch_other_subjects():
    scores = {"99": {"fooled": 5, "correct_guesses": 7, "total_guessers": 12}}
    update_scores(scores, 1, [2], [], 1)
    assert scores["99"] == {"fooled": 5, "correct_guesses": 7, "total_guessers": 12}


def test_update_scores_records_a_fooled_voter_too():
    """vote-games-57: a voter who guessed wrong every round used to leave
    no trace in ``scores`` at all, so the roster built from it never paid
    them. They get a zero entry now; the subject's own numbers are as before."""
    scores: dict = {}
    update_scores(scores, 42, correct_voters=[1], fooled_voters=[3], total_voters=2)
    assert scores["3"] == {"fooled": 0, "correct_guesses": 0, "total_guessers": 0}
    assert scores["1"]["correct_guesses"] == 1
    assert scores["42"] == {"fooled": 1, "correct_guesses": 0, "total_guessers": 2}


@pytest.mark.parametrize(
    "played, scores, expected",
    [
        # Subjects first, then every voter, no duplicates, ints out.
        pytest.param(
            {"1"}, {"1": {}, "2": {}, "3": {}}, [1, 2, 3], id="subject-plus-two-guessers",
        ),
        # A subject nobody voted on is still paid.
        pytest.param({"7"}, {}, [7], id="unvoted-subject"),
        # Junk keys are dropped, not raised.
        pytest.param(["4"], {"x": {}, "5": {}}, [4, 5], id="junk-dropped"),
        pytest.param(set(), {}, [], id="empty"),
    ],
)
def test_roster_ids_is_every_subject_plus_every_voter(played, scores, expected):
    assert roster_ids(played, scores) == expected


def test_roster_ids_mirrors_game_roster_extractor():
    """The sweep and /games end rebuild the roster from the stored payload;
    the two must never drift apart."""
    from bot_modules.games.utils.game_roster import roster_from_payload

    played = ["1", "2"]
    scores = {"1": {}, "2": {}, "3": {}, "4": {}}
    assert roster_from_payload("ttl", {"played": played, "scores": scores}) == (
        roster_ids(played, scores), len(played),
    )


def test_min_players_is_three():
    """At two, every round is one person's guess (vote-games-57)."""
    assert MIN_PLAYERS == 3


# ── compute_recap_winners ────────────────────────────────────────────


def test_compute_recap_winners_empty_scores():
    stats = compute_recap_winners({}, played_ids=set())
    assert stats["best_liar"] == []
    assert stats["most_honest"] == []
    assert stats["best_guesser"] == []
    assert stats["max_correct"] == 0
    assert stats["most_fooled_count"] == 0


def test_compute_recap_winners_picks_unique_best_liar():
    scores = {
        "1": {"fooled": 5, "correct_guesses": 0, "total_guessers": 5},
        "2": {"fooled": 1, "correct_guesses": 0, "total_guessers": 5},
    }
    played = {"1", "2"}
    stats = compute_recap_winners(scores, played)
    assert stats["best_liar"] == ["1"]
    assert stats["most_fooled_count"] == 5
    assert stats["most_honest"] == ["2"]


def test_compute_recap_winners_ties_for_best_liar():
    scores = {
        "1": {"fooled": 3, "correct_guesses": 0, "total_guessers": 5},
        "2": {"fooled": 3, "correct_guesses": 0, "total_guessers": 5},
        "3": {"fooled": 1, "correct_guesses": 0, "total_guessers": 5},
    }
    stats = compute_recap_winners(scores, played_ids={"1", "2", "3"})
    assert set(stats["best_liar"]) == {"1", "2"}
    assert stats["most_fooled_count"] == 3


def test_compute_recap_winners_excludes_non_subjects_from_liar():
    """A pure guesser (not in played_ids) must not appear as best liar
    even if they have a fooled count (e.g. residual from earlier
    round)."""
    scores = {
        "1": {"fooled": 99, "correct_guesses": 3, "total_guessers": 5},  # guesser only
        "2": {"fooled": 1, "correct_guesses": 0, "total_guessers": 5},   # subject
    }
    stats = compute_recap_winners(scores, played_ids={"2"})
    assert stats["best_liar"] == ["2"]
    assert stats["most_fooled_count"] == 1


def test_compute_recap_winners_best_guesser_considers_everyone():
    """Best Guesser includes non-subjects — a player who only guessed
    can still win."""
    scores = {
        "1": {"fooled": 0, "correct_guesses": 5, "total_guessers": 0},  # pure guesser
        "2": {"fooled": 3, "correct_guesses": 1, "total_guessers": 5},  # subject
    }
    stats = compute_recap_winners(scores, played_ids={"2"})
    assert stats["best_guesser"] == ["1"]
    assert stats["max_correct"] == 5


def test_compute_recap_winners_best_guesser_ties():
    scores = {
        "1": {"fooled": 0, "correct_guesses": 3, "total_guessers": 0},
        "2": {"fooled": 0, "correct_guesses": 3, "total_guessers": 0},
    }
    stats = compute_recap_winners(scores, played_ids=set())
    assert set(stats["best_guesser"]) == {"1", "2"}
    assert stats["max_correct"] == 3


def test_compute_recap_winners_all_subjects_zero_fooled_makes_everyone_most_honest():
    scores = {
        "1": {"fooled": 0, "correct_guesses": 0, "total_guessers": 5},
        "2": {"fooled": 0, "correct_guesses": 0, "total_guessers": 5},
    }
    stats = compute_recap_winners(scores, played_ids={"1", "2"})
    assert set(stats["most_honest"]) == {"1", "2"}
    # Tied -> best liar is also everyone, since min == max
    assert set(stats["best_liar"]) == {"1", "2"}


def test_compute_recap_winners_nobody_is_best_guesser_at_zero_correct():
    """With every voter now in ``scores`` a 0-correct "winner" would be the
    whole room, so the award is simply not given."""
    scores = {
        "1": {"fooled": 2, "correct_guesses": 0, "total_guessers": 2},
        "2": {"fooled": 0, "correct_guesses": 0, "total_guessers": 0},
    }
    stats = compute_recap_winners(scores, played_ids={"1"})
    assert stats["best_guesser"] == []
    assert stats["max_correct"] == 0


def test_compute_recap_winners_accepts_played_ids_as_list():
    scores = {"1": {"fooled": 1, "correct_guesses": 0, "total_guessers": 1}}
    stats = compute_recap_winners(scores, played_ids=["1"])
    assert stats["best_liar"] == ["1"]


# ── played tracking / resubmission lock ──────────────────────────────


def test_mark_played_creates_list_and_is_idempotent():
    payload = {}
    mark_played(payload, 1)
    mark_played(payload, 1)
    mark_played(payload, "2")
    assert payload["played"] == ["1", "2"]


def test_played_ids_prefers_explicit_list_over_scores():
    payload = {"played": ["1"], "scores": {"1": {}, "2": {}, "3": {}}}
    assert played_ids_from_payload(payload) == {"1"}


def test_played_ids_falls_back_to_scores_for_legacy_payloads():
    payload = {"scores": {"1": {}, "2": {}}}
    assert played_ids_from_payload(payload) == {"1", "2"}


def test_played_ids_empty_payload():
    assert played_ids_from_payload({}) == set()


def test_submission_locked_only_after_reveal():
    payload = {"submissions": {"1": {}, "2": {}}, "played": ["1"]}
    assert submission_locked(payload, 1) is True
    assert submission_locked(payload, 2) is False
    # A player who hasn't even submitted isn't locked either.
    assert submission_locked(payload, 3) is False


# ── submission modal (components v2) ─────────────────────────────────
#
# The modal is Discord glue, but its structure caused two live bugs
# (truncated prompt, lobby-embed clobbering), so we pin the parts that
# are testable without a gateway: prompt as static TextDisplay, prefill
# of existing statements, and no fallback edit target.


def _modal(**kwargs):
    from bot_modules.games.ttl.cog import SubmitStatementsModal
    return SubmitStatementsModal("game-1", db=None, **kwargs)


def test_modal_shows_full_prompt_as_text_display():
    import discord
    prompt = "how are you gonna get over your Monday traumas?"
    modal = _modal(prompt=prompt)
    displays = [c for c in modal.children if isinstance(c, discord.ui.TextDisplay)]
    assert len(displays) == 1
    assert prompt in displays[0].content  # full text, not truncated to 45 chars
    assert prompt not in modal.title


def test_modal_without_prompt_has_no_text_display():
    import discord
    modal = _modal()
    assert not any(isinstance(c, discord.ui.TextDisplay) for c in modal.children)


def test_modal_prefills_existing_submission():
    existing = {"statements": ["s1", "s2", "s3"], "lie": 2}
    modal = _modal(existing=existing)
    assert [ti.default for ti in modal._inputs] == ["s1", "s2", "s3"]
    assert modal._lie_input.default == "3"  # 0-indexed lie → 1-indexed display


def test_modal_never_redraws_without_an_explicit_lobby():
    # The live bug: falling back to interaction.message clobbered the
    # active guess embed's first field. The modal must only ever redraw
    # the lobby view explicitly handed to it — the guess card's Join / Edit
    # hands none.
    modal = _modal()
    assert modal._lobby is None


# ── Boot recovery: lobby or guessing underway? ───────────────────────────────
#
# Rows created before 'guessing' was written on Start Guessing are still
# 'joining' mid-game; a scored round is the tell that guessing is underway.
@pytest.mark.parametrize(
    ("state", "payload", "expected"),
    [
        pytest.param("joining", {"submissions": {}, "scores": {}}, True, id="fresh-lobby"),
        pytest.param("joining", {"submissions": {"1": {}}, "scores": {}}, True, id="lobby-with-submissions"),
        pytest.param("joining", {"scores": {"1": {"fooled": 2}}}, False, id="legacy-row-mid-guessing"),
        pytest.param("joining", {"scores": {}, "played": ["1"]}, False, id="legacy-row-played-list"),
        pytest.param("guessing", {"scores": {}}, False, id="guessing-state"),
    ],
)
def test_is_lobby_row_treats_a_scored_joining_row_as_guessing(state, payload, expected):
    assert is_lobby_row(state, payload) is expected
