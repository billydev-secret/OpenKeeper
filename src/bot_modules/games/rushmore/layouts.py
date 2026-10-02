"""Mt. Rushmore Draft's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_rushmore/embeds.py`` (deleted with this
conversion). Pure builders, no IO: primitives and pre-resolved names in, a
list of layout items out, buttons and the vote dropdown carrying the same
``rushmore_*`` custom ids the embed-era views used and no callbacks —
``games/rushmore/cog.py`` binds those.

Seven cards:

- **The lobby** (paradigm 8, live board): host, topic tease, the how-to,
  the roster, Join and Leave, with Help beside the opening text. It wears
  the small lobby strip (``core.layout.lobby_art``) like every game lobby.
  **Start Draft is not on it** — everything host-only lives on the host
  panel (Ben, 2026-09-30, extending Truth or Dare's 2026-09-29 rule to the
  party games), and one ``-#`` line points the host at ``/games host``.
- **The host panel** (paradigm 2, ephemeral): Start Draft, the lobby's one
  host-only control. The draft itself has none.
- **The draft board** (paradigm 8, live board): the round and its countdown,
  who is picking, the monospace board, and Make Your Pick. It edits over the
  lobby (or follows the chat under the Live Game Boards dial); the closed
  frame drops the button and the countdown.
- **The final boards** (paradigm 7, notice): every drafter's four picks.
  Twenty-five boards of hundred-character picks outgrow one card's 4,000
  characters, so the builder returns **pages** — whole boards only, never a
  cut one — and the cog posts each as its own message. The embed showed one
  inline field per player and nothing capped it.
- **The vote** (paradigm 9, game turn card): the question, a live countdown
  on a stable-id Text Display (``games/utils/timer.GameTimer`` reads it
  back), and the dropdown of boards.
- **The winner** (paradigm 7, notice): edits over the vote card. The embed
  was semantic green (2026-07-21 ruling); a layout carries no colour, so the
  🏆 line says it.
- **The recap** (paradigm 7, notice): topic, players, duration, the winner's
  board, the draft stats, the payout line, and 🔁 Run Again.

Every member-written string — the topic, a pick, a display name — is
escaped here and fitted *after* escaping (``escape_to_fit``): a Text Display
renders markdown, and escaping can double a string's length.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import code_safe, escape_to_fit, trim_escaped
from bot_modules.core.layout import MAX_CHARS, lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.utils.timer import format_deadline
from bot_modules.games.rushmore.logic import DRAFT_ROUNDS, SKIPPED_MARKER
from bot_modules.shared.embeds import take_to_fit

ICON = GAME_ICONS["rushmore"]
#: The game's name — the lobby's heading, and what a titled lobby header
#: wears between its leaves (``lobby_art.read_lobby_art``).
TITLE = GAME_NAMES["rushmore"]

JOIN_ID = "rushmore_join"
LEAVE_ID = "rushmore_leave"
HELP_ID = "rushmore_htp"
PICK_ID = "rushmore_pick"
VOTE_SELECT_ID = "rushmore_vote_select"
RUN_AGAIN_ID = "rushmore_run_again"
#: The host panel's id — a new one, not the lobby's old ``rushmore_start``:
#: the panel is an ephemeral message with its own short-lived view, never
#: the lobby's persistent one.
START_ID = "rushmore_host_start"

#: Stable numeric id of the vote card's countdown line, so ``GameTimer`` can
#: find it again through ``LayoutView.from_message`` (guide § Code
#: conventions: stable ids on anything a later edit reads back).
VOTE_TIMER_ITEM_ID = 7101

PICK_LABEL = f"{ICON} Make Your Pick"

#: The most one topic may spend of a card, escaped. The modal caps a topic
#: at 100 characters, but ``/games play rushmore topic:`` takes whatever
#: Discord allows, and every card after the lobby leads with it.
TOPIC_LIMIT = 200

#: The most the lobby's roster line may spend. ``MAX_PLAYERS`` is 25 and a
#: 32-character name of pure markdown escapes to 64, so a full adversarial
#: lobby reaches 1,648; whole names past the budget are counted, not cut.
ROSTER_BUDGET = 1500

#: Room a final-boards page or a winner's block keeps clear of the ceiling
#: for its own heading and tail.
_PAGE_HEADROOM = 400


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _topic(topic: str) -> str:
    return escape_to_fit(topic, TOPIC_LIMIT)


def _votes(count: int) -> str:
    return f"{count} vote{'s' if count != 1 else ''}"


def render_draft_board(
    players: list[tuple[int, str]],
    boards: dict[str, list[Any]],
    active_player_id: int | None,
) -> str:
    """Render the multi-line monospaced "Draft Board" string.

    Each player gets one row: their (left-padded) name followed by their
    4 picks, with the active picker marked by a target emoji. Names are
    capped at 16 chars and pick text at 18 chars (with ``...``) so the
    board stays readable on mobile.

    The name sits inside a `` `code span` ``, where Discord renders no
    markdown — a bare backtick in it would close the span early and put
    the rest of the row's picks back outside it, so it goes through
    ``code_safe`` (neutralise the delimiter) rather than
    ``escape_markdown`` (which would just print literal backslashes).
    Picks render outside the span, so they get the ordinary
    escape-then-truncate treatment.
    """
    max_name = max((len(n) for _, n in players), default=6)
    max_name = min(max_name, 16)

    lines: list[str] = []
    for uid, name in players:
        name = code_safe(name)
        board = boards.get(str(uid), [None] * DRAFT_ROUNDS)
        picks: list[str] = []
        for i, pick in enumerate(board):
            if pick is None:
                picks.append(f"{i+1}. —")
            elif pick == SKIPPED_MARKER:
                picks.append(f"{i+1}. *Skipped*")
            else:
                esc_pick = discord.utils.escape_markdown(pick)
                # ``trim_escaped`` rather than a bare slice: cutting escaped
                # text at a fixed index can land between a backslash and the
                # character it protects, and the orphan then escapes the
                # ellipsis instead — a stray ``\`` on the public board that
                # nobody typed. The 18/16 split is the column width this
                # board has always used, unchanged.
                display = (
                    esc_pick if len(esc_pick) <= 18 else trim_escaped(esc_pick, 16)
                )
                picks.append(f"{i+1}. {display}")

        truncated_name = (
            name if len(name) <= max_name else name[: max_name - 1] + "."
        )
        line = f"`{truncated_name:<{max_name}}` {' | '.join(picks)}"
        if uid == active_player_id:
            line += "  ← \U0001f3af"
        lines.append(line)

    return "**Draft Board:**\n" + "\n".join(lines)


# ── the lobby ────────────────────────────────────────────────────────────────


def help_button() -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label="❓ Help", style=discord.ButtonStyle.secondary, custom_id=HELP_ID,
    )


def build_lobby_action_row() -> discord.ui.ActionRow[Any]:
    """Join and Leave — what any member may press. Grey, both: neither is a
    decision and neither outranks the other (guide § Anatomy)."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Join", style=discord.ButtonStyle.secondary, custom_id=JOIN_ID,
        ),
        discord.ui.Button(
            label="Leave", style=discord.ButtonStyle.secondary, custom_id=LEAVE_ID,
        ),
    )


HOW_IT_WORKS_BLITZ = (
    "⚡ **How it works:** each round, everyone picks at once — hit "
    f"{ICON} **Make Your Pick** and type one. Top 4, no duplicates "
    "(fastest fingers win), then the room votes on the best board."
)
HOW_IT_WORKS_SNAKE = (
    f"**How it works:** when it's your turn, hit {ICON} **Make Your "
    "Pick** and type one. Snake draft, top 4, no duplicates, then "
    "the room votes on the best board."
)


def _fit_roster(player_names: list[str], limit: int = ROSTER_BUDGET) -> str:
    """Escape and comma-join the roster, dropping whole trailing names past
    ``limit`` and counting them — never a character slice, which could split
    an escape pair (``take_to_fit``)."""
    if not player_names:
        return "-# Nobody yet"
    escaped = [discord.utils.escape_markdown(n) for n in player_names]
    shown, remaining = take_to_fit(escaped, limit - 16)
    joined = ", ".join(shown)
    if remaining:
        tail = f"(+{remaining} more)"
        return f"{joined} {tail}" if joined else tail
    return joined


def build_lobby_layout(
    host_name: str,
    player_names: list[str],
    topic: str | None = None,
    mode: str = "snake",
    start_at: int | None = None,
    *,
    closed: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The lobby shown while players are joining. **Paradigm 8 — live board.**

    When ``topic`` is set (host-supplied), a line teases the upcoming draft
    topic. The how-to always renders — the ❓ Help button exists but live
    games showed nobody presses it, so the rules ride on the card itself.
    ``mode`` flips that line between snake ("when it's your turn") and blitz
    ("everyone picks at once").

    ``start_at`` is an optional UTC epoch shown as a live Discord relative
    timestamp — the host's advertised start time. The host still presses
    Start Draft (on the host panel); the countdown is advertising, not
    automation.

    ``closed`` is the frame left behind once the draft has started and the
    board could not take this message over: no Join, no Leave.

    ``header_url``/``footer_url``/``title_in_header`` are the guild's lobby
    art (``games/utils/lobby_art.read_lobby_art``): no header draws nothing,
    and a header with the title drawn in replaces the ``##`` line.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    lines.append(f"Hosted by **{discord.utils.escape_markdown(host_name)}**")
    if start_at:
        lines.append(f"⏰ Starting <t:{start_at}:R>")
    body: list[str] = []
    if topic:
        body.append(f"Build your Mt. Rushmore of **{_topic(topic)}**!")
    body.append(HOW_IT_WORKS_BLITZ if mode == "blitz" else HOW_IT_WORKS_SNAKE)
    body.append(f"-# {HOST_HINT}")
    items: list[discord.ui.Item[Any]] = [
        *top,
        # Help sits beside the card's opening text, where a hub's sits
        # beside its title (guide § Controls).
        discord.ui.Section(_text("\n".join(lines)), accessory=help_button()),
        _text("\n".join(body)),
        round_layouts.line(spacing=discord.SeparatorSpacing.large),
        _text(f"### Players ({len(player_names)})\n{_fit_roster(player_names)}"),
    ]
    if not closed:
        items.append(build_lobby_action_row())
    return [*items, *bottom]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    One button: Start Draft, the only thing in a Rushmore game that only
    the host (or a mod) may press. It carries no lobby state — the lobby
    shows the roster, and a copy here would go stale on the next Join.
    """
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb="Start the draft once everyone is in.",
        rows=[[round_layouts.grey("Start Draft", START_ID)]],
    )


# ── the draft board ──────────────────────────────────────────────────────────


def build_draft_layout(
    topic: str,
    players: list[tuple[int, str]],
    boards: dict[str, list[Any]],
    active_player_id: int | None,
    active_player_name: str | None,
    round_num: int,
    deadline: int,
    *,
    closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The live draft board. **Paradigm 8 — live board.**

    ``deadline`` is the Unix timestamp the round's countdown ends at. The
    caller computes it once (``now_plus(timer_secs)``) when a round or turn
    opens and passes the *same* value into every redraw: computing it per
    render reset the visible countdown whenever a blitz pick redrew the
    board mid-round (todo #208/#209).

    The "now picking" line is dropped when there's no active player (blitz,
    and the closed frame). ``closed`` is the draft-complete frame: no
    countdown and no Make Your Pick, since there is nothing left to pick.
    """
    head = [f"## {ICON} Mt. Rushmore of: {_topic(topic)}"]
    if closed:
        head.append("Draft complete — the boards are in.")
    else:
        head.append(f"Round {round_num}/{DRAFT_ROUNDS} · {format_deadline(deadline)}")
        if active_player_name:
            head.append(
                "\U0001f3af "
                f"**{discord.utils.escape_markdown(active_player_name)}**'s turn!"
            )
    items: list[discord.ui.Item[Any]] = [
        _text("\n".join(head)),
        round_layouts.line(),
        _text(render_draft_board(players, boards, active_player_id)),
    ]
    if not closed:
        items.append(discord.ui.ActionRow(
            discord.ui.Button(
                label=PICK_LABEL, style=discord.ButtonStyle.secondary, custom_id=PICK_ID,
            ),
        ))
    return items


def build_interrupted_layout() -> list[discord.ui.Item[Any]]:
    """What a board becomes when a restart ends its draft. **Paradigm 7 —
    notice.** A draft or a vote runs in a loop that can't be re-driven, so
    recovery ends the game; the board it leaves must not keep a button."""
    return [_text(
        f"## {ICON} {TITLE} — Interrupted\n"
        "A bot restart ended this draft before it finished."
    )]


# ── final boards ─────────────────────────────────────────────────────────────


def _board_lines(board: list[Any], *, indent: str = "") -> list[str]:
    lines: list[str] = []
    for i in range(DRAFT_ROUNDS):
        pick = board[i] if i < len(board) else None
        if pick is None:
            lines.append(f"{indent}{i+1}. —")
        elif pick == SKIPPED_MARKER:
            lines.append(f"{indent}{i+1}. *Skipped*")
        else:
            lines.append(f"{indent}{i+1}. {discord.utils.escape_markdown(pick)}")
    return lines


def build_final_boards_pages(
    topic: str,
    players: list[tuple[int, str]],
    boards: dict[str, list[Any]],
) -> list[list[discord.ui.Item[Any]]]:
    """Every drafter's finished board, as one card per page. **Paradigm 7 —
    notice.**

    Returns a list of cards because the worst case does not fit one: 25
    players of four 100-character picks run past 10,000 characters escaped,
    against a 4,000-character card. Boards are packed whole — a page ends
    before the board that would overflow it — so nothing is ever cut, and an
    ordinary game is one page. Later pages say so in their heading.
    """
    blocks = [
        "\n".join([
            f"**{discord.utils.escape_markdown(name)}**",
            *_board_lines(boards.get(str(uid), [None] * DRAFT_ROUNDS)),
        ])
        for uid, name in players
    ]
    title = f"## {ICON} Mt. Rushmore of: {_topic(topic)} — Final Boards"
    if not blocks:
        return [[_text(f"{title}\nNobody finished a board.")]]
    budget = MAX_CHARS - _PAGE_HEADROOM
    pages: list[list[str]] = []
    remaining = blocks
    while remaining:
        shown, _ = take_to_fit(remaining, budget, sep="\n\n")
        if not shown:
            # One board over the budget on its own — unreachable while a
            # pick is capped at 100 characters, but never loop forever.
            shown = [trim_escaped(remaining[0], budget)]
        pages.append(shown)
        remaining = remaining[len(shown):]
    cards: list[list[discord.ui.Item[Any]]] = []
    for number, page in enumerate(pages, start=1):
        heading = title if len(pages) == 1 else f"{title} ({number}/{len(pages)})"
        cards.append([_text(heading), round_layouts.line(), _text("\n\n".join(page))])
    return cards


# ── the vote, the winner ─────────────────────────────────────────────────────

VOTE_BLURB = (
    "Who built the best Mt. Rushmore? **Anyone in the channel can vote** — "
    "not just the drafters (you just can't vote for yourself)."
)


def build_vote_layout(
    topic: str,
    deadline: int,
    options: list[tuple[str, str]],
) -> list[discord.ui.Item[Any]]:
    """The voting card. **Paradigm 9 — game turn card.**

    ``options`` is ``(label, value)`` per eligible board, already cut to the
    25 a dropdown holds; a label renders no markdown, so it is fitted, not
    escaped. The countdown is its own Text Display with a stable id so
    ``GameTimer`` can find it on the posted message.
    """
    select: discord.ui.Select[Any] = discord.ui.Select(
        placeholder="\U0001f5f3️ Pick the winner…",
        options=[
            discord.SelectOption(label=trim_escaped(label, 100), value=value)
            for label, value in options[:25]
        ],
        custom_id=VOTE_SELECT_ID,
    )
    return [
        _text(f"## {ICON} Vote — Best Mt. Rushmore of {_topic(topic)}\n{VOTE_BLURB}"),
        discord.ui.TextDisplay(format_deadline(deadline), id=VOTE_TIMER_ITEM_ID),
        discord.ui.ActionRow(select),
    ]


def _winner_block(
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list[Any]],
    *,
    budget: int,
) -> str:
    """The 🏆 line and the winning board(s), fitted to ``budget``: tied
    winners stack their boards, and a tie wide enough to overflow drops
    whole trailing boards into a count."""
    label = " & ".join(discord.utils.escape_markdown(n) for n in winner_names)
    headline = f"\U0001f3c6 **{label}** wins! — {_votes(winner_votes)}"
    if len(headline) > budget // 2:
        headline = f"\U0001f3c6 **{len(winner_names)} drafters** tie! — {_votes(winner_votes)}"
    board_blocks = ["\n".join(_board_lines(board)) for board in winner_boards]
    shown, remaining = take_to_fit(board_blocks, budget - len(headline) - 40, sep="\n\n")
    parts = [headline, "\n\n".join(shown)] if shown else [headline]
    if remaining:
        parts.append(f"_…and {remaining} more winning board{'s' if remaining != 1 else ''}_")
    return "\n".join(parts)


def build_winner_layout(
    topic: str,
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list[Any]],
    all_results: list[tuple[str, int]],
) -> list[discord.ui.Item[Any]]:
    """The winner reveal, edited over the vote card. **Paradigm 7 — notice.**

    Handles ties by joining ``winner_names`` with ``&`` and stacking each
    winner's board. ``all_results`` is the sorted ``(name, votes)`` list the
    cog hands in.
    """
    results = [
        f"**{discord.utils.escape_markdown(name)}** — {_votes(votes)}"
        for name, votes in all_results
    ]
    shown, remaining = take_to_fit(results, 1800, sep="\n")
    if remaining:
        shown.append(f"_…and {remaining} more_")
    return [
        _text(f"## {ICON} Winner — Mt. Rushmore of {_topic(topic)}"),
        _text(_winner_block(winner_names, winner_votes, winner_boards, budget=1500)),
        round_layouts.line(),
        _text("### Full Results\n" + ("\n".join(shown) or "—")),
    ]


# ── the recap ────────────────────────────────────────────────────────────────

RUN_AGAIN_LABEL = "\U0001f501 Run Again"


def run_again_row(*, disabled: bool = False) -> discord.ui.ActionRow[Any]:
    return discord.ui.ActionRow(
        discord.ui.Button(
            label=RUN_AGAIN_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=RUN_AGAIN_ID,
            disabled=disabled,
        ),
    )


def _stat_lines(stats: dict[str, Any], winner_label: str) -> list[str]:
    lines: list[str] = []
    if stats.get("first_pick"):
        fp = stats["first_pick"]
        lines.append(
            "\U0001f947 First Overall Pick: "
            f"**{escape_to_fit(fp['pick'], 200)}** "
            f"({discord.utils.escape_markdown(fp['player'])}, Round 1)"
        )
    if stats.get("skipped_count") is not None:
        names = [discord.utils.escape_markdown(n) for n in stats.get("skipped_names", [])]
        shown, remaining = take_to_fit(names, 400)
        if remaining:
            shown.append(f"+{remaining} more")
        extra = f" ({', '.join(shown)})" if shown else ""
        lines.append(f"⏭️ Skipped Picks: **{stats['skipped_count']}**{extra}")
    for key, glyph, label in (
        ("fastest", "⚡", "Fastest Pick"),
        ("slowest", "\U0001f422", "Slowest Pick"),
    ):
        if stats.get(key):
            s = stats[key]
            lines.append(
                f"{glyph} {label}: **{escape_to_fit(s['pick'], 200)}** by "
                f"{discord.utils.escape_markdown(s['player'])} ({s['time']:.1f}s)"
            )
    if stats.get("unanimous"):
        lines.append(
            f"\U0001f3af Unanimous Vote: Yes — everyone voted for **{winner_label}**"
        )
    elif stats.get("vote_split"):
        lines.append(f"\U0001f3af Vote: {stats['vote_split']}-way split")
    return lines


def build_recap_layout(
    topic: str,
    player_count: int,
    duration_secs: float,
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list[Any]],
    stats: dict[str, Any],
    *,
    payout_line: str | None = None,
    run_again: bool = True,
    run_again_disabled: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The game-over recap. **Paradigm 7 — notice**, with Run Again.

    ``stats`` must come from
    :func:`bot_modules.games.rushmore.logic.compute_recap_stats`; any key
    listed in that function's docstring may be present (or absent). Absent
    keys collapse silently so a game with no completed picks still renders
    cleanly, and a game nobody won says so instead of crowning an empty name.

    ``payout_line`` (``economy.game_rewards.payout_footer_line``) is the
    ``-#`` line the embed's ``append_payout_footer`` used to stamp on.
    """
    mins = int(duration_secs // 60)
    secs = int(duration_secs % 60)
    facts = " · ".join([
        f"Topic: **{_topic(topic)}**",
        f"Players: **{player_count}**",
        f"Draft: **{mins}m {secs}s**",
    ])
    winner_label = " & ".join(discord.utils.escape_markdown(n) for n in winner_names)
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} {TITLE} — Game Over\n{facts}"),
        round_layouts.line(),
        _text(
            _winner_block(winner_names, winner_votes, winner_boards, budget=1500)
            if winner_names
            else "Nobody won this one."
        ),
    ]
    stat_lines = _stat_lines(stats, trim_escaped(winner_label, 300))
    if stat_lines:
        items.append(_text("### \U0001f4ca Draft Stats\n" + "\n".join(stat_lines)))
    if payout_line:
        items.append(_text(f"-# {payout_line}"))
    if run_again:
        items.append(run_again_row(disabled=run_again_disabled))
    return items
