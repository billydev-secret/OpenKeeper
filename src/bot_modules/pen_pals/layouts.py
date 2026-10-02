"""Pen Pals' Components V2 cards (``docs/components_v2_style_guide.md``).

Pure builders, no IO: primitives and already-resolved names in, a list of
layout items out, buttons and selects carrying custom ids and no callbacks.
``pen_pals/cog.py`` sends them and wires the behaviour — the panel's and the
closing DM's buttons keep the persistent ids the embed era used, so a panel
or a DM posted before the conversion still works.

Eight cards:

- **The signup panel** (paradigm 1, hub panel), sticky and public: what Pen
  Pals is, how a match happens under this guild's pairing mode, who is
  waiting, who can see a chat, and Join Pool / Leave Pool. **Help** beside
  the title opens the guild's ``penpals`` doc (:func:`build_help_layout`).
  It wears the tall hub banner and the hub footer like the other channel
  hubs (Ben, 2026-09-30: "use the big headers on the panel") — it was built
  on the small lobby strip first, under the 09-28 rule for a hub with no
  art of its own, and he chose the banner from the sample.
- **The room intro** (paradigm 7, notice), pinned in a pair's channel: the
  admin's opening message if there is one, the two names, when the chat
  closes, the commands, and who can see the room. It names the pair and
  pings nobody — the first-question post under it carries the ping.
- **The closing DM** (paradigm 7, notice, with one button in two of its
  three states): re-pooled with Leave the Pool, left out for staying quiet
  with Join the Pool, or the plain goodbye.
- **The two ended-early DMs** (paradigm 7): the partner is no longer
  available (re-pooled, or not because the member opted out), and the
  partner ended the chat.
- **Status** (paradigm 7), ephemeral: matched, waiting, left the pool, or
  not in it.
- **The blocklist** (paradigm 2, settings panel), ephemeral: who the member
  is never matched with, a user select to add and a select to remove.
- **The end confirm** (paradigm 10, confirm destructive), ephemeral.

Nothing here takes a colour: a layout has none. The DMs' server attribution
(icon and ``-# From …`` line) is stamped by ``shared.dm_branding``, so the
DM bodies do not name the server themselves.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import discord

from bot_modules.core.layout import hub_banner, hub_footer, lobby_art
from bot_modules.docs.help_card import build_doc_help_layout

ICON = "🖊️"
TITLE = "Pen Pals"
#: The signup panel's title, which ``panel_title_service.with_title`` draws
#: into the hub banner when the guild has **Title in the Banner** on.
PEN_PALS_PANEL_TITLE = TITLE

JOIN_ID = "pen_pals:join"
LEAVE_ID = "pen_pals:leave"
HELP_ID = "pen_pals:help"
BLOCK_ADD_ID = "pen_pals:block:add"
BLOCK_REMOVE_ID = "pen_pals:block:remove"
BLOCK_DONE_ID = "pen_pals:block:done"
END_CONFIRM_ID = "pen_pals:end:confirm"
END_CANCEL_ID = "pen_pals:end:cancel"

#: The dashboard doc the panel's Help opens. Renaming the doc's key on the
#: dashboard disconnects the button (``docs/specs/pen_pals_spec.md``).
HELP_DOC_KEY = "penpals"
HELP_TITLE = "Pen Pals Help"
_HELP_MISSING = (
    "There's no Pen Pals guide on this server yet — ask a moderator if "
    "something here isn't clear."
)

#: The dashboard caps the opening message at 1,000 characters; the card
#: holds the same line for a row written before the cap or by hand.
INTRO_MESSAGE_MAX = 1000

#: A select holds 25 options, so the blocklist names 25 and counts the rest.
BLOCK_LIST_LIMIT = 25

OUTCOME_REQUEUED = "requeued"
OUTCOME_SKIPPED = "skipped"
OUTCOME_PLAIN = "plain"

#: Who can see a pair's room, as the pinned intro says it. Keyed by
#: ``pen_pals_config.room_visibility``; an unknown value reads as the
#: default, admins only.
_ROOM_LINES = {
    "admin": "Admins can see this channel.",
    "mods": "Admins and mods can see this channel.",
    "everyone": "This channel is visible to everyone (they can read, not post).",
}

#: The same fact on the signup panel, before there is a room to point at.
_PANEL_PRIVACY_LINES = {
    "admin": "Each chat is private to the two of you and the admins.",
    "mods": "Each chat is private to the two of you, the admins and the mods.",
    "everyone": "Everyone can read a chat, but only the two of you can post in it.",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _grey(label: str, custom_id: str, *, emoji: str | None = None) -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=custom_id,
    )


def _name(name: str) -> str:
    return discord.utils.escape_markdown(name)


def room_visibility_line(visibility: str) -> str:
    """The pinned intro's line on who can see the room."""
    return _ROOM_LINES.get(visibility, _ROOM_LINES["admin"])


def session_length_label(seconds: int) -> str:
    """A session length as a member reads it: "24 hours", "3 days", "90 minutes".

    Session length is a dashboard dial, so the panel quotes the guild's own
    rather than the "24 hours" the embed hard-coded.
    """
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "day"), (3600, "hour")):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


# ── 1. The signup panel ──────────────────────────────────────────────────────


def build_panel_layout(
    pool_size: int,
    *,
    scheduled: bool = False,
    schedule_label: str = "",
    session_label: str = "24 hours",
    visibility: str = "admin",
    banner_url: str | None = None,
    footer_url: str | None = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The sticky signup panel. **Paradigm 1 — hub panel.**

    ``scheduled`` picks the match sentence: instant mode pairs on the spot,
    scheduled mode names the round's time (``schedule_label``, the cog's
    ``_round_time_label``). ``visibility`` is the guild's room-visibility
    dial, so the privacy line says who can really see a chat — the embed's
    footer said "and admins" whatever the dial was.

    ``banner_url``/``footer_url`` are the guild's hub banner and footer
    (``core.layout.hub_banner`` / ``hub_footer``), each absent until an
    admin uploads one. ``title_in_banner`` means the banner already carries
    :data:`PEN_PALS_PANEL_TITLE` drawn in: the ``##`` line goes, the title
    becomes the banner's alt text, and **Help** moves up beside the opening
    sentence. It needs a banner — without one the heading stays.

    Both buttons are grey (guide § Anatomy): Join was green in the embed
    era, and green is reserved for the yes on a decision card.
    """
    help_button = _grey("Help", HELP_ID)
    what = _text(f"Get matched 1-on-1 with another member for {session_label}.")
    head: list[discord.ui.Item[Any]]
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=PEN_PALS_PANEL_TITLE),
            discord.ui.Section(what, accessory=help_button),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.Section(
                _text(f"## {ICON} {PEN_PALS_PANEL_TITLE}"), accessory=help_button
            ),
            what,
        ]
    how = (
        f"Matches go out at {schedule_label} — join any time before then to "
        "be in the next round."
        if scheduled else
        "If someone's already waiting you're matched on the spot — a private "
        "channel opens for the two of you, with a conversation starter "
        "already in it."
    )
    waiting = (
        f"**{pool_size}** member{'' if pool_size == 1 else 's'} waiting"
        if pool_size else "No one waiting yet"
    )
    privacy = _PANEL_PRIVACY_LINES.get(visibility, _PANEL_PRIVACY_LINES["admin"])
    return [
        *head,
        _text(how),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        _text(f"{waiting}\n-# {privacy}"),
        discord.ui.ActionRow(
            _grey("Join Pool", JOIN_ID, emoji="✉️"),
            _grey("Leave Pool", LEAVE_ID, emoji="🚪"),
        ),
        *hub_footer(footer_url),
    ]


def build_help_layout(body_md: Optional[str]) -> list[discord.ui.Item[Any]]:
    """The panel's Help answer, ephemeral. **Paradigm 7 — notice.**

    The guild's ``penpals`` doc as an admin last saved it on the dashboard,
    read at click time so the card and the doc can't disagree. Send it with
    ``AllowedMentions.none()``: an admin-written doc may hold a mention.
    """
    return build_doc_help_layout(
        body_md, glyph=ICON, fallback_title=HELP_TITLE, missing_text=_HELP_MISSING
    )


# ── 2. The room intro ────────────────────────────────────────────────────────


def intro_commands(max_swaps: int) -> str:
    """The intro's commands, quoting the guild's real swap cap.

    The cap is a dashboard dial and is enforced on every swap, so the pinned
    copy has to follow it — a fixed "(3 max)" told a pair the wrong number
    the moment an admin moved it. At 0 there are no swaps, so the line goes
    away rather than advertising a command that always refuses.
    """
    lines = []
    if max_swaps > 0:
        lines.append(f"`/penpals new-question` — swap the prompt ({max_swaps} max)")
    lines.append("`/penpals end` — leave this chat early")
    return "\n".join(lines)


def build_intro_layout(
    name1: str,
    name2: str,
    expiry_at: float,
    *,
    intro_message: str = "",
    max_swaps: int = 3,
    visibility: str = "admin",
) -> list[discord.ui.Item[Any]]:
    """The card pinned when a pair's channel opens. **Paradigm 7 — notice.**

    Names, not mentions: the pair is named here and pinged by the
    first-question post under it, never both in one place. ``intro_message``
    is the admin's optional opening note, shown above the facts and left as
    the markdown they wrote; send the card with ``AllowedMentions.none()``
    since it may hold a mention.
    """
    expiry = int(expiry_at)
    head = f"## {ICON} {TITLE}"
    note = intro_message.strip()[:INTRO_MESSAGE_MAX]
    if note:
        head = f"{head}\n{note}"
    return [
        _text(head),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        _text(
            f"**{_name(name1)}** × **{_name(name2)}**\n"
            f"Closes <t:{expiry}:R>, on <t:{expiry}:F>"
        ),
        _text(intro_commands(max_swaps)),
        _text(f"-# {room_visibility_line(visibility)}"),
    ]


# ── 3–5. The DMs ─────────────────────────────────────────────────────────────


def build_closed_layout(
    guild_id: int,
    *,
    partner_name: str | None,
    outcome: str = OUTCOME_PLAIN,
    eligible_at: int | None = None,
) -> list[discord.ui.Item[Any]]:
    """The DM a member gets when a chat runs its course. **Paradigm 7.**

    ``partner_name`` is ``None`` when the partner must not be named — they
    have left, or the pair has since been blocked or put on the no-contact
    list — and the card says "your pen pal" for all of those alike, so the
    blocked side reads nothing out of the ordinary.

    ``outcome`` is where the close left this member: :data:`OUTCOME_REQUEUED`
    (back in the pool, with a Leave the Pool button), :data:`OUTCOME_SKIPPED`
    (left out for staying quiet, with a Join the Pool button), or
    :data:`OUTCOME_PLAIN` (nothing changed, or they opted out — no button,
    because offering to undo their own choice is the bot arguing with them).
    ``eligible_at`` is when the re-match cooldown ends, or ``None`` when
    there is no cooldown; it is a countdown because the cooldown is a dial.

    The buttons name the guild in their ids: a DM interaction carries none.
    """
    who = f"**{_name(partner_name)}**" if partner_name else "your pen pal"
    heading = f"## {ICON} Your Pen Pal Channel Has Closed"
    if outcome == OUTCOME_REQUEUED:
        when = (
            f"You can be matched again <t:{eligible_at}:R>."
            if eligible_at is not None
            else "A new match can come along any time."
        )
        return [
            _text(
                f"{heading}\nYour time with {who} is up — thanks for the chat.\n\n"
                f"You're back in the Pen Pals pool. {when}"
            ),
            discord.ui.ActionRow(
                _grey("Leave the Pool", f"pen_pals:dm:leave:{guild_id}", emoji="🚪")
            ),
        ]
    if outcome == OUTCOME_SKIPPED:
        return [
            _text(
                f"{heading}\nYour chat with {who} has closed.\n\n"
                "That one stayed quiet, so we've unqueued you for now. "
                "Hop back in whenever you're up for another."
            ),
            discord.ui.ActionRow(
                _grey("Join the Pool", f"pen_pals:dm:join:{guild_id}", emoji="✉️")
            ),
        ]
    return [_text(f"{heading}\nYour time with {who} is up — thanks for the chat.")]


_ENDED_EARLY_HEADING = f"## {ICON} Your Pen Pal Chat Ended Early"


def build_partner_gone_layout(*, requeued: bool) -> list[discord.ui.Item[Any]]:
    """The DM for a chat that ended because the partner is gone. **Paradigm 7.**

    One wording for every cause — the partner left, was removed, took a
    break, or the channel was deleted — so the card never says which.
    ``requeued`` is whether this member went back in the pool; an opted-out
    member is still told the chat ended, and not promised a seat they gave
    up.
    """
    tail = (
        "You've been put back in the Pen Pals pool for a new match."
        if requeued else
        "You've left the pool, so we haven't put you back in — "
        "join again whenever you'd like another."
    )
    return [_text(
        f"{_ENDED_EARLY_HEADING}\nYour partner is no longer available. {tail}"
    )]


def build_ended_by_partner_layout() -> list[discord.ui.Item[Any]]:
    """The DM for a chat the partner closed with ``/penpals end``. **Paradigm 7.**"""
    return [_text(f"{_ENDED_EARLY_HEADING}\nYour partner ended the chat early.")]


# ── 6. Status ────────────────────────────────────────────────────────────────

_STATUS_HEADING = f"## {ICON} Pen Pals Status"


def build_status_matched_layout(
    partner_name: str,
    *,
    channel_id: int | None,
    expiry_at: float,
    swaps_left: int,
    opted_out: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Status for a member with a live chat. **Paradigm 7 — notice.**

    ``partner_name`` comes from the cog's ``name_fn`` — a resolved name, or
    ``User <id>`` for someone the bot can't name — never a mention.
    ``opted_out`` adds the one line that reflects a mid-chat Leave back to
    the member: leaving is silent to the partner, so this is the only place
    it shows.
    """
    where = f"<#{channel_id}>" if channel_id else "(channel missing)"
    lines = [
        _STATUS_HEADING,
        f"You're matched with **{_name(partner_name)}** in {where}.",
        f"Closes <t:{int(expiry_at)}:R> · Question swaps left: **{max(0, swaps_left)}**",
    ]
    items: list[discord.ui.Item[Any]] = [_text("\n".join(lines))]
    if opted_out:
        items.append(_text(
            "-# You've left the pool, so you won't be matched again when "
            "this chat closes."
        ))
    return items


def build_status_pool_layout(
    position: int, *, scheduled: bool = False, schedule_label: str = "",
) -> list[discord.ui.Item[Any]]:
    """Status for a member waiting in the pool. **Paradigm 7 — notice.**"""
    when = (
        f"in the next round, at {schedule_label}"
        if scheduled else "as soon as someone eligible joins"
    )
    return [_text(
        f"{_STATUS_HEADING}\nYou're in the pool at position **#{position}** — "
        f"you'll be matched {when}."
    )]


def build_status_left_layout(left_at: float) -> list[discord.ui.Item[Any]]:
    """Status for a member who left the pool. **Paradigm 7 — notice.**"""
    return [_text(
        f"{_STATUS_HEADING}\nYou left the Pen Pals pool <t:{int(left_at)}:R> and "
        "won't be matched. Use `/penpals join` — or **Join Pool** on the "
        "signup panel — whenever you want back in."
    )]


def build_status_idle_layout() -> list[discord.ui.Item[Any]]:
    """Status for a member who has never joined. **Paradigm 7 — notice.**"""
    return [_text(
        f"{_STATUS_HEADING}\nYou're not in the pool and have no active pen pal. "
        "Use `/penpals join` to sign up."
    )]


# ── 7. The blocklist ─────────────────────────────────────────────────────────


def build_block_layout(blocked: Sequence[tuple[int, str]]) -> list[discord.ui.Item[Any]]:
    """A member's own never-match list. **Paradigm 2 — settings panel.**

    ``blocked`` is ``(user_id, name)`` pairs, newest first, the names already
    resolved (``User <id>`` for someone who has left). The list is the state
    sentence; a user select adds, a select of the current blocks removes,
    and Done closes the panel. Both selects rebuild the card in place.
    """
    if blocked:
        shown = blocked[:BLOCK_LIST_LIMIT]
        lines = [f"• {_name(name)}" for _uid, name in shown]
        if len(blocked) > len(shown):
            lines.append(f"…and {len(blocked) - len(shown)} more")
        body = (
            "You'll never be matched with:\n" + "\n".join(lines) + "\n\n"
            "Blocking someone doesn't end a chat you're already in — use "
            "`/penpals end` for that."
        )
    else:
        body = (
            "You have no blocks. Pick members below and Pen Pals will never "
            "match you with them."
        )
    items: list[discord.ui.Item[Any]] = [
        _text(f"## 🚫 Pen Pals Blocklist\n{body}\n-# They're never told they were blocked."),
        discord.ui.ActionRow(discord.ui.UserSelect(
            placeholder="Block someone…", min_values=1, max_values=25,
            custom_id=BLOCK_ADD_ID,
        )),
    ]
    if blocked:
        options = [
            discord.SelectOption(label=name[:100], value=str(uid))
            for uid, name in blocked[:BLOCK_LIST_LIMIT]
        ]
        items.append(discord.ui.ActionRow(discord.ui.Select(
            placeholder="Unblock someone…", min_values=1, max_values=len(options),
            options=options, custom_id=BLOCK_REMOVE_ID,
        )))
    items.append(discord.ui.ActionRow(_grey("Done", BLOCK_DONE_ID)))
    return items


# ── 8. The end confirm ───────────────────────────────────────────────────────


def build_end_confirm_layout() -> list[discord.ui.Item[Any]]:
    """The ``/penpals end`` gate. **Paradigm 10 — confirm destructive.**"""
    return [
        _text(
            "## ⚠️ End This Chat Early?\n"
            "This closes the channel for both of you, and your pen pal is "
            "told you ended it."
        ),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="End Session", style=discord.ButtonStyle.danger,
                custom_id=END_CONFIRM_ID,
            ),
            _grey("Cancel", END_CANCEL_ID),
        ),
    ]


def build_end_closing_layout() -> list[discord.ui.Item[Any]]:
    """What the confirm becomes once pressed, while the channel is deleted."""
    return [_text(f"## {ICON} Closing Your Pen Pal Chat…")]


# ── 9. The question posts ────────────────────────────────────────────────────

QUESTION_FIRST = "first"
QUESTION_AUTO = "auto"
QUESTION_SWAP = "swap"


def build_question_layout(
    kind: str,
    question: str,
    user1_id: int,
    user2_id: int,
    *,
    swaps_left: int = 0,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """A conversation starter posted in a pair's room. **Paradigm 7 — notice.**

    ``kind`` is :data:`QUESTION_FIRST` (under the pinned intro when the room
    opens), :data:`QUESTION_AUTO` (the daily top-up) or
    :data:`QUESTION_SWAP` (``/penpals new-question``, with ``swaps_left``).

    **It pings both members, deliberately.** The embed era sent this as plain
    content that pinged them; a V2 message has no ``content=``, so the ping
    lives in the card body (guide § Mentions and names). Send it with
    :func:`question_mentions`, an allow-list of exactly those two. Each
    member is named once, by the ping, and nowhere else on the card.

    It wears the small lobby strip with "Pen Pals" drawn between the leaves
    (Ben, 2026-09-30: "do the small banners for new questions in the
    channel"): a room is a public card surface, not one member's ephemeral
    one. Without a header it is a plain ``##`` card; with the title drawn in,
    the heading goes and the strip names it.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    heading = {
        QUESTION_FIRST: "Your First Question",
        QUESTION_AUTO: "A New Question",
        QUESTION_SWAP: "Question Swapped",
    }.get(kind, "A New Question")
    lead = {
        QUESTION_FIRST: "💬 Here's your first question:",
        QUESTION_AUTO: "💬 A new question to keep things going:",
        QUESTION_SWAP: (
            f"🔄 New question ({swaps_left} swap"
            f"{'' if swaps_left == 1 else 's'} remaining):"
        ),
    }.get(kind, "💬 A new question to keep things going:")
    quoted = "> " + question.replace("\n", "\n> ")
    lines = [] if titled else [f"## {ICON} {heading}"]
    lines += [f"<@{user1_id}> <@{user2_id}>", lead, quoted]
    return [*top, _text("\n".join(lines)), *bottom]


def question_mentions(user1_id: int, user2_id: int) -> discord.AllowedMentions:
    """The question card's allow-list: the two members of the room, no one else."""
    return discord.AllowedMentions(
        everyone=False, roles=False, replied_user=False,
        users=[discord.Object(id=user1_id), discord.Object(id=user2_id)],
    )
