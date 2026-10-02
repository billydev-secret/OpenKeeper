"""Components V2 card builders for the DM-permission feature.

The V2 replacement for this package's ``embeds.py`` (deleted 2026-09-20 with
the stage-2 conversion — ``docs/plans/components_v2_migration.md``). Every
function here is **pure**: primitives and a ``name_fn`` in, a list of
``discord.ui.Item`` out. No IO, no accent lookup, no callbacks — the cog's
``LayoutView`` subclasses add the items in order and bind behaviour by
``find_item(id)`` or by the persistent ``custom_id``s below.

Each builder's docstring names the paradigm it builds
(``docs/components_v2_style_guide.md`` § The paradigms), because a card that
fits none of the eleven is a design question rather than a local choice.

Conventions the guide fixes and this module follows throughout:

* **A card is bare** — no ``Container``, no accent colour. Weight comes from a
  ``##`` heading, a thumbnail beside the subject, a button beside a row and a
  ``-#`` subtext line. The public hub panel (``build_panel_layout``) was a
  boxed tile on trial from 2026-09-24 and is bare again since 2026-09-29.
* **A card's own heading is one glyph-led ``## Title`` line** in its first Text
  Display (guide § Anatomy). ``###`` marks a section *inside* a card — the
  settings panel's ``### Your Connections`` under its own ``## DM Settings``
  — and nothing else: an ephemeral confirm and a mod-feed notice are cards
  too, and a half-styled one reads as unfinished beside the receipts it sits
  next to. ``test_every_card_leads_with_a_glyph_led_title`` holds the whole
  module to it.
* **A member is named through ``name_fn``**, never as ``<@id>``: a mention in
  a Text Display renders from the *reading* client's cache, so it shows as a
  bare number to anyone who has not seen that member. The parameter has no
  default so a caller cannot forget it.
* **One name per thing, everywhere** (guide § Words). The button, the panel
  it opens and every card that points back at it say "DM Settings"; a
  connection's kind is "DM" or "Friend" on every card (``_TYPE_WORDS`` —
  ``request_type_label``'s long form belongs to the request form's radio
  and the mod feed); what a requester typed is their *reason*, what a
  decliner sends back is their *note*. The design pass that fixed this
  (2026-09-20) found the same panel called "Settings", "My DM Settings" and
  "the server's DM panel" across the twenty cards.
* **A card addressed to one person names the other person.** A DM receipt
  says "You and **Bo**", never "**Ada** ↔ **Bo**" — the reader is one of the
  two, and a card that names them in the third person reads like a notice
  about somebody else. Builders for a card that goes to both parties take a
  ``viewer_id`` and are built once per recipient. (The mod feed's
  ``audit_line_*`` keep both names and their arrows on purpose: a feed line
  is read by a third party.)
* **Quoted text is a ``>`` block**, not a bold label over a paragraph: the
  reason on the decision card, the reason echoed on the Sent receipt, a
  decliner's note. A quote straight under the sentence that introduces it
  needs no label; one anywhere else says whose words it is.
* **One emoji per outcome** (guide § Words): ✅ granted, ❌ did not happen,
  🚫 removed, ⌛ timed out or gone, ⚠️ about to destroy, 📨 a request, 📬
  the feature's own panels, 📜 the record.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

import discord

from bot_modules.core.layout import card_strips, hub_banner, hub_footer
from bot_modules.dm.perms.logic import safe_field_text
from bot_modules.docs.help_card import build_doc_help_layout
from bot_modules.dm.perms.service import (
    normalize_request_type,
    request_type_label,
)
from bot_modules.shared.name_resolver import NameFn

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt

# ── Persistent custom ids ────────────────────────────────────────────
#
# These survive a restart: the request panel is a sticky message and the
# consent buttons live in a DM the bot may not touch again for a day. The cog
# registers one instance of each view at load and recovers per-request state
# from the message id, so the *meaning* of these strings is part of the
# feature's on-disk state — renaming one orphans every card already out there.
# A button's *label* is not: the boot-time ``place_or_refresh`` repaints the
# panel, and a persistent view dispatches on the id alone.

DM_REQUEST_PANEL_CUSTOM_ID = "dm_request:open_modal"
DM_SETTINGS_PANEL_CUSTOM_ID = "dm_request:open_settings"
DM_HELP_PANEL_CUSTOM_ID = "dm_request:help"
DM_CONSENT_ACCEPT_CUSTOM_ID = "dm_consent:accept"
DM_CONSENT_DENY_CUSTOM_ID = "dm_consent:deny"
DM_CONSENT_DENY_REPLY_CUSTOM_ID = "dm_consent:deny_reply"

# ── Transient component ids ──────────────────────────────────────────
#
# An ephemeral panel's controls are looked up by numeric id rather than by
# custom_id: nothing outside the open panel ever dispatches to them, and the
# per-connection Remove buttons need an index the view can map back to a row.

MODE_SELECT_ID = 1
DONE_BUTTON_ID = 2
CONFIRM_REMOVE_ID = 3
CONFIRM_CANCEL_ID = 4
PREV_PAGE_ID = 5
NEXT_PAGE_ID = 6
REMOVE_BUTTON_ID_BASE = 100

#: Ten Sections (30 components) plus the heading, the dial, the separator,
#: the bottom row and its two page arrows is the 40-component ceiling exactly
#: — the guide's "a row-per-button list pages at ten rows".
MAX_CONNECTION_ROWS = 10

#: What "one line" costs on a 400 px phone, as a number a test can hold the
#: builder to. Rough — Discord's font is proportional — but a Section row that
#: stays under it with the 32-character display name Discord allows will not
#: wrap, and a wrapped row pushes its own button down the card (guide
#: § Screen use on a phone).
MAX_ROW_CHARS = 45


class _TypeWords(NamedTuple):
    """The words one request type puts on the cards.

    ``label`` is the short form beside a name; ``noun`` completes "your …";
    ``asks`` is what the decision card says the requester wants; ``grants``
    is what a connection of this kind lets the pair do.
    """

    label: str
    noun: str
    asks: str
    grants: str


#: The two request types as the cards say them. The long labels
#: (``request_type_label``: "Direct Message" / "Friend Request") stay on the
#: request form's radio, where each has a line of description, and in the
#: mod feed. On a card the short form is the whole vocabulary: "Direct
#: Message" beside a long display name is what tipped a connection row over
#: ``MAX_ROW_CHARS``, and a card that says "DM" in its heading and "Direct
#: Message" two lines down is teaching the reader two names for one thing.
_TYPE_WORDS = {
    "dm": _TypeWords(
        label="DM",
        noun="DM request",
        asks="wants to DM you",
        grants="can now DM each other",
    ),
    "friend": _TypeWords(
        label="Friend",
        noun="friend request",
        asks="wants to add you as a friend on Discord",
        grants="can now add each other on Discord",
    ),
}

#: The dial's three states, in the order they read: most reachable first.
MODE_ORDER = ("open", "ask", "closed")
MODE_LABELS = {"open": "Open", "ask": "Ask", "closed": "Closed"}
MODE_EMOJI = {"open": "🟢", "ask": "🟡", "closed": "🔴"}
MODE_BLURB = {
    "open": "Anyone here may DM you.",
    "ask": "People send a request, and you decide.",
    "closed": "No DM requests reach you.",
}

#: Every card that starts, grants or removes access carries this, the way
#: the embeds carried it in their footer. It rides on the *receipts* — Sent,
#: Connected, Removed — which are the moments the log gains a row; the
#: settings panel used to state it on every redraw, where it was the one
#: line on the card that answered no press.
_AUDIT_LINE = "-# DM connections are logged for audit transparency."

#: Where a receipt points the reader next. The hub is titled "DM Requests";
#: every card that mentions it uses that name.
_HUB_NAME = "the DM Requests panel"


def _words(request_type: Optional[str]) -> _TypeWords:
    return _TYPE_WORDS[normalize_request_type(request_type)]


def mode_sentence(mode: str, *, changed: bool = False) -> str:
    """The panel's statement of the member's current dial setting.

    A dropdown hides its option descriptions until it opens, so the card has
    to say what the current value *means* in text (guide § Controls).
    ``changed`` is the redraw right after a pick: the same sentence with
    "Changed to" in place of "You're on", so the press's answer is the
    status line itself rather than a second line under the card saying the
    same thing again.
    """
    key = mode if mode in MODE_LABELS else "ask"
    lead = "Changed to" if changed else "You're on"
    return f"{lead} {MODE_EMOJI[key]} **{MODE_LABELS[key]}** — {MODE_BLURB[key]}"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _quote(text: str) -> str:
    """``text`` as a markdown quote block, every line prefixed.

    Member-typed text (a reason, a note) is escaped first: it arrives from a
    modal and a bare ``**`` or ``[link](…)`` in it would render. A blank
    line inside the text keeps its ``>`` so the block stays one block.
    """
    return "\n".join(f"> {line}" for line in safe_field_text(text).splitlines())


# ── 1. Hub panel ─────────────────────────────────────────────────────


#: The hub's name — its text heading, or drawn into the banner.
DM_PANEL_TITLE = "DM Requests"


def build_panel_layout(
    *,
    banner_url: Optional[str] = None,
    footer_url: Optional[str] = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The sticky, public DM request panel. **Paradigm 1 — hub panel.**

    One sentence on what this is, then the two entry points in **their own
    headed groups**. Deliberately **no how-to**: the request form is one
    modal that explains itself, and the five numbered steps the embed
    carried described a flow that no longer exists. The long explanation
    lives one tap away, behind **Help** beside the title.

    The grouping is the guide's hub rule (§ Controls), and it is here because
    Billy read the first draft's two back-to-back Sections as one list —
    "can you put some sort of separation between requesting and setting your
    preference" (2026-09-20). One row reaches out to *another member* and the
    other configures *yourself*, but a Section carries no weight of its own,
    so two of them stacked read as two items of one menu however different
    the things they do are. So each kind of thing gets its own ``###``
    heading and a **visible** ``Separator`` divides the groups.

    Each group closes on its own ``-#`` line, under the group it is about:
    *Ask Someone*'s says what an accepted request leads to, *Your
    Preference*'s what DM Settings holds (Billy, 2026-09-24: a blurb under
    the preferences "so they're even", and friendlier row copy than the
    first cut's "Pick who, and say why."). The three-status legend that used to
    close the card is gone (Billy, 2026-09-20: "let's drop the 'your status
    shows on your profile' line"); the settings panel spells the modes out
    when it opens, which is the moment a member is choosing one.

    **Bare, like every other card** (Ben, 2026-09-29: "move the banners out
    for full width and drop the box"). The panel was a boxed tile on trial
    from 2026-09-24; inside the box the text and buttons overhung the
    banner's gold frame, and Discord can neither pad a line of text nor hide
    a box. So the parts sit directly on the message and the art's frame is
    drawn at the picture's edge, where the text column's edge is (guide
    § Media). The 09-24 call's line under the intro, above *Ask Someone*,
    and its **Help** button stay.

    ``banner_url`` is the guild's hub banner (``core.layout.hub_banner``),
    the panel's first item when an admin has set one. The request form,
    settings and help cards this panel opens are ephemeral and never carry
    it. ``footer_url`` (``core.layout.hub_footer``) is the strip that closes
    the panel, its last item, when one is set.

    ``title_in_banner`` means ``banner_url`` already carries
    :data:`DM_PANEL_TITLE` drawn in (``panel_title_service``, Billy
    2026-09-26): the ``##`` heading goes, the title becomes the banner's alt
    text, and **Help** moves up beside the intro line. It needs a banner —
    without one the heading stays whatever the flag says.
    """
    help_button = discord.ui.Button(
        label="Help",
        style=discord.ButtonStyle.secondary,
        custom_id=DM_HELP_PANEL_CUSTOM_ID,
    )
    intro = _text(
        "Want to reach someone privately? Ask them here first — the "
        "request goes to their DMs, never to this channel."
    )
    head: list[discord.ui.Item[Any]]
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=DM_PANEL_TITLE),
            discord.ui.Section(intro, accessory=help_button),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.Section(_text(f"## 📬 {DM_PANEL_TITLE}"), accessory=help_button),
            intro,
        ]
    return [
        *head,
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
        _text("### Ask Someone"),
        discord.ui.Section(
            _text("Ask to DM someone or add them as a friend."),
            # Grey like its siblings, not blurple (Billy, 2026-09-24:
            # "can both buttons be the same light grey") — guide
            # § Controls: a button is grey unless its colour means
            # something.
            accessory=discord.ui.Button(
                label="Open Form",
                style=discord.ButtonStyle.secondary,
                custom_id=DM_REQUEST_PANEL_CUSTOM_ID,
            ),
        ),
        _text(
            "-# If they accept, you're connected — either of you can undo "
            "it in DM Settings."
        ),
        # Large, not small: Billy on the posted gallery (2026-09-20) — "can
        # we get a little more vertical separation". A drawn line with small
        # padding still read as crowded against the two headings.
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
        _text("### Your Preference"),
        discord.ui.Section(
            _text("Choose who can message you directly."),
            accessory=discord.ui.Button(
                label="DM Settings",
                style=discord.ButtonStyle.secondary,
                custom_id=DM_SETTINGS_PANEL_CUSTOM_ID,
            ),
        ),
        # Each group ends on its own ``-#`` line so the two halves read
        # as a pair (Billy, 2026-09-24: "so they're even"). It says what
        # the panel lets you *do*, not what others see — the "your
        # status shows on your profile" legend stays gone.
        _text(
            "-# Open, Ask or Closed — change it whenever you like, and "
            "see who you're connected to."
        ),
        *hub_footer(footer_url),
    ]


# ── 1a. Help (the FAQ, one tap from the hub) ─────────────────────────

#: The Help card's title when the doc opens without a heading of its own.
HELP_TITLE = "DM Requests Help"

_HELP_MISSING = (
    "There's no DM requests FAQ on this server yet — ask a moderator if "
    "something here isn't clear."
)


def build_help_layout(body_md: Optional[str]) -> list[discord.ui.Item[Any]]:
    """The hub's Help answer, ephemeral. **Paradigm 7 — notice.**

    Billy (2026-09-24): "add a help button that gives the long explanation
    from the FAQ". The body is the guild's ``dmperms`` doc as an admin last
    saved it on the dashboard, read at click time so the card and the FAQ
    can't disagree. Reading it — breaks, images, the cut, the lifted
    heading — is the shared ``docs.help_card`` builder's job, so the next
    hub's Help reads a doc the same way.
    """
    return build_doc_help_layout(
        body_md, glyph="📬", fallback_title=HELP_TITLE, missing_text=_HELP_MISSING
    )


# ── 2 + 6. Settings panel ────────────────────────────────────────────


def _connection_row(
    connection: dict[str, Any], index: int, name_fn: NameFn
) -> discord.ui.Section[Any]:
    """One connected member, with their own Remove button beside them.

    **Paradigm 6 — list with per-row actions**, and the one place in this
    feature where the phone rule bites: a Section's accessory sits *beside*
    its text, so a row that wraps pushes its own Remove button down.

    Two facts fit, and the first draft's three did not. ``since <t:…:D>``
    expands to "September 20, 2026" on the reader's screen, which took even
    "Alice · Direct Message · since …" to 49 visible characters — over
    ``MAX_ROW_CHARS`` before a single long display name got involved. So the
    date is gone (the audit log keeps it; the row never needed it) and the
    type is the short form, which leaves the longest name Discord allows at
    41.
    """
    return discord.ui.Section(
        _text(
            f"**{name_fn(int(connection['user_id']))}** · "
            f"{_words(connection.get('type')).label}"
        ),
        accessory=discord.ui.Button(
            label="Remove",
            style=discord.ButtonStyle.danger,
            id=REMOVE_BUTTON_ID_BASE + index,
        ),
    )


def connection_page(
    connections: Sequence[dict[str, Any]], page: int
) -> tuple[int, int, list[dict[str, Any]]]:
    """Clamp ``page`` into range and slice it: ``(page, page_count, rows)``.

    One function for the builder and the view, so the rows the card draws
    and the rows the view binds its Remove buttons to can never disagree.
    An empty list is one empty page; a page past the end (the last row on
    it was just removed) lands on the new last page rather than on nothing.
    """
    page_count = max(1, -(-len(connections) // MAX_CONNECTION_ROWS))
    page = min(max(page, 0), page_count - 1)
    start = page * MAX_CONNECTION_ROWS
    return page, page_count, list(connections[start : start + MAX_CONNECTION_ROWS])


def build_settings_layout(
    *,
    mode: str,
    connections: Sequence[dict[str, Any]],
    name_fn: NameFn,
    changed: bool = False,
    note: str = "",
    page: int = 0,
) -> list[discord.ui.Item[Any]]:
    """The member's own ephemeral DM panel. **Paradigm 2 — settings panel**,
    with the connection list as **paradigm 6 — list with per-row actions**.

    The three mode buttons became one dropdown: three values that cannot be
    combined are a select, not a row of toggles (guide § Controls). It costs
    two components where the buttons cost four, carries a one-line
    description per option, and pre-selects the member's current mode — and
    the sentence above it states that mode at rest, because the descriptions
    are hidden until the dropdown opens. ``changed`` is the redraw right
    after a pick, and turns that sentence into the press's answer ("Changed
    to …") rather than adding a ``-#`` line that said the same thing twice.

    The connection *lookup* is gone with the user-select that drove it: the
    panel simply lists who the member is connected to, each row with its own
    red Remove. A no-contact pair is filtered out upstream, exactly as
    ``_is_mutual`` already reports one as unconnected.

    The list **pages at ten** (guide § The budget): ten Section rows plus
    the two arrows is the 40-component ceiling exactly. The arrows join Done
    in the bottom row only when there is a second page, and are disabled at
    the ends rather than hidden so the row keeps its shape. A plain cut at
    ten left a member's oldest connections — the ones past the cut —
    unreachable from the one surface that can remove them, and prod had
    members with eleven and twelve the day the pilot shipped.

    ``note`` is the line a *removal* leaves behind when the panel redraws
    under the confirm card's answer; it renders as the card's ``-#`` line.
    The subtext is the footer slot and a footer does one job
    (embed_style_guide.md § Footers), so a note displaces the page line for
    that one redraw — the note answers the press that just happened, and the
    count is stable information the rows re-state the moment the note ages
    off. With neither there is no subtext at all: the audit-transparency line
    that used to fill it moved to the receipts that actually add a log row.
    """
    page, page_count, rows = connection_page(connections, page)
    items: list[discord.ui.Item[Any]] = [
        _text(f"## 📬 DM Settings\n{mode_sentence(mode, changed=changed)}"),
        discord.ui.ActionRow(
            discord.ui.Select(
                placeholder="Pick your DM mode…",
                min_values=1,
                max_values=1,
                id=MODE_SELECT_ID,
                options=[
                    discord.SelectOption(
                        label=MODE_LABELS[key],
                        value=key,
                        description=MODE_BLURB[key],
                        emoji=MODE_EMOJI[key],
                        default=key == mode,
                    )
                    for key in MODE_ORDER
                ],
            )
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
    ]

    if rows:
        items.append(_text("### Your Connections"))
        items.extend(
            _connection_row(connection, index, name_fn)
            for index, connection in enumerate(rows)
        )
    else:
        items.append(
            _text(
                "### Your Connections\n"
                "Nobody yet — a member appears here once they accept your "
                "request, or you accept theirs."
            )
        )

    bottom: list[discord.ui.Button[Any]] = []
    if page_count > 1:
        bottom.extend(
            (
                discord.ui.Button(
                    label="◀",
                    style=discord.ButtonStyle.secondary,
                    id=PREV_PAGE_ID,
                    disabled=page == 0,
                ),
                discord.ui.Button(
                    label="▶",
                    style=discord.ButtonStyle.secondary,
                    id=NEXT_PAGE_ID,
                    disabled=page == page_count - 1,
                ),
            )
        )
    bottom.append(
        discord.ui.Button(
            label="Done", style=discord.ButtonStyle.secondary, id=DONE_BUTTON_ID
        )
    )
    items.append(discord.ui.ActionRow(*bottom))

    if note:
        items.append(_text(f"-# {note}"))
    elif page_count > 1:
        items.append(
            _text(
                f"-# Page {page + 1} of {page_count} • "
                f"{len(connections)} connections"
            )
        )
    return items


# ── 10. Confirm destructive ──────────────────────────────────────────


def build_remove_confirm_layout(
    *, user_id: int, request_type: Optional[str], name_fn: NameFn
) -> list[discord.ui.Item[Any]]:
    """The gate in front of a Remove press. **Paradigm 10 — confirm.**

    Nothing destructive is one tap, and a red button beside a row is one tap
    away from a mis-scroll on a phone. The card echoes the row that was
    pressed, word for word, so a mis-tap is visible before it is confirmed.
    The panel's own list is redrawn only after this card is answered.
    """
    return [
        _text(
            "## ⚠️ Remove Connection?\n"
            f"**{name_fn(user_id)}** · {_words(request_type).label}\n"
            "You'd no longer be connected, and you'll both get a DM saying so."
        ),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Yes, Remove",
                style=discord.ButtonStyle.danger,
                id=CONFIRM_REMOVE_ID,
            ),
            discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.secondary,
                id=CONFIRM_CANCEL_ID,
            ),
        ),
    ]


def build_remove_done_layout(
    *, user_id: int, name_fn: NameFn
) -> list[discord.ui.Item[Any]]:
    """The confirm card, answered yes and carried out. **Paradigm 7.**

    The two answers a confirmed Remove can end on are builders rather than
    literals in the view that sends them, for the same reason every other
    card here is: ``scripts/layout_samples.py`` renders this module, so a
    card built in the cog is a card Billy's phone review never sees. (A
    *cancelled* confirm has no card at all — the view deletes itself, and
    the panel behind it, unchanged, is the answer.)
    """
    return [
        _text(
            f"## 🚫 Connection Removed\nYou and **{name_fn(user_id)}** are "
            "no longer connected. You've both been sent a DM saying so."
        ),
        _text(_AUDIT_LINE),
    ]


def build_remove_missing_layout(
    *, user_id: int, name_fn: NameFn
) -> list[discord.ui.Item[Any]]:
    """Confirmed, but the pair had already gone. **Paradigm 7 — receipt.**

    The other member removed it first, or an erasure took it. The ❌ leads
    the heading, the way this feature's other did-not-happen cards do ("❌
    Request Declined", "❌ Server Unavailable"): the press did not do the
    thing it offered to do, and that is the first thing the card should say.
    """
    return [
        _text(
            f"## ❌ Nothing to Remove\nYou and **{name_fn(user_id)}** were "
            "no longer connected — they may have removed it first."
        )
    ]


# ── 5. Decision card ─────────────────────────────────────────────────


def build_consent_action_row() -> discord.ui.ActionRow[Any]:
    """The three decision buttons, with the ids that survive a restart.

    Built on its own as well as inside the card so the cog can register one
    persistent view at load without inventing placeholder request data for a
    card that is never sent.
    """
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=DM_CONSENT_ACCEPT_CUSTOM_ID,
        ),
        discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.danger,
            custom_id=DM_CONSENT_DENY_CUSTOM_ID,
        ),
        discord.ui.Button(
            label="Decline + Note",
            style=discord.ButtonStyle.secondary,
            custom_id=DM_CONSENT_DENY_REPLY_CUSTOM_ID,
        ),
    )


def build_request_dm_layout(
    *,
    requester_id: int,
    requester_avatar_url: str,
    guild_name: str,
    request_type: Optional[str],
    reason: str,
    expires_at: int,
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """The request as it lands in the target's DMs. **Paradigm 5 — decision
    card**: who is asking and for what, their reason as a quote, the
    decision buttons, edited into a receipt when answered.

    The requester takes the Thumbnail accessory on the opening Section — the
    embed's ``set_author`` slot, which is the one thing worth a picture on a
    "someone wants to DM you" card. ``brand_dm_layout`` therefore leaves the
    guild icon off this card (a Section's accessory is already spoken for)
    and appends only the server-name line, so the first sentence names the
    server itself: which server a request comes from is the first thing a
    reader asks, and a display name is per-server.

    ``expires_at`` is a Unix time rendered ``<t:…:R>``, so the card says
    "in 23 hours" on the reader's clock and keeps counting down; a fixed
    "24 hours" was true only at the moment of sending.
    """
    words = _words(request_type)
    items: list[discord.ui.Item[Any]] = [
        discord.ui.Section(
            _text(
                f"## 📨 {words.label} Request\n"
                f"**{name_fn(requester_id)}** from {guild_name} {words.asks}."
            ),
            accessory=discord.ui.Thumbnail(requester_avatar_url),
        ),
    ]
    if reason:
        items.append(_text(_quote(reason)))
    items.append(build_consent_action_row())
    items.append(
        _text(
            f"-# Expires <t:{expires_at}:R> • you can undo this later in "
            "DM Settings."
        )
    )
    return items


# ── 7. Receipts and notices ──────────────────────────────────────────


def build_acceptance_layout(
    *,
    viewer_id: int,
    requester_id: int,
    target_id: int,
    request_type: Optional[str],
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """Accepted, for both DMs and the card being replaced. **Paradigm 7.**

    Built once per recipient: ``viewer_id`` is whichever of the two is
    reading, and the card names the *other* one. The decision card being
    edited in place sits in the target's DMs, so it is built for the target.
    """
    other = target_id if viewer_id == requester_id else requester_id
    return [
        _text(
            "## ✅ Connected\n"
            f"You and **{name_fn(other)}** {_words(request_type).grants}."
        ),
        _text(f"-# Either of you can undo this in DM Settings.\n{_AUDIT_LINE}"),
    ]


def build_denial_layout_for_view(
    *,
    requester_id: int,
    request_type: Optional[str],
    reply: str = "",
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """Declined, as the decliner's own DM is edited to show. **Paradigm 7.**

    ``reply`` echoes the note they chose to send, so the card they keep says
    what the requester was told. The requester's reason is not repeated: it
    was on the card this one replaces, and the decision has been made.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            "## ❌ Request Declined\n"
            f"You declined **{name_fn(requester_id)}**'s "
            f"{_words(request_type).noun}."
        )
    ]
    if reply:
        items.append(_text(f"**Your note**\n{_quote(reply)}"))
    return items


def build_denial_layout_for_requester(
    *,
    target_id: int,
    guild_name: str,
    request_type: Optional[str],
    reply: str = "",
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """Declined, as the requester hears it. **Paradigm 7 — receipt.**

    The note is the decliner's words, so it is labelled with their name:
    unlabelled under "declined your request" it would read as the reason
    for declining rather than a message to the reader.
    """
    target = name_fn(target_id)
    items: list[discord.ui.Item[Any]] = [
        _text(
            "## ❌ Request Declined\n"
            f"**{target}** declined your {_words(request_type).noun} in "
            f"{guild_name}."
        )
    ]
    if reply:
        items.append(_text(f"**Note from {target}**\n{_quote(reply)}"))
    return items


def build_request_sent_layout(
    *,
    target_id: int,
    guild_name: str,
    request_type: Optional[str],
    reason: str,
    expires_at: int,
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """Delivered, DM'd back to the requester. **Paradigm 7 — receipt.**

    The one receipt that echoes the reason: it is the requester's record of
    what they sent, quoted straight under the sentence that says so.

    A no-contact pair never reaches this card: the pre-check answers as
    though the target's DMs were closed, which is the refusal a blocked
    requester must not be able to tell apart from an ordinary one.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            "## 📨 Request Sent\n"
            f"Your {_words(request_type).noun} to **{name_fn(target_id)}** "
            f"in {guild_name} has been delivered."
        )
    ]
    if reason:
        items.append(_text(_quote(reason)))
    items.append(
        _text(
            f"-# You'll get a DM when they answer • expires <t:{expires_at}:R>\n"
            f"{_AUDIT_LINE}"
        )
    )
    return items


def build_expired_layout(
    *,
    target_id: int,
    guild_name: str,
    request_type: Optional[str],
    expiry_label: str,
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """Aged out unanswered, DM'd to the requester. **Paradigm 7.**

    ``expiry_label`` stays a fixed "24 hours" here: the window is over, so
    a counting timestamp has nothing left to count.
    """
    return [
        _text(
            "## ⌛ Request Expired\n"
            f"Your {_words(request_type).noun} to **{name_fn(target_id)}** "
            f"in {guild_name} went unanswered for {expiry_label}."
        ),
        _text(f"-# You can send a new one from {_HUB_NAME}."),
    ]


def build_revoked_layout(
    *,
    viewer_id: Optional[int],
    actor_id: int,
    other_id: int,
    name_fn: NameFn,
) -> list[discord.ui.Item[Any]]:
    """Connection removed — both DMs and the in-server card. **Paradigm 7.**

    Symmetric on purpose: the card says the pair is no longer connected,
    not who pressed Remove. The other party can work that out, and a card
    that announces it reads as an accusation. ``viewer_id`` picks the "You
    and **X**" form for a DM; ``None`` is the request card edited in a
    channel, which both parties can read and so names both.
    """
    if viewer_id is None:
        sentence = (
            f"**{name_fn(actor_id)}** and **{name_fn(other_id)}** are no "
            "longer connected."
        )
    else:
        other = other_id if viewer_id == actor_id else actor_id
        sentence = f"You and **{name_fn(other)}** are no longer connected."
    return [
        _text(f"## 🚫 Connection Removed\n{sentence}"),
        _text(
            f"-# Either of you can send a new request from {_HUB_NAME}.\n"
            f"{_AUDIT_LINE}"
        ),
    ]


def build_stale_request_layout() -> list[discord.ui.Item[Any]]:
    """Clicked on a request that is already resolved. **Paradigm 7.**

    Which of answered / expired / cancelled it was is deliberately not
    guessed at: the row is gone, so the card says only that it is gone.
    """
    return [
        _text(
            "## ⌛ Request No Longer Active\n"
            "This request was already answered, expired, or cancelled — "
            "there's nothing left to do with it."
        )
    ]


def build_guild_unavailable_layout() -> list[discord.ui.Item[Any]]:
    """The bot has left the guild the request belongs to. **Paradigm 7.**"""
    return [
        _text(
            "## ❌ Server Unavailable\n"
            "The server this request came from is no longer reachable, so "
            "it can't be answered."
        )
    ]


def build_audit_notice_layout(
    *, message: str, timestamp: int, art: PanelArt | None = None
) -> list[discord.ui.Item[Any]]:
    """One line for the mod-visible audit feed. **Paradigm 7 — notice.**

    ``timestamp`` is a Unix time rendered ``<t:…:f>`` in the subtext: a
    record card keeps its timestamp, and ``embed.timestamp`` has no layout
    equivalent. ``art`` frames it in the guild's thin card strips
    (``core.layout.card_strips``; Ben, 2026-09-30).
    """
    top, bottom = card_strips(art)
    return [
        *top,
        _text(f"## 📜 DM Permission Audit\n{message}\n-# <t:{timestamp}:f>"),
        *bottom,
    ]


def request_type_options() -> list[discord.RadioGroupOption]:
    """The request form's DM-or-friend radio, with a line on each.

    Lives here rather than in the modal so the sample gallery can render the
    same vocabulary the form shows, and so the two labels have one home.
    """
    return [
        discord.RadioGroupOption(
            label=request_type_label("dm"),
            value=normalize_request_type("dm"),
            description="Permission to message you here, on this server.",
            default=True,
        ),
        discord.RadioGroupOption(
            label=request_type_label("friend"),
            value=normalize_request_type("friend"),
            description="You'd also like to add them on Discord itself.",
        ),
    ]
