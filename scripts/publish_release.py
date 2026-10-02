#!/usr/bin/env python3
"""Publish a release of this repo to its public mirror, one commit per release.

This checkout keeps its full history private. The public repo sees only
*snapshots*: each release is one commit on the local ``public`` branch whose
tree is the release commit's tree minus the paths in ``.publishignore``, and
whose parent is the previous release. The branch is pushed to the mirror's
``main``, so the public history is a flat, linear chain of releases that can
be diffed and forked, while nothing reachable from it points back at the
private history — it is built from tree objects, so not a single private
commit, author line or message leaves this machine.

    scripts/publish_release.py prepare vX.Y.Z           bump pyproject + retitle
                                                        CHANGELOG § Unreleased,
                                                        commit "Release: vX.Y.Z"
    scripts/publish_release.py publish vX.Y.Z           checks, build the public
                                                        commit + tags, print the push
    scripts/publish_release.py publish vX.Y.Z --push    …and push it
    scripts/publish_release.py publish vX.Y.Z --dry-run checks only, write nothing

``publish`` refuses unless HEAD is a clean ``main`` whose pyproject version
matches, the CHANGELOG has a non-empty ``## vX.Y.Z`` section (it becomes the
public commit message), a LICENSE is present, no tracked file matches
.gitignore, no secret-shaped string is in the tree, and the identifier guard
test passes. The push is a separate flag because it is the one irreversible
step. See docs/publishing.md.

Tags: ``vX.Y.Z`` is an annotated tag on the *public* commit and is pushed
verbatim, so the name means the same object in both repos and the mirror's
GitHub Release can be cut from it; ``release/vX.Y.Z`` marks the private
source commit on ``main``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PUBLIC_BRANCH = "public"
DEFAULT_REMOTE = "public"
SOURCE_BRANCH = "main"
IGNORE_FILE = ".publishignore"
CHANGELOG = "CHANGELOG.md"
UNRELEASED_HEADING = "## Unreleased"
LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
AUTHOR_RE = re.compile(r"^\s*(.+?)\s*<([^<>]+)>\s*$")

#: Tests that must pass before a snapshot leaves the machine. Paths relative
#: to the repo root; ``publish --skip-tests`` is the escape hatch for a repo
#: that has no suite (the publish tests build one in a temp dir).
GUARD_TESTS = (
    "tests/tooling/test_no_real_identifiers.py",
    "tests/tooling/test_public_tree_leaks.py",
)

#: Secret shapes that must never be in a published tree. Extended regexes,
#: run by ``git grep -E`` over the release commit (not the working tree).
#: ERE has no lookbehind, so ``_B`` is the left word boundary: without it
#: ``task-queue-…`` would read as an ``sk-`` key.
_B = r"(^|[^A-Za-z0-9_])"
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Discord bot token", _B + r"[MN][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
    ("GitHub token", _B + r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("sk- API key", _B + r"sk-[A-Za-z0-9_-]{20,}"),
    ("AWS access key", _B + r"AKIA[0-9A-Z]{16}"),
    ("Slack token", _B + r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class PublishError(Exception):
    """A precondition failed; the message says which and what to do."""


# ── git plumbing ──────────────────────────────────────────────────────


def git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=full_env,
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def out(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input: str | None = None,
    check: bool = True,
) -> str:
    return git(repo, *args, env=env, input=input, check=check).stdout.strip()


def rev(repo: Path, ref: str) -> str | None:
    """The sha ``ref`` names, or None when it doesn't exist."""
    proc = git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
    return proc.stdout.strip() or None


def identity_env(author: str) -> dict[str, str]:
    m = AUTHOR_RE.match(author)
    if not m:
        raise PublishError(f'author must look like "Name <email>", got {author!r}')
    name, email = m.group(1), m.group(2)
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def resolve_author(repo: Path, author: str | None) -> str:
    """``--author``, else ``git config publish.author``. Never the repo's own
    ``user.*`` identity: a public commit's author is chosen once, on purpose,
    not inherited from whichever checkout ran the script."""
    if author:
        return author
    configured = out(repo, "config", "--get", "publish.author", check=False)
    if configured:
        return configured
    raise PublishError('no public author: set `git config publish.author "Name <email>"` or pass --author')


# ── release metadata ──────────────────────────────────────────────────


def parse_version(tag: str) -> str:
    m = VERSION_RE.match(tag)
    if not m:
        raise PublishError(f"version must look like vX.Y.Z, got {tag!r}")
    return tag[1:]


def pyproject_version(text: str) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise PublishError("pyproject.toml has no version = \"…\" line")
    return m.group(1)


def set_pyproject_version(text: str, version: str) -> str:
    new, n = re.subn(r'^version\s*=\s*"[^"]+"', f'version = "{version}"', text, count=1, flags=re.M)
    if n != 1:
        raise PublishError("pyproject.toml has no version = \"…\" line")
    return new


def heading_index(lines: list[str], heading: str) -> int | None:
    """The first line that is ``heading`` exactly or ``heading (…)`` — the
    one place both the parser and the retitle look, so they cannot disagree."""
    return next(
        (i for i, line in enumerate(lines) if line.rstrip() == heading or line.startswith(heading + " (")),
        None,
    )


def changelog_section(text: str, heading: str) -> str | None:
    """The body under ``heading`` — stripped, up to the next ``## ``
    heading — or None when there is no such heading."""
    lines = text.splitlines()
    start = heading_index(lines, heading)
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def retitle_unreleased(text: str, tag: str, today: dt.date) -> str:
    """``## Unreleased`` becomes ``## vX.Y.Z (date)`` with a fresh, empty
    ``## Unreleased`` above it — the empty body is what makes the next
    ``prepare`` refuse until someone writes release notes."""
    body = changelog_section(text, UNRELEASED_HEADING)
    if body is None:
        raise PublishError(f"{CHANGELOG} has no '{UNRELEASED_HEADING}' section")
    if not body:
        raise PublishError(f"{CHANGELOG} § Unreleased is empty — write the release notes first")
    lines = text.splitlines(keepends=True)
    i = heading_index(lines, UNRELEASED_HEADING)
    assert i is not None  # changelog_section found it above
    lines[i] = f"{UNRELEASED_HEADING}\n\n## {tag} ({today.isoformat()})\n"
    return "".join(lines)


# ── the public tree ───────────────────────────────────────────────────


def load_excludes(repo: Path) -> list[str]:
    path = repo / IGNORE_FILE
    if not path.exists():
        return []
    rules = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/") or "**" in line or line.startswith("!"):
            raise PublishError(
                f"{IGNORE_FILE}: {line!r} is gitignore syntax; a rule here is a path from the repo root, "
                "a directory with a trailing slash, or a glob over the full path"
            )
        rules.append(line)
    return rules


def excluded(path: str, rules: list[str]) -> bool:
    """A rule is a file path, a directory (``dir/``, which also matches the
    bare ``dir``) or a glob over the full path."""
    for rule in rules:
        bare = rule.rstrip("/")
        if path == bare or path.startswith(bare + "/"):
            return True
        if any(ch in rule for ch in "*?[") and fnmatch.fnmatchcase(path, rule):
            return True
    return False


def build_public_tree(repo: Path, commit: str, rules: list[str]) -> tuple[str, list[str]]:
    """Write a tree object: ``commit``'s tree minus the excluded paths.

    Built in a throwaway index, so the working tree and the real index are
    never touched, and nothing is copied through the filesystem."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", commit, env=env)
        files = [f for f in out(repo, "ls-files", "-z", env=env).split("\0") if f]
        removed = [f for f in files if excluded(f, rules)]
        if removed:
            git(repo, "update-index", "--force-remove", "-z", "--stdin", env=env, input="\0".join(removed) + "\0")
        tree = out(repo, "write-tree", env=env)
    return tree, removed


# ── checks ────────────────────────────────────────────────────────────


def require_clean_main(repo: Path) -> str:
    branch = out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != SOURCE_BRANCH:
        raise PublishError(f"releases are cut from {SOURCE_BRANCH}; HEAD is on {branch}")
    dirty = out(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PublishError("working tree has uncommitted changes:\n" + dirty)
    return out(repo, "rev-parse", "HEAD")


def check_secrets(repo: Path, commit: str) -> list[str]:
    hits: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        proc = git(repo, "grep", "-I", "-n", "-E", "-e", pattern, commit, "--", ".", check=False)
        if proc.returncode not in (0, 1):
            raise PublishError(f"git grep failed for {label}: {proc.stderr.strip()}")
        for line in proc.stdout.splitlines():
            hits.append(f"{label}: {line.split(':', 1)[1] if ':' in line else line}")
    return hits


def run_guard_tests(repo: Path) -> None:
    missing = [t for t in GUARD_TESTS if not (repo / t).exists()]
    if missing:
        raise PublishError("guard test missing: " + ", ".join(missing) + " (or pass --skip-tests)")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *GUARD_TESTS],
        cwd=repo,
        # Under this flag a guard that cannot run (no private denylist) fails
        # instead of skipping — a skip exits 0 and would clear the release.
        env={**os.environ, "DK_PUBLISH_GUARD": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise PublishError("guard tests failed:\n" + proc.stdout[-4000:] + proc.stderr[-2000:])


@dataclass
class Plan:
    tag: str
    head: str
    tree: str
    parent: str | None
    removed: list[str]
    message: str
    author: str
    file_count: int
    problems: list[str] = field(default_factory=list)
    commit: str | None = None  # the public commit, once written


def plan_release(repo: Path, tag: str, *, author: str | None, skip_tests: bool) -> Plan:
    version = parse_version(tag)
    head = require_clean_main(repo)
    problems: list[str] = []

    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    found = pyproject_version(pyproject)
    if found != version:
        problems.append(f"pyproject.toml says version {found}, not {version} — run `prepare {tag}` first")

    changelog_path = repo / CHANGELOG
    notes = changelog_section(changelog_path.read_text(encoding="utf-8"), f"## {tag}") if changelog_path.exists() else None
    if not notes:
        problems.append(f"{CHANGELOG} has no non-empty '## {tag}' section — run `prepare {tag}` first")
        notes = ""

    if not any((repo / name).exists() for name in LICENSE_NAMES):
        problems.append("no LICENSE at the repo root — a public repo without one is all-rights-reserved")

    source_tag = rev(repo, f"refs/tags/release/{tag}")
    if source_tag and source_tag != head:
        problems.append(f"tag release/{tag} already points at {source_tag[:10]}, not HEAD")
    if rev(repo, f"refs/tags/{tag}"):
        problems.append(f"{tag} is already published (tag {tag} exists)")

    author = resolve_author(repo, author)
    if author.rstrip(">").endswith("@example.com") or author.rstrip(">").endswith(".invalid"):
        problems.append(f"author {author} is a placeholder — set `git config publish.author \"Name <email>\"`")

    tracked_ignored = out(repo, "ls-files", "-ci", "--exclude-standard")
    if tracked_ignored:
        problems.append("tracked files match .gitignore:\n  " + tracked_ignored.replace("\n", "\n  "))

    hits = check_secrets(repo, head)
    if hits:
        problems.append("secret-shaped strings in the tree:\n  " + "\n  ".join(hits[:20]))

    if not skip_tests:
        try:
            run_guard_tests(repo)
        except PublishError as exc:
            problems.append(str(exc))

    rules = load_excludes(repo)
    tree, removed = build_public_tree(repo, head, rules)
    file_count = len(out(repo, "ls-tree", "-r", "--name-only", tree).splitlines())
    parent = rev(repo, f"refs/heads/{PUBLIC_BRANCH}")
    message = f"Release {tag}\n\n{notes}\n\nSource-Commit: {head}\n"
    return Plan(
        tag=tag,
        head=head,
        tree=tree,
        parent=parent,
        removed=removed,
        message=message,
        author=author,
        file_count=file_count,
        problems=problems,
    )


# ── commands ──────────────────────────────────────────────────────────


def prepare(repo: Path, tag: str, *, today: dt.date | None = None, no_verify: bool = False) -> str:
    """Bump the version, retitle the changelog, commit. Returns the commit sha."""
    version = parse_version(tag)
    require_clean_main(repo)
    if rev(repo, f"refs/tags/{tag}") or rev(repo, f"refs/tags/release/{tag}"):
        raise PublishError(f"{tag} is already tagged")
    today = today or dt.date.today()

    # Validate both edits before writing either, so a refusal leaves no half-done tree.
    pyproject_path = repo / "pyproject.toml"
    new_pyproject = set_pyproject_version(pyproject_path.read_text(encoding="utf-8"), version)
    changelog_path = repo / CHANGELOG
    if not changelog_path.exists():
        raise PublishError(f"{CHANGELOG} is missing")
    text = changelog_path.read_text(encoding="utf-8")
    new_changelog = retitle_unreleased(text, tag, today)
    notes = changelog_section(text, UNRELEASED_HEADING) or ""
    pyproject_path.write_text(new_pyproject, encoding="utf-8")
    changelog_path.write_text(new_changelog, encoding="utf-8")

    git(repo, "add", "--", "pyproject.toml", CHANGELOG)
    args = ["commit", "-q", "-m", f"Release: {tag}\n\n{notes}\n"]
    if no_verify:
        args.append("--no-verify")
    proc = git(repo, *args, check=False)
    if proc.returncode != 0:
        # The tree was clean, so HEAD is exactly what both files held.
        git(repo, "checkout", "HEAD", "--", "pyproject.toml", CHANGELOG)
        raise PublishError(f"the release commit failed; both files are restored:\n{proc.stdout}{proc.stderr}")
    return out(repo, "rev-parse", "HEAD")


def publish(
    repo: Path,
    tag: str,
    *,
    author: str | None = None,
    remote: str = DEFAULT_REMOTE,
    push: bool = False,
    dry_run: bool = False,
    skip_tests: bool = False,
) -> Plan:
    """Checks → public commit on ``refs/heads/public`` → tags → (push)."""
    plan = plan_release(repo, tag, author=author, skip_tests=skip_tests)
    if plan.problems:
        raise PublishError("not publishable:\n- " + "\n- ".join(plan.problems))
    if push and not out(repo, "remote", "get-url", remote, check=False):
        raise PublishError(f"no git remote named {remote!r} — see docs/publishing.md § Setup")
    if dry_run:
        return plan

    env = identity_env(plan.author)
    args = ["commit-tree", plan.tree]
    if plan.parent:
        args += ["-p", plan.parent]
    commit = out(repo, *args, env=env, input=plan.message)

    # Every ref written here is undone if a later step fails, so a retry
    # starts from "nothing built" instead of "already published" or a
    # second release commit stacked on an untagged first one.
    written: list[str] = []
    try:
        git(repo, "tag", "-a", tag, commit, "-m", plan.message, env=env)
        written.append(f"refs/tags/{tag}")
        if not rev(repo, f"refs/tags/release/{tag}"):
            git(repo, "tag", f"release/{tag}", plan.head)
            written.append(f"refs/tags/release/{tag}")
        update = ["update-ref", f"refs/heads/{PUBLIC_BRANCH}", commit]
        if plan.parent:
            update.append(plan.parent)
        git(repo, *update)
        written.append(f"refs/heads/{PUBLIC_BRANCH}")
        if push:
            git(repo, "push", remote, *push_refspecs(tag))
    except subprocess.CalledProcessError as exc:
        for ref in reversed(written):
            if ref == f"refs/heads/{PUBLIC_BRANCH}" and plan.parent:
                git(repo, "update-ref", ref, plan.parent, commit, check=False)
            else:
                git(repo, "update-ref", "-d", ref, check=False)
        step = "push" if written and written[-1] == f"refs/heads/{PUBLIC_BRANCH}" else "tagging"
        raise PublishError(f"{step} failed; the public branch and tags are back as they were:\n{exc.stderr}") from exc
    plan.commit = commit
    return plan


def push_refspecs(tag: str) -> list[str]:
    return [f"refs/heads/{PUBLIC_BRANCH}:refs/heads/main", f"refs/tags/{tag}:refs/tags/{tag}"]


# ── CLI ───────────────────────────────────────────────────────────────


def repo_root() -> Path:
    try:
        return Path(out(Path.cwd(), "rev-parse", "--show-toplevel"))
    except subprocess.CalledProcessError as exc:
        raise PublishError("not inside a git repository") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_prep = sub.add_parser("prepare", help="bump pyproject + changelog and commit the release")
    p_prep.add_argument("tag", help="vX.Y.Z")
    p_prep.add_argument("--no-verify", action="store_true", help="skip the commit hooks (after /dk-regress)")
    p_pub = sub.add_parser("publish", help="build the public commit and tags; --push sends them")
    p_pub.add_argument("tag", help="vX.Y.Z")
    p_pub.add_argument("--remote", default=DEFAULT_REMOTE, help=f"git remote of the mirror (default {DEFAULT_REMOTE})")
    p_pub.add_argument("--author", help='"Name <email>" for the public commit (default: publish.author config)')
    p_pub.add_argument("--push", action="store_true", help="push the public branch and tag to the mirror")
    p_pub.add_argument("--dry-run", action="store_true", help="run the checks and report; write no refs")
    p_pub.add_argument("--skip-tests", action="store_true", help="skip the guard tests")
    args = ap.parse_args(argv)

    try:
        repo = repo_root()
        if args.cmd == "prepare":
            sha = prepare(repo, args.tag, no_verify=args.no_verify)
            print(f"committed {sha[:10]} Release: {args.tag}")
            print(f"next: python scripts/publish_release.py publish {args.tag}")
            return 0
        plan = publish(
            repo,
            args.tag,
            author=args.author,
            remote=args.remote,
            push=args.push,
            dry_run=args.dry_run,
            skip_tests=args.skip_tests,
        )
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: git {' '.join(exc.cmd[2:4])} failed:\n{exc.stderr}", file=sys.stderr)
        return 1

    print(f"{plan.tag}: tree {plan.tree[:10]} from {plan.head[:10]}, {plan.file_count} files, author {plan.author}")
    if plan.removed:
        print(f"withheld ({len(plan.removed)} paths via {IGNORE_FILE}):")
        for path in plan.removed:
            print(f"  {path}")
    if args.dry_run:
        print("dry run: all checks passed, nothing written")
        return 0
    print(f"public commit {(plan.commit or '?')[:10]} on {PUBLIC_BRANCH}; tags {plan.tag} (public), release/{plan.tag} (source)")
    if args.push:
        print(f"pushed to {args.remote}")
    else:
        refspecs = " ".join(push_refspecs(plan.tag))
        print(
            "not pushed. Pressing this publishes the snapshot to the mirror's main and tags it:\n"
            f"  git push {args.remote} {refspecs}\n"
            f"(or re-run with --push). Then, optionally, a GitHub Release from the tag's notes:\n"
            f"  gh release create {plan.tag} --repo <owner/repo> --verify-tag --notes-from-tag"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
