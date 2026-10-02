"""Tests for Greeting Watch — detection heuristic + watch/verdict DB helpers."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.onboarding.greeting_watch.service import (
    acknowledge_by_reaction,
    acknowledge_greetings,
    directed_target_ids,
    has_pending_greeting,
    is_greeting,
    list_due_greetings,
    mark_resolved,
    parse_extra_words,
    pending_greetings_for,
    record_greeting,
)
from tests.db_template import migrated_db


# ── Detection heuristic ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "good morning",
        "Good Morning everyone!",
        "goodmorning",
        "gm",
        "gm all",
        "morning",
        "mornin'",
        "hello",
        "hellooo",
        "hi",
        "hi everyone",
        "hey",
        "hey all",
        "heyyy",
        "hiya 👋",
        "howdy folks",
        "good afternoon",
        "good evening all",
        "Evening frands",
        "evening",
        "evenin'",
        "afternoon all",
        "henlo",
        "hewwo frands",
        "heyo",
        "heyooo",
        "ello",
        "hai",
        "haii chat",
        "oi",
        "oi oi oi",
        "g'day",
        "gday mates",
        "bonjour",
        "ciao",
        "aloha",
        "o/",
        "\\o/",
        "👋",
        "👋🏽",
        "yo",
        "sup",
        "what's up",
        "greetings",
        "good timezone",
        "Good Timezone!",
        "how's everyone's evening",
        "how's everyone's evening going",
        "how is your morning",
        "how's your day",
        "how's y'all's evening",
    ],
)
def test_is_greeting_positive(text):
    assert is_greeting(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "history channel is cool",  # starts with "hi"-ish but not a greeting
        "gaming tonight?",  # not "gm"
        "morningstar is my favorite weapon",  # "morning" prefix, but a sentence
        "does anyone know when the store opens",
        "himalayan salt lamps are great",  # "hi" prefix only
        "I said good morning earlier",  # greeting not at the start
        "hey can someone explain this whole ranked system to me in detail please",  # >8 words
        "how's this bug even possible",  # "how's" without a check-in subject
        "how's the weather where you live today at all",  # not everyone/your + time word
        "how do I fix this",
        "oil prices are wild",  # "oi" prefix, but a word
        "hairline fracture update",  # "hai" prefix, but a word
        "ciaociao pasta place review",  # "ciao" not at a word boundary
    ],
)
def test_is_greeting_negative(text):
    assert is_greeting(text) is False


# ---------------------------------------------------------------------------
# per-guild extra tokens


def test_parse_extra_words_splits_commas_and_newlines():
    assert parse_extra_words("henlo, good yawn\no7") == (
        "henlo",
        "good yawn",
        "o7",
    )


def test_parse_extra_words_trims_lowercases_dedupes_preserving_order():
    assert parse_extra_words(" Henlo ,henlo, GOOD Yawn ,, ") == (
        "henlo",
        "good yawn",
    )


def test_parse_extra_words_empty_and_none():
    assert parse_extra_words(None) == ()
    assert parse_extra_words("") == ()
    assert parse_extra_words(" , ,\n") == ()


def test_extra_token_matches_at_start_case_insensitive():
    extras = ("good yawn",)
    assert is_greeting("Good Yawn frands", extras) is True
    assert is_greeting("good yawn", extras) is True


def test_extra_token_not_matched_mid_message():
    assert is_greeting("I said good yawn earlier", ("good yawn",)) is False


def test_extra_token_respects_word_boundary():
    # "mep" the greeting shouldn't fire on "mepping" the verb.
    extras = ("mep",)
    assert is_greeting("mep", extras) is True
    assert is_greeting("mepping through my inbox", extras) is False


def test_extra_token_regex_specials_are_literal():
    # "h.y" must not act as a wildcard for "hey"-alikes.
    extras = ("h.y",)
    assert is_greeting("h.y all", extras) is True
    assert is_greeting("hay all", extras) is False


def test_extra_token_ending_in_symbol_matches_without_boundary():
    # A token ending in a non-word char can't take a trailing \b — it should
    # still match exactly.
    extras = ("yo!",)
    assert is_greeting("yo!", extras) is True


def test_extra_words_do_not_disturb_core_vocabulary():
    extras = ("good yawn",)
    assert is_greeting("good morning", extras) is True
    assert is_greeting("does anyone know when the store opens", extras) is False


def test_extra_words_key_registered_and_not_secret_shaped():
    # The registry rejects secret-shaped keys (anything matching "token" etc.)
    # at import time — the original `greeting_watch_extra_tokens` name crashed
    # the bot on every boot. Importing the registry here makes any regression
    # fail in this feature's scoped-gate tests, not first in prod.
    from bot_modules.core import settings_registry

    setting = settings_registry.get_setting("greeting_watch_extra_words")
    assert setting is not None
    assert settings_registry.get_setting("greeting_watch_extra_tokens") is None


# ── DB fixture ───────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "gw.db"
    migrated_db(path)
    return path


GUILD = 1000
CHANNEL = 2000
GREETER = 3000
OTHER = 4000


# ── record / dedup ───────────────────────────────────────────────────


def test_record_greeting_inserts_and_lists_due(db_path):
    with open_db(db_path) as conn:
        assert record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100) is True
    with open_db(db_path) as conn:
        due = list_due_greetings(conn, GUILD, cutoff_ts=200)
        assert len(due) == 1
        assert due[0].message_id == 1
        assert due[0].author_id == GREETER


def test_record_greeting_dedups_pending_author(db_path):
    """A second greeting from the same author in the same channel is a no-op
    while the first is still unresolved."""
    with open_db(db_path) as conn:
        assert record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100) is True
        assert has_pending_greeting(conn, GUILD, CHANNEL, GREETER) is True
        assert record_greeting(conn, GUILD, 2, CHANNEL, GREETER, created_ts=105) is False
        due = list_due_greetings(conn, GUILD, cutoff_ts=200)
        assert len(due) == 1


def test_record_greeting_allowed_again_after_resolution(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        # First one resolved → a fresh greeting is tracked again.
        assert record_greeting(conn, GUILD, 2, CHANNEL, GREETER, created_ts=1100) is True


def test_list_due_respects_cutoff(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        # cutoff before the greeting → not yet due
        assert list_due_greetings(conn, GUILD, cutoff_ts=50) == []
        # cutoff at/after → due
        assert len(list_due_greetings(conn, GUILD, cutoff_ts=100)) == 1


# ── acknowledgment (ingest-time, nothing about the answerer stored) ──


def _outcome(conn, message_id):
    return conn.execute(
        "SELECT resolved_at, outcome FROM greeting_watch WHERE message_id = ?",
        (message_id,),
    ).fetchone()


def test_acknowledge_marks_pending_greeting_but_leaves_it_open(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert acknowledge_greetings(conn, GUILD, OTHER, (CHANNEL,), (GREETER,)) == 1
        row = _outcome(conn, 1)
        assert row["outcome"] == "acknowledged"
        # Still pending: the window runs out before the loop closes it, and a
        # later answerer can still find it for the greeting_answered quest.
        assert row["resolved_at"] is None
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), (GREETER,)) == [
            (1, GREETER)
        ]
        due = list_due_greetings(conn, GUILD, cutoff_ts=200)
        assert [g.acknowledged for g in due] == [True]


# ── a hello back, and a reaction, are answers too ────────────────────
#
# Measured 2026-09-30 over 30 days of prod: 242 "unanswered" alerts, of which
# 55 had another member say hello back without replying or tagging, 54 were
# themselves a hello back to someone else's greeting, and 35 carried a reaction.


def test_a_hello_from_someone_else_answers_the_pending_greeting(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert record_greeting(conn, GUILD, 2, CHANNEL, OTHER, created_ts=130) is True
        # Both are answered: they greeted each other.
        assert _outcome(conn, 1)["outcome"] == "acknowledged"
        assert _outcome(conn, 2)["outcome"] == "acknowledged"
        # And both stay pending, so a reply to either still earns the quest.
        assert _outcome(conn, 1)["resolved_at"] is None
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), (GREETER, OTHER)) == [
            (1, GREETER), (2, OTHER),
        ]


@pytest.mark.parametrize(
    ("first", "second_channel", "second_author"),
    [
        pytest.param("pending", CHANNEL + 1, OTHER, id="other-channel"),
        pytest.param("pending", CHANNEL, GREETER, id="own-second-hello"),
        pytest.param("resolved", CHANNEL, OTHER, id="first-window-already-closed"),
    ],
)
def test_a_hello_that_follows_nobody_elses_is_watched_as_before(
    db_path, first, second_channel, second_author
):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        if first == "resolved":
            mark_resolved(conn, GUILD, 1, "unanswered", now_ts=500)
        record_greeting(conn, GUILD, 2, second_channel, second_author, created_ts=600)
        assert _outcome(conn, 1)["outcome"] == (
            "unanswered" if first == "resolved" else None
        )
        row = _outcome(conn, 2)
        assert row is None or row["outcome"] is None


@pytest.mark.parametrize(
    ("reactor", "resolved", "marked"),
    [
        pytest.param(OTHER, False, 1, id="someone-else-reacts"),
        pytest.param(GREETER, False, 0, id="own-reaction-never-counts"),
        pytest.param(OTHER, True, 0, id="verdict-already-given"),
    ],
)
def test_a_reaction_on_the_greeting_answers_it(db_path, reactor, resolved, marked):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        if resolved:
            mark_resolved(conn, GUILD, 1, "unanswered", now_ts=500)
        assert acknowledge_by_reaction(conn, GUILD, 1, reactor) == marked
        # A reaction on any other message is nothing to do with the greeting.
        assert acknowledge_by_reaction(conn, GUILD, 99, OTHER) == 0
        row = _outcome(conn, 1)
        if marked:
            assert row["outcome"] == "acknowledged" and row["resolved_at"] is None
        else:
            assert row["outcome"] == ("unanswered" if resolved else None)


def test_unacknowledged_greeting_lists_as_not_acknowledged(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert [g.acknowledged for g in list_due_greetings(conn, GUILD, 200)] == [False]


@pytest.mark.parametrize(
    ("author", "channels", "targets"),
    [
        pytest.param(OTHER, (9999,), (GREETER,), id="other-channel"),
        pytest.param(OTHER, (CHANNEL,), (OTHER,), id="someone-else-targeted"),
        pytest.param(GREETER, (CHANNEL,), (GREETER,), id="greeter-self-reply"),
        pytest.param(OTHER, (), (GREETER,), id="no-channels"),
        pytest.param(OTHER, (CHANNEL,), (), id="no-targets"),
    ],
)
def test_acknowledge_ignores_non_answers(db_path, author, channels, targets):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert acknowledge_greetings(conn, GUILD, author, channels, targets) == 0
        assert _outcome(conn, 1)["outcome"] is None


def test_acknowledge_ignores_resolved_greeting(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        assert acknowledge_greetings(conn, GUILD, OTHER, (CHANNEL,), (GREETER,)) == 0
        assert _outcome(conn, 1)["outcome"] == "unanswered"


def test_acknowledge_thread_parent_channel_counts(db_path):
    # A reply inside a thread off the watched channel passes (thread, parent).
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert acknowledge_greetings(
            conn, GUILD, OTHER, (7777, CHANNEL), (GREETER,)
        ) == 1


def test_acknowledge_stores_nothing_about_the_answerer(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        before = {
            r[0]: conn.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not r[0].startswith("sqlite_")
        }
        acknowledge_greetings(conn, GUILD, OTHER, (CHANNEL,), (GREETER,))
        after = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in before
        }
        assert after == before
        values = [v for v in tuple(conn.execute("SELECT * FROM greeting_watch").fetchone())]
        assert OTHER not in values


# ── directed targets (shared by the ack and the quest detectors) ─────


@pytest.mark.parametrize(
    "reply_author, mentions, expected",
    [
        pytest.param((GREETER, False), [], (GREETER,), id="reply-partner"),
        pytest.param(None, [(GREETER, False), (OTHER, False)], (GREETER, OTHER), id="mentions"),
        pytest.param((OTHER, False), [(GREETER, False), (OTHER, False)], (OTHER, GREETER), id="reply-first-deduped"),
        pytest.param((4242, True), [(4343, True)], (), id="bots-never-count"),
        pytest.param((9999, False), [(9999, False)], (), id="author-never-counts"),
        pytest.param(None, [], (), id="nothing-directed"),
    ],
)
def test_directed_target_ids(reply_author, mentions, expected):
    assert directed_target_ids(9999, reply_author, mentions) == expected


# ── resolution ───────────────────────────────────────────────────────


def test_mark_resolved_removes_from_due(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "acknowledged", now_ts=1000)
        assert list_due_greetings(conn, GUILD, cutoff_ts=2000) == []
        row = conn.execute(
            "SELECT resolved_at, outcome FROM greeting_watch WHERE message_id = 1"
        ).fetchone()
        assert row["resolved_at"] == 1000
        assert row["outcome"] == "acknowledged"


def test_mark_resolved_is_idempotent(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        # A second resolve must not overwrite the first verdict.
        mark_resolved(conn, GUILD, 1, "acknowledged", now_ts=2000)
        row = conn.execute(
            "SELECT resolved_at, outcome FROM greeting_watch WHERE message_id = 1"
        ).fetchone()
        assert row["resolved_at"] == 1000
        assert row["outcome"] == "unanswered"


def test_mark_resolved_keeps_an_acknowledgment_stamped_before_it(db_path):
    """A reply that lands between the loop's fresh check and its verdict write
    closes the row as acknowledged, not overwritten by ``unanswered``."""
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        acknowledge_greetings(conn, GUILD, OTHER, (CHANNEL,), (GREETER,))
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        row = conn.execute(
            "SELECT resolved_at, outcome FROM greeting_watch WHERE message_id = 1"
        ).fetchone()
        assert row["resolved_at"] == 1000
        assert row["outcome"] == "acknowledged"


# ── pending_greetings_for (the greeting_answered quest detector) ─────


def test_pending_greetings_for_matches_channel_and_target(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert pending_greetings_for(
            conn, GUILD, (CHANNEL,), (GREETER,)
        ) == [(1, GREETER)]
        # Wrong channel or wrong target: no match.
        assert pending_greetings_for(conn, GUILD, (9999,), (GREETER,)) == []
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), (OTHER,)) == []


def test_pending_greetings_for_ignores_resolved(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), (GREETER,)) == []


def test_pending_greetings_for_empty_inputs(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert pending_greetings_for(conn, GUILD, (), (GREETER,)) == []
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), ()) == []


def test_pending_greetings_for_multiple_targets(db_path):
    # A message replying to one greeter while mentioning another answers both.
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        record_greeting(conn, GUILD, 2, CHANNEL, OTHER, created_ts=110)
        got = pending_greetings_for(conn, GUILD, (CHANNEL,), (GREETER, OTHER))
        assert sorted(got) == [(1, GREETER), (2, OTHER)]


# ── resolved-row GC (2026-08 review, intake G1) ──────────────────────


def test_gc_resolved_watches_sweeps_only_old_resolved_rows(db_path):
    import time as _time

    from bot_modules.onboarding.greeting_watch.service import gc_resolved_watches

    now = _time.time()
    with open_db(db_path) as conn:
        # Old + resolved → swept.
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "acknowledged", now_ts=now - 40 * 86400)
        # Recent + resolved → kept.
        record_greeting(conn, GUILD, 2, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 2, "unanswered", now_ts=now - 86400)
        # Pending → never touched, however old.
        record_greeting(conn, GUILD, 3, CHANNEL, OTHER, created_ts=100)

        removed = gc_resolved_watches(conn)
        rows = conn.execute(
            "SELECT message_id FROM greeting_watch ORDER BY message_id"
        ).fetchall()
    assert removed == 1
    assert [r["message_id"] for r in rows] == [2, 3]
