"""Move ``src/bot_modules/`` into one package per feature, and rewrite every
reference to the moved files.

The map lives in ``scripts/relayout_map.py``. This is the codemod that
applies it, one stage at a time, so a move commit is exactly this script's
output and can be re-derived (``verify``) or replayed onto a branch
(``catch-up``). Stdlib only.

    python scripts/relayout.py map [STAGE]      # print old -> new (TSV)
    python scripts/relayout.py apply STAGE      # move stages <= STAGE, rewrite refs
    python scripts/relayout.py check STAGE      # leftover references; exit 1 if broken
    python scripts/relayout.py verify SHA STAGE # SHA == codemod(SHA^)?
    python scripts/relayout.py catch-up SHA STAGE  # replay a move onto this branch

Stage 6 moves tests instead: each ``tests/**/test_*.py`` to the folder
``relayout_map.place``s it in, rewriting its path and dotted name everywhere,
and deletes the packages stages 1-5 emptied.

What ``apply`` rewrites, in every tracked text file outside ``REWRITE_SKIP``:

* **Import statements** (ast-located). ``from bot_modules.services import
  economy_shop, xp_service as xp`` is split per name into
  ``from bot_modules.economy import shop as economy_shop`` and so on, keeping
  every local name as it was, so no call site changes. A relative import that
  no longer resolves from the file's new place becomes absolute.
* **Dotted module names** anywhere else: imports of the ``from X import y``
  form, ``mock.patch`` targets, extension-name strings, docs.
* **Paths**: ``src/bot_modules/<old>``, ``bot_modules/<old>`` and the
  bot_modules-relative ``<old>`` form the no-contact registry keys use.

What it cannot rewrite, ``check`` reports: a module name built in an f-string
(``f"bot_modules.cogs.{name}"``), a path assembled from parts
(``ROOT / "cogs" / "x.py"``) and a moved file's old name quoted on its own
(``COGS / "x_cog.py"``). Fix those by hand in the same commit.

There are no re-export shims: an old name that still imports would let a
branch keep writing it.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "src/bot_modules/"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relayout_map import (  # noqa: E402
    LEFT_BEHIND,
    MUST_EMPTY,
    RULES,
    TEST_KEEP,
    TEST_NAME_ALIASES,
    TEST_ROWS,
    Rule,
)

TESTS = "tests/"
TEST_STAGE = 6

#: Tracked files the rewrite never touches: the map and codemod name old paths
#: on purpose, and a review records the tree as it was when it was written.
REWRITE_SKIP = (
    "scripts/relayout.py",
    "scripts/relayout_map.py",
    "tests/test_relayout.py",
    "tests/tooling/test_relayout.py",  # where stage 6 puts it
    "docs/reviews/",
)
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".json", ".yml", ".yaml",
    ".html", ".js", ".sh", ".service", ".timer",
}
_STAR = "([a-z0-9_]+)"


# ── the plan ─────────────────────────────────────────────────────────────


def _rule_regex(old: str) -> re.Pattern[str]:
    return re.compile(re.escape(old).replace(r"\*", _STAR) + ("" if old.endswith("/") else "$"))


def match(rel: str, rules: list[Rule] = RULES) -> tuple[int, str] | None:
    """``rel`` (bot_modules-relative) -> ``(stage, new rel)`` by the first rule
    that matches, or None."""
    for stage, old, new in rules:
        m = _rule_regex(old).match(rel)
        if not m:
            continue
        target = new.replace("*", m.group(1)) if "*" in old else new
        if old.endswith("/"):
            target += rel[m.end():]
        return stage, target
    return None


def module_of(rel: str) -> str:
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["bot_modules", *parts])


@dataclass
class Plan:
    files: dict[str, str]  # old rel -> new rel (bot_modules-relative)
    modules: dict[str, str] = field(default_factory=dict)  # old dotted -> new
    paths: dict[str, str] = field(default_factory=dict)  # old rel path -> new
    all_modules: set[str] = field(default_factory=set)  # every module, old names
    tests: "TestPlan | None" = None  # stage 6's test moves

    def rename(self, module: str) -> str:
        """A dotted name under its new name, by its longest moved prefix."""
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            head = ".".join(parts[:i])
            if head in self.modules:
                return ".".join([self.modules[head], *parts[i:]])
        return module


def git_env() -> dict[str, str]:
    """The environment minus a commit hook's GIT_DIR / GIT_INDEX_FILE, which
    would point every git call here at the hook's repo instead of ``cwd``."""
    return {k: v for k, v in os.environ.items() if k not in _HOOK_GIT_VARS}


_HOOK_GIT_VARS = {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY"}


def _tracked(root: Path, prefix: str = "") -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", prefix or "."],
        cwd=root, capture_output=True, text=True, check=True, env=git_env(),
    ).stdout
    return [p for p in out.split("\0") if p]


def build_plan(root: Path, stage: int, rules: list[Rule] = RULES) -> Plan:
    files: dict[str, str] = {}
    base = root / PKG
    existing = sorted(
        p.relative_to(base).as_posix()
        for p in base.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    unplaced: list[str] = []
    for rel in existing:
        hit = match(rel, rules)
        if hit is None:
            if rel.startswith(MUST_EMPTY) and rel not in LEFT_BEHIND and rel.endswith(".py"):
                unplaced.append(rel)
            continue
        if hit[0] <= stage:
            files[rel] = hit[1]
    _validate(existing, files, unplaced)
    return _plan_from(existing, files, stage, rules)


def _validate(existing: list[str], files: dict[str, str], unplaced: list[str]) -> None:
    staying = set(existing) - files.keys()
    seen: dict[str, str] = {}
    errors = [f"{rel}: No rule places it; add a row to relayout_map.py" for rel in unplaced]
    for old, new in files.items():
        if new in staying:
            errors.append(f"{old} -> {new}: the destination already exists")
        if new in seen:
            errors.append(f"{old} and {seen[new]} both -> {new}")
        seen[new] = old
    olds = {module_of(o) for o in files if o.endswith(".py")}
    for new in files.values():
        if new.endswith(".py") and module_of(new) in olds:
            errors.append(f"{new}: a new name equal to an old one makes the move unreplayable")
    if errors:
        raise SystemExit("Map errors:\n  " + "\n  ".join(errors))


# ── the rewrite ──────────────────────────────────────────────────────────


def _package_of(module: str, rel: str) -> str:
    return module if rel.endswith("__init__.py") else module.rpartition(".")[0]


def _resolve(level: int, module: str | None, package: str) -> str:
    base = package.split(".")
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([module] if module else []))


def _file_modules(path: str, plan: Plan) -> tuple[str, str, str, str] | None:
    """(old module, old package, new module, new package) of a bot_modules file."""
    if not (path.startswith(PKG) and path.endswith(".py")):
        return None
    old_rel = path[len(PKG):]
    new_rel = plan.files.get(old_rel, old_rel)
    old_mod, new_mod = module_of(old_rel), module_of(new_rel)
    return old_mod, _package_of(old_mod, old_rel), new_mod, _package_of(new_mod, new_rel)


def _format_import(parent: str, names: list[tuple[str, str]], indent: str) -> str:
    parts = [n if n == local else f"{n} as {local}" for n, local in names]
    one = f"{indent}from {parent} import {', '.join(parts)}"
    if len(one) <= 88:
        return one
    body = "".join(f"{indent}    {p},\n" for p in parts)
    return f"{indent}from {parent} import (\n{body}{indent})"


def _with_comment(statement: str, comment: str) -> str:
    """``comment`` on the statement's first line, where ruff reads a noqa."""
    first, sep, rest = statement.partition("\n")
    return f"{first}  {comment}{sep}{rest}"


def rewrite_imports(text: str, path: str, plan: Plan, warnings: list[str]) -> str:
    """Split ``from <package> import <module>`` per moved module and absolutise
    relative imports that break; leave everything else to the dotted pass."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    where = _file_modules(path, plan)
    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or any(a.name == "*" for a in node.names):
            continue
        if node.level:
            if where is None:
                continue
            old_base = _resolve(node.level, node.module, where[1])
        else:
            old_base = node.module or ""
        groups: dict[str, list[tuple[str, str]]] = {}
        moved_member = False
        for alias in node.names:
            local = alias.asname or alias.name
            full = f"{old_base}.{alias.name}"
            if full in plan.all_modules and full in plan.modules:
                moved_member = True
                parent, _, name = plan.modules[full].rpartition(".")
            else:
                parent, name = plan.rename(old_base), alias.name
            groups.setdefault(parent, []).append((name, local))
        if node.level:
            assert where is not None
            still = _resolve(node.level, node.module, where[3])
            if list(groups) == [still] and all(
                n == a.name for (n, _), a in zip(groups[still], node.names)
            ):
                continue  # still resolves from the file's new place
        elif not moved_member:
            continue  # the dotted pass renames the module in place
        start, end = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        first, last = lines[start], lines[end]
        indent = first[: len(first) - len(first.lstrip())]
        # A trailing comment (usually a ``# noqa``) rides along onto every
        # statement the split writes; anything else after the import is code.
        tail = last.rstrip("\r\n")[node.end_col_offset or 0:].strip()
        if first[len(indent):].split()[0] != "from" or (tail and not tail.startswith("#")):
            warnings.append(f"{path}:{node.lineno}: import shares its line; fix by hand")
            continue
        body_lines = [lines[i] for i in range(start, end + 1)]
        body_lines[-1] = body_lines[-1].rstrip("\r\n")[: node.end_col_offset or 0]
        if any("#" in line for line in body_lines):
            warnings.append(f"{path}:{node.lineno}: comment inside a split import was dropped")
        newline = "\r\n" if last.endswith("\r\n") else "\n"
        statements = [_format_import(p, n, indent) for p, n in groups.items()]
        if tail:
            statements = [_with_comment(s, tail) for s in statements]
        new = newline.join(statements) + newline
        edits.append((start, end, new.replace("\n", newline) if newline == "\r\n" else new))
    for start, end, new in sorted(edits, reverse=True):
        lines[start : end + 1] = [new]
    return "".join(lines)


def _dotted_regex(plan: Plan) -> re.Pattern[str] | None:
    names = sorted(plan.modules, key=len, reverse=True)
    if not names:
        return None
    return re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, names)) + r")(?!\w)")


def _path_regex(plan: Plan) -> re.Pattern[str] | None:
    olds = sorted(plan.paths, key=len, reverse=True)
    if not olds:
        return None
    alt = "|".join(re.escape(o.rstrip("/")) + ("/" if o.endswith("/") else "") for o in olds)
    return re.compile(r"(?<![\w/.-])((?:src/)?(?:bot_modules/)?)(" + alt + r")(?![\w])")


#: Lists of paths kept sorted by a test, which a rename can put out of order:
#: file -> the TOML keys whose one-entry-per-line arrays are re-sorted.
SORTED_LISTS = {"pyproject.toml": ("strict",)}


def _resort(text: str, keys: tuple[str, ...]) -> str:
    for key in keys:
        m = re.search(rf"^{re.escape(key)} = \[\n((?:[ \t]+\"[^\"\n]*\",\n)+)\]", text, re.M)
        if m:
            body = "".join(sorted(m.group(1).splitlines(keepends=True)))
            text = text[: m.start(1)] + body + text[m.end(1):]
    return text


def rewrite_text(text: str, path: str, plan: Plan, warnings: list[str]) -> str:
    if path.endswith(".py"):
        text = rewrite_imports(text, path, plan, warnings)
    dotted, paths = _dotted_regex(plan), _path_regex(plan)
    if dotted:
        text = dotted.sub(lambda m: plan.modules[m.group(1)], text)
    if paths:
        text = paths.sub(lambda m: m.group(1) + plan.paths[m.group(2)], text)
    if path in SORTED_LISTS:
        text = _resort(text, SORTED_LISTS[path])
    return text


def _rewritable(path: str) -> bool:
    return Path(path).suffix in TEXT_SUFFIXES and not path.startswith(REWRITE_SKIP)


def apply(root: Path, stage: int, rules: list[Rule] = RULES) -> tuple[Plan, list[str]]:
    plan = build_plan(root, stage, rules)
    warnings: list[str] = []
    for path in _tracked(root):
        if not _rewritable(path) or not (root / path).is_file():
            continue
        src = root / path
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = rewrite_text(text, path, plan, warnings)
        if new != text:
            src.write_text(new, encoding="utf-8", newline="")
    base = root / PKG
    for old, new in plan.files.items():
        dest = base / new
        dest.parent.mkdir(parents=True, exist_ok=True)
        (base / old).rename(dest)
    for new in plan.files.values():
        d = (base / new).parent
        while d != base and d.is_relative_to(base):
            init = d / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
            d = d.parent
    _prune_empty(base)
    if stage >= TEST_STAGE:
        for rel in LEFT_BEHIND:
            (base / rel).unlink(missing_ok=True)
        _prune_empty(base)
        _apply_tests(root, plan, warnings)
    return plan, warnings


def _prune_empty(base: Path) -> None:
    for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        stale = [c for c in d.iterdir() if c.name == "__pycache__"]
        if all(c.name == "__pycache__" for c in d.iterdir()):
            for c in stale:
                for f in c.iterdir():
                    f.unlink()
                c.rmdir()
            d.rmdir()


# ── stage 6: tests ───────────────────────────────────────────────────────

#: ``core.db_utils`` is plumbing nearly every test opens a DB through, not
#: what the test is about, so it never counts toward placing one.
_FEATURE_REF = re.compile(r"bot_modules[./]([a-z_]+)(?![./]db_utils)")


def features(root: Path) -> set[str]:
    """The feature packages under ``src/bot_modules/`` once the move is done."""
    base = root / PKG
    return {
        p.name for p in base.iterdir()
        if (p / "__init__.py").is_file() and f"{p.name}/" not in MUST_EMPTY
    }


def place(rel: str, text: str, feats: set[str]) -> str | None:
    """The folder a test belongs in (``tests/<x>``), by the rules in
    ``relayout_map`` in their order; None leaves it where it is."""
    if rel.startswith(TEST_KEEP):
        return None
    name = Path(rel).name
    stem = name.removeprefix("test_").removesuffix(".py")
    if name in TEST_ROWS:
        return TESTS + TEST_ROWS[name] if TEST_ROWS[name] else None
    if "_contract" in stem or stem.startswith("embed"):
        return TESTS + "contracts"
    for prefix, folder in (("migration", "migrations"), ("dk_mcp", "dk_mcp")):
        if stem.startswith(prefix):
            return TESTS + folder
    keys = {f: f for f in feats} | TEST_NAME_ALIASES
    for key in sorted(keys, key=len, reverse=True):
        head = key if key.endswith("_") else key + "_"
        if stem == key.rstrip("_") or stem.startswith(head):
            return TESTS + keys[key]
    counts: dict[str, int] = {}
    for m in _FEATURE_REF.findall(text):
        if m in feats:
            counts[m] = counts.get(m, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return TESTS + ranked[0][0]
    return None


def test_module(rel: str) -> str:
    return rel.removesuffix(".py").replace("/", ".")


@dataclass
class TestPlan:
    files: dict[str, str]  # old repo-relative path -> new

    @property
    def modules(self) -> dict[str, str]:
        return {test_module(o): test_module(n) for o, n in self.files.items()}


def build_test_plan(root: Path, rels: list[str], read) -> TestPlan:
    """Where each ``tests/**/test_*.py`` in ``rels`` goes; ``read(rel)`` is its text."""
    feats = features(root)
    files: dict[str, str] = {}
    for rel in rels:
        if not (rel.startswith(TESTS) and Path(rel).name.startswith("test_") and rel.endswith(".py")):
            continue
        folder = place(rel, read(rel), feats)
        if folder and Path(rel).parent.as_posix() != folder:
            files[rel] = f"{folder}/{Path(rel).name}"
    names = [Path(n).name for n in files.values()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SystemExit("Test basenames must stay unique: " + ", ".join(dupes))
    return TestPlan(files)


def _test_regexes(tp: TestPlan) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    if not tp.files:
        return None, None
    paths = "|".join(map(re.escape, sorted(tp.files, key=len, reverse=True)))
    mods = "|".join(map(re.escape, sorted(tp.modules, key=len, reverse=True)))
    return (
        re.compile(r"(?<![\w/.-])(" + paths + r")(?![\w])"),
        re.compile(r"(?<![\w.])(" + mods + r")(?!\w)"),
    )


def rewrite_tests(text: str, tp: TestPlan) -> str:
    paths, dotted = _test_regexes(tp)
    if paths:
        text = paths.sub(lambda m: tp.files[m.group(1)], text)
    if dotted:
        mods = tp.modules
        text = dotted.sub(lambda m: mods[m.group(1)], text)
    return text


def _apply_tests(root: Path, plan: Plan, warnings: list[str]) -> TestPlan:
    tracked = _tracked(root)
    tp = build_test_plan(root, tracked, lambda rel: (root / rel).read_text(encoding="utf-8"))
    for path in tracked:
        if not _rewritable(path) or not (root / path).is_file():
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = rewrite_tests(text, tp)
        if new != text:
            (root / path).write_text(new, encoding="utf-8", newline="")
    for old, new in tp.files.items():
        dest = root / new
        dest.parent.mkdir(parents=True, exist_ok=True)
        (root / old).rename(dest)
    _prune_empty(root / TESTS)
    plan.tests = tp
    return tp


# ── check ────────────────────────────────────────────────────────────────


def _moved_member_imports(path: str, text: str, plan: Plan) -> list[str]:
    """``from bot_modules.services import x`` where ``x`` moved: the dotted
    pattern never sees it, because the old full name is never spelled out."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        f"{path}:{node.lineno}: old module name: from {node.module} import {alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and not node.level and node.module
        for alias in node.names
        if f"{node.module}.{alias.name}" in plan.modules
    ]


def leftovers(root: Path, plan: Plan) -> tuple[list[str], list[str]]:
    """References the rewrite could not reach: ``(broken, review)``.

    *Broken* is an old module name or path still spelled out, which is always
    wrong after the move. *Review* is a module name built in an f-string or a
    path assembled from parts: the pattern can't tell whether the part filled
    in names a moved file, so a person reads each line.
    """
    broken: list[str] = []
    review: list[str] = []
    dotted, paths = _dotted_regex(plan), _path_regex(plan)
    parents = sorted({m.rpartition(".")[0] for m in plan.modules if m.count(".") >= 2})
    fstring = re.compile(r"(?:" + "|".join(map(re.escape, parents)) + r")\.\{") if parents else None
    bases = {Path(o.rstrip("/")).name for o in plan.paths}
    old_dirs = {Path(o.rstrip("/")).parent.name for o in plan.paths} - {""}
    split = re.compile(
        r"""["'](%s)["']\s*[/,]\s*["'](%s)["']""" % (
            "|".join(map(re.escape, sorted(old_dirs))) or "(?!)",
            "|".join(map(re.escape, sorted(bases))) or "(?!)",
        )
    )
    # ``_COGS / "x_cog.py"``: the directory is a variable, so ``split`` can't
    # see it, but the old file name standing alone in quotes still can. Only
    # a file the move *renames*: one that keeps its name (``layouts.py``
    # riding along with its directory) matches all over the repo.
    renamed = sorted({
        Path(old).name for old, new in plan.files.items()
        if old.endswith(".py") and Path(old).name != Path(new).name
    })
    alone = re.compile(r"""["'](%s)["']""" % ("|".join(map(re.escape, renamed)) or "(?!)"))
    # ``REPO / "bot_modules" / "jail"``: a moved top-level package named as
    # the segment after ``bot_modules`` (a test's scan root, stage 9). An
    # rglob over the vanished directory finds nothing and passes empty.
    tops = sorted(
        {Path(o).parts[0] for o in plan.files if "/" in o}
        - {Path(n).parts[0] for n in plan.files.values()}
    )
    top_join = re.compile(r"""["']bot_modules["']\s*/\s*["'](%s)["']""" % ("|".join(map(re.escape, tops)) or "(?!)"))
    test_paths, test_dotted = _test_regexes(plan.tests) if plan.tests else (None, None)
    emptied = sorted({Path(o).parent.as_posix() + "/" for o in (plan.tests.files if plan.tests else ())} - {TESTS})
    test_dirs = re.compile("(?<![\\w/.-])(" + "|".join(map(re.escape, emptied)) + ")") if emptied else None
    for path in _tracked(root):
        if not _rewritable(path) or not (root / path).is_file():
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        is_py = path.endswith(".py")
        if is_py:
            broken.extend(_moved_member_imports(path, text, plan))
        for n, line in enumerate(text.splitlines(), 1):
            for pat, why, out in (
                (dotted, "old module name", broken),
                (paths, "old path", broken),
                (fstring, "module name built in an f-string", review),
                (split, "path assembled from parts", review),
                (top_join, "path assembled from parts", review),
                (alone if is_py else None, "moved file named alone", review),
                (test_paths, "old test path", broken),
                (test_dotted, "old test module name", broken),
                (test_dirs, "emptied test folder", review),
            ):
                if pat and pat.search(line):
                    out.append(f"{path}:{n}: {why}: {line.strip()[:120]}")
                    break
    return broken, review


def _report(plan: Plan) -> int:
    broken, review = leftovers(ROOT, plan)
    for title, lines in (("Broken references:", broken), ("Read these by hand:", review)):
        if lines:
            print(title, *lines, sep="\n  ")
    if not broken and not review:
        print("No leftover references.")
    return 1 if broken else 0


# ── CLI ──────────────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, env=git_env()
    ).stdout.strip()


def cmd_verify(sha: str, stage: int) -> int:
    with tempfile.TemporaryDirectory(prefix="relayout-verify-") as tmp:
        wt = Path(tmp) / "wt"
        _git(ROOT, "worktree", "add", "--detach", str(wt), f"{sha}^")
        try:
            subprocess.run(
                [sys.executable, "scripts/relayout.py", "apply", str(stage)], cwd=wt, check=True, env=git_env()
            )
            _git(wt, "add", "-A")
            ours, theirs = _git(wt, "write-tree"), _git(ROOT, "rev-parse", f"{sha}^{{tree}}")
            if ours != theirs:
                print(_git(wt, "diff", "--stat", "--cached", sha))
                print(f"{sha} is NOT the codemod's output for stage {stage}.")
                return 1
        finally:
            _git(ROOT, "worktree", "remove", "--force", str(wt))
    print(f"{sha} is exactly the stage-{stage} codemod applied to its parent.")
    return 0


def cmd_catch_up(sha: str, stage: int) -> int:
    """Bring a branch across a move commit without a conflict per file: merge
    main up to the move's parent, replay the codemod on the branch's own
    files, then record the move as merged."""
    if _git(ROOT, "status", "--porcelain", "--untracked-files=no"):
        print("Commit or set aside your changes first.")
        return 1
    if subprocess.run(["git", "merge", "--no-edit", f"{sha}^"], cwd=ROOT, env=git_env()).returncode:
        print("Resolve the merge of the move's parent, commit, and re-run.")
        return 1
    plan, warnings = apply(ROOT, stage)
    print("\n".join(warnings))
    _git(ROOT, "add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, env=git_env()).returncode:
        _git(ROOT, "commit", "-m", f"Layout: replay the stage-{stage} move on this branch")
    _git(ROOT, "merge", "-s", "ours", "--no-edit", sha)
    return _report(plan)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "map":
        stage = int(args[0]) if args else TEST_STAGE
        plan = build_plan(ROOT, stage)
        for old, new in sorted(plan.files.items()):
            print(f"{match(old)[0]}\t{old}\t{new}")  # type: ignore[index]
        if stage >= TEST_STAGE:
            tp = build_test_plan(ROOT, _tracked(ROOT), lambda rel: (ROOT / rel).read_text(encoding="utf-8"))
            for old, new in sorted(tp.files.items()):
                print(f"{TEST_STAGE}\t{old}\t{new}")
        return 0
    if cmd == "apply":
        plan, warnings = apply(ROOT, int(args[0]))
        print(f"moved {len(plan.files)} files")
        print("\n".join(warnings))
        return 0
    if cmd == "check":
        # After apply, the plan is rebuilt from the map alone: the files are
        # gone, so derive the names from the rules against git's record.
        stage = int(args[0])
        plan = _plan_from_head(stage)
        if stage >= TEST_STAGE:
            plan.tests = _test_plan_from_head()
        return _report(plan)
    if cmd == "verify":
        return cmd_verify(args[0], int(args[1]))
    if cmd == "catch-up":
        return cmd_catch_up(args[0], int(args[1]))
    print(__doc__)
    return 2


def _plan_from_head(stage: int) -> Plan:
    """The plan as it was before the working tree moved: rebuilt from HEAD's
    file list, or from the move commit's parent when HEAD is the move."""
    for ref in ("HEAD", "HEAD^"):
        listing = _git(ROOT, "ls-tree", "-r", "--name-only", ref, "--", PKG).splitlines()
        rels = [p[len(PKG):] for p in listing]
        files = {}
        for rel in rels:
            hit = match(rel)
            if hit and hit[0] <= stage:
                files[rel] = hit[1]
        if files:
            break
    return _plan_from(rels, files, stage)


def _test_plan_from_head() -> TestPlan:
    """Stage 6's test moves, from HEAD's test list (or the move's parent's)
    and each test's text wherever it now sits: basenames are unique, and the
    move changes no ``bot_modules`` name a placement reads."""
    here = {p.name: p for p in (ROOT / TESTS).rglob("test_*.py") if "__pycache__" not in p.parts}
    for ref in ("HEAD", "HEAD^"):
        rels = _git(ROOT, "ls-tree", "-r", "--name-only", ref, "--", TESTS).splitlines()
        rels = [r for r in rels if Path(r).name in here or not Path(r).name.startswith("test_")]
        tp = build_test_plan(ROOT, rels, lambda rel: here[Path(rel).name].read_text(encoding="utf-8"))
        if tp.files and all(not (ROOT / old).exists() for old in tp.files):
            return tp
    return TestPlan({})


def _plan_from(rels: list[str], files: dict[str, str], stage: int, rules: list[Rule] = RULES) -> Plan:
    plan = Plan(files)
    plan.all_modules = {module_of(r) for r in rels if r.endswith(".py")}
    for old, new in files.items():
        plan.paths[old] = new
        if old.endswith(".py"):
            plan.modules[module_of(old)] = module_of(new)
    for rule_stage, old, _new in rules:
        if rule_stage > stage or not old.endswith("/"):
            continue
        for rel, target in files.items():
            m = _rule_regex(old).match(rel)
            if m:
                old_dir, new_dir = m.group(0), target[: len(target) - len(rel[m.end():])]
                plan.modules.setdefault(module_of(old_dir.rstrip("/")), module_of(new_dir.rstrip("/")))
                plan.paths.setdefault(old_dir, new_dir)
    return plan


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
