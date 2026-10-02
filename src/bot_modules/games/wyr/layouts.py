"""Would You Rather's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_wyr/embeds.py`` (deleted with this
conversion). Pure builders: primitives and a ``name_fn`` in, layout items
out, buttons carrying the ``wyr_*`` custom ids the embed-era view used and
no callbacks — ``games/wyr/cog.py`` binds those. The frame — heading,
round line, controls, host panel, recap — is
``games/utils/round_layouts``, shared with Never Have I Ever and Most
Likely To so the three cannot drift.

Three cards:

- **The round card** (paradigm 9): each option over its own live bar, with
  🅰️ / 🅱️ / 👀 Show My Vote beneath and ✍️ Pose Question under those. The
  embed put the two options in inline fields beside each other and the
  bars in a third field; a layout has no columns, and an option directly
  over its own bar reads better on a phone than either.
- **The host panel** (paradigm 2, ephemeral): ⏭️ Next and 🏁 End Game.
- **The recap** (paradigm 7): rounds, votes, and the most divisive question.

A named voter — one who pressed **👀 Show My Vote** — renders through a
``name_fn`` (``services/name_resolver.build_name_fn``): a ``<@id>`` in a
Text Display is resolved by the *reading* client from its own cache, so it
renders as a bare number for any viewer who hasn't seen that member. The
default ``mention`` keeps an un-wired caller rendering; the cog is required
to pass a real resolver (a test walks its render sites), and a resolver
miss reads ``User <id>`` (``games/utils/names``).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.utils.names import resolved_name
from bot_modules.games.wyr.logic import (
    count_votes,
    most_divisive_round,
    played_rounds,
    shown_on_side,
)
from bot_modules.shared.name_resolver import NameFn, mention

ICON = GAME_ICONS["wyr"]
TITLE = GAME_NAMES["wyr"]

VOTE_A_ID = "wyr_a"
VOTE_B_ID = "wyr_b"
SHOW_VOTE_ID = "wyr_show_vote"
POSE_ID = "wyr_pose"
NEXT_ID = "wyr_next"
HELP_ID = "wyr_htp"
#: The host panel's ids — new ones, not the board's old ``wyr_next`` /
#: ``wyr_end``: the panel is an ephemeral message with its own short-lived
#: view. ``wyr_next`` stays the *board's* Next, which a scheduled game
#: still carries.
HOST_NEXT_ID = "wyr_host_next"
HOST_END_ID = "wyr_host_end"

VOTE_A_LABEL = "🅰️ Option A"
VOTE_B_LABEL = "🅱️ Option B"
POSE_LABEL = "✍️ Pose Question"
SHOW_VOTE_LABEL = "👀 Show My Vote"

#: The most one option may spend of the card, escaped. The Pose modal caps
#: an option at 200 characters, but ``/games play wyr question:`` takes
#: whatever Discord allows.
OPTION_LIMIT = 400

#: The most one side's named voters may spend. No player cap exists — the
#: whole room votes — and every voter may press Show My Vote, so the list
#: is fitted by whole names; the embed never was.
SHOWN_BUDGET = 700

#: Not a constant badge: it says what the default is and how to opt out of
#: it (vote-games-61 — "Anonymous" alone read as a promise nobody could act
#: on).
ANON_NOTE = "👁 Anonymous unless you Show My Vote"


def _side(
    glyph: str,
    option: str,
    votes: list,
    total: int,
    shown: list[int],
    name_fn: NameFn,
) -> str:
    bar, pct = build_bar(len(votes), total)
    lines = [
        f"{glyph} **{escape_to_fit(option, OPTION_LIMIT)}**",
        f"{bar} {pct} ({len(votes)})",
    ]
    named = shown_on_side(votes, shown)
    if named:
        names = round_layouts.fit_names(
            [resolved_name(name_fn, uid) for uid in named], SHOWN_BUDGET,
        )
        hidden = len(votes) - len(named)
        if hidden:
            names += f" +{hidden} anonymous"
        lines.append(names)
    return "\n".join(lines)


def build_round_layout(
    option_a: str,
    option_b: str,
    votes_a: list,
    votes_b: list,
    anonymous: bool,
    round_num: int,
    *,
    closed: bool = False,
    name_fn: NameFn = mention,
    waiting: bool = False,
    advance_at: int | None = None,
    shown: Collection[int] | None = None,
    queued: int = 0,
    scheduled: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The round card, open or over. **Paradigm 9 — game turn card.**

    ``shown`` names only the voters who pressed **👀 Show My Vote**
    (vote-games-61) — each side lists its shown voters and counts the rest
    as anonymous. ``waiting`` renders the round with no question yet (the
    bank had nothing to serve — a posed question starts it): only Pose and
    Help are on the card. ``advance_at`` adds the live countdown of a timed
    round. ``queued`` is how many posed questions are waiting their turn,
    and ``scheduled`` puts ⏭️ Next on the card, where any voter may press it
    once it unlocks; on a hosted game Next is on the host panel.
    """
    total = len(votes_a) + len(votes_b)
    shown_list = list(shown or [])
    body = "\n\n".join([
        _side("🅰️", option_a, votes_a, total, shown_list, name_fn),
        _side("🅱️", option_b, votes_b, total, shown_list, name_fn),
    ])
    controls: list[discord.ui.Item[Any]] = []
    if not waiting:
        controls.append(discord.ui.ActionRow(
            round_layouts.grey(VOTE_A_LABEL, VOTE_A_ID),
            round_layouts.grey(VOTE_B_LABEL, VOTE_B_ID),
            round_layouts.grey(SHOW_VOTE_LABEL, SHOW_VOTE_ID),
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
        notes=[
            ANON_NOTE if anonymous else None,
            round_layouts.queue_note(queued, "question"),
        ],
        closed=closed,
        waiting=(POSE_LABEL, "question") if waiting else None,
        advance_at=advance_at,
    )


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's ⏭️ Next and 🏁 End Game. **Paradigm 2 — panel.**"""
    return round_layouts.build_host_panel_layout(
        ICON,
        next_id=HOST_NEXT_ID,
        end_id=HOST_END_ID,
        blurb="Move to the next question, or end the game with its recap.",
    )


def build_recap_layout(
    rounds: dict,
    *,
    reason: str | None = None,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The game-over card: rounds played, total votes, the most divisive
    question and its split. **Paradigm 7 — notice.**

    Posted by the host's **🏁 End Game**, the round cap and ``/games end``
    (vote-games-52 / discovery-3 — until 2026-09-04 the only ending WYR had
    was the red Force-Closed card or the 24h sweep). Vote counts only; who
    voted is each voter's own to show, on the round, with Show My Vote.
    """
    played = played_rounds(rounds)
    most_div = most_divisive_round(rounds)
    if most_div is not None:
        a, b = len(most_div.get("a") or []), len(most_div.get("b") or [])
        question = escape_to_fit(str(most_div.get("q", "")), 400)
        divisive = f"{question}\n🅰️ {a} — 🅱️ {b}"
    else:
        divisive = "No votes were cast."
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        lead="That's the last round — thanks for playing!" if reason == "round_cap" else None,
        facts=[
            f"Rounds played: **{len(played)}**",
            f"Total votes: **{count_votes(rounds)}**",
        ],
        sections=[f"### Most Divisive\n{divisive}"],
        payout_line=payout_line,
    )
