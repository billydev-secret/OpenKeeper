"""gate.py's source → test mapping and mandatory-test rule (pure logic).

These two heuristics decide what the per-commit scoped tier actually runs, and
which new files it refuses to let through untested — so each branch gets a case.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gate  # noqa: E402


# ── mandatory-test rule ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/bios/logic.py",
        "src/bot_modules/inactive/store.py",
        "src/bot_modules/voice/master/voice_logic.py",
        "src/bot_modules/economy/service.py",
    ],
)
def test_logic_layers_require_a_test(path):
    assert gate.requires_test(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/bios/cog.py",
        "src/bot_modules/bios/views.py",
        "src/bot_modules/bios/embeds.py",
        "src/web_server/routes/bios.py",
        "scripts/gate.py",
        "src/bot_modules/bios/logic_helpers.py",
    ],
)
def test_glue_layers_do_not_require_a_test(path):
    assert gate.requires_test(path) is False


# ── feature-token mapping ────────────────────────────────────────────────

def test_nested_feature_dir_resolves_to_the_feature_not_cogs():
    # src/bot_modules/cogs/<feature>/ — 'cogs' is generic, 'casino' is the feature.
    toks = gate._tokens_for("src/bot_modules/cogs/casino/blackjack.py")
    assert "casino" in toks
    assert "cogs" not in toks


def test_nested_generic_filename_still_maps_to_its_feature():
    toks = gate._tokens_for("src/bot_modules/cogs/quickdraw/logic.py")
    assert toks == {"quickdraw"}


def test_top_level_feature_dir_still_maps():
    assert gate._tokens_for("src/bot_modules/breaks/logic.py") == {"breaks"}


def test_a_feature_nested_under_a_parent_maps_to_both():
    """A sibling folded into a parent (stage 7: voice_master -> voice/master)
    selects by either segment, so test_voice_master_* still runs for it and
    a test named for the parent alone does too."""
    assert gate._tokens_for("src/bot_modules/voice/master/logic.py") == {"voice", "master"}


def test_service_file_maps_to_both_bare_and_suffixed_names():
    # A hypothetical flat file, so the layout codemod leaves the path alone.
    toks = gate._tokens_for("src/bot_modules/services/widget_service.py")
    assert {"widget", "widget_service"} <= toks


@pytest.mark.parametrize(
    ("path", "must_have", "must_not_have"),
    [
        # Repo-layout reorganisation (feature packages): a module keeps the
        # tests it selected as a flat services/ or cogs/ file after it moves.
        pytest.param("src/bot_modules/economy/quests.py", {"economy_quests"}, set(),
                     id="moved-service-keeps-flat-name"),
        pytest.param("src/bot_modules/onboarding/intake/intake_service.py", {"intake"}, set(),
                     id="service-suffix-stripped-outside-services"),
        pytest.param("src/bot_modules/economy/cog_shop.py", {"shop", "economy_shop"}, set(),
                     id="cog-prefix-stripped"),
        pytest.param("src/bot_modules/games/ama/logic.py", {"ama"}, {"games"},
                     id="game-package-maps-to-its-game-not-every-game"),
        pytest.param("src/bot_modules/duels/chicken/cog.py", {"chicken"}, {"duels"},
                     id="duel-package-maps-to-its-duel"),
        pytest.param("src/bot_modules/games/utils/timer.py", {"games", "timer"}, set(),
                     id="shared-game-plumbing-still-selects-every-game"),
        pytest.param("src/bot_modules/shared/name_resolver.py", {"name_resolver"}, {"shared"},
                     id="shared-is-generic"),
        pytest.param("src/bot_modules/breaks/logic.py", {"breaks"},
                     {"breaks_logic"}, id="no-compound-from-a-generic-filename"),
        pytest.param("src/bot_modules/voice/master/logic.py", {"voice", "master"},
                     {"master_logic", "voice_master_logic"}, id="nested-feature-no-compound"),
    ],
)
def test_target_layout_mapping(path, must_have, must_not_have):
    toks = gate._tokens_for(path)
    assert must_have <= toks
    assert not (must_not_have & toks)


def test_game_package_selects_its_own_tests_not_every_games_test():
    selected = {
        t for t in ("test_games_ama_logic", "test_games_wyr_logic", "test_mahjong_game_logic")
        if any(gate._matches(t, k) for k in gate._tokens_for("src/bot_modules/games/ama/logic.py"))
    }
    assert selected == {"test_games_ama_logic"}


@pytest.mark.parametrize("name", ["logic", "models", "views", "formatters"])
def test_service_package_file_maps_to_its_package(name):
    # services/<feature>/ — the generic filename alone mapped to no test at all.
    toks = gate._tokens_for(f"src/bot_modules/games/risky_roll/{name}.py")
    assert "risky_roll" in toks
    assert "services" not in toks


def test_service_package_logic_selects_the_cog_no_contact_test():
    # The escape behind da5cf7ee: choose_roll's signature changed in logic.py and
    # the cog's no-contact wiring test, then named without "roll", never ran.
    targets, _, _ = gate.select_tests(
        ["src/bot_modules/games/risky_roll/logic.py"], new=set()
    )
    # Path objects render with backslashes on the Windows test runner.
    assert "tests/games/test_risky_roll_cog_no_contact.py" in {
        Path(t).as_posix() for t in targets
    }


def test_generic_only_path_maps_to_nothing():
    assert gate._tokens_for("src/bot_modules/utils.py") == set()


# ── migrations: new vs modified ──────────────────────────────────────────

def test_editing_an_existing_migration_forces_a_full_run():
    """An edited migration can reshape tables under the whole db-backed suite."""
    assert gate.forces_full_run("src/migrations/134_todo_board.sql", is_new=False) is True


def test_adding_a_new_migration_does_not_force_a_full_run():
    """A new file can't alter an existing table; it was 8 of 13 recent fallbacks."""
    assert gate.forces_full_run("src/migrations/199_new_thing.sql", is_new=True) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/core/sticky.py",
        "src/bot_modules/models/thing.py",
        "pyproject.toml",
        "tests/conftest.py",
        "tests/web/conftest.py",
    ],
)
def test_these_fan_out_even_when_newly_added(path):
    """Newness only ever excuses a migration — everything else still fans out."""
    assert gate.forces_full_run(path, is_new=True) is True


def test_a_migration_edit_selects_the_hash_manifest_guard():
    """The migration branch ``continue``s before any token matching.

    So a migration filename maps to no test by name — measured:
    ``_tokens_for("src/migrations/019_games.sql")`` returns ``{"019_games"}``
    and nothing in ``tests/`` matches it. The full-run trigger that would cover
    it is deferred on a work branch. Selecting the manifest guard by path is
    the only thing that makes an edit to a shipped migration fail where the
    commit is actually made rather than at CI after the merge.
    """
    targets, unmapped, _ = gate.select_tests(
        ["src/migrations/019_games.sql"], new=set(), defer_full=True
    )
    assert gate.MIGRATION_MANIFEST_TEST in targets
    assert not unmapped


def test_a_new_migration_selects_it_too():
    """A new file needs a manifest row, so it has to fail before it lands."""
    targets, _, _ = gate.select_tests(
        ["src/migrations/199_new_thing.sql"], new={"src/migrations/199_new_thing.sql"}
    )
    assert gate.MIGRATION_MANIFEST_TEST in targets


def test_select_tests_honours_a_new_migration():
    targets, _, run_full = gate.select_tests(
        ["src/migrations/199_new_thing.sql"], new={"src/migrations/199_new_thing.sql"}
    )
    assert run_full is False


@pytest.mark.parametrize(
    "path",
    [
        "src/migrations/199_new_thing.sql",
        "src/bot_modules/needle/store.py",
        "src/bot_modules/music/playlist/music_playlist_store.py",
        "src/bot_modules/economy/submission_store.py",
    ],
)
def test_a_new_table_always_selects_the_privacy_coverage_tests(path):
    """A migration or store is where a per-member or guild-less table appears,
    and the two coverage tests are what catch one missing from the data
    register or the guild purge. A work branch merges on scoped-green, so
    leaving them to a full run left them unenforced exactly where it mattered."""
    targets, _, run_full = gate.select_tests([path], new={path}, defer_full=True)
    assert run_full is False
    assert "tests/privacy/test_privacy_register_coverage.py" in targets
    assert "tests/privacy/test_guild_purge_coverage.py" in targets


@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/whisper/cog.py",
        "src/bot_modules/privacy/logic.py",
        "docs/data_register.md",
        # After the repo-layout move, cogs live in their feature packages.
        "src/bot_modules/whisper/cog.py",
        "src/bot_modules/economy/cog/shop.py",
        "src/bot_modules/economy/cog_shop.py",
        "src/bot_modules/moderation/jail/jail_commands.py",
    ],
)
def test_the_member_data_panel_contract_is_selected_by_path(path):
    """Neither half of the contract is reachable by a filename token: one
    watches every cog, the other watches the register — and a docs change maps
    by no filename token. A feature adding its own forget-me command, or a
    register row promising a member-erasable store with no button, would both
    merge on scoped-green without this."""
    targets, _, run_full = gate.select_tests([path], new=set(), defer_full=True)
    assert run_full is False
    assert "tests/privacy/test_privacy_member_data_panel.py" in targets


def test_a_plain_logic_module_does_not_select_the_member_data_panel():
    targets, _, _ = gate.select_tests(
        ["src/bot_modules/economy/wallet_logic.py"], new=set(), defer_full=True
    )
    assert "tests/privacy/test_privacy_member_data_panel.py" not in targets


def _withheld(path: str) -> bool:
    """Whether .publishignore withholds ``path`` from the public mirror."""
    ignore = gate.ROOT / ".publishignore"
    if not ignore.is_file():
        return False
    rules = [
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return any(path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/") for r in rules)


@pytest.mark.parametrize(
    ("doc", "test"),
    [(doc, t) for doc, tests in gate.DOC_DATA_TESTS.items() for t in tests],
)
def test_a_doc_change_selects_the_tests_that_parse_it(doc, test):
    """Row 4.14: a doc a test reads selects that test, and the pairing is
    real — the doc and the test exist, and the test names the doc."""
    if not (gate.ROOT / test).exists() and _withheld(test):
        # The public mirror is this tree minus .publishignore; a mapped test
        # the rules withhold is absent there by design, and the gate's own
        # existence filter already drops it. Absent *without* a rule is a
        # stale mapping and still fails below.
        pytest.skip(f"{test} is withheld from this tree (.publishignore)")
    targets, _, run_full = gate.select_tests([doc], new=set(), defer_full=True)
    assert run_full is False
    assert test in targets
    # The remote runner syncs only remote_test.SYNC_PATHS, and .claude/ is not
    # among them. Every checkout that can edit these docs has the directory,
    # so the existence check still runs wherever a rename could happen.
    if not (gate.ROOT / doc).is_file() and not (gate.ROOT / ".claude").is_dir():
        pytest.skip(f"{doc} is not synced to this runner")
    assert (gate.ROOT / doc).is_file()
    assert Path(doc).name in (gate.ROOT / test).read_text(encoding="utf-8")


def test_an_unread_doc_still_selects_nothing():
    targets, unmapped, _ = gate.select_tests(
        ["docs/specs/casino_spec.md"], new=set(), defer_full=True
    )
    assert targets == [] and unmapped == []


# A literal repo-doc path in a test: ``"docs" / "x.md"``, ``"docs" / "specs" /
# "x.md"``, ``"docs/x.md"``,
# ``".claude/…/x.md"`` or the manual's path.
_DOC_LITERAL = re.compile(
    r'"docs"\s*/\s*"([\w/.-]+\.md)"'
    r'|"docs"\s*/\s*"specs"\s*/\s*"([\w.-]+\.md)"'
    r'|"((?:docs|\.claude)/[\w/-]+\.md)"'
    r'|"\.claude"\s*/\s*"(\w+)"\s*/\s*"([\w.-]+\.md)"'
    r'|"(src/web_server/static/manual\.html)"'
)

#: Tests that name repo docs without reading the real file: fixture repos
#: built under tmp_path, or doc paths used as gate inputs.
_NOT_DOC_READERS = {
    "tests/dk_mcp/test_dk_mcp_paths_service.py",
    "tests/dk_mcp/test_dk_mcp_sections_service.py",
    "tests/tooling/test_gate_scope.py",
    "tests/tooling/test_relayout_docs.py",
}


def test_every_test_that_names_a_doc_is_routed_by_it():
    """A new test parsing a doc joins DOC_DATA_TESTS, or the doc's edits
    reach it only at CI. The scan sees the common path spellings; a doc
    reached through a constant elsewhere (the register parser in
    scripts/privacy_coverage.py) is mapped by hand."""
    mapped = {(d, t) for d, ts in gate.DOC_DATA_TESTS.items() for t in ts}
    missing = []
    for path in sorted((gate.ROOT / "tests").rglob("test_*.py")):
        rel = path.relative_to(gate.ROOT).as_posix()
        if rel in _NOT_DOC_READERS:
            continue
        for m in _DOC_LITERAL.finditer(path.read_text(encoding="utf-8")):
            a, spec, b, sub, name, manual = m.groups()
            doc = (
                f"docs/{a}" if a else f"docs/specs/{spec}" if spec
                else b or manual or f".claude/{sub}/{name}"
            )
            if (gate.ROOT / doc).is_file() and (doc, rel) not in mapped:
                missing.append(f"{rel} reads {doc}")
    assert not missing, (
        "Add these to DOC_DATA_TESTS in scripts/gate.py so an edit to the doc "
        "selects the test:\n  " + "\n  ".join(sorted(set(missing)))
    )


@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/games/ttl/cog.py",
        "src/bot_modules/dm/perms/layouts.py",
        "src/bot_modules/economy/service.py",
    ],
)
def test_a_bot_module_edit_selects_the_bot_wide_sweeps(path):
    """The mention audit and the no-Container sweep watch every module, so no
    filename token reaches them: a cog gaining an unguarded ``.mention`` send
    mapped only to that cog's own tests and merged on scoped-green, with the
    Bot's ``AllowedMentions.none()`` default silencing the ping in prod."""
    targets, _, run_full = gate.select_tests([path], new=set(), defer_full=True)
    assert run_full is False
    for sweep in gate.BOT_WIDE_SWEEP_TESTS:
        assert sweep in targets


def test_a_dashboard_edit_does_not_select_the_bot_wide_sweeps():
    targets, _, _ = gate.select_tests(
        ["src/web_server/static/js/app.js"], new=set(), defer_full=True
    )
    assert not set(gate.BOT_WIDE_SWEEP_TESTS) & set(targets)


def test_select_tests_still_fans_out_on_an_edited_migration():
    _, _, run_full = gate.select_tests(["src/migrations/134_todo_board.sql"], new=set())
    assert run_full is True


# ── bootstrap: mapped, not fanned out ────────────────────────────────────


def test_bootstrap_edit_does_not_force_a_full_run():
    """Nearly every feature ship registers itself in __main__.py; that edit
    maps to the wiring tests instead of invalidating the whole suite."""
    assert gate.forces_full_run("src/dungeonkeeper/__main__.py", is_new=False) is False


def test_select_tests_maps_bootstrap_to_tests_that_name_it():
    targets, unmapped, run_full = gate.select_tests(
        ["src/dungeonkeeper/__main__.py"], new=set()
    )
    assert run_full is False
    assert "src/dungeonkeeper/__main__.py" not in unmapped
    # The wiring tests read __main__.py by path, so the content scan finds them.
    assert any("wiring" in t or "structure" in t for t in targets), targets


# ── cogs reach the app context one way ────────────────────────────────


def _cog_classes():
    """Every commands.Cog subclass in src/, with its __init__ and its body."""
    import ast

    root = REPO_ROOT / "src"
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not our problem here
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if not any("Cog" in ast.unparse(b) for b in cls.bases):
                continue
            init = next(
                (n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
                None,
            )
            yield path.relative_to(root.parent).as_posix(), cls.name, init, cls


def test_no_cog_takes_a_ctx_parameter():
    """CLAUDE.md: a cog takes ``(self, bot)`` and reaches the context through it.

    Pinned repo-wide rather than per-directory because the one cog this
    branch's sweep missed lived in a feature package, not cogs/. A convention with an unpinned exception is how the next one gets
    written, and a rebase onto a main that added a cog would reintroduce the
    split silently.
    """

    offenders = [
        f"{path}::{name}"
        for path, name, init, _cls in _cog_classes()
        if init is not None and "ctx" in [a.arg for a in init.args.args]
    ]
    assert not offenders, (
        "these cogs still take a ctx argument; take (self, bot) and read "
        "self.bot.ctx instead:\n  " + "\n  ".join(offenders)
    )


def test_no_cog_stores_self_ctx():
    """The field that made helpers duck-type what a caller happened to hold."""
    import ast

    offenders = []
    for path, name, init, _cls in _cog_classes():
        if init is None:
            continue
        for node in ast.walk(init):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "ctx"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    offenders.append(f"{path}::{name}")
    assert not offenders, (
        "these cogs assign self.ctx; every cog already has self.bot, so read "
        "self.bot.ctx and let shared helpers rely on the bot:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_no_cog_reads_self_ctx_anywhere():
    """Not just in __init__ — a cog that reads self.ctx in a method body is an
    AttributeError waiting for that path to run.

    This is the half that protects a rebase. main has moved on while this
    branch was in flight and its new code does use ``self.ctx`` in cog method
    bodies; without this the sweep would have to be re-applied by hand and by
    memory. The __init__-only checks above wouldn't have seen it.
    """
    import ast

    offenders = []
    for path, name, _init, cls in _cog_classes():
        for node in ast.walk(cls):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "ctx"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                offenders.append(f"{path}:{node.lineno}  {name}")
    assert not offenders, (
        "a cog has no self.ctx — read self.bot.ctx instead:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


# ── the work-branch deferral ─────────────────────────────────────────────
#
# A shared-file edit used to cost the whole suite on every commit, on every
# branch. On a work branch that run is redundant: the branch merges into main,
# and main is gated afterwards. So the fallback is deferred there and kept in
# the prod checkout, which has no later gate to inherit.


def test_full_run_triggers_names_the_paths_that_force_a_full_run():
    assert gate.full_run_triggers(
        ["src/bot_modules/core/db_utils.py", "docs/INDEX.md"], set()
    ) == ["src/bot_modules/core/db_utils.py"]
    assert gate.full_run_triggers(["docs/INDEX.md"], set()) == []


def test_deferring_maps_a_shared_file_instead_of_fanning_out():
    """The point of the deferral: real targets, not an empty full-suite run."""
    targets, _, run_full = gate.select_tests(
        ["src/bot_modules/core/db_utils.py"], set(), defer_full=True
    )
    assert run_full is False
    assert targets, "a deferred shared file must still map to its own tests"


def test_not_deferring_keeps_the_fallback():
    _, _, run_full = gate.select_tests(
        ["src/bot_modules/core/db_utils.py"], set(), defer_full=False
    )
    assert run_full is True


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_in_linked_worktree_tells_a_worktree_from_its_main_checkout(tmp_path):
    """Guards the deferral's own gate: prod must never defer to itself.

    Built as a real repo plus a real `git worktree` rather than asserting
    against the ambient checkout, because the ambient answer differs by where
    the suite runs — False in prod, True in any dk-session worktree. A test
    pinning one of those passes in prod, goes green in CI (a fresh clone is not
    a worktree), and fails for every developer running it from a session.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "gate@example.com")
    _git(main, "config", "user.name", "gate")
    (main / "f.txt").write_text("hi", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")

    linked = tmp_path / "session"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))

    assert gate.in_linked_worktree(main) is False
    assert gate.in_linked_worktree(linked) is True


def test_somewhere_that_is_not_a_repo_does_not_defer(tmp_path):
    """The safe default: if git can't answer, run the full suite rather than
    silently skip it."""
    assert gate.in_linked_worktree(tmp_path) is False


# ── the full-gate tag ────────────────────────────────────────────────────
#
# Work branches defer the full suite to main, and nothing forces that run. The
# tag records what this machine has actually verified so teardown can say how
# far main has drifted from it.


def _repo(tmp_path: Path) -> Path:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "gate@example.com")
    _git(main, "config", "user.name", "gate")
    (main / "f.txt").write_text("hi", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")
    return main


def test_ungated_merges_is_none_before_the_tag_exists(tmp_path):
    """Unknown must not read as zero — they call for different advice."""
    assert gate.ungated_merges(_repo(tmp_path)) is None


def test_marking_then_counting_walks_from_zero(tmp_path):
    main = _repo(tmp_path)
    assert gate.mark_full_gate(main) is not None
    assert gate.ungated_merges(main) == 0

    for i in range(3):
        (main / f"m{i}.txt").write_text("x", encoding="utf-8")
        _git(main, "add", "-A")
        _git(main, "commit", "-qm", f"merge {i}")
    assert gate.ungated_merges(main) == 3

    gate.mark_full_gate(main)
    assert gate.ungated_merges(main) == 0


def test_a_work_branch_cannot_clear_mains_backlog(tmp_path):
    """The tag says main was verified. A green branch does not say that, so a
    full run from a linked worktree must leave the count alone."""
    main = _repo(tmp_path)
    gate.mark_full_gate(main)
    (main / "m.txt").write_text("x", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "a merge")

    linked = tmp_path / "session"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
    assert gate.mark_full_gate(linked) is None
    assert gate.ungated_merges(main) == 1


def test_a_detached_or_side_branch_checkout_does_not_mark(tmp_path):
    """Only main. Marking from anywhere else would credit main for a run that
    never touched it."""
    main = _repo(tmp_path)
    _git(main, "checkout", "-q", "-b", "side")
    assert gate.mark_full_gate(main) is None
    assert gate.ungated_merges(main) is None


def test_a_missing_repo_is_quiet(tmp_path):
    """A missing tag is a missing nag, never a failed gate."""
    assert gate.mark_full_gate(tmp_path) is None
    assert gate.ungated_merges(tmp_path) is None


def test_pytest_runs_without_the_hooks_git_pointers(monkeypatch):
    """A hooked commit exports GIT_DIR/GIT_INDEX_FILE; a test building a
    scratch repo that inherits them acts on the real one (2026-09-26: prod
    flipped to core.bare=true and the committing index was replaced)."""
    monkeypatch.setenv("GIT_DIR", "/real/.git/worktrees/x")
    monkeypatch.setenv("GIT_INDEX_FILE", "/real/.git/worktrees/x/index")
    monkeypatch.setenv("GIT_WORK_TREE", "/real")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kept")
    env = gate.pytest_env()
    assert not {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"} & env.keys()
    assert env["GIT_AUTHOR_NAME"] == "kept"
