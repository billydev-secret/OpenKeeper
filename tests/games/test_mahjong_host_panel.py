"""Meadow Mahjong's Cancel and Close Table live on the ephemeral host panel
(``/games host``), not on the public table card (Ben, 2026-10-01).

Pinned here: the card no longer carries them, the panel does (red), the
gates that decided who could press them on the card still decide it on the
panel, ``/games host`` finds a table through the cog (a table is not a
``games_active_games`` row), and the two small fixes made alongside —
an uncached seat renders as ``User <id>``, and the My Settings menu stops
the view it replaces.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.layout import card
from bot_modules.games.mahjong import game_logic as G
from bot_modules.games.mahjong import layouts as mj_layouts
from bot_modules.games.mahjong import views as mj_views
from bot_modules.games.mahjong.cog import MahjongCog, MOVED_TO_HOST_PANEL, NOT_AT_TABLE_REPLY
from bot_modules.games.mahjong.game_logic import (
    ActionRejected,
    GameState,
    Phase,
    SeatState,
    TableConfig,
    close_table_refusal,
    table_controls_refusal,
)
from bot_modules.games.mahjong.mahjong_service import STALE_TABLE
from bot_modules.games.utils import host_panel
from tests.fakes import FakeMember, fake_interaction

HOST, GUEST, STRANGER, MOD = 100, 101, 999, 5


def _state(phase: Phase, *, seats=(HOST, GUEST)) -> GameState:
    return GameState(
        config=TableConfig(seat_count=2), host=HOST,
        seats=[SeatState(member_id=m) for m in seats], phase=phase,
    )


def _button_ids(items) -> dict[str, discord.ButtonStyle]:
    return {
        b.custom_id: b.style for b in card(items).walk_children()
        if isinstance(b, discord.ui.Button) and b.custom_id
    }


def _text(items) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


# ── the cards ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("phase", [p for p in Phase])
def test_the_public_card_carries_no_cancel_or_close(phase):
    ids = _button_ids(mj_layouts.phase_controls(42, phase, add_bot=True))
    assert not {"mmj:42:cancel", "mmj:42:close"} & set(ids)


def test_the_host_panel_carries_both_red():
    ids = _button_ids(mj_layouts.build_host_panel_layout())
    assert ids == {
        mj_layouts.HOST_CANCEL_ID: discord.ButtonStyle.danger,
        mj_layouts.HOST_CLOSE_ID: discord.ButtonStyle.danger,
    }
    assert host_panel.HOST_COMMAND in _text(mj_layouts.build_host_panel_layout())


@pytest.mark.parametrize(
    ("phase", "pointed"),
    [
        pytest.param(Phase.LOBBY, True, id="lobby"),
        pytest.param(Phase.SETTLE, True, id="settle"),
        pytest.param(Phase.AWAIT_DISCARD, False, id="mid-hand"),
        pytest.param(Phase.CLOSED, False, id="closed"),
    ],
)
def test_the_hint_is_on_the_cards_that_used_to_hold_the_buttons(phase, pointed):
    state = _state(phase)
    text = _text(mj_layouts.build_table_panel(state, {HOST: "A", GUEST: "B"}, 1, 450))
    assert (host_panel.HOST_HINT in text) is pointed


def test_a_pre_move_card_still_registers_the_old_ids():
    """A sticky posted before the move shows Cancel / Close Table until its
    next repost; the resume-time view must answer them."""
    view = mj_views.TableView(MahjongCog.__new__(MahjongCog), 42, register_all=True)
    registered = {b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)}
    assert {"mmj:42:cancel", "mmj:42:close"} <= registered


# ── the gates (logic layer) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("phase", "who", "mod", "expected"),
    [
        pytest.param(Phase.LOBBY, HOST, False, None, id="host-opens"),
        pytest.param(Phase.LOBBY, GUEST, False, None, id="player-opens"),
        pytest.param(Phase.LOBBY, STRANGER, False, "not_player", id="stranger-refused"),
        pytest.param(Phase.LOBBY, STRANGER, True, None, id="mod-opens"),
        pytest.param(Phase.CLOSED, HOST, False, "stale", id="closed-table"),
        pytest.param(None, HOST, False, "stale", id="no-table"),
    ],
)
def test_who_may_open_the_table_controls(phase, who, mod, expected):
    state = None if phase is None else _state(phase)
    assert table_controls_refusal(state, who, is_mod=mod) == expected


@pytest.mark.parametrize(
    ("phase", "who", "mod", "expected"),
    [
        pytest.param(Phase.SETTLE, GUEST, False, None, id="seated-player-after-a-hand"),
        pytest.param(Phase.SETTLE, STRANGER, False, "not_player", id="stranger-after-a-hand"),
        pytest.param(Phase.SETTLE, STRANGER, True, None, id="mod-after-a-hand"),
        pytest.param(Phase.AWAIT_DISCARD, GUEST, False, "stale", id="player-mid-hand"),
        pytest.param(Phase.AWAIT_DISCARD, MOD, True, None, id="mod-mid-hand"),
        pytest.param(Phase.LOBBY, HOST, False, "stale", id="host-in-lobby-has-cancel-instead"),
        pytest.param(None, MOD, True, "stale", id="gone"),
    ],
)
def test_close_table_gate_is_what_the_card_button_enforced(phase, who, mod, expected):
    state = None if phase is None else _state(phase)
    assert close_table_refusal(state, who, is_mod=mod) == expected


# ── the cog: /games host and the panel's actions ─────────────────────────────


def _mod_member():
    member = MagicMock(spec=discord.Member)
    member.id = MOD
    member.guild_permissions = discord.Permissions(manage_guild=True)
    return member


def _cog(state: GameState | None, *, table_id: int | None = 7) -> MahjongCog:
    cog = MahjongCog.__new__(MahjongCog)
    cog.channel_tables = {100: table_id} if table_id is not None else {}
    cog.service = SimpleNamespace(
        load_state=AsyncMock(return_value=state),
        act=AsyncMock(return_value=[]),
        dissolve_table=AsyncMock(),
    )
    return cog


def _interaction(user):
    i = fake_interaction(user=user, channel_id=100)
    i.original_response = AsyncMock(return_value=MagicMock())
    return i


async def test_games_host_finds_the_table_by_channel_and_sends_the_panel():
    cog = _cog(_state(Phase.LOBBY))
    interaction = _interaction(FakeMember(id=HOST))
    assert await cog.open_channel_host_panel(interaction) is True
    sent = interaction.response.send_message.await_args.kwargs
    assert isinstance(sent["view"], host_panel.CallbackHostPanel)
    assert sent["ephemeral"] is True
    assert sent["view"].timeout < 15 * 60


async def test_games_host_leaves_a_channel_without_a_live_table_alone():
    for cog in (_cog(None, table_id=None), _cog(None), _cog(_state(Phase.CLOSED))):
        interaction = _interaction(FakeMember(id=HOST))
        assert await cog.open_channel_host_panel(interaction) is False
        interaction.response.send_message.assert_not_awaited()


async def test_games_host_turns_a_stranger_away_plainly():
    cog = _cog(_state(Phase.LOBBY))
    interaction = _interaction(FakeMember(id=STRANGER))
    assert await cog.open_channel_host_panel(interaction) is True
    assert interaction.response.send_message.await_args.args[0] == NOT_AT_TABLE_REPLY


async def test_cancel_passes_the_presser_unless_they_are_a_mod():
    cog = _cog(_state(Phase.LOBBY))
    panel = MagicMock()
    host = _interaction(FakeMember(id=HOST))
    await cog.panel_cancel(host, panel, 7)
    cog.service.act.assert_awaited_with(7, "cancel", member_id=HOST)
    panel.stop.assert_called_once()
    host.edit_original_response.assert_awaited_once()

    mod = _interaction(_mod_member())
    await cog.panel_cancel(mod, MagicMock(), 7)
    cog.service.act.assert_awaited_with(7, "cancel", member_id=None)


async def test_cancel_refused_by_the_engine_says_so_and_leaves_the_panel():
    cog = _cog(_state(Phase.LOBBY))
    cog.service.act.side_effect = ActionRejected(G.RejectCode.NOT_HOST, "Only the host can cancel this table.")
    panel = MagicMock()
    interaction = _interaction(FakeMember(id=GUEST))
    await cog.panel_cancel(interaction, panel, 7)
    assert "Only the host" in interaction.response.send_message.await_args.args[0]
    panel.stop.assert_not_called()


@pytest.mark.parametrize(
    ("phase", "user", "reply"),
    [
        pytest.param(Phase.AWAIT_DISCARD, FakeMember(id=GUEST), STALE_TABLE, id="mid-hand"),
        pytest.param(Phase.SETTLE, FakeMember(id=STRANGER), "❌ Only the players (or a mod) can close a table.", id="stranger"),
    ],
)
async def test_close_table_refusals_dissolve_nothing(phase, user, reply):
    cog = _cog(_state(phase))
    interaction = _interaction(user)
    await cog.panel_close(interaction, MagicMock(), 7)
    assert interaction.response.send_message.await_args.args[0] == reply
    cog.service.dissolve_table.assert_not_awaited()


async def test_close_table_after_a_hand_dissolves_it():
    cog = _cog(_state(Phase.SETTLE))
    panel = MagicMock()
    interaction = _interaction(FakeMember(id=GUEST))
    await cog.panel_close(interaction, panel, 7)
    cog.service.dissolve_table.assert_awaited_once_with(7, "closed")
    panel.stop.assert_called_once()


async def test_an_old_cards_cancel_and_close_point_at_the_panel():
    cog = _cog(_state(Phase.LOBBY))
    for action in ("cancel", "close"):
        interaction = _interaction(FakeMember(id=HOST))
        await cog.handle_table_button(interaction, 7, action)
        assert interaction.response.send_message.await_args.args[0] == MOVED_TO_HOST_PANEL
    cog.service.act.assert_not_awaited()
    cog.service.dissolve_table.assert_not_awaited()


# ── the two fixes made alongside ─────────────────────────────────────────────


def test_an_uncached_seat_is_named_user_id_not_a_hash():
    cog = MahjongCog.__new__(MahjongCog)
    guild = SimpleNamespace(get_member=lambda _id: None)
    assert cog._names(guild, _state(Phase.LOBBY)) == {HOST: f"User {HOST}", GUEST: f"User {GUEST}"}  # type: ignore[arg-type]


async def test_picking_an_assist_level_stops_the_menu_it_replaces():
    cog = MahjongCog.__new__(MahjongCog)
    cog.service = SimpleNamespace(choose_assist_mode=AsyncMock())
    cog._original = AsyncMock(return_value=None)  # type: ignore[method-assign]
    old = mj_views.MySettingsView(cog, "off")
    interaction = fake_interaction(user=FakeMember(id=HOST))
    interaction.guild = SimpleNamespace(id=1)
    interaction.response.edit_message = AsyncMock()
    await cog.handle_assist_pick(interaction, mj_views.ASSIST_CHOICES[0][0], replaced=old)
    assert old.is_finished()
