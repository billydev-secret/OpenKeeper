"""The two break cards — pure Components V2 builders.

**Paradigm 1 — hub panel**, the one-entry-point case: a ``##`` title, one
sentence on what this is, a line, then a single row with its button beside
it and a ``-#`` blurb. Bare, like every other card
(``docs/components_v2_style_guide.md`` § No container). No Help button: the
card's own sentence is the whole explanation, and there is no FAQ doc to
read.

Both panels live in a channel, so both take the guild's **panel art** — the
banner across the top and the footer strip along the bottom (guide § Media,
the hub banner). The ephemeral replies do not (Billy, 2026-09-26: "let's not
use it on the ephemeral messages, just the main panels that live in the
channel"), and neither does the Server Features hub's 💤 card, which reuses
``build_break_panel_layout`` with no art arguments.

The builders return components and no behaviour; ``cogs.breaks_cog`` binds
the callbacks by ``custom_id`` (``core.layout.bind``).
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.layout import hub_banner, hub_footer

#: Persistent ids. Frozen once posted — a live panel carries them.
START_CUSTOM_ID = "break:start"
END_CUSTOM_ID = "break:end"

#: The names ``panel_title_service`` draws into the banner when the guild has
#: "Title in the Banner" on. One name per thing: these are the ``##``
#: headings below, minus the glyph a banner font can't draw.
BREAK_PANEL_TITLE = "Take a Break"
RETURN_PANEL_TITLE = "On a Break"


def build_break_panel_layout(
    *,
    banner_url: str | None = None,
    footer_url: str | None = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The public "Take a break" card, posted where an admin chooses.

    ``title_in_banner`` means ``banner_url`` already carries
    :data:`BREAK_PANEL_TITLE` drawn in (``panel_title_service``, Billy
    2026-09-26): the ``##`` heading goes and the title becomes the banner's
    alt text instead. It needs a banner — without one the heading stays.
    """
    intro = (
        "Step away for a while: your roles and role pings come off, the bot "
        "stops DMing you, and the server goes quiet until you say you're back."
    )
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=BREAK_PANEL_TITLE),
            discord.ui.TextDisplay(intro),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.TextDisplay(f"## 💤 {BREAK_PANEL_TITLE}\n{intro}"),
        ]
    return [
        *head,
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
        discord.ui.Section(
            discord.ui.TextDisplay(
                "**Start a Break**\n"
                "-# Your roles are saved, not deleted."
            ),
            accessory=discord.ui.Button(
                label="💤 Start",
                style=discord.ButtonStyle.secondary,
                custom_id=START_CUSTOM_ID,
            ),
        ),
        *hub_footer(footer_url),
    ]


def build_return_panel_layout(
    *,
    banner_url: str | None = None,
    footer_url: str | None = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The "I'm back" card in the break channel — the only one a member on a
    break can see. Art works as on :func:`build_break_panel_layout`, with
    :data:`RETURN_PANEL_TITLE` as the drawn name."""
    intro = "Take all the time you need. Nothing pings you while you're here."
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=RETURN_PANEL_TITLE),
            discord.ui.TextDisplay(intro),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.TextDisplay(f"## 💤 {RETURN_PANEL_TITLE}\n{intro}"),
        ]
    return [
        *head,
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
        discord.ui.Section(
            discord.ui.TextDisplay(
                "**I'm Back**\n"
                "-# Your saved roles come straight back."
            ),
            accessory=discord.ui.Button(
                label="👋 Back",
                style=discord.ButtonStyle.secondary,
                custom_id=END_CUSTOM_ID,
            ),
        ),
        *hub_footer(footer_url),
    ]


def build_reply_layout(*, ok: bool, starting: bool, message: str) -> list[discord.ui.Item[Any]]:
    """The ephemeral answer to either press. A refusal leads with ❌, per the
    guide — there is no red bar to carry it."""
    if not ok:
        return [discord.ui.TextDisplay(f"## 💤 Take a Break\n❌ {message}")]
    title = "## 💤 On a Break" if starting else "## 👋 Welcome Back"
    return [discord.ui.TextDisplay(f"{title}\n{message}")]
