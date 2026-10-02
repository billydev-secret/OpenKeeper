"""The Components V2 frame shared by the three reactive round games.

Would You Rather, Never Have I Ever and Most Likely To are the same shape —
a round opens, the room votes, someone presses Next — and
``round_pacing`` already holds the rules they share so they cannot drift.
This module holds the *cards* they share, for the same reason: one round
frame, one host panel and one recap frame, which each game fills with its
own tally.

Pure builders (``docs/components_v2_style_guide.md`` § Code conventions):
text and ids in, layout items out, no callbacks and no IO.

- :func:`build_round_layout` — **paradigm 9, game turn card.** Heading with
  Help beside it, the round number and (on a timed round) its countdown,
  the game's own body, the game's own controls, then subtext. The closed
  frame is the round's record: no controls at all. A waiting round (the
  bank had nothing to serve) shows the notice instead of a body.
- :func:`pose_row` — the row under the vote controls: the Pose button, and
  **Next only on a scheduled game**. Everything host-only lives on the host
  panel (Ben, 2026-09-30); Next is host-only on a hosted game, but on a
  scheduled one any voter may press it once it unlocks
  (``round_pacing.voter_may_advance``), so there it stays public.
- :func:`build_host_panel_layout` — **paradigm 2, ephemeral panel.** Every
  game's host card: heading, blurb, the ``/games host`` pointer and the
  buttons the game passes (or Next and End Game, plus a Start button for a
  game with a lobby).
- :func:`build_recap_layout` — **paradigm 7, notice.** Heading, the line
  saying why the game ended, a facts line, the game's sections, the payout.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

from bot_modules.games.utils.host_panel import HOST_COMMAND, HOST_HINT
from bot_modules.games.utils.round_pacing import timer_field_value, waiting_notice
from bot_modules.shared.embeds import take_to_fit

NEXT_LABEL = "⏭️ Next"
END_LABEL = "🏁 End Game"


def text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def line(
    spacing: discord.SeparatorSpacing = discord.SeparatorSpacing.small,
) -> discord.ui.Separator[Any]:
    return discord.ui.Separator(spacing=spacing)


def grey(label: str, custom_id: str) -> discord.ui.Button[Any]:
    """A grey button. Every control in these games is grey: a vote is not a
    decision card's yes/no, and the embed era's blurple A/B, red Guilty and
    green Innocent ranked or coloured choices that carry no such meaning
    (guide § Anatomy)."""
    return discord.ui.Button(
        label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id,
    )


def fit_names(names: Sequence[str], limit: int) -> str:
    """Comma-join already-escaped names inside ``limit``, dropping whole
    trailing names into a ``+N more`` count — never a character slice, which
    could split an escape pair."""
    shown, remaining = take_to_fit(list(names), limit - 12)
    joined = ", ".join(shown)
    if remaining:
        return f"{joined} +{remaining} more" if joined else f"+{remaining} more"
    return joined


def queue_note(count: int, noun: str) -> str | None:
    """``"2 questions queued"`` for the round card's subtext, or ``None``.

    The count used to ride on the Next button's label; Next is off the board
    of a hosted game, so the queue says so itself."""
    if count <= 0:
        return None
    return f"{count} {noun}{'' if count == 1 else 's'} queued"


def pose_row(
    pose_label: str,
    pose_id: str,
    *,
    next_id: str | None = None,
    extra: Sequence[discord.ui.Button[Any]] = (),
) -> discord.ui.ActionRow[Any]:
    """Pose, any ``extra`` buttons, and — when ``next_id`` is given, i.e. on
    a scheduled game — Next."""
    buttons: list[discord.ui.Button[Any]] = [grey(pose_label, pose_id), *extra]
    if next_id:
        buttons.append(grey(NEXT_LABEL, next_id))
    return discord.ui.ActionRow(*buttons)


def build_round_layout(
    *,
    icon: str,
    title: str,
    round_num: int,
    body: str,
    help_id: str,
    controls: Sequence[discord.ui.Item[Any]] = (),
    notes: Sequence[str | None] = (),
    closed: bool = False,
    waiting: tuple[str, str] | None = None,
    advance_at: int | None = None,
    tail: Sequence[str] = (),
    progress: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """One round's card, open or closed. **Paradigm 9 — game turn card.**

    ``body`` is the game's own text (prompt and tally). ``waiting`` is
    ``(pose_label, noun)`` for a round with no prompt yet — the notice
    replaces the body (vote-games-50). ``advance_at`` adds the live
    countdown of a timed round while it runs. ``controls`` are the game's
    action rows and ``notes`` its subtext lines (``None`` entries are
    skipped); the pointer at the host's controls is added here. ``tail`` is
    further body text kept on the closed frame too (Never Have I Ever's
    lives). ``progress`` replaces the ``Round **N**`` line for a game that
    counts something else — Hot Takes' ``Take **3**/5``.

    Closed, the card is the round's record: the heading says Round Over and
    every control — Help included — is gone, so nothing on it can be pressed.
    """
    head = f"## {icon} {title}" + (" — Round Over" if closed else "")
    facts = progress or f"Round **{round_num}**"
    if advance_at and not closed and not waiting:
        facts += f" · ⏱️ {timer_field_value(advance_at)}"
    opening = text(f"{head}\n{facts}")
    items: list[discord.ui.Item[Any]] = [
        opening if closed else discord.ui.Section(
            opening, accessory=grey("❓ Help", help_id),
        ),
        line(),
        text(waiting_notice(*waiting) if waiting else body),
    ]
    items.extend(text(block) for block in tail if block)
    if closed:
        return items
    items.extend(controls)
    subtext = [note for note in notes if note] + [HOST_HINT]
    items.append(text("\n".join(f"-# {note}" for note in subtext)))
    return items


def red(label: str, custom_id: str) -> discord.ui.Button[Any]:
    """A red button — a host panel's one destructive press (Cancel Lobby,
    Cancel Game). Everything else in these games is :func:`grey`."""
    return discord.ui.Button(
        label=label, style=discord.ButtonStyle.danger, custom_id=custom_id,
    )


def fit_lines(lines: list[str], budget: int) -> str:
    """Join lines inside ``budget``, dropping whole trailing lines into an
    ``…and N more`` count — never a character slice, which could split an
    escape pair."""
    shown, remaining = take_to_fit(lines, budget - 20, sep="\n")
    if remaining:
        shown.append(f"_…and {remaining} more_")
    return "\n".join(shown)


def build_host_panel_layout(
    icon: str,
    *,
    blurb: str | None = None,
    rows: Sequence[Sequence[discord.ui.Button[Any]]] | None = None,
    next_id: str | None = None,
    end_id: str | None = None,
    start: tuple[str, str] | None = None,
) -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    Every game's panel is this one card: the heading, an optional ``blurb``
    line saying what the controls do, the pointer back to
    :data:`HOST_COMMAND`, then the buttons. A game lays them out with
    ``rows`` (one list of buttons per action row — build them with
    :func:`grey`, and :func:`red` for the one destructive press), or, for
    the round games, with the shorthand ``next_id`` / ``end_id`` and an
    optional ``start`` (``(label, custom_id)``) for a game with a lobby.
    It carries no game state — the board shows the round, and a copy here
    would go stale on the next vote.
    """
    if rows is None:
        buttons: list[discord.ui.Button[Any]] = []
        if start:
            buttons.append(grey(*start))
        if next_id:
            buttons.append(grey(NEXT_LABEL, next_id))
        if end_id:
            buttons.append(grey(END_LABEL, end_id))
        rows = [buttons]
    head = f"## {icon} Host Controls\n"
    if blurb:
        head += f"{blurb}\n"
    head += f"-# Only you can see this. {HOST_COMMAND} brings it back."
    return [text(head), *(discord.ui.ActionRow(*row) for row in rows)]


def build_recap_layout(
    *,
    icon: str,
    title: str,
    lead: str | None = None,
    facts: Sequence[str] = (),
    sections: Sequence[str] = (),
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The game-over card. **Paradigm 7 — notice.**

    One shape for all three games: the heading, ``lead`` (why the game
    ended, when the card says), a `` · ``-joined ``facts`` line, then each
    of the game's ``sections`` as its own block. ``payout_line``
    (``economy.game_rewards.payout_footer_line``) is the ``-#`` line the
    embeds' ``append_payout_footer`` used to stamp on.
    """
    heading = f"## {icon} {title} — Game Over"
    if lead:
        heading += f"\n{lead}"
    items: list[discord.ui.Item[Any]] = [text(heading)]
    if facts or sections:
        items.append(line())
    if facts:
        items.append(text(" · ".join(facts)))
    items.extend(text(section) for section in sections)
    if payout_line:
        items.append(text(f"-# {payout_line}"))
    return items
