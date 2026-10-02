"""Tests for services/intake_service — intake cards (welcome tracker).

The tested unit is the config parsing, card/step ledger, auto-tick matching,
completion (code wins, skips stamped), stale scan, and watch registry; the
Discord embed/buttons in intake_views are glue exercised via this layer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.onboarding.intake import service as svc
from tests.db_template import migrated_db

GUILD = 42
NEWCOMER = 7
GREETER = 501
CHANNEL = 555  # where the cards themselves post
GREET_CHANNEL = 556  # where newcomers are actually talked to
MEMBER_ROLE = 901
NSFW_ROLE = 902
VERIFIED_ROLE = 903
UNVERIFIED_ROLE = 904

CUSTOM_STEPS = [
    {"key": "greeted", "label": "Greeted", "auto": "greeted"},
    {"key": "member_role", "label": "Member role", "auto": "role_gained", "role_id": MEMBER_ROLE},
    {"key": "sfw_questions", "label": "SFW questions asked"},
    {"key": "nsfw_role", "label": "NSFW access", "auto": "role_gained", "role_id": NSFW_ROLE},
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "intake.db"
    migrated_db(path)
    return path


@pytest.fixture(autouse=True)
def _clean_watch():
    svc._reset_watch_for_tests()
    yield
    svc._reset_watch_for_tests()


def _enable(conn, channel=CHANNEL, greet_channel=GREET_CHANNEL):
    set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
    set_config_value(conn, svc.CHANNEL_KEY, str(channel), GUILD)
    # Cards post to `channel`; greeting happens where the members are.
    set_config_value(conn, svc.WELCOME_CHANNEL_KEY, str(greet_channel), GUILD)


def _use_custom_steps(conn):
    set_config_value(conn, svc.STEPS_KEY, json.dumps(CUSTOM_STEPS), GUILD)


# ── config / dark gate ────────────────────────────────────────────────


def test_ships_dark_until_enabled_and_channel(db_path):
    with open_db(db_path) as conn:
        assert svc.is_enabled(conn, GUILD) is False
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        # Enabled flag alone isn't enough — no channel resolves yet.
        assert svc.is_enabled(conn, GUILD) is False
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        assert svc.is_enabled(conn, GUILD) is True


def test_channel_falls_back_to_greeter_chat(db_path):
    with open_db(db_path) as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.FALLBACK_CHANNEL_KEY, "777", GUILD)
        assert svc.intake_channel_id(conn, GUILD) == 777
        assert svc.is_enabled(conn, GUILD) is True
        # An explicit intake channel wins over the fallback.
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        assert svc.intake_channel_id(conn, GUILD) == CHANNEL


def test_greet_channel_prefers_greeter_chat_over_welcome(db_path):
    with open_db(db_path) as conn:
        # Nothing configured → no channel counts as a greeting place.
        assert svc.greet_channel_id(conn, GUILD) == 0
        set_config_value(conn, svc.WELCOME_CHANNEL_KEY, str(GREET_CHANNEL), GUILD)
        assert svc.greet_channel_id(conn, GUILD) == GREET_CHANNEL
        # A greeter chat channel, when set, is the more specific answer.
        set_config_value(conn, svc.FALLBACK_CHANNEL_KEY, "778", GUILD)
        assert svc.greet_channel_id(conn, GUILD) == 778
        # The card channel is not a greeting channel.
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        assert svc.greet_channel_id(conn, GUILD) == 778


def test_stale_hours_tolerates_garbage(db_path):
    with open_db(db_path) as conn:
        assert svc.stale_hours(conn, GUILD) == svc.DEFAULT_STALE_HOURS
        set_config_value(conn, svc.STALE_HOURS_KEY, "nope", GUILD)
        assert svc.stale_hours(conn, GUILD) == svc.DEFAULT_STALE_HOURS
        set_config_value(conn, svc.STALE_HOURS_KEY, "-3", GUILD)
        assert svc.stale_hours(conn, GUILD) == svc.DEFAULT_STALE_HOURS
        set_config_value(conn, svc.STALE_HOURS_KEY, "6", GUILD)
        assert svc.stale_hours(conn, GUILD) == 6.0


def test_code_matches_case_insensitive_and_empty_never(db_path):
    assert svc.code_matches("Welcome aboard! dk-7734 🎉", "DK-7734") is True
    assert svc.code_matches("no code here", "DK-7734") is False
    # An unset code must never match anything.
    assert svc.code_matches("any message at all", "") is False


# ── step config parsing ───────────────────────────────────────────────


def test_parse_steps_defaults_on_empty_and_garbage():
    assert svc.parse_steps("") == list(svc.DEFAULT_STEPS)
    assert svc.parse_steps("not json {") == list(svc.DEFAULT_STEPS)
    assert svc.parse_steps('{"key": "x"}') == list(svc.DEFAULT_STEPS)  # not a list
    assert svc.parse_steps("[]") == list(svc.DEFAULT_STEPS)


def test_parse_steps_custom_list():
    steps = svc.parse_steps(json.dumps(CUSTOM_STEPS))
    assert [s.key for s in steps] == ["greeted", "member_role", "sfw_questions", "nsfw_role"]
    assert steps[1].auto_kind == svc.AUTO_ROLE_GAINED
    assert steps[1].auto_role_id == MEMBER_ROLE
    assert steps[2].auto_kind == ""  # manual


def test_parse_steps_drops_invalid_entries_individually():
    raw = json.dumps(
        [
            {"key": "ok", "label": "Fine"},
            "not a dict",
            {"key": "", "label": "no key"},
            {"key": "nolabel", "label": ""},
            {"key": "badauto", "label": "Bad", "auto": "telepathy"},
            {"key": "ok", "label": "Duplicate key"},
            {"key": "badrole", "label": "Bad role", "auto": "role_gained", "role_id": "x"},
        ]
    )
    steps = svc.parse_steps(raw)
    assert [s.key for s in steps] == ["ok", "badrole"]
    assert steps[1].auto_role_id == 0  # unparseable role id degrades to 0


def test_parse_steps_code_is_optional_and_stripped():
    # Configs written before step codes existed have no "code" key at all.
    raw = json.dumps(
        [
            {"key": "legacy", "label": "No code key"},
            {"key": "coded", "label": "Coded", "code": "  DK-SFW  "},
            {"key": "blank", "label": "Blank", "code": "   "},
        ]
    )
    assert [s.code for s in svc.parse_steps(raw)] == ["", "DK-SFW", ""]


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        pytest.param([("a", "DK-SFW"), ("b", "DK-NSFW")], None, id="distinct"),
        # Matching is containment, so a code inside another fires both. The
        # codes ride along in the result because labels aren't unique.
        pytest.param(
            [("a", "DK-SFW"), ("b", "DK-SFW-DONE")],
            (("b", "DK-SFW-DONE"), ("a", "DK-SFW")),
            id="substring",
        ),
        pytest.param(
            [("a", "dk-sfw"), ("b", "DK-SFW")],
            (("b", "DK-SFW"), ("a", "dk-sfw")),
            id="case-insensitive-dupe",
        ),
        # Empty codes are "off" and never collide, not even with each other.
        pytest.param([("a", ""), ("b", ""), ("c", "DK")], None, id="empties-ignored"),
    ],
)
def test_code_conflict(codes, expected):
    assert svc.code_conflict(codes) == expected


def test_config_code_conflict_reads_the_side_that_is_not_overridden(db_path):
    # The two sides are writable independently, so a save touching only one
    # of them still has to be checked against what's stored for the other.
    with open_db(db_path) as conn:
        _use_coded_steps(conn)  # DK-SFW / DK-NSFW
        set_config_value(conn, svc.CODE_KEY, "DK-DONE", GUILD)
        assert svc.config_code_conflict(conn, GUILD) is None
        # A completion code the stored step code contains: any message
        # carrying "DK-SFW" carries "DK" too, so completion fires.
        assert svc.config_code_conflict(conn, GUILD, completion="DK") == (
            ("SFW questions", "DK-SFW"),
            (svc.COMPLETION_CODE_LABEL, "DK"),
        )
        # …and the mirror: a step code that swallows the stored completion.
        clash = svc.config_code_conflict(
            conn, GUILD, steps=[svc.StepDef("s", "Wrap", code="say DK-DONE now")]
        )
        assert clash == (
            ("Wrap", "say DK-DONE now"),
            (svc.COMPLETION_CODE_LABEL, "DK-DONE"),
        )


def test_completion_code_containing_a_step_code_closes_the_card(db_path):
    # Why the guard exists, stated as behavior: with the clash in place, the
    # SFW canned message doesn't tick the SFW step — it completes the card and
    # stamps every remaining step skipped.
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        set_config_value(conn, svc.CODE_KEY, "DK", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, content="all done DK-SFW", channel_id=999) == [
            (svc.ACTION_COMPLETE, NEWCOMER, "")
        ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(SimpleNamespace(reference=None), 0, id="not-a-reply"),
        pytest.param(
            SimpleNamespace(reference=SimpleNamespace(resolved=None)), 0, id="unresolved"
        ),
        # DeletedReferencedMessage has no .author at all.
        pytest.param(
            SimpleNamespace(
                reference=SimpleNamespace(resolved=SimpleNamespace(spec=1))
            ),
            0,
            id="deleted-target",
        ),
        pytest.param(
            SimpleNamespace(
                reference=SimpleNamespace(
                    resolved=SimpleNamespace(author=SimpleNamespace(id=NEWCOMER))
                )
            ),
            NEWCOMER,
            id="reply-to-newcomer",
        ),
    ],
)
def test_reply_target_id(message, expected):
    assert svc.reply_target_id(message) == expected


# ── card ledger + dedup ───────────────────────────────────────────────


def test_create_card_snapshots_configured_steps(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert cid is not None
        steps = svc.steps_for(conn, cid)
        assert [s["step_key"] for s in steps] == [s2["key"] for s2 in CUSTOM_STEPS]
        # Snapshot: later config edits must not touch the in-flight card.
        set_config_value(conn, svc.STEPS_KEY, json.dumps([CUSTOM_STEPS[0]]), GUILD)
        assert len(svc.steps_for(conn, cid)) == len(CUSTOM_STEPS)


def test_one_open_card_per_member(db_path):
    with open_db(db_path) as conn:
        first = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert first is not None
        assert svc.create_card(conn, GUILD, NEWCOMER, 101.0) is None
        assert svc.get_open_card(conn, GUILD, NEWCOMER)["id"] == first


def test_resolve_frees_the_slot(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert svc.resolve_card(conn, cid, GREETER, 200.0, svc.RESOLUTION_DISMISSED) == 1
        assert svc.get_open_card(conn, GUILD, NEWCOMER) is None
        # Resolving again is a no-op; a rejoin can open a fresh card.
        assert svc.resolve_card(conn, cid, GREETER, 300.0, svc.RESOLUTION_LEFT) == 0
        assert svc.create_card(conn, GUILD, NEWCOMER, 400.0) is not None


def test_delete_card_removes_steps_too(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.set_card_message(conn, cid, CHANNEL, 12345)
        assert svc.get_card(conn, cid)["message_id"] == 12345
        svc.delete_card(conn, cid)
        assert svc.get_card(conn, cid) is None
        assert svc.steps_for(conn, cid) == []


# ── manual tick / untick ──────────────────────────────────────────────


def test_tick_records_actor_and_untick_toggles(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert svc.set_step_state(
            conn, cid, "sfw_questions", done=True, actor_id=GREETER, at=200.0
        )
        step = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == "sfw_questions")
        assert step["done_by"] == GREETER
        assert step["done_at"] == 200.0
        # A second tick loses: the original ticker is preserved.
        assert not svc.set_step_state(
            conn, cid, "sfw_questions", done=True, actor_id=999, at=300.0
        )
        step = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == "sfw_questions")
        assert step["done_by"] == GREETER
        # Untick clears; unticking an un-done step is a no-op.
        assert svc.set_step_state(
            conn, cid, "sfw_questions", done=False, actor_id=GREETER, at=400.0
        )
        assert not svc.set_step_state(
            conn, cid, "sfw_questions", done=False, actor_id=GREETER, at=500.0
        )


# ── auto ticks ────────────────────────────────────────────────────────


def test_auto_tick_greeted(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        card, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0, actor_id=GREETER
        )
        assert card is not None
        assert ticked == ["greeted"]
        # Already done → nothing new ticks, no re-render needed.
        _, again = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 300.0, actor_id=GREETER
        )
        assert again == []


def test_auto_tick_role_matches_configured_role_only(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # An unrelated role does nothing.
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 200.0, role_id=12345
        )
        assert ticked == []
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 200.0, role_id=MEMBER_ROLE
        )
        assert ticked == ["member_role"]
        step = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == "member_role")
        assert step["done_by"] == svc.AUTO_ACTOR


def test_auto_tick_role_never_matches_role_id_zero(db_path):
    with open_db(db_path) as conn:
        # Tolerantly-parsed stored config can still carry a role_gained step
        # with role_id 0 — gaining any role (even "role 0") must not tick it.
        set_config_value(
            conn,
            svc.STEPS_KEY,
            json.dumps([{"key": "r", "label": "Role", "auto": "role_gained"}]),
            GUILD,
        )
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 200.0, role_id=0
        )
        assert ticked == []


def test_default_steps_are_all_tickable():
    # Regression: the default role steps shipped as role_gained with
    # role_id 0 — auto-untickable AND button-less. Every default step must
    # be either manual (gets a button) or an auto kind that needs no role.
    for step in svc.DEFAULT_STEPS:
        if step.auto_kind == svc.AUTO_ROLE_GAINED:
            assert step.auto_role_id > 0, f"{step.key} can never tick"


def test_auto_tick_without_open_card(db_path):
    with open_db(db_path) as conn:
        card, ticked = svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0)
        assert card is None
        assert ticked == []


# ── verification signal (our gate, or a third-party verifier) ─────────


@pytest.mark.parametrize(
    "gained,unverified_removed,expected",
    [
        pytest.param([VERIFIED_ROLE], False, True, id="verifier-grants-role"),
        pytest.param([], True, True, id="our-gate-strips-unverified"),
        pytest.param([MEMBER_ROLE], False, False, id="unrelated-role"),
        pytest.param([], False, False, id="nothing-happened"),
        pytest.param([VERIFIED_ROLE, MEMBER_ROLE], False, True, id="among-others"),
    ],
)
def test_verification_signalled(db_path, gained, unverified_removed, expected):
    with open_db(db_path) as conn:
        set_config_value(conn, svc.VERIFIED_ROLE_KEY, str(VERIFIED_ROLE), GUILD)
        assert (
            svc.verification_signalled(conn, GUILD, gained, unverified_removed)
            is expected
        )


def test_verification_signalled_needs_a_configured_role(db_path):
    # Prod bug: unverified_role_id was 0 and verification was done by a
    # third-party bot granting a role, so the Verified step never ticked.
    # With neither key set, no role gain may be read as verification.
    with open_db(db_path) as conn:
        assert svc.verification_signalled(conn, GUILD, [VERIFIED_ROLE], False) is False
        assert svc.verification_signalled(conn, GUILD, [0], False) is False
        set_config_value(conn, svc.VERIFIED_ROLE_KEY, str(VERIFIED_ROLE), GUILD)
        assert svc.verification_signalled(conn, GUILD, [VERIFIED_ROLE], False) is True


def test_verified_step_ticks_when_the_verifier_grants_its_role(db_path):
    # End to end at the service layer: the role lands, the step ticks once.
    with open_db(db_path) as conn:
        _enable(conn)
        set_config_value(conn, svc.VERIFIED_ROLE_KEY, str(VERIFIED_ROLE), GUILD)
        set_config_value(
            conn,
            svc.STEPS_KEY,
            json.dumps([{"key": "verified", "label": "Verified", "auto": "verified"}]),
            GUILD,
        )
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert svc.verification_signalled(conn, GUILD, [VERIFIED_ROLE], False) is True
        _, ticked = svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_VERIFIED, 200.0)
        assert ticked == ["verified"]
        assert svc.steps_for(conn, cid)[0]["done_at"] == 200.0
        # Re-firing (role churn) doesn't re-tick.
        _, again = svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_VERIFIED, 300.0)
        assert again == []


# ── completion (the code always wins) ─────────────────────────────────


def test_complete_stamps_skips_and_records_poster(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0, actor_id=GREETER)
        result = svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 300.0)
        assert result is not None
        card, skipped = result
        assert card["id"] == cid
        # Everything un-done is skipped, in position order; done steps aren't.
        assert skipped == ["member_role", "sfw_questions", "nsfw_role"]
        closed = svc.get_card(conn, cid)
        assert closed["resolution"] == svc.RESOLUTION_COMPLETED
        assert closed["resolved_by"] == GREETER
        steps = {s["step_key"]: s for s in svc.steps_for(conn, cid)}
        assert steps["greeted"]["skipped"] == 0
        assert steps["sfw_questions"]["skipped"] == 1
        # Progress counts real ticks only, never skips.
        assert svc.count_progress(svc.steps_for(conn, cid)) == (1, 4)


def _tick_all(conn, cid, *, at=200.0, actor=GREETER, skip=()):
    """Tick every step on a card; returns the keys ticked."""
    keys = []
    for step in svc.steps_for(conn, cid):
        key = str(step["step_key"])
        if key in skip:
            continue
        svc.set_step_state(conn, cid, key, done=True, actor_id=actor, at=at)
        keys.append(key)
    return keys


# ── auto-completion (a fully ticked checklist closes itself) ──────────


def test_last_tick_closes_the_card(db_path):
    """The bug: a card with every box ticked used to sit open forever.

    Only a posted completion code closed a card, so finished intakes stayed
    in the queue and the completion-keyed report panels never had a row.
    """
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _tick_all(conn, cid, at=250.0, skip=("nsfw_role",))
        # Still one box short — nothing closes.
        assert svc.autocomplete_if_finished(conn, cid, actor_id=GREETER) is None
        assert svc.get_card(conn, cid)["resolved_at"] is None

        svc.set_step_state(
            conn, cid, "nsfw_role", done=True, actor_id=GREETER, at=400.0
        )
        result = svc.autocomplete_if_finished(conn, cid, actor_id=GREETER)
        assert result is not None
        card, welcomer = result
        assert card["id"] == cid
        assert welcomer == GREETER
        closed = svc.get_card(conn, cid)
        assert closed["resolution"] == svc.RESOLUTION_COMPLETED
        assert closed["resolved_by"] == GREETER
        # Closed at the last tick, not "now" — time-to-complete stays honest.
        assert closed["resolved_at"] == 400.0
        # Nothing was stamped skipped: every step was genuinely done.
        assert all(s["skipped"] == 0 for s in svc.steps_for(conn, cid))
        assert svc.count_progress(svc.steps_for(conn, cid)) == (4, 4)


def test_autocomplete_credits_last_human_when_an_auto_step_lands_last(db_path):
    """An auto tick closing the card must not credit the closure to nobody."""
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _tick_all(conn, cid, at=200.0, actor=GREETER, skip=("nsfw_role",))
        svc.set_step_state(
            conn, cid, "nsfw_role", done=True, actor_id=svc.AUTO_ACTOR, at=300.0
        )
        result = svc.autocomplete_if_finished(conn, cid)
        assert result is not None
        assert result[1] == GREETER
        assert svc.get_card(conn, cid)["resolved_by"] == GREETER


def test_autocomplete_credits_nobody_when_no_human_ever_ticked(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _tick_all(conn, cid, at=200.0, actor=svc.AUTO_ACTOR)
        result = svc.autocomplete_if_finished(conn, cid)
        assert result is not None
        assert result[1] == 0
        assert svc.get_card(conn, cid)["resolved_by"] == 0


def test_autocomplete_ignores_a_closed_or_missing_card(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _tick_all(conn, cid, at=200.0)
        assert svc.autocomplete_if_finished(conn, cid, actor_id=GREETER) is not None
        # Second pass is a no-op — a resolved card never re-resolves.
        assert svc.autocomplete_if_finished(conn, cid, actor_id=NEWCOMER) is None
        assert svc.get_card(conn, cid)["resolved_by"] == GREETER
        assert svc.autocomplete_if_finished(conn, 99999, actor_id=GREETER) is None


def test_autocomplete_never_closes_a_stepless_card(db_path):
    """No steps means nothing was done — "all ticked" must not be vacuous.

    An empty step config falls back to the defaults, so this state only
    arises if a card's rows go missing; it must not read as finished.
    """
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        conn.execute("DELETE FROM intake_card_steps WHERE card_id = ?", (cid,))
        assert svc.steps_for(conn, cid) == []
        assert svc.autocomplete_if_finished(conn, cid, actor_id=GREETER) is None
        assert svc.get_card(conn, cid)["resolved_at"] is None
        assert svc.finished_open_cards(conn, GUILD) == []


def test_finished_open_cards_finds_only_the_done_ones(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        done_id = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _tick_all(conn, done_id, at=200.0)
        partial_id = svc.create_card(conn, GUILD, NEWCOMER + 1, 110.0)
        _tick_all(conn, partial_id, at=200.0, skip=("nsfw_role",))
        other_guild_id = svc.create_card(conn, GUILD + 1, NEWCOMER + 2, 120.0)
        _tick_all(conn, other_guild_id, at=200.0)
        already = svc.create_card(conn, GUILD, NEWCOMER + 3, 130.0)
        _tick_all(conn, already, at=200.0)
        svc.resolve_card(conn, already, GREETER, 250.0, svc.RESOLUTION_DISMISSED)

        found = [int(c["id"]) for c in svc.finished_open_cards(conn, GUILD)]
        assert found == [done_id]


def test_swept_backlog_lands_in_the_report(db_path):
    """The point of the sweep: the two completion-keyed panels get data."""
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _tick_all(conn, cid, at=400.0)
        assert svc.report_outcomes(conn, GUILD, 0.0)["counts"] == {}
        assert svc.report_welcomers(conn, GUILD, 0.0)[0]["completions"] == 0
        assert svc.report_skipped_steps(conn, GUILD, 0.0) == []

        for card in svc.finished_open_cards(conn, GUILD):
            svc.autocomplete_if_finished(conn, int(card["id"]))

        assert svc.report_open_cards(conn, GUILD) == []
        outcomes = svc.report_outcomes(conn, GUILD, 0.0)
        assert outcomes["counts"] == {svc.RESOLUTION_COMPLETED: 1}
        # 100.0 created → 400.0 last tick; the sweep's own clock never counts.
        assert outcomes["median_seconds"] == 300.0
        assert svc.report_welcomers(conn, GUILD, 0.0)[0]["completions"] == 1
        assert [s["skipped"] for s in svc.report_skipped_steps(conn, GUILD, 0.0)] == [
            0,
            0,
            0,
            0,
        ]


def test_complete_without_open_card(db_path):
    with open_db(db_path) as conn:
        assert svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 300.0) is None


def test_close_for_member_on_leave(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.set_card_message(conn, cid, CHANNEL, 12345)
        card = svc.close_for_member(
            conn, GUILD, NEWCOMER, svc.RESOLUTION_LEFT, 0, 200.0
        )
        # Pre-close snapshot keeps the message location for the edit.
        assert card["message_id"] == 12345
        assert svc.get_card(conn, cid)["resolution"] == svc.RESOLUTION_LEFT
        assert svc.close_for_member(
            conn, GUILD, NEWCOMER, svc.RESOLUTION_BANNED, 0, 300.0
        ) is None


# ── message evaluation (greet + step/completion codes) ────────────────


def _eval(conn, **kw):
    defaults = dict(
        channel_id=GREET_CHANNEL,
        content="welcome!",
        mentioned_ids=[NEWCOMER],
        author_is_greeter=True,
        author_is_mod=False,
    )
    defaults.update(kw)
    return svc.evaluate_message(conn, GUILD, **defaults)


def test_evaluate_message_dark_or_no_mentions(db_path):
    with open_db(db_path) as conn:
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Feature dark → nothing, even from a greeter in the right channel.
        assert _eval(conn) == []
        _enable(conn)
        assert _eval(conn, mentioned_ids=[]) == []


def test_evaluate_message_greet_gating(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn) == [(svc.ACTION_GREET, NEWCOMER, "")]
        # Wrong channel → no greet; a plain member saying hi → no greet.
        assert _eval(conn, channel_id=999) == []
        assert _eval(conn, author_is_greeter=False) == []
        # Mentioning someone without an open card → nothing.
        assert _eval(conn, mentioned_ids=[12345]) == []


def test_evaluate_message_greets_where_the_members_are(db_path):
    # Prod bug (card #3, 2026-07-29): the rule watched the channel the CARDS
    # post to while the greeting happened in #welcome-chat, so `greeted` could
    # never tick. Greeting is watched where newcomers are talked to.
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, channel_id=GREET_CHANNEL) == [
            (svc.ACTION_GREET, NEWCOMER, "")
        ]
        # The card channel is the bot's noticeboard, not a greeting place.
        assert _eval(conn, channel_id=CHANNEL) == []


def test_evaluate_message_mods_greet_too(db_path):
    # A mod welcoming someone is a greeting; the greeter role isn't a
    # prerequisite for saying hello (it is for nothing else here either).
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, author_is_greeter=False, author_is_mod=True) == [
            (svc.ACTION_GREET, NEWCOMER, "")
        ]


def test_evaluate_message_no_greet_channel_configured(db_path):
    # Guild with neither greeter chat nor a welcome channel: nothing greets,
    # and channel 0 must not match the unset config.
    with open_db(db_path) as conn:
        _enable(conn, greet_channel=0)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, channel_id=GREET_CHANNEL) == []
        assert _eval(conn, channel_id=0) == []


def test_evaluate_message_completion_code(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        set_config_value(conn, svc.CODE_KEY, "DK-7734", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Code + mention from a greeter, any channel → complete beats greet.
        assert _eval(conn, content="all set, dk-7734!", channel_id=999) == [
            (svc.ACTION_COMPLETE, NEWCOMER, "")
        ]
        # A mod who isn't a greeter can also complete…
        assert _eval(
            conn,
            content="dk-7734",
            author_is_greeter=False,
            author_is_mod=True,
            channel_id=999,
        ) == [(svc.ACTION_COMPLETE, NEWCOMER, "")]
        # …but a regular member saying the code does nothing.
        assert _eval(
            conn, content="dk-7734", author_is_greeter=False, channel_id=999
        ) == []


def test_evaluate_message_no_code_configured_never_completes(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Without a configured code, a chatty message still only greets.
        assert _eval(conn, content="dk-7734") == [(svc.ACTION_GREET, NEWCOMER, "")]


def test_evaluate_message_dedupes_mentions_keeps_order(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        set_config_value(conn, svc.CODE_KEY, "DK-7734", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.create_card(conn, GUILD, 8, 100.0)
        actions = _eval(
            conn, content="dk-7734", mentioned_ids=[8, NEWCOMER, 8]
        )
        assert actions == [
            (svc.ACTION_COMPLETE, 8, ""),
            (svc.ACTION_COMPLETE, NEWCOMER, ""),
        ]


def _use_coded_steps(conn):
    """Steps where the two question phases carry their own codes."""
    set_config_value(
        conn,
        svc.STEPS_KEY,
        json.dumps(
            [
                {"key": "greeted", "label": "Greeted", "auto": "greeted"},
                {"key": "sfw_questions", "label": "SFW questions", "code": "DK-SFW"},
                {"key": "nsfw_questions", "label": "NSFW questions", "code": "DK-NSFW"},
            ]
        ),
        GUILD,
    )


def test_evaluate_message_step_code_ticks_one_step_any_channel(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Any channel, case-insensitive, and the card is NOT closed.
        assert _eval(conn, content="done here -# dk-sfw", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "sfw_questions")
        ]
        # A mod who isn't a greeter can tick too…
        assert _eval(
            conn,
            content="DK-NSFW",
            channel_id=999,
            author_is_greeter=False,
            author_is_mod=True,
        ) == [(svc.ACTION_STEP, NEWCOMER, "nsfw_questions")]
        # …but a regular member posting the code does nothing.
        assert (
            _eval(
                conn, content="DK-NSFW", channel_id=999, author_is_greeter=False
            )
            == []
        )


def test_evaluate_message_greeting_may_also_carry_a_step_code(db_path):
    # The canned greeting is both a greet and (optionally) a coded message —
    # unlike completion, a step code doesn't suppress the greet.
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, content="hi! dk-sfw") == [
            (svc.ACTION_GREET, NEWCOMER, ""),
            (svc.ACTION_STEP, NEWCOMER, "sfw_questions"),
        ]


def test_evaluate_message_completion_code_beats_step_codes(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        set_config_value(conn, svc.CODE_KEY, "DK-DONE", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # The card is closing and stamps the rest skipped, so ticking a step
        # first would be noise.
        assert _eval(conn, content="dk-sfw dk-done", channel_id=999) == [
            (svc.ACTION_COMPLETE, NEWCOMER, "")
        ]


def test_evaluate_message_one_message_may_tick_several_steps(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, content="DK-SFW and DK-NSFW", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "sfw_questions"),
            (svc.ACTION_STEP, NEWCOMER, "nsfw_questions"),
        ]


def test_evaluate_message_uncoded_steps_never_match(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # "greeted" has no code; an empty code must never match a message,
        # or every message would tick every uncoded step.
        assert _eval(conn, content="nothing special", channel_id=999) == []


def test_step_code_tick_lands_on_the_card(db_path):
    # The service-level effect the cog wires up: ACTION_STEP -> set_step_state
    # on the open card, and re-posting the code is a no-op.
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        (_, uid, key), = _eval(conn, content="dk-sfw", channel_id=999)
        assert svc.set_step_state(
            conn, cid, key, done=True, actor_id=GREETER, at=200.0
        )
        row = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == key)
        assert row["done_at"] == 200.0 and row["done_by"] == GREETER
        # Already ticked → no second write, so the original ticker stands.
        assert not svc.set_step_state(
            conn, cid, key, done=True, actor_id=999, at=300.0
        )


def test_step_code_for_a_step_not_on_this_card_ticks_nothing(db_path):
    # Codes match the LIVE config but ticks land on the card's snapshot, so a
    # step added after the card was created has nothing to tick — the match
    # still happens, it just doesn't land.
    with open_db(db_path) as conn:
        _enable(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)  # default steps
        set_config_value(
            conn,
            svc.STEPS_KEY,
            json.dumps(
                [{"key": "brand_new", "label": "Added later", "code": "DK-NEW"}]
            ),
            GUILD,
        )
        assert _eval(conn, content="dk-new", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "brand_new")
        ]
        assert not svc.set_step_state(
            conn, cid, "brand_new", done=True, actor_id=GREETER, at=200.0
        )


def test_inviter_for(db_path):
    from bot_modules.invites.tracker import record_invite

    with open_db(db_path) as conn:
        assert svc.inviter_for(conn, GUILD, NEWCOMER) is None
        record_invite(conn, GUILD, GREETER, NEWCOMER, "abc123", joined_at=100.0)
        assert svc.inviter_for(conn, GUILD, NEWCOMER) == GREETER


def test_greeter_role_id_getter(db_path):
    with open_db(db_path) as conn:
        assert svc.greeter_role_id(conn, GUILD) == 0
        set_config_value(conn, svc.GREETER_ROLE_KEY, "4242", GUILD)
        assert svc.greeter_role_id(conn, GUILD) == 4242


# ── stale scan ────────────────────────────────────────────────────────


def test_stale_after_default_window_and_tick_resets_clock(db_path):
    hour = 3600.0
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        assert svc.stale_cards(conn, GUILD, 23 * hour) == []
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 25 * hour)] == [cid]
        # Any progress resets the clock…
        svc.set_step_state(
            conn, cid, "sfw_questions", done=True, actor_id=GREETER, at=10 * hour
        )
        assert svc.stale_cards(conn, GUILD, 25 * hour) == []
        # …until the window passes again after the last tick.
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 35 * hour)] == [cid]


def test_stale_skips_nudged_and_resolved(db_path):
    hour = 3600.0
    with open_db(db_path) as conn:
        first = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        second = svc.create_card(conn, GUILD, 8, 0.0)
        svc.mark_nudged(conn, first, 25 * hour)
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 26 * hour)] == [second]
        svc.resolve_card(conn, second, GREETER, 26 * hour, svc.RESOLUTION_DISMISSED)
        assert svc.stale_cards(conn, GUILD, 27 * hour) == []


def test_stale_respects_configured_hours(db_path):
    hour = 3600.0
    with open_db(db_path) as conn:
        set_config_value(conn, svc.STALE_HOURS_KEY, "2", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        assert svc.stale_cards(conn, GUILD, 1 * hour) == []
        assert len(svc.stale_cards(conn, GUILD, 3 * hour)) == 1


def test_stale_skips_fully_completed_cards(db_path):
    """All steps done = complete-awaiting-closure, not stalled: the nudge
    asks greeters to pick work up, and there is none. (Live incident: card 8
    got a 24h "no progress" ping a day after its last step ticked.)"""
    hour = 3600.0
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        keys = [
            str(r["step_key"])
            for r in conn.execute(
                "SELECT step_key FROM intake_card_steps WHERE card_id = ? "
                "ORDER BY position",
                (cid,),
            )
        ]
        for key in keys[:-1]:
            svc.set_step_state(conn, cid, key, done=True, actor_id=GREETER, at=hour)
        # One pending step -> still nudgeable once the window passes.
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 30 * hour)] == [cid]
        svc.set_step_state(conn, cid, keys[-1], done=True, actor_id=GREETER, at=2 * hour)
        # Nothing left to tick -> never stale, however long it sits open.
        assert svc.stale_cards(conn, GUILD, 40 * hour) == []
        # A skipped remainder counts as not-pending too (same rule the
        # progress bar applies), not as work awaiting a greeter.
        other = svc.create_card(conn, GUILD, 8, 0.0)
        svc.set_step_state(conn, other, keys[0], done=True, actor_id=GREETER, at=hour)
        conn.execute(
            "UPDATE intake_card_steps SET skipped = 1 "
            "WHERE card_id = ? AND done_at IS NULL",
            (other,),
        )
        assert svc.stale_cards(conn, GUILD, 40 * hour) == []


# ── reports ───────────────────────────────────────────────────────────


def test_report_open_cards_progress_and_order(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        second = svc.create_card(conn, GUILD, 8, 50.0)
        first = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert first is not None and second is not None
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0, actor_id=GREETER)
        svc.mark_nudged(conn, second, 300.0)
        report = svc.report_open_cards(conn, GUILD)
        # Oldest first; progress and pending labels per card.
        assert [r["user_id"] for r in report] == [8, NEWCOMER]
        assert report[0]["nudged"] is True
        assert report[1] == {
            "user_id": NEWCOMER,
            "user_name": str(NEWCOMER),  # no known_users row → id as name
            "stage": svc.STAGE_JOIN,
            "created_at": 100.0,
            "nudged": False,
            "done": 1,
            "total": 4,
            "pending": ["Member role", "SFW questions asked", "NSFW access"],
        }


def test_report_outcomes_stats(db_path):
    with open_db(db_path) as conn:
        for uid, created, resolved in [(1, 0.0, 100.0), (2, 0.0, 300.0)]:
            svc.create_card(conn, GUILD, uid, created)
            svc.complete_card(conn, GUILD, uid, GREETER, resolved)
        cid = svc.create_card(conn, GUILD, 3, 0.0)
        svc.resolve_card(conn, cid, GREETER, 50.0, svc.RESOLUTION_DISMISSED)
        svc.create_card(conn, GUILD, 4, 0.0)  # still open — not in outcomes
        out = svc.report_outcomes(conn, GUILD, 0.0)
        assert out["resolved"] == 3
        assert out["counts"] == {"completed": 2, "dismissed": 1}
        assert out["mean_seconds"] == 200.0
        assert out["median_seconds"] == 200.0
        # Window: nothing created since ts 10 → empty.
        assert svc.report_outcomes(conn, GUILD, 10.0)["resolved"] == 0


def test_report_welcomers_counts_ticks_not_auto(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        # A human tick, an auto tick (done_by 0 — never credited), a completion.
        svc.set_step_state(conn, cid, "sfw_questions", done=True, actor_id=GREETER, at=10.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_VERIFIED, 20.0)
        svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 30.0)
        report = svc.report_welcomers(conn, GUILD, 0.0)
        assert report == [
            {
                "user_id": GREETER,
                "user_name": str(GREETER),
                "completions": 1,
                "ticks": 1,
            }
        ]


def test_report_skipped_steps(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 10.0, actor_id=GREETER)
        svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 20.0)
        rows = svc.report_skipped_steps(conn, GUILD, 0.0)
        assert [(r["key"], r["appeared"], r["skipped"]) for r in rows] == [
            ("greeted", 1, 0),
            ("member_role", 1, 1),
            ("sfw_questions", 1, 1),
            ("nsfw_role", 1, 1),
        ]


# ── watch registry ────────────────────────────────────────────────────


def test_warm_seeds_open_cards_even_when_disabled(db_path):
    # Regression: warm() used to skip disabled guilds, stranding open cards
    # as zombies across a disable → restart → enable cycle (no auto-ticks,
    # no leave/ban close). Open cards are watched regardless of the flag.
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.create_card(conn, 99, 5, 100.0)  # guild 99 is dark but carded
    svc.warm(db_path, [GUILD, 99])
    assert svc.is_watched(GUILD, NEWCOMER) is True
    assert svc.is_watched(99, 5) is True


def test_warm_excludes_resolved_cards(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.create_card(conn, GUILD, 8, 100.0)
        svc.resolve_card(conn, cid, GREETER, 200.0, svc.RESOLUTION_DISMISSED)
    svc.warm(db_path, [GUILD])
    assert svc.is_watched(GUILD, NEWCOMER) is False
    assert svc.is_watched(GUILD, 8) is True


def test_add_and_discard_watch(db_path):
    svc.add_watched(GUILD, 10)
    assert svc.is_watched(GUILD, 10) is True
    svc.discard(GUILD, 10)
    assert svc.is_watched(GUILD, 10) is False
    svc.discard(GUILD, 999)  # unknown member is harmless


# ── two stages: grant steps + the Level 5 card ────────────────────────
#
# docs/plans/intake-two-stage.md: the join card is stage 1; a Level 5 Grant
# press opens a stage 2 card with its own step list, closing any open join
# card as `advanced` so a member never holds two open cards.

APPROVER = 601
STAGE2_STEPS = [
    {"key": "nsfw_granted", "label": "NSFW granted", "auto": "grant", "role_id": NSFW_ROLE},
    {"key": "questions_finished", "label": "Questions finished", "code": "DK-DONE2"},
]


def _use_stage2_steps(conn, steps=STAGE2_STEPS):
    set_config_value(conn, svc.STAGE2_STEPS_KEY, json.dumps(steps), GUILD)


def _step(conn, card_id, key):
    return next(s for s in svc.steps_for(conn, card_id) if s["step_key"] == key)


def test_parse_steps_accepts_grant_kind():
    steps = svc.parse_steps(json.dumps(STAGE2_STEPS), svc.DEFAULT_STAGE2_STEPS)
    assert [(s.key, s.auto_kind, s.auto_role_id) for s in steps] == [
        ("nsfw_granted", svc.AUTO_GRANT, NSFW_ROLE),
        ("questions_finished", "", 0),
    ]


@pytest.mark.parametrize(
    "stored, expected_keys",
    [
        pytest.param(None, ["questions_finished"], id="unset-defaults"),
        pytest.param("not json", ["questions_finished"], id="garbage-defaults"),
        pytest.param(json.dumps(STAGE2_STEPS), ["nsfw_granted", "questions_finished"], id="custom"),
    ],
)
def test_stage2_step_config(db_path, stored, expected_keys):
    with open_db(db_path) as conn:
        if stored is not None:
            set_config_value(conn, svc.STAGE2_STEPS_KEY, stored, GUILD)
        assert [s.key for s in svc.step_config(conn, GUILD, svc.STAGE_LEVEL5)] == expected_keys
        # Stage 1 is untouched by the stage 2 key.
        assert [s.key for s in svc.step_config(conn, GUILD)] == [
            s.key for s in svc.DEFAULT_STEPS
        ]


def test_coded_step_keys_follow_the_live_config_per_stage(db_path):
    # The card draws no button for a step a code ticks (Ben, 2026-09-30).
    # The set is read from the live config, per stage, so an admin giving a
    # step a code (or clearing it) changes the buttons at the next render
    # without touching the card's step rows. The defaults carry no codes.
    with open_db(db_path) as conn:
        assert svc.coded_step_keys(conn, GUILD) == frozenset()
        assert svc.coded_step_keys(conn, GUILD, svc.STAGE_LEVEL5) == frozenset()
        _use_coded_steps(conn)  # greeted (auto, no code), DK-SFW, DK-NSFW
        _use_stage2_steps(conn)  # nsfw_granted (grant, no code), DK-DONE2
        assert svc.coded_step_keys(conn, GUILD) == {"sfw_questions", "nsfw_questions"}
        assert svc.coded_step_keys(conn, GUILD, svc.STAGE_LEVEL5) == {"questions_finished"}
        # A blank code is "off": the step gets its Done button back.
        _use_stage2_steps(
            conn, [{"key": "questions_finished", "label": "Questions finished", "code": " "}]
        )
        assert svc.coded_step_keys(conn, GUILD, svc.STAGE_LEVEL5) == frozenset()


@pytest.mark.parametrize(
    ("key", "coded_now", "takes_a_press"),
    [
        pytest.param("sfw_questions", False, True, id="manual-no-code"),
        pytest.param("sfw_questions", True, False, id="manual-with-code"),
        pytest.param("greeted", False, False, id="auto"),
        pytest.param("greeted", True, False, id="auto-beside-coded"),
    ],
)
def test_step_takes_a_press_refuses_auto_and_coded_steps(db_path, key, coded_now, takes_a_press):
    # A Done/Undo button outlives its reason: a card drawn before the step
    # got its code (or before coded steps lost their buttons) keeps the
    # button until it redraws. The press handler asks the live config, so a
    # stale press neither ticks the step the code owns nor pays for it, and
    # clearing the code makes the step pressable again.
    with open_db(db_path) as conn:
        _use_custom_steps(conn)  # greeted (auto) + sfw_questions (manual), no codes
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert cid is not None
        if coded_now:
            _use_coded_steps(conn)  # sfw_questions now ticks from DK-SFW
        step = _step(conn, cid, key)
        assert svc.step_takes_a_press(conn, GUILD, svc.STAGE_JOIN, step) is takes_a_press
        _use_custom_steps(conn)  # codes cleared again
        assert svc.step_takes_a_press(conn, GUILD, svc.STAGE_JOIN, step) is (key != "greeted")


def test_stage2_default_steps_are_manual_so_they_stay_tickable():
    # A grant step with no role would have no button: the defaults must be
    # manual until the dashboard stores a role.
    assert all(s.auto_kind == "" for s in svc.DEFAULT_STAGE2_STEPS)


def test_stage2_ships_dark_and_needs_intake_on(db_path):
    with open_db(db_path) as conn:
        assert svc.stage2_enabled(conn, GUILD) is False
        set_config_value(conn, svc.STAGE2_ENABLED_KEY, "1", GUILD)
        # Its own dial on, intake itself still off.
        assert svc.stage2_enabled(conn, GUILD) is False
        _enable(conn)
        assert svc.stage2_enabled(conn, GUILD) is True
        set_config_value(conn, svc.STAGE2_ENABLED_KEY, "0", GUILD)
        assert svc.stage2_enabled(conn, GUILD) is False


def test_existing_and_new_join_cards_are_stage_1(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        card = svc.get_card(conn, cid)
        assert svc.card_stage(card) == svc.STAGE_JOIN
        assert svc.card_opener(card) == 0  # a join card has no approver


def test_advance_with_no_join_card_opens_stage_2(db_path):
    # Veterans who joined before intake cards existed still get Stage 2.
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        closed, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        assert closed is None
        card = svc.get_card(conn, cid)
        assert svc.card_stage(card) == svc.STAGE_LEVEL5
        # The built-in approval line leads, ticked by the approver. No grant
        # role is passed, so there is no built-in NSFW line.
        assert [s["step_key"] for s in svc.steps_for(conn, cid)] == [
            svc.APPROVED_STEP, "nsfw_granted", "questions_finished",
        ]
        approved = _step(conn, cid, svc.APPROVED_STEP)
        assert (approved["done_by"], approved["done_at"]) == (APPROVER, 500.0)
        # Stored, not passed through: a V2 card is rebuilt whole on every
        # edit, and its header names the approver each time.
        assert svc.card_opener(card) == APPROVER


def test_configured_step_sharing_a_built_in_key_is_dropped(db_path):
    # A step labelled "NSFW access" slugs to the built-in key; two rows with
    # one key would tick together, so the built-in line wins.
    with open_db(db_path) as conn:
        set_config_value(
            conn,
            svc.STAGE2_STEPS_KEY,
            json.dumps([{"key": svc.NSFW_GRANT_STEP, "label": "NSFW access"}]),
            GUILD,
        )
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0, 777)
        keys = [s["step_key"] for s in svc.steps_for(conn, cid)]
        assert keys == [svc.APPROVED_STEP, svc.NSFW_GRANT_STEP]


def test_advance_closes_an_open_join_card_as_advanced(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        _use_stage2_steps(conn)
        join = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.set_step_state(conn, join, "greeted", done=True, actor_id=GREETER, at=150.0)
        closed, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        assert int(closed["id"]) == join
        old = svc.get_card(conn, join)
        assert old["resolution"] == svc.RESOLUTION_ADVANCED
        assert old["resolved_by"] == APPROVER
        assert old["resolved_at"] == 500.0
        steps = {s["step_key"]: s for s in svc.steps_for(conn, join)}
        assert steps["greeted"]["skipped"] == 0  # done stays done
        assert all(steps[k]["skipped"] == 1 for k in ("member_role", "sfw_questions", "nsfw_role"))
        # The member's one open card is now the stage 2 card.
        assert int(svc.get_open_card(conn, GUILD, NEWCOMER)["id"]) == cid


def test_second_level5_press_opens_nothing(db_path):
    with open_db(db_path) as conn:
        _, first = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        assert svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 600.0) == (None, None)
        assert svc.get_card(conn, first)["resolved_at"] is None


def test_leave_closes_a_stage_2_card(db_path):
    with open_db(db_path) as conn:
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        closed = svc.close_for_member(conn, GUILD, NEWCOMER, svc.RESOLUTION_LEFT, 0, 600.0)
        assert int(closed["id"]) == cid
        assert svc.get_open_card(conn, GUILD, NEWCOMER) is None


def test_role_gained_ticks_a_grant_step_as_auto(db_path):
    # Giving the role by hand (or /grant) is the step done, credited "auto".
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 510.0, role_id=MEMBER_ROLE
        )
        assert ticked == []  # a different role ticks nothing
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 520.0, role_id=NSFW_ROLE
        )
        assert ticked == ["nsfw_granted"]
        assert _step(conn, cid, "nsfw_granted")["done_by"] == svc.AUTO_ACTOR


def test_one_role_event_ticks_every_row_waiting_on_that_role(db_path):
    # A role row and a grant row may name the same role. The go-live join list
    # does: "Found their way in" waits on the main-chat role and "Main chat"
    # is the button that gives it. One role event ticks both as auto, and the
    # press's claim then credits the pressed row alone.
    steps = [
        {"key": "found_way_in", "label": "Found their way in",
         "auto": "role_gained", "role_id": MEMBER_ROLE},
        {"key": "main_chat", "label": "Main chat", "auto": "grant", "role_id": MEMBER_ROLE},
        {"key": "sfw_questions", "label": "SFW questions finished", "code": "DK-SFW"},
    ]
    with open_db(db_path) as conn:
        set_config_value(conn, svc.STEPS_KEY, json.dumps(steps), GUILD)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 500.0)
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 510.0, role_id=MEMBER_ROLE
        )
        assert sorted(ticked) == ["found_way_in", "main_chat"]
        assert svc.claim_grant_tick(conn, cid, "main_chat", GREETER, 520.0) is True
        assert _step(conn, cid, "main_chat")["done_by"] == GREETER
        assert _step(conn, cid, "found_way_in")["done_by"] == svc.AUTO_ACTOR
        assert _step(conn, cid, "sfw_questions")["done_at"] is None


@pytest.mark.parametrize(
    "setup, expected",
    [
        pytest.param("open", svc.GRANT_OK, id="ok"),
        pytest.param("done", svc.GRANT_DONE, id="already-ticked"),
        pytest.param("manual", svc.GRANT_NOT_GRANT, id="manual-step"),
        pytest.param("unknown", svc.GRANT_CLOSED, id="unknown-step"),
        pytest.param("resolved", svc.GRANT_CLOSED, id="closed-card"),
        pytest.param("missing", svc.GRANT_CLOSED, id="no-card"),
    ],
)
def test_grant_step_state(db_path, setup, expected):
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        key = "nsfw_granted"
        if setup == "done":
            svc.set_step_state(conn, cid, key, done=True, actor_id=0, at=510.0)
        elif setup == "manual":
            key = "questions_finished"
        elif setup == "unknown":
            key = "nope"
        elif setup == "resolved":
            svc.resolve_card(conn, cid, APPROVER, 510.0, svc.RESOLUTION_DISMISSED)
        elif setup == "missing":
            cid = 99999
        status, _, _ = svc.grant_step_state(conn, cid, key)
        assert status == expected


def test_grant_step_without_a_role_is_not_grantable(db_path):
    with open_db(db_path) as conn:
        _use_stage2_steps(conn, [{"key": "g", "label": "G", "auto": "grant", "role_id": 0}])
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        assert svc.grant_step_state(conn, cid, "g")[0] == svc.GRANT_NOT_GRANT


def test_claim_grant_tick_credits_the_presser(db_path):
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        assert svc.claim_grant_tick(conn, cid, "nsfw_granted", GREETER, 510.0) is True
        step = _step(conn, cid, "nsfw_granted")
        assert (step["done_by"], step["done_at"]) == (GREETER, 510.0)


def test_claim_grant_tick_overwrites_the_gateway_auto_tick(db_path):
    # The role landing fires the role-update hook, which may tick the step as
    # auto before the press returns; the presser must still get the credit.
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 505.0, role_id=NSFW_ROLE)
        assert svc.claim_grant_tick(conn, cid, "nsfw_granted", GREETER, 510.0) is True
        step = _step(conn, cid, "nsfw_granted")
        assert (step["done_by"], step["done_at"]) == (GREETER, 505.0)


def test_claim_grant_tick_leaves_a_human_tick_alone(db_path):
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        svc.claim_grant_tick(conn, cid, "nsfw_granted", GREETER, 510.0)
        assert svc.claim_grant_tick(conn, cid, "nsfw_granted", 777, 520.0) is False
        assert _step(conn, cid, "nsfw_granted")["done_by"] == GREETER


def test_evaluate_message_matches_codes_from_the_cards_own_stage(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)  # stage 1 codes: DK-SFW, DK-NSFW
        _use_stage2_steps(conn)  # stage 2 code: DK-DONE2
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # A stage 2 code means nothing to a join card…
        assert _eval(conn, content="DK-DONE2", channel_id=999) == []
        svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        # …and once on stage 2, stage 1 codes mean nothing.
        assert _eval(conn, content="DK-SFW", channel_id=999) == []
        assert _eval(conn, content="all done -# dk-done2", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "questions_finished")
        ]


def test_stage2_code_closes_the_card_when_it_is_the_last_step(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_stage2_steps(conn)
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0)
        svc.claim_grant_tick(conn, cid, "nsfw_granted", GREETER, 510.0)
        svc.set_step_state(conn, cid, "questions_finished", done=True, actor_id=GREETER, at=520.0)
        closed = svc.autocomplete_if_finished(conn, cid, actor_id=GREETER)
        assert closed is not None
        assert svc.get_card(conn, cid)["resolution"] == svc.RESOLUTION_COMPLETED


@pytest.mark.parametrize(
    "stage1_code, stage2_code, completion, clash",
    [
        pytest.param("DK-SFW", "DK-DONE2", "", False, id="distinct"),
        pytest.param("DK", "DK-DONE2", "", True, id="stage2-contains-stage1"),
        pytest.param("DK-SFW", "WELCOME", "WELCOME-IN", True, id="completion-contains-stage2"),
    ],
)
def test_config_code_conflict_spans_both_stages(db_path, stage1_code, stage2_code, completion, clash):
    with open_db(db_path) as conn:
        found = svc.config_code_conflict(
            conn,
            GUILD,
            completion=completion,
            steps=[svc.StepDef("sfw", "SFW", code=stage1_code)],
            stage2_steps=[svc.StepDef("done", "Done", code=stage2_code)],
        )
        assert (found is not None) is clash


def test_config_code_conflict_reads_stored_stage2_codes(db_path):
    # A stage 1 save must still be checked against the stored stage 2 codes.
    with open_db(db_path) as conn:
        _use_stage2_steps(conn)
        found = svc.config_code_conflict(
            conn, GUILD, steps=[svc.StepDef("x", "X", code="DK-DONE")]
        )
        assert found is not None
        assert found[1] == ("X", "DK-DONE")


def test_report_open_cards_carries_the_stage(db_path):
    with open_db(db_path) as conn:
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.advance_to_stage2(conn, GUILD, 8, APPROVER, 200.0)
        stages = {c["user_id"]: c["stage"] for c in svc.report_open_cards(conn, GUILD)}
        assert stages == {NEWCOMER: 1, 8: 2}


# ── Stage 2 NSFW opt-in (Ben, 2026-09-28) ─────────────────────────────

OPTIN = 4242


@pytest.mark.parametrize(
    "optin, roles, exists, verdict",
    [
        pytest.param(0, set(), False, svc.OPTIN_OPEN, id="unset-opens-for-everyone"),
        pytest.param(OPTIN, {OPTIN, 1}, True, svc.OPTIN_OPEN, id="opted-in-opens"),
        pytest.param(OPTIN, {1}, True, svc.OPTIN_NOT_OPTED_IN, id="not-opted-in"),
        # Fails closed: a deleted opt-in role must not widen Stage 2 to all.
        pytest.param(OPTIN, {OPTIN}, False, svc.OPTIN_ROLE_MISSING, id="deleted-role"),
    ],
)
def test_stage2_optin_verdict(optin, roles, exists, verdict):
    assert svc.stage2_optin_verdict(optin, roles, exists) == verdict


# The Level 5 press used to answer "Access granted." whatever Stage 2 did, so
# a skipped Stage 2 went unnoticed (2026-09-30). Since 2026-10-01 it grants
# nothing while Stage 2 is on, so every outcome but "opened" says so.
@pytest.mark.parametrize(
    "outcome, says",
    [
        pytest.param(svc.STAGE2_OPENED, "Approved. Stage 2 card opened", id="opened"),
        pytest.param(svc.STAGE2_ALREADY_OPEN, "already open", id="already-open"),
        pytest.param(svc.STAGE2_NOT_OPTED_IN, "don't have the NSFW opt-in role", id="not-opted-in"),
        pytest.param(svc.STAGE2_ROLE_MISSING, "opt-in role is gone", id="role-missing"),
        pytest.param(svc.STAGE2_NO_CHANNEL, "card channel is gone", id="no-channel"),
        pytest.param(svc.STAGE2_NO_GRANT, "no grant role is set", id="nothing-to-grant"),
        pytest.param(svc.STAGE2_FAILED, "couldn't be posted", id="failed"),
        pytest.param("unheard-of", "unexpected outcome", id="unknown"),
    ],
)
def test_stage2_press_reply_names_the_outcome(outcome, says):
    reply = svc.stage2_press_reply(outcome)
    assert says in reply
    # Only an opened card is an approval; nothing else grants anything.
    assert reply.startswith("Nothing") is (outcome != svc.STAGE2_OPENED)


SPICY = 903


@pytest.mark.parametrize(
    "configured, grant_role, keys",
    [
        pytest.param([], SPICY, [svc.APPROVED_STEP, svc.NSFW_GRANT_STEP], id="both-built-ins"),
        pytest.param([], 0, [svc.APPROVED_STEP], id="no-grant-role-no-grant-line"),
        pytest.param(
            [svc.StepDef("g", "NSFW", svc.AUTO_GRANT, SPICY)], SPICY, [svc.APPROVED_STEP],
            id="configured-grant-of-same-role-not-doubled",
        ),
        pytest.param(
            [svc.StepDef("g", "Other", svc.AUTO_GRANT, 77)], SPICY,
            [svc.APPROVED_STEP, svc.NSFW_GRANT_STEP], id="other-role-grant-keeps-built-in",
        ),
    ],
)
def test_stage2_lead_steps(configured, grant_role, keys):
    lead = svc.stage2_lead_steps(configured, grant_role)
    assert [s.key for s in lead] == keys
    assert lead[0].auto_kind == svc.AUTO_APPROVED
    if svc.NSFW_GRANT_STEP in keys:
        assert (lead[1].auto_kind, lead[1].auto_role_id) == (svc.AUTO_GRANT, SPICY)


@pytest.mark.parametrize(
    "configured, grant_role, grants",
    [
        pytest.param([], SPICY, True, id="built-in-grant"),
        pytest.param([svc.StepDef("g", "NSFW", svc.AUTO_GRANT, 77)], 0, True, id="configured-grant"),
        pytest.param([svc.StepDef("q", "Questions")], 0, False, id="nothing-to-grant"),
    ],
)
def test_stage2_grants_a_role(configured, grant_role, grants):
    lead = svc.stage2_lead_steps(configured, grant_role)
    assert svc.stage2_grants_a_role(lead, configured) is grants


def test_stage2_card_leads_with_approval_then_nsfw_access(db_path):
    with open_db(db_path) as conn:
        _use_stage2_steps(conn, [STAGE2_STEPS[1]])
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0, SPICY)
        steps = svc.steps_for(conn, cid)
        assert [s["step_key"] for s in steps] == [
            svc.APPROVED_STEP, svc.NSFW_GRANT_STEP, "questions_finished",
        ]
        # Approval is on the record already; the grant waits for a greeter.
        assert steps[0]["done_by"] == APPROVER
        assert steps[1]["done_at"] is None
        assert svc.grant_step_state(conn, cid, svc.NSFW_GRANT_STEP)[0] == svc.GRANT_OK
        # The approval line can't be pressed or undone.
        assert svc.step_takes_a_press(conn, GUILD, svc.STAGE_LEVEL5, steps[0]) is False


def test_role_landing_ticks_the_built_in_nsfw_line(db_path):
    # A member who already holds the role (or gets it by /grant) has the line
    # ticked by the role hook, as any grant step is.
    with open_db(db_path) as conn:
        _use_stage2_steps(conn, [STAGE2_STEPS[1]])
        _, cid = svc.advance_to_stage2(conn, GUILD, NEWCOMER, APPROVER, 500.0, SPICY)
        _card, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 510.0, role_id=SPICY
        )
        assert ticked == [svc.NSFW_GRANT_STEP]


def test_stage2_optin_role_id_reads_the_dial(db_path):
    with open_db(db_path) as conn:
        assert svc.stage2_optin_role_id(conn, GUILD) == 0
        set_config_value(conn, svc.STAGE2_OPTIN_ROLE_KEY, str(OPTIN), GUILD)
        assert svc.stage2_optin_role_id(conn, GUILD) == OPTIN
