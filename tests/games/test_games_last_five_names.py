"""Price, MLT and LegitLibs name players through the shared resolver: a
member the live cache lost but ``known_users`` knows is named, only a true
miss reads ``User <id>`` (the cache-only ``member_name`` printed that for
both, and is deleted)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

import bot_modules.games.mlt.cog as mlt_cog
import bot_modules.games.price.cog as price_cog
from bot_modules.core.layout import card
from bot_modules.shared.name_resolver import GuildNames

NAMES = GuildNames(guild=None, table_names={7: "Gone Girl"})


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in card(list(view.children)).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def test_attributed_story_names_a_known_users_member_and_a_true_miss():
    from bot_modules.games.legitlibs import rendering  # noqa: PLC0415

    body = rendering.render_filled_body_attributed(
        "{1} and {2}", [], {"1": {"by": 7, "value": "x"}, "2": {"by": 8, "value": "y"}}, NAMES,
    )
    assert "(-||Gone Girl||)" in body and "(-||User 8||)" in body


async def test_mlt_lobby_names_the_roster_through_the_resolver(monkeypatch):
    async def _names(bot: Any, guild: Any, ids: Any) -> GuildNames:
        return NAMES

    monkeypatch.setattr(mlt_cog, "build_names", _names)
    bot = SimpleNamespace(active_views={}, games_db=None)
    view = mlt_cog.MLTJoinView("gid", 1, None, bot, None, players=[7, 8], host_name="Ada")
    await view.refresh_names()
    view._rebuild()
    text = _text(view)
    assert "Gone Girl" in text and "User 8" in text


async def test_price_lobby_names_the_roster_through_the_resolver(monkeypatch):
    async def _names(bot: Any, guild: Any, ids: Any) -> GuildNames:
        return NAMES

    monkeypatch.setattr(price_cog, "build_names", _names)
    bot = SimpleNamespace(active_views={}, games_db=None)
    payload = {"players": [7, 8], "settings": {}}
    view = price_cog.PriceLobbyView("gid", 1, None, bot, None, host_name="Ada", payload=payload)
    await view.refresh_names(payload)
    view._rebuild(payload)
    text = _text(view)
    assert "Gone Girl" in text and "User 8" in text
