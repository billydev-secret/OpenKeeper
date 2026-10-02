"""The banner's box: its layout, its frame and its dots (Ben, 2026-09-29).

Geometry only, against a fixed-width stand-in for a face, so nothing here
needs a font file: every character is ``size // 2`` wide and capitals are
``0.7 × size`` tall.
"""

from __future__ import annotations

import math
import sys

import pytest

from bot_modules.shared import quote_box as qb
from tests.paths import REPO_ROOT

NOTICE = (
    "Welcome to the meadow. Read the rules, pick your roles, and say hello in "
    "the lounge. Staff are around most evenings if you need a hand finding "
    "your way."
)


def _body(size: int) -> qb.BodyMetrics:
    return qb.BodyMetrics(lambda t: len(t) * (size // 2), round(size * 0.7))


def _title(text: str, size: int) -> tuple[int, int]:
    return len(text) * (size // 2), round(size * 0.7)


def _layout(text: str, title: str = "", width: int = 900) -> qb.BoxLayout:
    return qb.layout(text, title, width=width, body=_body, title_metrics=_title)


def _measure(text: str) -> int:
    return len(text) * 10


# ── layout ───────────────────────────────────────────────────────────────────


def test_a_short_notice_is_set_at_full_size():
    lay = _layout("Game night starts at 8.")
    assert lay.body_size == 62  # up from the old card's 47
    assert lay.pitch == round(62 * qb.LINE_HEIGHT)
    assert not lay.truncated


def test_the_card_is_as_tall_as_its_text():
    one, many = _layout("Doors at 8."), _layout(NOTICE)
    assert len(one.lines) == 1 and len(many.lines) > 3
    assert many.height - one.height == (len(many.lines) - 1) * many.pitch
    assert many.body_size == one.body_size


@pytest.mark.parametrize(
    ("title", "above", "below"),
    [
        pytest.param("", 72, 66, id="untitled"),
        pytest.param("Tonight", 62, 70, id="titled"),
    ],
)
def test_padding_inside_the_frame(title, above, below):
    lay = _layout("Doors at 8.", title)
    stroke = qb.px(qb.FRAME_STROKE, 900)
    cap = round(lay.body_size * 0.7)
    if title:
        title_cap = round(lay.title_size * 0.7)
        assert lay.title_baseline - title_cap == stroke + above
        assert lay.baselines[0] - cap - lay.title_baseline == 44
    else:
        assert lay.title_baseline == 0
        assert lay.baselines[0] - cap == stroke + above
    assert lay.height - lay.baselines[-1] == below + stroke


def test_the_space_above_a_title_matches_the_space_under_the_words():
    # Ben's call on the first sample. Under the last line there is
    # PAD_BOTTOM less the descenders, or all of it; the title's gap sits
    # between the two. The 80 px it replaced fails here.
    lay = _layout("Doors at 8.", "Tonight")
    stroke = qb.px(qb.FRAME_STROKE, 900)
    above = lay.title_baseline - round(lay.title_size * 0.7) - stroke
    below = lay.height - stroke - lay.baselines[-1]
    descent = round(lay.body_size * 0.21)  # Belleza's, as a share of its size
    assert below - descent <= above <= below


def test_an_untitled_card_sits_lower_than_centre_on_purpose():
    # Descenders hang into the bottom padding, so equal numbers read
    # top-heavy; reverting to one constant fails here.
    assert qb.PAD_UNTITLED_TOP > qb.PAD_UNTITLED_BOTTOM


def test_a_long_notice_shrinks_before_it_loses_a_word():
    text = " ".join(["meadow"] * 40)
    lay = _layout(text)
    assert 52 <= lay.body_size < 62
    assert not lay.truncated
    assert " ".join(lay.lines) == text


def test_at_the_floor_a_notice_is_cut_at_a_word():
    text = " ".join(f"word{i:02d}" for i in range(80))
    lay = _layout(text)
    assert lay.body_size == 52  # still larger than the old card's 47
    assert lay.truncated
    last = lay.lines[-1]
    assert last.endswith("…")
    assert all(w.startswith("word") and len(w) == 6 for w in last[:-1].split())
    limit = 900 - 2 * (qb.px(qb.FRAME_STROKE, 900) + qb.px(qb.SIDE_PAD, 900))
    assert _body(52).measure(last) <= limit


@pytest.mark.parametrize("words", [1, 12, 40, 80, 200])
def test_a_card_is_never_taller_than_it_is_wide(words):
    lay = _layout(" ".join(["meadow"] * words), "Tonight")
    assert lay.height <= lay.width * qb.MAX_ASPECT


def test_every_line_fits_between_the_side_paddings():
    lay = _layout(NOTICE, "Start Here")
    limit = 900 - 2 * (qb.px(qb.FRAME_STROKE, 900) + qb.px(qb.SIDE_PAD, 900))
    assert limit == 708
    assert all(_body(lay.body_size).measure(line) <= limit for line in lay.lines)


def test_a_long_title_shrinks_to_the_measure():
    lay = _layout("Doors at 8.", "The Golden Meadow Casino Grand Reopening Gala")
    assert lay.title_size < 84
    assert _title(lay.title, lay.title_size)[0] <= 708


def test_a_title_too_long_at_the_floor_is_cut_at_a_word_not_drawn_into_the_frame():
    # Past the smallest size a title is one line that cannot wrap: without the
    # cut it ran off both sides of the card.
    title = " ".join(["meadow"] * 40)
    lay = _layout("Doors at 8.", title)
    assert lay.truncated
    assert lay.title.endswith("\u2026")
    assert _title(lay.title, lay.title_size)[0] <= 708
    assert not _layout("Doors at 8.", "Tonight").truncated


def test_a_short_title_is_set_at_full_size():
    assert _layout("Doors at 8.", "  Tonight ").title_size == 84
    assert _layout("Doors at 8.", "  Tonight ").title == "Tonight"


def test_the_layout_scales_with_the_cards_width():
    big, small = _layout(NOTICE, "Start Here"), _layout(NOTICE, "Start Here", width=450)
    assert small.body_size == big.body_size // 2
    assert abs(small.height * 2 - big.height) <= 12
    assert len(small.lines) == len(big.lines)


# ── wrapping ─────────────────────────────────────────────────────────────────


def test_balanced_keeps_the_greedy_line_count_and_every_word():
    words = NOTICE.split()
    widths = [_measure(w) for w in words]
    greedy = qb.wrap(words, widths, 10, 400)
    lines = qb.balanced(NOTICE, _measure, 400)
    assert len(lines) == len(greedy)
    assert " ".join(lines) == NOTICE
    assert all(_measure(line) <= 400 for line in lines)


def test_balanced_leaves_no_orphan():
    text = "Game night starts at eight tonight so bring snacks please"
    greedy = qb.wrap(text.split(), [_measure(w) for w in text.split()], 10, 500)
    assert len(greedy[-1]) == 1  # the greedy wrap strands "please"
    lines = qb.balanced(text, _measure, 500)
    assert len(lines) == 2
    assert min(len(line.split()) for line in lines) >= 3


def test_balanced_keeps_paragraphs_and_blank_lines():
    assert qb.balanced("one two\n\nthree", _measure, 400) == ["one two", "", "three"]
    assert qb.balanced("", _measure, 400) == [""]


def test_a_word_wider_than_the_measure_gets_its_own_line():
    lines = qb.balanced("a " + "x" * 60 + " b", _measure, 400)
    assert "x" * 60 in lines


@pytest.mark.parametrize(
    ("line", "limit", "expected"),
    [
        pytest.param("bring a friend, and", 400, "bring a friend, and…", id="fits"),
        pytest.param("bring a friend, and", 190, "bring a friend…", id="drops-a-word"),
        pytest.param("bring a friend.", 400, "bring a friend…", id="no-doubled-stop"),
        pytest.param("supercalifragilistic", 100, "supercali…", id="one-long-word"),
    ],
)
def test_cut_to_fit(line, limit, expected):
    cut = qb.cut_to_fit(line, _measure, limit)
    assert cut == expected
    assert _measure(cut) <= limit


# ── dots ─────────────────────────────────────────────────────────────────────

WORDS = [qb.Box(200, 150, 700, 210), qb.Box(150, 230, 750, 290)]


def _gap(dot: qb.Dot, box: qb.Box) -> float:
    dx = max(box.left - dot.x, 0.0, dot.x - box.right)
    dy = max(box.top - dot.y, 0.0, dot.y - box.bottom)
    return math.hypot(dx, dy)


def test_scatter_is_the_same_for_the_same_seed():
    a = qb.scatter(900, 500, seed=7, keep_clear=WORDS)
    assert a == qb.scatter(900, 500, seed=7, keep_clear=WORDS)
    assert a != qb.scatter(900, 500, seed=8, keep_clear=WORDS)


@pytest.mark.parametrize("seed", range(6))
def test_no_dot_sits_on_the_words(seed):
    dots = qb.scatter(900, 500, seed=seed, keep_clear=WORDS)
    stars = qb.star_count(900, 500)
    for dot in dots:
        away = min(_gap(dot, box) for box in WORDS)
        assert away >= qb.DOT_CLEAR * 900 + dot.radius
        if away < qb.DOT_HALO * 900:
            assert dot.order >= stars, "a star beside the words"
            assert dot.radius <= 2.2 and dot.peak <= 0.5


@pytest.mark.parametrize("seed", range(6))
def test_dots_stay_on_the_card_and_apart(seed):
    dots = qb.scatter(900, 500, seed=seed, keep_clear=WORDS)
    assert len(dots) > 40
    for i, dot in enumerate(dots):
        assert 24 <= dot.x <= 876 and 24 <= dot.y <= 476
        for other in dots[i + 1:]:
            assert math.hypot(dot.x - other.x, dot.y - other.y) >= 30


@pytest.mark.parametrize("seed", range(6))
def test_each_corner_has_a_star(seed):
    dots = qb.scatter(900, 700, seed=seed, keep_clear=WORDS)
    corners = {(d.x < 450, d.y < 350) for d in dots[:4]}
    assert len(corners) == 4
    assert all(d.radius >= 4.6 and d.peak >= 0.8 for d in dots[:4])


@pytest.mark.parametrize(
    ("height", "stars"),
    [
        pytest.param(272, 5, id="short-card"),
        pytest.param(387, 8, id="two-lines"),
        pytest.param(711, 10, id="long-card"),
        pytest.param(100, 4, id="never-under-four"),
    ],
)
def test_star_count_follows_the_cards_area(height, stars):
    assert qb.star_count(900, height) == stars
    assert qb.star_count(450, height // 2) == qb.star_count(900, height // 2 * 2)


def test_scatter_scales_with_the_cards_width():
    small = qb.scatter(450, 250, seed=3)
    assert len(small) == len(qb.scatter(900, 500, seed=3))
    assert max(d.radius for d in small) <= 3.0


@pytest.mark.parametrize("index", range(9))
def test_the_twinkle_loops_without_a_seam(index):
    values = [qb.twinkle(index, f) for f in range(qb.FRAMES + 1)]
    assert values[0] == pytest.approx(values[-1])
    assert 0.25 <= min(values) and max(values) <= 1.0
    assert max(values) - min(values) > 0.6


# ── pixels ───────────────────────────────────────────────────────────────────


def test_the_frame_runs_at_the_pictures_edge_on_all_four_sides():
    w, h = 900, 400
    frame = qb.frame_layer(w, h)
    stroke = qb.px(qb.FRAME_STROKE, w)
    assert stroke == 6
    for xy in [(w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]:
        assert frame.getpixel(xy)[3] >= 250, f"no line at {xy}"
    for xy in [(w // 2, stroke + 1), (stroke + 1, h // 2), (w // 2, h // 2)]:
        assert frame.getpixel(xy)[3] <= 5, f"the line is too wide at {xy}"


def test_the_frame_is_yellow_in_the_middle_and_orange_at_the_sides():
    frame = qb.frame_layer(900, 400)
    assert frame.getpixel((450, 2))[:3] == pytest.approx(qb.FRAME_GOLD_MID, abs=2)
    assert frame.getpixel((2, 200))[:3] == pytest.approx(qb.FRAME_GOLD_EDGE, abs=2)


def test_the_frame_is_drawn_once_and_handed_out_as_copies():
    # The animated renderer composes a card twice; the second frame is free.
    # A caller that scribbles on its copy must not mark the next card's.
    first = qb.frame_layer(320, 200)
    first.paste((255, 0, 0, 255), (0, 0, 320, 200))
    second = qb.frame_layer(320, 200)
    assert second.getpixel((160, 100))[3] == 0
    mask = qb.card_mask(320, 200)
    mask.paste(0, (0, 0, 320, 200))
    assert qb.card_mask(320, 200).getpixel((160, 100)) == 255
    assert qb._frame_layer.cache_info().hits >= 1


def test_the_cards_corners_are_cut():
    mask = qb.card_mask(900, 400)
    assert mask.getpixel((0, 0)) == 0 and mask.getpixel((899, 399)) == 0
    assert mask.getpixel((450, 200)) == 255
    radius = qb.px(qb.FRAME_RADIUS, 900)
    assert radius == 46  # the panel art's 62 at 1200 wide
    assert mask.getpixel((radius, 0)) >= 250


def test_the_field_is_black_but_for_its_dots():
    dots = qb.scatter(900, 400, seed=5)
    still = qb.field(900, 400, dots)
    assert still.mode == "RGB" and still.size == (900, 400)
    lit = [still.getpixel((round(d.x), round(d.y))) for d in dots[:4]]
    # A star at the bottom of its twinkle is a quarter of its peak.
    assert all(px[0] > 40 and px[0] >= px[1] > px[2] for px in lit), lit
    assert still.getpixel((3, 3)) == (0, 0, 0)
    assert qb.field(900, 400, ()).getextrema() == ((0, 0), (0, 0), (0, 0))


def test_a_dot_at_the_cards_edge_is_clipped_not_dropped():
    edge = (qb.Dot(1.0, 1.0, 5.0, 1.0, 0), qb.Dot(899.0, 399.0, 5.0, 1.0, 1))
    still = qb.field(900, 400, edge)
    assert still.getpixel((1, 1))[0] > 60 and still.getpixel((898, 398))[0] > 60


def test_the_loop_is_one_field_per_frame_and_they_differ():
    dots = qb.scatter(300, 200, seed=5)
    frames = list(qb.backgrounds(300, 200, dots))
    assert len(frames) == qb.FRAMES == 34
    assert frames[0].tobytes() == qb.field(300, 200, dots, 0).tobytes()
    assert len({f.tobytes() for f in frames}) == qb.FRAMES


def test_the_box_matches_the_panel_art_generator():
    """The owner asked for the panel art's look, so the two may not drift."""
    scripts = REPO_ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    art = pytest.importorskip("make_panel_art")

    assert qb.FRAME_RADIUS == art.RADIUS / art.W
    assert qb.FRAME_STROKE == art.STROKE / art.W
    assert qb.FRAME_GOLD_MID == tuple(int(c) for c in art._MID)
    assert qb.FRAME_GOLD_EDGE == tuple(int(c) for c in art._EDGE)
    assert qb.DOT_GOLD == tuple(int(c) for c in art.GOLD)
    assert (qb.FRAMES, qb.FRAME_MS) == (art.FRAMES, art.FRAME_MS)
    assert art.SIDE_INSET == 0  # the art's frame is at the edge, as the box's is
    for i in range(7):
        for t in (0, 5, 17):
            assert qb.twinkle(i, t) == pytest.approx(art.twinkle(i, t, art.FRAMES))


def test_a_word_wider_than_the_card_is_cut_not_drawn_into_the_frame():
    lay = _layout("see https://example.com/" + "a" * 90)
    side = qb.px(qb.FRAME_STROKE, 900) + qb.px(qb.SIDE_PAD, 900)
    assert lay.body_size == qb.px(qb.BODY_FLOOR_PX, 900)
    assert all(_body(lay.body_size).measure(line) <= 900 - 2 * side for line in lay.lines)
    assert any(line.endswith("…") for line in lay.lines)
    assert lay.truncated
