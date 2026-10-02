"""Hot Takes' Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_hottakes/embeds.py`` (deleted with this
conversion). Pure builders: primitives in, layout items out, buttons
carrying the ``ht_*`` custom ids the embed-era views used and no callbacks —
``games/hottakes/cog.py`` binds those. The take card rides on the
shared round frame (``games/utils/round_layouts``), the same frame the
three reactive vote games and Fantasies & Dealbreakers use, so an anonymous
vote card looks the same whichever game posted it.

Four cards:

- **The lobby** (paradigm 8, live board): how the game goes and what
  anonymous means here, the live submission count, the advertised start,
  **Submit Hot Take** with Help beside the title, and the small lobby strip.
  **Start Voting and Cancel Game are not on it** — everything host-only
  lives on the ephemeral host panel (Ben, 2026-09-30), and one ``-#`` line
  points the host at ``/games host``. The count is rebuilt from the payload
  on every submission; the embed era read it back off the message's field.
- **The take card** (paradigm 9): the take, the five 🧊 → 🔥 bars, the
  five vote buttons, 📝 Submit Take beneath. The closed frame is the take's
  record: the final bars and no controls. ⏭️ Next Take is on the panel.
- **The host panel** (paradigm 2, ephemeral): Start Voting, Cancel Game and
  ⏭️ Next Take on one card for the whole game — a press out of its phase
  answers rather than acting.
- **The recap** (paradigm 7): hottest, coldest, most divisive, the tallies,
  the payout line.

Every card names nobody: takes are anonymous, and the recap counts voters
rather than listing them.
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
from bot_modules.games.hottakes.logic import (
    MIN_TAKES,
    VOTE_LABELS,
    compute_recap_summary,
)

ICON = GAME_ICONS["hottakes"]
TITLE = GAME_NAMES["hottakes"]

SUBMIT_ID = "ht_submit"
HELP_ID = "ht_htp"
VOTE_IDS = ["ht_v0", "ht_v1", "ht_v2", "ht_v3", "ht_v4"]
VOTE_SUBMIT_ID = "ht_v_submit"
#: The host panel's ids — new ones, not the boards' old ``ht_start`` /
#: ``ht_cancel`` / ``ht_next``: the panel is an ephemeral message with its
#: own short-lived view.
HOST_START_ID = "ht_host_start"
HOST_CANCEL_ID = "ht_host_cancel"
HOST_NEXT_ID = "ht_host_next"

SUBMIT_LABEL = "Submit Hot Take"
VOTE_SUBMIT_LABEL = "📝 Submit Take"
START_LABEL = "Start Voting"
NEXT_LABEL = "⏭️ Next Take"
#: The button glyphs, one per step of the scale; the full labels are the
#: bars' captions.
VOTE_GLYPHS = [label.split(" ", 1)[0] for label in VOTE_LABELS]

#: The most a take may spend of a card, escaped: the modal caps it at 500
#: characters, which escaping can double.
TAKE_LIMIT = 1000
#: What each of the recap's three quoted takes may spend.
RECAP_TAKE_LIMIT = 600

# The lobby says how the game goes and what "anonymous" means here, on the
# card itself rather than behind the Help button (anon-tail-72/75). The
# mod-visibility line is the one FFA's reply help has always carried.
LOBBY_DESCRIPTION = (
    "Submit your spiciest take — all entries are anonymous.\n\n"
    f"**How to play:** press **{SUBMIT_LABEL}** (as many as you like); when "
    f"at least {MIN_TAKES} are in, the host opens the vote and the room rates "
    "each take 🧊 → 🔥, one at a time. The hottest take wins.\n"
    "Your name is never shown with your take — mods can still see who sent it."
)
VOTING_UNDERWAY = "📊 Voting is underway — a take submitted now joins the end of the queue."
CANCELLED_TEXT = "🛑 Hot Takes was cancelled before voting started."


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


# ── the lobby ────────────────────────────────────────────────────────────────


def build_lobby_layout(
    host_name: str,
    submission_count: int = 0,
    *,
    start_at: int | None = None,
    closed: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The submission lobby posted by ``/games play hottakes``. **Paradigm
    8 — live board.**

    ``submission_count`` is the live tally the cog rebuilds this card with as
    takes arrive. ``start_at`` is the host's advertised start (``start_in``),
    a live relative timestamp — advertising, not automation: the host still
    presses Start Voting. ``closed`` is the frame left once voting opens:
    no Submit button, a line saying where a late take goes.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    lines.append(f"Hosted by **{discord.utils.escape_markdown(host_name)}** · 👁 Anonymous")
    facts = [f"📝 Submissions: **{submission_count}**"]
    if start_at:
        facts.append(f"⏰ Starting <t:{int(start_at)}:R>")
    body = f"{LOBBY_DESCRIPTION}\n\n{' · '.join(facts)}"
    if closed:
        body += f"\n{VOTING_UNDERWAY}"
    else:
        body += f"\n-# {HOST_HINT}"
    items: list[discord.ui.Item[Any]] = [
        *top,
        # A closed lobby's view is stopped, so a Help on it could only fail;
        # the closed frame carries no controls (ship review, 2026-10-01).
        _text("\n".join(lines)) if closed else discord.ui.Section(
            _text("\n".join(lines)), accessory=round_layouts.grey("❓ Help", HELP_ID),
        ),
        _text(body),
    ]
    if not closed:
        items.append(discord.ui.ActionRow(round_layouts.grey(SUBMIT_LABEL, SUBMIT_ID)))
    return [*items, *bottom]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    Start Voting and Cancel Game for the lobby, ⏭️ Next Take for the vote,
    on one card: the game moves from lobby to vote on its own, so a press
    out of its phase answers plainly. Cancel Game is red — the one
    destructive press.
    """
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb=(
            f"Open the vote once {MIN_TAKES} takes are in or scrap the lobby; "
            "during the vote, skip to the next take."
        ),
        rows=[[
            round_layouts.grey(START_LABEL, HOST_START_ID),
            round_layouts.red("Cancel Game", HOST_CANCEL_ID),
            round_layouts.grey(NEXT_LABEL, HOST_NEXT_ID),
        ]],
    )


def build_notice_layout(text: str) -> list[discord.ui.Item[Any]]:
    """A one-line notice in the game's frame — what the lobby becomes when
    it is cancelled. **Paradigm 7.**"""
    return [_text(f"## {ICON} {TITLE}\n{text}")]


# ── the take card ────────────────────────────────────────────────────────────


def vote_bars(votes_by_user: dict[int, int]) -> str:
    """One captioned meter per step of the scale."""
    counts = [0] * len(VOTE_LABELS)
    for v in votes_by_user.values():
        if 0 <= v < len(VOTE_LABELS):
            counts[v] += 1
    total = sum(counts)
    lines = []
    for i, label in enumerate(VOTE_LABELS):
        bar, pct = build_bar(counts[i], total)
        lines.append(f"{label}\n{bar} {pct} ({counts[i]})")
    return "\n".join(lines)


def build_vote_layout(
    take_text: str,
    take_num: int,
    total_takes: int,
    votes_by_user: dict[int, int],
    *,
    closed: bool = False,
    advance_at: int | None = None,
) -> list[discord.ui.Item[Any]]:
    """One take's card, open or over. **Paradigm 9 — game turn card**, on
    the shared round frame.

    ``advance_at`` (epoch) is the live countdown while the take's timer runs.
    Open, the five vote buttons sit under the bars with 📝 Submit Take
    beneath — anyone may queue a take mid-vote, so it stays on the card;
    ⏭️ Next Take is the host's and lives on the panel. Closed, the card is
    the take's record: the final bars and no controls.
    """
    body = f"**{escape_to_fit(take_text, TAKE_LIMIT)}**\n\n{vote_bars(votes_by_user)}"
    controls: list[discord.ui.Item[Any]] = [
        discord.ui.ActionRow(*[
            round_layouts.grey(glyph, custom_id)
            for glyph, custom_id in zip(VOTE_GLYPHS, VOTE_IDS)
        ]),
        discord.ui.ActionRow(round_layouts.grey(VOTE_SUBMIT_LABEL, VOTE_SUBMIT_ID)),
    ]
    return round_layouts.build_round_layout(
        icon=ICON,
        title=f"Hot Take #{take_num}",
        round_num=take_num,
        progress=f"Take **{take_num}**/{total_takes} · 👁 Anonymous",
        body=body,
        help_id=HELP_ID,
        controls=controls,
        closed=closed,
        advance_at=advance_at,
    )


# ── the recap ────────────────────────────────────────────────────────────────


def _quoted(entry: dict[str, Any]) -> str:
    return f'"{escape_to_fit(str(entry.get("text", "")), RECAP_TAKE_LIMIT)}"'


def build_recap_layout(
    results: list[dict[str, Any]],
    *,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]] | None:
    """The game-over card. **Paradigm 7 — notice.**

    The hottest take, the coldest, an optional Most Divisive (only when two
    or more takes were voted on), then the take and voter tallies. Returns
    ``None`` when ``results`` is empty so the cog posts nothing — the same
    early return the embed builder had.
    """
    summary = compute_recap_summary(results)
    if summary is None:
        return None
    hottest, coldest = summary["hottest"], summary["coldest"]
    sections = [
        f"### 🔥 Hottest Take\n{_quoted(hottest)} (avg {hottest['avg']:.1f}/5)",
        f"### 🧊 Coldest Take\n{_quoted(coldest)} (avg {coldest['avg']:.1f}/5)",
    ]
    if summary["most_divisive"] is not None:
        sections.append(f"### ⚡ Most Divisive\n{_quoted(summary['most_divisive'])}")
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        facts=[
            f"Takes: **{summary['total_takes']}**",
            f"Voters: **{len(summary['total_voters'])}**",
        ],
        sections=sections,
        payout_line=payout_line,
    )
