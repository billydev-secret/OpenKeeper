"""Fantasies & Dealbreakers' Components V2 cards
(``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_fantasies/embeds.py`` (deleted with this
conversion). Pure builders: primitives in, layout items out, buttons
carrying the ``fan_*`` custom ids the embed-era views used and no
callbacks — ``games/fantasies/cog.py`` binds those. The entry card
rides on the shared round frame (``games/utils/round_layouts``), the same
frame Hot Takes' take card uses, so the two anonymous vote cards are one
shape.

Four cards:

- **The panel** (paradigm 8, live board): the game's one tracked message —
  how the game goes and what anonymous means here, the host, the rounds
  played, the advertised start, Help beside the title, and the small lobby
  strip. **Start Round and End Game are not on it** — everything host-only
  lives on the ephemeral host panel (Ben, 2026-09-30), and one ``-#`` line
  points the host at ``/games host``. Ended, it says the recap is below.
- **The submissions card** (paradigm 8): one round's window — 💖 Submit a
  Fantasy and 🚩 Submit a Dealbreaker. Close Submissions is the host's and
  is on the panel; the closed frame says the window shut.
- **The entry card** (paradigm 9): the entry, the ✅ Same / ❌ Not for me
  bars and the two vote buttons. The closed frame is the entry's record:
  the final bars and no controls. ⏭️ Next is on the panel.
- **The host panel** (paradigm 2, ephemeral): Start Round, Close
  Submissions, ⏭️ Next and End Game on one card for the whole game — a
  press out of its phase answers rather than acting.
- **The recap** (paradigm 7): most shared, most polarizing, biggest
  outlier, the tallies, the payout line.

Every card names nobody: entries are anonymous, and the recap counts
voters rather than listing them.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.fantasies.logic import (
    CATEGORY_DEALBREAKER,
    CATEGORY_FANTASY,
    compute_recap_summary,
)

ICON = GAME_ICONS["fantasies"]
TITLE = GAME_NAMES["fantasies"]

HELP_ID = "fan_htp"
SUBMIT_FANTASY_ID = "fan_submit_fantasy"
SUBMIT_DEALBREAKER_ID = "fan_submit_dealbreaker"
SAME_ID = "fan_same"
NOPE_ID = "fan_nope"
#: The host panel's ids — new ones, not the boards' old ``fan_start_round``
#: / ``fan_close_sub`` / ``fan_next`` / ``fan_end``: the panel is an
#: ephemeral message with its own short-lived view.
HOST_START_ID = "fan_host_start"
HOST_CLOSE_ID = "fan_host_close"
HOST_NEXT_ID = "fan_host_next"
HOST_END_ID = "fan_host_end"

START_LABEL = "Start Round"
CLOSE_LABEL = "Close Submissions"
END_LABEL = "End Game"
SAME_LABEL = "✅ Same"
NOPE_LABEL = "❌ Not for Me"
SUBMIT_LABELS = {
    CATEGORY_FANTASY: "💖 Submit a Fantasy",
    CATEGORY_DEALBREAKER: "🚩 Submit a Dealbreaker",
}

#: The most an entry may spend of a card, escaped: the modal caps it at 500
#: characters, which escaping can double.
ENTRY_LIMIT = 1000
#: What each of the recap's three quoted entries may spend.
RECAP_ENTRY_LIMIT = 600

# The panel says how the game goes and what "anonymous" means here, on the
# card itself rather than behind the Help button (anon-tail-72/75). The
# mod-visibility line is the one FFA's reply help has always carried.
LOBBY_DESCRIPTION = (
    "Submit anonymously each round, then vote!\n\n"
    f"**How to play:** the host opens a round; everyone submits a "
    "💖 fantasy or a 🚩 dealbreaker, then each entry is shown one at a time "
    "and the room votes ✅ Same or ❌ Not for me. Run as many rounds as you "
    f"like, then **{END_LABEL}** posts the recap.\n"
    "Your name is never shown with your entry — mods can still see who sent it."
)
ENDED_LINE = "🏁 Game over — the recap is below."


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


# ── the panel ────────────────────────────────────────────────────────────────


def build_lobby_layout(
    host_name: str,
    rounds_played: int = 0,
    *,
    start_at: int | None = None,
    ended: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The game's panel, posted by ``/games play fantasies``. **Paradigm 8
    — live board.**

    ``rounds_played`` is the count the cog rebuilds this card with after
    each round. ``start_at`` is the host's advertised first-round start
    (``start_in``), a live relative timestamp — advertising, not
    automation. ``ended`` is the frame left once the recap has posted: no
    Help, one line saying so.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    lines.append(f"Hosted by **{discord.utils.escape_markdown(host_name)}** · 👁 Anonymous")
    heading = _text("\n".join(lines))
    facts = [f"🔁 Rounds played: **{rounds_played}**"]
    if start_at and not ended:
        facts.append(f"⏰ Starting <t:{int(start_at)}:R>")
    body = f"{LOBBY_DESCRIPTION}\n\n{' · '.join(facts)}"
    body += f"\n{ENDED_LINE}" if ended else f"\n-# {HOST_HINT}"
    items: list[discord.ui.Item[Any]] = [
        *top,
        heading if ended else discord.ui.Section(
            heading, accessory=round_layouts.grey("❓ Help", HELP_ID),
        ),
        _text(body),
    ]
    return [*items, *bottom]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    Start Round and End Game between rounds, Close Submissions while a
    window is open, ⏭️ Next while an entry is being voted on — one card for
    the whole game, since the round moves between its phases on its own;
    a press out of its phase answers plainly. Nothing here is destructive
    (End Game posts the recap and pays), so every button is grey.
    """
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb=(
            "Open a round, close its submissions, move the vote along, or end "
            "the game with its recap."
        ),
        rows=[
            [round_layouts.grey(START_LABEL, HOST_START_ID), round_layouts.grey(CLOSE_LABEL, HOST_CLOSE_ID)],
            [
                round_layouts.grey(round_layouts.NEXT_LABEL, HOST_NEXT_ID),
                round_layouts.grey(END_LABEL, HOST_END_ID),
            ],
        ],
    )


# ── the submissions card ─────────────────────────────────────────────────────


def build_submit_layout(round_num: int, *, closed: bool = False) -> list[discord.ui.Item[Any]]:
    """One round's submission window. **Paradigm 8 — live board.**

    Open, the two category buttons — the choice is made before any typing
    (a typed category once cost members their whole entry). Closed, the
    frame says the window shut and carries nothing; the embed era wrote
    that into ``content=`` over the disabled buttons.
    """
    heading = f"## {ICON} Round {round_num}"
    if closed:
        return [_text(f"{heading}\n✅ Submissions closed for Round {round_num}!")]
    return [
        _text(
            f"{heading}\nSubmit your fantasy or dealbreaker anonymously!\n"
            f"-# {HOST_HINT}"
        ),
        discord.ui.ActionRow(
            round_layouts.grey(SUBMIT_LABELS[CATEGORY_FANTASY], SUBMIT_FANTASY_ID),
            round_layouts.grey(SUBMIT_LABELS[CATEGORY_DEALBREAKER], SUBMIT_DEALBREAKER_ID),
        ),
    ]


# ── the entry card ───────────────────────────────────────────────────────────


def vote_bars(same_votes: list[int], nope_votes: list[int]) -> str:
    """The ✅ Same and ❌ Not for me meters."""
    total = len(same_votes) + len(nope_votes)
    bar_s, pct_s = build_bar(len(same_votes), total)
    bar_n, pct_n = build_bar(len(nope_votes), total)
    return (
        f"✅ Same\n{bar_s} {pct_s} ({len(same_votes)})\n"
        f"❌ Not for me\n{bar_n} {pct_n} ({len(nope_votes)})"
    )


def build_vote_layout(
    *,
    entry_text: str,
    entry_num: int,
    category: str,
    same_votes: list[int],
    nope_votes: list[int],
    total_entries: int = 0,
    closed: bool = False,
    advance_at: int | None = None,
) -> list[discord.ui.Item[Any]]:
    """One entry's card, open or over. **Paradigm 9 — game turn card**, on
    the shared round frame.

    ``advance_at`` (epoch) is the live countdown while the entry's timer
    runs; ``total_entries`` adds the ``/N`` to the progress line when the
    cog knows it. Open, the two vote buttons sit under the bars; ⏭️ Next is
    the host's and lives on the panel. Closed, the card is the entry's
    record: the final bars and no controls.
    """
    progress = f"Entry **{entry_num}**"
    if total_entries:
        progress += f"/{total_entries}"
    body = f"**{escape_to_fit(entry_text, ENTRY_LIMIT)}**\n\n{vote_bars(same_votes, nope_votes)}"
    return round_layouts.build_round_layout(
        icon=ICON,
        title=f"{category} #{entry_num}",
        round_num=entry_num,
        progress=f"{progress} · 👁 Anonymous",
        body=body,
        help_id=HELP_ID,
        controls=[discord.ui.ActionRow(
            round_layouts.grey(SAME_LABEL, SAME_ID),
            round_layouts.grey(NOPE_LABEL, NOPE_ID),
        )],
        closed=closed,
        advance_at=advance_at,
    )


# ── the recap ────────────────────────────────────────────────────────────────


def _quoted(entry: dict[str, Any]) -> str:
    return f'"{escape_to_fit(str(entry.get("text", "")), RECAP_ENTRY_LIMIT)}"'


def build_recap_layout(
    results: list[dict[str, Any]],
    *,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]] | None:
    """The game-over card. **Paradigm 7 — notice.**

    The most universally shared entry, the most polarizing, the biggest
    outlier, then the entry and voter tallies. Returns ``None`` when
    ``results`` is empty so the cog posts its one-line notice instead — the
    same early return the embed builder had.
    """
    summary = compute_recap_summary(results)
    if summary is None:
        return None
    shared, polar, outlier = summary["most_shared"], summary["most_polar"], summary["biggest_outlier"]
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        facts=[
            f"Entries: **{summary['total_results']}**",
            f"Voters: **{len(summary['total_voters'])}**",
        ],
        sections=[
            f"### 🌟 Most Universally Shared\n{_quoted(shared)} ({shared['same_pct']:.0%} Same)",
            f"### ⚡ Most Polarizing\n{_quoted(polar)}",
            f"### 🏔️ Biggest Outlier\n{_quoted(outlier)} ({outlier['same_pct']:.0%} Same)",
        ],
        payout_line=payout_line,
    )
