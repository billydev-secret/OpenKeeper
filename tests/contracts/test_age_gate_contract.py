"""Contract: a channel's age gate is read through ``is_age_gated_channel()``.

``TextChannel.is_nsfw()`` and the raw ``nsfw`` attribute report only the
channel's own flag. Discord does not cascade an age-restricted category onto
the channels already inside it, so a channel that is gated only through its
category reads as SFW — the gap that deleted a member's upload on 2026-09-01,
and that let the starboard repost from such a channel into an SFW board until
2026-09-14. ``age_gate.is_age_gated_channel`` reads both levels.

Born green: a pinned baseline of reporting-only reads, which may only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

HELPER = "bot_modules/shared/age_gate.py"

#: Raw reads that exist today and gate nothing — they list NSFW channel ids for
#: dashboard stats and channel pickers. Moving them to the helper changes what
#: those panels count, which is its own decision. Shrink only.
BASELINE = {
    ("dungeonkeeper/__main__.py", "getattr(ch, 'nsfw', False)"),
    ("web_server/routes/health.py", "getattr(ch, 'nsfw', False)"),
    ("web_server/routes/home.py", "getattr(ch, 'nsfw', False)"),
    ("web_server/routes/meta.py", "getattr(ch, 'nsfw', False)"),
}

#: ``nsfw`` attributes that are not a channel's age gate.
NOT_A_CHANNEL = {
    # An app command's own nsfw flag, compared when syncing the command tree.
    ("bot_modules/core/command_sync.py", "getattr(cmd, 'nsfw', False)"),
    # A request body field on the Chat Revive route.
    ("web_server/routes/chat_revive.py", "body.nsfw"),
}


def _raw_reads(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("nsfw", "is_nsfw"):
            hits.append((node.lineno, ast.unparse(node)))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in ("nsfw", "is_nsfw")
        ):
            hits.append((node.lineno, ast.unparse(node)))
    return hits


def _all_reads() -> dict[tuple[str, str], list[int]]:
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == HELPER:
            continue
        for line, expr in _raw_reads(path):
            found.setdefault((rel, expr), []).append(line)
    return found


def test_age_gate_is_read_through_the_helper():
    found = _all_reads()
    offenders = sorted(
        f"src/{rel}:{lines[0]} {expr}"
        for (rel, expr), lines in found.items()
        if (rel, expr) not in BASELINE | NOT_A_CHANNEL
    )
    assert not offenders, (
        "Read a channel's age gate with "
        "age_gate.is_age_gated_channel(channel), not .nsfw / "
        ".is_nsfw() — those miss a category marked age-restricted. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_baseline_only_shrinks():
    found = _all_reads()
    stale = sorted(f"src/{rel} {expr}" for rel, expr in BASELINE | NOT_A_CHANNEL if (rel, expr) not in found)
    assert not stale, f"No longer present — remove from the baseline: {stale}"
