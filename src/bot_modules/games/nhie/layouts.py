"""Never Have I Ever's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_nhie/embeds.py`` (deleted with this
conversion). Pure builders: primitives and a ``name_fn`` in, layout items
out, buttons carrying the ``nhie_*`` custom ids the embed-era view used and
no callbacks — ``games/nhie/cog.py`` binds those. The frame — heading,
round line, controls, host panel, recap — is ``games/utils/round_layouts``,
shared with Would You Rather and Most Likely To so the three cannot drift.

Three cards:

- **The round card** (paradigm 9): the statement, the 😈 / 😇 bars, and —
  when lives are on — who is still standing and who is out. 😈 Guilty and
  😇 Innocent sit under it with ✍️ Pose Statement beneath.
- **The host panel** (paradigm 2, ephemeral): ⏭️ Next and 🏁 End Game.
- **The recap** (paradigm 7): how the game ended, then the guilt board.

Members are named through a required ``name_fn`` — a ``build_name_fn``
resolver the cog built — and a miss reads ``User <id>`` (``games/utils/names``);
the embed called ``resolve_name`` with the guild it was handed, which printed
a bare id for an uncached member.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.utils.names import resolved_name
from bot_modules.games.nhie.logic import DEFAULT_LIVES

ICON = GAME_ICONS["nhie"]
TITLE = GAME_NAMES["nhie"]

GUILTY_ID = "nhie_guilty"
INNOCENT_ID = "nhie_innocent"
POSE_ID = "nhie_pose"
NEXT_ID = "nhie_next"
HELP_ID = "nhie_htp"
#: The host panel's ids — new ones, not the board's old ``nhie_next`` /
#: ``nhie_end``: the panel is an ephemeral message with its own short-lived
#: view. ``nhie_next`` stays the *board's* Next, which a scheduled game
#: still carries.
HOST_NEXT_ID = "nhie_host_next"
HOST_END_ID = "nhie_host_end"

GUILTY_LABEL = "😈 Guilty"
INNOCENT_LABEL = "😇 Innocent"
POSE_LABEL = "✍️ Pose Statement"

#: The most the statement may spend of the card, escaped. The Pose modal
#: caps it at 300 characters, but ``/games play nhie question:`` takes
#: whatever Discord allows.
STATEMENT_LIMIT = 600

#: What the lives roster, the eliminated list and the guilt board may each
#: spend. No player cap exists — anyone who votes is tracked — so each is
#: fitted by whole lines; the embed fields never were.
STANDING_BUDGET = 1400
ELIMINATED_BUDGET = 500
SCORES_BUDGET = 2800

NameFn = Callable[[int], str]


def _lives_blocks(
    lives: dict[int, int] | None,
    eliminated: set[int] | None,
    max_lives: int,
    name_fn: NameFn,
) -> list[str]:
    """The **Still Standing** and **💀 Eliminated** blocks, when lives are on."""
    if not lives:
        return []
    out = eliminated or set()
    standing = [
        f"{'❤️' * hp}{'🖤' * (max_lives - hp)} **{resolved_name(name_fn, uid)}**"
        for uid, hp in sorted(lives.items(), key=lambda x: -x[1])
        if uid not in out
    ]
    blocks: list[str] = []
    if standing:
        blocks.append(
            f"### Still Standing ({len(standing)})\n{round_layouts.fit_lines(standing, STANDING_BUDGET)}"
        )
    gone = [resolved_name(name_fn, uid) for uid in out]
    if gone:
        blocks.append(
            f"### 💀 Eliminated\n{round_layouts.fit_names(gone, ELIMINATED_BUDGET)}"
        )
    return blocks


def build_round_layout(
    statement: str,
    guilty: list[int],
    innocent: list[int],
    round_num: int,
    *,
    closed: bool = False,
    lives: dict[int, int] | None = None,
    eliminated: set[int] | None = None,
    name_fn: NameFn,
    max_lives: int = DEFAULT_LIVES,
    waiting: bool = False,
    advance_at: int | None = None,
    queued: int = 0,
    scheduled: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The round card, open or over. **Paradigm 9 — game turn card.**

    ``waiting`` is the no-statement state (the bank had nothing; a posed
    statement starts the round) and ``advance_at`` the live countdown of a
    timed round. When ``lives`` is supplied (non-empty) a **Still Standing**
    block shows heart pips per player and a separate **Eliminated** block
    lists knocked-out players — on the closed frame too, where they are the
    round's record. ``scheduled`` puts ⏭️ Next on the card, where any voter
    may press it once it unlocks; on a hosted game Next is on the host
    panel.
    """
    total = len(guilty) + len(innocent)
    bar_g, pct_g = build_bar(len(guilty), total)
    bar_i, pct_i = build_bar(len(innocent), total)
    body = "\n".join([
        f"Never have I ever… **{escape_to_fit(statement, STATEMENT_LIMIT)}**",
        "",
        f"😈 {bar_g} {pct_g} ({len(guilty)})",
        f"😇 {bar_i} {pct_i} ({len(innocent)})",
    ])
    controls: list[discord.ui.Item[Any]] = []
    if not waiting:
        controls.append(discord.ui.ActionRow(
            round_layouts.grey(GUILTY_LABEL, GUILTY_ID),
            round_layouts.grey(INNOCENT_LABEL, INNOCENT_ID),
        ))
    controls.append(round_layouts.pose_row(
        POSE_LABEL, POSE_ID, next_id=NEXT_ID if scheduled and not waiting else None,
    ))
    return round_layouts.build_round_layout(
        icon=ICON,
        title=TITLE,
        round_num=round_num,
        body=body,
        help_id=HELP_ID,
        controls=controls,
        notes=[round_layouts.queue_note(queued, "statement")],
        closed=closed,
        waiting=(POSE_LABEL, "statement") if waiting else None,
        advance_at=advance_at,
        tail=_lives_blocks(lives, eliminated, max_lives, name_fn),
    )


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's ⏭️ Next and 🏁 End Game. **Paradigm 2 — panel.**"""
    return round_layouts.build_host_panel_layout(
        ICON,
        next_id=HOST_NEXT_ID,
        end_id=HOST_END_ID,
        blurb="Move to the next statement, or end the game with its guilt board.",
    )


def build_recap_layout(
    winner_id: int | None,
    guilt_scores: dict[str, int],
    *,
    name_fn: NameFn,
    ended: bool = False,
    reason: str | None = None,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The game-over card. **Paradigm 7 — notice.**

    Shows the winner (or the "everyone eliminated" line) and the final
    per-player guilty vote tally sorted from worst to best. ``guilt_scores``
    keys are stringified user IDs to match the persisted payload.
    ``ended=True`` is the **guilt board** ending — the host's 🏁 End Game,
    the round cap or ``/games end`` closed the game before elimination
    decided it (or lives were off), so nobody "won" and the board is the
    payoff.
    """
    if winner_id is not None:
        lead = f"🏆 **{resolved_name(name_fn, winner_id)}** is the last one standing!"
    elif ended:
        lead = (
            "That's the last round — here's the final guilt board."
            if reason == "round_cap"
            else "Game over — here's the final guilt board."
        )
    else:
        lead = "Everyone's been eliminated! No winner this time."
    lines = [
        f"**{resolved_name(name_fn, int(uid))}** — {score} guilty vote{'' if score == 1 else 's'}"
        for uid, score in sorted(guilt_scores.items(), key=lambda x: -x[1])
    ]
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        lead=lead,
        sections=[
            "### Final Guilt Scores\n" + (round_layouts.fit_lines(lines, SCORES_BUDGET) if lines else "—"),
        ],
        payout_line=payout_line,
    )
