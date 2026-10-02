"""Risky Rolls' Close Round lives on the ephemeral host panel (``/games
host``), not on the public round board (Ben, 2026-10-01).

Pinned here: ``/games host`` finds a round through the cog (rounds live in
``risky_roll.state``, not ``games_active_games``), only an opener or admin is
offered it, the press keeps the old gates, a closed round edits the *board*
(not the ephemeral panel) and posts the winner prompt to the channel, and a
board posted before the move answers its old Close Round with a pointer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.games.risky_roll import state as rr_state
from bot_modules.games.risky_roll import views as rr_views
from bot_modules.games.risky_roll.cog import NOT_OPENER_REPLY, RiskyRollCog
from bot_modules.games.risky_roll.layouts import host_close_id
from bot_modules.games.risky_roll.models import RiskyRollState
from bot_modules.games.utils import host_panel
from bot_modules.games.utils.lobby_art import NO_ART
from tests.fakes import FakeMember, fake_interaction

OPENER, OTHER = 1, 2


@pytest.fixture(autouse=True)
def _clean():
    yield
    rr_state.active_games.clear()
    rr_state.db_path = None


def _round(opener=OPENER, channel=100, **fields) -> RiskyRollState:
    state = RiskyRollState(channel_id=channel, guild_id=1, opener_id=opener, message_id=55)
    state.rolls = {OPENER: 40, OTHER: 90}
    for k, v in fields.items():
        setattr(state, k, v)
    rr_state.active_games[state.game_id] = state
    return state


def _admin():
    member = MagicMock(spec=discord.Member)
    member.id = 77
    member.guild_permissions = discord.Permissions(administrator=True)
    return member


def _interaction(user):
    i = fake_interaction(user=user, channel_id=100)
    i.original_response = AsyncMock(return_value=MagicMock())
    i.client = MagicMock()
    return i


def _cog() -> RiskyRollCog:
    return RiskyRollCog.__new__(RiskyRollCog)


# ── /games host ──────────────────────────────────────────────────────────────


async def test_games_host_finds_the_round_by_channel_for_its_opener():
    state = _round()
    interaction = _interaction(FakeMember(id=OPENER))
    assert await _cog().open_channel_host_panel(interaction) is True
    sent = interaction.response.send_message.await_args.kwargs
    panel = sent["view"]
    assert isinstance(panel, host_panel.CallbackHostPanel) and sent["ephemeral"] is True
    ids = [b.custom_id for b in panel.walk_children() if isinstance(b, discord.ui.Button)]
    assert ids == [host_close_id(state.game_id)]
    assert panel.timeout < 15 * 60


async def test_an_admin_is_offered_every_round_and_an_opener_only_their_own():
    mine, theirs = _round(), _round(opener=OTHER)
    admin = _interaction(_admin())
    await _cog().open_channel_host_panel(admin)
    ids = {
        b.custom_id for b in admin.response.send_message.await_args.kwargs["view"].walk_children()
        if isinstance(b, discord.ui.Button)
    }
    assert ids == {host_close_id(mine.game_id), host_close_id(theirs.game_id)}

    opener = _interaction(FakeMember(id=OPENER))
    await _cog().open_channel_host_panel(opener)
    ids = {
        b.custom_id for b in opener.response.send_message.await_args.kwargs["view"].walk_children()
        if isinstance(b, discord.ui.Button)
    }
    assert ids == {host_close_id(mine.game_id)}


async def test_games_host_turns_a_bystander_away_and_ignores_other_channels():
    _round()
    bystander = _interaction(FakeMember(id=OTHER + 5))
    assert await _cog().open_channel_host_panel(bystander) is True
    assert bystander.response.send_message.await_args.args[0] == NOT_OPENER_REPLY

    elsewhere = _interaction(FakeMember(id=OPENER))
    elsewhere.channel_id = 999
    assert await _cog().open_channel_host_panel(elsewhere) is False
    elsewhere.response.send_message.assert_not_awaited()


async def test_a_closed_round_is_not_offered():
    _round(is_open=False)
    interaction = _interaction(FakeMember(id=OPENER))
    assert await _cog().open_channel_host_panel(interaction) is False


# ── the press ────────────────────────────────────────────────────────────────


async def test_a_bystander_pressing_close_is_refused_and_the_round_stays_open():
    state = _round()
    interaction = _interaction(FakeMember(id=OTHER))
    await rr_views.close_round(interaction, state.game_id)
    assert interaction.response.send_message.await_args.args[0] == "❌ Only the round opener can close this round."
    assert state.is_open and state.game_id in rr_state.active_games


async def test_closing_edits_the_board_and_posts_the_prompt_to_the_channel():
    state = _round()
    panel = MagicMock()
    interaction = _interaction(FakeMember(id=OPENER))
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        get_partial_message=MagicMock(return_value=message), guild=None, id=100, name="c",
    )
    prompts = AsyncMock()
    with (
        patch("bot_modules.no_contact.service.no_contact_pairs_among", return_value=set()),
        patch.object(rr_views, "_board_art", AsyncMock(return_value=NO_ART)),
        patch.object(rr_views, "make_name_resolver", MagicMock(return_value=str)),
        patch.object(rr_views, "get_text_channel", AsyncMock(return_value=channel)),
        patch.object(rr_views, "_send_question_prompts_channel", prompts),
    ):
        await rr_views.close_round(interaction, state.game_id, panel)

    assert state.highest_user == OTHER and state.lowest_user == OPENER
    assert state.game_id not in rr_state.active_games
    channel.get_partial_message.assert_called_once_with(55)
    message.edit.assert_awaited_once()
    prompts.assert_awaited_once()
    panel.stop.assert_called_once()
    interaction.edit_original_response.assert_awaited_once()


# ── a board from before the move ─────────────────────────────────────────────


async def test_an_old_boards_close_round_points_at_the_panel():
    view = rr_views.RiskyRollView("g1")
    interaction = _interaction(FakeMember(id=OPENER))
    await view.close_button(interaction)
    assert interaction.response.send_message.await_args.args[0] == rr_views.MOVED_TO_HOST_PANEL


async def test_a_redrawn_board_binds_without_the_close_button():
    """``board_view`` hands the view only what the board draws now."""
    state = _round()
    view = await rr_views.board_view(None, state, None)
    ids = {b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)}
    assert "rr:close" not in ids


# ── ship review, 2026-10-01 ──────────────────────────────────────────────────


def _panel_text(items) -> str:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return "\n".join(i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay))


@pytest.mark.parametrize(
    "resolved, shown",
    [
        pytest.param("a\\_b", "**a\\_b**", id="escaped-once-not-twice"),
        pytest.param("<@1>", "**User 1**", id="a-miss-reads-user-id"),
    ],
)
def test_the_panel_names_the_opener_through_the_resolver_once(resolved, shown):
    """The resolver already escapes; escaping again showed a visible
    backslash, and a miss showed a raw mention instead of User <id>."""
    from bot_modules.games.risky_roll.layouts import build_host_panel_layout

    state = _round()
    text = _panel_text(build_host_panel_layout([state], lambda _uid: resolved))
    assert f"Round by {shown}" in text


def test_a_crowded_channel_keeps_the_panel_inside_the_component_budget():
    """An admin sees every round, and the per-channel cap has no ceiling: 13
    rounds would have been 40+ components and /games host would error."""
    from bot_modules.core.layout import card, check_budget
    from bot_modules.games.risky_roll.layouts import HOST_PANEL_MAX_ROUNDS, build_host_panel_layout

    rounds = [_round(opener=OPENER + i) for i in range(HOST_PANEL_MAX_ROUNDS + 5)]
    items = build_host_panel_layout(rounds, str)
    check_budget(card(items))
    assert "…and 5 more" in _panel_text(items)
    closes = [
        b for b in card(items).walk_children()
        if isinstance(b, discord.ui.Button) and b.custom_id and b.custom_id.startswith(host_close_id("")[:-1])
    ]
    assert len(closes) == HOST_PANEL_MAX_ROUNDS


async def test_a_round_in_a_voice_text_chat_still_closes_and_posts_its_prompt():
    """get_text_channel rejects a voice channel's text chat; the panel's own
    channel is the round's channel, so the board still closes and the winner
    prompt still posts."""
    state = _round()
    message = SimpleNamespace(edit=AsyncMock())
    voice_chat = MagicMock(spec=discord.VoiceChannel)
    voice_chat.get_partial_message = MagicMock(return_value=message)
    voice_chat.guild, voice_chat.id, voice_chat.name = None, 100, "vc"
    interaction = _interaction(FakeMember(id=OPENER))
    interaction.channel = voice_chat
    prompts = AsyncMock()
    with (
        patch("bot_modules.no_contact.service.no_contact_pairs_among", return_value=set()),
        patch.object(rr_views, "_board_art", AsyncMock(return_value=NO_ART)),
        patch.object(rr_views, "make_name_resolver", MagicMock(return_value=str)),
        patch.object(rr_views, "get_text_channel", AsyncMock(return_value=None)),
        patch.object(rr_views, "_send_question_prompts_channel", prompts),
    ):
        await rr_views.close_round(interaction, state.game_id, MagicMock())

    message.edit.assert_awaited_once()
    prompts.assert_awaited_once()
    assert prompts.await_args.args[1] is voice_chat


async def test_closing_one_round_says_how_many_are_left():
    first, second = _round(), _round()
    interaction = _interaction(FakeMember(id=OPENER))
    channel = SimpleNamespace(
        get_partial_message=MagicMock(return_value=SimpleNamespace(edit=AsyncMock())),
        guild=None, id=100, name="c",
    )
    with (
        patch("bot_modules.no_contact.service.no_contact_pairs_among", return_value=set()),
        patch.object(rr_views, "_board_art", AsyncMock(return_value=NO_ART)),
        patch.object(rr_views, "make_name_resolver", MagicMock(return_value=str)),
        patch.object(rr_views, "get_text_channel", AsyncMock(return_value=channel)),
        patch.object(rr_views, "_send_question_prompts_channel", AsyncMock()),
    ):
        await rr_views.close_round(interaction, first.game_id, MagicMock())

    view = interaction.edit_original_response.await_args.kwargs["view"]
    text = "\n".join(i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert "1 more round you can close" in text and second.game_id in rr_state.active_games
