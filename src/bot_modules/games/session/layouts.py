"""Components V2 card builder for the Session Recap cog.

Replaces ``games_session/embeds.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "Games core"). This
function accepts plain dicts/primitives and returns a list of
``discord.ui.Item``. It never calls out to Discord — testable with no
network and no mocks of the Bot/Guild API.

The one card :func:`build_session_recap_layout` is the recap view shown by
``/recap``. The cog builds a ``name_fn`` for the players and resolves
highlight strings against the live guild before calling so this layer stays
pure.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.shared.name_resolver import NameFn, mention


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_session_recap_layout(
    game_count: int,
    player_ids: list[int],
    duration_str: str,
    highlights: list[str],
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """Build the ``/recap`` card. **Paradigm 7 — receipt.**

    ``player_ids`` are rendered by name through ``name_fn`` — never as a
    ``<@id>``, which renders as digits to any client that hasn't cached
    that member (a departed player, for everyone). The default is a
    mention only so an un-wired caller still renders; the cog's render-site
    test forces it to pass one. ``highlights`` are the pre-formatted
    strings from :func:`build_highlights`; only the first 8 are shown to
    keep the card under the layout budget.

    Empty ``player_ids`` and empty ``highlights`` both skip their
    respective lines so a sparse session doesn't render dashes.
    """
    lines = [
        f"**🎮 Games Played:** {game_count}",
        f"**👥 Unique Players:** {len(player_ids)}",
        f"**⏱️ Total Duration:** {duration_str}",
    ]
    items: list[discord.ui.Item[Any]] = [
        _text("## 📋 Games Session Recap\n" + "\n".join(lines))
    ]

    if player_ids:
        items.append(
            _text(
                "**🏆 Players**\n"
                + ", ".join(name_fn(int(uid)) for uid in player_ids[:10])
            )
        )

    if highlights:
        items.append(
            _text(
                "**Game Highlights**\n"
                + "\n".join(f"• {h}" for h in highlights[:8])
            )
        )

    return items
