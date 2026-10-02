"""Every third-party GitHub Action is pinned to a commit SHA, not a tag.

A tag like ``@v4`` is a mutable pointer the upstream repo can repoint
without our git history changing — a compromised or careless maintainer
can silently swap out what a workflow runs. Pinning to the full commit SHA
(with a trailing ``# vX.Y.Z`` comment for humans) makes that impossible;
Dependabot's ``github-actions`` ecosystem entry keeps the pin current the
same way it does ``requirements*.lock``.

Composite actions under ``.github/actions/`` are scanned too: they carry
``uses:`` lines of their own (setup-python-deps pins actions/setup-python),
and a sweep of ``.github/workflows/`` alone would let a bare tag there
through.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_GITHUB_DIR = REPO_ROOT / ".github"

_USES_RE = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _action_files() -> list[Path]:
    """Every file GitHub reads ``uses:`` lines from: workflows and composite actions."""
    workflows = sorted((_GITHUB_DIR / "workflows").glob("*.yml"))
    actions = sorted((_GITHUB_DIR / "actions").glob("*/action.yml"))
    return workflows + actions


@pytest.mark.skipif(
    not _GITHUB_DIR.exists(),
    reason="workflows are not synced to the remote test runner",
)
def test_workflow_actions_pinned_to_commit_sha():
    files = _action_files()
    assert files, "no workflow or composite-action files found under .github/"

    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _USES_RE.search(line)
            if not match:
                continue
            repo, ref = match.group(1), match.group(2)
            if not _SHA_RE.match(ref):
                rel = path.relative_to(_GITHUB_DIR.parent).as_posix()  # one spelling on Windows too
                offenders.append(f"{rel}:{lineno} uses {repo}@{ref}")

    assert not offenders, (
        "these actions are pinned to a mutable tag instead of a commit SHA:\n"
        + "\n".join(offenders)
    )


def test_the_sweep_reads_composite_actions_too(tmp_path, monkeypatch):
    """A composite action with a bare tag is an offender, not a blind spot."""
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "ci.yml").write_text(
        "steps:\n  - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n",
        encoding="utf-8",
    )
    action = tmp_path / "actions" / "setup"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(
        "runs:\n  steps:\n    - uses: actions/setup-python@v5\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "_GITHUB_DIR", tmp_path)

    with pytest.raises(AssertionError, match=r"actions/setup/action\.yml:3 uses actions/setup-python@v5"):
        test_workflow_actions_pinned_to_commit_sha()
