"""The docs codemod (``scripts/relayout_docs.py``).

The move commit is exactly the codemod's output, so every reference form it
rewrites gets a row here against a small fixture repo, and the real tree is
checked for a complete, collision-free plan.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import relayout_docs as rd  # noqa: E402

TRACKED = [
    "CLAUDE.md",
    "README.md",
    "docs/INDEX.md",
    "docs/design_guide.md",
    "docs/DEPLOYMENT.md",
    "docs/casino_spec.md",
    "docs/economy_spec.md",
    "docs/word_cloud.md",
    "docs/reviews/2026-01-01-x.md",
    "docs/testing/user.md",
    "scripts/gate.py",
    "tests/x/test_x.py",
]
MOVES = rd.build_moves(TRACKED)
KNOWN = set(TRACKED)


def test_plan_moves_specs_and_renames_only():
    assert MOVES == {
        "docs/DEPLOYMENT.md": "docs/deployment.md",
        "docs/casino_spec.md": "docs/specs/casino_spec.md",
        "docs/economy_spec.md": "docs/specs/economy_spec.md",
        "docs/word_cloud.md": "docs/specs/word_cloud_spec.md",
    }


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        pytest.param(
            "docs/INDEX.md", "[casino_spec.md](casino_spec.md)",
            "[casino_spec.md](specs/casino_spec.md)", id="index-link-into-specs",
        ),
        pytest.param(
            "docs/INDEX.md", "[DEPLOYMENT.md](DEPLOYMENT.md#env)",
            "[deployment.md](deployment.md#env)", id="renamed-link-and-text",
        ),
        pytest.param(
            "docs/casino_spec.md", "see [the guide](design_guide.md)",
            "see [the guide](../design_guide.md)", id="spec-link-up-to-docs",
        ),
        pytest.param(
            "docs/casino_spec.md", "[economy](economy_spec.md#payouts)",
            "[economy](economy_spec.md#payouts)", id="spec-to-spec-unchanged",
        ),
        pytest.param(
            "docs/casino_spec.md", "[readme](../README.md)",
            "[readme](../../README.md)", id="spec-link-to-root",
        ),
        pytest.param(
            "docs/reviews/2026-01-01-x.md", "[c](../casino_spec.md)",
            "[c](../specs/casino_spec.md)", id="review-link",
        ),
        pytest.param(
            "docs/design_guide.md", "[u](testing/user.md) [p](plans/x.md)",
            "[u](testing/user.md) [p](plans/x.md)", id="unmoved-and-unknown-links-stay",
        ),
        pytest.param(
            "README.md", "[c](docs/casino_spec.md)",
            "[c](docs/specs/casino_spec.md)", id="root-link",
        ),
        pytest.param(
            "scripts/gate.py", '"docs/casino_spec.md": ("tests/x.py",),',
            '"docs/specs/casino_spec.md": ("tests/x.py",),', id="path-string",
        ),
        pytest.param(
            "tests/x/test_x.py", 'REPO / "docs" / "casino_spec.md"',
            'REPO / "docs" / "specs" / "casino_spec.md"', id="pathlib-join",
        ),
        pytest.param(
            "tests/x/test_x.py", 'REPO / "docs" / "design_guide.md"',
            'REPO / "docs" / "design_guide.md"', id="pathlib-join-unmoved",
        ),
        pytest.param(
            "CLAUDE.md", "docs/word_cloud.md and `docs/casino_spec.md#x`",
            "docs/specs/word_cloud_spec.md and `docs/specs/casino_spec.md#x`",
            id="prose-paths-with-fragment",
        ),
        pytest.param(
            "CLAUDE.md", "`casino_spec.md` stays; `DEPLOYMENT.md` renames",
            "`casino_spec.md` stays; `deployment.md` renames", id="bare-basename-only-if-renamed",
        ),
        pytest.param(
            "tests/x/test_x.py", '"./docs/casino_spec.md"',
            '"./docs/specs/casino_spec.md"', id="dot-slash-prefix",
        ),
        pytest.param(
            "CLAUDE.md", "docs/specs/casino_spec.md",
            "docs/specs/casino_spec.md", id="already-rewritten-is-idempotent",
        ),
    ],
)
def test_rewrite_forms(path: str, before: str, after: str):
    assert rd.rewrite_text(before, path, MOVES, KNOWN) == after


def test_apply_moves_and_rewrites(tmp_path: Path):
    root = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel in TRACKED:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[c](casino_spec.md) docs/casino_spec.md {rel}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    moves, changed = rd.apply(root)
    assert (root / "docs/specs/casino_spec.md").is_file()
    assert not (root / "docs/casino_spec.md").exists()
    assert "docs/INDEX.md" in changed
    index = (root / "docs/INDEX.md").read_text(encoding="utf-8")
    assert "[c](specs/casino_spec.md) docs/specs/casino_spec.md" in index
    spec = (root / "docs/specs/casino_spec.md").read_text(encoding="utf-8")
    assert spec.startswith("[c](casino_spec.md) docs/specs/casino_spec.md")
    assert rd.leftovers(root, moves) == []
    assert rd._moves_from_specs(rd._tracked(root)) == moves


# ── the real tree ────────────────────────────────────────────────────────


# The remote runner's workspace is a synced tree, not a git checkout, so the
# real-tree checks walk the filesystem (``rd.walk``) rather than ``git ls-files``.


def test_real_tree_leaves_no_top_level_spec():
    """After the move, ``docs/`` top level holds no feature spec."""
    top = [f"docs/{p.name}" for p in (REPO_ROOT / "docs").glob("*.md")]
    assert rd.build_moves(top) == {}, "run scripts/relayout_docs.py apply"
    specs = [p.name for p in (REPO_ROOT / "docs" / "specs").glob("*.md")]
    assert len(specs) > 50, "docs/specs/ is empty or not synced"
    assert all(p.endswith("_spec.md") or f"docs/{p}" in rd.ALSO_SPECS for p in specs)


def test_real_tree_has_no_stale_references():
    files = rd.walk(REPO_ROOT)
    moves = rd.full_map(files)
    assert len(moves) > 50
    assert rd.leftovers(REPO_ROOT, moves, files) == []


def test_real_tree_rewrite_is_idempotent():
    """``rewrite`` is the catch-up every rebased branch runs, so on a tree
    that is already moved it must change nothing — a doc that spells an old
    name in prose (the dev_sessions paragraph about the move) would
    otherwise be corrupted by every branch that follows the instructions."""
    files = rd.walk(REPO_ROOT)
    moves = rd.full_map(files)
    known = set(files)
    changed = []
    for path in files:
        if not rd._rewritable(path):
            continue
        src = (REPO_ROOT / path).read_text(encoding="utf-8")
        if rd.rewrite_text(src, path, moves, known) != src:
            changed.append(path)
    assert changed == []


def test_full_map_merges_waiting_and_moved_specs():
    tracked = ["docs/specs/casino_spec.md", "docs/new_spec.md", "docs/deployment.md"]
    assert rd.full_map(tracked) == {
        "docs/DEPLOYMENT.md": "docs/deployment.md",
        "docs/casino_spec.md": "docs/specs/casino_spec.md",
        "docs/new_spec.md": "docs/specs/new_spec.md",
    }


def test_walk_skips_plans_and_sees_root_files(tmp_path: Path):
    for rel in ("docs/plans/x.md", "docs/a_spec.md", "src/m.py", "CLAUDE.md", "src/__pycache__/m.pyc"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    assert sorted(rd.walk(tmp_path)) == ["CLAUDE.md", "docs/a_spec.md", "src/m.py"]
