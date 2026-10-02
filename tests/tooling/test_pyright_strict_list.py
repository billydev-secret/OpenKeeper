"""The pyright strict list only grows.

``[tool.pyright] strict`` in pyproject.toml holds the files type-checked in
strict mode — seeded with the logic modules that already passed it, so the
list landed green. Pyright itself (CI's ``pyright`` job, ``gate.py --quick``)
proves each listed file still passes; this pins the list so a file can't
quietly leave it to dodge an error.

``FLOOR`` is the list's length: raise it when a file joins, never lower it.
A moved or deleted file fails ``test_every_listed_file_exists`` — rename the
entry rather than drop it. Standards audit row 3.17.
"""

from __future__ import annotations

import tomllib
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT
FLOOR = 32


def _strict() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["pyright"]["strict"]


def test_the_list_never_shrinks():
    assert len(_strict()) >= FLOOR, (
        f"The pyright strict list fell below {FLOOR}. Fix the file's type "
        "errors instead of removing it from the list."
    )


def test_every_listed_file_exists():
    missing = [f for f in _strict() if not (ROOT / f).is_file()]
    assert not missing, f"Listed as strict but not on disk (renamed?): {missing}"


def test_the_list_is_sorted_and_unique():
    entries = _strict()
    assert entries == sorted(set(entries))
