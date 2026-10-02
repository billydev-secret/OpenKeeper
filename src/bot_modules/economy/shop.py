"""Perk shop — availability, pricing and pagination.

The vocabulary it renders lives in ``perks.py``; the cards themselves are
built in ``shop_layouts.py`` (Components V2 — ``docs/plans/
components_v2_migration.md`` wave 3a deleted this module's embed builders).
This module keeps what both the layouts and the cog need to agree on: which
perks a guild is actually selling, which sections and pages the shop has, and
what a row's price should read.

Two surfaces render from here and must not diverge: the ephemeral per-member
shop (one section at a time, opened from a panel Browse or the ``/bank shop``
chooser), which knows the viewer's balance, entitlements and shields, and the
member-agnostic channel panel, which passes none of them.

Pricing is not purely presentational — ``shop_row_price`` folds the flat
``price_role_icon`` into a curated catalog's span and decides the sort floor
— and that policy lives here with the table it drives.
"""

from __future__ import annotations

from bot_modules.economy.perks import SELF_PERKS, perk_on_sale, perk_price
from bot_modules.economy.shop_items import ItemView
from bot_modules.economy import raffle_service as raffle_svc
from bot_modules.economy.pin_service import pin_enabled
from bot_modules.economy.theme_service import theme_enabled
from bot_modules.economy.service import EconSettings

# Items per page of the dedicated storefront. Discord's select would hold 25,
# but 25 monospace rows is a wall on a phone and a 25-deep scroll in the
# picker — ten reads as a shelf, and a pair of arrows is cheaper than a
# scroll. It is also why the store no longer has a ceiling: the old picker
# showed the first 25 items and said so, which is a curation problem the
# guild never asked for.
STORE_PAGE_SIZE = 10

def shop_row_price(
    settings: EconSettings,
    perk: str,
    icon_catalog: tuple[int, int, int] | None,
    color_catalog: tuple[int, int, int] | None = None,
) -> tuple[int, str]:
    """(sort key, display string) for a shop row's price.

    A curated icon catalog prices per icon, so the role-icon row shows a span
    and sorts on its floor. The flat ``price_role_icon`` folds into that span —
    the picker's bring-your-own Custom entry sells at it, so it's a price the
    row genuinely offers.

    The palette spans the same way, but the flat ``price_role_preset`` is NOT
    folded in: there is no bring-your-own palette colour, so the only prices on
    offer are the palette's own (the catalog service has already substituted the
    flat price for colours left at 0).
    """
    if perk == "role_icon" and icon_catalog is not None:
        lo, hi, _count = icon_catalog
        lo = min(lo, settings.price_role_icon)
        hi = max(hi, settings.price_role_icon)
        return lo, f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    if perk == "role_preset" and color_catalog is not None:
        lo, hi, _count = color_catalog
        return lo, f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    price = perk_price(settings, perk)
    return price, f"{price:,}"


#: The shop's sections, in the order a member walks them. What a guild stocks
#: itself leads — it is the part that changes and the part they came for; the
#: rest is the same ladder in every guild.
SECTION_SPECIALS = "specials"
SECTION_COSMETICS = "cosmetics"
SECTION_SERVER = "server"
SECTION_GAMES = "games"
#: Not a shelf: the member's own rentals gathered from every section, with
#: their customise buttons and the Cancel & Refund row. The panel's last row
#: opens it, so a perk stays reachable after the section it was rented from
#: stops selling (Billy, 2026-09-26). ``shop_sections`` never emits it.
SECTION_YOURS = "yours"

#: What each section calls itself — the card's ``##`` heading, so the panel's
#: "Inside" list and the section's own card can never disagree about where
#: you are. "Server Features" is deliberately wider than the one product in
#: it today (the voice-room lease): personal channels land here when they
#: ship, and a section renamed under members is worse than one that reads
#: slightly roomy.
SECTION_CAPTIONS = {
    SECTION_SPECIALS: "🎁 Specials",
    SECTION_COSMETICS: "🎨 Role Cosmetics",
    SECTION_SERVER: "🏠 Server Features",
    SECTION_GAMES: "🎲 Game Features",
    SECTION_YOURS: "🧾 Your Rentals",
}

#: The panel's second line under each section's name, held to the phone's
#: ~30 columns beside a button (``shop_layouts.MAX_ROW_CHARS``).
SECTION_BLURBS = {
    SECTION_SPECIALS: "Custom items this server sells",
    SECTION_COSMETICS: "Perks for your personal role",
    SECTION_SERVER: "Voice, pins and themes",
    SECTION_GAMES: "Streak shields and raffles",
    SECTION_YOURS: "Change or cancel a rental",
}

#: Where a public paid-perk card sends a reader who wants one of their own.
#: Both halves have to stay true: ``/bank theme``, ``/bank pin`` and
#: ``/bank sponsor`` were **deleted** when the shop was reorganised into
#: sections (2026-08-29), so a card naming one would advertise a command
#: Discord answers with "unknown integration" — and the section caption is
#: read from ``SECTION_CAPTIONS`` rather than retyped, so a rename moves the
#: signpost with the aisle. Plain text, not a slash-command mention: a
#: clickable chip needs the live command id, which a layout builder has no
#: way to hold.
SHOP_POINTER = f"`/bank shop` → {SECTION_CAPTIONS[SECTION_SERVER]}"


def shop_pointer_line(offer: str) -> str:
    """The "you can buy this too" line for a public paid-perk card's ``-#``
    subtext.

    The card a member is looking at *is* the advertisement — someone paid for
    the themed day or the pin they are reading, and this is the one moment
    the reader is demonstrably interested. ``offer`` is the verb phrase for
    this product ("Buy a themed day of your own"); the route is shared. Every
    public paid-perk card is a Components V2 ``LayoutView`` now (theme's own
    2026-09-28 conversion was the last embed holdout), so this is the only
    pointer builder left — there is no field to name on a layout receipt, so
    the label an embed field used to carry drops rather than getting bolted
    onto plain text that has nowhere to put it.
    """
    return f"{offer} — {SHOP_POINTER}"


def stocked_perks(
    settings: EconSettings,
    perks: tuple[str, ...],
    *,
    owned: set[str] | frozenset[str] = frozenset(),
    has_palette: bool = True,
) -> tuple[str, ...]:
    """The perks of ``perks`` this guild is actually selling.

    Lifted out of the shop card builder so section visibility and the row
    list can ask the same question — a section whose rows would all drop must
    not get a page of its own, and that decision now happens before either.

    A switched-off perk leaves the list entirely, including any width
    calculation a caller does over it, so a hidden row can never pad the
    visible ones. The carve-out is a member still renting it out to their
    anniversary: billing them for a perk with no row and no ✅ anywhere in the
    shop is how you get a support ticket instead of a clean wind-down.
    ``has_palette`` is the same rule for the colour palette, whose product
    disappears when the guild's swatches do.
    """
    return tuple(
        p
        for p in perks
        if (p != "role_preset" or has_palette)
        and (perk_on_sale(settings, p) or p in owned)
    )


def shop_sections(
    settings: EconSettings,
    *,
    items: list[ItemView] | None = None,
    owned: set[str] | frozenset[str] = frozenset(),
    has_palette: bool = True,
) -> list[str]:
    """The sections this guild has anything to put in.

    An empty section gets no page at all rather than a page saying it is empty
    — a guild that never turned the voice lease on should not have members
    stepping through a blank shelf to reach the next one. That means the shop's
    shape differs per guild, which is the point: TGM sells no custom items, so
    it has no Specials section and never sees one.
    """
    out: list[str] = []
    if items:
        out.append(SECTION_SPECIALS)
    if stocked_perks(settings, SELF_PERKS, owned=owned, has_palette=has_palette):
        out.append(SECTION_COSMETICS)
    if (
        stocked_perks(settings, ("voice_style",), owned=owned)
        or theme_enabled(settings)
        or pin_enabled(settings)
    ):
        out.append(SECTION_SERVER)
    if settings.shop_streak_shield_enabled or raffle_svc.raffle_enabled(settings):
        out.append(SECTION_GAMES)
    return out


def shop_pages(
    settings: EconSettings,
    *,
    items: list[ItemView] | None = None,
    owned: set[str] | frozenset[str] = frozenset(),
    has_palette: bool = True,
) -> list[tuple[str, int]]:
    """The shop's pages in order, as ``(section, index within that section)``.

    Only Specials runs to more than one page: a guild can define unlimited
    items, and ten to a page is what survives a phone. Every other section is
    a fixed handful of rows.

    Never empty. A guild with every switch off and nothing stocked still gets
    one page — the cosmetics section, rendering its empty row list and the
    "shop is switched off" story — because a shop with no pages has nothing
    to show a member who opened it, and the arrows have nothing to step
    between.
    """
    pages: list[tuple[str, int]] = []
    for section in shop_sections(
        settings, items=items, owned=owned, has_palette=has_palette
    ):
        if section == SECTION_SPECIALS:
            pages += [
                (SECTION_SPECIALS, i)
                for i in range(store_page_count(items or []))
            ]
        else:
            pages.append((section, 0))
    return pages or [(SECTION_COSMETICS, 0)]


def section_book(
    pages: list[tuple[str, int]], section: str
) -> list[tuple[str, int]]:
    """The pages one panel button opens: ``section``'s own, and no other.

    The shop is split by section (Billy, 2026-09-26: "split up the ephemeral
    message and use different buttons on the panel for different sections"),
    so a Browse button's card never arrows across into a neighbour; only
    Specials, at more than ten items, has pages to step between.
    :data:`SECTION_YOURS` is always one page. Empty when the section has
    closed since the panel was posted — the caller refuses by name rather
    than serving a different shelf.
    """
    if section == SECTION_YOURS:
        return [(SECTION_YOURS, 0)]
    return [p for p in pages if p[0] == section]


def page_captions(pages: list[tuple[str, int]], total_items: int = 0) -> list[str]:
    """What each page calls itself, for the ``-#`` line and any card that
    needs to name a page other than the one it's on.

    Specials carries its span when it runs to more than one page — "Specials"
    twice over tells a member nothing about which half they are looking at —
    and drops it when it doesn't, because "🎁 Specials · 1–7" is worse than
    "🎁 Specials" with nothing to tell it apart from.
    """
    spread = sum(1 for section, _ in pages if section == SECTION_SPECIALS) > 1
    out: list[str] = []
    for section, index in pages:
        caption = SECTION_CAPTIONS[section]
        if section == SECTION_SPECIALS and spread:
            start = index * STORE_PAGE_SIZE + 1
            end = min(total_items, (index + 1) * STORE_PAGE_SIZE)
            caption += f" · {start}–{end}"
        out.append(caption)
    return out


def store_page_count(items: list[ItemView]) -> int:
    """Pages the storefront needs. Always at least one, so page 1/1 is valid."""
    return max(1, -(-len(items) // STORE_PAGE_SIZE))


def store_page(items: list[ItemView], page: int) -> list[ItemView]:
    """The slice shown on ``page`` (0-based), clamped into range.

    Clamped rather than trusted: an admin can delete items while a member has
    the storefront open, and page 3 of a store that just shrank to one page
    must show that page rather than an empty picker.
    """
    page = max(0, min(page, store_page_count(items) - 1))
    return items[page * STORE_PAGE_SIZE : (page + 1) * STORE_PAGE_SIZE]
