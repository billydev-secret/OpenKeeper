"""Dashboard field hints follow ``docs/copy_style_guide.md`` § Field hints.

A field hint is the grey line under a control. It had grown to a median of 129
characters, with 35 over 400 and 219 carrying an em dash, so the page's own
explanation was the hardest thing on it to read. The rule is at most
``MAX_HINT_CHARS`` of rendered text and no em dash; the detail a hint used to
carry lives in the panel's ``manual.html`` section.

The sweep reads hints statically from the panel JS: literal ``field-hint``
markup, and ``hint:`` properties that a shared helper renders. Interpolations
are dropped, tags stripped and entities decoded before counting, so the length
is what an admin sees minus any computed part. A hint built in code or passed
positionally is out of reach; the guide says so.

Hints written before the rule are allowed per file in ``ALLOWANCE``. The
number only goes down: a file over its allowance fails, and so does a file
under it, so a cleaned file must lower its number in the same commit.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

JS_ROOT = REPO_ROOT / "src" / "web_server" / "static" / "js"

MAX_HINT_CHARS = 120

#: Hints per file still breaking the rule, from before it existed. Lower a
#: number when you clean a file; delete the row when it reaches zero.
ALLOWANCE: dict[str, int] = {}

_MARKUP = re.compile(
    r"<(div|span|p|small|label|li|td|th|summary)\b[^>]*\bclass=([\"'])(?:[^\"']*\s)?field-hint(?:\s[^\"']*)?\2[^>]*>(.*?)</\1>",
    re.S,
)
_PROP = re.compile(
    r"\bhint:\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)",
    re.S,
)
_TAG = re.compile(r"<[^>]+>")
_JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.S)


def _unescape_js(s: str) -> str:
    def sub(m: re.Match[str]) -> str:
        e = m.group(1)
        if e.startswith("u") and len(e) == 5:
            return chr(int(e[1:], 16))
        return {"n": " ", "t": " "}.get(e, e)

    return _JS_ESCAPE.sub(sub, s)


def _drop_interpolations(raw: str) -> str:
    """Remove each ``${…}``, counting braces so an object literal inside one
    (``Object.keys(x || {})``) doesn't end it early."""
    out, i = [], 0
    while (start := raw.find("${", i)) != -1:
        out.append(raw[i:start])
        depth, j = 1, start + 2
        while j < len(raw) and depth:
            depth += {"{": 1, "}": -1}.get(raw[j], 0)
            j += 1
        i = j
    out.append(raw[i:])
    return "".join(out)


def rendered(raw: str) -> str:
    """The text an admin reads, minus anything interpolated at runtime."""
    text = html.unescape(_TAG.sub("", _drop_interpolations(raw)))
    return re.sub(r"\s+", " ", text).strip()


def hints_in(source: str) -> list[str]:
    found = [m.group(3) for m in _MARKUP.finditer(source)]
    found += [_unescape_js(next(g for g in m.groups() if g is not None)) for m in _PROP.finditer(source)]
    return [t for t in (rendered(f) for f in found) if t]


def problems(text: str) -> list[str]:
    out = []
    if "—" in text:
        out.append("em dash")
    if len(text) > MAX_HINT_CHARS:
        out.append(f"{len(text)} chars")
    return out


def _sweep() -> dict[str, list[tuple[str, list[str]]]]:
    report: dict[str, list[tuple[str, list[str]]]] = {}
    for path in sorted(JS_ROOT.rglob("*.js")):
        if "vendor" in path.parts:
            continue
        rel = path.relative_to(JS_ROOT).as_posix()
        for text in hints_in(path.read_text(encoding="utf-8")):
            if p := problems(text):
                report.setdefault(rel, []).append((text, p))
    return report


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('<div class="field-hint">Plain.</div>', ["Plain."], id="markup"),
        pytest.param(
            "<span class='muted field-hint' style='x'>A &mdash; b</span>",
            ["A — b"], id="entity-decoded-extra-class",
        ),
        pytest.param(
            '<div class="field-hint"><strong>Staff only.</strong>\n   Two\n lines.</div>',
            ["Staff only. Two lines."], id="tags-stripped-whitespace-folded",
        ),
        pytest.param(
            '<div class="field-hint">${n} item${n === 1 ? "" : "s"} left.</div>',
            ["item left."], id="interpolation-dropped",
        ),
        pytest.param('{ hint: "Say \\"hi\\" \\u2014 ok" }', ['Say "hi" — ok'], id="prop-double"),
        pytest.param("{ hint: 'It\\'s set.' }", ["It's set."], id="prop-single"),
        pytest.param("{ hint: `Across\n two.` }", ["Across two."], id="prop-template"),
        pytest.param(
            '<div class="field-hint">${Object.keys(x || {}).length} places.</div>',
            ["places."], id="braces-inside-interpolation",
        ),
        pytest.param(
            '<summary class="field-hint">A — b</summary><li class="field-hint">C</li>',
            ["A — b", "C"], id="non-block-tags",
        ),
        pytest.param('<div class="field-hint">${hint}</div>', [], id="pure-interpolation-skipped"),
        pytest.param('<div class="field-hints">no</div>', [], id="other-class-ignored"),
    ],
)
def test_extractor(source: str, expected: list[str]) -> None:
    assert hints_in(source) == expected


def test_the_sweep_reads_the_dashboard() -> None:
    """A regex that silently matches nothing would pass every other test."""
    total = sum(
        len(hints_in(p.read_text(encoding="utf-8")))
        for p in JS_ROOT.rglob("*.js")
        if "vendor" not in p.parts
    )
    assert total > 400


def test_hints_stay_within_their_allowance() -> None:
    report = _sweep()
    over = {
        f: hits for f, hits in report.items() if len(hits) > ALLOWANCE.get(f, 0)
    }
    lines = [
        f"{f}: {len(hits)} hints break docs/copy_style_guide.md (allowed {ALLOWANCE.get(f, 0)})"
        + "".join(f"\n    [{', '.join(p)}] {t[:90]}" for t, p in hits)
        for f, hits in over.items()
    ]
    assert not over, "\n".join(lines)


def test_allowance_is_not_stale() -> None:
    """A cleaned file lowers its number, so the ratchet can't slip back."""
    report = _sweep()
    stale = {
        f: (n, len(report.get(f, [])))
        for f, n in ALLOWANCE.items()
        if len(report.get(f, [])) < n
    }
    assert not stale, "Lower ALLOWANCE for: " + ", ".join(
        f"{f} {was} -> {now}" for f, (was, now) in stale.items()
    )


def test_allowance_names_real_files() -> None:
    missing = [f for f in ALLOWANCE if not (JS_ROOT / f).is_file()]
    assert not missing, missing


def test_js_root_exists() -> None:
    assert Path(JS_ROOT).is_dir()
