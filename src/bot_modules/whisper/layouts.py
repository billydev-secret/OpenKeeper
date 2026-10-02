"""Whisper's Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for ``whisper/embeds.py`` (deleted with this conversion,
2026-09-30) and for the four embeds the cog built inline. Every builder is
**pure**: a ``Whisper`` / ``WhisperReply`` row, primitives and a ``name_fn``
in, a list of ``discord.ui.Item`` out. No IO, no accent, no callbacks — the
persistent buttons carry the ``whisper:<verb>:<id>`` custom ids the
``discord.ui.DynamicItem`` classes in ``whisper/cog.py`` already match by
regex, and the ephemeral cards' buttons carry stable ids the cog binds with
``core.layout.bind``.

Each builder's docstring names the paradigm it builds. The cards:

- **The launcher** (paradigm 1, hub): the sticky poster at the foot of the
  feed channel. Title with a grey Help beside it (the guild's ``whisper``
  doc, read at click time), one sentence, a line, then a row per entry
  point — Send, Inbox, Sent — each with its grey button. A hub that had
  drawn no art before its conversion wears the **small lobby strip** with
  "Whisper" drawn between the leaves (guide § Media), so it sits in
  ``LOBBY_ART_BUILDERS``, not ``HUB_BANNER_BUILDERS``.
- **The feed notices** (paradigm 7): a whisper was sent, one was shared,
  one was solved. The recipient is named once, resolved.
- **The whisper DM** (paradigm 5, decision card): the message as a quote,
  a status line, then Guess / Share / Reply / Delete as the state allows
  (``logic.dm_action_buttons``). The same builder redraws the card after
  every press, so a DM sent as an embed before the conversion is rebuilt
  whole as a V2 card the first time one of its buttons is used.
- **The reply DM** and **the sender's guess digest** (paradigm 7).
- **The inbox** (paradigm 6): a list of ten rows a page, each with an
  **Open** beside it, and the opened whisper as its own pane with the
  actions and a **Back** (Ben, 2026-09-30: "open the whisper in a new pane,
  have a back button to get back to the list").
- **The delete confirm** (paradigm 10): Delete is destructive, so it is red
  and asks first (Ben, 2026-09-30). Report opens a form and destroys
  nothing, so it went grey at the same time.
- **The guess pickers and their results** (paradigm 9, editing into 7).
- **Opt-in** (paradigm 5) and its receipts, and the **mod-log notices**.

Member-written text — the whisper, a reply, a report reason — is always a
``>`` quote block, escaped: unescaped, a sender could write a ``## Whisper
Solved`` heading that reads as the bot's own voice.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.docs.help_card import build_doc_help_layout
from bot_modules.shared.name_resolver import NameFn
from bot_modules.whisper.logic import (
    dm_action_buttons,
    format_time_ago,
    guess_line,
    inbox_action_buttons,
    inbox_footer,
    preview,
    status_pill,
)
from bot_modules.whisper.models import Whisper, WhisperReply
from bot_modules.whisper.service import GuessOutcome

ICON = "📬"
TITLE = "Whisper"

#: The launcher's Help reads this doc key from the dashboard's Docs panel.
#: Renaming the doc disconnects the button (guide § Controls).
HELP_DOC_KEY = "whisper"
HELP_MISSING = (
    "Nobody has written the Whisper guide for this server yet — "
    "ask a moderator."
)

# ── Persistent custom ids ──────────────────────────────────────────────
#
# The launcher's three ids are the ones the embed-era ``WhisperFeedView``
# used, so a launcher posted before the conversion keeps answering after
# it; Help is new. The per-whisper templates are matched by regex in
# ``whisper/cog.py``'s ``DynamicItem`` classes.

SEND_ID = "whisper:send"
INBOX_ID = "whisper:check"
SENT_ID = "whisper:check_sent"
HELP_ID = "whisper:help"

GUESS_ID_TEMPLATE = "whisper:guess:{whisper_id}"
SHARE_ID_TEMPLATE = "whisper:share:{whisper_id}"
REPLY_ID_TEMPLATE = "whisper:reply:{whisper_id}"
DELETE_ID_TEMPLATE = "whisper:delete:{whisper_id}"
REPORT_ID_TEMPLATE = "whisper:report:{whisper_id}"
REPORT_REPLY_ID_TEMPLATE = "whisper:report_reply:{reply_id}"

# ── Ephemeral-card ids (bound per view, never registered) ──────────────

INBOX_OPEN_ID_TEMPLATE = "whisper_inbox:open:{whisper_id}"
INBOX_PREV_ID = "whisper_inbox:prev"
INBOX_NEXT_ID = "whisper_inbox:next"
INBOX_FILTER_ID = "whisper_inbox:filter"
INBOX_CLEAR_ID = "whisper_inbox:clear"
INBOX_BACK_ID = "whisper_inbox:back"
INBOX_ACTION_IDS: dict[str, str] = {
    "guess": "whisper_inbox:guess",
    "share": "whisper_inbox:share",
    "reply": "whisper_inbox:reply",
    "report": "whisper_inbox:report",
    "delete": "whisper_inbox:delete",
}

DELETE_CONFIRM_ID = "whisper_delete:yes"
DELETE_CANCEL_ID = "whisper_delete:cancel"

GUESS_PICK_ID = "whisper_guess:pick"
GUESS_PREV_ID = "whisper_guess:prev"
GUESS_NEXT_ID = "whisper_guess:next"
GUESS_FILTER_ID = "whisper_guess:filter"
GUESS_CLEAR_ID = "whisper_guess:clear"

OPTIN_CONFIRM_ID = "whisper_optin:confirm"
OPTIN_CANCEL_ID = "whisper_optin:cancel"

#: Ten rows a page: a Section with its button is three components, and ten
#: of them plus the heading, the subtext and a four-button bottom row is
#: 37 of the 40 (guide § The budget).
INBOX_PAGE_SIZE = 10

#: A row beside a button keeps ~30 columns on a phone (guide § Screen use);
#: the preview under a row is subtext and gets the same.
ROW_PREVIEW_CHARS = 30

#: The original whisper quoted on a reply DM is clipped here — the reader
#: already has the whole thing on the whisper's own card.
REPLY_QUOTE_CHARS = 200

ACTION_LABELS: dict[str, str] = {
    "guess": "Guess",
    "share": "Share",
    "reply": "Reply",
    "report": "Report",
    "delete": "Delete",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line(
    spacing: discord.SeparatorSpacing = discord.SeparatorSpacing.large,
) -> discord.ui.Separator[Any]:
    return discord.ui.Separator(spacing=spacing)


def _button(
    label: str,
    custom_id: str,
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label=label, style=style, custom_id=custom_id, disabled=disabled,
    )


def _quote(text: str) -> str:
    """``text`` as a markdown quote block, every line prefixed and escaped
    (``dm/perms/layouts.py``'s ``_quote``)."""
    escaped = discord.utils.escape_markdown(text.strip())
    return "\n".join(f"> {line}" for line in escaped.splitlines()) or "> "


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _action_button(key: str) -> discord.ui.Button[Any]:
    """One inbox action. Grey, except Delete: red is for a destructive
    press, and the confirm card stands behind it (guide § Controls)."""
    style = (
        discord.ButtonStyle.danger if key == "delete"
        else discord.ButtonStyle.secondary
    )
    return _button(ACTION_LABELS[key], INBOX_ACTION_IDS[key], style=style)


# ── 1. The launcher ──────────────────────────────────────────────────────


LAUNCHER_BLURB = (
    "Anonymous messages with a guessing game — "
    "whoever gets one has a few guesses at who sent it."
)
LAUNCHER_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Send someone a whisper.", "Send", SEND_ID),
    ("Whispers sent to you.", "Inbox", INBOX_ID),
    ("Whispers you've sent.", "Sent", SENT_ID),
)
LAUNCHER_FOOTNOTE = "-# `/whisper optin` to join · `/whisper optout` to leave"


def build_launcher_layout(
    *,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The feed channel's sticky launcher. **Paradigm 1 — hub panel.**

    One kind of row — every entry point opens something of the member's
    own — so no group headings and no divider between rows (guide
    § Controls); one line under the title block says where the card's own
    text ends. ``header_url`` / ``footer_url`` / ``title_in_header`` are the
    guild's lobby art with "Whisper" drawn into the strip
    (``panel_title_service.with_lobby_title``); with the title drawn the
    ``##`` line is dropped, and with no header nothing draws.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lead = LAUNCHER_BLURB if titled else f"## {ICON} {TITLE}\n{LAUNCHER_BLURB}"
    items: list[discord.ui.Item[Any]] = [
        *top,
        discord.ui.Section(_text(lead), accessory=_button("Help", HELP_ID)),
        _line(),
    ]
    for sentence, label, custom_id in LAUNCHER_ROWS:
        items.append(discord.ui.Section(_text(sentence), accessory=_button(label, custom_id)))
    items.append(_text(LAUNCHER_FOOTNOTE))
    return [*items, *bottom]


def build_help_layout(body_md: str | None) -> list[discord.ui.Item[Any]]:
    """The launcher's Help answer. **Paradigm 7 — notice.** The guild's
    ``whisper`` doc as an admin last saved it, read at click time."""
    return build_doc_help_layout(
        body_md, glyph=ICON, fallback_title=TITLE, missing_text=HELP_MISSING,
    )


# ── 7. Feed notices ──────────────────────────────────────────────────────


def build_send_feed_layout(target_id: int, name_fn: NameFn) -> list[discord.ui.Item[Any]]:
    """The feed's "someone sent X a whisper" post. **Paradigm 7 — notice.**
    Name only, no content; the recipient is already notified by the DM."""
    return [_text(
        f"## {ICON} Someone Sent a Whisper\n"
        f"Someone sent **{name_fn(target_id)}** an anonymous message."
    )]


def build_share_feed_layout(whisper: Whisper, name_fn: NameFn) -> list[discord.ui.Item[Any]]:
    """The feed post that replaces the announcement once the recipient
    shares. **Paradigm 7 — notice.** The whisper is quoted, escaped."""
    return [_text(
        f"## {ICON} A Fresh Whisper Was Shared\n"
        f"**{name_fn(whisper.target_id)}** shared what someone sent them:\n"
        f"{_quote(whisper.message)}"
    )]


def build_solved_feed_layout(target_id: int, name_fn: NameFn) -> list[discord.ui.Item[Any]]:
    """The feed's "solved" line. **Paradigm 7 — notice.** Was a bare
    ``✅ <@id> solved the whisper!`` content line; the name is resolved now
    so a reader without the member cached still sees a name."""
    return [_text(
        f"## ✅ Whisper Solved\n"
        f"**{name_fn(target_id)}** worked out who sent their whisper."
    )]


# ── 5. The whisper DM ────────────────────────────────────────────────────


GUESS_HINT = "Guess who — a wrong guess is gone for good."


def dm_buttons(whisper: Whisper, *, now: float | None = None) -> list[discord.ui.Button[Any]]:
    """The DM's buttons for the whisper's current state, each carrying the
    persistent id its ``DynamicItem`` matches."""
    templates = {
        "guess": GUESS_ID_TEMPLATE,
        "share": SHARE_ID_TEMPLATE,
        "reply": REPLY_ID_TEMPLATE,
        "delete": DELETE_ID_TEMPLATE,
    }
    buttons: list[discord.ui.Button[Any]] = []
    for key in dm_action_buttons(whisper, now=now):
        style = (
            discord.ButtonStyle.danger if key == "delete"
            else discord.ButtonStyle.secondary
        )
        buttons.append(_button(
            ACTION_LABELS[key],
            templates[key].format(whisper_id=whisper.id),
            style=style,
        ))
    return buttons


def build_whisper_dm_layout(
    whisper: Whisper, *, now: float | None = None,
) -> list[discord.ui.Item[Any]]:
    """The DM a whisper arrives as, and what it redraws to after each
    press. **Paradigm 5 — decision card.**

    The guess hint rides only while a guess is still possible; the ``-#``
    line carries the whisper's number and its status (``inbox_footer``, so
    the DM and the inbox say the same thing). The server is named by
    ``send_branded_dm``'s attribution line, not here. A deleted whisper
    keeps its text and loses every button.
    """
    actions = dm_action_buttons(whisper, now=now)
    lines = [f"## {ICON} You Got a Whisper", "Someone here sent you this anonymously."]
    if "guess" in actions:
        lines[-1] += f" {GUESS_HINT}"
    items: list[discord.ui.Item[Any]] = [
        _text("\n".join(lines)),
        _text(_quote(whisper.message)),
        _text(f"-# Whisper #{whisper.id} · {inbox_footer(whisper, mode='received', now=now)}"),
    ]
    buttons = dm_buttons(whisper, now=now)
    if buttons:
        items.append(discord.ui.ActionRow(*buttons))
    return items


# ── 7. Reply DM and the sender's digest ──────────────────────────────────


def build_reply_dm_layout(
    *, whisper_id: int, whisper_message: str, reply_content: str, reply_id: int,
) -> list[discord.ui.Item[Any]]:
    """The one anonymous reply a whisper gets, DMed to the other party.
    **Paradigm 7 — notice**, with the one button a reply can carry: Report.
    Both quotes are labelled — unlabelled, two quote blocks read as one
    conversation with no way to tell which is the reply."""
    return [
        _text(
            f"## {ICON} Anonymous Reply\n"
            f"**Whisper #{whisper_id}**\n"
            f"{_quote(_clip(whisper_message, REPLY_QUOTE_CHARS))}\n"
            f"**Their reply**\n"
            f"{_quote(reply_content)}"
        ),
        discord.ui.ActionRow(
            _button("Report", REPORT_REPLY_ID_TEMPLATE.format(reply_id=reply_id)),
        ),
    ]


def build_guess_digest_layout(
    *, whisper_id: int, guesses: list[tuple[str, GuessOutcome]],
) -> list[discord.ui.Item[Any]]:
    """Every guess on one whisper over the batch window, one line each, to
    its sender. **Paradigm 7 — receipt.** The heading carries the whisper's
    number once, so the lines don't; ``guessed_label`` is a resolved name
    or "someone" for a no-contact pair, decided by the cog."""
    body = "\n".join(guess_line(label, outcome) for label, outcome in guesses)
    return [_text(f"## {ICON} Guesses on Whisper #{whisper_id}\n{body}")]


# ── 6. The inbox ─────────────────────────────────────────────────────────


def inbox_title(mode: str) -> str:
    return "Your Inbox" if mode == "received" else "Whispers You've Sent"


def inbox_empty_text(mode: str) -> str:
    if mode == "received":
        return "No whispers yet — when someone sends you one, it lands here."
    return "You haven't sent any active whispers in this server."


def _row_head(w: Whisper, *, now: float | None) -> str:
    return f"**#{w.id}** · {status_pill(w, now=now)} · {format_time_ago(w.created_at, now=now)}"


def _escaped_preview(message: str, limit: int) -> str:
    """The whisper's one-line preview, clipped then escaped: it sits in the
    bot's own ``-#`` line, where an unescaped ``[text](url)`` would be a
    clickable link written by the sender."""
    return discord.utils.escape_markdown(_clip(preview(message), limit))


def _row_sub(w: Whisper, *, mode: str, name_fn: NameFn) -> str:
    """The row's second line: the preview, led by the recipient's name on
    the sent list. Both held to :data:`ROW_PREVIEW_CHARS` so the row stays
    beside its button on a phone; a long name leaves less for the text."""
    if mode == "received":
        return _escaped_preview(w.message, ROW_PREVIEW_CHARS)
    lead = f"To {name_fn(w.target_id)}"
    room = ROW_PREVIEW_CHARS - len(lead) - 3
    if room < 4:
        return lead
    return f"{lead} · {_escaped_preview(w.message, room)}"


def _inbox_row(w: Whisper, *, mode: str, name_fn: NameFn, now: float | None) -> discord.ui.Section[Any]:
    return discord.ui.Section(
        _text(f"{_row_head(w, now=now)}\n-# {_row_sub(w, mode=mode, name_fn=name_fn)}"),
        accessory=_button("Open", INBOX_OPEN_ID_TEMPLATE.format(whisper_id=w.id)),
    )


def _nav_row(
    *,
    page: int,
    page_count: int,
    filter_query: str,
    prev_id: str,
    next_id: str,
    filter_id: str,
    clear_id: str,
) -> discord.ui.ActionRow[Any]:
    """Page arrows (only with a second page, disabled at the ends), Filter,
    and Clear while a filter is on — the shape every paged card uses."""
    buttons: list[discord.ui.Button[Any]] = []
    if page_count > 1:
        buttons.append(_button("◀", prev_id, disabled=page <= 0))
        buttons.append(_button("▶", next_id, disabled=page >= page_count - 1))
    buttons.append(_button("🔍 Filter", filter_id))
    if filter_query:
        buttons.append(_button("✕ Clear", clear_id))
    return discord.ui.ActionRow(*buttons)


def build_inbox_list_layout(
    page_whispers: list[Whisper],
    *,
    mode: str,
    total: int,
    shown: int,
    page: int,
    page_count: int,
    filter_query: str,
    name_fn: NameFn,
    now: float | None = None,
) -> list[discord.ui.Item[Any]]:
    """The inbox as a list, ten rows a page with an Open beside each.
    **Paradigm 6 — list with per-row actions.**

    ``total`` is every whisper in the inbox, ``shown`` how many match the
    filter (the same number without one). An empty inbox is the heading and
    a sentence, no controls; a filter with no matches keeps the bottom row
    so Clear is reachable.
    """
    heading = f"## {ICON} {inbox_title(mode)}"
    if total:
        heading += f" · {total}"
    if not total:
        return [_text(f"{heading}\n{inbox_empty_text(mode)}")]

    subtext: list[str] = []
    if filter_query:
        noun = "match" if shown == 1 else "matches"
        subtext.append(f'{shown} {noun} for "{discord.utils.escape_markdown(filter_query)}"')
    if page_count > 1:
        subtext.append(f"Page {page + 1} of {page_count}")
    if subtext:
        heading += "\n-# " + " · ".join(subtext)
    items: list[discord.ui.Item[Any]] = [_text(heading)]
    if page_whispers:
        items.extend(_inbox_row(w, mode=mode, name_fn=name_fn, now=now) for w in page_whispers)
    else:
        items.append(_text("Nothing matches that search."))
    items.append(_nav_row(
        page=page, page_count=page_count, filter_query=filter_query,
        prev_id=INBOX_PREV_ID, next_id=INBOX_NEXT_ID,
        filter_id=INBOX_FILTER_ID, clear_id=INBOX_CLEAR_ID,
    ))
    return items


def build_inbox_detail_layout(
    whisper: Whisper, *, mode: str, name_fn: NameFn, now: float | None = None,
) -> list[discord.ui.Item[Any]]:
    """One whisper opened from the list: the message, its status, its
    actions, and Back. **Paradigm 6**, the pane an Open swaps to.

    The actions are ``logic.inbox_action_buttons``' set in its order, in a
    row of their own; Back has its own row so the two kinds of press don't
    sit together, and because five actions plus Back is one over a row's
    limit.
    """
    title = f"## {ICON} Whisper #{whisper.id}"
    if mode != "received":
        title += f" → {name_fn(whisper.target_id)}"
    facts = f"{status_pill(whisper, now=now)} · {format_time_ago(whisper.created_at, now=now)}"
    return [
        _text(f"{title}\n{facts}"),
        _text(_quote(whisper.message)),
        _text(f"-# {inbox_footer(whisper, mode=mode, now=now)}"),
        discord.ui.ActionRow(*(
            _action_button(key) for key in inbox_action_buttons(whisper, mode=mode, now=now)
        )),
        discord.ui.ActionRow(_button("◀ Back", INBOX_BACK_ID)),
    ]


# ── 10. Delete confirm ───────────────────────────────────────────────────


def build_delete_confirm_layout(whisper_id: int) -> list[discord.ui.Item[Any]]:
    """The gate in front of Delete. **Paradigm 10 — confirm destructive.**
    Cancel is silent — the cog deletes the confirm and leaves whatever was
    behind it as it was."""
    return [
        _text(
            f"## ⚠️ Delete Whisper #{whisper_id}?\n"
            "It leaves your inbox for good. The sender keeps their copy."
        ),
        discord.ui.ActionRow(
            _button("Yes, Delete", DELETE_CONFIRM_ID, style=discord.ButtonStyle.danger),
            _button("Cancel", DELETE_CANCEL_ID),
        ),
    ]


def build_deleted_layout(whisper_id: int) -> list[discord.ui.Item[Any]]:
    """What the confirm turns into after Yes. **Paradigm 7 — receipt.**"""
    return [_text(f"## 🚫 Whisper #{whisper_id} Deleted\nIt's gone from your inbox.")]


# ── 9. Guess pickers and results ─────────────────────────────────────────


def _guess_lead(whisper: Whisper) -> str:
    plural = "guess" if whisper.guesses_left == 1 else "guesses"
    return (
        f"## {ICON} Who Sent It?\n"
        f"Whisper #{whisper.id} · **{whisper.guesses_left} {plural}** left — "
        "a wrong guess is gone for good."
    )


def build_guess_native_picker_layout(whisper: Whisper) -> list[discord.ui.Item[Any]]:
    """The in-server guess picker: Discord's own member select (avatars,
    presence). **Paradigm 9 — game turn card.** It offers every member, so
    the cog refuses a pick outside the pool without spending a guess."""
    return [
        _text(_guess_lead(whisper)),
        discord.ui.ActionRow(discord.ui.UserSelect(
            placeholder="Pick the sender…", min_values=1, max_values=1,
            custom_id=GUESS_PICK_ID,
        )),
    ]


def build_guess_list_picker_layout(
    whisper: Whisper,
    options: list[discord.SelectOption],
    *,
    placeholder: str,
    page: int,
    page_count: int,
    filter_query: str,
) -> list[discord.ui.Item[Any]]:
    """The DM-side guess picker: the opt-in pool as a paged, filterable
    list (a native member select can't resolve guild members from a DM).
    **Paradigm 9 — game turn card.** ``options`` is the current page; an
    empty page (a filter with no matches) shows a disabled placeholder so
    the row keeps its shape."""
    if options:
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=placeholder[:150], options=options,
            min_values=1, max_values=1, custom_id=GUESS_PICK_ID,
        )
    else:
        select = discord.ui.Select(
            placeholder="No members match that search.",
            options=[discord.SelectOption(label="No results", value="__none__")],
            disabled=True, custom_id=GUESS_PICK_ID,
        )
    return [
        _text(_guess_lead(whisper)),
        discord.ui.ActionRow(select),
        _nav_row(
            page=page, page_count=page_count, filter_query=filter_query,
            prev_id=GUESS_PREV_ID, next_id=GUESS_NEXT_ID,
            filter_id=GUESS_FILTER_ID, clear_id=GUESS_CLEAR_ID,
        ),
    ]


def build_guess_result_layout(outcome: GuessOutcome) -> list[discord.ui.Item[Any]]:
    """What the picker edits into after a guess. **Paradigm 7 — receipt.**"""
    if outcome.correct:
        return [_text("## ✅ You Solved It\nYou guessed right.")]
    if outcome.exhausted:
        return [_text("## ❌ Out of Guesses\nWrong — and that was the last one. The sender stays anonymous.")]
    plural = "guess" if outcome.attempts_remaining == 1 else "guesses"
    return [_text(f"## ❌ Not Them\n{outcome.attempts_remaining} {plural} left.")]


def build_line_layout(text: str) -> list[discord.ui.Item[Any]]:
    """A one-line answer on a card that must stay V2 — a refusal that edits
    a picker in place. A bare Text Display, not a card (guide § Screen use)."""
    return [_text(text)]


# ── 5. Opt-in ────────────────────────────────────────────────────────────


def build_optin_layout() -> list[discord.ui.Item[Any]]:
    """The consent card behind ``/whisper optin``. **Paradigm 5 — decision
    card.** Green is the yes; Cancel is grey and silent."""
    return [
        _text(
            f"## {ICON} Join Whisper\n"
            "Opting in lets other members send you anonymous whispers, and "
            "lets you send your own. Leave any time with `/whisper optout`."
        ),
        discord.ui.ActionRow(
            _button("Opt In", OPTIN_CONFIRM_ID, style=discord.ButtonStyle.success),
            _button("Cancel", OPTIN_CANCEL_ID),
        ),
    ]


def build_optin_done_layout() -> list[discord.ui.Item[Any]]:
    """**Paradigm 7 — receipt.**"""
    return [_text("## ✅ You're In\nYou can send and receive whispers now.")]


def build_optout_done_layout() -> list[discord.ui.Item[Any]]:
    """**Paradigm 7 — receipt.** The whispers already exchanged stay."""
    return [_text(
        f"## {ICON} Opted Out\n"
        "No new whispers will reach you. The ones you already have stay."
    )]


# ── 7. Send refusal with the draft ───────────────────────────────────────


def build_send_refusal_layout(
    refusal: str, draft: str, *, title: str = "Whisper Not Sent",
    hint: str = "Here's what you wrote, so you can send it again:",
) -> list[discord.ui.Item[Any]]:
    """Any send refusal, with the member's draft quoted back so it isn't
    lost (Ben, 2026-09-30 / 10-01). A card rather than a content line: an
    escaped 1,000-character draft can pass a message's 2,000 where a Text
    Display has 4,000. The no-contact and Take a Break paths never reach
    this: they answer with the ordinary sent confirmation."""
    line = refusal[2:] if refusal.startswith("❌ ") else refusal
    if not draft or not draft.strip():
        # An empty message is its own refusal; there is nothing to hand back,
        # and a blank quote under "Here's what you wrote" reads as a bug.
        return [_text(f"## ❌ {title}\n{line}")]
    return [_text(f"## ❌ {title}\n{line}\n{hint}\n{_quote(draft)}")]


def build_pool_refusal_layout(refusal: str, draft: str) -> list[discord.ui.Item[Any]]:
    """The refusal for a pick who hasn't opted in (the send form's member
    picker is Discord's own and can't mark who accepts whispers)."""
    return build_send_refusal_layout(
        refusal, draft, title="They Haven't Opted In",
        hint="Here's what you wrote, so you can send it to someone else:",
    )


# ── 7. Mod-log notices ───────────────────────────────────────────────────


def _stamp(now: datetime | None) -> str:
    ts = int((now or datetime.now(timezone.utc)).timestamp())
    return f"<t:{ts}:f>"


def _who(label: str, user_id: int, name_fn: NameFn) -> str:
    """A participant line for moderators: the resolved name and the raw id,
    the copyable identifier the embed's fields carried."""
    return f"**{label}** {name_fn(user_id)} (`{user_id}`)"


def build_reply_audit_layout(
    *,
    whisper_id: int,
    from_user_id: int,
    to_user_id: int,
    content: str,
    name_fn: NameFn,
    now: datetime | None = None,
) -> list[discord.ui.Item[Any]]:
    """The mod-log's "Whisper Reply" notice. **Paradigm 7 — notice.**"""
    return [_text(
        "## 💬 Whisper Reply\n"
        f"{_who('From', from_user_id, name_fn)}\n"
        f"{_who('To', to_user_id, name_fn)}\n"
        f"{_quote(content)}\n"
        f"-# Whisper #{whisper_id} · {_stamp(now)}"
    )]


def build_report_audit_layout(
    *,
    whisper: Whisper,
    reason: str,
    name_fn: NameFn,
    now: datetime | None = None,
) -> list[discord.ui.Item[Any]]:
    """The mod-log's "Whisper Reported" notice. **Paradigm 7 — notice.**
    The reporter is always the recipient."""
    return [_text(
        "## 🚨 Whisper Reported\n"
        f"{_who('Sender', whisper.sender_id, name_fn)}\n"
        f"{_who('Reporter', whisper.target_id, name_fn)} · the recipient\n"
        f"{_quote(whisper.message)}\n"
        "**Reason**\n"
        f"{_quote(reason)}\n"
        f"-# Whisper #{whisper.id} · {_stamp(now)}"
    )]


def build_reply_report_audit_layout(
    *,
    reply: WhisperReply,
    reporter_id: int,
    reason: str,
    name_fn: NameFn,
    now: datetime | None = None,
) -> list[discord.ui.Item[Any]]:
    """The mod-log's "Reply Reported" notice. **Paradigm 7 — notice.**"""
    return [_text(
        "## 🚨 Reply Reported\n"
        f"{_who('Sender', reply.from_user_id, name_fn)} · anonymous to the reporter\n"
        f"{_who('Reporter', reporter_id, name_fn)} · the reply's recipient\n"
        f"{_quote(reply.content)}\n"
        "**Reason**\n"
        f"{_quote(reason)}\n"
        f"-# Reply #{reply.id} · Whisper #{reply.whisper_id} · {_stamp(now)}"
    )]
