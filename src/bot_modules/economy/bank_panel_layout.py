"""The economy panel as a Components V2 card — the channel's live bank board.

**Paradigm 8 — live board, in the hub's frame.** The panel is a board first (who
is earning, what there is to do, what just happened), so it keeps the board's
shape: the sections stacked under the title, then the member's buttons in one
row at the foot (Billy, 2026-09-26: "board, then a row"). What it takes from
the hub archetype is the frame — bare, the guild's panel banner opening it
and the footer closing it, the title optionally drawn into the banner, and a
grey **Help** beside the title.

The text comes from ``leaderboard.panel_sections``; this module only lays it
out, and returns components with no behaviour — the cog's view binds the
callbacks by ``custom_id`` (style guide § Code conventions). Every id here is
the one the embed panel's buttons carried, so a panel posted before the
conversion keeps answering until its sticky repaints it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.layout import hub_banner, hub_footer
from bot_modules.economy.guide import (
    GUIDE_TOPIC_CUSTOM_ID,
    HOW_IT_WORKS_CUSTOM_ID,
    HOW_IT_WORKS_HEADING,
    NOTIFY_CUSTOM_ID,
    guide_topics,
    topic_select,
)
from bot_modules.economy.leaderboard import _rel, panel_sections
from bot_modules.economy.quest_views import QUEST_BOARD_CUSTOM_ID, WALLET_CUSTOM_ID

if TYPE_CHECKING:
    from collections.abc import Callable

    from bot_modules.economy.leaderboard import LeaderboardData
    from bot_modules.economy.service import EconSettings

#: The panel's one sentence under the title. The long version is the How it
#: works block's (one line per topic, the detail one pick away).
INTRO = "See who's earning and what there is to do, updated live."

#: The row at the foot, in order: (label, emoji, custom_id).
PANEL_BUTTONS = (
    ("Show My Quests", "📋", QUEST_BOARD_CUSTOM_ID),
    ("Wallet", "💰", WALLET_CUSTOM_ID),
    ("Notifications", "🔔", NOTIFY_CUSTOM_ID),
)


def economy_panel_title(settings: EconSettings) -> str:
    """The panel's title, e.g. "Coins — The Bank".

    Per guild, because the currency is: this is the string the Branding
    **Title in the Banner** switch draws (``panel_title_service.with_title``),
    so it carries no emoji a banner font can't draw.
    """
    return f"{settings.currency_plural} — The Bank"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large)


def _help_button() -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label="Help",
        style=discord.ButtonStyle.secondary,
        custom_id=HOW_IT_WORKS_CUSTOM_ID,
    )


def _how_it_works(settings: EconSettings) -> list[discord.ui.Item[Any]]:
    """The **How it works** block: a gist line per guide topic, then the
    **Learn more about…** dropdown that opens one topic ephemerally.

    Built from the guild's settings like the guide itself, so a gist never
    promises a dial that is off.
    """
    topics = guide_topics(settings)
    return [
        _text(
            f"### {HOW_IT_WORKS_HEADING}\n"
            + "\n".join(t.headline for t in topics)
        ),
        discord.ui.ActionRow(topic_select(topics, custom_id=GUIDE_TOPIC_CUSTOM_ID)),
    ]


def _button_row() -> discord.ui.ActionRow[Any]:
    return discord.ui.ActionRow(*(
        discord.ui.Button(
            label=label, emoji=emoji,
            style=discord.ButtonStyle.secondary, custom_id=custom_id,
        )
        for label, emoji, custom_id in PANEL_BUTTONS
    ))


def build_economy_panel_layout(
    settings: EconSettings,
    data: LeaderboardData,
    resolve_name: Callable[[int], str],
    *,
    now_ts: float,
    banner_url: str | None = None,
    footer_url: str | None = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The economy panel: title and Help, the board, How it works, the
    member's buttons.

    The card is bare (style guide § No container; the hubs' box went on
    2026-09-29). ``banner_url`` opens it and ``footer_url`` closes it
    (``core.layout.hub_banner`` / ``hub_footer``); ``title_in_banner`` means
    the banner already carries :func:`economy_panel_title` drawn in, so the
    ``##`` heading goes, the title becomes the banner's alt text and **Help**
    moves up beside the intro line. It needs a banner — without one the
    heading stays.

    **How it works** follows the board: a one-line gist per guide topic
    and a **Learn more about…** dropdown opening one topic ephemerally
    (``economy.guide.show_guide_topic``). Billy, 2026-09-28: the guide was
    "too much at once", so the panel carries its headlines. It is the one
    how-to a hub card carries, against paradigm 1's "no how-to on the card"
    — his call, recorded in the style guide's paradigm 8. **Help** opens
    the same guide at its overview (``economy.guide.show_guide``). It
    replaced the embed panel's ❓ How it Works button on the same id.

    The currency icon the embed carried as a thumbnail is gone: a Section
    holds one accessory, and the title's is Help. The banner is the panel's
    art now.
    """
    title = economy_panel_title(settings)
    intro = _text(INTRO)
    head: list[discord.ui.Item[Any]]
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=title),
            discord.ui.Section(intro, accessory=_help_button()),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.Section(
                _text(f"## {settings.currency_emoji} {title}"),
                accessory=_help_button(),
            ),
            intro,
        ]
    body: list[discord.ui.Item[Any]] = []
    for heading, text in panel_sections(settings, data, resolve_name):
        body += [_line(), _text(f"### {heading}\n{text}")]
    foot = [
        _line(),
        *_how_it_works(settings),
        _line(),
        _button_row(),
        _text(f"-# ⚡ Live — updates within about two minutes of activity · {_rel(now_ts)}"),
    ]
    return [*head, *body, *foot, *hub_footer(footer_url)]
