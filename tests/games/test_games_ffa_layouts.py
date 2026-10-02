"""Tests for ``bot_modules/games/ffa/layouts.py`` — the Components V2 cards
that replaced the two embed builders in ``games/ffa/cog.py`` — and the
host panel that took Next and Close off the prompt card.

Budget is checked at the worst case: a 2,000-character host-written
prompt of markdown, and a recap of 200 prompts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.games.ffa.cog as ffa_cog
from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.utils import host_panel
from bot_modules.games.ffa import layouts
from bot_modules.games.ffa.layouts import (
    ALIAS_ID,
    HELP_ID,
    HOST_CLOSE_ID,
    HOST_NEXT_ID,
    REPLY_ID,
    build_host_panel_layout,
    build_prompt_layout,
    build_recap_layout,
)

NASTY = "*" * 32


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str]:
    return [
        i.custom_id for i in card(items).walk_children() if getattr(i, "custom_id", None)
    ]


# ── the prompt card ──────────────────────────────────────────────────────────


def test_prompt_card_quotes_the_prompt_and_carries_nothing_host_only():
    items = build_prompt_layout("Line *one*\nLine two", "TRUTH")
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Truth\n> Line \\*one\\*\n> Line two")
    assert "-# Free For All • tap a button to reply anonymously" in text
    assert f"-# {host_panel.HOST_HINT}" in text
    assert _ids(items) == [HELP_ID, REPLY_ID, ALIAS_ID]


@pytest.mark.parametrize("count, word", [(1, "1 anonymous reply"), (3, "3 anonymous replies")])
def test_prompt_card_counts_replies(count, word):
    assert f"-# Free For All • {word}" in _text(build_prompt_layout("p", "DARE", reply_count=count))


def test_closed_prompt_keeps_its_count_and_loses_its_buttons():
    items = build_prompt_layout("p", "DARE", reply_count=2, closed=True)
    assert _ids(items) == []
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Dare\n> p")
    assert "-# Free For All • closed • 2 anonymous replies" in text
    assert host_panel.HOST_HINT not in text


def test_every_prompt_button_is_grey():
    """The embed era coloured a truth blue and a dare rose; the label says
    which now, and the card is bare."""
    buttons = [b for b in card(build_prompt_layout("p", "TRUTH")).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}


def test_host_panel_carries_next_and_close_all_grey():
    items = build_host_panel_layout()
    assert _ids(items) == [HOST_NEXT_ID, HOST_CLOSE_ID]
    buttons = [b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    assert {b.custom_id for b in buttons}.isdisjoint({"ffa_embed_next", "ffa_embed_close"})
    assert host_panel.HOST_COMMAND in _text(items)


# ── the recap ────────────────────────────────────────────────────────────────


def _summary(prompts, busiest=None, total=0, repliers=()):
    return {
        "per_prompt": prompts, "total_replies": total, "busiest": busiest,
        "repliers": list(repliers), "prompt_count": len(prompts),
    }


def test_recap_lists_prompts_the_busiest_and_the_tallies_and_names_nobody():
    items = build_recap_layout(_summary(
        [("TRUTH", "a *quiet* one", 1), ("DARE", "x" * 200, 3)],
        busiest=("DARE", "x" * 200, 3), total=4, repliers=[123456789012345678, 5],
    ), payout_line="🪙 +5 to everyone")
    text = _text(items)
    assert text.startswith(f"## {layouts.ICON} Anonymous Truth or Dare — Game Over")
    assert "Replies: **4** · Repliers: **2**" in text
    assert "### Prompts\n**Truth** — a \\*quiet\\* one · 1 reply\n**Dare** — " in text
    assert "### 🔥 Busiest Prompt\nxxx" in text and "… (3 replies)" in text
    assert "123456789012345678" not in text
    assert text.endswith("-# 🪙 +5 to everyone")
    assert _ids(items) == []


def test_recap_without_replies_has_no_busiest_prompt():
    text = _text(build_recap_layout(_summary([("TRUTH", "a", 0)])))
    assert "Busiest" not in text and "**Truth** — a · 0 replies" in text


# ── every card: budget at the worst case, glyph heading ──────────────────────


CARDS = {
    "prompt": build_prompt_layout("~" * 2000, "DARE", reply_count=999),
    "closed": build_prompt_layout("~" * 2000, "DARE", reply_count=999, closed=True),
    "panel": build_host_panel_layout(),
    "recap": build_recap_layout(_summary(
        [("TRUTH", NASTY * 4, 99)] * 200, busiest=("TRUTH", NASTY * 4, 99), total=9999, repliers=range(300),
    ), payout_line="🪙 +20 to winners • +5 to everyone who played"),
}
every_card = pytest.mark.parametrize("name", list(CARDS))


@every_card
def test_every_card_fits_the_budget_at_its_worst_case(name):
    view = card(CARDS[name])
    check_budget(view)
    assert view.content_length() <= MAX_CHARS


@every_card
def test_every_card_leads_with_a_glyph_led_heading(name):
    first = next(i for i in card(CARDS[name]).walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert first.content.startswith(f"## {layouts.ICON} ")


# ── the host panel ───────────────────────────────────────────────────────────


def _interaction(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}"),
        guild=None, guild_id=None, channel=SimpleNamespace(id=100, name="games"),
        message=None,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def _said(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


def test_ffa_is_registered_for_games_host():
    assert host_panel.HOST_PANEL_COGS["ffa"] == "FFACog"


async def test_games_host_opens_on_the_latest_prompt_and_refuses_a_stranger():
    bot = SimpleNamespace(active_views={}, games_db=None, ctx=SimpleNamespace(db_path=":memory:"))
    cog = ffa_cog.FFACog(bot)  # type: ignore[arg-type]
    bot.active_views["gid"] = ffa_cog.FFAEmbedView("gid", 1, "p", "TRUTH", None, bot)
    host = _interaction(1)
    await cog.open_host_panel(host, "gid")  # type: ignore[arg-type]
    assert isinstance(host.response.send_message.await_args.kwargs["view"], host_panel.GameHostPanel)
    stranger = _interaction(2)
    await cog.open_host_panel(stranger, "gid")  # type: ignore[arg-type]
    assert _said(stranger) == host_panel.NOT_HOST_REPLY
    bot.active_views.clear()
    late = _interaction(1)
    await cog.open_host_panel(late, "gid")  # type: ignore[arg-type]
    assert _said(late) == host_panel.GAME_ENDED_REPLY


async def test_a_reply_landing_redraws_the_cards_count_from_the_payload():
    view = ffa_cog.FFAEmbedView("gid", 1, "p", "TRUTH", None, SimpleNamespace(active_views={}))
    view._game_msg = SimpleNamespace(edit=AsyncMock())  # type: ignore[assignment]
    await view.show_count(4)
    assert "4 anonymous replies" in _text(list(view.children))
    kwargs = view._game_msg.edit.await_args.kwargs
    assert kwargs["view"] is view and kwargs["embed"] is None and kwargs["content"] is None
