"""``read_lobby_art`` — the one read every V2 game lobby makes of the
guild's panel art (``core.layout.lobby_art`` draws it), which an age-gated
channel wears too — and the frame every lobby builder puts that art in."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.duels import layouts as duels_layouts
from bot_modules.games.utils import lobby_art
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.games.clapback import layouts as clapback_layouts
from bot_modules.games.compliment import layouts as compliment_layouts
from bot_modules.games.mfk import layouts as mfk_layouts
from bot_modules.games.story import layouts as story_layouts
from bot_modules.core import branding_service as bs
from bot_modules.games.risky_roll import layouts as risky_layouts
from bot_modules.games.risky_roll.models import RiskyRollState

GUILD = 111
HEADER = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"


@pytest.fixture
def bot(tmp_path):
    path = tmp_path / "art.db"
    bs.init_db(path)
    cfg = bs.get_branding(path, GUILD)
    cfg.panel_header_url = HEADER
    bs.upsert_branding(path, cfg)
    return SimpleNamespace(ctx=SimpleNamespace(db_path=path, open_db=lambda: open_db(path)))


def _channel(guild_id: int | None = GUILD, nsfw=False, category_nsfw=False):
    category = SimpleNamespace(is_nsfw=lambda: category_nsfw)
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        is_nsfw=lambda: nsfw,
        category=category,
    )


async def test_reads_the_guilds_lobby_header(bot):
    # Not an upload this dashboard wrote, so no title is drawn into it.
    art = await read_lobby_art(bot, _channel(), "Clapback")
    assert (art.header, art.title_in_header) == (HEADER, False)


async def test_the_games_name_is_drawn_into_the_header(bot, monkeypatch):
    seen = []

    def draw(art, title):
        seen.append((art.header, title))
        return art._replace(header="drawn", title_in_header=True)

    monkeypatch.setattr(lobby_art, "with_lobby_title", draw)
    art = await read_lobby_art(bot, _channel(), "Clapback")
    assert seen == [(HEADER, "Clapback")]
    assert (art.header, art.title_in_header) == ("drawn", True)


async def test_another_guild_has_its_own_art(bot):
    assert await read_lobby_art(bot, _channel(guild_id=GUILD + 1), "Clapback") == NO_ART


@pytest.mark.parametrize(
    "channel",
    [
        pytest.param(_channel(nsfw=True), id="age-gated"),
        pytest.param(_channel(category_nsfw=True), id="age-gated-category"),
        pytest.param(SimpleNamespace(guild=SimpleNamespace(id=GUILD)), id="unreadable-gate"),
    ],
)
async def test_an_age_gated_channel_wears_the_art_too(bot, channel):
    # Deliberate (Ben, 2026-09-28): spoilers are for member-submitted media,
    # not the server's own decoration. Until then an age-gated lobby stayed
    # plain, and Risky Rolls — only ever played in one — never wore the art.
    art = await read_lobby_art(bot, channel, "Risky Rolls")
    assert art.header == HEADER


async def test_no_art_outside_a_guild(bot):
    assert await read_lobby_art(bot, _channel(guild_id=None), "Clapback") == NO_ART


async def test_a_guild_keyed_read_matches_the_channel_read(bot):
    """``read_guild_lobby_art`` is the same read for a caller holding a guild
    (a channel hub's sticky panel), not a second copy of it."""
    guild = SimpleNamespace(id=GUILD)
    assert await lobby_art.read_guild_lobby_art(bot, guild, "Guess Who") == (
        await read_lobby_art(bot, _channel(), "Guess Who")
    )
    assert await lobby_art.read_guild_lobby_art(bot, None, "Guess Who") == NO_ART


# ── every V2 lobby builder frames itself the same way ─────────────────────

FOOTER = "https://dash.example/static/panel-banners/" + "f" * 32 + ".webp"


def _row() -> discord.ui.ActionRow:
    return discord.ui.ActionRow(discord.ui.Button(label="Join", custom_id="t_join"))


# (id, title the header wears, builder taking only the art kwargs). The duels
# and Compliment lobbies take their view's button row as ``controls``.
LOBBIES = [
    pytest.param(
        "Musical Chairs",
        lambda **art: duels_layouts.build_lobby_layout(
            "Musical Chairs", None, [1, 2], 1, 2, 8, "Winner takes all.",
            name_fn=str, controls=_row(), **art,
        ),
        id="duels",
    ),
    pytest.param(
        compliment_layouts.LOBBY_TITLE,
        lambda **art: compliment_layouts.build_lobby_layout(
            "Host", ["Ann"], controls=_row(), **art
        ),
        id="compliment",
    ),
    pytest.param(
        mfk_layouts.LOBBY_TITLE,
        lambda **art: mfk_layouts.build_lobby_layout("Host", ["Ann"], **art),
        id="mfk",
    ),
    pytest.param(
        story_layouts.LOBBY_TITLE,
        lambda **art: story_layouts.build_lobby_layout("Host", "blind", 10, [1], name_fn=str, **art),
        id="story",
    ),
    pytest.param(
        clapback_layouts.LOBBY_TITLE,
        lambda **art: clapback_layouts.build_lobby_layout("Host", {"rounds": 1}, [1], **art),
        id="clapback",
    ),
    pytest.param(
        risky_layouts.TITLE,
        lambda **art: risky_layouts.build_round_layout(
            RiskyRollState(channel_id=1, guild_id=GUILD, opener_id=1), str, **art
        ),
        id="risky",
    ),
]


def _galleries(items) -> list[str]:
    return [i.items[0].media.url for i in items if isinstance(i, discord.ui.MediaGallery)]


def _headings(items) -> list[str]:
    return [
        line for i in items if isinstance(i, discord.ui.TextDisplay)
        for line in i.content.splitlines() if line.startswith("## ")
    ]


@pytest.mark.parametrize("title, build", LOBBIES)
@pytest.mark.parametrize(
    "art",
    [
        pytest.param({}, id="no-art"),
        # A guild with a hub footer and no lobby header keeps plain lobbies.
        pytest.param({"footer_url": FOOTER}, id="footer-alone"),
        pytest.param({"footer_url": FOOTER, "title_in_header": True}, id="title-flag-alone"),
    ],
)
def test_a_lobby_without_a_header_is_unchanged(title, build, art):
    plain, items = build(), build(**art)
    assert _galleries(items) == []
    assert [type(i) for i in items] == [type(i) for i in plain]
    assert _headings(items) == _headings(plain) != []


@pytest.mark.parametrize("title, build", LOBBIES)
def test_the_header_tops_the_lobby_and_the_footer_closes_it_below_the_buttons(title, build):
    items = build(header_url=HEADER, footer_url=FOOTER)
    assert _galleries(items[:1]) == [HEADER]
    assert isinstance(items[-2], discord.ui.ActionRow)
    assert _galleries(items[-1:]) == [FOOTER]
    assert _headings(items)  # an untitled header leaves the heading in place


@pytest.mark.parametrize("title, build", LOBBIES)
def test_a_titled_header_replaces_the_heading(title, build):
    items = build(header_url=HEADER, footer_url=FOOTER, title_in_header=True)
    assert _headings(items) == []
    assert items[0].items[0].description == title  # type: ignore[attr-defined]
