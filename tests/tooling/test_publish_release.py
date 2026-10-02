"""The release publisher: snapshots of ``main`` onto a flat public branch.

Everything runs against throwaway repos built here, with git's global and
system config masked so the host's hooks path and identity never leak in.
The property that matters most is the last test: a bare "mirror" that
received a push holds the public commit and nothing reachable from the
private history.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import publish_release as pr  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")

AUTHOR = "Mirror Bot <mirror@mirror.test>"
TODAY = dt.date(2026, 10, 1)


def g(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", check=check
    )


def sha(repo: Path, ref: str) -> str:
    return g(repo, "rev-parse", ref + "^{commit}").stdout.strip()


def tree_files(repo: Path, ref: str) -> set[str]:
    return set(g(repo, "ls-tree", "-r", "--name-only", ref).stdout.split())


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Dev\n\temail = dev@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "private"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    write(repo, "pyproject.toml", '[project]\nname = "demo"\nversion = "0.0.0"\n')
    write(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n- First public cut.\n")
    write(repo, "LICENSE", "MIT\n")
    write(repo, ".gitignore", ".env\n")
    write(repo, "src/app.py", "print('hi')\n")
    write(repo, "docs/reviews/internal.md", "private notes\n")
    write(repo, ".github/workflows/claude.yml", "on: issue_comment\n")
    write(repo, ".publishignore", "# withheld\ndocs/reviews/\n.github/workflows/claude.yml\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "Initial")
    return repo


def cut(repo: Path, tag: str, notes: str | None = None) -> str:
    """prepare + publish, returning the public commit."""
    if notes is not None:
        text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        write(repo, "CHANGELOG.md", text.replace("## Unreleased\n", f"## Unreleased\n\n{notes}\n", 1))
        g(repo, "commit", "-qam", "notes")
    pr.prepare(repo, tag, today=TODAY)
    plan = pr.publish(repo, tag, author=AUTHOR, skip_tests=True)
    assert plan.commit
    return plan.commit


# ── prepare ───────────────────────────────────────────────────────────


def test_prepare_bumps_the_version_and_retitles_the_changelog(repo: Path) -> None:
    head = pr.prepare(repo, "v1.0.0", today=TODAY)
    assert sha(repo, "HEAD") == head
    assert pr.pyproject_version((repo / "pyproject.toml").read_text(encoding="utf-8")) == "1.0.0"
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased\n\n## v1.0.0 (2026-10-01)\n\n- First public cut." in text
    assert pr.changelog_section(text, "## Unreleased") == ""
    assert g(repo, "log", "-1", "--format=%B").stdout.startswith("Release: v1.0.0\n\n- First public cut.")
    assert g(repo, "status", "--porcelain").stdout == ""


def test_prepare_retitles_a_suffixed_unreleased_heading(repo: Path) -> None:
    """The parser accepts ``## Unreleased (…)``, so the retitle must too —
    the two looking at different lines is how a bump commit ends up with an
    unchanged changelog."""
    write(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased (wip)\n\n- notes\n")
    g(repo, "commit", "-qam", "suffixed")
    pr.prepare(repo, "v1.0.0", today=TODAY)
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased (wip)" not in text
    assert pr.changelog_section(text, "## v1.0.0") == "- notes"


def test_prepare_refuses_without_release_notes(repo: Path) -> None:
    write(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## v0.0.1 (2026-01-01)\n\n- old\n")
    g(repo, "commit", "-qam", "empty notes")
    with pytest.raises(pr.PublishError, match="Unreleased is empty"):
        pr.prepare(repo, "v1.0.0", today=TODAY)
    assert "0.0.0" in (repo / "pyproject.toml").read_text(encoding="utf-8")  # nothing half-done


@pytest.mark.parametrize("bad", ["1.0.0", "v1.0", "v1.0.0-rc1", "release"])
def test_prepare_rejects_a_malformed_version(repo: Path, bad: str) -> None:
    with pytest.raises(pr.PublishError, match="vX.Y.Z"):
        pr.prepare(repo, bad, today=TODAY)


# ── publish ───────────────────────────────────────────────────────────


def test_first_release_is_an_orphan_commit_of_the_withheld_tree(repo: Path) -> None:
    public = cut(repo, "v1.0.0")
    head = sha(repo, "main")

    assert sha(repo, "public") == public
    assert g(repo, "rev-list", "--count", "public").stdout.strip() == "1"
    assert g(repo, "log", "-1", "--format=%P", "public").stdout.strip() == ""
    files = tree_files(repo, "public")
    assert "src/app.py" in files and ".publishignore" in files and "pyproject.toml" in files
    assert not any(f.startswith("docs/reviews/") for f in files)
    assert ".github/workflows/claude.yml" not in files
    assert files < tree_files(repo, "main")

    message = g(repo, "log", "-1", "--format=%B", "public").stdout
    assert message.startswith("Release v1.0.0\n\n- First public cut.\n")
    assert f"Source-Commit: {head}" in message
    assert g(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "public").stdout.strip() == f"{AUTHOR}|{AUTHOR}"

    assert sha(repo, "v1.0.0") == public
    assert g(repo, "cat-file", "-t", "v1.0.0").stdout.strip() == "tag"
    assert sha(repo, "release/v1.0.0") == head
    assert g(repo, "status", "--porcelain").stdout == ""


def test_second_release_chains_onto_the_first(repo: Path) -> None:
    first = cut(repo, "v1.0.0")
    write(repo, "src/app.py", "print('v2')\n")
    g(repo, "commit", "-qam", "Feature: v2")
    second = cut(repo, "v1.1.0", notes="- Second cut.")

    assert sha(repo, "public") == second
    assert g(repo, "log", "-1", "--format=%P", "public").stdout.strip() == first
    assert g(repo, "rev-list", "--count", "public").stdout.strip() == "2"
    assert "print('v2')" in g(repo, "show", "public:src/app.py").stdout
    assert sha(repo, "v1.0.0") == first and sha(repo, "v1.1.0") == second


def test_dry_run_writes_nothing(repo: Path) -> None:
    pr.prepare(repo, "v1.0.0", today=TODAY)
    plan = pr.publish(repo, "v1.0.0", author=AUTHOR, skip_tests=True, dry_run=True)
    assert plan.problems == [] and plan.commit is None
    assert plan.removed == [".github/workflows/claude.yml", "docs/reviews/internal.md"]
    assert pr.rev(repo, "refs/heads/public") is None
    assert pr.rev(repo, "refs/tags/v1.0.0") is None


def test_push_sends_the_public_line_and_nothing_private(repo: Path, tmp_path: Path) -> None:
    mirror = tmp_path / "mirror.git"
    g(mirror.parent, "init", "-q", "--bare", str(mirror))
    g(repo, "remote", "add", "public", str(mirror))
    pr.prepare(repo, "v1.0.0", today=TODAY)
    plan = pr.publish(repo, "v1.0.0", author=AUTHOR, skip_tests=True, push=True)

    assert sha(mirror, "main") == plan.commit
    assert sha(mirror, "v1.0.0") == plan.commit
    assert g(mirror, "rev-list", "--count", "main").stdout.strip() == "1"
    # The private commit, and every blob only it holds, never left the machine.
    assert g(mirror, "cat-file", "-e", plan.head, check=False).returncode != 0
    assert "docs/reviews" not in g(mirror, "ls-tree", "-r", "--name-only", "main").stdout
    private_blob = g(repo, "rev-parse", "main:docs/reviews/internal.md").stdout.strip()
    assert g(mirror, "cat-file", "-e", private_blob, check=False).returncode != 0
    assert "release/v1.0.0" not in g(mirror, "tag").stdout


def test_push_needs_the_remote(repo: Path) -> None:
    pr.prepare(repo, "v1.0.0", today=TODAY)
    with pytest.raises(pr.PublishError, match="no git remote named 'public'"):
        pr.publish(repo, "v1.0.0", author=AUTHOR, skip_tests=True, push=True)
    assert pr.rev(repo, "refs/heads/public") is None


def test_the_repo_identity_is_never_the_public_author(repo: Path) -> None:
    """No ``publish.author`` and no ``--author`` is a refusal, not a fallback
    to ``user.name``: the prod checkout commits as ``gate <gate@example.com>``
    and a personal email would otherwise land on the mirror. A placeholder
    given explicitly is refused too."""
    pr.prepare(repo, "v1.0.0", today=TODAY)
    with pytest.raises(pr.PublishError, match="no public author"):
        pr.publish(repo, "v1.0.0", skip_tests=True)
    with pytest.raises(pr.PublishError, match="placeholder"):
        pr.publish(repo, "v1.0.0", author="Gate <gate@example.com>", skip_tests=True)
    assert pr.rev(repo, "refs/heads/public") is None


def test_prepare_restores_both_files_when_the_commit_fails(repo: Path, tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    g(repo, "config", "core.hooksPath", str(hooks))
    before = sha(repo, "HEAD")
    with pytest.raises(pr.PublishError, match="release commit failed"):
        pr.prepare(repo, "v1.0.0", today=TODAY)
    assert sha(repo, "HEAD") == before
    assert g(repo, "status", "--porcelain").stdout == ""
    assert "0.0.0" in (repo / "pyproject.toml").read_text(encoding="utf-8")
    # …and a retry after the hook is fixed works, instead of dying on a dirty tree.
    g(repo, "config", "--unset", "core.hooksPath")
    pr.prepare(repo, "v1.0.0", today=TODAY)


def test_a_rejected_push_rolls_the_local_refs_back(repo: Path, tmp_path: Path) -> None:
    """Otherwise every retry is refused as 'already published'."""
    first = cut(repo, "v1.0.0")
    g(repo, "remote", "add", "public", str(tmp_path / "does-not-exist.git"))
    write(repo, "src/app.py", "print('v2')\n")
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    write(repo, "CHANGELOG.md", text.replace("## Unreleased\n", "## Unreleased\n\n- Second cut.\n", 1))
    g(repo, "commit", "-qam", "Feature: v2")
    pr.prepare(repo, "v1.1.0", today=TODAY)
    with pytest.raises(pr.PublishError, match="push failed; the public branch and tags are back"):
        pr.publish(repo, "v1.1.0", author=AUTHOR, skip_tests=True, push=True)
    assert sha(repo, "public") == first
    assert pr.rev(repo, "refs/tags/v1.1.0") is None
    assert pr.rev(repo, "refs/tags/release/v1.1.0") is None
    # The retry is a normal publish, not a stacked second release.
    plan = pr.publish(repo, "v1.1.0", author=AUTHOR, skip_tests=True)
    assert g(repo, "log", "-1", "--format=%P", "public").stdout.strip() == first
    assert plan.commit == sha(repo, "public")


def test_author_falls_back_to_publish_author_config(repo: Path) -> None:
    g(repo, "config", "publish.author", "Configured <cfg@mirror.test>")
    pr.prepare(repo, "v1.0.0", today=TODAY)
    pr.publish(repo, "v1.0.0", skip_tests=True)
    assert g(repo, "log", "-1", "--format=%an <%ae>", "public").stdout.strip() == "Configured <cfg@mirror.test>"


# ── the guards ────────────────────────────────────────────────────────


def _not_main(repo: Path) -> None:
    g(repo, "checkout", "-qb", "feature")


def _dirty(repo: Path) -> None:
    write(repo, "src/app.py", "print('dirty')\n")


def _version_mismatch(repo: Path) -> None:
    write(repo, "pyproject.toml", '[project]\nname = "demo"\nversion = "9.9.9"\n')
    g(repo, "commit", "-qam", "wrong version")


def _no_notes(repo: Path) -> None:
    write(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## v1.0.0 (2026-10-01)\n")
    g(repo, "commit", "-qam", "empty section")


def _no_license(repo: Path) -> None:
    g(repo, "rm", "-q", "LICENSE")
    g(repo, "commit", "-qm", "drop license")


def _tracked_ignored(repo: Path) -> None:
    write(repo, ".env", "DISCORD_TOKEN=x\n")
    g(repo, "add", "-f", ".env")
    g(repo, "commit", "-qm", "oops")


def _secret(repo: Path) -> None:
    write(repo, "src/app.py", "TOKEN = 'ghp_" + "A" * 36 + "'\n")
    g(repo, "commit", "-qam", "leak")


def _already_published(repo: Path) -> None:
    g(repo, "tag", "-a", "v1.0.0", "-m", "x", "HEAD")


def _source_tag_elsewhere(repo: Path) -> None:
    g(repo, "tag", "release/v1.0.0", "HEAD~1")


@pytest.mark.parametrize(
    ("spoil", "expect"),
    [
        pytest.param(_not_main, "HEAD is on feature", id="not-on-main"),
        pytest.param(_dirty, "uncommitted changes", id="dirty-tree"),
        pytest.param(_version_mismatch, "says version 9.9.9", id="pyproject-version"),
        pytest.param(_no_notes, "no non-empty '## v1.0.0' section", id="empty-notes"),
        pytest.param(_no_license, "no LICENSE", id="no-license"),
        pytest.param(_tracked_ignored, "tracked files match .gitignore", id="tracked-ignored"),
        pytest.param(_secret, "GitHub token: src/app.py", id="secret-in-tree"),
        pytest.param(_already_published, "already published", id="already-published"),
        pytest.param(_source_tag_elsewhere, "release/v1.0.0 already points", id="source-tag-elsewhere"),
    ],
)
def test_publish_refuses(repo: Path, spoil, expect: str) -> None:
    pr.prepare(repo, "v1.0.0", today=TODAY)
    spoil(repo)
    with pytest.raises(pr.PublishError, match=expect):
        pr.publish(repo, "v1.0.0", author=AUTHOR, skip_tests=True)
    assert pr.rev(repo, "refs/heads/public") is None


def test_the_guard_test_step_runs_pytest(repo: Path) -> None:
    for i, guard in enumerate(pr.GUARD_TESTS):
        write(repo, guard, f"def test_{i}():\n    assert {i != 0}\n")  # the first guard fails
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "a failing guard")
    pr.prepare(repo, "v1.0.0", today=TODAY)
    with pytest.raises(pr.PublishError, match="guard tests failed"):
        pr.publish(repo, "v1.0.0", author=AUTHOR)


def test_the_guard_run_sets_the_publishing_flag(repo: Path) -> None:
    for guard in pr.GUARD_TESTS:
        write(repo, guard, "import os\n\ndef test_flag():\n    assert os.environ.get('DK_PUBLISH_GUARD') == '1'\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "guards that need the flag")
    pr.prepare(repo, "v1.0.0", today=TODAY)
    assert pr.publish(repo, "v1.0.0", author=AUTHOR, dry_run=True).problems == []


def test_every_guard_test_must_exist_unless_skipped(repo: Path) -> None:
    write(repo, pr.GUARD_TESTS[0], "def test_ok():\n    assert True\n")  # one of two present
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "one guard")
    pr.prepare(repo, "v1.0.0", today=TODAY)
    with pytest.raises(pr.PublishError, match="guard test missing: tests/tooling/test_public_tree_leaks.py"):
        pr.publish(repo, "v1.0.0", author=AUTHOR)


# ── pieces ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "rules", "hit"),
    [
        ("docs/reviews/a.md", ["docs/reviews/"], True),
        ("docs/reviews", ["docs/reviews/"], True),
        ("docs/reviews_index.md", ["docs/reviews/"], False),
        ("docs/reviews/a.md", ["docs/reviews"], True),
        (".github/workflows/claude.yml", [".github/workflows/claude.yml"], True),
        (".github/workflows/claude-code-review.yml", [".github/workflows/claude.yml"], False),
        ("a/b/x.secret", ["*.secret"], True),
        ("a/b/x.secrets", ["*.secret"], False),
        ("src/app.py", [], False),
    ],
)
def test_excluded(path: str, rules: list[str], hit: bool) -> None:
    assert pr.excluded(path, rules) is hit


@pytest.mark.parametrize("rule", ["/docs/reviews/", "docs/**/private.md", "!keep.md"])
def test_load_excludes_refuses_gitignore_syntax(tmp_path: Path, rule: str) -> None:
    write(tmp_path, ".publishignore", rule + "\n")
    with pytest.raises(pr.PublishError, match="gitignore syntax"):
        pr.load_excludes(tmp_path)


def test_load_excludes_skips_comments_and_blanks(tmp_path: Path) -> None:
    write(tmp_path, ".publishignore", "# a comment\n\n  docs/reviews/  \nfoo.txt\n")
    assert pr.load_excludes(tmp_path) == ["docs/reviews/", "foo.txt"]
    assert pr.load_excludes(tmp_path / "nowhere") == []


def test_changelog_section_stops_at_the_next_heading() -> None:
    text = "# Log\n\n## Unreleased\n\n## v1.0.0 (2026-10-01)\n\n- a\n- b\n\n### sub\n\nc\n\n## v0.9.0\n\n- z\n"
    assert pr.changelog_section(text, "## Unreleased") == ""
    assert pr.changelog_section(text, "## v1.0.0") == "- a\n- b\n\n### sub\n\nc"
    assert pr.changelog_section(text, "## v0.9.0") == "- z"
    assert pr.changelog_section(text, "## v2.0.0") is None


@pytest.mark.parametrize("bad", ["nobody", "<only@example.com>", "Name <>", ""])
def test_identity_env_rejects_a_malformed_author(bad: str) -> None:
    with pytest.raises(pr.PublishError, match="Name <email>"):
        pr.identity_env(bad)


def test_secret_patterns_are_valid_extended_regexes(repo: Path) -> None:
    """``git grep -E`` must accept every pattern, including the one that
    starts with dashes (passed through ``-e`` so it is never read as an
    option)."""
    assert pr.check_secrets(repo, "HEAD") == []


def test_secret_scan_is_word_anchored(repo: Path) -> None:
    write(repo, "docs/a.md", "the task-queue-management-panel-handler and risk-assessment-of-everything\n")
    write(repo, "src/k.py", "KEY = 'sk-" + "a1" * 12 + "'\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "kebab and a key")
    hits = pr.check_secrets(repo, "HEAD")
    assert hits == [f"sk- API key: src/k.py:1:KEY = 'sk-{'a1' * 12}'"]


# ── this repo's own exclusion list ────────────────────────────────────


def test_the_real_publishignore_names_paths_that_exist() -> None:
    """A stale rule withholds nothing. In the public mirror *every* rule
    withholds nothing, by construction — that tree is what the rules
    removed — so a tree where no rule matches anything is the mirror and
    the check does not apply there."""
    ignore = REPO_ROOT / pr.IGNORE_FILE
    if not ignore.exists():
        pytest.skip("root files are not on this runner")
    rules = pr.load_excludes(REPO_ROOT)
    assert rules, "an empty .publishignore means the Claude workflows ship with their secrets wiring"
    literal = [r for r in rules if not any(ch in r for ch in "*?[")]
    present = [r for r in literal if (REPO_ROOT / r).exists()]
    if not present:
        pytest.skip("no rule matches anything here: this is the published tree, not the repo that authors the rules")
    stale = sorted(set(literal) - set(present))
    assert stale == [], f"rules that withhold nothing: {stale}"
