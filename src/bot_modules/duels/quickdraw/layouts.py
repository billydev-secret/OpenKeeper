"""Components V2 card builders for the Quickdraw duel (wave 3b).

The V2 replacement for the ``discord.Embed`` builders ``cog.py`` used to
build inline (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b). Pure builders: plain
strings/floats in, ``discord.ui.Item`` lists out, no IO and no Discord
calls beyond constructing components.

Three card shapes, matching the render-hook seam
(``docs/specs/dk_pvp_games_suite_spec.md`` "Card format",
``bot_modules/duels/base_game.py`` module docstring):

* :func:`build_turn_card_layout` — **paradigm 9, game turn card.** Content
  only: the persistent 🔫 Fire button lives in
  ``QuickdrawLayoutView``'s own control row (``views.py``), appended by
  ``render()`` after these items, never here. Covers every ``qd_state``
  the message ever shows — WAITING, the live draw signal (DRAW and
  WINNER_FIRED render identically on purpose: the opponent must stay
  blind to a winner who already fired, or their press stops being a
  genuine reaction). A finished round has no frame of its own: the
  result card (below) replaces the turn card in the same message.
* :func:`build_void_layout` — **paradigm 7, receipt.** The bare "nobody
  fired in time" terminal notice, built on
  ``duels.layouts.build_duel_notice_layout`` — the shared shape every
  other duel-core terminal notice already uses, not a hand-rolled twin.
* :func:`build_result_items_layout` — **paradigm 7.** Not sent on its
  own: ``render_result_state`` returns this list of items and
  ``BaseGame._result_layout`` composes it into ``ResultLayoutView``
  behind a 🏆/💀 names line and ahead of the Name the Loser / Run It
  Back row. Since that names line already names both
  players, these items describe *what happened* without re-naming
  either one (``docs/components_v2_style_guide.md`` § Mentions: "name a
  person once ... never both") — the reaction times stand on their own,
  earlier/later matching the names line's winner-then-loser order.

Every display name reaching these builders is a member's own
``display_name`` (or a stand-in nickname from the nickname-forfeit flow)
— member-controlled text with no prior escaping — so every one of them
is run through ``discord.utils.escape_markdown`` at the call site in
``cog.py`` before it lands in a parameter here; a ``TextDisplay`` renders
markdown where an embed field never did
(``docs/reviews/2026-09-21-components-v2-wave-3a.md``, the wave's #1
repeated defect). ``stakes_text`` is pre-escaped upstream
(``duels.filters.validate_stakes``), so it is not re-escaped here.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.duels.layouts import build_duel_notice_layout


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_turn_card_layout(
    qd_state: str,
    p1_name: str,
    p2_name: str,
    stakes_text: str,
) -> list[discord.ui.Item[Any]]:
    """The turn card's content for whichever live ``qd_state`` it's showing."""
    if qd_state in ("DRAW", "WINNER_FIRED"):
        heading = "## 🔫 Draw!!!"
        body = f"**{p1_name}** vs **{p2_name}**\n\n**FIRE NOW — first to click wins!**"
    else:
        heading = "## 🤠 Quickdraw"
        body = (
            f"**{p1_name}** vs **{p2_name}**\n\n"
            "⏳ Waiting for the draw signal… **Don't fire early!**"
        )
    return [
        _text(f"{heading}\n{body}"),
        _text(f"**📋 Stakes**\n{stakes_text}"),
    ]


def build_void_layout(p1_name: str, p2_name: str) -> list[discord.ui.Item[Any]]:
    """Paradigm 7 — nobody fired before the draw window closed."""
    body = (
        f"**{p1_name}** vs **{p2_name}** — nobody fired in time. "
        "Round voided — no nickname penalty."
    )
    return build_duel_notice_layout("🌵 Nobody Drew", body)


def build_result_items_layout(
    *,
    clean_draw: bool,
    stakes_text: str,
    winner_reaction: float | None = None,
    loser_reaction: float | None = None,
    nick_heading: str | None = None,
    nick_body: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The mini-game's own content for the composed result card.

    ``winner_reaction``/``loser_reaction`` are seconds since the draw
    signal (``None`` for the false-start case, or for a loser who never
    fired before the draw window closed). ``nick_heading``/``nick_body``
    are the caller's already-assembled nickname-flow line (one of
    ``BaseGame.nick_self_apply_copy`` / ``nick_applied_copy`` /
    ``awaiting_nick_copy``, with any member name already escaped) — this
    builder just lays it out, since composing it needs the sentence-hours
    config the cog holds, not this pure module.
    """
    if clean_draw:
        heading = "## ⚡ Quickdraw!"
        if winner_reaction is not None and loser_reaction is not None:
            margin = loser_reaction - winner_reaction
            body = (
                f"🏆 **{winner_reaction:.3f}s** · 💀 **{loser_reaction:.3f}s** "
                f"· Won by **{margin:.3f}s**"
            )
        elif winner_reaction is not None:
            body = f"🏆 **{winner_reaction:.3f}s** · 💀 *didn't draw*"
        else:
            body = "🏆 Fastest draw wins."
    else:
        heading = "## 💀 False Start!"
        body = "Fired before the draw signal."

    items: list[discord.ui.Item[Any]] = [
        _text(f"{heading}\n{body}"),
        _text(f"**📋 Stakes**\n{stakes_text}"),
    ]
    if nick_heading and nick_body:
        items.append(_text(f"**{nick_heading}**\n{nick_body}"))
    return items
