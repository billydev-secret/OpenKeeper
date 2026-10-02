# Remote test dispatch

Run the test suite on a faster machine over SSH, falling back to local
automatically when that machine is off. Opt-in: unset `REMOTE_TEST_HOST` and
nothing changes.

> **Production-host tooling.** The runner, its SSH access and the workspace
> slots belong to the maintainer's box. A developer on their own machine runs
> the suite locally (`dev_setup.md` § 7) and never sets `REMOTE_TEST_HOST`;
> the sections on Windows portability below are still worth reading if that
> machine is Windows.

> **Where the config is found.** `.env` is gitignored, so it never travels with
> the code. `env_path()` looks in the current checkout, then at the main
> checkout via `git rev-parse --git-common-dir` (worktrees), then at a
> `remote.origin.url` that is a local filesystem path (session checkouts, which
> are *clones*: for those `--git-common-dir` returns the clone's own `.git`, so
> the worktree fallback lands back where it started). The clone case was missing
> until 2026-07-23, and because "no config" legitimately means "run locally",
> every gate run in a session checkout silently took the slow path instead of
> reporting anything.

The prod box is a 2-core VM (a Synology VMM guest sharing an embedded Ryzen),
where the full suite takes ~10 minutes. A spare desktop cuts that to a few.

## Why native Windows is fine

The suite is portable, which was checked rather than assumed:

- **Zero** POSIX-only stdlib imports (`fcntl`, `pwd`, `grp`, `resource`, …)
- Exactly one `open()` without an explicit `encoding=`, and it is binary mode
- `lavalink_manager.py` is already cross-platform (CTRL_BREAK_EVENT vs SIGTERM)
- `gate.py` already looks for `.venv/Scripts/python.exe`
- Every dependency resolves for cp314 on Windows, `mediapipe`, `onnxruntime`,
  `ctranslate2` and `pynacl` included. Verify with:

  ```bash
  uv pip compile pyproject.toml --group dev -o /tmp/win.lock -p 3.14 \
      --python-platform x86_64-pc-windows-msvc
  ```

**Nothing secret is synced, and nothing secret is needed.** No database and no
model weights are required, and a plain `git clone` is a complete test host.

The suite does, however, *inherit* a `.env` where one exists:
`bot_modules.core.config` calls `load_dotenv(override=True)` at module scope,
so importing almost anything merges it into `os.environ`, which is why a test
could pass here and fail in the production checkout. `tests/conftest.py`
scrubs those settings back out for every test (`SCRUBBED_ENV_VARS`, gated by
`tests/tooling/test_env_hermeticity.py`), so the runner and the prod box agree. See
`docs/web_testing.md` § Environment hermeticity. A runner still needs no
secrets: the point of the scrub is that having them changes nothing.

### The known risk

Windows cannot delete an open file. With ~16,000 tests creating temp SQLite
databases, any test leaving a connection open fails at `tmp_path` teardown
rather than in the test body. Relatedly, `xdist` uses spawn rather than fork on
Windows, so fixtures relying on fork-inherited state behave differently.

Neither is knowable without running it. If teardown failures turn out to be
widespread, switch to WSL2 (below) rather than fighting them.

### A real gotcha found this way: don't walk `app.routes` directly

The remote surfaced a genuine bug the local dev venv was masking: two web
tests (`test_authz_sweep.py`, `test_snowflake_precision.py`) enumerated routes
via `for route in app.routes: if isinstance(route, APIRoute): ...`. That
assumption broke silently once the pinned `fastapi`/`starlette` versions
diverged from whatever the local venv happened to have installed.
`include_router` doesn't reliably flatten a sub-router's routes into
`app.routes` as plain `APIRoute` instances across versions (a newer FastAPI
can defer them behind an internal lazy wrapper instead), so the walk found
almost nothing and the authz sweep passed *vacuously*: a security test that
looked green while checking zero routes. The fix: enumerate routes via
`app.openapi()["paths"]` instead, the same public schema Swagger UI and real
clients use, verified identical (300 paths, 358 `/api` operations) on both the
old and new FastAPI. This is exactly the kind of drift a stale local venv
hides and a remote running the actually-pinned lock file catches.

Remember the signal here is a **fast pre-filter**, not proof: prod is Linux, and
CI on push remains the authoritative gate. A Linux-only bug can pass here.

## One workspace per checkout

Several session checkouts (each a clone of the main repo) share one test host.
Each syncs into its own sub-directory of `REMOTE_TEST_DIR`, named
`ws-<basename>-<hash-of-abs-path>`, so two checkouts never extract over each
other and a run in one can't corrupt another's tree.

Two properties this fixes, both learned the hard way when a stale `src/`
produced 22 failures that didn't reproduce locally:

- **The sync is authoritative.** It ships a `.remote-manifest` listing every
  file sent, and the remote deletes anything under the synced roots that isn't
  on it. The tar stream only ever adds and overwrites, so before this a file
  removed from a branch, or belonging to a different branch tested here earlier,
  lingered forever and ran as a phantom test. Only the synced roots (`src`,
  `tests`, `scripts`, …) are pruned; the venv, `.git` and everything else are
  never touched.
- **The venv is shared, not per-workspace.** The freshness stamp lives beside
  the interpreter (`REMOTE_TEST_PYTHON`), not in a workspace, so a multi-GB
  reinstall doesn't repeat for every checkout, only when the lock files
  actually change.

`REMOTE_TEST_WORKSPACE` overrides the directory name; set it to `off` to sync
straight over `REMOTE_TEST_DIR` (the pre-workspace layout), which also disables
pruning.

## Setting up the remote (native Windows)

The fastest route is to run a Claude Code session *on the Windows box* and let
it do the setup locally. Installing Python, resolving pip failures, and
triaging the first run all want fast filesystem access, and SSH isn't up yet.

1. Install **Python 3.14**, **git**, and the **OpenSSH Server** optional feature
   (Settings → System → Optional features → OpenSSH Server), then
   `Start-Service sshd; Set-Service -Name sshd -StartupType Automatic`.
2. Add your public key to `C:\Users\<you>\.ssh\authorized_keys`. Key auth is
   required, not optional: the runner uses `BatchMode=yes`, so a password prompt
   makes it fall back to local instead of hanging a commit hook.
3. Clone the repo and build the venv:

   ```powershell
   git clone <repo> C:\dev\dungeon-keeper
   cd C:\dev\dungeon-keeper
   py -3.14 -m venv .venv
   .venv\Scripts\pip install -r requirements/dev.lock
   ```

   If that pin has no Windows wheel, compile a Windows-specific lock with the
   `uv` command above and use it instead.
4. Confirm it runs: `.venv\Scripts\python -m pytest -n 12 -x`

## Configuration

Set these where your shell will see them (the pre-commit hook inherits them):

| Variable | Meaning |
|---|---|
| `REMOTE_TEST_HOST` | `user@host`. **Unset ⇒ everything runs locally.** |
| `REMOTE_TEST_DIR` | Repo path on that host, e.g. `C:/dev/dungeon-keeper` |
| `REMOTE_TEST_PYTHON` | e.g. `C:/dev/dungeon-keeper/.venv/Scripts/python.exe` |
| `REMOTE_TEST_JOBS` | xdist workers, default 12, which leaves a desktop usable |
| `REMOTE_TEST_CD` | Override the `cd` template, e.g. `cd /d {dir} && {cmd}` |
| `REMOTE_TEST_TIMEOUT` | Wall-clock cap in seconds on the remote pytest run, enforced remote-side (expiry kills pytest there and falls back locally, with no orphaned run); the local ssh allows the cap + 60s grace as an outer belt. 0/unset ⇒ no cap. |
| `REMOTE_TEST_TMP` | Directory on the remote for the suite's temp files, e.g. `T:/dk-tmp` on a RAM disk (forward slashes; a backslash is refused like any other unsafe character). Shipped as `--tmp`; used only if it can be created and has headroom, else the OS default temp with one log line. Unset ⇒ the OS default. |
| `REMOTE_TEST_MAX_RUNS` | How many runs may use the host at once across every checkout on this machine, default 3 (three 8-worker runs fill the 24-CPU runner). A run past the ceiling waits for a slot. 0 ⇒ unlimited. See § Sharing the runner. |
| `REMOTE_TEST_MAX_WAIT` | Longest wait for a slot in seconds, default 600. Expiry runs locally, so a busy host slows a commit but never blocks it. 0 ⇒ never wait: local at once when the host is full. |
| `GATE_NO_REMOTE=1` | Force local for one run |

Forward slashes work fine in Windows paths here. Use `REMOTE_TEST_CD` with
`cd /d` only if the repo sits on a different drive than the SSH session's
default.

Setting `REMOTE_TEST_HOST` **without** `DIR` and `PYTHON` is a hard error rather
than a quiet fallback: a half-configured remote that silently never dispatches
is worse than one that complains.

## How it works

`gate.py` routes all three of its pytest call sites through `run_pytest()`,
which asks `scripts/remote_test.py` first and falls back locally whenever it
returns `None`:

1. Probe with `ssh -o ConnectTimeout=3 -o BatchMode=yes`
2. Ship `src/ tests/ scripts/ docs/ assets/fonts/ pyproject.toml README.md`
   and the two lock files via a **tar pipe** (no rsync, which native Windows
   lacks). A few MB, ~1-2s on a LAN. docs/ and the fonts are test inputs (the
   party-game tripwire reads `docs/features.md`, the quote renderer needs the
   fonts); the rest of assets/ is deliberately not shipped.
3. Run `python -m pytest -n <jobs> <targets>` over SSH, streaming output
4. Return the remote exit code

Every "can't dispatch" path returns `None` → local run: host unset, host
unreachable, no free slot within `REMOTE_TEST_MAX_WAIT`, sync failed (after
one retry), `GATE_NO_REMOTE`, an argument that cannot be safely embedded in
the remote command string, the `REMOTE_TEST_TIMEOUT` cap expiring, or an exit
code outside pytest's 0-5
(ssh's 255 for a lost connection, 128+N for a signal-killed ssh; the suite
never reported a verdict, so a network blip must not read as a red run). A
remote *test failure* is different: that propagates and fails the gate, as
it should.

The pytest ssh session runs with `ServerAliveInterval=15` /
`ServerAliveCountMax=4`, so a connection that dies mid-run surfaces as exit
255 within ~60 seconds, and falls back locally, instead of hanging the
commit hook on a silent TCP session.

### Sharing the runner

Every session worktree on the gating machine reads the same `.env`, so every
scoped gate, and `scripts/mahjong_sim.py --remote`, which goes through the
same transport, lands on the same host. Each xdist worker is its own
~300 MB `python.exe`, and on 2026-09-24 two unbounded runs stalled at 98% and
left 58 of them stranded for six days. The transport now holds a **bounded
semaphore** around sync + run:

- **Slots are files named by PID** in `<git common dir>/remote-test-slots/`.
  The common dir is shared by every worktree of the prod checkout (the ship
  flow already keeps `dk-ship.lock` there), which is what makes one session's
  run visible to all the others; `/tmp` was rejected because its shared state
  has bitten the browser gate before (`docs/web_testing.md`). Outside a git
  checkout there is nowhere shared to count, and the run dispatches uncounted,
  which is the old behavior, not a new failure.
- **A run past `REMOTE_TEST_MAX_RUNS` waits**, polling every 5 s and reporting
  every 30 s, up to `REMOTE_TEST_MAX_WAIT`; then it **runs locally**, exactly
  like every other can't-dispatch case. Waiting happens *after* the probe and
  *before* the sync, so a host that is down never makes anyone wait, and the
  slot covers the tar extract as well as the tests (both are load there).
- **Stale slots self-heal.** A slot whose PID no longer exists is reaped by
  the next run to count, so a gate killed mid-run (Ctrl-C on a commit, a
  crashed hook) frees its slot at once; a normal exit or an exception releases
  it in a `finally`. Belt-and-braces, a slot older than the remote cap plus
  six minutes is reaped whatever its PID says, which also covers PID reuse
  after a reboot. With `REMOTE_TEST_TIMEOUT` unset only the PID check applies.
  A sim run's slot is aged against *its* own timeout (`exec_remote`'s
  argument), so a long sim is not reaped out from under itself. A slot
  directory that cannot be written (a read-only `.git`, another user's files)
  is reported and the run dispatches uncounted rather than failing the gate.
- **Two runs starting together** can both count one short and both claim.
  Every claimant recounts after claiming, and the one that ranks past the
  ceiling by claim time withdraws and retries after a jittered half-second,
  so the earlier claim keeps its slot.

The count is local to this machine on purpose: a runner-side lock would also
cover runs launched from elsewhere, but nothing else launches them, and it
would cost an ssh round-trip per poll and a second way for the host to be
"down". The bounded count plus the wait ceiling is a *policy*, not a diagnosis
of what broke on 09-24 (that is still unknown), so it stays a dial.

### Argument safety

Arguments containing spaces, quotes, or shell metacharacters are **refused**
rather than quoted, because correct quoting differs between cmd.exe,
PowerShell, and bash. `-k spam and eggs` falls back to local. Use
`-ktest_foo` (no spaces) to keep dispatch, or `GATE_NO_REMOTE=1`.

A target list longer than `MAX_TARGET_CHARS` (6000) is **collapsed**: its
`tests/...` paths become the single target `tests`, because cmd.exe refuses a
command line over 8191 characters and a diff touching hundreds of files (a
layout stage) selects hundreds of test files. The whole suite is a superset of
the selection, so the gate loses nothing; it just runs longer. Options and a
path that is an option's value (`--deselect tests/a.py::t`) are kept as given.

### Keeping the remote venv in sync

Handled automatically. Both lock files are synced, and the remote command runs
`scripts/remote_test.py --bootstrap` rather than pytest directly. On the far
side that:

1. Hashes `requirements/base.lock` + `requirements/dev.lock`
2. Compares against `.remote-test-stamp` in the remote checkout (gitignored)
3. Reinstalls from `requirements/dev.lock` and rewrites the stamp if they differ
4. Runs pytest

So a Dependabot bump or a local `uv pip compile` is picked up on the next
dispatch, so you can't silently test new code against old dependencies.

The check lives on the remote deliberately: reading a file over SSH would need
a shell one-liner, and `cat` (bash) vs `type` (cmd.exe) diverge. Running plain
Python there sidesteps the whole quoting problem.

If the reinstall fails, the remote exits with a **sentinel code (97)**, chosen
outside pytest's 0-5 range, and the local side falls back to running the suite
locally. A broken remote venv is an environment problem, not a red suite. A
genuine test failure still propagates and fails the gate.

The first dispatch after setup will always reinstall, since no stamp exists yet.

### Remote housekeeping and preflight

The remote box once filled its tmp with leftover pytest session dirs and the
suite sprayed hundreds of unrelated sqlite errors that looked like a red run.
`--bootstrap` now defends the host before running anything:

1. **Sweeps stale tmp**: session dirs under `pytest-of-<user>` and
   per-worker `dk-db-template-*` dirs (`tests/db_template.py`; a killed
   worker never reaches the atexit that removes its own) older than 2 hours
   are deleted, under both the OS temp and `REMOTE_TEST_TMP` (age-gated, so a
   concurrent run from another checkout is never touched).
2. **GCs dead workspaces**: sibling `ws-*` directories untouched for 30 days
   belong to deleted session checkouts and are removed (skipped in the legacy
   `REMOTE_TEST_WORKSPACE=off` layout).
3. **Preflights headroom**: under 2 GiB free (or 100k free inodes, where the
   OS exposes them) in tmp exits with the sentinel (97), so an unfit host
   becomes a clean local fallback instead of a wall of bogus failures. A
   `REMOTE_TEST_TMP` without headroom is dropped in favor of the OS temp
   first; only the OS temp being unfit, when it is the one in use, ends the
   run (a usable `REMOTE_TEST_TMP` skips the OS-temp check).

### The suite's temp on a RAM disk

The runner's cores idled while its SSD sat at 100%: ~16k tests, 317 files
of them on a file-backed DB, each copying a ~3 MB template, writing WAL
frames and checkpointing on close, adding up to tens of GB of small writes and fsyncs per
full run. `REMOTE_TEST_TMP` moves all of it. The bootstrap sets `TMPDIR`,
`TEMP` and `TMP` in the pytest child's environment only; `tempfile.gettempdir()`
honors them on every platform, and both pytest's `tmp_path` root and the
template dirs derive from it, so nothing needs `--basetemp` (which would
defeat `tmp_path_retention_count`). The ImDisk-style drive is not listed by
`Get-Volume`, so the check is "can a file be created there", not "does the
volume exist". Whether `T:` survives a reboot is unknown: a missing drive is
one log line and a slower run on the SSD, never a failed gate.

Two things were deliberately left alone. `PYTHONPYCACHEPREFIX` would also
move bytecode writes off the SSD, but it makes Python ignore every existing
`__pycache__`, so the whole venv would recompile into RAM on the first run and
again after any reboot that wipes the disk, while the tar sync preserves
mtimes, so unchanged sources keep their cached `.pyc` anyway. And the
template DB is not shrunk: `VACUUM` saves 2.5% (measured).

The complementary half is in the suite itself: `tests/conftest.py` opens
every SQLite connection with `synchronous=OFF` and the `open_db` helpers skip
optimize-on-close under `DK_SQLITE_TEST_PROFILE` (only honored beside
`PYTEST_CURRENT_TEST`, so a bot can never inherit it). WAL stays, so lock
semantics match production. That part helps every host, RAM disk or not.

## WSL2 fallback

If Windows-native teardown failures prove widespread, WSL2 gives an identical
Linux userland. Fedora is now an official WSL distro, so you can match prod
exactly (`wsl --install FedoraLinux-42`, then `dnf upgrade`). Two rules:

- **Keep the repo in WSL's ext4 (`~/`), never `/mnt/c`**: cross-filesystem I/O
  is roughly 10× slower and this suite is heavy on temp SQLite files. Getting
  this wrong erases the entire speedup.
- Set `networkingMode=mirrored` in `.wslconfig` so WSL shares the host IP.
  Otherwise its NAT address changes every boot. Don't use `netsh portproxy`.

The runner needs no changes for WSL, only different `REMOTE_TEST_*` values.
