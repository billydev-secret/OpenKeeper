"""Pad wide doc images to portrait so the desktop client shows the embed full width.

Discord's desktop app sizes an embed to its image: a square or landscape image
renders the whole embed visibly narrower than a portrait one. Measured on
2026-09-12 against the staff bios — a 1:1 image rendered narrow, the same image
padded to 3:4 or 2:3 rendered full width, whatever text sat above it. The phone
apps stretch every embed to the screen, so padding changes nothing there.

Pure bytes-in/bytes-out (no web or discord imports) so it is unit-testable.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

# Target canvas height:width. 3:4 is the shortest portrait verified full width.
_TARGET_H, _TARGET_W = 4, 3
# A phone photo a pixel off 3:4 (e.g. 1265x1686) is left alone rather than
# padded by a sliver; it already renders full width.
_TOLERANCE = 0.01
# Discord never displays an embed image much past ~550px wide. Capping the
# canvas keeps a large landscape photo, re-encoded losslessly with an alpha
# channel, from ballooning past the upload limit.
_MAX_WIDTH = 1536


def needs_padding(width: int, height: int) -> bool:
    """True when an image is wider than 3:4 portrait (square and landscape included)."""
    return height / width < _TARGET_H / _TARGET_W - _TOLERANCE


def pad_to_portrait(data: bytes, ext: str) -> tuple[bytes, str] | None:
    """Return ``(padded_bytes, ext)`` for an image wider than 3:4, else None.

    None means "store the upload exactly as received": the image is already
    portrait, it is animated (flattening one would kill the animation — a GIF
    or an animated WEBP alike), or Pillow can't decode it. The image is centred
    on a transparent canvas — nothing is cropped — after EXIF orientation is
    applied, so a sideways-stored phone photo is measured the way it displays.
    Transparency needs an alpha channel, so a JPG comes back as PNG; WEBP stays
    WEBP.
    """
    if ext == "gif":
        return None
    try:
        with Image.open(io.BytesIO(data)) as src:
            if getattr(src, "is_animated", False):
                return None
            img = ImageOps.exif_transpose(src)
            width, height = img.size
            if not needs_padding(width, height):
                return None
            img = img.convert("RGBA")
    except Exception:  # noqa: BLE001 — Pillow's corrupt-upload raise set is open-ended (see comment below); must fall back, never 500
        # "Pillow can't decode it" covers more than the well-behaved cases
        # above — malformed EXIF and other odd-but-real uploads have raised
        # ValueError/struct.error/SyntaxError from deep inside Pillow. Any of
        # those should fall back to storing the upload untouched, not 500 the
        # request that used to succeed unconditionally before this padding
        # step existed.
        return None

    img.thumbnail((_MAX_WIDTH, _MAX_WIDTH))
    width, height = img.size
    canvas_h = -(-width * _TARGET_H // _TARGET_W)  # ceil: never shorter than 3:4
    canvas = Image.new("RGBA", (width, canvas_h), (0, 0, 0, 0))
    canvas.paste(img, (0, (canvas_h - height) // 2))

    out_ext = "webp" if ext == "webp" else "png"
    buf = io.BytesIO()
    if out_ext == "webp":
        canvas.save(buf, "WEBP", quality=90)
    else:
        canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue(), out_ext
