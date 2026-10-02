"""Generate the Branding → Panel Art files: hub banner and footer, lobby header, card strips.

Five animated WebPs, each a black field inside a gold frame with gold bokeh
dots twinkling over it, the frame open and faded on the side that meets the
card's text. The card header and footer, the thinnest, are only frame and
dots: nothing is ever drawn onto them. The bot draws each panel's title into the banner and
the lobby header at runtime (``services/panel_title_service``), so none of
these files carries text — the banner leaves its bottom third clear, and the
lobby header carries no leaves either: the bot draws the ``sprig`` sprite
either side of each title, a fixed distance from the words.

    python scripts/make_panel_art.py footer
    python scripts/make_panel_art.py lobby
    python scripts/make_panel_art.py card [--height 80]
    python scripts/make_panel_art.py sprig --out assets/panel_art
    python scripts/make_panel_art.py banner path/to/current_banner.webp
    python scripts/make_panel_art.py edge path/to/finished_banner.webp

``footer`` and ``lobby`` are drawn from scratch; their leaf sprigs are cut
from ``assets/panel_art/poppies_1200x400.webp`` (Billy's poppy art). The
banner *is* Billy's art, and its source isn't on disk, so ``banner``
re-twinkles an existing banner instead: only the dots move between frames,
so the per-pixel median over the loop is the still art and the per-pixel max
is every dot at full brightness. Each dot is lifted off, softened and put
back on its own twinkle. Feed it the crisp original: softening a banner
that's already soft blurs it again. ``edge`` is for a banner that is already
finished: it only moves the frame out to the picture's edge (below) and
leaves the dots alone.

**The frame's sides are the picture's sides** (``SIDE_INSET``; Ben,
2026-09-29). Discord draws a panel's text from the picture's left edge and
ends its buttons at the right one, so a frame drawn inside the picture has
text and buttons overhanging it on both sides.

Output goes to ``--out`` (default: the current directory). Nothing here
touches the live art — upload the files on the dashboard.
"""

from __future__ import annotations

import argparse
import math
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageSequence

# ── Dials ───────────────────────────────────────────────────────────────────
#: Bokeh edge softness: the Gaussian sigma, in px, blurred into each dot.
#: 0 is the original ~2.4 px feather; Ben picked 3 (2026-09-28).
SIGMA = 3.0
#: Frames per loop. 24 was the first cut (2.2 s); 34 is 40% slower with the
#: same smoothness (Ben, 2026-09-28: more frames, not longer frames).
FRAMES = 34
FRAME_MS = 90

W = 1200
ART = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "panel_art"
    / "poppies_1200x400.webp"
)

GOLD = np.array([250, 205, 80], np.float32)
_MID, _EDGE = np.array([253, 228, 102], np.float32), np.array([246, 157, 7], np.float32)
_t = (np.abs(np.arange(W) - W / 2) / (W / 2)) ** 2
FRAME_GOLD = (_MID * (1 - _t[:, None]) + _EDGE * _t[:, None])[None, :, :]
RADIUS, STROKE, INSET = 62, 8, 10
#: How far the frame's *side* strokes sit inside the picture. Zero, so the
#: gold line is exactly as wide as the text column under it: Discord gives a
#: line of text no padding and draws a gallery image as wide as the text, so
#: at the old 10 px the text and buttons overhung the frame (Ben,
#: 2026-09-29: "the text should move in the width of the yellow line").
#: ``INSET`` is still the closed side's — the top of a header, the bottom of
#: the footer — which keeps the titles where ``panel_title_service`` expects.
SIDE_INSET = 0
#: Each strip is open on its flat side — the side that meets the message: the
#: headers' bottom, the footer's top — with no gold line there, and the black
#: and the side strokes fade to transparent over this share of the height, so the art melts into
#: the card instead of stacking on it (Ben, 2026-09-28: "fade into the message
#: content instead of looking like a cheeseburger").
FADE = 0.10


# ── Shared pieces ───────────────────────────────────────────────────────────
def gaussian(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur of a 2-D array, mirrored at the edges."""
    if sigma <= 0:
        return a
    r = int(4 * sigma + 0.5)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        p = np.pad(a, pad, mode="symmetric")
        n = a.shape[axis]
        a = sum(
            k[j] * (p[j : j + n] if axis == 0 else p[:, j : j + n])
            for j in range(2 * r + 1)
        )
    return a


def brighten(sigma: float) -> float:
    """A blurred dot loses peak brightness; give a little back."""
    return 1 + 0.25 * min(sigma, 2)


def twinkle(i: int, t: int, frames: int) -> float:
    """Dot ``i``'s brightness in frame ``t``: golden-angle phases, a third twice as fast."""
    phase = (i * 2.399) % (2 * math.pi)
    speed = 1 + (i % 3 == 0)
    return 0.25 + 0.75 * (
        0.5 + 0.5 * math.sin(2 * math.pi * speed * t / frames + phase)
    )


def fade_ramp(h: int, side: str, fade: float = FADE) -> np.ndarray:
    """Opacity per row: 1, falling to 0 over the last ``fade`` of ``side``."""
    y = np.arange(h, dtype=np.float32) + 0.5
    edge = y if side == "top" else h - y
    return np.clip(edge / max(fade * h, 1e-6), 0, 1)


def save(
    frames: list[Image.Image],
    out: Path,
    quality: int,
    duration: int | list[int] = FRAME_MS,
) -> None:
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        quality=quality,
        method=6,
    )
    print(out, out.stat().st_size, "bytes,", len(frames), "frames")


# ── Footer and lobby header: drawn from scratch ────────────────────────────
def frame_shape(
    h: int, inset: int, radius: int, open_side: str, side_inset: int | None = None
) -> np.ndarray:
    """A rounded rectangle run off the canvas on ``open_side`` ('top'/'bottom'),
    so that side has neither corners nor a stroke. ``inset`` is the closed
    side's distance from the canvas edge; ``side_inset`` the left and right
    sides', the same unless given."""
    ss = 4
    side = inset if side_inset is None else side_inset
    m = Image.new("L", (W * ss, h * ss), 0)
    d = ImageDraw.Draw(m)
    over = (radius + STROKE) * ss
    top = -over if open_side == "top" else inset * ss
    bottom = h * ss + over if open_side == "bottom" else (h - inset) * ss - 1
    d.rounded_rectangle(
        [side * ss, top, (W - side) * ss - 1, bottom], radius=radius * ss, fill=255
    )
    return np.asarray(m.resize((W, h), Image.LANCZOS)).astype(np.float32) / 255


def sprig(fade_stem: bool) -> np.ndarray:
    """The leaf spray right of the art's poppies, every petal and bud keyed to black."""
    art = np.asarray(Image.open(ART).convert("RGB")).astype(np.float32)
    c = art[225:362, 870:1110].copy()
    petal = ((c[..., 0] > 200) & (c[..., 1] > 140)) | (
        (c[..., 0] > 120) & (c[..., 0] - c[..., 1] > 62)
    )
    pm = (
        Image.fromarray((petal * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(9))
        .filter(ImageFilter.GaussianBlur(1.5))
    )
    c = np.maximum(c - 7, 0) * (255 / 248)  # the art's near-black to true black
    c *= (1 - np.asarray(pm).astype(np.float32) / 255)[..., None]
    h, w = c.shape[:2]  # soften the crop's own edges so no seam shows
    ex = np.minimum(np.arange(w), np.arange(w)[::-1]) / 10
    ey = np.minimum(np.arange(h), np.arange(h)[::-1]) / 10
    c *= np.clip(np.minimum(ex[None, :], ey[:, None]), 0, 1)[..., None]
    if fade_stem:  # the cut beside the poppy leaves a hard edge on the stem side
        c *= (np.clip(np.arange(w) / 60, 0, 1) ** 1.5)[None, :, None]
    return c


def place(base: np.ndarray, img: np.ndarray, x: int, y: int, scale: float) -> None:
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize(
        (int(img.shape[1] * scale), int(img.shape[0] * scale)), Image.LANCZOS
    )
    a = np.asarray(im).astype(np.float32)
    region = base[y : y + a.shape[0], x : x + a.shape[1]]
    np.maximum(region, a[: region.shape[0], : region.shape[1]], out=region)


def scatter(
    h: int, count: int, seed: int, keep_out, margin: int = 26
) -> list[tuple[float, float, float, float]]:
    """``count`` dots (x, y, radius, dimness) at least 34 px apart, ``margin``
    px clear of the top and bottom, none where ``keep_out(x, y)``."""
    rng = np.random.default_rng(seed)
    dots: list[tuple[float, float, float, float]] = []
    for _ in range(count * 1000):
        if len(dots) == count:
            break
        x, y = rng.uniform(40, W - 40), rng.uniform(margin, h - margin)
        if keep_out(x, y) or any(
            (x - a) ** 2 + (y - b) ** 2 < 34**2 for a, b, *_ in dots
        ):
            continue
        r = rng.choice([1.5, 2.2, 3.2, 4.8, 6.5], p=[0.3, 0.3, 0.2, 0.13, 0.07])
        dots.append((x, y, r, rng.uniform(0.35, 1.0)))
    else:
        raise SystemExit(f"only {len(dots)} of {count} dots fit a {h} px strip")
    return dots


def animate(
    base: np.ndarray,
    dots,
    open_side: str,
    sigma: float,
    frames: int,
    radius: int = RADIUS,
) -> list[Image.Image]:
    h = base.shape[0]
    outer = frame_shape(h, INSET, radius, open_side, SIDE_INSET)
    inner = frame_shape(
        h, INSET + STROKE, radius - STROKE, open_side, SIDE_INSET + STROKE
    )
    ramp = fade_ramp(h, open_side)[:, None]
    fill = inner * ramp
    stroke = (outer - inner) * ramp  # the side strokes fade out with the black
    alpha = fill + stroke
    pad = 4 + int(3 * sigma)
    yy, xx = np.mgrid[0:h, 0:W]
    out = []
    for t in range(frames):
        f = base.copy()
        for i, (x, y, r, dim) in enumerate(dots):
            k = dim * twinkle(i, t, frames)
            x0, x1 = int(x - r - pad), int(x + r + pad + 1)
            y0, y1 = int(y - r - pad), int(y + r + pad + 1)
            dd = np.sqrt((xx[y0:y1, x0:x1] - x) ** 2 + (yy[y0:y1, x0:x1] - y) ** 2)
            cov = np.clip((r + 1.2 - dd) / 2.4, 0, 1)
            if sigma > 0:
                cov = (gaussian(cov, sigma) * brighten(sigma)).clip(0, 1)
            cov = cov[..., None] * k
            f[y0:y1, x0:x1] = f[y0:y1, x0:x1] * (1 - cov) + GOLD * cov
        f = f * fill[..., None] + FRAME_GOLD * stroke[..., None]
        col = np.where(alpha[..., None] > 0, f / np.maximum(alpha[..., None], 1e-6), 0)
        rgba = np.dstack([col, alpha * 255]).clip(0, 255).astype(np.uint8)
        out.append(Image.fromarray(rgba, "RGBA"))
    return out


#: The one height the footer and the lobby header share (Ben, 2026-09-29:
#: "make the small header banner the same height as the footer"). The footer
#: was 200 and the header 108 after two trims; he met them in between, from
#: samples at 140 / 154 / 160 / 170.
STRIP_H = 160


def footer(sigma: float, frames: int) -> list[Image.Image]:
    """Hub footer, 1200 × :data:`STRIP_H`: two sprigs meeting in the middle,
    open at the top where it fades up into the card. It is the 200 strip's
    layout at ``k`` of its size, sprigs and all."""
    h, k = STRIP_H, STRIP_H / 200
    s = 1.15 * k
    right = sprig(fade_stem=False)
    sw, sh = int(right.shape[1] * s), int(right.shape[0] * s)
    base = np.zeros((h, W, 3), np.float32)
    y = (h - sh) // 2 + round(4 * k)
    place(base, right[:, ::-1], W // 2 - sw + round(12 * k), y, s)
    place(base, right, W // 2 - round(12 * k), y, s)
    # 60 dots filled the 200 strip; scatter() keeps 26 px clear top and bottom.
    count = round(60 * (h - 52) / 148)
    dots = scatter(h, count, 11, lambda x, y: abs(x - W / 2) < 330 * k and y > 70 * k)
    return animate(base, dots, "top", sigma, frames)


#: The lobby strip's height and where the title's capitals centre on it —
#: ``panel_title_service.LOBBY`` must say the same (``LOBBY_CAP_CENTRE / LOBBY_H``).
#: The capitals sit midway between the frame's inside (``INSET + STROKE``,
#: y = 18) and the strip's faded bottom edge, as Ben centred them on the 108
#: strip (2026-09-28); the type grew with the strip, to 116 px.
LOBBY_H, LOBBY_CAP_CENTRE = STRIP_H, (INSET + STROKE + STRIP_H) // 2


def lobby(sigma: float, frames: int, clear: int = 318) -> list[Image.Image]:
    """Lobby header, 1200 × :data:`LOBBY_H`, open at the bottom where it fades
    into the card. No leaves: the bot draws a sprig either side of each game's
    title at a fixed distance from the words (Ben, 2026-09-29), from
    :func:`sprite`. The dots stay out of the middle ``2 × clear`` px, where
    the widest title goes (616 px, ``panel_title_service.LOBBY``)."""
    base = np.zeros((LOBBY_H, W, 3), np.float32)
    # 18 dots filled the 108 strip; scatter() keeps 26 px clear top and bottom.
    count = round(18 * (LOBBY_H - 52) / 56)
    dots = scatter(LOBBY_H, count, 7, lambda x, y: abs(x - W / 2) < clear)
    return animate(base, dots, "bottom", sigma, frames)


#: The card strips' height: the thin, untitled header and footer on the
#: public cards that are neither hubs nor lobbies (Ben, 2026-09-30: "a third
#: thinner header with no title for use in the embeds"), half the other strips.
CARD_H = 80


def card(
    sigma: float, frames: int, open_side: str, h: int = CARD_H
) -> list[Image.Image]:
    """Card header (``open_side="bottom"``) or footer (``"top"``), 1200 × ``h``:
    the frame and the dots, no leaves and no title. The corners shrink with
    the strip, or at 80 px the whole strip would be corner; the dots keep to
    the band between the closed side's stroke and the open side's fade."""
    margin = INSET + STROKE + 6
    count = round(60 * (h - 2 * margin) / 148)
    seed = 13 if open_side == "bottom" else 17
    dots = scatter(h, count, seed, lambda x, y: False, margin)
    base = np.zeros((h, W, 3), np.float32)
    radius = round(RADIUS * h / STRIP_H)
    return animate(base, dots, open_side, sigma, frames, radius=radius)


def sprite() -> Image.Image:
    """The lobby's leaf sprig as an RGBA sprite at the art's own scale, stem on
    the left, cropped to its leaves — ``assets/panel_art/lobby_sprig.png``.
    Over black it composites to exactly the painted sprig: the alpha is each
    pixel's brightest channel and the colour is un-premultiplied by it."""
    c = sprig(fade_stem=True)
    a = c.max(axis=2)
    rgb = np.where(a[..., None] > 0, c / np.maximum(a[..., None], 1e-6) * 255, 0)
    im = Image.fromarray(np.dstack([rgb, a]).clip(0, 255).astype(np.uint8), "RGBA")
    return im.crop(im.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox())


# ── Banner: re-twinkled from an existing file ───────────────────────────────
def _dilate(m: np.ndarray, n: int) -> np.ndarray:
    for _ in range(n):
        g = m.copy()
        g[1:] |= m[:-1]
        g[:-1] |= m[1:]
        g[:, 1:] |= m[:, :-1]
        g[:, :-1] |= m[:, 1:]
        m = g
    return m


def _blobs(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """4-connected components of ``mask``, in raster order of their first pixel."""
    seen = np.zeros_like(mask)
    out = []
    h, w = mask.shape
    for y0, x0 in zip(*np.nonzero(mask), strict=True):
        if seen[y0, x0]:
            continue
        q, pix = deque([(y0, x0)]), []
        seen[y0, x0] = True
        while q:
            y, x = q.popleft()
            pix.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        out.append(pix)
    return out


def open_bottom(still: np.ndarray) -> None:
    """Lift the banner's baked-in bottom line: every row below the last dark
    one inside the frame becomes a copy of it (black, with the side strokes),
    then all of it fades out over the bottom :data:`FADE`. The banner keeps its
    height, so the title the bot draws into it doesn't move."""
    h, w = still.shape[:2]
    col = still[:, w // 2]
    inside = np.nonzero((col[:, 3] == 255) & (col[:, :3].max(axis=1) < 40))[0]
    last = int(inside.max())
    still[last + 1 :] = still[last]
    still[..., 3] *= fade_ramp(h, "bottom")[:, None]  # side strokes too


def to_edge(frames: list[Image.Image]) -> list[Image.Image]:
    """Move a baked-in frame's sides out to the picture's sides
    (:data:`SIDE_INSET`): crop the clear margin left and right of the frame,
    then stretch back to the original width. The height is untouched, so the
    title the bot draws in doesn't move; the stretch is the margin's share of
    the width, 1.5% on Billy's banner. A picture whose frame is already at
    the edge comes back as it was."""
    alpha = np.asarray(frames[0].getchannel("A"))
    cols = np.nonzero((alpha > 128).any(axis=0))[0]
    if not len(cols):
        return frames
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    w, h = frames[0].size
    if (x0, x1) == (0, w):
        return frames
    return [f.crop((x0, 0, x1, h)).resize((w, h), Image.LANCZOS) for f in frames]


def edge(src: Path) -> tuple[list[Image.Image], list[int]]:
    """A finished banner's frames, frame moved out, nothing else redrawn —
    and each frame's own duration, so a banner from another hand keeps its
    pace. Refuses a picture with nothing to move (no clear margin beside the
    frame): writing the same file back would look like a job done."""
    frames, durations = [], []
    for f in ImageSequence.Iterator(Image.open(src)):
        frames.append(f.convert("RGBA"))
        durations.append(int(f.info.get("duration") or FRAME_MS))
    out = to_edge(frames)
    if out is frames:
        raise ValueError(
            f"{src.name}: no clear margin beside the frame, nothing to move"
        )
    return out, durations


def banner(src: Path, sigma: float, frames: int) -> list[Image.Image]:
    stack = np.stack(
        [
            np.asarray(f.convert("RGBA")).astype(np.float32)
            for f in ImageSequence.Iterator(Image.open(src))
        ]
    )
    hi, still = stack.max(axis=0), np.median(stack, axis=0)
    open_bottom(still)
    sd = stack[..., :3].max(axis=3).std(axis=0)
    dots = []
    dotmask = np.zeros(sd.shape, bool)
    for pix in _blobs(_dilate(sd > 6, 2)):
        ys, xs = np.array([p[0] for p in pix]), np.array([p[1] for p in pix])
        if (
            np.ptp(ys) < 26 and np.ptp(xs) < 26 and sd[ys, xs].max() > 25
        ):  # a twinkle, not codec noise
            dots.append((ys, xs))
            dotmask[ys, xs] = True
    print(len(dots), "dots")

    ring = _dilate(dotmask, 3) & ~dotmask
    sky = np.median(still[ring][:, :3], axis=0)
    still[dotmask, :3] = sky
    glow = np.maximum(hi[..., :3] - sky, 0)

    layers = []
    pad = int(4 * sigma + 0.5) + 2
    h, w = sd.shape
    for ys, xs in dots:
        y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, h)
        x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, w)
        d = np.zeros((y1 - y0, x1 - x0, 3), np.float32)
        d[ys - y0, xs - x0] = glow[ys, xs]
        if sigma > 0:
            peak = d.max()
            d = np.stack([gaussian(d[..., c], sigma) for c in range(3)], -1) * brighten(
                sigma
            )
            d *= min(
                1.0, peak / max(float(d.max()), 1e-6)
            )  # a clipped red would whiten it
        layers.append((slice(y0, y1), slice(x0, x1), d))

    out = []
    for t in range(frames):
        f = still.copy()
        for i, (sy, sx, d) in enumerate(layers):
            f[sy, sx, :3] += d * twinkle(i, t, frames)
        out.append(Image.fromarray(f.clip(0, 255).astype(np.uint8), "RGBA"))
    return to_edge(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "which", choices=["banner", "card", "edge", "footer", "lobby", "sprig"]
    )
    ap.add_argument(
        "source",
        nargs="?",
        type=Path,
        help="banner: the banner to re-twinkle; edge: the finished banner",
    )
    ap.add_argument("--sigma", type=float, default=SIGMA)
    ap.add_argument("--frames", type=int, default=FRAMES)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--height", type=int, default=CARD_H, help="card: the strips' height")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.which == "banner":
        if a.source is None:
            ap.error("banner needs the banner file to re-twinkle")
        save(banner(a.source, a.sigma, a.frames), a.out / "panel_banner.webp", 80)
    elif a.which == "edge":
        if a.source is None:
            ap.error("edge needs the finished banner file")
        try:
            frames, durations = edge(a.source)
        except ValueError as exc:
            ap.error(str(exc))
        save(frames, a.out / "panel_banner.webp", 80, durations)
    elif a.which == "footer":
        save(footer(a.sigma, a.frames), a.out / "panel_footer.webp", 85)
    elif a.which == "card":
        for side, name in (("bottom", "card_header"), ("top", "card_footer")):
            save(card(a.sigma, a.frames, side, a.height), a.out / f"{name}.webp", 85)
    elif a.which == "lobby":
        save(lobby(a.sigma, a.frames), a.out / "lobby_header.webp", 85)
    else:
        out = a.out / "lobby_sprig.png"
        sprite().save(out, optimize=True)
        print(out, out.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
