"""Components V2 card builders for the Pressure Cooker cog.

Replaces the two ``discord.Embed`` builders removed from ``cog.py`` in this
same commit (``docs/plans/components_v2_migration.md`` wave 3b, "games").
Pure — primitives and a ``name_fn`` in, a list of ``discord.ui.Item`` out;
no IO, no accent colour (``docs/components_v2_style_guide.md`` § No
container: pressure is a gauge and a meter bar, never a red→green tint).

Two builders:

* :func:`build_pump_layout` — the live turn card. Called on a game no
  longer ``ACTIVE`` it simply drops the Turn line. **Paradigm 9 — game
  turn card**: a state summary,
  then the single Pump button (``GameView`` in ``views.py``) below it.
* :func:`build_boom_layout` — the result card's own facts.
  ``duels.base_game.BaseGame._result_layout`` composes the fullest card a
  member actually sees: the names line (🏆 winner, 💀 loser — the one who
  busted it), *then these items*, then the Name the Loser / Run It Back
  row, edited over the turn card in place. Because the names line
  already names both players, this builder never repeats either by
  resolved name (style guide § Mentions: "name a person once ... never
  both"), and summarizes the pump count rather than re-listing it. **Paradigm 5 — decision card**: this builder supplies
  the facts half; the base builds the buttons.
"""
from __future__ import annotations

from typing import Any, Sequence

import discord

from bot_modules.shared.name_resolver import NameFn, mention

from .game import PumpEntry
from .views import gauge_bar


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _sep() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large)


def build_pump_layout(
    challenger_id: int,
    target_id: int,
    gauge: int,
    pumps: Sequence[PumpEntry],
    active_player_id: int | None,
    state: str,
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """Build the live Pressure Cooker turn card. **Paradigm 9 — game turn
    card.**

    ``pumps`` is the game's full pump history; only the last five render,
    same window the embed used. ``active_player_id``/``state`` gate the
    Turn line — it renders only while the game is still ``"ACTIVE"``, so
    calling this again after a bust (``state == "RESOLVED"``) naturally
    drops it without a separate "busted" builder. Names come from
    ``name_fn`` — never a raw ``<@id>``, which a client that hasn't cached
    that member would render as a bare number.
    """
    p1 = name_fn(challenger_id)
    p2 = name_fn(target_id)
    items: list[discord.ui.Item[Any]] = [
        _text(f"## 🔥 Pressure Cooker\n**{p1}** vs **{p2}**"),
        _text(f"**Gauge:** {gauge_bar(gauge)}"),
    ]

    if pumps:
        lines: list[str] = []
        for entry in pumps[-5:]:
            name = name_fn(entry.player_id)
            gauge_after = entry.gauge_before + entry.roll
            bust_marker = " 💥" if gauge_after >= 100 else ""
            lines.append(f"**{name}**: +{entry.roll} → {gauge_after}/100{bust_marker}")
        items.append(_sep())
        items.append(_text("### Recent Pumps\n" + "\n".join(lines)))

    if active_player_id and state == "ACTIVE":
        items.append(_sep())
        items.append(_text(f"▶️ **Turn:** {name_fn(active_player_id)}"))

    return items


def build_boom_layout(
    gauge: int,
    pump_count: int,
    stakes_text: str | None,
    forfeit_copy: str,
    *,
    imposed_copy: str | None = None,
    self_apply_copy: str | None = None,
    awaiting_copy: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build the Pressure Cooker result card's own facts. **Paradigm 5 —
    decision card** (the facts half; ``BaseGame._result_layout`` supplies
    the names line ahead of these items and the button row after).

    Never names the winner or loser — the names line the base prepends
    already does (style guide § Mentions: "name a person once ... never
    both"). ``stakes_text`` is a mixed column: it comes back from
    ``duels.filters.validate_stakes`` **already markdown-escaped** for its
    member-typed portion (``/pressure challenge stakes``), then has
    bot-composed lines like the wager line spliced onto it by
    ``resolve_stakes_text``. Escaping it again here would double-escape
    the member half (visible literal backslashes) and corrupt the bot's
    own formatting on the wager/forfeit lines, so this builder renders it
    as-is — the one escape already happened at creation, the one place
    that can tell the member text from the bot's own. ``forfeit_copy``/``imposed_copy``/
    ``self_apply_copy``/``awaiting_copy`` arrive pre-formatted and
    pre-escaped by the cog — the same contract
    ``duels.layouts.build_duel_notice_layout`` uses for its ``body`` — so
    this builder never has to know a duel's duration dial
    (``BaseGame._hours_span``/``_span``, which the cog's own static-string
    sweep, ``tests/duels/test_duels_copy.py::
    test_no_hard_coded_durations_remain_in_member_facing_strings``,
    forbids literal durations from bypassing) or a nickname's own
    escaping. Exactly one of ``imposed_copy``/``self_apply_copy``/
    ``awaiting_copy`` is set at a time, matching the mutually exclusive
    states ``render_result_state`` can be called with.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(f"## 💥 Boom.\nThe gauge hit **{gauge}/100** after **{pump_count}** pumps."),
    ]

    stakes = stakes_text if stakes_text else forfeit_copy
    items.append(_sep())
    items.append(_text(f"### 📋 Stakes\n{stakes}"))

    if self_apply_copy:
        items.append(_sep())
        items.append(_text(f"### 🏷️ Nickname — Over To You\n{self_apply_copy}"))
    elif imposed_copy:
        items.append(_sep())
        items.append(_text(f"### 🏷️ Nickname Applied\n{imposed_copy}"))
    elif awaiting_copy:
        items.append(_sep())
        items.append(_text(f"### ⏳ Awaiting Nickname\n{awaiting_copy}"))

    return items
