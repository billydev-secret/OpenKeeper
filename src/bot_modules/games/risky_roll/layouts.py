"""Risky Rolls' Components V2 cards (``docs/components_v2_style_guide.md``).

Pure builders, no IO: state and a ``name_fn`` in, a list of layout items
out, buttons carrying the same persistent ``rr:*`` custom ids the embed-era
views used and no callbacks — ``views.py`` binds those. The words come
from ``formatters`` (the roll lines, the result, the auto-close line, the
rules, the prompt), so the card can't word a round differently from the
rest of the game.

Four cards:

- **The round board** (paradigm 8, live board): the public card a round
  lives on, edited on every roll. Open, it carries Roll / Help and one
  ``-#`` line pointing the opener at ``/games host``; Close Round is on the
  ephemeral host panel (:func:`build_host_panel_layout`, Ben, 2026-10-01).
  Closed, it becomes the result with **no** controls — a finished board is
  replaced, never greyed out (the duels' "One card" rule).
- **The host panel** (paradigm 2, ephemeral): Close Round, one per round
  the presser may close.
- **How to Play** (paradigm 7): the Help button's ephemeral rules card.
- **The winner prompt** (paradigm 5, decision card): the public message
  that pings whoever gets to ask, with the Ask Question button; once asked
  it edits into the question summary with the button gone. Its ``<@id>``
  pings are deliberate, so its sends pass :func:`prompt_mentions`, an
  allow-list of exactly those users (guide § Mentions and names).

The round board wears the guild's lobby art (``core.layout.lobby_art``):
the header on top with "Risky Rolls" drawn into it, the footer closing the
card below its buttons. A round runs in an age-gated channel as a rule, and
the art goes there unspoilered — spoilers are for member-submitted media,
not the server's own decoration (style guide § Media). The prompt and the
Help card stay plain: art frames the board, not every message about it.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.games.utils.names import resolved_name

from bot_modules.core.layout import lobby_art
from bot_modules.core.utils import jump_url
from bot_modules.games.utils.host_panel import HOST_COMMAND, HOST_HINT
from bot_modules.shared.name_resolver import NameFn

from .formatters import (
    auto_close_text,
    build_pending_prompt_content,
    how_to_play_parts,
    result_text,
    roll_lines,
)
from .logic import PayoffDials
from .models import PendingQuestionState, PromptKind, RiskyRollState

ICON = "🎲"
TITLE = "Risky Rolls"

ROLL_ID = "rr:roll"
HELP_ID = "rr:help"
#: Close Round's id from when it was on the board. Only the restore-time
#: view still registers it (:func:`legacy_close_row`) so a board posted
#: before the move answers a press instead of dying as "interaction failed".
CLOSE_ID = "rr:close"
HOST_CLOSE_PREFIX = "rr_host_close:"
ASK_ID = "rr:ask"

#: Each prompt kind's glyph-led heading (guide § Card anatomy: every card
#: opens on a ``##`` title led by its glyph).
PROMPT_TITLES: dict[PromptKind, str] = {
    PromptKind.DIRECT: "🥇 Round Won",
    PromptKind.TWO_QUESTIONERS: "☠️ Someone Rolled a 1",
    PromptKind.ROOM: "🔥 Rolled a 69",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


#: Characters the roster may take. A card's text is capped at 4,000 in all,
#: and a round with no player limit can outgrow that — past it Discord
#: refuses the edit and the next roll fails — so the lowest rolls fold into
#: a count, well clear of the heading, the result and the auto-close line.
ROSTER_CHARS = 3000


def _roster(lines: list[str]) -> str:
    """The roll lines, highest first, folding the tail into "…and N more"
    once they pass :data:`ROSTER_CHARS`."""
    shown: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) + 1 > ROSTER_CHARS:
            shown.append(f"-# …and {len(lines) - index} more")
            break
        shown.append(line)
        used += len(line) + 1
    return "\n".join(shown)


def host_close_id(game_id: str) -> str:
    """The host panel's Close Round button for one round (ephemeral id)."""
    return f"{HOST_CLOSE_PREFIX}{game_id}"


def legacy_close_row() -> discord.ui.ActionRow[Any]:
    """A pre-move board's Close Round button, for the restore-time view
    only — never drawn on a board."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Close Round", emoji="🔒", style=discord.ButtonStyle.danger,
            custom_id=CLOSE_ID,
        ),
    )


def board_controls() -> discord.ui.ActionRow[Any]:
    """The open board's Roll / Help row — also what a restored round's
    persistent view registers, so the ids live in one place."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Roll", emoji="🎲", style=discord.ButtonStyle.primary,
            custom_id=ROLL_ID,
        ),
        discord.ui.Button(
            label="Help", emoji="❓", style=discord.ButtonStyle.secondary,
            custom_id=HELP_ID,
        ),
    )


def ask_row() -> discord.ui.ActionRow[Any]:
    """The prompt's Ask Question row (see :func:`board_controls`)."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Ask Question", emoji="💬", style=discord.ButtonStyle.success,
            custom_id=ASK_ID,
        )
    )


def build_round_layout(
    state: RiskyRollState,
    name_fn: NameFn,
    *,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The round board, open or closed. **Paradigm 8 — live board.**

    ``state.ping_role_id`` is the round role a pinging ``/risky start``
    rang: its ``<@&id>`` line heads the open board, because a V2 message has
    no ``content`` to carry it. Send and edit with :func:`board_mentions` so
    the allow-list is exactly that role. ``header_url``/``footer_url``/``title_in_header`` are the guild's lobby
    art (``games/utils/lobby_art.read_lobby_art``): nothing draws without a
    header, and a header with the title drawn in replaces the ``##`` line.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    if state.is_open and state.ping_role_id is not None:
        lines.append(f"<@&{state.ping_role_id}> A new round has begun!")
    lines.append(
        "Highest roll wins, lowest answers. Press **Roll** to join."
        if state.is_open
        else "Round over."
    )
    heading = "\n".join(lines)
    rolls = _roster(roll_lines(state, name_fn)) or "Nobody has rolled yet."
    items: list[discord.ui.Item[Any]] = [
        *top,
        _text(heading),
        _line(),
        _text(f"### Rolls ({len(state.rolls)})\n{rolls}"),
    ]
    result = result_text(state, name_fn)
    if result is not None:
        items += [_line(), _text(f"### Result\n{result}")]
    if not state.is_open:
        return [*items, *bottom]

    auto_close = auto_close_text(state)
    if auto_close:
        items.append(_text(f"-# {auto_close}"))
    items.append(_text(f"-# {HOST_HINT}"))
    items.append(board_controls())
    return [*items, *bottom]


#: Rounds the host panel lists at once: a header (1) plus a Section per round
#: (3 each) must stay inside Discord's 40 components, and an admin sees every
#: round in the channel while ``risky_max_games_per_channel`` has no upper
#: bound (ship review, 2026-10-01). 12 rounds is 37, plus the overflow line.
HOST_PANEL_MAX_ROUNDS = 12


def build_host_panel_layout(
    rounds: list[RiskyRollState], name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """The opener's controls, on an ephemeral card. **Paradigm 2 — panel.**

    One red Close Round per round the presser may close (a channel can hold
    several rounds at once; each line says who opened it, how many have
    rolled and links its board). Red: the one destructive press. Pass only
    rounds the presser passed :func:`logic.may_close_round` for.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} Host Controls\n"
            "Close a round early — the highest roll wins and the lowest answers.\n"
            f"-# Only you can see this. {HOST_COMMAND} brings it back."
        ),
    ]
    shown = rounds[:HOST_PANEL_MAX_ROUNDS]
    for state in shown:
        where = (
            f" · [board]({jump_url(state.guild_id, state.channel_id, state.message_id)})"
            if state.message_id else ""
        )
        items.append(discord.ui.Section(
            _text(
                # Already escaped by the resolver; a miss reads User <id>.
                f"Round by **{resolved_name(name_fn, state.opener_id)}** · "
                f"{len(state.rolls)} rolled{where}"
            ),
            accessory=discord.ui.Button(
                label="Close Round", emoji="🔒", style=discord.ButtonStyle.danger,
                custom_id=host_close_id(state.game_id),
            ),
        ))
    if len(rounds) > len(shown):
        items.append(_text(
            f"-# …and {len(rounds) - len(shown)} more — close one and run "
            f"{HOST_COMMAND} again to see the rest."
        ))
    return items


def board_mentions(state: RiskyRollState) -> discord.AllowedMentions:
    """The board's allow-list: exactly the round role while the board shows
    its line, else nobody. Passed on every edit as well as the send — an
    edit never re-notifies, and the list never widens past what was drawn."""
    if not state.is_open or state.ping_role_id is None:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False, users=False, replied_user=False,
        roles=[discord.Object(id=state.ping_role_id)],
    )


def build_how_to_play_layout(
    dials: PayoffDials | None = None,
) -> list[discord.ui.Item[Any]]:
    """The Help button's rules. **Paradigm 7 — notice**, ephemeral."""
    basics, specials, after = how_to_play_parts(dials)
    return [
        _text(f"## {ICON} How to Play\n" + "\n".join(basics)),
        _line(),
        _text("### Special Rolls\n" + "\n".join(specials)),
        _line(),
        _text("### After the Round\n" + "\n".join(after)),
    ]


def build_prompt_layout(
    state: PendingQuestionState,
    *,
    closed_text: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The winner prompt. **Paradigm 5 — decision card.**

    ``closed_text`` is what a spent prompt is left showing — the question
    summary (``formatters.build_pending_question_summary``) or why it was
    cleared — and takes the button away.
    """
    heading = f"## {PROMPT_TITLES[state.prompt_kind]}"
    if closed_text is not None:
        return [_text(f"{heading}\n{closed_text}")]
    return [_text(f"{heading}\n{build_pending_prompt_content(state)}"), ask_row()]


def prompt_mentions(state: PendingQuestionState) -> discord.AllowedMentions:
    """Exactly the users an open prompt names: whoever may ask, and whoever
    answers. A 69 room prompt names only the winner."""
    ids = state.allowed_questioners()
    if state.prompt_kind != PromptKind.ROOM:
        ids |= set(state.participant_user_ids)
    return discord.AllowedMentions(
        everyone=False, roles=False, replied_user=False,
        users=[discord.Object(id=uid) for uid in sorted(ids)],
    )
