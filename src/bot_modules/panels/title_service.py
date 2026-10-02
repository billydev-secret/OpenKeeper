"""A hub panel's title drawn into the guild's banner (Billy, 2026-09-26).

The hub banner is one upload per guild, but each hub has its own title, so
the title can't be baked into the upload. With the Branding page's **title in
the banner** switch on, the bot writes each hub's title across the foot of the
banner itself — Thught Black coloured like a poppy petal, with a dark glow,
centred in the band the art leaves clear under its picture — and the hub drops
its text heading (``core.layout.hub_banner``'s alt text carries the title for
a screen reader instead).

A title too wide for its geometry is drawn at the largest size that fits; one
that would have to drop below the geometry's floor is drawn at the floor and
cut with an ellipsis (:func:`_fit`). The alt text always carries it whole.

Each (banner, title) pair is drawn once and kept beside the upload under
``titled/`` in the public art directory; the file name hashes the title and
:data:`TITLE_STYLE_VERSION`, so a new title or a restyle draws afresh and an
unchanged panel repost is a stat. Every frame of an animated GIF/WebP is drawn,
so the twinkle keeps playing. Anything that goes wrong — a URL this dashboard
didn't write, the file gone, an image Pillow can't read — returns ``None``,
and the hub falls back to the plain banner and its text heading: a titled
banner is decoration, never a reason for the panel to fail to post.

The game lobbies' small header strip gets the same treatment, always on
(Billy, 2026-09-27: "Can we put the title between two leaves?"):
:func:`with_lobby_title` writes the game's name across it in Thught Regular
— :data:`LOBBY` sets the geometry — and draws a leaf sprig either side a
fixed distance from the words, so a short name and a long one are framed
alike (Ben, 2026-09-29). The upload carries no leaves; the sprig is
:data:`LOBBY_SPRIG`. A strip is a different upload from the banner, so the
two never share a cached file.

The intake card wears the lobby strip with the **member's name** drawn in
(Ben, 2026-09-28). That one is never cached on disk: the art directory is
served publicly and outlives an erasure, so a file per member would be a
store of names that no purge reaches. :func:`titled_strip_bytes` draws into
memory and the card attaches the result; a small in-process cache spares a
redraw on every tick.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import IO, NamedTuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageSequence

from bot_modules.core.branding_service import (
    PANEL_ART_DIR,
    PANEL_ART_URL_PATH,
    PanelArt,
)

log = logging.getLogger("dungeonkeeper.panel_title")

#: Bump when the drawing below changes, so every cached title redraws.
# 2: Thught, petal colours; 3: soft edges, lobby leaves + 108 strip;
# 4: banner type 121, 160 strip, exact fit with a floor;
# 5: art shorter than the geometry's own draws everything smaller (bug #274).
TITLE_STYLE_VERSION = 5

#: Subdirectory of the art directory holding the drawn variants.
TITLED_DIR_NAME = "titled"

_FONTS = Path(__file__).resolve().parents[3] / "assets" / "fonts"
#: The display face: Thught Black (Syafbe Studio), a licensed commercial face
#: bought on MyFonts — Ben's pick, 2026-09-28. It replaced Inter Display Bold.
TITLE_FONT = _FONTS / "ThughtBlack.ttf"
#: The lobby header's smaller title takes the family's Regular weight (Ben,
#: 2026-09-28: "use the regular variant for the small headers").
LOBBY_TITLE_FONT = _FONTS / "ThughtRegular.ttf"
#: The lobby title's leaf sprig, stem on the left (the right-hand one; the
#: left is its mirror), cut from the poppy art by ``scripts/make_panel_art.py
#: sprig`` at the art's 1200-wide scale.
LOBBY_SPRIG = (
    Path(__file__).resolve().parents[3] / "assets" / "panel_art" / "lobby_sprig.png"
)
#: How much the lobby strip has grown since its type, leaves and gap were
#: set on the 108 strip: Ben picked 160 with everything on it grown to match
#: (2026-09-29, from samples at 140 / 154 / 160 / 170).
_LOBBY_GROWTH = 160 / 108
#: Drawn at this share of the art's scale, the words-to-leaves gap in px at
#: 1200 wide — the same for every title (Ben, 2026-09-29: "I want the leaves
#: a fixed distance from the text").
_SPRIG_SCALE = 0.7 * _LOBBY_GROWTH
_SPRIG_GAP = round(32 * _LOBBY_GROWTH)


class TitleGeometry(NamedTuple):
    """Where a title sits, as fractions of the image so any upload size
    draws the same. ``key`` goes into the cached file's name."""

    key: str
    size_of_width: float  # type size
    cap_centre_of_height: float  # where the cap height is centred
    max_width_of_width: float  # a longer title shrinks to fit this
    rim_px: float  # the petal-orange rim, in px at 1200 wide
    min_size_of_width: float = 0.0  # below this type size a title is cut instead
    # The height of the art the numbers above were set on. Shorter art draws
    # everything smaller by the same share (:func:`_height_scale`); 0 = don't.
    height_of_width: float = 0.0


#: The orange rim at the petal look's own scale: a heavy face's, and the one
#: for thin strokes, where 4 px would leave no yellow inside.
HEAVY_RIM_PX, THIN_RIM_PX = 4.0, 2.5

#: The hub banner, on the 1200 × 520 poppy art: 121 px type, cap height
#: centred at y = 410. The approved mockup had 92, then 101 ("10% larger");
#: Ben picked 121 from samples at 101 to 121 (2026-09-29). A long title may
#: run to 90% of the width (84% until the frame moved out to the picture's
#: edge), which draws "Coins — The Bank" at 118 and the casino's title at 77;
#: one that would drop below 72 is cut.
#: No ``height_of_width``: a banner is any wide picture with a clear bottom
#: third ("about 3:1" is the dashboard's own advice), so a 1200 × 400 upload
#: is an ordinary banner whose 121 px title fits, not short art to shrink on.
BANNER = TitleGeometry("banner", 121 / 1200, 410 / 520, 0.90, HEAVY_RIM_PX, 72 / 1200)

#: The lobby header: the 108 strip's 78 px type and 2.5 px rim, grown with
#: the strip to 1200 × 160 (:data:`_LOBBY_GROWTH`) — 116 px type, the height
#: the footer has too. The capitals centre at y = 89, midway between the
#: frame's inside (y = 18) and the strip's faded bottom edge
#: (``scripts/make_panel_art.py``'s LOBBY_H / LOBBY_CAP_CENTRE). A title is
#: held to 616 px: what the two sprigs, their gaps and a 40 px margin leave
#: of the width. The floor is 58, under every game's name on the register.
LOBBY = TitleGeometry(
    "lobby",
    round(78 * _LOBBY_GROWTH) / 1200,
    89 / 160,
    616 / 1200,
    THIN_RIM_PX * _LOBBY_GROWTH,
    58 / 1200,
    160 / 1200,
)

#: The type size the petal look's blurs were tuned on (:func:`petal_scale`):
#: the banner's first Thught size. The banner's type has grown since; the
#: look, and every quote card heading drawn with it, stayed as it was.
_PETAL_TYPE_PX = 101

# Each letter is coloured like a poppy petal (Ben, 2026-09-28: "match the
# poppies — the rim is more orange"): the petal yellow inside, shading to the
# petal-rim orange over its outer ``rim_px``. Both sampled from the banner's
# petals (85th and 15th percentile of green/red), then _SUBDUE'd.
_PETAL = (250, 199, 39)
_PETAL_RIM = (230, 126, 10)
_SUBDUE = 0.95
_EDGE = (70, 35, 0)
# The finished title is blurred this much (Ben, 2026-09-28: "make the edges
# of the text softer"), taking the hard type edge off to sit with the bokeh.
_SOFTEN_PX = 0.7  # at 1200 px wide

_UPLOAD_NAME = re.compile(r"^([0-9a-f]{32})\.(?:png|jpg|gif|webp)$")


def _font(
    px: int, geometry: TitleGeometry
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # By key, not identity: a _replace'd copy of LOBBY is still the lobby.
    path = LOBBY_TITLE_FONT if geometry.key == LOBBY.key else TITLE_FONT
    if path.is_file():
        return ImageFont.truetype(str(path), px)
    # Only a checkout without assets/ (the remote test runner) gets here.
    return ImageFont.load_default(size=px)


def _source_for(banner_url: str) -> tuple[Path, str] | None:
    """The upload behind ``banner_url`` and its stem, or ``None`` if the URL
    isn't one the dashboard's upload route wrote."""
    if PANEL_ART_URL_PATH not in banner_url:
        return None
    m = _UPLOAD_NAME.match(banner_url.rsplit("/", 1)[-1])
    if m is None:
        return None
    return PANEL_ART_DIR / m.group(0), m.group(1)


def titled_name(stem: str, title: str, geometry: TitleGeometry = BANNER) -> str:
    # The banner's names predate the lobby geometry; keep them, so adding it
    # redrew nothing.
    style = "" if geometry is BANNER else f"{geometry.key}|"
    digest = hashlib.sha256(
        f"{TITLE_STYLE_VERSION}|{style}{title}".encode()
    ).hexdigest()[:12]
    return f"{stem}-{digest}.webp"


_ELLIPSIS = "…"


def _height_scale(size: tuple[int, int], geometry: TitleGeometry) -> float:
    """How much smaller than the geometry says to draw on art ``size``: the
    share of the geometry's own height the art has, never above 1.

    Every size in a geometry is a fraction of the *width*, which is only
    right on art of the proportions it was set on. The lobby's type was grown
    for the 160 strip while the 108 upload was still live, and its capitals
    ran into the frame's top line (bug #274: "this R hits the top line").
    Shorter art now gets the type, floor, rim, leaves and gap all smaller
    together — on the 108 strip, exactly the 78 px title it was designed
    with. Taller art is left alone: the width still bounds the title there.
    """
    w, h = size
    if geometry.height_of_width <= 0 or w <= 0:
        return 1.0
    return min(1.0, h / (w * geometry.height_of_width))


def _fit(
    width: int, title: str, geometry: TitleGeometry, shrink: float = 1.0
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    """The face and the text ``title`` is drawn with on art ``width`` wide.
    ``shrink`` is :func:`_height_scale`'s, for art shorter than the geometry's.

    The geometry's own size when the title fits its ``max_width_of_width``;
    otherwise the largest size that does — to the pixel, so a wider title is
    never drawn larger than a narrower one (the 8% steps this replaced drew
    "Story Builder" above "Bounty Board"). A title that would drop below
    ``min_size_of_width`` is drawn at that floor and cut with an ellipsis
    (Ben, 2026-09-29: "put a text limit in and truncate"): a 60-character
    casino name used to shrink to a line nobody could read.
    """
    limit = width * geometry.max_width_of_width
    px = max(8, round(width * geometry.size_of_width * shrink))
    floor = min(px, max(8, round(width * geometry.min_size_of_width * shrink)))
    font = _font(px, geometry)
    wide = font.getlength(title)
    if wide > limit:
        px = max(floor, int(px * limit / wide))
        font = _font(px, geometry)
        while px > floor and font.getlength(title) > limit:  # hinting: a px over
            px -= 1
            font = _font(px, geometry)
    if font.getlength(title) <= limit:
        return font, title
    kept = title
    while len(kept) > 1 and font.getlength(kept.rstrip() + _ELLIPSIS) > limit:
        kept = kept[:-1]
    return font, kept.rstrip() + _ELLIPSIS


def _title_mask(
    size: tuple[int, int], title: str, geometry: TitleGeometry
) -> Image.Image:
    """The title's glyphs, white on black, sized and placed by ``geometry``."""
    w, h = size
    font, title = _fit(w, title, geometry, _height_scale(size, geometry))
    cap_top = font.getbbox("H", anchor="ls")[1]  # negative: above the baseline
    baseline = h * geometry.cap_centre_of_height - cap_top / 2

    mask = Image.new("L", size)
    ImageDraw.Draw(mask).text(
        (w / 2, baseline), title, font=font, fill=255, anchor="ms"
    )
    return mask


def _title_layer(
    size: tuple[int, int],
    title: str,
    geometry: TitleGeometry = BANNER,
    mask: Image.Image | None = None,
) -> Image.Image:
    """The title — glow, halo, edge and fill — on a transparent canvas.
    ``mask`` is :func:`_title_mask`'s, when the caller already drew it."""
    if mask is None:
        mask = _title_mask(size, title, geometry)
    return petal_layer(
        mask,
        rim_px=geometry.rim_px,
        scale=size[0] / 1200 * _height_scale(size, geometry),
    )


def petal_scale(type_px: float) -> float:
    """The ``scale`` of lettering ``type_px`` tall: 1.0 is the size the look
    was tuned on (:data:`_PETAL_TYPE_PX`), not the banner's current type."""
    return type_px / _PETAL_TYPE_PX


def petal_reach(scale: float) -> int:
    """How far past the glyphs the look draws, in px: the halo, its widest
    blur, out to three sigma, plus the 1 px floor every blur has."""
    return int(48 * scale) + 8


def petal_layer(mask: Image.Image, *, rim_px: float, scale: float) -> Image.Image:
    """Lettering coloured like the panel titles, from its glyph ``mask``.

    The one place the petal look is drawn, so a quote card's heading and a
    hub banner's title cannot drift apart. ``scale`` is the lettering's size
    against the one the look was tuned on (:func:`petal_scale`; the titles
    themselves draw at 1.0 on 1200-wide art); ``rim_px`` is the orange rim at
    that scale — :data:`HEAVY_RIM_PX` for a heavy face, :data:`THIN_RIM_PX`
    for thin strokes.
    """
    size = mask.size
    if mask.getbbox() is None:
        return Image.new("RGBA", size)

    def blur(radius: float) -> ImageFilter.GaussianBlur:
        return ImageFilter.GaussianBlur(max(1.0, radius * scale))

    def tone(rgb: tuple[int, int, int]) -> Image.Image:
        return Image.new("RGBA", size, (*(round(c * _SUBDUE) for c in rgb), 255))

    # A blurred mask reads ~128 on the glyph's edge and 255 deep inside, so
    # it is a depth map: rim orange at the edge, petal yellow in the middle.
    def edge_to_depth(v: int) -> int:
        return min(255, max(0, (v - 128) * 255 // 122))

    depth = mask.filter(blur(rim_px)).point(edge_to_depth)
    grad = Image.composite(tone(_PETAL), tone(_PETAL_RIM), depth)

    grow = max(3, round(9 * scale) | 1)  # MaxFilter needs an odd size
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    glow.putalpha(
        mask.filter(ImageFilter.MaxFilter(grow))
        .filter(blur(10))
        .point(lambda v: v * 0.9)
    )
    halo = Image.new("RGBA", size, (255, 190, 60, 0))
    halo.putalpha(mask.filter(blur(14)).point(lambda v: int(v * 0.35)))
    edge = Image.new("RGBA", size, (*_EDGE, 0))
    edge.putalpha(mask.filter(ImageFilter.MaxFilter(3)))
    fill = grad.copy()
    fill.putalpha(mask)

    layer = Image.new("RGBA", size)
    for part in (glow, halo, edge, fill):
        layer.alpha_composite(part)
    if _SOFTEN_PX > 0:  # not blur(): its 1 px floor is too soft for this
        layer = layer.filter(ImageFilter.GaussianBlur(_SOFTEN_PX * scale))
    return layer


def _leaves_layer(
    strip_alpha: Image.Image, mask: Image.Image, geometry: TitleGeometry
) -> Image.Image | None:
    """A sprig either side of the title, :data:`_SPRIG_GAP` from its glyphs and
    centred on its capitals, faded wherever the strip itself is (its open
    bottom). ``None`` without a sprig on disk (the remote test runner)."""
    if not LOBBY_SPRIG.is_file():
        return None
    size = strip_alpha.size
    box = mask.getbbox()
    if box is None:
        return None
    w, h = size
    scale = w / 1200 * _height_scale(size, geometry)
    with Image.open(LOBBY_SPRIG) as im:
        right = im.convert("RGBA").resize(
            (
                max(1, round(im.width * _SPRIG_SCALE * scale)),
                max(1, round(im.height * _SPRIG_SCALE * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    gap = round(_SPRIG_GAP * scale)
    y = round(h * geometry.cap_centre_of_height - right.height / 2)
    layer = Image.new("RGBA", size)
    # paste, not alpha_composite: the two never overlap, and paste clips.
    layer.paste(right, (box[2] + gap, y))
    layer.paste(
        right.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
        (box[0] - gap - right.width, y),
    )
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), strip_alpha))
    return layer


def render_titled(
    source: Path, dest: Path, title: str, geometry: TitleGeometry = BANNER
) -> None:
    """Draw ``title`` into every frame of ``source`` and write a WebP at ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    _draw_titled(source, tmp, title, geometry)
    os.replace(tmp, dest)  # a concurrent reader never sees half a file


def _draw_titled(
    source: Path, out: Path | IO[bytes], title: str, geometry: TitleGeometry
) -> None:
    with Image.open(source) as im:
        frames: list[Image.Image] = []
        durations: list[int] = []
        layer: Image.Image | None = None
        for frame in ImageSequence.Iterator(im):
            rgba = frame.convert("RGBA")
            if layer is None:
                mask = _title_mask(rgba.size, title, geometry)
                layer = _title_layer(rgba.size, title, geometry, mask)
                if geometry.key == LOBBY.key:  # by key, as in _font
                    leaves = _leaves_layer(rgba.getchannel("A"), mask, geometry)
                    if leaves is not None:
                        leaves.alpha_composite(layer)  # the title over its leaves
                        layer = leaves
            rgba.alpha_composite(layer)
            frames.append(rgba)
            durations.append(int(frame.info.get("duration", 80) or 80))
        loop = im.info.get("loop", 0)
    if len(frames) > 1:
        frames[0].save(
            out,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            quality=92,
            method=4,
        )
    else:
        frames[0].save(out, format="WEBP", quality=92, method=4)


#: A code point no face maps, so its bitmap is the face's missing-glyph box.
_UNMAPPED = "\U0010fffd"


def _glyph(ch: str) -> bytes:
    im = Image.new("L", (96, 96))
    # The lobby face: the member's name is drawn on the lobby strip.
    ImageDraw.Draw(im).text((8, 8), ch, font=_font(48, LOBBY), fill=255)
    return im.tobytes()


@lru_cache(maxsize=4096)
def _drawable(ch: str) -> bool:
    return ch.isspace() or _glyph(ch) != _glyph(_UNMAPPED)


def drawable_title(title: str, max_len: int = 32) -> str:
    """``title`` without the characters the title face can't draw, spaces
    collapsed, cut to ``max_len`` with an ellipsis.

    A display name is the member's own text: emoji and scripts outside the
    lobby face would otherwise draw as tofu boxes. Empty when nothing drawable is left —
    the caller then keeps a text heading, which Discord renders in full.
    """
    kept = " ".join("".join(ch for ch in title if _drawable(ch)).split())
    if len(kept) > max_len:
        kept = kept[: max_len - 1].rstrip() + "…"
    return kept


@lru_cache(maxsize=32)
def titled_strip_bytes(
    header_url: str, title: str, geometry: TitleGeometry = LOBBY
) -> bytes | None:
    """``header_url``'s upload with ``title`` drawn in, as WebP bytes, or
    ``None`` when it can't be drawn. Nothing is written to disk (module
    docstring). Blocking — call it off the event loop."""
    found = _source_for(header_url)
    if found is None:
        return None
    buf = io.BytesIO()
    try:
        _draw_titled(found[0], buf, title, geometry)
    except Exception:
        log.exception("Could not draw %r into %s", title, found[0])
        return None
    return buf.getvalue()


def titled_banner_url(
    banner_url: str, title: str, geometry: TitleGeometry = BANNER
) -> str | None:
    """The public URL of ``banner_url`` with ``title`` drawn in, drawing it on
    first use; ``None`` when it can't be drawn (see the module docstring)."""
    found = _source_for(banner_url)
    if found is None:
        return None
    source, stem = found
    name = titled_name(stem, title, geometry)
    dest = source.parent / TITLED_DIR_NAME / name
    if not dest.is_file():
        try:
            render_titled(source, dest, title, geometry)
        except Exception:
            # Broad on purpose: Pillow raises well outside OSError (a
            # decompression bomb is its own Exception subclass), and a banner
            # that can't be drawn must fall back, never break the panel.
            log.exception("Could not draw %r into %s", title, source)
            return None
    return f"{banner_url.rsplit('/', 1)[0]}/{TITLED_DIR_NAME}/{name}"


def with_title(art: PanelArt, title: str) -> PanelArt:
    """``art`` with its banner swapped for the titled one when the guild asked
    for it and the drawing worked; otherwise ``art`` with the switch cleared,
    so the hub keeps its text heading. Blocking — call it off the event loop."""
    if not (art.banner and art.title_in_banner):
        return art._replace(title_in_banner=False)
    url = titled_banner_url(art.banner, title)
    if url is None:
        return art._replace(title_in_banner=False)
    return art._replace(banner=url)


def with_lobby_title(art: PanelArt, title: str) -> PanelArt:
    """``art`` with its lobby header swapped for one carrying ``title``
    between the leaves, and ``title_in_header`` set so the lobby drops its
    text heading. No switch: the header is authored for it. Without a header,
    or when the drawing fails, ``art`` comes back as it was and the lobby
    keeps its heading. Blocking — call it off the event loop."""
    if not art.header:
        return art
    url = titled_banner_url(art.header, title, LOBBY)
    if url is None:
        return art
    return art._replace(header=url, title_in_header=True)


def delete_titled_variants(art_dir: Path, upload_name: str) -> None:
    """Remove every drawn variant of an upload the dashboard is deleting."""
    m = _UPLOAD_NAME.match(upload_name)
    if m is None:
        return
    for path in (art_dir / TITLED_DIR_NAME).glob(f"{m.group(1)}-*.webp"):
        path.unlink(missing_ok=True)
