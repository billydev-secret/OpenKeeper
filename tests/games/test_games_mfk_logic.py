"""Tests for the extracted Marry/Fornicate/Kiss pure-logic modules.

Covers ``bot_modules/games/mfk/logic.py`` (participant toggle, label
parsing, target assignment, payload serialisation) and the cog's
Components V2 wiring (``bot_modules/games/mfk/cog.py``). The card
builders themselves (``bot_modules/games/mfk/layouts.py``) have their own
suite, ``tests/games/test_mfk_layouts.py`` — mirrors the games_traditional
template: the cog stays thin; this module proves the extracted pieces work
without spinning up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games.mfk.logic import (
    DEFAULT_LABELS,
    MAX_PARTICIPANTS,
    MIN_PARTICIPANTS,
    TARGETS_PER_PLAYER,
    assign_targets,
    parse_labels,
    serialize_assignments,
    toggle_participant,
)

def _named(uid: int) -> str:
    return f"Member{uid}"


def _seen(view) -> str:
    """Every ``TextDisplay``'s content in a ``LayoutView``, joined."""
    import discord

    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


# ── toggle_participant ───────────────────────────────────────────────


def test_toggle_participant_adds_new_user_and_returns_joined():
    payload: dict = {}
    action = toggle_participant(payload, user_id=42)
    assert action == "joined"
    assert payload["participants"] == [42]


def test_toggle_participant_removes_existing_user_and_returns_left():
    payload = {"participants": [42]}
    action = toggle_participant(payload, user_id=42)
    assert action == "left"
    assert payload["participants"] == []


def test_toggle_participant_creates_list_when_missing():
    payload: dict = {}
    toggle_participant(payload, 1)
    assert "participants" in payload


def test_toggle_participant_preserves_other_users():
    payload = {"participants": [1, 2, 3]}
    toggle_participant(payload, 2)
    assert payload["participants"] == [1, 3]


def test_toggle_participant_appends_to_existing_list():
    payload = {"participants": [1]}
    action = toggle_participant(payload, 2)
    assert action == "joined"
    assert payload["participants"] == [1, 2]


# ── participant cap (MAX_PARTICIPANTS) ───────────────────────────────


def test_max_participants_matches_assignments_card_row_cap():
    """The assignments card packs one line per player — guard against drift."""
    assert MAX_PARTICIPANTS == 25


def test_toggle_participant_rejects_new_joiner_past_cap():
    """A new joiner past the cap is refused (would overflow the assignments card)."""
    payload = {"participants": list(range(MAX_PARTICIPANTS))}
    action = toggle_participant(payload, user_id=9999)
    assert action == "full"
    assert 9999 not in payload["participants"]
    assert len(payload["participants"]) == MAX_PARTICIPANTS


def test_toggle_participant_allows_last_slot():
    payload = {"participants": list(range(MAX_PARTICIPANTS - 1))}
    action = toggle_participant(payload, user_id=9999)
    assert action == "joined"
    assert len(payload["participants"]) == MAX_PARTICIPANTS


def test_toggle_participant_leave_allowed_from_full_pool():
    """Leaving is never blocked, even at the cap."""
    payload = {"participants": list(range(MAX_PARTICIPANTS))}
    action = toggle_participant(payload, user_id=0)
    assert action == "left"
    assert len(payload["participants"]) == MAX_PARTICIPANTS - 1


def test_toggle_participant_respects_custom_cap():
    payload = {"participants": [1, 2]}
    action = toggle_participant(payload, 3, max_participants=2)
    assert action == "full"
    assert payload["participants"] == [1, 2]


# ── parse_labels ─────────────────────────────────────────────────────


def test_parse_labels_none_input_returns_none_labels_no_error():
    """Empty input means caller falls back to DEFAULT_LABELS."""
    labels, error = parse_labels(None)
    assert labels is None
    assert error is None


def test_parse_labels_empty_string_returns_none_no_error():
    labels, error = parse_labels("")
    assert labels is None
    assert error is None


def test_parse_labels_valid_three_labels():
    labels, error = parse_labels("Cruise, Wedding, Vacation")
    assert error is None
    assert labels == ["Cruise", "Wedding", "Vacation"]


def test_parse_labels_strips_whitespace_around_each_entry():
    labels, error = parse_labels("  A  ,  B  ,  C  ")
    assert error is None
    assert labels == ["A", "B", "C"]


def test_parse_labels_ignores_empty_segments():
    """Trailing commas / double-commas shouldn't count toward the total."""
    labels, error = parse_labels("A,,B,C")
    assert error is None
    assert labels == ["A", "B", "C"]


def test_parse_labels_too_few_returns_error():
    labels, error = parse_labels("A, B")
    assert labels is None
    assert error is not None
    assert "got 2" in error


def test_parse_labels_too_many_returns_error():
    labels, error = parse_labels("A, B, C, D")
    assert labels is None
    assert error is not None
    assert "got 4" in error


def test_parse_labels_error_message_mentions_example():
    _, error = parse_labels("only one")
    assert error is not None
    assert "Cruise" in error  # Example in the message


# ── assign_targets ───────────────────────────────────────────────────


def test_assign_targets_below_min_raises():
    with pytest.raises(ValueError):
        assign_targets([1, 2, 3])  # MIN is 4


def test_assign_targets_exactly_min_works():
    rng = random.Random(0)
    out = assign_targets([1, 2, 3, 4], rng=rng)
    assert set(out.keys()) == {1, 2, 3, 4}
    for player_id, targets in out.items():
        assert len(targets) == TARGETS_PER_PLAYER
        assert player_id not in targets


def test_assign_targets_no_self_targeting_in_larger_pool():
    rng = random.Random(0)
    pool = list(range(1, 11))
    out = assign_targets(pool, rng=rng)
    for player_id, targets in out.items():
        assert player_id not in targets


def test_assign_targets_three_distinct_targets_per_player():
    rng = random.Random(7)
    pool = list(range(1, 8))
    out = assign_targets(pool, rng=rng)
    for targets in out.values():
        assert len(set(targets)) == TARGETS_PER_PLAYER


def test_assign_targets_uses_module_random_when_rng_omitted():
    """No rng path: still returns a valid assignment for every player."""
    random.seed(0)
    out = assign_targets([1, 2, 3, 4, 5])
    assert set(out.keys()) == {1, 2, 3, 4, 5}
    for player_id, targets in out.items():
        assert player_id not in targets
        assert len(targets) == 3


def test_assign_targets_deterministic_with_seeded_rng():
    """Same seed → same assignment shape."""
    pool = [1, 2, 3, 4, 5]
    out1 = assign_targets(pool, rng=random.Random(42))
    out2 = assign_targets(pool, rng=random.Random(42))
    assert out1 == out2


# ── assign_targets: the no-contact gate ──────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [pytest.param({(1, 2)}, id="stored-order"), pytest.param({(2, 1)}, id="reversed")],
)
def test_assign_targets_never_hands_a_no_contact_pair_to_each_other(forbidden):
    """Neither member of a blocked pair ever appears in the other's three."""
    for seed in range(40):
        out = assign_targets([1, 2, 3, 4, 5, 6], rng=random.Random(seed), forbidden_pairs=forbidden)
        assert 2 not in out[1] and 1 not in out[2]
        for player_id, targets in out.items():
            assert player_id not in targets
            assert len(targets) == TARGETS_PER_PLAYER


@pytest.mark.parametrize(
    ("pool", "forbidden"),
    [
        # Four players, one blocked pair: the two blocked members could only
        # be shown two names each, which would name the pair on the card.
        pytest.param([1, 2, 3, 4], {(1, 2)}, id="four-with-one-pair"),
        # One member blocked from everyone: an empty list would be an empty
        # embed field, which Discord rejects.
        pytest.param([1, 2, 3, 4, 5], {(1, 2), (1, 3), (1, 4), (1, 5)}, id="blocked-from-all"),
    ],
)
def test_assign_targets_refuses_when_anyone_cannot_fill_three_names(pool, forbidden):
    """The draw is all-or-nothing: ``{}`` when the pairs leave any player
    short, so the cog's ordinary too-few-players refusal covers it."""
    assert assign_targets(pool, rng=random.Random(0), forbidden_pairs=forbidden) == {}


def test_assign_targets_unrelated_players_are_unaffected_by_a_pair():
    out = assign_targets([1, 2, 3, 4, 5], rng=random.Random(3), forbidden_pairs={(1, 2)})
    for player_id in (3, 4, 5):
        assert len(out[player_id]) == TARGETS_PER_PLAYER


# ── serialize_assignments ────────────────────────────────────────────


def test_serialize_assignments_stringifies_player_keys():
    assert serialize_assignments({1: [2, 3, 4]}) == {"1": [2, 3, 4]}


def test_serialize_assignments_preserves_target_lists():
    """Target ids stay as ints in the serialized output."""
    result = serialize_assignments({100: [200, 300, 400]})
    assert result["100"] == [200, 300, 400]
    for val in result["100"]:
        assert isinstance(val, int)


def test_serialize_assignments_empty():
    assert serialize_assignments({}) == {}


# ── DEFAULT_LABELS / constants sanity ────────────────────────────────


def test_default_labels_has_three_entries():
    assert len(DEFAULT_LABELS) == TARGETS_PER_PLAYER


def test_min_participants_constant():
    assert MIN_PARTICIPANTS == 4


# ── integration ──────────────────────────────────────────────────────


def test_full_lobby_flow_four_players():
    """Four players join, then assignments are produced."""
    payload: dict = {}
    for uid in [1, 2, 3, 4]:
        toggle_participant(payload, uid)
    assert payload["participants"] == [1, 2, 3, 4]
    out = assign_targets(payload["participants"], rng=random.Random(0))
    for uid, targets in out.items():
        assert uid not in targets
        assert len(targets) == 3
    # Serialize for end-game payload
    serialized = serialize_assignments(out)
    assert set(serialized.keys()) == {"1", "2", "3", "4"}


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.games.mfk.cog as mfk_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.games.db import GamesDb  # noqa: E402
from bot_modules.no_contact.service import add_pair  # noqa: E402
from tests.fakes import FakeGuild, FakeMember, FakeUser, fake_interaction  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_close_assign_pays_pool(monkeypatch, sync_db_path):
    """Assigning roles pays everyone who joined the pool."""
    spy = AsyncMock()
    monkeypatch.setattr(mfk_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "mfk", payload={"participants": [1, 2, 3, 4]})
    view = mfk_cog.MFKView(gid, 1, bot.games_db, bot)  # type: ignore[arg-type]
    interaction = fake_interaction(user=FakeUser(id=1))
    interaction.guild = None
    await view.close_assign(interaction)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3, 4]
    assert call.kwargs["bot"] is bot


# ── Close & Assign: the no-contact gate and the resolved names ───────


def _guild_of(*uids: int) -> FakeGuild:
    guild = FakeGuild(id=9001)
    for uid in uids:
        guild.members[uid] = FakeMember(id=uid, name=f"user{uid}", display_name=f"Member{uid}")
    return guild


async def _close(monkeypatch, sync_db_path, participants: list[int]):
    spy = AsyncMock()
    monkeypatch.setattr(mfk_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    guild = _guild_of(*participants)
    gid = await create_game(
        bot.games_db, 100, participants[0], "mfk",
        payload={"participants": list(participants)},
    )
    view = mfk_cog.MFKView(gid, participants[0], bot.games_db, bot)  # type: ignore[arg-type]
    interaction = fake_interaction(user=guild.members[participants[0]], guild=guild)
    await view.close_assign(interaction)
    return interaction, spy


async def test_close_assign_never_hands_a_no_contact_pair_to_each_other(monkeypatch, sync_db_path):
    add_pair(sync_db_path, 9001, 1, 2, created_by=1, protected_user_id=1)
    for _ in range(10):
        _, spy = await _close(monkeypatch, sync_db_path, [1, 2, 3, 4, 5])
        assert spy.await_args is not None
        assignments = spy.await_args.kwargs["payload"]["assignments"]
        assert 2 not in assignments["1"] and 1 not in assignments["2"]
        assert spy.await_args.kwargs["player_ids"] == [1, 2, 3, 4, 5]


async def test_close_assign_card_pings_players_in_the_body_and_names_their_targets(
    monkeypatch, sync_db_path
):
    """A Components V2 send has no ``content=`` to carry the reveal ping, so
    it moved into the card itself: each player's own line opens with a raw
    mention (the deliberate ping) while their drawn targets are still named
    through the resolver."""
    interaction, _ = await _close(monkeypatch, sync_db_path, [1, 2, 3, 4])
    call = interaction.followup.send.await_args
    assert "content" not in call.kwargs
    view = call.kwargs["view"]
    text = _seen(view)
    for uid in (1, 2, 3, 4):
        assert f"<@{uid}>" in text
    for name in ("Member1", "Member2", "Member3", "Member4"):
        assert name in text


async def test_close_assign_actually_notifies_every_assigned_player(
    monkeypatch, sync_db_path
):
    """The reveal is where a player learns their own targets, so it pings.

    The bot suppresses mentions by default, and this line is built by
    ``name_resolver.mention(uid)`` rather than a ``.mention`` attribute — a
    shape the static sweep could not read, so the reveal went out looking
    exactly the same and notifying nobody.
    """
    interaction, _ = await _close(monkeypatch, sync_db_path, [1, 2, 3, 4])
    allowed = interaction.followup.send.await_args.kwargs["allowed_mentions"]
    assert sorted(o.id for o in allowed.users) == [1, 2, 3, 4]
    assert allowed.everyone is False and allowed.roles == []
