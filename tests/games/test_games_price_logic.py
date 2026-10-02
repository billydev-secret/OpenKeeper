"""Tests for the extracted Name Your Price pure-logic modules.

Covers ``bot_modules/games/price/logic.py`` (price parsing/formatting,
ladder + stats, vote winner tally, recap awards + highlight) and
the cog glue that needs no Discord. The Components V2 cards are covered
in ``test_games_price_layouts.py``. Mirrors the pressure_cooker /
games_hottakes / games_ttl pattern: the cog file stays thin; this
module proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.games.price.logic import (
    MIN_PLAYERS,
    MIN_VOTERS,
    lobby_players,
    resolve_source,
    roster_all_in,
    toggle_player,
    vote_possible,
    MAX_PRICE,
    MIN_PRICE,
    build_ladder,
    collect_all_players,
    compute_highlight,
    compute_recap_awards,
    format_price,
    ladder_stats,
    parse_price,
    price_label,
    tally_winners,
)


# ── parse_price ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", 0),
        ("100", 100),
        ("42", 42),
        ("$500", 500),
        ("$1,000", 1000),
        ("1,234,567", 1_234_567),
        ("  $50  ", 50),
        ("5k", 5_000),
        ("5K", 5_000),
        ("1.5k", 1_500),
        ("2m", 2_000_000),
        ("2.5M", 2_500_000),
        ("1B", MAX_PRICE),  # 1B == 1_000_000_000 clamps to MAX_PRICE (999_999_999)
        ("3 million", 3_000_000),
        ("4 billion", MAX_PRICE),  # clamped
        ("1 BILLION", MAX_PRICE),  # clamped
        ("$1.2m", 1_200_000),
    ],
)
def test_parse_price_recognizes_common_forms(raw: str, expected: int) -> None:
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "$abc", "k", "m", "$$", "$,"])
def test_parse_price_returns_none_on_garbage(raw: str) -> None:
    assert parse_price(raw) is None


@pytest.mark.parametrize("raw", ["abck", "xyzm", "$??b"])
def test_parse_price_returns_none_when_suffix_leaves_bad_number(raw: str) -> None:
    """The suffix matches but the residue can't be parsed as a float."""
    assert parse_price(raw) is None


def test_parse_price_handles_none() -> None:
    assert parse_price(None) is None  # type: ignore[arg-type]


def test_parse_price_clamps_below_min() -> None:
    assert parse_price("-100") == MIN_PRICE
    assert parse_price("-5k") == MIN_PRICE


def test_parse_price_clamps_above_max() -> None:
    # 10B is way over the cap
    assert parse_price("10b") == MAX_PRICE
    assert parse_price("1000000000000") == MAX_PRICE


def test_parse_price_exact_cap() -> None:
    assert parse_price(str(MAX_PRICE)) == MAX_PRICE


# ── format_price ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "amount,expected",
    [
        (0, "$0"),
        (1, "$1"),
        (42, "$42"),
        (999, "$999"),
        (1_000, "$1,000"),
        (12_345, "$12,345"),
        (999_999, "$999,999"),
        (1_000_000, "$1.0M"),
        (2_500_000, "$2.5M"),
        (1_000_000_000, "$1.0B"),
        (1_500_000_000, "$1.5B"),
    ],
)
def test_format_price(amount: int, expected: str) -> None:
    assert format_price(amount) == expected


# ── price_label ──────────────────────────────────────────────────────


def test_price_label_zero_gets_free_flavor() -> None:
    assert price_label(0) == "$0 (free?!)"


def test_price_label_huge_gets_refuse_flavor() -> None:
    assert price_label(999_000_000).endswith("(absolutely not)")
    assert price_label(MAX_PRICE).endswith("(absolutely not)")


def test_price_label_plain_in_middle() -> None:
    # 500 is neither free nor over the refuse threshold
    assert price_label(500) == "$500"
    assert price_label(50_000) == "$50,000"


# ── build_ladder ─────────────────────────────────────────────────────


def test_build_ladder_sorts_by_amount() -> None:
    prices = {10: 500, 20: 100, 30: 250}
    assert build_ladder(prices) == [(20, 100), (30, 250), (10, 500)]


def test_build_ladder_breaks_amount_ties_by_uid() -> None:
    prices = {7: 100, 3: 100, 5: 100}
    assert build_ladder(prices) == [(3, 100), (5, 100), (7, 100)]


def test_build_ladder_empty_returns_empty() -> None:
    assert build_ladder({}) == []


# ── ladder_stats ─────────────────────────────────────────────────────


def test_ladder_stats_empty_returns_none() -> None:
    assert ladder_stats([]) is None


def test_ladder_stats_single_amount() -> None:
    s = ladder_stats([42])
    assert s == {"low": 42, "high": 42, "median": 42, "mean": 42}


def test_ladder_stats_multiple_amounts() -> None:
    s = ladder_stats([100, 200, 300, 400])
    assert s is not None
    assert s["low"] == 100
    assert s["high"] == 400
    assert s["median"] == 250
    assert s["mean"] == 250


def test_ladder_stats_truncates_floats_to_int() -> None:
    # median of [1, 2] is 1.5 → int 1
    s = ladder_stats([1, 2])
    assert s is not None
    assert s["median"] == 1
    # mean of [1, 2, 4] is 7/3 ≈ 2.33 → int 2
    s2 = ladder_stats([1, 2, 4])
    assert s2 is not None
    assert s2["mean"] == 2


# ── tally_winners ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "votes,expected_winners,expected_max_votes",
    [
        pytest.param({}, [], 0, id="no-votes"),
        pytest.param({1: 100, 2: 100, 3: 200}, [100], 2, id="single-winner"),
        pytest.param(
            {1: 100, 2: 200, 3: 100, 4: 200}, [100, 200], 2, id="two-way-tie"
        ),
        pytest.param(
            {1: 100, 2: 200, 3: 300}, [100, 200, 300], 1, id="three-way-tie"
        ),
        pytest.param({1: 50, 2: 50, 3: 50}, [50], 3, id="unanimous"),
    ],
)
def test_tally_winners(
    votes: dict[int, int],
    expected_winners: list[int],
    expected_max_votes: int,
) -> None:
    winners, max_votes = tally_winners(votes)
    assert sorted(winners) == expected_winners
    assert max_votes == expected_max_votes


# ── compute_recap_awards ─────────────────────────────────────────────


def test_compute_recap_awards_empty_inputs() -> None:
    awards = compute_recap_awards({}, {})
    assert awards == {}


def test_compute_recap_awards_reasonable_unhinged_single_winner() -> None:
    scores = {
        "reasonable_wins": {"1": 3, "2": 1},
        "unhinged_wins": {"2": 2},
    }
    awards = compute_recap_awards({}, scores)
    assert "reasonable" in awards
    label, uids, detail = awards["reasonable"]
    assert label.startswith("🎯")
    assert uids == [1]
    assert detail == "won 3 rounds"

    label2, uids2, detail2 = awards["unhinged"]
    assert uids2 == [2]
    assert detail2 == "won 2 rounds"


def test_compute_recap_awards_single_round_uses_singular_round_word() -> None:
    scores = {"reasonable_wins": {"5": 1}, "unhinged_wins": {}}
    awards = compute_recap_awards({}, scores)
    _label, _uids, detail = awards["reasonable"]
    assert detail == "won 1 round"


def test_compute_recap_awards_ties_include_all_winners() -> None:
    scores = {
        "reasonable_wins": {"10": 2, "20": 2, "30": 1},
        "unhinged_wins": {},
    }
    awards = compute_recap_awards({}, scores)
    _label, uids, _detail = awards["reasonable"]
    assert sorted(uids) == [10, 20]


def test_compute_recap_awards_spender_and_cheapest_one_round() -> None:
    rounds_data = {
        "1": {"prices": {"100": 50_000, "200": 100}},
    }
    awards = compute_recap_awards(rounds_data, {})
    _label, spender_uids, spender_detail = awards["spender"]
    assert spender_uids == [100]
    assert "$50,000" in spender_detail

    _label, cheap_uids, cheap_detail = awards["cheapest"]
    assert cheap_uids == [200]
    assert "$100" in cheap_detail

    # Single round means no consistent/wildest
    assert "consistent" not in awards
    assert "wildest" not in awards


def test_compute_recap_awards_consistent_and_wildest_need_two_rounds() -> None:
    rounds_data = {
        "1": {"prices": {"100": 100, "200": 50}},
        "2": {"prices": {"100": 110, "200": 5_000}},
    }
    awards = compute_recap_awards(rounds_data, {})
    assert "consistent" in awards
    assert "wildest" in awards
    _label, cons_uids, _detail = awards["consistent"]
    _label, wild_uids, _detail = awards["wildest"]
    # Player 100 swung only 10; player 200 swung a lot
    assert cons_uids == [100]
    assert wild_uids == [200]


def test_compute_recap_awards_skips_stddev_if_only_one_round_per_player() -> None:
    # Different players each only appear in one round
    rounds_data = {
        "1": {"prices": {"100": 50}},
        "2": {"prices": {"200": 75}},
    }
    awards = compute_recap_awards(rounds_data, {})
    assert "spender" in awards
    assert "cheapest" in awards
    assert "consistent" not in awards
    assert "wildest" not in awards


def test_compute_recap_awards_ignores_malformed_uid_keys() -> None:
    rounds_data = {
        "1": {"prices": {"notanumber": 50, "100": 200}},
    }
    awards = compute_recap_awards(rounds_data, {})
    # Only the valid uid should show up
    _label, spender_uids, _ = awards["spender"]
    assert spender_uids == [100]


# ── compute_highlight ────────────────────────────────────────────────


def test_compute_highlight_empty_returns_none() -> None:
    assert compute_highlight({}) is None


def test_compute_highlight_single_submission_round_returns_none() -> None:
    rounds_data = {"1": {"prices": {"100": 50}}}
    assert compute_highlight(rounds_data) is None


def test_compute_highlight_picks_widest_spread() -> None:
    rounds_data = {
        "1": {"prices": {"100": 10, "200": 20}},  # spread 10
        "2": {"prices": {"100": 5, "200": 1000}},  # spread 995
        "3": {"prices": {"100": 50, "200": 60}},  # spread 10
    }
    hi = compute_highlight(rounds_data)
    assert hi is not None
    rnum, lo, high = hi
    assert rnum == "2"
    assert lo == 5
    assert high == 1000


def test_compute_highlight_first_wins_on_tie() -> None:
    # Both rounds have spread 100 — the first encountered wins.
    rounds_data = {
        "1": {"prices": {"100": 0, "200": 100}},
        "2": {"prices": {"100": 200, "200": 300}},
    }
    hi = compute_highlight(rounds_data)
    assert hi is not None
    rnum, _lo, _hi = hi
    assert rnum == "1"


# ── collect_all_players ──────────────────────────────────────────────


def test_collect_all_players_empty() -> None:
    assert collect_all_players({}) == set()


def test_collect_all_players_dedupes_across_rounds() -> None:
    rounds_data = {
        "1": {"prices": {"100": 50, "200": 75}},
        "2": {"prices": {"100": 99, "300": 1}},
    }
    assert collect_all_players(rounds_data) == {100, 200, 300}


def test_collect_all_players_skips_malformed() -> None:
    rounds_data = {
        "1": {"prices": {"100": 50, "junk": 99}},
    }
    assert collect_all_players(rounds_data) == {100}


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.games.price.cog as price_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.games.db import GamesDb  # noqa: E402
from tests.fakes import FakeChannel  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_show_recap_pays_all_submitters(monkeypatch, sync_db_path):
    """Recap pays everyone who submitted a price in any round."""
    spy = AsyncMock()
    monkeypatch.setattr(price_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {
        "rounds": {"1": {"prices": {"1": 100, "2": 200}}, "2": {"prices": {"3": 50}}},
        "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
    }
    gid = await create_game(bot.games_db, 100, 1, "price", payload=payload)
    cog = price_cog.PriceCog(bot)  # type: ignore[arg-type]
    channel = FakeChannel(id=100)
    await cog._show_recap(gid, 1, "Host", channel, None, {})
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert set(call.kwargs["player_ids"]) == {1, 2, 3}
    assert call.kwargs["bot"] is bot


# ── the lobby, the vote floor and the source default (trivia-tail-85/86/87) ──


def test_two_voters_forced_to_cross_vote_always_tie():
    """Why the vote needs three: with two submitters the self-vote refusal
    leaves each exactly one target, so both are crowned every round."""
    winners, votes = tally_winners({1: 2, 2: 1})
    assert sorted(winners) == [1, 2] and votes == 1


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        pytest.param(0, False, id="nobody"),
        pytest.param(1, False, id="one-is-reveal-only"),
        pytest.param(2, False, id="two-is-reveal-only"),
        pytest.param(3, True, id="three-votes"),
        pytest.param(8, True, id="a-room"),
    ],
)
def test_vote_possible(count, expected):
    assert MIN_VOTERS == 3
    assert vote_possible(count) is expected


@pytest.mark.parametrize(
    ("requested", "bank_has_rows", "expected"),
    [
        pytest.param(None, True, "bank", id="no-choice-bank-full"),
        pytest.param(None, False, "host", id="no-choice-bank-empty"),
        pytest.param("", True, "bank", id="blank-bank-full"),
        pytest.param("host", True, "host", id="explicit-host-wins"),
        pytest.param("players", False, "players", id="explicit-players-wins"),
        pytest.param("bank", False, "bank", id="explicit-bank-even-if-empty"),
        pytest.param("ai", True, "bank", id="retired-ai-runs-on-the-bank"),
        pytest.param("both", False, "host", id="retired-both-empty-bank"),
        pytest.param("BANK", False, "bank", id="case-insensitive"),
    ],
)
def test_resolve_source(requested, bank_has_rows, expected):
    assert resolve_source(requested, bank_has_rows) == expected


def test_toggle_player_joins_then_leaves():
    payload: dict = {}
    assert toggle_player(payload, 5) == "joined"
    assert toggle_player(payload, 6) == "joined"
    assert payload["players"] == [5, 6]
    assert toggle_player(payload, 5) == "left"
    assert payload["players"] == [6]


def test_lobby_players_coerces_and_dedupes():
    assert lobby_players({"players": ["1", 2, "x", 2]}) == [1, 2]
    assert lobby_players({}) == []


@pytest.mark.parametrize(
    "roster,submitted,expected",
    [
        # A spectator's price never fills a joined player's seat (P5): with
        # 3 joined and 3 prices in, the round used to close on a headcount
        # and lock the third joined player out.
        pytest.param({1, 2, 3}, {1, 2, 4}, False, id="spectator-fills-no-seat"),
        pytest.param({1, 2, 3}, {1, 2, 3, 4}, True, id="roster-complete-plus-spectator"),
        pytest.param({1, 2, 3}, {1, 2, 3}, True, id="roster-complete"),
        pytest.param({1, 2, 3}, {1, 2}, False, id="one-short"),
        pytest.param(set(), {1, 2, 3}, False, id="no-roster-runs-the-timer"),
        pytest.param(set(), set(), False, id="nothing-at-all"),
    ],
)
def test_roster_all_in(roster, submitted, expected):
    assert roster_all_in(roster, submitted) is expected


def test_lobby_floor_is_mirrored_in_the_sweep_registry():
    from bot_modules.games.constants import LOBBY_GAME_TYPES, LOBBY_MIN_PLAYERS, LOBBY_START_BUTTON

    assert "price" in LOBBY_GAME_TYPES
    assert LOBBY_MIN_PLAYERS["price"] == MIN_PLAYERS == 2
    assert LOBBY_START_BUTTON["price"] == "Start"


# ── the submission view closes on the roster, and End Game pays ──────────


class _FakeTimer:
    def __init__(self) -> None:
        self.skipped = False

    def skip(self) -> None:
        self.skipped = True


def _game_view(bot, expected: set[int], **kw):
    cog = price_cog.PriceCog(bot)  # type: ignore[arg-type]
    return price_cog.PriceGameView(
        game_id=kw.get("game_id", "g"), host_id=1, host_name="Host", scenario="Eat a bug",
        round_num=1, total_rounds=3, timer_secs=30, db=bot.games_db, bot=bot, cog=cog,
        expected_ids=expected, settings={"rounds": 3},
    )


@pytest.mark.parametrize(
    ("expected", "prices", "closes"),
    [
        pytest.param({1, 2}, {1: 10, 2: 20}, True, id="everyone-in-closes"),
        pytest.param({1, 2, 3}, {1: 10, 2: 20}, False, id="one-short-waits"),
        # Submission is open to the room: a spectator's price counts in the
        # reveal but fills nobody's seat, so three prices for a three-seat
        # roster with one joined player still waiting keeps the round open
        # (it used to close on the headcount and lock that player out).
        pytest.param({1, 2, 3}, {1: 10, 2: 20, 9: 30}, False, id="spectator-fills-no-seat"),
        pytest.param({1, 2, 3}, {1: 10, 2: 20, 9: 30, 3: 40}, True, id="roster-in-plus-spectator-closes"),
        pytest.param(set(), {1: 10, 2: 20}, False, id="no-roster-runs-the-timer"),
    ],
)
async def test_price_modal_closes_the_round_when_the_roster_has_answered(sync_db_path, expected, prices, closes):
    bot = _SpyBot(sync_db_path)
    view = _game_view(bot, expected)
    view._timer = _FakeTimer()  # type: ignore[assignment]
    responses: list = []

    async def _send(*a, **kw):
        responses.append((a, kw))

    for uid, amount in prices.items():
        modal = price_cog.PriceModal(view)
        modal.price._value = str(amount)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=uid, display_name=f"u{uid}"),
            channel=SimpleNamespace(name="games"),
            response=SimpleNamespace(send_message=_send),
        )
        await modal.on_submit(interaction)  # type: ignore[arg-type]
    assert view.prices == prices
    assert view._timer.skipped is closes  # type: ignore[union-attr]


def test_game_view_ends_through_the_host_panel_and_the_dead_close_path_is_gone():
    src = inspect.getsource(price_cog.PriceGameView.end_game_button)
    assert "is_host_or_mod" in src and "ConfirmCloseView" in src and "end_early" in src
    assert not hasattr(price_cog.PriceCog, "_end_game")


async def test_end_early_keeps_this_rounds_prices_and_pays_through_the_recap(monkeypatch, sync_db_path):
    spy = AsyncMock()
    monkeypatch.setattr(price_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {
        "settings": {"rounds": 3}, "total_rounds": 3,
        "rounds": {"1": {"prices": {"1": 100, "2": 200}}},
        "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
    }
    gid = await create_game(bot.games_db, 100, 1, "price", payload=payload)
    view = _game_view(bot, {1, 2, 3}, game_id=gid)
    view.round_num = 2
    view.prices = {3: 50}
    view._timer = _FakeTimer()  # type: ignore[assignment]
    view._msg = SimpleNamespace(edit=AsyncMock())
    bot.active_views[gid] = view

    await view.cog.end_early(view, FakeChannel(id=100), None)
    # The board's closed frame carries no controls.
    assert not [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]

    assert view._closed and view._timer.skipped  # type: ignore[union-attr]
    call = spy.await_args
    assert call is not None and set(call.kwargs["player_ids"]) == {1, 2, 3}
    assert call.kwargs["round_count"] == 2
    assert gid not in bot.active_views
    # A second press does nothing — the row is already claimed.
    await view.cog.end_early(view, FakeChannel(id=100), None)
    assert spy.await_count == 1


def test_lobby_view_offers_join_leave_help_and_nothing_host_only():
    """Ben, 2026-09-30: Start and Cancel live on the host panel."""
    view = price_cog.PriceLobbyView("g", 1, None, None, None)  # type: ignore[arg-type]
    labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert labels == ["❓ Help", "Join", "Leave"]
    assert view.timeout is None  # survives a restart via recover_game


def test_scenario_prompt_view_has_no_dead_edit_and_gates_on_the_host():
    src = inspect.getsource(price_cog.ScenarioPromptView)
    assert "edit_original_response" not in src
    assert "is_host_or_mod" in src
    assert not hasattr(price_cog, "HostWriteView") and not hasattr(price_cog, "PlayerWriteView")


import inspect  # noqa: E402
