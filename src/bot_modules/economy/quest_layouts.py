"""Components V2 card builders for economy quests — the ``/bank quests``
board, its detail/claim receipts, and the manager sign-off card.

The V2 replacement for the ``Embed(``-building code that used to live in
``quest_views.py``. Every function here is **pure**: primitives and a
``name_fn`` in, a list of ``discord.ui.Item`` out. No IO, no accent lookup,
no callbacks — ``quest_views.py``'s ``LayoutView`` subclasses add the items
in order and bind behaviour by ``find_item(id)`` or a persistent
``custom_id``.

Each builder's docstring names the paradigm it builds
(``docs/components_v2_style_guide.md`` § The paradigms).

Conventions this module follows throughout, matching the pilot
(``dm/perms/layouts.py``):

* **A card is bare** — no ``Container``, no accent colour.
* **A card's own heading is one glyph-led ``## Title`` line** in its first
  Text Display; ``###`` marks a section *inside* a card (the board's two
  quest groups) and nothing else.
* **A member is named through ``name_fn``**, never ``<@id>``.
* The board's short status glyphs (``_quest_line_status``) and the detail
  popup's long explainer (``QUEST_STATE_LABEL``) share one vocabulary on
  purpose — see the docstring on ``_quest_line_status`` below. They lived in
  separate modules before this conversion and drifted; keeping them in the
  same module is what keeps ✅ from meaning "claimable" in one place and
  "done" in the other.
"""

from __future__ import annotations

from typing import Any, Optional

import discord

from bot_modules.economy.approval_layouts import build_review_card_layout
from bot_modules.economy.leaderboard import bar_fill, progress_bar
from bot_modules.economy.view_helpers import unit as _unit
from bot_modules.economy.service import EconSettings
from bot_modules.shared.embeds import fit_lines, pad_cell as _pad
from bot_modules.shared.name_resolver import NameFn

# Member-facing explainer per quest state / trigger kind — shown in the
# per-quest detail card (the /bank quests list itself stays one line per
# quest; this is where the long form lives).
# Glyphs for the three live states stay in lock-step with the one-line
# board (_quest_line_status, below): 🔶 = your move / claimable,
# ⏳ = awaiting sign-off, ✅ = done. Sharing the vocabulary keeps ✅ from
# meaning "claimable" here and "done" on the board.
QUEST_STATE_LABEL = {
    "claimable": "🔶 Ready to claim",
    "pending": "⏳ Awaiting sign-off",
    "done": "✅ Completed this period",
    "trigger": "🗣️ Completes automatically when you say its trigger phrase",
    "photo_post": "📸 Completes automatically when you post a photo in the Photo Challenge channel",
    "party_game": "🎲 Completes automatically when you finish a party game",
    "duel": "⚔️ Completes automatically when you finish a 1v1 duel",
    "risky_roll": "🎰 Completes automatically when you take a Risky Roll dare",
    "casino_play": "🃏 Completes automatically when you place a casino bet",
    "auction_bid": "🔨 Completes automatically when you bid in an auction",
    "guess": "🕵️ Completes automatically when you play a Guess Who round",
    "voice_session": "🎙️ Completes automatically when you're active in voice chat",
    "qotd_reply": "📣 Completes automatically when you answer the Question of the Day",
    "starboard": "⭐ Completes automatically when a message of yours hits the starboard",
    "invite": "📨 Completes automatically when someone you invited joins",
    "boost": "🚀 Completes automatically when you boost the server",
    "bio_set": "📇 Completes automatically when you set up your bio",
    "birthday_set": "🎂 Completes automatically when you set your birthday",
    "media_post": "🖼️ Completes automatically when you post an image",
    "pen_pal": "💌 Completes automatically when you're matched with a Pen Pal",
    "message_sent": "💬 Completes automatically as you chat",
    "reply_sent": "↩️ Completes automatically when you reply to people",
    "reaction_given": "👍 Completes automatically when you react to people's messages",
    "game_win": "🏆 Completes automatically when you win a party game",
    "duel_win": "🥇 Completes automatically when you win a duel",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _reward_text(settings: EconSettings, reward: int) -> str:
    return f"{settings.currency_emoji} **{reward:,}** {_unit(settings, reward)}"


# ── the /bank quests board ────────────────────────────────────────────────
#
# The list render lives beside the details card it must stay consistent
# with: both name the same quest states, and QUEST_STATE_LABEL above is the
# long-form of the short glyph phrases _quest_line_status produces.

# The /bank quests board is two sections: the member's own board (daily/
# weekly board draws) and the guild-wide goals everyone moves together (the
# monthly goal + the weekly community goals — both shared counters, no
# self-claim). Event ("Anytime") quests aren't board-drawn and don't get a
# section here — they stay a surprise payout rather than a proactively
# listed menu; they're still claimable via the Detail select if one lands in
# a claimable state. Each section is one Text Display; within it, a cadence
# sub-label separates the groups when more than one is present.
_CADENCE_LABEL = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "community": "Weekly",
}
_QUEST_SECTIONS = (
    ("🧍 Your quests", ("daily", "weekly")),
    # Weekly community goals lead, the slower monthly goal anchors the foot —
    # same near-term-first order as the member's own board above.
    ("🌐 Community goals", ("community", "monthly")),
)

# The ``/bank quests`` list draws the same ``▰▱`` meter the detail card and
# login digest use, just narrower so a bar + reward still fit one line on
# mobile. One-shot quests keep a glyph phrase.
_QUEST_BAR_WIDTH = 8

#: Per-section character budget. The message-wide ceiling is 4,000 across
#: every Text Display (``core.layout.MAX_CHARS``); two sections at this cap
#: plus the heading and per-quest selects underneath leaves comfortable
#: headroom rather than needing the two sections to race each other for the
#: same pool, the way one shared budget would.
_SECTION_CHAR_BUDGET = 1500


def _quest_line_status(q: dict) -> str:
    """The status column: a progress bar for counted/community quests, else
    one short glyph phrase."""
    state = str(q.get("state") or "")
    if state == "community":
        return bar_fill(int(q["current"]), int(q["target"]), _QUEST_BAR_WIDTH)
    if state == "done":
        return "✅ done"
    if state == "pending":
        return "⏳ sign-off"
    if state == "claimable":
        return "🔶 claim below"
    if q.get("progress_target"):
        # code=False: this cell is padded and wrapped into the row's own code
        # span below, so it must not carry backticks of its own.
        return progress_bar(
            int(q["progress_current"]),
            int(q["progress_target"]),
            _QUEST_BAR_WIDTH,
            code=False,
        )
    return "☐ to do"


def _quest_line_reward(q: dict, settings: EconSettings) -> str:
    """The payment column: coins, optional XP, optional spotlight bolt.
    The XP suffix stays glyph-free — on phone widths the reward column
    hugs the wrap point, and a ⭐ was enough to push it onto its own line."""
    reward = f"{settings.currency_emoji} {int(q['reward']):,}"
    if q.get("reward_xp"):
        reward += f" +{int(q['reward_xp']):,}xp"
    if q.get("spotlight"):
        reward += " ⚡"
    return reward


# Emoji-presentation glyphs that appear in a status cell render ~2 monospace
# columns wide (unlike ☐/▰▱, which sit at ~1). Counting them as 2 when sizing
# the status column keeps the reward column that follows the code cell aligned.
_WIDE_STATUS_GLYPHS = ("✅", "⏳", "🔶")


def _status_disp_width(text: str) -> int:
    """Approximate monospace column width of a status cell for padding."""
    return len(text) + sum(text.count(g) for g in _WIDE_STATUS_GLYPHS)


def _quest_section_lines(
    cadences: tuple[str, ...],
    groups: dict[str, list[dict]],
    settings: EconSettings,
    width: int,
) -> list[str]:
    """Display lines for one board section — each present cadence's quests,
    one line apiece, with a bold cadence sub-label above them when the section
    spans more than one cadence (a single-cadence section needs no sub-label,
    the section's own ``###`` heading already names it).

    Title and status share one monospace code cell so their columns line up;
    the status column is padded to the section's widest status so the
    reward — which stays *outside* the backticks, emoji and all — starts at
    the same column on every row."""
    present = [c for c in cadences if groups.get(c)]
    show_labels = len(present) > 1
    rows = [(c, q, _quest_line_status(q)) for c in present for q in groups[c]]
    status_w = max((_status_disp_width(s) for _, _, s in rows), default=0)
    lines: list[str] = []
    seen: set[str] = set()
    for cadence, q, status in rows:
        if show_labels and cadence not in seen:
            lines.append(f"**{_CADENCE_LABEL[cadence]}**")
            seen.add(cadence)
        pad = " " * max(0, status_w - _status_disp_width(status))
        cell = f"{_pad(str(q['title']), width)}  {status}{pad}"
        lines.append(f"`{cell}` {_quest_line_reward(q, settings)}")
    return lines


def build_quest_board_layout(
    settings: EconSettings,
    quests_state: list[dict],
) -> list[discord.ui.Item[Any]]:
    """The ``/bank quests`` list. **Paradigm 6 — list with per-row actions**,
    where the "action" on a row is one of the selects
    ``QuestClaimView`` appends below the board (Detail / Claim / Reroll)
    rather than a per-row button: a button per quest would blow the
    40-component ceiling the moment a guild ran more than a handful of
    quests, and a select already caps this feature at 25 rows the same way a
    button-per-row page would need to.

    One line per quest — title | status | payment — split into two Text
    Display sections (the member's own board vs the guild-wide goals),
    unchanged from the embed fields this replaces. Descriptions and the
    how-it-completes explainers stay behind the Detail select so the list
    itself stays scannable.

    Returns the "nothing active" card unchanged when ``quests_state`` is
    empty — the caller sends this alone, with no selects underneath.
    """
    heading = f"## {settings.currency_emoji} Quests"
    if not quests_state:
        return [
            _text(f"{heading}\n_No active quests right now — check back soon!_")
        ]

    desc_bits = []
    if any(q.get("spotlight") for q in quests_state):
        desc_bits.append("⚡ Spotlight quests pay **double** this week!")
    desc_bits.append("Pick a quest from the menu below for its full story.")
    items: list[discord.ui.Item[Any]] = [
        _text(heading + "\n" + "\n".join(desc_bits))
    ]

    groups: dict[str, list[dict]] = {}
    for q in quests_state:
        groups.setdefault(str(q["qtype"]), []).append(q)
    width = min(max(len(str(q["title"])) for q in quests_state), 22)
    sections = [
        (section_heading, _quest_section_lines(cadences, groups, settings, width))
        for section_heading, cadences in _QUEST_SECTIONS
    ]
    first_section = True
    for section_heading, quest_lines in sections:
        if not quest_lines:
            continue
        # Defensive cap: a large quest_board_* size could still blow the
        # message-wide character budget.
        value = fit_lines(quest_lines, _SECTION_CHAR_BUDGET)
        shown = value.count("\n") + 1 if value else 0
        if shown < len(quest_lines):
            value += f"\n_…and {len(quest_lines) - shown} more_"
        if not first_section:
            items.append(
                discord.ui.Separator(
                    visible=True, spacing=discord.SeparatorSpacing.large
                )
            )
        first_section = False
        items.append(_text(f"### {section_heading}\n{value}"))
    return items


# ── sign-off card ────────────────────────────────────────────────────────


def build_signoff_card_layout(
    settings: EconSettings,
    *,
    claimant_id: int,
    name_fn: NameFn,
    quest_title: str,
    reward: int,
    criteria: str,
    deny_count: int,
    state: str,
    resolver_id: Optional[int] = None,
    deny_reason: Optional[str] = None,
    buttons: Optional["discord.ui.ActionRow[Any]"] = None,
) -> list[discord.ui.Item[Any]]:
    """Build the sign-off view of a claim in the given state.

    **Paradigm 5 — decision card, redrawn as its own receipt once resolved**
    (``approval_layouts.build_review_card_layout`` — the shape quest
    sign-off shares with any future paid-request review card). The board's
    Sign-Offs picker renders this as its ephemeral detail, and the
    resolved/refresh edit reuses it, so what a manager looks at always
    mirrors the claim's true state.

    Claimant and resolver are named through ``name_fn`` (``card_name_fn``
    covers both), never ``<@id>``, which shows digits to a manager who
    hasn't seen that member. ``buttons`` is the Approve/Deny row, passed in
    (rather than built here) because it carries the two persistent
    ``DynamicItem`` buttons — a layout builder returns components, not
    behaviour, and those two *are* the behaviour.
    """
    card_state = (
        "approved" if state == "paid"
        else "denied" if state in ("denied", "expired")
        else "pending"
    )
    body = (
        f"**{name_fn(claimant_id)}** completed **{quest_title}** for "
        f"{_reward_text(settings, reward)}."
    )
    quote = f"📋 {criteria}" if criteria else ""
    facts = (
        f"🚫 {deny_count} prior denial{'s' if deny_count != 1 else ''}"
        if deny_count > 0 else ""
    )
    resolved_by = ""
    if card_state == "approved" and resolver_id:
        resolved_by = f"Approved by **{name_fn(resolver_id)}**"
    elif card_state == "denied" and resolver_id:
        resolved_by = f"Denied by **{name_fn(resolver_id)}**"
    deny_reason_quote = ""
    if card_state == "denied" and deny_reason:
        deny_reason_quote = "\n".join(
            f"> {line}" for line in discord.utils.escape_markdown(deny_reason).splitlines()
        )
    return build_review_card_layout(
        state=card_state,
        pending_heading="📋 Quest Sign-Off Requested",
        approved_heading="✅ Quest Approved",
        denied_heading="❌ Quest Denied",
        body=body,
        quote=quote,
        resolved_by=resolved_by,
        deny_reason_quote=deny_reason_quote,
        facts=facts,
        buttons=buttons,
    )


# ── quest detail popup ──────────────────────────────────────────────────


def build_quest_detail_layout(
    settings: EconSettings,
    q: dict,
    *,
    scope_line: str = "",
) -> list[discord.ui.Item[Any]]:
    """One quest's full story, on demand. **Paradigm 7 — notice.**

    The /bank quests list is deliberately terse (one line per quest); this
    card carries the long form — description, how it completes, progress,
    and for a channel-scoped quest the channel it counts in
    (``scope_line``, pre-built by the caller: whether it is still worth
    showing is ``quests.scope_worth_showing``'s call, not this card's).
    """
    reward = int(q["reward"])
    reward_line = f"**{reward:,}** {_unit(settings, reward)}"
    if q.get("reward_xp"):
        reward_line += f" + ⭐ {int(q['reward_xp']):,} XP"
    reward_line += f" · {q['qtype']}"
    if q.get("spotlight"):
        reward_line += " · ⚡×2 this week"
    lines = [reward_line]
    if q.get("description"):
        lines.append(str(q["description"]))
    state = str(q.get("state") or "")
    if scope_line:
        lines.append(scope_line)
    if state == "community":
        lines.append(progress_bar(int(q["current"]), int(q["target"])))
    else:
        label = QUEST_STATE_LABEL.get(state, "")
        if label:
            lines.append(label)
        if q.get("progress_target") and state not in ("done", "pending"):
            lines.append(
                progress_bar(int(q["progress_current"]), int(q["progress_target"]))
            )
    return [_text(f"## {settings.currency_emoji} {q['title']}\n" + "\n".join(lines))]


# ── quest-complete receipt ──────────────────────────────────────────────


def build_quest_claimed_layout(
    settings: EconSettings,
    *,
    title: str,
    paid: int,
) -> list[discord.ui.Item[Any]]:
    """Instant-payout receipt. **Paradigm 7 — receipt.**

    The currency icon, when the guild has set one, rides as a Thumbnail
    beside the sentence — the embed's ``set_thumbnail`` slot, the one thing
    on this card worth a picture.
    """
    text = _text(
        f"## 🎉 Quest Complete!\n**{title}** — "
        f"{_reward_text(settings, paid)} added to your wallet."
    )
    if settings.currency_icon_url:
        return [
            discord.ui.Section(
                text, accessory=discord.ui.Thumbnail(settings.currency_icon_url)
            )
        ]
    return [text]
