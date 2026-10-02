"""Components V2 card builders for the games-config admin cog.

Replaces ``games_config/embeds.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "Games core"). Four of
the six embed builders that lived there had no caller left at all:
``/games allow-channel``, ``/games disallow-channel``, ``/games
list-channels`` and ``/games audit-channel`` were already removed in the
move to dashboard config (``src/web_server/routes/games.py``), so
``build_channel_allowed_embed``, ``build_channel_disallowed_embed``,
``build_channel_list_embed`` and ``build_audit_channel_embed`` are deleted
rather than converted — along with the ``logic.py`` helpers that fed only
them (``format_allowed_channels``, ``audit_channel_change``,
``ChannelResolver``).

Every function here is pure: primitives and a ``name_fn`` in, a list of
``discord.ui.Item`` out. No IO, no accent lookup, no callbacks — the cog
wraps the result in a bare ``LayoutView`` for the one-off sends both cards
are (guide § The paradigms, paradigm 7 — receipt/notice).
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.shared.name_resolver import NameFn, mention

from .logic import describe_active_game, describe_force_end


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_game_status_layout(
    row: Any, name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """Mod-facing card for ``/games config game-status``.

    **Paradigm 7 — receipt.** ``row`` is the ``games_active_games`` row (or
    ``None`` when no game is running in the channel); the glyph carries the
    idle/active state so the title line answers the question at a glance.
    ``name_fn`` names the host — never a bare ``<@id>`` (a Text Display
    mention renders from the *reading* client's own cache, so it would show
    as digits to a mod who hasn't seen the host).
    """
    title, body = describe_active_game(row, name_fn)
    glyph = "🎲" if row else "💤"
    return [_text(f"## {glyph} {title}\n{body}")]


def build_force_end_layout(game_type: str) -> list[discord.ui.Item[Any]]:
    """Notice posted to the channel after ``/games end`` force-closes a game.

    **Paradigm 7 — notice.**
    """
    return [_text(f"## 🛑 Game Force-Closed\n{describe_force_end(game_type)}")]
