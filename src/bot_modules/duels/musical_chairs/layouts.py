"""Pure Components V2 card builders for Musical Chairs.

The Components V2 replacement for the inline ``discord.Embed`` cards
``MusicalChairsCog`` used to build itself
(``docs/plans/components_v2_migration.md`` wave 3b). Every function here is
**pure**: primitives and a ``name_fn`` in, a list of ``discord.ui.Item`` out
— no IO, no accent colour, no callbacks (guide § No container). The cog's
``SitView`` (``views.py``) adds the returned items to its layout and appends
its own persistent 🪑 Sit control row last.
"""
from __future__ import annotations

from typing import Any

import discord

from bot_modules.shared.name_resolver import NameFn, mention

Item = discord.ui.Item[Any]


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_round_layout(
    *,
    phase: str,
    round_no: int,
    chairs: int,
    alive: list[int],
    stakes: str,
    name_fn: NameFn = mention,
) -> list[Item]:
    """**Paradigm 9** (game turn card): the live round panel, edited in
    place on every phase flip and every seat claim. Carries no buttons of
    its own — the cog's ``SitView`` supplies the persistent 🪑 Sit control
    row every render re-attaches last.

    ``stakes`` arrives pre-formatted and pre-escaped by the caller
    (``duels.filters.validate_stakes`` already escapes a custom stakes
    string; the built-in fallback line is bot-authored text).
    """
    names = ", ".join(name_fn(u) for u in alive) or "—"
    if phase == "SCRAMBLE":
        heading = "## 🪑 Sit!!! — Grab a Chair!"
        body = (
            "The music stopped — **press 🪑 Sit now!**\n"
            f"Only **{chairs}** will get a seat; everyone slower is out."
        )
    else:  # MUSIC
        heading = f"## 🎵 Musical Chairs — Round {round_no}"
        body = (
            "🎶 …the music is playing… **don't sit yet** — press 🪑 Sit "
            "while the music is on and you're out.\n"
            "Wait for this panel to turn red and say **Sit!**"
        )
    facts = f"🪑 Chairs: **{chairs}** · 👥 Still In: {names}"
    return [_text(f"{heading}\n{body}\n\n{facts}\n📋 {stakes}")]


def build_result_layout(
    *,
    players: int,
    stakes: str,
    nick_stake: bool,
    imposed_nick: str | None,
    self_apply_nick: str | None,
    nick_span: str,
    naming_deadline: int,
) -> list[Item]:
    """**Paradigm 7** (receipt): the game-over card
    ``BaseGame._result_layout`` appends after its own
    🏆/💀 names line — so this describes the outcome, the stakes and the
    nickname sentence without re-naming either player (guide § Mentions: a
    person is named once in a card, never in both that line and the
    body).

    ``naming_deadline`` is a unix timestamp rendered as a live
    ``<t:…:R>`` countdown (guide § Words: count down, don't state — "closes
    in 24 hours" is only true the instant it's posted). ``imposed_nick`` /
    ``self_apply_nick`` are member-typed text and are escaped here;
    ``stakes`` arrives already escaped by the caller.
    """
    lines = [
        "## 🪑 Musical Chairs — Game Over",
        "Takes the last chair!",
        "",
        f"👥 **{players}** players · 📋 {stakes}",
    ]
    if self_apply_nick:
        safe = discord.utils.escape_markdown(self_apply_nick)
        lines += [
            "",
            "🏷️ **Nickname — Over To You** — Discord won't let me rename "
            f"the server owner, so it's on the honour system: **{safe}**, "
            f"for {nick_span}.",
        ]
    elif imposed_nick:
        safe = discord.utils.escape_markdown(imposed_nick)
        lines += ["", f"🏷️ Now known as **{safe}** for {nick_span}."]
    elif nick_stake:
        lines += [
            "",
            f"⏳ Awaiting nickname — press **Name the Loser** on this card "
            f"<t:{naming_deadline}:R>. It lasts {nick_span}.",
        ]
    return [_text("\n".join(lines))]
