"""The panel-art generator's open, faded edge (Ben, 2026-09-28: "fade into the
message content instead of looking like a cheeseburger"), on synthetic art —
the poppy source lives in assets/, which the remote runner doesn't have."""

from __future__ import annotations

import sys

import numpy as np
import pytest
from tests.paths import REPO_ROOT

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import make_panel_art as art  # noqa: E402


@pytest.mark.parametrize("side", ["top", "bottom"])
def test_fade_ramp_reaches_transparent_over_the_open_tenth(side):
    ramp = art.fade_ramp(100, side)
    edge, far = (ramp[0], ramp[-1]) if side == "top" else (ramp[-1], ramp[0])
    assert edge < 0.1 and far == 1.0
    solid = np.nonzero(ramp == 1.0)[0]
    assert len(solid) == 90  # only the last 10% fades


@pytest.mark.parametrize(
    ("open_side", "open_row", "closed_row"),
    [("bottom", -1, 14), ("top", 0, -15)],
)
def test_frame_has_no_stroke_on_its_open_side(open_side, open_row, closed_row):
    h = 120
    outer = art.frame_shape(h, art.INSET, art.RADIUS, open_side)
    inner = art.frame_shape(
        h, art.INSET + art.STROKE, art.RADIUS - art.STROKE, open_side
    )
    stroke = outer - inner
    mid = art.W // 2
    assert stroke[open_row, mid] < 0.01, "a gold line on the open side"
    assert inner[open_row, mid] > 0.99, "the black runs to the open edge"
    assert stroke[closed_row, mid] > 0.99, "the closed side keeps its line"


def test_open_bottom_lifts_the_banners_baked_line_and_fades_it():
    # A banner-shaped still: black inside side strokes, a gold bottom line
    # at rows 40-46 and a transparent margin under it, as Billy's art has.
    h, w = 60, 200
    still = np.zeros((h, w, 4), np.float32)
    still[:40, 10:190] = (0, 0, 0, 255)
    still[:47, 2:10] = still[:47, 190:198] = (240, 160, 30, 255)
    still[40:47, 2:198] = (240, 160, 30, 255)
    art.open_bottom(still)
    mid = still[:, w // 2]
    assert mid[40:, :3].max() < 40, "the gold bottom line is gone"
    assert still[-1, :, 3].max() < 0.1 * 255, "the bottom edge is transparent"
    assert still[30, w // 2, 3] == 255 and still[30, 5, 3] == 255  # above the fade
    assert still[50, 5, :3].max() > 200, "the side stroke runs on down"


@pytest.mark.parametrize(("open_side", "y"), [("top", 30), ("bottom", 90)])
def test_the_frames_sides_are_the_pictures_sides(open_side, y):
    """Discord starts a panel's text at the picture's left edge and ends its
    buttons at the right one, with no padding to give either; a frame drawn
    inside the picture has both overhanging it (Ben, 2026-09-29)."""
    (frame,) = art.animate(
        np.zeros((120, art.W, 3), np.float32), [], open_side, sigma=0, frames=1
    )
    row = np.asarray(frame)[y]  # clear of the corners and the faded edge
    for x in (0, art.STROKE - 1, art.W - art.STROKE, art.W - 1):
        assert row[x, 3] == 255 and row[x, 0] > 200, f"no gold stroke at x={x}"
    assert row[art.STROKE + 2, :3].max() < 40, "black starts inside the stroke"


def _inset_banner(w: int = 200, h: int = 60, margin: int = 4):
    from PIL import Image

    still = np.zeros((h, w, 4), np.uint8)
    still[:, margin : w - margin] = (0, 0, 0, 255)
    still[:, margin : margin + 6] = still[:, w - margin - 6 : w - margin] = (
        240, 160, 30, 255,
    )
    return Image.fromarray(still, "RGBA")


def test_to_edge_moves_a_baked_frame_out_and_keeps_the_size():
    src = _inset_banner()
    (out,) = art.to_edge([src])
    assert out.size == src.size, "the title's place depends on the height"
    px = np.asarray(out)
    for x in (0, -1):
        assert px[30, x, 3] == 255 and px[30, x, 0] > 200, "gold at the very edge"
    (again,) = art.to_edge([out])
    assert again is out, "a frame already at the edge is left alone"


def _write(frames, path, ms):
    # One soft pixel, as any antialiased art has: Pillow reads an animated
    # WebP whose alpha is all 0 or 255 back as RGB, margin and all.
    frames[0].putpixel((1, 1), (10, 10, 10, 128))
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=ms, loop=0, lossless=True
    )
    return path


def test_edge_keeps_the_sources_own_pace(tmp_path):
    """A banner from another hand has its own frame time; re-saving it at
    this script's would change the speed of someone else's animation."""
    a, b = _inset_banner(), _inset_banner()
    b.putpixel((100, 30), (255, 255, 255, 255))  # two frames that differ
    frames, durations = art.edge(_write([a, b], tmp_path / "in.webp", 140))
    assert durations == [140, 140]
    assert [f.size for f in frames] == [a.size, a.size]
    assert frames[0].getpixel((0, 30))[3] == 255


def test_edge_refuses_a_picture_with_nothing_to_move(tmp_path):
    """An opaque background, or a frame already at the edge: the same file
    written back would read as done while the frame is still where it was."""
    done = art.to_edge([_inset_banner()])
    src = _write(done, tmp_path / "done.webp", 90)
    with pytest.raises(ValueError, match="nothing to move"):
        art.edge(src)


def test_scatter_refuses_more_dots_than_fit():
    with pytest.raises(SystemExit, match="dots fit"):
        art.scatter(60, 200, 1, lambda x, y: False)


def test_lobby_strip_matches_the_title_geometry():
    # The bot centres lobby titles on LOBBY's fraction of the strip; a strip
    # regenerated at another height or cap line would mis-centre every title.
    from bot_modules.panels.title_service import LOBBY

    assert LOBBY.cap_centre_of_height == art.LOBBY_CAP_CENTRE / art.LOBBY_H


def test_footer_and_lobby_header_are_the_same_height(monkeypatch):
    # Ben, 2026-09-29: "make the small header banner the same height as the
    # footer". One constant sets both; a strip regenerated at its old height
    # (200 footer, 108 header) fails here.
    assert art.LOBBY_H == art.STRIP_H == 160
    # The poppy art the sprigs are cut from lives in assets/: stand one in.
    monkeypatch.setattr(
        art, "sprig", lambda fade_stem: np.full((137, 240, 3), 90, np.float32)
    )
    foot = art.footer(0, 1)
    head = art.lobby(0, 1)
    assert [f.size for f in foot] == [f.size for f in head] == [(art.W, 160)]


def test_lobby_capitals_centre_between_the_frame_and_the_open_edge():
    inside = art.INSET + art.STROKE
    assert art.LOBBY_CAP_CENTRE - inside == art.LOBBY_H - art.LOBBY_CAP_CENTRE


@pytest.mark.parametrize(
    ("open_side", "closed_row", "side_row"), [("bottom", 12, 60), ("top", -13, 20)]
)
def test_card_strip_is_frame_and_dots_at_its_own_height(open_side, closed_row, side_row):
    """The card strips (Ben, 2026-09-30): thin, open on the card's side, the
    frame's sides at the picture's sides, and the dots inside the frame."""
    (frame,) = art.card(sigma=0, frames=1, open_side=open_side, h=80)
    px = np.asarray(frame)
    assert px.shape == (80, art.W, 4)
    open_row = -1 if open_side == "bottom" else 0
    assert px[open_row, :, 3].max() < 0.1 * 255, "the open side fades out"
    assert px[closed_row, art.W // 2, 0] > 200, "the closed side keeps its line"
    for x in (0, art.W - 1):
        # Clear of the rounded corners and of the faded edge.
        row = px[side_row, x]
        assert row[3] == 255 and row[0] > 200, f"no gold side at x={x}"


def test_card_dots_keep_clear_of_the_closed_stroke():
    dots = art.scatter(80, 13, 13, lambda x, y: False, margin=24)
    assert len(dots) == 13
    assert all(24 <= y <= 56 for _x, y, *_ in dots)
