"""Find the bot's extensions (cogs) on disk by what they define, not where they sit.

A module is an extension when it defines a top-level ``async def setup(``. A
package is one when its ``__init__.py`` defines ``setup`` or re-exports it from
a submodule (``from .cog import setup``); that submodule is then part of the
package's extension, not one of its own.

Every check that used to glob ``cogs/`` asks this instead: the boot-time drift
warning, the boot smoke test, the command-tree size test, the cog size cap and
the context-menu count. A directory glob narrows silently when a cog moves out
of ``cogs/``, and the check keeps passing with fewer rows. This rule follows
the cog to wherever it lives.
"""

from __future__ import annotations

import re
from pathlib import Path

_SETUP = re.compile(r"^async def setup\(", re.M)
_REEXPORT = re.compile(r"^from \.(\w+) import [^\n#]*\bsetup\b", re.M)

#: Directories whose every file is cog code, extension or not: today's
#: ``cogs/``, and a feature package's ``cog/`` mixin folder after the move.
_COG_DIRS = frozenset({"cogs", "cog"})


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _module_name(path: Path, package_root: Path) -> str:
    parts = list(path.relative_to(package_root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _reexported_submodule(init: Path) -> str | None:
    if not init.exists():
        return None
    match = _REEXPORT.search(init.read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else None


def discover_extensions(package_root: Path) -> dict[str, Path]:
    """Map each extension's dotted name to its file or package directory.

    *package_root* is the ``bot_modules`` directory; names come out as
    ``bot_modules.<...>``, the form ``extension_names`` lists.
    """
    found: dict[str, Path] = {}
    for path in _python_files(package_root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.name == "__init__.py":
            if _SETUP.search(text) or _REEXPORT.search(text):
                found[_module_name(path, package_root)] = path.parent
        elif _SETUP.search(text):
            if _reexported_submodule(path.parent / "__init__.py") == path.stem:
                continue  # its package is the extension
            found[_module_name(path, package_root)] = path
    return found


def cog_files(package_root: Path) -> list[Path]:
    """Every file that is cog code: an extension module, any file inside an
    extension package, and any file under a ``cogs/`` or ``cog/`` directory
    (mixins the extension imports)."""
    files: set[Path] = set()
    for target in discover_extensions(package_root).values():
        files.update(_python_files(target) if target.is_dir() else [target])
    for path in _python_files(package_root):
        rel_dirs = path.relative_to(package_root).parts[:-1]
        if _COG_DIRS.intersection(rel_dirs):
            files.add(path)
    return sorted(files)
