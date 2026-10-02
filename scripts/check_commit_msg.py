#!/usr/bin/env python3
"""Reject a commit message carrying an AI attribution trailer.

The ``commit-msg`` half of the rule in CLAUDE.md § Commits. The other half is
``tests/contracts/test_commit_trailer_contract.py``, which scans history and so catches
a ``--no-verify`` bypass or a checkout where this hook was never installed;
this one catches it at the only moment the message is still cheap to change.

Rejects rather than strips. Silently editing someone's commit message is a
surprise the next reader cannot account for, and the failure is worth seeing
once: the trailers come from tooling defaults, so a session that hits this is
a session running with the wrong default, which is worth knowing.

**Only trailer-shaped lines count** — a line whose first non-space characters
are the forbidden key. Prose *about* the rule (this docstring, a commit body
explaining the guard) does not match, because there the key sits inside a
sentence or inside backticks. A message that genuinely needs the key at the
start of a line can quote it, indent it, or use ``--no-verify``.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Matched case-insensitively at the start of a line. Two entries because a
#: message can carry the session URL without the co-author line.
FORBIDDEN = ("co-authored-by: claude", "claude-session:")


def offending_lines(message: str) -> list[tuple[int, str]]:
    """``(1-based line number, line)`` for every trailer-shaped violation."""
    found = []
    for number, line in enumerate(message.splitlines(), start=1):
        if line.lstrip().startswith("#"):  # a comment git will strip anyway
            continue
        lowered = line.strip().lower()
        if any(lowered.startswith(key) for key in FORBIDDEN):
            found.append((number, line.rstrip()))
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <commit-msg-file>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        message = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read the commit message ({exc})", file=sys.stderr)
        return 2

    offenders = offending_lines(message)
    if not offenders:
        return 0

    print("This commit message carries a trailer CLAUDE.md forbids:", file=sys.stderr)
    for number, line in offenders:
        print(f"  line {number}: {line}", file=sys.stderr)
    print(
        "\nDelete those lines and commit again. They name a co-author who "
        "cannot be contacted and publish a URL that resolves for nobody else, "
        "in the one record of intent this repo keeps.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main(sys.argv))
