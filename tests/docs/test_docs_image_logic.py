"""Doc image padding: wide uploads become 3:4 portrait, everything else is untouched."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bot_modules.docs.image_logic import needs_padding, pad_to_portrait

_RED = (200, 30, 40, 255)


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _solid(w: int, h: int, fmt: str, **kw) -> bytes:
    mode = "RGB" if fmt == "JPEG" else "RGBA"
    return _encode(Image.new(mode, (w, h), _RED[: len(mode)]), fmt, **kw)


@pytest.mark.parametrize(
    ("w", "h", "fmt", "ext", "out_ext", "out_size"),
    [
        pytest.param(1254, 1254, "PNG", "png", "png", (1254, 1672), id="square-png"),
        pytest.param(
            900, 600, "JPEG", "jpg", "png", (900, 1200), id="landscape-jpg-to-png"
        ),
        pytest.param(
            300, 300, "WEBP", "webp", "webp", (300, 400), id="square-webp-stays-webp"
        ),
        pytest.param(301, 301, "PNG", "png", "png", (301, 402), id="height-rounds-up"),
        pytest.param(3000, 2000, "PNG", "png", "png", (1536, 2048), id="width-capped"),
    ],
)
def test_wide_images_pad_to_three_by_four(w, h, fmt, ext, out_ext, out_size):
    result = pad_to_portrait(_solid(w, h, fmt), ext)
    assert result is not None
    data, got_ext = result
    assert got_ext == out_ext
    with Image.open(io.BytesIO(data)) as out:
        assert out.size == out_size


def test_padding_centres_the_image_on_transparent_bars():
    data, _ = pad_to_portrait(_solid(300, 300, "PNG"), "png")
    with Image.open(io.BytesIO(data)) as out:
        rgba = out.convert("RGBA")
        assert rgba.getpixel((150, 200)) == _RED  # picture intact in the middle
        assert rgba.getpixel((150, 0))[3] == 0  # top bar transparent
        assert rgba.getpixel((150, 399))[3] == 0  # bottom bar transparent
        assert rgba.getpixel((0, 50)) == _RED  # first picture row, nothing cropped
        assert rgba.getpixel((0, 349)) == _RED  # last picture row


@pytest.mark.parametrize(
    ("w", "h"),
    [
        pytest.param(1206, 1764, id="portrait"),
        pytest.param(300, 400, id="exactly-3x4"),
        pytest.param(1265, 1686, id="a-pixel-off-3x4"),
        pytest.param(1215, 2160, id="9x16"),
    ],
)
def test_portrait_images_are_left_alone(w, h):
    assert not needs_padding(w, h)
    assert pad_to_portrait(_solid(w, h, "JPEG"), "jpg") is None


def test_gif_is_never_padded():
    # Re-encoding would flatten an animation to its first frame.
    assert pad_to_portrait(_solid(400, 300, "GIF"), "gif") is None


def test_animated_webp_is_never_padded():
    # Same reasoning as GIF: flattening would kill the animation.
    frames = [
        Image.new("RGBA", (300, 300), _RED),
        Image.new("RGBA", (300, 300), (0, 255, 0, 255)),
    ]
    buf = io.BytesIO()
    frames[0].save(
        buf, "WEBP", save_all=True, append_images=frames[1:], duration=200, loop=0
    )
    assert pad_to_portrait(buf.getvalue(), "webp") is None


def test_undecodable_bytes_keep_the_upload():
    assert pad_to_portrait(b"\xff\xd8\xff not really a jpeg", "jpg") is None


def test_pillow_decode_error_keeps_the_upload(monkeypatch):
    # A malformed-but-sniffable upload can raise something other than the
    # three exception types Pillow itself documents (e.g. bad EXIF triggers
    # ValueError/struct.error deep inside the library) — any decode failure
    # must fall back to storing the upload untouched, never 500 the request.
    import bot_modules.docs.image_logic as image_logic

    def _boom(*a, **k):
        raise ValueError("simulated malformed EXIF")

    monkeypatch.setattr(image_logic.ImageOps, "exif_transpose", _boom)
    assert pad_to_portrait(_solid(900, 600, "JPEG"), "jpg") is None


def _with_orientation(w: int, h: int, orientation: int) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    return _encode(Image.new("RGB", (w, h), _RED[:3]), "JPEG", exif=exif)


def test_sideways_stored_portrait_photo_is_measured_as_displayed():
    # Stored 400x300 but EXIF-rotated 90° — it displays as a 300x400 portrait.
    assert pad_to_portrait(_with_orientation(400, 300, 6), "jpg") is None


def test_sideways_stored_landscape_photo_is_padded_upright():
    # Stored 300x400 but EXIF-rotated — it displays as 400x300 landscape.
    data, _ = pad_to_portrait(_with_orientation(300, 400, 6), "jpg")
    with Image.open(io.BytesIO(data)) as out:
        assert out.size == (400, 534)
