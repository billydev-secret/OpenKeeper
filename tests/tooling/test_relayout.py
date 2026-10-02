"""The layout codemod (``scripts/relayout.py``) and its map.

A move commit is exactly the codemod's output, so every reference form it
rewrites gets a row here, run against a small fixture repo: if a form stops
being rewritten, the move commit that relies on it would break imports the
tests of that stage might not happen to exercise. The map is checked against
the real tree: every file is placed, no two land on one path, and no file
selects fewer tests in the pre-commit gate at its new path than at its old.
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

import gate  # noqa: E402
import relayout  # noqa: E402
from relayout_map import LEFT_BEHIND, MUST_EMPTY, RULES  # noqa: E402

FIXTURE_RULES = [
    (1, "services/a_service.py", "alpha/service.py"),
    (1, "services/b_*.py", "beta/*.py"),
    (1, "cogs/pkg/", "gamma/cog/"),
    (1, "cogs/a_cog.py", "alpha/cog.py"),
    (2, "services/later.py", "later/service.py"),
    # Placed, so the map is complete, but never reached by stages 1-2.
    (9, "services/a_service_extra.py", "extra/service.py"),
]


# ── match ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        pytest.param("services/a_service.py", (1, "alpha/service.py"), id="exact"),
        pytest.param("services/b_store.py", (1, "beta/store.py"), id="family"),
        pytest.param("cogs/pkg/sub/x.py", (1, "gamma/cog/sub/x.py"), id="directory-keeps-sub-path"),
        pytest.param("cogs/pkg/data.json", (1, "gamma/cog/data.json"), id="directory-moves-non-python"),
        pytest.param("services/a_service_extrb.py", None, id="exact-is-not-a-prefix"),
        pytest.param("services/b_.py", None, id="family-star-is-not-empty"),
        pytest.param("services/later.py", (2, "later/service.py"), id="later-stage"),
    ],
)
def test_match(rel, expected):
    assert relayout.match(rel, FIXTURE_RULES) == expected


def test_first_rule_wins():
    rules = [(1, "services/b_special.py", "special/x.py"), (1, "services/b_*.py", "beta/*.py")]
    assert relayout.match("services/b_special.py", rules) == (1, "special/x.py")


# ── apply, on a fixture repo ─────────────────────────────────────────────

FIXTURE = {
    "src/bot_modules/__init__.py": "",
    "src/bot_modules/services/__init__.py": "",
    "src/bot_modules/services/a_service.py": "def f():\n    return 1\n",
    "src/bot_modules/services/a_service_extra.py": "X = 1\n",
    "src/bot_modules/services/b_store.py": "from bot_modules.services.a_service import f\n",
    "src/bot_modules/services/later.py": "",
    "src/bot_modules/cogs/__init__.py": "",
    "src/bot_modules/cogs/a_cog.py": "async def setup(bot):\n    pass\n",
    "src/bot_modules/cogs/pkg/__init__.py": "from .cog import setup\n",
    "src/bot_modules/cogs/pkg/cog.py": (
        "from . import views\n"
        "from .views import V\n"
        "from ..a_cog import setup as other\n"
        "from ...services import a_service\n"
    ),
    "src/bot_modules/cogs/pkg/views.py": "class V:\n    pass\n",
    "src/bot_modules/user.py": (
        "from bot_modules.services import a_service, a_service_extra as extra, b_store\n"
        "from bot_modules.services.a_service import f\n"
        "import bot_modules.services.b_store as bs\n"
        "from bot_modules.cogs.pkg.views import V\n"
        "from bot_modules.cogs import pkg\n"
        "\n"
        "def g():\n"
        "    from bot_modules.services import (\n"
        "        a_service,\n"
        "        b_store,\n"
        "    )\n"
        "    return a_service, b_store\n"
        "\n"
        "def h():\n"
        "    from bot_modules.services import a_service, b_store  # noqa: PLC0415\n"
        "    return a_service, b_store\n"
    ),
    "tests/test_user.py": (
        'PATCH = "bot_modules.services.a_service.f"\n'
        'EXT = "bot_modules.cogs.pkg"\n'
        'KEEP = "bot_modules.services.a_service_extra"\n'
        'PATH = "src/bot_modules/services/b_store.py"\n'
        'KEY = "cogs/a_cog.py"\n'
        'NOT_A_KEY = "x/cogs/a_cog.py"\n'
        'DYN = f"bot_modules.cogs.{name}"\n'
        'SPLIT = ROOT / "services" / "a_service.py"\n'
    ),
    "docs/spec.md": "See `services/a_service.py` and `bot_modules/cogs/pkg/`.\n",
    "docs/reviews/old.md": "`services/a_service.py` as it was.\n",
    "pyproject.toml": (
        "strict = [\n"
        '    "src/bot_modules/beta_first.py",\n'
        '    "src/bot_modules/services/a_service.py",\n'
        '    "src/bot_modules/zebra.py",\n'
        "]\n"
        "dependencies = [\n"
        '    "zzz",\n'
        '    "aaa",\n'
        "]\n"
    ),
}

EXPECTED = {
    "src/bot_modules/alpha/service.py": "def f():\n    return 1\n",
    "src/bot_modules/beta/store.py": "from bot_modules.alpha.service import f\n",
    "src/bot_modules/gamma/cog/cog.py": (
        "from . import views\n"
        "from .views import V\n"
        "from bot_modules.alpha.cog import setup as other\n"
        "from bot_modules.alpha import service as a_service\n"
    ),
    "src/bot_modules/user.py": (
        "from bot_modules.alpha import service as a_service\n"
        "from bot_modules.services import a_service_extra as extra\n"
        "from bot_modules.beta import store as b_store\n"
        "from bot_modules.alpha.service import f\n"
        "import bot_modules.beta.store as bs\n"
        "from bot_modules.gamma.cog.views import V\n"
        "from bot_modules.gamma import cog as pkg\n"
        "\n"
        "def g():\n"
        "    from bot_modules.alpha import service as a_service\n"
        "    from bot_modules.beta import store as b_store\n"
        "    return a_service, b_store\n"
        "\n"
        "def h():\n"
        "    from bot_modules.alpha import service as a_service  # noqa: PLC0415\n"
        "    from bot_modules.beta import store as b_store  # noqa: PLC0415\n"
        "    return a_service, b_store\n"
    ),
    "tests/test_user.py": (
        'PATCH = "bot_modules.alpha.service.f"\n'
        'EXT = "bot_modules.gamma.cog"\n'
        'KEEP = "bot_modules.services.a_service_extra"\n'
        'PATH = "src/bot_modules/beta/store.py"\n'
        'KEY = "alpha/cog.py"\n'
        'NOT_A_KEY = "x/cogs/a_cog.py"\n'
        'DYN = f"bot_modules.cogs.{name}"\n'
        'SPLIT = ROOT / "services" / "a_service.py"\n'
    ),
    "docs/spec.md": "See `alpha/service.py` and `bot_modules/gamma/cog/`.\n",
    "docs/reviews/old.md": "`services/a_service.py` as it was.\n",
    # The strict list is re-sorted after the rename; other arrays are not.
    "pyproject.toml": (
        "strict = [\n"
        '    "src/bot_modules/alpha/service.py",\n'
        '    "src/bot_modules/beta_first.py",\n'
        '    "src/bot_modules/zebra.py",\n'
        "]\n"
        "dependencies = [\n"
        '    "zzz",\n'
        '    "aaa",\n'
        "]\n"
    ),
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, text=True, env=relayout.git_env(),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in FIXTURE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def _apply(root: Path, stage: int = 1):
    return relayout.apply(root, stage, FIXTURE_RULES)


@pytest.mark.parametrize("rel", sorted(EXPECTED))
def test_apply_rewrites_every_reference_form(repo, rel):
    _apply(repo)
    assert (repo / rel).read_text(encoding="utf-8") == EXPECTED[rel]


def test_apply_moves_files_creates_packages_and_prunes_empty_dirs(repo):
    plan, _ = _apply(repo)
    base = repo / "src/bot_modules"
    assert not (base / "services/a_service.py").exists()
    assert (base / "gamma/cog/views.py").exists()
    for pkg in ("alpha", "beta", "gamma", "gamma/cog"):
        assert (base / pkg / "__init__.py").exists(), pkg
    assert not (base / "cogs/pkg").exists()
    assert (base / "services/later.py").exists(), "a later stage's file stays"
    assert plan.modules["bot_modules.cogs.pkg"] == "bot_modules.gamma.cog"


def test_apply_twice_is_a_no_op(repo):
    _apply(repo)
    _git(repo, "add", "-A")
    before = _git(repo, "write-tree")
    plan, _ = _apply(repo)
    _git(repo, "add", "-A")
    assert plan.files == {}
    assert _git(repo, "write-tree") == before


def test_a_later_stage_applies_the_earlier_ones(repo):
    plan, _ = _apply(repo, 2)
    assert plan.files["services/later.py"] == "later/service.py"
    assert plan.files["services/a_service.py"] == "alpha/service.py"


def test_leftovers_flags_what_the_rewrite_cannot_reach(repo):
    plan, _ = _apply(repo)
    broken, review = relayout.leftovers(repo, plan)
    assert broken == []
    assert [line.split(":")[:2] for line in review] == [
        ["tests/test_user.py", "7"],
        ["tests/test_user.py", "8"],
    ]
    (repo / "late.py").write_text("import bot_modules.services.a_service\n", encoding="utf-8")
    _git(repo, "add", "late.py")
    broken, _ = relayout.leftovers(repo, plan)
    assert broken and broken[0].startswith("late.py:1: old module name")


def test_leftovers_flags_a_moved_package_named_as_a_path_segment(repo):
    """``REPO / "bot_modules" / "services"``: the directory is gone, so a
    scan rooted there finds nothing and a test passes empty (a jail test's
    scan root, stage 9)."""
    plan, _ = _apply(repo)
    (repo / "late.py").write_text(
        'root = REPO / "src" / "bot_modules" / "services"\n', encoding="utf-8"
    )
    _git(repo, "add", "late.py")
    _, review = relayout.leftovers(repo, plan)
    assert any(r.startswith("late.py:1: path assembled from parts") for r in review)


def test_leftovers_flags_a_moved_module_imported_from_its_old_package(repo):
    """The old full name never appears in ``from <old package> import <x>``,
    so only the AST pass can see it (quote_box's lazy imports, 2026-09-29)."""
    plan, _ = _apply(repo)
    (repo / "late.py").write_text(
        "def f():\n    from bot_modules.services import a_service\n", encoding="utf-8"
    )
    _git(repo, "add", "late.py")
    broken, _ = relayout.leftovers(repo, plan)
    assert broken == ["late.py:2: old module name: from bot_modules.services import a_service"]


def test_leftovers_flags_a_moved_file_named_alone(repo):
    """The directory lives in a variable, so only the quoted old file name
    shows (``_COGS / "pen_pals_cog.py"``, stage 2, 2026-09-29)."""
    plan, _ = _apply(repo)
    (repo / "late.py").write_text(
        'COGS = ROOT / "cogs"\nSOURCES = [COGS / "a_cog.py"]\nVIEWS = PKG / "views.py"\n',
        encoding="utf-8",
    )
    _git(repo, "add", "late.py")
    _, review = relayout.leftovers(repo, plan)
    # views.py rides along with cogs/pkg/ under its own name: every package
    # has one, so quoting it proves nothing (stage 4, 2026-09-29).
    assert [line.split(":")[:3] for line in review if line.startswith("late.py")] == [
        ["late.py", "2", " moved file named alone"]
    ]


def test_an_import_sharing_its_line_is_left_with_a_warning(repo):
    user = repo / "src/bot_modules/user.py"
    user.write_text("import os; from bot_modules.services import a_service\n", encoding="utf-8")
    _, warnings = _apply(repo)
    assert any("shares its line" in w for w in warnings)


@pytest.mark.parametrize(
    ("rules", "error"),
    [
        pytest.param(
            [(1, "services/a_service.py", "x.py"), (1, "services/b_store.py", "x.py")],
            "both ->",
            id="two-files-one-destination",
        ),
        pytest.param(
            [(1, "services/a_service.py", "services/a_service_extra.py")],
            "already exists",
            id="destination-exists",
        ),
        pytest.param(
            [(1, "services/a_service.py", "cogs/a_cog.py"), (1, "cogs/a_cog.py", "z.py")],
            "unreplayable",
            id="new-name-equals-an-old-one",
        ),
        pytest.param([(1, "services/a_service.py", "x.py")], "No rule places", id="unplaced-file"),
    ],
)
def test_the_plan_refuses_a_bad_map(repo, rules, error):
    with pytest.raises(SystemExit, match=error):
        relayout.build_plan(repo, 1, rules)


# ── stage 6: tests ───────────────────────────────────────────────────────

FEATS = {"alpha", "core", "games", "music", "music_playlist"}


@pytest.mark.parametrize(
    ("rel", "text", "expected"),
    [
        pytest.param("tests/web/test_alpha.py", "", None, id="web-keeps-its-folder"),
        pytest.param("tests/test_gate_scope.py", "", "tests/tooling", id="row"),
        pytest.param("tests/test_web_routes.py", "bot_modules.core", None, id="empty-row-stays"),
        pytest.param("tests/test_x_contract.py", "bot_modules.alpha", "tests/contracts", id="contract"),
        pytest.param("tests/test_embed_colors.py", "", "tests/contracts", id="embed-sweep"),
        pytest.param("tests/test_migration_12_x.py", "", "tests/migrations", id="migration"),
        pytest.param("tests/test_dk_mcp_paths.py", "", "tests/dk_mcp", id="dk-mcp"),
        pytest.param("tests/cogs/test_alpha_cog.py", "", "tests/alpha", id="name-prefix"),
        pytest.param("tests/test_alpha.py", "", "tests/alpha", id="name-whole"),
        pytest.param("tests/test_music_playlist_x.py", "", "tests/music_playlist", id="longest-name-wins"),
        pytest.param("tests/test_guess_repo.py", "", "tests/games", id="alias"),
        pytest.param("tests/test_alphabet.py", "", None, id="prefix-needs-a-word-break"),
        pytest.param(
            "tests/test_x.py",
            "from bot_modules.alpha import a\nfrom bot_modules.core import b\nimport bot_modules.alpha.c",
            "tests/alpha",
            id="imports-outright-winner",
        ),
        pytest.param(
            "tests/test_x.py", "bot_modules.alpha bot_modules.core", None, id="imports-tie-stays"
        ),
        pytest.param(
            "tests/test_x.py",
            "bot_modules.core.db_utils bot_modules.core.db_utils bot_modules.alpha",
            "tests/alpha",
            id="db-utils-does-not-count",
        ),
        pytest.param("tests/test_x.py", "bot_modules.services.y", None, id="unknown-package-ignored"),
    ],
)
def test_place(rel, text, expected):
    assert relayout.place(rel, text, FEATS) == expected


TEST_FIXTURE = {
    "src/bot_modules/__init__.py": "",
    "src/bot_modules/alpha/__init__.py": "",
    "src/bot_modules/services/__init__.py": "",
    "tests/conftest.py": "",
    "tests/cogs/test_alpha_cog.py": "def helper():\n    return 1\n",
    "tests/test_alpha_logic.py": (
        "from tests.cogs.test_alpha_cog import helper\n"
        "import tests.cogs.test_alpha_cog as mod\n"
    ),
    "tests/test_misc.py": "X = 1\n",
    "tests/web/test_alpha_route.py": "",
    "docs/spec.md": (
        "Run `tests/cogs/test_alpha_cog.py` and tests/test_alpha_logic.py::test_x;\n"
        "tests/test_misc.py stays, and so does not_tests/test_alpha_logic.py.\n"
    ),
}


@pytest.fixture
def test_repo(tmp_path: Path) -> Path:
    for rel, text in TEST_FIXTURE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def test_stage_6_moves_tests_rewrites_references_and_deletes_emptied_packages(test_repo):
    plan, _ = relayout.apply(test_repo, 6, [])
    assert plan.tests is not None
    assert plan.tests.files == {
        "tests/cogs/test_alpha_cog.py": "tests/alpha/test_alpha_cog.py",
        "tests/test_alpha_logic.py": "tests/alpha/test_alpha_logic.py",
    }
    assert (test_repo / "tests/alpha/test_alpha_logic.py").read_text(encoding="utf-8") == (
        "from tests.alpha.test_alpha_cog import helper\n"
        "import tests.alpha.test_alpha_cog as mod\n"
    )
    assert (test_repo / "docs/spec.md").read_text(encoding="utf-8") == (
        "Run `tests/alpha/test_alpha_cog.py` and tests/alpha/test_alpha_logic.py::test_x;\n"
        "tests/test_misc.py stays, and so does not_tests/test_alpha_logic.py.\n"
    )
    assert not (test_repo / "tests/cogs").exists()
    assert (test_repo / "tests/web/test_alpha_route.py").exists()
    assert not (test_repo / "src/bot_modules/services").exists()
    assert relayout.leftovers(test_repo, plan) == ([], [])


def test_stage_6_twice_is_a_no_op(test_repo):
    relayout.apply(test_repo, 6, [])
    _git(test_repo, "add", "-A")
    before = _git(test_repo, "write-tree")
    plan, _ = relayout.apply(test_repo, 6, [])
    _git(test_repo, "add", "-A")
    assert plan.tests is not None and plan.tests.files == {}
    assert _git(test_repo, "write-tree") == before


def test_stage_6_refuses_two_tests_landing_on_one_basename(test_repo):
    (test_repo / "tests/unit").mkdir()
    (test_repo / "tests/unit/test_alpha_logic.py").write_text("", encoding="utf-8")
    _git(test_repo, "add", "-A")
    with pytest.raises(SystemExit, match="unique: test_alpha_logic.py"):
        relayout.apply(test_repo, 6, [])


# ── the real map ─────────────────────────────────────────────────────────


REAL = relayout.build_plan(REPO_ROOT, max(stage for stage, _, _ in RULES))


def test_the_real_map_places_every_file():
    """``build_plan`` raises on an unplaced file, a collision or a name reuse;
    this pins that the plan it built covers every module still waiting in the
    old directories (362 before stage 1, none after stage 5)."""
    base = REPO_ROOT / relayout.PKG
    waiting = {
        p.relative_to(base).as_posix()
        for d in MUST_EMPTY
        for p in (base / d).rglob("*.py")
        if "__pycache__" not in p.parts
    } - LEFT_BEHIND
    assert waiting <= REAL.files.keys()
    assert base.is_dir() and (not waiting or REAL.files)


def _selected(path: str, tests: list[str]) -> set[str]:
    tokens = gate._tokens_for(path)
    return {t for t in tests if any(gate._matches(t, k) for k in tokens)}


#: Rows whose new path deliberately selects fewer tests, with the reason.
NARROWER: dict[str, str] = {}


def test_no_file_selects_fewer_tests_at_its_new_path():
    """A move that shrinks a file's scoped selection hides a regression from
    the commit that causes it until the full run on main."""
    tests = [p.stem for p in gate._test_files()]
    lost = {}
    for old, new in REAL.files.items():
        if not old.endswith(".py") or old in NARROWER:
            continue
        missing = _selected(relayout.PKG + old, tests) - _selected(relayout.PKG + new, tests)
        if missing:
            lost[f"{old} -> {new}"] = sorted(missing)
    assert not lost, lost
