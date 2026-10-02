"""Tests for the ``/info`` panel's shaping layer.

The panel's whole job is deciding what a member is told and which buttons
they get, so that decision is what is tested: every feature's configured /
unconfigured branch, every opt-in state, the "configured but the cog isn't
loaded" degradation, and the two filters that exist for privacy reasons
(top-channel visibility, and the deliberate absence of a no-contact count).
"""

from __future__ import annotations

import pytest

from bot_modules.member_info.logic import (
    ACTION_JOIN,
    ACTION_LEAVE,
    ACTION_OPEN,
    STATE_IN,
    STATE_OUT,
    STATE_UNSET,
    FeatureState,
    build_optin_rows,
    displayable_roles,
    level_progress,
    visible_top_channels,
    xp_to_next_level,
)

ALL_KEYS = (
    "pen_pals",
    "whispers",
    "guess",
    "dm_mode",
    "wellness",
    "birthday",
    "no_contact",
    "my_data",
)


def _rows(states):
    return {row.key: row for row in build_optin_rows(states)}


# ── Unconfigured features never appear ───────────────────────────────────


def test_no_states_means_no_rows():
    assert build_optin_rows({}) == []


@pytest.mark.parametrize("key", ALL_KEYS)
def test_unconfigured_feature_is_dropped_entirely(key):
    """A feature the guild never set up must not be advertised, nor joinable."""
    rows = _rows({key: FeatureState(configured=False, state=STATE_UNSET)})
    assert key not in rows


@pytest.mark.parametrize("key", ALL_KEYS)
def test_configured_feature_appears(key):
    rows = _rows({key: FeatureState(configured=True, state=STATE_UNSET)})
    assert key in rows
    assert rows[key].text


def test_rows_keep_spec_order_regardless_of_input_order():
    states = {key: FeatureState(configured=True) for key in reversed(ALL_KEYS)}
    assert [row.key for row in build_optin_rows(states)] == list(ALL_KEYS)


# ── Per-state actions ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "state", "expected_action"),
    [
        # Joining is offered to someone who never joined *and* to someone who
        # left — leaving is a decision, not a ban, and rejoining is the undo.
        ("pen_pals", STATE_UNSET, ACTION_JOIN),
        ("pen_pals", STATE_OUT, ACTION_JOIN),
        ("pen_pals", STATE_IN, ACTION_LEAVE),
        ("whispers", STATE_UNSET, ACTION_JOIN),
        ("whispers", STATE_IN, ACTION_LEAVE),
        ("guess", STATE_UNSET, ACTION_JOIN),
        ("guess", STATE_IN, ACTION_LEAVE),
        ("birthday", STATE_UNSET, ACTION_JOIN),
        ("birthday", STATE_IN, ACTION_LEAVE),
        # These three always open their own panel — there is no one-click
        # state to flip, which is the point of routing through them.
        ("dm_mode", STATE_IN, ACTION_OPEN),
        ("wellness", STATE_IN, ACTION_OPEN),
        ("wellness", STATE_OUT, ACTION_OPEN),
        ("no_contact", STATE_UNSET, ACTION_OPEN),
        ("my_data", STATE_UNSET, ACTION_OPEN),
    ],
)
def test_action_offered_per_state(key, state, expected_action):
    rows = _rows({key: FeatureState(configured=True, state=state)})
    assert rows[key].action == expected_action
    assert rows[key].action_label


def test_pen_pals_unset_and_out_read_differently():
    """Someone who left is not told "not joined" — the bot doesn't re-pitch."""
    unset = _rows({"pen_pals": FeatureState(configured=True, state=STATE_UNSET)})
    out = _rows({"pen_pals": FeatureState(configured=True, state=STATE_OUT)})
    assert unset["pen_pals"].text != out["pen_pals"].text
    assert "left" in out["pen_pals"].text.lower()


# ── Degradation when a flow can't be reached ─────────────────────────────


@pytest.mark.parametrize("key", ALL_KEYS)
def test_not_actionable_shows_status_without_a_button(key):
    """A missing cog (or an unmet role gate) costs the button, not the row."""
    rows = _rows({key: FeatureState(configured=True, actionable=False)})
    assert key in rows
    assert rows[key].text
    assert rows[key].action is None
    assert rows[key].has_action is False


def test_detail_is_prepended_to_the_row_text():
    rows = _rows(
        {"dm_mode": FeatureState(configured=True, state=STATE_IN, detail="Currently **ask**")}
    )
    assert rows["dm_mode"].text.startswith("Currently **ask**")


# ── The no-contact row must never carry a count ──────────────────────────


def test_no_contact_row_states_are_indistinguishable():
    """Whoever is on the list, the row reads identically.

    The No Contact card hides entries the other party created against you. If
    this row's text varied with the member's own state, differencing it
    against that filtered view would leak the hidden ones back.
    """
    texts = {
        _rows({"no_contact": FeatureState(configured=True, state=state)})[
            "no_contact"
        ].text
        for state in (STATE_IN, STATE_OUT, STATE_UNSET)
    }
    assert len(texts) == 1
    assert not any(ch.isdigit() for ch in texts.pop())


# ── Top-channel visibility filter ────────────────────────────────────────


def _chan(channel_id: int, count: int):
    return {"channel_id": channel_id, "cnt": count}


def test_top_channels_drops_channels_the_member_cannot_see():
    rows = [_chan(1, 50), _chan(2, 40), _chan(3, 30)]
    assert visible_top_channels(rows, {1, 3}) == [(1, 50), (3, 30)]


def test_top_channels_backfills_from_lower_ranks_after_filtering():
    """Over-fetching is why the field isn't left empty by the filter."""
    rows = [_chan(1, 90), _chan(2, 80), _chan(3, 70), _chan(4, 60), _chan(5, 50)]
    assert visible_top_channels(rows, {4, 5}) == [(4, 60), (5, 50)]


def test_top_channels_honors_the_limit():
    rows = [_chan(i, 100 - i) for i in range(1, 10)]
    assert len(visible_top_channels(rows, set(range(1, 10)))) == 3


def test_top_channels_with_nothing_visible_is_empty():
    assert visible_top_channels([_chan(1, 5)], set()) == []


# ── Role display ─────────────────────────────────────────────────────────


def test_roles_are_capped_with_an_overflow_count():
    names, overflow = displayable_roles([f"r{i}" for i in range(20)], limit=5)
    assert names == ["r0", "r1", "r2", "r3", "r4"]
    assert overflow == 15


def test_roles_under_the_cap_have_no_overflow():
    names, overflow = displayable_roles(["a", "b"])
    assert names == ["a", "b"]
    assert overflow == 0


# ── Regression: the wellness button must not promise a read-only view ────


def test_wellness_opted_in_label_does_not_say_settings():
    """The only entry point re-runs opt-in, which resets notification prefs."""
    rows = _rows({"wellness": FeatureState(configured=True, state=STATE_IN)})
    assert "settings" not in rows["wellness"].action_label.lower()


# ── Level progress ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("total_xp", "next_level_xp", "expected"),
    [
        (12_345, 13_585, 1_240),
        (100, 100, 0),
        # The stored level lags the XP that earns it, and the curve factor is a
        # live dial — so "already past the threshold" is reachable and must not
        # render as a negative.
        (14_000, 13_585, 0),
        (0, None, None),
    ],
)
def test_xp_to_next_level_never_goes_negative(total_xp, next_level_xp, expected):
    assert xp_to_next_level(total_xp, next_level_xp) == expected


@pytest.mark.parametrize(
    ("total_xp", "floor_xp", "next_level_xp", "expected"),
    [
        pytest.param(150, 100, 200, 0.5, id="halfway-measured-from-the-floor"),
        pytest.param(100, 100, 200, 0.0, id="just-levelled"),
        # The stored level lags its XP, and the curve factor is a live dial:
        # XP can sit past the next threshold or under this level's floor.
        pytest.param(250, 100, 200, 1.0, id="past-the-threshold-clamps-full"),
        pytest.param(50, 100, 200, 0.0, id="under-the-floor-clamps-empty"),
        pytest.param(0, None, None, None, id="no-xp-row"),
        pytest.param(150, 200, 200, None, id="degenerate-span"),
    ],
)
def test_level_progress(total_xp, floor_xp, next_level_xp, expected):
    assert level_progress(total_xp, floor_xp, next_level_xp) == expected


def test_every_action_label_fits_beside_its_row():
    """A label over the accessory width drops under its row on a phone."""
    from bot_modules.core.layout import MAX_ACCESSORY_LABEL, text_columns

    for state in (STATE_IN, STATE_OUT, STATE_UNSET):
        rows = build_optin_rows(
            {key: FeatureState(configured=True, state=state) for key in ALL_KEYS}
        )
        for row in rows:
            assert text_columns(row.action_label) <= MAX_ACCESSORY_LABEL, row
