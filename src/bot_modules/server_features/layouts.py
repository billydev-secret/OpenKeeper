"""Components V2 card builders for the Server Features panel.

Pure, like every ``layouts`` module: primitives, a ``logic`` value and a
``name_fn`` in, a list of ``discord.ui.Item`` out. The views in
``cogs.server_features_cog`` add the items and bind behaviour by persistent
``custom_id`` (the hub) or numeric ``id`` (the ephemeral cards).

Bug-list row 259 (Billy, 2026-09-26): "combine the make a ticket, ask poppy,
feature opt-ins, no contact and data services into a single server features
panel … a list of top level function areas that bring up ephemeral messages
with user specific things." The hub is that list; every row but two opens a
flow another feature already owns (ℹ️ Your Info, added the same day, is the
``/info`` card), and the two it builds here — Roles & Pings
and No Contact — are the ones that had no card of their own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

import discord

from bot_modules.core.layout import hub_banner, hub_footer
from bot_modules.member_info.layouts import INFO_TITLE
from bot_modules.server_features.logic import (
    HOLD_TEXT,
    REPLY_DMS_BLURB,
    REPLY_DMS_EMOJI,
    REPLY_DMS_LABEL,
    RoleCard,
)
from bot_modules.shared.name_resolver import NameFn
from bot_modules.no_contact.logic import OwnEntry

# ── Persistent custom ids (the hub) ──────────────────────────────────
#
# Frozen once posted: a hub in a channel carries them, and the cog's one
# registered view dispatches on the id alone.

TICKET_CUSTOM_ID = "server_features:ticket"
ASK_CUSTOM_ID = "server_features:ask"
ROLES_CUSTOM_ID = "server_features:roles"
ASK_TO_DM_CUSTOM_ID = "server_features:ask_to_dm"
DMS_CUSTOM_ID = "server_features:dms"
BREAK_CUSTOM_ID = "server_features:break"
NO_CONTACT_CUSTOM_ID = "server_features:no_contact"
MY_DATA_CUSTOM_ID = "server_features:my_data"
INFO_CUSTOM_ID = "server_features:info"

# ── Transient ids (the ephemeral cards) ──────────────────────────────

DONE_ID = 1
PREV_PAGE_ID = 2
NEXT_PAGE_ID = 3
ADD_ID = 4
CONFIRM_YES_ID = 5
CONFIRM_CANCEL_ID = 6
REPLY_DMS_BUTTON_ID = 7
PING_BUTTON_ID_BASE = 100
MENU_SELECT_ID_BASE = 200
REMOVE_BUTTON_ID_BASE = 300

#: Ten Section rows (30) plus the title, separator, heading, the bottom row
#: with its four buttons and the ``-#`` line is 39 — the guide's "a
#: row-per-button list pages at ten".
MAX_LIST_ROWS = 10

#: The hub's one button label. Every row opens something, and the same word
#: on every row keeps the buttons one size (CLAUDE.md: consistent button
#: shapes). Under ``core.layout.MAX_ACCESSORY_LABEL`` so a phone keeps it
#: beside its row.
OPEN_LABEL = "Open"

_DEFAULT_ASSISTANT = "Poppy"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large)


def _row(title: str, blurb: str, custom_id: str) -> discord.ui.Section[Any]:
    return discord.ui.Section(
        _text(f"**{title}**\n-# {blurb}"),
        accessory=discord.ui.Button(
            label=OPEN_LABEL, style=discord.ButtonStyle.secondary, custom_id=custom_id
        ),
    )


# ── 1. The hub ───────────────────────────────────────────────────────


#: The hub's name — its ``##`` heading, or drawn into the banner
#: (``panel_title_service``) when the guild ticks "Title in the Banner".
SERVER_FEATURES_PANEL_TITLE = "Help & Settings"

#: The No Contact row's line. It has to say what the button does and give the
#: discretion promise as reassurance. The old two clipped sentences ("They
#: aren't told.") read as a warning. Billy, 2026-09-27: "something like
#: Requests for no contact are kept anonymous".
NO_CONTACT_BLURB = "Stop someone reaching you through the bot — requests are kept anonymous."

#: The No Contact card's opening line. It names the kinds of contact rather
#: than listing features, because the list grew past what the old line named
#: (DM requests, payments, bounties and games were gated but missing).
NO_CONTACT_CARD_INTRO = (
    "No one on your list can reach you through the bot — not by DM request, "
    "whisper, reply, game, match or voice room. They are never told they're "
    "on it."
)

_HUB_INTRO = (
    "Everything here is yours to use or change. Each button opens "
    "a private card that only you can see."
)


def build_hub_layout(
    *,
    assistant_name: str = _DEFAULT_ASSISTANT,
    banner_url: Optional[str] = None,
    footer_url: Optional[str] = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The public Server Features panel. **Paradigm 1 — hub panel.**

    Four headed groups, one Section row per area, a visible line between the
    groups — the grouping rule Billy set on the DM Requests hub ("some sort of
    separation", 2026-09-20), and his pick over a dropdown for this one
    (2026-09-26, "Grouped rows"). **About You** — the ``/info`` card, the
    one row that shows rather than changes — sits above Safety & Privacy
    (Billy, 2026-09-26: "do it above safety").

    **Every row always shows.** The hub is posted once and never redrawn, so
    hiding a row because its feature isn't set up would outlive the fix; a
    row whose feature is off answers the press with that feature's own "not
    set up" line instead.

    **Bare**, the guild's hub banner opening it and the footer closing it —
    the DM Requests hub's shape, which was a boxed tile until 2026-09-29
    (Ben: "move the banners out for full width and drop the box").
    ``assistant_name`` is the guild's name for the Ask assistant,
    read when the panel is posted; the registered instance's default never
    renders.

    ``title_in_banner`` means ``banner_url`` already carries
    :data:`SERVER_FEATURES_PANEL_TITLE` drawn in (the DM Requests hub's
    pattern, Billy 2026-09-26): the ``##`` line goes and the banner's alt
    text carries the name. Without a banner it is ignored.
    """
    if title_in_banner and banner_url:
        head = [*hub_banner(banner_url, title=SERVER_FEATURES_PANEL_TITLE), _text(_HUB_INTRO)]
    else:
        head = [
            *hub_banner(banner_url),
            _text(f"## 🧭 {SERVER_FEATURES_PANEL_TITLE}\n{_HUB_INTRO}"),
        ]
    return [
        *head,
        _line(),
        _text("### Get Help"),
        _row("🎫 Open a Ticket", "Talk privately with the mod team.", TICKET_CUSTOM_ID),
        _row(
            f"🤖 Ask {assistant_name}",
            "How does something here work? Just ask.",
            ASK_CUSTOM_ID,
        ),
        _line(),
        _text("### Your Settings"),
        _row(
            "🔔 Roles & Pings",
            "Pick your pings and the roles you wear.",
            ROLES_CUSTOM_ID,
        ),
        # Above DM Settings, as on the DM Requests panel: reaching out to
        # someone, then your own preference (Ben, 2026-09-30: "to the left
        # of the dm preferences"). The panel's Open Form button, moved here
        # so its channel can be dropped.
        _row("📨 Ask to DM", "Ask a member if you may DM them.", ASK_TO_DM_CUSTOM_ID),
        _row("📬 DM Settings", "Choose who can DM you through the bot.", DMS_CUSTOM_ID),
        _row("💤 Take a Break", "Take time away without losing your roles.", BREAK_CUSTOM_ID),
        _line(),
        _text("### About You"),
        _row(INFO_TITLE, "See your level, rank, activity, roles and opt-ins.", INFO_CUSTOM_ID),
        _line(),
        _text("### Safety & Privacy"),
        _row(
            "🛡️ No Contact",
            NO_CONTACT_BLURB,
            NO_CONTACT_CUSTOM_ID,
        ),
        _row(
            "📜 My Data",
            "See what's stored about you and delete what's yours.",
            MY_DATA_CUSTOM_ID,
        ),
        *hub_footer(footer_url),
    ]


# ── 2. Roles & Pings ─────────────────────────────────────────────────

_ROLES_TITLE = "## 🔔 Roles & Pings"


def build_roles_layout(card: RoleCard, *, held: bool = False, note: str = "") -> list[
    discord.ui.Item[Any]
]:
    """The member's own roles card. **Paradigm 2 — settings panel.**

    Three sections: the bot's own pings, one On/Off row each; the bot's DMs
    (the Auto-Thread reply opt-in, when the guild offers it), drawn the same
    way; and one dropdown per question in Discord's Customize page,
    pre-selected with what the member wears. What the card may offer — and the rules that keep a gate,
    a hold or an unsafe role off it — is ``server_features.logic``'s job;
    this only draws the answer.

    Each ping row states its setting in words ("On"/"Off") and its button
    says what a press does, so the state reads without colour (guide
    § Controls: a button is grey unless its colour means something).

    ``held`` is a member under a jail, an inactive hold or a break: the card
    says so and offers nothing. ``note`` is the answer to the press that
    caused this redraw, in the ``-#`` slot.
    """
    if held:
        return [_text(f"{_ROLES_TITLE}\n{HOLD_TEXT}"), _done_row()]
    if card.empty:
        return [
            _text(
                f"{_ROLES_TITLE}\nThere's nothing to pick here yet — the server "
                "hasn't set up any roles members can choose for themselves."
            ),
            _done_row(),
        ]

    items: list[discord.ui.Item[Any]] = [
        _text(f"{_ROLES_TITLE}\nChanges apply the moment you make them."),
    ]
    if card.pings:
        items.append(_line())
        items.append(_text("### Bot Pings"))
        for index, row in enumerate(card.pings):
            state = "On" if row.held else "Off"
            items.append(
                discord.ui.Section(
                    _text(f"{row.emoji} **{row.label}** · {state}\n-# {row.blurb}"),
                    accessory=discord.ui.Button(
                        label="Turn Off" if row.held else "Turn On",
                        style=discord.ButtonStyle.secondary,
                        id=PING_BUTTON_ID_BASE + index,
                    ),
                )
            )
    if card.reply_dms is not None:
        state = "On" if card.reply_dms else "Off"
        items.append(_line())
        items.append(_text("### Bot DMs"))
        items.append(
            discord.ui.Section(
                _text(f"{REPLY_DMS_EMOJI} **{REPLY_DMS_LABEL}** · {state}\n-# {REPLY_DMS_BLURB}"),
                accessory=discord.ui.Button(
                    label="Turn Off" if card.reply_dms else "Turn On",
                    style=discord.ButtonStyle.secondary,
                    id=REPLY_DMS_BUTTON_ID,
                ),
            )
        )
    if card.menus:
        items.append(_line())
        items.append(_text("### From Customize"))
        for index, menu in enumerate(card.menus):
            items.append(_text(f"**{menu.title}**"))
            items.append(
                discord.ui.ActionRow(
                    discord.ui.Select(
                        id=MENU_SELECT_ID_BASE + index,
                        placeholder="Pick one…" if menu.single_select else "Pick any…",
                        min_values=menu.min_values,
                        max_values=menu.max_values,
                        options=[
                            discord.SelectOption(
                                label=option.label,
                                value=option.value,
                                description=option.description or None,
                                emoji=option.emoji or None,
                                default=option.selected,
                            )
                            for option in menu.options
                        ],
                    )
                )
            )
    items.append(_done_row())
    if note:
        items.append(_text(f"-# {note}"))
    elif card.menus_truncated:
        items.append(
            _text("-# More roles are in Discord's **Channels & Roles** page.")
        )
    return items


def _done_row() -> discord.ui.ActionRow[Any]:
    return discord.ui.ActionRow(
        discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, id=DONE_ID)
    )


# ── 2 + 6. No Contact ────────────────────────────────────────────────

_NO_CONTACT_TITLE = "## 🛡️ No Contact"


def list_page(
    entries: Sequence[OwnEntry], page: int
) -> tuple[int, int, list[OwnEntry]]:
    """Clamp ``page`` and slice it: ``(page, page_count, rows)``. Shared by
    the builder and the view so the rows drawn and the Remove buttons bound
    can't disagree."""
    page_count = max(1, -(-len(entries) // MAX_LIST_ROWS))
    page = min(max(page, 0), page_count - 1)
    start = page * MAX_LIST_ROWS
    return page, page_count, list(entries[start : start + MAX_LIST_ROWS])


def build_no_contact_layout(
    entries: Sequence[OwnEntry], *, name_fn: NameFn, page: int = 0, note: str = ""
) -> list[discord.ui.Item[Any]]:
    """The member's own no-contact list. **Paradigm 2 + 6** — a settings
    card whose body is a list with per-row actions, paged at ten.

    Replaced ``/nocontact add``, ``remove`` and ``list`` (2026-09-26). The
    list shows only what ``no_contact_logic.own_entries`` lets this member
    see: an entry the *other* person made against them is never here, and an
    entry they can't lift (a moderator's, or a mutual one) is tagged "set by
    a moderator" with no button.

    The names are shown plainly, unlike the ``User <id>`` a no-contact pair
    gets on a card someone else reads: this card is read by the person who
    made the entry, about the person they made it against.
    """
    page, page_count, rows = list_page(entries, page)
    items: list[discord.ui.Item[Any]] = [
        _text(f"{_NO_CONTACT_TITLE}\n{NO_CONTACT_CARD_INTRO}"),
        _line(),
    ]
    if rows:
        items.append(_text("### Your List"))
        for index, entry in enumerate(rows):
            name = discord.utils.escape_markdown(name_fn(entry.other_id))
            if entry.removable:
                items.append(
                    discord.ui.Section(
                        _text(f"**{name}**"),
                        accessory=discord.ui.Button(
                            label="Remove",
                            style=discord.ButtonStyle.danger,
                            id=REMOVE_BUTTON_ID_BASE + index,
                        ),
                    )
                )
            else:
                items.append(_text(f"**{name}** · set by a moderator"))
    else:
        items.append(_text("### Your List\nNobody yet."))

    bottom: list[discord.ui.Button[Any]] = []
    if page_count > 1:
        bottom.extend(
            (
                discord.ui.Button(
                    label="◀", style=discord.ButtonStyle.secondary,
                    id=PREV_PAGE_ID, disabled=page == 0,
                ),
                discord.ui.Button(
                    label="▶", style=discord.ButtonStyle.secondary,
                    id=NEXT_PAGE_ID, disabled=page == page_count - 1,
                ),
            )
        )
    bottom.append(
        discord.ui.Button(label="Add Someone", style=discord.ButtonStyle.secondary, id=ADD_ID)
    )
    bottom.append(
        discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, id=DONE_ID)
    )
    items.append(discord.ui.ActionRow(*bottom))

    if note:
        items.append(_text(f"-# {note}"))
    elif page_count > 1:
        items.append(_text(f"-# Page {page + 1} of {page_count} • {len(entries)} people"))
    return items


def build_remove_confirm_layout(*, user_id: int, name_fn: NameFn) -> list[
    discord.ui.Item[Any]
]:
    """The gate in front of a Remove press. **Paradigm 10 — confirm.**"""
    return [
        _text(
            "## ⚠️ Remove From Your List?\n"
            f"**{discord.utils.escape_markdown(name_fn(user_id))}** would be able to reach you through the "
            "bot again. They aren't told either way."
        ),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Yes, Remove", style=discord.ButtonStyle.danger, id=CONFIRM_YES_ID
            ),
            discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.secondary, id=CONFIRM_CANCEL_ID
            ),
        ),
    ]


def build_no_contact_answer_layout(message: str) -> list[discord.ui.Item[Any]]:
    """The answer to an add, a remove or a refusal. **Paradigm 7 — receipt.**

    ``message`` is one of ``no_contact_logic``'s outcome strings, already led
    by its ✅ or ❌ — the same string for a fresh add and a repeat, and for a
    missing entry and one that isn't the member's, as the spec requires.
    """
    return [_text(f"{_NO_CONTACT_TITLE}\n{message}")]
