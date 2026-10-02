"""The ``/info`` card, as a Components V2 layout. Plain values in, items out.

One card, two doors: the ``/info`` command and the Server Features hub's
ℹ️ Your Info row both render it through ``MemberInfoCog.open_panel``.

Deliberately parallel to ``bot_modules.moderation.jail.embeds.build_modinfo_embed`` in
content and order, so the two read as the same card seen from two sides —
but it carries none of the mod-only fields, and the omissions are the point:

* **No watch-list count.** ``/modinfo`` shows how many mods are watching a
  member. Telling the member that is telling the target of an investigation
  they are under one.
* **No warnings, jail history or tickets.** Their own record is not shown
  here (an explicit product decision, 2026-08-22); if that ever changes it
  changes here, not by widening what the cog fetches.

Shape (``docs/components_v2_style_guide.md``): bare, no container — an
ephemeral card is one member's working surface. The member's avatar is the
``Thumbnail`` beside the heading, a line between each group, and each opt-in
is a ``Section`` with its one-word button beside it. The buttons carry
numeric ids (:data:`OPTIN_BUTTON_ID_BASE` + the row's index); the view in
``member_info.views`` binds them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

from bot_modules.core import meters
from bot_modules.member_info.logic import (
    AccountFacts,
    OptInRow,
    displayable_roles,
    level_progress,
    xp_to_next_level,
)
from bot_modules.shared.embeds import (
    EMBED_FIELD_LIMIT,
    fit_lines,
    xp_breakdown_parts,
)

#: The card's heading, also the hub row's name — one name per thing.
INFO_TITLE = "ℹ️ Your Info"

#: Transient button ids: the opt-in row at index ``i`` carries ``BASE + i``.
OPTIN_BUTTON_ID_BASE = 100

#: Cells in the level bar. Ten reads as tenths at a glance.
LEVEL_BAR_WIDTH = 10

CHART_ALT = "Your XP over the last 30 days, by source."

#: What each text section is held to. Not a Discord limit on a layout (the
#: card's limit is 4,000 characters across everything), but the per-section
#: share that keeps the fullest card inside it — the roles line alone can
#: otherwise run to forty 100-character names.
SECTION_LIMIT = EMBED_FIELD_LIMIT


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large)


def _account_text(facts: AccountFacts) -> str:
    # Relative, not a date: how long someone has been here is the question
    # this line answers. The client keeps it current and shows the full date
    # on hover, so nothing is lost and a card never goes stale.
    lines = [f"Joined: <t:{facts.joined_ts}:R>"] if facts.joined_ts else []
    lines.append(f"Account: <t:{facts.created_ts}:D> ({facts.account_age_days:,}d)")
    return "\n".join(lines)


def _level_text(facts: AccountFacts) -> str:
    """Level, a bar through it, what's left, the total and where that ranks.

    The bar is ``core.meters`` inside a code span (guide § Tables, bars):
    the style guide's preferred emoji meter set has not been uploaded, and
    a text bar is its stated fallback.
    """
    if facts.level is None:
        return "### ⭐ Level\nNo XP yet — chat a little and it starts counting."
    lines = [f"### ⭐ Level {facts.level}"]
    progress = level_progress(facts.total_xp, facts.level_floor_xp, facts.next_level_xp)
    remaining = xp_to_next_level(facts.total_xp, facts.next_level_xp)
    to_next = (
        f"{remaining:,} XP to Level {facts.level + 1}" if remaining is not None else ""
    )
    if progress is not None:
        bar = meters.fill(progress, 1.0, LEVEL_BAR_WIDTH)
        # Truncated, not rounded: "100%" beside "12 XP to Level 8" contradicts.
        pct = meters.mono(f"{bar} {int(progress * 100)}%")
        lines.append(f"{pct} · {to_next}" if to_next else pct)
    elif to_next:
        lines.append(to_next)
    total = f"{facts.total_xp:,.0f} XP"
    if facts.xp_rank is not None:
        rank, of = facts.xp_rank
        total += f" · Rank #{rank:,} of {of:,}"
    lines.append(total)
    parts = xp_breakdown_parts(dict(facts.xp_by_source))
    if parts:
        lines.append("-# " + " · ".join(parts))
    return fit_lines(lines, SECTION_LIMIT)


def _roles_text(facts: AccountFacts) -> str:
    """Roles as one separated line, capped by *length* as well as by count.

    Discord allows 100-character role names, so twelve of them overran the
    old embed field and Discord rejected the whole card — past the command's
    defer(), a card that never arrives. The count cap alone is not enough;
    names are dropped until the line fits, and the overflow counter absorbs
    whatever went.
    """
    heading = "### 🎭 Roles\n"
    names, overflow = displayable_roles(facts.role_names)
    if not names:
        return heading + "None yet."

    def render(kept: list[str], dropped: int) -> str:
        text = " · ".join(kept)
        return f"{text} · *+{dropped} more*" if dropped else text

    while names and len(render(names, overflow)) > SECTION_LIMIT:
        names.pop()
        overflow += 1
    return heading + (render(names, overflow) if names else f"*{overflow} roles*")


def _activity_text(facts: AccountFacts) -> str:
    """Last seen, then the busiest nameable channels.

    The empty case is split in two on purpose. The channel list is filtered to
    what the member can currently open, and some rows can never be resolved:
    Discord evicts a thread from the guild cache the moment it archives (which
    it does after 24h–1 week of quiet), and ``processed_messages`` stores only
    the thread's own id — there is no parent to fall back to. So a member whose
    month was spent in threads that have since archived has a real message
    count and nothing nameable to attribute it to.

    Printing "No messages recorded" there would have the section contradict
    its own heading, which counts those very messages. Saying we can't name
    the places is both true and non-leaking.
    """
    last_seen = f"<t:{int(facts.last_seen_ts)}:R>" if facts.last_seen_ts else "—"
    lines = [f"Last seen: {last_seen}"]
    if facts.top_channels:
        lines += [
            f"<#{channel_id}> — {count:,} msgs" for channel_id, count in facts.top_channels
        ]
    elif facts.msgs_30d:
        lines.append(
            "Posted in threads or channels we can't list here "
            "(archived threads, or channels you no longer have access to)."
        )
    else:
        lines.append("No messages recorded in the last 30 days.")
    return f"### 💬 Activity — {facts.msgs_30d:,} msgs (30d)\n" + fit_lines(
        lines, SECTION_LIMIT
    )


def _optin_row(index: int, row: OptInRow) -> discord.ui.Item[Any]:
    text = f"**{row.emoji} {row.label}**\n-# {row.text}"
    if not row.has_action:
        # Configured but its cog isn't loaded: the status is true and worth
        # showing, but a button that would do nothing is not offered.
        return _text(text)
    return discord.ui.Section(
        _text(text),
        accessory=discord.ui.Button(
            label=row.action_label,
            style=discord.ButtonStyle.secondary,
            id=OPTIN_BUTTON_ID_BASE + index,
        ),
    )


def build_member_info_layout(
    *,
    display_name: str,
    avatar_url: str | None,
    facts: AccountFacts,
    optin_rows: Sequence[OptInRow],
    wallet_line: str = "",
    wallet_extra: Sequence[str] = (),
    help_lines: Sequence[str] = (),
    chart_filename: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The member's own card. **Paradigm 2 — settings panel** (ephemeral).

    ``wallet_line`` is empty when the guild's economy is disabled — the whole
    section then vanishes rather than rendering a zero balance for a currency
    that does not exist here. ``chart_filename`` names the attached activity
    chart; ``None`` renders the card without it.

    All buttons grey: Join, Leave and Open are none of them a destructive
    press or a decision's yes (guide § Anatomy — colour is a meaning).
    """
    head = _text(f"## {INFO_TITLE}\n**{discord.utils.escape_markdown(display_name)}**")
    account = _text(_account_text(facts))
    items: list[discord.ui.Item[Any]] = [
        discord.ui.Section(
            head,
            account,
            accessory=discord.ui.Thumbnail(avatar_url, description="Your avatar"),
        )
        if avatar_url
        else discord.ui.TextDisplay(f"{head.content}\n{account.content}"),
        _line(),
        _text(_level_text(facts)),
    ]
    if chart_filename:
        items.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media=f"attachment://{chart_filename}", description=CHART_ALT
                )
            )
        )
    items += [_line(), _text(_activity_text(facts)), _text(_roles_text(facts))]
    if wallet_line:
        items += [
            _line(),
            _text("### 💰 Wallet\n" + fit_lines([wallet_line, *wallet_extra], SECTION_LIMIT)),
        ]
    if optin_rows:
        items += [_line(), _text("### 🔔 Your Opt-Ins")]
        items += [_optin_row(i, row) for i, row in enumerate(optin_rows)]
    if help_lines:
        items.append(_text("\n".join(f"-# {line}" for line in help_lines)))
    return items
