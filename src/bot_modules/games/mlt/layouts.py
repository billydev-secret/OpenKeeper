"""Most Likely To's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_mlt/embeds.py`` (deleted with this
conversion). Pure builders: primitives and a ``name_fn`` in, layout items
out, buttons and the vote dropdown carrying the ``mlt_*`` custom ids the
embed-era views used and no callbacks — ``games/mlt/cog.py`` binds
those. The round frame, host panel and recap are
``games/utils/round_layouts``, shared with Would You Rather and Never Have
I Ever so the three cannot drift.

Five cards:

- **The lobby** (paradigm 8, live board): host, the advertised start, the
  roster, Join and Leave, Help beside the opening text. It wears the small
  lobby strip. **Start is not on it** — it is host-only, so it lives on the
  host panel.
- **The round card** (paradigm 9): the prompt, the dropdown of players, and
  ✍️ Pose Prompt. It shows no tally while it is open — votes are secret
  until the round closes — and the closed frame says how many were cast.
- **The host panel** (paradigm 2, ephemeral): Start, ⏭️ Next and 🏁 End
  Game on one card for the whole game; a press out of its phase answers
  rather than acting.
- **The round results** (paradigm 7): every player's votes, the crowned
  one(s) marked. Posted fresh when a round closes.
- **The final standings** (paradigm 7): the crowns, on the shared recap.

The results and standings name players through a ``name_fn``
(``services/name_resolver.build_name_fn``): a ``<@id>`` in a Text Display is
resolved by the *reading* client from its own cache, so it renders as a bare
number for any viewer who hasn't seen that member. The default ``mention``
keeps an un-wired caller rendering; the cog is required to pass a real
resolver (a test walks its render sites). A resolver miss reads
``User <id>`` (``games/utils/names``).
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit, trim_escaped
from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.utils.names import resolved_name
from bot_modules.shared.embeds import take_to_fit
from bot_modules.shared.name_resolver import NameFn, mention

ICON = GAME_ICONS["mlt"]
TITLE = GAME_NAMES["mlt"]

JOIN_ID = "mlt_join"
LEAVE_ID = "mlt_leave"
LOBBY_HELP_ID = "mlt_htp"
VOTE_SELECT_ID = "mlt_vote_select"
POSE_ID = "mlt_pose"
NEXT_ID = "mlt_next"
#: The round card's Help — the embed era gave the lobby's and the round's
#: Help different ids, and both are persistent.
HELP_ID = "mlt_htp2"
#: The host panel's ids — new ones, not the old ``mlt_start`` / ``mlt_next``
#: / ``mlt_end``: the panel is an ephemeral message with its own
#: short-lived view. ``mlt_next`` stays the *board's* Next, which a
#: scheduled game still carries.
HOST_START_ID = "mlt_host_start"
HOST_NEXT_ID = "mlt_host_next"
HOST_END_ID = "mlt_host_end"

POSE_LABEL = "✍️ Pose Prompt"
START_LABEL = "Start"

#: The most the prompt may spend of a card, escaped. The Pose modal caps it
#: at 300 characters, but ``/games play mlt question:`` takes whatever
#: Discord allows.
PROMPT_LIMIT = 600

#: The most the lobby's roster may spend. ``MAX_PLAYERS`` is 25 and a
#: 32-character name of pure markdown escapes to 64, so a full adversarial
#: lobby reaches 1,648; whole names past the budget are counted, not cut.
ROSTER_BUDGET = 1500

#: What the results lines and the standings lines may each spend: up to 25
#: lines of a 64-character escaped name.
LINES_BUDGET = 2800


def _fit_roster(players: list[str], budget: int = ROSTER_BUDGET) -> str:
    """Escape and comma-join player names, dropping whole trailing names
    past *budget* and saying how many didn't fit rather than going quiet.

    ``escape_markdown`` can double a name, so the names are escaped first
    and budgeted after — a full lobby of markdown-heavy names is exactly
    the case escaping exists to handle safely, and an unfitted join loses
    the whole lobby card to a 400.
    """
    if not players:
        return "-# Nobody yet"
    escaped = [discord.utils.escape_markdown(name) for name in players]
    shown, remaining = take_to_fit(escaped, budget - 24)
    if not remaining:
        return ", ".join(shown)
    tail = f"_…and {remaining} more_"
    return f"{', '.join(shown)}\n{tail}" if shown else tail


def build_lobby_action_row() -> discord.ui.ActionRow[Any]:
    """Join and Leave — what any member may press."""
    return discord.ui.ActionRow(
        round_layouts.grey("Join", JOIN_ID),
        round_layouts.grey("Leave", LEAVE_ID),
    )


def build_lobby_layout(
    host_name: str,
    players: list[str],
    start_at: int | None = None,
    *,
    closed: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The lobby shown while players join. **Paradigm 8 — live board.**

    ``players`` is a list of pre-resolved display names — escaped here.
    ``start_at`` is an optional UTC epoch shown as a live Discord relative
    timestamp — the host's advertised start time. The host still presses
    Start (on the host panel); the countdown is advertising, not
    automation. ``closed`` is the frame left once the game has started: no
    Join, no Leave.

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
    items: list[discord.ui.Item[Any]] = [
        *top,
        discord.ui.Section(
            round_layouts.text("\n".join(lines)),
            accessory=round_layouts.grey("❓ Help", LOBBY_HELP_ID),
        ),
        round_layouts.text(
            "Join the pool, then vote each round for whoever fits the prompt best.\n"
            f"-# {HOST_HINT}"
        ),
        round_layouts.line(spacing=discord.SeparatorSpacing.large),
        round_layouts.text(f"### Players ({len(players)})\n{_fit_roster(players)}"),
    ]
    if not closed:
        items.append(build_lobby_action_row())
    return [*items, *bottom]


# ── the round ────────────────────────────────────────────────────────────────


def build_round_layout(
    prompt: str,
    round_num: int,
    vote_count: int,
    options: list[tuple[str, str]],
    *,
    closed: bool = False,
    waiting: bool = False,
    advance_at: int | None = None,
    queued: int = 0,
    scheduled: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The round card, open or over. **Paradigm 9 — game turn card.**

    ``options`` is ``(label, value)`` per player, cut here to the 25 a
    dropdown holds; a label renders no markdown, so it is fitted, not
    escaped. ``waiting`` is the no-prompt state (the bank had nothing; a
    posed prompt starts the round) and ``advance_at`` the countdown of a
    timed round. ``vote_count`` shows only on the closed frame: the tally
    is secret while the round is open, and an open card is not redrawn on
    a vote, so a count there would sit at whatever it was when the card
    was last drawn. ``scheduled`` puts ⏭️ Next on the card, where any voter
    may press it once it unlocks.

    ``prompt`` is escaped-then-fit, not just escaped: the Pose modal caps
    it at 300 raw chars, but ``/mlt``'s own ``question:`` option has no
    ``max_length``.
    """
    body = f"Most likely to… **{escape_to_fit(prompt, PROMPT_LIMIT)}**"
    if closed:
        body += f"\n**{vote_count}** vote{'' if vote_count == 1 else 's'} cast"
    controls: list[discord.ui.Item[Any]] = []
    if not waiting and options:
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="🗳️ Vote: Pick a player…",
            options=[
                discord.SelectOption(label=trim_escaped(label, 100), value=value)
                for label, value in options[:25]
            ],
            custom_id=VOTE_SELECT_ID,
        )
        controls.append(discord.ui.ActionRow(select))
    controls.append(round_layouts.pose_row(
        POSE_LABEL, POSE_ID, next_id=NEXT_ID if scheduled and not waiting else None,
    ))
    return round_layouts.build_round_layout(
        icon=ICON,
        title=f"{TITLE}…",
        round_num=round_num,
        body=body,
        help_id=HELP_ID,
        controls=controls,
        notes=[round_layouts.queue_note(queued, "prompt")],
        closed=closed,
        waiting=(POSE_LABEL, "prompt") if waiting else None,
        advance_at=advance_at,
    )


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's Start, ⏭️ Next and 🏁 End Game. **Paradigm 2 — panel.**

    One card for the whole game rather than one per phase: a panel that
    swapped its buttons when the game started would have to be edited from
    a press that may come from a different panel (a mod's, or a second
    ``/games host``). A press out of its phase answers instead of acting.
    """
    return round_layouts.build_host_panel_layout(
        ICON,
        next_id=HOST_NEXT_ID,
        end_id=HOST_END_ID,
        blurb="Start once the pool is in, move rounds along, or end the game with its standings.",
        start=(START_LABEL, HOST_START_ID),
    )


# ── results, standings ───────────────────────────────────────────────────────


def build_results_layout(
    prompt: str,
    round_num: int,
    tally: dict[int, int],
    *,
    name_fn: NameFn = mention,
    winners: list[int] | None = None,
) -> list[discord.ui.Item[Any]]:
    """The per-round results posted after votes are tallied. **Paradigm 7 —
    notice.**

    Lines are sorted by descending vote count; the crowned player(s) get
    a 👑 prefix. ``winners`` is what :func:`logic.find_round_winners`
    decided — the cog passes it so the crowns shown match the crowns
    banked once a tie at the top has been broken without self-votes
    (vote-games-62); omitted, the top vote count is crowned (every tied
    player on a tie). When ``tally`` is empty (no votes were cast) a
    placeholder renders so the card is never blank.

    ``name_fn`` turns a user id into card-ready (already escaped) text.
    """
    sorted_tally = sorted(tally.items(), key=lambda x: -x[1])
    max_votes = sorted_tally[0][1] if sorted_tally else 0
    crowned = (
        set(winners) if winners is not None
        else {uid for uid, count in sorted_tally if count == max_votes and count > 0}
    )
    lines = [
        f"{'👑 ' if uid in crowned else ''}**{resolved_name(name_fn, uid)}** — "
        f"{count} vote{'' if count == 1 else 's'}"
        for uid, count in sorted_tally
    ]
    return [
        round_layouts.text(
            f"## {ICON} {TITLE} — Round {round_num} Results\n"
            f"Most likely to… **{escape_to_fit(prompt, PROMPT_LIMIT)}**"
        ),
        round_layouts.line(),
        round_layouts.text(round_layouts.fit_lines(lines, LINES_BUDGET) if lines else "No votes cast."),
    ]


def build_final_standings_layout(
    crowns: dict,
    *,
    name_fn: NameFn = mention,
    reason: str | None = None,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """Final cumulative-crown standings shown when a game ends.
    **Paradigm 7 — notice**, on the shared recap frame.

    ``crowns`` maps ``str(user_id) -> crown count``. Players are ranked by
    crown count; ties at the top all get 👑. ``reason`` of ``round_cap``
    says the game ran its last round — the embed had that line prepended by
    the cog after the build.
    """
    items = sorted(
        ((int(uid), int(c)) for uid, c in (crowns or {}).items() if int(c) > 0),
        key=lambda x: -x[1],
    )
    if items:
        top = items[0][1]
        # ``crowns`` is payload-shaped (str keys); the resolver's caches are
        # int-keyed, and a str id falls straight through to a miss.
        lines = [
            f"{'👑 ' if count == top else f'{rank}. '}"
            f"**{resolved_name(name_fn, int(uid))}** — {count} crown{'' if count == 1 else 's'}"
            for rank, (uid, count) in enumerate(items, start=1)
        ]
        standings = round_layouts.fit_lines(lines, LINES_BUDGET)
    else:
        standings = "No crowns were awarded this game."
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        lead="That's the last round!" if reason == "round_cap" else None,
        sections=[f"### Final Standings\n{standings}"],
        payout_line=payout_line,
    )
