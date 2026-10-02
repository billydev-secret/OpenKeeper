"""Components V2 card builders for the Hot Potato (duel) mini-game.

The V2 replacement for this cog's embed-built cards
(``docs/specs/dk_pvp_games_suite_spec.md`` §9.3, ``docs/plans/
components_v2_migration.md`` wave 3b). Pure builders: primitives and a
``name_fn`` in, a list of ``discord.ui.Item`` out — no IO, no Discord calls,
no accent colour.

Two cards:

* :func:`build_hot_potato_card` — the live turn card
  (``BaseGame.render_game_state``). **Paradigm 9 — game turn card**
  (``docs/components_v2_style_guide.md`` § The paradigms): a prompt only,
  no controls — the base's ``PassLayoutView`` owns the PASS button in its
  own control row. Branches on ``game.winner_id`` exactly as the embed arm
  did: the holding state while the timer runs, and the same card
  re-rendered once more, disabled, as the terminal BOOM paint ``_explode``
  makes right before the result card posts.
* :func:`build_hot_potato_result` — the result content
  (``BaseGame.render_result_state``). Feeds **paradigm 5 — decision card**:
  ``BaseGame._result_layout`` composes the names line (🏆 winner,
  💀 loser) ahead of these items and the Name the Loser / Run It Back
  buttons after, so this list must not re-name the winner or loser that
  line already named (guide § Mentions: "name a person once ... never
  both"). It uses the role words "🏆 Winner" / "💀 Loser" instead, and the
  caller passes the already-composed nickname-sentence copy
  (``BaseGame.nick_applied_copy`` etc., which take a ``who`` string) built
  the same way, with "The Winner"/"The Loser" in place of a resolved name.

Both players are always named (or role-worded) on this game's cards — a
no-contact pair never reaches a duel at all (``BaseDuel._base_challenge``
refuses the challenge before a game exists), so neither builder carries a
degrade case.

``game.stakes_text`` is member-typed free text (``/hotpotato challenge
stakes:``) but arrives here **already markdown-escaped**:
``duels.filters.validate_stakes`` escapes the member-typed portion before
``_base_challenge`` ever persists it as ``stakes_text``, so a builder that
escapes it again doubles the backslashes on the card. Read it as-is, the
same contract every other ``stakes_text`` reader (``duels/layouts.py``'s
``build_challenge_layout``) follows.
"""
from __future__ import annotations

from typing import Any

import discord

from bot_modules.shared.name_resolver import NameFn

from .game import HotPotatoGame


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_hot_potato_card(
    game: HotPotatoGame, name_fn: NameFn
) -> list[discord.ui.Item[Any]]:
    """The live turn card. See module docstring."""
    c_name = name_fn(game.challenger_id)
    t_name = name_fn(game.target_id)
    pass_count = max(0, len(game.pass_log) - 1)
    stakes = game.stakes_text or "Loser surrenders their nickname."

    if game.winner_id:
        heading = "## 💥 BOOM!"
        body = f"**{c_name}** vs **{t_name}** — the potato exploded!"
    else:
        holder_name = name_fn(game.holder_id) if game.holder_id else "?"
        heading = "## 🥔 Hot Potato"
        body = (
            f"**{c_name}** vs **{t_name}**\n\n"
            f"**{holder_name}** is holding the potato! Quick — PASS it!"
        )

    facts = f"🥔 Passes: {pass_count} · 📋 Stakes: {stakes}"
    return [_text(f"{heading}\n{body}\n\n{facts}")]


def build_hot_potato_result(
    pass_count: int,
    stakes_line: str,
    *,
    style_lines: list[str] | None = None,
    nick_heading: str | None = None,
    nick_body: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The result content. See module docstring.

    ``stakes_line`` is already resolved and, when it carries member text,
    escaped by the caller (``game.stakes_text`` or
    ``BaseGame.nick_forfeit_copy(sentence_hours)`` — the same source the
    embed arm read). ``style_lines`` are pre-formatted
    ``"**🏆 Winner**: +N pts — now has M style points"``-shaped strings,
    role words already substituted for names. ``nick_heading``/
    ``nick_body`` are the nickname-sentence section — Applied / Over To
    You / Awaiting, mutually exclusive with each other — pre-built by the
    caller through the shared ``BaseGame`` copy helpers with a role word in
    place of a name.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            "## 💥 Hot Potato!\n"
            "The Loser was holding it when it blew!\n\n"
            f"🥔 Passes: {pass_count} · 📋 Stakes: {stakes_line}"
        )
    ]
    if style_lines:
        items.append(
            _text("### ✨ Style Points (Danger Zone)\n" + "\n".join(style_lines))
        )
    if nick_heading and nick_body:
        items.append(_text(f"### {nick_heading}\n{nick_body}"))
    return items
