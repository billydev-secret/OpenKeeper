#!/usr/bin/env python3
"""Regenerate the migration hash manifest in tests/migrations/test_migration_manifest.py.

Run this when you add a migration, rename one before it ships, or deliberately
edit one that has already shipped::

    python scripts/migration_manifest.py

It recomputes every hash and rewrites the block between the two MANIFEST
markers, sorted by filename. An untouched migration's line comes out
byte-identical, so the diff names exactly the files whose content moved — which
is the whole point. Review that diff before committing it; a line changing under
a filename that has already run in production is the question this exists to ask.

Same shape as ``uv pip compile`` for requirements*.lock: a human runs it, reads
the diff, and commits the source change and the regenerated artifact together.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "migrations"
TEST_FILE = ROOT / "tests" / "migrations" / "test_migration_manifest.py"

BEGIN = "# --- BEGIN MANIFEST"
END = "# --- END MANIFEST ---"


def sha256_text(path: Path) -> str:
    """Hash the file the way the runner reads it: decoded UTF-8, not raw bytes.

    ``apply_migrations_sync`` does ``path.read_text(encoding="utf-8")``, and
    matching that exactly is what makes the hash stable across the Linux dev box
    and the Windows remote test runner — universal-newline translation happens
    inside ``read_text`` on both, before anything is hashed.
    """
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def migration_files() -> list[Path]:
    """The same glob the runner uses, so the manifest can never cover a different set."""
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))


def render_block() -> str:
    lines = [
        f"{BEGIN} — regenerate with scripts/migration_manifest.py ---",
        "MANIFEST: dict[str, str] = {",
    ]
    for path in migration_files():
        lines.append(f'    "{path.name}": "{sha256_text(path)}",')
    lines.append("}")
    lines.append("")
    lines.append(f'RUNNER_SHA = "{sha256_text(MIGRATIONS / "__init__.py")}"')
    lines.append(END)
    return "\n".join(lines)


def main() -> int:
    text = TEST_FILE.read_text(encoding="utf-8")
    start = text.find(BEGIN)
    end = text.find(END)
    if start == -1 or end == -1:
        print(f"markers not found in {TEST_FILE}", file=sys.stderr)
        return 1
    updated = text[:start] + render_block() + text[end + len(END) :]
    if updated == text:
        print(f"{len(migration_files())} migrations — manifest already current")
        return 0
    TEST_FILE.write_text(updated, encoding="utf-8")
    print(f"{len(migration_files())} migrations — manifest rewritten; review the diff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
