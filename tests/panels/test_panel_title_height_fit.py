"""A title on art shorter than its geometry was set on (bug #274).

Every size in a ``TitleGeometry`` is a fraction of the art's *width*. The
lobby's numbers were grown for the 1200 × 160 strip while the 1200 × 108
upload was still live, so the 116 px name filled the old strip top to bottom
and its capitals ran into the frame's top line ("this R hits the top line,
there should be a little room"). ``_height_scale`` draws everything smaller by
the share of the geometry's height the art actually has.

These run without the licensed faces (the remote runner has no ``assets/``):
the sizes asserted are the ones asked of the font, which do not depend on
which font answers.
"""

from __future__ import annotations

import pytest

from bot_modules.panels import title_service as ts

#: The frame's inside edge on the lobby strip art, in px at either height.
FRAME_INSIDE_TOP = 18


@pytest.mark.parametrize(
    ("size", "geometry", "expected"),
    [
        pytest.param((1200, 160), ts.LOBBY, 1.0, id="lobby-own-height"),
        pytest.param((1200, 108), ts.LOBBY, 108 / 160, id="lobby-old-108-strip"),
        pytest.param((600, 54), ts.LOBBY, 108 / 160, id="lobby-108-at-half-width"),
        pytest.param((1200, 400), ts.LOBBY, 1.0, id="taller-art-is-left-alone"),
        # Deliberate: a banner is any wide picture (the dashboard advises
        # "about 3:1"), so a 1200 × 400 upload keeps its full-size title.
        pytest.param((1200, 520), ts.BANNER, 1.0, id="banner-own-height"),
        pytest.param((1200, 400), ts.BANNER, 1.0, id="banner-3-to-1-is-not-shrunk"),
        pytest.param(
            (1200, 54),
            ts.LOBBY._replace(height_of_width=160 / 1200),
            54 / 160,
            id="explicit-height-scales",
        ),
        pytest.param(
            (1200, 108),
            ts.LOBBY._replace(height_of_width=0.0),
            1.0,
            id="geometry-without-a-height-opts-out",
        ),
        pytest.param((0, 108), ts.LOBBY, 1.0, id="zero-width-does-not-divide"),
    ],
)
def test_height_scale(size, geometry, expected):
    assert ts._height_scale(size, geometry) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("size", "geometry", "type_px"),
    [
        # The regression: the 108 strip gets the 78 px name it was designed
        # with, not the 160 strip's 116.
        pytest.param((1200, 108), ts.LOBBY, 78, id="lobby-108-strip"),
        pytest.param((1200, 160), ts.LOBBY, 116, id="lobby-160-strip-unchanged"),
        pytest.param((1200, 520), ts.BANNER, 121, id="banner-unchanged"),
        pytest.param((1200, 400), ts.BANNER, 121, id="banner-3-to-1-unchanged"),
    ],
)
def test_type_size_follows_the_art_height(size, geometry, type_px):
    font, text = ts._fit(size[0], "Risky", geometry, ts._height_scale(size, geometry))
    assert text == "Risky"
    assert font.size == type_px


def test_title_on_a_short_strip_clears_the_frame():
    """The rendered glyphs leave room under the frame's top line and stay
    inside the strip, on the upload that showed the bug."""
    box = ts._title_mask((1200, 108), "Risky Rolls", ts.LOBBY).getbbox()
    assert box is not None
    assert box[1] >= FRAME_INSIDE_TOP + 4
    assert box[3] <= 108


def test_floor_shrinks_with_the_art():
    """A name too long for the strip is cut at the *scaled* floor — the full
    58 px floor would put an over-tall line back on a 108 strip."""
    long_title = "An Extraordinarily Long Game Name Nobody Would Ever Pick"
    font, text = ts._fit(1200, long_title, ts.LOBBY, 108 / 160)
    assert font.size == round(58 * 108 / 160)
    assert text.endswith("…")
    assert font.getlength(text) <= 1200 * ts.LOBBY.max_width_of_width
