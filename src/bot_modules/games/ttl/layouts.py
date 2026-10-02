"""Two Truths and a Lie's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_ttl/embeds.py`` (deleted with this
conversion). Pure builders: primitives and pre-resolved names in, layout
items out, buttons carrying the ``ttl_*`` custom ids the embed-era views
used and no callbacks — ``games/ttl/cog.py`` binds those.

Four cards:

- **The lobby** (paradigm 8, live board): the prompt, how the game goes,
  the players who have submitted, **Submit Statements** with Help beside
  the title, and the small lobby strip. **Start Guessing is not on it** —
  everything host-only lives on the ephemeral host panel (Ben,
  2026-09-30), and one ``-#`` line points the host at ``/games host``. The
  roster is rebuilt from the payload on every submission; the embed era
  read the Players field back off the message and patched it in place.
- **The guess card** (paradigm 9): the subject's three statements with a
  live meter each, 1️⃣ 2️⃣ 3️⃣ under them and **Join / Edit** beneath. The
  closed frame is the round's record — the final meters, no controls —
  and the reveal posts after it as its own card. ⏭️ Next is on the panel.
- **The reveal** (paradigm 7): the lie, and who was right and who was
  fooled, fitted by whole names.
- **The recap** (paradigm 7): Best Liar, Open Book, Best Guesser. A
  winner who is still a member is named as a ping — the send allow-lists
  exactly those ids, which is the winner ping the embed carried in
  ``content=`` (a V2 message has none) — and anyone else by resolved name,
  never both (guide § Mentions and names).
- **The host panel** (paradigm 2, ephemeral): Start Guessing and ⏭️ Next
  on one card for the whole game; a press out of its phase answers rather
  than acting.

Members are named through ``name_fn`` / pre-resolved names and a miss
reads ``User <id>`` (``games/utils/names``); the embed printed a bare id
for an uncached member.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.utils.names import resolved_name, unknown
from bot_modules.games.utils.round_pacing import timer_field_value

ICON = GAME_ICONS["ttl"]
TITLE = GAME_NAMES["ttl"]

SUBMIT_ID = "ttl_submit"
HELP_ID = "ttl_htp"
VOTE_IDS = ["ttl_v1", "ttl_v2", "ttl_v3"]
JOIN_ID = "ttl_join"
#: The host panel's ids — new ones, not the boards' old ``ttl_start`` /
#: ``ttl_next``: the panel is an ephemeral message with its own short-lived
#: view.
HOST_START_ID = "ttl_host_start"
HOST_NEXT_ID = "ttl_host_next"

SUBMIT_LABEL = "Submit Statements"
START_LABEL = "Start Guessing"
JOIN_LABEL = "Join / Edit"
#: Number glyphs for statement 1/2/3, shared by the guess card, the reveal
#: and the vote buttons.
NUM_GLYPHS: tuple[str, str, str] = ("1️⃣", "2️⃣", "3️⃣")

#: The most a prompt or a statement may spend of a card, escaped. The
#: modal caps a statement at 200 characters; the slash prompt takes
#: whatever Discord allows.
PROMPT_LIMIT = 600
STATEMENT_LIMIT = 400
#: What the roster and each of the reveal's two name lists may spend.
#: Anyone in the channel may submit or vote, so none has a cap; each is
#: fitted by whole names, which the embed fields already were.
ROSTER_BUDGET = 1500
VOTERS_BUDGET = 900
WINNERS_BUDGET = 500

NameFn = Callable[[int], str]

LOBBY_DESCRIPTION = (
    "Submit your three statements — two true, one lie.\n"
    "When everyone's ready, the host will start the guessing!"
)
GUESSING_UNDERWAY = "🎯 Guessing is underway — **Join / Edit** on the round card still gets you in."


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _prompt_line(prompt: str | None) -> str | None:
    return f"**Prompt:** {escape_to_fit(prompt, PROMPT_LIMIT)}" if prompt else None


# ── the lobby ────────────────────────────────────────────────────────────────


def build_lobby_layout(
    prompt: str | None,
    players: Sequence[str],
    *,
    closed: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The submission lobby posted by ``/games play twotruths``. **Paradigm
    8 — live board.**

    ``prompt`` is rendered above the explanation so the submission modal and
    the lobby share the same context. ``players`` are the submitters' display
    names — escaped here and fitted by whole names. ``closed`` is the frame
    left once guessing starts: no Submit button, a line saying where a late
    joiner goes.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    if prompt:
        lines.append(_prompt_line(prompt) or "")
    lines.append(LOBBY_DESCRIPTION)
    roster = round_layouts.fit_names(
        [discord.utils.escape_markdown(n) for n in players], ROSTER_BUDGET,
    ) if players else "-# Nobody yet"
    body = f"### Players ({len(players)})\n{roster}"
    body += f"\n{GUESSING_UNDERWAY}" if closed else f"\n-# {HOST_HINT}"
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
    """The host's Start Guessing and ⏭️ Next, on an ephemeral card.
    **Paradigm 2 — panel.** One card for the whole game, since the lobby
    turns into the guessing on its own once started; a press out of its
    phase answers plainly."""
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb=(
            "Start the guessing once three have submitted; during a round, "
            "move on to the next subject."
        ),
        rows=[[
            round_layouts.grey(START_LABEL, HOST_START_ID),
            round_layouts.grey(round_layouts.NEXT_LABEL, HOST_NEXT_ID),
        ]],
    )


# ── the guess card ───────────────────────────────────────────────────────────


def _statement_lines(statements: Sequence[str], votes: dict[int, int]) -> str:
    counts = [0, 0, 0]
    for v in votes.values():
        if 0 <= v < 3:
            counts[v] += 1
    total = sum(counts)
    lines = []
    for i, stmt in enumerate(statements[:3]):
        bar, pct = build_bar(counts[i], total)
        lines.append(f"{NUM_GLYPHS[i]} {bar} {pct} ({counts[i]})\n\"{escape_to_fit(stmt, STATEMENT_LIMIT)}\"")
    return "\n".join(lines)


def build_guess_layout(
    subject_name: str,
    statements: Sequence[str],
    votes: dict[int, int],
    *,
    closed: bool = False,
    prompt: str | None = None,
    advance_at: int | None = None,
) -> list[discord.ui.Item[Any]]:
    """One subject's round, open or over. **Paradigm 9 — game turn card.**

    ``subject_name`` is the round's subject, raw — escaped here. ``prompt``
    is repeated on every round so a mid-game joiner (who never saw the
    lobby) still knows what the statements answer. ``advance_at`` is the
    live countdown of a timed round while it runs. Open, the three vote
    buttons sit under the statements with Join / Edit beneath; ⏭️ Next is
    the host's and lives on the panel. Closed, the card is the round's
    record: the final meters and no controls — the reveal follows it.
    """
    safe_subject = discord.utils.escape_markdown(subject_name)
    head = f"## {ICON} Reveal — {safe_subject}" if closed else f"## {ICON} Guess the Lie — {safe_subject}'s Turn"
    facts = [line for line in (_prompt_line(prompt),) if line]
    if advance_at and not closed:
        facts.append(f"⏱️ {timer_field_value(advance_at)}")
    opening = _text("\n".join([head, *facts]))
    items: list[discord.ui.Item[Any]] = [
        opening if closed else discord.ui.Section(
            opening, accessory=round_layouts.grey("❓ Help", HELP_ID),
        ),
        round_layouts.line(),
        _text(_statement_lines(statements, votes)),
    ]
    if closed:
        return items
    items.extend([
        discord.ui.ActionRow(*[
            round_layouts.grey(glyph, custom_id) for glyph, custom_id in zip(NUM_GLYPHS, VOTE_IDS)
        ]),
        discord.ui.ActionRow(round_layouts.grey(JOIN_LABEL, JOIN_ID)),
        _text(f"-# {HOST_HINT}"),
    ])
    return items


# ── the reveal ───────────────────────────────────────────────────────────────


def fit_voters(names: Iterable[str]) -> str:
    """Comma-join raw display names, escaped, inside the reveal's budget."""
    escaped = [discord.utils.escape_markdown(n) for n in names]
    return round_layouts.fit_names(escaped, VOTERS_BUDGET) if escaped else "—"


def build_reveal_layout(
    subject_name: str,
    statements: Sequence[str],
    lie_index: int,
    correct_names: Iterable[str],
    fooled_names: Iterable[str],
) -> list[discord.ui.Item[Any]]:
    """The post-round reveal: the lie, and who saw through it. **Paradigm
    7 — notice.** Names arrive raw (``names.plain_name``) and are escaped here.
    """
    correct = list(correct_names)
    fooled = list(fooled_names)
    return [
        _text(
            f"## {ICON} Reveal — {discord.utils.escape_markdown(subject_name)}\n"
            f"The lie was {NUM_GLYPHS[lie_index]}: "
            f"\"{escape_to_fit(statements[lie_index], STATEMENT_LIMIT)}\" ✅"
        ),
        round_layouts.line(),
        _text(
            f"### 🎯 Correct ({len(correct)})\n{fit_voters(correct)}\n"
            f"### ❌ Fooled ({len(fooled)})\n{fit_voters(fooled)}"
        ),
    ]


# ── the recap ────────────────────────────────────────────────────────────────


def winner_ids(stats: dict[str, Any]) -> list[int]:
    """Every uid the recap names, once, in the order the cards list them."""
    out: list[int] = []
    for key in ("best_liar", "most_honest", "best_guesser"):
        for uid in stats.get(key, []):
            try:
                n = int(uid)
            except (TypeError, ValueError):
                continue
            if n not in out:
                out.append(n)
    return out


def build_recap_layout(
    stats: dict[str, Any],
    *,
    name_fn: NameFn = unknown,
    pingable: Iterable[int] = (),
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The final-results card from :func:`logic.compute_recap_winners`'s
    ``stats``. **Paradigm 7 — notice.**

    A winner in ``pingable`` (a cached member, per the cog) is named as a
    raw mention — the send passes an allow-list of exactly those ids, so
    this *is* the winner ping — and anyone else through ``name_fn`` (a miss
    reads ``User <id>``). One or the other, never both. An award with no
    winner is omitted.
    """
    pings = set(pingable)

    def _who(uids: list[str]) -> str:
        names = []
        for raw in uids:
            try:
                uid = int(raw)
            except (TypeError, ValueError):
                names.append(unknown(raw))
                continue
            names.append(f"<@{uid}>" if uid in pings else resolved_name(name_fn, uid))  # embed-mention-ok: winner ping, allow-listed at the send
        return round_layouts.fit_names(names, WINNERS_BUDGET)

    sections: list[str] = []
    if stats.get("best_liar"):
        sections.append(f"🤥 **Best Liar** — {_who(stats['best_liar'])} ({stats.get('most_fooled_count', 0)} fooled)")
    if stats.get("most_honest"):
        least = stats.get("least_fooled_count", 0)
        note = "fooled no one" if least == 0 else f"fooled only {least}"
        sections.append(f"🪞 **Open Book** — {_who(stats['most_honest'])} ({note})")
    if stats.get("best_guesser"):
        sections.append(f"🎯 **Best Guesser** — {_who(stats['best_guesser'])} ({stats.get('max_correct', 0)} correct)")
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        lead=None if sections else "Nobody was fooled and nobody guessed — no awards this time.",
        sections=["\n".join(sections)] if sections else [],
        payout_line=payout_line,
    )
