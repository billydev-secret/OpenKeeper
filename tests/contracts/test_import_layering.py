"""The layers import downward: glue may reach logic, logic never reaches glue.

``design_guide.md`` § Layering puts behaviour in the logic/service layer and
calls cogs, views and routes glue. Two import directions break that, and this
pins both:

* **The dashboard imports a cog or a command module.** A route that needs a
  cog's private ``_get_config`` is reading a store that lives in the wrong
  file, and importing it drags discord.py's command tree into the web
  process's import graph.
* **A logic-layer module imports a cog or a command module.** The logic
  layer is the one tested offline; a service that imports a cog can't be
  imported without it.

Both sides are classified by file, not directory, so they hold wherever a
feature's package sits:

* **Glue** is a cog file (``extension_discovery.cog_files``: extension modules,
  extension packages, anything under ``cogs/`` or ``cog/``) or a command module
  (``*_commands.py``, anything under ``commands/``).
* **Logic layer** is every other module under ``bot_modules/`` except the
  presentation files: ``views``, ``embeds``, ``layouts``, ``modals`` and
  ``panel(s)``, bare or as a ``_``-joined part of the name. Views are glue and
  may import a cog — a view opening another feature's modal is exactly what
  they are for.

Imports under ``if TYPE_CHECKING:`` are skipped: they never run, so they
couple nothing at import time.

``ALLOWED`` holds today's crossings, each with the reason it has not moved
yet, and only shrinks — a moved import left listed fails
``test_allowed_only_shrinks``. Standards audit row 4.15.
"""

from __future__ import annotations

import ast
from pathlib import Path
from tests.paths import REPO_ROOT

from bot_modules.core.extension_discovery import cog_files

SRC = REPO_ROOT / "src"
BOT_MODULES = SRC / "bot_modules"

_COG_STORE = (
    "the route reads a store that still lives inside the cog as private "
    "helpers; the fix is moving them into a *_store.py the cog imports too"
)
_LEGITLIBS = (
    "games_legitlibs is a package under cogs/ whose validation/data modules "
    "are pure; the fix is moving the package out of cogs/"
)

ALLOWED: dict[tuple[str, str], str] = {
    ("web_server/routes/config.py", "bot_modules.needle.cog"): _COG_STORE,
    ("web_server/routes/config.py", "bot_modules.bump_tracker.cog"): _COG_STORE,
    ("web_server/routes/config.py", "bot_modules.pen_pals.cog"): _COG_STORE,
    ("web_server/routes/games.py", "bot_modules.games.legitlibs.validation"): _LEGITLIBS,
    ("web_server/routes/games.py", "bot_modules.games.legitlibs.data"): _LEGITLIBS,
    ("web_server/routes/moderation.py", "bot_modules.moderation.jail.jail_commands"): (
        "unjail's role restore lives in the command module; the dashboard's "
        "Release button must run the same restore, not a copy of it"
    ),
    ("bot_modules/inactive/sweep_service.py", "bot_modules.moderation.jail.jail_commands"): (
        "the inactive channel reuses the jail ticket panel's persistent "
        "button class; the fix is moving the button into a views module"
    ),
    ("bot_modules/games/utils/game_manager.py", "bot_modules.todo.cog"): (
        "ticking the game chore repaints the todo board, and repaint_board "
        "lives in the cog; the fix is moving it into a todo board service"
    ),
}

_PRESENTATION = ("views", "view", "embeds", "layouts", "modals", "panel", "panels")


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _module(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_command_module(path: Path) -> bool:
    return path.stem.endswith("_commands") or "commands" in path.relative_to(
        BOT_MODULES
    ).parts[:-1]


def _is_presentation(path: Path) -> bool:
    stem = path.stem
    return any(
        stem == p or stem.startswith(p + "_") or stem.endswith("_" + p)
        for p in _PRESENTATION
    )


def _glue_files() -> set[Path]:
    return set(cog_files(BOT_MODULES)) | {
        p for p in _py_files(BOT_MODULES) if _is_command_module(p)
    }


GLUE = frozenset(_module(p) for p in _glue_files())


def _checked_files() -> list[Path]:
    glue = _glue_files()
    files = _py_files(SRC / "web_server")
    files += [
        p for p in _py_files(BOT_MODULES)
        if p not in glue and not _is_presentation(p)
    ]
    return files


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_imports(tree: ast.AST) -> list[str]:
    """Every module a file imports at run time, TYPE_CHECKING blocks pruned."""
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _crossings() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path in _checked_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()
        for module in _runtime_imports(tree):
            if module in GLUE or any(module.startswith(g + ".") for g in GLUE):
                out.add((rel, module))
    return out


def test_no_new_import_reaches_up_into_glue():
    new = sorted(_crossings() - ALLOWED.keys())
    assert not new, (
        "These import a cog or command module from a layer below it:\n  "
        + "\n  ".join(f"{f} -> {m}" for f, m in new)
        + "\nMove what they need into a *_logic.py / *_store.py / *_service.py "
        "that the cog imports too (design_guide.md § Layering)."
    )


def test_allowed_only_shrinks():
    stale = sorted(ALLOWED.keys() - _crossings())
    assert not stale, f"No longer imported — remove from ALLOWED: {stale}"


def test_the_classification_sees_the_tree():
    """Floors pinned to 2026-09-29 (140 glue modules, 433 checked files), so
    a move that drops files out of either side fails instead of shrinking the
    check."""
    assert len(GLUE) >= 135
    assert len(_checked_files()) >= 425


def test_the_walk_skips_type_checking_and_sees_function_local_imports():
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from bot_modules.cogs.a_cog import ACog\n"
        "def f():\n"
        "    from bot_modules.cogs.b_cog import helper\n"
        "    import bot_modules.commands.c_commands\n"
    )
    found = _runtime_imports(tree)
    assert "bot_modules.cogs.a_cog" not in found
    assert "bot_modules.cogs.b_cog" in found
    assert "bot_modules.commands.c_commands" in found
