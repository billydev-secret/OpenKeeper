"""Tests for the extracted Most Likely To pure-logic module and its cog.

Covers ``bot_modules/games/mlt/logic.py`` (lobby membership, vote
recording, per-round tally, crown winners, payload codecs, prompt
queue) and the cog glue that needs no Discord. The Components V2 cards
are covered in ``test_games_mlt_layouts.py``.

Mirrors the games_nhie template since MLT is the closest sibling
(per-round single-target voting). The cog stays thin; this module
proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games.mlt.logic import (
    MAX_PLAYERS,
    MIN_PLAYERS,
    add_player,
    apply_vote,
    bump_crowns,
    can_start,
    clamp_player_limits,
    encode_round_votes,
    find_round_winners,
    is_eligible_voter,
    lobby_is_full,
    pop_next_prompt,
    queue_prompt,
    record_vote,
    remove_player,
    tally_votes,
)


# ── add_player / remove_player ───────────────────────────────────────


def test_add_player_appends_new_uid():
    players: list[int] = []
    assert add_player(players, 42) is True
    assert players == [42]


def test_add_player_idempotent_re_press():
    players: list[int] = [42]
    assert add_player(players, 42) is False
    assert players == [42]


def test_add_player_preserves_order():
    players: list[int] = [1, 2]
    add_player(players, 3)
    add_player(players, 4)
    assert players == [1, 2, 3, 4]


def test_remove_player_removes_existing():
    players: list[int] = [1, 2, 3]
    assert remove_player(players, 2) is True
    assert players == [1, 3]


def test_remove_player_noop_when_absent():
    players: list[int] = [1, 3]
    assert remove_player(players, 2) is False
    assert players == [1, 3]


def test_add_then_remove_round_trip():
    players: list[int] = []
    add_player(players, 7)
    add_player(players, 9)
    remove_player(players, 7)
    assert players == [9]


# ── lobby cap (MAX_PLAYERS) ──────────────────────────────────────────


def test_max_players_matches_select_option_limit():
    """A Discord Select hard-caps at 25 options — guard against drift."""
    assert MAX_PLAYERS == 25


@pytest.mark.parametrize(
    ("initial", "uid", "max_players", "expected_added", "expected_players"),
    [
        # A new joiner is dropped once the lobby is full (would 400 the vote select).
        pytest.param(
            list(range(MAX_PLAYERS)),
            9999,
            None,
            False,
            list(range(MAX_PLAYERS)),
            id="rejects-new-uid-past-cap",
        ),
        pytest.param(
            list(range(MAX_PLAYERS - 1)),
            9999,
            None,
            True,
            [*range(MAX_PLAYERS - 1), 9999],
            id="allows-last-slot-at-cap",
        ),
        pytest.param(
            list(range(MAX_PLAYERS)),
            0,
            None,
            False,
            list(range(MAX_PLAYERS)),
            id="existing-uid-at-cap-still-idempotent",
        ),
        pytest.param([1, 2], 3, 2, False, [1, 2], id="respects-custom-cap"),
    ],
)
def test_add_player_cap_variants(
    initial: list[int],
    uid: int,
    max_players: int | None,
    expected_added: bool,
    expected_players: list[int],
):
    players = list(initial)
    kwargs = {} if max_players is None else {"max_players": max_players}
    assert add_player(players, uid, **kwargs) is expected_added
    assert players == expected_players


def test_lobby_is_full_false_below_cap():
    assert lobby_is_full(list(range(MAX_PLAYERS - 1))) is False


def test_lobby_is_full_true_at_cap():
    assert lobby_is_full(list(range(MAX_PLAYERS))) is True


# ── can_start ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("players", "min_players", "expected"),
    [
        pytest.param([1, 2], None, False, id="below-minimum"),
        pytest.param([1, 2, 3], None, True, id="at-minimum"),
        pytest.param([1, 2, 3, 4, 5], None, True, id="above-minimum"),
        pytest.param([1, 2], 2, True, id="custom-threshold-at"),
        pytest.param([1], 2, False, id="custom-threshold-below"),
    ],
)
def test_can_start(players: list[int], min_players: int | None, expected: bool):
    kwargs = {} if min_players is None else {"min_players": min_players}
    assert can_start(players, **kwargs) is expected


def test_can_start_min_players_constant_matches_cog_threshold():
    """The cog's hard-coded check used 3 — guard against drift."""
    assert MIN_PLAYERS == 3


# ── apply_vote ───────────────────────────────────────────────────────


def test_apply_vote_records_new_vote_returns_false():
    """A first-time voter is not 'changed' — only switching is."""
    votes: dict[int, int] = {}
    changed = apply_vote(votes, voter_id=1, target_id=2)
    assert changed is False
    assert votes == {1: 2}


def test_apply_vote_switching_target_returns_true():
    votes: dict[int, int] = {1: 2}
    changed = apply_vote(votes, voter_id=1, target_id=3)
    assert changed is True
    assert votes == {1: 3}


def test_apply_vote_re_voting_same_target_returns_false():
    """An idempotent re-press for the same target doesn't count as a switch."""
    votes: dict[int, int] = {1: 2}
    changed = apply_vote(votes, voter_id=1, target_id=2)
    assert changed is False
    assert votes == {1: 2}


def test_apply_vote_two_voters_independent():
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=2, target_id=10)
    assert votes == {1: 10, 2: 10}


def test_apply_vote_allows_self_vote():
    """The HOW_TO_PLAY explicitly allows voting for yourself."""
    votes: dict[int, int] = {}
    changed = apply_vote(votes, voter_id=42, target_id=42)
    assert changed is False
    assert votes == {42: 42}


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_counts_per_target():
    votes = {1: 10, 2: 10, 3: 20}
    tally = tally_votes(votes, players=[10, 20, 30])
    assert tally == {10: 2, 20: 1, 30: 0}


def test_tally_votes_zero_for_uncovered_players():
    """Every player in the pool gets an entry, even with no votes."""
    tally = tally_votes({}, players=[1, 2, 3])
    assert tally == {1: 0, 2: 0, 3: 0}


def test_tally_votes_empty_players_empty_votes_empty_tally():
    assert tally_votes({}, players=[]) == {}


def test_tally_votes_includes_off_pool_target():
    """If a vote sneaks past eligibility, the target still appears."""
    votes = {1: 999}
    tally = tally_votes(votes, players=[1])
    assert tally == {1: 0, 999: 1}


def test_tally_votes_handles_unanimous_vote():
    votes = {1: 5, 2: 5, 3: 5, 4: 5}
    tally = tally_votes(votes, players=[5, 6])
    assert tally == {5: 4, 6: 0}


# ── find_round_winners ───────────────────────────────────────────────


def test_find_round_winners_single_winner():
    tally = {1: 3, 2: 1, 3: 0}
    assert find_round_winners(tally) == [1]


def test_find_round_winners_tie_returns_all_top():
    tally = {1: 2, 2: 2, 3: 1}
    winners = find_round_winners(tally)
    assert set(winners) == {1, 2}
    assert len(winners) == 2


def test_find_round_winners_empty_when_no_votes_cast():
    """Top score is 0 → no crowns awarded."""
    tally = {1: 0, 2: 0, 3: 0}
    assert find_round_winners(tally) == []


def test_find_round_winners_empty_when_tally_empty():
    assert find_round_winners({}) == []


@pytest.mark.parametrize(
    "tally, votes, expected",
    [
        # Everyone voted for themselves: three-way tie, no outside support —
        # nobody is crowned (this is the "crown everyone" case).
        pytest.param({1: 1, 2: 1, 3: 1}, {1: 1, 2: 2, 3: 3}, [], id="all-self-votes"),
        # 1 and 2 tie at 2; 1's are a self-vote plus one outside vote, 2's
        # are both outside votes — the crown goes to 2.
        pytest.param(
            {1: 2, 2: 2, 3: 0}, {1: 1, 3: 1, 4: 2, 5: 2}, [2], id="tie-broken-by-outside-votes",
        ),
        # Both finalists have one outside vote each: still a shared crown.
        pytest.param({1: 2, 2: 2}, {1: 1, 2: 2, 3: 1, 4: 2}, [1, 2], id="genuine-tie-stays"),
        # A lone leader keeps the crown even if their only vote is their own.
        pytest.param({1: 1, 2: 0}, {1: 1}, [1], id="single-self-vote-leader"),
        # No votes: nothing to crown, with or without the votes map.
        pytest.param({1: 0, 2: 0}, {}, [], id="no-votes"),
    ],
)
def test_find_round_winners_breaks_top_ties_without_self_votes(tally, votes, expected):
    """vote-games-62: self-votes count, but they don't decide a tie."""
    assert find_round_winners(tally, votes) == expected


def test_find_round_winners_three_way_tie():
    tally = {1: 1, 2: 1, 3: 1}
    winners = find_round_winners(tally)
    assert sorted(winners) == [1, 2, 3]


# ── bump_crowns ──────────────────────────────────────────────────────


def test_bump_crowns_increments_per_user():
    crowns: dict[str, int] = {}
    bump_crowns(crowns, [1, 2])
    assert crowns == {"1": 1, "2": 1}


def test_bump_crowns_preserves_existing_counts():
    crowns: dict[str, int] = {"1": 3}
    bump_crowns(crowns, [1])
    assert crowns == {"1": 4}


def test_bump_crowns_handles_tie_winners():
    crowns: dict[str, int] = {"1": 1}
    bump_crowns(crowns, [1, 2, 3])
    assert crowns == {"1": 2, "2": 1, "3": 1}


def test_bump_crowns_empty_winners_noop():
    crowns: dict[str, int] = {"1": 5}
    bump_crowns(crowns, [])
    assert crowns == {"1": 5}


def test_bump_crowns_string_keys_match_payload_convention():
    """The persisted payload uses str keys — guard against int leakage."""
    crowns: dict[str, int] = {}
    bump_crowns(crowns, [123456789])
    assert "123456789" in crowns
    assert 123456789 not in crowns


# ── encode_round_votes ───────────────────────────────────────────────


def test_encode_round_votes_stringifies_voter_keys():
    encoded = encode_round_votes({1: 10, 2: 20})
    assert encoded == {"1": 10, "2": 20}


def test_encode_round_votes_keeps_target_as_int():
    encoded = encode_round_votes({1: 999})
    assert encoded["1"] == 999
    assert isinstance(encoded["1"], int)


def test_encode_round_votes_empty_dict_round_trip():
    assert encode_round_votes({}) == {}


# ── is_eligible_voter ────────────────────────────────────────────────


def test_is_eligible_voter_true_when_in_pool():
    assert is_eligible_voter(42, [1, 42, 99]) is True


def test_is_eligible_voter_false_when_outside_pool():
    assert is_eligible_voter(42, [1, 2, 3]) is False


def test_is_eligible_voter_false_for_empty_pool():
    assert is_eligible_voter(42, []) is False


# ── queue_prompt / pop_next_prompt ───────────────────────────────────


def test_queue_prompt_appends_and_returns_count():
    queued: list[str] = []
    n = queue_prompt(queued, "win a staring contest")
    assert n == 1
    assert queued == ["win a staring contest"]


def test_queue_prompt_strips_whitespace():
    queued: list[str] = []
    queue_prompt(queued, "   say something nice   ")
    assert queued == ["say something nice"]


def test_queue_prompt_ignores_empty_after_strip():
    queued: list[str] = ["x"]
    n = queue_prompt(queued, "    ")
    assert n == 1  # unchanged
    assert queued == ["x"]


def test_queue_prompt_returns_running_total():
    queued: list[str] = []
    assert queue_prompt(queued, "a") == 1
    assert queue_prompt(queued, "b") == 2
    assert queue_prompt(queued, "c") == 3


def test_pop_next_prompt_returns_first_and_rest():
    next_p, remaining = pop_next_prompt(["a", "b", "c"])
    assert next_p == "a"
    assert remaining == ["b", "c"]


def test_pop_next_prompt_empty_queue():
    next_p, remaining = pop_next_prompt([])
    assert next_p is None
    assert remaining == []


def test_pop_next_prompt_does_not_mutate_original():
    """The original list must be untouched — the cog mutates it elsewhere."""
    original = ["a", "b"]
    pop_next_prompt(original)
    assert original == ["a", "b"]


def test_pop_next_prompt_single_item_leaves_empty_remaining():
    next_p, remaining = pop_next_prompt(["only"])
    assert next_p == "only"
    assert remaining == []


# ── record_vote (the no-contact gate) ────────────────────────────────


def test_record_vote_counts_an_ordinary_pick():
    votes: dict[int, int] = {}
    assert record_vote(votes, 1, 2, blocked=False) == (True, False)
    assert votes == {1: 2}
    assert record_vote(votes, 1, 3, blocked=False) == (True, True)
    assert votes == {1: 3}


def test_record_vote_drops_a_blocked_pick_but_reports_the_same_change_flag():
    """A no-contact pair's vote is never stored — the results only show
    counts, so a dropped vote is invisible — while the ``changed`` flag
    comes back exactly as it would for a counted vote, so the voter's
    ephemeral ack reads identically either way."""
    votes = {1: 2}
    assert record_vote(votes, 1, 3, blocked=True) == (False, True)
    assert votes == {1: 2}
    assert record_vote({}, 1, 3, blocked=True) == (False, False)


# ── sanity / integration ─────────────────────────────────────────────


def test_full_round_flow_single_winner_then_crown():
    """Cast votes, tally, find winners, bump crowns — end to end."""
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=2, target_id=10)
    apply_vote(votes, voter_id=3, target_id=20)
    tally = tally_votes(votes, players=[10, 20, 30])
    assert tally == {10: 2, 20: 1, 30: 0}
    winners = find_round_winners(tally)
    assert winners == [10]
    crowns: dict[str, int] = {}
    bump_crowns(crowns, winners)
    assert crowns == {"10": 1}


def test_full_round_flow_self_vote_tie_crowns_nobody():
    """Three players, three self-votes: the round banks no crown at all."""
    players = [1, 2, 3]
    votes: dict[int, int] = {}
    for uid in players:
        apply_vote(votes, uid, uid)
    tally = tally_votes(votes, players)
    winners = find_round_winners(tally, votes)
    crowns: dict[str, int] = {}
    bump_crowns(crowns, winners)
    assert winners == [] and crowns == {}


def test_full_round_flow_tie_awards_two_crowns():
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=2, target_id=20)
    tally = tally_votes(votes, players=[10, 20])
    winners = find_round_winners(tally)
    crowns: dict[str, int] = {}
    bump_crowns(crowns, winners)
    assert crowns == {"10": 1, "20": 1}


def test_voter_changes_pick_only_last_vote_counted():
    """The cog calls apply_vote on every press — the tally must reflect
    only the final pick, not any history."""
    votes: dict[int, int] = {}
    apply_vote(votes, voter_id=1, target_id=10)
    apply_vote(votes, voter_id=1, target_id=20)  # switched
    apply_vote(votes, voter_id=1, target_id=30)  # switched again
    tally = tally_votes(votes, players=[10, 20, 30])
    assert tally == {10: 0, 20: 0, 30: 1}


def test_queue_then_pop_full_cycle():
    queued: list[str] = []
    queue_prompt(queued, "first")
    queue_prompt(queued, "second")
    queue_prompt(queued, "third")
    next_p, remaining = pop_next_prompt(queued)
    assert next_p == "first"
    assert remaining == ["second", "third"]


@pytest.mark.parametrize("vote_kind_unused", [None])
def test_lobby_minimum_enforced_then_passes(vote_kind_unused):
    """Two players can't start; adding a third unlocks it."""
    players: list[int] = []
    add_player(players, 1)
    add_player(players, 2)
    assert can_start(players) is False
    add_player(players, 3)
    assert can_start(players) is True


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402

import discord  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.games.mlt.cog as mlt_cog  # noqa: E402
from bot_modules.games.utils.game_manager import (  # noqa: E402
    create_game,
    get_active_game_by_id,
    get_game_payload,
)
from bot_modules.games.db import GamesDb  # noqa: E402


def _ids(view) -> list[str]:
    return [i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)]


def _view_text(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_run_round_empty_bank_opens_a_waiting_round(monkeypatch, sync_db_path):
    """platform-27 / vote-games-50: a bare /mlt used to fill a 3+ lobby and
    die at Start on an empty bank. The slash entry now refuses an empty bank
    up front, and a round that finds nothing to serve waits for a posed prompt
    instead of ending the game."""
    monkeypatch.setattr(mlt_cog, "get_mlt_prompt", AsyncMock(return_value=None))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "mlt", payload={"rounds": {}, "players": [1, 2, 3], "crowns": {}})
    cog = mlt_cog.MLTCog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(
        id=100, name="games", guild=None,
        send=AsyncMock(return_value=SimpleNamespace(id=555, edit=AsyncMock())),
    )
    await cog._run_round(None, gid, 1, "Host", 1, [1, 2, 3], channel)

    view = bot.active_views[gid]
    assert view.waiting is True
    assert _ids(view) == ["mlt_htp2", "mlt_pose"]
    assert await get_active_game_by_id(bot.games_db, gid) is not None
    await view.begin_round("win a staring contest", SimpleNamespace(id=555, edit=AsyncMock()))
    assert view.waiting is False
    assert _ids(view) == ["mlt_htp2", "mlt_vote_select", "mlt_pose"]
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["prompt"] == "win a staring contest"


async def test_end_game_posts_final_standings_and_pays_every_voter(monkeypatch, sync_db_path):
    """vote-games-52: the host's End Game (and /games end) close through the
    paying path with the crown standings, tallying the open round first."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(mlt_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {
        "rounds": {"1": {"votes": {"1": 2, "2": 2}, "prompt": "p1"}, "2": {"votes": {}, "prompt": "p2"}},
        "players": [1, 2, 3], "crowns": {"2": 1},
    }
    gid = await create_game(bot.games_db, 100, 1, "mlt", payload=payload)
    cog = mlt_cog.MLTCog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, name="games", guild=None, send=AsyncMock())
    view = cog._build_vote_view(
        game_id=gid, host_id=1, host_name="Host", round_num=2, players=[1, 2, 3],
        channel=channel, prompt="p2",
    )
    bot.active_views[gid] = view
    view.votes = {3: 1}

    assert view.finish_callback is not None
    await view.finish_callback(SimpleNamespace(id=555, edit=AsyncMock()), "ended")

    cards = [_view_text(c.kwargs["view"]) for c in channel.send.await_args_list]
    assert len(cards) == 2  # the open round's results, then the standings
    assert "Round 2 Results" in cards[0] and "Final Standings" in cards[-1]
    assert _ids(view) == [], "a closed round carries no controls"
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3]
    assert call.kwargs["bot"] is bot
    assert call.kwargs["reason"] == "ended"
    assert call.kwargs["payload"]["crowns"] == {"2": 1, "1": 1}  # round 2's crown banked
    assert gid not in bot.active_views



# ── lobby limits (the dashboard dials, finally enforced) ────────────────


def test_clamp_player_limits_never_exceeds_the_select_cap():
    """MAX_PLAYERS exists because the per-round vote is a Discord Select and a
    bigger lobby would 400 the round message. The dashboard dial used to offer
    up to 200, so a stored value has to be clamped rather than trusted."""
    assert clamp_player_limits(3, 200) == (3, 25)


def test_clamp_player_limits_raises_a_ceiling_below_its_floor():
    assert clamp_player_limits(8, 4) == (8, 8)


def test_clamp_player_limits_treats_zero_as_unset():
    """0 meant "no limit" on the old dial. There is no no-limit here."""
    assert clamp_player_limits(0, 0) == (MIN_PLAYERS, MAX_PLAYERS)


def test_add_player_respects_a_configured_ceiling():
    """The join handler's guard is not the only thing holding the line — the
    logic layer enforces it too, so a caller that forgets the guard cannot
    overfill the lobby."""
    players = [1, 2, 3]
    assert add_player(players, 4, 3) is False
    assert players == [1, 2, 3]
    assert add_player(players, 4, 4) is True


# ── vote select wiring: the no-contact list reaches record_vote ─────────

from bot_modules.no_contact.service import add_pair  # noqa: E402


class _VoteResponse:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class _VoteGuild:
    def __init__(self, members: dict[int, str]) -> None:
        self.id = 77
        self._members = {
            uid: SimpleNamespace(id=uid, display_name=name) for uid, name in members.items()
        }

    def get_member(self, uid):
        return self._members.get(uid)


async def _vote(sync_db_path, *, voter: int, target: int, pair=None):
    bot = _SpyBot(sync_db_path)
    if pair is not None:
        add_pair(sync_db_path, 77, pair[0], pair[1], created_by=99)
    gid = await create_game(
        bot.games_db, 100, 1, "mlt",
        payload={"rounds": {"1": {"prompt": "x", "votes": {}}}, "players": [1, 2, 3]},
    )
    guild = _VoteGuild({1: "Ann", 2: "Bee", 3: "Cat"})
    view = mlt_cog.MLTVoteView(
        gid, 1, "x", 1, [1, 2, 3], bot.games_db, bot, "Ann", guild, AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=guild.get_member(voter),
        guild=guild,
        guild_id=77,
        channel=SimpleNamespace(id=100, name="games", guild=guild),
        data={"values": [str(target)]},
        response=_VoteResponse(),
    )
    await view._vote_select_callback(interaction)  # type: ignore[arg-type]
    return view, interaction


async def test_vote_select_records_an_ordinary_vote(sync_db_path):
    view, interaction = await _vote(sync_db_path, voter=1, target=2)
    assert view.votes == {1: 2}
    assert interaction.response.messages[0][0] == "✅ Voted for **Bee**"


async def test_vote_select_drops_a_no_contact_vote_behind_the_ordinary_ack(sync_db_path):
    """Voter 1 and target 2 are a no-contact pair: the pick is not stored,
    yet the voter sees the same ack an ordinary vote gets."""
    view, interaction = await _vote(sync_db_path, voter=1, target=2, pair=(1, 2))
    assert view.votes == {}
    assert interaction.response.messages[0][0] == "✅ Voted for **Bee**"
