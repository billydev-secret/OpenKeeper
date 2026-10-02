"""Components V2 card builders for the Story Builder cog.

Replaces ``games_story/embeds.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3b, "games"). These
functions accept plain dicts/primitives and return a list of
``discord.ui.Item``. They never call out to Discord — testable with no
network and no mocks of the Bot/Guild API.

Four builders, matching the feature brief's card mapping:

* :func:`build_lobby_layout` — the join lobby. **Paradigm 8 — live board**:
  it is rebuilt and re-edited in place on every Join/Leave/Start press, the
  same shape as a sticky board even though it isn't on the Live Game Boards
  dial.
* :func:`build_story_board_layout` — the turn panel's progress half
  (sentence count, who's writing, turn order). **Paradigm 8 — live board**:
  it is what the sticky-board plumbing re-posts on every restick while a
  turn is open.
* :func:`build_turn_prompt_layout` — the turn panel's action half (the
  Write / Skip / Leave row). **Paradigm 9 — game turn card**: a short
  prompt, then the answer buttons.
* :func:`build_story_recap_layout` — the reveal. **Paradigm 7 — receipt**:
  merges what used to be two embeds (the complete story, the "Who Wrote
  What" attribution) into one card, since Components V2's whole-message
  4,000-char budget is far tighter than the old pair's
  4096-description + N*1024-field allowance. ``story_text`` and
  ``attribution_lines`` are expected pre-trimmed by the caller against the
  smaller per-section budgets in :mod:`bot_modules.games.story.logic`
  (:func:`logic.assemble_story_text`, :func:`logic.trim_attribution_lines`)
  so the combined card stays under the ceiling on the fullest real story.

``build_story_board_layout`` and ``build_turn_prompt_layout`` are combined
into **one** ``StoryTurnView`` message by the cog — the turn loop posts and
retires a single panel per turn (see the cog's module docstring) — so the
board half carries the card's one glyph-led ``##`` title and the prompt half
continues under it with a ``###`` subheading rather than a second ``##``.
Both are still separate builders (and separate sample states) because they
answer different questions: what does the story look like right now, versus
what can the current writer do about it.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.shared.name_resolver import NameFn, mention


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


#: The name a titled lobby header wears (``read_lobby_art``).
LOBBY_TITLE = GAME_NAMES["story"]


def build_lobby_layout(
    host_name: str,
    visibility: str,
    max_sentences: int,
    player_ids: list[int],
    name_fn: NameFn = mention,
    start_at: int | None = None,
    *,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Build the ``/games play story`` join-lobby card. **Paradigm 8 — live
    board.**

    Players are named through ``name_fn`` — never a raw ``<@id>``, which
    renders as digits to any client that hasn't cached that member. The
    whole card is rebuilt and re-edited on every Join/Leave (the cog's
    ``StoryJoinView._rebuild``), the same live-in-place-edit shape a sticky
    board uses even though the lobby is not itself on the Live Game Boards
    dial. ``start_at`` is an optional UTC epoch rendered as a live Discord
    relative timestamp — advertising the host's chosen start time, not
    automation; the host still presses Start Story. Names go through
    ``discord.utils.escape_markdown`` (the same as the board/prompt
    builders below) — a display name carrying ``*``/``_``/`` ` ``/``~``
    would otherwise break the Writers list or Host line.

    ``header_url``/``footer_url``/``title_in_header`` are the guild's lobby
    art (``core.layout.lobby_art``): the header tops the card and the footer
    closes it below the buttons, only when a header is set. A titled header
    already says :data:`LOBBY_TITLE`, so the ``##`` heading goes.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=LOBBY_TITLE if titled else None)
    esc = discord.utils.escape_markdown
    names = [esc(name_fn(int(uid))) for uid in player_ids]
    lines = [] if titled else [f"## {GAME_ICONS['story']} {LOBBY_TITLE}"]
    lines.append("Join to contribute to the story!")
    items: list[discord.ui.Item[Any]] = [*top, _text("\n".join(lines))]
    items.append(_text(f"**Writers ({len(names)})**\n" + (", ".join(names) or "—")))
    mode_line = f"**Host:** {esc(host_name)}\n**Mode:** {visibility} | {max_sentences} sentences"
    if start_at:
        mode_line += f"\n**⏰ Starting** <t:{start_at}:R>"
    items.append(_text(mode_line))

    join_button: discord.ui.Button[Any] = discord.ui.Button(
        label="Join", style=discord.ButtonStyle.success, custom_id="story_join",
    )
    leave_button: discord.ui.Button[Any] = discord.ui.Button(
        label="Leave", style=discord.ButtonStyle.secondary, custom_id="story_leave",
    )
    start_button: discord.ui.Button[Any] = discord.ui.Button(
        label="Start Story", style=discord.ButtonStyle.primary, custom_id="story_start",
    )
    help_button: discord.ui.Button[Any] = discord.ui.Button(
        label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="story_htp",
    )
    items.append(discord.ui.ActionRow(join_button, leave_button, start_button, help_button))
    items.extend(bottom)
    return items


def build_story_board_layout(
    sentence_count: int,
    max_sentences: int,
    current_player_id: int,
    turn_order: list[int],
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """Build the turn panel's progress half. **Paradigm 8 — live board.**

    Rebuilt by ``StoryTurnView._rebuild`` on every restick while a turn is
    open — the card doesn't change mid-turn, only what has to stay
    reachable (the buttons in :func:`build_turn_prompt_layout`) does. All
    rendered names go through ``discord.utils.escape_markdown`` (via
    ``name_fn``'s caller) — a writer whose nickname carries markdown can't
    break the card.
    """
    esc = discord.utils.escape_markdown
    current_name = esc(name_fn(current_player_id))
    lines = [
        f"## {GAME_ICONS['story']} Story in Progress",
        f"**Sentence:** {sentence_count + 1}/{max_sentences}",
        f"**Currently Writing:** {current_name} ✍️",
    ]
    items: list[discord.ui.Item[Any]] = [_text("\n".join(lines))]

    order_lines: list[str] = []
    for pid in turn_order:
        name = esc(name_fn(pid))
        if pid == current_player_id:
            order_lines.append(f"**▸ {name}** ✍️")
        else:
            order_lines.append(f"   {name}")
    items.append(_text("**Turn Order**\n" + "\n".join(order_lines)))
    return items


def build_turn_prompt_layout(
    current_player_id: int,
    name_fn: NameFn = mention,
) -> list[discord.ui.Item[Any]]:
    """Build the turn panel's action half. **Paradigm 9 — game turn card.**

    A short prompt, then the answer buttons — Write / Skip / Leave, in one
    ``ActionRow`` under the 5-button ceiling. The buttons carry their
    persistent ``custom_id``s (``story_write`` / ``story_skip`` /
    ``story_leave_turn``, frozen) and no callback: the cog wires the
    callbacks after ``add_item`` (guide § Code conventions).
    """
    name = discord.utils.escape_markdown(name_fn(current_player_id))
    items: list[discord.ui.Item[Any]] = [
        _text(f"### ✍️ {name}'s Turn")
    ]
    write_button: discord.ui.Button[Any] = discord.ui.Button(
        label="✍️ Write Your Sentence",
        style=discord.ButtonStyle.primary,
        custom_id="story_write",
    )
    skip_button: discord.ui.Button[Any] = discord.ui.Button(
        label="⏭️ Skip",
        style=discord.ButtonStyle.secondary,
        custom_id="story_skip",
    )
    leave_button: discord.ui.Button[Any] = discord.ui.Button(
        label="🚪 Leave",
        style=discord.ButtonStyle.secondary,
        custom_id="story_leave_turn",
    )
    items.append(discord.ui.ActionRow(write_button, skip_button, leave_button))
    return items


def build_story_recap_layout(
    story_text: str,
    player_count: int,
    sentence_count: int,
    attribution_lines: list[str],
    omitted: int = 0,
    footer_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build the ``/games play story`` reveal card. **Paradigm 7 — receipt.**

    Merges the old two-embed reveal (the complete story, then a separate
    "Who Wrote What" attribution embed) into one card. ``story_text`` and
    ``attribution_lines`` must already be trimmed by the caller
    (:func:`logic.assemble_story_text`, :func:`logic.trim_attribution_lines`)
    against the module's smaller per-section budgets — this builder does no
    trimming of its own, so it can be unit-tested against exactly what a
    caller decided to keep. ``omitted`` is how many attribution lines the
    caller dropped to make room; when nonzero it renders as a one-line note
    so the count reads as partial rather than complete. ``footer_line`` is
    the plain-text payout summary (``economy.game_rewards.
    payout_footer_line``), rendered as the card's ``-#`` footer.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {GAME_ICONS['story']} The Complete Story\n*{story_text}*")
    ]
    items.append(
        _text(f"**A Community Original**\n{player_count} writers | {sentence_count} sentences")
    )
    if attribution_lines:
        attribution = "**Who Wrote What**\n" + "\n".join(attribution_lines)
        if omitted:
            attribution += f"\n*+{omitted} more not shown above.*"
        items.append(_text(attribution))
    if footer_line:
        items.append(_text(f"-# {footer_line}"))
    return items
