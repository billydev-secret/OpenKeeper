"""Thught on quote cards: the default faces, the picker labels, the fallback.

The runner has no ``assets/``, so the rule is tested against stand-in files and
a stubbed character map; the cases that need the real faces skip without them.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from bot_modules.shared import quote_renderer as qr
from bot_modules.word_cloud import presets

LATIN = frozenset(map(ord, "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZé—“”"))
CYRILLIC = frozenset(map(ord, "Алексей"))


@pytest.fixture
def faces(tmp_path, monkeypatch):
    """Thught present and Latin-only; the fallbacks carry Cyrillic too."""
    maps: dict[Path, frozenset[int]] = {}
    styles = dict(qr.FONT_STYLES)
    for key, cover in {
        "thught": LATIN,
        "thught_black": LATIN,
        "times": LATIN | CYRILLIC | frozenset({0x200B, 0x200D}),
        "helvetica": LATIN | CYRILLIC,
        "bebas": LATIN,
    }.items():
        path = tmp_path / f"{key}.ttf"
        path.write_bytes(b"")
        styles[key] = path
        maps[path] = cover
    monkeypatch.setattr(qr, "FONT_STYLES", styles)
    monkeypatch.setattr(qr, "_codepoints", lambda path: maps.get(path, frozenset()))
    return styles


@pytest.mark.parametrize(
    ("style", "text", "expected"),
    [
        pytest.param("thught", "A plain quote — Céline", "thught", id="covered-stays"),
        pytest.param("thught", "Hello Алексей", "times", id="uncovered-falls-back"),
        pytest.param("thught_black", "Алексей", "times", id="black-falls-back-too"),
        # Neither face draws these, so switching would change the look for nothing.
        pytest.param("thught", "nice \U0001f48b 美咲", "thught", id="nobody-has-it-stays"),
        pytest.param("thught", "", "thught", id="empty-stays"),
        # A joiner or zero-width space is never drawn, so the fallback having it
        # is no reason to switch (a 👩‍💻 would otherwise turn the card to Times).
        pytest.param("thught", "ship it \U0001f469\u200d\U0001f4bb a\u200bb", "thught", id="format-chars-ignored"),
        # Only Thught degrades: every other face keeps the behaviour it shipped with.
        pytest.param("bebas", "Hello Алексей", "bebas", id="other-style-untouched"),
        pytest.param("no-such-style", "Алексей", "no-such-style", id="unknown-untouched"),
    ],
)
def test_resolve_font_style(faces, style, text, expected):
    assert qr.resolve_font_style(style, text, fallback="times") == expected


@pytest.mark.parametrize("style", ["thught", "thught_black"])
def test_a_missing_thught_file_degrades_instead_of_raising(faces, style):
    # Deliberate anomaly (quote_renderer_spec.md § Fonts): every other face
    # fails loudly, but the default must not take every card down with it.
    faces[style].unlink()
    assert qr.resolve_font_style(style, "plain", fallback="helvetica") == "helvetica"


def test_other_faces_still_fail_loudly_when_missing(faces):
    faces["bebas"].unlink()
    assert qr.resolve_font_style("bebas", "plain", fallback="times") == "bebas"
    with pytest.raises(FileNotFoundError):
        qr._load_font(32, "bebas")


def test_an_unreadable_face_has_no_characters(tmp_path):
    junk = tmp_path / "junk.ttf"
    junk.write_bytes(b"<html>not a font</html>")
    assert qr._codepoints(junk) == frozenset()


def test_a_failed_read_is_not_remembered(tmp_path, monkeypatch):
    # One bad read must not hold a face's map empty until restart.
    answers = iter([OSError("busy"), LATIN])

    def read(_path):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(qr, "_read_codepoints", read)
    face = tmp_path / "face.ttf"
    assert qr._codepoints(face) == frozenset()
    assert qr._codepoints(face) == LATIN


def test_a_missing_fonttools_leaves_the_card_in_thught(tmp_path, monkeypatch):
    def read(_path):
        raise ImportError("No module named 'fontTools'")

    monkeypatch.setattr(qr, "_read_codepoints", read)
    styles = dict(qr.FONT_STYLES)
    for key in ("thught", "times"):
        styles[key] = tmp_path / f"{key}.ttf"
        styles[key].write_bytes(b"")
    monkeypatch.setattr(qr, "FONT_STYLES", styles)
    assert qr.resolve_font_style("thught", "Алексей", fallback="times") == "thught"


def test_defaults_are_thught_and_are_offered_in_the_picker():
    assert qr.DEFAULT_FONT_STYLE == "thught"
    assert qr.DEFAULT_HEADER_FONT_STYLE == "thught_black"
    assert qr.FONT_STYLES["thught"].name == "ThughtRegular.ttf"
    assert qr.FONT_STYLES["thught_black"].name == "ThughtBlack.ttf"
    # What a Thught card degrades to must itself be a style.
    assert {qr._BODY_FALLBACK, qr._HEADER_FALLBACK} <= set(qr.FONT_STYLES)
    assert not {qr._BODY_FALLBACK, qr._HEADER_FALLBACK} & qr._THUGHT_STYLES


@pytest.mark.parametrize(
    ("key", "label"),
    [("thught", "Thught"), ("thught_black", "Thught Black"), ("times", "Times")],
)
def test_font_label(key, label):
    assert qr.font_label(key) == label


def test_every_style_has_a_distinct_label_within_discords_limit():
    labels = [qr.font_label(k) for k in qr.FONT_STYLES]
    assert len(set(labels)) == len(labels)
    assert all(0 < len(label) <= 100 for label in labels)
    assert len(labels) <= 25  # one select menu, one choices list


@pytest.mark.parametrize(
    ("style", "size", "stroke"),
    [("thught_black", 80, 0), ("helvetica", 80, 2), ("helvetica", 20, 1)],
)
def test_header_stroke_skips_a_face_that_is_already_heavy(style, size, stroke):
    assert qr._header_stroke(size, style) == stroke


def test_poppy_preset_draws_in_thught():
    preset = presets.resolve_preset("poppy")
    assert preset.key == "poppy" and preset.label == "Poppy"
    assert preset.font_path.name == "ThughtRegular.ttf"


# ── Against the real faces ──────────────────────────────────────────────────


def _need(*styles: str) -> None:
    missing = [s for s in styles if not qr.FONT_STYLES[s].exists()]
    if missing:
        pytest.skip(f"no font file for {missing} in this checkout")


def _avatar() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (80, 50, 130)).save(buf, "PNG")
    return buf.getvalue()


def test_real_thught_covers_latin_and_not_cyrillic():
    _need("thught", "thught_black", "times", "helvetica")
    assert qr.resolve_font_style("thught", "“Céline — déjà vu”", fallback="times") == "thught"
    assert qr.resolve_font_style("thught", "Алексей", fallback="times") == "times"
    assert (
        qr.resolve_font_style("thught_black", "Question of the Day", fallback="helvetica")
        == "thught_black"
    )


@pytest.mark.parametrize("pfp_shape", ["circle", "none"])
def test_card_renders_without_the_thught_files(monkeypatch, tmp_path, pfp_shape):
    _need("times", "helvetica")
    gone = dict(qr.FONT_STYLES, thught=tmp_path / "a.ttf", thught_black=tmp_path / "b.ttf")
    monkeypatch.setattr(qr, "FONT_STYLES", gone)
    png = qr.render_quote_card(
        "A quote",
        author_name="Robin",
        avatar_bytes=_avatar(),
        theme=qr.THEMES["midnight"],
        font_style=qr.DEFAULT_FONT_STYLE,
        pfp_shape=pfp_shape,
    )
    assert Image.open(io.BytesIO(png)).format == "PNG"


def test_a_name_thught_lacks_redraws_the_card_in_the_fallback():
    _need("thught", "times")

    def card(name: str, style: str) -> bytes:
        return qr.render_quote_card(
            "Same words",
            author_name=name,
            avatar_bytes=_avatar(),
            theme=qr.THEMES["midnight"],
            font_style=style,
        )

    assert card("Алексей", "thught") == card("Алексей", "times")
    assert card("Robin", "thught") != card("Robin", "times")


# ── Headings in the panel titles' petal colours ─────────────────────────────


def test_a_plain_header_takes_the_petal_colours():
    assert qr.header_takes_petal("Question of the Day · sponsored by Алексей")


def test_an_empty_header_draws_nothing():
    assert not qr.header_takes_petal("")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Friday \U0001f33c", id="unicode-emoji"),
        pytest.param("Friday <:poppy:123456789012345678>", id="custom-emoji"),
    ],
)
def test_a_header_with_an_emoji_keeps_the_flat_tint(text):
    # The petal look is cut from the letters' shapes; it can't carry a colour emoji.
    if not qr._HAS_PILMOJI:
        pytest.skip("pilmoji is not installed here")
    assert not qr.header_takes_petal(text)


def test_without_pilmoji_no_emoji_is_drawn_so_every_header_is_petal(monkeypatch):
    monkeypatch.setattr(qr, "_HAS_PILMOJI", False)
    assert qr.header_takes_petal("Friday \U0001f33c")


@pytest.mark.parametrize("style", ["thught_black", "helvetica"])
def test_petal_header_is_gold_inside_and_leaves_the_rest_alone(style):
    from PIL import ImageFont

    bg = Image.new("RGB", (400, 160), (20, 20, 40))
    font = ImageFont.load_default(size=72)
    qr._draw_petal_header(
        bg, (30, 30), "HI", font=font, size=72,
        stroke=qr._header_stroke(72, style), style=style,
    )
    colours = [c for _, c in bg.getcolors(bg.width * bg.height)]
    gold = [c for c in colours if c[0] > 200 and 110 < c[1] < 215 and c[2] < 90]
    assert gold, "no petal-coloured pixel was drawn"
    assert bg.getpixel((399, 159)) == (20, 20, 40)


@pytest.mark.parametrize(
    ("xy", "size"),
    [
        pytest.param((120, 60), 48, id="mid-card"),
        pytest.param((20, 2), 24, id="against-the-top-edge"),
        pytest.param((0, 0), 14, id="corner-smallest"),
        pytest.param((340, 120), 90, id="running-off-the-card"),
    ],
)
def test_petal_header_on_its_own_patch_matches_the_whole_card(xy, size):
    # The look is drawn on a crop around the letters to save the blurs; the
    # crop must not clip the glow or move anything.
    from PIL import ImageDraw, ImageFont

    from bot_modules.panels import title_service as svc

    font = ImageFont.load_default(size=size)
    whole = Image.new("RGB", (400, 160), (20, 20, 40))
    mask = Image.new("L", whole.size)
    ImageDraw.Draw(mask).text(xy, "Hi", font=font, fill=255, stroke_width=1, stroke_fill=255)
    layer = svc.petal_layer(mask, rim_px=svc.THIN_RIM_PX, scale=svc.petal_scale(size))
    whole.paste(layer, (0, 0), layer)

    patched = Image.new("RGB", (400, 160), (20, 20, 40))
    qr._draw_petal_header(
        patched, xy, "Hi", font=font, size=size, stroke=1, style="helvetica"
    )
    assert patched.tobytes() == whole.tobytes()


def test_petal_header_of_nothing_drawable_leaves_the_card_alone():
    from PIL import ImageFont

    bg = Image.new("RGB", (120, 60), (20, 20, 40))
    before = bg.tobytes()
    qr._draw_petal_header(
        bg, (10, 10), " ", font=ImageFont.load_default(size=24), size=24,
        stroke=0, style="thught_black",
    )
    assert bg.tobytes() == before


@pytest.mark.parametrize(
    ("header", "petal"),
    [("TRUTH", True), ("", False)],
)
def test_card_header_goes_through_the_petal_drawing(monkeypatch, header, petal):
    _need("inter", "helvetica")
    drawn: list[str] = []
    real = qr._draw_petal_header

    def spy(bg, xy, text, **kwargs):
        drawn.append(text)
        real(bg, xy, text, **kwargs)

    monkeypatch.setattr(qr, "_draw_petal_header", spy)
    qr.render_quote_card(
        "A prompt",
        author_name=header,
        avatar_bytes=_avatar(),
        theme=qr.THEMES["midnight"],
        pfp_shape="none",
    )
    assert drawn == ([header] if petal else [])
