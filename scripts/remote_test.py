"""Dispatch pytest to a faster machine over SSH, falling back to local silently.

The prod box is a 2-core VM; a spare desktop runs the suite several times
faster. This ships the source over and runs pytest there, but **never blocks
work when that machine is off** — if the probe fails, the caller runs locally
as if this module didn't exist.

Transport-agnostic on purpose: the remote may be native Windows (OpenSSH
Server), WSL2, or plain Linux. Everything goes through one `ssh host "<cmd>"`
invocation and a tar pipe, so there is no rsync dependency (rsync is not
present on native Windows) and no assumption about the remote shell beyond
`cd X && Y`, which cmd.exe, PowerShell, and bash all accept.

Configuration comes from the process environment, falling back to the
checkout's `.env` (which gate.py does not otherwise load). Absent
`REMOTE_TEST_HOST`, this module is inert.

    REMOTE_TEST_HOST      user@host of the test runner. Unset ⇒ always local.
    REMOTE_TEST_DIR       Path to the repo checkout on that host.
    REMOTE_TEST_PYTHON    Python to run there (e.g. .venv/Scripts/python.exe).
    REMOTE_TEST_JOBS      xdist workers (default 12 — leaves a desktop usable).
    REMOTE_TEST_LOCK      Lock file the remote installs from, when it needs its
                          own (default requirements/dev.lock).
    REMOTE_TEST_CD        Override the `cd` template if the remote shell needs
                          it (e.g. "cd /d {dir} && {cmd}" for a cmd.exe remote
                          on a different drive letter).
    REMOTE_TEST_TIMEOUT   Wall-clock cap in seconds on the remote pytest run,
                          enforced on the remote (bootstrap kills its pytest
                          child and reports a setup failure ⇒ local fallback,
                          no orphan); the local side allows 60s of grace on
                          top as an outer belt. 0 or unset ⇒ no cap
                          (keepalives still catch a dead connection ~60s).
    REMOTE_TEST_WORKSPACE Sub-directory of REMOTE_TEST_DIR to sync into (default
                          derived per checkout, so parallel checkouts don't
                          collide). Set to "off" for the legacy layout: sync
                          straight over REMOTE_TEST_DIR, and don't prune.
    REMOTE_TEST_TMP       Directory on the remote for the suite's temp files
                          (e.g. "T:/dk-tmp" on a RAM disk). Shipped to the
                          bootstrap as --tmp; used only if it can be created
                          there, else the OS default temp with one log line.
                          Unset ⇒ the OS default.
    REMOTE_TEST_MAX_RUNS  How many runs may use the host at once, across every
                          checkout gating on this machine (default 3). A run
                          past that waits for a slot. 0 ⇒ no limit.
    REMOTE_TEST_MAX_WAIT  Longest wait for a slot, in seconds (default 600).
                          Expiry runs locally, so a busy host slows a commit
                          but never blocks it. 0 ⇒ never wait: go local at
                          once when the host is full.
    GATE_NO_REMOTE=1      Force local for one run.

The real environment takes precedence over `.env`, so a one-off
`GATE_NO_REMOTE=1 git commit ...` works without editing the file.

The remote needs only a git clone — the suite reads no .env, no database, and
no model files, so nothing secret is ever synced.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

# Compiled pins. Shipped so the remote can detect when its venv is stale, and
# reinstall from the same file this side is using.
LOCK_FILES = ("requirements/base.lock", "requirements/dev.lock")

# Only these are shipped. Everything else (the DB, models/, backups/, .venv,
# .git) is either irrelevant to the suite or far too large to sync per run.
# README.md is included even though it isn't code: it is tiny, and prose
# tripwires have historically read it (the party-game count lived there until
# 2026-09-03, when it moved to docs/features.md). A remote whose only copy
# dates back to its initial `git clone` drifts silently, since nothing else
# re-syncs it.
# docs/ and assets/fonts/ are test inputs, not documentation niceties: the
# party-game tripwire now reads docs/features.md, the
# quote renderer refuses to run without its bundled fonts (the QA-card
# chunker also read the testing checklists until they were retired on
# 2026-10-01) — a workspace without them fails tests that pass everywhere else (found the first time a full suite ran in the
# workspace layout; the legacy layout hid it because git clone had the full
# tree). assets/ beyond fonts/ (logos, an 8MB gif) stays unsynced.
SYNC_PATHS = (
    "src", "tests", "scripts", "docs", "assets/fonts",
    "pyproject.toml", "README.md", *LOCK_FILES,
)

# Records which lock hash the remote venv was last installed from. Kept beside
# the venv rather than in a workspace: workspaces come and go per checkout, and
# re-running a multi-GB pip install for each one would defeat the point.
STAMP_FILE = ".remote-test-stamp"

# Shipped inside each workspace; lists every file the sync sent, so the remote
# can prune whatever it still has that we did not.
MANIFEST_FILE = ".remote-manifest"

# Exit code meaning "the remote could not prepare itself — run locally
# instead". Chosen outside pytest's 0-5 range so it can never collide with a
# genuine test result, which must keep failing the gate.
BOOTSTRAP_FAILED = 97

# pytest's documented exit codes. Anything else coming back from the ssh
# invocation is the transport failing (255 = connection lost, 128+N/-N =
# signal-killed), which must fall back locally rather than fail the gate.
PYTEST_EXIT_CODES = range(6)

# Remote-side housekeeping thresholds. The remote box once filled its tmp with
# leftover pytest session dirs and the suite sprayed hundreds of bogus sqlite
# errors that read as a red run — these keep that failure mode from ever
# blocking a commit again.
STALE_TMP_AGE = 2 * 3600        # pytest tmp sessions older than this are dead
WORKSPACE_GC_AGE = 30 * 86400   # workspaces untouched this long belong to dead checkouts
# Safety margins, not budgets: since the template-copy fixtures (tests/
# db_template.py) a run's tmp footprint stays small, so a host under these
# floors is already in trouble for other reasons.
MIN_FREE_BYTES = 2 * 1024**3
MIN_FREE_INODES = 100_000       # ~4 inodes per DB-backed test worst case

# Prefix for per-checkout workspace directories. Spelled once: workspace_slug
# mints names with it, gc_workspaces matches on it, and bootstrap uses it to
# recognize that its cwd IS a workspace (the config never ships over ssh, so
# the directory name is the only layout signal the remote has).
WORKSPACE_PREFIX = "ws-"

# Characters that would change meaning inside the single remote command string.
# Rather than guess at cmd.exe vs POSIX quoting, refuse to build a command we
# cannot faithfully represent — a loud failure beats a silently wrong test run.
_UNSAFE = set(' \t\n"\'\\|&;<>()$`')

# A Windows remote runs the command through cmd.exe, which refuses a line over
# 8191 characters ("The command line is too long"). A diff touching hundreds of
# files maps onto hundreds of test paths, so past this length the listed test
# files collapse to the whole tests/ directory: a superset, so the gate still
# covers every test it selected.
MAX_TARGET_CHARS = 6000
_PATH_OPTIONS = frozenset({"--deselect", "--ignore", "--ignore-glob", "--rootdir", "--confcutdir", "-c"})

DEFAULT_JOBS = 12
DEFAULT_CD = "cd {dir} && {cmd}"

# Cross-checkout semaphore on the test host. Every session worktree on this
# machine reads the same .env, so every gate dispatches to the same box, and
# nothing else bounds how many run at once: on 2026-09-24 two concurrent runs
# stalled at 98% and stranded 58 python.exe there for six days. Each xdist
# worker is its own ~300 MB process, so three 8-worker runs already saturate
# the 24-CPU runner — hence the default. The slot directory lives in the git
# common dir, which every worktree of the prod checkout shares (the ship flow
# already keeps its flock there), rather than /tmp, whose shared state has
# bitten the browser gate before (docs/web_testing.md).
DEFAULT_MAX_RUNS = 3
DEFAULT_MAX_WAIT = 600
SLOT_DIR_NAME = "remote-test-slots"
SLOT_POLL_SECONDS = 5
SLOT_PROGRESS_EVERY = 30
# Largest name that can be a slot: above this it is a stray file, and on
# Linux os.kill would raise OverflowError rather than probe it.
MAX_PID = 2**31 - 1
# Extra seconds past the remote cap (+ the local grace) before a slot counts
# as abandoned on age alone: sync, a sync retry and the ssh teardown all
# happen inside the slot but outside the cap.
SLOT_AGE_GRACE = 300

# Which compiled pins the remote installs from. Overridable because a host may
# need its own: the Windows runner requires onnxruntime 1.27, while the Linux
# lock pins 1.26 (no cp314 Windows wheel for it).
DEFAULT_LOCK = "requirements/dev.lock"

# The locks lived at the repo root until 2026-09-28. The production checkout's
# .env still says REMOTE_TEST_LOCK=requirements-dev.lock, and that file is no
# longer synced, so without this map the remote pip install would fail and
# every gate would quietly fall back to a local run. Delete an entry once no
# .env names it.
LEGACY_LOCK_PATHS = {
    "requirements-dev.lock": "requirements/dev.lock",
    "requirements.lock": "requirements/base.lock",
}


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    directory: str
    python: str
    jobs: int = DEFAULT_JOBS
    cd_template: str = DEFAULT_CD
    lock: str = DEFAULT_LOCK
    # Sub-directory of `directory` this checkout syncs into. Empty means the
    # legacy behaviour of syncing straight over the remote checkout.
    workspace: str = ""
    # Wall-clock cap in seconds for the whole remote run; 0 means uncapped.
    timeout: int = 0
    # Where the suite's temp files go on the remote; empty means the OS default.
    tmp: str = ""
    # Concurrent runs the host accepts across all checkouts; 0 means unlimited.
    max_runs: int = DEFAULT_MAX_RUNS
    # Longest wait for a slot before running locally; 0 means never wait.
    max_wait: int = DEFAULT_MAX_WAIT

    @property
    def run_dir(self) -> str:
        """Where pytest runs on the remote."""
        return f"{self.directory}/{self.workspace}" if self.workspace else self.directory


def workspace_slug(root: Path = ROOT) -> str:
    """A stable, filesystem-safe directory name for one local checkout.

    Several session checkouts share one test host, and they must not extract
    over each other: two concurrent runs sharing a directory interleave their
    writes and both report nonsense. Keyed on the absolute path so the same
    checkout reuses its workspace (and so its venv-independent state survives),
    but two checkouts — even two with the same basename — never collide.
    """
    resolved = root.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:10]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in resolved.name)[:24]
    return f"{WORKSPACE_PREFIX}{safe or 'checkout'}-{digest}"


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command in root, or None if it can't be run."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def env_path(root: Path = ROOT) -> Path | None:
    """Locate the .env holding this config, or None.

    Prefers the current checkout. Two fallbacks, because .env is gitignored and
    so never travels with the code:

    1. **Worktrees** (`git rev-parse --git-common-dir`) — the documented edit
       workflow, and a worktree has no .env of its own.
    2. **Local clones** (`git config remote.origin.url`) — session checkouts are
       cloned from the main checkout rather than added as worktrees, and for
       those `--git-common-dir` resolves back to the clone itself, so step 1
       finds nothing. Only a filesystem path is followed; a real URL is ignored.

    Without these, dispatch silently never fires and every run falls back to the
    slow local box — the failure mode is a quiet 10x slowdown, not an error.
    """
    local = root / ".env"
    if local.exists():
        return local

    common_dir = _git(root, "rev-parse", "--git-common-dir")
    if common_dir is None:
        return None
    # --git-common-dir points at the main checkout's .git; its parent is the
    # working tree that owns the .env. In a plain clone it is this checkout's
    # own .git, so this resolves back to `local` and finds nothing.
    candidate = (root / common_dir).resolve().parent / ".env"
    if candidate.exists():
        return candidate

    origin = _git(root, "config", "--get", "remote.origin.url")
    if not origin:
        return None
    # Filesystem paths only: "user@host:repo" and "https://…" are remote and
    # have no .env we could read.
    if "://" in origin or (":" in origin and not Path(origin).is_absolute()):
        return None
    candidate = Path(origin).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    # origin may point at the bare .git or at the working tree above it.
    for base in (candidate, candidate.parent):
        if base.name == ".git":
            continue
        env = base / ".env"
        if env.exists():
            return env
    return None


def env_file_values(root: Path = ROOT) -> dict[str, str]:
    """Read REMOTE_TEST_* / GATE_NO_REMOTE settings out of the checkout's .env.

    gate.py runs from a plain shell — often the pre-commit hook's, under a bare
    system Python — so it never sees the bot's dotenv-loaded config. Parsing the
    file directly (same approach as scripts/post_testing_docs.py) keeps this
    config beside every other setting instead of in a shell profile, and .env is
    gitignored so host-specific paths never reach the repo.

    Deliberately no python-dotenv import: it may not exist in the interpreter
    running the hook.
    """
    path = env_path(root)
    if path is None:
        return {}

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if not (key.startswith("REMOTE_TEST_") or key == "GATE_NO_REMOTE"):
            continue

        # A quoted value keeps everything inside the quotes (a '#' there is
        # data); an unquoted one is truncated at an inline comment.
        value = raw.strip()
        if value[:1] in ("'", '"'):
            closing = value.find(value[0], 1)
            value = value[1:closing] if closing > 0 else value[1:]
        else:
            value = value.split("#")[0]
        values[key] = value.strip()
    return values


class UnsafeArgument(ValueError):
    """A pytest argument cannot be safely embedded in the remote command."""


def _resolve_lock(configured: str) -> str:
    """The lock the remote installs from: the configured one, a pre-move name
    mapped to its new home, or the default when nothing is set."""
    name = configured.strip()
    return LEGACY_LOCK_PATHS.get(name, name) or DEFAULT_LOCK


def load_config(env: Mapping[str, str] | None = None) -> RemoteConfig | None:
    """Build a config from the environment, or None when remote is disabled.

    Returns None (meaning "run locally") when the host is unset or when
    GATE_NO_REMOTE is truthy. A host set without DIR/PYTHON is a
    misconfiguration and raises, because silently running locally there would
    hide the fact that dispatch never happened.
    """
    # Real environment wins over .env, so `GATE_NO_REMOTE=1 git commit ...`
    # still forces a local run without editing the file.
    if env is None:
        env = {**env_file_values(), **os.environ}

    if env.get("GATE_NO_REMOTE", "").strip() in ("1", "true", "yes"):
        return None

    host = env.get("REMOTE_TEST_HOST", "").strip()
    if not host:
        return None

    directory = env.get("REMOTE_TEST_DIR", "").strip()
    python = env.get("REMOTE_TEST_PYTHON", "").strip()
    if not directory or not python:
        raise ValueError(
            "REMOTE_TEST_HOST is set but REMOTE_TEST_DIR and/or "
            "REMOTE_TEST_PYTHON are missing — refusing to guess."
        )

    raw_jobs = env.get("REMOTE_TEST_JOBS", "").strip()
    try:
        jobs = int(raw_jobs) if raw_jobs else DEFAULT_JOBS
    except ValueError:
        raise ValueError(f"REMOTE_TEST_JOBS must be an integer, got {raw_jobs!r}") from None
    if jobs < 1:
        raise ValueError(f"REMOTE_TEST_JOBS must be >= 1, got {jobs}")

    timeout = _non_negative_int(env, "REMOTE_TEST_TIMEOUT", 0)
    max_runs = _non_negative_int(env, "REMOTE_TEST_MAX_RUNS", DEFAULT_MAX_RUNS)
    max_wait = _non_negative_int(env, "REMOTE_TEST_MAX_WAIT", DEFAULT_MAX_WAIT)

    workspace = env.get("REMOTE_TEST_WORKSPACE", "").strip()
    if workspace.lower() in ("0", "off", "false", "none"):
        workspace = ""          # opt back into syncing over the checkout itself
    elif not workspace:
        workspace = workspace_slug()
    if workspace:               # empty is legal (legacy layout); "" fails check_args
        check_args([workspace])

    tmp = env.get("REMOTE_TEST_TMP", "").strip()
    if tmp:                     # rides the same command line as the targets
        check_args([tmp])

    return RemoteConfig(
        host=host,
        directory=directory,
        python=python,
        jobs=jobs,
        workspace=workspace,
        cd_template=env.get("REMOTE_TEST_CD", "").strip() or DEFAULT_CD,
        lock=_resolve_lock(env.get("REMOTE_TEST_LOCK", "")),
        timeout=timeout,
        tmp=tmp,
        max_runs=max_runs,
        max_wait=max_wait,
    )


def _non_negative_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse an integer dial (seconds or a count) where 0 is a legal "off" value."""
    raw = env.get(key, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{key} must be a whole number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{key} must be >= 0, got {value}")
    return value


def check_args(args: Sequence[str]) -> None:
    """Raise if any pytest argument would need shell quoting on the remote."""
    for arg in args:
        if not arg or _UNSAFE & set(arg):
            raise UnsafeArgument(
                f"pytest argument {arg!r} contains characters that cannot be "
                "safely passed to the remote shell; re-run with GATE_NO_REMOTE=1"
            )


def probe_command(cfg: RemoteConfig, timeout: int = 3) -> list[str]:
    """An ssh invocation that succeeds iff the host is reachable non-interactively.

    BatchMode stops ssh prompting for a password/passphrase, which would
    otherwise hang a commit hook forever waiting on stdin.
    """
    return [
        "ssh",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        cfg.host,
        "exit",
    ]


def remote_command(cfg: RemoteConfig, command: str, *, base: bool = False) -> list[str]:
    """Wrap a command string so it runs on the remote.

    `base=True` runs in the configured directory (where the venv lives and
    where workspaces are extracted); otherwise it runs in this checkout's
    workspace, which is where the code under test actually is.
    """
    directory = cfg.directory if base else cfg.run_dir
    # Keepalives make a dead connection surface as exit 255 within ~60s
    # instead of hanging the commit hook on a silent TCP session; run() maps
    # that 255 to a local fallback.
    return ["ssh", "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            cfg.host, cfg.cd_template.format(dir=directory, cmd=command)]


def tar_command(paths: Sequence[str] = SYNC_PATHS, prefix: str = "") -> list[str]:
    """Local half of the sync: stream a gzipped tar of the source to stdout.

    `prefix` re-roots every member under that directory, so the remote's plain
    `tar -xzf -` creates the workspace itself. That avoids needing a portable
    mkdir — cmd.exe, PowerShell and bash disagree about `mkdir -p`, and the
    whole transport deliberately assumes nothing beyond `cd X && Y`.
    """
    transform = [f"--transform=s,^,{prefix}/,"] if prefix else []
    return ["tar", "-czf", "-", "--exclude=__pycache__", "--exclude=*.pyc",
            *transform, "-C", str(ROOT), *paths]


def collapse_targets(args: Sequence[str]) -> list[str]:
    """Replace a too-long list of test files with ``tests`` (see MAX_TARGET_CHARS).

    Options and anything outside tests/ are kept as given, in order."""
    args = list(args)
    if len(" ".join(args)) <= MAX_TARGET_CHARS:
        return args
    kept: list[str] = []
    collapsed = False
    for i, arg in enumerate(args):
        # A path that is an option's value (``--deselect tests/a.py::t``) is not
        # a test to run; dropping it would change what the run means.
        if arg.startswith("tests/") and not (i and args[i - 1] in _PATH_OPTIONS):
            collapsed = True
        else:
            kept.append(arg)
    return [*kept, "tests"] if collapsed else args


def pytest_command(cfg: RemoteConfig, args: Sequence[str]) -> list[str]:
    """Full ssh argv that runs pytest on the remote, via the bootstrap hook.

    Invoking this module rather than pytest directly keeps the staleness check
    on the far side, where it is plain Python — reading a file over SSH would
    otherwise mean a shell one-liner, and `cat` vs `type` differ between bash
    and cmd.exe. Nothing here needs quoting beyond the args already validated
    by check_args.
    """
    check_args(args)
    check_args([cfg.lock])
    args = collapse_targets(args)
    # The cap ships to the remote so expiry kills pytest *there* (a direct
    # child of bootstrap) and comes back as BOOTSTRAP_FAILED — no orphan left
    # writing into the shared workspace.
    cap = f"--timeout {cfg.timeout} " if cfg.timeout else ""
    tmp = f"--tmp {cfg.tmp} " if cfg.tmp else ""
    inner = (
        f"{cfg.python} scripts/remote_test.py --bootstrap --lock {cfg.lock} "
        f"{cap}{tmp}-n {cfg.jobs} " + " ".join(args)
    )
    # Runs in the workspace, whose freshly synced scripts/ is the copy that
    # matches the code under test.
    return remote_command(cfg, inner.strip())


# ── remote side (runs on the test host, under --bootstrap) ─────────────────────


def lock_hash(root: Path = ROOT) -> str:
    """Digest of the compiled pins, used to detect a stale remote venv."""
    digest = hashlib.sha256()
    for name in LOCK_FILES:  # fixed order — the hash must be reproducible
        path = root / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()


def stamp_dir(python: str | None = None) -> Path:
    """Where the venv-freshness stamp lives: beside the venv, not in a workspace.

    Workspaces are per-checkout and disposable; the venv is shared and costs
    gigabytes to rebuild. Keying the stamp to the interpreter means every
    workspace using that venv agrees about whether it is current.
    """
    # Deliberately not resolve()d: this always runs on the remote against its
    # own native path, and resolving a Windows path from a Linux test would
    # silently graft the cwd onto the front of it.
    exe = Path(python or sys.executable)
    parent = exe.parent
    # .../.venv/bin/python or ...\.venv\Scripts\python.exe → .../.venv. A system
    # python (/usr/bin/python3) also has a "bin" parent, so only strip when the
    # grandparent actually looks like a venv — otherwise keep the bin dir.
    if parent.name in ("bin", "Scripts") and "venv" in parent.parent.name.lower():
        return parent.parent
    return parent


def read_stamp(root: Path) -> str:
    try:
        return (root / STAMP_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_stamp(root: Path, digest: str) -> None:
    try:
        (root / STAMP_FILE).write_text(digest, encoding="utf-8")
    except OSError:
        # A read-only venv directory just means we re-check next run.
        pass


def prune_to_manifest(root: Path) -> list[str]:
    """Delete files under the synced roots that this sync did not ship.

    tar only adds and overwrites, so without this a file deleted from the
    branch — or left behind by a different branch tested here earlier — keeps
    running as a phantom test. Returns what was removed, for reporting.
    """
    manifest = root / MANIFEST_FILE
    try:
        shipped = {
            line.strip() for line in
            manifest.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    except OSError:
        return []  # Older sender: leave the tree alone rather than guess.

    removed: list[str] = []
    for entry in SYNC_PATHS:
        target = root / entry
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_dir():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in shipped:
                try:
                    path.unlink()
                    removed.append(rel)
                except OSError:
                    pass
    return removed


def _remove_older_than(candidates, age: float, now: float) -> list[str]:
    """rmtree every candidate whose mtime is older than age; return their names.

    Age-gating is the concurrency guard for both callers: anything a live run
    (this checkout's or a sibling's) still owns has a fresh mtime and is never
    touched. Unstattable/undeletable entries are skipped — housekeeping must
    never fail the run it's protecting.
    """
    removed: list[str] = []
    for path in candidates:
        try:
            if now - path.stat().st_mtime > age:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.name)
        except OSError:
            continue
    return removed


def sweep_stale_pytest_tmp(base: Path | None = None, now: float | None = None) -> int:
    """Delete pytest tmp session dirs old enough that no run can still own them.

    tmp_path_retention_count only prunes *previous* sessions at startup, and
    a run that dies (or a box that sleeps mid-run) leaves its whole session
    behind.
    """
    if base is None:
        try:
            base = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"
        except (KeyError, OSError):  # no resolvable user — nothing to sweep
            return 0
    if now is None:
        now = time.time()

    try:
        children = list(base.iterdir())
    except OSError:
        return 0
    return len(_remove_older_than(children, STALE_TMP_AGE, now))


def sweep_stale_template_dbs(base: Path | None = None, now: float | None = None) -> int:
    """Delete dead ``dk-db-template-*`` dirs (tests/db_template.py) under base.

    Each xdist worker builds one template and registers an atexit rmtree for
    it, so a worker that is killed rather than exiting (the timeout cap, a
    lost ssh session, a box that sleeps) leaves a ~3 MB directory behind
    every time. Same age gate as the pytest sessions: a live worker's
    template is at most one run old.
    """
    if base is None:
        base = Path(tempfile.gettempdir())
    if now is None:
        now = time.time()
    try:
        children = list(base.glob("dk-db-template-*"))
    except OSError:
        return 0
    return len(_remove_older_than(children, STALE_TMP_AGE, now))


def sweep_stale_tmp(roots: Sequence[Path], now: float | None = None) -> int:
    """Sweep every temp root the suite may have written to; return the count.

    Both kinds of leftover, under both the OS default temp (where past runs
    wrote) and the configured one (where this run will). Passing the same
    root twice is harmless: the second pass finds nothing.
    """
    if now is None:
        now = time.time()
    try:
        user: str | None = getpass.getuser()
    except (KeyError, OSError):  # no resolvable user — no pytest-of-<user> to find
        user = None
    total = 0
    for root in roots:
        if user is not None:
            total += sweep_stale_pytest_tmp(root / f"pytest-of-{user}", now)
        total += sweep_stale_template_dbs(root, now)  # not keyed by user
    return total


def choose_tmp(configured: str) -> Path | None:
    """The configured temp directory if it can be used, else None.

    Meant for a RAM disk (``REMOTE_TEST_TMP=T:/dk-tmp``), which may not
    survive a reboot: a missing drive must cost one log line, never the
    gate. Only the leaf is created (``parents=False``), so a path whose
    drive is gone fails here instead of being built under the cwd — on a
    POSIX host ``T:/dk-tmp`` is a relative path.
    """
    if not configured:
        return None
    path = Path(configured)
    try:
        path.mkdir(exist_ok=True)
        probe = tempfile.TemporaryFile(dir=path)
        probe.close()
    except OSError as exc:
        print(
            f"remote-test: temp dir {configured} unavailable ({exc}) — using the OS default",
            flush=True,
        )
        return None
    return path


def pytest_env(tmp: Path | None) -> dict[str, str]:
    """The pytest child's environment: the parent's, plus the temp override.

    ``tempfile.gettempdir()`` honours TMPDIR, then TEMP, then TMP on every
    platform, and both pytest's ``tmp_path`` root and the template DBs derive
    from it, so setting all three moves every file the suite writes without
    a ``--basetemp`` (which would defeat pytest's own session retention).
    """
    env = dict(os.environ)
    if tmp is not None:
        env.update(TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
    return env


def tmp_headroom(path: str | Path | None = None) -> tuple[int, int | None]:
    """(free bytes, free inodes) for the tmp filesystem.

    Inodes are None wherever they aren't a meaningful resource: Windows has
    no statvfs, and dynamic-inode filesystems (btrfs — Fedora's default root)
    report f_files == 0, which must not be read as "none free" or the
    preflight would permanently declare the host unfit.
    """
    target = str(path or tempfile.gettempdir())
    free = shutil.disk_usage(target).free
    inodes: int | None = None
    if hasattr(os, "statvfs"):
        st = os.statvfs(target)
        if st.f_files > 0:
            inodes = st.f_favail
    return free, inodes


def preflight(path: str | Path | None = None) -> str | None:
    """Why this host cannot safely run the suite right now, or None if it can.

    A box with no tmp headroom doesn't fail cleanly — it produces hundreds of
    unrelated sqlite errors that read as a red suite. Refusing up front turns
    that into a BOOTSTRAP_FAILED local fallback instead.
    """
    free, inodes = tmp_headroom(path)
    if free < MIN_FREE_BYTES:
        return f"only {free // 2**20} MiB free in tmp (need {MIN_FREE_BYTES // 2**20})"
    if inodes is not None and inodes < MIN_FREE_INODES:
        return f"only {inodes} free inodes in tmp (need {MIN_FREE_INODES})"
    return None


def gc_workspaces(base: Path, keep: Path, now: float | None = None) -> list[str]:
    """Remove sibling workspaces untouched for WORKSPACE_GC_AGE.

    Every session checkout gets its own workspace and nothing ever deleted
    them, so dead checkouts accumulated forever. Each sync refreshes the live
    workspace's mtime, so age is a safe liveness signal.
    """
    if now is None:
        now = time.time()
    candidates = [
        c for c in sorted(base.glob(WORKSPACE_PREFIX + "*"))
        if c != keep and c.is_dir()
    ]
    return _remove_older_than(candidates, WORKSPACE_GC_AGE, now)


def needs_install(root: Path, expected: str) -> bool:
    """True when the remote venv was built from different pins than we shipped."""
    return read_stamp(root) != expected


def install_deps(python: str, root: Path, lock: str = DEFAULT_LOCK) -> int:
    return subprocess.run(
        [python, "-m", "pip", "install", "-r", lock], cwd=root
    ).returncode


def bootstrap(
    args: Sequence[str],
    root: Path | None = None,
    python: str | None = None,
    lock: str = DEFAULT_LOCK,
    timeout: int = 0,
    tmp: str = "",
) -> int:
    """Entry point executed **on the remote**: sync deps, then run pytest.

    Returns BOOTSTRAP_FAILED if the venv could not be brought up to date, so
    the calling side falls back to a local run rather than reporting a
    dependency problem as a test failure.

    ``tmp`` is where the suite's temp files go (``REMOTE_TEST_TMP``); if it
    cannot be used, or has no headroom, the OS default temp is used instead
    — the point of a RAM disk is speed, and a missing one must not turn into
    a local run.
    """
    root = Path.cwd() if root is None else root
    python = sys.executable if python is None else python

    removed = prune_to_manifest(root)
    if removed:
        shown = ", ".join(removed[:5]) + (f" (+{len(removed) - 5} more)" if len(removed) > 5 else "")
        print(f"remote-test: pruned {len(removed)} stale file(s): {shown}", flush=True)

    chosen = choose_tmp(tmp)
    roots = [Path(tempfile.gettempdir())] + ([chosen] if chosen is not None else [])
    swept = sweep_stale_tmp(roots)
    if swept:
        print(f"remote-test: swept {swept} stale tmp dir(s)", flush=True)
    if root.name.startswith(WORKSPACE_PREFIX):  # legacy layout has no sibling workspaces
        for name in gc_workspaces(root.parent, keep=root):
            print(f"remote-test: removed dead workspace {name}", flush=True)

    if chosen is not None:
        unfit = preflight(chosen)
        if unfit is not None:
            print(f"remote-test: temp dir {chosen} unfit ({unfit}) — using the OS default", flush=True)
            chosen = None
    # The suite's temp traffic goes to the chosen dir when there is one, so the
    # default temp's headroom only matters when it is the one in use.
    unfit = preflight() if chosen is None else None
    if unfit is not None:
        print(f"remote-test: host not fit to run ({unfit})", file=sys.stderr, flush=True)
        return BOOTSTRAP_FAILED

    stamps = stamp_dir(python)
    expected = lock_hash(root)
    if needs_install(stamps, expected):
        print("remote-test: dependency lock changed — reinstalling remote venv", flush=True)
        if install_deps(python, root, lock) != 0:
            print("remote-test: remote pip install failed", file=sys.stderr, flush=True)
            return BOOTSTRAP_FAILED
        write_stamp(stamps, expected)

    try:
        return subprocess.run(
            [python, "-m", "pytest", *args], cwd=root, timeout=timeout or None,
            env=pytest_env(chosen),
        ).returncode
    except subprocess.TimeoutExpired:
        # pytest is a direct child here, so the expiry actually kills it —
        # unlike the local-side cap, which can only kill ssh and orphan the
        # remote run. Environment problem, not a verdict: fall back locally.
        print(
            f"remote-test: suite exceeded {timeout}s on the remote — giving up",
            file=sys.stderr, flush=True,
        )
        return BOOTSTRAP_FAILED


def is_available(cfg: RemoteConfig, timeout: int = 3) -> bool:
    """True if the remote answers promptly. Any failure means 'run locally'."""
    if shutil.which("ssh") is None:
        return False
    try:
        result = subprocess.run(
            probe_command(cfg, timeout),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


# ── runner semaphore (local side) ──────────────────────────────────────────────
#
# One file per running dispatch, named by PID, in a directory every checkout on
# this machine resolves to the same place. Chosen over flock: there is no
# counting flock, and fcntl is POSIX-only while this module — and its tests —
# also load on the Windows runner. Two reapers make a stale file self-heal: a
# slot whose PID is gone goes at the next count, and a slot older than the run
# could possibly last goes whatever its PID says (PIDs are reused after a
# reboot; the age check does not care).

# Indirections so the tests can drive the wait with a fake clock instead of
# sleeping through it.
_clock = time.monotonic
_wall = time.time
_sleep = time.sleep


def slot_dir(root: Path = ROOT) -> Path | None:
    """Where this machine's checkouts count their runs, or None outside git.

    The git common dir is shared by every worktree of the prod checkout, so a
    gate in any session sees every other session's slot. None means the runs
    cannot be coordinated from here; the caller dispatches uncounted rather
    than refusing, since an unbounded run is the old behaviour, not a new
    failure.
    """
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    return Path(common) / SLOT_DIR_NAME


def pid_alive(pid: int) -> bool:
    """Whether a slot's owning process still exists.

    Only ever asked on the machine that gates (Linux). On Windows os.kill
    with any signal *terminates* the target, so there the answer is always
    "alive" and the age check is the only reaper — the tests run there, the
    semaphore does not.
    """
    if sys.platform == "win32":  # pragma: no cover — exercised on the runner only
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False  # larger than any PID: a stray file, not a live process
    except OSError:
        # EPERM: exists, owned by someone else. Anything odder: assume alive,
        # since a wrongly reaped slot is worse than a wrongly held one.
        return True
    return True


def slot_max_age(cfg: RemoteConfig, cap: int | None = None) -> int:
    """Seconds after which a slot is abandoned regardless of its PID; 0 = never.

    *cap* is the wall-clock limit of the run that will hold the slot; it
    defaults to the pytest cap, but a caller with its own limit (exec_remote's
    `timeout`, or none at all) passes that, so a legitimately long run is not
    reaped out from under itself.
    """
    cap = cfg.timeout if cap is None else cap
    return cap + 60 + SLOT_AGE_GRACE if cap else 0


def live_slots(directory: Path, max_age: int = 0) -> list[tuple[float, int]]:
    """Every slot still held, as (claimed-at, pid) oldest first — reaping the rest.

    Anything that is not a PID-named file is ignored, never deleted.
    """
    now = _wall()
    held: list[tuple[float, int]] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return held
    for path in entries:
        if not (path.name.isascii() and path.name.isdigit()):
            continue
        pid = int(path.name)
        if not 0 < pid <= MAX_PID:
            continue  # a stray file, not a process on any platform
        try:
            claimed = path.stat().st_mtime
        except OSError:
            continue  # released between listing and stat
        stale = (max_age and now - claimed > max_age) or not pid_alive(pid)
        if stale:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # not ours to reap (another user's file): count it as gone
            continue
        held.append((claimed, pid))
    held.sort()
    return held


def _claim(path: Path) -> bool:
    """Create the slot file atomically; False if it already exists."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def wait_for_slot(
    cfg: RemoteConfig, directory: Path, label: str, stream=None, cap: int | None = None,
) -> Path | None:
    """Block until this process holds one of cfg.max_runs slots, or give up.

    Returns the held slot path (the caller unlinks it when done), or None once
    cfg.max_wait has elapsed without one — the signal to run locally.

    Two gates starting together can both count N-1 holders and both claim;
    every claimant therefore recounts after claiming and, if it ranks past the
    ceiling by claim time, withdraws and retries after a short jittered pause,
    so the earlier claim keeps its slot.
    """
    stream = stream or sys.stdout
    directory.mkdir(parents=True, exist_ok=True)
    mine = directory / str(os.getpid())
    # A file with our PID can only be a dead predecessor's: we own the PID now.
    mine.unlink(missing_ok=True)

    max_age = slot_max_age(cfg, cap)
    started = _clock()
    deadline = started + cfg.max_wait
    last_report = started
    announced = False
    while True:
        holders = live_slots(directory, max_age)
        if len(holders) < cfg.max_runs:
            if _claim(mine):
                order = [pid for _, pid in live_slots(directory, max_age)]
                # Our own file missing from the recount means someone reaped
                # it; treat that as a lost claim and go round again.
                if os.getpid() in order and order.index(os.getpid()) < cfg.max_runs:
                    if announced:
                        print(f"{label}: slot free after {int(_clock() - started)}s.",
                              file=stream, flush=True)
                    return mine
                mine.unlink(missing_ok=True)
            if _clock() >= deadline:
                return None
            _sleep(0.5 + random.random())
            continue

        now = _clock()
        if now >= deadline:
            return None
        if not announced:
            print(
                f"{label}: {cfg.host} is running {len(holders)} of {cfg.max_runs} "
                f"— waiting up to {cfg.max_wait}s for a slot.",
                file=stream, flush=True,
            )
            announced = True
        elif now - last_report >= SLOT_PROGRESS_EVERY:
            print(f"{label}: still waiting ({int(now - started)}s).", file=stream, flush=True)
            last_report = now
        _sleep(min(SLOT_POLL_SECONDS, deadline - now))


@contextmanager
def runner_slot(
    cfg: RemoteConfig, label: str, stream=None, cap: int | None = None,
) -> Iterator[bool]:
    """Hold a slot on the host for the block; yields False when none came free.

    Unlimited (max_runs 0) and a checkout outside git both yield True without
    counting, so this can never be the reason a dispatch fails outright.
    """
    directory = slot_dir() if cfg.max_runs else None
    if directory is None:
        yield True
        return
    try:
        held = wait_for_slot(cfg, directory, label, stream, cap)
    except OSError as exc:
        # An unwritable slot directory (read-only .git, another user's files)
        # must not fail the gate: dispatch uncounted, as with no git dir.
        print(f"{label}: cannot count runs ({exc}) — dispatching uncounted.",
              file=stream or sys.stdout, flush=True)
        yield True
        return
    if held is None:
        yield False
        return
    try:
        yield True
    finally:
        held.unlink(missing_ok=True)


def manifest_lines(paths: Sequence[str] = SYNC_PATHS, root: Path = ROOT) -> list[str]:
    """Every file the sync ships, as forward-slash paths relative to the root.

    Shipped alongside the tree so the remote can delete anything it still has
    that we did not send. tar only ever adds and overwrites, so without this a
    file removed from a branch — or belonging to a branch tested here earlier —
    survives indefinitely and runs as a phantom test.
    """
    out: list[str] = []
    for entry in paths:
        target = root / entry
        if target.is_file():
            out.append(entry)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            out.append(path.relative_to(root).as_posix())
    return out


def sync(cfg: RemoteConfig) -> bool:
    """Ship the source tree into this checkout's workspace.

    Returns False if the transfer failed, which the caller treats as "run
    locally" rather than reporting a broken transfer as a test failure.
    """
    # Extract at the base: the archive carries the workspace directory as its
    # own prefix, so tar creates it and no remote mkdir is needed.
    extract = remote_command(cfg, "tar -xzf -", base=bool(cfg.workspace))

    with TemporaryDirectory() as staging:
        manifest = Path(staging) / MANIFEST_FILE
        manifest.write_text("\n".join(manifest_lines()) + "\n", encoding="utf-8")
        argv = tar_command(prefix=cfg.workspace)
        # A second -C switches directory for the members that follow, so the
        # manifest joins the stream without ever being written into the repo.
        argv += ["-C", staging, MANIFEST_FILE]

        tar = subprocess.Popen(argv, stdout=subprocess.PIPE)
        assert tar.stdout is not None
        try:
            pushed = subprocess.run(extract, stdin=tar.stdout)
        finally:
            tar.stdout.close()
            tar.wait()
    return pushed.returncode == 0 and tar.returncode == 0


def exec_remote(
    command: str,
    env: Mapping[str, str] | None = None,
    *,
    timeout: int | None = None,
) -> int | None:
    """Run one arbitrary command in the synced workspace on the test host.

    The same contract as :func:`run`, and for the same reason: **None means
    "do it locally"**. Not configured, host down, sync failed, transport
    broken — every one of those is a reason to fall back, never to fail.

    This exists because the transport was never really about pytest. It
    ships the tree and runs a string in it; `scripts/mahjong_sim.py --remote`
    is the first caller that wants the fast machine without being a test
    suite. Deliberately *not* routed through ``--bootstrap``: a caller that
    only needs the standard library should not trigger a multi-gigabyte
    dependency install to check whether the venv is stale.

    The command runs in the workspace, so it sees exactly the tree that was
    just synced — which means any file it reads must be one of SYNC_PATHS.
    """
    try:
        cfg = load_config(env)
    except ValueError as exc:
        print(f"remote-exec: {exc}", file=sys.stderr)
        return None

    if cfg is None:
        return None

    if not is_available(cfg):
        print(
            f"remote-exec: {cfg.host} unreachable — running locally.",
            file=sys.stderr, flush=True,
        )
        return None

    # Every line this function prints goes to stderr, unlike run()'s: a
    # dispatched command's stdout is its *result*, and callers pipe it
    # (`--json > report.json`). Chatter on stdout corrupted that.
    # A sim run counts against the host's slots like a gate does: it is the
    # same load, and a gate arriving behind it waits or goes local.
    with runner_slot(cfg, "remote-exec", sys.stderr, cap=timeout or 0) as held:
        if not held:
            print(
                f"remote-exec: {cfg.host} still full after {cfg.max_wait}s — "
                "running locally.", file=sys.stderr,
            )
            return None
        print(f"remote-exec: dispatching to {cfg.host}", file=sys.stderr, flush=True)
        if not sync(cfg):
            print("remote-exec: sync failed — retrying once.", file=sys.stderr)
            if not sync(cfg):
                print("remote-exec: sync failed — running locally.", file=sys.stderr)
                return None

        try:
            code = subprocess.run(
                remote_command(cfg, command), timeout=timeout
            ).returncode
        except subprocess.TimeoutExpired:
            print(
                f"remote-exec: no result after {timeout}s — running locally.",
                file=sys.stderr,
            )
            return None

    if code in (255, -1) or code < 0:
        # 255 is ssh losing the connection; a negative code is ssh killed by
        # a signal. Neither is the command's verdict.
        print(
            f"remote-exec: transport failed (exit {code}) — running locally.",
            file=sys.stderr,
        )
        return None
    return code


def remote_python(env: Mapping[str, str] | None = None) -> str | None:
    """The interpreter path on the test host, for building a command."""
    try:
        cfg = load_config(env)
    except ValueError:
        return None
    return cfg.python if cfg else None


def remote_jobs(env: Mapping[str, str] | None = None) -> int | None:
    """The worker count configured for the test host."""
    try:
        cfg = load_config(env)
    except ValueError:
        return None
    return cfg.jobs if cfg else None


def run(args: Sequence[str], env: Mapping[str, str] | None = None) -> int | None:
    """Run pytest remotely.

    Returns the remote exit code, or **None** when the caller should fall back
    to running locally (not configured, host down, sync failed, or an argument
    that could not be safely quoted).
    """
    try:
        cfg = load_config(env)
    except ValueError as exc:
        print(f"remote-test: {exc}", file=sys.stderr)
        return None

    if cfg is None:
        return None

    try:
        check_args(args)
    except UnsafeArgument as exc:
        print(f"remote-test: {exc} — running locally.", file=sys.stderr)
        return None

    if not is_available(cfg):
        print(f"remote-test: {cfg.host} unreachable — running locally.", flush=True)
        return None

    # The slot spans sync as well as the run: the tar extract is load on the
    # host too, and one try/finally around both is the whole release story.
    with runner_slot(cfg, "remote-test") as held:
        if not held:
            print(
                f"remote-test: {cfg.host} still full after {cfg.max_wait}s — "
                "running locally.", file=sys.stderr,
            )
            return None
        print(f"remote-test: dispatching to {cfg.host} (-n {cfg.jobs})", flush=True)
        if not sync(cfg):
            # One retry: a transient blip shouldn't cost a 10x slower local run.
            print("remote-test: sync failed — retrying once.", file=sys.stderr)
            if not sync(cfg):
                print("remote-test: sync failed — running locally.", file=sys.stderr)
                return None

        try:
            # The real cap is enforced remote-side (bootstrap kills its own
            # pytest child and exits BOOTSTRAP_FAILED — no orphan). This local
            # cap is the outer belt for an ssh session that stays alive while
            # the remote wedges *outside* pytest, so it gets a grace margin;
            # killing ssh here does orphan whatever the remote was doing.
            code = subprocess.run(
                pytest_command(cfg, args),
                timeout=(cfg.timeout + 60) if cfg.timeout else None,
            ).returncode
        except subprocess.TimeoutExpired:
            print(
                f"remote-test: no result after {cfg.timeout}s — running locally.",
                file=sys.stderr,
            )
            return None

    if code == BOOTSTRAP_FAILED:
        # The remote couldn't ready itself (stale venv install failed, or the
        # preflight found no tmp headroom). That's an environment problem,
        # not a test result — don't let it read as a red suite.
        print("remote-test: remote setup failed — running locally.", file=sys.stderr)
        return None
    if code not in PYTEST_EXIT_CODES:
        # 255 = ssh lost the connection; 128+N/-N = ssh killed by a signal.
        # The suite never reported a verdict, so falling back is honest —
        # whereas propagating this would block the commit on a network blip.
        print(
            f"remote-test: transport failed (exit {code}) — running locally.",
            file=sys.stderr,
        )
        return None
    return code


if __name__ == "__main__":
    # Only ever invoked on the remote, by pytest_command() above.
    if len(sys.argv) > 1 and sys.argv[1] == "--bootstrap":
        rest = sys.argv[2:]
        lock = DEFAULT_LOCK
        cap = 0
        tmp = ""
        while len(rest) >= 2 and rest[0] in ("--lock", "--timeout", "--tmp"):
            if rest[0] == "--lock":
                lock = rest[1]
            elif rest[0] == "--tmp":
                tmp = rest[1]
            else:
                cap = int(rest[1])
            rest = rest[2:]
        sys.exit(bootstrap(rest, lock=lock, timeout=cap, tmp=tmp))
    print(__doc__)
    sys.exit(0)
