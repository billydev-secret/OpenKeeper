"""The banner's box: the panel art's look as a closed frame (Ben, 2026-09-29).

``/banner`` used to wear the quote cards' Golden Poppy frame. Ben asked for
"the box to look like the large header and footer but a box around it with
the bokeh that we're using in the titles today", with the body text bigger
and in a more readable face. So the box is the hub art's own ingredients
(``scripts/make_panel_art.py``) closed on all four sides: a black field, the
gold gradient line drawn right at the picture's edge, and small gold dots
twinkling on their own phases.

Nothing here is loaded from a file, which is what lets the card's **height
follow its text**: a drawn frame has no artwork to stretch. The old card was
a fixed shape, so a bigger face could only have meant fewer words before the
ellipsis.

This module is the geometry and the pixels. It knows nothing of fonts or
emoji: :func:`layout` takes the caller's measures, so
``quote_renderer._compose_box_banner`` owns loading a face and pilmoji.
Every length is a share of the card's width, so any width draws the same
card. The values were argued out against rendered mockups at desktop
(550 px) and phone (350 px) scale; ``docs/specs/quote_renderer_spec.md`` § The
banner box records what each was chosen over.
"""

from __future__ import annotations

import functools
import math
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

#: The widths below are written in px at this card width.
_AT = 900

# ── The frame: the panel art's, at the card's scale ──────────────────────────
#: ``make_panel_art.py``'s RADIUS and STROKE over its 1200 px width. A test
#: holds the two files to the same numbers, colours included.
FRAME_RADIUS = 62 / 1200
FRAME_STROKE = 8 / 1200
FRAME_GOLD_MID = (253, 228, 102)
FRAME_GOLD_EDGE = (246, 157, 7)
DOT_GOLD = (250, 205, 80)

# ── The type ─────────────────────────────────────────────────────────────────
#: Body size, up from the old card's 47 px. A long notice shrinks toward the
#: floor before it loses a word: a notice cut off mid-sentence is a broken
#: notice, and the floor is still larger than the old size.
BODY_PX = 62 / _AT
BODY_FLOOR_PX = 52 / _AT
BODY_STEP_PX = 2 / _AT
#: Baseline to baseline, as a multiple of the body size.
LINE_HEIGHT = 1.3
TITLE_PX = 84 / _AT

# ── The spacing ──────────────────────────────────────────────────────────────
#: Frame to words, left and right: a measure of about 32 characters. A
#: narrower one (140 px was tried) made the same notice a line longer and
#: 80 px taller on a phone.
SIDE_PAD = 90 / _AT
#: Frame to the title's capitals. Ben, 2026-09-29, of the 80 px it first
#: shipped with: "move the title up so there's the same amount of space
#: between the text and top gold line". The space under the words is
#: :data:`PAD_BOTTOM` from the last baseline, which is 57 px of black under a
#: line with descenders and 70 under one without; 62 sits between the two, so
#: the card reads even whichever the last line is, and the title does not
#: move with the letters of the body.
PAD_TOP_TITLED = 62 / _AT
#: A card with no title: frame to the first line's capitals, and last
#: baseline to the frame. Unequal on purpose: descenders hang into the space
#: below, so equal numbers read top-heavy.
PAD_UNTITLED_TOP = 72 / _AT
PAD_UNTITLED_BOTTOM = 66 / _AT
#: Last baseline to the frame.
PAD_BOTTOM = 70 / _AT
#: Title baseline to the first line's capitals.
TITLE_GAP = 44 / _AT
#: A card is never taller than it is wide: past that it fills a phone screen.
MAX_ASPECT = 1.0

# ── The dots ─────────────────────────────────────────────────────────────────
#: The twinkle's loop, ``make_panel_art.py``'s FRAMES and FRAME_MS.
FRAMES = 34
FRAME_MS = 90
#: Dots per 100 000 px² of card at 900 wide. Most are the panel art's specks;
#: a few are stars, large and bright enough to survive a phone: one per
#: :data:`STAR_AREA` px² of card, between the four corners' worth and
#: :data:`DOT_STARS`. A short card with ten looked busy.
DOT_DENSITY = 16.0
DOT_STARS = 10
STAR_AREA = 45_000
_SPECK_RADII = ((1.6, 0.30), (2.2, 0.30), (3.0, 0.22), (4.0, 0.18))
_STAR_RADII = (4.6, 5.2, 6.0)
_DOT_SPACING = 30 / _AT
_DOT_MARGIN = 24 / _AT
#: No dot within this of a word; out to the halo they are small and dim.
DOT_CLEAR = 6 / _AT
DOT_HALO = 30 / _AT
_DOT_BLUR = 2.2 / _AT


class Box(NamedTuple):
    left: float
    top: float
    right: float
    bottom: float


class Dot(NamedTuple):
    x: float
    y: float
    radius: float
    peak: float  # brightness at the top of its twinkle, 0-1
    order: int   # sets its phase and speed


@dataclass(frozen=True)
class BoxLayout:
    """Where everything on a boxed banner sits, in px."""

    width: int
    height: int
    body_size: int
    pitch: int                      # baseline to baseline
    lines: tuple[str, ...]
    baselines: tuple[int, ...]      # one per line
    title: str
    title_size: int
    title_baseline: int             # 0 when there is no title
    truncated: bool


def px(share: float, width: int) -> int:
    return max(1, round(share * width))


class BodyMetrics(NamedTuple):
    measure: Callable[[str], int]   # width of a run of text, emoji counted
    cap_height: int


def wrap(words: Sequence[str], widths: Sequence[int], space: int, limit: float) -> list[list[int]]:
    """Greedy wrap by word widths: lines of indexes into ``words``."""
    lines: list[list[int]] = []
    run = 0.0
    for i, w in enumerate(widths):
        if lines and lines[-1] and run + space + w <= limit:
            lines[-1].append(i)
            run += space + w
        else:
            lines.append([i])
            run = w
    return lines


def balanced(text: str, measure: Callable[[str], int], limit: int) -> list[str]:
    """``text`` wrapped to ``limit``, then evened out.

    A greedy wrap fills every line but the last, which leaves a centred block
    ending on an orphan. Narrowing the measure while the line count holds
    spreads the words evenly instead. Each paragraph is balanced on its own;
    a blank line stays blank. Words are measured once and lines summed from
    them, so a long notice costs a few dozen measures, not thousands.
    """
    out: list[str] = []
    space = measure("n n") - measure("nn")
    for para in text.splitlines() or [""]:
        words = para.split()
        if not words:
            out.append("")
            continue
        widths = [measure(w) for w in words]
        best = wrap(words, widths, space, limit)
        floor = max(max(widths), limit * 0.5)
        step = max(1.0, limit / 90)
        at = limit - step
        while at >= floor:
            tried = wrap(words, widths, space, at)
            if len(tried) != len(best):
                break
            best = tried
            at -= step
        out.extend(" ".join(words[i] for i in line) for line in best)
    return out


def cut_to_fit(line: str, measure: Callable[[str], int], limit: int) -> str:
    """``line`` closed with an ellipsis, shortened by whole words to fit."""
    base = line.rstrip(" ,;:.!?…-–—")
    while base and measure(f"{base}…") > limit:
        head, _, _ = base.rpartition(" ")
        base = (head or base[:-1]).rstrip(" ,;:.!?…-–—")
    return f"{base}…"


def layout(
    text: str,
    title: str,
    *,
    width: int,
    body: Callable[[int], BodyMetrics],
    title_metrics: Callable[[str, int], "tuple[int, int]"],
) -> BoxLayout:
    """Lay a banner out inside the box and size the box to it.

    ``body(size)`` gives the body face's measure and cap height at ``size``;
    ``title_metrics(title, size)`` the title's drawn width and cap height.
    The body takes the largest size from :data:`BODY_PX` down to the floor
    whose lines fit under :data:`MAX_ASPECT`; at the floor, what still won't
    fit is cut at a word with an ellipsis, as is a single word wider than a
    line, after the body has shrunk toward the floor for it.
    """
    stroke = px(FRAME_STROKE, width)
    side = stroke + px(SIDE_PAD, width)
    limit = width - 2 * side
    max_height = round(width * MAX_ASPECT)

    title = title.strip()
    title_size = title_cap = 0
    title_cut = False
    if title:
        title_size = px(TITLE_PX, width)
        floor = max(12, title_size // 3)
        while title_size > floor and title_metrics(title, title_size)[0] > limit:
            title_size -= 2
        # A title is one line and never wraps: past the floor it is cut at a
        # word, as the body's over-wide words are, not drawn into the frame.
        if title_metrics(title, title_size)[0] > limit:
            title = cut_to_fit(
                title, lambda t: title_metrics(t, title_size)[0], limit
            )
            title_cut = True
        title_cap = title_metrics(title, title_size)[1]
    pad_top = px(PAD_TOP_TITLED if title else PAD_UNTITLED_TOP, width)
    pad_bottom = px(PAD_BOTTOM if title else PAD_UNTITLED_BOTTOM, width)
    head = title_cap + px(TITLE_GAP, width) if title else 0

    size = px(BODY_PX, width)
    floor_size = px(BODY_FLOOR_PX, width)
    step = px(BODY_STEP_PX, width)
    while True:
        metrics = body(size)
        pitch = round(size * LINE_HEIGHT)
        lines = balanced(text, metrics.measure, limit)
        room = max_height - 2 * stroke - pad_top - pad_bottom - head - metrics.cap_height
        fits = max(1, room // pitch + 1)
        wide = any(metrics.measure(line) > limit for line in lines)
        if (len(lines) <= fits and not wide) or size <= floor_size:
            break
        size = max(floor_size, size - step)

    truncated = len(lines) > fits
    if truncated:
        lines = lines[:fits]
        lines[-1] = cut_to_fit(lines[-1], metrics.measure, limit)
    # A word wider than the measure (a long link) is never wrapped: at the
    # floor it is cut, so no lettering runs into the frame.
    too_wide = [metrics.measure(line) > limit for line in lines]
    lines = [
        cut_to_fit(line, metrics.measure, limit) if wide else line
        for line, wide in zip(lines, too_wide)
    ]
    truncated = truncated or any(too_wide) or title_cut

    top = stroke + pad_top
    title_baseline = top + title_cap if title else 0
    first = top + head + metrics.cap_height
    baselines = tuple(first + i * pitch for i in range(len(lines)))
    height = baselines[-1] + pad_bottom + stroke
    return BoxLayout(
        width=width,
        height=height,
        body_size=size,
        pitch=pitch,
        lines=tuple(lines),
        baselines=baselines,
        title=title,
        title_size=title_size,
        title_baseline=title_baseline,
        truncated=truncated,
    )


# ── Pixels ───────────────────────────────────────────────────────────────────
_SS = 4  # the frame is drawn this many times larger and scaled down


def _rounded(width: int, height: int, inset: int, radius: int) -> "PILImage":
    from PIL import Image, ImageDraw  # noqa: PLC0415

    big = Image.new("L", (width * _SS, height * _SS), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (inset * _SS, inset * _SS, (width - inset) * _SS - 1, (height - inset) * _SS - 1),
        radius=max(1, radius) * _SS,
        fill=255,
    )
    return big.resize((width, height), Image.Resampling.LANCZOS)


# The frame is most of what a card costs to compose, and the animated renderer
# composes each card twice for its matte: drawn once per size, copied out.
@functools.lru_cache(maxsize=8)
def _card_mask(width: int, height: int) -> "PILImage":
    return _rounded(width, height, 0, px(FRAME_RADIUS, width))


def card_mask(width: int, height: int) -> "PILImage":
    """The card's shape: everything inside the frame's outer edge."""
    return _card_mask(width, height).copy()


def frame_layer(width: int, height: int) -> "PILImage":
    """The gold line on a transparent canvas, at the picture's very edge.

    Yellow at the middle of the card shading to orange at its sides, as the
    panel art's frame does. At the edge, not inset: Discord draws a picture
    flush with the text around it, so a frame set in from the edge floats.
    """
    return _frame_layer(width, height).copy()


@functools.lru_cache(maxsize=8)
def _frame_layer(width: int, height: int) -> "PILImage":
    from PIL import Image, ImageChops  # noqa: PLC0415

    stroke = px(FRAME_STROKE, width)
    radius = px(FRAME_RADIUS, width)
    line = ImageChops.subtract(
        _card_mask(width, height), _rounded(width, height, stroke, radius - stroke)
    )
    row = Image.new("RGB", (width, 1))
    half = width / 2
    row.putdata([
        tuple(
            round(m + (e - m) * (abs(x + 0.5 - half) / half) ** 2)
            for m, e in zip(FRAME_GOLD_MID, FRAME_GOLD_EDGE)
        )
        for x in range(width)
    ])
    layer = row.resize((width, height), Image.Resampling.NEAREST).convert("RGBA")
    layer.putalpha(line)
    return layer


def star_count(width: int, height: int) -> int:
    """How many of a card's dots are stars: by its area, four to ten."""
    area = width * height / (width / _AT) ** 2
    return min(DOT_STARS, max(4, round(area / STAR_AREA)))


def scatter(
    width: int, height: int, *, seed: int, keep_clear: Sequence[Box] = ()
) -> tuple[Dot, ...]:
    """The card's dots: the same for the same ``seed``, none on the words.

    ``keep_clear`` is a box per line of lettering. A dot within
    :data:`DOT_CLEAR` of one is dropped, so no star sits behind a letter; out
    to :data:`DOT_HALO` a dot is kept small and dim, which keeps the field
    from looking swept around the text. The first :func:`star_count` are the
    large bright ones, and the first four of those go one to each corner
    region so the band beside the title is never empty.
    """
    rng = random.Random(seed)
    scale = width / _AT
    margin = _DOT_MARGIN * width
    spacing = _DOT_SPACING * width
    clear = DOT_CLEAR * width
    halo = DOT_HALO * width
    stars = star_count(width, height)
    count = max(stars, round(width * height / (scale * scale) / 100_000 * DOT_DENSITY))

    def distance(x: float, y: float) -> float:
        best = math.inf
        for box in keep_clear:
            dx = max(box.left - x, 0.0, x - box.right)
            dy = max(box.top - y, 0.0, y - box.bottom)
            best = min(best, math.hypot(dx, dy))
        return best

    corners = [
        (0.0, 0.0, 0.26, 0.34), (0.74, 0.0, 1.0, 0.34),
        (0.0, 0.66, 0.26, 1.0), (0.74, 0.66, 1.0, 1.0),
    ]
    dots: list[Dot] = []
    for _ in range(count * 400):
        if len(dots) >= count:
            break
        n = len(dots)
        star = n < stars
        x0, y0, x1, y1 = corners[n] if n < len(corners) else (0.0, 0.0, 1.0, 1.0)
        x = rng.uniform(max(margin, x0 * width), min(width - margin, x1 * width))
        y = rng.uniform(max(margin, y0 * height), min(height - margin, y1 * height))
        if star:
            radius = rng.choice(_STAR_RADII) * scale
            peak = rng.uniform(0.80, 1.0)
        else:
            pick = rng.random()
            radius = _SPECK_RADII[-1][0] * scale
            for r, share in _SPECK_RADII:
                pick -= share
                if pick < 0:
                    radius = r * scale
                    break
            peak = rng.uniform(0.45, 0.95)
        away = distance(x, y)
        if away < clear + radius:
            continue
        if away < halo:
            if star:
                continue  # a star beside the words pulls the eye off them
            radius = min(radius, 2.2 * scale)
            peak *= 0.5
        if any((x - d.x) ** 2 + (y - d.y) ** 2 < spacing**2 for d in dots):
            continue
        dots.append(Dot(x, y, radius, peak, n))
    return tuple(dots)


def twinkle(order: int, frame: int, frames: int = FRAMES) -> float:
    """Dot ``order``'s share of its peak in ``frame``: golden-angle phases, a
    third of the dots twice as fast (``make_panel_art.twinkle``)."""
    phase = (order * 2.399) % (2 * math.pi)
    speed = 1 + (order % 3 == 0)
    return 0.25 + 0.75 * (
        0.5 + 0.5 * math.sin(2 * math.pi * speed * frame / frames + phase)
    )


def _sprites(dots: Sequence[Dot], width: int) -> "list[tuple[PILImage, int, int]]":
    """Each dot at full brightness as an "L" patch, with where it goes."""
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    blur = max(0.8, _DOT_BLUR * width)
    out = []
    for dot in dots:
        pad = int(dot.radius + 3 * blur + 3)
        left, top = int(dot.x) - pad, int(dot.y) - pad
        side = 2 * pad + 1
        big = Image.new("L", (side * _SS, side * _SS), 0)
        cx, cy, r = (dot.x - left) * _SS, (dot.y - top) * _SS, dot.radius * _SS
        ImageDraw.Draw(big).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        patch = big.resize((side, side), Image.Resampling.LANCZOS).filter(
            ImageFilter.GaussianBlur(blur)
        )
        # A blurred dot loses its peak; give most of it back.
        brightest = patch.getextrema()[1]
        gain = min(1.5, 235 / max(1, int(brightest))) if isinstance(brightest, (int, float)) else 1.0
        out.append((patch.point([min(255, int(v * gain)) for v in range(256)]), left, top))
    return out


def field(
    width: int, height: int, dots: Sequence[Dot], frame: int = 0,
    *, sprites: "list[tuple[PILImage, int, int]] | None" = None,
) -> "PILImage":
    """The black field with its dots as they are in ``frame``, as RGB."""
    from PIL import Image, ImageChops  # noqa: PLC0415

    sprites = _sprites(dots, width) if sprites is None else sprites
    light = Image.new("L", (width, height), 0)
    for dot, (patch, left, top) in zip(dots, sprites):
        k = dot.peak * twinkle(dot.order, frame)
        lit = patch.point([int(v * k) for v in range(256)])
        box = (left, top, left + patch.width, top + patch.height)
        clip = (max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3]))
        if clip[0] >= clip[2] or clip[1] >= clip[3]:
            continue
        part = lit.crop((clip[0] - left, clip[1] - top, clip[2] - left, clip[3] - top))
        light.paste(ImageChops.lighter(light.crop(clip), part), clip[:2])
    out = Image.new("RGB", (width, height), (0, 0, 0))
    out.paste(Image.new("RGB", (width, height), DOT_GOLD), mask=light)
    return out


def backgrounds(width: int, height: int, dots: Sequence[Dot]) -> "Iterator[PILImage]":
    """One loop of the twinkle: :data:`FRAMES` fields, seamless end to start."""
    sprites = _sprites(dots, width)
    for frame in range(FRAMES):
        yield field(width, height, dots, frame, sprites=sprites)
