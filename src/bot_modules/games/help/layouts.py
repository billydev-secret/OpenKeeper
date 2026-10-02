"""Components V2 card builders for the Games Help panel and ``/support``.

Replaces ``games_help/embeds.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "Games core"). These
functions accept plain dicts/primitives and return lists of
``discord.ui.Item``. They never call out to Discord — testable with no
network and no mocks of the Bot/Guild API.

Three builders:

* :func:`build_help_layout` — the ``/games help`` overview: every game
  grouped (party / duels & group / rooms & tables), one line each. The
  game picker itself is a ``discord.ui.Select`` the view wraps in an
  ``ActionRow`` and adds alongside these items — a control needs a
  callback, so it is not something a pure builder can return.
* :func:`build_game_detail_layout` — one game's rules, floor, pacing and
  length, shown when it is picked from the panel's select menu.
* :func:`build_support_layout` — the ``/support`` invite card.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import discord

from bot_modules.games.help.logic import (
    OTHER_COMMANDS_VALUE,
    support_invite_url,
    game_detail,
    help_groups,
)


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_help_layout(
    *, extra_lines: Iterable[str] = ()
) -> list[discord.ui.Item[Any]]:
    """Build the ``/games help`` overview card.

    **Paradigm 11 — menu dropdown** (the picker select lives in the view;
    this is the card that sits above and around it).

    Groups come from :func:`help_groups` (which iterates ``GAME_ICONS``, the
    canonical registry, so a new game shows up on its own). Discord's old
    25-field/1024-char-per-field embed ceilings have no layout equivalent —
    a card's only budget is the whole message's, guarded by
    ``core.layout.check_budget`` in the test, not a per-group split here.

    ``extra_lines`` are the channel-native rooms the cog found open on this
    server (Survivor, Mahjong, Guess Who, the casino); they land in the
    Rooms & Tables group.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            "## 🌸 Community Games\n"
            "Pick a game below to read the rules, how many players it needs "
            "and who keeps it moving — then press **Start Here** to launch "
            "it in this channel, or use the command shown."
        )
    ]
    for name, lines in help_groups(list(extra_lines)):
        if not lines:
            continue
        items.append(_text(f"### {name}\n" + "\n".join(lines)))
    items.append(_text(f"### ⚙️ Other Commands\n{OTHER_COMMANDS_VALUE}"))
    return items


def build_game_detail_layout(key: str) -> list[discord.ui.Item[Any]]:
    """One game's card: the in-game ❓ Help text, the floor and pacing from
    the play registry, a rough length, and the command to type.

    **Paradigm 11 — menu dropdown** (the card the picker's selection swaps
    the overview for).
    """
    d = game_detail(key)
    start_lines = "\n".join([f"`{d.command}`", *d.variant_lines])
    return [
        _text(f"## {d.icon} {d.name}\n{d.rules[:4000]}"),
        _text(
            f"**👥 Players:** {d.floor}\n"
            f"**⏱️ Typical Length:** {d.length}\n"
            f"**🎛️ Pacing:** {d.hosting_label} — {d.pacing}"
        ),
        _text(f"**▶️ Start With**\n{start_lines}"),
    ]


def build_support_layout() -> list[discord.ui.Item[Any]]:
    """Build the ``/support`` invite card. **Paradigm 7 — receipt.**"""
    url = support_invite_url()
    body = f"Join us here: {url}" if url else "This bot has no support server set up yet — ask a moderator here."
    return [
        _text(
            "## 🛟 Support Server\n"
            "Need help, want to report a bug, or share feedback?\n"
            f"{body}"
        )
    ]
