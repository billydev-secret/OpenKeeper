"""Tests for the extracted Mt. Rushmore Draft pure-logic modules.

Covers ``bot_modules/games/rushmore/logic.py`` (snake-order generation,
duplicate detection, eligible voter filtering, vote tallying, recap
stats, settings clamp), and the cog glue that needs no Discord. The
Components V2 cards are covered in ``test_games_rushmore_layouts.py``.

Mirrors the games_ttl / games_clapback pattern: the cog file stays
thin; this module proves the extracted pieces work without spinning up
Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games.rushmore.logic import (
    DEFAULT_PICK_SECONDS,
    DRAFT_ROUNDS,
    MAX_PLAYERS,
    MIN_PLAYERS,
    SKIPPED_MARKER,
    apply_backfill,
    can_start,
    clamp_player_limits,
    clamp_settings,
    compute_recap_stats,
    eligible_voters,
    find_who_picked,
    first_skipped_slot,
    generate_snake_order,
    is_duplicate,
    lobby_is_full,
    players_with_skips,
    tally_votes,
)


def _name_resolver(uid: int) -> str:
    return f"User{uid}"


# ── constants ────────────────────────────────────────────────────────


def test_draft_rounds_is_four():
    assert DRAFT_ROUNDS == 4


def test_default_pick_seconds_is_forty_five():
    assert DEFAULT_PICK_SECONDS == 45


def test_skipped_marker_is_string():
    assert isinstance(SKIPPED_MARKER, str) and SKIPPED_MARKER


# ── generate_snake_order ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("players", "rounds", "expected"),
    [
        pytest.param(
            [10, 20, 30],
            None,
            [
                [1, 10], [1, 20], [1, 30],
                [2, 30], [2, 20], [2, 10],
                [3, 10], [3, 20], [3, 30],
                [4, 30], [4, 20], [4, 10],
            ],
            id="three-players-four-default-rounds",
        ),
        pytest.param([1, 2], 2, [[1, 1], [1, 2], [2, 2], [2, 1]], id="custom-rounds"),
        pytest.param([1, 2, 3], 1, [[1, 1], [1, 2], [1, 3]], id="single-round"),
        pytest.param([], None, [], id="empty-players"),
    ],
)
def test_generate_snake_order(
    players: list[int], rounds: int | None, expected: list[list[int]]
):
    kwargs = {} if rounds is None else {"rounds": rounds}
    assert generate_snake_order(players, **kwargs) == expected


def test_generate_snake_order_round_count_matches():
    """Every player picks exactly ``rounds`` times."""
    players = [11, 22, 33, 44]
    order = generate_snake_order(players, rounds=4)
    assert len(order) == len(players) * 4
    for p in players:
        assert sum(1 for _, pid in order if pid == p) == 4


# ── is_duplicate ─────────────────────────────────────────────────────


def test_is_duplicate_case_insensitive_match():
    assert is_duplicate("Pizza", ["sushi", "PIZZA", "tacos"]) is True


def test_is_duplicate_whitespace_trimmed():
    assert is_duplicate("  pizza  ", ["pizza"]) is True


def test_is_duplicate_not_found():
    assert is_duplicate("burger", ["pizza", "sushi"]) is False


def test_is_duplicate_against_empty_list():
    assert is_duplicate("pizza", []) is False


# ── find_who_picked ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pick", "boards", "expected"),
    [
        pytest.param(
            "pizza",
            {"1": ["Pizza", None, None, None], "2": ["Sushi", "Tacos", None, None]},
            "1",
            id="returns-uid-str-of-owner",
        ),
        pytest.param(
            "TACOS",
            {"1": ["Pizza", None, None, None], "2": ["Sushi", "Tacos", None, None]},
            "2",
            id="case-insensitive-owner-lookup",
        ),
        # A slot holding the SKIPPED_MARKER must not match any input.
        pytest.param(
            SKIPPED_MARKER,
            {"1": [SKIPPED_MARKER, None, None, None]},
            None,
            id="skips-skipped-markers",
        ),
        pytest.param(
            "Burger",
            {"1": ["Pizza", None, None, None]},
            None,
            id="returns-none-when-not-found",
        ),
        pytest.param(
            "anything",
            {"1": [None, None, None, None]},
            None,
            id="skips-none-slots",
        ),
        pytest.param("anything", {}, None, id="empty-boards-dict"),
    ],
)
def test_find_who_picked(
    pick: str, boards: dict[str, list[str | None]], expected: str | None
):
    assert find_who_picked(pick, boards) == expected


# ── eligible_voters ──────────────────────────────────────────────────


def test_eligible_voters_includes_players_with_any_real_pick():
    players = [1, 2, 3]
    boards = {
        "1": ["Pizza", None, None, None],
        "2": [SKIPPED_MARKER, SKIPPED_MARKER, SKIPPED_MARKER, SKIPPED_MARKER],
        "3": [None, None, None, None],
    }
    assert eligible_voters(players, boards) == [1]


def test_eligible_voters_preserves_input_order():
    players = [3, 1, 2]
    boards = {
        "1": ["A", None, None, None],
        "2": ["B", None, None, None],
        "3": ["C", None, None, None],
    }
    assert eligible_voters(players, boards) == [3, 1, 2]


def test_eligible_voters_handles_missing_board():
    """A player whose uid isn't in ``boards`` is treated as having no picks."""
    assert eligible_voters([42], {}) == []


def test_eligible_voters_empty_player_list():
    assert eligible_voters([], {}) == []


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_single_winner():
    votes = {1: 10, 2: 10, 3: 20}
    winners, max_v, results = tally_votes(votes, eligible=[10, 20, 30])
    assert winners == [10]
    assert max_v == 2
    assert results == [(10, 2), (20, 1), (30, 0)]


def test_tally_votes_dead_heat_returns_all_winners():
    votes = {1: 10, 2: 20}
    winners, max_v, _ = tally_votes(votes, eligible=[10, 20])
    assert set(winners) == {10, 20}
    assert max_v == 1


# social-prompt-36: at the three-player floor the vote cycles and every tied
# uid used to win and be paid. Fewest skips breaks it, then total pick time.
@pytest.mark.parametrize(
    ("skipped", "pick_times", "expected"),
    [
        pytest.param(["20_2"], {}, [10], id="fewest-skips-wins"),
        pytest.param(["10_1", "20_2"], {"10_2": 4.0, "20_1": 9.0}, [10], id="equal-skips-fastest-wins"),
        pytest.param([], {"10_1": 3.0, "10_2": 3.0, "20_1": 2.0, "20_2": 5.0}, [10], id="times-are-summed"),
        pytest.param([], {"10_1": 5.0, "20_1": 5.0}, [10, 20], id="true-dead-heat-is-shared"),
        pytest.param(["10_1"], {"10_2": 1.0}, [20], id="a-skip-outweighs-speed"),
        pytest.param(None, None, [10, 20], id="nothing-to-break-on"),
    ],
)
def test_tally_votes_breaks_a_tie_on_skips_then_pick_time(skipped, pick_times, expected):
    votes = {1: 10, 2: 20, 3: 10, 4: 20}
    winners, max_v, results = tally_votes(votes, eligible=[10, 20, 30], skipped=skipped, pick_times=pick_times)
    assert winners == expected
    assert max_v == 2
    assert dict(results) == {10: 2, 20: 2, 30: 0}, "the results table still shows the tie"


def test_tally_votes_tie_break_does_not_touch_a_clear_winner():
    winners, _, _ = tally_votes({1: 10, 2: 10, 3: 20}, eligible=[10, 20], skipped=["10_1", "10_2"])
    assert winners == [10]


def test_tally_votes_no_votes_returns_empty_winners():
    winners, max_v, results = tally_votes({}, eligible=[10, 20])
    assert winners == []
    assert max_v == 0
    # Zero-vote players still show up in the results list, sorted by votes
    assert sorted(r[0] for r in results) == [10, 20]
    assert all(v == 0 for _, v in results)


def test_tally_votes_results_sorted_highest_first():
    votes = {1: 30, 2: 30, 3: 20, 4: 10}
    _, _, results = tally_votes(votes, eligible=[10, 20, 30])
    vals = [v for _, v in results]
    assert vals == sorted(vals, reverse=True)
    # 30 has 2 votes; 20 and 10 each have 1
    by_uid = dict(results)
    assert by_uid[30] == 2
    assert by_uid[20] == 1
    assert by_uid[10] == 1


# ── compute_recap_stats ──────────────────────────────────────────────


def test_compute_recap_stats_first_pick_present_when_round1_filled():
    draft_order = [[1, 10], [1, 20], [2, 20], [2, 10]]
    boards = {"10": ["Pizza", None], "20": [None, None]}
    stats = compute_recap_stats(
        draft_order, boards, all_picks=["Pizza"],
        pick_times={}, skipped=[], votes={},
        name_resolver=_name_resolver,
    )
    assert stats["first_pick"] == {"pick": "Pizza", "player": "User10"}


def test_compute_recap_stats_first_pick_absent_when_round1_skipped():
    draft_order = [[1, 10], [1, 20]]
    boards = {"10": [SKIPPED_MARKER, None], "20": [None, None]}
    stats = compute_recap_stats(
        draft_order, boards, all_picks=["dummy"],
        pick_times={}, skipped=["10_1"], votes={},
        name_resolver=_name_resolver,
    )
    assert "first_pick" not in stats


def test_compute_recap_stats_first_pick_absent_when_no_picks():
    """When no picks ever happened, the first_pick key is omitted."""
    stats = compute_recap_stats(
        [[1, 10]], {"10": [None]}, all_picks=[], pick_times={},
        skipped=[], votes={}, name_resolver=_name_resolver,
    )
    assert "first_pick" not in stats


def test_compute_recap_stats_skipped_count_and_names():
    stats = compute_recap_stats(
        [], {}, all_picks=[], pick_times={},
        skipped=["10_1", "20_2", "10_3"], votes={},
        name_resolver=_name_resolver,
    )
    assert stats["skipped_count"] == 3
    # Two unique players, dedup preserves first-seen order
    assert stats["skipped_names"] == ["User10", "User20"]


def test_compute_recap_stats_skipped_count_zero_when_none():
    stats = compute_recap_stats(
        [], {}, [], {}, skipped=[], votes={},
        name_resolver=_name_resolver,
    )
    assert stats["skipped_count"] == 0
    assert "skipped_names" not in stats


def test_compute_recap_stats_fastest_and_slowest():
    boards = {"10": ["A", "B"], "20": ["C", "D"]}
    pick_times = {"10_1": 5.0, "10_2": 12.0, "20_1": 2.5, "20_2": None}
    stats = compute_recap_stats(
        [[1, 10], [1, 20], [2, 20], [2, 10]],
        boards, all_picks=["A", "B", "C"], pick_times=pick_times,
        skipped=[], votes={}, name_resolver=_name_resolver,
    )
    # Fastest: 20_1 (2.5s) → "C"
    assert stats["fastest"]["pick"] == "C"
    assert stats["fastest"]["player"] == "User20"
    assert stats["fastest"]["time"] == pytest.approx(2.5)
    # Slowest: 10_2 (12s) → "B"
    assert stats["slowest"]["pick"] == "B"
    assert stats["slowest"]["player"] == "User10"
    assert stats["slowest"]["time"] == pytest.approx(12.0)


def test_compute_recap_stats_fast_slow_absent_when_no_timed_picks():
    """All None pick_times (e.g. every pick was skipped) → no fast/slow."""
    stats = compute_recap_stats(
        [], {}, [], pick_times={"10_1": None}, skipped=["10_1"], votes={},
    )
    assert "fastest" not in stats
    assert "slowest" not in stats


def test_compute_recap_stats_fastest_handles_multi_digit_uid():
    """``rsplit("_", 1)`` must work for uids that include digits anywhere."""
    boards = {"123456789": ["Pick!"]}
    pick_times: dict[str, float | None] = {"123456789_1": 4.2}
    stats = compute_recap_stats(
        [[1, 123456789]], boards, ["Pick!"], pick_times,
        skipped=[], votes={}, name_resolver=_name_resolver,
    )
    assert stats["fastest"]["pick"] == "Pick!"
    assert stats["fastest"]["player"] == "User123456789"


def test_compute_recap_stats_unanimous_flag_when_one_target():
    stats = compute_recap_stats(
        [], {}, [], {}, [], votes={1: 99, 2: 99, 3: 99},
    )
    assert stats.get("unanimous") is True
    assert "vote_split" not in stats


def test_compute_recap_stats_vote_split_flag_when_multi_target():
    stats = compute_recap_stats(
        [], {}, [], {}, [], votes={1: 10, 2: 20, 3: 30},
    )
    assert stats.get("vote_split") == 3
    assert "unanimous" not in stats


def test_compute_recap_stats_no_unanimous_or_split_when_no_votes():
    stats = compute_recap_stats([], {}, [], {}, [], votes={})
    assert "unanimous" not in stats
    assert "vote_split" not in stats


def test_compute_recap_stats_default_name_resolver_uses_str_uid():
    """When name_resolver is None, ``str(uid)`` is used in stat fields."""
    stats = compute_recap_stats(
        [[1, 42]],
        {"42": ["Pizza"]},
        all_picks=["Pizza"],
        pick_times={"42_1": 3.0},
        skipped=["42_1"],  # also test skipped_names path
        votes={},
    )
    assert stats["first_pick"]["player"] == "42"
    assert stats["fastest"]["player"] == "42"
    assert stats["skipped_names"] == ["42"]


def test_compute_recap_stats_pick_info_handles_out_of_range_round():
    """If a pick_times key points past the end of the board, the
    pick text falls back to ``"?"`` instead of raising."""
    boards = {"10": ["only_one"]}  # board is length 1
    pick_times: dict[str, float | None] = {"10_5": 3.3}
    stats = compute_recap_stats(
        [], boards, ["only_one"], pick_times, [], {},
        name_resolver=_name_resolver,
    )
    assert stats["fastest"]["pick"] == "?"


# ── clamp_settings ───────────────────────────────────────────────────


def test_clamp_settings_within_range_unchanged():
    assert clamp_settings(60, 30) == (60, 30)


def test_clamp_settings_below_min_pulled_up():
    assert clamp_settings(1, 1) == (10, 10)


def test_clamp_settings_above_max_pushed_down():
    assert clamp_settings(9999, 9999) == (120, 60)


def test_clamp_settings_boundary_values():
    assert clamp_settings(10, 10) == (10, 10)
    assert clamp_settings(120, 60) == (120, 60)


def test_clamp_settings_range_unchanged_by_new_default():
    # DEFAULT_PICK_SECONDS moved from 30 to 45, but the dial's own bounds
    # (10-120) did not — the new default clamps through untouched, and the
    # min/max edges still bite exactly where they used to.
    assert clamp_settings(DEFAULT_PICK_SECONDS, 30) == (DEFAULT_PICK_SECONDS, 30)
    assert clamp_settings(9, 30)[0] == 10
    assert clamp_settings(121, 30)[0] == 120


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

import asyncio  # noqa: E402
import time as _time_mod  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.games.rushmore.cog as rushmore_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.games.rushmore.logic import RedrawCoalescer  # noqa: E402
from bot_modules.games.db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_show_recap_pays_drafters(monkeypatch, sync_db_path):
    """Recap pays the full drafting roster."""
    spy = AsyncMock()
    monkeypatch.setattr(rushmore_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "rushmore", payload={"players": [1, 2, 3]})
    cog = rushmore_cog.RushmoreCog(bot)  # type: ignore[arg-type]
    draft_view = SimpleNamespace(
        game_id=gid, _draft_start=_time_mod.time(), boards={}, draft_order=[],
        all_picks=[], pick_times={}, skipped=[], host_name="Host",
        topic="Best X", players=[1, 2, 3], host_id=1, mode="snake",
    )
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())
    await cog._show_recap(draft_view, channel, None, {}, {}, [])  # type: ignore[arg-type]
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3]
    assert call.kwargs["bot"] is bot


# ── blitz board coalescing (todo #208/#209) ──────────────────────────
#
# Blitz mode has every player picking at once; without coalescing, an
# accepted pick redrew the board immediately, so five simultaneous picks
# meant five Discord edits. RedrawCoalescer collapses a burst into one, and
# RushmoreDraftView.accept_pick is the only thing that schedules one.


def _blitz_view(players=(1, 2, 3)):
    view = rushmore_cog.RushmoreDraftView(
        "gid", 1, "Host", "Topic", list(players), 30,
        None, None, None, None, mode="blitz",
    )
    # A fast coalescer so the tests don't wait on the real ~1.5s default.
    view._redraw_coalescer = RedrawCoalescer(delay=0.01)
    view._msg = SimpleNamespace(edit=AsyncMock())
    view._blitz_round = 1
    view._blitz_pending = set(players)
    return view


async def test_redraw_coalescer_collapses_a_simultaneous_burst_into_one_call():
    """The coalescer in isolation: N ``schedule`` calls made back-to-back
    (no ``await`` between them, i.e. arriving "together") fire ``redraw_fn``
    exactly once."""
    calls = 0

    async def redraw():
        nonlocal calls
        calls += 1

    coalescer = RedrawCoalescer(delay=0.01)
    for _ in range(5):
        coalescer.schedule(redraw)

    await asyncio.sleep(0.05)
    assert calls == 1


async def test_five_simultaneous_blitz_picks_produce_exactly_one_board_edit():
    """The full wiring: five players picking within the same tick — via
    ``accept_pick``, the real entry point — must cost exactly one
    ``_msg.edit`` call, not five."""
    view = _blitz_view(players=range(1, 6))

    for uid in range(1, 6):
        view.accept_pick(f"Pick {uid}", uid)

    await asyncio.sleep(0.05)
    assert view._msg.edit.await_count == 1
    assert view._blitz_pending == set()


async def test_accept_pick_schedules_a_redraw_only_for_a_valid_blitz_pick():
    view = _blitz_view()

    # Not currently owed a pick (already picked, or not this round) — the
    # attempt is a no-op and nothing gets scheduled.
    view.accept_pick("Nope", 999)
    assert view._redraw_coalescer._task is None
    assert view._msg.edit.await_count == 0

    # A valid pick schedules exactly one coalesced redraw.
    view.accept_pick("Real Pick", 1)
    assert view._redraw_coalescer._task is not None
    await asyncio.sleep(0.05)
    assert view._msg.edit.await_count == 1


async def test_closing_the_draft_cancels_a_pending_redraw():
    """No pending redraw task survives ``_closed`` — a pick landing right
    as the round/draft ends must not fire a late edit after the fact."""
    view = _blitz_view()
    view.accept_pick("Last Pick", 1)
    assert view._redraw_coalescer._task is not None

    view._closed = True
    view._redraw_coalescer.cancel()

    assert view._redraw_coalescer._task is None
    await asyncio.sleep(0.05)
    assert view._msg.edit.await_count == 0


# ── backfill helpers ─────────────────────────────────────────────────


def test_first_skipped_slot_finds_earliest():
    board = ["A", SKIPPED_MARKER, None, SKIPPED_MARKER]
    assert first_skipped_slot(board) == 1


def test_first_skipped_slot_none_when_no_skips():
    assert first_skipped_slot(["A", "B", None, "C"]) is None
    assert first_skipped_slot([]) is None


def test_players_with_skips_lists_only_skip_owners():
    boards = {
        "1": ["A", "B", "C", "D"],
        "2": [SKIPPED_MARKER, "X", "Y", "Z"],
        "3": [SKIPPED_MARKER] * 4,
    }
    assert players_with_skips(boards) == ["2", "3"]


def test_apply_backfill_fills_first_slot_and_clears_skip_entry():
    boards = {"7": ["A", SKIPPED_MARKER, SKIPPED_MARKER, "D"]}
    skipped = ["7_2", "7_3", "9_1"]
    slot = apply_backfill(boards, skipped, 7, "New Pick")
    assert slot == 1
    assert boards["7"] == ["A", "New Pick", SKIPPED_MARKER, "D"]
    # Only the filled slot's skip entry is removed.
    assert skipped == ["7_3", "9_1"]


def test_apply_backfill_sequential_fills_advance_through_slots():
    boards = {"7": [SKIPPED_MARKER, "B", SKIPPED_MARKER, "D"]}
    skipped = ["7_1", "7_3"]
    assert apply_backfill(boards, skipped, 7, "P1") == 0
    assert apply_backfill(boards, skipped, 7, "P2") == 2
    assert apply_backfill(boards, skipped, 7, "P3") is None  # nothing left
    assert boards["7"] == ["P1", "B", "P2", "D"]
    assert skipped == []


def test_apply_backfill_unknown_player_is_noop():
    boards = {"7": [SKIPPED_MARKER, None, None, None]}
    skipped = ["7_1"]
    assert apply_backfill(boards, skipped, 99, "Pick") is None
    assert boards["7"][0] == SKIPPED_MARKER
    assert skipped == ["7_1"]


def test_backfilled_pick_makes_player_vote_eligible():
    boards = {"7": [SKIPPED_MARKER] * 4}
    assert eligible_voters([7], boards) == []
    apply_backfill(boards, ["7_1"], 7, "Pick")
    assert eligible_voters([7], boards) == [7]


# ── lobby limits (the dashboard dials, finally enforced) ────────────────


def test_clamp_player_limits_never_exceeds_the_select_cap():
    """The vote is a Discord Select, which holds 25 options. A server that
    saved 200 while the dial was unbounded must not produce a lobby the vote
    message cannot render."""
    assert clamp_player_limits(3, 200) == (3, 25)
    assert clamp_player_limits(3, 25) == (3, 25)


def test_clamp_player_limits_raises_a_ceiling_below_its_floor():
    """Otherwise the pair describes a lobby that can fill but never start."""
    assert clamp_player_limits(10, 4) == (10, 10)


def test_clamp_player_limits_treats_zero_as_unset():
    """0 was what the old dial's "no limit" meant. There is no such thing here
    — the Select cap is real — so it reads as "use the default"."""
    assert clamp_player_limits(0, 0) == (MIN_PLAYERS, MAX_PLAYERS)


@pytest.mark.parametrize("stored", [1, 2], ids=["one", "two"])
def test_clamp_player_limits_floors_at_three(stored):
    """social-prompt-36: a two-player draft always ties 1–1 (nobody can vote
    for themselves), so a stored 2 from before the floor moved is raised."""
    assert clamp_player_limits(stored, 25) == (MIN_PLAYERS, 25)
    assert MIN_PLAYERS == 3


def test_lobby_is_full_honours_a_configured_ceiling():
    """Before this the Join button had no cap at all, and eligible_players[:25]
    silently dropped everyone past the 25th — after they had drafted a full
    board and could no longer be voted for."""
    assert lobby_is_full([1, 2, 3], 3) is True
    assert lobby_is_full([1, 2], 3) is False


def test_can_start_honours_a_configured_floor():
    assert can_start([1, 2, 3, 4], 5) is False
    assert can_start([1, 2, 3, 4, 5], 5) is True


# ── the recap card (social-prompt-45) ────────────────────────────────


def _recap_items():
    from bot_modules.games.rushmore.layouts import build_recap_layout

    return build_recap_layout("Snacks", 3, 60.0, [], 0, [], {})


def test_recap_card_has_run_again_and_no_hand_off():
    """Run Again under the presser *is* the hand-off; the Hand Off button
    only printed a command hint and disabled Run Again."""
    view = rushmore_cog.RushmoreRecapView(
        "gid", 1, SimpleNamespace(db=None), {}, _recap_items(),
    )
    ids = {getattr(c, "custom_id", None) for c in view.walk_children()} - {None}
    assert ids == {"rushmore_run_again"}


async def test_run_again_is_open_to_any_member_who_becomes_the_host(monkeypatch, sync_db_path):
    monkeypatch.setattr(rushmore_cog, "sign_off_game_chore", AsyncMock())
    bot = _SpyBot(sync_db_path)
    cog = rushmore_cog.RushmoreCog(bot)  # type: ignore[arg-type]
    async def _launch(**kwargs):
        bot.active_views["new-gid"] = object()  # the lobby a real launch registers
        return "new-gid"

    launch = AsyncMock(side_effect=_launch)
    cog.launch = launch  # type: ignore[method-assign]
    panel = AsyncMock()
    cog.open_host_panel = panel  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (778, 4242),
    )
    view = rushmore_cog.RushmoreRecapView(
        "old", host_id=1, cog=cog, settings={}, items=_recap_items(),
    )
    presser = SimpleNamespace(
        id=42, display_name="Someone",
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False, manage_messages=False),
    )
    inter = SimpleNamespace(
        user=presser, guild_id=4242, channel_id=778,
        channel=SimpleNamespace(id=778, name="games", guild=None),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )
    await view.run_again(inter)  # type: ignore[arg-type]
    launch.assert_awaited_once()
    assert launch.await_args.kwargs["host_id"] == 42
    # The lobby carries nothing host-only, so the new host is handed the
    # panel Start Draft lives on.
    assert launch.await_args.kwargs["options"]["mode"] == "blitz", "blitz is the default draft mode"
    panel.assert_awaited_once_with(inter, "new-gid")


async def test_run_again_hands_no_panel_for_a_launch_that_failed(monkeypatch, sync_db_path):
    """A launch that returned no game (no send permission) has no lobby to
    start, so there is no panel to send and no chore to sign off."""
    sign_off = AsyncMock()
    monkeypatch.setattr(rushmore_cog, "sign_off_game_chore", sign_off)
    bot = _SpyBot(sync_db_path)
    cog = rushmore_cog.RushmoreCog(bot)  # type: ignore[arg-type]
    cog.launch = AsyncMock(return_value=None)  # type: ignore[method-assign]
    panel = AsyncMock()
    cog.open_host_panel = panel  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (778, 4242),
    )
    view = rushmore_cog.RushmoreRecapView(
        "old", host_id=1, cog=cog, settings={}, items=_recap_items(),
    )
    inter = SimpleNamespace(
        user=SimpleNamespace(id=42, display_name="Someone"), guild_id=4242, channel_id=778,
        channel=SimpleNamespace(id=778, name="games", guild=None),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )
    await view.run_again(inter)  # type: ignore[arg-type]
    panel.assert_not_awaited()
    sign_off.assert_not_awaited()


def test_schedule_schema_pick_seconds_tracks_the_code_default():
    """A scheduled or rotation-launched draft must offer the same pick timer a
    member-started one does.

    ``SCHEDULE_OPTION_SCHEMA`` is what the Scheduling and Feature Rotation
    option forms render and submit, and an explicit value in the submitted
    options beats both the per-guild dial and ``DEFAULT_PICK_SECONDS``. While
    the schema restated the number, raising the code default to 45 silently
    left every scheduled draft on the old 30 seconds.
    """
    from bot_modules.games.constants import SCHEDULE_OPTION_SCHEMA
    from bot_modules.games.rushmore.logic import DEFAULT_PICK_SECONDS

    timer = next(
        opt for opt in SCHEDULE_OPTION_SCHEMA["rushmore"] if opt["name"] == "timer"
    )
    assert timer["default"] == DEFAULT_PICK_SECONDS
