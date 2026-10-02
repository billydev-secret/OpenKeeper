"""No cog file grows past a line ceiling.

"Cog file" is ``extension_discovery.cog_files``: every extension module, every
file in an extension package, and every file under a ``cogs/`` or ``cog/``
directory, wherever the feature's package sits.

``economy_cog.py`` reached 5,392 lines before it was split (standards audit
row 4.29) into the cog — the command groups, the commands and ``__init__`` —
and ``economy/cog/``: one mixin per area (shop, perks, quests, panels), a
shared base, and the views. A cog that big is where logic hides from the
logic-layer tests and where parallel branches collide.

When a file hits the ceiling, split it the same way (``casino/cog.py`` +
``pools_panel.PoolsMixin`` is the other precedent) or move its logic into a
``*_logic.py`` / ``*_service.py``. Don't raise ``CEILING``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

from bot_modules.core.extension_discovery import cog_files

BOT_MODULES = REPO_ROOT / "src" / "bot_modules"
CEILING = 3000


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


FILES = cog_files(BOT_MODULES)


def test_the_sweep_sees_the_cogs():
    """An empty or shrunken sweep would pass every row below vacuously. Pinned
    near the 136 cog files of 2026-09-29, so a move that drops files out of
    the discovery fails here instead of quietly losing rows."""
    assert len(FILES) >= 130, f"only {len(FILES)} cog files under {BOT_MODULES}"


@pytest.mark.parametrize(
    "path", FILES, ids=lambda p: str(p.relative_to(BOT_MODULES))
)
def test_no_cog_file_passes_the_ceiling(path):
    lines = _line_count(path)
    assert lines <= CEILING, (
        f"{path.relative_to(BOT_MODULES)} is {lines} lines (ceiling {CEILING}). "
        "Split it by area into mixins, as economy_cog.py was, or move the "
        "logic into a *_logic.py / *_service.py."
    )
