"""Will the bot actually start?

`setup_hook` loads every name in `extension_names` in a loop, and `__main__`
says the quiet part in its own comment: one bad cog takes the whole process.
A restart is when that gets discovered, which is the worst possible moment —
it is the same keystroke that puts new code in front of members.

Nothing else covers this. Every other cog test imports one or two modules;
only the whole list hits an import that a refactor broke three cogs away.
"""

from __future__ import annotations

import ast
import importlib

import pytest
from tests.paths import REPO_ROOT

_ROOT = REPO_ROOT
_MAIN = _ROOT / "src" / "dungeonkeeper" / "__main__.py"
_BOT_MODULES = _ROOT / "src" / "bot_modules"


def _extension_names() -> list[str]:
    """The hand-maintained list, read without importing `__main__`.

    Importing the entry point would run argument parsing and construct a Bot;
    the list is a plain literal, so parse it instead.
    """
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "extension_names"
            and isinstance(node.value, ast.List)
        ):
            return [
                el.value for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
    raise AssertionError("could not find bot.extension_names in __main__.py")


def _discovered() -> set[str]:
    """Cogs on disk, by the rule `_warn_on_extension_drift` uses: anything
    under ``bot_modules/`` defining ``async def setup(``, wherever it lives.

    The reverse direction — a list entry that no longer exists — needs no test
    of its own: it fails the import check above, which is the honest signal.
    """
    from bot_modules.core.extension_discovery import discover_extensions

    return set(discover_extensions(_BOT_MODULES))


EXTENSIONS = _extension_names()


def test_the_extension_list_is_not_empty():
    """A parse that silently returned [] would make every check below vacuous."""
    assert len(EXTENSIONS) > 50, f"only found {len(EXTENSIONS)} extensions — parse broke?"


@pytest.mark.parametrize("module", EXTENSIONS)
def test_every_registered_extension_imports(module):
    """Import, not load: `setup()` needs a Bot, but an ImportError, a bad
    module-level constant or a circular import all fire here — and those are
    what actually kill a boot."""
    importlib.import_module(module)


def test_no_cog_on_disk_is_missing_from_the_list():
    """`_warn_on_extension_drift` logs this at boot; nobody reads boot logs
    before a restart. A cog nobody registered is a feature that silently
    never loads."""
    missing = sorted(_discovered() - set(EXTENSIONS))
    assert not missing, (
        f"{len(missing)} cog(s) on disk are not in extension_names and will "
        f"never load: {missing} — add them in dungeonkeeper/__main__.py"
    )
