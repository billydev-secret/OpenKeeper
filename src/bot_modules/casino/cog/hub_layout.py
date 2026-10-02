"""The casino hub panel as a Components V2 card — the channel's live board.

**Paradigm 8 — live board, in the hub's frame**, like the economy panel: the
jackpot, the floor and the house rules stacked under the title, then the
member's controls at the foot. The frame is the hub archetype's — bare, the
guild's panel banner opening it and the footer closing it, the title
optionally drawn into the banner, and a grey **Help** beside the title.

The tables are a **menu dropdown** (style guide § Paradigm 11): ten tables
as buttons plus the board, the art and the footer would outgrow a card, and
a dropdown carries each table's one-line blurb as the option's description
— the list the embed panel printed as a Tables field (Billy, 2026-09-26:
"Can the game selector be a dropdown?"). An option opens that table; it
never holds a value, so the view repaints the panel after a pick to put the
placeholder back.

**My Stats** is the dropdown's last option, below the tables (Billy,
bug-list row 265, 2026-09-27). It moved there from a button, so the foot
keeps only 🎁 Daily Comp. The ``casino:stats`` button id stays routed. The
``cog_load`` registration still carries the button, so an older post keeps
answering until its repaint. With every table closed there is no dropdown,
and the button comes back as the only way to see your stats.

The text comes from ``layouts.hub_sections``; this module only lays it out,
and returns components with no behaviour — ``views.CasinoPanelView`` binds
the callbacks by ``custom_id`` (style guide § Code conventions). Daily Comp,
My Stats and Help keep the ids the embed panel's buttons carried.

``TABLES``/``casino_title``/``hub_sections`` moved from ``embeds.py`` to
``layouts.py`` when the casino's game cards converted to Components V2
(``docs/plans/components_v2_migration.md`` wave 3a) — the hub itself, this
module, was intentionally left alone (it already shipped its own V2 panel
first) and just follows that move.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.casino.cog.layouts import TABLES, casino_title, hub_sections
from bot_modules.core.layout import hub_banner, hub_footer
from bot_modules.casino import service as svc
from bot_modules.core.branding_service import DEFAULT_CASINO_NAME
from bot_modules.economy.service import EconSettings
from bot_modules.shared.name_resolver import NameFn, mention

#: The table dropdown. Each option's value is its game key (``svc.GAMES``),
#: except the last, :data:`STATS_OPTION`.
TABLE_SELECT_CUSTOM_ID = "casino:table"
#: The dropdown's My Stats option value. It is not a game key, so it can't
#: open a table.
STATS_OPTION = "stats"
COMP_CUSTOM_ID = "casino:comp"
STATS_CUSTOM_ID = "casino:stats"
HELP_CUSTOM_ID = "casino:help"

#: The panel's one sentence under the title. The long version is Help's.
INTRO = (
    "Bright lights, long odds, and questionable financial decisions. "
    "Every bet comes straight from your wallet."
)
NO_TABLES = "*Every table is closed right now.*"


def casino_panel_title(casino_name: str = DEFAULT_CASINO_NAME) -> str:
    """The panel's title with no emoji, e.g. "The Golden Meadow Casino".

    The string the Branding **Title in the Banner** switch draws
    (``panel_title_service.with_title``), so it carries nothing a banner
    font can't draw. The ``##`` heading shown without it is
    :func:`layouts.casino_title`, which leads with 🎲.
    """
    return f"The {casino_name} Casino"


def open_tables(settings: svc.CasinoSettings) -> list[str]:
    """The games a member can pick right now, in menu order."""
    return [game for game in TABLES if svc.game_enabled(settings, game)]


def table_select(games: list[str]) -> discord.ui.Select[Any]:
    """The **Pick a table…** dropdown offering ``games``, then My Stats."""
    return discord.ui.Select(
        custom_id=TABLE_SELECT_CUSTOM_ID,
        placeholder="Pick a table…",
        options=[
            *(
                discord.SelectOption(
                    label=TABLES[game][1],
                    value=game,
                    emoji=TABLES[game][0],
                    description=TABLES[game][2],
                )
                for game in games
            ),
            discord.SelectOption(
                label="My Stats",
                value=STATS_OPTION,
                emoji="📊",
                description="Your net, biggest win, and how much of today's cap is left.",
            ),
        ],
    )


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large)


def _button(label: str, emoji: str, custom_id: str) -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label=label, emoji=emoji,
        style=discord.ButtonStyle.secondary, custom_id=custom_id,
    )


def _help_button() -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label="Help", style=discord.ButtonStyle.secondary, custom_id=HELP_CUSTOM_ID,
    )


def build_casino_hub_layout(
    econ: EconSettings,
    settings: svc.CasinoSettings,
    *,
    games: list[str] | None = None,
    comp: bool | None = None,
    jackpot: int | None = None,
    ticker: list[tuple[int, str, int, int]] | None = None,
    standings: tuple[tuple[int, int] | None, tuple[int, int] | None]
    | None = None,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn = mention,
    banner_url: str | None = None,
    footer_url: str | None = None,
    title_in_banner: bool = False,
    stats_button: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The casino hub: title and Help, the board, the table dropdown (its
    last option is 📊 My Stats), then 🎁 Daily Comp.

    ``games`` and ``comp`` default to what ``settings`` has open — closed
    tables are not in the dropdown, and the comp button only exists while
    the comp is on (``svc.comp_on``; off in every guild by default). The
    ``cog_load`` registration passes every game, ``comp=True`` and
    ``stats_button=True`` so each
    id routes whatever a posted panel shows.

    ``title_in_banner`` means the banner already carries
    :func:`casino_panel_title` drawn in, so the ``##`` heading goes and
    **Help** moves up beside the intro line. It needs a banner — without
    one the heading stays.

    ``stats_button`` adds the My Stats button even when the dropdown carries
    the option. Only the ``cog_load`` registration passes it, so an older
    post's button still routes. The button also shows without it when every
    table is closed, because then there is no dropdown.
    """
    games = open_tables(settings) if games is None else games
    comp = svc.comp_on(settings) if comp is None else comp
    intro = _text(INTRO)
    head: list[discord.ui.Item[Any]]
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=casino_panel_title(casino_name)),
            discord.ui.Section(intro, accessory=_help_button()),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.Section(
                _text(f"## {casino_title(casino_name)}"), accessory=_help_button(),
            ),
            intro,
        ]
    body: list[discord.ui.Item[Any]] = []
    for heading, text in hub_sections(
        econ, settings, jackpot=jackpot, ticker=ticker, standings=standings,
        name_fn=name_fn,
    ):
        body += [_line(), _text(f"### {heading}\n{text}")]
    buttons = [_button("Daily Comp", "🎁", COMP_CUSTOM_ID)] if comp else []
    if stats_button or not games:
        buttons.append(_button("My Stats", "📊", STATS_CUSTOM_ID))
    foot: list[discord.ui.Item[Any]] = [
        _line(),
        discord.ui.ActionRow(table_select(games)) if games else _text(NO_TABLES),
    ]
    if buttons:
        foot.append(discord.ui.ActionRow(*buttons))
    return [*head, *body, *foot, *hub_footer(footer_url)]
