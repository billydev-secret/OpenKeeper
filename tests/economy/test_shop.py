"""``bot_modules/economy/shop.py`` — availability, pricing and pagination.

Pure functions, no cog, interaction or Discord mock. What renders from them
(the Components V2 cards) is tested in ``test_shop_layouts.py``; this file
only covers what both that module and the cog agree on: which perks a guild
is selling, what sections and pages the shop has, and what a row's price
reads as.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.shop import (
    SECTION_COSMETICS,
    SECTION_GAMES,
    SECTION_SERVER,
    SECTION_SPECIALS,
    SECTION_YOURS,
    page_captions,
    section_book,
    shop_pages,
    shop_pointer_line,
    shop_row_price,
    shop_sections,
    stocked_perks,
    store_page,
    store_page_count,
)
from bot_modules.economy.perks import SELF_PERKS
from bot_modules.economy.service import (
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD_ID = 9001


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _settings(db):
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _items(n: int, **kw):
    from bot_modules.economy.shop_items import ItemView

    return [
        ItemView(item_id=i, name=f"Item {i:02d}", blurb="a thing", price=100 + i, **kw)
        for i in range(1, n + 1)
    ]


# ── shop_row_price: the icon/palette catalog span, folded vs. not ──────────


@pytest.mark.parametrize(
    ("overrides", "icon_catalog", "expected"),
    [
        pytest.param({}, (120, 400, 40), (75, "75–400"), id="flat-below-catalog"),
        pytest.param(
            {"price_role_icon": 500}, (120, 400, 40), (120, "120–500"),
            id="catalog-below-flat",
        ),
        pytest.param(
            {"price_role_icon": 200}, (200, 200, 3), (200, "200"),
            id="single-price-collapses",
        ),
    ],
)
def test_role_icon_price_folds_in_the_flat_price(db, overrides, icon_catalog, expected):
    """A curated icon catalog prices per icon, but the bring-your-own Custom
    entry still sells at the flat price, so the row's span must cover it."""
    _enable(db, **overrides)
    assert shop_row_price(_settings(db), "role_icon", icon_catalog) == expected


def test_role_preset_price_ignores_the_flat_price(db):
    """No bring-your-own palette colour exists, so the flat price never
    widens the span — only the catalog's own prices are on offer."""
    _enable(db, price_role_preset=999)
    sort_key, text = shop_row_price(_settings(db), "role_preset", None, (100, 250, 11))
    assert text == "100–250"
    assert "999" not in text
    assert sort_key == 100


def test_a_flat_perk_has_no_span(db):
    _enable(db, price_role_name=35)
    assert shop_row_price(_settings(db), "role_name", None) == (35, "35")


# ── stocked_perks ────────────────────────────────────────────────────────


def test_stocked_perks_drops_a_switched_off_perk(db):
    _enable(db, shop_role_gradient_enabled=False)
    settings = _settings(db)
    assert "role_gradient" not in stocked_perks(settings, SELF_PERKS)
    assert "role_color" in stocked_perks(settings, SELF_PERKS)


def test_stocked_perks_keeps_a_switched_off_perk_a_member_still_rents(db):
    """A member mid-rental keeps their row after the perk is withdrawn — the
    carve-out that avoids billing someone for a perk with no row anywhere."""
    _enable(db, shop_role_gradient_enabled=False)
    settings = _settings(db)
    assert "role_gradient" in stocked_perks(
        settings, SELF_PERKS, owned={"role_gradient"}
    )


def test_stocked_perks_drops_the_palette_without_one(db):
    _enable(db)
    settings = _settings(db)
    assert "role_preset" not in stocked_perks(settings, SELF_PERKS, has_palette=False)
    assert "role_preset" in stocked_perks(settings, SELF_PERKS, has_palette=True)


# ── shop_sections: an empty section gets no page at all ─────────────────


def test_shop_sections_is_the_four_shelves_billy_specced(db):
    _enable(db, shop_voice_style_enabled=True)
    assert shop_sections(_settings(db), items=_items(1)) == [
        SECTION_SPECIALS, SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES,
    ]


def test_shop_sections_drops_specials_with_nothing_stocked(db):
    _enable(db)
    assert SECTION_SPECIALS not in shop_sections(_settings(db))


def test_shop_sections_server_survives_on_any_one_product(db):
    """Switching the voice lease off alone must not retire the section —
    pin/theme keep it alive independently."""
    _enable(db, shop_voice_style_enabled=False, price_pin_of_day=25, pin_channel_id=43)
    assert SECTION_SERVER in shop_sections(_settings(db))
    _enable(db, price_pin_of_day=0)
    assert SECTION_SERVER not in shop_sections(_settings(db))


def test_shop_sections_server_is_empty_in_a_fresh_guild(db):
    """The retired sponsored question (price 40 by default) was the only thing
    that stocked Server Features out of the box; with it gone the section waits
    for an admin to switch a product on."""
    _enable(db)
    assert SECTION_SERVER not in shop_sections(_settings(db))


def test_shop_sections_games_follows_shield_or_raffle(db):
    _enable(db, shop_streak_shield_enabled=False, raffle_enabled=False)
    assert SECTION_GAMES not in shop_sections(_settings(db))
    _enable(db, shop_streak_shield_enabled=True)
    assert SECTION_GAMES in shop_sections(_settings(db))


# ── shop_pages: the shop as one flat book ────────────────────────────────


def test_shop_pages_is_the_book_with_specials_first_and_spread(db):
    _enable(db, shop_voice_style_enabled=True)
    full = shop_pages(_settings(db), items=_items(20))
    assert full == [
        (SECTION_SPECIALS, 0), (SECTION_SPECIALS, 1),
        (SECTION_COSMETICS, 0), (SECTION_SERVER, 0), (SECTION_GAMES, 0),
    ]
    # Only Specials runs to more than one page; the rest are one page flat.
    assert len(shop_pages(_settings(db), items=_items(3))) == 4


def test_shop_pages_never_empty(db):
    """Every switch off and nothing stocked still gets one page — the
    cosmetics floor — because the arrows need something to step between."""
    from bot_modules.economy.service import SHOP_TOGGLE_PERKS

    _enable(
        db, **{f"shop_{p}_enabled": False for p in SHOP_TOGGLE_PERKS},
        raffle_enabled=False,
    )
    assert shop_pages(_settings(db)) == [(SECTION_COSMETICS, 0)]


def test_page_captions_name_each_page_and_span_only_when_it_spreads(db):
    _enable(db, shop_voice_style_enabled=True)
    pages = shop_pages(_settings(db), items=_items(20))
    assert page_captions(pages, 20) == [
        "🎁 Specials · 1–10", "🎁 Specials · 11–20",
        "🎨 Role Cosmetics", "🏠 Server Features", "🎲 Game Features",
    ]
    assert page_captions(shop_pages(_settings(db), items=_items(12)), 12)[1] == (
        "🎁 Specials · 11–12"
    )
    assert page_captions(shop_pages(_settings(db), items=_items(7)), 7)[0] == (
        "🎁 Specials"
    )


def test_section_book_keeps_only_its_own_pages(db):
    """A panel Browse opens one shelf; its arrows never cross the seam."""
    _enable(db, shop_voice_style_enabled=True)
    pages = shop_pages(_settings(db), items=_items(20))
    assert section_book(pages, SECTION_SPECIALS) == [
        (SECTION_SPECIALS, 0), (SECTION_SPECIALS, 1),
    ]
    assert section_book(pages, SECTION_SERVER) == [(SECTION_SERVER, 0)]


def test_section_book_is_empty_for_a_closed_section(db):
    """The caller refuses by name rather than serving a different shelf."""
    _enable(db)
    assert section_book(shop_pages(_settings(db)), SECTION_SPECIALS) == []


def test_your_rentals_is_always_one_page(db):
    """It is not a shelf, so it has a page whatever the shelves hold."""
    _enable(db)
    assert section_book(shop_pages(_settings(db)), SECTION_YOURS) == [(SECTION_YOURS, 0)]
    assert SECTION_YOURS not in shop_sections(_settings(db))


# ── store paging ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "pages"), [(0, 1), (1, 1), (10, 1), (11, 2), (20, 2), (21, 3)],
)
def test_store_page_count(count, pages):
    assert store_page_count(_items(count)) == pages


def test_store_page_clamps_out_of_range():
    """An admin can delete items while a member sits on page 3 — clamping
    shows the last page that exists rather than an empty picker."""
    items = _items(12)
    assert [i.item_id for i in store_page(items, 0)] == list(range(1, 11))
    assert [i.item_id for i in store_page(items, 1)] == [11, 12]
    assert [i.item_id for i in store_page(items, 9)] == [11, 12]
    assert [i.item_id for i in store_page(items, -3)] == list(range(1, 11))


def test_shop_pointer_line_names_the_offer():
    line = shop_pointer_line("Buy a themed day of your own")
    assert line.startswith("Buy a themed day of your own — ")
    assert "/bank shop" in line
