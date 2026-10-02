"""What a release would send to the public mirror, scanned before it leaves.

``scripts/publish_release.py`` publishes HEAD's tree minus ``.publishignore``.
This file builds exactly that tree and reads it back, so every check here is
on the bytes that would ship — not the working tree, which also holds the
gitignored private material, and not ``.publishignore``, which a stray edit
could shorten. ``publish`` runs this file as a guard; CI's full run and the
pre-release gate run it too. It needs the repository (skips on the remote
runner, which syncs files without ``.git``).

Four contracts, each its own test so a failure names the one that broke:

* nothing under the paths that must never ship (``assets/`` is the server's
  own branding: fonts, borders, logo, panel art — Ben, 2026-10-01: "that
  folder stays blank"), and no image, font or audio file anywhere;
* no real-looking Discord id, by the same rule as
  ``test_no_real_identifiers`` but over the shipped blobs;
* no email address off the placeholder domains;
* nothing on the private denylist — strings that name the maintainers, their
  members or their machines, kept in ``private/`` (gitignored, so the list
  itself can never ship) and never printed on failure.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import publish_release as pr  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs the repository (the remote runner syncs files without .git)",
)

#: Pinned here, independent of .publishignore: the publish flow's own contract.
MUST_NOT_SHIP = (
    "assets/",
    "private/",
    "docs/plans/",
    "docs/server_map.md",
    ".env",
    ".github/workflows/claude.yml",
    ".github/workflows/claude-code-review.yml",
    # The maintainers' MCP server and its deployment: host paths, the
    # tunnel's name, the operator's unit (Ben, 2026-10-01).
    "src/dk_mcp/",
    "deploy/dk-mcp.service",
    "scripts/deploy_dk_mcp.sh",
    # The maintainers' Claude Code session tooling (Ben, 2026-10-01).
    ".claude/",
    "scripts/dk_session.py",
    "scripts/systemd/",
)
MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".psd",
    ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".wav", ".ogg", ".mp4", ".webm",
    ".db", ".sqlite", ".sqlite3",
}  # fmt: skip
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PLACEHOLDER_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "example.invalid",
    "mirror.test",
    "users.noreply.github.com",
    "github.com",  # the ssh remote syntax, git@github.com:owner/repo
    "anthropic.com",  # the trailer checker's fixture strings
}
#: Third-party metadata with other people's addresses in it.
EMAIL_SKIP_FILES = {"package-lock.json"}
#: The dashboard's self-hosted web fonts (OFL 1.1, static/fonts/README.md):
#: the one place a font file may ship.
MEDIA_ALLOWED_DIRS = ("src/web_server/static/fonts/",)
PRIVATE_DIR = REPO_ROOT / "private"
#: Set by publish_release.run_guard_tests. Under it a missing denylist is a
#: failure, not a skip: a release must never be cleared by a check that
#: could not run. Elsewhere (CI, a session worktree) the denylist half skips.
PUBLISHING = os.environ.get("DK_PUBLISH_GUARD") == "1"
#: Shorter denylist entries match inside ordinary words; they are refused
#: rather than silently skipped.
MIN_DENYLIST_LEN = 4


def _load_identifier_rules():
    """The synthetic-id rule and allowlist from test_no_real_identifiers,
    loaded by path so this file does not depend on test-package imports."""
    path = Path(__file__).with_name("test_no_real_identifiers.py")
    spec = importlib.util.spec_from_file_location("no_real_identifiers", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def public_tree() -> dict[str, bytes]:
    """path -> bytes of every file the publish flow would send from HEAD."""
    head = pr.out(REPO_ROOT, "rev-parse", "HEAD")
    tree, _removed = pr.build_public_tree(REPO_ROOT, head, pr.load_excludes(REPO_ROOT))
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", tree],
        capture_output=True,
        check=True,
    ).stdout
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isfile():
                extracted = tar.extractfile(member)
                assert extracted is not None
                files[member.name] = extracted.read()
    assert len(files) > 1000, "the public tree came back nearly empty — the build is broken, not clean"
    return files


def _text_files(files: dict[str, bytes]):
    for path, data in files.items():
        if b"\0" not in data[:8000]:
            yield path, data.decode("utf-8", errors="replace")


# ── 1. paths ──────────────────────────────────────────────────────────


def test_withheld_paths_and_media_never_ship(public_tree: dict[str, bytes]) -> None:
    withheld = sorted(p for p in public_tree if pr.excluded(p, list(MUST_NOT_SHIP)) or Path(p).name == ".env")
    assert withheld == [], f"paths that must stay private are in the public tree: {withheld[:20]}"
    media = sorted(
        p
        for p, data in public_tree.items()
        if (Path(p).suffix.lower() in MEDIA_SUFFIXES and not p.startswith(MEDIA_ALLOWED_DIRS))
        or data.startswith(b"SQLite format 3\0")  # an extensionless `db` was committed once
    )
    assert media == [], f"media ships in the public tree (images, fonts and audio are the server's own): {media[:20]}"


def test_the_publishignore_still_withholds_the_contract() -> None:
    """A shortened .publishignore is caught by the tree test above, but this
    says *which* rule went missing."""
    rules = pr.load_excludes(REPO_ROOT)
    missing = [
        must
        for must in MUST_NOT_SHIP
        if pr.out(REPO_ROOT, "ls-files", "--", must) and not pr.excluded(must.rstrip("/"), rules)
    ]
    assert missing == [], f".publishignore no longer withholds tracked paths: {missing}"


# ── 2. ids ────────────────────────────────────────────────────────────


def test_no_real_looking_discord_id_ships(public_tree: dict[str, bytes]) -> None:
    ids = _load_identifier_rules()
    offenders: list[str] = []
    for path, text in _text_files(public_tree):
        if Path(path).suffix not in ids.SUFFIXES or path in ids.SKIP_FILES or any(s in path for s in ids.SKIP):
            continue
        for match in ids.SNOWFLAKE.finditer(text):
            sf = match.group(0)
            if not ids._is_synthetic(sf) and sf not in ids.ALLOWED:
                offenders.append(f"{path}: {sf}")
    assert offenders == [], "real-looking ids in the public tree:\n" + "\n".join(offenders[:40])


# ── 3. emails ─────────────────────────────────────────────────────────


def test_no_email_off_the_placeholder_domains(public_tree: dict[str, bytes]) -> None:
    offenders: list[str] = []
    for path, text in _text_files(public_tree):
        if Path(path).name in EMAIL_SKIP_FILES:
            continue
        for match in EMAIL.finditer(text):
            domain = match.group(1).lower()
            if domain not in PLACEHOLDER_DOMAINS and not domain.endswith((".example.com", ".invalid", ".test")):
                offenders.append(f"{path}: ***@{domain}")
    assert sorted(set(offenders)) == [], "email addresses in the public tree:\n" + "\n".join(sorted(set(offenders))[:40])


# ── 4. the private denylist ───────────────────────────────────────────


def load_denylist(private_dir: Path) -> list[str]:
    """Strings that must not appear in a public tree, from three private
    sources: ``publish-denylist.txt`` (one per line, ``#`` comments), the
    left-hand side of every ``old==>new`` line in a ``replace-text-*.txt``
    written for ``git filter-repo`` (``regex:`` and ``glob:`` lines are not
    literals and are skipped), and every mailmap address that is not a
    placeholder. Case-insensitive, matched literally; an entry shorter than
    ``MIN_DENYLIST_LEN`` is an error, not a wildcard."""
    entries: list[str] = []
    if (private_dir / "publish-denylist.txt").exists():
        for raw in (private_dir / "publish-denylist.txt").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    for path in sorted(private_dir.glob("replace-text-*.txt")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "==>" not in raw or raw.startswith(("#", "regex:", "glob:")):
                continue
            old = raw.split("==>", 1)[0]
            old = old.removeprefix("literal:").strip()
            if old:
                entries.append(old)
    for path in sorted(private_dir.glob("mailmap-*.txt")):
        for match in EMAIL.finditer(path.read_text(encoding="utf-8")):
            if match.group(1).lower() not in PLACEHOLDER_DOMAINS:
                entries.append(match.group(0))
    short = [e for e in entries if len(e) < MIN_DENYLIST_LEN]
    if short:
        raise ValueError(f"{len(short)} denylist entries are shorter than {MIN_DENYLIST_LEN} chars and would match inside words")
    return sorted(set(entries), key=str.lower)


def test_nothing_on_the_private_denylist_ships(public_tree: dict[str, bytes]) -> None:
    entries = load_denylist(PRIVATE_DIR) if PRIVATE_DIR.is_dir() else []
    if not entries:
        why = "no private/ directory here" if not PRIVATE_DIR.is_dir() else "private/ holds no denylist entries"
        if PUBLISHING:
            pytest.fail(f"{why}: a release cannot be cleared by a denylist scan that did not run (docs/publishing.md § The leak scan)")
        pytest.skip(f"{why} — a session worktree, CI or another machine; the prod checkout runs this")
    pattern = re.compile("|".join(re.escape(e) for e in entries), re.IGNORECASE)
    index = {e.lower(): i for i, e in enumerate(entries)}
    # Report the entry's number and length, never the string: this output
    # lands in CI logs and terminal scrollback.
    offenders: list[str] = []
    for path, text in _text_files(public_tree):
        for match in pattern.finditer(text):
            key = match.group(0).lower()
            offenders.append(f"{path}: denylist entry #{index.get(key, '?')} ({len(key)} chars)")
    assert sorted(set(offenders)) == [], "private strings in the public tree:\n" + "\n".join(sorted(set(offenders))[:40])


def test_load_denylist_reads_all_three_sources_and_drops_placeholders(tmp_path: Path) -> None:
    # Assembled so this file, which ships, holds no literal off-domain address.
    real = "someone@" + "realmail.tld"
    (tmp_path / "publish-denylist.txt").write_text("# names\nSome Person\n\n  a-handle  \n", encoding="utf-8")
    (tmp_path / "replace-text-2026-01-01.txt").write_text(
        "# comment\nOld Name==>New Name\nliteral:old_handle==>new_handle\nregex:\\d+==>N\nglob:x*==>y\n",
        encoding="utf-8",
    )
    (tmp_path / "mailmap-2026-01-01.txt").write_text(
        "handle <1+handle@users.noreply.github.com> gate <gate@example.com>\n"
        f"handle <1+handle@users.noreply.github.com> <{real}>\n",
        encoding="utf-8",
    )
    assert load_denylist(tmp_path) == ["a-handle", "Old Name", "old_handle", "Some Person", real]
    assert load_denylist(tmp_path / "missing") == []


def test_a_short_denylist_entry_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "publish-denylist.txt").write_text("Ok Name\nabc\n", encoding="utf-8")
    with pytest.raises(ValueError, match="shorter than 4"):
        load_denylist(tmp_path)


def test_a_missing_denylist_fails_a_publishing_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-open case: under DK_PUBLISH_GUARD the denylist test must
    fail, not skip, when private/ is absent."""
    monkeypatch.setattr(sys.modules[__name__], "PRIVATE_DIR", tmp_path / "absent")
    monkeypatch.setattr(sys.modules[__name__], "PUBLISHING", True)
    with pytest.raises(pytest.fail.Exception, match="cannot be cleared"):
        test_nothing_on_the_private_denylist_ships({})
