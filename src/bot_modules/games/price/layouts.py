"""Name Your Price's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_price/embeds.py`` (deleted with this
conversion). Pure builders: primitives and pre-resolved names in, layout
items out, buttons and dropdowns carrying the ``price_*`` custom ids the
embed-era views used and no callbacks — ``games/price/cog.py`` binds
those.

The cards, in the order a game shows them:

- **The lobby** (paradigm 8, live board): host, rounds, where scenarios
  come from, the advertised start, the roster; grey **Join** / **Leave**
  with Help beside the title, and the small lobby strip. **Start and
  Cancel are not on it** — everything host-only lives on the ephemeral
  host panel (Ben, 2026-09-30), and one ``-#`` line points the host at
  ``/games host``.
- **Starting up**, **the scenario wait** (paradigm 9): the board between
  the lobby and a round's first price. The wait carries **📝 Write
  Scenario** only when anyone may write one (the ``players`` source); the
  host's Write Scenario and 🎲 Draw From the Bank are on the panel.
- **The submission board** (paradigm 8, live board): the scenario, a
  countdown on a stable-id Text Display (``GameTimer`` reads it back), the
  live count, **💵 Name Your Price**.
- **The reveal** (paradigm 7): the price ladder and its stats, fitted by
  whole lines — anyone in the channel may price, so it has no cap.
- **The vote** (paradigm 9): the countdown and the two dropdowns.
- **Round results** (paradigm 7): the two winners. The embed's semantic
  green is gone with every other colour; 🎯 and 🤯 carry it.
- **The recap** (paradigm 7): rounds, players, awards, highlight, payout,
  and 🔁 Run Again.
- **The host panel** (paradigm 2, ephemeral): every host-only control of
  the whole game on one card — Start, Cancel Lobby, Write Scenario, Draw
  From the Bank, Skip, Add Rounds, End Game. A press out of its phase
  answers rather than acting: the game moves between phases on its own,
  so a panel that showed only the current phase's buttons would be stale
  a minute after it was opened.
- **Notices** (paradigm 7): the cancelled lobby and the restart pick-up,
  which the embed era wrote into ``content=``.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_COMMAND, HOST_HINT
from bot_modules.games.utils.timer import format_deadline
from bot_modules.games.price.logic import format_price, ladder_stats, price_label
from bot_modules.shared.embeds import take_to_fit

ICON = GAME_ICONS["price"]
TITLE = GAME_NAMES["price"]

JOIN_ID = "price_join"
LEAVE_ID = "price_leave"
LOBBY_HELP_ID = "price_lobby_htp"
WRITE_ID = "price_write_scenario"
SUBMIT_ID = "price_submit"
HELP_ID = "price_htp"
REASONABLE_ID = "price_vote_reasonable"
UNHINGED_ID = "price_vote_unhinged"
RUN_AGAIN_ID = "price_run_again"
#: The host panel's ids — new ones, not the boards' old ``price_start`` /
#: ``price_cancel`` / ``price_skip`` / ``price_add_rounds`` / ``price_end``
#: / ``price_draw_bank``: the panel is an ephemeral message with its own
#: short-lived view.
HOST_START_ID = "price_host_start"
HOST_CANCEL_ID = "price_host_cancel"
HOST_WRITE_ID = "price_host_write"
HOST_DRAW_ID = "price_host_draw"
HOST_SKIP_ID = "price_host_skip"
HOST_ADD_ROUNDS_ID = "price_host_add_rounds"
HOST_END_ID = "price_host_end"

#: Stable numeric ids of the two countdown lines, so ``GameTimer`` can find
#: them again through ``LayoutView.from_message``.
SUBMIT_TIMER_ITEM_ID = 7201
VOTE_TIMER_ITEM_ID = 7202

WRITE_LABEL = "📝 Write Scenario"
SUBMIT_LABEL = "💵 Name Your Price"

#: The most a scenario may spend of a card, escaped. The modal caps it at
#: 500 characters, and every card of the round leads with it.
SCENARIO_LIMIT = 1000

#: The most the roster, the ladder and the results' winner lists may
#: spend. Anyone in the channel may join or price, so none has a cap; each
#: is fitted by whole lines, which the embed fields never were.
ROSTER_BUDGET = 1500
LADDER_BUDGET = 2600
NAMES_BUDGET = 600

# What the lobby says about where scenarios will come from.
SOURCE_LABELS: dict[str, str] = {
    "bank": "the question bank",
    "host": "the host writes them",
    "players": "anyone can write one — first in wins",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _round(round_num: int, total_rounds: int) -> str:
    return f"Round {round_num}/{total_rounds}"


def _scenario(scenario: str) -> str:
    return escape_to_fit(scenario, SCENARIO_LIMIT)


def join_names(names: list[str], *, budget: int = NAMES_BUDGET) -> str:
    """``A & B & C`` of escaped names, fitted by whole names."""
    escaped = [discord.utils.escape_markdown(n) for n in names]
    shown, remaining = take_to_fit(escaped, budget - 16, sep=" & ")
    joined = " & ".join(shown)
    return f"{joined} & {remaining} more" if remaining else joined


# ── the lobby ────────────────────────────────────────────────────────────────

LOBBY_BLURB = (
    "Press **Join** to play. Each round a scenario is posed and everyone "
    "who joined names their price in secret — the round closes as soon as "
    "the whole room has answered."
)


def build_lobby_layout(
    host_name: str,
    players: list[str],
    rounds: int,
    source: str,
    *,
    start_at: int | None = None,
    min_players: int = 2,
    closed: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The join lobby posted by ``/games play price``. **Paradigm 8 — live
    board.**

    ``players`` are pre-resolved display names — escaped here and fitted
    by whole names. ``start_at`` is the host's advertised start
    (``start_in``), rendered as a live relative timestamp; at that moment
    the game starts itself once ``min_players`` have joined (the start-ping
    sweep calls the cog's auto-starter), so the line says so. ``closed`` is
    the frame left once the game has started: no Join, no Leave.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    lines.append(f"Hosted by **{discord.utils.escape_markdown(host_name)}**")
    facts = [f"Rounds: **{rounds}**", f"Scenarios: {SOURCE_LABELS.get(source, source)}"]
    if start_at:
        facts.append(f"⏰ Starting <t:{start_at}:R> — on its own, once {min_players} have joined")
    roster = round_layouts.fit_lines([discord.utils.escape_markdown(n) for n in players], ROSTER_BUDGET) if players else "-# Nobody yet"
    items: list[discord.ui.Item[Any]] = [
        *top,
        discord.ui.Section(_text("\n".join(lines)), accessory=round_layouts.grey("❓ Help", LOBBY_HELP_ID)),
        _text(f"{LOBBY_BLURB}\n{' · '.join(facts[:2])}\n" + (facts[2] + "\n" if len(facts) > 2 else "") + f"-# {HOST_HINT}"),
        round_layouts.line(spacing=discord.SeparatorSpacing.large),
        _text(f"### Players ({len(players)})\n{roster}"),
    ]
    if not closed:
        items.append(discord.ui.ActionRow(round_layouts.grey("Join", JOIN_ID), round_layouts.grey("Leave", LEAVE_ID)))
    return [*items, *bottom]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    Every host-only control of the game on one card, since the game moves
    between phases on its own; a press out of its phase answers plainly.
    Cancel Lobby is red — the one destructive press.
    """
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb="Start the lobby, write or draw a round's scenario, move a round along, or end the game.",
        rows=[
            [round_layouts.grey("Start", HOST_START_ID), round_layouts.red("Cancel Lobby", HOST_CANCEL_ID)],
            [round_layouts.grey(WRITE_LABEL, HOST_WRITE_ID), round_layouts.grey("🎲 Draw From the Bank", HOST_DRAW_ID)],
            [
                round_layouts.grey("⏭️ Skip", HOST_SKIP_ID),
                round_layouts.grey("➕ Add Rounds", HOST_ADD_ROUNDS_ID),
                round_layouts.grey("End Game", HOST_END_ID),
            ],
        ],
    )


# ── between the lobby and a price ────────────────────────────────────────────


def build_start_layout(host_name: str, round_num: int, total_rounds: int) -> list[discord.ui.Item[Any]]:
    """The "starting up" frame that holds the message slot while the first
    scenario is fetched. **Paradigm 7 — notice.**"""
    return [_text(
        f"## {ICON} {TITLE}\n"
        f"Hosted by **{discord.utils.escape_markdown(host_name)}** · {_round(round_num, total_rounds)}\n"
        "Starting up — first scenario incoming…"
    )]


def build_scenario_wait_layout(
    round_num: int, total_rounds: int, source: str,
) -> list[discord.ui.Item[Any]]:
    """The board while this round's scenario is being written. **Paradigm
    9 — game turn card.**

    Replaces the public ``<@host>`` ping the round used to post every time
    (trivia-tail-97). For the ``players`` source anyone may write and the
    button is on the card; for the host it is on the host panel, with
    🎲 Draw From the Bank beside it. Says what happens if nobody writes one.
    """
    open_to_all = source == "players"
    who = "Anyone in the room" if open_to_all else "The host"
    where = "below" if open_to_all else f"on the host panel ({HOST_COMMAND})"
    items: list[discord.ui.Item[Any]] = [_text(
        f"## {ICON} {TITLE} — {_round(round_num, total_rounds)}\n"
        f"✍️ {who} is writing this round's scenario — press **{WRITE_LABEL}** {where}. "
        "If nobody does within two minutes, one is drawn from the question bank."
    )]
    if open_to_all:
        items.append(discord.ui.ActionRow(round_layouts.grey(WRITE_LABEL, WRITE_ID)))
    return items


# ── the submission board ─────────────────────────────────────────────────────


def build_scenario_layout(
    scenario: str,
    round_num: int,
    total_rounds: int,
    deadline: int,
    submitted: int,
    total_players: int | None = None,
    *,
    roster_submitted: int | None = None,
    closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The submission board. **Paradigm 8 — live board.**

    ``deadline`` is the Unix timestamp the countdown ends at, on its own
    Text Display with a stable id. ``submitted`` / ``total_players`` drive
    the live count the cog refreshes after every modal submit;
    ``roster_submitted`` is how many of ``total_players`` have answered
    when the crowd may also submit, so a spectator's price never reads as a
    joined player's. ``closed`` is the frame left when the window shuts:
    no countdown, no button.
    """
    count = f"💵 Submitted: **{submitted}**"
    if total_players is not None and roster_submitted is not None:
        count += (
            f" ({roster_submitted}/{total_players} who joined"
            " — the round closes once everyone who joined has answered)"
        )
    elif total_players is not None:
        count += f"/{total_players} — the round closes once everyone has answered"
    heading = _text(f"## {ICON} {TITLE} — {_round(round_num, total_rounds)}")
    items: list[discord.ui.Item[Any]] = [
        heading if closed else discord.ui.Section(
            heading, accessory=round_layouts.grey("❓ Help", HELP_ID),
        ),
    ]
    if not closed:
        items.append(discord.ui.TextDisplay(format_deadline(deadline), id=SUBMIT_TIMER_ITEM_ID))
    items.extend([
        _text(f'### "{_scenario(scenario)}"'),
        _text(count if closed else f"{count}\n⏭️ The host can press **Skip** to close it early."),
    ])
    if closed:
        return items
    items.append(discord.ui.ActionRow(round_layouts.grey(SUBMIT_LABEL, SUBMIT_ID)))
    items.append(_text(f"-# {HOST_HINT}"))
    return items


# ── the reveal, the vote, the results ────────────────────────────────────────


def build_reveal_layout(
    scenario: str,
    round_num: int,
    total_rounds: int,
    ladder: list[tuple[str, int]],
) -> list[discord.ui.Item[Any]]:
    """The reveal after the submission window. **Paradigm 7 — notice.**

    ``ladder`` is ``(display_name, amount)`` already sorted by the cog
    (``logic.build_ladder`` then names). The price column is fixed-width
    in a code span; the flavour suffix follows the name.
    """
    lines: list[str] = []
    for name, amount in ladder:
        label = price_label(amount)
        base_price = format_price(amount)
        suffix = label[len(base_price):]
        lines.append(f"`{base_price:>12}` — **{discord.utils.escape_markdown(name)}**{suffix}")
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} Reveal — {_round(round_num, total_rounds)}\n\"{_scenario(scenario)}\""),
        round_layouts.line(),
        _text("### 💵 Price Ladder\n" + (round_layouts.fit_lines(lines, LADDER_BUDGET) or "—")),
    ]
    stats = ladder_stats([a for _, a in ladder])
    if stats is not None:
        items.append(_text(
            "### 📊 Stats\n"
            f"Spread: {format_price(stats['low'])} — {format_price(stats['high'])}\n"
            f"Median: {format_price(stats['median'])}\n"
            f"Average: {format_price(stats['mean'])}"
        ))
    return items


def build_vote_layout(
    scenario: str,
    round_num: int,
    total_rounds: int,
    deadline: int,
    options: list[tuple[str, str]],
) -> list[discord.ui.Item[Any]]:
    """The voting card. **Paradigm 9 — game turn card.**

    ``options`` is ``(label, value)`` per submitter — ``name — price``, cut
    here to the 25 a dropdown holds (the embed era had no guard). The two
    dropdowns are one component each.
    """
    def _select(placeholder: str, custom_id: str) -> discord.ui.Select[Any]:
        return discord.ui.Select(
            placeholder=placeholder,
            options=[
                discord.SelectOption(label=label[:100], value=value)
                for label, value in options[:25]
            ],
            custom_id=custom_id,
        )

    return [
        _text(f"## {ICON} Vote — {_round(round_num, total_rounds)}\n\"{_scenario(scenario)}\""),
        discord.ui.TextDisplay(format_deadline(deadline), id=VOTE_TIMER_ITEM_ID),
        _text("Who had the **Most Reasonable** price? Who was the **Most Unhinged**?"),
        discord.ui.ActionRow(_select("🎯 Most Reasonable — Pick a player…", REASONABLE_ID)),
        discord.ui.ActionRow(_select("🤯 Most Unhinged — Pick a player…", UNHINGED_ID)),
    ]


def _votes(count: int) -> str:
    return f"{count} vote{'s' if count != 1 else ''}"


def build_round_results_layout(
    round_num: int,
    total_rounds: int,
    reasonable_winners: list[str],
    reasonable_price: int,
    reasonable_votes: int,
    unhinged_winners: list[str],
    unhinged_price: int,
    unhinged_votes: int,
) -> list[discord.ui.Item[Any]]:
    """The round's winners, edited over the vote card. **Paradigm 7 —
    notice.** An empty winner list reads "Nobody"; a tie lists everyone."""
    def _line_for(names: list[str], price: int, votes: int) -> str:
        who = join_names(names) if names else "Nobody"
        return f"**{who}** ({format_price(price)}) — {_votes(votes)}"

    return [
        _text(f"## {ICON} Round {round_num} Results\n-# {_round(round_num, total_rounds)}"),
        round_layouts.line(),
        _text(
            f"🎯 **Most Reasonable**\n{_line_for(reasonable_winners, reasonable_price, reasonable_votes)}\n\n"
            f"🤯 **Most Unhinged**\n{_line_for(unhinged_winners, unhinged_price, unhinged_votes)}"
        ),
    ]


# ── the recap ────────────────────────────────────────────────────────────────


def run_again_row() -> discord.ui.ActionRow[Any]:
    return discord.ui.ActionRow(round_layouts.grey("🔁 Run Again", RUN_AGAIN_ID))


def build_recap_layout(
    rounds_played: int,
    player_count: int,
    awards: dict[str, tuple[str, list[str], str]],
    highlight: str | None,
    *,
    payout_line: str | None = None,
    run_again: bool = True,
) -> list[discord.ui.Item[Any]]:
    """The game-over recap. **Paradigm 7 — notice**, with Run Again.

    ``awards`` is ``slug -> (label, names, detail)`` — the names already
    resolved by the cog from ``logic.compute_recap_awards``'s uid lists,
    joined and fitted here. ``highlight`` is the prebuilt sentence or
    ``None`` to omit it. ``payout_line`` is the economy's own line.
    """
    award_lines = [
        f"{label} **{join_names(names)}** — {detail}"
        for _key, (label, names, detail) in awards.items()
        if names
    ]
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} {TITLE} — Game Over\n"
            f"🎮 Rounds played: **{rounds_played}** · 👥 Players: **{player_count}**"
        ),
    ]
    if award_lines:
        items.append(_text("### 🏆 Awards\n" + "\n".join(award_lines)))
    if highlight:
        items.append(_text(f"### 💡 Highlight\n{highlight}"))
    if payout_line:
        items.append(_text(f"-# {payout_line}"))
    if run_again:
        items.append(run_again_row())
    return items


# ── notices ──────────────────────────────────────────────────────────────────

CANCELLED_TEXT = "🛑 Name Your Price was cancelled before it started."
PICKING_UP_TEXT = "↻ Picking up where we left off after a restart…"


def build_notice_layout(text: str) -> list[discord.ui.Item[Any]]:
    """A one-line notice in the game's frame — what a board becomes when the
    lobby is cancelled or a restart picks the game back up. **Paradigm 7.**"""
    return [_text(f"## {ICON} {TITLE}\n{text}")]
