"""Extension discovery follows a cog by what it defines, not where it sits.

The boot drift warning, the boot smoke test, the command-tree size test, the
cog size cap and the context-menu count all ask ``extension_discovery``, so a
cog moved out of ``cogs/`` stays covered by every one of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

from bot_modules.core.extension_discovery import cog_files, discover_extensions

SETUP = "async def setup(bot):\n    pass\n"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "bot_modules"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param({"cogs/a_cog.py": SETUP}, {"bot_modules.cogs.a_cog"}, id="flat-cog"),
        pytest.param(
            {"bios/cog.py": SETUP, "bios/__init__.py": ""},
            {"bot_modules.bios.cog"},
            id="cog-in-a-feature-package",
        ),
        pytest.param(
            {"casino/__init__.py": "from .cog import setup\n", "casino/cog.py": SETUP},
            {"bot_modules.casino"},
            id="package-reexporting-setup",
        ),
        pytest.param(
            {"legit/__init__.py": SETUP, "legit/data.py": ""},
            {"bot_modules.legit"},
            id="package-defining-setup",
        ),
        pytest.param(
            {"breaks/service.py": "async def setup_break_channel(ctx):\n    pass\n"},
            set(),
            id="setup-prefix-is-not-setup",
        ),
        pytest.param(
            {"x/__init__.py": "from .views import Board  # setup the board\n", "x/views.py": ""},
            set(),
            id="setup-in-a-trailing-comment-is-not-a-reexport",
        ),
        pytest.param(
            {"x/y.py": "class C:\n    async def setup(self):\n        pass\n"},
            set(),
            id="indented-method-is-not-an-extension",
        ),
    ],
)
def test_discover_extensions(tmp_path, files, expected):
    assert set(discover_extensions(_tree(tmp_path, files))) == expected


def test_cog_files_covers_extensions_packages_and_cog_dirs(tmp_path):
    root = _tree(
        tmp_path,
        {
            "cogs/a_cog.py": SETUP,
            "casino/__init__.py": "from .cog import setup\n",
            "casino/cog.py": SETUP,
            "casino/embeds.py": "",
            "economy/economy_cog.py": SETUP,
            "economy/cog/shop.py": "",
            "economy/wallet.py": "",
        },
    )
    got = {p.relative_to(root).as_posix() for p in cog_files(root)}
    assert got == {
        "cogs/a_cog.py",
        "casino/__init__.py",
        "casino/cog.py",
        "casino/embeds.py",
        "economy/economy_cog.py",
        "economy/cog/shop.py",
    }


def test_the_real_tree_matches_the_registered_list():
    """Discovery and the hand-kept ``extension_names`` agree in both directions
    (81 each on 2026-09-29), so the drift check is neither blind nor noisy."""
    from tests.contracts.test_boot_smoke import EXTENSIONS

    found = set(discover_extensions(REPO_ROOT / "src" / "bot_modules"))
    assert len(found) >= 80
    assert found == set(EXTENSIONS)
