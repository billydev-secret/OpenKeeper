"""No real Discord snowflake in the public tree (design_guide.md § The docs contract).

A 17-20 digit number is a guild, channel, role, message or user id. In this
repo one is *synthetic* when it carries a run of six or more repeated digits
(``1284999999999999995``) or is one of the obvious fakes below; anything else
must be in ``ALLOWED`` with the reason it is allowed. Migrations are hash-pinned
and keep what they were written with; the ids they pin are the allowlist's core.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from tests.paths import REPO_ROOT

SNOWFLAKE = re.compile(r"(?<![\w.])\d{17,20}(?![\w.])")
RUN = re.compile(r"(\d)\1{5}")
#: Other shapes the suite uses for made-up ids: a counting run, a two-digit
#: pattern repeated (424242…, 515151…), the 555000 / 999000 test guilds.
COUNTING = re.compile(r"123456789|987654321|(\d\d)\1{3}")
FAKES = {"555000555000555549", "999000999000999549"}

#: id -> why a real-looking id may stay.
ALLOWED: dict[str, str] = {
    "1469491362444480666": "the home guild: pinned by migration 104 and the tests that replay it",
    "966695034340663367": "pinned by a migration seed",
    "826100334534328340": "pinned by a migration seed",
    "1502766814315282553": "pinned by migration 217",
    "1159147139960676422": "pinned by a migration seed",
    "1222548162741538938": "the public Discadia bump bot (migration 137)",
    "302050872383242240": "the public Disboard bump bot",
    "620307267241377793": "the public Gamebot (external games)",
    "1211781489931452447": "the public Wordle bot (external games)",
    "1071892566158614608": "the public Coordle bot (external games)",
    "1527184897775763549": "the testing-queue channel scripts/post_testing_docs.py posts to (host tooling)",
}

SCAN_DIRS = ("docs", "tests", "src", "scripts", "deploy", ".github", ".claude")
ROOT_FILES = ("CLAUDE.md", "README.md", "CONTRIBUTING.md", "SECURITY.md", "pyproject.toml")
SUFFIXES = {".py", ".md", ".json", ".js", ".html", ".yml", ".yaml", ".toml", ".sh", ".sql", ".txt", ".css"}
SKIP = ("docs/plans/", "__pycache__/", "node_modules/", "/static/vendor/")
#: Files whose ids are not Discord objects of this guild or anyone's.
SKIP_FILES = {
    "src/bot_modules/games/mahjong/tile_emoji.json",  # application-owned emoji ids
    "tests/tooling/test_no_real_identifiers.py",
}


def _wanted(rel: str) -> bool:
    if rel in SKIP_FILES or any(s in rel for s in SKIP):
        return False
    if rel in ROOT_FILES:
        return True
    return rel.startswith(tuple(d + "/" for d in SCAN_DIRS)) and Path(rel).suffix in SUFFIXES


def _tracked_files() -> list[str] | None:
    """What git tracks, or None where there is no repository (the remote
    runner syncs files without ``.git``). A gitignored file in a scanned
    directory — ``.claude/settings.local.json``, a plan under ``docs/plans/``
    — can never ship, so it must not fail the guard either."""
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        return None
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        return None
    return [f for f in proc.stdout.split("\0") if f]


def _files():
    tracked = _tracked_files()
    if tracked is not None:
        yield from (rel for rel in sorted(tracked) if _wanted(rel))
        return
    for f in ROOT_FILES:
        if (REPO_ROOT / f).is_file():
            yield f
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(REPO_ROOT).as_posix()
                if _wanted(rel):
                    yield rel


def _is_synthetic(sf: str) -> bool:
    return sf in FAKES or bool(RUN.search(sf)) or bool(COUNTING.search(sf))


def test_every_snowflake_is_synthetic_or_allowed():
    offenders: list[str] = []
    for rel in _files():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            for sf in SNOWFLAKE.findall(line):
                if _is_synthetic(sf) or sf in ALLOWED:
                    continue
                offenders.append(f"{rel}:{n}: {sf}")
    assert not offenders, (
        "Real-looking Discord ids in the public tree — replace with a synthetic id "
        "(six repeated digits) or add to ALLOWED with the reason:\n  " + "\n  ".join(offenders[:40])
    )


def test_allowlist_entries_are_still_used():
    """An allowlist row nobody references is a stale exception."""
    text = "\n".join((REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore") for rel in _files())
    unused = [sf for sf in ALLOWED if sf not in text]
    assert not unused, f"ALLOWED rows no file uses any more: {unused}"


def test_synthetic_rule():
    assert _is_synthetic("1284999999999999995") and _is_synthetic("700000000000000009")
    assert _is_synthetic("123456789012345678") and _is_synthetic("424242424242424242")
    assert _is_synthetic("1469123456789012345")
    assert not _is_synthetic("1469491362444480666")
    assert not SNOWFLAKE.search("1759276800000")  # a millisecond timestamp is 13 digits
