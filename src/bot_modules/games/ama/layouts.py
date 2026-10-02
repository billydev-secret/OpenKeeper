"""Anonymous AMA's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``games_ama/embeds.py`` (deleted with this
conversion). Pure builders: primitives and a ``name_fn`` in, layout items
out, buttons carrying the ``ama_*`` custom ids the embed-era views used and
no callbacks — ``games/ama/cog.py`` binds those.

The cards:

- **The board** (paradigm 8, live board): one builder for the hot-seat
  and open-panel formats and for the empty lobby, where the embed era had
  three. Who the room can ask, the queue or the panel roster, and the
  progress bar; **🙋 Volunteer**, **Ask a Question** and **❓ Help**
  beneath. **Nothing host-only is on it** — Skip, New Hot Seat and End
  AMA live on the ephemeral host panel (Ben, 2026-09-30), and one ``-#``
  line points the host at ``/games host``.
- **The host panel** (paradigm 2, ephemeral): ⏭️ Skip and 🔄 New Hot Seat
  (hot-seat format only) and 🏁 End AMA.
- **The question card** (paradigm 9): the question for the seat, with
  💬 Reply and Pass beneath. It names its target exactly once: the raw
  ping when the seat is a cached member (the send allow-lists that one id,
  and the ping *is* the turn ping the embed carried in ``content=``, which
  a V2 message has no room for), a resolved name otherwise. The same
  builder draws the card's retired states — passed, ended, expired — as a
  ``-#`` line in place of the buttons, where the embed era read the
  message's own embed back and stamped a footer on it.
- **The Q + A card** (paradigm 7): the question card edited into the
  answer, signed by the answerer.
- **The screened DM** (paradigm 5, decision card): the question, who it is
  for and where, with ✅ Approve / ❌ Reject; edits into a receipt.
- **The asker's DM** (paradigm 7): "your question got a reply", with the
  jump link.
- **The recap** (paradigm 7): the session's numbers, the payout line.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.utils.names import resolved_name
from bot_modules.games.ama.logic import (
    AMA_FORMAT_PANEL,
    DEFAULT_QUESTIONS_PER_TURN,
    mode_label,
    question_count,
    remaining_questions_text,
)
from bot_modules.shared.embeds import take_to_fit

ICON = GAME_ICONS["ama"]
TITLE = GAME_NAMES["ama"]

VOLUNTEER_ID = "ama_volunteer"
ASK_ID = "ama_ask"
HELP_ID = "ama_htp"
REPLY_ID = "ama_reply"
PASS_ID = "ama_pass"
APPROVE_ID = "ama_screen_approve"
REJECT_ID = "ama_screen_reject"
#: The host panel's ids — new ones, not the board's old ``ama_skip`` /
#: ``ama_new_hs`` / ``ama_end``: the panel is an ephemeral message with its
#: own short-lived view.
HOST_SKIP_ID = "ama_host_skip"
HOST_NEW_SEAT_ID = "ama_host_new_seat"
HOST_END_ID = "ama_host_end"

VOLUNTEER_LABEL = "🙋 Volunteer"
ASK_LABEL = "Ask a Question"

#: The most a question or an answer may spend of a card, escaped. The
#: modals cap them at 500 and 1,000 characters; escaping can double that.
QUESTION_LIMIT = 1000
ANSWER_LIMIT = 2000

#: The most the queue line and the panel roster may spend. Neither has a
#: cap — anyone may volunteer — so each is fitted by whole names; the
#: embed's queue was joined with no cap at all.
NAMES_BUDGET = 1200

NameFn = Callable[[int], str]


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _quoted(text: str, limit: int) -> str:
    """Member-written text as a ``>`` block, escaped and fitted, every line
    prefixed so a multi-line entry stays inside the quote."""
    return "> " + escape_to_fit(text, limit).replace("\n", "\n> ")


def fit_names(names: list[str], *, sep: str = ", ", budget: int = NAMES_BUDGET) -> str:
    """Join already-escaped names inside ``budget``, dropping whole trailing
    names into ``…and N more`` — never a character slice, which could split
    an escape pair."""
    shown, remaining = take_to_fit(names, budget - 20, sep=sep)
    joined = sep.join(shown)
    if remaining:
        tail = f"…and {remaining} more"
        return f"{joined}{sep}{tail}" if joined else tail
    return joined


def progress_line(payload: dict[str, Any] | None) -> str | None:
    """``Questions · Answered · Passed`` over the answered bar, or ``None``
    before there is a payload to count."""
    if not payload:
        return None
    total_q = question_count(payload.get("questions", []))
    answered = payload.get("total_answered", 0)
    passed = payload.get("total_passed", 0)
    bar, pct = build_bar(answered, total_q)
    return (
        f"Questions: **{total_q}** · Answered: **{answered}** · Passed: **{passed}**\n"
        f"{bar} {pct} answered"
    )


# ── the board ────────────────────────────────────────────────────────────────


def build_board_layout(
    host_name: str,
    mode: str,
    *,
    game_format: str,
    hot_seat_name: str | None = None,
    questions_this_turn: int = 0,
    per_turn: int = DEFAULT_QUESTIONS_PER_TURN,
    queue: list[int] | None = None,
    panel: list[int] | None = None,
    name_fn: NameFn,
    payload: dict[str, Any] | None = None,
    closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The AMA's one board, for both formats and every state. **Paradigm 8
    — live board.**

    Hot-seat format: who is in the seat and how many questions their turn
    has left, or the prompt for a volunteer, then the queue. Open-panel
    format: the roster of panelists, or the prompt to join it. ``payload``
    adds the progress bar; the launch omits it, since a fresh game can only
    ever read zero. ``closed`` is the frame the recap leaves behind: no
    controls.
    """
    host = discord.utils.escape_markdown(host_name)
    panel_format = game_format == AMA_FORMAT_PANEL
    facts = [f"Host: **{host}**", f"Mode: **{mode_label(mode)}**"]
    if panel_format:
        names = [resolved_name(name_fn, uid) for uid in (panel or [])]
        lead = (
            "Ask **anyone on the panel** anything — questions are anonymous."
            if names else f"Tap **{VOLUNTEER_LABEL}** to join the panel and take anonymous questions."
        )
        roster = (
            f"### 🙋 Panel ({len(names)})\n" + fit_names([f"• {n}" for n in names], sep="\n")
            if names else "### 🙋 Panel\n-# Nobody yet"
        )
    else:
        if hot_seat_name is None:
            lead = "Who's taking the hot seat?"
            facts.append("Hot seat: **—**")
        else:
            seat = discord.utils.escape_markdown(hot_seat_name)
            lead = (
                f"Ask **{seat}** anything — questions are anonymous.\n"
                f"{remaining_questions_text(questions_this_turn, per_turn)}"
            )
            facts.append(f"Hot seat: **{seat}**")
        names = [resolved_name(name_fn, uid) for uid in (queue or [])]
        roster = (
            f"### 📋 Queue ({len(names)})\n" + fit_names(names, sep=" → ")
            if names else ""
        )
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} {TITLE}\n{lead}") if closed else discord.ui.Section(
            _text(f"## {ICON} {TITLE}\n{lead}"),
            accessory=round_layouts.grey("❓ Help", HELP_ID),
        ),
        _text(" · ".join(facts)),
    ]
    if roster:
        items.append(_text(roster))
    progress = progress_line(payload)
    if progress:
        items.extend([round_layouts.line(), _text(f"### 📊 Progress\n{progress}")])
    if closed:
        return items
    items.append(discord.ui.ActionRow(
        round_layouts.grey(VOLUNTEER_LABEL, VOLUNTEER_ID),
        round_layouts.grey(ASK_LABEL, ASK_ID),
    ))
    items.append(_text(f"-# {HOST_HINT}"))
    return items


def host_panel_handlers(game_format: str) -> dict[str, str]:
    """``custom_id`` → AMAView method for exactly the buttons
    :func:`build_host_panel_layout` draws for ``game_format``.

    Kept beside the builder so the two can't disagree: ``GameHostPanel``
    binds every id it is handed and raises on one the card doesn't carry,
    which is how an open-panel AMA's ``/games host`` crashed when it was
    handed Skip and New Hot Seat (ship review, 2026-10-01).
    """
    handlers = {}
    if game_format != AMA_FORMAT_PANEL:
        handlers[HOST_SKIP_ID] = "skip_hot_seat"
        handlers[HOST_NEW_SEAT_ID] = "new_hot_seat"
    handlers[HOST_END_ID] = "end_ama"
    return handlers


def build_host_panel_layout(game_format: str) -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    ⏭️ Skip and 🔄 New Hot Seat only make sense with a seat to rotate, so the
    open-panel format's panel carries End AMA alone.
    """
    buttons: list[discord.ui.Button[Any]] = []
    if game_format != AMA_FORMAT_PANEL:
        buttons.extend([
            round_layouts.grey("⏭️ Skip", HOST_SKIP_ID),
            round_layouts.grey("🔄 New Hot Seat", HOST_NEW_SEAT_ID),
        ])
    buttons.append(round_layouts.grey("🏁 End AMA", HOST_END_ID))
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb=(
            "Move the hot seat along, or end the AMA with its recap."
            if game_format != AMA_FORMAT_PANEL
            else "End the AMA with its recap."
        ),
        rows=[buttons],
    )


# ── the question card ────────────────────────────────────────────────────────

#: The retired states a question card can show in place of its buttons.
STATE_PASSED = "⏩ Passed"
STATE_ENDED = "AMA ended"
STATE_EXPIRED = "Expired after 7 days without an answer"


def build_question_layout(
    question_text: str,
    *,
    target_id: int | None = None,
    target_name: str | None = None,
    state: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """One anonymous question for the seat. **Paradigm 9 — game turn card.**

    Names the target once: the raw ``<@id>`` when ``target_id`` is a cached
    member — the send passes :func:`question_mentions` so it notifies them,
    which is the turn ping the embed carried in ``content=`` — or a plain
    ``target_name``. ``state`` is one of the ``STATE_*`` lines: the card
    keeps its question and loses its buttons, where the embed era fetched
    the message, took its embed and stamped a footer on it.
    """
    who = f"<@{target_id}>" if target_id else f"**{discord.utils.escape_markdown(target_name or 'the hot seat')}**"
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} Question\nFor {who}\n{_quoted(question_text, QUESTION_LIMIT)}"),
    ]
    if state:
        items.append(_text(f"-# {state}"))
    else:
        items.append(discord.ui.ActionRow(round_layouts.grey("💬 Reply", REPLY_ID), round_layouts.grey("Pass", PASS_ID)))
    return items


def question_mentions(target_id: int | None) -> discord.AllowedMentions:
    """The question card's allow-list: exactly the target while the card
    pings them, else nobody."""
    if target_id is None:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False, roles=False, replied_user=False,
        users=[discord.Object(id=target_id)],
    )


def build_answered_layout(
    question_text: str,
    answer_text: str,
    answerer_display_name: str,
) -> list[discord.ui.Item[Any]]:
    """The question card edited into the answer. **Paradigm 7 — notice.**
    Signed by the answerer, so the post stays anonymous on the asker's
    side."""
    return [
        _text(
            f"## {ICON} Question + Answer\n"
            f"**Q:**\n{_quoted(question_text, QUESTION_LIMIT)}\n"
            f"**A:**\n{_quoted(answer_text, ANSWER_LIMIT)}\n"
            f"-# — {discord.utils.escape_markdown(answerer_display_name)}"
        ),
    ]


# ── the DMs ──────────────────────────────────────────────────────────────────


def build_screened_dm_layout(
    question_text: str,
    target_name: str,
    channel_mention: str,  # embed-mention-ok: channel_mention
    *,
    receipt: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The host's DM for a screened question. **Paradigm 5 — decision
    card.** Carries the question itself — the buttons ride on this message,
    so it is the only place the host can read what they are approving.
    ``receipt`` (approved / rejected / the game ended) replaces the buttons
    once the host has acted, where the embed era edited the DM's
    ``content`` — which a V2 message has no room for."""
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} Screened Question\n"
            f"📨 For **{discord.utils.escape_markdown(target_name)}** in {channel_mention}:\n"
            f"{_quoted(question_text, QUESTION_LIMIT)}"
        ),
    ]
    if receipt:
        items.append(_text(receipt))
    else:
        items.extend([
            discord.ui.ActionRow(
                discord.ui.Button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id=APPROVE_ID),
                discord.ui.Button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id=REJECT_ID),
            ),
            _text("-# Approve to post it anonymously, or reject it. These buttons don't expire."),
        ])
    return items


def build_asker_dm_layout(
    channel_mention: str,  # embed-mention-ok: channel_mention
    *,
    answer_url: str | None = None,
    answer_count: int = 1,
) -> list[discord.ui.Item[Any]]:
    """The DM the asker receives when their question gets a reply.
    **Paradigm 7 — notice.**

    Batched (``ANSWER_BATCH_KIND``): ``answer_count`` above one is several
    of the asker's questions answered inside one window, and ``answer_url``
    then lands on the first of them. The link lands on the answer itself,
    without which the reader arrives at the bottom of a busy channel and has
    to hunt for their own question.
    """
    if answer_count > 1:
        line = f"{answer_count} of your anonymous questions in **{channel_mention}** got replies!"
        link_label = "Jump to the first answer"
    else:
        line = f"Your anonymous question in **{channel_mention}** got a reply!"
        link_label = "Jump to the answer"
    body = f"## 🔔 Answered\n{line}"
    if answer_url:
        body += f"\n[{link_label}]({answer_url})"
    return [_text(body)]


# ── the recap ────────────────────────────────────────────────────────────────


def build_recap_layout(
    mode: str,
    stats: dict[str, int],
    *,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The game-over card from a precomputed ``stats`` dict
    (``logic.compute_recap_stats``). **Paradigm 7 — notice.** One thanks,
    in the heading — the embed said it twice, in the description and the
    footer (embed filler review, 2026-09-03)."""
    total_q = stats.get("total_q", 0)
    total_answered = stats.get("total_answered", 0)
    total_passed = stats.get("total_passed", 0)
    rotations = stats.get("rotations", 0)
    unique_askers = stats.get("unique_askers", 0)
    bar, pct = build_bar(total_answered, total_q)
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} {TITLE} — Game Over\nThanks for playing! Here's how the session went."),
        round_layouts.line(),
        _text(
            "### 📊 Session Stats\n"
            f"**{total_q}** questions asked by **{unique_askers}** people\n"
            f"**{total_answered}** answered · **{total_passed}** passed\n"
            f"{bar} {pct} answered"
        ),
        _text(f"🔄 Hot seat rotations: **{rotations}** · 🎙️ Mode: **{mode_label(mode)}**"),
    ]
    if payout_line:
        items.append(_text(f"-# {payout_line}"))
    return items
