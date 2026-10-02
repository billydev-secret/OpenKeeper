"""No member mention inside an embed or a V2 card — a static sweep of ``src/``.

House rule (CLAUDE.md, ``docs/embed_style_guide.md``): a member named inside an
embed is resolved through ``services/name_resolver.build_name_fn``, never
written as ``<@id>``. An embed mention is resolved by the *reading* client from
its own cache, so it renders as a bare number for anyone who hasn't seen that
member — and for everyone once the member has left. Mentions in message
``content=`` are fine, and so are role and channel mentions.

A Components V2 ``TextDisplay`` is the same surface under a new name: it is
rendered from the reader's cache exactly as an embed description is, so the
rule extends to it unchanged. It is *worse* in one way — the shop pilot
confirmed a ``<@id>`` in a Text Display also notifies, which an embed's never
did — so an unresolved name there is both a bare number and a stray ping.

This walks every module's AST and fails on:

* **sink** — ``.mention``, a ``mention(...)`` helper call, or a ``<@{`` /
  ``<@!{`` string inside an embed text slot: ``Embed(title=, description=)``,
  ``add_field`` / ``insert_field_at`` / ``set_field_at`` name+value,
  ``set_footer`` text, ``set_author`` name, an assignment to
  ``.title`` / ``.description``, a ``TextDisplay(...)`` string, or a string
  child of a ``Section(...)`` (which Discord turns into a Text Display);
* **taint** — one hop of the same through a local variable
  (``desc = f"{m.mention} …"`` then ``Embed(description=desc)``);
* **param** — a ``build_*`` / ``render_*`` / ``*_embed`` function taking a
  parameter named ``*_mention`` / ``mention_*``: a builder takes a name or a
  ``name_fn``, never pre-rendered mention text.

Role and channel mentions are told apart by a same-line marker naming the
expression, never by guessing from a variable name::

    embed.add_field(name="Channel", value=channel.mention)  # embed-mention-ok: channel

**What it cannot see** — so don't read green as proof: a mention built in one
function and used in another (a helper returning ``lines``), container joins,
``str.replace`` / ``.format`` templates, ``Embed.from_dict``, and a builder whose
caller forgets its ``name_fn`` (which is why builders touched by the 2026-09-14
sweep take ``name_fn`` with no default). Welcome's ``{member}`` template, the
intake checklist and the XP leaderboard were all of that shape; they are
guarded by their own builder tests.

``KNOWN_EMBED_MENTIONS`` is keyed by ``path::function`` and fails on stale
entries, so fixing a site forces its row out. Adding a row is an explicit,
reviewable decision — say why beside it.
"""

from __future__ import annotations

import ast
import functools
import re

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

_FIELD_METHODS = {"add_field", "insert_field_at", "set_field_at"}
_MARKER = re.compile(r"#\s*embed-mention-ok:\s*(.+)$")
_RAW_MENTION = re.compile(r"<@!?$")

# The debt, as of the 2026-09-14 sweep (docs/embed_style_guide.md § Mentions).
# Each stage deletes its rows; the stale check below makes that mandatory.
KNOWN_EMBED_MENTIONS: dict[str, str] = {
    # ── XP, confessions, QA ─────────────────────────────────────────
    # ── Pen Pals & DM permissions ───────────────────────────────────
    # ── Music, games, and the rest ──────────────────────────────────
    # ── Voice Control ───────────────────────────────────────────────
    # The pending-challenge card pings its target deliberately — first item,
    # ahead of the name_fn-resolved body — because a V2 message has no
    # content= to carry the ping (guide § No container), and the challenge gate in
    # _base_challenge already refuses a no-contact pair before this builder
    # ever runs, so there is no degrade case to cover here either.
    "bot_modules/duels/layouts.py::build_challenge_layout": (
        "deliberate ping, not a name render — see build_challenge_layout's docstring"
    ),
}


def _markers(lines: list[str], lineno: int) -> set[str]:
    m = _MARKER.search(lines[lineno - 1]) if 0 < lineno <= len(lines) else None
    return {s.strip() for s in m.group(1).split(",")} if m else set()


def _mention_nodes(node: ast.AST, lines: list[str], tainted: set[str]):
    """Yield (lineno, label) for every member-mention expression under node."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "mention":
            base = ast.unparse(sub.value)
            if base not in _markers(lines, sub.lineno):
                yield sub.lineno, f"{base}.mention"
        elif isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id in {"mention", "_mention"}:
                yield sub.lineno, f"{func.id}(...)"
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            if sub.id in tainted:
                yield sub.lineno, f"{sub.id} (holds a mention)"
        elif isinstance(sub, ast.JoinedStr):
            parts = sub.values
            for before, after in zip(parts, parts[1:]):
                if (
                    isinstance(before, ast.Constant)
                    and isinstance(before.value, str)
                    and isinstance(after, ast.FormattedValue)
                    and _RAW_MENTION.search(before.value)
                    and ast.unparse(after.value) not in _markers(lines, sub.lineno)
                ):
                    yield sub.lineno, "<@{...}>"
        elif (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and re.search(r"<@!?\{", sub.value)
        ):
            yield sub.lineno, "'<@{...}>' template"


def _sink_args(node: ast.AST) -> list[ast.AST]:
    """The embed text slots this node writes, or [] if it isn't a sink."""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if name == "Embed":
            return [v for k, v in kw.items() if k in {"title", "description"}]
        if name in _FIELD_METHODS:
            skip = 0 if name == "add_field" else 1  # leading index
            return [*node.args[skip:skip + 2], *(v for k, v in kw.items() if k in {"name", "value"})]
        if name == "set_footer":
            return [*node.args[:1], *(v for k, v in kw.items() if k == "text")]
        if name == "set_author":
            return [*node.args[:1], *(v for k, v in kw.items() if k == "name")]
        if name == "TextDisplay":
            return [*node.args[:1], *(v for k, v in kw.items() if k == "content")]
        if name == "Section":
            # Positional args are the Section's children — an ``Item`` or a
            # bare string Discord turns into a Text Display. ``accessory``
            # is keyword-only and holds a button or a thumbnail, never text.
            return list(node.args)
        return []
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(t, ast.Attribute) and t.attr in {"title", "description"}
            for t in targets
        ):
            return [node.value]
    return []


def _tainted_locals(scope: ast.AST, lines: list[str]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(True for _ in _mention_nodes(node.value, lines, set())):
                names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _scopes(tree: ast.Module):
    """(qualified name, node) for every function, plus the module itself."""
    yield "<module>", tree

    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                yield qual, child
                yield from visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _innermost_scope(tree: ast.Module) -> dict[int, str]:
    """Map each node id to the qualified name of the function that owns it."""
    owner: dict[int, str] = {}
    for qual, scope in _scopes(tree):
        for node in ast.walk(scope):
            owner[id(node)] = qual  # later (deeper) scopes overwrite outer ones
    return owner


def scan_source(source: str) -> list[tuple[str, int, str]]:
    """Every violation in one module as (function, lineno, description)."""
    tree = ast.parse(source)
    lines = source.splitlines()
    owner = _innermost_scope(tree)
    hits: set[tuple[str, int, str]] = set()

    for qual, scope in _scopes(tree):
        if isinstance(scope, ast.Module):
            continue
        is_builder = scope.name.startswith(("build_", "render_")) or scope.name.endswith("_embed")
        if is_builder:
            args = scope.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                n = arg.arg
                if (n.endswith("_mention") or n.startswith("mention_")) and n not in _markers(
                    lines, arg.lineno
                ):
                    hits.add((qual, arg.lineno, f"builder param {n}"))

    for qual, scope in _scopes(tree):
        tainted = _tainted_locals(scope, lines) if not isinstance(scope, ast.Module) else set()
        for node in ast.walk(scope):
            if owner.get(id(node)) != qual:
                continue
            for arg in _sink_args(node):
                for lineno, label in _mention_nodes(arg, lines, tainted):
                    hits.add((qual, lineno, label))
    return sorted(hits, key=lambda h: (h[1], h[0], h[2]))


@functools.cache
def _scan_tree() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        if "mention" not in source and "<@" not in source:
            continue  # nothing to find; parsing every module costs ~10 s
        for qual, lineno, label in scan_source(source):
            found.setdefault(f"{rel}::{qual}", []).append((lineno, label))
    return found


# ── the scanner itself ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "def f(m):\n    discord.Embed(description=f'{m.mention} joined')\n",
            id="attr-in-description",
        ),
        pytest.param(
            "def f(e, uid):\n    e.add_field(name='By', value=f'<@{uid}>')\n",
            id="raw-fstring-in-field",
        ),
        pytest.param(
            "def f(e, uid):\n    e.set_field_at(0, name='x', value=f'<@!{uid}>')\n",
            id="nick-form-set-field-at",
        ),
        pytest.param(
            "def f(e, m):\n    e.set_footer(text=m.mention)\n", id="footer"
        ),
        pytest.param(
            "def f(e, m):\n    e.description += m.mention\n", id="augassign"
        ),
        pytest.param(
            "def f(m):\n    desc = f'by {m.mention}'\n    return Embed(title='t', description=desc)\n",
            id="one-hop-local",
        ),
        pytest.param(
            "def f(e, uid):\n    e.add_field(name='x', value=mention(uid))\n", id="helper"
        ),
        pytest.param(
            "def build_card(*, opener_mention):\n    return None\n", id="builder-param"
        ),
        pytest.param(
            "def f(m):\n    discord.ui.TextDisplay(f'{m.mention} is up')\n",
            id="attr-in-text-display",
        ),
        pytest.param(
            "def f(uid):\n    TextDisplay(content=f'<@{uid}> is up')\n",
            id="raw-fstring-in-text-display",
        ),
        pytest.param(
            "def f(m, btn):\n    Section(f'{m.mention} is up', accessory=btn)\n",
            id="string-child-of-section",
        ),
    ],
)
def test_scanner_flags(source):
    assert scan_source(source), "scanner missed a member mention in an embed"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "async def f(ch, m):\n    await ch.send(f'{m.mention} hi', embed=Embed(description='x'))\n",
            id="content-is-fine",
        ),
        pytest.param(
            "def f(e, rid):\n    e.add_field(name='Role', value=f'<@&{rid}>')\n",
            id="role-literal",
        ),
        pytest.param(
            "def f(e, ch):\n    e.add_field(name='In', value=ch.mention)  # embed-mention-ok: ch\n",
            id="marked-channel",
        ),
        pytest.param(
            "def f(e, name_fn, uid):\n    e.add_field(name='By', value=name_fn(uid))\n",
            id="resolved-name",
        ),
        pytest.param(
            "def post(*, hub_mention):  # not a builder\n    return hub_mention\n",
            id="non-builder-param",
        ),
        pytest.param(
            "def f(name_fn, uid):\n    TextDisplay(f'{name_fn(uid)} is up')\n",
            id="resolved-name-in-text-display",
        ),
        pytest.param(
            "def f(ch, btn):\n    Section(ch.mention, accessory=btn)  # embed-mention-ok: ch\n",
            id="marked-channel-in-section",
        ),
    ],
)
def test_scanner_passes(source):
    assert not scan_source(source)


def test_marker_names_only_the_marked_expression():
    """A channel marker on a line that also names a member must not hide it."""
    source = (
        "def f(u, ch):\n"
        "    Embed(description=f'{u.mention} in {ch.mention}')  # embed-mention-ok: ch\n"
    )
    assert [label for _, _, label in scan_source(source)] == ["u.mention"]


# ── the tree ────────────────────────────────────────────────────────────


def test_no_member_mention_inside_an_embed():
    found = _scan_tree()
    new = {k: v for k, v in found.items() if k not in KNOWN_EMBED_MENTIONS}
    assert not new, (
        "Member mention inside an embed — resolve it with "
        "services/name_resolver.build_name_fn (a builder takes a name or a "
        "name_fn). Role/channel mention? mark the line "
        "`# embed-mention-ok: <expr>`.\n  "
        + "\n  ".join(
            f"{k}:{ln} {label}" for k, hits in sorted(new.items()) for ln, label in hits
        )
    )


def test_known_embed_mentions_ledger_is_not_stale():
    found = _scan_tree()
    stale = sorted(set(KNOWN_EMBED_MENTIONS) - set(found))
    assert not stale, (
        "KNOWN_EMBED_MENTIONS rows whose site no longer has a mention — delete "
        "them so the ledger stays honest:\n  " + "\n  ".join(stale)
    )
