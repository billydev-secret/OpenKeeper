"""Animated quote cards — every posted card is an animated WebP.

The animated renderer pulls the text/frame layer out of two solid-fill renders
and plays the avatar (or, for a still picture, drifting golden bokeh)
underneath it. These tests pin the
pieces that make that safe: the layer really reproduces the still card, an
animated avatar plays in the avatar box, and any failure posts the still card
under a matching filename.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from bot_modules.shared import quote_renderer as qr

TEXT = "Stars are just the universe's way of saying hello back."


def _still(color=(80, 50, 130)) -> bytes:
    av = Image.new("RGB", (256, 256), color)
    ImageDraw.Draw(av).ellipse([40, 40, 216, 216], fill=(230, 170, 70))
    buf = io.BytesIO()
    av.save(buf, "PNG")
    return buf.getvalue()


def _gif(colors, *, duration=30, size=64) -> bytes:
    frames = [Image.new("RGB", (size, size), c) for c in colors]
    buf = io.BytesIO()
    frames[0].save(
        buf, "GIF", save_all=True, append_images=frames[1:], duration=duration, loop=0,
    )
    return buf.getvalue()


def _kw(avatar: bytes, **extra) -> dict:
    return {"author_name": "Poppy", "avatar_bytes": avatar, "theme": qr.THEMES["midnight"], **extra}


def _frames(data: bytes) -> list[Image.Image]:
    im = Image.open(io.BytesIO(data))
    out = []
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        out.append(im.convert("RGBA"))
    return out


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(b"RIFF\x00\x00\x00\x00WEBPVP8X", "quote.webp", id="webp"),
        pytest.param(b"\x89PNG\r\n\x1a\n", "quote.png", id="png"),
        pytest.param(b"RIFF\x00\x00\x00\x00WAVEfmt ", "quote.png", id="riff-not-webp"),
    ],
)
def test_card_filename_follows_the_format(data: bytes, expected: str) -> None:
    assert qr.card_filename("quote", data) == expected


# Half-size canvas: layout is proportional, and a light's per-frame motion in
# pixels is the same, so the frame-merging guard below still bites.
SMALL = {"width": 450, "height": 250}


@pytest.mark.parametrize("pfp_shape", ["circle", "square", "none"])
def test_still_avatar_gets_drifting_bokeh(pfp_shape: str) -> None:
    data = qr.render_animated_quote_card(TEXT, **_kw(_still(), pfp_shape=pfp_shape, **SMALL))

    assert qr.card_filename("quote", data) == "quote.webp"
    frames = _frames(data)
    # Every frame survives encoding. A soft light moving under a pixel a frame
    # is exactly what libwebp's lossy encoder merges away at lower quality, and
    # the merged frames play back as a stutter (the first cut kept 32 of 48).
    assert len(frames) == qr.BOKEH_FRAMES
    # Rounded card corners stay transparent in every frame.
    assert all(np.asarray(f)[0, 0, 3] == 0 for f in frames)
    # The lights move: frames a quarter-loop apart differ.
    a = np.asarray(frames[0], dtype=np.int16)
    b = np.asarray(frames[len(frames) // 4], dtype=np.int16)
    assert np.abs(a - b).max() > 0


def test_bokeh_replaces_a_still_picture() -> None:
    """A still avatar no longer tints the background — the bokeh is warm gold."""
    data = qr.render_animated_quote_card(
        "", **_kw(_still(color=(0, 255, 0)), pfp_shape="none", author_name="", **SMALL),
    )
    frame = np.asarray(_frames(data)[0], dtype=np.int32)
    h, w = frame.shape[:2]
    patch = frame[h // 3: 2 * h // 3, w // 4: w // 2, :3].reshape(-1, 3).mean(axis=0)
    red, green, blue = patch
    assert red >= green >= blue


def test_bokeh_stays_inside_the_gold_frame_line() -> None:
    w, h = 450, 254
    geo = qr._CardGeometry(
        width=w, height=h, bg_offset_x=0, pfp_pos=None, pfp_size=0, pfp_mask=None,
        slim_frame=True,
    )
    frames = [
        np.asarray(f, dtype=np.int16)
        for f in qr._bokeh_backgrounds(geo, qr.THEMES["midnight"], seed=5)
    ]
    inset, _, line_w = qr._slim_frame_line(w, h)
    margin = np.zeros((h, w), dtype=bool)
    margin[:inset] = margin[-inset:] = True
    margin[:, :inset] = margin[:, -inset:] = True
    inner = ~margin
    inner[: inset + line_w + 4] = inner[-(inset + line_w + 4):] = False
    # The margin between the card edge and the gold line never changes …
    assert max(int(np.abs(f - frames[0])[margin].max()) for f in frames) == 0
    # … while the lights move inside it.
    assert max(int(np.abs(f - frames[0])[inner].max()) for f in frames) > 10


def test_bokeh_arrangement_is_seeded() -> None:
    geo = qr._CardGeometry(
        width=200, height=120, bg_offset_x=0, pfp_pos=None, pfp_size=0, pfp_mask=None,
    )
    theme = qr.THEMES["midnight"]

    def first(seed: int) -> bytes:
        return next(qr._bokeh_backgrounds(geo, theme, seed=seed)).tobytes()

    assert first(1) == first(1)
    assert first(1) != first(2)


def test_animated_avatar_plays_in_the_avatar_box() -> None:
    red, blue = (220, 30, 30), (30, 30, 220)
    data = qr.render_animated_quote_card(TEXT, **_kw(_gif([red, blue], duration=200)))

    frames = _frames(data)
    assert len(frames) == 2
    w, h = frames[0].size
    # Default layout: avatar disc centred at (0.18w, h/2).
    cx, cy = int(w * 0.18), h // 2
    r0, _, b0, _ = (int(v) for v in np.asarray(frames[0])[cy, cx])
    r1, _, b1, _ = (int(v) for v in np.asarray(frames[1])[cy, cx])
    assert r0 > 150 > b0
    assert b1 > 150 > r1


def test_source_frames_sample_long_animations_and_keep_their_speed() -> None:
    n = 150
    gif = _gif([(i, 0, 0) for i in range(n)], duration=30, size=8)

    frames = qr._source_frames(Image.open(io.BytesIO(gif)))

    assert len(frames) == math.ceil(n / math.ceil(n / qr.ANIM_MAX_FRAMES))
    assert len(frames) <= qr.ANIM_MAX_FRAMES
    assert sum(ms for _, ms in frames) == n * 30


def test_source_frames_of_a_still_is_one_frame() -> None:
    frames = qr._source_frames(Image.open(io.BytesIO(_still())))
    assert len(frames) == 1


@pytest.mark.parametrize("pfp_shape", ["circle", "square", "none"])
def test_matte_layer_reproduces_the_still_card(pfp_shape: str, monkeypatch) -> None:
    """Background + avatar + the recovered layer == the card composed directly.

    Without an avatar the name is the heading, in the panel titles' petal
    colours: a see-through glow the matte has to recover as faithfully as it
    did the flat tint.
    """
    petal_headers: list[str] = []
    draw_petal = qr._draw_petal_header

    def _spy(bg, xy, text, **kw):
        petal_headers.append(text)
        draw_petal(bg, xy, text, **kw)

    monkeypatch.setattr(qr, "_draw_petal_header", _spy)
    kw = {
        "author_name": "Poppy", "avatar_bytes": _still(), "theme": qr.THEMES["midnight"],
        "font_style": "inter", "header_font_style": "helvetica", "border_style": None,
        "width": 900, "height": 500, "custom_emojis": None, "pfp_shape": pfp_shape,
    }
    real, geo = qr._compose_card(TEXT, **kw)
    on_black, _ = qr._compose_card(TEXT, fill=(0, 0, 0), **kw)
    on_white, _ = qr._compose_card(TEXT, fill=(255, 255, 255), **kw)
    assert petal_headers == (["Poppy"] * 3 if pfp_shape == "none" else [])

    rebuilt = qr._build_background(_still(), geo.width, geo.height, qr.THEMES["midnight"],
                                   offset_x=geo.bg_offset_x)
    if geo.pfp_pos is not None:
        pfp = qr._fit_pfp_frame(Image.open(io.BytesIO(_still())), geo.pfp_size)
        rebuilt.paste(pfp, geo.pfp_pos, mask=geo.pfp_mask)
    rebuilt = rebuilt.convert("RGBA")
    rebuilt.alpha_composite(qr._matte(on_black, on_white))

    inside = np.asarray(real)[..., 3] == 255
    diff = np.abs(
        np.asarray(real, dtype=np.int16)[..., :3] - np.asarray(rebuilt, dtype=np.int16)[..., :3]
    )
    assert diff[inside].max() <= 3


def test_render_failure_posts_the_still_card(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(qr, "_render_animated", _boom)
    data = qr.render_animated_quote_card(TEXT, **_kw(_still(), **SMALL))
    assert qr.card_filename("quote", data) == "quote.png"


def test_oversized_animation_posts_the_still_card(monkeypatch) -> None:
    monkeypatch.setattr(qr, "ANIM_MAX_BYTES", 10)
    data = qr.render_animated_quote_card(TEXT, **_kw(_still(), **SMALL))
    assert qr.card_filename("quote", data) == "quote.png"


def test_vignette_matches_the_per_pixel_formula() -> None:
    """The numpy vignette is the old per-pixel loop, so still cards are unchanged."""
    w, h, s = 37, 21, 0.55
    got = np.asarray(qr._vignette_mask(w, h, s))
    cx, cy = w / 2, h / 2
    max_r = math.hypot(cx, cy)
    for y in range(h):
        for x in range(w):
            r = math.hypot(x - cx, y - cy) / max_r
            assert abs(int(got[y, x]) - min(255, int(s * r * r * 255))) <= 1
