"""A Components V2 card is bare: no ``ui.Container`` anywhere in ``src/``.

Billy's call on 2026-09-20, after seeing the shop card rendered three ways:
"drop the big bars … the modern simple look". A `Container` draws exactly
the thing the migration is removing — a boxed outline with an optional
coloured bar down its edge, the embed's block under a new name. Text
Displays, Sections, Separators and galleries sit directly on the message
instead (docs/components_v2_style_guide.md § No container, no accent bar).

The allow-list is **empty, and meant to stay short if it ever isn't**. From
2026-09-24 to 2026-09-29 it held the five channel hub panels — a box with no
accent bar, Billy's trial tile — until the hubs got their banner art: inside
a box the text and buttons overhung the art's gold frame, and Discord can
neither pad a line of text nor hide a box (Ben, 2026-09-29: "move the banners
out for full width and drop the box"). It is not a debt ledger like
``KNOWN_MISSING_ON_TIMEOUT``; it exists so that a container someone wants
badly enough becomes a visible design decision with a name on it, rather
than a local choice inside one builder that the next card copies. Each row
says whose call it was and when.

The sweep is static and syntactic — it matches the three spellings a
container is actually constructed by, not the rendered message:

* ``discord.ui.Container(...)``
* ``ui.Container(...)`` (``from discord import ui``)
* ``Container(...)`` where ``Container`` was imported from ``discord.ui``,
  under whatever local name the import bound

Subclassing is caught the same way, because ``class Card(ui.Container)``
names the base by one of those spellings too.

**What it cannot see**: a container built by a helper in a third-party
package, or one assembled from a raw component dict and handed to
``Message.edit``. Neither is a shape this repo builds cards in, and the
browser-side review at the end of each stage is what catches a bar that
somehow renders anyway.
"""

from __future__ import annotations

import ast

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

#: Deliberately short — see the module docstring before adding to it.
ALLOWED_CONTAINERS: dict[str, str] = {}


def _container_names(tree: ast.Module) -> set[str]:
    """Local names bound to ``discord.ui.Container`` by an import."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "discord.ui":
            for alias in node.names:
                if alias.name == "Container":
                    names.add(alias.asname or alias.name)
    return names


def _is_ui_container_attr(node: ast.expr) -> bool:
    """``discord.ui.Container`` or ``ui.Container`` as an attribute chain."""
    if not (isinstance(node, ast.Attribute) and node.attr == "Container"):
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id == "ui"
    return (
        isinstance(base, ast.Attribute)
        and base.attr == "ui"
        and isinstance(base.value, ast.Name)
        and base.value.id == "discord"
    )


def scan_source(source: str) -> list[tuple[int, str]]:
    """Every container construction or subclassing in one module."""
    tree = ast.parse(source)
    local = _container_names(tree)
    hits: list[tuple[int, str]] = []

    def names_a_container(node: ast.expr) -> bool:
        if _is_ui_container_attr(node):
            return True
        return isinstance(node, ast.Name) and node.id in local

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and names_a_container(node.func):
            hits.append((node.lineno, f"{ast.unparse(node.func)}(...)"))
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if names_a_container(base):
                    hits.append(
                        (node.lineno, f"class {node.name}({ast.unparse(base)})")
                    )
    return sorted(set(hits))


def _scan_tree() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "Container" not in source:
            continue
        hits = scan_source(source)
        if hits:
            found[path.relative_to(SRC).as_posix()] = hits
    return found


# ── the sweep can actually see a violation ──────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import discord\ndef f():\n    return discord.ui.Container()\n",
            id="fully-qualified",
        ),
        pytest.param(
            "from discord import ui\ndef f():\n    return ui.Container(accent_color=1)\n",
            id="ui-prefix",
        ),
        pytest.param(
            "from discord.ui import Container\ndef f():\n    return Container()\n",
            id="bare-import",
        ),
        pytest.param(
            "from discord.ui import Container as Box\ndef f():\n    return Box()\n",
            id="aliased-import",
        ),
        pytest.param(
            "from discord import ui\nclass Card(ui.Container):\n    pass\n",
            id="subclass",
        ),
    ],
)
def test_scanner_flags(source):
    assert scan_source(source), "scanner missed a Container"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import discord\ndef f():\n    return discord.ui.Section(accessory=None)\n",
            id="section-is-fine",
        ),
        pytest.param(
            "class Container:\n    pass\ndef f():\n    return Container()\n",
            id="unrelated-local-class",
        ),
        pytest.param(
            "from mypkg import Container\ndef f():\n    return Container()\n",
            id="unrelated-import",
        ),
    ],
)
def test_scanner_passes(source):
    assert not scan_source(source)


# ── the tree ────────────────────────────────────────────────────────────


def test_no_container_anywhere_in_src():
    found = {k: v for k, v in _scan_tree().items() if k not in ALLOWED_CONTAINERS}
    assert not found, (
        "A Components V2 card is bare — no Container, no box outline, no "
        "coloured bar (docs/components_v2_style_guide.md § No container). "
        "Use a heading, a Separator or a Section instead; a container that "
        "genuinely has to exist is Billy's call, not a local one.\n  "
        + "\n  ".join(
            f"src/{k}:{ln} {what}" for k, hits in sorted(found.items()) for ln, what in hits
        )
    )


def test_allow_list_is_not_stale():
    found = _scan_tree()
    stale = sorted(set(ALLOWED_CONTAINERS) - set(found))
    assert not stale, (
        "ALLOWED_CONTAINERS rows whose site no longer builds one — delete "
        "them so the list stays honest:\n  " + "\n  ".join(stale)
    )
