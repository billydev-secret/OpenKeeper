"""Special-roll odds: the per-guild dials for how often 69, 100 and 1 land.

The flat default must be the die every guild rolled before the dials
existed — the same ``randint`` — so shipping this changes nothing until an
admin moves a dial. The no-contact redraw must keep drawing with the dialled
odds, not a differently shaped pool. See docs/specs/risky_roll_spec.md,
"Special-roll odds", and docs/specs/no_contact_spec.md, "Risky Rolls".
"""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from bot_modules.games.risky_roll import logic
from bot_modules.games.risky_roll.logic import (
    FLAT_ODDS,
    SPECIAL_ODDS_KEYS,
    SpecialRollOdds,
    choose_roll,
    has_blocked_edge,
    normalize_special_odds,
    special_odds_valid,
)
from bot_modules.games.risky_roll.store import StateStore
from tests.paths import REPO_ROOT

ANN, BOB, CID = 1, 2, 3


def pair(a: int, b: int) -> set[tuple[int, int]]:
    return {(min(a, b), max(a, b))}


def _rate(draw, face: int, n: int = 20_000, seed: int = 7) -> float:
    random.seed(seed)
    return sum(draw() == face for _ in range(n)) / n


# ── the dials' shape ─────────────────────────────────────────────────


def test_default_odds_are_the_flat_die():
    assert FLAT_ODDS.is_flat
    assert FLAT_ODDS.face_weights() == pytest.approx([0.01] * 100)


def test_flat_odds_keep_the_plain_randint():
    # The literal draw every guild rolled before the dials: a default guild
    # must not be moved onto the weighted path.
    with patch.object(logic.random, "randint", return_value=42), \
            patch.object(logic.random, "choices", side_effect=AssertionError):
        assert choose_roll({ANN: 10}, BOB, set()) == 42


def test_each_special_face_gets_exactly_its_dialled_chance():
    weights = SpecialRollOdds(sixty_nine=5, hundred=1000, one=0).face_weights()
    assert sum(weights) == pytest.approx(1.0)
    assert weights[69 - 1] == pytest.approx(1 / 5)
    assert weights[100 - 1] == pytest.approx(1 / 1000)
    assert weights[1 - 1] == 0.0
    ordinary = [w for face, w in enumerate(weights, start=1) if face not in (1, 69, 100)]
    assert len(set(round(w, 12) for w in ordinary)) == 1  # the rest stay flat


@pytest.mark.parametrize(
    ("n", "valid"),
    [
        pytest.param(0, True, id="never"),
        pytest.param(5, True, id="commonest"),
        pytest.param(100, True, id="flat"),
        pytest.param(1000, True, id="rarest"),
        pytest.param(4, False, id="too common"),
        pytest.param(1, False, id="every roll"),
        pytest.param(1001, False, id="too rare"),
        pytest.param(-1, False, id="negative"),
    ],
)
def test_special_odds_bounds(n, valid):
    assert special_odds_valid(n) is valid


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("250", 250, id="stored text"),
        pytest.param("0", 0, id="never survives"),
        pytest.param("junk", 100, id="garbage reads as flat, not never"),
        pytest.param(None, 100, id="missing"),
        pytest.param("3", 100, id="out of range reads as flat"),
    ],
)
def test_normalize_special_odds(raw, expected):
    assert normalize_special_odds(raw) == expected


# ── the draw ─────────────────────────────────────────────────────────


def test_a_dialled_face_lands_at_its_rate():
    odds = SpecialRollOdds(sixty_nine=5)
    assert _rate(lambda: choose_roll({}, ANN, set(), odds), 69) == pytest.approx(0.2, abs=0.02)


def test_a_rarer_face_lands_less_often():
    odds = SpecialRollOdds(hundred=1000)
    assert _rate(lambda: choose_roll({}, ANN, set(), odds), 100) < 0.004


@pytest.mark.parametrize("face", [69, 100, 1])
def test_a_face_set_to_never_is_never_rolled(face):
    odds = SpecialRollOdds.from_values({face: 0})
    assert _rate(lambda: choose_roll({}, ANN, set(), odds), face, n=5_000) == 0


# ── the no-contact redraw under custom odds ──────────────────────────


def test_a_blocked_roller_still_gets_69_at_the_dialled_rate():
    # A natural 69 is always safe and kept, and a redraw only follows a
    # non-69, so the pair sees 69 exactly as often as anyone — the dial
    # cannot become a tell.
    odds = SpecialRollOdds(sixty_nine=5)
    rolls = {ANN: 10, BOB: 90}
    rate = _rate(lambda: choose_roll(rolls, CID, pair(ANN, BOB), odds), 69)
    assert rate == pytest.approx(0.2, abs=0.02)


def test_the_redraw_uses_the_dialled_weights_over_the_safe_faces():
    odds = SpecialRollOdds(hundred=5, one=10)
    weights = odds.face_weights()
    rolls = {ANN: 10, BOB: 90}
    calls: list[tuple[list[int], list[float]]] = []

    def fake_choices(population, weights=None, k=1):
        calls.append((list(population), list(weights)))
        # 50 leaves BOB winning and ANN answering — unsafe, so it is redrawn.
        return [50] if len(calls) == 1 else [calls[-1][0][-1]]

    with patch.object(logic.random, "choices", side_effect=fake_choices):
        value = choose_roll(rolls, CID, pair(ANN, BOB), odds)

    assert len(calls) == 2
    natural_pool, natural_weights = calls[0]
    assert natural_pool == list(range(1, 101))
    assert natural_weights == weights
    safe, redraw_weights = calls[1]
    assert 69 not in safe
    assert redraw_weights == [weights[v - 1] for v in safe]
    assert not has_blocked_edge({**rolls, CID: value}, pair(ANN, BOB))


def test_the_redraw_never_lands_on_a_face_set_to_never():
    # CID's safe values include 100 (taking the win off BOB); with 100 set to
    # never it must not come back from the redraw either.
    odds = SpecialRollOdds(hundred=0)
    rolls = {ANN: 10, BOB: 90}
    random.seed(3)
    for _ in range(2_000):
        value = choose_roll(rolls, CID, pair(ANN, BOB), odds)
        assert value != 100
        if value != 69:
            assert not has_blocked_edge({**rolls, CID: value}, pair(ANN, BOB))


# ── storage ──────────────────────────────────────────────────────────


def test_store_reads_a_guilds_odds_and_defaults_the_rest(sync_db_path):
    from bot_modules.core.db_utils import open_db  # noqa: PLC0415

    with open_db(sync_db_path) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [
                (1, SPECIAL_ODDS_KEYS[69], "500"),
                (1, SPECIAL_ODDS_KEYS[1], "0"),
                (2, SPECIAL_ODDS_KEYS[100], "5"),
            ],
        )
    store = StateStore(sync_db_path)
    assert store._load_special_roll_odds(1) == SpecialRollOdds(sixty_nine=500, one=0)
    assert store._load_special_roll_odds(2) == SpecialRollOdds(hundred=5)
    assert store._load_special_roll_odds(3) == FLAT_ODDS


def test_the_member_manual_does_not_advertise_the_odds():
    # Deliberate: the owner chose not to tell players the special-roll odds
    # can be tuned (docs/specs/risky_roll_spec.md, "Special-roll odds"). A docs
    # sweep that "completes" the manual would undo that decision.

    manual = (
        REPO_ROOT / "src/web_server/static/manual.html"
    ).read_text(encoding="utf-8")
    for tell in ("Special Rolls", "risky_odds"):
        assert tell not in manual
