"""A PvP game is one framed card from challenge or lobby to result
(``docs/specs/dk_pvp_games_suite_spec.md`` "One card"): ``FramedCard`` draws the
guild's lobby header and footer around every re-render, and the cog reads
that art once per game (``BaseGame._load_card_art``) so the synchronous
renders after it — the board, notices, the result — frame the same way.
The result's in-place edit is pinned in ``test_duels_render_hooks.py``.
"""
from __future__ import annotations

from pathlib import Path

import discord
import pytest
import pytest_asyncio

from bot_modules.duels.hot_potato.cog import HotPotatoDuel
from bot_modules.duels.quickdraw.views import QuickdrawLayoutView as QuickdrawView
from bot_modules.duels import base_game
from bot_modules.duels.layouts import build_challenge_layout
from bot_modules.games.utils.lobby_art import NO_ART
from bot_modules.core.branding_service import PanelArt
from bot_modules.games.db import GamesDb
from tests.fakes import FakeEconGamesBot

HEADER = "https://dash.example/h.webp"
FOOTER = "https://dash.example/f.webp"
ART = PanelArt(None, FOOTER, header=HEADER, title_in_header=True)


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _urls(children) -> list[str | None]:
    """Each child's gallery url, or None for anything that isn't art."""
    return [
        c.items[0].media.url if isinstance(c, discord.ui.MediaGallery) else None
        for c in children
    ]


# ── the frame ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "art, expected",
    [
        pytest.param(ART, [HEADER, None, "row", FOOTER], id="framed"),
        pytest.param(None, [None, "row"], id="unframed-renders-bare"),
        pytest.param(NO_ART, [None, "row"], id="no-art-renders-bare"),
    ],
)
def test_a_card_renders_header_items_buttons_footer(art, expected):
    view = QuickdrawView(7, on_fire=None).frame(art, "Quickdraw")  # type: ignore[arg-type]

    view.render([discord.ui.TextDisplay("board")])

    children = list(view.children)
    shape = ["row" if c is view._control_row else u for c, u in zip(children, _urls(children))]
    assert shape == expected


def test_a_rerender_keeps_the_frame_and_never_stacks_it():
    view = QuickdrawView(7, on_fire=None).frame(ART, "Quickdraw")  # type: ignore[arg-type]

    view.render([discord.ui.TextDisplay("one")])
    view.render([discord.ui.TextDisplay("two")])

    assert _urls(view.children).count(HEADER) == 1
    assert [c.content for c in view.children if isinstance(c, discord.ui.TextDisplay)] == ["two"]


@pytest.mark.parametrize(
    "titled, heading",
    [
        pytest.param(True, "## ⚔️ Challenge", id="header-says-the-name"),
        pytest.param(False, "## ⚔️ Quickdraw Challenge", id="bare-card-names-the-game"),
    ],
)
def test_the_challenge_heading_names_the_game_once(titled, heading):
    items = build_challenge_layout(
        "Quickdraw", 1, 2, "stakes", expires_at=0, name_fn=lambda uid: f"U{uid}",
        titled=titled,
    )
    texts = [i.content for i in items if isinstance(i, discord.ui.TextDisplay)]
    assert texts[0] == "<@2>"  # the ping still leads
    assert any(t.startswith(heading) for t in texts)


# ── the art, read once per game ───────────────────────────────────────────


def _cog(db, sync_db_path, *, with_channel=True) -> HotPotatoDuel:
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2], with_channel=with_channel)
    return HotPotatoDuel(bot)  # type: ignore[arg-type]


async def test_the_art_is_read_once_and_kept(db, sync_db_path, monkeypatch):
    reads = []

    async def art(bot, channel, title):
        reads.append(title)
        return ART

    monkeypatch.setattr(base_game, "read_lobby_art", art)
    cog = _cog(db, sync_db_path)

    assert await cog._load_card_art(5, 100) is ART
    assert await cog._load_card_art(5, 100) is ART
    assert reads == [cog.GAME_DISPLAY_NAME]


@pytest.mark.parametrize(
    "channel_id, with_channel",
    [
        pytest.param(100, False, id="channel-not-cached-yet"),
        pytest.param(None, True, id="no-channel-id"),
    ],
)
async def test_an_unseen_channel_reads_as_no_art_and_is_not_kept(
    db, sync_db_path, monkeypatch, channel_id, with_channel,
):
    async def art(bot, channel, title):
        return ART

    monkeypatch.setattr(base_game, "read_lobby_art", art)
    cog = _cog(db, sync_db_path, with_channel=with_channel)

    assert await cog._load_card_art(5, channel_id) is NO_ART
    assert 5 not in cog._card_art  # the next call tries again


async def test_the_kept_art_is_bounded(db, sync_db_path, monkeypatch):
    async def art(bot, channel, title):
        return ART

    monkeypatch.setattr(base_game, "read_lobby_art", art)
    monkeypatch.setattr(base_game, "_CARD_ART_KEPT", 2)
    cog = _cog(db, sync_db_path)

    for game_id in (1, 2, 1, 3):  # 1 is touched again, so 2 is the oldest
        await cog._load_card_art(game_id, 100)

    assert list(cog._card_art) == [1, 3]


async def test_a_notice_wears_the_games_art(db, sync_db_path):
    cog = _cog(db, sync_db_path)
    cog._card_art[5] = ART

    framed = list(cog._framed_card(5, [discord.ui.TextDisplay("Expired.")]).children)
    bare = list(cog._framed_card(6, [discord.ui.TextDisplay("Expired.")]).children)

    assert _urls(framed)[0] == HEADER and _urls(framed)[-1] == FOOTER
    assert _urls(bare) == [None] * len(bare)


async def test_the_board_view_is_framed_with_the_kept_art(db, sync_db_path):
    cog = _cog(db, sync_db_path)
    cog._card_art[5] = ART

    view = cog.build_game_view(5)

    assert (view._art, view._art_title) == (ART, cog.GAME_DISPLAY_NAME)  # type: ignore[attr-defined]
