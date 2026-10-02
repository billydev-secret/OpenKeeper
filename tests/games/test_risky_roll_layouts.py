"""Risky Rolls' Components V2 cards and the views that send them.

The words are ``test_risky_roll.py``'s; this file holds the card contract:
which controls a board carries in which state, who a card may ping, that
every edit clears what an embed-era message still carries, and the budget.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.games.utils.lobby_art import NO_ART
from bot_modules.core.branding_service import PanelArt
from bot_modules.games.risky_roll import layouts
from bot_modules.games.risky_roll.logic import may_close_round
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.risky_roll import state as rr_state
from bot_modules.games.risky_roll import views as rr_views
from bot_modules.games.risky_roll.models import (
    PendingQuestionState,
    PromptKind,
    RiskyRollState,
)

HEADER = "https://example.test/header.webp"
FOOTER = "https://example.test/footer.webp"
WINNER, LOSER, SECOND, EXTRA = 11, 22, 33, 44


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    rr_state.board_art.clear()
    rr_state.db_path = None


def _round(**fields) -> RiskyRollState:
    state = RiskyRollState(channel_id=100, guild_id=1, opener_id=WINNER)
    for key, value in fields.items():
        setattr(state, key, value)
    return state


def _ids(items) -> list[str]:
    view = discord.ui.LayoutView()
    for item in items:
        view.add_item(item)
    return [
        i.custom_id for i in view.walk_children()
        if isinstance(i, discord.ui.Button) and i.custom_id
    ]


def _text(items) -> str:
    view = discord.ui.LayoutView()
    for item in items:
        view.add_item(item)
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _pending(kind: PromptKind, **fields) -> PendingQuestionState:
    state = PendingQuestionState(
        channel_id=100, guild_id=1, winner_id=WINNER,
        participant_user_ids={LOSER}, game_id="g1", prompt_kind=kind,
        prompt_message_id=4242,
    )
    for key, value in fields.items():
        setattr(state, key, value)
    return state


# ── the board ────────────────────────────────────────────────────────


def test_an_open_board_carries_roll_and_help_and_points_at_the_host_panel():
    """Close Round is host-only, so it is on the ephemeral host panel
    (Ben, 2026-10-01); the board keeps the one pointer line."""
    items = layouts.build_round_layout(_round(rolls={WINNER: 50}), str)
    assert _ids(items) == [layouts.ROLL_ID, layouts.HELP_ID]
    assert f"-# {HOST_HINT}" in _text(items)


def test_the_host_panel_offers_a_red_close_per_round():
    rounds = [_round(rolls={WINNER: 50}, message_id=9), _round()]
    items = layouts.build_host_panel_layout(rounds, str)
    view = discord.ui.LayoutView()
    for item in items:
        view.add_item(item)
    buttons = [b for b in view.walk_children() if isinstance(b, discord.ui.Button)]
    assert [b.custom_id for b in buttons] == [layouts.host_close_id(r.game_id) for r in rounds]
    assert {b.style for b in buttons} == {discord.ButtonStyle.danger}
    assert "1 rolled" in _text(items) and "/channels/1/100/9" in _text(items)
    check_budget(view)


@pytest.mark.parametrize(
    ("who", "admin", "may"),
    [
        pytest.param(WINNER, False, True, id="opener"),
        pytest.param(LOSER, False, False, id="player"),
        pytest.param(LOSER, True, True, id="admin"),
    ],
)
def test_only_the_opener_or_an_admin_may_close_a_round(who, admin, may):
    assert may_close_round(_round(), who, is_admin=admin) is may


def test_a_closed_board_carries_no_controls_at_all():
    """A finished board is replaced, never greyed out (style guide, "One card")."""
    state = _round(rolls={WINNER: 90, LOSER: 5}, highest_user=WINNER, lowest_user=LOSER, is_open=False)
    items = layouts.build_round_layout(state, str)
    assert _ids(items) == []
    assert not any(isinstance(i, discord.ui.ActionRow) for i in items)


@pytest.mark.parametrize(
    ("is_open", "drawn"),
    [pytest.param(True, True, id="open"), pytest.param(False, False, id="closed")],
)
def test_the_round_role_line_is_drawn_only_while_open(is_open, drawn):
    state = _round(ping_role_id=777, is_open=is_open)
    text = _text(layouts.build_round_layout(state, str))
    assert ("<@&777> A new round has begun!" in text) is drawn


def test_board_mentions_allow_exactly_the_round_role_while_open():
    mentions = layouts.board_mentions(_round(ping_role_id=777))
    assert [r.id for r in mentions.roles] == [777]  # type: ignore[union-attr]
    assert mentions.users is False and mentions.everyone is False


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(_round(), id="no-role"),
        pytest.param(_round(ping_role_id=777, is_open=False), id="closed"),
    ],
)
def test_board_mentions_are_none_without_a_drawn_role_line(state):
    mentions = layouts.board_mentions(state)
    assert mentions.roles is False and mentions.users is False


def test_the_art_frames_the_board_below_its_buttons():
    items = layouts.build_round_layout(
        _round(), str, header_url=HEADER, footer_url=FOOTER, title_in_header=True
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert isinstance(items[-2], discord.ui.ActionRow)
    # The drawn title replaces the ## heading.
    assert "## 🎲 Risky Rolls" not in _text(items)


def test_the_fullest_board_fits_the_budget():
    """Eighty players with the longest names Discord allows, art on, open."""
    rolls = {uid: (uid % 100) + 1 for uid in range(1, 81)}
    state = _round(rolls=rolls, ping_role_id=777, auto_close_players=90, auto_close_minutes=120)
    long_name = "W" * 32
    items = layouts.build_round_layout(
        state, lambda _uid: long_name, header_url=HEADER, footer_url=FOOTER
    )
    view = discord.ui.LayoutView()
    for item in items:
        view.add_item(item)
    check_budget(view)
    assert "more" in _text(items)  # the tail folded into a count


def test_a_small_roster_is_never_folded():
    state = _round(rolls={WINNER: 50, LOSER: 20})
    assert "more" not in _text(layouts.build_round_layout(state, str))


# ── the winner prompt ────────────────────────────────────────────────


def test_an_open_prompt_carries_ask_and_a_spent_one_nothing():
    state = _pending(PromptKind.DIRECT)
    assert _ids(layouts.build_prompt_layout(state)) == [layouts.ASK_ID]
    closed = layouts.build_prompt_layout(state, closed_text="Asked already.")
    assert _ids(closed) == [] and "Asked already." in _text(closed)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(_pending(PromptKind.DIRECT), {WINNER, LOSER}, id="direct"),
        pytest.param(
            _pending(PromptKind.TWO_QUESTIONERS, extra_questioner_id=EXTRA),
            {WINNER, EXTRA, LOSER}, id="two-questioners",
        ),
        pytest.param(
            _pending(PromptKind.ROOM, participant_user_ids={LOSER, SECOND}),
            {WINNER}, id="room-names-only-the-winner",
        ),
    ],
)
def test_prompt_mentions_are_exactly_who_the_prompt_names(state, expected):
    mentions = layouts.prompt_mentions(state)
    assert {u.id for u in mentions.users} == expected  # type: ignore[union-attr]
    assert mentions.roles is False and mentions.everyone is False


# ── the views ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("view", "ids"),
    [
        pytest.param(
            rr_views.RiskyRollView("g1"),
            # CLOSE_ID is the pre-move board's Close Round, kept so a board
            # posted before the host panel answers a press after a restart.
            [layouts.ROLL_ID, layouts.HELP_ID, layouts.CLOSE_ID], id="board",
        ),
        pytest.param(rr_views.SixtyNineQuestionView("g1"), [layouts.ASK_ID], id="prompt"),
    ],
)
def test_a_bare_view_is_persistent_with_every_button_bound(view, ids):
    """What ``cog_load`` registers for a restored message."""
    assert view.is_persistent()
    buttons = [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]
    assert [b.custom_id for b in buttons] == ids
    # bind() swapped each button's no-op callback for the view's method.
    assert all(getattr(b.callback, "__self__", None) is view for b in buttons)


async def test_board_view_is_live_while_open_and_a_plain_card_once_closed():
    open_view = await rr_views.board_view(None, _round(), None)
    assert isinstance(open_view, rr_views.RiskyRollView)
    closed_view = await rr_views.board_view(None, _round(is_open=False), None)
    assert type(closed_view) is discord.ui.LayoutView
    assert not any(isinstance(i, discord.ui.Button) for i in closed_view.walk_children())


def test_every_board_edit_clears_what_an_embed_message_carries():
    """A round restored from before the port sits on an embed message."""
    state = _round(ping_role_id=777)
    kwargs = rr_views.board_edit(rr_views.RiskyRollView("g1"), state)
    assert kwargs["content"] is None and kwargs["embed"] is None
    assert kwargs["attachments"] == []
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [777]


def test_an_open_prompt_edit_is_v2_with_its_allow_list():
    state = _pending(PromptKind.DIRECT)
    kwargs = rr_views.prompt_edit(state)
    assert kwargs["content"] is None and kwargs["embed"] is None
    assert isinstance(kwargs["view"], rr_views.SixtyNineQuestionView)
    assert {u.id for u in kwargs["allowed_mentions"].users} == {WINNER, LOSER}


def _text_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = None
    partial = MagicMock()
    partial.edit = AsyncMock()
    channel.get_partial_message.return_value = partial
    return channel


async def test_a_spent_prompt_is_left_showing_its_text_with_no_button():
    channel = _text_channel()
    client = MagicMock()
    client.get_channel.return_value = channel

    await rr_views.disable_pending_question_message(
        client, _pending(PromptKind.DIRECT), "The pending question prompt was cleared."
    )

    kwargs = channel.get_partial_message.return_value.edit.call_args.kwargs
    assert kwargs["content"] is None and kwargs["embed"] is None
    view = kwargs["view"]
    assert not any(isinstance(i, discord.ui.Button) for i in view.walk_children())
    assert "cleared" in _text(view.children)
    assert kwargs["allowed_mentions"].users is False


async def test_a_closed_round_message_is_repainted_without_buttons():
    channel = _text_channel()
    state = _round(message_id=555, is_open=False)

    await rr_views.disable_round_message(state, channel)

    channel.get_partial_message.assert_called_once_with(555)
    kwargs = channel.get_partial_message.return_value.edit.call_args.kwargs
    assert kwargs["content"] is None and kwargs["embed"] is None
    assert not any(isinstance(i, discord.ui.Button) for i in kwargs["view"].walk_children())


# ── the board art cache ──────────────────────────────────────────────


async def test_board_art_is_read_once_and_kept(tmp_path):
    rr_state.db_path = tmp_path / "db.sqlite"
    art = PanelArt(None, FOOTER, header=HEADER, title_in_header=True)
    reader = AsyncMock(return_value=art)
    state = _round()
    with patch.object(rr_views, "read_lobby_art", reader):
        assert await rr_views._board_art(MagicMock(), state, MagicMock()) == art
        assert await rr_views._board_art(MagicMock(), state, MagicMock()) == art
    reader.assert_awaited_once()


@pytest.mark.parametrize(
    ("db", "client", "channel"),
    [
        pytest.param(False, True, True, id="no-store"),
        pytest.param(True, False, True, id="no-client"),
        pytest.param(True, True, False, id="channel-not-in-hand"),
    ],
)
async def test_board_art_misses_are_plain_and_not_kept(tmp_path, db, client, channel):
    rr_state.db_path = (tmp_path / "db.sqlite") if db else None
    reader = AsyncMock(return_value=PanelArt(None, FOOTER, header=HEADER))
    state = _round()
    with patch.object(rr_views, "read_lobby_art", reader):
        art = await rr_views._board_art(
            MagicMock() if client else None, state, MagicMock() if channel else None
        )
    assert art == NO_ART
    reader.assert_not_awaited()
    assert state.game_id not in rr_state.board_art


async def test_a_failed_art_read_renders_plain_and_retries(tmp_path):
    rr_state.db_path = tmp_path / "db.sqlite"
    state = _round()
    with patch.object(rr_views, "read_lobby_art", AsyncMock(side_effect=RuntimeError("db"))):
        assert await rr_views._board_art(MagicMock(), state, MagicMock()) == NO_ART
    assert state.game_id not in rr_state.board_art


async def test_the_art_cache_is_bounded(tmp_path):
    rr_state.db_path = tmp_path / "db.sqlite"
    with patch.object(rr_views, "read_lobby_art", AsyncMock(return_value=NO_ART)):
        for _ in range(rr_views._BOARD_ART_KEPT + 5):
            await rr_views._board_art(MagicMock(), _round(), MagicMock())
    assert len(rr_state.board_art) == rr_views._BOARD_ART_KEPT
