"""``/banner`` in its box: the renderer's side of ``quote_box`` (Ben, 2026-09-29).

"I want make the body text bigger and an adjacent but more readable font. Id
like to change the box to look like the large header and footer but a box
around it with the bokeh that we're using in the titles today."
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bot_modules.shared import quote_box as qb
from bot_modules.shared import quote_renderer as qr

NOTICE = "Game night starts at 8. Bring snacks."
THEME = qr.THEMES["golden_meadow"]


def _card(text: str = NOTICE, **kw) -> Image.Image:
    kw.setdefault("author_name", "Tonight")
    kw.setdefault("font_style", qr.DEFAULT_BANNER_FONT_STYLE)
    kw.setdefault("pfp_shape", "none")
    png = qr.render_quote_card(
        text, avatar_bytes=b"", theme=THEME, border_style=qr.BANNER_BOX, **kw
    )
    return Image.open(io.BytesIO(png)).convert("RGBA")


@pytest.fixture
def drawn(monkeypatch):
    """Every body line the renderer draws, with its colour and size."""
    seen: list[dict] = []
    real = qr._render_line_mixed

    def spy(line, x, y, **kw):
        seen.append({"line": line, "x": x, "color": kw["color"], "size": kw["font"].size})
        return real(line, x, y, **kw)

    monkeypatch.setattr(qr, "_render_line_mixed", spy)
    return seen


# ── defaults and the frame's choice ──────────────────────────────────────────


def test_a_banner_defaults_to_belleza_and_a_quote_stays_in_thught():
    assert qr.DEFAULT_BANNER_FONT_STYLE == "belleza"
    assert qr.FONT_STYLES["belleza"].name == "Belleza-Regular.ttf"
    assert qr.font_label("belleza") == "Belleza"
    assert qr.DEFAULT_FONT_STYLE == "thught"


def test_the_box_is_the_banners_alone():
    assert qr.BANNER_BOX.box
    assert not any(b.box for b in qr.BORDERS.values()), "the Quote picker offers it"
    assert not qr.BANNER_BOX.path.exists(), "the box is drawn, not loaded"


def test_a_servers_uploaded_border_does_not_change_its_banners():
    """Ben, at ship: "Box always wins". An upload is a quote-card frame; when
    it also picked the banner's look, no panel said so, and the only way back
    to the box was to delete the border every quote card wears."""
    import ast
    import importlib.util
    from pathlib import Path

    # By dotted name, so a layout move rewrites it with every import.
    cog = Path(importlib.util.find_spec("bot_modules.quotes.cog").origin)
    tree = ast.parse(cog.read_text(encoding="utf-8"))
    banner = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "banner"
    )
    names = {n.id for n in ast.walk(banner) if isinstance(n, ast.Name)}
    assert "BANNER_BOX" in names
    assert not names & {"_resolve_border", "custom_border_style", "CUSTOM_BORDER_KEY"}
    assert not hasattr(qr, "banner_border")
    # And with no picture in the box, the command downloads none.
    attrs = {n.attr for n in ast.walk(banner) if isinstance(n, ast.Attribute)}
    assert not attrs & {"icon", "display_avatar"}


# ── Belleza's fallback ───────────────────────────────────────────────────────


@pytest.fixture
def faces(tmp_path, monkeypatch):
    latin = frozenset(map(ord, "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    maps = {}
    styles = dict(qr.FONT_STYLES)
    for key, cover in {"belleza": latin, "times": latin | frozenset(map(ord, "Алексей"))}.items():
        styles[key] = tmp_path / f"{key}.ttf"
        styles[key].write_bytes(b"")
        maps[styles[key]] = cover
    monkeypatch.setattr(qr, "FONT_STYLES", styles)
    monkeypatch.setattr(qr, "_codepoints", lambda path: maps.get(path, frozenset()))
    return styles


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Doors at eight", "belleza", id="covered-stays"),
        pytest.param("Привет Алексей", "times", id="uncovered-falls-back"),
        pytest.param("nice \U0001f48b", "belleza", id="emoji-stays"),
    ],
)
def test_belleza_falls_back_on_a_character_it_lacks(faces, text, expected):
    assert qr.resolve_font_style("belleza", text, fallback="times") == expected


def test_a_missing_belleza_file_still_fails_loudly(faces, monkeypatch):
    # Thught is a bought face a copy of the repo may lack; Belleza is committed.
    faces["belleza"].unlink()
    assert qr.resolve_font_style("belleza", "Doors at eight", fallback="times") == "belleza"
    with pytest.raises(FileNotFoundError):
        qr._load_font(40, "belleza")


def test_the_banners_face_draws_everything_the_renderer_adds():
    """A default face with a hole in it draws an empty box on every card."""
    path = qr.FONT_STYLES[qr.DEFAULT_BANNER_FONT_STYLE]
    if not path.is_file():
        pytest.skip("no assets/fonts in this checkout")
    have = qr._codepoints(path)
    needed = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        " .,;:!?'\"()-&@#%/+=*"
        "…–—‘’“”"          # what truncation and a phone keyboard add
        "éèêëàâäîïôöùûüçñßøåæœłőűšžčř¿¡€£"
    )
    assert not [ch for ch in needed if ord(ch) not in have]


# ── the card ─────────────────────────────────────────────────────────────────


def test_the_card_needs_no_picture_and_is_as_tall_as_its_text():
    short = _card()
    long_ = _card(NOTICE * 4)
    assert short.width == long_.width == 900
    assert short.height < 500 < long_.height <= 900


def test_the_card_wears_the_frame_at_its_edge_on_black():
    card = _card()
    w, h = card.size
    assert card.getpixel((0, 0))[3] == 0, "square corners"
    top = card.getpixel((w // 2, 2))
    side = card.getpixel((2, h // 2))
    assert top[3] == 255 and top[:3] == pytest.approx(qb.FRAME_GOLD_MID, abs=3)
    assert side[3] == 255 and side[:3] == pytest.approx(qb.FRAME_GOLD_EDGE, abs=3)
    inside = card.getpixel((12, h // 2))
    assert inside[3] == 255 and max(inside[:3]) < 40, "the field is black to the line"


def test_the_words_are_bigger_centred_and_unquoted(drawn):
    card = _card()
    assert [d["line"] for d in drawn] == ["Game night starts", "at 8. Bring snacks."]
    assert not any(ch in d["line"] for d in drawn for ch in "“”")
    assert {d["size"] for d in drawn} == {62}  # the old card's were 47
    font = qr._load_font(62, "belleza")
    for d in drawn:
        assert d["x"] == (card.width - int(font.getlength(d["line"]))) // 2


def test_the_theme_colours_the_words(drawn):
    _card()
    assert {d["color"] for d in drawn} == {THEME.text_color}
    drawn.clear()
    png = qr.render_quote_card(
        NOTICE, avatar_bytes=b"", theme=qr.THEMES["rose"], pfp_shape="none",
        font_style="belleza", border_style=qr.BANNER_BOX,
    )
    assert png and {d["color"] for d in drawn} == {qr.THEMES["rose"].text_color}


def test_a_card_in_the_box_is_a_banner_whatever_shape_was_asked(drawn):
    _, geo = qr._compose_card(
        NOTICE, author_name="Poppy", avatar_bytes=b"", theme=THEME,
        font_style="belleza", header_font_style=qr.DEFAULT_HEADER_FONT_STYLE,
        border_style=qr.BANNER_BOX, width=900, height=500,
        custom_emojis=None, pfp_shape="circle",
    )
    assert geo.pfp_pos is None and geo.box_dots


def test_an_over_long_notice_ends_on_a_whole_word(drawn):
    # Capitals are wide: inside the 280-character limit, and still more than
    # the tallest card holds at the smallest size.
    words = "MEADOW POPPIES GOLDEN LANTERN EVENING".split()
    text = " ".join(words[i % 5] for i in range(37))
    assert len(text) <= qr.QUOTE_MAX_CHARS
    card = _card(text)
    assert card.height <= 900
    assert {d["size"] for d in drawn} == {52}  # still larger than the old 47
    last = drawn[-1]["line"]
    assert last.endswith("…")
    assert all(w in words for w in last[:-1].split())
    assert sum(len(d["line"].split()) for d in drawn) < 37


def test_a_long_titled_notice_loses_nothing(drawn):
    # The old fixed card cut a notice this long off before its halfway point.
    text = (
        "Movie night moves to Saturday this week because the projector room is "
        "booked. Doors open at 7, film starts at 7:30 sharp, and the vote for "
        "what we watch closes Friday at noon. Bring a blanket, bring a friend, "
        "and if you want a seat saved then tell a mod before Saturday."
    )
    assert 260 < len(text) <= qr.QUOTE_MAX_CHARS
    card = _card(text, author_name="Movie Night")
    assert " ".join(d["line"] for d in drawn) == text
    assert 52 <= drawn[0]["size"] < 62
    assert card.height <= 900


@pytest.mark.parametrize(
    ("title", "petal"),
    [
        pytest.param("Tonight", True, id="plain-title-takes-the-petal"),
        pytest.param("Tonight \U0001f389", False, id="emoji-title-keeps-a-flat-gold"),
        pytest.param("", None, id="no-title-draws-none"),
    ],
)
def test_the_title_is_drawn_like_every_other_heading(monkeypatch, title, petal):
    calls: list[str] = []
    monkeypatch.setattr(qr, "header_takes_petal", lambda t: not t.endswith("\U0001f389"))
    monkeypatch.setattr(qr, "_draw_petal_header", lambda *a, **k: calls.append("petal"))
    monkeypatch.setattr(qr, "_draw_text_layers", lambda *a, **k: calls.append("flat"))
    _card(author_name=title)
    assert calls == ([] if petal is None else ["petal" if petal else "flat"])


def test_a_failed_emoji_fetch_still_draws_the_words(monkeypatch, drawn):
    if not qr._HAS_PILMOJI:
        pytest.skip("pilmoji not installed")

    class Broken:
        def __init__(self, *a, **k):
            raise OSError("cdn down")

    monkeypatch.setattr(qr, "_Pilmoji", Broken)
    card = _card(author_name="")
    assert card.height < 500
    assert [d["line"] for d in drawn] == ["Game night starts", "at 8. Bring snacks."]


def test_the_same_notice_draws_the_same_card():
    assert _card().tobytes() == _card().tobytes()


# ── animated ─────────────────────────────────────────────────────────────────


def test_the_posted_banner_twinkles():
    data = qr.render_animated_quote_card(
        NOTICE, author_name="Tonight", avatar_bytes=b"", theme=THEME,
        font_style="belleza", pfp_shape="none", border_style=qr.BANNER_BOX,
    )
    assert qr.card_filename("banner", data) == "banner.webp"
    assert len(data) < qr.ANIM_MAX_BYTES
    im = Image.open(io.BytesIO(data))
    assert im.n_frames == qb.FRAMES, "the encoder merged frames it judged unchanged"
    frames = []
    for i in (0, 8, 17):
        im.seek(i)
        frames.append(im.convert("RGBA"))
        assert im.info["duration"] == qb.FRAME_MS
    assert frames[0].size == _card().size
    assert frames[0].tobytes() != frames[1].tobytes() != frames[2].tobytes()
    w, h = frames[0].size
    for frame in frames:  # the frame and the corners hold still
        assert frame.getpixel((0, 0))[3] == 0
        assert frame.getpixel((w // 2, 2))[:3] == pytest.approx(qb.FRAME_GOLD_MID, abs=12)


def test_the_animated_layer_rebuilds_the_still_card():
    kw = dict(
        author_name="Tonight", avatar_bytes=b"", theme=THEME, font_style="belleza",
        header_font_style=qr.DEFAULT_HEADER_FONT_STYLE, border_style=qr.BANNER_BOX,
        width=900, height=500, custom_emojis=None, pfp_shape="none",
    )
    still, geo = qr._compose_card(NOTICE, **kw)
    on_black, _ = qr._compose_card(NOTICE, fill=(0, 0, 0), **kw)
    on_white, _ = qr._compose_card(NOTICE, fill=(255, 255, 255), **kw)
    rebuilt = qb.field(geo.width, geo.height, geo.box_dots).convert("RGBA")
    rebuilt.alpha_composite(qr._matte(on_black, on_white))
    rebuilt.putalpha(on_black.getchannel("A"))
    import numpy as np

    diff = np.abs(
        np.asarray(rebuilt, dtype=np.int16) - np.asarray(still, dtype=np.int16)
    )
    assert diff.max() <= 3
