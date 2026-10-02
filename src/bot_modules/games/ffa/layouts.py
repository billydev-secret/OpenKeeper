"""Anonymous Truth or Dare's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for the two embed builders that lived in
``games/ffa/cog.py``. Pure builders: primitives in, layout items out,
buttons carrying the ``ffa_embed_*`` custom ids the embed-era view used and
no callbacks — the cog binds those.

Three cards:

- **The prompt card** (paradigm 8, live board): TRUTH or DARE, the prompt
  as a block quote, the running anonymous-reply count, and 🎭 Reply, 🎲 New
  Alias and ❓ Info — the three things anyone may press. **Next and Close
  are not on it** — everything host-only lives on the ephemeral host panel
  (Ben, 2026-09-30), and one ``-#`` line points the host at ``/games
  host``. The closed frame (the host's Close) keeps the prompt and its
  count and loses its buttons; the embed era stripped the view off.
- **The host panel** (paradigm 2, ephemeral): ⏭️ Next and 🏁 Close.
- **The recap** (paradigm 7): replies per prompt, the busiest prompt, the
  tallies. Names nobody — the replies were anonymous and so is the recap.

The embed era coloured a truth cool blue and a dare hot rose; the label
carries that now, and every card is bare (guide § No container, no accent).
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.games.constants import GAME_ICONS
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.shared.embeds import take_to_fit

ICON = GAME_ICONS["ffa"]
TITLE = "Anonymous Truth or Dare"

REPLY_ID = "ffa_embed_reply_anon"
ALIAS_ID = "ffa_embed_reply_super"
HELP_ID = "ffa_embed_help"
#: The host panel's ids — new ones, not the card's old ``ffa_embed_next`` /
#: ``ffa_embed_close``: the panel is an ephemeral message with its own
#: short-lived view.
HOST_NEXT_ID = "ffa_host_next"
HOST_CLOSE_ID = "ffa_host_close"

REPLY_LABEL = "🎭 Reply"
ALIAS_LABEL = "🎲 New Alias"
CLOSE_LABEL = "🏁 Close"

#: The most a prompt may spend of its card, escaped. A host-written prompt
#: takes whatever Discord allows; the bank's are short.
PROMPT_LIMIT = 2000
#: How many characters of a prompt the recap quotes per line, and what the
#: prompt list may spend in all — fitted by whole lines.
RECAP_PROMPT_CHARS = 90
PROMPTS_BUDGET = 2400

FOOTER = "Free For All"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _replies(count: int) -> str:
    return f"{count} anonymous {'reply' if count == 1 else 'replies'}"


# ── the prompt card ──────────────────────────────────────────────────────────


def build_prompt_layout(
    text: str, label: str, *, reply_count: int = 0, closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    """One prompt's card. **Paradigm 8 — live board.**

    ``label`` is ``TRUTH`` or ``DARE``. The prompt is a block quote, matching
    the confession look, escaped and fitted. The footer line flips to the
    running reply count once one has landed; the cog rebuilds the card on
    every reply. ``closed`` is the frame the host's Close leaves: the prompt
    and its count, no buttons.
    """
    quoted = "\n".join(f"> {line}" for line in escape_to_fit(text, PROMPT_LIMIT).split("\n"))
    if closed:
        status = f"{FOOTER} • closed • {_replies(reply_count)}"
    elif reply_count > 0:
        status = f"{FOOTER} • {_replies(reply_count)}"
    else:
        status = f"{FOOTER} • tap a button to reply anonymously"
    opening = _text(f"## {ICON} {label.title()}\n{quoted}")
    items: list[discord.ui.Item[Any]] = [
        opening if closed else discord.ui.Section(
            opening, accessory=round_layouts.grey("❓ Help", HELP_ID),
        ),
    ]
    if closed:
        items.append(_text(f"-# {status}"))
        return items
    items.append(discord.ui.ActionRow(
        round_layouts.grey(REPLY_LABEL, REPLY_ID),
        round_layouts.grey(ALIAS_LABEL, ALIAS_ID),
    ))
    items.append(_text(f"-# {status}\n-# {HOST_HINT}"))
    return items


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's ⏭️ Next and 🏁 Close. **Paradigm 2 — panel.**"""
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb="Pull another prompt, or close the game with its recap.",
        rows=[[
            round_layouts.grey(round_layouts.NEXT_LABEL, HOST_NEXT_ID),
            round_layouts.grey(CLOSE_LABEL, HOST_CLOSE_ID),
        ]],
    )


# ── the recap ────────────────────────────────────────────────────────────────


def _quoted(text: str) -> str:
    return escape_to_fit(text, RECAP_PROMPT_CHARS)


def build_recap_layout(
    summary: dict[str, Any], *, payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The host's close card from :func:`logic.recap_summary`. **Paradigm 7
    — notice.** Names nobody; the only member-shaped number is how many
    distinct people replied.
    """
    lines = [
        f"**{label.title()}** — {_quoted(text)} · {count} {'reply' if count == 1 else 'replies'}"
        for label, text, count in summary["per_prompt"]
    ]
    shown, remaining = take_to_fit(lines, PROMPTS_BUDGET, sep="\n")
    if remaining:
        shown.append(f"_…and {remaining} more_")
    sections = ["### Prompts\n" + ("\n".join(shown) or "—")]
    busiest = summary.get("busiest")
    if busiest is not None:
        label, text, count = busiest
        sections.append(f"### 🔥 Busiest Prompt\n{_quoted(text)} ({count} replies)")
    return round_layouts.build_recap_layout(
        icon=ICON,
        title=TITLE,
        facts=[
            f"Replies: **{summary['total_replies']}**",
            f"Repliers: **{len(summary['repliers'])}**",
        ],
        sections=sections,
        payout_line=payout_line,
    )
