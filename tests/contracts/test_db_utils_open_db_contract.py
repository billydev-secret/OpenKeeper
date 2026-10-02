"""Contract: connections go through ``open_db``, never a hand-rolled ``sqlite3.connect``.

``open_db`` owns the pragma set (WAL, busy timeout, synchronous, cache, mmap)
and closes the connection. A copied pragma block drifts the next time the set
changes, and ``with sqlite3.connect(...) as conn`` commits but never closes —
Risky Rolls' store did both until 2026-09-14.

Born green: it passes today, and exists to fail on the next copy.
"""

from __future__ import annotations

import ast
from pathlib import Path
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

#: Files allowed to call ``sqlite3.connect`` directly, and why.
ALLOWED = {
    # Defines open_db / open_db_immediate.
    "bot_modules/core/db_utils.py": "the helper itself",
    # The backup API needs a read-only URI source and a bare destination file,
    # neither of which open_db (WAL pragma, transaction) can open.
    "bot_modules/admin/db_backup.py": "sqlite backup API",
    # Migrations run before anything else, on the runner's own transaction terms.
    "migrations/__init__.py": "migration runner",
}


def _connect_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
    ]


def test_no_raw_sqlite_connect_outside_open_db():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        offenders += [f"src/{rel}:{line}" for line in _connect_calls(path)]
    assert not offenders, (
        "Open connections with bot_modules.core.db_utils.open_db (or ctx.open_db), "
        "not sqlite3.connect — it owns the pragma set and closes the connection. "
        f"Offenders: {offenders}"
    )


def test_allowlist_has_no_stale_entries():
    """An allowlisted file that no longer connects directly should leave the list."""
    stale = [rel for rel in ALLOWED if not _connect_calls(SRC / rel)]
    assert not stale, f"No sqlite3.connect left in {stale}; drop them from ALLOWED"
