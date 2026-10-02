"""Pure Components V2 card builders for Chicken.

**Paradigm 8 — live board** (``docs/components_v2_style_guide.md`` § The
paradigms): :func:`build_round_layout` is the sticky climb card, redrawn on
every bail and every ~2s meter tick by ``ChickenCog``'s ticker and
``_on_bail`` — text only, no stable per-player ids to track since the whole
board reflows each time (there is no sub-card worth pinning in place).

**Paradigm 7 — receipt**: every result card
(:func:`build_crash_result_layout`, :func:`build_everyone_blinked_layout`,
:func:`build_total_wipeout_layout`) is a bare text notice. The crash card is
composed by the shared duel base (``duels.base_game.BaseGame._result_layout``),
which prepends a ``TextDisplay`` naming both winner and loser, so this
module never names either one — the caller (``ChickenCog``) passes only
counts and percentages, per the guide's mentions rule ("name a person once
... never both"). ``build_everyone_blinked_layout`` has no such names line
of its own (it's edited in outside the base's result seam, since a cosmetic
resolution has no loser); its winner is instead named exactly once through
``winner_ref``, the caller's resolved name — never a mention, since the
card is an edit of the board and an edit notifies nobody.

Like ``casino/layouts.py``, every card here is one flowing ``TextDisplay``
per section rather than a ``Section`` per field: these are all receipts or a
live board (guide's own model for both is "bare text"), and each field
becomes a bold label line inside the block instead of its own component —
a deliberate deviation from mirroring dm_perms's Section-per-row shape, the
same one casino's module documents.

Pure, like every ``*_layouts.py`` module (``test_embed_builder_purity_contract.py``):
primitives + ``name_fn`` in, a list of ``discord.ui.Item`` out. No IO, no
accent colour (V2 has none), no callbacks.
"""
from __future__ import annotations

from typing import Any

import discord

from bot_modules.core import meters
from bot_modules.shared.name_resolver import NameFn

Item = discord.ui.Item[Any]

_BAR_WIDTH = 16


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def meter_bar(pct: float) -> str:
    """The crash meter, code-spanned so it holds one length as it fills."""
    return meters.mono(meters.fill(pct, 100, _BAR_WIDTH))


def build_round_layout(
    game: Any,
    *,
    pct: float,
    name_fn: NameFn,
) -> list[Item]:
    """The live climb card. **Paradigm 8 — live board.**"""
    holders = ", ".join(name_fn(u) for u in game.alive) or "—"
    bailed = ", ".join(
        f"{name_fn(b['player_id'])} ({b['meter_pct']:.0f}%)" for b in game.bail_log
    ) or "—"
    if game.stakes_text:
        stakes = discord.utils.escape_markdown(game.stakes_text)
    else:
        stakes = "Loser surrenders their nickname."
    parts = [
        "## 🐔 Chicken\n"
        "Bail to get out safely — but the crash point is **hidden**, and "
        "anyone still holding when it blows **crashes**.",
        f"**Still Holding**\n{holders}",
        f"**Bailed**\n{bailed}",
        f"**⚡ Meter — {pct:.0f}%**\n"
        f"{meter_bar(pct)}\n"
        "↑ it can blow at any moment. blink first or ride it out.",
        f"**📋 Stakes**\n{stakes}",
    ]
    return [_text("\n\n".join(parts))]


def nick_self_apply_section(nick: str, span: str) -> str:
    """The "server owner" nickname sub-section — no name: the loser (the
    owner) is already named by the base's result-card names line."""
    safe = discord.utils.escape_markdown(nick)
    return (
        "**🏷️ Nickname — Over To You**\n"
        f"Discord won't let me rename the server owner, so they have to set "
        f"**{safe}** themselves. It stands for {span}."
    )


def nick_applied_section(nick: str, span: str) -> str:
    """The "nickname landed" sub-section — no name, same reason."""
    safe = discord.utils.escape_markdown(nick)
    return f"**🏷️ Nickname Applied**\nNow known as **{safe}** for {span}."


def awaiting_nick_section(deadline_ts: float, span: str) -> str:
    """The "winner hasn't named the loser yet" sub-section. Counts down to
    ``deadline_ts`` rather than stating the window (guide § Words) — no
    name, the winner is already pinged."""
    return (
        "**⏳ Awaiting Nickname**\n"
        f"Press **Name the Loser** <t:{int(deadline_ts)}:R>. "
        f"The nickname lasts {span}."
    )


def build_crash_result_layout(
    *,
    blew_at_pct: float,
    n_alive: int,
    bravest_pct: float | None,
    stakes: str,
    nick_section: str | None,
) -> list[Item]:
    """The crash-with-loser card, composed *under* the base's names line
    (``BaseGame._result_layout``): winner and loser are never named here —
    only counts and the meter reading. **Paradigm 7 — receipt.**"""
    if n_alive > 1:
        holding_line = (
            f"😵 {n_alive} still holding when it blew — the stake went to "
            "one of them, drawn at random."
        )
        stake_line = "Drawn at random from everyone who crashed."
    else:
        holding_line = "😵 Only one still holding when it blew — no draw needed."
        stake_line = "The only one left holding."

    parts = [f"## 💥 Crash at {blew_at_pct:.0f}%!\n{holding_line}"]
    if bravest_pct is not None:
        parts.append(f"**🐔 Nerves of Steel**\nBailed last at {bravest_pct:.0f}%.")
    parts.append(f"**💀 Takes the Stake**\n{stake_line}")
    parts.append(f"**📋 Stakes**\n{stakes}")
    if nick_section:
        parts.append(nick_section)
    return [_text("\n\n".join(parts))]


def build_everyone_blinked_layout(
    *,
    winner_ref: str,
    ranking: list[tuple[str, float]],
) -> list[Item]:
    """Cosmetic resolution: everyone bailed, no nickname stake. ``ranking``
    is the bail log by meter %, already excluding the winner's own row (they
    are already named once by ``winner_ref``). **Paradigm 7 — receipt.**"""
    parts = [f"## 🐔 Everyone Blinked!\n🏆 **{winner_ref}** held longest. No nicknames today."]
    if ranking:
        lines = [f"**{name}** — {pct:.0f}%" for name, pct in ranking]
        parts.append("**Chicken Ranking**\n" + "\n".join(lines))
    return [_text("\n\n".join(parts))]


def build_total_wipeout_layout(*, blew_at_pct: float) -> list[Item]:
    """Nobody bailed at all — no winner, no loser, pot refunded.
    **Paradigm 7 — receipt.**"""
    body = (
        f"## 💥 Total Wipeout!\n"
        f"Nobody blinked — everyone crashed at {blew_at_pct:.0f}%. No winner, "
        "no nickname, and the pot is refunded: any wager goes straight back "
        "to the players."
    )
    return [_text(body)]
