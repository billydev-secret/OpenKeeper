"""Components V2 builder for the posted Mod Settings panel.

Pure, like every ``layouts`` module: a ``logic`` value in, a list of
``discord.ui.Item`` out. The view in ``cogs.mod_settings_cog`` binds
behaviour by ``custom_id``. Rows are drawn the way Roles & Pings draws its
Bot Pings (``server_features.layouts.build_roles_layout``) so a moderator who
knows one knows the other — but the panel is public, so a row shows no state:
the press answers privately with what it did.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

from bot_modules.moderation.settings.logic import EMPTY_TEXT, StaffRow

TITLE = "## 🛠️ Mod Settings"

#: Frozen prefix: every posted copy carries ``prefix + ping key`` on its
#: buttons, and changing either orphans them.
PING_CUSTOM_ID_PREFIX = "mod_settings:ping:"

TOGGLE_LABEL = "On / Off"


def ping_custom_id(key: str) -> str:
    return f"{PING_CUSTOM_ID_PREFIX}{key}"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_mod_settings_hub_layout(rows: Sequence[StaffRow]) -> list[discord.ui.Item[Any]]:
    """The posted panel. **Paradigm 1 — hub panel**, persistent.

    One row per staff ping the guild has set up; its button switches that
    ping on for the moderator who pressed it, or off if they already have it.
    """
    if not rows:
        return [_text(f"{TITLE}\n{EMPTY_TEXT}")]
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"{TITLE}\nPick which staff jobs ping you. Press a job to turn its "
            "ping on, or off if you already have it — only you see the answer. "
            "For moderators only."
        ),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
    ]
    for row in rows:
        items.append(
            discord.ui.Section(
                _text(f"{row.emoji} **{row.label}**\n-# {row.blurb}"),
                accessory=discord.ui.Button(
                    label=TOGGLE_LABEL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=ping_custom_id(row.key),
                ),
            )
        )
    return items
