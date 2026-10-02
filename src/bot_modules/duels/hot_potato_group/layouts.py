"""Components V2 card builders for Hot Potato (Group).

Replaces the two ``discord.Embed`` builders on
``HotPotatoGroupGameCog`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "games"). Pure
functions: ids/primitives and a ``name_fn`` in, a list of
``discord.ui.Item`` out. No IO, no Discord calls, no accent colour — the
cog resolves everything (names, the fuse emoji, the config fallback text)
before calling, so these are testable with no network and no mocks of the
Bot/Guild API.

Two card shapes:

* :func:`build_holding_layout` — **paradigm 8, live board.** The live
  round card: who's still in, who's out, who is holding the bomb, and the
  stakes. Rebuilt and re-rendered onto the same persistent
  ``PassGroupLayoutView`` (``duels/hot_potato_group/views.py``) on every
  pass, every elimination and every restart resume — the same "same card,
  redrawn in place" shape as ``games_story.layouts.build_story_board_layout``
  and the MFK/Compliment lobby cards, not a one-shot "prompt, then reveal"
  turn card (paradigm 9): there is no reveal here, only the same card
  ticking forward until the final pass ends the game. Carries **no
  controls** — the base (``duels.base_game.BaseGame``) owns the Pass
  button's control row and only ever calls ``PassGroupLayoutView.render``
  with what this returns.
* :func:`build_result_layout` — **paradigm 7, receipt** — content only.
  ``BaseGame._result_layout`` composes this into its own paradigm-5
  decision card: a 🏆/💀 names line first, then these
  items, then the Name the Loser / Run It Back buttons. Its docstring is
  explicit that *this* list "should describe the outcome without
  re-naming the winner/loser" — the same person named twice on the same
  card is exactly what the style guide's § Mentions
  rules out — so nobody is named here by role or by resolved name; see
  the function's own docstring for how "Bravest Hands" (a third stat that
  can legitimately coincide with either pinged player) is handled.

``stakes_text`` (a host-typed custom stakes string, ``game.stakes_text``)
arrives **already markdown-escaped**: ``duels.filters.validate_stakes`` —
the one place it is ever written, for a fresh lobby and for every rematch
that re-launches one — returns it pre-escaped, precisely because the same
column also carries bot-composed text like "💰 **10** gems to join" that
must render its own markdown untouched (see that function's docstring).
Escaping it again here, the way ``games_mfk``/``games_compliment`` escape
their own free text, would double-escape a member's literal ``*`` into
``\\\\*``. ``nick_line`` (the pre-formatted nickname-stake sentence, built
by the cog from ``BaseGame``'s ``nick_applied_copy`` /
``nick_self_apply_copy`` / ``awaiting_nick_copy``) is likewise handed over
already escaped: those shared helpers escape the typed nickname
themselves, at render, because the raw value is also written to Discord as
the loser's real nickname.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.shared.name_resolver import NameFn, mention


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_holding_layout(
    alive_ids: list[int],
    eliminated_ids: list[int],
    holder_id: int | None,
    emoji: str,
    stakes_text: str | None,
    *,
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """The live round card. **Paradigm 8 — live board.**

    ``emoji`` is ``game.shake_emoji``'s escalating potato/bomb string — the
    "creeping-dread tell" the fuse's hidden countdown gives instead of a
    number (``docs/specs/dk_pvp_games_suite_spec.md`` § 9.4) — led into the
    title exactly as the pre-conversion embed's title did.

    ``alive_ids``/``eliminated_ids`` are rendered through ``name_fn``
    (never a raw mention — a Text Display mention is resolved from the
    *reading* client's own cache and shows as a bare number to anyone who
    hasn't seen that member). ``eliminated_ids`` renders no line at all
    when empty, matching the embed's conditional "Out" field.
    ``holder_id`` of ``None`` (a state that should not reach a live round
    but the pre-conversion embed also guarded) renders as ``?``.

    ``stakes_text`` is the caller's resolved stakes line, already
    markdown-safe — see the module docstring for why this builder must
    not run ``escape_markdown`` on it a second time. ``None``/empty falls
    back to the plain-nickname-forfeit default, matching the embed.
    """
    alive = ", ".join(name_fn(u) for u in alive_ids) or "—"
    holder = name_fn(holder_id) if holder_id is not None else "?"

    lines = [f"## {emoji} Hot Potato (Group)", f"**Still in:** {alive}"]
    if eliminated_ids:
        lines.append(f"**Out:** {', '.join(name_fn(u) for u in eliminated_ids)}")
    lines.append(f"🤲 **Holding:** {holder} — pass it before it blows!")
    lines.append(f"📋 **Stakes:** {stakes_text or 'Loser surrenders their nickname.'}")
    return [_text("\n".join(lines))]


def build_result_layout(
    player_count: int,
    stakes_line: str,
    *,
    bravest_id: int | None = None,
    bravest_seconds: float = 0.0,
    nick_line: str | None = None,
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """The result card's own content. **Paradigm 7 — receipt** (content
    only — see the module docstring for how ``BaseGame._result_layout``
    composes it: names line, then this, then the result buttons).

    No heading of its own and nobody named or pinged by role: the
    composed card's names line is the whole message's lead line for this
    paradigm (``_result_layout``'s docstring), so a ``## Title`` here
    would duplicate it, and the outcome line below names neither the
    winner nor the loser, matching the precedent in
    ``scripts/layout_samples.py``'s ``duels_samples`` (a bare "Knockout in
    round two. Match over." line, no re-naming).

    ``bravest_id`` is the one exception to "nobody is named here": it is
    a third stat — the player, of any of the ones in ``roster`` (not just
    the winner/loser), who held the bomb longest in total — genuinely
    informative on its own right rather than a restatement of who won or
    lost. It can coincide with the pinged winner or loser (a natural
    outcome — the winner is often also the steadiest holder), which is a
    real gap the "never re-name" rule (written for a two-party duel) does
    not resolve either way; named here on the read that a *distinct fact
    about* a pinged person is not the same thing as re-announcing their
    identity. ``None`` (a single-round game with no completed hold, or a
    solo winner) omits the line entirely, matching the embed's
    conditional "Bravest Hands" field.

    ``stakes_line``/``nick_line`` are the caller's fully-resolved,
    already-markdown-safe strings — see the module docstring for why this
    builder does not escape either again.
    """
    lines = ["💥 The bomb went off on the final pass — game over!"]
    lines.append(f"**Players:** {player_count}")
    if bravest_id is not None:
        lines.append(
            f"🫡 **Bravest Hands:** {name_fn(bravest_id)} held {bravest_seconds:.0f}s total"
        )
    lines.append(f"📋 **Stakes:** {stakes_line}")
    if nick_line:
        lines.append(nick_line)
    return [_text("\n".join(lines))]
