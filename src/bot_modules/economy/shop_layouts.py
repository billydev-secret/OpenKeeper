"""Components V2 card builders for the perk shop.

The V2 replacement for this package's embed builders in ``shop.py``
(``build_store_embed``, ``build_panel_embed``, ``build_shop_embed`` — deleted
with this conversion, ``docs/plans/components_v2_migration.md`` wave 3a).
Every function here is **pure**: primitives in, a list of ``discord.ui.Item``
out. No IO, no accent lookup, no callbacks — the cog's ``LayoutView``
subclasses add the items in order and bind behaviour by ``find_item(id)`` or
by the persistent ``custom_id``s below.

``shop.py`` keeps the pure *availability* logic (``stocked_perks``,
``shop_sections``, ``shop_pages``, pricing) — what sections and pages this
guild's shop has at all. This module adds the *row* decision that used to
live partly in ``build_shop_embed``'s per-perk ``_line()`` and partly in the
cog's ``_ShopView.__init__`` (which button each row shows, and why a row is
disabled): a converted card has no separate price table above its buttons, so
each row now carries its own price and status text, and that decision belongs
beside the other rendering rules rather than split across a service module
and a cog.

Conventions the guide fixes and this module follows throughout:

* **A card is bare** — no ``Container``, no accent colour.
* **A card's own heading is one glyph-led ``## Title`` line** in its first
  Text Display.
* **A row's disabled reason lives on the row**, not in a card-level ``-#``
  line — the guide's paradigm 6 shape for "shop, connections, bounties,
  auctions": a Section per item, its own button, its own state.
* **Cancel is silent** — a confirm's Back/Cancel deletes the ephemeral
  message rather than editing it to a "Nothing changed" card.
* **Non-wrapping pager**: ◀ / ▶ disabled at the ends, never wrapping round.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import discord

from bot_modules.core.layout import hub_banner, hub_footer
from bot_modules.docs.help_card import build_doc_help_layout
from bot_modules.economy.perks import (
    CUSTOMISE_LABELS,
    NO_CONFIG_PERKS,
    PERK_BLURBS,
    PERK_EMOJI,
    PERK_LABELS,
    SELF_PERKS,
    perk_on_sale,
)
from bot_modules.economy.rentals import prorated_refund
from bot_modules.economy.shop import (
    SECTION_BLURBS,
    SECTION_CAPTIONS,
    SECTION_COSMETICS,
    SECTION_GAMES,
    SECTION_SERVER,
    SECTION_SPECIALS,
    SECTION_YOURS,
    shop_row_price,
)
from bot_modules.economy.shop_items import (
    BILLING_ONCE,
    BILLING_WEEKLY,
    KIND_MANUAL,
    KIND_ROLE,
    ItemView,
)
from bot_modules.economy import raffle_service as raffle_svc
from bot_modules.economy.pin_service import pin_enabled, pin_price
from bot_modules.economy.service import EconSettings
from bot_modules.economy.theme_service import theme_enabled

# ── Persistent custom ids ────────────────────────────────────────────
#
# The channel panel's buttons are sticky, static-custom_id items
# (re-registered on every restart in ``cog_load``), so their ids are part of
# the feature's on-disk state exactly like the DM panel's.
#
# ``SHOP_PANEL_OPEN_CUSTOM_ID`` was the single Open Shop button before the
# split by section, and briefly (2026-09-26) the stock list's one Browse. No
# panel carries it now, but a posted one still may, so it stays registered
# and opens the section chooser — kept byte-for-byte, as is
# ``ShopRentButton``'s older per-perk template, which routes the same way.

SHOP_PANEL_OPEN_CUSTOM_ID = "econ_shop_open"

#: The Help beside the panel's title (and the chooser's), opening the
#: guild's shop FAQ doc.
SHOP_HELP_CUSTOM_ID = "econ_shop_help"

#: One Browse button per section, beside its row on the panel and on the
#: chooser, plus Manage on Your Rentals.
SHOP_SECTION_CUSTOM_IDS = {
    section: f"econ_shop_open:{section}"
    for section in (
        SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES,
        SECTION_YOURS,
    )
}

# ── Transient component ids ──────────────────────────────────────────
#
# Every other control here lives on an ephemeral, per-member view, so it is
# looked up by numeric id rather than by custom_id — nothing outside the open
# menu ever dispatches to it.

#: A section's action rows, assigned in the order ``shop_section_rows``
#: emits them. At most eight rows exist today (six self-perks, or four
#: server products, or two games products, plus the trailing refund entry),
#: well inside the arrows' own id range below.
ACTION_ROW_ID_BASE = 100
PREV_PAGE_ID = 1
NEXT_PAGE_ID = 2
#: The disabled caption chip between the arrows — a section's name, not a
#: control (see ``_nav_row``).
PAGE_CHIP_ID = 3
#: The Server Store's buy buttons, one per row on the page (ten per page).
STORE_ROW_ID_BASE = 200
#: The refund picker's cancel buttons, one per cancellable rental + shield.
REFUND_ROW_ID_BASE = 300
REFUND_CONFIRM_ID = 10
REFUND_BACK_ID = 11
POST_RENT_CUSTOMISE_ID = 20

#: What one line costs beside a Section's button accessory on a phone, in
#: ``core.layout.text_columns``. It is the label, not the wrap, that drops a
#: button under its row (corrected 2026-09-24: Billy's phone screenshot
#: showed "Browse Colors" — a 13-column *label* — drop while "Set Color"
#: (9) stayed, and the row text next to "Set Color" held 32 columns of
#: blurb on one line without dropping anything) — but a button held to
#: ``core.layout.MAX_ACCESSORY_LABEL`` (10 columns) still only leaves the
#: text column beside it about 30 columns wide before Discord wraps it,
#: which is its own problem (a wrapped *text* line just grows the row a
#: line taller, but a needlessly long line reads worse on a phone than a
#: short one does), so every row line is held to this number too.
MAX_ROW_CHARS = 30

#: A Server Store item's name is the only bit in ``_store_row`` an admin can
#: type to any length (the dashboard caps it at 60 chars — ``routes/
#: economy.py``'s ``ShopItemBody``). Since the name now has the row's whole
#: title line to itself (2026-09-24: name / blurb / price each on their own
#: line), it's clipped to the same per-line budget every row line holds to
#: rather than a fixed-width cell shared with the price, mirroring
#: ``bounty_layouts.HUB_TITLE_CLIP``'s fixed-width clip for the same reason.
STORE_NAME_CLIP = MAX_ROW_CHARS

# One-line descriptions for the section rows that aren't a self-perk (a
# self-perk row's description is ``perks.PERK_BLURBS`` instead) — each row's
# second line, held under ``MAX_ROW_CHARS`` by ``tests/economy/test_shop_layouts.py``
# exactly like the perk blurbs are.
PIN_DESC = "Your message, pinned for a day"
THEME_DESC = "A whole day on your theme"
SHIELD_DESC = "Covers one missed streak day"
RAFFLE_DESC = "A ticket in this week's draw"
#: The refund row's own description — unlike every other row, it doubles as
#: ``ShopRow.note`` here rather than ``ShopRow.desc`` (see the field's
#: comment): the row has no price to sit beside a status note, so the note
#: carries the whole third line on its own.
REFUND_DESC = "End a rental early"

# A Server Store item with no admin-typed blurb (``ItemView.blurb == ""``)
# gets one of these instead, keyed by ``(item.kind, item.billing)`` — a
# catalogue row is never two lines (2026-09-24, at Billy's request: "make
# sure all the text elements are there for every item"). Held under
# ``MAX_ROW_CHARS`` like every other row line (all four are well inside it).
STORE_FALLBACK_BLURBS = {
    (KIND_ROLE, BILLING_ONCE): "A role, granted when you buy",
    (KIND_ROLE, BILLING_WEEKLY): "A role, rented by the week",
    (KIND_MANUAL, BILLING_ONCE): "Delivered by a mod",
    (KIND_MANUAL, BILLING_WEEKLY): "Delivered by a mod, weekly",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` visible chars with a trailing ``…``.

    ``limit <= 0`` drops the text entirely. No caller passes one today — a
    store row's name and blurb each get their own fixed line budget
    (``STORE_NAME_CLIP`` / ``MAX_ROW_CHARS``) since the three-line row of
    2026-09-24 — but the guard stays for a caller that computes a shrinking
    budget the way this row used to.
    """
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _nav_row(page: int, nav: list[str]) -> discord.ui.Item[Any] | None:
    """The shop's non-wrapping ◀ / chip / ▶ triple, or None for a one-page shop.

    Each panel button opens one section's own book (``shop.section_book``),
    so in practice only a Specials run past ten items has pages to turn; the
    arrows never cross into the next shelf. Unlike the embed era's wrapping arrows, the ends disable rather than
    stepping round — the guide's pager shape (§ Controls).

    The middle button is a disabled caption chip (back at Billy's request,
    2026-09-24, after a spell with no chip at all): bare arrows moved a member
    without ever saying where they landed, and a greyed chip is how this shop
    already says "you are here" elsewhere on the same card (🎙️ Leased,
    🛡️ Held). It carries ``nav[page]`` — the same caption
    ``shop.page_captions`` hands every other reader of a page number, so a
    spread Specials page reads "🎁 Specials · 11–20" and every other page its
    bare section caption. It has no callback (nothing to press) and the
    ``-#`` line below still carries the page count — the chip says *which*
    page, the count says *how many*.
    """
    if len(nav) < 2:
        return None
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="◀", style=discord.ButtonStyle.secondary,
            id=PREV_PAGE_ID, disabled=page == 0,
        ),
        discord.ui.Button(
            label=nav[page][:80], style=discord.ButtonStyle.secondary,
            id=PAGE_CHIP_ID, disabled=True,
        ),
        discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary,
            id=NEXT_PAGE_ID, disabled=page == len(nav) - 1,
        ),
    )


def _page_line(page: int, nav: list[str]) -> discord.ui.Item[Any] | None:
    if len(nav) < 2:
        return None
    return _text(f"-# Page {page + 1} of {len(nav)}")


# ── 1. Hub panel (the channel poster) ────────────────────────────────

#: The hub's name — its text heading, or drawn into the banner.
SHOP_PANEL_TITLE = "Perk Shop"

#: The dashboard Docs key the Help button reads. Renaming the doc on the
#: dashboard disconnects the button — it then shows :data:`_HELP_MISSING`.
SHOP_HELP_DOC_KEY = "shop"


def _help_button() -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label="Help",
        style=discord.ButtonStyle.secondary,
        custom_id=SHOP_HELP_CUSTOM_ID,
    )


def _section_row(section: str) -> discord.ui.Section[Any]:
    """One chooser row: the section's name and blurb, its button beside it."""
    return discord.ui.Section(
        _text(f"**{SECTION_CAPTIONS[section]}**\n-# {SECTION_BLURBS[section]}"),
        accessory=discord.ui.Button(
            label="Manage" if section == SECTION_YOURS else "Browse",
            style=discord.ButtonStyle.secondary,
            custom_id=SHOP_SECTION_CUSTOM_IDS[section],
        ),
    )


def _chooser_rows(sections: list[str]) -> list[discord.ui.Item[Any]]:
    """A row per stocked section, a line, then the member's own rentals."""
    items: list[discord.ui.Item[Any]] = [discord.ui.Separator()]
    if sections:
        items += [_section_row(s) for s in sections]
    else:
        items.append(_text("_Nothing is for sale right now._"))
    items += [discord.ui.Separator(), _section_row(SECTION_YOURS)]
    return items


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large)


def _panel_row(section: str) -> discord.ui.Section[Any]:
    """One panel row: the section's ``###`` caption over its blurb, its
    button beside it — **Browse** on a shelf, **Manage** on Your Rentals."""
    return discord.ui.Section(
        _text(f"### {SECTION_CAPTIONS[section]}\n-# {SECTION_BLURBS[section]}"),
        accessory=discord.ui.Button(
            label="Manage" if section == SECTION_YOURS else "Browse",
            style=discord.ButtonStyle.secondary,
            custom_id=SHOP_SECTION_CUSTOM_IDS[section],
        ),
    )


def build_shop_panel_layout(
    sections: list[str],
    *,
    banner_url: str | None = None,
    footer_url: str | None = None,
    title_in_banner: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The channel shop panel: a row per section, each with its own
    **Browse** beside it.

    **Paradigm 1's rows in the hub's frame** (Billy, 2026-09-26: "inline
    buttons with each section"). A row per stocked section
    (``shop.shop_sections``, read for nobody): its ``###`` caption over the
    section's blurb (``SECTION_BLURBS``, the same line the private chooser
    shows) and a grey **Browse** beside it, opening just that section
    privately. A line sits between rows, then **🧾 Your Rentals** with
    **Manage**, opening the member's own rentals with customise and Cancel &
    Refund. That row is always there, because a perk stays reachable after
    its section stops selling.

    The rows **describe** each section and do not list its stock. Billy,
    bug-list row 260, 2026-09-27, reversing the stock list he asked for on
    09-26 (02508aa6). A list of names changed every time the stock did, and
    it still sent the reader to Browse for the rest. No prices and no member
    state either: the panel is the same for everyone, and balance, prices,
    owned marks and gated rows all live one tap away, correct for whoever
    opens them.

    The card is bare — the hub archetype DM Requests set (style guide § No
    container; the hubs' box went on 2026-09-29). ``banner_url``
    opens it and ``footer_url`` closes it (``core.layout.hub_banner`` /
    ``hub_footer``); ``title_in_banner`` means the banner already carries
    :data:`SHOP_PANEL_TITLE` drawn in (``panel_title_service``), so the
    ``##`` heading goes, the title becomes the banner's alt text, and
    **Help** moves up beside the intro line. It needs a banner — without
    one the heading stays.

    **Help** beside the title opens the guild's shop FAQ doc
    (:data:`SHOP_HELP_DOC_KEY`, ``build_shop_help_layout``) — the long
    explanation lives there, never as prose on the card.
    """
    intro = _text("Rent a perk or buy what the server sells.")
    head: list[discord.ui.Item[Any]]
    if title_in_banner and banner_url:
        head = [
            *hub_banner(banner_url, title=SHOP_PANEL_TITLE),
            discord.ui.Section(intro, accessory=_help_button()),
        ]
    else:
        head = [
            *hub_banner(banner_url),
            discord.ui.Section(
                _text(f"## 🛍️ {SHOP_PANEL_TITLE}"), accessory=_help_button()
            ),
            intro,
        ]
    body: list[discord.ui.Item[Any]] = []
    for section in sections:
        body += [_line(), _panel_row(section)]
    if not sections:
        body += [_line(), _text("_Nothing is for sale right now._")]
    foot: list[discord.ui.Item[Any]] = [
        _line(),
        _panel_row(SECTION_YOURS),
    ]
    return [*head, *body, *foot, *hub_footer(footer_url)]


def build_shop_chooser_layout(sections: list[str]) -> list[discord.ui.Item[Any]]:
    """``/bank shop``: a row per shelf, privately. **Paradigm 1's rows on
    an ephemeral card.**

    A **Browse** per stocked section and **Manage** on the member's own
    rentals — the channel panel's buttons, with a blurb where the panel
    lists the stock. Bare, with no art and no box: an
    ephemeral card is one member's working surface (style guide § Media,
    the hub banner's exception covers channel panels only).
    """
    return [
        discord.ui.Section(
            _text(f"## 🛍️ {SHOP_PANEL_TITLE}"), accessory=_help_button()
        ),
        _text("Pick a shelf, or manage what you rent."),
        *_chooser_rows(sections),
    ]


#: The Help card's title when the doc opens without a heading of its own.
SHOP_HELP_TITLE = "Perk Shop Help"

_HELP_MISSING = (
    "There's no shop FAQ on this server yet — ask a moderator if something "
    "here isn't clear."
)


def build_shop_help_layout(body_md: str | None) -> list[discord.ui.Item[Any]]:
    """The shop hub's Help answer, ephemeral. **Paradigm 7 — notice.**

    The guild's ``shop`` doc as an admin last saved it on the dashboard,
    read at click time so the card and the FAQ can't disagree; reading it
    is the shared ``docs.help_card`` builder's job (style guide § Controls).
    """
    return build_doc_help_layout(
        body_md, glyph="🛍️", fallback_title=SHOP_HELP_TITLE, missing_text=_HELP_MISSING
    )


# ── 6. Server Store ───────────────────────────────────────────────────


def _store_row(
    settings: EconSettings, item: ItemView, index: int, owned: bool
) -> discord.ui.Section[Any]:
    """One Server Store item, its own Buy button beside it. **Paradigm 6.**

    Three lines, always (2026-09-24, at Billy's request — "make sure all the
    text elements are there for every item"): the name alone, a blurb line,
    then the price and status bit in ``-#`` subtext — replacing the single
    ``·``-joined line this row used to fold everything onto. The blurb line
    is the admin's when they set one, else a ``STORE_FALLBACK_BLURBS`` line
    derived from what the item *is* (a role, or something a mod delivers) —
    a row is never two lines, so an admin who left the field blank doesn't
    read as a row that lost its second line by accident. Name and an admin
    blurb are the only admin-typed, unbounded-length bits in the row — price
    and the status word are short and fixed-shape — so they're the ones
    clipped, each to its own line's budget rather than splitting one shared
    budget between them: the name gets the whole title line (``STORE_NAME_CLIP``
    is ``MAX_ROW_CHARS``), and the blurb is clipped to ``MAX_ROW_CHARS`` too,
    defensively, even though the dashboard already caps it tighter
    (``routes/economy.py``'s ``ShopItemBody``) — a row from before that cap
    existed still has to fit. Both are escaped *after* clipping, the same
    order ``bounty_layouts._bounty_row`` uses for its own admin/member-typed
    title — clip on the raw text so the budget counts visible characters,
    then escape so the row's own ``**{name}**`` wrapping can't be broken by
    markdown the admin typed into the name or blurb. The fallback line is
    this module's own text, not admin input, so it needs neither.
    """
    name = discord.utils.escape_markdown(_clip(item.name, STORE_NAME_CLIP))
    price_bit = f"{settings.currency_emoji} {item.price:,}" + ("/wk" if item.is_rental else "")
    if owned:
        status_bit = "✅ yours"
    elif item.remaining == 0:
        status_bit = "sold out"
    elif item.remaining is not None:
        status_bit = f"{item.remaining} left"
    elif item.per_member_limit == 1:
        status_bit = "one each"
    else:
        status_bit = None
    price_bits = [price_bit] + ([status_bit] if status_bit else [])
    if item.blurb:
        blurb_line = discord.utils.escape_markdown(_clip(item.blurb, MAX_ROW_CHARS))
    else:
        blurb_line = STORE_FALLBACK_BLURBS[(item.kind, item.billing)]
    lines = [f"**{name}**", blurb_line, "-# " + " · ".join(price_bits)]
    text = "\n".join(lines)
    return discord.ui.Section(
        _text(text),
        accessory=discord.ui.Button(
            label="Buy",
            style=discord.ButtonStyle.primary,
            disabled=owned or item.remaining == 0,
            id=STORE_ROW_ID_BASE + index,
        ),
    )


def build_store_layout(
    settings: EconSettings,
    items: list[ItemView],
    *,
    owned_item_ids: set[int] | frozenset[int],
    balance: int,
    page: int,
    nav: list[str],
) -> list[discord.ui.Item[Any]]:
    """One page of the Server Store. **Paradigm 6 — list with per-row actions.**

    Ten rows a page (``shop.STORE_PAGE_SIZE``) — a row-per-button list pages
    at ten, per the guide, and the store's own page slicing already does
    that; this just renders the slice it's handed.
    """
    items_out: list[discord.ui.Item[Any]] = [
        _text(
            f"## {SECTION_CAPTIONS[SECTION_SPECIALS]}\n"
            f"Everything this server sells, beyond the built-in perks. You "
            f"have {settings.currency_emoji} **{balance:,}**."
        ),
    ]
    if items:
        for index, item in enumerate(items):
            items_out.append(
                _store_row(settings, item, index, item.item_id in owned_item_ids)
            )
    else:
        items_out.append(_text("_Nothing on the shelves yet._"))
    nav_row = _nav_row(page, nav)
    if nav_row is not None:
        items_out.append(nav_row)
    line = _page_line(page, nav)
    items_out.append(line if line is not None else _text("-# Pick one below to buy it."))
    return items_out


# ── 6. Section rows (cosmetics / server / games) ─────────────────────


class ShopRow(NamedTuple):
    """One section row: rendered by ``build_shop_section_layout``, bound by
    the cog's view. ``shop_section_rows`` is the single source of these, so
    the card and the view that binds it can never disagree about what row N
    means — the same relationship ``connection_page`` has to the DM
    settings panel.
    """

    row_id: int
    kind: str  # "rent" | "cfg" | "active" | "colors" | "icons" | "voice" |
    # "pin" | "theme" | "shield" | "raffle" | "refund"
    perk: str  # "" when the row isn't perk-specific
    title: str
    desc: str  # the row's second line — always set, so no row is ever a
    # bare bold title with nothing under it
    price: str  # "" only for the trailing refund row, which has no price —
    # its third line is carried by ``note`` alone instead (below)
    note: str  # every other row's status word; the refund row's whole
    # third line ("unused days credited back"), since it has no price to
    # sit beside
    button_label: str
    button_style: discord.ButtonStyle
    disabled: bool


def shop_section_rows(
    settings: EconSettings,
    section: str,
    *,
    gated: set[str],
    owned: set[str],
    icon_catalog: tuple[int, int, int] | None,
    color_catalog: tuple[int, int, int] | None,
    has_palette: bool,
    shields_held: int,
    refundable: bool,
) -> list[ShopRow]:
    """Which rows this section shows, and each one's state. Pure.

    Mirrors the cog's old ``_ShopView.__init__`` button logic and
    ``build_shop_embed``'s per-perk ``_line()`` price/status note in one
    pass, since a converted row carries both.
    """
    rows: list[ShopRow] = []
    #: Short enough to still fit a row's own line beside the longest label
    #: this section can show — the fuller reason a locked row is locked
    #: lives in the manual, not the row (guide § Screen use on a phone).
    gate_note = "❌ not available"

    if section == SECTION_COSMETICS:
        for perk in SELF_PERKS:
            if not perk_on_sale(settings, perk) and perk not in owned:
                continue
            _sort, price = shop_row_price(settings, perk, icon_catalog, color_catalog)
            price_bit = f"{settings.currency_emoji} {price}/wk"
            desc = PERK_BLURBS[perk]
            if perk == "role_preset":
                if not has_palette:
                    continue
                if perk in gated:
                    note = gate_note
                elif color_catalog is not None:
                    note = f"{color_catalog[2]} colors"
                else:
                    note = ""
                rows.append(ShopRow(
                    ACTION_ROW_ID_BASE + len(rows), "colors", perk,
                    PERK_LABELS[perk], desc, price_bit, note,
                    "🖌️ Change" if perk in owned else "🖌️ Browse",
                    discord.ButtonStyle.success if perk in owned
                    else discord.ButtonStyle.secondary,
                    perk in gated,
                ))
                continue
            if perk == "role_icon" and icon_catalog is not None:
                note = gate_note if perk in gated else f"{icon_catalog[2]} + custom"
                rows.append(ShopRow(
                    ACTION_ROW_ID_BASE + len(rows), "icons", perk,
                    PERK_LABELS[perk], desc, price_bit, note,
                    "🖼️ Change" if perk in owned else "🖼️ Browse",
                    discord.ButtonStyle.success if perk in owned
                    else discord.ButtonStyle.secondary,
                    perk in gated,
                ))
                continue
            if perk in owned and perk in NO_CONFIG_PERKS:
                rows.append(ShopRow(
                    ACTION_ROW_ID_BASE + len(rows), "active", perk,
                    PERK_LABELS[perk], desc, price_bit, "✅ active",
                    f"{PERK_EMOJI[perk]} Active", discord.ButtonStyle.success, True,
                ))
                continue
            if perk in owned:
                rows.append(ShopRow(
                    ACTION_ROW_ID_BASE + len(rows), "cfg", perk,
                    PERK_LABELS[perk], desc, price_bit,
                    gate_note if perk in gated else "✅ owned",
                    CUSTOMISE_LABELS[perk], discord.ButtonStyle.success,
                    perk in gated,
                ))
                continue
            rows.append(ShopRow(
                ACTION_ROW_ID_BASE + len(rows), "rent", perk,
                PERK_LABELS[perk], desc, price_bit,
                gate_note if perk in gated else "",
                f"{PERK_EMOJI[perk]} Rent", discord.ButtonStyle.primary,
                perk in gated,
            ))

    elif section == SECTION_SERVER:
        if settings.shop_voice_style_enabled or "voice_style" in owned:
            _sort, price = shop_row_price(settings, "voice_style", None, None)
            price_bit = f"{settings.currency_emoji} {price}/wk"
            desc = PERK_BLURBS["voice_style"]
            if "voice_style" in owned:
                rows.append(ShopRow(
                    ACTION_ROW_ID_BASE + len(rows), "voice_active", "voice_style",
                    PERK_LABELS["voice_style"], desc, price_bit, "🎙️ leased",
                    "🎙️ Leased", discord.ButtonStyle.success, True,
                ))
            else:
                rows.append(ShopRow(
                    ACTION_ROW_ID_BASE + len(rows), "voice", "voice_style",
                    PERK_LABELS["voice_style"], desc, price_bit, "",
                    "🎙️ Rent", discord.ButtonStyle.primary, False,
                ))
        if pin_enabled(settings):
            rows.append(ShopRow(
                ACTION_ROW_ID_BASE + len(rows), "pin", "", "Pinned Message",
                PIN_DESC,
                f"{settings.currency_emoji} {pin_price(settings):,} once",
                "", "📌 Pin", discord.ButtonStyle.primary, False,
            ))
        if theme_enabled(settings):
            rows.append(ShopRow(
                ACTION_ROW_ID_BASE + len(rows), "theme", "", "Themed Day",
                THEME_DESC,
                f"{settings.currency_emoji} {settings.price_flash_theme:,} "
                f"once · {settings.theme_hours}h",
                "", "🎭 Book", discord.ButtonStyle.primary, False,
            ))

    elif section == SECTION_GAMES:
        if settings.shop_streak_shield_enabled:
            held = shields_held > 0
            rows.append(ShopRow(
                ACTION_ROW_ID_BASE + len(rows), "shield", "", "Streak Shield",
                SHIELD_DESC,
                f"{settings.currency_emoji} {settings.price_streak_shield:,} once",
                "🛡️ held" if held else "",
                "🛡️ Held" if held else "🛡️ Shield",
                discord.ButtonStyle.success if held else discord.ButtonStyle.secondary,
                held,
            ))
        if raffle_svc.raffle_enabled(settings):
            rows.append(ShopRow(
                ACTION_ROW_ID_BASE + len(rows), "raffle", "", "Weekly Raffle",
                RAFFLE_DESC,
                f"{settings.currency_emoji} {settings.price_raffle_ticket:,} each",
                f"up to {settings.raffle_max_tickets}/wk",
                "🎟️ Tickets", discord.ButtonStyle.secondary, False,
            ))

    elif section == SECTION_YOURS:
        # What the member holds, gathered from every shelf with the rows'
        # own buttons (customise, the Active/Leased/Held chips) — rows a
        # member doesn't own are shelf rows, not theirs. Renumbered, since
        # each shelf numbers its own rows from the base.
        for shelf in (SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES):
            for row in shop_section_rows(
                settings, shelf, gated=gated, owned=owned,
                icon_catalog=icon_catalog, color_catalog=color_catalog,
                has_palette=has_palette, shields_held=shields_held,
                refundable=False,
            ):
                if row.perk:
                    mine = row.perk in owned
                else:
                    mine = row.kind == "shield" and shields_held > 0
                if mine:
                    rows.append(row._replace(row_id=ACTION_ROW_ID_BASE + len(rows)))

    # Cancel & Refund ends *any* rental, so it lives on the one card that
    # gathers every rental whichever shelf it came from — not on each shelf.
    if refundable and section == SECTION_YOURS:
        rows.append(ShopRow(
            ACTION_ROW_ID_BASE + len(rows), "refund", "", "Cancel & Refund",
            REFUND_DESC, "", "unused days credited back", "↩️ Refund",
            discord.ButtonStyle.secondary, False,
        ))
    return rows


def build_shop_section_layout(
    settings: EconSettings,
    section: str,
    rows: list[ShopRow],
    *,
    balance: int,
    page: int,
    nav: list[str],
) -> list[discord.ui.Item[Any]]:
    """One section of the shop. **Paradigm 6 — list with per-row actions.**

    Every row carries its own price and status (owned, gated, held) — the
    embed era's price table above the buttons is gone, so each row has to
    stand on its own. Never pages internally: the largest section (six
    self-perks plus the trailing refund entry) is seven rows, comfortably
    inside the 40-component budget on its own.

    Three lines a row, always (2026-09-24, at Billy's request — "make sure
    all the text elements are there for every item"): the title bold and
    alone, the description under it, then the price and/or status note as
    ``-#`` subtext — the shop's own way of saying "smaller and below" for the
    price a row carries. Line 3 is no longer conditional: ``shop_section_rows``
    guarantees every row carries a price, a note, or (the trailing refund
    row) both a description *and* a note standing in for the missing price,
    so ``price or note`` is never empty and the assert below is the contract,
    not a guard against something that can actually happen.
    """
    header = SECTION_CAPTIONS.get(section, "🛍️ Perk Shop")
    if section == SECTION_YOURS:
        tail = "Everything you rent right now."
    elif section == SECTION_GAMES:
        tail = "Bought once — nothing here renews."
    else:
        tail = "Rent by the week, cancel any time."
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {header}\n{tail} You have {settings.currency_emoji} "
            f"**{balance:,}**."
        ),
    ]
    if rows:
        for row in rows:
            lines = [f"**{row.title}**", row.desc]
            bits = [b for b in (row.price, row.note) if b]
            assert bits, f"every catalogue row is three lines: {row!r}"
            lines.append("-# " + " · ".join(bits))
            text = "\n".join(lines)
            items.append(
                discord.ui.Section(
                    _text(text),
                    accessory=discord.ui.Button(
                        label=row.button_label,
                        style=row.button_style,
                        disabled=row.disabled,
                        id=row.row_id,
                    ),
                )
            )
    elif section == SECTION_YOURS:
        items.append(_text("_You aren't renting anything right now._"))
    else:
        items.append(_text("_Nothing for sale here right now._"))
    nav_row = _nav_row(page, nav)
    if nav_row is not None:
        items.append(nav_row)
    line = _page_line(page, nav)
    if line is not None:
        items.append(line)
    return items


# ── 6 + 10. Refund picker and confirm ─────────────────────────────────


def build_refund_picker_layout(
    settings: EconSettings, rentals: list[dict[str, Any]], shield_price: int
) -> list[discord.ui.Item[Any]]:
    """The member's cancellable rentals + held shield. **Paradigm 6.**

    A row per cancellable thing rather than the old dropdown: three or more
    *settings* are a dial (a Select), but this is a *list of items to act
    on* — the same shape the store and the section pages already use, so a
    member doesn't learn a second control for the same kind of choice.

    Two lines a row (2026-09-24, at Billy's request): the bold name alone,
    then what comes back as ``-#`` subtext — replacing the one-line
    ``**name** — … — …`` join, which at ``MAX_ROW_CHARS`` = 30 can't hold the
    longest perk label plus a refund amount and "ends now" on one line the
    way it could at the old 45.
    """
    items: list[discord.ui.Item[Any]] = [
        _text(
            "## ↩️ Cancel & Refund\n"
            "Pick what to cancel — this credits you back right away and ends "
            "the perk immediately."
        ),
    ]
    now = time.time()
    index = 0
    for rental in rentals:
        label = PERK_LABELS.get(str(rental["perk"]), str(rental["perk"]))
        if rental["state"] == "active":
            preview = prorated_refund(
                int(rental["price"]), float(rental["next_bill_at"]), now
            )
            note = f"{settings.currency_emoji} {preview:,} back · ends now"
        else:
            note = "➖ no refund · ends now"
        items.append(
            discord.ui.Section(
                _text(f"**{label}**\n-# {note}"),
                accessory=discord.ui.Button(
                    label="Cancel", style=discord.ButtonStyle.danger,
                    id=REFUND_ROW_ID_BASE + index,
                ),
            )
        )
        index += 1
    if shield_price > 0:
        items.append(
            discord.ui.Section(
                _text(
                    f"**Streak Shield**\n-# {settings.currency_emoji} "
                    f"{shield_price:,} back"
                ),
                accessory=discord.ui.Button(
                    label="Cancel", style=discord.ButtonStyle.danger,
                    id=REFUND_ROW_ID_BASE + index,
                ),
            )
        )
        index += 1
    if index == 0:
        items.append(_text("_Nothing to cancel right now._"))
    return items


def build_refund_confirm_layout(
    settings: EconSettings, title: str, refund_text: str
) -> list[discord.ui.Item[Any]]:
    """The Confirm/Back gate before a refund runs. **Paradigm 10 — confirm.**

    ``refund_text`` is the pre-computed sentence naming what comes back (or
    that nothing does) — the same text ``show_refund_confirm`` always built,
    now the card's body instead of an edited plain-text message.
    """
    return [
        _text(f"## ⚠️ Cancel {title}?\n{refund_text}"),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Yes, Cancel & Refund", style=discord.ButtonStyle.danger,
                id=REFUND_CONFIRM_ID,
            ),
            discord.ui.Button(
                label="Back", style=discord.ButtonStyle.secondary,
                id=REFUND_BACK_ID,
            ),
        ),
    ]


def build_refund_done_layout(
    settings: EconSettings, title: str, amount: int
) -> list[discord.ui.Item[Any]]:
    """A refund, carried out. **Paradigm 7 — receipt.**"""
    if amount > 0:
        body = f"{settings.currency_emoji} **{amount:,}** credited back."
    else:
        body = "➖ No refund — it was already in the grace period."
    return [_text(f"## ✅ {title} Cancelled\n{body}")]


def build_refund_missing_layout(title: str) -> list[discord.ui.Item[Any]]:
    """Confirmed, but the thing was already gone. **Paradigm 7 — receipt.**"""
    return [_text(f"## ❌ Nothing to Cancel\nYour {title.lower()} isn't there "
                   "anymore — it may already have ended.")]


# ── 7. Post-rent receipt ──────────────────────────────────────────────


def build_post_rent_layout(
    perk: str, *, comped: bool, customise: bool
) -> list[discord.ui.Item[Any]]:
    """A rental just landed. **Paradigm 7 — receipt**, with an optional
    Customise accessory for a perk that has settings to make.

    ``comped`` swaps "Rented" for "Unlocked" — nothing was bought on the
    comp path, so the receipt shouldn't claim it was.
    """
    got = "Unlocked" if comped else "Rented"
    label = PERK_LABELS[perk]
    if perk == "voice_style":
        body = (
            f"## ✅ {got} {label}\n"
            "Renaming and sizing your voice channel are unlocked — your "
            "saved name and limit apply the next time you spin one up."
        )
        return [_text(body)]
    if perk in NO_CONFIG_PERKS:
        body = (
            f"## ✅ {got} {label}\n"
            "Your personal role now wears Discord's holographic shimmer — "
            "no setup needed."
        )
        return [_text(body)]
    note = (
        " Upload an image with `/bank role icon`." if perk == "role_icon" else ""
    )
    heading = f"## ✅ {got} {label}\nSet it up right here.{note}"
    return build_role_notice_layout(heading, perk=perk, customise=customise)


def build_role_notice_layout(
    body: str, *, perk: str, customise: bool
) -> list[discord.ui.Item[Any]]:
    """A role-perk notice with an optional Customise accessory.

    Shares the Customise button's id and label with the post-rent receipt
    above but not its wording — the icon picker's own switch confirmations
    ("Switched to a custom icon…") aren't a rental receipt, so they carry
    their own body over the same accessory.
    """
    text = _text(body)
    if not customise:
        return [text]
    return [
        discord.ui.Section(
            text,
            accessory=discord.ui.Button(
                label=CUSTOMISE_LABELS.get(perk, "Customize"),
                style=discord.ButtonStyle.success,
                id=POST_RENT_CUSTOMISE_ID,
            ),
        )
    ]
