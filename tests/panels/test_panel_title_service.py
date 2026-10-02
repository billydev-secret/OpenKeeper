"""The hub title drawn into the guild banner — panel_title_service."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops

from bot_modules.panels import title_service as svc
from bot_modules.core.branding_service import PanelArt

STEM = "0123456789abcdef0123456789abcdef"
BASE = "https://dk.example/static/panel-banners"


@pytest.fixture
def art_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(svc, "PANEL_ART_DIR", tmp_path)
    # The runner has no assets/: draw with Pillow's bundled face everywhere,
    # so every checkout exercises the same path.
    monkeypatch.setattr(svc, "TITLE_FONT", tmp_path / "missing.ttf")
    monkeypatch.setattr(svc, "LOBBY_TITLE_FONT", tmp_path / "missing.ttf")
    monkeypatch.setattr(svc, "LOBBY_SPRIG", tmp_path / "missing.png")
    return tmp_path


def _banner(art_dir: Path, *, frames: int = 1, ext: str = "webp") -> str:
    """A dark 300 × 130 banner (the title-ready 1200 × 520 at quarter size)."""
    imgs = [Image.new("RGBA", (300, 130), (0, 0, 0, 255)) for _ in range(frames)]
    for i, im in enumerate(imgs):
        im.putpixel((5, 5), (i * 40, 0, 0, 255))  # frames differ, so none merge
    path = art_dir / f"{STEM}.{ext}"
    if frames > 1:
        imgs[0].save(
            path, save_all=True, append_images=imgs[1:], duration=[70] * frames, loop=0
        )
    else:
        imgs[0].save(path)
    return f"{BASE}/{STEM}.{ext}"


def _titled_path(art_dir: Path, url: str) -> Path:
    return art_dir / svc.TITLED_DIR_NAME / url.rsplit("/", 1)[-1]


def test_titled_url_sits_beside_the_upload(art_dir):
    url = svc.titled_banner_url(_banner(art_dir), "DM Requests")
    assert url == f"{BASE}/titled/{svc.titled_name(STEM, 'DM Requests')}"
    assert _titled_path(art_dir, url).is_file()


def test_title_lands_in_the_bottom_band_and_leaves_the_art_above_alone(art_dir):
    url = svc.titled_banner_url(_banner(art_dir), "DM Requests")
    with Image.open(_titled_path(art_dir, url)) as im:
        drawn = im.convert("RGBA")
    assert drawn.size == (300, 130)
    lit = [
        (x, y)
        for y in range(130)
        for x in range(300)
        if sum(drawn.getpixel((x, y))[:3]) > 200
    ]
    assert lit, "no title drawn"
    top = min(y for _, y in lit)
    assert top > 130 * 0.6, "title should sit in the band under the art"
    xs = [x for x, _ in lit]
    assert abs((min(xs) + max(xs)) / 2 - 150) < 12, "title should be centred"


def test_every_frame_of_an_animation_is_titled(art_dir):
    url = svc.titled_banner_url(_banner(art_dir, frames=3), "Perk Shop")
    with Image.open(_titled_path(art_dir, url)) as im:
        assert getattr(im, "n_frames", 1) == 3
        for i in range(3):
            im.seek(i)
            frame = im.convert("RGBA")
            assert max(sum(frame.getpixel((x, 105))[:3]) for x in range(300)) > 200


def test_a_drawn_banner_is_reused_not_redrawn(art_dir, monkeypatch):
    banner = _banner(art_dir)
    first = svc.titled_banner_url(banner, "DM Requests")

    def boom(*_a, **_k):
        raise AssertionError("redrew a cached title")

    monkeypatch.setattr(svc, "render_titled", boom)
    assert svc.titled_banner_url(banner, "DM Requests") == first


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda: ("Perk Shop", svc.TITLE_STYLE_VERSION), id="other-title"),
        pytest.param(
            lambda: ("DM Requests", svc.TITLE_STYLE_VERSION + 1), id="restyle"
        ),
    ],
)
def test_a_new_title_or_style_draws_a_new_file(art_dir, monkeypatch, change):
    banner = _banner(art_dir)
    before = svc.titled_banner_url(banner, "DM Requests")
    title, version = change()
    monkeypatch.setattr(svc, "TITLE_STYLE_VERSION", version)
    after = svc.titled_banner_url(banner, title)
    assert after != before
    assert _titled_path(art_dir, after).is_file()


def test_a_long_title_shrinks_to_fit_the_frame(art_dir):
    url = svc.titled_banner_url(_banner(art_dir), "A Very Long Hub Name Indeed")
    with Image.open(_titled_path(art_dir, url)) as im:
        drawn = im.convert("RGBA")
    xs = [
        x
        for y in range(130)
        for x in range(300)
        if sum(drawn.getpixel((x, y))[:3]) > 200
    ]
    assert min(xs) > 300 * 0.05 and max(xs) < 300 * 0.95


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://elsewhere.example/banner.png", id="not-our-upload"),
        pytest.param(f"{BASE}/../../etc/passwd", id="traversal"),
        pytest.param(f"{BASE}/{STEM}.svg", id="not-raster-name"),
        pytest.param(f"{BASE}/{'f' * 32}.webp", id="file-gone"),
    ],
)
def test_undrawable_banners_return_none(art_dir, url):
    assert svc.titled_banner_url(url, "DM Requests") is None


def test_an_unreadable_upload_returns_none(art_dir):
    (art_dir / f"{STEM}.webp").write_bytes(b"not an image")
    assert svc.titled_banner_url(f"{BASE}/{STEM}.webp", "DM Requests") is None
    assert not (
        art_dir / svc.TITLED_DIR_NAME / svc.titled_name(STEM, "DM Requests")
    ).exists()


def test_with_title_swaps_in_the_drawn_banner(art_dir):
    banner = _banner(art_dir)
    art = svc.with_title(
        PanelArt(banner=banner, footer="F", title_in_banner=True), "DM Requests"
    )
    assert art.title_in_banner is True
    assert art.banner == svc.titled_banner_url(banner, "DM Requests")
    assert art.footer == "F"


@pytest.mark.parametrize(
    "art",
    [
        pytest.param(
            PanelArt(banner="B", footer=None, title_in_banner=False), id="switch-off"
        ),
        pytest.param(
            PanelArt(banner=None, footer=None, title_in_banner=True), id="no-banner"
        ),
        pytest.param(
            PanelArt(
                banner=f"{BASE}/{'e' * 32}.webp", footer=None, title_in_banner=True
            ),
            id="draw-failed",
        ),
    ],
)
def test_with_title_keeps_the_text_heading_when_it_cannot_draw(art_dir, art):
    out = svc.with_title(art, "DM Requests")
    assert out.title_in_banner is False
    assert out.banner == art.banner


def test_deleting_an_upload_deletes_its_drawn_titles_only(art_dir):
    banner = _banner(art_dir)
    a = _titled_path(art_dir, svc.titled_banner_url(banner, "DM Requests"))
    b = _titled_path(art_dir, svc.titled_banner_url(banner, "Perk Shop"))
    other = art_dir / svc.TITLED_DIR_NAME / f"{'a' * 32}-000000000000.webp"
    other.write_bytes(b"x")
    svc.delete_titled_variants(art_dir, f"{STEM}.webp")
    assert not a.exists() and not b.exists()
    assert other.exists()


# ── the lobby header: the game's name between two leaves ────────────────


def _strip(art_dir: Path) -> str:
    """A dark 300 × 40 lobby header (the 1200 × 160 strip at quarter size)."""
    Image.new("RGBA", (300, 40), (0, 0, 0, 255)).save(art_dir / f"{STEM}.webp")
    return f"{BASE}/{STEM}.webp"


def _lit(path: Path) -> list[tuple[int, int]]:
    with Image.open(path) as im:
        drawn = im.convert("RGBA")
    w, h = drawn.size
    return [
        (x, y)
        for y in range(h)
        for x in range(w)
        if sum(drawn.getpixel((x, y))[:3]) > 200
    ]


def test_banner_file_names_did_not_change():
    """Adding the lobby geometry must not redraw every titled banner."""
    import hashlib

    digest = hashlib.sha256(
        f"{svc.TITLE_STYLE_VERSION}|DM Requests".encode()
    ).hexdigest()
    assert svc.titled_name(STEM, "DM Requests") == f"{STEM}-{digest[:12]}.webp"
    assert svc.titled_name(STEM, "DM Requests", svc.LOBBY) != svc.titled_name(
        STEM, "DM Requests"
    )


def test_lobby_title_sits_centred_between_the_leaves(art_dir):
    url = svc.titled_banner_url(_strip(art_dir), "Clapback", svc.LOBBY)
    assert url is not None
    lit = _lit(_titled_path(art_dir, url))
    assert lit, "no title drawn"
    xs, ys = [x for x, _ in lit], [y for _, y in lit]
    assert abs((min(xs) + max(xs)) / 2 - 150) < 8, "centred across"
    assert abs((min(ys) + max(ys)) / 2 - 40 * 89 / 160) < 6, "on the capitals line"


def test_a_long_lobby_title_stays_between_the_leaves(art_dir):
    url = svc.titled_banner_url(
        _strip(art_dir), "A Very Long Game Name Indeed", svc.LOBBY
    )
    assert url is not None
    xs = [x for x, _ in _lit(_titled_path(art_dir, url))]
    half_gap = 300 * svc.LOBBY.max_width_of_width / 2
    assert min(xs) >= 150 - half_gap - 2 and max(xs) <= 150 + half_gap + 2


def test_with_lobby_title_swaps_in_the_drawn_header(art_dir):
    header = _strip(art_dir)
    art = svc.with_lobby_title(
        PanelArt(banner="B", footer="F", header=header), "Clapback"
    )
    assert art.title_in_header is True
    assert art.header == svc.titled_banner_url(header, "Clapback", svc.LOBBY)
    assert (art.banner, art.footer, art.title_in_banner) == ("B", "F", False)


@pytest.mark.parametrize(
    "art",
    [
        pytest.param(PanelArt(banner="B", footer="F"), id="no-header"),
        pytest.param(
            PanelArt(banner=None, footer=None, header=f"{BASE}/{'e' * 32}.webp"),
            id="draw-failed",
        ),
    ],
)
def test_with_lobby_title_leaves_the_heading_when_it_cannot_draw(art_dir, art):
    assert svc.with_lobby_title(art, "Clapback") == art


def test_delete_ignores_a_name_the_route_did_not_write(art_dir):
    svc.delete_titled_variants(art_dir, "../../*.webp")  # no error, nothing matched


@pytest.mark.parametrize("attr", ["TITLE_FONT", "LOBBY_TITLE_FONT"])
def test_title_font_is_committed_with_its_licence_row(attr):
    # The faces are a bought licence, not OFL, so each README row is the only
    # record of it (style guide § Media). The runner has no assets/: skip there.
    # The README is tracked; the faces themselves left git on 2026-09-29 and
    # are gitignored, so a worktree or the runner has the row and no file —
    # the row is what this pins, not the file's presence.
    font = getattr(svc, attr)
    readme = font.parent / "README.md"
    if not readme.is_file():
        pytest.skip("no assets/ in this checkout")
    row = next(
        line
        for line in readme.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"| `{font.name}` |")
    )
    assert "licence" in row.lower() and "panel_title_service" in row


@pytest.mark.parametrize(
    ("size", "geometry"),
    [((1200, 520), svc.BANNER), ((1200, 160), svc.LOBBY)],
    ids=["banner", "lobby"],
)
def test_title_is_coloured_like_a_petal_yellow_core_orange_rim(art_dir, size, geometry):
    # Ben, 2026-09-28: "match the poppies — the rim is more orange". The fill
    # must carry both petal tones; the old top-to-bottom gold never reached
    # rim orange. Only bright pixels count: the dark edge line is excluded.
    layer = svc._title_layer(size, "DM Requests", geometry)
    ratios = [
        g / r for r, g, _b, a in layer.get_flattened_data() if a == 255 and r > 180
    ]
    assert ratios
    assert min(ratios) < 0.6  # petal-rim orange
    assert max(ratios) > 0.75  # petal yellow


@pytest.mark.parametrize(
    ("geometry", "attr"),
    [
        (svc.BANNER, "TITLE_FONT"),
        (svc.LOBBY, "LOBBY_TITLE_FONT"),
        (svc.LOBBY._replace(cap_centre_of_height=0.5), "LOBBY_TITLE_FONT"),
    ],
    ids=["banner-black", "lobby-regular", "lobby-copy-regular"],
)
def test_each_title_draws_in_its_own_weight(tmp_path, monkeypatch, geometry, attr):
    # Ben, 2026-09-28: "use the regular variant for the small headers" — the
    # lobby strip takes Thught Regular, the hub banner keeps Thught Black.
    for name in ("TITLE_FONT", "LOBBY_TITLE_FONT"):
        face = tmp_path / f"{name}.ttf"
        face.write_bytes(b"")
        monkeypatch.setattr(svc, name, face)
    opened: list[str] = []

    monkeypatch.setattr(svc.ImageFont, "truetype", lambda path, px: opened.append(path))
    svc._font(40, geometry)
    assert opened == [str(tmp_path / f"{attr}.ttf")]


def _hardest_edge(layer: Image.Image) -> int:
    """The biggest brightness step between side-by-side pixels, over black."""
    flat = Image.new("RGBA", layer.size, (0, 0, 0, 255))
    flat.alpha_composite(layer)
    grey = flat.convert("L")
    return ImageChops.difference(grey, ImageChops.offset(grey, 1, 0)).getextrema()[1]


@pytest.mark.parametrize(
    ("size", "geometry"),
    [((1200, 520), svc.BANNER), ((1200, 160), svc.LOBBY)],
    ids=["banner", "lobby"],
)
def test_title_edges_are_softened(art_dir, monkeypatch, size, geometry):
    # Ben, 2026-09-28: "make the edges of the text softer" — a light blur on
    # the whole finished layer. Reverting it brings the hard type edge back.
    soft = _hardest_edge(svc._title_layer(size, "DM Requests", geometry))
    monkeypatch.setattr(svc, "_SOFTEN_PX", 0.0)
    crisp = _hardest_edge(svc._title_layer(size, "DM Requests", geometry))
    assert soft < crisp * 0.85


def test_lobby_capitals_have_even_space_above_and_below(art_dir):
    # Ben, 2026-09-28: "vertically center the small header". The strip is
    # 1200 × 160 since 2026-09-29, the footer's height, with the type grown to
    # match: the capitals get the same black from the frame's inside (y = 18)
    # as down to the strip's faded, open bottom edge.
    height, frame_inside = 160, 18
    layer = svc._title_layer((1200, height), "HHH", svc.LOBBY)
    w = layer.width
    rows = [
        i // w
        for i, (r, g, _b, _a) in enumerate(layer.get_flattened_data())
        if r > 150 and g > 60
    ]
    assert rows, "no title drawn"
    above, below = min(rows) - frame_inside, height - 1 - max(rows)
    assert abs(above - below) <= 2
    assert 24 <= above <= 36  # the 108 strip's 18, grown with the strip


GREEN = (0, 255, 0, 255)


@pytest.fixture
def sprig(art_dir, monkeypatch) -> Path:
    """A solid green 40 × 30 stand-in for the leaf sprig (drawn at 1.04: 41 × 31)."""
    path = art_dir / "sprig.png"
    Image.new("RGBA", (40, 30), GREEN).save(path)
    monkeypatch.setattr(svc, "LOBBY_SPRIG", path)
    return path


def _lobby_strip(art_dir: Path, *, fade_rows: int = 0) -> str:
    """A black 1200 × 160 lobby strip; its last ``fade_rows`` are transparent."""
    im = Image.new("RGBA", (1200, 160), (0, 0, 0, 255))
    if fade_rows:
        im.paste((0, 0, 0, 0), (0, 160 - fade_rows, 1200, 160))
    im.save(art_dir / f"{STEM}.webp", lossless=True)
    return f"{BASE}/{STEM}.webp"


def _columns(path: Path, keep) -> list[int]:
    with Image.open(path) as im:
        drawn = im.convert("RGBA")
    w = drawn.width
    return sorted(
        {i % w for i, px in enumerate(drawn.get_flattened_data()) if keep(px)}
    )


def _leaf(px) -> bool:
    return px[1] > 200 and px[0] < 60 and px[3] > 200


def _word(px) -> bool:
    return px[0] > 150 and px[3] > 200


@pytest.mark.parametrize("title", ["Chicken", "Marry/Fornicate/Kiss"])
def test_lobby_leaves_sit_a_fixed_distance_from_the_words(art_dir, sprig, title):
    # Ben, 2026-09-29: "I want the leaves a fixed distance from the text" —
    # a short name and a long one get the same gap, so the leaves move.
    url = svc.titled_banner_url(_lobby_strip(art_dir), title, svc.LOBBY)
    assert url is not None
    path = _titled_path(art_dir, url)
    word, leaf = _columns(path, _word), _columns(path, _leaf)
    assert word and leaf, "title and leaves both drawn"
    left = [x for x in leaf if x < word[0]]
    right = [x for x in leaf if x > word[-1]]
    # One 41 px sprig each side (40 at 1.04); the title's dark glow may shade
    # a column or two of its inner edge.
    assert 35 <= len(left) <= 41 and 35 <= len(right) <= 41
    # The glow and blur spread the lit word a few px past its glyphs.
    assert abs((word[0] - left[-1]) - svc._SPRIG_GAP) <= 6
    assert abs((right[0] - word[-1]) - svc._SPRIG_GAP) <= 6


def test_short_and_long_titles_move_the_leaves(art_dir, sprig):
    spans = []
    for title in ("Chicken", "Marry/Fornicate/Kiss"):
        url = svc.titled_banner_url(_lobby_strip(art_dir), title, svc.LOBBY)
        leaf = _columns(_titled_path(art_dir, url), _leaf)
        spans.append(leaf[-1] - leaf[0])
    assert spans[1] > spans[0] + 150


def test_lobby_leaves_fade_with_the_strips_open_bottom(art_dir, sprig):
    # The strip fades into the card; the leaves drawn over it must too, or
    # they hang below the art. Here its last 50 rows are fully transparent.
    url = svc.titled_banner_url(
        _lobby_strip(art_dir, fade_rows=50), "Chicken", svc.LOBBY
    )
    with Image.open(_titled_path(art_dir, url)) as im:
        drawn = im.convert("RGBA")
    rows = {
        i // drawn.width for i, px in enumerate(drawn.get_flattened_data()) if _leaf(px)
    }
    assert rows and max(rows) < 160 - 50


def test_only_the_lobby_gets_leaves(art_dir, sprig):
    url = svc.titled_banner_url(_lobby_strip(art_dir), "Chicken", svc.BANNER)
    assert url is not None
    assert not _columns(_titled_path(art_dir, url), _leaf)


def test_lobby_without_a_sprig_on_disk_still_draws_its_title(art_dir):
    # The remote runner has no assets/: the title draws, leafless.
    url = svc.titled_banner_url(_lobby_strip(art_dir), "Chicken", svc.LOBBY)
    assert url is not None
    path = _titled_path(art_dir, url)
    assert _columns(path, _word) and not _columns(path, _leaf)


# ── petal_layer: the one place the petal look is drawn ──────────────────────


def _block_mask(size=(240, 120), box=(40, 30, 200, 90)):
    from PIL import Image, ImageDraw

    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rectangle(box, fill=255)
    return mask


def test_petal_layer_is_yellow_inside_and_orange_at_the_rim():
    layer = svc.petal_layer(_block_mask(), rim_px=4.0, scale=1.0)
    assert layer.mode == "RGBA" and layer.size == (240, 120)
    r, g, b, a = layer.getpixel((120, 60))  # deep inside the block
    rr, rg, _rb, _ra = layer.getpixel((120, 32))  # just inside its top edge
    assert a == 255
    assert (r, g, b) == tuple(round(c * svc._SUBDUE) for c in svc._PETAL)
    assert rg < g and rr <= r  # the rim shades toward the petal-rim orange


def test_petal_layer_glows_past_the_letters_and_fades_out():
    layer = svc.petal_layer(_block_mask(), rim_px=4.0, scale=1.0)
    assert layer.getpixel((120, 22))[3] > 0  # the glow, 8 px above the block
    assert layer.getpixel((0, 0))[3] == 0


def test_petal_scale_is_one_at_the_size_the_look_was_tuned_on():
    # Not the banner's own type size: that grew to 121 (Ben, 2026-09-29), and
    # tying the two would have thinned the glow on every quote card heading.
    assert svc.petal_scale(101) == pytest.approx(1.0)
    assert svc.petal_scale(50.5) == pytest.approx(0.5)
    assert (svc.HEAVY_RIM_PX, svc.THIN_RIM_PX) == (4.0, 2.5)


@pytest.mark.parametrize("scale", [0.14, 0.5, 1.0])
def test_petal_reach_covers_everything_the_layer_draws(scale):
    # A caller that crops to the glyphs plus the reach must lose nothing.
    reach = svc.petal_reach(scale)
    side = 2 * (reach + 40) + 20
    mask = _block_mask((side, side), (reach + 40, reach + 40, reach + 60, reach + 60))
    box = svc.petal_layer(mask, rim_px=4.0, scale=scale).getbbox()
    assert box is not None
    assert box[0] >= 40 and box[1] >= 40
    assert box[2] <= side - 40 and box[3] <= side - 40


def test_petal_layer_of_an_empty_mask_is_transparent():
    from PIL import Image

    layer = svc.petal_layer(Image.new("L", (64, 32)), rim_px=4.0, scale=1.0)
    assert layer.size == (64, 32) and layer.getbbox() is None


@pytest.mark.parametrize("geometry", [svc.BANNER, svc.LOBBY])
def test_title_layer_is_the_petal_layer_of_its_own_mask(geometry):
    # The banner must keep drawing exactly what it drew before the petal look
    # was shared with the quote cards (no TITLE_STYLE_VERSION bump needed).
    size = (1200, 520)
    mask = svc._title_mask(size, "Casino", geometry)
    assert (
        svc._title_layer(size, "Casino", geometry).tobytes()
        == svc.petal_layer(mask, rim_px=geometry.rim_px, scale=1.0).tobytes()
    )


# ── sizes, the fit and the floor ────────────────────────────────────────────


def test_banner_and_lobby_sizes_are_the_ones_ben_picked():
    # 2026-09-29, from samples: banner type 121 (was 101) allowed 90% of the
    # width (was 84%); the lobby strip at the footer's 160 with its type,
    # leaves and gap grown by the same 160/108.
    assert svc.BANNER.size_of_width * 1200 == pytest.approx(121)
    assert svc.BANNER.max_width_of_width == pytest.approx(0.90)
    assert svc.LOBBY.size_of_width * 1200 == pytest.approx(116)
    assert svc.LOBBY.cap_centre_of_height == pytest.approx(89 / 160)
    assert svc._SPRIG_GAP == 47
    assert svc._SPRIG_SCALE == pytest.approx(0.7 * 160 / 108)


def _drawn(title: str, geometry: svc.TitleGeometry, width: int = 1200):
    font, text = svc._fit(width, title, geometry)
    return font.size, text, font.getlength(text)


@pytest.mark.parametrize("geometry", [svc.BANNER, svc.LOBBY], ids=["banner", "lobby"])
def test_a_title_that_fits_is_drawn_at_the_geometrys_size(art_dir, geometry):
    size, text, _ = _drawn("Shop", geometry)
    assert text == "Shop"
    assert size == round(1200 * geometry.size_of_width)


@pytest.mark.parametrize("geometry", [svc.BANNER, svc.LOBBY], ids=["banner", "lobby"])
def test_a_wide_title_takes_the_largest_size_that_fits(art_dir, geometry):
    limit = 1200 * geometry.max_width_of_width
    base = round(1200 * geometry.size_of_width)
    title = "Spin the Compliments"
    size, text, wide = _drawn(title, geometry)
    assert text == title, "shrunk, not cut"
    assert size < base and wide <= limit
    # To the pixel: the next size up would not have fitted.
    assert svc._font(size + 1, geometry).getlength(title) > limit


def test_a_wider_title_is_never_drawn_larger_than_a_narrower_one(art_dir):
    # The 8% steps drew "Story Builder" (603 px) at 116 and the narrower
    # "Bounty Board" (572 px) at 106. An exact fit is ordered by width.
    titles = [f"Meadow {'m' * n}" for n in range(1, 9)]
    sizes = [_drawn(t, svc.LOBBY)[0] for t in titles]
    assert sizes == sorted(sizes, reverse=True)
    assert len(set(sizes)) > 3


@pytest.mark.parametrize("geometry", [svc.BANNER, svc.LOBBY], ids=["banner", "lobby"])
def test_a_title_too_long_for_the_floor_is_cut_with_an_ellipsis(art_dir, geometry):
    # Ben, 2026-09-29: "put a text limit in and truncate". A casino name may
    # be 60 characters; it used to shrink until nobody could read it.
    title = "The " + "Extraordinarily Long Winded " * 3 + "Casino"
    size, text, wide = _drawn(title, geometry)
    assert size == round(1200 * geometry.min_size_of_width), "held at the floor"
    assert text.endswith("…") and not text.endswith(" …")
    assert title.startswith(text[:-1])
    assert wide <= 1200 * geometry.max_width_of_width
    # Cut no shorter than it has to be: one more character would not fit.
    longer = title[: len(text)].rstrip() + "…"
    assert (
        svc._font(size, geometry).getlength(longer) > 1200 * geometry.max_width_of_width
    )


def test_a_geometry_without_a_floor_shrinks_and_never_cuts_early(art_dir):
    # TitleGeometry's floor defaults to none, so a caller's own geometry
    # keeps the old behaviour: shrink as far as it takes.
    plain = svc.TitleGeometry("banner", 100 / 1200, 0.5, 0.5, 4.0)
    size, text, wide = _drawn("A Fairly Long Title For Half A Banner Width", plain)
    assert text == "A Fairly Long Title For Half A Banner Width"
    assert 8 < size < 100 and wide <= 600


def test_the_cut_title_is_what_gets_drawn(art_dir):
    url = svc.titled_banner_url(
        _banner(art_dir), "The " + "Extraordinarily Long Winded " * 3 + "Casino"
    )
    assert url is not None
    xs = [x for x, _ in _lit(_titled_path(art_dir, url))]
    half = 300 * svc.BANNER.max_width_of_width / 2
    assert xs and min(xs) >= 150 - half - 4 and max(xs) <= 150 + half + 4


# ── the intake card's in-memory strip (member names never hit the disk) ─


@pytest.mark.parametrize(
    "raw, drawn",
    [
        pytest.param("🌸 Rosie ✨ the  Brave", "Rosie the Brave", id="emoji-dropped"),
        pytest.param("🌸🌸", "", id="nothing-drawable"),
        pytest.param("x" * 40, "x" * 31 + "…", id="long-cut"),
    ],
)
def test_drawable_title(art_dir, raw, drawn):
    svc._drawable.cache_clear()
    assert svc.drawable_title(raw) == drawn


def test_titled_strip_bytes_draws_in_memory_only(art_dir):
    svc.titled_strip_bytes.cache_clear()
    url = _banner(art_dir, frames=3)
    data = svc.titled_strip_bytes(url, "Rosie", svc.LOBBY)
    assert data is not None and data[:4] == b"RIFF"
    with Image.open(__import__("io").BytesIO(data)) as im:
        assert im.n_frames == 3  # the twinkle keeps playing
    # No file per member: the art directory is public and outlives an erasure.
    assert not (art_dir / svc.TITLED_DIR_NAME).exists()


def test_titled_strip_bytes_foreign_url_is_none(art_dir):
    svc.titled_strip_bytes.cache_clear()
    assert svc.titled_strip_bytes("https://elsewhere.example/x.webp", "Rosie") is None
