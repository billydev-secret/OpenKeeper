"""Tests for ``bot_modules/games/nhie/layouts.py`` — the Components V2 cards
that replaced ``games_nhie/embeds.py`` — and the host panel that took Next
and End Game off the card.

The frame the three round games share is pinned in
``test_round_layouts.py``; what is here is Never Have I Ever's own: the
statement over its bars, the lives roster, the guilt board. Budget is
checked at the worst case the game can produce: no player cap exists, so
a whole channel on the lives tracker is the shape to check.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.nhie.cog as nhie_cog
from bot_modules.core.layout import card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.utils.round_pacing import CONTROL_DENIED, END_DENIED, RoundPacing
from bot_modules.games.nhie import layouts
from bot_modules.games.nhie.layouts import (
    GUILTY_ID,
    HELP_ID,
    HOST_END_ID,
    HOST_NEXT_ID,
    INNOCENT_ID,
    NEXT_ID,
    POSE_ID,
    build_host_panel_layout,
    build_recap_layout,
    build_round_layout,
)
from bot_modules.shared.name_resolver import GuildNames, mention


def _named(uid: int) -> str:
    return f"Member{uid}"


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str]:
    return [
        i.custom_id for i in card(items).walk_children() if getattr(i, "custom_id", None)
    ]


# ── the round card ───────────────────────────────────────────────────────────


def test_round_card_shows_the_statement_over_the_two_bars():
    text = _text(build_round_layout("gone skydiving", [1, 2, 3], [4], 3, name_fn=mention))
    assert text.startswith(f"## {layouts.ICON} Never Have I Ever\nRound **3**")
    assert "Never have I ever… **gone skydiving**" in text
    assert text.index("😈") < text.index("75% (3)") < text.index("😇") < text.index("25% (1)")


def test_round_card_escapes_markdown_in_the_statement():
    assert "\\*\\*bold\\*\\*" in _text(build_round_layout("**bold**", [], [], 1, name_fn=mention))


def test_open_round_carries_the_votes_pose_and_help_and_nothing_host_only():
    """Ben, 2026-09-30: everything host-only lives on the host panel."""
    assert _ids(build_round_layout("x", [], [], 1, name_fn=mention)) == [HELP_ID, GUILTY_ID, INNOCENT_ID, POSE_ID]


def test_scheduled_round_keeps_next_on_the_card():
    assert _ids(build_round_layout("x", [], [], 1, scheduled=True, name_fn=mention))[-1] == NEXT_ID


def test_waiting_round_offers_only_pose_and_help():
    items = build_round_layout("", [], [], 1, waiting=True, scheduled=True, name_fn=mention)
    assert _ids(items) == [HELP_ID, POSE_ID]
    assert "Pose Statement" in _text(items)


def test_closed_round_keeps_tally_and_lives_and_drops_every_control():
    items = build_round_layout(
        "x", [1, 2], [3], 4, closed=True, scheduled=True,
        lives={1: 2, 2: 3, 3: 0}, eliminated={3}, name_fn=_named,
    )
    assert _ids(items) == []
    text = _text(items)
    assert "Round Over" in text
    assert "67% (2)" in text and "Still Standing (2)" in text and "💀 Eliminated\nMember3" in text


def test_no_lives_blocks_when_lives_are_off():
    text = _text(build_round_layout("x", [1], [2], 1, lives={}, name_fn=_named))
    assert "Still Standing" not in text and "Eliminated" not in text


def test_lives_show_heart_pips_sorted_by_hearts_and_the_eliminated_apart():
    text = _text(build_round_layout(
        "x", [], [], 1, lives={1: 1, 2: 3, 3: 0}, eliminated={3}, max_lives=3, name_fn=_named,
    ))
    assert "### Still Standing (2)\n❤️❤️❤️ **Member2**\n❤️🖤🖤 **Member1**" in text
    assert "### 💀 Eliminated\nMember3" in text
    assert "Member3**" not in text.split("Eliminated")[0]


def test_an_uncached_member_reads_user_id():
    """A resolver miss (``<@id>``) reads ``User <id>``, never a mention."""
    text = _text(build_round_layout("x", [], [], 1, lives={7: 3}, name_fn=mention))
    assert "**User 7**" in text


def test_a_member_only_known_users_knows_is_named_not_user_id():
    """A player the live cache misses but ``known_users`` holds — the
    ``build_name_fn`` table path — reads their name (2026-10-01: the cache-only
    ``member_name`` printed ``User <id>`` for them)."""
    names = GuildNames(guild=None, table_names={7: "Gone_Girl"})
    text = _text(build_round_layout("x", [], [], 1, lives={7: 3}, name_fn=names))
    assert "**Gone\\_Girl**" in text and "User 7" not in text


def test_queued_statements_are_counted_in_the_subtext():
    assert "-# 1 statement queued" in _text(build_round_layout("x", [], [], 1, queued=1, name_fn=mention))


def test_round_card_fits_the_budget_with_a_whole_channel_on_the_lives_tracker():
    """No player cap exists; the embed's lives field could pass 1,024 and
    lose the whole board. The card fits whole lines and counts the rest."""
    everyone = list(range(1, 301))
    nasty = lambda uid: "\\*" * 32  # noqa: E731
    items = build_round_layout(
        "~" * 2000, everyone[:150], everyone[150:], 50,
        lives={uid: (uid % 4) for uid in everyone}, eliminated={uid for uid in everyone if uid % 4 == 0},
        name_fn=nasty, advance_at=1, queued=15, scheduled=True,
    )
    check_budget(card(items))
    assert "more" in _text(items)


# ── the host panel, the recap ────────────────────────────────────────────────


def test_host_panel_is_next_and_end_on_its_own_ids():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_NEXT_ID, HOST_END_ID]
    assert {HOST_NEXT_ID, HOST_END_ID}.isdisjoint({"nhie_next", "nhie_end"})


@pytest.mark.parametrize(
    "kwargs, lead",
    [
        pytest.param({"winner_id": 42}, "🏆 **Member42** is the last one standing!", id="winner"),
        pytest.param({"winner_id": None}, "Everyone's been eliminated! No winner this time.", id="wipe-out"),
        pytest.param({"winner_id": None, "ended": True}, "Game over — here's the final guilt board.", id="ended"),
        pytest.param(
            {"winner_id": None, "ended": True, "reason": "round_cap"},
            "That's the last round — here's the final guilt board.", id="round-cap",
        ),
    ],
)
def test_recap_lead_says_how_the_game_ended(kwargs, lead):
    text = _text(build_recap_layout(guilt_scores={"42": 1}, name_fn=_named, **kwargs))
    assert text.startswith(f"## {layouts.ICON} Never Have I Ever — Game Over\n{lead}")


def test_recap_lists_scores_worst_first_and_a_dash_when_empty():
    text = _text(build_recap_layout(None, {"1": 1, "2": 3}, name_fn=_named, ended=True))
    assert "### Final Guilt Scores\n**Member2** — 3 guilty votes\n**Member1** — 1 guilty vote" in text
    assert _text(build_recap_layout(None, {}, name_fn=mention, ended=True)).endswith("### Final Guilt Scores\n—")


def test_recap_carries_the_payout_line_and_fits_a_whole_channel():
    scores = {str(uid): uid for uid in range(1, 301)}
    items = build_recap_layout(
        None, scores, name_fn=lambda uid: "\\*" * 32, ended=True, payout_line="🪙 +5 to everyone",
    )
    check_budget(card(items))
    assert _text(items).endswith("-# 🪙 +5 to everyone")


# ── the host panel: Next and End Game, off the card ──────────────────────────


def _bot():
    return SimpleNamespace(active_views={}, games_db=None)


def _view(bot):
    view = nhie_cog.NHIERoundView(
        "gid", 1, "x", 1, None, bot, "Host", AsyncMock(),
        pacing=RoundPacing(opened_at=1.0), finish_callback=AsyncMock(),
    )
    view.message = SimpleNamespace(id=555, edit=AsyncMock())
    bot.active_views["gid"] = view
    return view


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}", roles=[]),
        guild=None, guild_id=None, channel=SimpleNamespace(id=100, name="games"),
        message=SimpleNamespace(id=999, edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_nhie_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["nhie"] == "NHIECog"


async def test_games_host_sends_the_host_their_panel_and_refuses_others():
    bot = _bot()
    _view(bot)
    cog = nhie_cog.NHIECog(bot)  # type: ignore[arg-type]
    host = _interaction()
    await cog.open_host_panel(host, "gid")  # type: ignore[arg-type]
    assert isinstance(host.response.send_message.await_args.kwargs["view"], host_panel.GameHostPanel)

    stranger = _interaction(2)
    await cog.open_host_panel(stranger, "gid")  # type: ignore[arg-type]
    assert _said(stranger) == CONTROL_DENIED

    bot.active_views.clear()
    late = _interaction()
    await cog.open_host_panel(late, "gid")  # type: ignore[arg-type]
    assert _said(late) == host_panel.GAME_ENDED_REPLY


async def test_panel_next_advances_the_board_and_end_confirms_first():
    bot = _bot()
    view = _view(bot)
    nxt = _interaction()
    await view.press_next(nxt)  # type: ignore[arg-type]
    view.advance_callback.assert_awaited_once_with(view.message)

    stranger = _interaction(2)
    await view.press_end(stranger)  # type: ignore[arg-type]
    assert _said(stranger) == END_DENIED
    host = _interaction()
    await view.press_end(host)  # type: ignore[arg-type]
    assert "Are you sure" in _said(host)
    view.finish_callback.assert_not_awaited()


async def test_recovery_redraws_a_board_that_is_still_an_embed(sync_db_path):
    from bot_modules.games.utils.game_manager import create_game, get_active_game_by_id
    from bot_modules.games.db import GamesDb

    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path), add_view=lambda *a, **k: None,
    )
    payload = {"rounds": {"1": {"guilty": [7], "innocent": [], "stmt": "x"}}, "lives": {}, "eliminated": [], "max_lives": 3}
    gid = await create_game(bot.games_db, 100, 1, "nhie", payload=payload)
    row = await get_active_game_by_id(bot.games_db, gid)
    cog = nhie_cog.NHIECog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, name="games", guild=None)

    embed_era = SimpleNamespace(id=1, embeds=[object()], edit=AsyncMock())
    assert await cog.recover_game(row, payload, channel, embed_era)
    kwargs = embed_era.edit.await_args.kwargs
    assert kwargs["embed"] is None and kwargs["view"] is bot.active_views[gid]
