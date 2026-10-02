"""Remote pytest dispatch — config parsing, command building, and fallback.

The contract that matters: this must never block a commit. Every "can't
dispatch" path has to return None so gate.py runs locally instead. These
tests exercise that without touching the network.
"""

from __future__ import annotations

import os

import pytest
from pathlib import Path

from scripts import remote_test as rt

# Direct bindings for the housekeeping helpers: the autouse fixture below
# stubs them out on the module (so bootstrap() in tests never touches the real
# machine), and these pre-fixture bindings are how their own unit tests still
# reach the real implementations.
from scripts.remote_test import (
    gc_workspaces,
    preflight,
    slot_dir,
    sweep_stale_pytest_tmp,
    sweep_stale_template_dbs,
    sweep_stale_tmp,
)


class _Exit:
    """Stub subprocess result: just a returncode."""

    def __init__(self, code):
        self.returncode = code


class _Result(_Exit):
    """Stub subprocess result with captured stdout (for the git helpers)."""

    def __init__(self, code, stdout=""):
        super().__init__(code)
        self.stdout = stdout


@pytest.fixture(autouse=True)
def _hermetic_housekeeping(monkeypatch):
    """bootstrap() housekeeping must never touch the machine running the tests.

    Without this, every bootstrap test would sweep the real pytest tmp dir and
    stat the real filesystem — and a nearly-full /tmp would fail unrelated
    tests. Tests of the helpers themselves use the module-level imports above;
    tests of bootstrap's *wiring* re-monkeypatch over these stubs.
    """
    monkeypatch.setattr(rt, "sweep_stale_tmp", lambda *a, **k: 0)
    monkeypatch.setattr(rt, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(rt, "gc_workspaces", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _hermetic_slots(monkeypatch, tmp_path):
    """The runner semaphore counts in the real git common dir; tests must not.

    Every run() test would otherwise claim (and, on a crash, leak) a slot in
    the prod checkout's .git, and a gate running these tests could see them
    as competing runs. Slots go under tmp_path, and a wait never sleeps for
    real: the fake clock advances by whatever was asked.
    """
    monkeypatch.setattr(rt, "slot_dir", lambda root=None: tmp_path / "slots")
    clock = _FakeClock()
    monkeypatch.setattr(rt, "_clock", clock.now)
    monkeypatch.setattr(rt, "_sleep", clock.sleep)
    return clock


class _FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

FULL_ENV = {
    "REMOTE_TEST_HOST": "ben@bigbox",
    "REMOTE_TEST_DIR": "C:/dev/dungeon-keeper",
    "REMOTE_TEST_PYTHON": "C:/dev/dungeon-keeper/.venv/Scripts/python.exe",
    # Pin the workspace so path assertions stay stable and don't depend on the
    # slug of whatever checkout the tests happen to run in. Dedicated tests
    # below cover slug generation and the disabled/legacy layout.
    "REMOTE_TEST_WORKSPACE": "wsfixed",
}
RUN_DIR = "C:/dev/dungeon-keeper/wsfixed"


# ── load_config ────────────────────────────────────────────────────────────────


def test_unset_host_means_local():
    assert rt.load_config({}) is None


def test_blank_host_means_local():
    assert rt.load_config({"REMOTE_TEST_HOST": "   "}) is None


@pytest.mark.parametrize("flag", ["1", "true", "yes"])
def test_gate_no_remote_forces_local(flag):
    assert rt.load_config({**FULL_ENV, "GATE_NO_REMOTE": flag}) is None


def test_gate_no_remote_ignores_unrecognised_values():
    """A typo'd opt-out must not silently keep dispatching... or silently stop."""
    cfg = rt.load_config({**FULL_ENV, "GATE_NO_REMOTE": "maybe"})
    assert cfg is not None and cfg.host == "ben@bigbox"


def test_host_without_dir_or_python_raises():
    """Half-configured is a mistake, not a reason to quietly run locally."""
    with pytest.raises(ValueError, match="refusing to guess"):
        rt.load_config({"REMOTE_TEST_HOST": "ben@bigbox"})

    with pytest.raises(ValueError, match="refusing to guess"):
        rt.load_config({"REMOTE_TEST_HOST": "ben@bigbox", "REMOTE_TEST_DIR": "/x"})


def test_defaults_applied():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert cfg.jobs == rt.DEFAULT_JOBS
    assert cfg.cd_template == rt.DEFAULT_CD


def test_jobs_override():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_JOBS": "16"})
    assert cfg is not None and cfg.jobs == 16


@pytest.mark.parametrize("bad", ["abc", "1.5", "0", "-4"])
def test_invalid_jobs_rejected(bad):
    with pytest.raises(ValueError):
        rt.load_config({**FULL_ENV, "REMOTE_TEST_JOBS": bad})


def test_cd_template_override_for_cmd_exe():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_CD": "cd /d {dir} && {cmd}"})
    assert cfg is not None
    assert rt.remote_command(cfg, "echo hi")[-1] == "cd /d " + RUN_DIR + " && echo hi"


# ── argument safety ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arg",
    [
        "tests/test_a.py; rm -rf /",
        "tests/test a.py",
        "-k spam and eggs",
        'tests/"quoted".py',
        "tests/$(whoami).py",
        "tests/a.py|b",
        "",
    ],
)
def test_unsafe_arguments_rejected(arg):
    with pytest.raises(rt.UnsafeArgument):
        rt.check_args([arg])


@pytest.mark.parametrize(
    "arg",
    ["tests/shared/test_ollama_client.py", "-x", "--maxfail=2", "-ktest_foo"],
)
def test_safe_arguments_accepted(arg):
    rt.check_args([arg])  # must not raise


def test_run_falls_back_locally_on_unsafe_argument(capsys):
    """An argument we can't quote means local, not a mangled remote run."""
    assert rt.run(["-k", "spam and eggs"], env=FULL_ENV) is None
    assert "running locally" in capsys.readouterr().err


# ── exec_remote: the transport without pytest ─────────────────────────────────


def test_exec_remote_returns_none_when_unconfigured():
    assert rt.exec_remote("python -c pass", env={}) is None


def test_exec_remote_returns_none_on_bad_config_instead_of_raising(capsys):
    assert rt.exec_remote("python -c pass", env={"REMOTE_TEST_HOST": "h"}) is None
    assert "refusing to guess" in capsys.readouterr().err


def test_exec_remote_falls_back_when_host_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: False)
    assert rt.exec_remote("python -c pass", env=FULL_ENV) is None
    assert "running locally" in capsys.readouterr().err


def test_exec_remote_retries_the_sync_once_then_gives_up(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    calls = []
    monkeypatch.setattr(rt, "sync", lambda cfg: calls.append(1) or False)
    assert rt.exec_remote("python -c pass", env=FULL_ENV) is None
    assert len(calls) == 2
    assert "running locally" in capsys.readouterr().err


def test_exec_remote_runs_in_the_workspace_and_returns_the_exit_code(monkeypatch):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Exit(7)

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    assert rt.exec_remote("python scripts/mahjong_sim.py -n 4", env=FULL_ENV) == 7
    assert seen["argv"][0] == "ssh"
    assert f"cd {RUN_DIR} && python scripts/mahjong_sim.py -n 4" in seen["argv"][-1]


def test_exec_remote_treats_a_lost_connection_as_no_result(monkeypatch, capsys):
    """255 is ssh, not the command — reporting it as an exit code would make
    a network blip look like a failed simulation."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(255))
    assert rt.exec_remote("python -c pass", env=FULL_ENV) is None
    assert "transport failed" in capsys.readouterr().err


def test_exec_remote_keeps_stdout_clean_for_piping(monkeypatch, capsys):
    """A dispatched command's stdout is its result — callers redirect it to a
    file. Every status line must go to stderr."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    rt.exec_remote("python -c pass", env=FULL_ENV)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dispatching" in captured.err


def test_exec_remote_times_out_into_a_local_fallback(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    def boom(*a, **k):
        raise rt.subprocess.TimeoutExpired(cmd="ssh", timeout=1)

    monkeypatch.setattr(rt.subprocess, "run", boom)
    assert rt.exec_remote("python -c pass", env=FULL_ENV, timeout=1) is None
    assert "no result after" in capsys.readouterr().err


def test_remote_python_and_jobs_read_the_config():
    assert rt.remote_python(FULL_ENV) == FULL_ENV["REMOTE_TEST_PYTHON"]
    assert rt.remote_jobs(FULL_ENV) == rt.DEFAULT_JOBS
    assert rt.remote_python({}) is None
    assert rt.remote_jobs({}) is None


# ── mahjong_sim --remote dispatch ─────────────────────────────────────────────

from scripts import mahjong_sim as ms  # noqa: E402


def test_synced_roots_are_read_from_the_transport_not_restated():
    roots = ms.synced_roots()
    assert "src" in roots and "scripts" in roots
    assert all(r in rt.SYNC_PATHS for r in roots)
    # files in SYNC_PATHS are not directories a card could live in
    assert "README.md" not in roots


def test_no_card_means_the_shipped_default():
    assert ms.remote_card_arg(None) is None


def test_a_card_inside_a_synced_directory_becomes_a_relative_path():
    card = rt.ROOT / "src/bot_modules/games/mahjong/cards/meadow_first_light.json"
    assert ms.remote_card_arg(card) == (
        "src/bot_modules/games/mahjong/cards/meadow_first_light.json"
    )


def test_a_card_outside_the_repo_is_refused_with_a_reason(tmp_path):
    card = tmp_path / "card.json"
    card.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the repo"):
        ms.remote_card_arg(card)


def test_a_card_in_an_unsynced_repo_directory_is_refused(tmp_path):
    """In the repo is not enough — the sync only ships some of it."""
    with pytest.raises(ValueError, match="not synced"):
        ms.remote_card_arg(rt.ROOT / "models" / "card.json")


def test_the_forwarded_argv_round_trips_through_the_parser():
    """Whatever --remote sends must mean the same thing over there."""
    import argparse
    parsed = argparse.Namespace(
        card=None, games=250, seats=2, seed=9, wall_trim=60,
        no_second_charleston=True, workers=12, top=5, json=True,
    )
    argv = ms._remote_argv(parsed, None)
    assert argv[:2] == ["-n", "250"]
    for flag, value in (("--seats", "2"), ("--seed", "9"),
                        ("--wall-trim", "60"), ("-j", "12"), ("--top", "5")):
        assert argv[argv.index(flag) + 1] == value
    assert "--no-second-charleston" in argv and "--json" in argv
    assert "--remote" not in argv, "the far side must not dispatch again"


def test_the_card_leads_the_forwarded_argv():
    import argparse
    parsed = argparse.Namespace(
        card=None, games=1, seats=4, seed=0, wall_trim=0,
        no_second_charleston=False, workers=1, top=None, json=False,
    )
    argv = ms._remote_argv(parsed, "src/cards/x.json")
    assert argv[0] == "src/cards/x.json"


# ── command construction ───────────────────────────────────────────────────────


def test_probe_command_is_non_interactive():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    cmd = rt.probe_command(cfg, timeout=5)

    # BatchMode is what stops ssh hanging a commit hook on a password prompt.
    assert "BatchMode=yes" in cmd
    assert "ConnectTimeout=5" in cmd
    assert cmd[-2:] == ["ben@bigbox", "exit"]


def test_pytest_command_includes_targets_and_jobs():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_JOBS": "8"})
    assert cfg is not None
    inner = rt.pytest_command(cfg, ["tests/test_a.py", "tests/test_b.py"])[-1]

    assert inner.startswith("cd " + RUN_DIR + " && ")
    assert "scripts/remote_test.py --bootstrap --lock requirements/dev.lock -n 8 " in inner
    assert inner.endswith("tests/test_a.py tests/test_b.py")


def test_pytest_command_collapses_a_target_list_cmd_exe_would_refuse():
    """A layout stage selected hundreds of test files and cmd.exe refused the
    line; past the cap they become ``tests``, keeping every option."""
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    many = [f"tests/test_feature_{i:04d}.py" for i in range(400)]
    inner = rt.pytest_command(cfg, ["-x", *many, "--maxfail=3"])[-1]
    assert inner.endswith("-n 12 -x --maxfail=3 tests")
    assert len(inner) < 8191


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["tests/test_a.py", "-x"], id="short-list-kept"),
        pytest.param(["-k", "x" * (rt.MAX_TARGET_CHARS + 1)], id="long-but-no-test-paths"),
    ],
)
def test_collapse_targets_leaves_what_it_cannot_shorten(args):
    assert rt.collapse_targets(args) == args


def test_collapse_targets_keeps_a_path_that_is_an_options_value():
    many = [f"tests/test_feature_{i:04d}.py" for i in range(400)]
    out = rt.collapse_targets(["--deselect", "tests/test_a.py::t", *many])
    assert out == ["--deselect", "tests/test_a.py::t", "tests"]


def test_pytest_command_with_no_targets_has_no_trailing_space():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert rt.pytest_command(cfg, [])[-1].endswith("--lock requirements/dev.lock -n 12")


def test_pytest_command_goes_through_bootstrap_not_pytest_directly():
    """The staleness check must not be bypassable by the happy path."""
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    inner = rt.pytest_command(cfg, [])[-1]
    assert "--bootstrap" in inner
    assert "-m pytest" not in inner


# ── remote-side bootstrap ──────────────────────────────────────────────────────


def _write_locks(root, main="a", dev="b"):
    (root / "requirements").mkdir(exist_ok=True)
    (root / "requirements/base.lock").write_text(main, encoding="utf-8")
    (root / "requirements/dev.lock").write_text(dev, encoding="utf-8")


def test_lock_hash_is_stable_and_content_sensitive(tmp_path):
    _write_locks(tmp_path)
    first = rt.lock_hash(tmp_path)

    assert first == rt.lock_hash(tmp_path), "hash must be reproducible"

    (tmp_path / "requirements/dev.lock").write_text("changed", encoding="utf-8")
    assert rt.lock_hash(tmp_path) != first


def test_lock_hash_distinguishes_which_file_changed(tmp_path):
    """Swapping content between the two locks must not collide."""
    _write_locks(tmp_path, main="x", dev="y")
    one = rt.lock_hash(tmp_path)
    _write_locks(tmp_path, main="y", dev="x")
    assert rt.lock_hash(tmp_path) != one


def test_lock_hash_tolerates_a_missing_lock(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements/base.lock").write_text("only-one", encoding="utf-8")
    assert rt.lock_hash(tmp_path)  # does not raise


def test_needs_install_when_no_stamp(tmp_path):
    _write_locks(tmp_path)
    assert rt.needs_install(tmp_path, rt.lock_hash(tmp_path)) is True


def test_needs_install_false_after_stamp_written(tmp_path):
    _write_locks(tmp_path)
    digest = rt.lock_hash(tmp_path)
    rt.write_stamp(tmp_path, digest)
    assert rt.needs_install(tmp_path, digest) is False


def test_needs_install_true_after_lock_changes(tmp_path):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))

    (tmp_path / "requirements/dev.lock").write_text("bumped", encoding="utf-8")
    assert rt.needs_install(tmp_path, rt.lock_hash(tmp_path)) is True


def test_read_stamp_survives_a_corrupt_file(tmp_path):
    (tmp_path / rt.STAMP_FILE).write_bytes(b"\xff\xfe\x00garbage")
    assert rt.read_stamp(tmp_path) == ""  # treated as "needs install"


def test_bootstrap_skips_install_when_stamp_current(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))

    calls = []
    monkeypatch.setattr(rt, "install_deps", lambda py, root, lock: calls.append("install") or 0)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))

    assert rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python")) == 0
    assert calls == []


def test_bootstrap_installs_then_stamps_when_stale(tmp_path, monkeypatch):
    _write_locks(tmp_path)

    monkeypatch.setattr(rt, "install_deps", lambda py, root, lock: 0)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))

    assert rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python")) == 0
    assert rt.read_stamp(tmp_path) == rt.lock_hash(tmp_path)


def test_bootstrap_returns_sentinel_when_install_fails(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    monkeypatch.setattr(rt, "install_deps", lambda py, root, lock: 1)

    ran = []
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: ran.append(a) or None)

    assert rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python")) == rt.BOOTSTRAP_FAILED
    assert ran == [], "pytest must not run against a half-installed venv"
    assert rt.read_stamp(tmp_path) == "", "a failed install must not be stamped"


def test_bootstrap_sentinel_cannot_collide_with_pytest_exit_codes():
    assert rt.BOOTSTRAP_FAILED > 5


def test_bootstrap_propagates_test_failure(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(1))

    assert rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python")) == 1


def test_run_falls_back_locally_on_bootstrap_sentinel(monkeypatch, capsys):
    """A broken remote venv is an environment problem, not a red suite."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(rt.BOOTSTRAP_FAILED))

    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "remote setup failed" in capsys.readouterr().err


def test_locks_are_synced_so_the_remote_can_detect_staleness():
    for name in rt.LOCK_FILES:
        assert name in rt.SYNC_PATHS


def test_prose_docs_are_synced_so_their_doc_tests_cant_go_stale():
    # test_games_help_logic.py checks docs/features.md's party-game count
    # against the code — without shipping it every run, a remote's only copy
    # is whatever its initial `git clone` had, drifting silently forever
    # after. README.md rides along for the same reason: it is tiny, and it
    # carried this very tripwire until 2026-09-03.
    assert "docs" in rt.SYNC_PATHS
    assert "README.md" in rt.SYNC_PATHS


def test_test_input_trees_are_synced():
    # The quote renderer hard-fails without assets/fonts, and docs/ holds
    # test inputs (features.md, the INDEX) — a workspace missing either
    # fails tests that pass everywhere else. The full assets/ tree
    # (logos, an 8MB gif) stays out: fonts only.
    assert "docs" in rt.SYNC_PATHS
    assert "assets/fonts" in rt.SYNC_PATHS
    assert "assets" not in rt.SYNC_PATHS


# ── .env fallback ──────────────────────────────────────────────────────────────


def _write_env(root, body):
    (root / ".env").write_text(body, encoding="utf-8")


def test_env_file_values_reads_only_our_keys(tmp_path):
    _write_env(tmp_path, "\n".join([
        "DISCORD_TOKEN_PROD=supersecret",
        "REMOTE_TEST_HOST=ben@box",
        "REMOTE_TEST_DIR=C:/dev/dk",
        "GATE_NO_REMOTE=0",
        "SESSION_SECRET=alsosecret",
    ]))
    values = rt.env_file_values(tmp_path)

    assert values == {
        "REMOTE_TEST_HOST": "ben@box",
        "REMOTE_TEST_DIR": "C:/dev/dk",
        "GATE_NO_REMOTE": "0",
    }
    # Secrets in .env must not be hoovered up by a dev-tooling helper.
    assert "DISCORD_TOKEN_PROD" not in values
    assert "SESSION_SECRET" not in values


def test_env_file_values_strips_comments_and_quotes(tmp_path):
    _write_env(tmp_path, '\n'.join([
        "# a comment",
        "",
        'REMOTE_TEST_HOST="ben@box"   # trailing note',
        "REMOTE_TEST_DIR='C:/dev/dk'",
    ]))
    values = rt.env_file_values(tmp_path)
    assert values["REMOTE_TEST_HOST"] == "ben@box"
    assert values["REMOTE_TEST_DIR"] == "C:/dev/dk"


def test_env_file_values_missing_file_is_empty(tmp_path):
    assert rt.env_file_values(tmp_path) == {}


def test_env_path_prefers_the_local_checkout(tmp_path):
    _write_env(tmp_path, "REMOTE_TEST_HOST=ben@box")
    assert rt.env_path(tmp_path) == tmp_path / ".env"


def test_env_path_falls_back_to_the_main_checkout_from_a_worktree(tmp_path, monkeypatch):
    """Edits happen in worktrees, which have no .env of their own."""
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    _write_env(main, "REMOTE_TEST_HOST=ben@box")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_git(cmd, **kwargs):
        assert cmd[:2] == ["git", "rev-parse"]
        return _Result(0, str(main / ".git"))

    monkeypatch.setattr(rt.subprocess, "run", fake_git)
    assert rt.env_path(worktree) == main / ".env"


def test_env_path_is_none_when_git_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Result(128))
    assert rt.env_path(tmp_path) is None


def test_env_path_survives_git_being_absent(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(rt.subprocess, "run", boom)
    assert rt.env_path(tmp_path) is None


def test_env_file_values_ignores_malformed_lines(tmp_path):
    _write_env(tmp_path, "REMOTE_TEST_HOST\nnot a setting\nREMOTE_TEST_DIR=C:/x")
    assert rt.env_file_values(tmp_path) == {"REMOTE_TEST_DIR": "C:/x"}


def test_real_environment_overrides_env_file(monkeypatch, tmp_path):
    """GATE_NO_REMOTE=1 on the command line must beat .env without editing it."""
    monkeypatch.setattr(rt, "env_file_values", lambda root=None: dict(FULL_ENV))
    monkeypatch.setenv("GATE_NO_REMOTE", "1")
    assert rt.load_config() is None


def test_env_file_supplies_config_when_shell_has_none(monkeypatch):
    monkeypatch.setattr(rt, "env_file_values", lambda root=None: dict(FULL_ENV))
    for key in FULL_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GATE_NO_REMOTE", raising=False)

    cfg = rt.load_config()
    assert cfg is not None and cfg.host == "ben@bigbox"


# ── per-host lock file ─────────────────────────────────────────────────────────


def test_lock_defaults_to_the_dev_lock():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None and cfg.lock == rt.DEFAULT_LOCK


def test_lock_override_reaches_the_remote_command():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_LOCK": "requirements-win.lock"})
    assert cfg is not None and cfg.lock == "requirements-win.lock"
    assert "--lock requirements-win.lock" in rt.pytest_command(cfg, [])[-1]


@pytest.mark.parametrize(
    ("configured", "resolved"),
    [
        ("requirements-dev.lock", "requirements/dev.lock"),
        ("requirements.lock", "requirements/base.lock"),
    ],
)
def test_a_pre_move_lock_name_maps_to_its_new_home(configured, resolved):
    """The locks moved into requirements/ on 2026-09-28, but the production
    .env still names requirements-dev.lock. That file is no longer synced, so
    passing the old name through would fail the remote pip install on every
    stale venv and silently push every gate onto the local fallback."""
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_LOCK": configured})
    assert cfg is not None and cfg.lock == resolved
    assert resolved in rt.LOCK_FILES


def test_lock_with_unsafe_characters_is_rejected():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_LOCK": "a lock.txt; rm -rf /"})
    assert cfg is not None
    with pytest.raises(rt.UnsafeArgument):
        rt.pytest_command(cfg, [])


def test_install_deps_uses_the_configured_lock(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Exit(0)

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    rt.install_deps("py", Path("/tmp"), "requirements-win.lock")
    assert seen["cmd"][-1] == "requirements-win.lock"


def test_bootstrap_passes_lock_through_to_install(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    seen = {}
    monkeypatch.setattr(rt, "install_deps",
                        lambda py, root, lock: seen.setdefault("lock", lock) or 0)
    monkeypatch.setattr(rt.subprocess, "run",
                        lambda *a, **k: _Exit(0))

    rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python"), lock="requirements-win.lock")
    assert seen["lock"] == "requirements-win.lock"


def test_tar_command_excludes_caches_and_never_ships_secrets():
    cmd = rt.tar_command()
    assert "--exclude=__pycache__" in cmd

    # The suite needs no .env, database, or model weights — so none are synced.
    joined = " ".join(cmd)
    for secret in (".env", "dungeonkeeper.db", "models", "backups", ".git"):
        assert secret not in rt.SYNC_PATHS
    assert "src" in cmd and "tests" in cmd
    assert "dungeonkeeper.db" not in joined


# ── fallback wiring ────────────────────────────────────────────────────────────


def test_run_returns_none_when_unconfigured():
    assert rt.run(["tests/test_a.py"], env={}) is None


def test_run_returns_none_on_bad_config_instead_of_raising(capsys):
    """gate.py must not crash because REMOTE_TEST_* is half-set."""
    assert rt.run(["tests/test_a.py"], env={"REMOTE_TEST_HOST": "h"}) is None
    assert "refusing to guess" in capsys.readouterr().err


def test_run_falls_back_when_host_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: False)
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "unreachable" in capsys.readouterr().out


def test_run_falls_back_when_sync_fails(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: False)
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "sync failed" in capsys.readouterr().err


def test_run_returns_remote_exit_code_on_success(monkeypatch):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) == 0


def test_run_propagates_remote_failure(monkeypatch):
    """A remote failure must fail the gate, not silently retry locally."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(1))
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) == 1


def test_is_available_is_false_without_ssh(monkeypatch):
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert rt.is_available(cfg) is False


# ── Session checkouts are clones, not worktrees ───────────────────────
#
# The worktree fallback resolves --git-common-dir, which in a plain clone is
# the clone's *own* .git — so it lands back on the checkout that had no .env
# to begin with. Session checkouts are cloned from the main one, so without
# the origin fallback every gate run in them silently went local: a ~10x
# slowdown that reports nothing, because "no config" legitimately means
# "run locally".


def _fake_git(mapping):
    """Stub subprocess.run for the read-only git calls env_path makes."""
    def run(cmd, **kwargs):
        key = tuple(cmd[1:])
        out = mapping.get(key)
        return _Result(0 if out is not None else 128, out or "")
    return run


def test_env_path_follows_a_local_origin_from_a_clone(tmp_path, monkeypatch):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    _write_env(main, "REMOTE_TEST_HOST=ben@box")

    clone = tmp_path / "session"
    (clone / ".git").mkdir(parents=True)

    monkeypatch.setattr(rt.subprocess, "run", _fake_git({
        # A clone reports its own .git, so the worktree fallback finds nothing.
        ("rev-parse", "--git-common-dir"): ".git",
        ("config", "--get", "remote.origin.url"): str(main),
    }))
    assert rt.env_path(clone) == main / ".env"


def test_env_path_follows_an_origin_pointing_at_a_bare_git_dir(tmp_path, monkeypatch):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    _write_env(main, "REMOTE_TEST_HOST=ben@box")

    clone = tmp_path / "session"
    (clone / ".git").mkdir(parents=True)

    monkeypatch.setattr(rt.subprocess, "run", _fake_git({
        ("rev-parse", "--git-common-dir"): ".git",
        ("config", "--get", "remote.origin.url"): str(main / ".git"),
    }))
    assert rt.env_path(clone) == main / ".env"


@pytest.mark.parametrize("origin", [
    "https://github.com/example/dk.git",
    "git@github.com:example/dk.git",
    "ssh://git@example.com/dk.git",
])
def test_env_path_ignores_non_filesystem_origins(tmp_path, monkeypatch, origin):
    """A network remote has no .env to read; don't try to treat it as a path."""
    clone = tmp_path / "session"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(rt.subprocess, "run", _fake_git({
        ("rev-parse", "--git-common-dir"): ".git",
        ("config", "--get", "remote.origin.url"): origin,
    }))
    assert rt.env_path(clone) is None


def test_env_path_is_none_when_origin_has_no_env(tmp_path, monkeypatch):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)  # exists, but no .env in it
    clone = tmp_path / "session"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(rt.subprocess, "run", _fake_git({
        ("rev-parse", "--git-common-dir"): ".git",
        ("config", "--get", "remote.origin.url"): str(main),
    }))
    assert rt.env_path(clone) is None


# ── Per-checkout workspaces ───────────────────────────────────────────
#
# One test host serves several session checkouts. Before workspaces they all
# extracted over the same directory, so two concurrent runs interleaved their
# writes; and because tar only adds and overwrites, a file removed from a
# branch (or belonging to another branch tested earlier) survived forever and
# ran as a phantom test. That is exactly how a stale src/ once produced 22
# failures that did not reproduce locally.


def test_workspace_slug_is_stable_and_safe(tmp_path):
    first = rt.workspace_slug(tmp_path)
    assert first == rt.workspace_slug(tmp_path)
    assert first.startswith("ws-")
    assert not (rt._UNSAFE & set(first))
    assert "/" not in first and "\\" not in first


def test_workspace_slug_differs_per_checkout_even_with_the_same_name(tmp_path):
    a = tmp_path / "one" / "gambling"
    b = tmp_path / "two" / "gambling"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert rt.workspace_slug(a) != rt.workspace_slug(b)


def test_run_dir_nests_the_workspace_under_the_configured_directory():
    cfg = rt.RemoteConfig(host="h", directory="C:/dev/dk", python="py", workspace="ws-x")
    assert cfg.run_dir == "C:/dev/dk/ws-x"


def test_run_dir_is_the_directory_itself_without_a_workspace():
    cfg = rt.RemoteConfig(host="h", directory="C:/dev/dk", python="py")
    assert cfg.run_dir == "C:/dev/dk"


def test_tar_prefixes_every_member_with_the_workspace():
    argv = rt.tar_command(prefix="ws-x")
    assert "--transform=s,^,ws-x/," in argv


def test_extraction_happens_at_the_base_so_tar_creates_the_workspace():
    cfg = rt.RemoteConfig(host="h", directory="C:/dev/dk", python="py", workspace="ws-x")
    assert "cd C:/dev/dk &&" in rt.remote_command(cfg, "tar -xzf -", base=True)[-1]
    # pytest, by contrast, must run inside the workspace
    assert "cd C:/dev/dk/ws-x &&" in rt.pytest_command(cfg, ["-q"])[-1]


def test_workspace_can_be_disabled_for_the_legacy_layout():
    env = {
        "REMOTE_TEST_HOST": "ben@box", "REMOTE_TEST_DIR": "C:/dev/dk",
        "REMOTE_TEST_PYTHON": "py.exe", "REMOTE_TEST_WORKSPACE": "off",
    }
    assert rt.load_config(env).workspace == ""


def test_manifest_lists_shipped_files_and_skips_bytecode(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\x00")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    lines = rt.manifest_lines(("src", "README.md"), root=tmp_path)
    assert lines == ["src/a.py", "README.md"]


def test_prune_removes_files_the_sync_did_not_ship(tmp_path):
    (tmp_path / "tests").mkdir()
    keep = tmp_path / "tests" / "test_keep.py"
    phantom = tmp_path / "tests" / "test_phantom.py"
    keep.write_text("", encoding="utf-8")
    phantom.write_text("", encoding="utf-8")
    (tmp_path / rt.MANIFEST_FILE).write_text("tests/test_keep.py\n", encoding="utf-8")

    removed = rt.prune_to_manifest(tmp_path)
    assert removed == ["tests/test_phantom.py"]
    assert keep.exists() and not phantom.exists()


def test_prune_leaves_bytecode_and_unsynced_roots_alone(tmp_path):
    (tmp_path / "tests" / "__pycache__").mkdir(parents=True)
    cached = tmp_path / "tests" / "__pycache__" / "x.cpython-314.pyc"
    cached.write_bytes(b"\x00")
    outside = tmp_path / ".venv"
    outside.mkdir()
    lib = outside / "lib.py"
    lib.write_text("", encoding="utf-8")
    (tmp_path / rt.MANIFEST_FILE).write_text("tests/test_keep.py\n", encoding="utf-8")

    rt.prune_to_manifest(tmp_path)
    assert cached.exists(), "bytecode is not ours to manage"
    assert lib.exists(), "only synced roots may be pruned — never the venv"


def test_prune_is_a_no_op_without_a_manifest(tmp_path):
    (tmp_path / "tests").mkdir()
    survivor = tmp_path / "tests" / "test_a.py"
    survivor.write_text("", encoding="utf-8")
    assert rt.prune_to_manifest(tmp_path) == []
    assert survivor.exists(), "an older sender must not trigger deletions"


@pytest.mark.parametrize("exe,expected", [
    ("C:/dev/dk/.venv/Scripts/python.exe", "C:/dev/dk/.venv"),
    ("/srv/dk/.venv/bin/python", "/srv/dk/.venv"),
    ("/usr/bin/python3", "/usr/bin"),
])
def test_stamp_dir_sits_beside_the_venv_not_in_a_workspace(exe, expected):
    """Workspaces are disposable; a multi-GB reinstall per checkout is not."""
    assert rt.stamp_dir(exe).as_posix() == expected


# ── Transport failures must not read as test failures ─────────────────
#
# pytest's own exit codes are 0-5 and BOOTSTRAP_FAILED is 97; everything else
# coming back from the ssh invocation is the *transport* failing (255 for a
# dropped connection, 128+N for a signal-killed ssh). Before this was handled,
# a Wi-Fi blip mid-run surfaced as "GATE FAILED: pytest" and blocked the
# commit — the exact outcome the module docstring promises can't happen.


@pytest.mark.parametrize("code", [255, 137, 6, -9])
def test_run_falls_back_on_transport_failure(monkeypatch, capsys, code):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(code))

    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "running locally" in capsys.readouterr().err


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5])
def test_run_propagates_every_genuine_pytest_exit_code(monkeypatch, code):
    """Real results — pass, red, interrupted, usage error — must reach the gate."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(code))

    assert rt.run(["tests/test_a.py"], env=FULL_ENV) == code


def test_remote_command_keeps_the_connection_alive():
    """A network drop mid-run must fail fast, not hang the commit hook on TCP."""
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    cmd = rt.remote_command(cfg, "echo hi")
    assert "ServerAliveInterval=15" in cmd
    assert "ServerAliveCountMax=4" in cmd


# ── Wall-clock cap ─────────────────────────────────────────────────────


def test_timeout_defaults_to_uncapped():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None and cfg.timeout == 0


def test_timeout_parsed_from_env():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_TIMEOUT": "900"})
    assert cfg is not None and cfg.timeout == 900


@pytest.mark.parametrize("bad", ["abc", "1.5", "-30"])
def test_invalid_timeout_rejected(bad):
    with pytest.raises(ValueError):
        rt.load_config({**FULL_ENV, "REMOTE_TEST_TIMEOUT": bad})


def test_run_falls_back_when_remote_exceeds_timeout(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    def hang(*args, **kwargs):
        # Local cap = remote cap + grace, so the remote-side enforcement
        # (which kills pytest cleanly) always gets to fire first.
        assert kwargs.get("timeout") == 960, "the outer cap must reach subprocess.run"
        raise rt.subprocess.TimeoutExpired(cmd="ssh", timeout=960)

    monkeypatch.setattr(rt.subprocess, "run", hang)
    assert rt.run(["-q"], env={**FULL_ENV, "REMOTE_TEST_TIMEOUT": "900"}) is None
    assert "running locally" in capsys.readouterr().err


def test_timeout_ships_to_the_remote_bootstrap():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_TIMEOUT": "900"})
    assert cfg is not None
    assert "--timeout 900" in rt.pytest_command(cfg, [])[-1]


def test_no_timeout_flag_when_uncapped():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert "--timeout" not in rt.pytest_command(cfg, [])[-1]


def test_bootstrap_times_out_as_setup_failure_not_verdict(tmp_path, monkeypatch, capsys):
    """Remote-side expiry must read as BOOTSTRAP_FAILED (local fallback),
    never as a test result — and it kills pytest as a direct child."""
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))

    def expire(*args, **kwargs):
        assert kwargs.get("timeout") == 900
        raise rt.subprocess.TimeoutExpired(cmd="pytest", timeout=900)

    monkeypatch.setattr(rt.subprocess, "run", expire)
    code = rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python"), timeout=900)
    assert code == rt.BOOTSTRAP_FAILED
    assert "exceeded 900s" in capsys.readouterr().err


def test_run_passes_no_timeout_when_uncapped(monkeypatch):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    seen = {}

    def record(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return _Exit(0)

    monkeypatch.setattr(rt.subprocess, "run", record)
    assert rt.run(["-q"], env=FULL_ENV) == 0
    assert seen["timeout"] is None


# ── Sync retry ─────────────────────────────────────────────────────────


def test_sync_is_retried_once_before_falling_back(monkeypatch):
    """One transient blip must not cost a 10x slower local run."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    attempts = []
    monkeypatch.setattr(rt, "sync", lambda cfg: attempts.append(1) or len(attempts) > 1)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))

    assert rt.run(["-q"], env=FULL_ENV) == 0
    assert len(attempts) == 2


def test_run_falls_back_when_sync_fails_twice(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    attempts = []
    monkeypatch.setattr(rt, "sync", lambda cfg: attempts.append(1) and False)

    assert rt.run(["-q"], env=FULL_ENV) is None
    assert len(attempts) == 2
    assert "running locally" in capsys.readouterr().err


# ── Remote-side housekeeping and preflight ─────────────────────────────
#
# The remote box exhausted tmp inodes once and the result was hundreds of
# bogus sqlite failures reported as a red suite. bootstrap() runs on the
# remote as plain Python, so it can defend itself: sweep stale pytest tmp,
# GC dead workspaces, and refuse (BOOTSTRAP_FAILED → clean local fallback)
# when the host has no headroom.


def test_sweep_removes_only_stale_session_dirs(tmp_path):
    base = tmp_path / "pytest-of-ben"
    base.mkdir()
    stale = base / "pytest-1"
    fresh = base / "pytest-2"
    stale.mkdir()
    fresh.mkdir()
    old = 1_000_000.0
    os.utime(stale, (old, old))

    assert sweep_stale_pytest_tmp(base=base, now=old + rt.STALE_TMP_AGE + 1) == 1
    assert not stale.exists() and fresh.exists()


def test_sweep_tolerates_a_missing_base(tmp_path):
    assert sweep_stale_pytest_tmp(base=tmp_path / "absent", now=0.0) == 0


def test_preflight_rejects_low_disk(monkeypatch):
    monkeypatch.setattr(rt, "tmp_headroom", lambda path=None: (1024, 10**6))
    assert "free" in (preflight() or "")


def test_preflight_rejects_low_inodes(monkeypatch):
    monkeypatch.setattr(rt, "tmp_headroom", lambda path=None: (10**12, 10))
    assert "inode" in (preflight() or "")


def test_preflight_accepts_a_healthy_host(monkeypatch):
    monkeypatch.setattr(rt, "tmp_headroom", lambda path=None: (10**12, 10**6))
    assert preflight() is None


def test_preflight_skips_inode_check_where_unavailable(monkeypatch):
    """Windows has no statvfs; a None inode count must not read as 'low'."""
    monkeypatch.setattr(rt, "tmp_headroom", lambda path=None: (10**12, None))
    assert preflight() is None


def test_headroom_treats_dynamic_inode_filesystems_as_not_applicable(monkeypatch, tmp_path):
    """btrfs reports f_files=0/f_favail=0 — that's 'inodes don't apply here',
    not 'zero free', which would permanently fail the preflight."""
    class _Vfs:
        f_files = 0
        f_favail = 0

    class _Usage:
        free = 10**12

    # shutil.disk_usage also calls os.statvfs on Linux, so stub it as well.
    monkeypatch.setattr(rt.shutil, "disk_usage", lambda target: _Usage())
    monkeypatch.setattr(rt.os, "statvfs", lambda target: _Vfs(), raising=False)
    _, inodes = rt.tmp_headroom(tmp_path)
    assert inodes is None


def test_bootstrap_bails_before_pytest_when_preflight_fails(tmp_path, monkeypatch, capsys):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))
    monkeypatch.setattr(rt, "preflight", lambda *a, **k: "only 12 MiB free in tmp")

    ran = []
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: ran.append(a) or None)

    assert rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python")) == rt.BOOTSTRAP_FAILED
    assert ran == [], "an unfit host must not run the suite and report noise"
    assert "12 MiB" in capsys.readouterr().err


def test_gc_removes_only_old_sibling_workspaces(tmp_path):
    mine = tmp_path / "ws-mine"
    dead = tmp_path / "ws-dead"
    fresh = tmp_path / "ws-fresh"
    other = tmp_path / "not-a-workspace"
    for d in (mine, dead, fresh, other):
        d.mkdir()
    old = 1_000_000.0
    os.utime(dead, (old, old))
    os.utime(other, (old, old))

    removed = gc_workspaces(tmp_path, keep=mine, now=old + rt.WORKSPACE_GC_AGE + 1)
    assert removed == ["ws-dead"]
    assert mine.exists() and fresh.exists() and other.exists()


def test_bootstrap_gc_only_applies_to_workspace_layouts(tmp_path, monkeypatch):
    """In the legacy layout the run dir IS the checkout — never GC its siblings."""
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))
    called = []
    monkeypatch.setattr(rt, "gc_workspaces", lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))

    rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python"))
    assert called == [], "root has no ws- prefix, so no sibling GC"

    ws = tmp_path / "ws-abc"
    ws.mkdir()
    _write_locks(ws)
    rt.bootstrap(["-q"], root=ws, python=str(tmp_path / "python"))
    assert called == [1]


# ── RAM-disk temp (REMOTE_TEST_TMP) ────────────────────────────────────
# The suite's temp traffic (a 3 MB template copy plus WAL churn per DB-backed
# test, ~16k tests) saturated the runner's SSD while its cores idled. The
# bootstrap can point the pytest child at a RAM disk — but that disk may not
# survive a reboot, so every failure to use it costs one log line, never the
# gate.


def test_tmp_defaults_to_the_os_temp():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None and cfg.tmp == ""


def test_tmp_parsed_from_env():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_TMP": "T:/dk-tmp"})
    assert cfg is not None and cfg.tmp == "T:/dk-tmp"


def test_tmp_with_unsafe_characters_is_rejected():
    """It rides the same cmd.exe line as the targets: backslashes and spaces
    would need quoting nobody has portably, so they are refused up front."""
    with pytest.raises(rt.UnsafeArgument):
        rt.load_config({**FULL_ENV, "REMOTE_TEST_TMP": "T:\\dk tmp"})


def test_tmp_ships_to_the_remote_bootstrap():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_TMP": "T:/dk-tmp"})
    assert cfg is not None
    assert "--tmp T:/dk-tmp " in rt.pytest_command(cfg, [])[-1]


def test_no_tmp_flag_when_unset():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert "--tmp" not in rt.pytest_command(cfg, [])[-1]


def test_choose_tmp_is_none_when_unset():
    assert rt.choose_tmp("") is None


def test_choose_tmp_creates_and_returns_a_usable_dir(tmp_path, capsys):
    ram = tmp_path / "ram"
    assert rt.choose_tmp(str(ram)) == ram
    assert ram.is_dir()
    assert rt.choose_tmp(str(ram)) == ram, "an existing dir is reused, not an error"
    assert capsys.readouterr().out == ""


def test_choose_tmp_falls_back_when_the_drive_is_missing(tmp_path, capsys):
    """A RAM disk that did not come back after a reboot: one line, OS default.

    Only the leaf is created, so a path whose parent is gone fails rather
    than being built — the real T:/dk-tmp exists on the runner, so the
    missing-drive case is a parent this test knows does not exist.
    """
    gone = tmp_path / "no-such-drive" / "dk-tmp"
    assert rt.choose_tmp(str(gone)) is None
    assert not gone.parent.exists()
    out = capsys.readouterr().out
    assert f"{gone} unavailable" in out and "OS default" in out


def test_pytest_env_overrides_every_temp_variable(tmp_path):
    env = rt.pytest_env(tmp_path)
    assert {env["TMPDIR"], env["TEMP"], env["TMP"]} == {str(tmp_path)}


def test_pytest_env_is_the_parent_environment_when_unset(monkeypatch):
    monkeypatch.setenv("TEMP", "C:/somewhere")
    assert rt.pytest_env(None) == dict(os.environ)


def _run_bootstrap_capturing_env(tmp_path, monkeypatch, tmp):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))
    seen = {}

    def record(*args, **kwargs):
        seen["env"] = kwargs.get("env")
        return _Exit(0)

    monkeypatch.setattr(rt.subprocess, "run", record)
    code = rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python"), tmp=tmp)
    return code, seen["env"]


def test_bootstrap_runs_pytest_with_its_temp_on_the_chosen_dir(tmp_path, monkeypatch):
    ram = tmp_path / "ram"
    code, env = _run_bootstrap_capturing_env(tmp_path, monkeypatch, str(ram))
    assert code == 0
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(ram)


def test_bootstrap_still_runs_when_the_tmp_drive_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", "C:/default")
    gone = str(tmp_path / "no-such-drive" / "dk-tmp")
    code, env = _run_bootstrap_capturing_env(tmp_path, monkeypatch, gone)
    assert code == 0, "a missing RAM disk is a slower run, not a failed one"
    assert env["TEMP"] == "C:/default"


def test_bootstrap_falls_back_to_the_default_temp_when_the_chosen_one_is_unfit(
    tmp_path, monkeypatch
):
    """A full RAM disk must not become a local run: the SSD temp is still fine."""
    ram = tmp_path / "ram"
    monkeypatch.setenv("TEMP", "C:/default")
    monkeypatch.setattr(
        rt, "preflight",
        lambda path=None: "only 12 MiB free in tmp" if path is not None else None,
    )
    code, env = _run_bootstrap_capturing_env(tmp_path, monkeypatch, str(ram))
    assert code == 0
    assert env["TEMP"] == "C:/default"


def test_bootstrap_sweeps_both_the_default_and_the_chosen_temp(tmp_path, monkeypatch):
    ram = tmp_path / "ram"
    swept = []
    monkeypatch.setattr(rt, "sweep_stale_tmp", lambda roots, now=None: swept.extend(roots) or 0)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))
    rt.bootstrap(["-q"], root=tmp_path, python=str(tmp_path / "python"), tmp=str(ram))
    assert swept == [Path(rt.tempfile.gettempdir()), ram]


def test_sweep_removes_only_stale_template_dirs(tmp_path):
    """The per-worker template (tests/db_template.py) is removed by atexit,
    which a killed worker never reaches; 225 of them had piled up."""
    stale = tmp_path / "dk-db-template-abc123"
    fresh = tmp_path / "dk-db-template-def456"
    other = tmp_path / "unrelated-old-dir"
    for d in (stale, fresh, other):
        d.mkdir()
        (d / "template.db").write_bytes(b"x")
    old = 1_000_000.0
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    assert sweep_stale_template_dbs(base=tmp_path, now=old + rt.STALE_TMP_AGE + 1) == 1
    assert not stale.exists() and fresh.exists() and other.exists()


def test_sweep_template_dirs_tolerates_a_missing_base(tmp_path):
    assert sweep_stale_template_dbs(base=tmp_path / "absent", now=0.0) == 0


def test_sweep_stale_tmp_covers_both_kinds_in_every_root(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.getpass, "getuser", lambda: "ben")
    old = 1_000_000.0
    roots = [tmp_path / "ssd", tmp_path / "ram"]
    for root in roots:
        session = root / "pytest-of-ben" / "pytest-1"
        template = root / "dk-db-template-x"
        session.mkdir(parents=True)
        template.mkdir()
        os.utime(session, (old, old))
        os.utime(template, (old, old))

    assert sweep_stale_tmp(roots, now=old + rt.STALE_TMP_AGE + 1) == 4
    for root in roots:
        assert list(root.glob("dk-db-template-*")) == []
        assert list((root / "pytest-of-ben").iterdir()) == []


def test_sweep_stale_tmp_still_clears_templates_without_a_resolvable_user(tmp_path, monkeypatch):
    """Template dirs are not keyed by user, so a failed getuser() must not skip them."""

    def no_user():
        raise KeyError("no passwd entry")

    monkeypatch.setattr(rt.getpass, "getuser", no_user)
    old = 1_000_000.0
    template = tmp_path / "dk-db-template-x"
    template.mkdir()
    os.utime(template, (old, old))
    assert sweep_stale_tmp([tmp_path], now=old + rt.STALE_TMP_AGE + 1) == 1
    assert not template.exists()


def test_bootstrap_skips_the_default_temp_preflight_when_the_chosen_dir_is_used(tmp_path, monkeypatch):
    """A full SSD temp is irrelevant while the suite writes to the RAM disk."""
    seen = []

    def fake_preflight(path=None):
        seen.append(path)
        return "only 12 MiB free in tmp" if path is None else None

    monkeypatch.setattr(rt, "preflight", fake_preflight)
    code, _env = _run_bootstrap_capturing_env(tmp_path, monkeypatch, str(tmp_path / "ram"))
    assert code == 0
    assert seen == [tmp_path / "ram"]
# ── runner semaphore ───────────────────────────────────────────────────────────


def _slots(tmp_path):
    return tmp_path / "slots"


def _hold(tmp_path, pid, age=0.0):
    """Plant a slot file for *pid*, claimed *age* seconds ago."""
    d = _slots(tmp_path)
    d.mkdir(exist_ok=True)
    path = d / str(pid)
    path.touch()
    if age:
        then = rt._wall() - age
        os.utime(path, (then, then))
    return path


def _cfg(**over):
    cfg = rt.load_config({**FULL_ENV, **over})
    assert cfg is not None
    return cfg


def test_max_runs_and_wait_default():
    cfg = _cfg()
    assert (cfg.max_runs, cfg.max_wait) == (rt.DEFAULT_MAX_RUNS, rt.DEFAULT_MAX_WAIT)
    assert rt.DEFAULT_MAX_RUNS == 3, "three 8-worker runs saturate the 24-CPU runner"


def test_max_runs_and_wait_parsed_from_env():
    cfg = _cfg(REMOTE_TEST_MAX_RUNS="2", REMOTE_TEST_MAX_WAIT="0")
    assert (cfg.max_runs, cfg.max_wait) == (2, 0)


@pytest.mark.parametrize("key", ["REMOTE_TEST_MAX_RUNS", "REMOTE_TEST_MAX_WAIT"])
@pytest.mark.parametrize("bad", ["abc", "1.5", "-1"])
def test_invalid_semaphore_dials_rejected(key, bad):
    with pytest.raises(ValueError, match=key):
        rt.load_config({**FULL_ENV, key: bad})


def test_slot_dir_is_in_the_git_common_dir(monkeypatch, tmp_path):
    """Every worktree of the prod checkout resolves the same common dir, so a
    slot claimed from any session is visible to all of them."""
    monkeypatch.setattr(rt, "_git", lambda root, *a: str(tmp_path / ".git") if "--git-common-dir" in a else None)
    assert slot_dir(tmp_path) == tmp_path / ".git" / rt.SLOT_DIR_NAME


def test_slot_dir_is_none_outside_git(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "_git", lambda root, *a: None)
    assert slot_dir(tmp_path) is None


def test_pid_alive_for_this_process_and_not_for_a_dead_one(monkeypatch):
    if rt.sys.platform == "win32":
        pytest.skip("the semaphore only runs on the gating (Linux) side")
    assert rt.pid_alive(os.getpid())
    def lookup_fails(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(rt.os, "kill", lookup_fails)
    assert not rt.pid_alive(4_000_000)


def test_pid_alive_treats_someone_elses_process_as_alive(monkeypatch):
    def denied(pid, sig):
        raise PermissionError
    monkeypatch.setattr(rt.os, "kill", denied)
    assert rt.pid_alive(1)


def test_slot_max_age_tracks_the_remote_cap():
    assert rt.slot_max_age(_cfg(REMOTE_TEST_TIMEOUT="2400")) == 2400 + 60 + rt.SLOT_AGE_GRACE
    assert rt.slot_max_age(_cfg()) == 0, "uncapped ⇒ only the PID check reaps"


def test_live_slots_reaps_dead_pids_and_keeps_live_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "pid_alive", lambda pid: pid != 222)
    _hold(tmp_path, 111, age=10)
    dead = _hold(tmp_path, 222, age=5)
    (_slots(tmp_path) / "README").write_text("not a slot", encoding="utf-8")
    assert [pid for _, pid in rt.live_slots(_slots(tmp_path))] == [111]
    assert not dead.exists()
    assert (_slots(tmp_path) / "README").exists(), "only PID-named files are touched"


def test_live_slots_reaps_by_age_even_when_the_pid_looks_alive(tmp_path, monkeypatch):
    """PIDs come back after a reboot; a slot older than any run can last is dead."""
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    old = _hold(tmp_path, 333, age=5000)
    _hold(tmp_path, 444, age=100)
    assert [pid for _, pid in rt.live_slots(_slots(tmp_path), max_age=3000)] == [444]
    assert not old.exists()


def test_live_slots_is_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    _hold(tmp_path, 10, age=1)
    _hold(tmp_path, 20, age=50)
    assert [pid for _, pid in rt.live_slots(_slots(tmp_path))] == [20, 10]


def test_live_slots_tolerates_a_missing_directory(tmp_path):
    assert rt.live_slots(tmp_path / "nowhere") == []


def test_run_holds_a_slot_for_the_run_and_releases_it(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    seen = {}

    def during_run(*a, **k):
        seen["held"] = sorted(p.name for p in _slots(tmp_path).iterdir())
        return _Exit(0)

    monkeypatch.setattr(rt.subprocess, "run", during_run)
    assert rt.run(["-q"], env=FULL_ENV) == 0
    assert seen["held"] == [str(os.getpid())]
    assert list(_slots(tmp_path).iterdir()) == [], "released after the run"


def test_run_releases_the_slot_when_the_run_raises(monkeypatch, tmp_path):
    """Ctrl-C on a commit must not hold a slot until the age reaper gets it."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(rt.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rt.run(["-q"], env=FULL_ENV)
    assert list(_slots(tmp_path).iterdir()) == []


def test_run_releases_the_slot_when_sync_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: False)
    assert rt.run(["-q"], env=FULL_ENV) is None
    assert list(_slots(tmp_path).iterdir()) == []


def test_run_waits_for_a_slot_then_dispatches(monkeypatch, tmp_path, capsys, _hermetic_slots):
    """Fourth run in: waits while three hold, gets the slot when one leaves."""
    clock = _hermetic_slots
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    holders = [_hold(tmp_path, pid, age=60) for pid in (1, 2, 3)]

    def leave_after_two_polls(seconds):
        clock.t += seconds
        if len(clock.slept) == 1:
            holders[0].unlink()
        clock.slept.append(seconds)

    monkeypatch.setattr(rt, "_sleep", leave_after_two_polls)

    assert rt.run(["-q"], env=FULL_ENV) == 0
    out = capsys.readouterr().out
    assert "running 3 of 3" in out and "waiting up to 600s" in out
    assert "slot free after" in out
    assert clock.slept[:2] == [rt.SLOT_POLL_SECONDS] * 2
    assert sorted(p.name for p in _slots(tmp_path).iterdir()) == ["2", "3"]


def test_run_falls_back_locally_when_the_wait_expires(monkeypatch, tmp_path, capsys, _hermetic_slots):
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: pytest.fail("must not sync without a slot"))
    for pid in (1, 2, 3):
        _hold(tmp_path, pid)

    assert rt.run(["-q"], env={**FULL_ENV, "REMOTE_TEST_MAX_WAIT": "60"}) is None
    captured = capsys.readouterr()
    assert "still full after 60s — running locally" in captured.err
    assert sum(_hermetic_slots.slept) == 60, "waits exactly the ceiling, no more"
    assert "still waiting" in captured.out, "progress is reported during a long wait"
    assert sorted(p.name for p in _slots(tmp_path).iterdir()) == ["1", "2", "3"]


def test_zero_wait_goes_local_at_once_when_full(monkeypatch, tmp_path, capsys, _hermetic_slots):
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    for pid in (1, 2, 3):
        _hold(tmp_path, pid)
    assert rt.run(["-q"], env={**FULL_ENV, "REMOTE_TEST_MAX_WAIT": "0"}) is None
    assert _hermetic_slots.slept == []
    assert "running locally" in capsys.readouterr().err


def test_zero_max_runs_means_no_counting(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    for pid in range(1, 10):
        _hold(tmp_path, pid)
    assert rt.run(["-q"], env={**FULL_ENV, "REMOTE_TEST_MAX_RUNS": "0"}) == 0
    assert len(list(_slots(tmp_path).iterdir())) == 9, "no slot claimed, none reaped"


def test_no_git_common_dir_dispatches_uncounted(monkeypatch, tmp_path):
    """Outside a git checkout there is nowhere shared to count; the old
    (unbounded) behaviour is the honest fallback, not a refusal."""
    monkeypatch.setattr(rt, "slot_dir", lambda root=None: None)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    assert rt.run(["-q"], env=FULL_ENV) == 0


def test_a_dead_holder_is_reaped_on_the_way_in(monkeypatch, tmp_path):
    """A gate killed mid-run left its slot; the next run takes it."""
    monkeypatch.setattr(rt, "pid_alive", lambda pid: pid != 2)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    for pid in (1, 2, 3):
        _hold(tmp_path, pid)
    assert rt.run(["-q"], env=FULL_ENV) == 0
    assert sorted(p.name for p in _slots(tmp_path).iterdir()) == ["1", "3"]


def test_an_over_age_holder_is_reaped_on_the_way_in(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    _hold(tmp_path, 1, age=900 + 60 + rt.SLOT_AGE_GRACE + 1)
    _hold(tmp_path, 2)
    _hold(tmp_path, 3)
    assert rt.run(["-q"], env={**FULL_ENV, "REMOTE_TEST_TIMEOUT": "900"}) == 0
    assert sorted(p.name for p in _slots(tmp_path).iterdir()) == ["2", "3"]


def test_a_stale_slot_under_our_own_pid_is_replaced(monkeypatch, tmp_path):
    """PID reuse: a dead predecessor's slot bears our number; we own it now."""
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    _hold(tmp_path, os.getpid(), age=100)
    assert rt.run(["-q"], env=FULL_ENV) == 0
    assert list(_slots(tmp_path).iterdir()) == []


def test_a_simultaneous_claim_that_ranks_past_the_ceiling_withdraws(monkeypatch, tmp_path, _hermetic_slots):
    """Two gates count N-1 and both claim: the later claim backs off and retries."""
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    _hold(tmp_path, 1, age=30)
    _hold(tmp_path, 2, age=30)
    rival = {"planted": False}
    real_claim = rt._claim

    def claim_with_a_rival(path):
        ok = real_claim(path)
        if not rival["planted"]:
            # Someone else claimed a hair earlier: their file is older.
            _hold(tmp_path, 3, age=1)
            rival["planted"] = True
        return ok

    monkeypatch.setattr(rt, "_claim", claim_with_a_rival)
    monkeypatch.setattr(rt.random, "random", lambda: 0.0)

    cfg = _cfg(REMOTE_TEST_MAX_WAIT="60")
    # Rival leaves on the retry's count.
    real_live = rt.live_slots
    counts = []

    def live_then_rival_leaves(directory, max_age=0):
        counts.append(1)
        if len(counts) == 3:
            (_slots(tmp_path) / "3").unlink()
        return real_live(directory, max_age)

    monkeypatch.setattr(rt, "live_slots", live_then_rival_leaves)
    held = rt.wait_for_slot(cfg, _slots(tmp_path), "t")
    assert held is not None and held.name == str(os.getpid())
    # Only a withdrawn claim takes the short jittered pause; a full holder
    # count would have polled for SLOT_POLL_SECONDS instead.
    assert _hermetic_slots.slept == [0.5]
    assert sorted(p.name for p in _slots(tmp_path).iterdir()) == sorted(["1", "2", str(os.getpid())])


def test_exec_remote_takes_a_slot_too(monkeypatch, tmp_path, capsys):
    """A sim run is the same load as a gate and counts the same way."""
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: pytest.fail("must not sync without a slot"))
    for pid in (1, 2, 3):
        _hold(tmp_path, pid)
    env = {**FULL_ENV, "REMOTE_TEST_MAX_WAIT": "0"}
    assert rt.exec_remote("python -V", env=env) is None
    captured = capsys.readouterr()
    assert "still full" in captured.err and captured.out == "", "stdout stays clean for piping"


def test_exec_remote_releases_its_slot(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))
    assert rt.exec_remote("python -V", env=FULL_ENV) == 0
    assert list(_slots(tmp_path).iterdir()) == []


def test_unwritable_slot_dir_dispatches_uncounted(monkeypatch, tmp_path, capsys):
    """A slot directory we cannot use must never fail the gate."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Exit(0))

    def denied(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(rt, "wait_for_slot", denied)
    assert rt.run(["-q"], env=FULL_ENV) == 0
    assert "cannot count runs" in capsys.readouterr().out


def test_slot_max_age_follows_the_callers_own_cap():
    cfg = _cfg(REMOTE_TEST_TIMEOUT="600")
    assert rt.slot_max_age(cfg, cap=0) == 0, "an uncapped sim run is never reaped on age"
    assert rt.slot_max_age(cfg, cap=7200) == 7200 + 60 + rt.SLOT_AGE_GRACE


def test_live_slots_ignores_non_ascii_and_absurd_pid_names(tmp_path):
    d = _slots(tmp_path)
    d.mkdir()
    (d / "²").touch()
    huge = d / ("9" * 30)
    huge.touch()
    assert rt.live_slots(d) == []
    assert (d / "²").exists(), "non-PID names are ignored, never deleted"
    # Out of PID range is a stray file too, decided before any liveness probe —
    # so the answer is the same on the Windows runner, where pid_alive never
    # says "dead".
    assert huge.exists()


def test_a_lost_claim_cannot_outrun_the_wait_ceiling(monkeypatch, tmp_path, _hermetic_slots):
    """Withdraw-and-retry checks the deadline, so max_wait 0 never loops."""
    monkeypatch.setattr(rt, "pid_alive", lambda pid: True)
    _hold(tmp_path, 1, age=30)
    _hold(tmp_path, 2, age=30)
    real_claim = rt._claim

    def claim_with_a_rival(path):
        ok = real_claim(path)
        _hold(tmp_path, 3, age=1)  # an older rival appears every time
        return ok

    monkeypatch.setattr(rt, "_claim", claim_with_a_rival)
    assert rt.wait_for_slot(_cfg(REMOTE_TEST_MAX_WAIT="0"), _slots(tmp_path), "t") is None
