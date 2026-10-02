"""The casino hub panel as a V2 hub — the table dropdown, the frame, budget.

The board text is ``embeds.hub_sections``' and is tested there
(``test_casino_embeds``: the ticker, the winner-never-the-loser rule, the
house rules); routing a pick is ``test_casino_views``'. This file covers what
the layout decides: which tables the dropdown offers, when the comp button
exists, and the hub frame around the board.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.casino.cog.hub_layout import (
    COMP_CUSTOM_ID,
    HELP_CUSTOM_ID,
    INTRO,
    NO_TABLES,
    STATS_CUSTOM_ID,
    STATS_OPTION,
    TABLE_SELECT_CUSTOM_ID,
    build_casino_hub_layout,
    casino_panel_title,
    open_tables,
)
from bot_modules.core.layout import check_budget
from bot_modules.casino.service import GAMES, CasinoSettings
from bot_modules.economy.service import EconSettings

BANNER = "https://dash.example/static/panel-banners/" + "c" * 32 + ".webp"
FOOTER = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"


def _items(settings: CasinoSettings | None = None, **kw) -> list[discord.ui.Item]:
    items = build_casino_hub_layout(EconSettings(), settings or CasinoSettings(), **kw)
    assert not any(isinstance(i, discord.ui.Container) for i in items), (
        "the hub is bare — its parts sit on the message, not in a box"
    )
    return items


def _view(settings: CasinoSettings | None = None, **kw) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView()
    for item in build_casino_hub_layout(EconSettings(), settings or CasinoSettings(), **kw):
        view.add_item(item)
    return view


def _select(view: discord.ui.LayoutView) -> discord.ui.Select | None:
    return next(
        (i for i in view.walk_children() if isinstance(i, discord.ui.Select)), None
    )


def _ids(view: discord.ui.LayoutView) -> list[str]:
    return [
        i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)
    ]


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def test_ids_are_frozen():
    # Comp, stats and help are the ids the embed panel's buttons carried, so a
    # panel posted before the conversion keeps answering.
    assert (COMP_CUSTOM_ID, STATS_CUSTOM_ID, HELP_CUSTOM_ID) == (
        "casino:comp", "casino:stats", "casino:help",
    )
    assert TABLE_SELECT_CUSTOM_ID == "casino:table"


def test_every_table_is_on_the_menu_with_a_description():
    select = _select(_view())
    assert select is not None
    assert select.placeholder == "Pick a table…"
    assert [o.value for o in select.options] == [*GAMES, STATS_OPTION]
    for option in select.options:
        assert option.emoji is not None and option.label
        # Discord rejects an option description over 100 characters.
        assert option.description and len(option.description) <= 100


@pytest.mark.parametrize(
    "closed",
    [
        pytest.param(("derby", "slots"), id="two-closed"),
        pytest.param(("mines",), id="one-closed"),
    ],
)
def test_closed_tables_drop_off_the_menu(closed):
    """The dashboard's promise that unchecked games disappear from the hub."""
    settings = CasinoSettings(**{f"{game}_enabled": False for game in closed})
    select = _select(_view(settings))
    assert select is not None
    values = [o.value for o in select.options]
    assert not set(closed) & set(values)
    # Menu order kept, My Stats last.
    assert values == [*(g for g in GAMES if g not in closed), STATS_OPTION]
    assert open_tables(settings) == values[:-1]


def test_every_table_closed_says_so_instead_of_an_empty_menu():
    view = _view(CasinoSettings(**{f"{game}_enabled": False for game in GAMES}))
    assert _select(view) is None
    assert NO_TABLES in _text(view)
    assert STATS_CUSTOM_ID in _ids(view)


@pytest.mark.parametrize(
    ("settings", "shown"),
    [
        # casino-134: the comp ships DARK — no button until the dial is set.
        pytest.param(CasinoSettings(), False, id="default-off"),
        pytest.param(CasinoSettings(daily_comp=5), True, id="dial-on"),
        # A spin on the slots needs the slots open.
        pytest.param(CasinoSettings(daily_comp=5, slots_enabled=False), False, id="slots-closed"),
    ],
)
def test_daily_comp_only_while_it_is_on(settings, shown):
    assert (COMP_CUSTOM_ID in _ids(_view(settings))) is shown


def test_help_beside_the_title_then_the_menu_and_buttons_at_the_foot():
    items = _items(CasinoSettings(daily_comp=5))
    head = items[0]
    assert isinstance(head, discord.ui.Section)
    assert head.accessory.label == "Help"
    assert head.accessory.custom_id == HELP_CUSTOM_ID
    (heading,) = head.children
    assert heading.content == "## 🎲 The Golden Meadow Casino"
    assert items[1].content == INTRO
    menu, buttons = items[-2], items[-1]
    assert isinstance(menu, discord.ui.ActionRow) and isinstance(buttons, discord.ui.ActionRow)
    assert [c.custom_id for c in menu.children] == [TABLE_SELECT_CUSTOM_ID]
    assert [(c.label, c.style) for c in buttons.children] == [
        ("Daily Comp", discord.ButtonStyle.secondary),
    ]


@pytest.mark.parametrize(
    ("settings", "kw", "button"),
    [
        # Billy, bug-list row 265: My Stats lives in the table dropdown.
        pytest.param(CasinoSettings(), {}, False, id="in-the-menu"),
        # No dropdown to carry it, so the button is the only way in.
        pytest.param(
            CasinoSettings(**{f"{game}_enabled": False for game in GAMES}), {}, True,
            id="every-table-closed",
        ),
        # The cog_load registration: an older post's button still routes.
        pytest.param(CasinoSettings(), {"stats_button": True}, True, id="registration"),
    ],
)
def test_my_stats_is_a_menu_option_and_a_button_only_as_a_fallback(settings, kw, button):
    view = _view(settings, **kw)
    assert (STATS_CUSTOM_ID in _ids(view)) is button
    assert STATS_OPTION not in GAMES


def test_a_panel_with_nothing_but_the_menu_has_no_empty_button_row():
    """Comp off, tables open: the foot is the dropdown alone. An empty
    ActionRow would be refused by Discord."""
    items = _items(CasinoSettings())
    assert isinstance(items[-1], discord.ui.ActionRow)
    assert [c.custom_id for c in items[-1].children] == [TABLE_SELECT_CUSTOM_ID]


def test_the_board_sits_between_the_intro_and_the_menu():
    text = _text(_view(CasinoSettings(channel_id=1), jackpot=500))
    order = [text.index(f"### {h}") for h in ("💰 Progressive Jackpot", "House Rules")]
    assert order == sorted(order)
    assert "Tables" not in text  # the dropdown replaced the text list


def test_no_box_banner_first_footer_last():
    items = _items(banner_url=BANNER, footer_url=FOOTER)
    first, last = items[0], items[-1]
    assert isinstance(first, discord.ui.MediaGallery)
    assert [m.media.url for m in first.items] == [BANNER]
    assert isinstance(last, discord.ui.MediaGallery)
    assert [m.media.url for m in last.items] == [FOOTER]
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in _items())


def test_title_in_the_banner_moves_help_beside_the_intro():
    items = _items(casino_name="Neon Pines", banner_url=BANNER, title_in_banner=True)
    banner, head = items[0], items[1]
    assert banner.items[0].description == "The Neon Pines Casino"
    assert isinstance(head, discord.ui.Section)
    (intro,) = head.children
    assert intro.content == INTRO
    assert head.accessory.custom_id == HELP_CUSTOM_ID
    text = _text(_view(banner_url=BANNER, title_in_banner=True))
    assert not any(line.startswith("## ") for line in text.splitlines())


def test_title_in_the_banner_needs_a_banner():
    assert "## 🎲 The Golden Meadow Casino" in _text(_view(title_in_banner=True))


def test_banner_title_carries_no_emoji():
    # it is the string a banner font draws
    assert casino_panel_title("Neon Pines") == "The Neon Pines Casino"


def test_fullest_panel_fits_the_budget():
    view = _view(
        CasinoSettings(channel_id=1, daily_comp=5, daily_wager_cap=5_000, max_bet=1_000),
        jackpot=1_234_567,
        ticker=[(uid, GAMES[uid % len(GAMES)], 1_000, 9_000) for uid in range(10)],
        standings=((1, 99_999), (2, -99_999)),
        casino_name="A Very Long Casino Name For The Budget",
        name_fn=lambda uid: "N" * 32,
        banner_url=BANNER,
        footer_url=FOOTER,
    )
    check_budget(view)
