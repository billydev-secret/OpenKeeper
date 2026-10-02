"""Move the feature specs out of ``docs/`` into ``docs/specs/`` and rewrite
every reference, so ``docs/`` itself is the how-we-work folder.

The rule is the file name: a top-level ``docs/*_spec.md`` is a feature spec
and moves; a handful of specs without the suffix are renamed to carry it on
the way (``RENAMES``); the rest of ``docs/`` — the design guide, the dev and
ops docs, the style guides, the data register — stays where it is. The two
SHOUTING names are lowercased in the same pass.

Reference forms rewritten, across every tracked text file (``TEXT_SUFFIXES``;
not ``.sql`` — see there):

* a repo-relative path, ``docs/casino_spec.md`` (prose, code, YAML, the
  gate's ``DOC_DATA_TESTS``), with any ``#fragment`` kept;
* a ``pathlib`` join, ``"docs" / "no_contact_spec.md"``;
* a Markdown link in a ``.md`` file, re-resolved from the *linking* file's own
  new location — so INDEX's ``(casino_spec.md)`` becomes ``(specs/casino_spec.md)``,
  a spec's ``(design_guide.md)`` becomes ``(../design_guide.md)``, and a link
  between two specs does not change;
* the bare old basename of a *renamed* file (``DEPLOYMENT.md``).

A bare basename of a file that only moves (``casino_spec.md`` in prose) is
left alone: it still names the file.

Commands: ``plan`` prints the moves; ``apply`` runs them with ``git mv`` and
rewrites; ``rewrite`` only rewrites, for a branch cut before the move that
still spells old paths; ``check`` lists references the rewrite would miss.
Like ``relayout.py``, the move lands as one pure commit that is exactly this
script's output.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

DOCS = "docs"
SPECS = "docs/specs"

#: Files whose name changes as they move (or, for the ops doc, stay).
RENAMES: dict[str, str] = {
    "docs/DEPLOYMENT.md": "docs/deployment.md",
    "docs/DUNGEON_KEEPER_TEST_ENV_SPEC.md": "docs/specs/test_env_spec.md",
    "docs/word_cloud.md": "docs/specs/word_cloud_spec.md",
    "docs/member_info.md": "docs/specs/member_info_spec.md",
    "docs/announcements.md": "docs/specs/announcements_spec.md",
}

#: Specs that keep their name but carry no ``_spec`` suffix.
ALSO_SPECS = (
    "docs/duel_minigame_flows_v2.md",
    "docs/tgm_dashboard_concept.md",
)

#: ``.sql`` is left out on purpose: migrations are hash-pinned
#: (``scripts/migration_manifest.py``), so a comment in one keeps the path
#: it was written with.
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".json", ".yml", ".yaml",
    ".html", ".js", ".sh", ".service", ".timer", ".example",
}
REWRITE_SKIP = ("scripts/relayout_docs.py", "tests/tooling/test_relayout_docs.py")

_LINK = re.compile(r"(\]\()([^)\s]+)(\))")
_JOIN = re.compile(r'"docs"(\s*/\s*)"([\w.-]+\.md)"')


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]


def build_moves(tracked: list[str]) -> dict[str, str]:
    """old repo path -> new repo path, for every top-level doc that moves."""
    moves: dict[str, str] = {}
    for rel in tracked:
        parts = PurePosixPath(rel).parts
        if len(parts) != 2 or parts[0] != DOCS:
            continue
        if rel in RENAMES:
            moves[rel] = RENAMES[rel]
        elif rel.endswith("_spec.md") or rel in ALSO_SPECS:
            moves[rel] = f"{SPECS}/{parts[1]}"
    return dict(sorted(moves.items()))


def _path_regex(moves: dict[str, str]) -> re.Pattern[str]:
    olds = sorted((PurePosixPath(o).name for o in moves), key=len, reverse=True)
    return re.compile(
        r"(?<![\w/.-])(\./)?docs/(" + "|".join(map(re.escape, olds)) + r")(?![\w])"
    )


def _bare_regex(moves: dict[str, str]) -> re.Pattern[str] | None:
    renamed = [
        PurePosixPath(o).name
        for o, n in moves.items()
        if PurePosixPath(o).name != PurePosixPath(n).name
    ]
    if not renamed:
        return None
    alt = "|".join(map(re.escape, sorted(renamed, key=len, reverse=True)))
    return re.compile(r"(?<![\w/.-])(" + alt + r")(?![\w])")


def _resolve(base: PurePosixPath, target: str) -> str:
    return os.path.normpath(str(base / target)).replace(os.sep, "/")


def rewrite_links(text: str, path: str, moves: dict[str, str], known: set[str]) -> str:
    """Re-point relative Markdown links from ``path``'s new location."""
    old_dir = PurePosixPath(path).parent
    new_dir = PurePosixPath(moves.get(path, path)).parent

    def fix(m: re.Match[str]) -> str:
        target = m.group(2)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)
        plain, sep, frag = target.partition("#")
        if not plain:
            return m.group(0)
        resolved = _resolve(old_dir, plain)
        if resolved not in known and resolved not in moves:
            return m.group(0)
        dest = moves.get(resolved, resolved)
        rel = os.path.relpath(dest, str(new_dir)).replace(os.sep, "/")
        if rel == plain:
            return m.group(0)
        return f"{m.group(1)}{rel}{sep}{frag}{m.group(3)}"

    return _LINK.sub(fix, text)


def rewrite_text(text: str, path: str, moves: dict[str, str], known: set[str]) -> str:
    by_name = {PurePosixPath(o).name: n for o, n in moves.items()}
    if path.endswith(".md"):
        text = rewrite_links(text, path, moves, known)
    text = _path_regex(moves).sub(lambda m: (m.group(1) or "") + by_name[m.group(2)], text)

    def join(m: re.Match[str]) -> str:
        new = by_name.get(m.group(2))
        if new is None:
            return m.group(0)
        parts = PurePosixPath(new).parts
        return m.group(1).join(f'"{p}"' for p in parts)

    text = _JOIN.sub(join, text)
    bare = _bare_regex(moves)
    if bare:
        new_names = {
            PurePosixPath(o).name: PurePosixPath(n).name for o, n in moves.items()
        }
        text = bare.sub(lambda m: new_names[m.group(1)], text)
    return text


def _rewritable(path: str) -> bool:
    return Path(path).suffix in TEXT_SUFFIXES and path not in REWRITE_SKIP


def rewrite_tree(root: Path, moves: dict[str, str], tracked: list[str]) -> list[str]:
    """Rewrite every tracked text file in place; returns the paths changed.

    Paths are the *pre-move* ones — call this before ``git mv`` so a moved
    file's links are resolved from where they were written.
    """
    known = set(tracked)
    changed: list[str] = []
    for path in tracked:
        if not _rewritable(path) or not (root / path).is_file():
            continue
        src = (root / path).read_text(encoding="utf-8")
        out = rewrite_text(src, path, moves, known)
        if out != src:
            (root / path).write_text(out, encoding="utf-8")
            changed.append(path)
    return changed


def full_map(tracked: list[str]) -> dict[str, str]:
    """Every old path -> new path the tree knows about: the specs already
    under ``docs/specs/`` (a branch rebased onto the move still spells the
    old paths) plus any top-level spec still waiting to move."""
    return {**_moves_from_specs(tracked), **build_moves(tracked)}


def apply(root: Path) -> tuple[dict[str, str], list[str]]:
    tracked = _tracked(root)
    moves = build_moves(tracked)
    changed = rewrite_tree(root, full_map(tracked), tracked)
    (root / SPECS).mkdir(exist_ok=True)
    for old, new in moves.items():
        subprocess.run(["git", "mv", old, new], cwd=root, check=True)
    return moves, changed


#: Where references live, for a walk of a tree that is not a git checkout
#: (the remote test runner's workspace). ``docs/plans`` is gitignored and
#: still spells the old paths on purpose.
WALK_DIRS = ("src", "tests", "scripts", "docs", ".claude", ".github")
WALK_ROOT_FILES = (
    "CLAUDE.md", "README.md", "CONTRIBUTING.md", "SECURITY.md", "pyproject.toml",
    ".env.example",
)
WALK_SKIP = ("docs/plans/", "__pycache__/", "node_modules/")


def walk(root: Path) -> list[str]:
    """Repo-relative text files, from the filesystem rather than git."""
    out = [f for f in WALK_ROOT_FILES if (root / f).is_file()]
    for d in WALK_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in TEXT_SUFFIXES:
                continue
            rel = p.relative_to(root).as_posix()
            if any(s in rel for s in WALK_SKIP):
                continue
            out.append(rel)
    return out


def leftovers(root: Path, moves: dict[str, str], files: list[str] | None = None) -> list[str]:
    """``path:line`` for every reference to an old path still in the tree."""
    pat = _path_regex(moves)
    join = _JOIN
    names = {PurePosixPath(o).name for o in moves}
    hits: list[str] = []
    for path in (files if files is not None else _tracked(root)):
        if not _rewritable(path) or not (root / path).is_file():
            continue
        for n, line in enumerate((root / path).read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) or any(
                m.group(2) in names for m in join.finditer(line)
            ):
                hits.append(f"{path}:{n}: {line.strip()[:100]}")
    return hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("command", choices=("plan", "apply", "rewrite", "check"))
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args(argv)
    root: Path = args.root
    if args.command == "plan":
        for old, new in build_moves(_tracked(root)).items():
            print(f"{old} -> {new}")
        return 0
    if args.command == "apply":
        moves, changed = apply(root)
        print(f"moved {len(moves)} docs, rewrote {len(changed)} files")
        return 0
    if args.command == "rewrite":
        tracked = _tracked(root)
        for path in rewrite_tree(root, full_map(tracked), tracked):
            print(path)
        return 0
    tracked = _tracked(root)
    hits = leftovers(root, full_map(tracked))
    for hit in hits:
        print(hit)
    return 1 if hits else 0


def _moves_from_specs(tracked: list[str]) -> dict[str, str]:
    """The map as it was, reconstructed from a tree where the move happened."""
    present = set(tracked)
    moves = {o: n for o, n in RENAMES.items() if n in present}
    for rel in tracked:
        parts = PurePosixPath(rel).parts
        if len(parts) == 3 and rel.startswith(SPECS + "/") and rel not in RENAMES.values():
            moves[f"{DOCS}/{parts[2]}"] = rel
    return moves


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
