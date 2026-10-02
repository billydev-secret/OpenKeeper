"""Contract: a member name reaching a markdown-rendering slot is escaped.

``tests/contracts/test_member_name_escaping_contract.py`` guards the same rule for the
shape with a reliable signature — a name read inside an **f-string slot** — and
its docstring is honest that it sees only that half. This file takes one bite
out of the other half.

**Why a second file rather than a wider pattern in the first.** Widening that
one to "any unguarded member-name read" flags 339 sites across 76 files, and
almost all of them are correct: a name passed to a logger, written to a row,
compared against another name, or handed to ``build_name_fn`` (which escapes at
``shared/name_resolver.py``). A rule with 339 exceptions teaches the next
person to add a 340th. So this file does not ask "is the name escaped?" — it
asks the narrower question that has a right answer every time: **does this name
reach a slot Discord renders markdown in?**

The sinks below are that list. ``embed.set_footer(text=...)`` and
``embed.set_author(name=...)`` are deliberately *not* on it, because Discord
renders no markdown in either — which is why five of the seven rows in the
sibling file's ``ALLOWED`` are footers and author lines, and why they should
never have needed an excuse.

**Born green, and it was born green by fixing the one site that wasn't.**
Measured over ``src/`` on 2026-09-24 this found exactly one live defect:
``welcome_service._resolve`` substituted ``{member}`` with an escaped display
name and ``{member_name}`` with a raw one, both into an embed
``description=``. The shipped default leave template is
``{member_name} has left the server.``, so every departure in every guild with
a leave channel rendered a member-controlled string as markdown — including a
masked link, which an embed description renders and a message ``content=`` does
not. ``tests/onboarding/test_welcome_service.py`` had asserted the raw output, pinning the
defect rather than catching it.

Two apparent hits were verified benign and shaped the rule rather than earning
an exception:

* ``games/mahjong/layouts.py`` passes a title through ``escape_to_fit``, which
  escapes and *then* re-fits the cap. It is a guard, so it is named in
  ``SAFE_CALLS``. Its neighbour ``trim_escaped`` is deliberately **not**: that
  one fits text that is *already* escaped, and treating it as a guard would
  excuse the exact mistake of fitting text nobody escaped.
* ``whisper_cog.py`` renders ``f"@{m.name}"`` and reads ``display_name`` only
  in the ternary's *condition*. A condition is not a rendered value, so the
  walk below skips ``IfExp.test`` — an ``x if <name> else y`` must not be read
  as rendering that name.

**What this cannot see.** Member *free text* — a bio answer, a whisper body, a
ticket description — is exactly as dangerous in these same slots and has no
distinguishing AST shape; the sibling file says the same and it is still true.
This file narrows the blind spot by one well-defined shape. It does not close
it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

#: Attributes holding a member-chosen name. ``.name`` is absent for the reason
#: the sibling contract gives: channels, roles, emoji, guilds and files all
#: carry one.
NAME_ATTRS = frozenset({"display_name", "nick", "global_name"})

#: Calls that render a name safe. ``escape_to_fit`` escapes *then* fits a cap.
#: ``trim_escaped`` is pointedly not here — it fits text already escaped.
SAFE_CALLS = frozenset({"escape_markdown", "escape_mentions", "escape_to_fit"})

#: Keyword slots Discord renders markdown in. ``text=`` (footer) and the
#: ``name=`` of ``set_author``/``add_field`` are excluded on purpose: markdown
#: is not rendered there, and escaping would show the member a backslash.
RENDER_KWARGS = frozenset({"description", "content", "value", "title"})

#: Empty, and shrink-only. A new entry is a claim that Discord does not render
#: markdown in that slot — which is checkable, and usually false.
BASELINE: set[tuple[str, str]] = set()


def _callee(call: ast.Call) -> str:
    fn = call.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def _rendered_subnodes(node: ast.AST):
    """Walk *node*, skipping the condition of any ternary inside it.

    ``f"@{m.name}" if m.name != m.display_name else None`` renders ``m.name``
    and merely *tests* ``m.display_name``. Walking the test would report a read
    that never reaches a surface.
    """
    if isinstance(node, ast.IfExp):
        yield from _rendered_subnodes(node.body)
        yield from _rendered_subnodes(node.orelse)
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _rendered_subnodes(child)


def _is_guarded(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call) and _callee(sub) in SAFE_CALLS
        for sub in _rendered_subnodes(node)
    )


def _unguarded_source(source: str) -> list[tuple[int, str]]:
    """Member-name reads reaching a rendering sink, outside an f-string.

    F-string slots are the sibling contract's territory; reporting them here
    would make one site answer to two files with two allowlists.
    """
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []

    def report(value: ast.AST, lineno: int) -> None:
        if isinstance(value, ast.JoinedStr) or _is_guarded(value):
            return
        for sub in _rendered_subnodes(value):
            if isinstance(sub, ast.Attribute) and sub.attr in NAME_ATTRS:
                found.append((lineno, ast.unparse(sub)))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A template substitution: `template.replace("{member_name}", name)`.
        if _callee(node) == "replace" and len(node.args) == 2:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.startswith("{")
                and first.value.endswith("}")
            ):
                report(node.args[1], node.lineno)
        for kw in node.keywords:
            if kw.arg in RENDER_KWARGS:
                report(kw.value, kw.value.lineno)
    return found


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_sweep_sees_the_source_tree() -> None:
    """A moved tree would make the check below pass vacuously."""
    assert len(_modules()) >= 300


def test_no_member_name_reaches_a_rendering_slot_unescaped() -> None:
    offenders = []
    for path in _modules():
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover
            continue
        try:
            hits = _unguarded_source(source)
        except SyntaxError:  # pragma: no cover - its own tests fail first
            continue
        rel = path.relative_to(SRC).as_posix()
        offenders += [
            f"{rel}:{line} — {expr}" for line, expr in hits if (rel, expr) not in BASELINE
        ]
    assert not offenders, (
        "A member-controlled name reaches a slot Discord renders markdown in, "
        "with nothing escaping it:\n  "
        + "\n  ".join(sorted(offenders))
        + "\nWrap it in `discord.utils.escape_markdown(...)`, or in "
        "`escape_to_fit(..., LIMIT)` if the slot has a length cap. A name "
        "destined for an embed built with a `name_fn` should go through the "
        "`name_fn` instead — it escapes in `shared/name_resolver.py`."
    )


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        pytest.param(
            't.replace("{member_name}", member.display_name)', True, id="replace-raw"
        ),
        pytest.param(
            't.replace("{member_name}", escape_markdown(member.display_name))',
            False,
            id="replace-escaped",
        ),
        pytest.param(
            'Embed(description=member.display_name)', True, id="description-raw"
        ),
        pytest.param(
            'Embed(description=discord.utils.escape_markdown(m.display_name))',
            False,
            id="description-escaped",
        ),
        pytest.param(
            'Embed(title=escape_to_fit(card.display_name, LIMIT))',
            False,
            id="escape-to-fit-is-a-guard",
        ),
        pytest.param(
            'Embed(title=trim_escaped(card.display_name, LIMIT))',
            True,
            id="trim-escaped-is-not-a-guard",
        ),
        pytest.param(
            'e.set_footer(text=member.display_name)', False, id="footer-renders-nothing"
        ),
        pytest.param(
            'e.set_author(name=member.display_name)', False, id="author-renders-nothing"
        ),
        pytest.param(
            'Embed(description=f"hi {m.display_name}")',
            False,
            id="f-strings-belong-to-the-sibling-contract",
        ),
        pytest.param(
            'Embed(description=f"@{m.name}" if m.name != m.display_name else None)',
            False,
            id="a-ternary-condition-is-not-a-rendered-value",
        ),
        pytest.param(
            'Embed(description=m.display_name if flag else "x")',
            True,
            id="but-a-ternary-branch-is",
        ),
        pytest.param('x = member.display_name', False, id="a-bare-read-is-not-a-sink"),
    ],
)
def test_the_detector_actually_bites(source: str, caught: bool) -> None:
    """A born-green guard that never demonstrates a catch proves nothing."""
    assert bool(_unguarded_source(source)) is caught
