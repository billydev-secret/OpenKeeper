"""``bot_modules/economy/shop_layouts.py`` — the Components V2 shop cards.

Pure builders, so they're testable the way the embed builders they replaced
were: call one, walk the items, assert on the text and the button state. Two
things get their own coverage beyond the copy: the **budget** (the cosmetics
section grows a row per self-perk, the store a row per item on the page —
either can overflow 40 components on the wrong data), and ``shop_section_rows``'s
branch matrix, since a row's kind/label/style/disabled is the whole of what
used to be split across ``build_shop_embed``'s price table and the cog's
``_ShopView.__init__`` button logic — get one branch wrong and a row either
can't be acted on or lies about whether it can.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import discord
import pytest

from bot_modules.core.layout import (
    MAX_ACCESSORY_LABEL,
    MAX_COMPONENTS,
    check_budget,
    layout_budget,
    text_columns,
)
from bot_modules.economy.perks import (
    CUSTOMISE_LABELS,
    NO_CONFIG_PERKS,
    SELF_PERKS,
)
from bot_modules.economy.shop import (
    SECTION_BLURBS,
    SECTION_COSMETICS,
    SECTION_GAMES,
    SECTION_SERVER,
    SECTION_SPECIALS,
    SECTION_YOURS,
    shop_sections,
)
from bot_modules.economy.shop_items import BILLING_ONCE, BILLING_WEEKLY, KIND_MANUAL, KIND_ROLE, ItemView
from bot_modules.economy.shop_layouts import (
    SHOP_PANEL_TITLE,
    MAX_ROW_CHARS,
    PAGE_CHIP_ID,
    POST_RENT_CUSTOMISE_ID,
    REFUND_BACK_ID,
    REFUND_CONFIRM_ID,
    REFUND_ROW_ID_BASE,
    SHOP_HELP_CUSTOM_ID,
    SHOP_PANEL_OPEN_CUSTOM_ID,
    SHOP_SECTION_CUSTOM_IDS,
    STORE_FALLBACK_BLURBS,
    ShopRow,
    build_post_rent_layout,
    build_refund_confirm_layout,
    build_refund_done_layout,
    build_refund_missing_layout,
    build_refund_picker_layout,
    build_role_notice_layout,
    STORE_NAME_CLIP,
    build_shop_chooser_layout,
    build_shop_help_layout,
    build_shop_panel_layout,
    build_shop_section_layout,
    build_store_layout,
    shop_section_rows,
)
from bot_modules.economy.service import EconSettings

SETTINGS = EconSettings()


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    """Every TextDisplay in the card, however deeply nested, joined."""
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _sections(items) -> list[discord.ui.Section]:
    return [
        item for item in _view(items).walk_children()
        if isinstance(item, discord.ui.Section)
    ]


def _accessories(items) -> list[discord.ui.Button[Any]]:
    return [
        s.accessory for s in _sections(items)
        if isinstance(s.accessory, discord.ui.Button)
    ]


def _row_buttons(row: discord.ui.ActionRow[Any]) -> list[discord.ui.Button[Any]]:
    return [c for c in row.children if isinstance(c, discord.ui.Button)]


def _visible_length(row_text: str) -> int:
    """What a Section row costs on screen: markdown syntax is not drawn, and
    an emoji costs 2 columns, not 1 (``core.layout.text_columns`` — Billy's
    phone screenshot, 2026-09-24)."""
    return text_columns(row_text.replace("**", ""))


def _line_visible_length(line: str) -> int:
    """``_visible_length``, plus stripping a ``-# `` subtext prefix — the
    per-line measure for a catalogue row's three lines (2026-09-24: title /
    description / ``-#`` price+status, always all three; a picker row is
    two), where the prefix isn't drawn either.
    """
    return _visible_length(line.removeprefix("-# "))


def _rows(section=SECTION_COSMETICS, settings=SETTINGS, **kw: Any) -> list[ShopRow]:
    defaults: dict[str, Any] = dict(
        gated=set(), owned=set(), icon_catalog=None, color_catalog=None,
        has_palette=True, shields_held=0, refundable=False,
    )
    defaults.update(kw)
    return shop_section_rows(settings, section, **defaults)


def _stock(*sections: str) -> list[str]:
    """The panel's stocked ``sections``."""
    return list(sections)


def _item(item_id: int = 1, **kw: Any) -> ItemView:
    defaults: dict[str, Any] = dict(name="Widget", price=250, blurb="a thing")
    defaults.update(kw)
    return ItemView(item_id=item_id, **defaults)


def _rental_row(perk="role_color", state="active", price=100, next_bill_at=2_000_000_000.0):
    return {"perk": perk, "state": state, "price": price, "next_bill_at": next_bill_at}


BANNER = "https://dash.example/static/panel-banners/" + "c" * 32 + ".gif"
FOOTER = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"

#: Every card this feature can produce, one lambda per state — the sweep
#: every other converted economy feature in this migration wave carries
#: (``test_pin_layouts.py`` et al.), so the shop drifts back to
#: a Container, an unresolved mention or a headingless card as loudly as
#: they would.
ALL_CARDS = [
    pytest.param(
        lambda: build_shop_panel_layout(_stock(SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES)),
        id="panel",
    ),
    pytest.param(
        lambda: build_shop_panel_layout(
            _stock(SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES), banner_url=BANNER
        ),
        id="panel-banner",
    ),
    pytest.param(
        lambda: build_shop_panel_layout(
            _stock(SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES),
            banner_url=BANNER,
            footer_url=FOOTER,
        ),
        id="panel-art",
    ),
    pytest.param(
        lambda: build_shop_chooser_layout([SECTION_SPECIALS, SECTION_COSMETICS]),
        id="chooser",
    ),
    pytest.param(lambda: build_shop_chooser_layout([]), id="chooser-empty"),
    pytest.param(
        lambda: build_shop_section_layout(
            SETTINGS, SECTION_YOURS,
            _rows(section=SECTION_YOURS, owned={"role_color"}, refundable=True),
            balance=500, page=0, nav=[],
        ),
        id="section-yours",
    ),
    pytest.param(
        lambda: build_shop_section_layout(
            SETTINGS, SECTION_COSMETICS,
            _rows(
                icon_catalog=(75, 400, 12), color_catalog=(50, 200, 9),
                gated={"role_gradient"}, owned={"role_holographic"}, refundable=True,
            ),
            balance=500, page=0, nav=["a", "b"],
        ),
        id="section-cosmetics",
    ),
    pytest.param(
        lambda: build_shop_section_layout(
            SETTINGS, SECTION_SERVER,
            _rows(
                section=SECTION_SERVER,
                settings=replace(SETTINGS, shop_voice_style_enabled=True),
            ),
            balance=500, page=0, nav=[],
        ),
        id="section-server",
    ),
    pytest.param(
        lambda: build_shop_section_layout(SETTINGS, SECTION_GAMES, [], balance=0, page=0, nav=[]),
        id="section-empty",
    ),
    pytest.param(
        lambda: build_store_layout(
            SETTINGS, [_item()], owned_item_ids=set(), balance=0, page=0, nav=["a", "b"],
        ),
        id="store-with-items",
    ),
    pytest.param(
        lambda: build_store_layout(SETTINGS, [], owned_item_ids=set(), balance=0, page=0, nav=[]),
        id="store-empty",
    ),
    pytest.param(
        lambda: build_refund_picker_layout(SETTINGS, [_rental_row()], shield_price=30),
        id="refund-picker-with-rentals",
    ),
    pytest.param(
        lambda: build_refund_picker_layout(SETTINGS, [], shield_price=0),
        id="refund-picker-empty",
    ),
    pytest.param(
        lambda: build_refund_confirm_layout(SETTINGS, "Custom Role Color", "🪙 40 back."),
        id="refund-confirm",
    ),
    pytest.param(
        lambda: build_refund_done_layout(SETTINGS, "Custom Role Color", 40),
        id="refund-done-credited",
    ),
    pytest.param(
        lambda: build_refund_done_layout(SETTINGS, "Streak Shield", 0),
        id="refund-done-no-refund",
    ),
    pytest.param(
        lambda: build_refund_missing_layout("Custom Role Color"),
        id="refund-missing",
    ),
    pytest.param(
        lambda: build_post_rent_layout("voice_style", comped=False, customise=True),
        id="post-rent-voice-style",
    ),
    pytest.param(
        lambda: build_post_rent_layout(next(iter(NO_CONFIG_PERKS)), comped=False, customise=True),
        id="post-rent-no-config",
    ),
    pytest.param(
        lambda: build_post_rent_layout("role_color", comped=False, customise=True),
        id="post-rent-configurable",
    ),
    pytest.param(
        lambda: build_post_rent_layout("role_color", comped=True, customise=False),
        id="post-rent-comped",
    ),
]


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    """Each builder's items are accepted by a LayoutView and carry text.

    discord.py validates placement on ``add_item`` — a Thumbnail loose at
    top level, or a Button outside a row, raises here rather than as a 400.
    """
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


def _lead_heading(items) -> str:
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


def _heading_faults(heading: str) -> list[str]:
    if not heading.startswith("## "):
        return [f"a card-level heading is a `## Title` line: {heading!r}"]
    if len(heading) < 4 or heading[3].isascii():
        return [f"lead the title with the feature's glyph: {heading!r}"]
    return []


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    """Every card is bare, the channel panel included: the hubs' box went on
    2026-09-29 (style guide § No container)."""
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_mention(build):
    """None of the shop's cards name another member, so none of them should
    ever grow a raw ``<@id>`` — the same drift guard every other converted
    card in this migration wave carries."""
    assert "<@" not in _all_text(build())


# ── 1. Hub panel ─────────────────────────────────────────────────────────


def _panel(stock, **kw) -> list:
    """The panel's parts, which sit directly on the message."""
    return build_shop_panel_layout(stock, **kw)


def _section_buttons(items) -> list[discord.ui.Button]:
    """The entry-point buttons, in order — every accessory but Help."""
    return [
        i.accessory for i in _view(items).walk_children()
        if isinstance(i, discord.ui.Section) and isinstance(i.accessory, discord.ui.Button)
        and i.accessory.custom_id != SHOP_HELP_CUSTOM_ID
    ]


def _help_section(items) -> discord.ui.Section:
    (section,) = [
        i for i in _view(items).walk_children()
        if isinstance(i, discord.ui.Section)
        and getattr(i.accessory, "custom_id", None) == SHOP_HELP_CUSTOM_ID
    ]
    return section


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: build_shop_panel_layout(_stock(SECTION_COSMETICS)), id="panel"),
        pytest.param(lambda: build_shop_chooser_layout([SECTION_COSMETICS]), id="chooser"),
    ],
)
def test_help_sits_beside_the_title(build):
    """Style guide § Controls: a hub's long explanation is a grey Help
    beside its title, never prose on the card."""
    section = _help_section(build())
    assert section.children[0].content == "## 🛍️ Perk Shop"
    assert section.accessory.label == "Help"
    assert section.accessory.style == discord.ButtonStyle.secondary


def test_help_moves_beside_the_intro_when_the_title_is_in_the_banner():
    section = _help_section(
        build_shop_panel_layout(
            _stock(SECTION_COSMETICS), banner_url=BANNER, title_in_banner=True
        )
    )
    assert section.children[0].content == "Rent a perk or buy what the server sells."


def test_help_id_is_frozen():
    """Posted panels carry it on Discord's side."""
    assert SHOP_HELP_CUSTOM_ID == "econ_shop_help"


def test_help_card_reads_the_doc_and_lifts_its_heading():
    items = build_shop_help_layout("# Shop FAQ\n\nRent by the week.")
    text = _all_text(items)
    assert text.startswith("## 🛍️ Shop FAQ")
    assert "Rent by the week." in text


def test_help_card_without_a_doc_says_so():
    text = _all_text(build_shop_help_layout(None))
    assert text.startswith("## 🛍️ Perk Shop Help")
    assert "no shop FAQ on this server yet" in text


def test_shop_panel_describes_each_section_with_its_browse():
    """A row per stocked section, its heading over the same blurb the
    private chooser shows, with its own Browse beside it ("inline buttons
    with each section", Billy, 2026-09-26). A line sits between rows, then
    Your Rentals with Manage. Bug-list row 260 (Billy, 2026-09-27): the rows
    describe the section rather than list its stock."""
    stock = [SECTION_COSMETICS, SECTION_SERVER]
    text = _all_text(build_shop_panel_layout(stock))
    assert "### 🎨 Role Cosmetics\n-# Perks for your personal role" in text
    assert "### 🏠 Server Features\n-# Voice, pins and themes" in text
    assert "🎲 Game Features" not in text
    assert text.index("Role Cosmetics") < text.index("Server Features")
    assert "### 🧾 Your Rentals\n-# Change or cancel a rental" in text
    parts = _panel(stock)
    assert isinstance(parts[-1], discord.ui.Section)
    assert isinstance(parts[-2], discord.ui.Separator)
    assert not any(isinstance(p, discord.ui.ActionRow) for p in parts)
    buttons = _section_buttons(build_shop_panel_layout(stock))
    assert [b.custom_id for b in buttons] == [
        SHOP_SECTION_CUSTOM_IDS[SECTION_COSMETICS],
        SHOP_SECTION_CUSTOM_IDS[SECTION_SERVER],
        SHOP_SECTION_CUSTOM_IDS[SECTION_YOURS],
    ]
    assert [b.label for b in buttons] == ["Browse", "Browse", "Manage"]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}
    # Each Browse sits beside its own shelf's blurb, not somewhere below it.
    rows = [p for p in parts if isinstance(p, discord.ui.Section)][1:]  # past the title
    assert [r.accessory.custom_id for r in rows] == [b.custom_id for b in buttons]
    assert "Perks for your personal role" in rows[0].children[0].content
    assert "Voice, pins and themes" in rows[1].children[0].content


@pytest.mark.parametrize(
    "section", [SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES]
)
def test_panel_and_chooser_say_the_same_thing_about_a_section(section):
    """Row 260: the panel's line under a section is the chooser's blurb, so
    the two can't drift apart. No row's body is a list of stock."""
    panel = _all_text(build_shop_panel_layout([section]))
    chooser = _all_text(build_shop_chooser_layout([section]))
    blurb = SECTION_BLURBS[section]
    assert f"-# {blurb}" in panel and f"-# {blurb}" in chooser
    assert " · " not in panel


def test_section_ids_are_frozen():
    """Posted panels carry these ids on Discord's side — renaming one
    strands every panel posted before the rename."""
    assert SHOP_SECTION_CUSTOM_IDS == {
        "specials": "econ_shop_open:specials",
        "cosmetics": "econ_shop_open:cosmetics",
        "server": "econ_shop_open:server",
        "games": "econ_shop_open:games",
        "yours": "econ_shop_open:yours",
    }
    assert SHOP_PANEL_OPEN_CUSTOM_ID == "econ_shop_open"


@pytest.mark.parametrize(
    "section", [SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES, SECTION_YOURS]
)
def test_section_rows_fit_a_phone_beside_their_button(section):
    """Each chooser row's two lines stay inside the phone's column budget."""
    shelves = [] if section == SECTION_YOURS else [section]
    (row,) = [
        i for i in _view(build_shop_chooser_layout(shelves)).walk_children()
        if isinstance(i, discord.ui.Section)
        and i.accessory.custom_id == SHOP_SECTION_CUSTOM_IDS[section]
    ]
    for line in row.children[0].content.splitlines():
        assert _line_visible_length(line.replace("**", "")) <= MAX_ROW_CHARS


def test_shop_panel_with_nothing_stocked_says_so_and_keeps_manage():
    items = build_shop_panel_layout([])
    assert "Nothing is for sale right now." in _all_text(items)
    assert [b.label for b in _section_buttons(items)] == ["Manage"]


def test_chooser_is_a_browse_per_shelf_then_manage_bare():
    """Browse on the panel (and ``/bank shop``) opens this: the rows the
    panel carried before it listed the stock — privately, so no box and no
    art (ephemeral cards never carry the hub art)."""
    sections = [SECTION_SPECIALS, SECTION_GAMES]
    chooser = build_shop_chooser_layout(sections)
    assert not any(isinstance(i, discord.ui.Container) for i in chooser)
    assert not any(
        isinstance(i, discord.ui.MediaGallery) for i in _view(chooser).walk_children()
    )
    buttons = _section_buttons(chooser)
    assert [b.custom_id for b in buttons] == [
        SHOP_SECTION_CUSTOM_IDS[SECTION_SPECIALS],
        SHOP_SECTION_CUSTOM_IDS[SECTION_GAMES],
        SHOP_SECTION_CUSTOM_IDS[SECTION_YOURS],
    ]
    assert [b.label for b in buttons] == ["Browse", "Browse", "Manage"]


def test_chooser_with_no_sections_says_so_and_keeps_manage():
    items = build_shop_chooser_layout([])
    assert "Nothing is for sale right now." in _all_text(items)
    assert [b.label for b in _section_buttons(items)] == ["Manage"]


def test_shop_panel_banner_heads_the_card_only_when_set():
    """The guild's hub banner (Billy, 2026-09-26: "just the main panels that
    live in the channel") opens the panel when set, and is absent otherwise —
    the shop pages the panel opens are ephemeral and never take one."""
    plain = _panel(_stock(SECTION_COSMETICS))
    bannered = _panel(_stock(SECTION_COSMETICS), banner_url=BANNER)
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in plain)
    assert isinstance(bannered[0], discord.ui.MediaGallery)
    assert [i.media.url for i in bannered[0].items] == [BANNER]
    assert len(bannered) == len(plain) + 1


def test_title_in_the_banner_replaces_the_heading():
    """With the title drawn into the banner (Billy, 2026-09-26) the ``##``
    line goes and the banner's alt text carries the name instead; the intro
    follows the banner directly."""
    titled = _panel(_stock(SECTION_COSMETICS), banner_url=BANNER, title_in_banner=True)
    assert [i.description for i in titled[0].items] == [SHOP_PANEL_TITLE]
    assert not any(
        line.startswith("## ")
        for line in _all_text(titled).splitlines()
    )
    plain = _panel(_stock(SECTION_COSMETICS), banner_url=BANNER)
    assert plain[2].content == titled[1].children[0].content  # same intro


def test_title_in_the_banner_needs_a_banner():
    assert "## 🛍️ Perk Shop" in _all_text(
        build_shop_panel_layout(_stock(SECTION_COSMETICS), title_in_banner=True)
    )


def test_shop_panel_footer_closes_the_card_only_when_set():
    """The footer strip is the box's last item, under Your Rentals."""
    plain = _panel(_stock(SECTION_COSMETICS))
    footed = _panel(_stock(SECTION_COSMETICS), footer_url=FOOTER)
    assert isinstance(footed[-1], discord.ui.MediaGallery)
    assert [i.media.url for i in footed[-1].items] == [FOOTER]
    assert len(footed) == len(plain) + 1


#: Every shelf switched on — the fullest listing a guild can have.
ALL_ON = replace(
    SETTINGS, shop_voice_style_enabled=True,
    price_pin_of_day=1500, pin_channel_id=1, flash_theme_enabled=True,
    theme_channel_id=1, price_flash_theme=1500, shop_streak_shield_enabled=True,
    raffle_enabled=True, price_raffle_ticket=250,
)


def test_shop_panel_budget():
    stock = shop_sections(ALL_ON, items=[_item(1)], has_palette=True)
    assert stock == [SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES]
    check_budget(_view(build_shop_panel_layout(
        stock, banner_url=BANNER, footer_url=FOOTER,
    )))


# ── 6. shop_section_rows: cosmetics ──────────────────────────────────────


def test_cosmetics_drops_a_switched_off_perk_nobody_owns():
    settings = replace(SETTINGS, shop_role_gradient_enabled=False)
    kinds = {r.perk: r.kind for r in _rows(settings=settings)}
    assert "role_gradient" not in kinds


def test_cosmetics_keeps_a_switched_off_perk_a_member_still_rents():
    settings = replace(SETTINGS, shop_role_gradient_enabled=False)
    rows = _rows(settings=settings, owned={"role_gradient"})
    row = next(r for r in rows if r.perk == "role_gradient")
    assert row.kind == "cfg"


def test_cosmetics_hides_the_palette_row_without_one():
    rows = _rows(has_palette=False)
    assert not any(r.perk == "role_preset" for r in rows)


def test_cosmetics_palette_row_browse_vs_change():
    browse = next(r for r in _rows(has_palette=True) if r.perk == "role_preset")
    assert browse.kind == "colors"
    assert browse.button_label == "🖌️ Browse"
    assert browse.button_style == discord.ButtonStyle.secondary
    change = next(
        r for r in _rows(has_palette=True, owned={"role_preset"})
        if r.perk == "role_preset"
    )
    assert change.button_label == "🖌️ Change"
    assert change.button_style == discord.ButtonStyle.success


def test_cosmetics_palette_row_notes_the_catalog_size():
    row = next(
        r for r in _rows(color_catalog=(50, 200, 9)) if r.perk == "role_preset"
    )
    assert row.note == "9 colors"


def test_cosmetics_palette_row_is_gated_like_any_other_row():
    row = next(
        r for r in _rows(gated={"role_preset"}) if r.perk == "role_preset"
    )
    assert row.disabled is True


def test_cosmetics_palette_row_gate_note_wins_over_the_catalog_size():
    # A guild can have both a curated catalog configured *and* the boost
    # feature the perk needs turned off — the disabled row must say why
    # it's disabled, not report the catalog size as if nothing were wrong.
    row = next(
        r for r in _rows(gated={"role_preset"}, color_catalog=(50, 200, 9))
        if r.perk == "role_preset"
    )
    assert row.note == "❌ not available"


def test_cosmetics_icon_row_is_a_plain_rent_row_without_a_catalog():
    # No catalog to browse yet — the row still lets a member rent the perk
    # at the flat price, it just can't offer "🖼️ Browse" over nothing.
    row = next(r for r in _rows(icon_catalog=None) if r.perk == "role_icon")
    assert row.kind == "rent"
    rows = _rows(icon_catalog=(75, 400, 12))
    row = next(r for r in rows if r.perk == "role_icon")
    assert row.kind == "icons"
    assert row.note == "12 + custom"
    assert row.button_label == "🖼️ Browse"


def test_cosmetics_icon_row_gate_note_wins_over_the_catalog_size():
    row = next(
        r for r in _rows(gated={"role_icon"}, icon_catalog=(75, 400, 12))
        if r.perk == "role_icon"
    )
    assert row.note == "❌ not available"
    assert row.disabled is True


def test_cosmetics_icon_row_owned_reads_change():
    row = next(
        r for r in _rows(icon_catalog=(75, 400, 12), owned={"role_icon"})
        if r.perk == "role_icon"
    )
    assert row.button_label == "🖼️ Change"
    assert row.button_style == discord.ButtonStyle.success


def test_cosmetics_no_config_perk_owned_shows_active_and_is_never_clickable():
    row = next(
        r for r in _rows(owned={"role_holographic"}) if r.perk == "role_holographic"
    )
    assert row.kind == "active"
    assert row.disabled is True
    assert "Active" in row.button_label
    # Gating a perk that's already active must not un-disable it — there is
    # nothing left to configure, gated or not.
    gated_too = next(
        r for r in _rows(owned={"role_holographic"}, gated={"role_holographic"})
        if r.perk == "role_holographic"
    )
    assert gated_too.disabled is True


def test_cosmetics_owned_configurable_perk_shows_cfg():
    row = next(
        r for r in _rows(owned={"role_color"}) if r.perk == "role_color"
    )
    assert row.kind == "cfg"
    assert row.note == "✅ owned"
    assert row.disabled is False
    assert row.button_label == CUSTOMISE_LABELS["role_color"]


def test_cosmetics_owned_gated_perk_shows_the_gate_note_and_disables():
    row = next(
        r for r in _rows(owned={"role_color"}, gated={"role_color"})
        if r.perk == "role_color"
    )
    assert row.kind == "cfg"
    assert row.note == "❌ not available"
    assert row.disabled is True


def test_cosmetics_unowned_perk_shows_rent():
    row = next(r for r in _rows() if r.perk == "role_color")
    assert row.kind == "rent"
    assert row.note == ""
    assert row.disabled is False
    assert row.button_label == "🎨 Rent"


def test_cosmetics_unowned_gated_perk_still_shows_rent_but_disabled():
    row = next(r for r in _rows(gated={"role_color"}) if r.perk == "role_color")
    assert row.kind == "rent"
    assert row.disabled is True
    assert row.note == "❌ not available"


def test_cosmetics_row_ids_are_sequential_from_the_base():
    rows = _rows(icon_catalog=(75, 400, 12))
    ids = [r.row_id for r in rows]
    assert ids == list(range(100, 100 + len(rows)))


def test_cosmetics_every_self_perk_appears_when_all_on_sale_and_unowned():
    rows = _rows(icon_catalog=(75, 400, 12), color_catalog=(50, 200, 9))
    assert {r.perk for r in rows} == set(SELF_PERKS)


# ── shop_section_rows: server ────────────────────────────────────────────


def test_server_voice_style_absent_when_off_and_unowned():
    settings = replace(SETTINGS, shop_voice_style_enabled=False)
    rows = _rows(section=SECTION_SERVER, settings=settings)
    assert not any(r.perk == "voice_style" for r in rows)


def test_server_voice_style_rent_row():
    settings = replace(SETTINGS, shop_voice_style_enabled=True)
    row = next(r for r in _rows(section=SECTION_SERVER, settings=settings))
    assert row.kind == "voice"
    assert row.disabled is False
    assert row.button_label == "🎙️ Rent"


def test_server_voice_style_owned_shows_leased_and_locked():
    settings = replace(SETTINGS, shop_voice_style_enabled=False)
    row = next(r for r in _rows(section=SECTION_SERVER, settings=settings, owned={"voice_style"}))
    assert row.kind == "voice_active"
    assert row.disabled is True
    assert row.button_label == "🎙️ Leased"


def test_server_pin_row_needs_price_and_channel():
    settings = replace(SETTINGS, price_pin_of_day=50, pin_channel_id=0)
    assert not any(r.kind == "pin" for r in _rows(section=SECTION_SERVER, settings=settings))
    settings = replace(settings, pin_channel_id=123)
    row = next(r for r in _rows(section=SECTION_SERVER, settings=settings) if r.kind == "pin")
    assert row.button_label == "📌 Pin"


def test_server_theme_row_needs_toggle_and_channel():
    settings = replace(SETTINGS, flash_theme_enabled=True, theme_channel_id=0)
    assert not any(r.kind == "theme" for r in _rows(section=SECTION_SERVER, settings=settings))
    settings = replace(settings, theme_channel_id=321)
    row = next(r for r in _rows(section=SECTION_SERVER, settings=settings) if r.kind == "theme")
    assert row.button_label == "🎭 Book"


def test_server_empty_when_nothing_switched_on():
    settings = replace(
        SETTINGS, shop_voice_style_enabled=False,
        pin_channel_id=0, theme_channel_id=0,
    )
    assert _rows(section=SECTION_SERVER, settings=settings) == []


# ── shop_section_rows: games ──────────────────────────────────────────────


def test_games_shield_row_unheld():
    settings = replace(SETTINGS, shop_streak_shield_enabled=True)
    row = next(r for r in _rows(section=SECTION_GAMES, settings=settings))
    assert row.kind == "shield"
    assert row.disabled is False
    assert row.button_label == "🛡️ Shield"
    assert row.note == ""


def test_games_shield_row_held_locks_and_relabels():
    settings = replace(SETTINGS, shop_streak_shield_enabled=True)
    row = next(r for r in _rows(section=SECTION_GAMES, settings=settings, shields_held=1))
    assert row.disabled is True
    assert row.button_label == "🛡️ Held"
    assert row.note == "🛡️ held"


def test_games_shield_absent_when_switched_off():
    settings = replace(SETTINGS, shop_streak_shield_enabled=False)
    assert not any(
        r.kind == "shield" for r in _rows(section=SECTION_GAMES, settings=settings)
    )


def test_games_raffle_row_follows_the_toggle_and_price():
    off = _rows(section=SECTION_GAMES, settings=replace(SETTINGS, raffle_enabled=False))
    assert not any(r.kind == "raffle" for r in off)
    on = _rows(
        section=SECTION_GAMES,
        settings=replace(SETTINGS, raffle_enabled=True, price_raffle_ticket=10),
    )
    row = next(r for r in on if r.kind == "raffle")
    assert row.button_label == "🎟️ Tickets"


# ── shop_section_rows: the trailing refund row ───────────────────────────


def test_refund_row_trails_your_rentals_when_refundable():
    rows = _rows(section=SECTION_YOURS, settings=replace(SETTINGS, enabled=False), refundable=True)
    assert rows[-1].kind == "refund"
    assert rows[-1].button_label == "↩️ Refund"
    # No price to sit beside a status note, so the note carries the row's
    # whole third line instead — a row is never two lines.
    assert rows[-1].price == ""
    assert rows[-1].note == "unused days credited back"


@pytest.mark.parametrize("section", [SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES])
def test_no_shelf_carries_the_refund_row(section):
    """Cancel & Refund lives on 🧾 Your Rentals only, since the split."""
    rows = _rows(section=section, settings=replace(SETTINGS, enabled=False), refundable=True)
    assert not any(r.kind == "refund" for r in rows)


# ── shop_section_rows: 🧾 Your Rentals ──────────────────────────────────


def test_your_rentals_gathers_owned_rows_from_every_shelf():
    settings = replace(SETTINGS, shop_voice_style_enabled=True)
    rows = _rows(
        section=SECTION_YOURS, settings=settings,
        owned={"role_color", "voice_style"}, refundable=True,
    )
    assert [r.perk for r in rows if r.kind != "refund"] == ["role_color", "voice_style"]
    # The shelf's own button for the row, not a Rent.
    assert rows[0].button_label == _rows(owned={"role_color"})[
        [r.perk for r in _rows(owned={"role_color"})].index("role_color")
    ].button_label


def test_your_rentals_skips_what_the_member_does_not_own():
    assert _rows(section=SECTION_YOURS) == []


def test_your_rentals_keeps_a_perk_whose_shelf_stopped_selling_it():
    """The reason the row exists: a rental outlives its perk leaving sale."""
    settings = replace(SETTINGS, shop_role_gradient_enabled=False)
    rows = _rows(section=SECTION_YOURS, settings=settings, owned={"role_gradient"})
    assert [r.perk for r in rows] == ["role_gradient"]


@pytest.mark.parametrize("held, shown", [(0, False), (1, True)])
def test_your_rentals_shows_a_held_shield_only(held, shown):
    rows = _rows(section=SECTION_YOURS, shields_held=held)
    assert any(r.kind == "shield" for r in rows) is shown


def test_your_rentals_renumbers_rows_from_the_base():
    """Each shelf numbers from the base, so gathered rows must be renumbered
    or two buttons on one card would share an id."""
    settings = replace(SETTINGS, shop_voice_style_enabled=True)
    rows = _rows(
        section=SECTION_YOURS, settings=settings,
        owned={"role_color", "voice_style"}, shields_held=1, refundable=True,
    )
    ids = [r.row_id for r in rows]
    assert ids == list(range(ids[0], ids[0] + len(rows)))
    assert ids[0] == _rows()[0].row_id


def test_your_rentals_empty_says_so():
    items = build_shop_section_layout(SETTINGS, SECTION_YOURS, [], balance=0, page=0, nav=[])
    text = _all_text(items)
    assert "## 🧾 Your Rentals" in text
    assert "You aren't renting anything right now." in text


def test_no_refund_row_when_not_refundable():
    settings = replace(SETTINGS, enabled=False)
    rows = _rows(section=SECTION_GAMES, settings=settings, refundable=False)
    assert not any(r.kind == "refund" for r in rows)


# ── build_shop_section_layout ────────────────────────────────────────────


def test_section_layout_renders_a_row_per_shop_row():
    rows = _rows(icon_catalog=(75, 400, 12))
    items = build_shop_section_layout(
        SETTINGS, SECTION_COSMETICS, rows, balance=500, page=0, nav=["a"],
    )
    accessories = _accessories(items)
    assert [b.label for b in accessories] == [r.button_label for r in rows]
    assert [b.disabled for b in accessories] == [r.disabled for r in rows]
    assert [b.id for b in accessories] == [r.row_id for r in rows]
    text = _all_text(items)
    assert "## 🎨 Role Cosmetics" in text
    assert "500" in text


def test_section_layout_games_tail_differs():
    text = _all_text(build_shop_section_layout(SETTINGS, SECTION_GAMES, [], balance=0, page=0, nav=[]))
    assert "Bought once" in text
    other = _all_text(build_shop_section_layout(SETTINGS, SECTION_SERVER, [], balance=0, page=0, nav=[]))
    assert "Rent by the week" in other


def test_section_layout_empty_says_so():
    items = build_shop_section_layout(SETTINGS, SECTION_SERVER, [], balance=0, page=0, nav=[])
    assert "Nothing for sale here right now." in _all_text(items)


def test_section_layout_single_page_has_no_arrows_or_counter():
    items = build_shop_section_layout(SETTINGS, SECTION_COSMETICS, [], balance=0, page=0, nav=["one"])
    assert not any(isinstance(i, discord.ui.ActionRow) for i in items)
    assert "Page" not in _all_text(items)
    # A one-page shop gets no chip either — it rides the same row as the
    # arrows, and there's no row at all to carry it.
    assert not any(
        isinstance(c, discord.ui.Button) and c.id == PAGE_CHIP_ID
        for i in items if isinstance(i, discord.ui.ActionRow)
        for c in i.children
    )


def test_section_layout_multi_page_arrows_disable_at_the_ends():
    items = build_shop_section_layout(
        SETTINGS, SECTION_COSMETICS, [], balance=0, page=0, nav=["a", "b", "c"],
    )
    (row,) = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    prev_btn, chip, next_btn = _row_buttons(row)
    assert prev_btn.disabled is True
    assert next_btn.disabled is False
    assert "Page 1 of 3" in _all_text(items)
    # The caption chip sits between the arrows: greyed out, naming the page
    # you're on, no callback to press.
    assert chip.id == PAGE_CHIP_ID
    assert chip.label == "a"
    assert chip.disabled is True
    assert chip.style == discord.ButtonStyle.secondary

    last_page = build_shop_section_layout(
        SETTINGS, SECTION_COSMETICS, [], balance=0, page=2, nav=["a", "b", "c"],
    )
    (row2,) = [i for i in last_page if isinstance(i, discord.ui.ActionRow)]
    prev2, chip2, next2 = _row_buttons(row2)
    assert prev2.disabled is False
    assert next2.disabled is True
    assert chip2.label == "c"
    assert chip2.disabled is True


def test_section_layout_chip_caption_is_cut_to_discords_80_char_label_limit():
    """A section's own caption can't overflow Discord's button label limit —
    mirrors the embed era's ``_add_page_nav`` (``captions[page][:80]``)."""
    long_caption = "🎁 Specials · " + "x" * 90
    items = build_shop_section_layout(
        SETTINGS, SECTION_COSMETICS, [], balance=0, page=0, nav=[long_caption, "b"],
    )
    (row,) = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    _prev, chip, _next = _row_buttons(row)
    assert chip.label == long_caption[:80]
    assert len(chip.label) == 80


def test_section_layout_budget_worst_case_all_six_self_perks():
    """Measured, not estimated: every cosmetics row on at once (icon *and*
    palette catalogs, a gated/owned mix so the longest status note —
    ``❌ not available`` — wins over the shorter ones), four-figure prices, a
    three-figure catalog size (the true worst case for the icon row's
    ``{count} + custom`` note beside a price *span*, not the flat price every
    other row shows), balance 99,999, a three-page nav. Pins the number the
    commit body reports rather than leaving it a one-off measurement nothing
    reproduces. No refund row since the split: it moved to 🧾 Your Rentals,
    measured below.
    """
    budget_settings = replace(
        SETTINGS,
        price_role_color=1000, price_role_name=1000, price_role_icon=1000,
        price_role_preset=1000, price_role_gradient=1000, price_role_holographic=1000,
    )
    rows = _rows(
        settings=budget_settings,
        gated={"role_color", "role_gradient"},
        owned={"role_color", "role_holographic"},
        icon_catalog=(1000, 9999, 999), color_catalog=(1000, 9999, 999),
        refundable=True,
    )
    view = _view(build_shop_section_layout(
        budget_settings, SECTION_COSMETICS, rows, balance=99_999, page=0,
        nav=["a", "b", "c"],
    ))
    check_budget(view)
    budget = layout_budget(view)
    assert budget == (24, 545)
    assert budget[0] < MAX_COMPONENTS


def test_your_rentals_budget_worst_case_owning_everything():
    """🧾 Your Rentals gathers every shelf, so its worst case is a member
    renting every perk on every shelf, holding a shield, with the refund row
    — the biggest card the shop can draw now."""
    settings = replace(
        SETTINGS, shop_voice_style_enabled=True, shop_streak_shield_enabled=True,
    )
    rows = _rows(
        section=SECTION_YOURS, settings=settings,
        owned={*SELF_PERKS, "voice_style"}, shields_held=1, refundable=True,
        icon_catalog=(1000, 9999, 999), color_catalog=(1000, 9999, 999),
    )
    assert {r.kind for r in rows} >= {"refund", "shield"}
    view = _view(build_shop_section_layout(
        settings, SECTION_YOURS, rows, balance=99_999, page=0, nav=[],
    ))
    check_budget(view)
    assert layout_budget(view)[0] < MAX_COMPONENTS


#: The worst-case gated/owned/held state per section, shared by the per-line
#: budget sweep below and the accessory-label sweep further down — one
#: matrix of "everything a section can show at once", not two copies of it.
SECTION_WORST_CASE_MATRIX = [
    pytest.param(
        SECTION_COSMETICS, SETTINGS,
        dict(
            gated={"role_color", "role_gradient"},
            owned={"role_color", "role_holographic"},
            icon_catalog=(75, 400, 999), color_catalog=(50, 200, 999),
        ),
        id="cosmetics-mixed-states",
    ),
    pytest.param(
        SECTION_COSMETICS,
        replace(SETTINGS, price_role_preset=9999, price_role_icon=9999),
        dict(
            gated={"role_preset", "role_icon"},
            icon_catalog=(9999, 9999, 999),
        ),
        id="cosmetics-gated-preset-icon-four-figure-price",
    ),
    pytest.param(
        SECTION_SERVER,
        replace(
            SETTINGS, shop_voice_style_enabled=True,
            price_pin_of_day=1500, pin_channel_id=1, flash_theme_enabled=True,
            theme_channel_id=1, price_flash_theme=1500, theme_hours=48,
        ),
        dict(owned={"voice_style"}),
        id="server-leased-and-priced",
    ),
    pytest.param(
        SECTION_GAMES,
        replace(
            SETTINGS, shop_streak_shield_enabled=True, price_streak_shield=1500,
            raffle_enabled=True, price_raffle_ticket=250, raffle_max_tickets=25,
        ),
        dict(shields_held=1),
        id="games-shield-held",
    ),
    pytest.param(
        SECTION_YOURS,
        replace(SETTINGS, shop_voice_style_enabled=True, shop_streak_shield_enabled=True),
        dict(
            owned={*SELF_PERKS, "voice_style"}, shields_held=1,
            icon_catalog=(75, 400, 999), color_catalog=(50, 200, 999),
        ),
        id="yours-everything-rented",
    ),
]


@pytest.mark.parametrize("section,settings,kw", SECTION_WORST_CASE_MATRIX)
def test_section_rows_stay_under_budget_line_by_line(section, settings, kw):
    """Every row a section can show, at a representative worst case (every
    gate/owned/held state on at once, four-figure prices, a three-figure
    catalog size), is **exactly** three lines — ``**{title}**``, the
    description, and a ``-#`` price/status line — each held under
    ``MAX_ROW_CHARS`` on its own (2026-09-24, at Billy's request: every row
    carries all three text elements, always — no row is a bare title or a
    two-line one).
    """
    rows = _rows(section=section, settings=settings, refundable=True, **kw)
    assert rows, "nothing to measure"
    items = build_shop_section_layout(
        settings, section, rows, balance=99_999, page=0, nav=[],
    )
    for section_item, row in zip(_sections(items), rows, strict=True):
        text = section_item.children[0].content
        lines = text.split("\n")
        assert len(lines) == 3, f"a catalogue row is always three lines: {text!r}"
        assert lines[0] == f"**{row.title}**"
        assert row.desc, "every row carries a description"
        assert lines[1] == row.desc, "the middle line is the description"
        assert lines[2].startswith("-# "), f"the price line is subtext: {text!r}"
        if row.price:
            assert row.price in lines[2]
        if row.note:
            assert row.note in lines[2]
        for line in lines:
            assert _line_visible_length(line) <= MAX_ROW_CHARS, (line, text)


# ── build_store_layout / _store_row ──────────────────────────────────────


def test_store_row_shows_owned():
    items = build_store_layout(
        SETTINGS, [_item()], owned_item_ids={1}, balance=0, page=0, nav=[],
    )
    text = _all_text(items)
    assert "✅ yours" in text
    (accessory,) = _accessories(items)
    assert accessory.disabled is True


def test_store_row_shows_sold_out():
    item = _item(stock=5, sold=5)
    items = build_store_layout(SETTINGS, [item], owned_item_ids=set(), balance=0, page=0, nav=[])
    assert "sold out" in _all_text(items)
    (accessory,) = _accessories(items)
    assert accessory.disabled is True


def test_store_row_shows_remaining_count():
    item = _item(stock=5, sold=2)
    items = build_store_layout(SETTINGS, [item], owned_item_ids=set(), balance=0, page=0, nav=[])
    assert "3 left" in _all_text(items)
    (accessory,) = _accessories(items)
    assert accessory.disabled is False


def test_store_row_shows_one_each_when_unlimited_but_per_member_capped():
    item = _item(per_member_limit=1)
    text = _all_text(build_store_layout(SETTINGS, [item], owned_item_ids=set(), balance=0, page=0, nav=[]))
    assert "one each" in text


def test_store_row_marks_a_rental_per_week():
    from bot_modules.economy.shop_items import BILLING_WEEKLY

    item = _item(billing=BILLING_WEEKLY)
    assert "/wk" in _all_text(build_store_layout(SETTINGS, [item], owned_item_ids=set(), balance=0, page=0, nav=[]))


def test_store_empty_says_so():
    items = build_store_layout(SETTINGS, [], owned_item_ids=set(), balance=0, page=0, nav=[])
    assert "Nothing on the shelves yet." in _all_text(items)


def test_store_footer_falls_back_without_a_pager():
    items = build_store_layout(SETTINGS, [_item()], owned_item_ids=set(), balance=0, page=0, nav=["a"])
    assert "Pick one below to buy it." in _all_text(items)


def test_store_footer_is_the_page_line_with_a_pager():
    items = build_store_layout(
        SETTINGS, [_item()], owned_item_ids=set(), balance=0, page=1, nav=["a", "b"],
    )
    assert "Page 2 of 2" in _all_text(items)
    assert "Pick one below" not in _all_text(items)


def test_store_page_carries_the_caption_chip_between_the_arrows():
    """The chip isn't a section-only shape — a Specials page gets it too,
    reading whatever ``shop.page_captions`` handed it for that page (a
    spread caption like "🎁 Specials · 11–20" on a multi-page store)."""
    items = build_store_layout(
        SETTINGS, [_item()], owned_item_ids=set(), balance=0, page=0,
        nav=["🎁 Specials · 1–10", "🎁 Specials · 11–20"],
    )
    (row,) = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    prev_btn, chip, next_btn = _row_buttons(row)
    assert chip.id == PAGE_CHIP_ID
    assert chip.label == "🎁 Specials · 1–10"
    assert chip.disabled is True
    assert chip.style == discord.ButtonStyle.secondary
    assert prev_btn.disabled is True
    assert next_btn.disabled is False


def test_store_single_page_has_no_chip_or_arrows():
    items = build_store_layout(
        SETTINGS, [_item()], owned_item_ids=set(), balance=0, page=0, nav=["only"],
    )
    assert not any(isinstance(i, discord.ui.ActionRow) for i in items)


def test_store_budget_worst_case_ten_item_page():
    """Measured, not estimated: ten items with 60-char names, 40-char
    blurbs, price 999,999, one of every status bit (sold out / N left /
    one each / weekly rental / owned / plain), a three-page nav. Pins the
    number the commit body reports rather than leaving it a one-off
    measurement nothing reproduces.
    """
    from bot_modules.economy.shop_items import BILLING_WEEKLY

    name60 = "A" * 60
    blurb40 = "B" * 40
    items = [
        _item(item_id=1, name=name60, price=999_999, blurb=blurb40, stock=1, sold=1),  # sold out
        _item(item_id=2, name=name60, price=999_999, blurb=blurb40, stock=5, sold=2),  # N left
        _item(item_id=3, name=name60, price=999_999, blurb=blurb40, per_member_limit=1),  # one each
        _item(item_id=4, name=name60, price=999_999, blurb=blurb40, billing=BILLING_WEEKLY),
        _item(item_id=5, name=name60, price=999_999, blurb=blurb40),  # owned -> "yours"
        *[_item(item_id=i, name=name60, price=999_999, blurb=blurb40) for i in range(6, 11)],
    ]
    view = _view(build_store_layout(
        SETTINGS, items, owned_item_ids={5}, balance=99_999, page=0, nav=["a", "b", "c"],
    ))
    check_budget(view)
    budget = layout_budget(view)
    assert budget == (36, 931)


def test_store_row_with_a_blurb_is_its_own_line():
    """The blurb is the row's middle line — present, and never merged with
    the name or the price line."""
    items = build_store_layout(
        SETTINGS, [_item(blurb="a short note")], owned_item_ids=set(),
        balance=0, page=0, nav=[],
    )
    (section,) = _sections(items)
    lines = section.children[0].content.split("\n")
    assert lines == ["**Widget**", "a short note", "-# 🪙 250"]


@pytest.mark.parametrize(
    "kind, billing",
    [
        (KIND_ROLE, BILLING_ONCE),
        (KIND_ROLE, BILLING_WEEKLY),
        (KIND_MANUAL, BILLING_ONCE),
        (KIND_MANUAL, BILLING_WEEKLY),
    ],
)
def test_store_row_without_a_blurb_falls_back_to_a_kind_derived_line(kind, billing):
    """A row is never two lines: an admin who left the blurb blank still
    gets a middle line, derived from what the item *is*."""
    items = build_store_layout(
        SETTINGS, [_item(blurb="", kind=kind, billing=billing)],
        owned_item_ids=set(), balance=0, page=0, nav=[],
    )
    (section,) = _sections(items)
    lines = section.children[0].content.split("\n")
    assert len(lines) == 3
    assert lines[1] == STORE_FALLBACK_BLURBS[(kind, billing)]


def test_store_row_stays_under_budget_line_by_line_at_the_worst_case():
    """The dashboard now caps an admin's blurb at 30 chars and the name at
    60 (``routes/economy.py``'s ``ShopItemBody``), but a row saved before
    the blurb cap dropped from 40 still has to fit, and ``_store_row`` — unlike
    ``shop_section_rows`` and the refund picker — once had no budget guard
    of its own, so admin-authored data this long wrapped and pushed the Buy
    button down (guide § Screen use on a phone). Name, blurb and price/status now each
    get their own line, and each line is held under ``MAX_ROW_CHARS`` on its
    own; the name is clipped, never silently dropped — it's the row's
    identity.
    """
    item = _item(
        item_id=1, name="A" * 60, blurb="B" * 40, price=999_999,
    )
    items = build_store_layout(
        SETTINGS, [item], owned_item_ids=set(), balance=0, page=0, nav=[],
    )
    (section,) = _sections(items)
    lines = section.children[0].content.split("\n")
    assert len(lines) == 3
    for line in lines:
        assert _line_visible_length(line) <= MAX_ROW_CHARS, line
    assert lines[0].replace("**", "").startswith("A" * (STORE_NAME_CLIP - 1) + "…")
    assert lines[1].startswith("B")
    assert lines[2].startswith("-# 🪙 999,999")


def test_store_row_escapes_admin_typed_markdown_in_name_and_blurb():
    """An admin can type anything into a Server Store item's name and blurb
    (``routes/economy.py``'s ``ShopItemBody`` caps length only, not markup).
    Unescaped, a stray ``**``/``_``/`` ` `` collides with the row's own
    ``**{name}**`` wrapping and breaks the line's rendering — mirrors
    ``bounty_layouts._bounty_row``'s ``escape_markdown(_clip(...))`` guard
    for the same admin/member-typed title.
    """
    item = _item(name="**Founder**", blurb="click `here` now")
    items = build_store_layout(
        SETTINGS, [item], owned_item_ids=set(), balance=0, page=0, nav=[],
    )
    (section,) = _sections(items)
    lines = section.children[0].content.split("\n")
    # the row's own bold wrapper is the only unescaped "**" pair in the text
    assert lines[0] == "**\\*\\*Founder\\*\\***"
    assert lines[1] == "click \\`here\\` now"
    assert lines[2] == "-# 🪙 250"


# ── build_refund_picker_layout ────────────────────────────────────────────


def _rental(perk="role_color", state="active", price=100, next_bill_at=2_000_000_000.0):
    return {"perk": perk, "state": state, "price": price, "next_bill_at": next_bill_at}


def test_refund_picker_active_rental_previews_a_prorated_amount():
    items = build_refund_picker_layout(SETTINGS, [_rental()], shield_price=0)
    text = _all_text(items)
    assert "back · ends now" in text
    (accessory,) = _accessories(items)
    assert accessory.id == REFUND_ROW_ID_BASE
    assert accessory.style == discord.ButtonStyle.danger


def test_refund_picker_grace_rental_gets_no_refund():
    items = build_refund_picker_layout(SETTINGS, [_rental(state="grace")], shield_price=0)
    assert "no refund" in _all_text(items)


def test_refund_picker_shield_row_only_when_held():
    none_held = build_refund_picker_layout(SETTINGS, [], shield_price=0)
    assert "Nothing to cancel right now." in _all_text(none_held)
    held = build_refund_picker_layout(SETTINGS, [], shield_price=30)
    assert "Streak Shield" in _all_text(held)


def test_refund_picker_row_ids_follow_rentals_then_shield():
    items = build_refund_picker_layout(SETTINGS, [_rental(), _rental(perk="role_name")], shield_price=30)
    ids = [a.id for a in _accessories(items)]
    assert ids == [REFUND_ROW_ID_BASE, REFUND_ROW_ID_BASE + 1, REFUND_ROW_ID_BASE + 2]


def test_refund_picker_rows_stay_on_one_line_at_the_worst_case():
    """Every picker row is exactly two lines (2026-09-24, at Billy's request)
    — the bold label, then a ``-#`` line with what comes back, held under
    ``MAX_ROW_CHARS`` on its own even at the longest label (``role_color``)
    and a four-figure price."""
    items = build_refund_picker_layout(
        SETTINGS,
        [_rental(perk="role_color", price=9_999), _rental(perk="role_gradient", state="grace")],
        shield_price=9_999,
    )
    for section in _sections(items):
        text = section.children[0].content
        lines = text.split("\n")
        assert len(lines) == 2, f"a picker row is always two lines: {text!r}"
        assert lines[1].startswith("-# ")
        for line in lines:
            assert _line_visible_length(line) <= MAX_ROW_CHARS, line


# ── refund confirm / done / missing ───────────────────────────────────────


def test_refund_confirm_has_confirm_and_back():
    items = build_refund_confirm_layout(SETTINGS, "Custom Role Color", "🪙 40 back.")
    (row,) = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    confirm, back = _row_buttons(row)
    assert confirm.id == REFUND_CONFIRM_ID
    assert confirm.style == discord.ButtonStyle.danger
    assert back.id == REFUND_BACK_ID
    assert "Cancel Custom Role Color?" in _all_text(items)
    assert "40 back." in _all_text(items)


def test_refund_done_credits_when_there_was_something_to_refund():
    text = _all_text(build_refund_done_layout(SETTINGS, "Custom Role Color", 40))
    assert "40" in text
    assert "Cancelled" in text


def test_refund_done_says_no_refund_at_zero():
    text = _all_text(build_refund_done_layout(SETTINGS, "Streak Shield", 0))
    assert "already in the grace period" in text


def test_refund_missing_names_the_thing_lowercased():
    text = _all_text(build_refund_missing_layout("Custom Role Color"))
    assert "custom role color" in text
    assert "isn't there anymore" in text


# ── build_post_rent_layout ─────────────────────────────────────────────


def test_post_rent_voice_style_has_no_customise_button_even_if_asked():
    items = build_post_rent_layout("voice_style", comped=False, customise=True)
    assert not _accessories(items)
    assert "Renaming and sizing" in _all_text(items)


def test_post_rent_no_config_perk_has_no_customise_button():
    (perk,) = NO_CONFIG_PERKS
    items = build_post_rent_layout(perk, comped=False, customise=True)
    assert not _accessories(items)
    assert "holographic shimmer" in _all_text(items)


def test_post_rent_configurable_perk_gets_the_customise_accessory():
    items = build_post_rent_layout("role_color", comped=False, customise=True)
    (accessory,) = _accessories(items)
    assert accessory.id == POST_RENT_CUSTOMISE_ID
    assert accessory.label == CUSTOMISE_LABELS["role_color"]


def test_post_rent_no_customise_when_not_asked_for():
    items = build_post_rent_layout("role_color", comped=False, customise=False)
    assert not _accessories(items)


def test_post_rent_role_icon_carries_the_upload_note():
    text = _all_text(build_post_rent_layout("role_icon", comped=False, customise=True))
    assert "/bank role icon" in text


def test_post_rent_comped_says_unlocked_not_rented():
    comped = _all_text(build_post_rent_layout("role_color", comped=True, customise=False))
    assert "Unlocked" in comped
    paid = _all_text(build_post_rent_layout("role_color", comped=False, customise=False))
    assert "Rented" in paid


# ── build_role_notice_layout ─────────────────────────────────────────────


def test_role_notice_without_customise_is_plain_text():
    items = build_role_notice_layout("Some body text.", perk="role_color", customise=False)
    assert not _accessories(items)
    assert _all_text(items) == "Some body text."


def test_role_notice_with_customise_shares_the_post_rent_accessory():
    items = build_role_notice_layout("Switched to a custom icon.", perk="role_icon", customise=True)
    (accessory,) = _accessories(items)
    assert accessory.id == POST_RENT_CUSTOMISE_ID
    assert accessory.label == CUSTOMISE_LABELS["role_icon"]


def test_role_notice_falls_back_to_a_generic_label_for_an_unmapped_perk():
    items = build_role_notice_layout("Body.", perk="not_a_real_perk", customise=True)
    (accessory,) = _accessories(items)
    assert accessory.label == "Customize"


# ── every accessory label stays a phone-safe shape ───────────────────────


@pytest.mark.parametrize("section,settings,kw", SECTION_WORST_CASE_MATRIX)
def test_every_section_accessory_label_fits_the_phone(section, settings, kw):
    """Every Section's own Button accessory — never an ActionRow button, hub
    panel or pager included — stays within ``MAX_ACCESSORY_LABEL`` columns
    (2026-09-24, Billy's phone screenshot: "Browse Colors", at 13 columns,
    dropped under its row while "Set Color", at 9, stayed beside it). Swept
    over the same worst-case gated/owned/held matrix the per-line budget
    tests use, plus a refundable pass so the refund row's own accessory is
    covered too.
    """
    for refundable in (False, True):
        rows = _rows(section=section, settings=settings, refundable=refundable, **kw)
        items = build_shop_section_layout(
            settings, section, rows, balance=99_999, page=0, nav=[],
        )
        for accessory in _accessories(items):
            assert accessory.label is not None
            assert text_columns(accessory.label) <= MAX_ACCESSORY_LABEL, accessory.label


def test_every_store_buy_button_fits_the_phone():
    items = build_store_layout(
        SETTINGS, [_item(name="A" * 60, blurb="B" * 40, price=999_999)],
        owned_item_ids=set(), balance=0, page=0, nav=[],
    )
    (accessory,) = _accessories(items)
    assert accessory.label is not None
    assert text_columns(accessory.label) <= MAX_ACCESSORY_LABEL


def test_every_refund_picker_cancel_button_fits_the_phone():
    items = build_refund_picker_layout(
        SETTINGS,
        [_rental(perk="role_color", price=9_999), _rental(perk="role_gradient", state="grace")],
        shield_price=9_999,
    )
    for accessory in _accessories(items):
        assert accessory.label is not None
        assert text_columns(accessory.label) <= MAX_ACCESSORY_LABEL


def test_every_post_rent_customise_button_fits_the_phone():
    for perk in CUSTOMISE_LABELS:
        items = build_post_rent_layout(perk, comped=False, customise=True)
        for accessory in _accessories(items):
            assert accessory.label is not None
            assert text_columns(accessory.label) <= MAX_ACCESSORY_LABEL


def test_every_customise_label_fits_the_phone():
    for label in CUSTOMISE_LABELS.values():
        assert text_columns(label) <= MAX_ACCESSORY_LABEL, label
